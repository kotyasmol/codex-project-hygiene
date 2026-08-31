#!/usr/bin/env python3
"""Metadata-only workspace hygiene audit.

This helper deliberately has no mutation command.  It inventories one bounded
project root through directory file descriptors, groups high-signal clutter
patterns, and writes only to stdout.  Names and metadata are evidence of shape,
not evidence that an item belongs to Codex or may be deleted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import cleanup as core


REPORT_KIND = "codex-workspace-hygiene-report"
SCHEMA_VERSION = 1

DEFAULT_MAX_ENTRIES = 100_000
HARD_MAX_ENTRIES = 200_000
DEFAULT_MAX_CHILDREN = 20_000
HARD_MAX_CHILDREN = 50_000
DEFAULT_MAX_DEPTH = 64
HARD_MAX_DEPTH = 128
DEFAULT_MAX_SECONDS = 30.0
HARD_MAX_SECONDS = 300.0
MAX_FINDINGS = 100
MAX_FINDING_PATHS = 10
MAX_WARNINGS = 200
MAX_SPACE_CANDIDATES = 100
MAX_PATH_BYTES = 16 * 1024 * 1024
MAX_OUTPUT_PATH_BYTES = 256 * 1024
MAX_CANDIDATE_PATH_BYTES = 64 * 1024

PROTECTED_METADATA_NAMES = {".git", ".hg", ".svn", ".codex", ".agents"}
PROTECTED_DELIVERABLE_NAMES = {"output", "outputs", "assets"}
SCRATCH_NAMES = {
    "tmp",
    "temp",
    "scratch",
    "staging",
    "drafts",
    "generated",
    "processed",
    "results",
    "work",
}
MACHINE_CACHE_DIRECTORY_NAMES = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".pyre",
    ".hypothesis",
    ".vite",
    ".turbo",
    ".parcel-cache",
}
MACHINE_CACHE_FILE_NAMES = {".DS_Store"}
LOCKFILE_NAMES = {
    "package-lock.json",
    "npm-shrinkwrap.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "bun.lock",
    "bun.lockb",
}
ARTIFACT_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
    ".svg",
    ".pdf",
    ".pptx",
    ".docx",
    ".zip",
    ".mp4",
    ".mov",
}
OPAQUE_NAME = re.compile(
    r"^(?:img|image|file|output|result|untitled|new|copy)[-_ ]?[0-9a-f-]*$",
    re.IGNORECASE,
)
VERSION_SUFFIX = re.compile(
    r"^(?P<base>.+?)(?:[-_.](?:v|ver|version)(?P<number>[2-9][0-9]*)|"
    r"[-_.](?P<label>final(?:[-_.]final)+|copy(?:[-_.][0-9]+)?|new(?:[-_.][0-9]+)?))$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Budgets:
    max_entries: int = DEFAULT_MAX_ENTRIES
    max_children: int = DEFAULT_MAX_CHILDREN
    max_depth: int = DEFAULT_MAX_DEPTH
    max_seconds: float = DEFAULT_MAX_SECONDS


@dataclass
class EntryRecord:
    rel: str
    parent: str
    name: str
    kind: str
    depth: int
    logical_bytes: int
    allocated_bytes: int


@dataclass
class DirectoryRecord:
    rel: str
    parent: Optional[str]
    name: str
    depth: int
    own_allocated_bytes: int
    own_logical_bytes: int
    children: list[str] = field(default_factory=list)
    direct_entries: int = 0
    direct_files: int = 0
    direct_symlinks: int = 0
    direct_special: int = 0
    direct_logical_bytes: int = 0
    direct_allocated_bytes: int = 0
    total_files: int = 0
    total_directories: int = 1
    total_symlinks: int = 0
    total_special: int = 0
    total_logical_bytes: int = 0
    total_allocated_bytes: int = 0
    empty_directories: int = 0
    enumeration_complete: bool = True
    excluded: bool = False


class AuditState:
    def __init__(self, root: Path, budgets: Budgets) -> None:
        self.root = root
        self.budgets = budgets
        self.started = time.monotonic()
        self.entries_examined = 0
        self.path_bytes = 0
        self.partial = False
        self.stop = False
        self.warnings: list[dict] = []
        self.excluded_trees: list[str] = []
        self.mounts_skipped = 0
        self.races_skipped = 0
        self.permission_skipped = 0
        self.directories: dict[str, DirectoryRecord] = {}
        self.entries: list[EntryRecord] = []
        self.entries_by_parent: dict[str, list[EntryRecord]] = {}
        self.warning_output_truncated = False

    def warn(self, code: str, rel: str, error: Optional[OSError] = None) -> None:
        self.partial = True
        if len(self.warnings) >= MAX_WARNINGS:
            self.warning_output_truncated = True
            return
        item = {"code": code, "path": rel}
        if error is not None and error.errno is not None:
            item["errno"] = int(error.errno)
        if item not in self.warnings:
            self.warnings.append(item)

    def budget_entry(self, rel: str) -> bool:
        if self.stop:
            return False
        if time.monotonic() - self.started > self.budgets.max_seconds:
            self.warn("wall-clock-budget-exceeded", rel)
            self.stop = True
            return False
        if self.entries_examined >= self.budgets.max_entries:
            self.warn("entry-budget-exceeded", rel)
            self.stop = True
            return False
        encoded = len(os.fsencode(rel))
        if self.path_bytes + encoded > MAX_PATH_BYTES:
            self.warn("path-byte-budget-exceeded", rel)
            self.stop = True
            return False
        self.entries_examined += 1
        self.path_bytes += encoded
        return True


def allocated_from_stat(info: os.stat_result) -> int:
    blocks = getattr(info, "st_blocks", None)
    return int(blocks * 512) if blocks is not None else int(info.st_size)


def path_join(parent: str, name: str) -> str:
    return name if parent == "." else f"{parent}/{name}"


def path_depth(rel: str) -> int:
    return 0 if rel == "." else rel.count("/") + 1


def is_below_name(rel: str, names: set[str]) -> bool:
    return any(part.casefold() in names for part in rel.split("/"))


def has_named_ancestor(rel: str, names: set[str]) -> bool:
    """Return whether a non-final relative path component has a listed name."""
    parts = rel.split("/")
    return any(part.casefold() in names for part in parts[:-1])


def linux_fd_mount_id(descriptor: int) -> Optional[int]:
    path = f"/proc/self/fdinfo/{descriptor}"
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        data = os.read(fd, 64 * 1024 + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    if len(data) > 64 * 1024:
        return None
    for raw_line in data.splitlines():
        if raw_line.startswith(b"mnt_id:"):
            try:
                return int(raw_line.partition(b":")[2].strip())
            except ValueError:
                return None
    return None


def open_directory_at(parent_fd: int, name: str) -> int:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    return os.open(name, flags, dir_fd=parent_fd)


def open_regular_at(parent_fd: int, name: str) -> int:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    return os.open(name, flags, dir_fd=parent_fd)


def open_root_directory(root: Path) -> int:
    """Open an absolute root component-by-component without following links."""
    flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )
    descriptor = os.open(root.anchor, flags)
    try:
        for component in root.parts[1:]:
            child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        int(left.st_dev),
        int(left.st_ino),
        stat.S_IFMT(left.st_mode),
    ) == (
        int(right.st_dev),
        int(right.st_ino),
        stat.S_IFMT(right.st_mode),
    )


def validate_root(raw: Path) -> tuple[Path, int]:
    if not (sys.platform == "darwin" or sys.platform.startswith("linux")):
        raise ValueError("workspace hygiene audit is unsupported on this platform")
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        raise ValueError("required no-follow directory primitives are unavailable")
    expanded = Path(os.path.abspath(os.path.expanduser(str(raw))))
    if core.has_symlink_component(expanded):
        raise ValueError("root or one of its existing ancestors is a symlink")
    # Keep the descriptor opened from the user's original component chain alive
    # through path-policy validation.  A concurrent ancestor swap can therefore
    # neither redirect the scan nor make validation bless a different inode.
    descriptor = open_root_directory(expanded)
    try:
        opened = os.fstat(descriptor)
        validated = core.validate_scan_root(expanded)
        current = os.stat(validated, follow_symlinks=False)
        if core.has_symlink_component(expanded) or not same_identity(opened, current):
            raise ValueError("root changed during policy validation")
        return validated, descriptor
    except BaseException:
        os.close(descriptor)
        raise


def scan_metadata(root: Path, budgets: Budgets, root_fd: int) -> AuditState:
    state = AuditState(root, budgets)
    root_info = os.fstat(root_fd)
    root_mount_id = linux_fd_mount_id(root_fd) if sys.platform.startswith("linux") else None
    if sys.platform.startswith("linux") and root_mount_id is None:
        state.warn("root-mount-id-unavailable", ".")

    state.directories["."] = DirectoryRecord(
        rel=".",
        parent=None,
        name=root.name,
        depth=0,
        own_allocated_bytes=allocated_from_stat(root_info),
        own_logical_bytes=int(root_info.st_size),
    )

    def visit_directory(descriptor: int, rel: str) -> None:
        record = state.directories[rel]
        try:
            before = os.fstat(descriptor)
            child_count = 0
            try:
                iterator = os.scandir(descriptor)
            except OSError as error:
                record.enumeration_complete = False
                state.permission_skipped += 1
                state.warn("directory-open-failed", rel, error)
                return
            with iterator:
                for entry in iterator:
                    child_count += 1
                    if child_count > budgets.max_children:
                        record.enumeration_complete = False
                        state.warn("per-directory-entry-budget-exceeded", rel)
                        break
                    name = entry.name
                    child_rel = path_join(rel, name)
                    if not state.budget_entry(child_rel):
                        record.enumeration_complete = False
                        break
                    try:
                        discovered = entry.stat(follow_symlinks=False)
                    except OSError as error:
                        state.permission_skipped += 1
                        state.warn("entry-stat-failed", child_rel, error)
                        continue

                    record.direct_entries += 1
                    mode = discovered.st_mode
                    logical = int(discovered.st_size)
                    allocated = allocated_from_stat(discovered)
                    depth = path_depth(child_rel)

                    if stat.S_ISLNK(mode):
                        record.direct_symlinks += 1
                        record.direct_logical_bytes += logical
                        record.direct_allocated_bytes += allocated
                        state.entries.append(
                            EntryRecord(child_rel, rel, name, "symlink", depth, logical, allocated)
                        )
                        continue

                    if stat.S_ISREG(mode):
                        try:
                            file_fd = open_regular_at(descriptor, name)
                        except OSError as error:
                            state.permission_skipped += 1
                            state.warn("regular-open-failed", child_rel, error)
                            continue
                        try:
                            opened = os.fstat(file_fd)
                        finally:
                            os.close(file_fd)
                        if not stat.S_ISREG(opened.st_mode) or not same_identity(discovered, opened):
                            state.races_skipped += 1
                            state.warn("regular-entry-raced", child_rel)
                            continue
                        logical = int(opened.st_size)
                        allocated = allocated_from_stat(opened)
                        record.direct_files += 1
                        record.direct_logical_bytes += logical
                        record.direct_allocated_bytes += allocated
                        state.entries.append(
                            EntryRecord(child_rel, rel, name, "file", depth, logical, allocated)
                        )
                        continue

                    if not stat.S_ISDIR(mode):
                        record.direct_special += 1
                        record.direct_logical_bytes += logical
                        record.direct_allocated_bytes += allocated
                        state.entries.append(
                            EntryRecord(child_rel, rel, name, "special", depth, logical, allocated)
                        )
                        continue

                    try:
                        child_fd = open_directory_at(descriptor, name)
                    except OSError as error:
                        state.races_skipped += 1
                        state.warn("directory-open-raced-or-denied", child_rel, error)
                        continue
                    opened = os.fstat(child_fd)
                    if not stat.S_ISDIR(opened.st_mode) or not same_identity(discovered, opened):
                        os.close(child_fd)
                        state.races_skipped += 1
                        state.warn("directory-entry-raced", child_rel)
                        continue

                    child_record = DirectoryRecord(
                        rel=child_rel,
                        parent=rel,
                        name=name,
                        depth=depth,
                        own_allocated_bytes=allocated_from_stat(opened),
                        own_logical_bytes=int(opened.st_size),
                    )
                    state.directories[child_rel] = child_record
                    record.children.append(child_rel)
                    state.entries.append(
                        EntryRecord(
                            child_rel,
                            rel,
                            name,
                            "directory",
                            depth,
                            int(opened.st_size),
                            allocated_from_stat(opened),
                        )
                    )

                    if name.casefold() in PROTECTED_METADATA_NAMES:
                        child_record.excluded = True
                        child_record.enumeration_complete = False
                        state.excluded_trees.append(child_rel)
                        os.close(child_fd)
                        continue
                    # The configured depth is the deepest path whose directory
                    # contents may be enumerated.  A directory at the boundary
                    # is reported, but deliberately not opened for traversal.
                    if depth >= budgets.max_depth:
                        child_record.enumeration_complete = False
                        state.warn("depth-budget-exceeded", child_rel)
                        os.close(child_fd)
                        continue
                    if opened.st_dev != root_info.st_dev:
                        child_record.enumeration_complete = False
                        state.mounts_skipped += 1
                        state.warn("filesystem-device-boundary", child_rel)
                        os.close(child_fd)
                        continue
                    if sys.platform.startswith("linux"):
                        child_mount_id = linux_fd_mount_id(child_fd)
                        if root_mount_id is None or child_mount_id is None:
                            child_record.enumeration_complete = False
                            state.mounts_skipped += 1
                            state.warn("mount-id-unavailable", child_rel)
                            os.close(child_fd)
                            continue
                        if child_mount_id != root_mount_id:
                            child_record.enumeration_complete = False
                            state.mounts_skipped += 1
                            state.warn("linux-mount-id-boundary", child_rel)
                            os.close(child_fd)
                            continue
                    elif os.path.ismount(root / child_rel):
                        child_record.enumeration_complete = False
                        state.mounts_skipped += 1
                        state.warn("nested-mount-boundary", child_rel)
                        os.close(child_fd)
                        continue
                    # Recurse immediately instead of retaining one descriptor
                    # for every sibling.  Open descriptors are therefore
                    # bounded by the configured depth, even for huge fanout.
                    visit_directory(child_fd, child_rel)
                    if state.stop:
                        break

            after = os.fstat(descriptor)
            before_state = (
                before.st_dev,
                before.st_ino,
                getattr(before, "st_mtime_ns", int(before.st_mtime * 1_000_000_000)),
                getattr(before, "st_ctime_ns", int(before.st_ctime * 1_000_000_000)),
            )
            after_state = (
                after.st_dev,
                after.st_ino,
                getattr(after, "st_mtime_ns", int(after.st_mtime * 1_000_000_000)),
                getattr(after, "st_ctime_ns", int(after.st_ctime * 1_000_000_000)),
            )
            if before_state != after_state:
                record.enumeration_complete = False
                state.warn("directory-changed-during-audit", rel)
        except OSError as error:
            record.enumeration_complete = False
            state.warn("directory-audit-failed", rel, error)
        finally:
            os.close(descriptor)

    visit_directory(root_fd, ".")

    for entry in state.entries:
        state.entries_by_parent.setdefault(entry.parent, []).append(entry)
    for values in state.entries_by_parent.values():
        values.sort(key=lambda value: os.fsencode(value.name))

    for record in sorted(state.directories.values(), key=lambda value: value.depth, reverse=True):
        record.total_files = record.direct_files
        record.total_directories = 1
        record.total_symlinks = record.direct_symlinks
        record.total_special = record.direct_special
        record.total_logical_bytes = record.own_logical_bytes + record.direct_logical_bytes
        record.total_allocated_bytes = record.own_allocated_bytes + record.direct_allocated_bytes
        record.empty_directories = int(
            record.enumeration_complete and not record.excluded and record.direct_entries == 0
        )
        for child_rel in record.children:
            child = state.directories[child_rel]
            record.enumeration_complete = record.enumeration_complete and child.enumeration_complete
            record.total_files += child.total_files
            record.total_directories += child.total_directories
            record.total_symlinks += child.total_symlinks
            record.total_special += child.total_special
            record.total_logical_bytes += child.total_logical_bytes
            record.total_allocated_bytes += child.total_allocated_bytes
            record.empty_directories += child.empty_directories
    return state


class FindingCollector:
    def __init__(self, state: AuditState) -> None:
        self.state = state
        self.findings: list[dict] = []
        self.path_bytes = 0
        self.truncated = False

    def add(
        self,
        *,
        code: str,
        dimension: str,
        severity: str,
        paths: list[str],
        metrics: dict,
        space_status: str,
        message: str,
        recommendation: str,
    ) -> None:
        if len(self.findings) >= MAX_FINDINGS:
            self.truncated = True
            return
        ordered_paths = sorted(dict.fromkeys(paths), key=os.fsencode)
        samples = ordered_paths[:MAX_FINDING_PATHS]
        needed = sum(len(os.fsencode(value)) for value in samples)
        if self.path_bytes + needed > MAX_OUTPUT_PATH_BYTES:
            self.truncated = True
            return
        self.path_bytes += needed
        digest = hashlib.sha256()
        digest.update(code.encode("ascii"))
        for value in ordered_paths:
            digest.update(b"\0")
            digest.update(os.fsencode(value))
        self.findings.append(
            {
                "id": f"hyg-{digest.hexdigest()[:16]}",
                "code": code,
                "dimension": dimension,
                "severity": severity,
                "paths": samples,
                "path_count": len(ordered_paths),
                "metrics": metrics,
                "space_status": space_status,
                "origin": "unknown",
                "mutation_allowed": False,
                "message": message,
                "recommendation": recommendation,
            }
        )


def record_for_entry(state: AuditState, entry: EntryRecord) -> Optional[DirectoryRecord]:
    return state.directories.get(entry.rel) if entry.kind == "directory" else None


def detect_findings(state: AuditState) -> tuple[list[dict], list[dict]]:
    collector = FindingCollector(state)
    candidate_roots: list[tuple[str, str, int]] = []
    technical_roots: list[str] = []

    directory_records = sorted(state.directories.values(), key=lambda value: os.fsencode(value.rel))
    for record in directory_records:
        if record.rel == "." or record.excluded:
            continue
        folded = record.name.casefold()
        protected = is_below_name(record.rel, PROTECTED_DELIVERABLE_NAMES)
        scratch = is_below_name(record.rel, SCRATCH_NAMES)

        if folded == ".pnpm-store":
            collector.add(
                code="project-local-package-store",
                dimension="visual-clutter",
                severity="high",
                paths=[record.rel],
                metrics={
                    "directories": record.total_directories,
                    "empty_directories": record.empty_directories,
                    "allocated_bytes_estimate": record.total_allocated_bytes,
                },
                space_status="protected" if protected else "review-required",
                message="A project-local package store creates many tool-managed paths.",
                recommendation="Use the package manager's external or global store unless the repository requires a local store.",
            )
            technical_roots.append(record.rel)
            if not protected:
                candidate_roots.append((record.rel, "package-store", record.total_allocated_bytes))

        if (
            folded in MACHINE_CACHE_DIRECTORY_NAMES
            and not has_named_ancestor(
                record.rel,
                MACHINE_CACHE_DIRECTORY_NAMES | {"node_modules", ".pnpm-store"},
            )
        ):
            collector.add(
                code="machine-cache",
                dimension="space-and-clutter",
                severity="low",
                paths=[record.rel],
                metrics={"allocated_bytes_estimate": record.total_allocated_bytes},
                space_status="protected" if protected else "review-required",
                message="This path matches a standard tool cache name; ownership and disposability remain unproven.",
                recommendation="Use the sealed cleanup scan before selecting it for Trash.",
            )
            technical_roots.append(record.rel)
            if not protected:
                candidate_roots.append((record.rel, "machine-cache", record.total_allocated_bytes))

        # Package-manager layouts contain many nested directories also named
        # node_modules.  Report only the outer logical dependency tree so the
        # audit itself does not recreate the visual noise it is explaining.
        if folded == "node_modules" and not has_named_ancestor(record.rel, {"node_modules"}):
            parent_names = {entry.name for entry in state.entries_by_parent.get(record.parent or ".", [])}
            metrics = {
                "directories": record.total_directories,
                "files": record.total_files,
                "allocated_bytes_estimate": record.total_allocated_bytes,
                "package_manifest_present": "package.json" in parent_names,
                "lockfile_present": bool(parent_names & LOCKFILE_NAMES),
            }
            technical_roots.append(record.rel)
            if protected:
                code = "dependency-inside-deliverable"
                status = "protected"
                recommendation = "Do not clean through a protected deliverable tree; separate source from handoff outputs first."
            elif scratch:
                code = "dependency-inside-scratch"
                status = "review-required"
                recommendation = "Keep task dependencies in external scratch and review this exact tree separately."
                candidate_roots.append((record.rel, "dependencies", record.total_allocated_bytes))
            else:
                code = "dependency-tree"
                status = "review-required"
                recommendation = "Use the sealed cleanup scan and repository evidence before selecting this dependency tree."
                candidate_roots.append((record.rel, "dependencies", record.total_allocated_bytes))
            collector.add(
                code=code,
                dimension="space-and-clutter",
                severity="high" if record.total_allocated_bytes >= 64 * 1024 * 1024 else "medium",
                paths=[record.rel],
                metrics=metrics,
                space_status=status,
                message="A standard dependency tree accounts for many generated paths.",
                recommendation=recommendation,
            )

        if record.depth == 1 and folded in SCRATCH_NAMES and record.direct_entries:
            collector.add(
                code="persistent-scratch-tree",
                dimension="workspace-structure",
                severity="high",
                paths=[record.rel],
                metrics={
                    "directories": record.total_directories,
                    "files": record.total_files,
                    "allocated_bytes_estimate": record.total_allocated_bytes,
                },
                space_status="review-required",
                message="A persistent top-level scratch-like tree mixes task intermediates into the durable workspace.",
                recommendation="Review exact contents; future task scratch should live outside the project.",
            )

        if (
            record.depth == 1
            and record.total_directories >= 20
            and record.enumeration_complete
            and record.empty_directories * 4 >= record.total_directories * 3
        ):
            collector.add(
                code="mostly-empty-directory-tree",
                dimension="visual-clutter",
                severity="high",
                paths=[record.rel],
                metrics={
                    "directories": record.total_directories,
                    "empty_directories": record.empty_directories,
                    "allocated_bytes_estimate": record.total_allocated_bytes,
                },
                space_status="review-required",
                message="At least three quarters of this directory tree is empty; visual impact may exceed disk impact.",
                recommendation="Review the owning tool configuration instead of deleting paths by name.",
            )

    for entry in sorted(state.entries, key=lambda value: os.fsencode(value.rel)):
        if entry.kind != "file":
            continue
        protected = is_below_name(entry.rel, PROTECTED_DELIVERABLE_NAMES)
        if (
            entry.name in MACHINE_CACHE_FILE_NAMES
            or entry.name.casefold().endswith((".pyc", ".pyo"))
        ) and not has_named_ancestor(
            entry.rel,
            MACHINE_CACHE_DIRECTORY_NAMES | {"node_modules", ".pnpm-store"},
        ):
            collector.add(
                code="machine-cache",
                dimension="space-and-clutter",
                severity="low",
                paths=[entry.rel],
                metrics={"allocated_bytes_estimate": entry.allocated_bytes},
                space_status="protected" if protected else "review-required",
                message="This file matches a standard metadata or bytecode cache name.",
                recommendation="Use the sealed cleanup scan before selecting it for Trash.",
            )
            if not protected:
                candidate_roots.append((entry.rel, "machine-cache", entry.allocated_bytes))

    for parent, entries in sorted(state.entries_by_parent.items(), key=lambda pair: os.fsencode(pair[0])):
        names = {entry.name.casefold(): entry for entry in entries}
        families: dict[tuple[str, str], list[EntryRecord]] = {}
        for entry in entries:
            stem, suffix = os.path.splitext(entry.name) if entry.kind == "file" else (entry.name, "")
            match = VERSION_SUFFIX.match(stem)
            if match is None:
                continue
            key = (match.group("base").casefold(), suffix.casefold())
            families.setdefault(key, []).append(entry)
            counterpart = f"{match.group('base')}{suffix}".casefold()
            if counterpart in names:
                families[key].append(names[counterpart])
        for family_entries in families.values():
            unique = {entry.rel: entry for entry in family_entries}
            if len(unique) < 2:
                continue
            ordered = sorted(unique.values(), key=lambda value: os.fsencode(value.rel))
            collector.add(
                code="possible-iteration-family",
                dimension="naming",
                severity="medium",
                paths=[entry.rel for entry in ordered],
                metrics={
                    "members": len(ordered),
                    "allocated_bytes_estimate": sum(
                        state.directories[entry.rel].total_allocated_bytes
                        if entry.kind == "directory"
                        else entry.allocated_bytes
                        for entry in ordered
                    ),
                },
                space_status="review-required",
                message="Sibling names look like retained iterations; the names do not prove that any member is obsolete.",
                recommendation="Keep accepted variants under semantic names and review every member explicitly.",
            )

        opaque = [
            entry
            for entry in entries
            if entry.kind == "file" and OPAQUE_NAME.match(os.path.splitext(entry.name)[0])
        ]
        if len(opaque) >= 3:
            collector.add(
                code="opaque-name-family",
                dimension="naming",
                severity="medium",
                paths=[entry.rel for entry in opaque],
                metrics={"members": len(opaque)},
                space_status="review-required",
                message="Several files use opaque sequence-like names that do not explain their durable role.",
                recommendation="Rename only accepted artifacts, with user approval, using subject and semantic role.",
            )

    root_entries = [entry for entry in state.entries_by_parent.get(".", []) if entry.kind == "file"]
    artifact_families: dict[str, list[EntryRecord]] = {}
    for entry in root_entries:
        stem, suffix = os.path.splitext(entry.name)
        if suffix.casefold() not in ARTIFACT_SUFFIXES:
            continue
        prefix = re.split(r"[-_. ]+", stem, maxsplit=1)[0].casefold()
        if prefix:
            artifact_families.setdefault(prefix, []).append(entry)
    for entries in artifact_families.values():
        if len(entries) < 4:
            continue
        collector.add(
            code="root-artifact-family",
            dimension="workspace-structure",
            severity="medium",
            paths=[entry.rel for entry in entries],
            metrics={
                "members": len(entries),
                "allocated_bytes_estimate": sum(entry.allocated_bytes for entry in entries),
            },
            space_status="review-required",
            message="A family of durable media or document artifacts is spread across the project root.",
            recommendation="Keep only accepted deliverables in the repository's existing artifact location with semantic names.",
        )

    if collector.truncated:
        state.warn("finding-output-budget-exceeded", ".")

    selected_candidates: list[dict] = []
    selected_candidate_path_bytes = 0
    for rel, category, size in sorted(
        candidate_roots,
        key=lambda value: (path_depth(value[0]), os.fsencode(value[0]), value[1]),
    ):
        if any(rel == chosen["path"] or rel.startswith(chosen["path"] + "/") for chosen in selected_candidates):
            continue
        encoded_path_bytes = len(os.fsencode(rel))
        if (
            len(selected_candidates) >= MAX_SPACE_CANDIDATES
            or selected_candidate_path_bytes + encoded_path_bytes > MAX_CANDIDATE_PATH_BYTES
        ):
            state.warn("candidate-output-budget-exceeded", ".")
            break
        selected_candidate_path_bytes += encoded_path_bytes
        selected_candidates.append(
            {
                "path": rel,
                "category": category,
                "space_status": "review-required",
                "origin": "unknown",
                "mutation_allowed": False,
                "allocated_bytes_estimate": size,
            }
        )

    collector.findings.sort(
        key=lambda item: (
            {"high": 0, "medium": 1, "low": 2}.get(item["severity"], 3),
            item["code"],
            [os.fsencode(value) for value in item["paths"]],
        )
    )
    return collector.findings, selected_candidates


def top_level_inventory(state: AuditState) -> list[dict]:
    values: list[dict] = []
    for entry in state.entries_by_parent.get(".", []):
        if entry.kind == "directory":
            record = state.directories[entry.rel]
            values.append(
                {
                    "path": entry.rel,
                    "kind": "directory",
                    "policy_status": (
                        "protected"
                        if entry.name.casefold() in PROTECTED_DELIVERABLE_NAMES
                        else "review-only"
                    ),
                    "files": record.total_files,
                    "directories": record.total_directories,
                    "empty_directories": record.empty_directories,
                    "allocated_bytes_estimate": record.total_allocated_bytes,
                    "coverage_complete": record.enumeration_complete and not record.excluded,
                }
            )
        else:
            values.append(
                {
                    "path": entry.rel,
                    "kind": entry.kind,
                    "policy_status": "review-only",
                    "allocated_bytes_estimate": entry.allocated_bytes,
                }
            )
    values.sort(key=lambda item: os.fsencode(item["path"]))
    return values[:200]


def make_report(state: AuditState) -> dict:
    findings, space_candidates = detect_findings(state)
    root_record = state.directories["."]
    technical_records: list[DirectoryRecord] = []
    technical_root_paths: set[str] = set()
    for record in sorted(
        state.directories.values(),
        key=lambda value: (value.depth, os.fsencode(value.rel)),
    ):
        if record.rel == ".":
            continue
        if record.name.casefold() not in ({".pnpm-store", "node_modules"} | MACHINE_CACHE_DIRECTORY_NAMES):
            continue
        parent_rel = record.parent
        nested = False
        while parent_rel and parent_rel != ".":
            if parent_rel in technical_root_paths:
                nested = True
                break
            parent = state.directories.get(parent_rel)
            parent_rel = parent.parent if parent is not None else "."
        if nested:
            continue
        technical_records.append(record)
        technical_root_paths.add(record.rel)
    technical_directories = sum(record.total_directories for record in technical_records)
    protected_bytes = sum(
        state.directories[entry.rel].total_allocated_bytes
        for entry in state.entries_by_parent.get(".", [])
        if entry.kind == "directory" and entry.name.casefold() in PROTECTED_DELIVERABLE_NAMES
    )
    warnings = sorted(
        state.warnings,
        key=lambda item: (item["code"], os.fsencode(item["path"]), item.get("errno", -1)),
    )
    if state.warning_output_truncated:
        warnings.append({"code": "warning-output-budget-exceeded", "path": "."})
    report = {
        "kind": REPORT_KIND,
        "schema_version": SCHEMA_VERSION,
        "root": str(state.root),
        "mutation_allowed": False,
        "policy": {
            "read_only": True,
            "mutation_supported": False,
            "metadata_only": True,
            "provenance_default": "unknown",
            "names_never_authorize_deletion": True,
        },
        "coverage": {
            "complete": not state.partial,
            "entries_examined": state.entries_examined,
            "path_bytes_examined": state.path_bytes,
            "symlinks_skipped": root_record.total_symlinks,
            "mounts_skipped": state.mounts_skipped,
            "races_skipped": state.races_skipped,
            "permission_skipped": state.permission_skipped,
            "excluded_trees": sorted(state.excluded_trees, key=os.fsencode),
            "warnings": warnings,
        },
        "inventory": {
            "files": root_record.total_files,
            "directories": root_record.total_directories,
            "empty_directories": root_record.empty_directories,
            "symlinks_skipped": root_record.total_symlinks,
            "special_files_skipped": root_record.total_special,
            "logical_bytes_estimate": root_record.total_logical_bytes,
            "allocated_bytes_estimate": root_record.total_allocated_bytes,
            "max_depth_examined": max((record.depth for record in state.directories.values()), default=0),
        },
        "visual_clutter": {
            "technical_directories": technical_directories,
            "directory_share": (
                round(technical_directories / root_record.total_directories, 6)
                if root_record.total_directories
                else 0.0
            ),
            "note": "Visual directory count is not confirmed reclaimable space.",
        },
        "space_summary": {
            "confirmed_reclaimable_bytes": 0,
            "review_required_nonoverlapping_bytes_estimate": sum(
                item["allocated_bytes_estimate"] for item in space_candidates
            ),
            "protected_top_level_bytes_estimate": protected_bytes,
        },
        "space_candidates": space_candidates,
        "top_level": top_level_inventory(state),
        "findings": findings,
    }
    payload = json.dumps(report, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    report["seal"] = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return report


def print_terminal(report: dict) -> None:
    coverage = report["coverage"]
    inventory = report["inventory"]
    clutter = report["visual_clutter"]
    status = "complete" if coverage["complete"] else "PARTIAL"
    print("Workspace hygiene audit (metadata-only, read-only)")
    print(f"Root: {core.display_path(report['root'])}")
    print(f"Coverage: {status}; entries examined={coverage['entries_examined']}")
    print(
        "Inventory: "
        f"directories={inventory['directories']}, files={inventory['files']}, "
        f"symlinks skipped={inventory['symlinks_skipped']}, "
        f"allocated estimate={core.human_size(inventory['allocated_bytes_estimate'])}"
    )
    print(
        "Technical directory share: "
        f"{clutter['technical_directories']}/{inventory['directories']} "
        f"({clutter['directory_share'] * 100:.1f}%). "
        "Visual clutter is not confirmed reclaimable space."
    )
    print(
        "Confirmed reclaimable: 0.0 B; review-only non-overlapping estimate: "
        f"{core.human_size(report['space_summary']['review_required_nonoverlapping_bytes_estimate'])}"
    )
    if report["findings"]:
        print("\nFindings (none authorizes mutation):")
        for finding in report["findings"]:
            paths = ", ".join(core.display_path(value) for value in finding["paths"])
            if finding["path_count"] > len(finding["paths"]):
                paths += f", ... ({finding['path_count']} paths total)"
            print(
                f"- {finding['id']} [{finding['severity']}] {finding['code']}: "
                f"{paths}\n  {finding['message']} {finding['recommendation']}"
            )
    else:
        print("\nNo high-signal hygiene pattern was found in the examined metadata.")
    if not coverage["complete"]:
        print("\nCoverage is partial. Do not interpret missing findings as a clean workspace.")
        for warning in coverage["warnings"][:20]:
            print(
                f"- {warning['code']}: {core.display_path(warning['path'])}"
                + (f" (errno={warning['errno']})" if "errno" in warning else "")
            )
    print(f"\nReport seal: {report['seal']}")
    print("This report cannot be passed to cleanup token/apply and selects nothing.")


def validate_budgets(args: argparse.Namespace) -> Budgets:
    if not 1 <= args.max_entries <= HARD_MAX_ENTRIES:
        raise ValueError(f"--max-entries must be between 1 and {HARD_MAX_ENTRIES}")
    if not 1 <= args.max_children <= HARD_MAX_CHILDREN:
        raise ValueError(f"--max-children must be between 1 and {HARD_MAX_CHILDREN}")
    if not 1 <= args.max_depth <= HARD_MAX_DEPTH:
        raise ValueError(f"--max-depth must be between 1 and {HARD_MAX_DEPTH}")
    if not 0.01 <= args.max_seconds <= HARD_MAX_SECONDS:
        raise ValueError(f"--max-seconds must be between 0.01 and {HARD_MAX_SECONDS:g}")
    return Budgets(
        max_entries=args.max_entries,
        max_children=args.max_children,
        max_depth=args.max_depth,
        max_seconds=args.max_seconds,
    )


def audit_root(root: Path, budgets: Budgets) -> tuple[dict, int]:
    validated, root_fd = validate_root(root)
    state = scan_metadata(validated, budgets, root_fd)
    report = make_report(state)
    return report, 0 if report["coverage"]["complete"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit workspace clutter and naming patterns without reading file contents or changing the project."
    )
    parser.add_argument("--root", required=True, help="one explicit bounded project root")
    parser.add_argument("--format", choices=("terminal", "json"), default="terminal")
    parser.add_argument("--max-entries", type=int, default=DEFAULT_MAX_ENTRIES)
    parser.add_argument("--max-children", type=int, default=DEFAULT_MAX_CHILDREN)
    parser.add_argument("--max-depth", type=int, default=DEFAULT_MAX_DEPTH)
    parser.add_argument("--max-seconds", type=float, default=DEFAULT_MAX_SECONDS)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        budgets = validate_budgets(args)
        report, result = audit_root(Path(args.root), budgets)
    except (OSError, ValueError) as error:
        print(f"Audit error: {json.dumps(str(error), ensure_ascii=True)}", file=sys.stderr)
        return 2
    if args.format == "json":
        print(json.dumps(report, ensure_ascii=True, sort_keys=True, indent=2))
    else:
        print_terminal(report)
    return result


if __name__ == "__main__":
    raise SystemExit(main())

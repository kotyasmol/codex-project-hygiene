#!/usr/bin/env python3
"""Inventory and remove reproducible project junk using a sealed deletion plan.

The scanner is read-only.  The apply command accepts only a fresh, unchanged
plan and revalidates every filesystem object immediately before mutation.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import errno
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.parse
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Optional


SCHEMA_VERSION = 4
PLAN_KIND = "codex-disk-cleanup-plan"
PLAN_TTL_HOURS = 24
PLAN_CLOCK_SKEW_MINUTES = 5
NEVER_SCAN_NAMES = {
    ".git",
    ".hg",
    ".svn",
    ".agents",
    ".codex",
    ".idea",
}
NEVER_DESCEND_NAMES = {"output", "outputs", "assets"}
PYTHON_CACHE_NAMES = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".pyre",
}
JS_CACHE_NAMES = {
    ".vite",
    ".turbo",
    ".parcel-cache",
}
JS_FRAMEWORK_OUTPUT_NAMES = {".nuxt", ".svelte-kit"}

# These are exact user-level cache locations discovered by user_cache_specs().
# Project-local paths need affirmative Git-ignore evidence before they may be
# labelled safe; without that evidence they remain review-only.
SAFE_WITHOUT_GIT_CATEGORIES = {
    "codex-cache",
    "codex-temp",
    "codex-ui-cache",
    "codex-runtime-temp",
    "gradle-daemon",
    "gradle-global-cache",
    "npm-cache",
    "npx-cache",
    "playwright-browsers",
    "xcode-derived-data",
}


@dataclass
class Candidate:
    id: str
    path: str
    root: str
    kind: str
    classification: str
    category: str
    allocated_bytes: int
    reason: str
    regeneration: str
    repo: Optional[str]
    git_ignored: Optional[bool]
    tracked_content: Optional[bool]
    git_error: Optional[str]
    requires_process_stopped: Optional[str]
    policy_evidence: dict
    identity: dict
    tree_fingerprint: str


class PreconditionFailed(Exception):
    pass


class PartialDeletionError(PreconditionFailed):
    pass


def eprint(*values: object) -> None:
    print(*values, file=sys.stderr)


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso_time(value: dt.datetime) -> str:
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024.0 or unit == "TiB":
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{size} B"


def display_path(value: object) -> str:
    """Quote untrusted filesystem text so terminals cannot interpret controls."""
    return json.dumps(os.fspath(value), ensure_ascii=True)


def real(path: Path) -> Path:
    return Path(os.path.realpath(os.path.abspath(os.path.expanduser(str(path)))))


def same_path(path: Path, other: Path) -> bool:
    """Compare filesystem locations, including case aliases on macOS."""
    try:
        return os.path.samefile(path, other)
    except OSError:
        left = str(real(path))
        right = str(real(other))
        if sys.platform == "darwin":
            return left.casefold() == right.casefold()
        return left == right


def is_within(path: Path, root: Path, *, allow_equal: bool = True) -> bool:
    """Use filesystem identity for ancestry instead of case-sensitive strings."""
    resolved_path = real(path)
    resolved_root = real(root)
    if same_path(resolved_path, resolved_root):
        return allow_equal
    current = resolved_path.parent
    while True:
        if same_path(current, resolved_root):
            return True
        if same_path(current, current.parent):
            break
        current = current.parent
    return False


def protected_policy_boundary(path: Path, root: Path) -> Optional[Path]:
    """Return a protected ancestor, including the explicit root itself."""
    current = real(path)
    approved_root = real(root)
    if not is_within(current, approved_root, allow_equal=True):
        return None
    protected_names = NEVER_DESCEND_NAMES | NEVER_SCAN_NAMES
    while True:
        if current.name.casefold() in protected_names:
            return current
        if same_path(current, approved_root):
            break
        current = current.parent
    return None


def protected_scan_root_boundary(path: Path) -> Optional[Path]:
    """Reject roots selected at or below a tree the scanner always protects."""
    current = real(path)
    home = real(Path.home())
    stop = home if is_within(current, home, allow_equal=True) else Path(current.anchor)
    protected_names = NEVER_DESCEND_NAMES | NEVER_SCAN_NAMES
    while not same_path(current, stop):
        if current.name.casefold() in protected_names:
            return current
        current = current.parent
    return None


def protected_item_failure(
    item: dict, path: Path, root: Path, known_cache: Optional[dict]
) -> Optional[str]:
    cache_ancestor = known_user_cache_ancestor(path)
    if cache_ancestor is not None:
        return (
            "only the exact built-in cache root can be selected; descendant is protected: "
            f"{cache_ancestor['path']}"
        )
    if is_codex_ui_cache_path(path):
        if known_cache is not None and same_path(path, Path(known_cache["path"])):
            return None
        return "only an exact built-in Codex UI cache root can be a cleanup item"
    boundary = protected_policy_boundary(path, root)
    if boundary is None:
        return None
    if known_cache is not None and same_path(path, Path(known_cache["path"])):
        return None
    if (
        boundary.name.casefold() in NEVER_DESCEND_NAMES
        and same_path(path, boundary)
        and item.get("classification") == "never-delete"
        and item.get("category") == "deliverable-tree"
    ):
        return None
    return f"path entered protected policy tree: {boundary}"


def has_symlink_component(path: Path) -> bool:
    """Check every existing component without resolving it first."""
    expanded = Path(os.path.abspath(os.path.expanduser(str(path))))
    current = Path(expanded.anchor)
    for part in expanded.parts[1:]:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            return False
        except OSError:
            return True
        if stat.S_ISLNK(info.st_mode):
            return True
    return False


def filesystem_boundary_failure(path: Path, root: Path) -> Optional[str]:
    """Reject a target or ancestor that crosses the explicit root's device/mount."""
    approved_root = real(root)
    current = real(path)
    try:
        root_info = approved_root.lstat()
    except OSError as error:
        return f"cannot inspect approved root boundary: {error}"
    linux_mounts: Optional[list[tuple[Path, int]]] = None
    root_mount_id: Optional[int] = None
    if sys.platform.startswith("linux"):
        linux_mounts, mount_error = linux_mount_table()
        if mount_error:
            return mount_error
        root_mount_id = linux_mount_id(approved_root, linux_mounts)
        if root_mount_id is None:
            return "cannot determine Linux mount ID for approved root"
    while not same_path(current, approved_root):
        try:
            info = current.lstat()
        except OSError as error:
            return f"cannot inspect filesystem boundary {current}: {error}"
        if info.st_dev != root_info.st_dev:
            return f"filesystem device boundary at {current}"
        if linux_mounts is not None and linux_mount_id(current, linux_mounts) != root_mount_id:
            return f"Linux mount-ID boundary at {current}"
        try:
            if os.path.ismount(current):
                return f"nested mount point at {current}"
        except OSError as error:
            return f"cannot verify mount boundary {current}: {error}"
        parent = current.parent
        if same_path(parent, current):
            return "target is not reachable from its approved root"
        current = parent
    return None


def decode_mountinfo_path(value: str) -> str:
    return re.sub(
        r"\\([0-7]{3})",
        lambda match: chr(int(match.group(1), 8)),
        value,
    )


def linux_mount_table() -> tuple[list[tuple[Path, int]], Optional[str]]:
    """Read Linux mount IDs so same-device bind mounts are still boundaries."""
    mountinfo = Path("/proc/self/mountinfo")
    try:
        info = mountinfo.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > 8 * 1024 * 1024:
            return [], "cannot safely inspect Linux mount table"
        data = mountinfo.read_bytes()
    except OSError as error:
        return [], f"cannot inspect Linux mount table: {error}"
    if len(data) > 8 * 1024 * 1024:
        return [], "Linux mount table exceeds safety limit"
    try:
        text_value = data.decode("utf-8")
    except UnicodeDecodeError:
        return [], "Linux mount table is not valid UTF-8"
    entries: list[tuple[Path, int]] = []
    try:
        for line in text_value.splitlines():
            before, _separator, after = line.partition(" - ")
            fields = before.split()
            if not after or len(fields) < 6:
                return [], "Linux mount table contains a malformed record"
            mount_id = int(fields[0])
            mount_point = Path(decode_mountinfo_path(fields[4]))
            if not mount_point.is_absolute():
                return [], "Linux mount table contains a relative mount point"
            entries.append((mount_point, mount_id))
    except (ValueError, IndexError):
        return [], "Linux mount table contains a malformed record"
    if not entries:
        return [], "Linux mount table is empty"
    entries.sort(key=lambda entry: len(entry[0].parts), reverse=True)
    return entries, None


def linux_mount_id(path: Path, entries: list[tuple[Path, int]]) -> Optional[int]:
    canonical = real(path)
    for mount_point, mount_id in entries:
        try:
            if os.path.commonpath([str(canonical), str(mount_point)]) == str(mount_point):
                return mount_id
        except ValueError:
            continue
    return None


def codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return real(Path(configured)) if configured else real(Path.home() / ".codex")


def codex_homes() -> tuple[Path, ...]:
    """Return every Codex state root that must stay protected.

    The default root remains protected even when CODEX_HOME points elsewhere;
    otherwise changing one environment variable could expose historical state.
    """
    homes = [real(Path.home() / ".codex"), codex_home()]
    return tuple(dict.fromkeys(homes))


def codex_context() -> dict:
    """Capture the path policy that must remain stable for a sealed plan."""
    return {
        "active_home": str(codex_home()),
        "protected_homes": [str(path) for path in codex_homes()],
    }


def codex_ui_cache_roots() -> tuple[Path, ...]:
    home = real(Path.home())
    return (
        home / "Library" / "Caches" / "Codex",
        home / "Library" / "Caches" / "com.openai.codex",
    )


def is_codex_ui_cache_path(path: Path) -> bool:
    resolved = real(path)
    return any(same_path(resolved, root) or is_within(resolved, root) for root in codex_ui_cache_roots())


def protected_codex_path(path: Path) -> bool:
    resolved = real(path)
    protected_roots = [
        *codex_homes(),
        real(Path.home() / "Library" / "Application Support" / "Codex"),
        real(Path.home() / "Library" / "Application Support" / "com.openai.codex"),
        real(Path.home() / "Library" / "Application Support" / "OpenAI" / "Codex"),
    ]
    for protected in protected_roots:
        if same_path(resolved, protected) or is_within(resolved, protected):
            return True
    return False


def allowed_codex_item(item: dict) -> bool:
    path = real(Path(item["path"]))
    spec = known_user_cache_spec(path)
    return spec is not None and item.get("category") == spec["category"]


def is_managed_codex_worktree(repo: Optional[Path]) -> Optional[bool]:
    if repo is None:
        return False
    result = run_git(repo, ["rev-parse", "--git-path", "codex-thread.json"])
    if result.returncode != 0:
        return None
    try:
        marker_text = result.stdout.decode().strip()
    except UnicodeDecodeError:
        return None
    if not marker_text:
        return None
    marker = Path(marker_text)
    if not marker.is_absolute():
        marker = repo / marker
    try:
        marker.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return None
    # Any filesystem object at the marker path is enough to protect the repo.
    return True


def validate_scan_root(path: Path) -> Path:
    expanded = Path(os.path.abspath(os.path.expanduser(str(path))))
    if not expanded.exists():
        raise ValueError(f"root does not exist: {expanded}")
    if expanded.is_symlink():
        raise ValueError(f"root must not be a symlink: {expanded}")
    resolved = real(expanded)
    home = real(Path.home())
    forbidden = [real(Path("/")), home, *codex_homes()]
    if (
        any(
            same_path(resolved, value) or is_within(value, resolved, allow_equal=True)
            for value in forbidden
        )
        or same_path(resolved, resolved.parent)
    ):
        raise ValueError(f"root is too broad or protected: {resolved}")
    boundary = protected_scan_root_boundary(resolved)
    if boundary is not None:
        raise ValueError(f"root is inside a protected policy tree: {boundary}")
    if protected_codex_path(resolved):
        raise ValueError(
            f"Codex session/worktree state is not scanned directly: {resolved}. "
            "Archive managed tasks through Codex instead."
        )
    if is_codex_ui_cache_path(resolved):
        raise ValueError(
            f"Codex UI cache is available only through --include-user-caches: {resolved}"
        )
    if known_user_cache_spec(resolved) is not None or known_user_cache_ancestor(resolved) is not None:
        raise ValueError(
            f"built-in user cache trees are available only through --include-user-caches: {resolved}"
        )
    managed = is_managed_codex_worktree(find_repo(resolved))
    if managed is not False:
        raise ValueError(
            f"managed or unverifiable Codex worktree must be cleaned through task archiving: {resolved}"
        )
    if not resolved.is_dir():
        raise ValueError(f"root is not a directory: {resolved}")
    return resolved


def run_git(repo: Path, args: list[str]) -> subprocess.CompletedProcess:
    command = ["git", "-C", str(repo), *args]
    try:
        return subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as error:
        return subprocess.CompletedProcess(command, 127, b"", str(error).encode("utf-8", errors="replace"))


def find_repo(path: Path) -> Optional[Path]:
    current = path if path.is_dir() else path.parent
    while current.parent != current:
        marker = current / ".git"
        try:
            marker.lstat()
        except FileNotFoundError:
            current = current.parent
            continue
        except OSError:
            return current
        else:
            result = run_git(current, ["rev-parse", "--show-toplevel"])
            if result.returncode == 0:
                try:
                    return real(Path(result.stdout.decode().strip()))
                except UnicodeDecodeError:
                    return current
            return current
    return None


def find_unsupported_vcs(path: Path) -> Optional[tuple[str, Path]]:
    """Find an Hg/SVN ancestor; those checkouts are protected fail-closed."""
    current = path if path.is_dir() else path.parent
    while True:
        for marker, label in ((".hg", "Mercurial"), (".svn", "Subversion")):
            try:
                (current / marker).lstat()
            except FileNotFoundError:
                continue
            except OSError:
                return "unknown VCS", real(current)
            return label, real(current)
        if same_path(current, current.parent):
            return None
        current = current.parent


def git_evidence(
    path: Path,
    repo: Optional[Path],
    *,
    allow_missing: bool = False,
    directory_hint: bool = False,
) -> tuple[Optional[bool], Optional[bool], Optional[str]]:
    if repo is None:
        return None, None, None
    try:
        relative = os.path.relpath(path, repo)
    except ValueError:
        return None, None, "cannot resolve path relative to repository"
    tracked = run_git(repo, ["ls-files", "-z", "--", relative])
    if tracked.returncode != 0:
        return None, None, f"git ls-files failed with status {tracked.returncode}"
    has_tracked = bool(tracked.stdout)
    ignore_path = relative.replace(os.sep, "/")
    ignore_args = ["check-ignore", "-q"]
    if allow_missing:
        ignore_args.append("--no-index")
        if directory_hint and not ignore_path.endswith("/"):
            ignore_path += "/"
    ignored = run_git(repo, [*ignore_args, "--", ignore_path])
    if ignored.returncode not in {0, 1}:
        return None, has_tracked, f"git check-ignore failed with status {ignored.returncode}"
    return ignored.returncode == 0, has_tracked, None


def package_has_build(project: Path) -> bool:
    manifest = project / "package.json"
    if manifest.is_symlink() or not manifest.is_file():
        return False
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    scripts = data.get("scripts")
    return isinstance(scripts, dict) and isinstance(scripts.get("build"), str)


def has_direct_marker(directory: Path, names: Iterable[str]) -> bool:
    for name in names:
        marker = directory / name
        try:
            if marker.is_file() and not marker.is_symlink():
                return True
        except OSError:
            return False
    return False


def direct_regular_suffix_files(directory: Path, suffixes: set[str]) -> list[Path]:
    matches: list[Path] = []
    try:
        for entry in os.scandir(directory):
            if Path(entry.name).suffix.casefold() not in suffixes or entry.is_symlink():
                continue
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISREG(info.st_mode):
                matches.append(Path(entry.path))
    except OSError:
        return []
    return sorted(matches, key=lambda value: os.fsencode(value.name))


def classify_directory(path: Path, root: Path) -> Optional[tuple[str, str, str, str]]:
    """Return classification, category, reason, regeneration."""
    name = path.name
    parent = path.parent

    if name == "node_modules":
        project = parent
        if has_direct_marker(project, {"package.json"}):
            lockfiles = [
                project / "package-lock.json",
                project / "npm-shrinkwrap.json",
                project / "pnpm-lock.yaml",
                project / "yarn.lock",
                project / "bun.lock",
                project / "bun.lockb",
            ]
            if any(lock.is_file() and not lock.is_symlink() for lock in lockfiles):
                return (
                    "safe",
                    "js-dependencies",
                    "dependency tree with package manifest and lockfile",
                    "run the matching frozen/locked package-manager install",
                )
            return (
                "review",
                "js-dependencies",
                "dependency tree has package.json but no lockfile",
                "run the project package-manager install",
            )

    if name in PYTHON_CACHE_NAMES:
        return "safe", "python-cache", "standard Python tool cache", "rerun Python/tests"

    if name == ".hypothesis":
        return "review", "hypothesis-examples", "may contain valuable minimized counterexamples", "rerun property tests"

    if name in {".venv", "venv"} and (path / "pyvenv.cfg").is_file():
        if has_direct_marker(parent, {"pyproject.toml", "requirements.txt", "Pipfile", "poetry.lock", "uv.lock"}):
            return (
                "review",
                "python-environment",
                "virtual environment with dependency manifest",
                "recreate the virtual environment from its manifest",
            )

    if name in JS_CACHE_NAMES:
        if has_direct_marker(parent, {"package.json"}):
            return "safe", "js-build-cache", f"{name} is generated by the JavaScript toolchain", "rerun build/tests"

    if name == "coverage" and has_direct_marker(parent, {"package.json"}):
        return "review", "test-output", "coverage output may be a retained report", "rerun tests with coverage"

    if name == "cache" and parent.name == ".next":
        if has_direct_marker(parent.parent, {"package.json"}):
            return "safe", "next-cache", ".next/cache is a rebuildable Next.js cache", "rerun the Next.js build"

    if name in JS_FRAMEWORK_OUTPUT_NAMES:
        if has_direct_marker(parent, {"package.json"}):
            return (
                "review",
                "framework-output",
                f"{name} may contain deployable server/static output",
                "rerun the framework build",
            )

    if name in {"dist", "out"}:
        if has_direct_marker(parent, {"package.json"}) and package_has_build(parent):
            return (
                "review",
                "compiled-output",
                f"{name} has a package build script but may be a deliverable",
                "run the package build script",
            )

    gradle_markers = {"build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts", "gradlew"}
    if name == ".gradle" and has_direct_marker(parent, gradle_markers):
        return "safe", "gradle-project-cache", "project-local Gradle cache", "rerun Gradle"
    if name == ".cxx" and has_direct_marker(parent, gradle_markers):
        return "safe", "android-native-build", "Android/NDK intermediates", "rerun the Android build"
    if name == "build" and has_direct_marker(parent, gradle_markers):
        return (
            "review",
            "gradle-build-output",
            "Gradle build output; exact folder requires review",
            "rerun the Gradle build",
        )
    if name == "build":
        if has_direct_marker(parent, {"package.json"}) and package_has_build(parent):
            return (
                "review",
                "compiled-output",
                "package build output may also be a deliverable",
                "run the package build script",
            )

    if name in {"bin", "obj"}:
        dotnet_project = bool(direct_regular_suffix_files(parent, {".csproj", ".fsproj"}))
        if dotnet_project:
            return "safe", "dotnet-build", ".NET build intermediates", "run dotnet restore/build"

    if name == "target" and has_direct_marker(parent, {"Cargo.toml"}):
        return "safe", "rust-build", "Cargo build output", "run cargo build"

    if name == ".build" and has_direct_marker(parent, {"Package.swift"}):
        return "safe", "swift-build", "Swift Package Manager build output", "run swift build"

    if name == "Pods" and has_direct_marker(parent, {"Podfile", "Podfile.lock"}):
        return "review", "cocoapods", "CocoaPods dependencies", "run pod install"

    return None


def allocated_size(path: Path) -> int:
    try:
        result = subprocess.run(
            ["du", "-skx", str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            text=True,
        )
        if result.returncode == 0:
            return int(result.stdout.split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        pass

    total = 0
    try:
        if path.is_file():
            info = path.lstat()
            blocks = getattr(info, "st_blocks", None)
            return blocks * 512 if isinstance(blocks, int) else info.st_size
        for current, directories, files in os.walk(path, followlinks=False):
            directories[:] = [name for name in directories if not (Path(current) / name).is_symlink()]
            for name in files:
                try:
                    info = (Path(current) / name).lstat()
                    blocks = getattr(info, "st_blocks", None)
                    total += blocks * 512 if isinstance(blocks, int) else info.st_size
                except OSError:
                    continue
    except OSError:
        return total
    return total


def identity(path: Path) -> dict:
    info = path.lstat()
    return {
        "device": info.st_dev,
        "inode": info.st_ino,
        "mode": stat.S_IFMT(info.st_mode),
        "mtime_ns": info.st_mtime_ns,
        "ctime_ns": info.st_ctime_ns,
    }


def tree_metadata_snapshot(path: Path) -> tuple[str, Optional[str]]:
    """Hash tree metadata without following symlinks and report unsafe boundaries."""
    digest = hashlib.sha256()
    try:
        root_info = path.lstat()
    except OSError as error:
        return "", f"cannot inspect candidate root: {error}"
    root_device = root_info.st_dev
    linux_mounts: Optional[list[tuple[Path, int]]] = None
    root_mount_id: Optional[int] = None
    if sys.platform.startswith("linux"):
        linux_mounts, mount_error = linux_mount_table()
        if mount_error:
            return "", mount_error
        root_mount_id = linux_mount_id(path, linux_mounts)
        if root_mount_id is None:
            return "", "cannot determine Linux mount ID for candidate root"
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            info = current.lstat()
            relative = os.path.relpath(current, path)
            record = (
                relative,
                stat.S_IFMT(info.st_mode),
                stat.S_IMODE(info.st_mode),
                info.st_dev,
                info.st_ino,
                info.st_nlink,
                info.st_size,
                info.st_mtime_ns,
                0 if current == path else info.st_ctime_ns,
            )
            digest.update(repr(record).encode("utf-8", errors="surrogateescape"))
            digest.update(b"\0")
            if current.is_symlink():
                digest.update(os.fsencode(os.readlink(current)))
                digest.update(b"\0")
                continue
            if not current.is_dir():
                continue
            entries = sorted(os.scandir(current), key=lambda entry: os.fsencode(entry.name))
        except OSError as error:
            return digest.hexdigest(), f"cannot inspect candidate tree: {error}"
        for entry in reversed(entries):
            child = Path(entry.path)
            try:
                child_info = entry.stat(follow_symlinks=False)
                if entry.name.casefold() in {".git", ".hg", ".svn"}:
                    return digest.hexdigest(), f"nested repository/worktree metadata at {child}"
                if entry.is_dir(follow_symlinks=False) and (
                    child_info.st_dev != root_device or os.path.ismount(child)
                ):
                    return digest.hexdigest(), f"nested mount point at {child}"
                if (
                    linux_mounts is not None
                    and linux_mount_id(child, linux_mounts) != root_mount_id
                ):
                    return digest.hexdigest(), f"nested Linux mount-ID boundary at {child}"
            except OSError as error:
                return digest.hexdigest(), f"cannot inspect candidate entry: {error}"
            stack.append(child)
    return digest.hexdigest(), None


JS_POLICY_MARKERS = {
    "package.json",
    "package-lock.json",
    "npm-shrinkwrap.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "bun.lock",
    "bun.lockb",
}
GRADLE_POLICY_MARKERS = {
    "build.gradle",
    "build.gradle.kts",
    "settings.gradle",
    "settings.gradle.kts",
    "gradlew",
}


def policy_marker_paths(path: Path, category: str) -> list[Path]:
    parent = path.parent
    names: set[str] = set()
    base = parent
    if category == "next-cache":
        base = parent.parent
        names = JS_POLICY_MARKERS
    elif category in {
        "js-dependencies",
        "js-build-cache",
        "test-output",
        "framework-output",
        "compiled-output",
    }:
        names = JS_POLICY_MARKERS
    elif category == "python-environment":
        names = {"pyproject.toml", "requirements.txt", "Pipfile", "poetry.lock", "uv.lock"}
    elif category in {"gradle-project-cache", "android-native-build", "gradle-build-output"}:
        names = GRADLE_POLICY_MARKERS
    elif category == "rust-build":
        names = {"Cargo.toml", "Cargo.lock"}
    elif category == "swift-build":
        names = {"Package.swift", "Package.resolved"}
    elif category == "cocoapods":
        names = {"Podfile", "Podfile.lock"}
    paths = [base / name for name in sorted(names)]
    if category == "dotnet-build":
        paths.extend(direct_regular_suffix_files(parent, {".csproj", ".fsproj"}))
    return paths


def policy_evidence_snapshot(path: Path, category: str) -> tuple[dict, Optional[str]]:
    """Seal adjacent manifests that made a generated directory eligible."""
    evidence: dict[str, dict] = {}
    for marker in policy_marker_paths(path, category):
        marker_key = str(real(marker.parent) / marker.name)
        try:
            info = marker.lstat()
        except FileNotFoundError:
            evidence[marker_key] = {"state": "missing"}
            continue
        except OSError as error:
            return evidence, f"cannot inspect classification marker {marker}: {error}"
        if stat.S_ISLNK(info.st_mode):
            evidence[marker_key] = {"state": "symlink"}
            continue
        if not stat.S_ISREG(info.st_mode):
            evidence[marker_key] = {
                "state": "other",
                "device": info.st_dev,
                "inode": info.st_ino,
                "mode": stat.S_IFMT(info.st_mode),
            }
            continue
        digest = hashlib.sha256()
        try:
            with marker.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            after = marker.lstat()
        except OSError as error:
            return evidence, f"cannot hash classification marker {marker}: {error}"
        before_identity = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        if before_identity != after_identity:
            return evidence, f"classification marker changed while hashing: {marker}"
        evidence[marker_key] = {
            "state": "file",
            "device": info.st_dev,
            "inode": info.st_ino,
            "size": info.st_size,
            "mtime_ns": info.st_mtime_ns,
            "ctime_ns": info.st_ctime_ns,
            "sha256": digest.hexdigest(),
        }
    return evidence, None


def make_candidate(
    path: Path,
    root: Path,
    classification: str,
    category: str,
    reason: str,
    regeneration: str,
    requires_process_stopped: Optional[str] = None,
) -> Candidate:
    repo = find_repo(path)
    ignored, tracked, git_error = git_evidence(path, repo)
    fingerprint, tree_error = tree_metadata_snapshot(path)
    policy_evidence, policy_error = policy_evidence_snapshot(path, category)
    nested_repo = (
        any((path / marker).exists() for marker in (".git", ".hg", ".svn"))
        if path.is_dir()
        else False
    )
    unsupported_vcs = find_unsupported_vcs(path)
    if tree_error:
        classification = "never-delete"
        reason = f"{reason}; tree safety check failed: {tree_error}"
    elif git_error:
        classification = "never-delete"
        reason = f"{reason}; Git safety check failed: {git_error}"
    elif policy_error:
        classification = "never-delete"
        reason = f"{reason}; marker safety check failed: {policy_error}"
    elif tracked:
        classification = "never-delete"
        reason = f"{reason}; contains Git-tracked content"
    elif nested_repo:
        classification = "never-delete"
        reason = f"{reason}; directory is a repository/worktree boundary"
    elif unsupported_vcs is not None:
        classification = "never-delete"
        reason = f"{reason}; belongs to protected {unsupported_vcs[0]} checkout at {unsupported_vcs[1]}"
    elif is_managed_codex_worktree(repo) is not False:
        classification = "never-delete"
        reason = f"{reason}; Codex-managed worktree state is present or unverifiable"
    elif classification == "safe" and category not in SAFE_WITHOUT_GIT_CATEGORIES:
        if repo is None:
            classification = "review"
            reason = f"{reason}; no Git repository exists to prove the path is ignored"
        elif ignored is not True:
            classification = "review"
            reason = f"{reason}; generated path is not Git-ignored"
    item_id = hashlib.sha256(str(real(path)).encode("utf-8")).hexdigest()[:16]
    return Candidate(
        id=item_id,
        path=str(real(path)),
        root=str(real(root)),
        kind="directory" if path.is_dir() else "file",
        classification=classification,
        category=category,
        allocated_bytes=0 if tree_error else allocated_size(path),
        reason=reason,
        regeneration=regeneration,
        repo=str(repo) if repo else None,
        git_ignored=ignored,
        tracked_content=tracked,
        git_error=git_error,
        requires_process_stopped=requires_process_stopped,
        policy_evidence=policy_evidence,
        identity=identity(path),
        tree_fingerprint=fingerprint,
    )


def scan_root(root: Path, warnings: Optional[list[str]] = None) -> list[Candidate]:
    warnings = warnings if warnings is not None else []
    root_device = root.lstat().st_dev
    linux_mounts: Optional[list[tuple[Path, int]]] = None
    root_mount_id: Optional[int] = None
    if sys.platform.startswith("linux"):
        linux_mounts, mount_error = linux_mount_table()
        if mount_error:
            warnings.append(mount_error)
            return []
        root_mount_id = linux_mount_id(root, linux_mounts)
        if root_mount_id is None:
            warnings.append("cannot determine Linux mount ID for scan root")
            return []
    candidates: list[Candidate] = []
    relevant_cache_roots = [
        Path(record["path"])
        for record in safe_user_cache_specs()
        if is_within(Path(record["path"]), root, allow_equal=True)
        or is_within(root, Path(record["path"]), allow_equal=True)
    ]
    stack = [root]

    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError as error:
            warnings.append(f"could not read directory {current}: {error}")
            continue
        for entry in entries:
            path = Path(entry.path)
            try:
                if entry.is_symlink():
                    continue
                info = entry.stat(follow_symlinks=False)
                if info.st_dev != root_device:
                    continue
                if linux_mounts is not None and linux_mount_id(path, linux_mounts) != root_mount_id:
                    warnings.append(f"skipped Linux mount-ID boundary: {path}")
                    continue
            except OSError as error:
                warnings.append(f"could not inspect entry {path}: {error}")
                continue

            if entry.is_dir(follow_symlinks=False):
                entry_name = entry.name.casefold()
                if entry_name in NEVER_SCAN_NAMES:
                    continue
                if any(
                    same_path(path, cache_root)
                    or is_within(path, cache_root, allow_equal=False)
                    for cache_root in relevant_cache_roots
                ):
                    continue
                # UI caches are special candidates because deleting them while
                # Codex is running can corrupt live browser/database state.
                # Never let an ordinary project-root scan classify descendants.
                if is_codex_ui_cache_path(path):
                    continue
                if entry_name in NEVER_DESCEND_NAMES:
                    candidates.append(
                        make_candidate(
                            path,
                            root,
                            "never-delete",
                            "deliverable-tree",
                            f"{entry.name}/ is protected user output/assets and is not traversed",
                            "not a cleanup target",
                        )
                    )
                    continue
                if protected_codex_path(path):
                    continue
                if (path / ".git").exists():
                    managed = is_managed_codex_worktree(find_repo(path))
                    if managed is not False:
                        continue
                rule = classify_directory(path, root)
                if rule:
                    candidates.append(make_candidate(path, root, *rule))
                    continue
                stack.append(path)
                continue

            if entry.is_file(follow_symlinks=False) and (
                entry.name == ".DS_Store" or entry.name.endswith((".pyc", ".pyo"))
            ):
                candidates.append(
                    make_candidate(
                        path,
                        root,
                        "safe",
                        "metadata-cache" if entry.name == ".DS_Store" else "python-bytecode",
                        "regenerable filesystem metadata/bytecode",
                        "regenerated automatically",
                    )
                )
    return candidates


def user_cache_specs(include_emulator_snapshots: bool) -> list[tuple]:
    home = real(Path.home())
    specs = [
        (home / ".npm" / "_cacache", "safe", "npm-cache", "npm download cache", "npm will redownload packages", None),
        (home / ".npm" / "_npx", "safe", "npx-cache", "temporary npx installs", "npx will reinstall packages", None),
        (home / ".gradle" / "caches", "safe", "gradle-global-cache", "global Gradle cache", "Gradle will redownload/rebuild", "GradleDaemon"),
        (home / ".gradle" / "daemon", "safe", "gradle-daemon", "Gradle daemon state/logs", "Gradle restarts daemons", "GradleDaemon"),
        (home / "Library" / "Caches" / "ms-playwright", "safe", "playwright-browsers", "downloaded Playwright browsers", "Playwright reinstalls browsers", None),
        (home / "Library" / "Developer" / "Xcode" / "DerivedData", "safe", "xcode-derived-data", "Xcode derived data", "Xcode rebuilds it", "Xcode"),
        (codex_home() / "cache", "safe", "codex-cache", "Codex tool/catalog cache", "Codex recreates it", "Codex"),
        (codex_home() / ".tmp", "safe", "codex-temp", "Codex temporary downloads", "Codex recreates it", "Codex"),
    ]
    for cache_root in codex_ui_cache_roots():
        specs.append(
            (
                cache_root,
                "safe",
                "codex-ui-cache",
                "Codex UI/browser cache",
                "Codex recreates it",
                "Codex",
            )
        )
    runtime_cache = home / ".cache" / "codex-runtimes"
    if runtime_cache.is_dir():
        try:
            for child in runtime_cache.iterdir():
                if child.is_dir() and child.name.startswith("codex-runtime-install-"):
                    specs.append((child, "safe", "codex-runtime-temp", "incomplete runtime installer", "Codex redownloads if needed", "Codex"))
        except OSError:
            pass
    if include_emulator_snapshots:
        avd_root = home / ".android" / "avd"
        if avd_root.is_dir():
            try:
                for snapshots in avd_root.glob("*.avd/snapshots"):
                    specs.append((snapshots, "review", "android-snapshots", "emulator quick-boot snapshots", "the emulator recreates snapshots", "AndroidEmulator"))
            except OSError:
                pass
    return specs


def add_user_caches(candidates: list[Candidate], include_emulator_snapshots: bool) -> None:
    known = {real(Path(item.path)) for item in candidates}
    for path, classification, category, reason, regeneration, process in user_cache_specs(
        include_emulator_snapshots
    ):
        try:
            info = path.lstat()
        except OSError:
            continue
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or has_symlink_component(path)
            or real(path) in known
        ):
            continue
        candidates.append(
            make_candidate(
                path,
                path.parent,
                classification,
                category,
                reason,
                regeneration,
                requires_process_stopped=process,
            )
        )


def plan_payload(plan: dict) -> dict:
    payload = copy.deepcopy(plan)
    payload.pop("seal", None)
    return payload


def plan_seal(plan: dict) -> str:
    encoded = json.dumps(plan_payload(plan), sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def new_output_path(path: Path) -> Path:
    """Resolve a new output path without following/replacing its final entry."""
    expanded = Path(os.path.abspath(os.path.expanduser(str(path))))
    if os.path.lexists(expanded):
        raise FileExistsError(f"refusing to overwrite existing path: {expanded}")
    return real(expanded.parent) / expanded.name


def write_secure_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        raise


def print_plan(plan: dict, plan_path: Path) -> None:
    totals = {"safe": 0, "review": 0, "never-delete": 0}
    counts = {key: 0 for key in totals}
    for item in plan["items"]:
        classification = item["classification"]
        totals[classification] += item["allocated_bytes"]
        counts[classification] += 1
    print(f"Plan: {display_path(plan_path)}")
    print(f"Seal: {plan['seal']}")
    print(
        "Totals: "
        + ", ".join(
            f"{key}={counts[key]} ({human_size(totals[key])})"
            for key in ("safe", "review", "never-delete")
        )
    )
    print("\nID               CLASS         SIZE       CATEGORY                 PATH")
    for item in sorted(plan["items"], key=lambda value: value["allocated_bytes"], reverse=True):
        print(
            f"{item['id']:<16} {item['classification']:<13} "
            f"{human_size(item['allocated_bytes']):>10}  {item['category']:<23} "
            f"{display_path(item['path'])}"
        )
    if plan.get("notes"):
        print("\nNotes:")
        for note in plan["notes"]:
            print(f"- {note}")


def command_scan(args: argparse.Namespace) -> int:
    try:
        roots = [validate_scan_root(Path(value)) for value in args.root]
        if any(same_path(root, prior) for index, root in enumerate(roots) for prior in roots[:index]):
            raise ValueError("duplicate scan roots are not allowed")
        plan_path = (
            new_output_path(Path(args.plan))
            if args.plan
            else new_output_path(Path(tempfile.gettempdir()) / f"codex-disk-cleanup-{uuid.uuid4().hex}.json")
        )
    except ValueError as error:
        eprint(f"Policy error: {error}")
        return 2
    except OSError as error:
        eprint(f"Plan output error: {error}")
        return 2

    candidates: list[Candidate] = []
    warnings: list[str] = []
    for root in roots:
        candidates.extend(scan_root(root, warnings))
    if args.include_user_caches:
        add_user_caches(candidates, args.include_emulator_snapshots)

    # Exact-path dedupe, followed by stable IDs and ordering.
    deduped: dict[str, Candidate] = {}
    for candidate in candidates:
        deduped[candidate.path] = candidate
    items = [asdict(candidate) for candidate in deduped.values()]
    items.sort(key=lambda value: (value["classification"], value["path"]))

    created = utc_now()
    plan = {
        "kind": PLAN_KIND,
        "schema_version": SCHEMA_VERSION,
        "created_at": iso_time(created),
        "expires_at": iso_time(created + dt.timedelta(hours=PLAN_TTL_HOURS)),
        "host": platform.node(),
        "codex_context": codex_context(),
        "roots": [str(root) for root in roots],
        "items": items,
        "coverage": {
            "complete": not warnings,
            "warnings": warnings,
        },
        "notes": [
            "Allocated sizes are estimates; APFS clones, hard links, and snapshots can change actual reclaimed space.",
            "outputs/, Codex sessions, and Codex-managed worktree roots are never deletion targets.",
            "Archive managed worktree tasks in Codex so the app can snapshot and remove them safely.",
        ],
    }
    plan["seal"] = plan_seal(plan)
    try:
        write_secure_json(plan_path, plan)
    except OSError as error:
        eprint(f"Plan output error: {error}")
        return 2
    print_plan(plan, plan_path)
    if warnings:
        print(f"\nCoverage warnings: {len(warnings)} (stored in the sealed plan)")
    return 0


def parse_time(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def process_running(name: str) -> Optional[bool]:
    """Return True, False, or None when the process list is unverifiable."""
    commands = {
        "Codex": [
            ["pgrep", "-x", "Codex"],
            ["pgrep", "-x", "codex"],
            ["pgrep", "-x", "codex-cli"],
            ["pgrep", "-x", "Codex Helper"],
            ["pgrep", "-x", "ChatGPT"],
            ["pgrep", "-x", "ChatGPT Helper"],
        ],
        "GradleDaemon": [["pgrep", "-f", "org.gradle.launcher.daemon.bootstrap.GradleDaemon"]],
        "AndroidEmulator": [
            ["pgrep", "-f", r"(^|/)(emulator|qemu-system-[^ /]+)( |$)"],
        ],
    }.get(name, [["pgrep", "-x", name]])
    states: list[Optional[bool]] = []
    for command in commands:
        try:
            result = subprocess.run(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            states.append(None)
            continue
        if result.returncode == 0:
            return True
        if result.returncode == 1:
            states.append(False)
        else:
            states.append(None)
    if any(state is None for state in states):
        return None
    return False


def open_file_state(path: Path) -> Optional[bool]:
    """Return True for open files, False for none, and None when unverifiable."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return None
    if stat.S_ISLNK(info.st_mode):
        return None
    command = ["lsof", "-t", "+D", str(path)] if stat.S_ISDIR(info.st_mode) else ["lsof", "-t", "--", str(path)]
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.stdout.strip():
        return True
    if result.returncode == 1 and not result.stderr.strip():
        return False
    return None


def required_process_state(name: str, candidate: Path) -> Optional[bool]:
    home = real(Path.home())
    if name == "Codex":
        probes = [candidate]
        for state_home in codex_homes():
            probes.extend(
                [
                    state_home / "thread-writer-locks",
                    state_home / "sqlite",
                    state_home / "ipc",
                ]
            )
    elif name == "GradleDaemon":
        probes = [home / ".gradle" / "daemon", candidate]
    else:
        probes = [candidate]
    states = [open_file_state(path) for path in probes]
    if any(state is True for state in states):
        return True
    if any(state is None for state in states):
        return None
    running = process_running(name)
    if running is True:
        return True
    if running is None:
        return None
    return False


def safe_user_cache_specs() -> list[dict]:
    records: list[dict] = []
    for spec_path, classification, category, reason, regeneration, process in user_cache_specs(True):
        if has_symlink_component(spec_path):
            continue
        try:
            spec_info = spec_path.lstat()
        except OSError:
            continue
        if stat.S_ISLNK(spec_info.st_mode) or not stat.S_ISDIR(spec_info.st_mode):
            continue
        records.append(
            {
                "path": str(real(spec_path)),
                "classification": classification,
                "category": category,
                "reason": reason,
                "regeneration": regeneration,
                "requires_process_stopped": process,
            }
        )
    return records


def known_user_cache_spec(path: Path) -> Optional[dict]:
    """Return one exact built-in cache spec without following a final symlink."""
    try:
        path_info = path.lstat()
    except OSError:
        return None
    if stat.S_ISLNK(path_info.st_mode) or not stat.S_ISDIR(path_info.st_mode):
        return None
    for record in safe_user_cache_specs():
        spec_path = Path(record["path"])
        if same_path(path, spec_path) and same_path(real(path), spec_path):
            return record
    return None


def known_user_cache_ancestor(path: Path) -> Optional[dict]:
    """Protect every strict descendant of a process-guarded built-in cache."""
    try:
        info = path.lstat()
    except OSError:
        return None
    if stat.S_ISLNK(info.st_mode):
        return None
    for record in safe_user_cache_specs():
        spec_path = Path(record["path"])
        if is_within(path, spec_path, allow_equal=False):
            return record
    return None


def known_user_cache_rule(path: Path) -> Optional[tuple[str, str]]:
    spec = known_user_cache_spec(path)
    if spec is None:
        return None
    return spec["classification"], spec["category"]


def effective_rule(
    item: dict, path: Path, repo: Optional[Path], ignored: Optional[bool]
) -> Optional[tuple[str, str]]:
    user_rule = known_user_cache_rule(path)
    if user_rule is not None:
        return user_rule
    if path.is_file():
        if item["category"] == "metadata-cache" and path.name == ".DS_Store":
            return "safe", "metadata-cache"
        if item["category"] == "python-bytecode" and path.name.endswith((".pyc", ".pyo")):
            return "safe", "python-bytecode"
        return None
    raw_rule = classify_directory(path, Path(item["root"]))
    if raw_rule is None:
        return None
    classification, category, _reason, _regeneration = raw_rule
    if classification == "safe" and category not in SAFE_WITHOUT_GIT_CATEGORIES:
        if repo is None or ignored is not True:
            classification = "review"
    return classification, category


def external_policy_failure(item: dict, original_path: Path) -> Optional[str]:
    evidence, evidence_error = policy_evidence_snapshot(original_path, item["category"])
    if evidence_error:
        return evidence_error
    if evidence != item.get("policy_evidence"):
        return "classification markers changed after scan"
    unsupported = find_unsupported_vcs(original_path)
    if unsupported is not None:
        return f"path belongs to protected {unsupported[0]} checkout"
    planned_repo = real(Path(item["repo"])) if item.get("repo") else None
    current_repo = find_repo(original_path)
    if (planned_repo is None) != (current_repo is None):
        return "repository boundary changed after scan"
    if planned_repo is not None and current_repo is not None and not same_path(planned_repo, current_repo):
        return "repository root changed after scan"
    original_state = path_presence(original_path)
    if original_state is None:
        return "cannot verify original path state after atomic quarantine"
    original_missing = not original_state
    ignored, tracked, git_error = git_evidence(
        original_path,
        current_repo,
        allow_missing=original_missing,
        directory_hint=item.get("kind") == "directory",
    )
    if git_error:
        return f"Git safety check failed: {git_error}"
    if tracked:
        return "path now contains Git-tracked content"
    if ignored != item.get("git_ignored"):
        return "Git ignore status changed after scan"
    managed = is_managed_codex_worktree(current_repo)
    if managed is True:
        return "path belongs to a Codex-managed worktree"
    if managed is None:
        return "could not verify Codex-managed worktree state"
    return None


def revalidate_item(item: dict, plan_path: Path) -> Optional[str]:
    path = Path(item["path"])
    root = Path(item["root"])
    if item["classification"] == "never-delete":
        return "policy forbids this item"
    try:
        path_info = path.lstat()
    except FileNotFoundError:
        return "path no longer exists"
    except OSError as error:
        return f"cannot inspect candidate path: {error}"
    if stat.S_ISLNK(path_info.st_mode):
        return "path became a symlink"
    expected_mode = stat.S_IFDIR if item.get("kind") == "directory" else stat.S_IFREG
    if stat.S_IFMT(path_info.st_mode) != expected_mode:
        return "candidate filesystem type changed after scan"
    if not is_within(path, root, allow_equal=False):
        return "path escaped its approved root"
    boundary_failure = filesystem_boundary_failure(path, root)
    if boundary_failure:
        return boundary_failure
    cache_spec = known_user_cache_spec(path)
    protected_failure = protected_item_failure(item, path, root, cache_spec)
    if protected_failure:
        return protected_failure
    if protected_codex_path(path) and not allowed_codex_item(item):
        return "path is protected Codex state/worktree data"
    current = identity(path)
    if current != item["identity"]:
        return "filesystem identity changed after scan"
    fingerprint, hazard = tree_metadata_snapshot(path)
    if hazard:
        return hazard
    if fingerprint != item.get("tree_fingerprint"):
        return "tree metadata changed after scan"
    if allocated_size(path) != item["allocated_bytes"]:
        return "allocated size changed after scan"
    cwd = real(Path.cwd())
    if same_path(real(path), cwd) or is_within(cwd, path):
        return "path contains the current working directory"
    if same_path(real(path), real(plan_path)) or is_within(plan_path, path):
        return "path contains the sealed plan"
    if cache_spec is not None:
        if item.get("requires_process_stopped") != cache_spec["requires_process_stopped"]:
            return "built-in cache process requirement changed"
        process = cache_spec["requires_process_stopped"]
    else:
        if item.get("requires_process_stopped") is not None:
            return "unexpected process requirement on project-local item"
        process = None
    if process:
        process_state = required_process_state(process, path)
        if process_state is True:
            return f"required process is still using relevant files: {process}"
        if process_state is None:
            return f"could not verify required process is stopped: {process}"
    open_state = open_file_state(path)
    if open_state is True:
        return "candidate has files open in a running process"
    if open_state is None:
        return "could not verify that candidate has no open files"
    external_failure = external_policy_failure(item, path)
    if external_failure:
        return external_failure
    current_repo = find_repo(path)
    ignored, _tracked, _git_error = git_evidence(path, current_repo)
    current_rule = effective_rule(item, path, current_repo, ignored)
    if current_rule != (item["classification"], item["category"]):
        return "cleanup classification changed after scan"
    if path.is_dir() and (path / ".git").exists():
        return "path is now a repository/worktree boundary"
    return None


def relocated_failure(item: dict, path: Path, original_path: Path) -> Optional[str]:
    try:
        current = identity(path)
    except OSError as error:
        return f"cannot inspect relocated item: {error}"
    for key in ("device", "inode", "mode"):
        if current[key] != item["identity"][key]:
            return "filesystem identity changed before atomic quarantine"
    fingerprint, hazard = tree_metadata_snapshot(path)
    if hazard:
        return hazard
    if fingerprint != item.get("tree_fingerprint"):
        return "tree metadata changed before atomic quarantine"
    if allocated_size(path) != item["allocated_bytes"]:
        return "allocated size changed before atomic quarantine"
    external_failure = external_policy_failure(item, original_path)
    if external_failure:
        return external_failure
    return None


def restore_relocated(relocated: Path, original: Path) -> None:
    relocated_state = path_presence(relocated)
    original_state = path_presence(original)
    if relocated_state is None or original_state is None:
        raise OSError(
            f"cannot verify recovery state; inspect relocated={relocated} original={original}"
        )
    if not relocated_state:
        raise FileNotFoundError(f"quarantined path disappeared: {relocated}")
    if original_state:
        raise FileExistsError(f"original path was recreated; data remains quarantined at {relocated}")
    os.replace(relocated, original)


def path_presence(path: Path) -> Optional[bool]:
    """Return existence including symlinks, or None when state is unverifiable."""
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return None
    return True


def apply_platform_supported() -> bool:
    return sys.platform == "darwin" or sys.platform.startswith("linux")


def _directory_state(path: Path, *, private: bool, allow_missing: bool) -> Optional[str]:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None if allow_missing else f"required directory does not exist: {path}"
    except OSError as error:
        return f"cannot inspect directory {path}: {error}"
    if stat.S_ISLNK(info.st_mode):
        return f"directory must not be a symlink: {path}"
    if not stat.S_ISDIR(info.st_mode):
        return f"path is not a directory: {path}"
    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        return f"directory is not owned by the current user: {path}"
    permissions = stat.S_IMODE(info.st_mode)
    if private and permissions & 0o077:
        return f"directory permissions are not private (expected 0700): {path}"
    if not private and permissions & 0o022:
        return f"directory is writable by another user/group: {path}"
    return None


def trash_policy_context() -> dict:
    """Describe and validate the exact OS Trash location without creating it."""
    if not apply_platform_supported():
        raise ValueError("OS Trash apply is unsupported on this platform; audit only")
    home = real(Path.home())
    if sys.platform == "darwin":
        anchor = home
        base_raw = home / ".Trash"
        files_raw = base_raw
        info_raw: Optional[Path] = None
        backend = "macos-trash"
    else:
        configured = os.environ.get("XDG_DATA_HOME")
        data_home_raw = Path(configured) if configured else home / ".local" / "share"
        if not data_home_raw.is_absolute():
            raise ValueError("XDG_DATA_HOME must be absolute for Trash apply")
        anchor = real(data_home_raw)
        base_raw = anchor / "Trash"
        files_raw = base_raw / "files"
        info_raw = base_raw / "info"
        backend = "freedesktop-trash"

    anchor_error = _directory_state(anchor, private=False, allow_missing=True)
    if anchor_error:
        raise ValueError(anchor_error)
    for path in tuple(dict.fromkeys([base_raw, files_raw, *([info_raw] if info_raw else [])])):
        error = _directory_state(path, private=True, allow_missing=True)
        if error:
            raise ValueError(error)
    return {
        "backend": backend,
        "platform": sys.platform,
        "anchor": str(real(anchor)),
        "base_root": str(real(base_raw)),
        "files_root": str(real(files_raw)),
        "info_root": str(real(info_raw)) if info_raw is not None else None,
    }


def _ensure_directory(path: Path, *, private: bool, parents: bool = False) -> None:
    try:
        path.mkdir(mode=0o700, parents=parents, exist_ok=True)
    except OSError as error:
        raise PreconditionFailed(f"cannot create Trash directory {path}: {error}") from error
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise PreconditionFailed(f"cannot safely open Trash directory {path}: {error}") from error
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode):
            raise PreconditionFailed(f"Trash path is not a directory: {path}")
        if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
            raise PreconditionFailed(f"Trash directory is not owned by the current user: {path}")
        if private:
            os.fchmod(descriptor, 0o700)
            info = os.fstat(descriptor)
            if stat.S_IMODE(info.st_mode) & 0o077:
                raise PreconditionFailed(f"could not enforce private Trash permissions: {path}")
        elif stat.S_IMODE(info.st_mode) & 0o022:
            raise PreconditionFailed(f"Trash anchor is writable by another user/group: {path}")
    except OSError as error:
        raise PreconditionFailed(f"cannot secure Trash directory {path}: {error}") from error
    finally:
        os.close(descriptor)


def prepare_trash_context(expected: dict) -> tuple[Path, Optional[Path]]:
    try:
        current = trash_policy_context()
    except ValueError as error:
        raise PreconditionFailed(str(error)) from error
    if current != expected:
        raise PreconditionFailed("Trash backend or canonical root changed after approval")
    anchor = Path(current["anchor"])
    try:
        anchor.lstat()
    except FileNotFoundError:
        _ensure_directory(anchor, private=True, parents=True)
    except OSError as error:
        raise PreconditionFailed(f"cannot inspect Trash anchor {anchor}: {error}") from error
    base = Path(current["base_root"])
    _ensure_directory(base, private=True, parents=True)
    files = Path(current["files_root"])
    _ensure_directory(files, private=True, parents=True)
    info = Path(current["info_root"]) if current["info_root"] else None
    if info is not None:
        _ensure_directory(info, private=True, parents=True)
    try:
        after = trash_policy_context()
    except ValueError as error:
        raise PreconditionFailed(str(error)) from error
    if after != expected:
        raise PreconditionFailed("Trash backend or canonical root changed while preparing apply")
    return files, info


def mutation_context(mode: str) -> dict:
    if mode == "trash":
        return trash_policy_context()
    return {"backend": "permanent", "platform": sys.platform}


def write_new_text(path: Path, value: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        raise


def move_to_trash(
    path: Path,
    item: dict,
    trash_root: Path,
    info_root: Optional[Path],
    destination: Path,
    info_path: Optional[Path],
) -> tuple[Path, Optional[Path]]:
    failure = _directory_state(trash_root, private=True, allow_missing=False)
    if failure:
        raise PreconditionFailed(failure)
    if path.lstat().st_dev != trash_root.lstat().st_dev:
        raise OSError(errno.EXDEV, "Trash target is on another filesystem; refusing copy-and-delete fallback")
    if destination.parent != trash_root:
        raise PreconditionFailed("Trash destination escaped its prepared root")
    destination_state = path_presence(destination)
    if destination_state is None:
        raise PreconditionFailed(f"cannot verify Trash destination state: {destination}")
    if destination_state:
        raise FileExistsError(f"trash destination exists: {destination}")
    if info_root is not None:
        failure = _directory_state(info_root, private=True, allow_missing=False)
        if failure:
            raise PreconditionFailed(failure)
        if info_path is None or info_path.parent != info_root:
            raise PreconditionFailed("Trash metadata path escaped its prepared root")
        deleted_at = dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        write_new_text(
            info_path,
            f"[Trash Info]\nPath={urllib.parse.quote(str(path))}\nDeletionDate={deleted_at}\n",
        )
    try:
        os.replace(path, destination)
        failure = relocated_failure(item, destination, path)
        if failure:
            raise PreconditionFailed(failure)
    except BaseException as error:
        restore_error: Optional[OSError] = None
        destination_state = path_presence(destination)
        original_state = path_presence(path)
        if destination_state is None or original_state is None:
            raise PreconditionFailed(
                f"{error}; recovery state is unverifiable; inspect destination={destination} "
                f"and original={path}"
            ) from error
        if destination_state and not original_state:
            try:
                restore_relocated(destination, path)
            except OSError as caught:
                restore_error = caught
        if info_path is not None:
            try:
                info_path.unlink()
            except OSError:
                pass
        if restore_error is not None:
            raise PreconditionFailed(
                f"{error}; automatic restore failed; data may remain at {destination}: {restore_error}"
            ) from restore_error
        raise
    return destination, info_path


def trash_destination_paths(
    path: Path,
    item: dict,
    batch_label: str,
    trash_root: Path,
    info_root: Optional[Path],
) -> tuple[Path, Optional[Path]]:
    leaf = path.name[:80]
    destination = trash_root / f"CodexCleanup-{batch_label}-{item['id']}-{leaf}"
    info_path = info_root / f"{destination.name}.trashinfo" if info_root is not None else None
    return destination, info_path


def delete_permanently(path: Path, item: dict) -> None:
    try:
        path_info = path.lstat()
    except OSError as error:
        raise PreconditionFailed(f"cannot inspect permanent-deletion target: {error}") from error
    if stat.S_ISDIR(path_info.st_mode):
        if not getattr(shutil.rmtree, "avoids_symlink_attacks", False):
            raise PreconditionFailed(
                "this Python runtime cannot prove symlink-safe recursive deletion; use Trash mode"
            )
        tombstone = path.with_name(f".codex-disk-cleanup-deleting-{uuid.uuid4().hex}")
        os.replace(path, tombstone)
        failure = relocated_failure(item, tombstone, path)
        if failure:
            try:
                restore_relocated(tombstone, path)
            except OSError as restore_error:
                raise PreconditionFailed(f"{failure}; restore failed: {restore_error}") from restore_error
            raise PreconditionFailed(failure)
        try:
            shutil.rmtree(tombstone)
        except BaseException as error:
            try:
                tombstone.lstat()
                tombstone_present = True
            except FileNotFoundError:
                tombstone_present = False
            except OSError as state_error:
                raise PartialDeletionError(
                    f"permanent directory deletion failed; partial deletion may have occurred; "
                    f"cannot inspect remainder at {tombstone}: {state_error}; original error: {error}"
                ) from state_error
            try:
                path.lstat()
                original_present = True
            except FileNotFoundError:
                original_present = False
            except OSError as state_error:
                raise PartialDeletionError(
                    f"permanent directory deletion failed; partial deletion may have occurred; "
                    f"cannot inspect original path {path}: {state_error}; remainder may be at {tombstone}"
                ) from state_error
            if tombstone_present and not original_present:
                try:
                    os.replace(tombstone, path)
                except OSError as restore_error:
                    raise PartialDeletionError(
                        f"permanent directory deletion failed; partial deletion may have occurred; "
                        f"recovery data remains at {tombstone}: {restore_error}; original error: {error}"
                    ) from restore_error
                raise PartialDeletionError(
                    f"permanent directory deletion failed; partial deletion may have occurred; "
                    f"remaining data was restored to {path}: {error}"
                ) from error
            location = tombstone if tombstone_present else path
            raise PartialDeletionError(
                f"permanent directory deletion failed; partial deletion may have occurred; "
                f"inspect {location}: {error}"
            ) from error
    else:
        tombstone = path.with_name(f".codex-disk-cleanup-deleting-{uuid.uuid4().hex}")
        os.replace(path, tombstone)
        failure = relocated_failure(item, tombstone, path)
        if failure:
            try:
                restore_relocated(tombstone, path)
            except OSError as restore_error:
                raise PreconditionFailed(f"{failure}; restore failed: {restore_error}") from restore_error
            raise PreconditionFailed(failure)
        try:
            tombstone.unlink()
        except OSError as error:
            try:
                restore_relocated(tombstone, path)
            except OSError as restore_error:
                raise PreconditionFailed(
                    f"{error}; permanent deletion failed and recovery data remains at "
                    f"{tombstone}: {restore_error}"
                ) from restore_error
            raise


def _plain_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _sha256_text(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def existing_plan_path(value: Path) -> Path:
    expanded = Path(os.path.abspath(os.path.expanduser(str(value))))
    if expanded.is_symlink():
        raise ValueError(f"plan must not be a symlink: {expanded}")
    try:
        info = expanded.stat()
    except OSError as error:
        raise ValueError(f"cannot inspect plan: {error}") from error
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("plan is not a regular file")
    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        raise ValueError("plan is not owned by the current user")
    if os.name != "nt" and stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError("plan permissions are too broad; expected mode 0600")
    return real(expanded)


def validate_plan_schema(plan: dict) -> None:
    """Treat a plan as untrusted input, even when its checksum is valid."""
    if not isinstance(plan, dict):
        raise ValueError("plan must be a JSON object")
    if plan.get("kind") != PLAN_KIND or plan.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported plan schema")
    if not _sha256_text(plan.get("seal")) or plan["seal"] != plan_seal(plan):
        raise ValueError("plan seal mismatch: the plan changed")
    if plan.get("host") != platform.node():
        raise ValueError("plan belongs to another host; scan again")
    if plan.get("codex_context") != codex_context():
        raise ValueError("CODEX_HOME/path policy changed since scan; scan again")

    try:
        created = parse_time(plan["created_at"])
        expires = parse_time(plan["expires_at"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("plan timestamps are malformed") from error
    if created.tzinfo is None or expires.tzinfo is None or expires <= created:
        raise ValueError("plan timestamp interval is invalid")
    if expires - created > dt.timedelta(hours=PLAN_TTL_HOURS, minutes=1):
        raise ValueError("plan lifetime exceeds policy")
    now = utc_now()
    skew = dt.timedelta(minutes=PLAN_CLOCK_SKEW_MINUTES)
    if created > now + skew:
        raise ValueError("plan was created too far in the future")
    if expires > now + dt.timedelta(hours=PLAN_TTL_HOURS) + skew:
        raise ValueError("plan expiry is too far in the future")
    if now > expires:
        raise ValueError("plan expired; scan again")

    raw_roots = plan.get("roots")
    if not isinstance(raw_roots, list) or not raw_roots:
        raise ValueError("plan must contain at least one explicit root")
    roots: list[Path] = []
    for value in raw_roots:
        if not isinstance(value, str) or not value:
            raise ValueError("plan root is malformed")
        root = validate_scan_root(Path(value))
        if str(root) != value:
            raise ValueError("plan root is not canonical")
        if any(same_path(root, prior) for prior in roots):
            raise ValueError("plan contains duplicate roots")
        roots.append(root)

    coverage = plan.get("coverage")
    if not isinstance(coverage, dict) or not isinstance(coverage.get("complete"), bool):
        raise ValueError("plan coverage metadata is malformed")
    warnings = coverage.get("warnings")
    if not isinstance(warnings, list) or not all(isinstance(value, str) for value in warnings):
        raise ValueError("plan coverage warnings are malformed")
    if coverage["complete"] != (len(warnings) == 0):
        raise ValueError("plan coverage status is inconsistent")
    notes = plan.get("notes")
    if not isinstance(notes, list) or not all(isinstance(value, str) for value in notes):
        raise ValueError("plan notes are malformed")

    items = plan.get("items")
    if not isinstance(items, list):
        raise ValueError("plan items are malformed")
    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    identity_keys = {"device", "inode", "mode", "mtime_ns", "ctime_ns"}
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("plan item is not an object")
        item_id = item.get("id")
        path_value = item.get("path")
        root_value = item.get("root")
        if (
            not isinstance(item_id, str)
            or len(item_id) != 16
            or any(character not in "0123456789abcdef" for character in item_id)
        ):
            raise ValueError("plan item ID is malformed")
        if item_id in seen_ids:
            raise ValueError("plan contains duplicate item IDs")
        if not isinstance(path_value, str) or not isinstance(root_value, str):
            raise ValueError("plan item path/root is malformed")
        path = Path(path_value)
        known_cache = known_user_cache_spec(path)
        root_matches = [root for root in roots if str(root) == root_value]
        if str(real(path)) != path_value:
            raise ValueError("plan item path/root is not canonical")
        if len(root_matches) == 1:
            root = root_matches[0]
        elif len(root_matches) == 0 and known_cache is not None:
            root = real(path.parent)
            if root_value != str(root) or path_value != known_cache["path"]:
                raise ValueError("known user-cache item has an invalid boundary")
        else:
            raise ValueError("plan item is not bound to an approved root")
        if not is_within(path, root, allow_equal=False):
            raise ValueError("plan item escaped its approved root")
        boundary_failure = filesystem_boundary_failure(path, root)
        if boundary_failure:
            raise ValueError(boundary_failure)
        protected_failure = protected_item_failure(item, path, root, known_cache)
        if protected_failure:
            raise ValueError(protected_failure)
        expected_id = hashlib.sha256(path_value.encode("utf-8")).hexdigest()[:16]
        if item_id != expected_id:
            raise ValueError("plan item ID does not match its path")
        if path_value in seen_paths:
            raise ValueError("plan contains duplicate item paths")
        seen_ids.add(item_id)
        seen_paths.add(path_value)

        if item.get("kind") not in {"file", "directory"}:
            raise ValueError("plan item kind is malformed")
        if item.get("classification") not in {"safe", "review", "never-delete"}:
            raise ValueError("plan item classification is malformed")
        category = item.get("category")
        if not isinstance(category, str) or not category:
            raise ValueError("plan item category is malformed")
        if known_cache is not None:
            if item.get("kind") != "directory":
                raise ValueError("known user-cache item must be a directory")
            if category != known_cache["category"]:
                raise ValueError("known user-cache item category is forged")
            if (
                item["classification"] != "never-delete"
                and item["classification"] != known_cache["classification"]
            ):
                raise ValueError("known user-cache item classification is forged")
            if item.get("requires_process_stopped") != known_cache["requires_process_stopped"]:
                raise ValueError("known user-cache process requirement is forged")
            if item.get("regeneration") != known_cache["regeneration"]:
                raise ValueError("known user-cache regeneration policy is forged")
            if not isinstance(item.get("reason"), str) or not item["reason"].startswith(
                known_cache["reason"]
            ):
                raise ValueError("known user-cache reason is forged")
        elif item.get("requires_process_stopped") is not None:
            raise ValueError("project-local item has an unexpected process requirement")
        if item["classification"] == "safe" and category not in SAFE_WITHOUT_GIT_CATEGORIES:
            if not isinstance(item.get("repo"), str) or item.get("git_ignored") is not True:
                raise ValueError("project-local safe item lacks affirmative Git-ignore evidence")
        for key in ("reason", "regeneration", "tree_fingerprint"):
            if not isinstance(item.get(key), str):
                raise ValueError(f"plan item {key} is malformed")
        if not _sha256_text(item["tree_fingerprint"]):
            raise ValueError("plan item tree fingerprint is malformed")
        if not _plain_int(item.get("allocated_bytes")) or item["allocated_bytes"] < 0:
            raise ValueError("plan item size is malformed")
        if item.get("repo") is not None and not isinstance(item.get("repo"), str):
            raise ValueError("plan item repository is malformed")
        for key in ("git_ignored", "tracked_content"):
            if item.get(key) is not None and not isinstance(item.get(key), bool):
                raise ValueError(f"plan item {key} is malformed")
        for key in ("git_error", "requires_process_stopped"):
            if item.get(key) is not None and not isinstance(item.get(key), str):
                raise ValueError(f"plan item {key} is malformed")
        if not isinstance(item.get("policy_evidence"), dict):
            raise ValueError("plan item policy evidence is malformed")
        item_identity = item.get("identity")
        if not isinstance(item_identity, dict) or set(item_identity) != identity_keys:
            raise ValueError("plan item identity is malformed")
        if not all(_plain_int(item_identity[key]) for key in identity_keys):
            raise ValueError("plan item identity fields are malformed")


def read_valid_plan(plan_path: Path) -> dict:
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read plan: {error}") from error
    validate_plan_schema(plan)
    return plan


def mutation_mode(args: argparse.Namespace) -> str:
    if args.permanent == args.trash:
        raise ValueError("choose exactly one mutation mode: --trash or --permanent")
    if args.permanent and not args.ack_no_recovery:
        raise ValueError("--permanent also requires --ack-no-recovery")
    return "permanent" if args.permanent else "trash"


def selected_items(plan: dict, args: argparse.Namespace) -> list[dict]:
    selected_ids = {value.strip() for value in args.ids.split(",") if value.strip()} if args.ids else set()
    selected: list[dict] = []
    for item in plan["items"]:
        if args.safe and item["classification"] == "safe":
            selected.append(item)
        elif item["id"] in selected_ids:
            selected.append(item)
    if not selected:
        raise ValueError("nothing selected; use --safe and/or --ids ID,ID")
    unknown = selected_ids - {item["id"] for item in plan["items"]}
    if unknown:
        raise ValueError(f"unknown item IDs: {', '.join(sorted(unknown))}")
    forbidden = [item["id"] for item in selected if item["classification"] == "never-delete"]
    if forbidden:
        raise ValueError(f"policy forbids selected IDs: {', '.join(sorted(forbidden))}")
    return selected


def selection_token(plan: dict, selected: list[dict], mode: str, context: dict) -> str:
    payload = {
        "plan_seal": plan["seal"],
        "mode": mode,
        "mutation_context": context,
        "ids": sorted(item["id"] for item in selected),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def command_token(args: argparse.Namespace) -> int:
    try:
        plan_path = existing_plan_path(Path(args.plan))
        plan = read_valid_plan(plan_path)
        mode = mutation_mode(args)
        selected = selected_items(plan, args)
        context = mutation_context(mode)
    except ValueError as error:
        eprint(f"Policy error: {error}")
        return 2
    total = sum(item["allocated_bytes"] for item in selected)
    token = selection_token(plan, selected, mode, context)
    print(f"Plan: {display_path(plan_path)}")
    print(f"Selection: {len(selected)} items ({human_size(total)})")
    print(f"Mode: {mode}")
    if mode == "trash":
        print(f"Trash backend: {context['backend']}")
        print(f"Trash root: {display_path(context['files_root'])}")
    print("Selected IDs and paths:")
    for item in selected:
        print(f"- {item['id']} {display_path(item['path'])}")
    print(f"Approval token: {token}")
    return 0


def append_journal(handle, event: dict) -> None:
    handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
    handle.flush()
    os.fsync(handle.fileno())


def open_execution_journal(
    plan_path: Path, plan: dict, token: str, mode: str, selected: list[dict]
):
    path = new_output_path(
        plan_path.with_name(
            f"{plan_path.stem}.journal-{utc_now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}.jsonl"
        )
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    handle = os.fdopen(descriptor, "w", encoding="utf-8")
    append_journal(
        handle,
        {
            "type": "header",
            "schema_version": SCHEMA_VERSION,
            "plan": str(plan_path),
            "plan_seal": plan["seal"],
            "approval_token": token,
            "mode": mode,
            "started_at": iso_time(utc_now()),
            "selected_ids": [item["id"] for item in selected],
        },
    )
    return path, handle


def command_apply(args: argparse.Namespace) -> int:
    if not apply_platform_supported():
        eprint("Policy error: apply is unsupported on this platform; audit only")
        return 2
    try:
        plan_path = existing_plan_path(Path(args.plan))
        plan = read_valid_plan(plan_path)
        mode = mutation_mode(args)
        selected = selected_items(plan, args)
        context = mutation_context(mode)
    except ValueError as error:
        eprint(f"Policy error: {error}")
        return 2
    expected_confirmation = selection_token(plan, selected, mode, context)
    if args.confirm != expected_confirmation:
        eprint("Approval token mismatch: selection or mutation mode differs from the approved operation")
        return 2

    trash_root: Optional[Path] = None
    trash_info_root: Optional[Path] = None
    if mode == "trash":
        try:
            trash_root, trash_info_root = prepare_trash_context(context)
        except PreconditionFailed as error:
            eprint(f"Policy error: {error}; no target was changed")
            return 2

    try:
        journal_path, journal = open_execution_journal(
            plan_path, plan, expected_confirmation, mode, selected
        )
    except OSError as error:
        eprint(f"Journal error: {error}; no target was changed")
        return 2

    batch_label = f"{utc_now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    results = []
    reclaimed = 0
    try:
        for item in selected:
            expected_destination: Optional[Path] = None
            expected_info_path: Optional[Path] = None
            attempt = {"type": "attempt", "id": item["id"], "path": item["path"]}
            if args.trash:
                assert trash_root is not None
                expected_destination, expected_info_path = trash_destination_paths(
                    Path(item["path"]), item, batch_label, trash_root, trash_info_root
                )
                attempt["expected_trash_path"] = str(expected_destination)
                if expected_info_path is not None:
                    attempt["expected_trash_info_path"] = str(expected_info_path)
            append_journal(journal, attempt)
            failure = revalidate_item(item, plan_path)
            if failure:
                event = {"id": item["id"], "path": item["path"], "status": "skipped", "reason": failure}
                results.append(event)
                append_journal(journal, {"type": "item", **event})
                continue
            path = Path(item["path"])
            try:
                if args.trash:
                    assert trash_root is not None
                    assert expected_destination is not None
                    destination, info_path = move_to_trash(
                        path,
                        item,
                        trash_root,
                        trash_info_root,
                        expected_destination,
                        expected_info_path,
                    )
                    event = {
                        "id": item["id"],
                        "path": item["path"],
                        "status": "trashed",
                        "trash_path": str(destination),
                    }
                    if info_path is not None:
                        event["trash_info_path"] = str(info_path)
                else:
                    delete_permanently(path, item)
                    reclaimed += item["allocated_bytes"]
                    event = {"id": item["id"], "path": item["path"], "status": "deleted"}
                results.append(event)
                append_journal(journal, {"type": "item", **event})
            except PartialDeletionError as error:
                event = {"id": item["id"], "path": item["path"], "status": "error", "reason": str(error)}
                results.append(event)
                append_journal(journal, {"type": "item", **event})
            except PreconditionFailed as error:
                event = {"id": item["id"], "path": item["path"], "status": "skipped", "reason": str(error)}
                results.append(event)
                append_journal(journal, {"type": "item", **event})
            except OSError as error:
                event = {"id": item["id"], "path": item["path"], "status": "error", "reason": str(error)}
                results.append(event)
                append_journal(journal, {"type": "item", **event})
        append_journal(
            journal,
            {
                "type": "summary",
                "completed_at": iso_time(utc_now()),
                "changed": sum(1 for value in results if value["status"] in {"deleted", "trashed"}),
                "skipped": sum(1 for value in results if value["status"] == "skipped"),
                "errors": sum(1 for value in results if value["status"] == "error"),
                "estimated_reclaimed_bytes": reclaimed,
            },
        )
    finally:
        journal.close()

    result_path = plan_path.with_name(
        f"{plan_path.stem}.result-{utc_now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}.json"
    )
    result = {
        "schema_version": SCHEMA_VERSION,
        "plan": str(plan_path),
        "plan_seal": plan["seal"],
        "approval_token": expected_confirmation,
        "completed_at": iso_time(utc_now()),
        "mode": mode,
        "journal": str(journal_path),
        "estimated_reclaimed_bytes": reclaimed,
        "items": results,
    }
    write_secure_json(result_path, result)
    changed = sum(1 for value in results if value["status"] in {"deleted", "trashed"})
    skipped = sum(1 for value in results if value["status"] == "skipped")
    errors = sum(1 for value in results if value["status"] == "error")
    print(f"Completed: changed={changed}, skipped={skipped}, errors={errors}")
    if args.permanent:
        print(f"Estimated space reclaimed: {human_size(reclaimed)}")
    else:
        print(f"Moved to OS Trash under batch label: {batch_label}")
        print("Trash mode does not reclaim disk space until those exact items are purged.")
    print(f"Append-only execution journal: {display_path(journal_path)}")
    print(f"Execution log: {display_path(result_path)}")
    return 1 if errors or skipped else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Safely inventory and remove reproducible project/build junk. Scan is always read-only."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan = subparsers.add_parser("scan", help="create a sealed, read-only cleanup plan")
    scan.add_argument("--root", action="append", required=True, help="explicit project parent/root; repeatable")
    scan.add_argument("--plan", help="plan JSON path; defaults to a secure temporary file")
    scan.add_argument("--include-user-caches", action="store_true", help="include known user-level developer caches")
    scan.add_argument(
        "--include-emulator-snapshots",
        action="store_true",
        help="with --include-user-caches, add Android quick-boot snapshots as review items",
    )
    scan.set_defaults(func=command_scan)

    token = subparsers.add_parser("token", help="derive a token bound to an exact selection and mutation mode")
    token.add_argument("--plan", required=True, help="sealed scan plan JSON")
    token.add_argument("--safe", action="store_true", help="select every item classified safe")
    token.add_argument("--ids", help="comma-separated exact IDs; required for review items")
    token_mode = token.add_mutually_exclusive_group(required=True)
    token_mode.add_argument("--trash", action="store_true", help="bind approval to Trash mode")
    token_mode.add_argument("--permanent", action="store_true", help="bind approval to permanent deletion")
    token.add_argument(
        "--ack-no-recovery",
        action="store_true",
        help="required with --permanent; confirms there is no recovery promise",
    )
    token.set_defaults(func=command_token)

    apply = subparsers.add_parser("apply", help="apply exact items from a fresh sealed plan")
    apply.add_argument("--plan", required=True, help="sealed plan JSON")
    apply.add_argument("--confirm", required=True, help="approval token printed by the token command")
    apply.add_argument("--safe", action="store_true", help="select every item classified safe")
    apply.add_argument("--ids", help="comma-separated exact IDs; required for review items")
    mode = apply.add_mutually_exclusive_group(required=True)
    mode.add_argument("--trash", action="store_true", help="move selected paths to a dedicated Trash batch")
    mode.add_argument("--permanent", action="store_true", help="delete selected paths permanently")
    apply.add_argument(
        "--ack-no-recovery",
        action="store_true",
        help="required with --permanent; confirms there is no recovery promise",
    )
    apply.set_defaults(func=command_apply)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

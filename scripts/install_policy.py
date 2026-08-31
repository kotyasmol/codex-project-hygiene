#!/usr/bin/env python3
"""Preview and atomically install persistent project-hygiene instructions."""

import argparse
import difflib
import hashlib
import json
import os
import stat
import sys
import tempfile
from pathlib import Path
from typing import Dict, Optional, Tuple


START_MARKER = "<!-- codex-project-hygiene:start -->"
END_MARKER = "<!-- codex-project-hygiene:end -->"
MAX_AGENTS_BYTES = 256 * 1024
TOKEN_DOMAIN = b"codex-project-hygiene-policy-v1\0"


class PolicyError(RuntimeError):
    """A fail-closed policy installation error."""


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def resolve_project_root(raw_root: str) -> Path:
    requested = Path(raw_root).expanduser()
    absolute = Path(os.path.abspath(str(requested)))
    try:
        root = absolute.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise PolicyError("project root cannot be resolved: %s" % error)
    if absolute != root:
        raise PolicyError("project root must not be a symlink or contain symlink ancestors")
    try:
        root_info = os.lstat(root)
    except OSError as error:
        raise PolicyError("project root cannot be inspected: %s" % error)
    if not stat.S_ISDIR(root_info.st_mode):
        raise PolicyError("project root is not a directory")

    filesystem_root = Path(root.anchor)
    home = Path.home().resolve()
    if root == filesystem_root or root == home or root in home.parents:
        raise PolicyError("project policy cannot target a filesystem root, home, or home ancestor")
    return root


def load_fragment() -> str:
    fragment_path = (
        Path(__file__).resolve().parent.parent
        / "assets"
        / "AGENTS.project-hygiene.md"
    )
    try:
        text = fragment_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise PolicyError("bundled policy fragment cannot be read: %s" % error)
    text = text.strip()
    if not text or START_MARKER in text or END_MARKER in text:
        raise PolicyError("bundled policy fragment is empty or contains reserved markers")
    return text


def read_agents(path: Path) -> Tuple[Optional[bytes], Optional[os.stat_result]]:
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        return None, None
    except OSError as error:
        raise PolicyError("AGENTS.md cannot be inspected: %s" % error)
    if stat.S_ISLNK(before.st_mode):
        raise PolicyError("AGENTS.md must not be a symlink")
    if not stat.S_ISREG(before.st_mode):
        raise PolicyError("AGENTS.md must be a regular file")
    if before.st_size > MAX_AGENTS_BYTES:
        raise PolicyError("AGENTS.md exceeds the 256 KiB installation limit")

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise PolicyError("AGENTS.md cannot be opened without following links: %s" % error)
    try:
        opened = os.fstat(descriptor)
        after = os.lstat(path)
        before_identity = (before.st_dev, before.st_ino, stat.S_IFMT(before.st_mode))
        opened_identity = (opened.st_dev, opened.st_ino, stat.S_IFMT(opened.st_mode))
        after_identity = (after.st_dev, after.st_ino, stat.S_IFMT(after.st_mode))
        if before_identity != opened_identity or opened_identity != after_identity:
            raise PolicyError("AGENTS.md changed while it was being opened")
        if not stat.S_ISREG(opened.st_mode):
            raise PolicyError("AGENTS.md must remain a regular file")
        chunks = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(65536, MAX_AGENTS_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_AGENTS_BYTES:
                raise PolicyError("AGENTS.md changed beyond the 256 KiB installation limit")
        data = b"".join(chunks)
    except OSError as error:
        raise PolicyError("AGENTS.md cannot be read safely: %s" % error)
    finally:
        os.close(descriptor)
    if len(data) > MAX_AGENTS_BYTES:
        raise PolicyError("AGENTS.md changed beyond the 256 KiB installation limit")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise PolicyError("AGENTS.md is not valid UTF-8: %s" % error)
    if "\x00" in text:
        raise PolicyError("AGENTS.md contains a NUL byte")
    return data, opened


def build_managed_block(fragment: str) -> str:
    return "%s\n%s\n%s" % (START_MARKER, fragment, END_MARKER)


def propose(existing_bytes: Optional[bytes], fragment: str) -> Tuple[str, bytes]:
    existing = "" if existing_bytes is None else existing_bytes.decode("utf-8")
    start_count = existing.count(START_MARKER)
    end_count = existing.count(END_MARKER)
    if start_count != end_count or start_count > 1:
        raise PolicyError("AGENTS.md has missing or duplicate hygiene policy markers")

    block = build_managed_block(fragment)
    if start_count == 1:
        start = existing.index(START_MARKER)
        end = existing.index(END_MARKER, start) + len(END_MARKER)
        candidate = existing[:start] + block + existing[end:]
        action = "already-active" if candidate == existing else "update"
    else:
        if not existing:
            candidate = block + "\n"
        elif existing.endswith("\n\n"):
            candidate = existing + block + "\n"
        elif existing.endswith("\n"):
            candidate = existing + "\n" + block + "\n"
        else:
            candidate = existing + "\n\n" + block + "\n"
        action = "create" if existing_bytes is None else "append"

    candidate_bytes = candidate.encode("utf-8")
    if len(candidate_bytes) > MAX_AGENTS_BYTES:
        raise PolicyError("resulting AGENTS.md would exceed 256 KiB")
    return action, candidate_bytes


def approval_token(root: Path, existing: Optional[bytes], proposed: bytes) -> str:
    payload = {
        "root": str(root),
        "target": str(root / "AGENTS.md"),
        "existing_sha256": "missing" if existing is None else sha256_bytes(existing),
        "proposed_sha256": sha256_bytes(proposed),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256_bytes(TOKEN_DOMAIN + encoded)


def calculate(raw_root: str) -> Dict[str, object]:
    root = resolve_project_root(raw_root)
    target = root / "AGENTS.md"
    existing, info = read_agents(target)
    action, proposed = propose(existing, load_fragment())
    token = approval_token(root, existing, proposed)
    return {
        "root": root,
        "root_info": os.lstat(root),
        "target": target,
        "existing": existing,
        "existing_info": info,
        "proposed": proposed,
        "action": action,
        "token": token,
    }


def render_preview(calculation: Dict[str, object], output_format: str) -> str:
    root = calculation["root"]
    target = calculation["target"]
    existing = calculation["existing"]
    proposed = calculation["proposed"]
    action = calculation["action"]
    token = calculation["token"]
    assert isinstance(root, Path)
    assert isinstance(target, Path)
    assert existing is None or isinstance(existing, bytes)
    assert isinstance(proposed, bytes)
    assert isinstance(action, str)
    assert isinstance(token, str)

    if output_format == "json":
        result = {
            "kind": "codex-project-hygiene-policy-preview",
            "schema_version": 1,
            "root": str(root),
            "target": str(target),
            "action": action,
            "existing_sha256": "missing" if existing is None else sha256_bytes(existing),
            "proposed_sha256": sha256_bytes(proposed),
            "approval_token": token,
            "mutation_performed": False,
        }
        return json.dumps(result, sort_keys=True, ensure_ascii=True) + "\n"

    before = "" if existing is None else existing.decode("utf-8")
    after = proposed.decode("utf-8")
    diff = "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=str(target) if existing is not None else "/dev/null",
            tofile=str(target),
        )
    )
    lines = [
        "Project hygiene policy preview",
        "Root: %s" % root,
        "Target: %s" % target,
        "Action: %s" % action,
        "Approval token: %s" % token,
        "Mutation performed: no",
    ]
    if diff:
        lines.extend(("", diff.rstrip("\n")))
    return "\n".join(lines) + "\n"


def state_matches(calculation: Dict[str, object]) -> bool:
    root = calculation["root"]
    root_info = calculation["root_info"]
    target = calculation["target"]
    existing = calculation["existing"]
    assert isinstance(root, Path)
    assert isinstance(root_info, os.stat_result)
    assert isinstance(target, Path)
    assert existing is None or isinstance(existing, bytes)
    current_root = os.lstat(root)
    if (current_root.st_dev, current_root.st_ino) != (root_info.st_dev, root_info.st_ino):
        return False
    current, _info = read_agents(target)
    return current == existing


def atomic_install(calculation: Dict[str, object]) -> None:
    root = calculation["root"]
    target = calculation["target"]
    proposed = calculation["proposed"]
    existing_info = calculation["existing_info"]
    assert isinstance(root, Path)
    assert isinstance(target, Path)
    assert isinstance(proposed, bytes)
    assert existing_info is None or isinstance(existing_info, os.stat_result)

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".AGENTS.md.codex-hygiene-",
        suffix=".tmp",
        dir=str(root),
    )
    temporary: Optional[Path] = Path(temporary_name)
    try:
        mode = 0o644 if existing_info is None else stat.S_IMODE(existing_info.st_mode)
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, mode)
        else:
            os.chmod(temporary, mode)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = -1
            stream.write(proposed)
            stream.flush()
            os.fsync(stream.fileno())
        if not state_matches(calculation):
            raise PolicyError("project root or AGENTS.md changed after preview; preview again")
        os.replace(temporary, target)
        temporary = None
        try:
            directory_fd = os.open(root, os.O_RDONLY)
        except OSError:
            directory_fd = -1
        if directory_fd >= 0:
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None and temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Preview or install persistent project-level Codex hygiene instructions."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    preview = subparsers.add_parser("preview", help="show the bounded AGENTS.md change")
    preview.add_argument("--root", required=True, help="exact project root")
    preview.add_argument("--format", choices=("terminal", "json"), default="terminal")

    apply = subparsers.add_parser("apply", help="atomically apply the previewed change")
    apply.add_argument("--root", required=True, help="same exact project root")
    apply.add_argument("--confirm", required=True, help="approval token printed by preview")
    return parser


def main() -> int:
    arguments = build_parser().parse_args()
    try:
        calculation = calculate(arguments.root)
        if arguments.command == "preview":
            sys.stdout.write(render_preview(calculation, arguments.format))
            return 0

        if calculation["action"] == "already-active":
            sys.stdout.write("Project hygiene policy is already active: %s\n" % calculation["target"])
            return 0
        if arguments.confirm != calculation["token"]:
            raise PolicyError("approval token does not match the current project policy change")
        atomic_install(calculation)
        sys.stdout.write("Installed project hygiene policy: %s\n" % calculation["target"])
        return 0
    except (OSError, PolicyError) as error:
        sys.stderr.write("error: %s\n" % error)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

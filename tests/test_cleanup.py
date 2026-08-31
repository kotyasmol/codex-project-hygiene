#!/usr/bin/env python3
import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Optional
from unittest import mock


HELPER = Path(__file__).parent.parent / "scripts" / "cleanup.py"
HELPER_SPEC = importlib.util.spec_from_file_location("codex_cleanup_helper", HELPER)
assert HELPER_SPEC is not None and HELPER_SPEC.loader is not None
HELPER_MODULE = importlib.util.module_from_spec(HELPER_SPEC)
sys.modules[HELPER_SPEC.name] = HELPER_MODULE
HELPER_SPEC.loader.exec_module(HELPER_MODULE)


def run_helper(
    *args: str, cwd: Optional[Path] = None, env: Optional[dict[str, str]] = None
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HELPER), *args],
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=env,
    )


def write(path: Path, value: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def init_git_ignored(project: Path, *patterns: str) -> None:
    write(project / ".gitignore", "".join(f"{pattern}\n" for pattern in patterns))
    subprocess.run(["git", "init", "-q", str(project)], check=True)


class CleanupSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="codex-cleanup-test-"))
        self.root = self.temp / "projects"
        self.root.mkdir()
        self.plan = self.temp / "plan.json"

    def tearDown(self) -> None:
        shutil.rmtree(self.temp, ignore_errors=True)

    def scan(self) -> tuple[subprocess.CompletedProcess, dict]:
        result = run_helper("scan", "--root", str(self.root), "--plan", str(self.plan))
        plan = json.loads(self.plan.read_text(encoding="utf-8")) if self.plan.exists() else {}
        return result, plan

    def token(self, *, safe: bool = False, ids: str = "", mode: str = "--permanent") -> str:
        args = ["token", "--plan", str(self.plan), mode]
        if mode == "--permanent":
            args.append("--ack-no-recovery")
        if safe:
            args.append("--safe")
        if ids:
            args.extend(["--ids", ids])
        result = run_helper(*args)
        self.assertEqual(result.returncode, 0, result.stderr)
        return next(line.split(": ", 1)[1] for line in result.stdout.splitlines() if line.startswith("Approval token:"))

    def test_lockfile_backed_node_modules_is_safe(self) -> None:
        project = self.root / "app"
        write(project / "package.json", '{"scripts":{"build":"vite build"}}')
        write(project / "package-lock.json", "{}")
        write(project / "node_modules" / "pkg" / "index.js")
        init_git_ignored(project, "node_modules/")
        result, plan = self.scan()
        self.assertEqual(result.returncode, 0, result.stderr)
        item = next(value for value in plan["items"] if value["path"].endswith("node_modules"))
        self.assertEqual(item["classification"], "safe")

    def test_non_vcs_project_cache_is_review_only(self) -> None:
        project = self.root / "no-vcs"
        write(project / "package.json", "{}")
        write(project / "package-lock.json", "{}")
        write(project / "node_modules" / "pkg.js")
        _, plan = self.scan()
        item = next(value for value in plan["items"] if value["path"].endswith("node_modules"))
        self.assertEqual(item["classification"], "review")

    def test_outputs_tree_is_completely_protected(self) -> None:
        project = self.root / "task" / "outputs" / "site"
        write(project / "package.json", "{}")
        write(project / "package-lock.json", "{}")
        write(project / "node_modules" / "pkg" / "index.js")
        _, plan = self.scan()
        item = next(value for value in plan["items"] if value["path"].endswith("/outputs"))
        self.assertEqual(item["classification"], "never-delete")
        self.assertFalse(any("/outputs/" in value["path"] for value in plan["items"]))

    def test_singular_output_tree_is_completely_protected(self) -> None:
        project = self.root / "task" / "output" / "site"
        write(project / "package.json", "{}")
        write(project / "package-lock.json", "{}")
        write(project / "node_modules" / "pkg" / "index.js")
        _, plan = self.scan()
        item = next(value for value in plan["items"] if value["path"].endswith("/output"))
        self.assertEqual(item["classification"], "never-delete")
        self.assertFalse(any("/output/" in value["path"] for value in plan["items"]))

    def test_case_variant_outputs_tree_is_completely_protected(self) -> None:
        project = self.root / "task" / "Outputs" / "site"
        write(project / "package.json", "{}")
        write(project / "package-lock.json", "{}")
        write(project / "node_modules" / "pkg" / "index.js")
        _, plan = self.scan()
        item = next(value for value in plan["items"] if value["path"].endswith("/Outputs"))
        self.assertEqual(item["classification"], "never-delete")
        self.assertFalse(any("/Outputs/" in value["path"] for value in plan["items"]))

    def test_symlink_escape_is_ignored(self) -> None:
        outside = self.temp / "outside"
        write(outside / "keep.txt")
        project = self.root / "app"
        write(project / "package.json", "{}")
        write(project / "package-lock.json", "{}")
        os.symlink(outside, project / "node_modules")
        _, plan = self.scan()
        self.assertFalse(any(value["path"].endswith("node_modules") for value in plan["items"]))
        self.assertTrue((outside / "keep.txt").exists())

    def test_tracked_build_is_never_delete(self) -> None:
        project = self.root / "tracked"
        write(project / "package.json", '{"scripts":{"build":"vite build"}}')
        write(project / "build" / "keep.txt")
        subprocess.run(["git", "init", "-q", str(project)], check=True)
        subprocess.run(["git", "-C", str(project), "add", "package.json", "build/keep.txt"], check=True)
        _, plan = self.scan()
        item = next(value for value in plan["items"] if value["path"].endswith("/build"))
        self.assertEqual(item["classification"], "never-delete")

    def test_nested_repo_boundary_is_never_delete(self) -> None:
        project = self.root / "nested"
        write(project / "package.json", '{"scripts":{"build":"vite build"}}')
        write(project / "build" / "keep.txt")
        subprocess.run(["git", "init", "-q", str(project / "build")], check=True)
        _, plan = self.scan()
        item = next(value for value in plan["items"] if value["path"].endswith("/build"))
        self.assertEqual(item["classification"], "never-delete")

    def test_mercurial_checkout_candidates_are_never_delete(self) -> None:
        project = self.root / "hg-project"
        (project / ".hg").mkdir(parents=True)
        write(project / "Cargo.toml", "[package]\nname='demo'\nversion='0.1.0'\n")
        write(project / "target" / "debug" / "artifact")
        _, plan = self.scan()
        item = next(value for value in plan["items"] if value["path"].endswith("/target"))
        self.assertEqual(item["classification"], "never-delete")

    def test_dangling_mercurial_marker_protects_checkout(self) -> None:
        project = self.root / "dangling-hg"
        project.mkdir(parents=True)
        os.symlink(project / "missing-store", project / ".hg")
        write(project / "Cargo.toml", "[package]\nname='demo'\nversion='0.1.0'\n")
        write(project / "target" / "debug" / "artifact")
        _, plan = self.scan()
        item = next(value for value in plan["items"] if value["path"].endswith("/target"))
        self.assertEqual(item["classification"], "never-delete")

    def test_dangling_git_marker_protects_checkout(self) -> None:
        project = self.root / "dangling-git"
        project.mkdir(parents=True)
        os.symlink(project / "missing-gitdir", project / ".git")
        write(project / "Cargo.toml", "[package]\nname='demo'\nversion='0.1.0'\n")
        write(project / "target" / "debug" / "artifact")
        _, plan = self.scan()
        item = next(value for value in plan["items"] if value["path"].endswith("/target"))
        self.assertEqual(item["classification"], "never-delete")

    def test_nested_mercurial_boundary_is_never_delete(self) -> None:
        project = self.root / "nested-hg"
        write(project / "Cargo.toml", "[package]\nname='demo'\nversion='0.1.0'\n")
        write(project / "target" / ".hg" / "requires")
        _, plan = self.scan()
        item = next(value for value in plan["items"] if value["path"].endswith("/target"))
        self.assertEqual(item["classification"], "never-delete")

    def test_case_variant_nested_mercurial_boundary_is_never_delete(self) -> None:
        project = self.root / "nested-upper-hg"
        write(project / "Cargo.toml", "[package]\nname='demo'\nversion='0.1.0'\n")
        write(project / "target" / ".HG" / "requires")
        _, plan = self.scan()
        item = next(value for value in plan["items"] if value["path"].endswith("/target"))
        self.assertEqual(item["classification"], "never-delete")

    def test_tampered_plan_is_rejected_before_mutation(self) -> None:
        project = self.root / "tamper"
        write(project / "package.json", "{}")
        write(project / "package-lock.json", "{}")
        write(project / "node_modules" / "keep.txt")
        _, plan = self.scan()
        original_seal = plan["seal"]
        plan["items"][0]["reason"] = "tampered"
        self.plan.write_text(json.dumps(plan), encoding="utf-8")
        result = run_helper(
            "apply", "--plan", str(self.plan), "--confirm", original_seal, "--safe", "--permanent"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((project / "node_modules" / "keep.txt").exists())

    def test_recomputed_plan_cannot_escape_approved_root(self) -> None:
        project = self.root / "escape"
        write(project / "package.json", "{}")
        write(project / "package-lock.json", "{}")
        write(project / "node_modules" / "candidate.txt")
        outside = self.temp / "outside" / "keep.txt"
        write(outside, "important")
        _, plan = self.scan()
        item = plan["items"][0]
        item["path"] = str(outside)
        item["id"] = HELPER_MODULE.hashlib.sha256(str(outside).encode("utf-8")).hexdigest()[:16]
        plan["seal"] = HELPER_MODULE.plan_seal(plan)
        self.plan.write_text(json.dumps(plan), encoding="utf-8")
        result = run_helper(
            "token", "--plan", str(self.plan), "--ids", item["id"], "--trash"
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(outside.read_text(encoding="utf-8"), "important")

    def test_recomputed_plan_from_another_host_is_rejected(self) -> None:
        project = self.root / "host"
        write(project / "package.json", "{}")
        write(project / "package-lock.json", "{}")
        write(project / "node_modules" / "candidate.txt")
        _, plan = self.scan()
        plan["host"] = "different-host.example"
        plan["seal"] = HELPER_MODULE.plan_seal(plan)
        self.plan.write_text(json.dumps(plan), encoding="utf-8")
        result = run_helper(
            "token", "--plan", str(self.plan), "--ids", plan["items"][0]["id"], "--trash"
        )
        self.assertEqual(result.returncode, 2)

    def test_token_rejects_plan_symlink(self) -> None:
        write(self.root / "app" / "package.json", "{}")
        write(self.root / "app" / "package-lock.json", "{}")
        write(self.root / "app" / "node_modules" / "candidate.txt")
        _, plan = self.scan()
        real_plan = self.temp / "real-plan.json"
        self.plan.replace(real_plan)
        os.symlink(real_plan, self.plan)
        result = run_helper(
            "token", "--plan", str(self.plan), "--ids", plan["items"][0]["id"], "--trash"
        )
        self.assertEqual(result.returncode, 2)

    @unittest.skipIf(os.name == "nt", "POSIX mode bits are not authoritative on Windows")
    def test_token_rejects_world_readable_plan(self) -> None:
        write(self.root / "app" / "package.json", "{}")
        write(self.root / "app" / "package-lock.json", "{}")
        write(self.root / "app" / "node_modules" / "candidate.txt")
        _, plan = self.scan()
        self.plan.chmod(0o644)
        result = run_helper(
            "token", "--plan", str(self.plan), "--ids", plan["items"][0]["id"], "--trash"
        )
        self.assertEqual(result.returncode, 2)

    def test_permanent_token_requires_no_recovery_ack(self) -> None:
        write(self.root / "app" / "package.json", "{}")
        write(self.root / "app" / "package-lock.json", "{}")
        write(self.root / "app" / "node_modules" / "candidate.txt")
        _, plan = self.scan()
        result = run_helper(
            "token",
            "--plan",
            str(self.plan),
            "--ids",
            plan["items"][0]["id"],
            "--permanent",
        )
        self.assertEqual(result.returncode, 2)

    def test_scan_records_incomplete_coverage(self) -> None:
        warnings: list[str] = []
        with mock.patch.object(HELPER_MODULE.os, "scandir", side_effect=PermissionError("denied")):
            candidates = HELPER_MODULE.scan_root(self.root, warnings)
        self.assertEqual(candidates, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("could not read directory", warnings[0])

    def test_symlinked_builtin_cache_cannot_authorize_its_target(self) -> None:
        important = self.temp / "important"
        write(important / "keep.txt", "important")
        cache_alias = self.temp / "fake-home" / ".npm" / "_cacache"
        cache_alias.parent.mkdir(parents=True)
        os.symlink(important, cache_alias)
        specs = [
            (
                cache_alias,
                "safe",
                "npm-cache",
                "npm download cache",
                "npm will redownload packages",
                None,
            )
        ]
        with mock.patch.object(HELPER_MODULE, "user_cache_specs", return_value=specs):
            self.assertIsNone(HELPER_MODULE.known_user_cache_spec(important))
            self.assertIsNone(HELPER_MODULE.known_user_cache_rule(important))
        self.assertEqual((important / "keep.txt").read_text(encoding="utf-8"), "important")

    def test_recomputed_builtin_cache_plan_cannot_drop_process_requirement(self) -> None:
        # tempfile paths on macOS are commonly exposed through /var -> /private/var.
        # A built-in cache spec itself must be canonical and symlink-free.
        cache = HELPER_MODULE.real(self.temp) / "fake-cache"
        write(cache / "entry.bin", "cache")
        specs = [
            (
                cache,
                "safe",
                "npm-cache",
                "synthetic cache",
                "synthetic rebuild",
                "Codex",
            )
        ]
        with mock.patch.object(HELPER_MODULE, "user_cache_specs", return_value=specs):
            candidate = HELPER_MODULE.make_candidate(
                cache,
                cache.parent,
                "safe",
                "npm-cache",
                "synthetic cache",
                "synthetic rebuild",
                requires_process_stopped="Codex",
            )
            created = HELPER_MODULE.utc_now()
            plan = {
                "kind": HELPER_MODULE.PLAN_KIND,
                "schema_version": HELPER_MODULE.SCHEMA_VERSION,
                "created_at": HELPER_MODULE.iso_time(created),
                "expires_at": HELPER_MODULE.iso_time(
                    created + HELPER_MODULE.dt.timedelta(hours=HELPER_MODULE.PLAN_TTL_HOURS)
                ),
                "host": HELPER_MODULE.platform.node(),
                "codex_context": HELPER_MODULE.codex_context(),
                "roots": [str(HELPER_MODULE.real(self.root))],
                "items": [HELPER_MODULE.asdict(candidate)],
                "coverage": {"complete": True, "warnings": []},
                "notes": [],
            }
            plan["seal"] = HELPER_MODULE.plan_seal(plan)
            HELPER_MODULE.validate_plan_schema(plan)
            plan["items"][0]["requires_process_stopped"] = None
            plan["seal"] = HELPER_MODULE.plan_seal(plan)
            with self.assertRaisesRegex(ValueError, "process requirement"):
                HELPER_MODULE.validate_plan_schema(plan)

    def test_inode_drift_is_skipped(self) -> None:
        project = self.root / "drift"
        write(project / "package.json", "{}")
        write(project / "package-lock.json", "{}")
        module_dir = project / "node_modules"
        write(module_dir / "old.txt")
        init_git_ignored(project, "node_modules/")
        _, plan = self.scan()
        token = self.token(safe=True)
        shutil.rmtree(module_dir)
        write(module_dir / "new.txt")
        result = run_helper(
            "apply", "--plan", str(self.plan), "--confirm", token, "--safe", "--permanent",
            "--ack-no-recovery", cwd=self.temp
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertTrue((module_dir / "new.txt").exists())
        logs = list(self.temp.glob("plan.result-*.json"))
        self.assertEqual(json.loads(logs[0].read_text())["items"][0]["status"], "skipped")

    def test_deep_file_drift_is_skipped(self) -> None:
        project = self.root / "deep-drift"
        write(project / "package.json", "{}")
        write(project / "package-lock.json", "{}")
        deep_file = project / "node_modules" / "pkg" / "deep" / "file.js"
        write(deep_file, "before")
        init_git_ignored(project, "node_modules/")
        self.scan()
        token = self.token(safe=True)
        write(deep_file, "after-and-different")
        result = run_helper(
            "apply", "--plan", str(self.plan), "--confirm", token, "--safe", "--permanent",
            "--ack-no-recovery", cwd=self.temp
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(deep_file.read_text(encoding="utf-8"), "after-and-different")
        logs = list(self.temp.glob("plan.result-*.json"))
        self.assertEqual(json.loads(logs[0].read_text())["items"][0]["status"], "skipped")

    def test_lockfile_drift_is_skipped(self) -> None:
        project = self.root / "marker-drift"
        write(project / "package.json", "{}")
        lockfile = project / "package-lock.json"
        write(lockfile, "{}")
        write(project / "node_modules" / "pkg.js")
        init_git_ignored(project, "node_modules/")
        self.scan()
        token = self.token(safe=True)
        lockfile.unlink()
        result = run_helper(
            "apply", "--plan", str(self.plan), "--confirm", token, "--safe", "--permanent",
            "--ack-no-recovery", cwd=self.temp
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertTrue((project / "node_modules" / "pkg.js").exists())

    def test_git_ignore_drift_is_skipped(self) -> None:
        project = self.root / "ignore-drift"
        write(project / "package.json", "{}")
        write(project / "package-lock.json", "{}")
        write(project / ".gitignore", "node_modules/\n")
        write(project / "node_modules" / "pkg.js")
        subprocess.run(["git", "init", "-q", str(project)], check=True)
        self.scan()
        token = self.token(safe=True)
        write(project / ".gitignore", "")
        result = run_helper(
            "apply", "--plan", str(self.plan), "--confirm", token, "--safe", "--permanent",
            "--ack-no-recovery", cwd=self.temp
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertTrue((project / "node_modules" / "pkg.js").exists())

    def test_new_mercurial_boundary_after_scan_is_skipped(self) -> None:
        project = self.root / "late-hg"
        write(project / "Cargo.toml", "[package]\nname='demo'\nversion='0.1.0'\n")
        write(project / "target" / "debug" / "artifact")
        init_git_ignored(project, "target/")
        self.scan()
        token = self.token(safe=True)
        (project / ".hg").mkdir()
        result = run_helper(
            "apply", "--plan", str(self.plan), "--confirm", token, "--safe", "--permanent",
            "--ack-no-recovery", cwd=self.temp
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertTrue((project / "target" / "debug" / "artifact").exists())

    def test_safe_apply_does_not_remove_review_dist(self) -> None:
        project = self.root / "select"
        write(project / "package.json", '{"scripts":{"build":"vite build"}}')
        write(project / "package-lock.json", "{}")
        write(project / "node_modules" / "pkg.js")
        write(project / "dist" / "artifact.js")
        init_git_ignored(project, "node_modules/")
        _, plan = self.scan()
        token = self.token(safe=True)
        result = run_helper(
            "apply",
            "--plan",
            str(self.plan),
            "--confirm",
            token,
            "--safe",
            "--permanent",
            "--ack-no-recovery",
            cwd=self.temp,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((project / "node_modules").exists())
        self.assertTrue((project / "dist" / "artifact.js").exists())
        journals = list(self.temp.glob("plan.journal-*.jsonl"))
        self.assertEqual(len(journals), 1)
        events = [json.loads(line) for line in journals[0].read_text(encoding="utf-8").splitlines()]
        self.assertEqual(events[0]["type"], "header")
        self.assertEqual(events[1]["type"], "attempt")
        self.assertEqual(events[-1]["type"], "summary")

    @unittest.skipUnless(
        sys.platform == "darwin" or sys.platform.startswith("linux"),
        "OS Trash adapter is implemented only for macOS and Linux",
    )
    def test_full_trash_flow_stays_inside_synthetic_home(self) -> None:
        fake_home = self.temp / "fake-home"
        fake_home.mkdir()
        project = self.root / "trash-flow"
        write(project / "package.json", "{}")
        write(project / "package-lock.json", "{}")
        module_dir = project / "node_modules"
        write(module_dir / "pkg.js", "fixture")
        init_git_ignored(project, "node_modules/")
        environment = os.environ.copy()
        environment["HOME"] = str(fake_home)
        environment["CODEX_HOME"] = str(fake_home / ".codex")
        environment["XDG_DATA_HOME"] = str(fake_home / ".local" / "share")
        scanned = run_helper(
            "scan", "--root", str(self.root), "--plan", str(self.plan), env=environment
        )
        self.assertEqual(scanned.returncode, 0, scanned.stderr)
        token_result = run_helper(
            "token", "--plan", str(self.plan), "--safe", "--trash", env=environment
        )
        self.assertEqual(token_result.returncode, 0, token_result.stderr)
        token = next(
            line.split(": ", 1)[1]
            for line in token_result.stdout.splitlines()
            if line.startswith("Approval token:")
        )
        applied = run_helper(
            "apply",
            "--plan",
            str(self.plan),
            "--safe",
            "--trash",
            "--confirm",
            token,
            cwd=self.temp,
            env=environment,
        )
        self.assertEqual(applied.returncode, 0, applied.stderr + "\n" + applied.stdout)
        self.assertFalse(module_dir.exists())
        if sys.platform == "darwin":
            trash_files = fake_home / ".Trash"
        else:
            trash_files = fake_home / ".local" / "share" / "Trash" / "files"
        moved = list(trash_files.glob("CodexCleanup-*-node_modules"))
        self.assertEqual(len(moved), 1)
        self.assertEqual((moved[0] / "pkg.js").read_text(encoding="utf-8"), "fixture")
        os.replace(moved[0], module_dir)
        self.assertEqual((module_dir / "pkg.js").read_text(encoding="utf-8"), "fixture")

    def test_next_deploy_output_is_preserved_but_cache_is_safe(self) -> None:
        project = self.root / "next"
        write(project / "package.json", '{"scripts":{"build":"next build"}}')
        write(project / ".next" / "cache" / "cache.bin")
        write(project / ".next" / "standalone" / "server.js")
        init_git_ignored(project, ".next/cache/")
        _, plan = self.scan()
        item = next(value for value in plan["items"] if value["path"].endswith("/.next/cache"))
        self.assertEqual(item["classification"], "safe")
        self.assertFalse(any(value["path"].endswith("/.next/standalone") for value in plan["items"]))

    def test_nonignored_node_modules_in_git_is_review(self) -> None:
        project = self.root / "git-node"
        write(project / "package.json", "{}")
        write(project / "package-lock.json", "{}")
        write(project / "node_modules" / "pkg.js")
        subprocess.run(["git", "init", "-q", str(project)], check=True)
        _, plan = self.scan()
        item = next(value for value in plan["items"] if value["path"].endswith("node_modules"))
        self.assertEqual(item["classification"], "review")

    def test_git_ignored_node_modules_can_be_deleted(self) -> None:
        project = self.root / "git-ignored-node"
        write(project / "package.json", "{}")
        write(project / "package-lock.json", "{}")
        write(project / ".gitignore", "node_modules/\n")
        write(project / "node_modules" / "pkg.js")
        subprocess.run(["git", "init", "-q", str(project)], check=True)
        self.scan()
        token = self.token(safe=True)
        result = run_helper(
            "apply",
            "--plan",
            str(self.plan),
            "--confirm",
            token,
            "--safe",
            "--permanent",
            "--ack-no-recovery",
            cwd=self.temp,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((project / "node_modules").exists())

    def test_approval_token_binds_mutation_mode(self) -> None:
        project = self.root / "mode"
        write(project / "package.json", "{}")
        write(project / "package-lock.json", "{}")
        write(project / "node_modules" / "keep.txt")
        init_git_ignored(project, "node_modules/")
        self.scan()
        permanent_token = self.token(safe=True, mode="--permanent")
        result = run_helper(
            "apply",
            "--plan",
            str(self.plan),
            "--confirm",
            permanent_token,
            "--safe",
            "--trash",
            cwd=self.temp,
        )
        self.assertEqual(result.returncode, 2)
        self.assertTrue((project / "node_modules" / "keep.txt").exists())

    def test_home_root_is_rejected(self) -> None:
        result = run_helper("scan", "--root", str(Path.home()), "--plan", str(self.plan))
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.plan.exists())

    def test_case_alias_of_home_is_rejected(self) -> None:
        home = Path.home()
        alias = Path(str(home).swapcase())
        try:
            same = alias.exists() and os.path.samefile(alias, home)
        except OSError:
            same = False
        if not same:
            self.skipTest("Filesystem does not expose a case-insensitive home alias")
        result = run_helper("scan", "--root", str(alias), "--plan", str(self.plan))
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.plan.exists())

    def test_codex_sessions_root_is_rejected(self) -> None:
        sessions = Path.home() / ".codex" / "sessions"
        if not sessions.exists():
            self.skipTest("No local Codex sessions directory")
        result = run_helper("scan", "--root", str(sessions), "--plan", str(self.plan))
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.plan.exists())

    def test_generated_media_cannot_enter_generic_cleanup_plan(self) -> None:
        custom_home = self.temp / "custom-codex-home"
        media_root = custom_home / "generated_images"
        write(media_root / "thread-one" / "image.png", "not-important-for-this-boundary-test")
        environment = os.environ.copy()
        environment["CODEX_HOME"] = str(custom_home)
        direct = run_helper(
            "scan", "--root", str(media_root), "--plan", str(self.plan), env=environment
        )
        self.assertEqual(direct.returncode, 2)
        self.assertFalse(self.plan.exists())
        legacy = run_helper(
            "scan",
            "--root",
            str(self.root),
            "--plan",
            str(self.plan),
            "--include-user-caches",
            "--include-codex-media",
            env=environment,
        )
        self.assertEqual(legacy.returncode, 2)
        self.assertFalse(self.plan.exists())

    def test_default_codex_home_stays_protected_when_overridden(self) -> None:
        default_home = Path.home() / ".codex"
        if not default_home.exists():
            self.skipTest("No default Codex home")
        environment = os.environ.copy()
        environment["CODEX_HOME"] = str(self.temp / "custom-codex-home")
        result = run_helper(
            "scan", "--root", str(default_home), "--plan", str(self.plan), env=environment
        )
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.plan.exists())

    def test_codex_home_context_change_invalidates_plan(self) -> None:
        project = self.root / "context"
        write(project / "package.json", "{}")
        write(project / "package-lock.json", "{}")
        write(project / "node_modules" / "pkg.js")
        first_environment = os.environ.copy()
        first_environment["CODEX_HOME"] = str(self.temp / "codex-one")
        second_environment = os.environ.copy()
        second_environment["CODEX_HOME"] = str(self.temp / "codex-two")
        scanned = run_helper(
            "scan",
            "--root",
            str(self.root),
            "--plan",
            str(self.plan),
            env=first_environment,
        )
        self.assertEqual(scanned.returncode, 0, scanned.stderr)
        token = run_helper(
            "token", "--plan", str(self.plan), "--safe", "--permanent", env=second_environment
        )
        self.assertEqual(token.returncode, 2)

    def test_direct_codex_ui_cache_scan_is_rejected(self) -> None:
        cache_root = Path.home() / "Library" / "Caches" / "Codex"
        if not cache_root.exists():
            self.skipTest("No local Codex UI cache")
        result = run_helper("scan", "--root", str(cache_root), "--plan", str(self.plan))
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.plan.exists())

    def test_process_list_error_is_unknown_not_stopped(self) -> None:
        failed = subprocess.CompletedProcess(["pgrep"], 3, b"", b"Cannot get process list")
        with mock.patch.object(HELPER_MODULE.subprocess, "run", return_value=failed):
            self.assertIsNone(HELPER_MODULE.process_running("Codex"))

    def test_managed_worktree_probe_error_is_unknown(self) -> None:
        failed = subprocess.CompletedProcess(["git"], 127, b"", b"git unavailable")
        with mock.patch.object(HELPER_MODULE, "run_git", return_value=failed):
            self.assertIsNone(HELPER_MODULE.is_managed_codex_worktree(self.root))

    def test_managed_worktree_directory_marker_is_protected(self) -> None:
        (self.root / "codex-thread.json").mkdir()
        result = subprocess.CompletedProcess(["git"], 0, b"codex-thread.json\n", b"")
        with mock.patch.object(HELPER_MODULE, "run_git", return_value=result):
            self.assertTrue(HELPER_MODULE.is_managed_codex_worktree(self.root))

    def test_managed_worktree_empty_marker_path_is_unknown(self) -> None:
        result = subprocess.CompletedProcess(["git"], 0, b"\n", b"")
        with mock.patch.object(HELPER_MODULE, "run_git", return_value=result):
            self.assertIsNone(HELPER_MODULE.is_managed_codex_worktree(self.root))

    def test_managed_worktree_marker_stat_error_is_unknown(self) -> None:
        result = subprocess.CompletedProcess(["git"], 0, b"codex-thread.json\n", b"")
        with mock.patch.object(HELPER_MODULE, "run_git", return_value=result):
            with mock.patch.object(HELPER_MODULE.Path, "lstat", side_effect=PermissionError("denied")):
                self.assertIsNone(HELPER_MODULE.is_managed_codex_worktree(self.root))

    def test_scan_refuses_to_replace_plan_symlink_target(self) -> None:
        target = self.temp / "keep.txt"
        write(target, "important")
        os.symlink(target, self.plan)
        result = run_helper("scan", "--root", str(self.root), "--plan", str(self.plan))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(target.read_text(encoding="utf-8"), "important")
        self.assertTrue(self.plan.is_symlink())

    def test_scan_root_at_or_below_outputs_is_rejected(self) -> None:
        protected = self.root / "outputs" / "site"
        write(protected / "package.json", "{}")
        write(protected / "package-lock.json", "{}")
        write(protected / "node_modules" / "keep.txt")
        result = run_helper("scan", "--root", str(protected), "--plan", str(self.plan))
        self.assertEqual(result.returncode, 2)
        self.assertTrue((protected / "node_modules" / "keep.txt").exists())

    def test_recomputed_plan_cannot_enter_never_scan_tree(self) -> None:
        project = self.root / "normal"
        write(project / "package.json", "{}")
        write(project / "package-lock.json", "{}")
        write(project / "node_modules" / "candidate.txt")
        _, plan = self.scan()
        protected = HELPER_MODULE.real(self.root / ".codex" / "nested" / "node_modules")
        write(protected / "keep.txt", "important")
        item = plan["items"][0]
        item["path"] = str(protected)
        item["id"] = HELPER_MODULE.hashlib.sha256(str(protected).encode()).hexdigest()[:16]
        plan["seal"] = HELPER_MODULE.plan_seal(plan)
        self.plan.write_text(json.dumps(plan), encoding="utf-8")
        result = run_helper("token", "--plan", str(self.plan), "--ids", item["id"], "--trash")
        self.assertEqual(result.returncode, 2)
        self.assertEqual((protected / "keep.txt").read_text(), "important")

    def test_recomputed_plan_cannot_select_nested_codex_ui_cache(self) -> None:
        project = self.root / "normal-ui-forge"
        write(project / "package.json", "{}")
        write(project / "package-lock.json", "{}")
        write(project / "node_modules" / "candidate.txt")
        _, plan = self.scan()
        ui_root = HELPER_MODULE.real(self.root / "synthetic-ui-cache")
        nested = ui_root / "project" / "node_modules"
        write(nested / "keep.txt", "important")
        item = plan["items"][0]
        item["path"] = str(nested)
        item["id"] = HELPER_MODULE.hashlib.sha256(str(nested).encode()).hexdigest()[:16]
        plan["seal"] = HELPER_MODULE.plan_seal(plan)
        with mock.patch.object(HELPER_MODULE, "codex_ui_cache_roots", return_value=(ui_root,)):
            with self.assertRaisesRegex(ValueError, "exact built-in cache root"):
                HELPER_MODULE.validate_plan_schema(plan)
        self.assertEqual((nested / "keep.txt").read_text(), "important")

    def test_recomputed_plan_cannot_select_descendant_of_process_guarded_cache(self) -> None:
        project = self.root / "normal-cache-forge"
        write(project / "package.json", "{}")
        write(project / "package-lock.json", "{}")
        write(project / "node_modules" / "candidate.txt")
        _, plan = self.scan()
        cache_root = HELPER_MODULE.real(self.root / "synthetic-gradle-cache")
        nested = cache_root / "nested" / "node_modules"
        write(nested / "keep.txt", "important")
        item = plan["items"][0]
        item["path"] = str(nested)
        item["id"] = HELPER_MODULE.hashlib.sha256(str(nested).encode()).hexdigest()[:16]
        plan["seal"] = HELPER_MODULE.plan_seal(plan)
        specs = [
            (
                cache_root,
                "safe",
                "gradle-global-cache",
                "synthetic Gradle cache",
                "rebuild",
                "GradleDaemon",
            )
        ]
        with mock.patch.object(HELPER_MODULE, "user_cache_specs", return_value=specs):
            with self.assertRaisesRegex(ValueError, "exact built-in cache root"):
                HELPER_MODULE.validate_plan_schema(plan)
        self.assertEqual((nested / "keep.txt").read_text(), "important")

    def test_root_containing_home_is_rejected(self) -> None:
        result = run_helper("scan", "--root", str(Path.home().parent), "--plan", str(self.plan))
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.plan.exists())

    def test_future_dated_plan_is_rejected(self) -> None:
        write(self.root / "app" / "package.json", "{}")
        write(self.root / "app" / "package-lock.json", "{}")
        write(self.root / "app" / "node_modules" / "candidate.txt")
        _, plan = self.scan()
        future = HELPER_MODULE.utc_now() + HELPER_MODULE.dt.timedelta(days=365)
        plan["created_at"] = HELPER_MODULE.iso_time(future)
        plan["expires_at"] = HELPER_MODULE.iso_time(
            future + HELPER_MODULE.dt.timedelta(hours=HELPER_MODULE.PLAN_TTL_HOURS)
        )
        plan["seal"] = HELPER_MODULE.plan_seal(plan)
        self.plan.write_text(json.dumps(plan), encoding="utf-8")
        result = run_helper("token", "--plan", str(self.plan), "--safe", "--trash")
        self.assertEqual(result.returncode, 2)

    def test_symlinked_cache_ancestor_cannot_authorize_real_target(self) -> None:
        important_parent = HELPER_MODULE.real(self.temp / "important-parent")
        cache = important_parent / "_cacache"
        write(cache / "keep.txt", "important")
        alias_parent = self.temp / "alias-parent"
        os.symlink(important_parent, alias_parent)
        alias_cache = alias_parent / "_cacache"
        specs = [(alias_cache, "safe", "npm-cache", "cache", "rebuild", None)]
        with mock.patch.object(HELPER_MODULE, "user_cache_specs", return_value=specs):
            self.assertIsNone(HELPER_MODULE.known_user_cache_spec(cache))

    def test_builtin_cache_rule_requires_real_directory(self) -> None:
        cache = HELPER_MODULE.real(self.temp) / "fake-cache-file"
        write(cache, "important")
        specs = [(cache, "safe", "npm-cache", "cache", "rebuild", None)]
        with mock.patch.object(HELPER_MODULE, "user_cache_specs", return_value=specs):
            self.assertIsNone(HELPER_MODULE.known_user_cache_spec(cache))
            candidates = []
            HELPER_MODULE.add_user_caches(candidates, False)
            self.assertEqual(candidates, [])

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO requires POSIX")
    def test_builtin_cache_rule_rejects_fifo(self) -> None:
        cache = HELPER_MODULE.real(self.temp) / "fake-cache-fifo"
        os.mkfifo(cache)
        specs = [(cache, "safe", "npm-cache", "cache", "rebuild", None)]
        with mock.patch.object(HELPER_MODULE, "user_cache_specs", return_value=specs):
            self.assertIsNone(HELPER_MODULE.known_user_cache_spec(cache))

    def test_dotnet_symlink_or_directory_marker_does_not_authorize_bin(self) -> None:
        outside = self.temp / "outside.csproj"
        write(outside, "<Project/>")
        symlink_project = self.root / "dotnet-link"
        symlink_project.mkdir()
        os.symlink(outside, symlink_project / "fake.csproj")
        write(symlink_project / "bin" / "keep.dll")
        directory_project = self.root / "dotnet-directory"
        (directory_project / "fake.csproj").mkdir(parents=True)
        write(directory_project / "obj" / "keep.dll")
        _, plan = self.scan()
        paths = {item["path"] for item in plan["items"]}
        self.assertNotIn(str(HELPER_MODULE.real(symlink_project / "bin")), paths)
        self.assertNotIn(str(HELPER_MODULE.real(directory_project / "obj")), paths)

    def test_android_emulator_policy_detects_x86_process(self) -> None:
        running = subprocess.CompletedProcess(["pgrep"], 0, b"123\n", b"")
        with mock.patch.object(HELPER_MODULE.subprocess, "run", return_value=running) as called:
            self.assertTrue(HELPER_MODULE.process_running("AndroidEmulator"))
        self.assertIn("qemu-system-", called.call_args.args[0][-1])

    def test_codex_process_policy_detects_lowercase_cli(self) -> None:
        def probe(command, **_kwargs):
            return subprocess.CompletedProcess(
                command, 0 if command[-1] == "codex" else 1, b"", b""
            )

        with mock.patch.object(HELPER_MODULE.subprocess, "run", side_effect=probe):
            self.assertTrue(HELPER_MODULE.process_running("Codex"))

    def test_linux_mount_id_boundary_is_rejected(self) -> None:
        nested = self.root / "mounted" / "child"
        nested.mkdir(parents=True)
        table = [(HELPER_MODULE.real(nested.parent), 22), (HELPER_MODULE.real(self.root), 11), (Path("/"), 1)]
        with mock.patch.object(HELPER_MODULE.sys, "platform", "linux"):
            with mock.patch.object(HELPER_MODULE, "linux_mount_table", return_value=(table, None)):
                failure = HELPER_MODULE.filesystem_boundary_failure(nested, self.root)
        self.assertIn("mount-ID", failure or "")

    def test_scan_skips_same_device_linux_mount_id_boundary(self) -> None:
        mounted = self.root / "mounted"
        write(mounted / "package.json", "{}")
        write(mounted / "package-lock.json", "{}")
        write(mounted / "node_modules" / "private.txt")
        table = [(HELPER_MODULE.real(mounted), 22), (HELPER_MODULE.real(self.root), 11), (Path("/"), 1)]
        warnings: list[str] = []
        with mock.patch.object(HELPER_MODULE.sys, "platform", "linux"):
            with mock.patch.object(HELPER_MODULE, "linux_mount_table", return_value=(table, None)):
                candidates = HELPER_MODULE.scan_root(HELPER_MODULE.real(self.root), warnings)
        self.assertEqual(candidates, [])
        self.assertTrue(any("mount-ID" in warning for warning in warnings))

    def test_tree_snapshot_rejects_nested_linux_mount_id(self) -> None:
        candidate = self.root / "app" / "node_modules"
        nested = candidate / "bound"
        write(nested / "private.txt")
        table = [(HELPER_MODULE.real(nested), 22), (HELPER_MODULE.real(candidate), 11), (Path("/"), 1)]
        with mock.patch.object(HELPER_MODULE.sys, "platform", "linux"):
            with mock.patch.object(HELPER_MODULE, "linux_mount_table", return_value=(table, None)):
                _fingerprint, failure = HELPER_MODULE.tree_metadata_snapshot(candidate)
        self.assertIn("mount-ID", failure or "")

    def test_relocated_mount_hazard_is_checked_before_size_walk(self) -> None:
        target = self.root / "candidate"
        write(target / "keep.txt")
        item = {
            "identity": HELPER_MODULE.identity(target),
            "allocated_bytes": 123,
            "tree_fingerprint": "0" * 64,
        }
        with mock.patch.object(
            HELPER_MODULE,
            "tree_metadata_snapshot",
            return_value=("", "nested Linux mount-ID boundary"),
        ):
            with mock.patch.object(HELPER_MODULE, "allocated_size") as size:
                failure = HELPER_MODULE.relocated_failure(item, target, target)
        self.assertIn("mount-ID", failure or "")
        size.assert_not_called()

    def test_open_file_stat_error_is_unknown(self) -> None:
        with mock.patch.object(HELPER_MODULE.Path, "lstat", side_effect=PermissionError("denied")):
            self.assertIsNone(HELPER_MODULE.open_file_state(self.root))

    def test_permanent_directory_delete_requires_symlink_safe_runtime(self) -> None:
        project = self.root / "delete-gate"
        write(project / "package.json", "{}")
        write(project / "package-lock.json", "{}")
        target = project / "node_modules"
        write(target / "keep.txt")
        init_git_ignored(project, "node_modules/")
        _, plan = self.scan()
        item = next(value for value in plan["items"] if value["path"] == str(HELPER_MODULE.real(target)))
        with mock.patch.object(HELPER_MODULE.shutil.rmtree, "avoids_symlink_attacks", False):
            with self.assertRaisesRegex(HELPER_MODULE.PreconditionFailed, "symlink-safe"):
                HELPER_MODULE.delete_permanently(HELPER_MODULE.real(target), item)
        self.assertTrue((target / "keep.txt").exists())

    def test_partial_recursive_delete_reports_and_restores_remainder(self) -> None:
        project = self.root / "partial-delete"
        write(project / "package.json", "{}")
        write(project / "package-lock.json", "{}")
        target = project / "node_modules"
        write(target / "lost.txt")
        write(target / "remaining.txt")
        init_git_ignored(project, "node_modules/")
        _, plan = self.scan()
        item = next(value for value in plan["items"] if value["path"] == str(HELPER_MODULE.real(target)))

        def partial(path: Path) -> None:
            (Path(path) / "lost.txt").unlink()
            raise OSError("synthetic interruption")

        with mock.patch.object(HELPER_MODULE.shutil, "rmtree", side_effect=partial) as removal:
            removal.avoids_symlink_attacks = True
            with self.assertRaisesRegex(HELPER_MODULE.PartialDeletionError, "partial deletion"):
                HELPER_MODULE.delete_permanently(HELPER_MODULE.real(target), item)
        self.assertTrue(target.exists())
        self.assertTrue((target / "remaining.txt").exists())
        self.assertFalse((target / "lost.txt").exists())

    def test_apply_is_audit_only_on_unsupported_platform(self) -> None:
        with mock.patch.object(HELPER_MODULE.sys, "platform", "win32"):
            self.assertEqual(HELPER_MODULE.command_apply(argparse.Namespace()), 2)

    @unittest.skipIf(os.name == "nt", "POSIX permissions required")
    def test_trash_directory_is_forced_private(self) -> None:
        directory = self.temp / "trash-private"
        directory.mkdir(mode=0o755)
        directory.chmod(0o755)
        HELPER_MODULE._ensure_directory(directory, private=True)
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)

    def test_approval_token_binds_linux_xdg_trash_root(self) -> None:
        selected = [{"id": "a" * 16}]
        plan = {"seal": "b" * 64}
        fake_home = HELPER_MODULE.real(self.temp / "fake-home")
        fake_home.mkdir()
        with mock.patch.object(HELPER_MODULE.sys, "platform", "linux"):
            with mock.patch.object(HELPER_MODULE.Path, "home", return_value=fake_home):
                with mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(fake_home / "one")}, clear=False):
                    first = HELPER_MODULE.mutation_context("trash")
                with mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(fake_home / "two")}, clear=False):
                    second = HELPER_MODULE.mutation_context("trash")
        self.assertNotEqual(
            HELPER_MODULE.selection_token(plan, selected, "trash", first),
            HELPER_MODULE.selection_token(plan, selected, "trash", second),
        )

    @unittest.skipIf(os.name == "nt", "symlink fixture requires POSIX")
    def test_trash_policy_rejects_symlinked_backend_root(self) -> None:
        fake_home = HELPER_MODULE.real(self.temp / "trash-home")
        data_home = fake_home / "data"
        important = fake_home / "important"
        data_home.mkdir(parents=True)
        important.mkdir()
        os.symlink(important, data_home / "Trash")
        with mock.patch.object(HELPER_MODULE.sys, "platform", "linux"):
            with mock.patch.object(HELPER_MODULE.Path, "home", return_value=fake_home):
                with mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(data_home)}, clear=False):
                    with self.assertRaisesRegex(ValueError, "symlink"):
                        HELPER_MODULE.trash_policy_context()

    def test_terminal_output_escapes_control_characters_in_paths(self) -> None:
        project = self.root / "line\n\x1b[2Jproject"
        write(project / "package.json", "{}")
        write(project / "package-lock.json", "{}")
        write(project / "node_modules" / "candidate.txt")
        result, _plan = self.scan()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("\x1b", result.stdout)
        self.assertIn("\\n\\u001b[2Jproject", result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)

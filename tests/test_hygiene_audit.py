#!/usr/bin/env python3
"""Behavioral tests for the metadata-only workspace hygiene audit."""

import hashlib
import json
import os
import runpy
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


ROOT = Path(__file__).parent.parent
HELPER = ROOT / "scripts" / "hygiene_audit.py"
CLEANUP_HELPER = ROOT / "scripts" / "cleanup.py"


def run_command(command: List[str], *, timeout: float = 10.0) -> subprocess.CompletedProcess:
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=timeout,
        env=environment,
    )


def run_audit(
    root: Path,
    *,
    output_format: str = "json",
    extra: Optional[Iterable[str]] = None,
    timeout: float = 10.0,
) -> subprocess.CompletedProcess:
    command = [
        sys.executable,
        str(HELPER),
        "--root",
        str(root),
        "--format",
        output_format,
    ]
    if extra:
        command.extend(extra)
    return run_command(command, timeout=timeout)


def write(path: Path, value: bytes = b"x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)


def parse_report(result: subprocess.CompletedProcess) -> Dict[str, Any]:
    if not result.stdout:
        raise AssertionError("audit produced no JSON stdout; stderr=" + result.stderr)
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise AssertionError(
            "audit stdout is not one JSON document: %s\nstdout=%r\nstderr=%r"
            % (error, result.stdout, result.stderr)
        )
    if not isinstance(report, dict):
        raise AssertionError("audit JSON must be an object")
    return report


def findings_with_code(report: Dict[str, Any], code: str) -> List[Dict[str, Any]]:
    return [
        finding
        for finding in report.get("findings", [])
        if isinstance(finding, dict) and finding.get("code") == code
    ]


def finding_paths(finding: Dict[str, Any]) -> List[str]:
    paths: List[str] = []
    direct = finding.get("path")
    if isinstance(direct, str):
        paths.append(direct)
    many = finding.get("paths")
    if isinstance(many, list):
        paths.extend(value for value in many if isinstance(value, str))
    scope = finding.get("scope")
    if isinstance(scope, str):
        paths.append(scope)
    return paths


def finding_space_status(finding: Dict[str, Any]) -> str:
    for key in ("space_status", "reclaimability", "classification"):
        value = finding.get(key)
        if isinstance(value, str):
            return value
    space = finding.get("space")
    if isinstance(space, dict):
        for key in ("status", "classification"):
            value = space.get(key)
            if isinstance(value, str):
                return value
    return ""


def all_string_values(value: Any) -> List[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        strings: List[str] = []
        for child in value:
            strings.extend(all_string_values(child))
        return strings
    if isinstance(value, dict):
        strings = []
        for key, child in value.items():
            strings.extend(all_string_values(key))
            strings.extend(all_string_values(child))
        return strings
    return []


def snapshot_tree(root: Path) -> List[Tuple[str, str, int, str]]:
    """Capture names, kinds, modes, link text, and regular-file content."""
    snapshot: List[Tuple[str, str, int, str]] = []
    stack = [root]
    while stack:
        directory = stack.pop()
        entries = sorted(os.scandir(directory), key=lambda entry: os.fsencode(entry.name))
        child_directories: List[Path] = []
        for entry in entries:
            path = Path(entry.path)
            relative = os.path.relpath(path, root)
            info = entry.stat(follow_symlinks=False)
            mode = stat.S_IMODE(info.st_mode)
            if stat.S_ISLNK(info.st_mode):
                snapshot.append((relative, "symlink", mode, os.readlink(path)))
            elif stat.S_ISDIR(info.st_mode):
                snapshot.append((relative, "directory", mode, ""))
                child_directories.append(path)
            elif stat.S_ISREG(info.st_mode):
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                snapshot.append((relative, "file", mode, digest))
            else:
                snapshot.append((relative, "other", mode, str(stat.S_IFMT(info.st_mode))))
        stack.extend(reversed(child_directories))
    return snapshot


class HygieneAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        # macOS exposes the system temp directory through the `/var` symlink.
        # Exercise the audit against the canonical path so this fixture does not
        # weaken the production rule that rejects symlinks in root ancestors.
        self.temp = Path(tempfile.mkdtemp(prefix="codex-hygiene-test-")).resolve()
        self.project = self.temp / "project"
        self.project.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.temp, ignore_errors=True)

    def assert_report_contract(self, report: Dict[str, Any]) -> None:
        self.assertEqual(report.get("kind"), "codex-workspace-hygiene-report")
        self.assertEqual(report.get("schema_version"), 1)
        self.assertIs(report.get("policy", {}).get("mutation_supported"), False)
        self.assertIsInstance(report.get("coverage"), dict)
        self.assertIn("complete", report["coverage"])
        self.assertIsInstance(report.get("inventory"), dict)
        self.assertIsInstance(report.get("findings"), list)
        for finding in report["findings"]:
            self.assertEqual(finding.get("origin"), "unknown")
            self.assertIs(finding.get("mutation_allowed"), False)
            self.assertNotEqual(finding_space_status(finding).casefold(), "safe")

    def test_studyingcrm_like_layout_reports_structure_without_claiming_savings(self) -> None:
        store = self.project / ".pnpm-store" / "v11" / "projects"
        for index in range(24):
            (store / ("entry-%02d" % index)).mkdir(parents=True)

        write(self.project / "tmp" / "brandbook" / "rendered" / "page-01.png")
        write(self.project / "tmp" / "site-captures" / "home.png")
        write(self.project / "tmp" / "render-job" / "node_modules" / "pkg" / "index.js")

        for directory in ("processed_reference_style", "processed_reference_style_v2"):
            for index in range(3):
                write(self.project / directory / ("IMG_%04d.png" % (3300 + index)))

        for name in (
            "logo-balance-soft.png",
            "logo-balance-balanced.png",
            "logo-selected-outline.png",
            "logo-variants-contact-sheet.png",
        ):
            write(self.project / name)

        result = run_audit(self.project)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = parse_report(result)
        self.assert_report_contract(report)
        self.assertIs(report["coverage"]["complete"], True)

        codes = {
            finding.get("code")
            for finding in report["findings"]
            if isinstance(finding, dict)
        }
        self.assertIn("project-local-package-store", codes)
        self.assertIn("mostly-empty-directory-tree", codes)
        self.assertIn("persistent-scratch-tree", codes)
        self.assertIn("possible-iteration-family", codes)
        self.assertIn("root-artifact-family", codes)
        self.assertIn("opaque-name-family", codes)
        self.assertIn("dependency-inside-scratch", codes)

        package_store = findings_with_code(report, "project-local-package-store")[0]
        self.assertIn(".pnpm-store", "\n".join(finding_paths(package_store)))
        self.assertNotEqual(finding_space_status(package_store).casefold(), "safe")
        confirmed = report.get("space_summary", {}).get("confirmed_reclaimable_bytes", 0)
        self.assertEqual(confirmed, 0)

    def test_node_modules_inside_output_is_protected_not_safe(self) -> None:
        dependency = self.project / "output" / "site-source" / "app" / "node_modules"
        write(self.project / "output" / "site-source" / "app" / "package.json", b"{}")
        write(self.project / "output" / "site-source" / "app" / "pnpm-lock.yaml", b"lockfileVersion: 9\n")
        write(dependency / "pkg" / "index.js")

        result = run_audit(self.project)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = parse_report(result)
        matches = findings_with_code(report, "dependency-inside-deliverable")
        self.assertTrue(matches, report.get("findings"))
        finding = matches[0]
        self.assertIn("output/site-source/app/node_modules", "\n".join(finding_paths(finding)))
        self.assertIn("protected", finding_space_status(finding).casefold())
        self.assertEqual(finding.get("origin"), "unknown")
        self.assertIs(finding.get("mutation_allowed"), False)

    def test_nested_package_manager_node_modules_are_one_logical_finding(self) -> None:
        outer = self.project / "output" / "site-source" / "app" / "node_modules"
        write(outer / ".pnpm" / "alpha@1.0.0" / "node_modules" / "alpha" / "index.js")
        write(
            outer
            / ".pnpm"
            / "beta@1.0.0"
            / "node_modules"
            / "beta"
            / "node_modules"
            / "gamma"
            / "index.js"
        )

        result = run_audit(self.project)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = parse_report(result)
        matches = findings_with_code(report, "dependency-inside-deliverable")
        self.assertEqual(len(matches), 1, matches)
        self.assertEqual(finding_paths(matches[0]), ["output/site-source/app/node_modules"])

    def test_symlink_escape_is_not_followed_or_disclosed(self) -> None:
        outside = self.temp / "outside-private"
        write(outside / "outside-secret-do-not-report.txt", b"secret")
        for index in range(24):
            (outside / ".pnpm-store" / ("empty-%02d" % index)).mkdir(parents=True)
        os.symlink(outside, self.project / "linked-outside")

        result = run_audit(self.project)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = parse_report(result)
        self.assert_report_contract(report)
        self.assertNotIn("outside-secret-do-not-report", result.stdout)
        self.assertNotIn(str(outside), result.stdout)
        self.assertFalse(findings_with_code(report, "project-local-package-store"))
        skipped = report.get("coverage", {}).get("symlinks_skipped", 0)
        self.assertGreaterEqual(skipped, 1)
        self.assertEqual((outside / "outside-secret-do-not-report.txt").read_bytes(), b"secret")

    def test_audit_does_not_change_or_add_project_entries(self) -> None:
        write(self.project / "tmp" / "rendered" / "preview.png", b"preview")
        write(self.project / "logo-draft-one.png", b"one")
        write(self.project / "logo-draft-two.png", b"two")
        write(self.project / "logo-draft-three.png", b"three")
        write(self.project / "logo-draft-four.png", b"four")
        before = snapshot_tree(self.project)

        result = run_audit(self.project)
        self.assertEqual(result.returncode, 0, result.stderr)
        parse_report(result)
        after = snapshot_tree(self.project)

        self.assertEqual(after, before)

    def test_generic_names_have_unknown_origin_and_never_become_safe(self) -> None:
        for directory in ("tmp", "final", "backup", ".codex-generated"):
            write(self.project / directory / "result.txt")
        write(self.project / "output" / "keep.txt")

        result = run_audit(self.project)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = parse_report(result)
        self.assert_report_contract(report)
        self.assertTrue(report["findings"])
        for finding in report["findings"]:
            self.assertEqual(finding.get("origin"), "unknown")
            self.assertNotEqual(finding_space_status(finding).casefold(), "safe")
        self.assertEqual(
            report.get("space_summary", {}).get("confirmed_reclaimable_bytes", 0),
            0,
        )

    @unittest.skipIf(os.name == "nt", "control-character filename fixture requires POSIX")
    def test_json_and_terminal_escape_untrusted_filename_controls(self) -> None:
        hostile = "logo-evil\n\t\x1b[31m-\u202e-one.png"
        for name in (
            hostile,
            "logo-safe-two.png",
            "logo-safe-three.png",
            "logo-safe-four.png",
        ):
            write(self.project / name)

        json_result = run_audit(self.project, output_format="json")
        self.assertEqual(json_result.returncode, 0, json_result.stderr)
        report = parse_report(json_result)
        self.assertTrue(any(hostile in value for value in all_string_values(report)))
        json_result.stdout.encode("ascii")
        self.assertNotIn("\x1b", json_result.stdout)
        self.assertNotIn("\u202e", json_result.stdout)
        self.assertIn("\\u001b", json_result.stdout)
        self.assertIn("\\u202e", json_result.stdout.casefold())

        terminal_result = run_audit(self.project, output_format="terminal")
        self.assertEqual(terminal_result.returncode, 0, terminal_result.stderr)
        self.assertNotIn("\x1b", terminal_result.stdout)
        self.assertNotIn("\u202e", terminal_result.stdout)
        self.assertNotIn("\r", terminal_result.stdout)
        self.assertIn("\\u001b", terminal_result.stdout)
        self.assertIn("\\u202e", terminal_result.stdout.casefold())

    @unittest.skipIf(os.name == "nt" or not hasattr(os, "mkfifo"), "FIFO fixture requires POSIX")
    def test_fifo_named_like_manifest_does_not_block_or_get_replaced(self) -> None:
        fifo = self.project / "package.json"
        os.mkfifo(fifo, 0o600)
        write(self.project / "tmp" / "preview.png")

        result = run_audit(self.project, timeout=3.0)
        self.assertIn(result.returncode, (0, 1), result.stderr)
        report = parse_report(result)
        self.assert_report_contract(report)
        self.assertTrue(stat.S_ISFIFO(os.lstat(fifo).st_mode))

    def test_entry_and_directory_budgets_produce_parseable_partial_reports(self) -> None:
        for index in range(12):
            write(self.project / "fanout" / ("file-%02d.txt" % index))

        cases = (
            ("--max-entries", "3"),
            ("--max-children", "3"),
            ("--max-depth", "1"),
        )
        for flag, value in cases:
            with self.subTest(flag=flag):
                result = run_audit(self.project, extra=(flag, value))
                self.assertEqual(result.returncode, 1, result.stderr)
                report = parse_report(result)
                self.assert_report_contract(report)
                self.assertIs(report["coverage"]["complete"], False)
                self.assertTrue(report["coverage"].get("warnings"))

    @unittest.skipIf(os.name == "nt", "file-descriptor limit fixture requires POSIX")
    def test_wide_directory_keeps_open_descriptors_bounded_by_depth(self) -> None:
        for index in range(200):
            write(self.project / ("sibling-%03d" % index) / "sentinel.txt")

        limiter = (
            "import os,resource,runpy,sys;"
            "soft,hard=resource.getrlimit(resource.RLIMIT_NOFILE);"
            "limit=min(64,hard);"
            "resource.setrlimit(resource.RLIMIT_NOFILE,(limit,limit));"
            "script=sys.argv[1];sys.path.insert(0,os.path.dirname(script));sys.argv=sys.argv[1:];"
            "runpy.run_path(script,run_name='__main__')"
        )
        result = run_command(
            [
                sys.executable,
                "-c",
                limiter,
                str(HELPER),
                "--root",
                str(self.project),
                "--format",
                "json",
            ]
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        report = parse_report(result)
        self.assertIs(report["coverage"]["complete"], True)
        self.assertEqual(report["inventory"]["directories"], 201)

    def test_json_is_byte_for_byte_deterministic(self) -> None:
        write(self.project / "tmp" / "rendered" / "preview.png")
        for index in range(4):
            write(self.project / ("logo-variant-%02d.png" % index))

        first = run_audit(self.project)
        second = run_audit(self.project)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        parse_report(first)
        parse_report(second)
        self.assertEqual(second.stdout, first.stdout)
        self.assertEqual(second.stderr, first.stderr)

    def test_invalid_and_symlink_roots_return_policy_error(self) -> None:
        missing = self.temp / "missing"
        missing_result = run_audit(missing)
        self.assertEqual(missing_result.returncode, 2)

        alias = self.temp / "project-alias"
        os.symlink(self.project, alias)
        alias_result = run_audit(alias)
        self.assertEqual(alias_result.returncode, 2)

        real_parent = self.temp / "real-parent"
        nested_project = real_parent / "nested-project"
        nested_project.mkdir(parents=True)
        parent_alias = self.temp / "parent-alias"
        os.symlink(real_parent, parent_alias)
        ancestor_alias_result = run_audit(parent_alias / "nested-project")
        self.assertEqual(ancestor_alias_result.returncode, 2)

    def test_ancestor_swap_during_validation_cannot_redirect_root(self) -> None:
        original_parent = self.temp / "race-parent"
        original_project = original_parent / "project"
        write(original_project / "original.txt", b"original")
        outside_parent = self.temp / "outside-parent"
        outside_project = outside_parent / "project"
        write(outside_project / "outside.txt", b"outside")
        parked_parent = self.temp / "parked-parent"

        scripts_path = str(ROOT / "scripts")
        sys.path.insert(0, scripts_path)
        try:
            namespace = runpy.run_path(str(HELPER), run_name="hygiene_race_fixture")
        finally:
            sys.path.remove(scripts_path)
        helper_core = namespace["core"]
        original_validate = helper_core.validate_scan_root

        def swap_then_validate(path: Path) -> Path:
            original_parent.rename(parked_parent)
            os.symlink(outside_parent, original_parent)
            return original_validate(path)

        try:
            with mock.patch.object(helper_core, "validate_scan_root", swap_then_validate):
                with self.assertRaisesRegex(ValueError, "root changed"):
                    namespace["validate_root"](original_project)
        finally:
            if original_parent.is_symlink():
                original_parent.unlink()
            if parked_parent.exists():
                parked_parent.rename(original_parent)

        self.assertEqual((original_project / "original.txt").read_bytes(), b"original")
        self.assertEqual((outside_project / "outside.txt").read_bytes(), b"outside")

    def test_cleanup_helper_rejects_hygiene_report_as_plan(self) -> None:
        write(self.project / "tmp" / "preview.png")
        audit = run_audit(self.project)
        self.assertEqual(audit.returncode, 0, audit.stderr)
        report = parse_report(audit)
        self.assertEqual(report.get("kind"), "codex-workspace-hygiene-report")

        fake_plan = self.temp / "hygiene-report.json"
        fake_plan.write_text(audit.stdout, encoding="utf-8")
        if os.name != "nt":
            fake_plan.chmod(0o600)
        result = run_command(
            [
                sys.executable,
                str(CLEANUP_HELPER),
                "token",
                "--plan",
                str(fake_plan),
                "--safe",
                "--trash",
            ]
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("unsupported plan schema", result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)

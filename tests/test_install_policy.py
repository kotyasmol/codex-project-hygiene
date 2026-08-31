#!/usr/bin/env python3
"""Behavioral tests for persistent project-hygiene policy installation."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Dict, List, Optional


ROOT = Path(__file__).parent.parent
HELPER = ROOT / "scripts" / "install_policy.py"
START_MARKER = "<!-- codex-project-hygiene:start -->"
END_MARKER = "<!-- codex-project-hygiene:end -->"


class InstallPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="codex-policy-test-")).resolve()
        self.home = self.temp / "home"
        self.project = self.temp / "project"
        self.home.mkdir()
        self.project.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.temp, ignore_errors=True)

    def run_helper(self, arguments: List[str], *, home: Optional[Path] = None) -> subprocess.CompletedProcess:
        environment = os.environ.copy()
        environment["HOME"] = str(self.home if home is None else home)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        return subprocess.run(
            [sys.executable, str(HELPER)] + arguments,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=10.0,
            env=environment,
        )

    def preview(self) -> Dict[str, object]:
        result = self.run_helper(
            ["preview", "--root", str(self.project), "--format", "json"]
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def apply_token(self, token: str) -> subprocess.CompletedProcess:
        return self.run_helper(
            ["apply", "--root", str(self.project), "--confirm", token]
        )

    def test_preview_is_non_mutating_and_binds_exact_target(self) -> None:
        report = self.preview()
        self.assertEqual(report["kind"], "codex-project-hygiene-policy-preview")
        self.assertEqual(report["action"], "create")
        self.assertEqual(report["target"], str(self.project / "AGENTS.md"))
        self.assertIs(report["mutation_performed"], False)
        self.assertFalse((self.project / "AGENTS.md").exists())
        self.assertEqual(list(self.project.iterdir()), [])

    def test_apply_creates_one_managed_policy_block(self) -> None:
        token = str(self.preview()["approval_token"])
        result = self.apply_token(token)
        self.assertEqual(result.returncode, 0, result.stderr)
        text = (self.project / "AGENTS.md").read_text(encoding="utf-8")
        self.assertEqual(text.count(START_MARKER), 1)
        self.assertEqual(text.count(END_MARKER), 1)
        self.assertIn("smallest useful media batch", text)
        self.assertIn("outside the repository", text)

    def test_existing_instructions_are_preserved_byte_for_byte(self) -> None:
        original = "# Existing project rules\n\n- Keep the required dist directory.\n"
        target = self.project / "AGENTS.md"
        target.write_text(original, encoding="utf-8")
        token = str(self.preview()["approval_token"])
        result = self.apply_token(token)
        self.assertEqual(result.returncode, 0, result.stderr)
        updated = target.read_text(encoding="utf-8")
        self.assertTrue(updated.startswith(original))
        self.assertEqual(updated[: len(original)], original)
        self.assertEqual(updated.count(START_MARKER), 1)

    def test_stale_token_cannot_overwrite_a_concurrent_change(self) -> None:
        target = self.project / "AGENTS.md"
        target.write_text("# Initial\n", encoding="utf-8")
        token = str(self.preview()["approval_token"])
        concurrent = "# Initial\n\n- Concurrent user rule.\n"
        target.write_text(concurrent, encoding="utf-8")
        result = self.apply_token(token)
        self.assertEqual(result.returncode, 2)
        self.assertIn("token does not match", result.stderr)
        self.assertEqual(target.read_text(encoding="utf-8"), concurrent)
        self.assertFalse(any(path.name.startswith(".AGENTS.md.") for path in self.project.iterdir()))

    @unittest.skipIf(os.name == "nt", "symlink fixture requires elevated privileges on some Windows hosts")
    def test_symlink_destination_is_rejected_without_touching_target(self) -> None:
        outside = self.temp / "outside-agents.md"
        outside.write_text("outside\n", encoding="utf-8")
        os.symlink(outside, self.project / "AGENTS.md")
        result = self.run_helper(
            ["preview", "--root", str(self.project), "--format", "json"]
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("must not be a symlink", result.stderr)
        self.assertEqual(outside.read_text(encoding="utf-8"), "outside\n")

    def test_malformed_or_duplicate_markers_fail_closed(self) -> None:
        target = self.project / "AGENTS.md"
        target.write_text(START_MARKER + "\npartial\n", encoding="utf-8")
        result = self.run_helper(
            ["preview", "--root", str(self.project), "--format", "json"]
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("missing or duplicate", result.stderr)
        self.assertEqual(target.read_text(encoding="utf-8"), START_MARKER + "\npartial\n")

    def test_second_apply_is_idempotent(self) -> None:
        token = str(self.preview()["approval_token"])
        first = self.apply_token(token)
        self.assertEqual(first.returncode, 0, first.stderr)
        before = (self.project / "AGENTS.md").read_bytes()
        second = self.apply_token("not-the-token")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("already active", second.stdout)
        self.assertEqual((self.project / "AGENTS.md").read_bytes(), before)

    def test_home_directory_scope_is_rejected(self) -> None:
        result = self.run_helper(
            ["preview", "--root", str(self.home), "--format", "json"]
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("home", result.stderr)
        self.assertFalse((self.home / "AGENTS.md").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)

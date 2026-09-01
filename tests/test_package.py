#!/usr/bin/env python3
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).parent.parent


class PackageContractTests(unittest.TestCase):
    def test_skill_frontmatter_is_minimal_and_named_correctly(self) -> None:
        text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\n"))
        _empty, frontmatter, _body = text.split("---", 2)
        fields = {}
        for line in frontmatter.strip().splitlines():
            key, separator, value = line.partition(":")
            self.assertTrue(separator, line)
            fields[key.strip()] = value.strip()
        self.assertEqual(set(fields), {"name", "description"})
        self.assertEqual(fields["name"], "codex-project-hygiene")
        self.assertRegex(fields["name"], r"^[a-z0-9-]+$")
        self.assertGreater(len(fields["description"]), 20)
        self.assertLessEqual(len(fields["description"]), 1024)

    def test_ui_manifest_mentions_the_skill(self) -> None:
        text = (ROOT / "agents" / "openai.yaml").read_text(encoding="utf-8")
        self.assertIn('display_name: "Codex Project Hygiene"', text)
        self.assertIn("$codex-project-hygiene", text)

    def test_workspace_hygiene_release_assets_exist(self) -> None:
        expected = (
            ROOT / "scripts" / "install_policy.py",
            ROOT / "scripts" / "hygiene_audit.py",
            ROOT / "tests" / "test_install_policy.py",
            ROOT / "tests" / "test_hygiene_audit.py",
            ROOT / "assets" / "AGENTS.project-hygiene.md",
            ROOT / "references" / "workspace-hygiene.md",
        )
        for path in expected:
            self.assertTrue(path.is_file(), str(path.relative_to(ROOT)))

    def test_release_tree_has_no_runtime_artifacts_or_private_data(self) -> None:
        forbidden_suffixes = {".pyc", ".sqlite", ".sqlite3", ".db", ".jsonl", ".png", ".jpg", ".jpeg", ".webp"}
        forbidden_names = {"__pycache__", ".DS_Store"}
        allowed_public_images = {Path("docs/social-preview.jpg")}
        for path in ROOT.rglob("*"):
            relative = path.relative_to(ROOT)
            self.assertFalse(any(part in forbidden_names for part in relative.parts), str(relative))
            if path.is_file():
                if relative not in allowed_public_images:
                    self.assertNotIn(path.suffix.casefold(), forbidden_suffixes, str(relative))
                self.assertIsNone(re.search(r"(^|[-_.])(plan|gallery|journal)([-_.]|$)", path.name, re.I), str(relative))

    def test_social_preview_is_bounded_synthetic_jpeg(self) -> None:
        preview = ROOT / "docs" / "social-preview.jpg"
        data = preview.read_bytes()
        self.assertTrue(data.startswith(b"\xff\xd8\xff"))
        self.assertLessEqual(len(data), 1_000_000)


if __name__ == "__main__":
    unittest.main(verbosity=2)

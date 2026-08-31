#!/usr/bin/env python3
import argparse
import importlib.util
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from typing import Optional
from unittest import mock


SCRIPTS = Path(__file__).parent.parent / "scripts"
HELPER = SCRIPTS / "media_cleanup.py"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("codex_media_cleanup", HELPER)
assert SPEC is not None and SPEC.loader is not None
MEDIA = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MEDIA
SPEC.loader.exec_module(MEDIA)


def run_helper(*args: str, cwd: Optional[Path] = None, env: Optional[dict[str, str]] = None):
    return subprocess.run(
        [sys.executable, str(HELPER), *args],
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def png_chunk(kind: bytes, payload: bytes) -> bytes:
    checksum = zlib.crc32(kind)
    checksum = zlib.crc32(payload, checksum) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", checksum)


def write_png(path: Path, *, invert: bool = False, tweak: int = 0) -> None:
    width, height = 18, 12
    rows = bytearray()
    for y in range(height):
        rows.append(0)
        for x in range(width):
            value = min(255, max(0, (x * 13 + y * 3 + tweak)))
            if invert:
                value = 255 - value
            rows.extend((value, value, value))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    data = b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", ihdr)
    data += png_chunk(b"IDAT", zlib.compress(bytes(rows))) + png_chunk(b"IEND", b"")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


class MediaCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="codex-media-test-"))
        self.codex_home = self.temp / "codex-home"
        self.root = self.codex_home / "generated_images"
        self.thread = self.root / "thread-one"
        self.thread.mkdir(parents=True)
        self.plan = self.temp / "media-plan.json"
        self.gallery = self.temp / "gallery.html"
        self.env = os.environ.copy()
        self.env["CODEX_HOME"] = str(self.codex_home)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp, ignore_errors=True)

    def scan(self):
        result = run_helper(
            "scan",
            "--root",
            str(self.root),
            "--plan",
            str(self.plan),
            "--gallery",
            str(self.gallery),
            "--visual-backend",
            "stdlib",
            env=self.env,
        )
        plan = json.loads(self.plan.read_text(encoding="utf-8")) if self.plan.exists() else {}
        return result, plan

    def token(self, item_id: str, *mode: str) -> str:
        result = run_helper(
            "token", "--plan", str(self.plan), "--ids", item_id, *mode, env=self.env
        )
        self.assertEqual(result.returncode, 0, result.stderr + "\n" + result.stdout)
        return next(
            line.split(": ", 1)[1]
            for line in result.stdout.splitlines()
            if line.startswith("Approval token:")
        )

    def test_scan_clusters_but_never_selects(self) -> None:
        write_png(self.thread / "first.png")
        shutil.copy2(self.thread / "first.png", self.thread / "exact.png")
        write_png(self.thread / "near.png", tweak=1)
        write_png(self.thread / "different.png", invert=True)
        result, plan = self.scan()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(plan["items"]), 4)
        self.assertTrue(all(item["classification"] == "review" for item in plan["items"]))
        self.assertTrue(all(item["decision"] == "unknown" for item in plan["items"]))
        self.assertEqual(len({item["exact_group"] for item in plan["items"] if item["exact_group"]}), 1)
        self.assertIn("Similarity is not rejection evidence", self.gallery.read_text(encoding="utf-8"))
        self.assertNotIn(" checked", self.gallery.read_text(encoding="utf-8"))
        self.assertIn("data:image/png;base64,", self.gallery.read_text(encoding="utf-8"))
        self.assertNotIn("file:", self.gallery.read_text(encoding="utf-8"))
        self.assertTrue(all("thumbnail_data_uri" not in item for item in plan["items"]))
        self.assertTrue((self.thread / "first.png").exists())

    def test_token_requires_explicit_ids(self) -> None:
        write_png(self.thread / "first.png")
        self.scan()
        result = run_helper(
            "token", "--plan", str(self.plan), "--ids", "", "--trash", env=self.env
        )
        self.assertEqual(result.returncode, 2)
        self.assertTrue((self.thread / "first.png").exists())

    def test_permanent_mode_requires_second_ack(self) -> None:
        write_png(self.thread / "first.png")
        _, plan = self.scan()
        item_id = plan["items"][0]["id"]
        result = run_helper(
            "token",
            "--plan",
            str(self.plan),
            "--ids",
            item_id,
            "--permanent",
            env=self.env,
        )
        self.assertEqual(result.returncode, 2)

    def test_exact_approved_file_is_deleted_and_sentinel_survives(self) -> None:
        selected = self.thread / "reject.png"
        kept = self.thread / "keep.png"
        sentinel = self.temp / "sentinel.txt"
        write_png(selected)
        write_png(kept, invert=True)
        sentinel.write_text("important", encoding="utf-8")
        _, plan = self.scan()
        item_id = next(item["id"] for item in plan["items"] if item["filename"] == selected.name)
        mode = ("--permanent", "--ack-no-recovery")
        token = self.token(item_id, *mode)
        result = run_helper(
            "apply",
            "--plan",
            str(self.plan),
            "--ids",
            item_id,
            "--confirm",
            token,
            *mode,
            cwd=self.temp,
            env=self.env,
        )
        journal_debug = "\n".join(
            path.read_text(encoding="utf-8") for path in self.temp.glob("media-plan.media-result-*.jsonl")
        )
        self.assertEqual(result.returncode, 0, result.stderr + "\n" + result.stdout + "\n" + journal_debug)
        self.assertFalse(selected.exists())
        self.assertTrue(kept.exists())
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "important")
        journals = list(self.temp.glob("media-plan.media-result-*.jsonl"))
        self.assertEqual(len(journals), 1)
        events = [json.loads(line) for line in journals[0].read_text().splitlines()]
        self.assertEqual(events[0]["type"], "header")
        self.assertEqual(events[-1]["type"], "summary")

    @unittest.skipUnless(
        sys.platform == "darwin" or sys.platform.startswith("linux"),
        "OS Trash adapter is implemented only for macOS and Linux",
    )
    def test_media_trash_flow_stays_inside_synthetic_home(self) -> None:
        selected = self.thread / "reject.png"
        write_png(selected)
        fake_home = self.temp / "fake-home"
        fake_home.mkdir()
        environment = self.env.copy()
        environment["HOME"] = str(fake_home)
        environment["XDG_DATA_HOME"] = str(fake_home / ".local" / "share")
        scanned = run_helper(
            "scan",
            "--root",
            str(self.root),
            "--plan",
            str(self.plan),
            "--visual-backend",
            "stdlib",
            env=environment,
        )
        self.assertEqual(scanned.returncode, 0, scanned.stderr)
        plan = json.loads(self.plan.read_text(encoding="utf-8"))
        item_id = plan["items"][0]["id"]
        token_result = run_helper(
            "token", "--plan", str(self.plan), "--ids", item_id, "--trash", env=environment
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
            "--ids",
            item_id,
            "--trash",
            "--confirm",
            token,
            cwd=self.temp,
            env=environment,
        )
        self.assertEqual(applied.returncode, 0, applied.stderr + "\n" + applied.stdout)
        self.assertFalse(selected.exists())
        if sys.platform == "darwin":
            trash_files = fake_home / ".Trash"
        else:
            trash_files = fake_home / ".local" / "share" / "Trash" / "files"
        moved = list(trash_files.glob("codex-rejected-*.png"))
        self.assertEqual(len(moved), 1)
        journal = next(self.temp.glob("media-plan.media-result-*.jsonl"))
        events = [json.loads(line) for line in journal.read_text().splitlines()]
        attempt = next(event for event in events if event["type"] == "attempt")
        self.assertEqual(
            MEDIA.core.real(Path(attempt["expected_trash_path"])), MEDIA.core.real(moved[0])
        )
        os.replace(moved[0], selected)
        self.assertTrue(selected.exists())

    def test_content_drift_is_skipped(self) -> None:
        selected = self.thread / "reject.png"
        write_png(selected)
        _, plan = self.scan()
        item_id = plan["items"][0]["id"]
        mode = ("--permanent", "--ack-no-recovery")
        token = self.token(item_id, *mode)
        write_png(selected, invert=True)
        result = run_helper(
            "apply",
            "--plan",
            str(self.plan),
            "--ids",
            item_id,
            "--confirm",
            token,
            *mode,
            cwd=self.temp,
            env=self.env,
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertTrue(selected.exists())

    def test_recomputed_plan_cannot_escape_media_root(self) -> None:
        write_png(self.thread / "reject.png")
        _, plan = self.scan()
        outside = self.temp / "outside.png"
        write_png(outside)
        plan["items"][0]["path"] = str(outside)
        plan["items"][0]["relative_path"] = "thread-one/reject.png"
        plan["seal"] = MEDIA.canonical_hash(plan)
        self.plan.write_text(json.dumps(plan), encoding="utf-8")
        result = run_helper(
            "token", "--plan", str(self.plan), "--ids", plan["items"][0]["id"], "--trash", env=self.env
        )
        self.assertEqual(result.returncode, 2)
        self.assertTrue(outside.exists())

    def test_recomputed_media_plan_from_another_host_is_rejected(self) -> None:
        write_png(self.thread / "reject.png")
        _, plan = self.scan()
        plan["host"] = "different-host.example"
        plan["seal"] = MEDIA.canonical_hash(plan)
        self.plan.write_text(json.dumps(plan), encoding="utf-8")
        result = run_helper(
            "token", "--plan", str(self.plan), "--ids", plan["items"][0]["id"], "--trash", env=self.env
        )
        self.assertEqual(result.returncode, 2)

    def test_recomputed_media_plan_cannot_forge_item_id(self) -> None:
        write_png(self.thread / "reject.png")
        _, plan = self.scan()
        plan["items"][0]["id"] = "0" * 16
        plan["seal"] = MEDIA.canonical_hash(plan)
        self.plan.write_text(json.dumps(plan), encoding="utf-8")
        result = run_helper(
            "token", "--plan", str(self.plan), "--ids", "0" * 16, "--trash", env=self.env
        )
        self.assertEqual(result.returncode, 2)

    def test_media_token_rejects_plan_symlink(self) -> None:
        write_png(self.thread / "reject.png")
        _, plan = self.scan()
        real_plan = self.temp / "real-media-plan.json"
        self.plan.replace(real_plan)
        os.symlink(real_plan, self.plan)
        result = run_helper(
            "token", "--plan", str(self.plan), "--ids", plan["items"][0]["id"], "--trash", env=self.env
        )
        self.assertEqual(result.returncode, 2)

    def test_arbitrary_image_root_is_rejected(self) -> None:
        other = self.temp / "photos"
        other.mkdir()
        result = run_helper(
            "scan", "--root", str(other), "--plan", str(self.plan), env=self.env
        )
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.plan.exists())

    def test_symlinked_configured_media_root_cannot_authorize_target(self) -> None:
        unsafe_home = self.temp / "unsafe-codex-home"
        unsafe_home.mkdir()
        external = self.temp / "external-media"
        write_png(external / "thread-x" / "private.png")
        os.symlink(external, unsafe_home / "generated_images")
        environment = os.environ.copy()
        environment["CODEX_HOME"] = str(unsafe_home)
        result = run_helper(
            "scan",
            "--root",
            str(MEDIA.core.real(external)),
            "--plan",
            str(self.plan),
            env=environment,
        )
        self.assertEqual(result.returncode, 2)
        self.assertTrue((external / "thread-x" / "private.png").exists())

    def test_corrupt_png_remains_reviewable_without_visual_hash(self) -> None:
        path = self.thread / "corrupt.png"
        path.write_bytes(b"not a png")
        result, plan = self.scan()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(plan["items"]), 1)
        self.assertIsNone(plan["items"][0]["dhash"])
        self.assertTrue(plan["items"][0]["visual_hash_error"])
        gallery = self.gallery.read_text(encoding="utf-8")
        self.assertIn("Preview unavailable", gallery)
        self.assertNotIn(path.as_uri(), gallery)

    def test_stdlib_visual_hash_rejects_oversized_png_before_reading(self) -> None:
        path = self.thread / "oversized.png"
        with path.open("wb") as handle:
            handle.seek(MEDIA.MAX_PNG_FILE_BYTES)
            handle.write(b"x")
        with self.assertRaisesRegex(MEDIA.MediaPolicyError, "visual-hash byte limit"):
            MEDIA.png_dhash(path)

    def test_symlink_is_not_in_inventory(self) -> None:
        outside = self.temp / "outside.png"
        write_png(outside)
        os.symlink(outside, self.thread / "linked.png")
        result, plan = self.scan()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(plan["items"], [])
        self.assertTrue(outside.exists())

    def test_future_dated_media_plan_is_rejected(self) -> None:
        write_png(self.thread / "reject.png")
        _, plan = self.scan()
        future = MEDIA.utc_now() + MEDIA.dt.timedelta(days=365)
        plan["created_at"] = MEDIA.iso_time(future)
        plan["expires_at"] = MEDIA.iso_time(
            future + MEDIA.dt.timedelta(hours=MEDIA.PLAN_TTL_HOURS)
        )
        plan["seal"] = MEDIA.canonical_hash(plan)
        self.plan.write_text(json.dumps(plan), encoding="utf-8")
        result = run_helper(
            "token", "--plan", str(self.plan), "--ids", plan["items"][0]["id"], "--trash", env=self.env
        )
        self.assertEqual(result.returncode, 2)

    def test_recomputed_allocated_size_cannot_hide_approval_total(self) -> None:
        write_png(self.thread / "reject.png")
        _, plan = self.scan()
        item = plan["items"][0]
        item["allocated_bytes"] = 0
        plan["seal"] = MEDIA.canonical_hash(plan)
        self.plan.write_text(json.dumps(plan), encoding="utf-8")
        result = run_helper(
            "token",
            "--plan",
            str(self.plan),
            "--ids",
            item["id"],
            "--permanent",
            "--ack-no-recovery",
            env=self.env,
        )
        self.assertEqual(result.returncode, 2)
        self.assertTrue((self.thread / "reject.png").exists())

    def test_thread_lock_stat_error_is_fail_closed(self) -> None:
        item = {"thread_id": "thread-one"}
        original_lstat = Path.lstat

        def flaky(path: Path):
            if path.name == "thread-one.lock":
                raise PermissionError("denied")
            return original_lstat(path)

        with mock.patch.object(MEDIA.Path, "lstat", flaky):
            failure = MEDIA.active_thread_failure(item)
        self.assertIn("could not inspect thread lock", failure or "")

    def test_apply_is_audit_only_on_unsupported_platform(self) -> None:
        with mock.patch.object(MEDIA.core.sys, "platform", "win32"):
            self.assertEqual(MEDIA.command_apply(argparse.Namespace()), 2)

    def test_media_token_binds_mutation_context(self) -> None:
        plan = {"seal": "a" * 64}
        selected = [{"id": "b" * 16}]
        first = {"backend": "freedesktop-trash", "platform": "linux", "files_root": "/one"}
        second = {"backend": "freedesktop-trash", "platform": "linux", "files_root": "/two"}
        self.assertNotEqual(
            MEDIA.approval_token(plan, selected, "trash", first),
            MEDIA.approval_token(plan, selected, "trash", second),
        )

    def test_gallery_limit_stops_before_decoding_unbounded_collection(self) -> None:
        for index in range(MEDIA.MAX_GALLERY_ITEMS + 1):
            (self.thread / f"{index:04d}.png").write_bytes(b"not-decoded")
        result = run_helper(
            "scan",
            "--root",
            str(self.root),
            "--plan",
            str(self.plan),
            "--gallery",
            str(self.gallery),
            "--visual-backend",
            "stdlib",
            env=self.env,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("safe limit", result.stderr)
        self.assertFalse(self.plan.exists())
        self.assertFalse(self.gallery.exists())

    def test_similarity_limit_is_linear_fail_closed_warning(self) -> None:
        items = [
            {
                "sha256": f"{index:064x}",
                "dhash": "0" * 16,
                "thread_id": "large-thread",
                "mtime_ns": index,
                "id": f"{index:016x}",
                "width": 100,
                "height": 100,
                "exact_group": None,
                "similar_group": None,
            }
            for index in range(MEDIA.MAX_SIMILARITY_ITEMS_PER_THREAD + 1)
        ]
        warnings = MEDIA.assign_groups(items, 8)
        self.assertTrue(any("complexity limit" in warning for warning in warnings))
        self.assertTrue(all(item["similar_group"] is None for item in items))

    def test_thread_filter_seals_bounded_inventory(self) -> None:
        write_png(self.thread / "one.png")
        other = self.root / "thread-two"
        write_png(other / "two.png")
        result = run_helper(
            "scan",
            "--root",
            str(self.root),
            "--thread",
            "thread-one",
            "--plan",
            str(self.plan),
            "--visual-backend",
            "stdlib",
            env=self.env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads(self.plan.read_text())
        self.assertEqual(plan["thread_filter"], ["thread-one"])
        self.assertEqual({item["thread_id"] for item in plan["items"]}, {"thread-one"})

    def test_scan_skips_same_device_linux_media_mount_id(self) -> None:
        write_png(self.thread / "private.png")
        table = [(MEDIA.core.real(self.thread), 22), (MEDIA.core.real(self.root), 11), (Path("/"), 1)]
        with mock.patch.object(MEDIA.core.sys, "platform", "linux"):
            with mock.patch.object(MEDIA.core, "linux_mount_table", return_value=(table, None)):
                items, warnings = MEDIA.scan_items(
                    MEDIA.core.real(self.root), 8, 1, "stdlib", None, False
                )
        self.assertEqual(items, [])
        self.assertTrue(any("mount-ID" in warning for warning in warnings))

    def test_visual_digest_must_match_exact_hash(self) -> None:
        path = self.thread / "race.png"
        write_png(path)
        with mock.patch.object(
            MEDIA,
            "image_metadata",
            return_value=(18, 12, "0" * 16, None, None, "0" * 64),
        ):
            result = MEDIA.inspect_image((str(path), "stdlib", None, False))
        self.assertFalse(result["ok"])
        self.assertIn("different image content", result["error"])

    def test_ffmpeg_16bit_decode_budget_stops_before_subprocess(self) -> None:
        width = height = 4096
        ihdr = struct.pack(">IIBBBBB", width, height, 16, 6, 0, 0, 0)
        path = self.thread / "bomb.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", ihdr))
        with mock.patch.object(MEDIA.subprocess, "run") as run:
            with self.assertRaisesRegex(MEDIA.MediaPolicyError, "decoded size"):
                MEDIA.ffmpeg_dhash(path, "ffmpeg")
        run.assert_not_called()

    @unittest.skipIf(os.name == "nt", "executable script fixture requires POSIX")
    def test_ffmpeg_output_is_strictly_bounded(self) -> None:
        path = self.thread / "valid.png"
        write_png(path)
        fake = self.temp / "fake-ffmpeg"
        fake.write_text(
            f"#!{sys.executable}\nimport sys\nsys.stdout.buffer.write(b'x' * 1000000)\n",
            encoding="utf-8",
        )
        fake.chmod(0o700)
        with self.assertRaisesRegex(MEDIA.MediaPolicyError, "strict safety limit"):
            MEDIA.ffmpeg_dhash(path, str(fake), include_thumbnail=False)

    def test_gallery_enforces_aggregate_thumbnail_budget(self) -> None:
        oversized = {
            "seal": "a" * 64,
            "items": [
                {"thumbnail_data_uri": "x" * (MEDIA.MAX_GALLERY_BYTES + 1)}
            ],
        }
        with self.assertRaisesRegex(MEDIA.MediaPolicyError, "gallery limit"):
            MEDIA.gallery_html(oversized)

    def test_permanent_recovery_unknown_reports_both_locations(self) -> None:
        path = self.thread / "reject.png"
        write_png(path)
        _, plan = self.scan()
        item = plan["items"][0]
        with mock.patch.object(MEDIA, "sha256_file", side_effect=OSError("synthetic hash failure")):
            with mock.patch.object(MEDIA.core, "path_presence", side_effect=[False, None, False]):
                with self.assertRaisesRegex(MEDIA.MediaPolicyError, "recovery state is unverifiable") as caught:
                    MEDIA.delete_permanently(path, item)
        tombstones = list(self.thread.glob(".codex-rejected-deleting-*.png"))
        self.assertEqual(len(tombstones), 1)
        self.assertIn(str(tombstones[0]), str(caught.exception))
        self.assertIn(str(path), str(caught.exception))
        os.replace(tombstones[0], path)


if __name__ == "__main__":
    unittest.main(verbosity=2)

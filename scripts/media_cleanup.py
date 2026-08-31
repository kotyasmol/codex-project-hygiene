#!/usr/bin/env python3
"""Review and remove explicitly rejected Codex-generated images.

Similarity is only a review aid. This helper never infers that an image was
rejected and never selects an image for deletion automatically.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import datetime as dt
import hashlib
import html
import json
import os
import platform
import shutil
import stat
import struct
import subprocess
import sys
import threading
import urllib.parse
import uuid
import zlib
from pathlib import Path
from typing import Iterable, Optional

import cleanup as core


SCHEMA_VERSION = 1
PLAN_KIND = "codex-generated-media-review"
PLAN_TTL_HOURS = 24
PLAN_CLOCK_SKEW_MINUTES = 5
SUPPORTED_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
MAX_PNG_PIXELS = 80_000_000
MAX_PNG_FILE_BYTES = 64 * 1024 * 1024
MAX_PNG_RAW_BYTES = 64 * 1024 * 1024
MAX_VISUAL_WORKERS = 2
MAX_THUMBNAIL_EDGE = 256
MAX_GALLERY_ITEMS = 200
MAX_GALLERY_BYTES = 32 * 1024 * 1024
MAX_SIMILARITY_ITEMS_PER_THREAD = 500


class MediaPolicyError(Exception):
    pass


def canonical_hash(value: dict) -> str:
    payload = dict(value)
    payload.pop("seal", None)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso_time(value: dt.datetime) -> str:
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_time(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024.0 or unit == "TiB":
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{size} B"


def new_file_path(value: str) -> Path:
    try:
        return core.new_output_path(Path(value))
    except OSError as error:
        raise MediaPolicyError(str(error)) from error


def media_roots() -> tuple[Path, ...]:
    return tuple(home / "generated_images" for home in core.codex_homes())


def validate_media_root(value: str) -> Path:
    expanded = Path(os.path.abspath(os.path.expanduser(value)))
    if not expanded.is_dir() or expanded.is_symlink():
        raise MediaPolicyError(f"media root must be a real directory: {expanded}")
    resolved = core.real(expanded)
    safe_allowed: list[Path] = []
    for allowed in media_roots():
        if core.has_symlink_component(allowed):
            continue
        try:
            info = allowed.lstat()
        except OSError:
            continue
        if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
            safe_allowed.append(core.real(allowed))
    if not any(core.same_path(resolved, allowed) for allowed in safe_allowed):
        allowed_text = ", ".join(core.display_path(path) for path in media_roots())
        raise MediaPolicyError(f"root must be an exact Codex generated_images directory: {allowed_text}")
    return resolved


def allocated_bytes(info: os.stat_result) -> int:
    blocks = getattr(info, "st_blocks", None)
    return int(blocks * 512) if blocks is not None else int(info.st_size)


def sha256_file(path: Path) -> tuple[str, os.stat_result]:
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode):
        raise MediaPolicyError(f"not a regular file: {path}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise MediaPolicyError(f"cannot safely open image: {error}") from error
    digest = hashlib.sha256()
    try:
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
            or stat.S_IFMT(opened.st_mode) != stat.S_IFMT(before.st_mode)
        ):
            raise MediaPolicyError("image changed before hashing")
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    path_after = path.lstat()
    before_key = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    after_key = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    path_after_key = (
        path_after.st_dev,
        path_after.st_ino,
        path_after.st_size,
        path_after.st_mtime_ns,
        path_after.st_ctime_ns,
    )
    if before_key != after_key or after_key != path_after_key:
        raise MediaPolicyError(f"file changed while hashing: {path}")
    return digest.hexdigest(), after


def png_data_uri(rgb: bytes, width: int, height: int) -> str:
    if len(rgb) != width * height * 3 or max(width, height) > MAX_THUMBNAIL_EDGE:
        raise MediaPolicyError("thumbnail pixel count is invalid")

    def chunk(kind: bytes, payload: bytes) -> bytes:
        checksum = zlib.crc32(kind)
        checksum = zlib.crc32(payload, checksum) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", checksum)

    stride = width * 3
    rows = b"".join(
        b"\x00" + rgb[index * stride : (index + 1) * stride] for index in range(height)
    )
    image = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows, level=9))
        + chunk(b"IEND", b"")
    )
    return "data:image/png;base64," + base64.b64encode(image).decode("ascii")


def pixel_rgb(row: bytes, x: int, channels: int, color_type: int) -> tuple[int, int, int]:
    start = x * channels
    if color_type == 0:
        value = row[start]
        return value, value, value
    if color_type == 2:
        return tuple(row[start : start + 3])  # type: ignore[return-value]
    if color_type == 4:
        value, alpha = row[start : start + 2]
        composite = (value * alpha + 255 * (255 - alpha)) // 255
        return composite, composite, composite
    red, green, blue, alpha = row[start : start + 4]
    return (
        (red * alpha + 255 * (255 - alpha)) // 255,
        (green * alpha + 255 * (255 - alpha)) // 255,
        (blue * alpha + 255 * (255 - alpha)) // 255,
    )


def read_bounded_regular_file(path: Path, limit: int) -> bytes:
    """Read at most limit+1 bytes from one stable, non-symlink file descriptor."""
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode):
        raise MediaPolicyError(f"not a regular non-symlink file: {path}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise MediaPolicyError(f"cannot safely open image: {error}") from error
    data = bytearray()
    try:
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
            or stat.S_IFMT(opened.st_mode) != stat.S_IFMT(before.st_mode)
        ):
            raise MediaPolicyError("image changed before bounded read")
        while len(data) <= limit:
            chunk = os.read(descriptor, min(1024 * 1024, limit + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if len(data) > limit or after.st_size > limit:
        raise MediaPolicyError("PNG file exceeds visual-hash byte limit")
    before_key = (
        opened.st_dev,
        opened.st_ino,
        opened.st_size,
        opened.st_mtime_ns,
        opened.st_ctime_ns,
    )
    after_key = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if before_key != after_key:
        raise MediaPolicyError("image changed during bounded read")
    return bytes(data)


def paeth(left: int, above: int, upper_left: int) -> int:
    estimate = left + above - upper_left
    left_distance = abs(estimate - left)
    above_distance = abs(estimate - above)
    upper_left_distance = abs(estimate - upper_left)
    if left_distance <= above_distance and left_distance <= upper_left_distance:
        return left
    if above_distance <= upper_left_distance:
        return above
    return upper_left


def png_dhash(
    path: Path, include_thumbnail: bool = True
) -> tuple[int, int, str, Optional[str], str]:
    """Return width, height, and a 64-bit dHash for common Codex PNGs."""
    data = read_bounded_regular_file(path, MAX_PNG_FILE_BYTES)
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise MediaPolicyError("invalid PNG signature")
    offset = 8
    compressed = bytearray()
    width = height = bit_depth = color_type = interlace = None
    saw_end = False
    while offset + 12 <= len(data):
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        chunk_type = data[offset + 4 : offset + 8]
        chunk_end = offset + 12 + length
        if chunk_end > len(data):
            raise MediaPolicyError("truncated PNG chunk")
        chunk_data = data[offset + 8 : offset + 8 + length]
        expected_crc = struct.unpack(">I", data[offset + 8 + length : chunk_end])[0]
        actual_crc = zlib.crc32(chunk_type)
        actual_crc = zlib.crc32(chunk_data, actual_crc) & 0xFFFFFFFF
        if actual_crc != expected_crc:
            raise MediaPolicyError("PNG CRC mismatch")
        if chunk_type == b"IHDR":
            if length != 13 or width is not None:
                raise MediaPolicyError("invalid PNG IHDR")
            width, height, bit_depth, color_type, compression, filtering, interlace = struct.unpack(
                ">IIBBBBB", chunk_data
            )
            if compression != 0 or filtering != 0:
                raise MediaPolicyError("unsupported PNG compression/filter method")
        elif chunk_type == b"IDAT":
            compressed.extend(chunk_data)
        elif chunk_type == b"IEND":
            saw_end = True
            break
        offset = chunk_end
    if not saw_end or width is None or height is None:
        raise MediaPolicyError("incomplete PNG")
    channels = {0: 1, 2: 3, 4: 2, 6: 4}.get(color_type)
    if bit_depth != 8 or channels is None or interlace != 0:
        raise MediaPolicyError("visual hash supports non-interlaced 8-bit grayscale/RGB/RGBA PNG")
    if width <= 0 or height <= 0 or width * height > MAX_PNG_PIXELS:
        raise MediaPolicyError("PNG dimensions exceed safety limit")
    stride = width * channels
    expected_size = (stride + 1) * height
    if expected_size > MAX_PNG_RAW_BYTES:
        raise MediaPolicyError("PNG decoded size exceeds safety limit")
    decoder = zlib.decompressobj()
    raw = bytearray(decoder.decompress(bytes(compressed), expected_size + 1))
    if decoder.unconsumed_tail or len(raw) > expected_size:
        raise MediaPolicyError("PNG decompressed data exceeds expected size")
    raw.extend(decoder.flush(expected_size + 1 - len(raw)))
    if len(raw) != expected_size or not decoder.eof:
        raise MediaPolicyError("PNG decompressed size mismatch")

    sample_x = [round(index * (width - 1) / 8) for index in range(9)]
    sample_y = [round(index * (height - 1) / 7) for index in range(8)]
    wanted_rows = {value: index for index, value in enumerate(sample_y)}
    samples: list[Optional[list[int]]] = [None] * 8
    scale = min(1.0, MAX_THUMBNAIL_EDGE / max(width, height))
    thumbnail_width = max(1, round(width * scale))
    thumbnail_height = max(1, round(height * scale))
    thumbnail_x = [
        round(index * (width - 1) / max(1, thumbnail_width - 1))
        for index in range(thumbnail_width)
    ]
    thumbnail_y = [
        round(index * (height - 1) / max(1, thumbnail_height - 1))
        for index in range(thumbnail_height)
    ]
    thumbnail_rows_by_source: dict[int, list[int]] = {}
    for target_index, source_index in enumerate(thumbnail_y):
        thumbnail_rows_by_source.setdefault(source_index, []).append(target_index)
    thumbnail_rows: list[Optional[bytes]] = [None] * thumbnail_height
    previous = bytearray(stride)
    cursor = 0
    for row_index in range(height):
        filter_type = raw[cursor]
        cursor += 1
        row = bytearray(raw[cursor : cursor + stride])
        cursor += stride
        if filter_type not in {0, 1, 2, 3, 4}:
            raise MediaPolicyError("unsupported PNG row filter")
        for index in range(stride):
            left = row[index - channels] if index >= channels else 0
            above = previous[index]
            upper_left = previous[index - channels] if index >= channels else 0
            if filter_type == 1:
                row[index] = (row[index] + left) & 0xFF
            elif filter_type == 2:
                row[index] = (row[index] + above) & 0xFF
            elif filter_type == 3:
                row[index] = (row[index] + ((left + above) // 2)) & 0xFF
            elif filter_type == 4:
                row[index] = (row[index] + paeth(left, above, upper_left)) & 0xFF
        if row_index in wanted_rows:
            values: list[int] = []
            for x in sample_x:
                red, green, blue = pixel_rgb(row, x, channels, color_type)
                gray = (299 * red + 587 * green + 114 * blue) // 1000
                values.append(gray)
            samples[wanted_rows[row_index]] = values
        if row_index in thumbnail_rows_by_source:
            thumbnail_row = bytearray()
            for x in thumbnail_x:
                thumbnail_row.extend(pixel_rgb(row, x, channels, color_type))
            for target_index in thumbnail_rows_by_source[row_index]:
                thumbnail_rows[target_index] = bytes(thumbnail_row)
        previous = row
    if any(values is None for values in samples):
        raise MediaPolicyError("could not sample PNG")
    if any(value is None for value in thumbnail_rows):
        raise MediaPolicyError("could not build bounded thumbnail")
    bits = 0
    for values in samples:
        assert values is not None
        for index in range(8):
            bits = (bits << 1) | int(values[index] > values[index + 1])
    if not include_thumbnail:
        return width, height, f"{bits:016x}", None, hashlib.sha256(data).hexdigest()
    thumbnail = b"".join(value for value in thumbnail_rows if value is not None)
    return (
        width,
        height,
        f"{bits:016x}",
        png_data_uri(thumbnail, thumbnail_width, thumbnail_height),
        hashlib.sha256(data).hexdigest(),
    )


def png_header(data: bytes) -> tuple[int, int, int, int]:
    header = data[:33]
    if len(header) != 33 or not header.startswith(b"\x89PNG\r\n\x1a\n"):
        raise MediaPolicyError("invalid PNG header")
    if header[12:16] != b"IHDR" or struct.unpack(">I", header[8:12])[0] != 13:
        raise MediaPolicyError("invalid PNG IHDR")
    expected_crc = struct.unpack(">I", header[29:33])[0]
    actual_crc = zlib.crc32(header[12:29]) & 0xFFFFFFFF
    if expected_crc != actual_crc:
        raise MediaPolicyError("PNG IHDR CRC mismatch")
    width, height, bit_depth, color_type, compression, filtering, _interlace = struct.unpack(
        ">IIBBBBB", header[16:29]
    )
    if width <= 0 or height <= 0 or width * height > MAX_PNG_PIXELS:
        raise MediaPolicyError("PNG dimensions exceed safety limit")
    if compression != 0 or filtering != 0:
        raise MediaPolicyError("unsupported PNG compression/filter method")
    return width, height, bit_depth, color_type


def png_dimensions(path: Path) -> tuple[int, int]:
    data = read_bounded_regular_file(path, MAX_PNG_FILE_BYTES)
    width, height, _bit_depth, _color_type = png_header(data)
    return width, height


def run_bounded_process(
    command: list[str],
    input_data: bytes,
    stdout_limit: int,
    stderr_limit: int,
    timeout: float,
) -> tuple[int, bytes, bytes]:
    """Run an optional decoder without allowing unbounded pipe buffering."""
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as error:
        raise MediaPolicyError(f"ffmpeg visual hash failed: {error}") from error
    stdout = bytearray()
    stderr = bytearray()
    overflow = threading.Event()

    def drain(stream, buffer: bytearray, limit: int) -> None:
        try:
            while True:
                chunk = stream.read(64 * 1024)
                if not chunk:
                    break
                remaining = limit + 1 - len(buffer)
                if remaining > 0:
                    buffer.extend(chunk[:remaining])
                if len(buffer) > limit or len(chunk) > remaining:
                    overflow.set()
                    try:
                        process.kill()
                    except OSError:
                        pass
                    break
        except (OSError, ValueError):
            overflow.set()

    def feed() -> None:
        try:
            assert process.stdin is not None
            process.stdin.write(input_data)
            process.stdin.close()
        except (BrokenPipeError, OSError, ValueError):
            pass

    assert process.stdout is not None and process.stderr is not None
    readers = [
        threading.Thread(target=drain, args=(process.stdout, stdout, stdout_limit), daemon=True),
        threading.Thread(target=drain, args=(process.stderr, stderr, stderr_limit), daemon=True),
    ]
    writer = threading.Thread(target=feed, daemon=True)
    for thread in readers:
        thread.start()
    writer.start()
    try:
        return_code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as error:
        process.kill()
        process.wait()
        raise MediaPolicyError("ffmpeg visual hash timed out") from error
    finally:
        writer.join(timeout=1)
        for thread in readers:
            thread.join(timeout=1)
        if writer.is_alive() or any(thread.is_alive() for thread in readers):
            overflow.set()
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is None:
                continue
            try:
                stream.close()
            except OSError:
                pass
    if overflow.is_set():
        raise MediaPolicyError("ffmpeg output exceeded its strict safety limit")
    return return_code, bytes(stdout), bytes(stderr)


def ffmpeg_dhash(
    path: Path, executable: str, include_thumbnail: bool = True
) -> tuple[int, int, str, Optional[str], str]:
    data = read_bounded_regular_file(path, MAX_PNG_FILE_BYTES)
    width, height, bit_depth, color_type = png_header(data)
    if color_type not in {0, 2, 3, 4, 6} or bit_depth not in {1, 2, 4, 8, 16}:
        raise MediaPolicyError("unsupported PNG color type/bit depth")
    decoded_bytes = width * height * (8 if bit_depth == 16 else 4)
    if decoded_bytes > MAX_PNG_RAW_BYTES:
        raise MediaPolicyError("PNG decoded size exceeds visual-hash safety limit")
    if include_thumbnail:
        scale = min(1.0, MAX_THUMBNAIL_EDGE / max(width, height))
        thumbnail_width = max(1, round(width * scale))
        thumbnail_height = max(1, round(height * scale))
        filter_value = f"scale={thumbnail_width}:{thumbnail_height}:flags=area,format=rgb24"
        pixel_format = "rgb24"
    else:
        thumbnail_width, thumbnail_height = 9, 8
        filter_value = "scale=9:8:flags=area,format=gray"
        pixel_format = "gray"
    command = [
        executable,
        "-v",
        "error",
        "-i",
        "pipe:0",
        "-vf",
        filter_value,
        "-frames:v",
        "1",
        "-f",
        "rawvideo",
        "-pix_fmt",
        pixel_format,
        "-",
    ]
    expected_thumbnail_bytes = thumbnail_width * thumbnail_height * (3 if include_thumbnail else 1)
    return_code, output, error_output = run_bounded_process(
        command,
        data,
        stdout_limit=expected_thumbnail_bytes,
        stderr_limit=64 * 1024,
        timeout=30,
    )
    if return_code != 0 or len(output) != expected_thumbnail_bytes:
        detail = error_output.decode("utf-8", errors="replace").strip()[:200]
        raise MediaPolicyError(f"ffmpeg visual hash failed with status {return_code}: {detail}")
    bits = 0
    sample_x = [round(index * (thumbnail_width - 1) / 8) for index in range(9)]
    sample_y = [round(index * (thumbnail_height - 1) / 7) for index in range(8)]
    for y in sample_y:
        values = []
        for x in sample_x:
            if include_thumbnail:
                start = (y * thumbnail_width + x) * 3
                red, green, blue = output[start : start + 3]
                values.append((299 * red + 587 * green + 114 * blue) // 1000)
            else:
                values.append(output[y * thumbnail_width + x])
        for index in range(8):
            bits = (bits << 1) | int(values[index] > values[index + 1])
    thumbnail = (
        png_data_uri(output, thumbnail_width, thumbnail_height)
        if include_thumbnail
        else None
    )
    return width, height, f"{bits:016x}", thumbnail, hashlib.sha256(data).hexdigest()


def image_metadata(
    path: Path, backend: str, ffmpeg_path: Optional[str], include_thumbnail: bool
) -> tuple[
    Optional[int],
    Optional[int],
    Optional[str],
    Optional[str],
    Optional[str],
    Optional[str],
]:
    if path.suffix.casefold() != ".png":
        return None, None, None, None, "perceptual hash currently supports PNG; exact hash is still available", None
    try:
        if backend == "none":
            data = read_bounded_regular_file(path, MAX_PNG_FILE_BYTES)
            width, height, _depth, _color = png_header(data)
            return width, height, None, None, "visual hashing disabled", hashlib.sha256(data).hexdigest()
        if backend == "ffmpeg":
            if not ffmpeg_path:
                raise MediaPolicyError("ffmpeg backend requested but ffmpeg was not found")
            width, height, perceptual, thumbnail, visual_digest = ffmpeg_dhash(
                path, ffmpeg_path, include_thumbnail
            )
        else:
            width, height, perceptual, thumbnail, visual_digest = png_dhash(
                path, include_thumbnail
            )
        return width, height, perceptual, thumbnail, None, visual_digest
    except (OSError, ValueError, zlib.error, MediaPolicyError) as error:
        return None, None, None, None, str(error), None


def hamming(left: str, right: str) -> int:
    return bin(int(left, 16) ^ int(right, 16)).count("1")


def similar(left: dict, right: dict, threshold: int) -> bool:
    if not left.get("dhash") or not right.get("dhash"):
        return False
    if left["thread_id"] != right["thread_id"]:
        return False
    left_ratio = left["width"] / left["height"]
    right_ratio = right["width"] / right["height"]
    if abs(left_ratio - right_ratio) / max(left_ratio, right_ratio) > 0.05:
        return False
    return hamming(left["dhash"], right["dhash"]) <= threshold


def assign_groups(items: list[dict], threshold: int) -> list[str]:
    warnings: list[str] = []
    exact: dict[str, list[dict]] = {}
    for item in items:
        exact.setdefault(item["sha256"], []).append(item)
    for digest, members in exact.items():
        if len(members) < 2:
            continue
        group_id = "exact-" + digest[:12]
        for member in members:
            member["exact_group"] = group_id

    by_thread: dict[str, list[dict]] = {}
    for item in items:
        if item.get("dhash"):
            by_thread.setdefault(item["thread_id"], []).append(item)
    for thread_id, members in by_thread.items():
        if len(members) > MAX_SIMILARITY_ITEMS_PER_THREAD:
            warnings.append(
                f"visual grouping disabled for thread {thread_id}: {len(members)} images exceed "
                f"the {MAX_SIMILARITY_ITEMS_PER_THREAD}-item complexity limit"
            )
            continue
        remaining = sorted(members, key=lambda item: (item["mtime_ns"], item["id"]))
        cluster_number = 0
        while remaining:
            seed = remaining.pop(0)
            cluster = [seed]
            rest: list[dict] = []
            for candidate in remaining:
                if similar(seed, candidate, threshold):
                    cluster.append(candidate)
                else:
                    rest.append(candidate)
            remaining = rest
            if len(cluster) < 2:
                continue
            cluster_number += 1
            cluster_id = hashlib.sha256(
                (thread_id + ":" + str(cluster_number) + ":" + ":".join(item["id"] for item in cluster)).encode()
            ).hexdigest()[:12]
            for member in cluster:
                member["similar_group"] = "similar-" + cluster_id
    return warnings


def inspect_image(task: tuple[str, str, Optional[str], bool]) -> dict:
    """CPU-heavy worker; returns data only and never mutates the image."""
    path_value, backend, ffmpeg_path, include_thumbnail = task
    path = Path(path_value)
    try:
        identity_before = core.identity(path)
        digest, stable_info = sha256_file(path)
        width, height, perceptual, thumbnail, visual_error, visual_digest = image_metadata(
            path, backend, ffmpeg_path, include_thumbnail
        )
        identity_after = core.identity(path)
        if identity_before != identity_after:
            raise MediaPolicyError("image identity changed across inspection")
        if visual_digest is not None and visual_digest != digest:
            raise MediaPolicyError("thumbnail/visual metadata came from different image content")
        stable_identity = {
            "device": stable_info.st_dev,
            "inode": stable_info.st_ino,
            "mode": stat.S_IFMT(stable_info.st_mode),
            "mtime_ns": stable_info.st_mtime_ns,
            "ctime_ns": stable_info.st_ctime_ns,
        }
        if stable_identity != identity_after:
            raise MediaPolicyError("hash and final filesystem identity differ")
        return {
            "ok": True,
            "sha256": digest,
            "size_bytes": stable_info.st_size,
            "allocated_bytes": allocated_bytes(stable_info),
            "mtime_ns": stable_info.st_mtime_ns,
            "width": width,
            "height": height,
            "dhash": perceptual,
            "thumbnail_data_uri": thumbnail,
            "visual_hash_error": visual_error,
            "identity": identity_after,
        }
    except (OSError, ValueError, MediaPolicyError) as error:
        return {"ok": False, "error": str(error)}


def scan_items(
    root: Path,
    threshold: int,
    workers: int,
    backend: str,
    ffmpeg_path: Optional[str],
    include_thumbnails: bool,
    thread_ids: Optional[set[str]] = None,
) -> tuple[list[dict], list[str]]:
    pending: list[tuple[Path, str, str]] = []
    warnings: list[str] = []
    root_device = root.lstat().st_dev
    linux_mounts: Optional[list[tuple[Path, int]]] = None
    root_mount_id: Optional[int] = None
    if sys.platform.startswith("linux"):
        linux_mounts, mount_error = core.linux_mount_table()
        if mount_error:
            raise MediaPolicyError(mount_error)
        root_mount_id = core.linux_mount_id(root, linux_mounts)
        if root_mount_id is None:
            raise MediaPolicyError("cannot determine Linux mount ID for media root")
    try:
        thread_entries = sorted(os.scandir(root), key=lambda entry: os.fsencode(entry.name))
    except OSError as error:
        raise MediaPolicyError(f"cannot read media root: {error}") from error
    for thread_entry in thread_entries:
        if thread_ids and thread_entry.name not in thread_ids:
            continue
        thread_path = Path(thread_entry.path)
        try:
            if thread_entry.is_symlink():
                warnings.append(f"skipped symlink thread entry: {thread_entry.name}")
                continue
            thread_info = thread_entry.stat(follow_symlinks=False)
            if not thread_entry.is_dir(follow_symlinks=False):
                continue
            if thread_info.st_dev != root_device or os.path.ismount(thread_path):
                warnings.append(f"skipped mount/device boundary: {thread_entry.name}")
                continue
            if (
                linux_mounts is not None
                and core.linux_mount_id(thread_path, linux_mounts) != root_mount_id
            ):
                warnings.append(f"skipped Linux mount-ID boundary: {thread_entry.name}")
                continue
            file_entries = sorted(os.scandir(thread_path), key=lambda entry: os.fsencode(entry.name))
        except OSError as error:
            warnings.append(f"could not inspect thread directory {thread_entry.name}: {error}")
            continue
        for file_entry in file_entries:
            path = Path(file_entry.path)
            try:
                if file_entry.is_symlink():
                    warnings.append(f"skipped image symlink: {thread_entry.name}/{file_entry.name}")
                    continue
                info = file_entry.stat(follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode) or info.st_dev != root_device:
                    continue
                if linux_mounts is not None and core.linux_mount_id(path, linux_mounts) != root_mount_id:
                    warnings.append(
                        f"skipped image across Linux mount-ID boundary: "
                        f"{thread_entry.name}/{file_entry.name}"
                    )
                    continue
                if path.suffix.casefold() not in SUPPORTED_SUFFIXES:
                    continue
            except OSError as error:
                warnings.append(f"could not inspect {thread_entry.name}/{file_entry.name}: {error}")
                continue
            pending.append((path, thread_entry.name, file_entry.name))

    if include_thumbnails and len(pending) > MAX_GALLERY_ITEMS:
        raise MediaPolicyError(
            f"gallery would contain {len(pending)} images; the safe limit is {MAX_GALLERY_ITEMS}. "
            "Rerun with one or more --thread filters instead of one unbounded gallery."
        )
    worker_count = max(1, min(workers, MAX_VISUAL_WORKERS))
    tasks = [
        (str(path), backend, ffmpeg_path, include_thumbnails)
        for path, _thread, _name in pending
    ]
    if worker_count == 1 or len(pending) < 10:
        inspected = [inspect_image(task) for task in tasks]
    else:
        with concurrent.futures.ProcessPoolExecutor(max_workers=worker_count) as executor:
            inspected = list(executor.map(inspect_image, tasks))

    items: list[dict] = []
    for (path, thread_name, file_name), result in zip(pending, inspected):
        if not result.get("ok"):
            warnings.append(f"could not hash {thread_name}/{file_name}: {result.get('error', 'unknown error')}")
            continue
        relative = path.relative_to(root).as_posix()
        item_id = hashlib.sha256((relative + "\0" + result["sha256"]).encode("utf-8")).hexdigest()[:16]
        items.append(
            {
                "id": item_id,
                "path": str(core.real(path)),
                "relative_path": relative,
                "thread_id": thread_name,
                "filename": file_name,
                "size_bytes": result["size_bytes"],
                "allocated_bytes": result["allocated_bytes"],
                "mtime_ns": result["mtime_ns"],
                "sha256": result["sha256"],
                "width": result["width"],
                "height": result["height"],
                "dhash": result["dhash"],
                "thumbnail_data_uri": result["thumbnail_data_uri"],
                "visual_hash_error": result["visual_hash_error"],
                "exact_group": None,
                "similar_group": None,
                "classification": "review",
                "decision": "unknown",
                "identity": result["identity"],
            }
        )
    warnings.extend(assign_groups(items, threshold))
    if include_thumbnails:
        thumbnail_bytes = sum(len(item.get("thumbnail_data_uri") or "") for item in items)
        if thumbnail_bytes > MAX_GALLERY_BYTES:
            raise MediaPolicyError(
                f"bounded thumbnails total {thumbnail_bytes} bytes; gallery limit is "
                f"{MAX_GALLERY_BYTES}. Rerun with narrower --thread filters."
            )
    return items, warnings


def write_new_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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


def gallery_html(plan: dict) -> str:
    thumbnail_bytes = sum(
        len(item.get("thumbnail_data_uri") or "") for item in plan.get("items", [])
    )
    if thumbnail_bytes > MAX_GALLERY_BYTES:
        raise MediaPolicyError(
            f"bounded thumbnails total {thumbnail_bytes} bytes; gallery limit is "
            f"{MAX_GALLERY_BYTES}. Rerun with narrower --thread filters."
        )
    grouped: dict[str, list[dict]] = {}
    singles: list[dict] = []
    for item in plan["items"]:
        group = item.get("similar_group") or item.get("exact_group")
        if group:
            grouped.setdefault(group, []).append(item)
        else:
            singles.append(item)

    def card(item: dict) -> str:
        dimensions = (
            f"{item['width']}×{item['height']}" if item.get("width") and item.get("height") else "dimensions unknown"
        )
        thumbnail = item.get("thumbnail_data_uri")
        preview = (
            f'<img loading="lazy" src="{html.escape(thumbnail, quote=True)}" '
            f'alt="Bounded thumbnail for generated image {html.escape(item["id"])}">'
            if thumbnail
            else '<div class="placeholder">Preview unavailable; inspect the original only with a trusted, resource-limited viewer.</div>'
        )
        return f"""
        <article class="card">
          {preview}
          <label><input type="checkbox" value="{html.escape(item['id'])}"> explicitly reject this file</label>
          <code>{html.escape(item['id'])}</code>
          <small>{html.escape(item['path'])}</small>
          <small>{html.escape(dimensions)} · {html.escape(human_size(item['allocated_bytes']))}</small>
        </article>"""

    sections: list[str] = []
    for group_id, members in sorted(grouped.items()):
        cards = "".join(card(item) for item in sorted(members, key=lambda item: item["mtime_ns"]))
        sections.append(
            f"<section><h2>{html.escape(group_id)} · {len(members)} review candidates</h2>"
            f"<div class=grid>{cards}</div></section>"
        )
    if singles:
        cards = "".join(card(item) for item in sorted(singles, key=lambda item: item["mtime_ns"], reverse=True))
        sections.append(
            f"<details><summary>Unclustered images ({len(singles)})</summary><div class=grid>{cards}</div></details>"
        )
    body = "".join(sections) or "<p>No supported images found.</p>"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline'; script-src 'unsafe-inline'">
<title>Codex generated image review</title>
<style>
body{{font:15px system-ui;margin:24px;background:#0d1117;color:#e6edf3}} h1,h2{{line-height:1.2}}
.notice{{padding:14px;border:1px solid #d29922;border-radius:10px;background:#2b2300;max-width:900px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:14px;margin:14px 0 30px}}
.card{{display:grid;gap:8px;padding:10px;border:1px solid #30363d;border-radius:10px;background:#161b22}}
.card img{{width:100%;height:180px;object-fit:contain;background:#010409;border-radius:6px}}
.placeholder{{min-height:120px;display:grid;place-items:center;padding:14px;background:#010409;color:#8b949e;border-radius:6px}}
code,small{{overflow-wrap:anywhere}} textarea{{width:min(900px,100%);min-height:90px;background:#010409;color:#e6edf3}}
button{{padding:8px 12px;margin:8px 8px 8px 0}} details{{margin-top:24px}}
</style></head><body>
<h1>Codex generated image review</h1>
<p class="notice"><strong>Similarity is not rejection evidence.</strong> Nothing is selected by default.
Check only files you explicitly reject. Accepted, exported, referenced, or uncertain images should remain unchecked.
Previews are bounded, lossy thumbnails; inspect the exact path in a trusted resource-limited viewer whenever fine detail matters.</p>
<p>Plan seal: <code>{html.escape(plan['seal'])}</code> · {len(plan['items'])} images · local-only report</p>
{body}
<h2>Explicit reject IDs</h2>
<button id=build>Build exact ID list</button><button id=copy>Copy</button>
<textarea id=ids readonly placeholder="No images selected"></textarea>
<script>
const box=document.getElementById('ids');
function build(){{box.value=[...document.querySelectorAll('input[type=checkbox]:checked')].map(x=>x.value).sort().join(',');}}
document.getElementById('build').onclick=build;
document.getElementById('copy').onclick=async()=>{{build();if(box.value)await navigator.clipboard.writeText(box.value);}};
</script></body></html>"""


def command_scan(args: argparse.Namespace) -> int:
    try:
        root = validate_media_root(args.root or str(core.codex_home() / "generated_images"))
        plan_path = new_file_path(args.plan)
        gallery_path = new_file_path(args.gallery) if args.gallery else None
        if not 0 <= args.threshold <= 16:
            raise MediaPolicyError("similarity threshold must be between 0 and 16")
        workers = args.workers or min(os.cpu_count() or 1, 8)
        ffmpeg_path = shutil.which("ffmpeg")
        backend = args.visual_backend
        if backend == "auto":
            backend = "ffmpeg" if ffmpeg_path else "stdlib"
        workers = min(workers, MAX_VISUAL_WORKERS)
        thread_ids = set(args.thread or [])
        for thread_id in thread_ids:
            if thread_id in {"", ".", ".."} or Path(thread_id).name != thread_id:
                raise MediaPolicyError(f"invalid direct thread ID: {core.display_path(thread_id)}")
            thread_path = root / thread_id
            try:
                thread_info = thread_path.lstat()
            except OSError as error:
                raise MediaPolicyError(
                    f"cannot inspect requested thread {core.display_path(thread_id)}: {error}"
                ) from error
            if stat.S_ISLNK(thread_info.st_mode) or not stat.S_ISDIR(thread_info.st_mode):
                raise MediaPolicyError(
                    f"requested thread is not a real directory: {core.display_path(thread_id)}"
                )
        items, warnings = scan_items(
            root,
            args.threshold,
            workers,
            backend,
            ffmpeg_path,
            include_thumbnails=gallery_path is not None,
            thread_ids=thread_ids or None,
        )
        plan_items = []
        for item in items:
            plan_item = dict(item)
            plan_item.pop("thumbnail_data_uri", None)
            plan_items.append(plan_item)
        created = utc_now()
        plan = {
            "schema_version": SCHEMA_VERSION,
            "kind": PLAN_KIND,
            "created_at": iso_time(created),
            "expires_at": iso_time(created + dt.timedelta(hours=PLAN_TTL_HOURS)),
            "host": platform.node(),
            "codex_context": core.codex_context(),
            "root": str(root),
            "thread_filter": sorted(thread_ids),
            "similarity": {
                "algorithm": f"dhash-64-{backend}-review-only",
                "threshold": args.threshold,
                "scope": "within-thread",
            },
            "items": plan_items,
            "warnings": warnings,
            "notes": [
                "No Codex state currently provides a reliable accepted/rejected flag.",
                "Exact and perceptual hashes are review aids; neither authorizes deletion.",
                "Only explicit IDs can be selected, and apply revalidates full SHA-256 and filesystem identity.",
            ],
        }
        plan["seal"] = canonical_hash(plan)
        gallery_value: Optional[str] = None
        if gallery_path:
            gallery_plan = dict(plan)
            gallery_plan["items"] = items
            gallery_value = gallery_html(gallery_plan)
        core.write_secure_json(plan_path, plan)
        if gallery_path and gallery_value is not None:
            write_new_text(gallery_path, gallery_value)
    except (OSError, ValueError, MediaPolicyError) as error:
        print(f"Policy error: {error}", file=sys.stderr)
        return 2
    exact_groups = len({item["exact_group"] for item in items if item.get("exact_group")})
    similar_groups = len({item["similar_group"] for item in items if item.get("similar_group")})
    print(f"Plan: {core.display_path(plan_path)}")
    if gallery_path:
        print(f"Gallery: {core.display_path(gallery_path)}")
    print(f"Seal: {plan['seal']}")
    print(
        f"Images: {len(items)} ({human_size(sum(item['allocated_bytes'] for item in items))}); "
        f"exact groups={exact_groups}; visual review groups={similar_groups}"
    )
    print("Selected for deletion: 0. Review is mandatory; similarity never means rejected.")
    if warnings:
        print(f"Warnings: {len(warnings)} (stored in the sealed plan)")
    return 0


def validate_plan_schema(plan: dict) -> None:
    if not isinstance(plan, dict):
        raise MediaPolicyError("media plan must be a JSON object")
    if plan.get("schema_version") != SCHEMA_VERSION or plan.get("kind") != PLAN_KIND:
        raise MediaPolicyError("unsupported media plan schema")
    if not core._sha256_text(plan.get("seal")) or plan["seal"] != canonical_hash(plan):
        raise MediaPolicyError("plan seal mismatch")
    if plan.get("host") != platform.node():
        raise MediaPolicyError("plan belongs to another host")
    if plan.get("codex_context") != core.codex_context():
        raise MediaPolicyError("CODEX_HOME/path policy changed since scan")
    try:
        created = parse_time(plan["created_at"])
        expires = parse_time(plan["expires_at"])
    except (KeyError, TypeError, ValueError) as error:
        raise MediaPolicyError("plan timestamps are malformed") from error
    if created.tzinfo is None or expires.tzinfo is None or expires <= created:
        raise MediaPolicyError("plan timestamp interval is invalid")
    if expires - created > dt.timedelta(hours=PLAN_TTL_HOURS, minutes=1):
        raise MediaPolicyError("plan lifetime exceeds policy")
    now = utc_now()
    skew = dt.timedelta(minutes=PLAN_CLOCK_SKEW_MINUTES)
    if created > now + skew:
        raise MediaPolicyError("plan was created too far in the future")
    if expires > now + dt.timedelta(hours=PLAN_TTL_HOURS) + skew:
        raise MediaPolicyError("plan expiry is too far in the future")
    if now > expires:
        raise MediaPolicyError("plan expired; scan again")
    root = validate_media_root(plan.get("root", ""))
    if plan.get("root") != str(root):
        raise MediaPolicyError("media root is not canonical")
    thread_filter = plan.get("thread_filter")
    if (
        not isinstance(thread_filter, list)
        or not all(isinstance(value, str) and value and Path(value).name == value for value in thread_filter)
        or thread_filter != sorted(set(thread_filter))
    ):
        raise MediaPolicyError("thread filter is malformed")
    similarity = plan.get("similarity")
    if not isinstance(similarity, dict):
        raise MediaPolicyError("similarity metadata is malformed")
    algorithm = similarity.get("algorithm")
    threshold = similarity.get("threshold")
    if (
        not isinstance(algorithm, str)
        or not algorithm.startswith("dhash-64-")
        or not algorithm.endswith("-review-only")
        or not core._plain_int(threshold)
        or not 0 <= threshold <= 16
        or similarity.get("scope") != "within-thread"
    ):
        raise MediaPolicyError("similarity policy is malformed")
    warnings = plan.get("warnings")
    notes = plan.get("notes")
    if not isinstance(warnings, list) or not all(isinstance(value, str) for value in warnings):
        raise MediaPolicyError("media warnings are malformed")
    if not isinstance(notes, list) or not all(isinstance(value, str) for value in notes):
        raise MediaPolicyError("media notes are malformed")
    items = plan.get("items")
    if not isinstance(items, list):
        raise MediaPolicyError("media plan items are malformed")
    seen: set[str] = set()
    seen_paths: set[str] = set()
    identity_keys = {"device", "inode", "mode", "mtime_ns", "ctime_ns"}
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise MediaPolicyError("malformed plan item")
        if (
            len(item["id"]) != 16
            or any(character not in "0123456789abcdef" for character in item["id"])
            or item["id"] in seen
        ):
            raise MediaPolicyError("duplicate plan item ID")
        seen.add(item["id"])
        path_value = item.get("path")
        if not isinstance(path_value, str) or str(core.real(Path(path_value))) != path_value:
            raise MediaPolicyError("plan item path is not canonical")
        if path_value in seen_paths:
            raise MediaPolicyError("duplicate plan item path")
        seen_paths.add(path_value)
        path = Path(path_value)
        if not core.is_within(path, root, allow_equal=False):
            raise MediaPolicyError("plan item escaped media root")
        boundary_failure = core.filesystem_boundary_failure(path, root)
        if boundary_failure:
            raise MediaPolicyError(boundary_failure)
        try:
            relative = path.relative_to(root)
        except ValueError as error:
            raise MediaPolicyError("plan item has invalid relative path") from error
        if len(relative.parts) != 2 or relative.as_posix() != item.get("relative_path"):
            raise MediaPolicyError("plan item is not a direct thread image")
        if item.get("thread_id") != relative.parts[0] or item.get("filename") != relative.parts[1]:
            raise MediaPolicyError("plan item thread/filename mismatch")
        if thread_filter and item.get("thread_id") not in thread_filter:
            raise MediaPolicyError("plan item is outside the sealed thread filter")
        if path.suffix.casefold() not in SUPPORTED_SUFFIXES:
            raise MediaPolicyError("plan item has an unsupported image suffix")
        digest = item.get("sha256")
        if not core._sha256_text(digest):
            raise MediaPolicyError("plan item SHA-256 is malformed")
        expected_id = hashlib.sha256((item["relative_path"] + "\0" + digest).encode("utf-8")).hexdigest()[:16]
        if item["id"] != expected_id:
            raise MediaPolicyError("plan item ID does not match its path/content")
        for key in ("size_bytes", "allocated_bytes", "mtime_ns"):
            if not core._plain_int(item.get(key)) or item[key] < 0:
                raise MediaPolicyError(f"plan item {key} is malformed")
        for key in ("width", "height"):
            if item.get(key) is not None and (
                not core._plain_int(item.get(key)) or item[key] <= 0
            ):
                raise MediaPolicyError(f"plan item {key} is malformed")
        dhash = item.get("dhash")
        if dhash is not None and (
            not isinstance(dhash, str)
            or len(dhash) != 16
            or any(character not in "0123456789abcdef" for character in dhash)
        ):
            raise MediaPolicyError("plan item visual hash is malformed")
        if "thumbnail_data_uri" in item:
            raise MediaPolicyError("bounded thumbnails belong only in the local gallery")
        for key in ("visual_hash_error", "exact_group", "similar_group"):
            if item.get(key) is not None and not isinstance(item.get(key), str):
                raise MediaPolicyError(f"plan item {key} is malformed")
        if item.get("classification") != "review" or item.get("decision") != "unknown":
            raise MediaPolicyError("plan cannot pre-authorize a media decision")
        identity = item.get("identity")
        if not isinstance(identity, dict) or set(identity) != identity_keys:
            raise MediaPolicyError("plan item identity is malformed")
        if not all(core._plain_int(identity[key]) for key in identity_keys):
            raise MediaPolicyError("plan item identity fields are malformed")
        if identity["mtime_ns"] != item["mtime_ns"]:
            raise MediaPolicyError("plan item timestamps are inconsistent")


def read_plan(path: Path) -> dict:
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MediaPolicyError(f"cannot read plan: {error}") from error
    validate_plan_schema(plan)
    return plan


def select_items(plan: dict, value: str) -> list[dict]:
    requested = {part.strip() for part in value.split(",") if part.strip()}
    if not requested:
        raise MediaPolicyError("explicit --ids are required; there is no automatic media selection")
    by_id = {item["id"]: item for item in plan["items"]}
    unknown = requested - set(by_id)
    if unknown:
        raise MediaPolicyError("unknown image IDs: " + ", ".join(sorted(unknown)))
    return [by_id[item_id] for item_id in sorted(requested)]


def mutation_mode(args: argparse.Namespace) -> str:
    if args.trash == args.permanent:
        raise MediaPolicyError("choose exactly one mode: --trash or --permanent")
    if args.permanent and not args.ack_no_recovery:
        raise MediaPolicyError("--permanent also requires --ack-no-recovery")
    return "permanent" if args.permanent else "trash"


def approval_token(plan: dict, selected: Iterable[dict], mode: str, context: dict) -> str:
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
        plan_path = core.existing_plan_path(Path(args.plan))
        plan = read_plan(plan_path)
        selected = select_items(plan, args.ids)
        mode = mutation_mode(args)
        context = core.mutation_context(mode)
        for item in selected:
            failure = approval_metadata_failure(item, plan)
            if failure:
                raise MediaPolicyError(f"selected image changed or is unverifiable: {failure}")
    except (OSError, ValueError, MediaPolicyError) as error:
        print(f"Policy error: {error}", file=sys.stderr)
        return 2
    total = sum(item["allocated_bytes"] for item in selected)
    print(f"Plan: {core.display_path(plan_path)}")
    print(f"Explicitly rejected selection: {len(selected)} images ({human_size(total)})")
    print(f"Mode: {mode}")
    if mode == "trash":
        print(f"Trash backend: {context['backend']}")
        print(f"Trash root: {core.display_path(context['files_root'])}")
    print("Selected IDs and paths:")
    for item in selected:
        print(f"- {item['id']} {core.display_path(item['path'])}")
    print(f"Approval token: {approval_token(plan, selected, mode, context)}")
    return 0


def active_thread_failure(item: dict) -> Optional[str]:
    for home in core.codex_homes():
        lock = home / "thread-writer-locks" / f"{item['thread_id']}.lock"
        try:
            info = lock.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            return f"could not inspect thread lock: {error}"
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            return "could not verify thread lock type"
        state = core.open_file_state(lock)
        if state is True:
            return "thread is active in Codex"
        if state is None:
            return "could not verify that the thread is inactive"
    return None


def approval_metadata_failure(item: dict, plan: dict) -> Optional[str]:
    path = Path(item["path"])
    root = Path(plan["root"])
    try:
        info = path.lstat()
    except OSError as error:
        return f"cannot inspect image: {error}"
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return "image is not a regular non-symlink file"
    if not core.is_within(path, root, allow_equal=False):
        return "image escaped the approved media root"
    boundary_failure = core.filesystem_boundary_failure(path, root)
    if boundary_failure:
        return boundary_failure
    if core.identity(path) != item["identity"]:
        return "filesystem identity changed after scan"
    if info.st_size != item["size_bytes"]:
        return "image size changed after scan"
    if info.st_mtime_ns != item["mtime_ns"]:
        return "image timestamp changed after scan"
    if allocated_bytes(info) != item["allocated_bytes"]:
        return "allocated size changed after scan"
    return None


def revalidate(item: dict, plan: dict, plan_path: Path) -> Optional[str]:
    root = Path(plan["root"])
    path = Path(item["path"])
    try:
        info = path.lstat()
    except FileNotFoundError:
        return "image disappeared"
    except OSError as error:
        return f"cannot inspect image: {error}"
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return "image is no longer a regular file"
    if not core.is_within(path, root, allow_equal=False):
        return "image escaped the approved media root"
    boundary_failure = core.filesystem_boundary_failure(path, root)
    if boundary_failure:
        return boundary_failure
    try:
        relative = path.relative_to(root)
    except ValueError:
        return "image relative path changed"
    if len(relative.parts) != 2 or relative.as_posix() != item["relative_path"]:
        return "image thread boundary changed"
    if core.same_path(path, plan_path) or core.is_within(plan_path, path):
        return "image contains the sealed plan"
    metadata_failure = approval_metadata_failure(item, plan)
    if metadata_failure:
        return metadata_failure
    try:
        digest, stable_info = sha256_file(path)
    except (OSError, ValueError, MediaPolicyError) as error:
        return str(error)
    if (
        digest != item["sha256"]
        or stable_info.st_size != item["size_bytes"]
        or stable_info.st_mtime_ns != item["mtime_ns"]
        or allocated_bytes(stable_info) != item["allocated_bytes"]
    ):
        return "image content changed after scan"
    thread_failure = active_thread_failure(item)
    if thread_failure:
        return thread_failure
    open_state = core.open_file_state(path)
    if open_state is True:
        return "image is open in a running process"
    if open_state is None:
        return "could not verify that the image has no open handles"
    return None


def unique_trash_name(item: dict) -> str:
    suffix = Path(item["filename"]).suffix
    return f"codex-rejected-{item['thread_id']}-{item['id']}-{uuid.uuid4().hex[:8]}{suffix}"


def move_to_trash(
    path: Path,
    item: dict,
    files_root: Path,
    info_root: Optional[Path],
    destination: Path,
    info_path: Optional[Path],
) -> tuple[Path, Optional[Path]]:
    failure = core._directory_state(files_root, private=True, allow_missing=False)
    if failure:
        raise MediaPolicyError(failure)
    if path.lstat().st_dev != files_root.lstat().st_dev:
        raise MediaPolicyError("Trash is on another filesystem; copy-and-delete fallback is forbidden")
    if destination.parent != files_root:
        raise MediaPolicyError("Trash destination escaped its prepared root")
    destination_state = core.path_presence(destination)
    if destination_state is None:
        raise MediaPolicyError(f"cannot verify Trash destination state: {destination}")
    if destination_state:
        raise MediaPolicyError("Trash destination already exists")
    if info_root is not None:
        failure = core._directory_state(info_root, private=True, allow_missing=False)
        if failure:
            raise MediaPolicyError(failure)
        if info_path is None or info_path.parent != info_root:
            raise MediaPolicyError("Trash metadata path escaped its prepared root")
        value = "[Trash Info]\nPath=" + urllib.parse.quote(str(path)) + "\nDeletionDate=" + dt.datetime.now().strftime(
            "%Y-%m-%dT%H:%M:%S"
        ) + "\n"
        write_new_text(info_path, value)
    try:
        os.replace(path, destination)
        digest, moved_info = sha256_file(destination)
        if digest != item["sha256"] or moved_info.st_ino != item["identity"]["inode"]:
            raise MediaPolicyError("image changed during atomic Trash move")
    except BaseException as error:
        restore_error: Optional[OSError] = None
        destination_state = core.path_presence(destination)
        original_state = core.path_presence(path)
        if destination_state is None or original_state is None:
            raise MediaPolicyError(
                f"{error}; recovery state is unverifiable; inspect destination={destination} "
                f"and original={path}"
            ) from error
        if destination_state and not original_state:
            try:
                core.restore_relocated(destination, path)
            except OSError as caught:
                restore_error = caught
        if info_path is not None:
            try:
                info_path.unlink()
            except OSError:
                pass
        if restore_error is not None:
            raise MediaPolicyError(
                f"{error}; automatic restore failed; image may remain at {destination}: {restore_error}"
            ) from restore_error
        raise
    return destination, info_path


def trash_destination_paths(
    item: dict, files_root: Path, info_root: Optional[Path]
) -> tuple[Path, Optional[Path]]:
    name = unique_trash_name(item)
    destination = files_root / name
    info_path = info_root / f"{name}.trashinfo" if info_root is not None else None
    return destination, info_path


def delete_permanently(path: Path, item: dict) -> None:
    tombstone = path.with_name(f".codex-rejected-deleting-{uuid.uuid4().hex}{path.suffix}")
    tombstone_state = core.path_presence(tombstone)
    if tombstone_state is None:
        raise MediaPolicyError(f"cannot verify atomic tombstone state: {tombstone}")
    if tombstone_state:
        raise MediaPolicyError("atomic tombstone collision")
    os.replace(path, tombstone)
    try:
        digest, moved_info = sha256_file(tombstone)
        if digest != item["sha256"] or moved_info.st_ino != item["identity"]["inode"]:
            raise MediaPolicyError("image changed during atomic quarantine")
        tombstone.unlink()
    except BaseException as error:
        tombstone_state = core.path_presence(tombstone)
        original_state = core.path_presence(path)
        if tombstone_state is None or original_state is None:
            raise MediaPolicyError(
                f"{error}; recovery state is unverifiable; inspect tombstone={tombstone} "
                f"and original={path}"
            ) from error
        if tombstone_state and not original_state:
            try:
                core.restore_relocated(tombstone, path)
            except OSError as restore_error:
                raise MediaPolicyError(
                    f"{error}; permanent deletion failed and recovery image remains at "
                    f"{tombstone}: {restore_error}"
                ) from restore_error
        raise


def open_journal(
    plan_path: Path,
    plan: dict,
    token: str,
    mode: str,
    selected: list[dict],
    context: dict,
):
    path = plan_path.with_name(
        f"{plan_path.stem}.media-result-{utc_now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}.jsonl"
    )
    path = core.new_output_path(path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    handle = os.fdopen(descriptor, "w", encoding="utf-8")
    header = {
        "type": "header",
        "schema_version": SCHEMA_VERSION,
        "plan": str(plan_path),
        "plan_seal": plan["seal"],
        "approval_token": token,
        "mode": mode,
        "mutation_context": context,
        "started_at": iso_time(utc_now()),
        "selected_ids": [item["id"] for item in selected],
    }
    append_journal(handle, header)
    return path, handle


def append_journal(handle, event: dict) -> None:
    handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
    handle.flush()
    os.fsync(handle.fileno())


def command_apply(args: argparse.Namespace) -> int:
    if not core.apply_platform_supported():
        print("Policy error: apply is unsupported on this platform; audit only", file=sys.stderr)
        return 2
    try:
        plan_path = core.existing_plan_path(Path(args.plan))
        plan = read_plan(plan_path)
        selected = select_items(plan, args.ids)
        mode = mutation_mode(args)
        context = core.mutation_context(mode)
        expected = approval_token(plan, selected, mode, context)
        if args.confirm != expected:
            raise MediaPolicyError("approval token mismatch")
        trash_root: Optional[Path] = None
        trash_info_root: Optional[Path] = None
        if mode == "trash":
            trash_root, trash_info_root = core.prepare_trash_context(context)
        journal_path, journal = open_journal(
            plan_path, plan, expected, mode, selected, context
        )
    except (OSError, ValueError, MediaPolicyError, core.PreconditionFailed) as error:
        print(f"Policy error: {error}", file=sys.stderr)
        return 2
    changed = skipped = errors = 0
    reclaimed = 0
    try:
        for item in selected:
            expected_destination: Optional[Path] = None
            expected_info_path: Optional[Path] = None
            attempt = {
                "type": "attempt",
                "id": item["id"],
                "path": item["path"],
            }
            if mode == "trash":
                assert trash_root is not None
                expected_destination, expected_info_path = trash_destination_paths(
                    item, trash_root, trash_info_root
                )
                attempt["expected_trash_path"] = str(expected_destination)
                if expected_info_path is not None:
                    attempt["expected_trash_info_path"] = str(expected_info_path)
            append_journal(journal, attempt)
            failure = revalidate(item, plan, plan_path)
            if failure:
                skipped += 1
                append_journal(journal, {"type": "item", "id": item["id"], "status": "skipped", "reason": failure})
                continue
            path = Path(item["path"])
            try:
                if mode == "trash":
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
                    event = {"type": "item", "id": item["id"], "status": "trashed", "trash_path": str(destination)}
                    if info_path is not None:
                        event["trash_info_path"] = str(info_path)
                else:
                    delete_permanently(path, item)
                    reclaimed += item["allocated_bytes"]
                    event = {"type": "item", "id": item["id"], "status": "deleted"}
                changed += 1
                append_journal(journal, event)
            except (OSError, MediaPolicyError) as error:
                errors += 1
                append_journal(journal, {"type": "item", "id": item["id"], "status": "error", "reason": str(error)})
        append_journal(
            journal,
            {
                "type": "summary",
                "completed_at": iso_time(utc_now()),
                "changed": changed,
                "skipped": skipped,
                "errors": errors,
                "estimated_reclaimed_bytes": reclaimed,
            },
        )
    finally:
        journal.close()
    print(f"Completed: changed={changed}, skipped={skipped}, errors={errors}")
    if mode == "trash":
        print("Selected images moved to OS Trash. Empty Trash separately to reclaim disk space.")
    else:
        print(f"Estimated space reclaimed: {human_size(reclaimed)}")
    print(f"Execution journal: {core.display_path(journal_path)}")
    return 1 if skipped or errors else 0


def add_mode_arguments(parser: argparse.ArgumentParser) -> None:
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--trash", action="store_true", help="move to OS Trash (recommended)")
    modes.add_argument("--permanent", action="store_true", help="permanently delete after atomic revalidation")
    parser.add_argument(
        "--ack-no-recovery",
        action="store_true",
        help="required with --permanent; confirms there is no recovery promise",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Local-only review of Codex generated images. Similarity never authorizes deletion."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    scan = subparsers.add_parser("scan", help="create a sealed inventory and optional local gallery")
    scan.add_argument("--root", help="exact Codex generated_images root; defaults to active CODEX_HOME")
    scan.add_argument("--plan", required=True, help="new JSON plan path; existing files are never overwritten")
    scan.add_argument("--gallery", help="new local HTML review gallery path")
    scan.add_argument(
        "--thread",
        action="append",
        help="inventory one exact direct thread ID; repeat for a bounded batch",
    )
    scan.add_argument(
        "--threshold",
        type=int,
        default=8,
        help="dHash Hamming threshold for review groups (0-16, never a deletion rule)",
    )
    scan.add_argument(
        "--workers",
        type=int,
        default=0,
        help=f"parallel image workers (0=auto, hard cap {MAX_VISUAL_WORKERS})",
    )
    scan.add_argument(
        "--visual-backend",
        choices=("auto", "ffmpeg", "stdlib", "none"),
        default="auto",
        help="review-only visual hash backend; auto prefers an installed ffmpeg",
    )
    scan.set_defaults(function=command_scan)

    token = subparsers.add_parser("token", help="derive approval for explicit rejected image IDs")
    token.add_argument("--plan", required=True)
    token.add_argument("--ids", required=True, help="comma-separated IDs explicitly rejected by the user")
    add_mode_arguments(token)
    token.set_defaults(function=command_token)

    apply = subparsers.add_parser("apply", help="apply one exact approved rejected-image selection")
    apply.add_argument("--plan", required=True)
    apply.add_argument("--ids", required=True, help="same explicit IDs used to derive the token")
    apply.add_argument("--confirm", required=True, help="approval token from the token command")
    add_mode_arguments(apply)
    apply.set_defaults(function=command_apply)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.function(args)


if __name__ == "__main__":
    raise SystemExit(main())

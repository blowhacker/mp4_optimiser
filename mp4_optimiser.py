#!/usr/bin/env python3
# https://gist.github.com/blowhacker/21f5fe02e791871a78a784357452e9fe

import argparse
import csv
import fcntl
import json
import logging
import math
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".wmv"}

EFFICIENT_VIDEO_CODECS = {"hevc", "h265", "av1", "vp9"}

MIN_SOURCE_SIZE = 50 * 1024 * 1024  # 50 MiB

CRF = 24
PRESET = "medium"

# Converted file must save at least this much space.
MIN_SAVING = 0.15

# Don't begin an encode unless this much free disk space is available
# relative to the source size.
FREE_SPACE_FACTOR = 1.10

# Approximate source bitrate thresholds below which we don't bother.
BITRATE_THRESHOLDS = [
    (2000, 10_000_000),  # 4K-ish
    (1000, 4_000_000),  # 1080p
    (700, 2_500_000),  # 720p
    (400, 1_500_000),  # SD
    (0, 1_000_000),
]

# Audio codecs that are sensible to copy directly into MP4.
MP4_AUDIO_CODECS = {"aac", "mp3", "ac3", "eac3", "alac"}

# Native MP4 subtitle types we are happy to copy unchanged.
MP4_SUBTITLE_CODECS = {"mov_text", "tx3g"}

# Bump this whenever a code change affects whether a previous rejection is
# safe to reuse.
STATE_POLICY_VERSION = 2


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

log = logging.getLogger("mp4_optimiser")


def human_size(num_bytes):
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    value = float(num_bytes)

    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.2f} {unit}"
        value /= 1024

    return f"{num_bytes} B"


def human_duration(seconds):
    try:
        seconds = int(float(seconds))
    except (TypeError, ValueError):
        return "unknown"

    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def run_capture(cmd):
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=True,
    )


def require_binary(name):
    if shutil.which(name) is None:
        sys.exit(f"Required program not found: {name}")


def append_summary_log(log_path, source, final_path, old_size, new_size):
    """Append one compact record for an installed conversion."""
    needs_header = not log_path.exists() or log_path.stat().st_size == 0
    log_path.parent.mkdir(parents=True, exist_ok=True)

    with log_path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        if needs_header:
            writer.writerow(
                [
                    "time",
                    "source",
                    "output",
                    "original_bytes",
                    "final_bytes",
                    "saved_bytes",
                    "saving_percent",
                ]
            )
        writer.writerow(
            [
                datetime.now().astimezone().isoformat(timespec="seconds"),
                source,
                final_path,
                old_size,
                new_size,
                old_size - new_size,
                f"{(1 - new_size / old_size) * 100:.1f}",
            ]
        )


# ---------------------------------------------------------------------------
# Persistent state
# ---------------------------------------------------------------------------


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def source_fingerprint(path):
    """A cheap identity for invalidating cached decisions after a file changes."""
    stat = path.stat()
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
            "ctime_ns": stat.st_ctime_ns, "device": stat.st_dev, "inode": stat.st_ino}


def policy_signature(encoder_identity=None):
    """Settings that make a prior low-saving result comparable to this run."""
    return json.dumps(
        {
            "version": STATE_POLICY_VERSION,
            "codec": "libx265",
            "crf": CRF,
            "preset": PRESET,
            "min_saving": MIN_SAVING,
            "encoder_identity": encoder_identity,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


class StateStore:
    """SQLite-backed run history and cache for low-saving encodes."""

    def __init__(self, path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self._initialise()
        self.signature = policy_signature()

    def _initialise(self):
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                parameters_json TEXT NOT NULL,
                source_root TEXT NOT NULL,
                destination_root TEXT NOT NULL,
                videos_discovered INTEGER,
                converted INTEGER,
                skipped INTEGER,
                failed INTEGER,
                bytes_saved INTEGER,
                runtime_seconds REAL
            );

            CREATE TABLE IF NOT EXISTS attempts (
                id INTEGER PRIMARY KEY,
                run_id INTEGER NOT NULL REFERENCES runs(id),
                attempted_at TEXT NOT NULL,
                source_path TEXT NOT NULL,
                source_size INTEGER,
                source_mtime_ns INTEGER,
                policy_signature TEXT NOT NULL,
                outcome TEXT NOT NULL,
                detail TEXT,
                output_path TEXT,
                output_size INTEGER,
                saving_percent REAL
            );

            CREATE INDEX IF NOT EXISTS attempts_source_index
            ON attempts(source_path, attempted_at);

            CREATE TABLE IF NOT EXISTS rejection_cache (
                source_path TEXT PRIMARY KEY,
                source_size INTEGER NOT NULL,
                source_mtime_ns INTEGER NOT NULL,
                policy_signature TEXT NOT NULL,
                rejected_at TEXT NOT NULL,
                output_size INTEGER NOT NULL,
                saving_percent REAL NOT NULL
            );
            """
        )
        self.connection.commit()

        # Additive migration also works with databases from the first release.
        for table, column, declaration in (
            ("runs", "status", "TEXT NOT NULL DEFAULT 'unknown'"),
            ("rejection_cache", "fingerprint_json", "TEXT"),
        ):
            columns = {row[1] for row in self.connection.execute(f"PRAGMA table_info({table})")}
            if column not in columns:
                self.connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
        self.connection.commit()

    def start_run(self, source_root, destination_root, parameters):
        cursor = self.connection.execute(
            """
            INSERT INTO runs (started_at, parameters_json, source_root, destination_root, status)
            VALUES (?, ?, ?, ?, 'running')
            """,
            (utc_now(), json.dumps(parameters, sort_keys=True), str(source_root), str(destination_root)),
        )
        self.connection.commit()
        return cursor.lastrowid

    def finish_run(self, run_id, started_monotonic, videos_discovered, converted, skipped, failed, bytes_saved, status="completed"):
        self.connection.execute(
            """
            UPDATE runs
            SET finished_at = ?, videos_discovered = ?, converted = ?, skipped = ?,
                failed = ?, bytes_saved = ?, runtime_seconds = ?, status = ?
            WHERE id = ?
            """,
            (
                utc_now(), videos_discovered, converted, skipped, failed, bytes_saved,
                time.monotonic() - started_monotonic, status, run_id,
            ),
        )
        self.connection.commit()

    def cached_rejection(self, source, fingerprint, signature):
        return self.connection.execute(
            """
            SELECT rejected_at, saving_percent FROM rejection_cache
            WHERE source_path = ? AND source_size = ? AND source_mtime_ns = ?
              AND policy_signature = ?
              AND fingerprint_json = ?
            """,
            (str(source), fingerprint["size"], fingerprint["mtime_ns"], signature,
             json.dumps(fingerprint, sort_keys=True)),
        ).fetchone()

    def record_attempt(self, run_id, source, fingerprint, signature, outcome, detail=None,
                       output_path=None, output_size=None, saving_percent=None):
        self.connection.execute(
            """
            INSERT INTO attempts (
                run_id, attempted_at, source_path, source_size, source_mtime_ns,
                policy_signature, outcome, detail, output_path, output_size, saving_percent
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id, utc_now(), str(source), fingerprint["size"], fingerprint["mtime_ns"],
                signature, outcome, detail, str(output_path) if output_path else None,
                output_size, saving_percent,
            ),
        )
        self.connection.commit()

    def cache_rejection(self, source, fingerprint, signature, output_size, saving_percent):
        self.connection.execute(
            """
            INSERT INTO rejection_cache (
                source_path, source_size, source_mtime_ns, policy_signature,
                rejected_at, output_size, saving_percent, fingerprint_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_path) DO UPDATE SET
                source_size = excluded.source_size,
                source_mtime_ns = excluded.source_mtime_ns,
                policy_signature = excluded.policy_signature,
                rejected_at = excluded.rejected_at,
                output_size = excluded.output_size,
                saving_percent = excluded.saving_percent,
                fingerprint_json = excluded.fingerprint_json
            """,
            (
                str(source), fingerprint["size"], fingerprint["mtime_ns"], signature,
                utc_now(), output_size, saving_percent, json.dumps(fingerprint, sort_keys=True),
            ),
        )
        self.connection.commit()

    def close(self):
        self.connection.close()

    def clear_rejection(self, source):
        self.connection.execute("DELETE FROM rejection_cache WHERE source_path = ?", (str(source),))
        self.connection.commit()


# ---------------------------------------------------------------------------
# Locking
# ---------------------------------------------------------------------------


def acquire_lock(source_root):
    lock_path = source_root / ".mp4_optimiser.lock"

    # Do not truncate the PID recorded by an already-running process before
    # we know that this process owns the lock.
    handle = open(lock_path, "a+")

    try:
        fcntl.flock(
            handle.fileno(),
            fcntl.LOCK_EX | fcntl.LOCK_NB,
        )
    except BlockingIOError:
        handle.close()
        log.error("Another optimiser process is already running.")
        sys.exit(2)

    handle.seek(0)
    handle.truncate()
    handle.write(f"{os.getpid()}\n")
    handle.flush()

    return handle


# ---------------------------------------------------------------------------
# ffprobe
# ---------------------------------------------------------------------------


def probe(path):
    result = run_capture(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            "-show_chapters",
            str(path),
        ]
    )

    data = json.loads(result.stdout)

    streams = data.get("streams", [])
    fmt = data.get("format", {})
    chapters = data.get("chapters", [])

    video_streams = [s for s in streams if s.get("codec_type") == "video"]

    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]

    subtitle_streams = [s for s in streams if s.get("codec_type") == "subtitle"]

    attachment_streams = [s for s in streams if s.get("codec_type") == "attachment"]

    data_streams = [s for s in streams if s.get("codec_type") == "data"]

    if not video_streams:
        return None

    primary_video = video_streams[0]

    size = int(fmt.get("size") or path.stat().st_size)

    duration = float(fmt.get("duration") or 0)

    video_bitrate = int(primary_video.get("bit_rate") or 0)

    total_bitrate = int(fmt.get("bit_rate") or 0)

    if not video_bitrate and total_bitrate:
        video_bitrate = total_bitrate

    if not video_bitrate and duration > 0:
        video_bitrate = int(size * 8 / duration)

    return {
        "streams": streams,
        "video_streams": video_streams,
        "audio_streams": audio_streams,
        "subtitle_streams": subtitle_streams,
        "attachment_streams": attachment_streams,
        "data_streams": data_streams,
        "chapters": chapters,
        "codec": primary_video.get(
            "codec_name",
            "unknown",
        ).lower(),
        "width": int(primary_video.get("width") or 0),
        "height": int(primary_video.get("height") or 0),
        "size": size,
        "duration": duration,
        "video_bitrate": video_bitrate,
        "total_bitrate": total_bitrate,
    }


# ---------------------------------------------------------------------------
# Decision logic
# ---------------------------------------------------------------------------


def bitrate_threshold(height):
    for minimum_height, bitrate in BITRATE_THRESHOLDS:
        if height >= minimum_height:
            return bitrate

    return 1_000_000


def should_encode(info):
    if info["size"] < MIN_SOURCE_SIZE:
        return (
            False,
            f"under {human_size(MIN_SOURCE_SIZE)} ({human_size(info['size'])})",
        )

    codec = info["codec"]

    if any(s.get("disposition", {}).get("attached_pic") for s in info["video_streams"]):
        return False, "attached artwork requires explicit video stream selection"
    if info["data_streams"]:
        return False, "data streams require an explicit compatible muxing policy"
    for stream in info["video_streams"][:1]:
        if stream.get("color_transfer") in {"smpte2084", "arib-std-b67"}:
            return False, "HDR requires an explicit metadata-preserving encode policy"
        if stream.get("field_order") not in {None, "unknown", "progressive"}:
            return False, "interlaced video requires an explicit encode policy"

    if codec in EFFICIENT_VIDEO_CODECS:
        return (False, f"already efficient codec: {codec}")

    bitrate = info["video_bitrate"]

    if not bitrate:
        return (False, "unable to determine bitrate")

    threshold = bitrate_threshold(info["height"])

    if bitrate < threshold:
        return (False, f"bitrate already modest: {bitrate / 1_000_000:.2f} Mbps")

    return (
        True,
        f"{codec}, {info['width']}x{info['height']}, {bitrate / 1_000_000:.2f} Mbps",
    )


def choose_container(info):
    """
    Prefer MP4 when we can preserve everything unchanged.

    Fall back to MKV whenever MP4 would require us to throw away
    or substantially transform useful ancillary streams.
    """

    # Attachments commonly contain fonts used by ASS subtitles.
    if info["attachment_streams"]:
        return ".mkv", "contains attachments"

    # Arbitrary data streams aren't something we want to silently lose.
    if info["data_streams"]:
        return ".mkv", "contains data streams"

    # If subtitles exist, only native MP4 subtitle codecs qualify for MP4.
    for stream in info["subtitle_streams"]:
        codec = (stream.get("codec_name") or "").lower()

        if codec not in MP4_SUBTITLE_CODECS:
            return (".mkv", f"subtitle codec {codec or 'unknown'}")

    # Keep unusual audio unchanged by using Matroska.
    for stream in info["audio_streams"]:
        codec = (stream.get("codec_name") or "").lower()

        if codec not in MP4_AUDIO_CODECS:
            return (".mkv", f"audio codec {codec or 'unknown'}")

    return ".mp4", "all streams MP4-compatible"


# ---------------------------------------------------------------------------
# Paths / scanning
# ---------------------------------------------------------------------------


def is_relative_to(path, other):
    try:
        path.relative_to(other)
        return True
    except ValueError:
        return False


def discover_videos(source_root, destination_root):
    videos = []

    source_root = source_root.resolve()
    destination_root = destination_root.resolve()

    # When replacing originals the destination is the source root.  In that
    # case, every child directory is necessarily below the destination, so
    # pruning paths below it would prevent os.walk from seeing any media.
    exclude_destination = (
        destination_root != source_root
        and is_relative_to(destination_root, source_root)
    )

    for root, dirs, files in os.walk(
        source_root,
        topdown=True,
        followlinks=False,
    ):
        root_path = Path(root)

        kept_dirs = []

        for dirname in dirs:
            candidate = (root_path / dirname).resolve()

            if dirname.endswith(".trickplay"):
                continue

            if exclude_destination and (
                candidate == destination_root
                or is_relative_to(candidate, destination_root)
            ):
                continue

            kept_dirs.append(dirname)

        dirs[:] = kept_dirs

        for filename in files:
            path = root_path / filename

            if ".__optimising__" in filename:
                continue

            if path.suffix.lower() not in VIDEO_EXTENSIONS:
                continue

            if path.is_symlink():
                log.warning(
                    "Skipping symlink: %s",
                    path,
                )
                continue

            videos.append(path)

    return sorted(videos)


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------


def make_temp_path(final_path):
    return final_path.with_name(f".{final_path.stem}.__optimising__{final_path.suffix}")


def build_ffmpeg_command(source, temp, container):
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-y",
        "-i",
        str(source),
        # Preserve all streams.
        "-map",
        "0",
        # Preserve global metadata and chapters.
        "-map_metadata",
        "0",
        "-map_chapters",
        "0",
        # Copy everything by default.
        "-c",
        "copy",
        # Re-encode only the primary video stream.
        "-c:v:0",
        "libx265",
        "-preset",
        PRESET,
        "-crf",
        str(CRF),
        "-max_muxing_queue_size",
        "4096",
    ]

    if container == ".mp4":
        cmd += [
            "-tag:v:0",
            "hvc1",
            "-movflags",
            "+faststart",
        ]

    cmd.append(str(temp))

    return cmd


def free_space_ok(path, source_size):
    usage = shutil.disk_usage(path.parent)

    required = int(source_size * FREE_SPACE_FACTOR)

    return usage.free >= required, usage.free, required


# ---------------------------------------------------------------------------
# Output validation
# ---------------------------------------------------------------------------


def validate_output(source_info, output_info):
    errors = []

    if output_info is None:
        return ["output contains no video stream"]

    if output_info["codec"] not in {"hevc", "h265"}:
        errors.append(f"expected HEVC output, got {output_info['codec']}")

    if (
        output_info["width"] != source_info["width"]
        or output_info["height"] != source_info["height"]
    ):
        errors.append(
            "resolution changed: "
            f"{source_info['width']}x"
            f"{source_info['height']} -> "
            f"{output_info['width']}x"
            f"{output_info['height']}"
        )

    source_duration = source_info["duration"]
    output_duration = output_info["duration"]

    if not math.isfinite(output_duration) or output_duration <= 0:
        errors.append("output duration is missing or invalid")
    if not math.isfinite(source_duration) or source_duration <= 0:
        errors.append("source duration is missing or invalid")

    if source_duration > 0 and output_duration > 0:
        tolerance = max(
            2.0,
            source_duration * 0.002,
        )

        difference = abs(source_duration - output_duration)

        if difference > tolerance:
            errors.append(f"duration differs by {difference:.2f}s")

    comparisons = [
        ("video streams", len(source_info["video_streams"]), len(output_info["video_streams"])),
        (
            "audio streams",
            len(source_info["audio_streams"]),
            len(output_info["audio_streams"]),
        ),
        (
            "subtitle streams",
            len(source_info["subtitle_streams"]),
            len(output_info["subtitle_streams"]),
        ),
        (
            "attachment streams",
            len(source_info["attachment_streams"]),
            len(output_info["attachment_streams"]),
        ),
        (
            "data streams",
            len(source_info["data_streams"]),
            len(output_info["data_streams"]),
        ),
        (
            "chapters",
            len(source_info["chapters"]),
            len(output_info["chapters"]),
        ),
    ]

    for name, before, after in comparisons:
        if before != after:
            errors.append(f"{name}: {before} -> {after}")

    return errors


# ---------------------------------------------------------------------------
# Trickplay
# ---------------------------------------------------------------------------


def delete_trickplay(source):
    trickplay = source.with_suffix(".trickplay")

    if not trickplay.exists():
        return

    if trickplay.is_symlink() or not trickplay.is_dir():
        log.warning(
            "Expected trickplay directory but found something else: %s", trickplay
        )
        return

    log.info("Removing trickplay: %s", trickplay)

    shutil.rmtree(trickplay)


# ---------------------------------------------------------------------------
# File replacement
# ---------------------------------------------------------------------------


def install_output(
    source,
    final_path,
    temp,
    replace_originals,
):
    warnings = []
    final_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    if replace_originals:
        if final_path != source and final_path.exists():
            raise RuntimeError(f"replacement target already exists: {final_path}")

        if final_path == source:
            # Same pathname: atomic replacement.
            os.replace(temp, source)

        else:
            # Different extension.
            #
            # Install the validated replacement FIRST.
            # Only then remove the source.
            # Creating a hard link is atomic and refuses even a dangling symlink
            # at the destination. The temporary file is on the same filesystem.
            os.link(temp, final_path)
            try:
                temp.unlink()
            except OSError as error:
                warnings.append(f"temporary link cleanup failed: {error}")

            try:
                source.unlink()
            except OSError as error:
                warnings.append(f"original retained at {source}: {error}")

    else:
        if final_path.exists():
            raise RuntimeError(f"destination already exists: {final_path}")

        os.link(temp, final_path)
        try:
            temp.unlink()
        except OSError as error:
            warnings.append(f"temporary link cleanup failed: {error}")

    return warnings


# ---------------------------------------------------------------------------
# Per-file processing
# ---------------------------------------------------------------------------


def process_file(*args, **kwargs):
    temporary_paths = []
    try:
        return _process_file(*args, **kwargs, temporary_paths=temporary_paths)
    finally:
        for path in temporary_paths:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                log.warning("Could not remove temporary output: %s", path)


def _process_file(
    source,
    source_root,
    destination_root,
    replace_originals,
    dry_run,
    keep_trickplay,
    summary_log,
    state_store=None,
    run_id=None,
    force=False,
    temporary_paths=None,
):
    fingerprint = source_fingerprint(source)
    signature = state_store.signature if state_store is not None else policy_signature()
    info = probe(source)

    def record(outcome, detail=None, output_path=None, output_size=None, saving_percent=None):
        if state_store is not None:
            state_store.record_attempt(
                run_id, source, fingerprint, signature, outcome, detail,
                output_path, output_size, saving_percent,
            )

    if info is None:
        log.warning(
            "SKIP no video stream: %s",
            source,
        )
        record("skipped_no_video", "no video stream")
        return "skipped", 0

    log.info(
        "%s | %s | %dx%d | %.2f Mbps | %s | %s",
        source,
        info["codec"],
        info["width"],
        info["height"],
        info["video_bitrate"] / 1_000_000,
        human_size(info["size"]),
        human_duration(info["duration"]),
    )

    encode_it, reason = should_encode(info)

    if not encode_it:
        log.info(
            "SKIP: %s",
            reason,
        )
        record("skipped_ineligible", reason)
        return "skipped", 0

    if state_store is not None and not force:
        cached = state_store.cached_rejection(source, fingerprint, signature)
        if cached is not None:
            detail = (
                f"previously rejected at {cached['rejected_at']}: "
                f"only {cached['saving_percent']:.1f}% smaller"
            )
            log.info("SKIP: %s", detail)
            record("skipped_cached_rejection", detail)
            return "skipped", 0

    container, container_reason = choose_container(info)
    if container == ".mkv" and any(
        s.get("codec_name") in MP4_SUBTITLE_CODECS for s in info["subtitle_streams"]
    ):
        record("skipped_ineligible", "MP4 text subtitles cannot be copied into the required MKV output")
        return "skipped", 0

    log.info("CANDIDATE: %s", reason)
    log.info(
        "Container: %s (%s)",
        container,
        container_reason,
    )

    relative = source.relative_to(source_root)

    if replace_originals:
        final_path = source.with_suffix(container)
    else:
        relative_output = relative.with_suffix(container)

        final_path = destination_root / relative_output

    if final_path != source and os.path.lexists(final_path):
        log.error("COLLISION: target already exists: %s", final_path)
        record("failed_collision", "destination already exists", final_path)
        return "failed", 0

    if dry_run:
        log.info(
            "DRY RUN: would encode to %s",
            final_path,
        )
        record("dry_run", "would encode", final_path)
        return "dry-run", 0

    final_path.parent.mkdir(parents=True, exist_ok=True)
    enough, free, required = free_space_ok(final_path, info["size"])
    if not enough:
        log.warning("SKIP insufficient free space: %s available, %s required",
                    human_size(free), human_size(required))
        record("skipped_insufficient_space", "insufficient free space", final_path)
        return "skipped", 0

    fd, name = tempfile.mkstemp(prefix=f".{final_path.stem}.__optimising__", suffix=container,
                                dir=final_path.parent)
    os.close(fd)
    temp = Path(name)
    temporary_paths.append(temp)

    cmd = build_ffmpeg_command(source, temp, container)

    if state_store is not None:
        # A fresh attempt supersedes a prior rejection, even if this attempt
        # fails or is interrupted (and should therefore be retried next run).
        state_store.clear_rejection(source)

    log.info("Encoding -> %s", temp)

    process = subprocess.run(cmd)

    if process.returncode != 0:
        if temp.exists():
            temp.unlink()

        raise RuntimeError(f"ffmpeg exited with {process.returncode}")

    if not temp.exists():
        raise RuntimeError("ffmpeg reported success but output does not exist")

    new_size = temp.stat().st_size
    if source_fingerprint(source) != fingerprint:
        raise RuntimeError("source changed during encoding; refusing to install or cache output")
    old_size = fingerprint["size"]

    saving = 1 - (new_size / old_size)

    if new_size > old_size * (1 - MIN_SAVING):
        log.info(
            "REJECTED: only %.1f%% smaller (%s -> %s)",
            saving * 100,
            human_size(old_size),
            human_size(new_size),
        )

        temp.unlink()

        saving_percent = saving * 100
        if state_store is not None:
            state_store.cache_rejection(
                source, fingerprint, signature, new_size, saving_percent
            )
        record(
            "rejected_low_saving",
            f"only {saving_percent:.1f}% smaller",
            final_path,
            new_size,
            saving_percent,
        )

        return "skipped", 0

    log.info("Validating output...")

    output_info = probe(temp)

    errors = validate_output(info, output_info)
    if errors:
        for error in errors:
            log.error("VALIDATION: %s", error)

        temp.unlink()

        record("failed_validation", "; ".join(errors), final_path, new_size, saving * 100)

        return "failed", 0

    log.info(
        "VALID: %s -> %s, %.1f%% saving",
        human_size(old_size),
        human_size(new_size),
        saving * 100,
    )

    # Preserve modification/access times before installation.
    source_stat = source.stat()

    if source_fingerprint(source) != fingerprint:
        raise RuntimeError("source changed during validation; refusing replacement")
    os.chmod(temp, source_stat.st_mode & 0o777)
    os.utime(temp, ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns))

    warnings = install_output(
        source,
        final_path,
        temp,
        replace_originals,
    )

    saved = old_size - new_size
    try:
        if state_store is not None:
            state_store.clear_rejection(source)
        record("converted", None, final_path, new_size, saving * 100)
    except sqlite3.Error as error:
        warnings.append(f"state recording failed: {error}")
    try:
        append_summary_log(summary_log, source.relative_to(source_root),
                           final_path.relative_to(destination_root), old_size, new_size)
    except OSError as error:
        warnings.append(f"CSV reporting failed: {error}")
    if replace_originals and not keep_trickplay and not warnings:
        try:
            delete_trickplay(source)
        except OSError as error:
            warnings.append(f"trickplay cleanup failed: {error}")
    for warning in warnings:
        log.warning("Output installed at %s; %s", final_path, warning)

    log.info("DONE: %s", final_path)

    return "converted_with_warnings" if warnings else "converted", saved


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Recursively optimise oversized video files "
            "using HEVC while preserving ancillary streams."
        )
    )

    parser.add_argument(
        "source",
        type=Path,
        help="Media library root",
    )

    parser.add_argument(
        "destination",
        nargs="?",
        type=Path,
        help=("Staging/output root. Required unless --replace-originals is used."),
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=("Inspect files and show decisions without encoding anything."),
    )

    parser.add_argument(
        "--replace-originals",
        action="store_true",
        help=("After successful validation, replace the source media file."),
    )

    parser.add_argument(
        "--keep-trickplay",
        action="store_true",
        help=("Do not remove sibling *.trickplay directories after replacing media."),
    )

    parser.add_argument(
        "--summary-log",
        type=Path,
        help=(
            "Append successful conversions to this CSV file "
            "(default: mp4_optimiser_summary.csv beside this script)."
        ),
    )

    parser.add_argument(
        "--state-db",
        type=Path,
        help=(
            "SQLite database for run history and cached low-saving rejections "
            "(default: .mp4_optimiser.sqlite in the source root)."
        ),
    )

    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-encode files previously rejected for insufficient space saving.",
    )

    args = parser.parse_args()

    require_binary("ffmpeg")
    require_binary("ffprobe")

    source_root = args.source.resolve()
    if not source_root.is_dir():
        parser.error("source must be an existing directory")

    if args.replace_originals:
        destination_root = source_root
    else:
        if args.destination is None:
            parser.error("destination is required unless --replace-originals is used")
        destination_root = args.destination.resolve()
        if destination_root == source_root or is_relative_to(source_root, destination_root):
            parser.error("destination must not equal or contain the source root")

    summary_log = (
        args.summary_log.resolve()
        if args.summary_log is not None
        else Path(__file__).resolve().with_name("mp4_optimiser_summary.csv")
    )

    state_db_path = (
        args.state_db.resolve()
        if args.state_db is not None
        else source_root / ".mp4_optimiser.sqlite"
    )

    logging.basicConfig(
        level=logging.INFO,
        format=("%(asctime)s %(levelname)s %(message)s"),
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    encoder_identity = run_capture(["ffmpeg", "-version"]).stdout

    # Keep this handle alive for the entire process.
    lock_handle = acquire_lock(source_root)

    state_store = StateStore(state_db_path)
    state_store.signature = policy_signature(encoder_identity)
    run_started_monotonic = time.monotonic()
    run_id = state_store.start_run(
        source_root,
        destination_root,
        {
            "dry_run": args.dry_run,
            "replace_originals": args.replace_originals,
            "keep_trickplay": args.keep_trickplay,
            "force": args.force,
            "policy": json.loads(state_store.signature),
            "summary_log": str(summary_log),
            "state_db": str(state_db_path),
            "min_source_size": MIN_SOURCE_SIZE,
            "bitrate_thresholds": BITRATE_THRESHOLDS,
        },
    )

    videos = []

    log.info("Source: %s", source_root)
    log.info("Destination: %s", destination_root)
    log.info("HEVC settings: CRF %d, preset %s", CRF, PRESET)
    log.info("Minimum source size: %s", human_size(MIN_SOURCE_SIZE))
    log.info("Minimum saving: %.0f%%", MIN_SAVING * 100)
    log.info("Replace originals: %s", args.replace_originals)
    log.info("Summary log: %s", summary_log)
    log.info("State database: %s", state_db_path)
    log.info("Force cached rejections: %s", args.force)

    converted = 0
    skipped = 0
    failed = 0
    bytes_saved = 0
    run_status = "failed"

    try:
        videos = discover_videos(source_root, destination_root)
        log.info("Videos discovered: %d", len(videos))
        for index, source in enumerate(
            videos,
            start=1,
        ):
            log.info("=" * 72)

            log.info(
                "[%d/%d] %s",
                index,
                len(videos),
                source,
            )

            fingerprint = {"size": None, "mtime_ns": None}
            try:
                fingerprint = source_fingerprint(source)
                state_store.record_attempt(run_id, source, fingerprint, state_store.signature, "started")
                status, saved = process_file(
                    source=source,
                    source_root=source_root,
                    destination_root=destination_root,
                    replace_originals=(args.replace_originals),
                    dry_run=args.dry_run,
                    keep_trickplay=(args.keep_trickplay),
                    summary_log=summary_log,
                    state_store=state_store,
                    run_id=run_id,
                    force=args.force,
                )

                if status in {"converted", "converted_with_warnings"}:
                    converted += 1
                    bytes_saved += saved
                    if status == "converted_with_warnings":
                        failed += 1

                elif status in {"skipped", "dry-run"}:
                    skipped += 1

                else:
                    failed += 1

            except KeyboardInterrupt:
                log.warning("Interrupted.")
                state_store.record_attempt(run_id, source, fingerprint, state_store.signature, "interrupted")
                raise

            except sqlite3.Error:
                # Stop before touching another source when durable state is unavailable.
                raise
            except Exception as error:
                failed += 1
                log.exception("FAILED processing %s", source)
                try:
                    state_store.record_attempt(
                        run_id, source, fingerprint, state_store.signature,
                        "failed_exception", str(error),
                    )
                except OSError:
                    log.warning("Could not record failed attempt; source no longer exists.")
        run_status = "completed_with_errors" if failed else "completed"
    except KeyboardInterrupt:
        run_status = "interrupted"
        raise
    finally:
        try:
            state_store.finish_run(
                run_id, run_started_monotonic, len(videos), converted,
                skipped, failed, bytes_saved, run_status,
            )
        finally:
            state_store.close()
            lock_handle.close()

    log.info("=" * 72)
    log.info("COMPLETE")
    log.info("Converted: %d", converted)
    log.info("Skipped:   %d", skipped)
    log.info("Failed:    %d", failed)
    log.info("Saved:     %s", human_size(bytes_saved))

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()

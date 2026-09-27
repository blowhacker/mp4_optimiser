#!/usr/bin/env python3
# https://gist.github.com/blowhacker/21f5fe02e791871a78a784357452e9fe

import argparse
import fcntl
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

VIDEO_EXTENSIONS = {
    ".mp4",
    ".mkv",
    ".wmv",
}

EFFICIENT_VIDEO_CODECS = {
    "hevc",
    "h265",
    "av1",
    "vp9",
}

MIN_SOURCE_SIZE = 100 * 1024 * 1024  # 100 MiB

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
MP4_AUDIO_CODECS = {
    "aac",
    "mp3",
    "ac3",
    "eac3",
    "alac",
}

# Native MP4 subtitle types we are happy to copy unchanged.
MP4_SUBTITLE_CODECS = {
    "mov_text",
    "tx3g",
}


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
    except TypeError, ValueError:
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


# ---------------------------------------------------------------------------
# Locking
# ---------------------------------------------------------------------------


def acquire_lock(source_root):
    lock_path = source_root / ".mp4_optimiser.lock"

    handle = open(lock_path, "w")

    try:
        fcntl.flock(
            handle.fileno(),
            fcntl.LOCK_EX | fcntl.LOCK_NB,
        )
    except BlockingIOError:
        log.error("Another optimiser process is already running.")
        sys.exit(2)

    handle.write(str(os.getpid()))
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
            f"under 100 MiB ({human_size(info['size'])})",
        )

    codec = info["codec"]

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

            if candidate == destination_root or is_relative_to(
                candidate,
                destination_root,
            ):
                continue

            kept_dirs.append(dirname)

        dirs[:] = kept_dirs

        for filename in files:
            path = root_path / filename

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

    if output_info["codec"] not in {
        "hevc",
        "h265",
    }:
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

    if source_duration and output_duration:
        tolerance = max(
            2.0,
            source_duration * 0.002,
        )

        difference = abs(source_duration - output_duration)

        if difference > tolerance:
            errors.append(f"duration differs by {difference:.2f}s")

    comparisons = [
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

    if not trickplay.is_dir():
        log.warning(
            "Expected trickplay directory but found something else: %s",
            trickplay,
        )
        return

    log.info(
        "Removing trickplay: %s",
        trickplay,
    )

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
            os.replace(
                temp,
                final_path,
            )

            source.unlink()

    else:
        if final_path.exists():
            raise RuntimeError(f"destination already exists: {final_path}")

        os.replace(
            temp,
            final_path,
        )


# ---------------------------------------------------------------------------
# Per-file processing
# ---------------------------------------------------------------------------


def process_file(
    source,
    source_root,
    destination_root,
    replace_originals,
    dry_run,
    keep_trickplay,
):
    info = probe(source)

    if info is None:
        log.warning(
            "SKIP no video stream: %s",
            source,
        )
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
        return "skipped", 0

    container, container_reason = choose_container(info)

    log.info(
        "CANDIDATE: %s",
        reason,
    )

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

    if final_path != source and final_path.exists():
        log.error(
            "COLLISION: target already exists: %s",
            final_path,
        )
        return "failed", 0

    temp = make_temp_path(final_path)

    if temp.exists():
        log.warning(
            "Removing stale temp file: %s",
            temp,
        )
        temp.unlink()

    final_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    enough, free, required = free_space_ok(
        final_path,
        info["size"],
    )

    if not enough:
        log.warning(
            "SKIP insufficient free space: %s available, %s required",
            human_size(free),
            human_size(required),
        )
        return "skipped", 0

    if dry_run:
        log.info(
            "DRY RUN: would encode to %s",
            final_path,
        )
        return "dry-run", 0

    cmd = build_ffmpeg_command(
        source,
        temp,
        container,
    )

    log.info(
        "Encoding -> %s",
        temp,
    )

    process = subprocess.run(cmd)

    if process.returncode != 0:
        if temp.exists():
            temp.unlink()

        raise RuntimeError(f"ffmpeg exited with {process.returncode}")

    if not temp.exists():
        raise RuntimeError("ffmpeg reported success but output does not exist")

    new_size = temp.stat().st_size
    old_size = source.stat().st_size

    saving = 1 - (new_size / old_size)

    if saving < MIN_SAVING:
        log.info(
            "REJECTED: only %.1f%% smaller (%s -> %s)",
            saving * 100,
            human_size(old_size),
            human_size(new_size),
        )

        temp.unlink()

        return "skipped", 0

    log.info("Validating output...")

    output_info = probe(temp)

    errors = validate_output(
        info,
        output_info,
    )

    if errors:
        for error in errors:
            log.error(
                "VALIDATION: %s",
                error,
            )

        temp.unlink()

        return "failed", 0

    log.info(
        "VALID: %s -> %s, %.1f%% saving",
        human_size(old_size),
        human_size(new_size),
        saving * 100,
    )

    # Preserve modification/access times before installation.
    source_stat = source.stat()

    install_output(
        source,
        final_path,
        temp,
        replace_originals,
    )

    os.utime(
        final_path,
        ns=(
            source_stat.st_atime_ns,
            source_stat.st_mtime_ns,
        ),
    )

    if replace_originals and not keep_trickplay:
        delete_trickplay(source)

    saved = old_size - new_size

    log.info(
        "DONE: %s",
        final_path,
    )

    return "converted", saved


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

    args = parser.parse_args()

    require_binary("ffmpeg")
    require_binary("ffprobe")

    source_root = args.source.resolve()

    if args.replace_originals:
        destination_root = source_root
    else:
        if args.destination is None:
            parser.error("destination is required unless --replace-originals is used")
        destination_root = args.destination.resolve()
        destination_root.mkdir(
            parents=True,
            exist_ok=True,
        )

    logging.basicConfig(
        level=logging.INFO,
        format=("%(asctime)s %(levelname)s %(message)s"),
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Keep this handle alive for the entire process.
    lock_handle = acquire_lock(source_root)

    videos = discover_videos(
        source_root,
        destination_root,
    )

    log.info(
        "Source: %s",
        source_root,
    )

    log.info(
        "Destination: %s",
        destination_root,
    )

    log.info(
        "Videos discovered: %d",
        len(videos),
    )

    log.info(
        "HEVC settings: CRF %d, preset %s",
        CRF,
        PRESET,
    )

    log.info(
        "Minimum source size: %s",
        human_size(MIN_SOURCE_SIZE),
    )

    log.info(
        "Minimum saving: %.0f%%",
        MIN_SAVING * 100,
    )

    log.info(
        "Replace originals: %s",
        args.replace_originals,
    )

    converted = 0
    skipped = 0
    failed = 0
    bytes_saved = 0

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

        try:
            status, saved = process_file(
                source=source,
                source_root=source_root,
                destination_root=destination_root,
                replace_originals=(args.replace_originals),
                dry_run=args.dry_run,
                keep_trickplay=(args.keep_trickplay),
            )

            if status == "converted":
                converted += 1
                bytes_saved += saved

            elif status in {
                "skipped",
                "dry-run",
            }:
                skipped += 1

            else:
                failed += 1

        except KeyboardInterrupt:
            log.warning("Interrupted.")
            raise

        except Exception:
            failed += 1

            log.exception(
                "FAILED processing %s",
                source,
            )

    log.info("=" * 72)

    log.info("COMPLETE")

    log.info(
        "Converted: %d",
        converted,
    )

    log.info(
        "Skipped:   %d",
        skipped,
    )

    log.info(
        "Failed:    %d",
        failed,
    )

    log.info(
        "Saved:     %s",
        human_size(bytes_saved),
    )

    # Explicit reference prevents linters deciding this is unused.
    _ = lock_handle

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()

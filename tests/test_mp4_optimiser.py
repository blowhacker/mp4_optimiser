import csv
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import mp4_optimiser as optimiser


def make_info(**overrides):
    """Return the smallest complete probe result used by decision functions."""
    info = {
        "codec": "h264",
        "width": 1920,
        "height": 1080,
        "size": 200 * 1024 * 1024,
        "duration": 120.0,
        "video_bitrate": 8_000_000,
        "total_bitrate": 8_500_000,
        "streams": [],
        "video_streams": [{}],
        "audio_streams": [],
        "subtitle_streams": [],
        "attachment_streams": [],
        "data_streams": [],
        "chapters": [],
    }
    info.update(overrides)
    return info


class HumanReadableTests(unittest.TestCase):
    def test_human_duration_handles_values_and_invalid_input(self):
        self.assertEqual(optimiser.human_duration(3661), "01:01:01")
        self.assertEqual(optimiser.human_duration("not a duration"), "unknown")


class SummaryLogTests(unittest.TestCase):
    def test_append_summary_log_writes_one_header_and_compact_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "summary.csv"
            optimiser.append_summary_log(
                log_path,
                Path("films/original.mp4"),
                Path("/output/films/original.mkv"),
                200,
                150,
            )
            optimiser.append_summary_log(
                log_path,
                Path("films/second.mp4"),
                Path("/output/films/second.mp4"),
                100,
                50,
            )

            with log_path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.reader(handle))

            self.assertEqual(len(rows), 3)
            self.assertEqual(
                rows[0],
                [
                    "time",
                    "source",
                    "output",
                    "original_bytes",
                    "final_bytes",
                    "saved_bytes",
                    "saving_percent",
                ],
            )
            self.assertEqual(
                rows[1][1:],
                [
                    "films/original.mp4",
                    "/output/films/original.mkv",
                    "200",
                    "150",
                    "50",
                    "25.0",
                ],
            )
            self.assertEqual(
                rows[2][1:],
                [
                    "films/second.mp4",
                    "/output/films/second.mp4",
                    "100",
                    "50",
                    "50",
                    "50.0",
                ],
            )


class DecisionTests(unittest.TestCase):
    def test_should_encode_eligible_h264(self):
        should_encode, reason = optimiser.should_encode(make_info())
        self.assertTrue(should_encode)
        self.assertIn("h264", reason)

    def test_should_encode_skips_small_efficient_and_low_bitrate_files(self):
        should_encode, _ = optimiser.should_encode(
            make_info(size=optimiser.MIN_SOURCE_SIZE - 1)
        )
        self.assertFalse(should_encode)

        should_encode, _ = optimiser.should_encode(make_info(codec="hevc"))
        self.assertFalse(should_encode)

        should_encode, _ = optimiser.should_encode(make_info(video_bitrate=3_999_999))
        self.assertFalse(should_encode)

    def test_choose_container_preserves_incompatible_streams_in_mkv(self):
        container, _ = optimiser.choose_container(make_info())
        self.assertEqual(container, ".mp4")

        container, _ = optimiser.choose_container(
            make_info(subtitle_streams=[{"codec_name": "ass"}])
        )
        self.assertEqual(container, ".mkv")

        container, _ = optimiser.choose_container(
            make_info(audio_streams=[{"codec_name": "opus"}])
        )
        self.assertEqual(container, ".mkv")

        container, _ = optimiser.choose_container(
            make_info(attachment_streams=[{"codec_name": "ttf"}])
        )
        self.assertEqual(container, ".mkv")


class ValidationTests(unittest.TestCase):
    def test_validate_output_accepts_matching_hevc_output(self):
        source = make_info(audio_streams=[{}], chapters=[{}])
        output = make_info(codec="hevc", audio_streams=[{}], chapters=[{}])
        self.assertEqual(optimiser.validate_output(source, output), [])

    def test_validate_output_reports_changed_resolution_and_stream_count(self):
        source = make_info(audio_streams=[{}])
        output = make_info(codec="hevc", width=1280, audio_streams=[])
        errors = optimiser.validate_output(source, output)
        self.assertTrue(any("resolution changed" in error for error in errors))
        self.assertIn("audio streams: 1 -> 0", errors)


class DiscoveryTests(unittest.TestCase):
    def test_discover_videos_excludes_destination_and_trickplay(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            destination = source / "optimised"
            source.mkdir()
            destination.mkdir()
            (source / "keep.mp4").touch()
            (destination / "ignore.mkv").touch()
            trickplay = source / "keep.trickplay"
            trickplay.mkdir()
            (trickplay / "ignore.mp4").touch()

            self.assertEqual(
                optimiser.discover_videos(source, destination),
                [(source / "keep.mp4").resolve()],
            )

    def test_discover_videos_does_not_prune_source_when_replacing_originals(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            nested = source / "movie"
            nested.mkdir(parents=True)
            video = nested / "keep.mkv"
            video.touch()

            self.assertEqual(
                optimiser.discover_videos(source, source),
                [video.resolve()],
            )


class LockingTests(unittest.TestCase):
    def test_rejected_contender_preserves_owner_pid(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            source.mkdir()
            lock_path = source / ".mp4_optimiser.lock"
            owner = optimiser.acquire_lock(source)

            try:
                owner_pid = lock_path.read_text()
                contender = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        (
                            "from pathlib import Path; "
                            "import mp4_optimiser as optimiser; "
                            f"optimiser.acquire_lock(Path({str(source)!r}))"
                        ),
                    ],
                    capture_output=True,
                    text=True,
                )

                self.assertEqual(contender.returncode, 2)
                self.assertEqual(lock_path.read_text(), owner_pid)
            finally:
                owner.close()


if __name__ == "__main__":
    unittest.main()

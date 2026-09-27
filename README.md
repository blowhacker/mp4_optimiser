# MP4 Optimiser

Recursively transcodes large, high-bitrate videos to HEVC using `ffmpeg`.
It only installs an output after it passes basic stream, resolution, duration,
and space-saving checks. Compatible ancillary streams are copied, with MKV
used when appropriate. Unsupported combinations are skipped.

## Requirements

- Python 3.9 or newer
- `ffmpeg` and `ffprobe` on `PATH`


Preview what would be encoded without changing media files. Run history and
the source lock are still written:

```bash
python mp4_optimiser.py /media/library /media/optimised --dry-run
```

Write validated conversions to a separate destination directory:

```bash
python mp4_optimiser.py /media/library /media/optimised
```

Successful conversions are appended as compact CSV rows to
`mp4_optimiser_summary.csv` beside the script. Each row includes the timestamp,
source and output filenames, original/final byte sizes, and saving. Set a
different location with `--summary-log /path/to/summary.csv`.

## State and run history

The optimiser creates `mp4_optimiser.sqlite` beside `mp4_optimiser.py`,
independently of the source library and current working directory. It records
each run (parameters, start/finish times, runtime, counts, and bytes saved) and
each file decision. In particular, a file whose completed encode saves less
than the minimum threshold is cached, so an unchanged file is not encoded again
on the next run.

The cache is automatically invalidated when the source file changes or the
encoding policy changes (such as CRF, preset, or minimum saving). Use a custom
database location with `--state-db /path/to/state.sqlite`; use `--force` to
retry cached low-saving rejections for one run.

Existing databases in source directories are not moved automatically. To retain
that history, move the database beside the script while no optimiser is running,
or select its existing location with `--state-db`.

File identity uses size, modification/change times, device and inode, not a
content hash. The policy also includes `ffmpeg -version` output. Existing
databases are migrated automatically; older cache records are retried.
Runs have an explicit status and file processing writes a `started` event before
work. Ctrl-C records interruption; a hard kill leaves a `running` run and any
unfinished `started` event for inspection. Attempts are events, so one file can
have multiple rows per run.

Temporary encodes use unique names and are excluded from discovery. Failed
encodes are cleaned up; leftovers from a hard kill are ignored, not deleted
automatically. Installation refuses an existing destination, and a changed
source is checked before replacement. Permissions and timestamps are preserved.
Reporting/cleanup failures after installation are logged as warnings and produce
a nonzero exit status while still counting the installed conversion.

HDR, interlaced video, attached artwork, data streams, and MP4 text subtitles
that would require MKV are skipped pending dedicated preservation policies.
Validation checks stream counts, dimensions and duration; it is not a complete
frame-by-frame corruption or visual-quality check. Avoid modifying the library
concurrently: filesystem replacement and SQLite recording are not one atomic
transaction, and the source checks cannot eliminate every external-writer race.

Replace source files only after a successful encode and validation. This can
remove sibling `*.trickplay` directories that no longer match the media file:

```bash
python mp4_optimiser.py /media/library --replace-originals
```

Keep those trickplay directories when replacing originals:

```bash
python mp4_optimiser.py /media/library --replace-originals --keep-trickplay
```

## Tests

The tests use the Python standard library and do not encode media:

```bash
python -m unittest discover -s tests -v
```

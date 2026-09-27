# MP4 Optimiser

Recursively transcodes large, high-bitrate videos to HEVC using `ffmpeg`.
It only installs an output after it passes basic stream, resolution, duration,
and space-saving checks. Incompatible ancillary streams cause an MKV output so
they can be copied without silently dropping them.

## Requirements

- Python 3.9 or newer
- `ffmpeg` and `ffprobe` on `PATH`

## Usage

Activate the project virtual environment, if desired:

```bash
source /Users/navinkumar/work/vindaloo/.venv/bin/activate
```

Preview what would be encoded, without making changes:

```bash
python mp4_optimiser.py /media/library /media/optimised --dry-run
```

Write validated conversions to a separate destination directory:

```bash
python mp4_optimiser.py /media/library /media/optimised
```

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

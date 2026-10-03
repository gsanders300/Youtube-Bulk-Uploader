# Agent Guide — youtube-bulk-upload

Compact reference for AI agents working in this repository.

## Developer Commands

- **Environment**: `uv sync --locked --all-groups` (uses Python 3.11 from `.python-version`)
- **CLI Entry**: `uv run --locked youtube-uploader <command>` (commands: `auth`, `doctor`, `scan`, `upload`, `exiftool`, `sync-dates`)
- **Linting**: `uv run --no-sync ruff check src/ tests/` (auto-fix: `--fix`)
- **Formatting**: `uv run --no-sync ruff format src/ tests/`
- **Type Check**: `uv run --no-sync mypy src/` (configured for Python 3.11)
- **Testing**: `uv run --no-sync pytest`
- **Build**: `uv build --clear` (native `uv_build` backend)

## Architecture & Implementation Quirks

- **Entry Point**: `src/youtube_bulk_uploader/main.py` using Click.
- **ExifTool**: Critical for metadata extraction. The bundled `exiftool.exe` at project root is checked first by `src/youtube_bulk_uploader/exiftool.py`.
- **YouTube API Chunk Size**: Must be a multiple of 256 KB. Handled in `src/youtube_bulk_uploader/main.py` and `src/youtube_bulk_uploader/uploader.py`.
- **Privacy Policy**: Hardcoded to `unlisted`. Overridden and logged as warning if set otherwise in code.
- **Metadata Priority**: EXIF recording dates (DateTimeOriginal, etc.) > Filesystem dates. Extracted in `src/youtube_bulk_uploader/metadata.py`.
- **Batch Processing**: `get_batch_exiftool_dates` uses bounded subprocess batches to avoid Windows command-line limits.

## Operational Gotchas

- **Input Folder**: Defaults to `./input` (ignored by git). Automatically created by `scan` if missing.
- **Credentials**: Stored in the platform user config directory; legacy files under `config/` are detected.
- **Upload State**: Successful file versions and unfinished resumable-session URIs are recorded in SQLite under the platform user data directory.
- **Quotas**: The current `videos.insert` reference uses one Video Uploads unit per call and documents 100 calls per day.

## ExifTool Date Parsing — Critical Notes

ExifTool outputs dates in `YYYY:MM:DD HH:MM:SS` format (colons as date separators, e.g. `2026:05:15 11:41:48`). This is standard EXIF format but **`dateutil.parser.parse()` does not understand it** — it misreads the month/day tokens and silently substitutes today's date, making all embedded video dates appear as today.

**The fix lives in `src/youtube_bulk_uploader/exiftool.py:_parse_exiftool_date`**: the raw string is normalized with `re.sub(r"^(\d{4}):(\d{2}):(\d{2})", r"\1-\2-\3", date_str)` before being passed to `dateutil`. If ExifTool dates ever start coming out as today's date instead of the actual recording date, this is the first place to check.

**Naive datetimes** (no timezone offset in the EXIF string) are treated as UTC via `dt.replace(tzinfo=timezone.utc)` — **not** `dt.astimezone(timezone.utc)`. The difference matters: `astimezone` on a naive datetime assumes local time and shifts by the local UTC offset; `replace` stamps UTC directly. Most cameras store EXIF time in UTC without an offset, so `replace` is correct. Windows Explorer's "Media created" property reflects this UTC interpretation.

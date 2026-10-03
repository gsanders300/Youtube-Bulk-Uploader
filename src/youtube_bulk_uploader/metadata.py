"""File metadata extraction for YouTube Bulk Uploader."""

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .exiftool import (
    _filesystem_date,
    get_best_date_details,
    is_exiftool_available,
    select_recording_date,
)
from .utils import extract_tags_from_filename, format_file_size, sanitize_filename

logger = logging.getLogger("youtube_uploader")
EASTERN_TIME = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class UploadSettings:
    """YouTube settings that apply to one upload request."""

    category_id: str = "22"
    default_language: str | None = None
    made_for_kids: bool = False
    contains_synthetic_media: bool = False
    has_paid_product_placement: bool = False
    license: str = "youtube"
    embeddable: bool = False
    public_stats_viewable: bool = True
    notify_subscribers: bool = True


def get_file_creation_date(
    filepath: Path,
    use_exiftool: bool = True,
) -> tuple[datetime, str]:
    """
    Extract file creation date, preferring embedded metadata via ExifTool.

    Returns a tuple of (datetime, source) so callers do not need to run a
    second ExifTool probe just to determine which source was used.

    When ExifTool is available and use_exiftool is True:
    - Extracts the recording date from video metadata
      (DateTimeOriginal, CreateDate, etc.)
    - Falls back to filesystem dates if no metadata date is found

    When ExifTool is not available or use_exiftool is False:
    - Windows: Uses st_ctime (creation time)
    - macOS: Uses st_birthtime (creation time)
    - Linux: Falls back to st_mtime (modification time)

    All returned datetimes are timezone-aware (UTC).

    Args:
        filepath: Path to the file.
        use_exiftool: Whether to use ExifTool for date extraction (default: True).

    Returns:
        Tuple of (timezone-aware UTC datetime, source string:
        "exiftool" or "filesystem").
    """
    filepath = Path(filepath)

    if use_exiftool and is_exiftool_available():
        best_date, source, _field = get_best_date_details(filepath)
        return best_date, source

    # Filesystem fallback - always returns a timezone-aware UTC datetime.
    return _filesystem_date(filepath), "filesystem"


def generate_description(
    filename: str,
    custom_prefix: str | None = None,
    recording_date: datetime | None = None,
) -> str:
    """
    Generate a video description from the filename.

    The description is the filename stem (no extension). If a custom prefix is
    provided, it is prepended followed by two newlines.

    Args:
        filename: Original filename (with or without extension).
        custom_prefix: Optional custom text to prepend.
        recording_date: Optional recording date to append in Eastern time.

    Returns:
        Description string, truncated to YouTube's 5000-character limit.
    """
    name_without_ext = Path(filename).stem

    if custom_prefix:
        description = f"{custom_prefix.strip()}\n\n{name_without_ext}"
    else:
        description = name_without_ext

    if recording_date is None:
        return description[:5000]

    if recording_date.tzinfo is None:
        recording_date = recording_date.replace(tzinfo=UTC)

    eastern_date = recording_date.astimezone(EASTERN_TIME)
    recording_line = eastern_date.strftime("Recorded (Eastern): %Y-%m-%d %H:%M:%S %Z")
    suffix = f"\n\n{recording_line}"

    # Reserve room for the timestamp so a long prefix cannot remove it.
    return f"{description[: 5000 - len(suffix)]}{suffix}"


@dataclass
class VideoMetadata:
    """Metadata for a video file to be uploaded."""

    filepath: Path
    title: str
    description: str
    tags: list[str]
    creation_date: datetime
    file_size: int
    fs_date: datetime
    exif_date: datetime | None = None
    privacy_status: str = "unlisted"
    date_source: str = "filesystem"  # "exiftool" or "filesystem"
    date_field: str = "filesystem"
    file_mtime_ns: int = 0
    upload_settings: UploadSettings = field(default_factory=UploadSettings)

    @classmethod
    def from_file(
        cls,
        filepath: Path,
        custom_prefix: str | None = None,
        use_exiftool: bool = True,
        prefetched_exif_dates: dict | None = None,
    ) -> "VideoMetadata":
        """
        Create VideoMetadata from a file path.

        A single stat() call is shared between the filesystem-date and
        file-size computations to avoid redundant I/O.
        """
        filepath = Path(filepath)
        filename = filepath.name

        # Single stat() call — reused for both fs_date and file_size.
        stat = filepath.stat()
        file_size = stat.st_size
        file_mtime_ns = stat.st_mtime_ns
        fs_date = _filesystem_date(filepath, stat_result=stat)

        exif_date = None
        date_source = "filesystem"
        date_field = "filesystem"

        # 1. Try pre-fetched dates first (normal batch-processing path).
        if use_exiftool and prefetched_exif_dates is not None:
            creation_date, date_source, date_field = select_recording_date(
                prefetched_exif_dates, fs_date
            )
            if date_source == "exiftool":
                exif_date = creation_date

        else:
            # 2. Fall back to per-file ExifTool extraction (no prefetched context).
            if use_exiftool and is_exiftool_available():
                creation_date, date_source, date_field = get_best_date_details(filepath)
            else:
                creation_date = fs_date
            if date_source == "exiftool":
                exif_date = creation_date

        title = sanitize_filename(filename)
        description = generate_description(filename, custom_prefix, creation_date)
        tags = extract_tags_from_filename(filename)

        if date_source == "exiftool":
            logger.info(
                f"[{filename}] Recording date from EXIF: "
                f"{creation_date.strftime('%Y-%m-%d %H:%M:%S UTC')}"
            )
        else:
            logger.debug(
                f"[{filename}] Using filesystem date: "
                f"{creation_date.strftime('%Y-%m-%d %H:%M:%S UTC')}"
            )

        return cls(
            filepath=filepath,
            title=title,
            description=description,
            tags=tags,
            creation_date=creation_date,
            file_size=file_size,
            fs_date=fs_date,
            exif_date=exif_date,
            date_source=date_source,
            date_field=date_field,
            file_mtime_ns=file_mtime_ns,
        )

    def __str__(self) -> str:
        """Human-readable representation."""
        date_indicator = " (from metadata)" if self.date_source == "exiftool" else ""
        return (
            f"Video: {self.title}\n"
            f"  File: {self.filepath.name}\n"
            f"  Size: {format_file_size(self.file_size)}\n"
            f"  Created: {self.creation_date.strftime('%Y-%m-%d %H:%M UTC')}"
            f"{date_indicator}\n"
            f"  Tags: {', '.join(self.tags[:5])}{'...' if len(self.tags) > 5 else ''}\n"
            f"  Privacy: {self.privacy_status}"
        )

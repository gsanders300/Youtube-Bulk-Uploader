"""Video file discovery and processing for YouTube Bulk Uploader."""

import logging
from collections.abc import Callable
from pathlib import Path

from .exiftool import get_batch_exiftool_dates
from .metadata import VideoMetadata
from .utils import format_file_size, get_video_extensions

logger = logging.getLogger("youtube_uploader")


def discover_videos(directory: Path, recursive: bool = False) -> list[Path]:
    """
    Find all video files in a directory.

    Args:
        directory: Directory to search
        recursive: Whether to search subdirectories

    Returns:
        List of paths to video files, sorted by name
    """
    directory = Path(directory)
    extensions = get_video_extensions()
    videos = []

    if recursive:
        pattern = "**/*"
    else:
        pattern = "*"

    for path in directory.glob(pattern):
        if path.is_file() and path.suffix.lower() in extensions:
            videos.append(path)

    # Sort by relative path for deterministic recursive ordering, including
    # directories that contain files with the same basename.
    videos.sort(key=lambda p: p.relative_to(directory).as_posix().casefold())

    logger.info(f"Discovered {len(videos)} video(s) in {directory}")
    return videos


def validate_videos(
    video_paths: list[Path],
) -> tuple[list[Path], list[tuple[Path, str]]]:
    """
    Validate video files exist and are readable.

    Args:
        video_paths: List of paths to validate

    Returns:
        Tuple of (valid_paths, [(invalid_path, error_reason), ...])
    """
    valid = []
    invalid = []

    for path in video_paths:
        path = Path(path)

        if not path.exists():
            invalid.append((path, "File does not exist"))
            continue

        if not path.is_file():
            invalid.append((path, "Path is not a file"))
            continue

        try:
            if path.stat().st_size == 0:
                invalid.append((path, "File is empty"))
                continue
            # Opening is a more reliable readability test than os.access(), which
            # can disagree with the eventual operation under ACLs or elevated users.
            with path.open("rb") as file_handle:
                file_handle.read(1)
            valid.append(path)
        except OSError as e:
            invalid.append((path, f"Cannot read file: {e}"))

    if invalid:
        logger.warning(f"Found {len(invalid)} invalid video(s)")
        for path, reason in invalid:
            logger.warning(f"  {path.name}: {reason}")

    return valid, invalid


def prepare_video_batch(
    video_paths: list[Path],
    custom_prefix: str | None = None,
    use_exiftool: bool = True,
    progress_callback: Callable[[int, int], None] | None = None,
) -> list[VideoMetadata]:
    """
    Process list of video paths into VideoMetadata objects.

    Uses a single batch ExifTool call for date extraction, then processes
    remaining metadata sequentially.

    Args:
        video_paths: List of paths to video files
        custom_prefix: Optional custom description prefix
        use_exiftool: Use ExifTool for date extraction (default: True)
        progress_callback: Optional callback(current, total) for progress tracking

    Returns:
        List of VideoMetadata objects
    """
    metadata_list = []
    total = len(video_paths)

    # 1. Pre-fetch ALL metadata using a single ExifTool call (if enabled)
    prefetched_data = {}
    if use_exiftool:
        logger.debug(f"Pre-fetching metadata for {total} file(s) via ExifTool batch")
        prefetched_data = get_batch_exiftool_dates(video_paths)
        logger.debug(f"Retrieved metadata for {len(prefetched_data)} file(s)")

    def process_file(path: Path) -> VideoMetadata | None:
        try:
            # Match using absolute path string
            norm_path = str(path.absolute())
            file_prefetched = prefetched_data.get(norm_path)

            metadata = VideoMetadata.from_file(
                path,
                custom_prefix=custom_prefix,
                use_exiftool=use_exiftool,
                prefetched_exif_dates=file_prefetched,
            )
            return metadata
        except Exception as e:
            logger.error(f"Failed to extract metadata from {path.name}: {e}")
            return None

    # 2. Process the remaining metadata in a simple loop.
    for i, path in enumerate(video_paths):
        metadata = process_file(path)
        if metadata:
            metadata_list.append(metadata)

        if progress_callback:
            progress_callback(i + 1, total)

    logger.info(f"Prepared {len(metadata_list)} video(s) for upload")
    return metadata_list


def get_batch_summary(videos: list[VideoMetadata]) -> dict:
    """
    Get summary statistics for a batch of videos.

    Args:
        videos: List of VideoMetadata objects

    Returns:
        Dictionary with summary stats
    """
    if not videos:
        return {
            "count": 0,
            "total_size": 0,
            "total_size_formatted": "0 B",
            "extensions": set(),
        }

    total_size = sum(v.file_size for v in videos)
    extensions = sorted(set(v.filepath.suffix.lower() for v in videos))

    return {
        "count": len(videos),
        "total_size": total_size,
        "total_size_formatted": format_file_size(total_size),
        "extensions": extensions,
    }

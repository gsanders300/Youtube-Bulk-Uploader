"""Utility functions for YouTube Bulk Uploader."""

import logging
import os
import re
from pathlib import Path

from platformdirs import user_config_path, user_data_path

# Common stop words to filter from tags
STOP_WORDS = {
    "a",
    "an",
    "the",
    "and",
    "or",
    "but",
    "in",
    "on",
    "at",
    "to",
    "for",
    "of",
    "with",
    "by",
    "from",
    "as",
    "is",
    "was",
    "are",
    "were",
    "been",
    "be",
    "have",
    "has",
    "had",
    "do",
    "does",
    "did",
    "will",
    "would",
    "could",
    "should",
    "may",
    "might",
    "must",
    "shall",
    "can",
    "need",
    "that",
    "this",
    "these",
    "those",
    "it",
    "its",
    "my",
    "your",
    "our",
    "their",
    "his",
    "her",
    "mp4",
    "mov",
    "avi",
    "mkv",
    "webm",
    "video",
}


_project_root: Path | None = None


def get_project_root() -> Path:
    """Get the project root directory (result is cached after the first call)."""
    global _project_root
    if _project_root is not None:
        return _project_root
    current = Path(__file__).parent
    while current != current.parent:
        if (current / "pyproject.toml").exists():
            _project_root = current
            return _project_root
        current = current.parent
    # Fallback to parent of src directory
    _project_root = Path(__file__).parent.parent
    return _project_root


def get_config_dir() -> Path:
    """Return the writable per-user configuration directory.

    ``YOUTUBE_UPLOADER_CONFIG_DIR`` is primarily useful for portable installs,
    tests, and automation.  Platform defaults are used otherwise.
    """
    override = os.environ.get("YOUTUBE_UPLOADER_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    return user_config_path("youtube-bulk-uploader", appauthor=False)


def get_data_dir() -> Path:
    """Return the writable per-user application data directory."""
    override = os.environ.get("YOUTUBE_UPLOADER_DATA_DIR")
    if override:
        return Path(override).expanduser()
    return user_data_path("youtube-bulk-uploader", appauthor=False)


def setup_logging(
    log_level: str = "INFO", log_file: str | None = None
) -> logging.Logger:
    """
    Configure logging with both console and optional file output.

    Args:
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        log_file: Optional path to log file

    Returns:
        Configured logger instance
    """
    logger = logging.getLogger("youtube_uploader")
    logger.setLevel(getattr(logging, log_level.upper()))
    logger.propagate = False

    # Clear existing handlers
    logger.handlers.clear()

    # Console handler using Rich for seamless integration with Live displays
    from rich.logging import RichHandler

    console_handler = RichHandler(
        rich_tracebacks=True,
        show_time=False,  # We'll use our own format or let Rich handle it
    )
    console_handler.setLevel(getattr(logging, log_level.upper()))
    logger.addHandler(console_handler)

    # File handler if specified
    if log_file:
        log_path = Path(log_file).expanduser()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_format = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        )
        file_handler.setFormatter(file_format)
        logger.addHandler(file_handler)

    return logger


# Precompiled regexes used in sanitize_filename and extract_tags_from_filename.
_SEPARATOR_RE = re.compile(r"[-_.]")
_WHITESPACE_RE = re.compile(r"\s+")
_TAG_SPLIT_RE = re.compile(r"[-_.\s]+")

_VIDEO_EXTENSIONS: frozenset[str] = frozenset(
    {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".wmv", ".flv"}
)


def get_video_extensions() -> frozenset[str]:
    """Return the frozenset of supported video file extensions."""
    return _VIDEO_EXTENSIONS


def sanitize_filename(filename: str) -> str:
    """
    Clean up filename for use as video title.

    - Removes file extension
    - Replaces separators with spaces
    - Cleans up multiple spaces
    - Strips leading/trailing whitespace

    Args:
        filename: Original filename

    Returns:
        Cleaned title string
    """
    # Remove extension
    name = Path(filename).stem

    # Replace common separators with spaces
    name = _SEPARATOR_RE.sub(" ", name)

    # Clean up multiple spaces
    name = _WHITESPACE_RE.sub(" ", name)

    # Strip whitespace
    name = name.strip()

    # Ensure we have something
    if not name:
        name = "Untitled Video"

    # YouTube title limit is 100 characters
    return name[:100]


def extract_tags_from_filename(filename: str, max_total_chars: int = 500) -> list[str]:
    """
    Extract tags from filename by splitting on separators.

    - Splits on hyphens, underscores, dots, and spaces
    - Filters out stop words and short tokens
    - Respects YouTube's 500 character limit for all tags

    Args:
        filename: Original filename
        max_total_chars: Maximum total characters for all tags combined

    Returns:
        List of tag strings
    """
    # Remove extension
    name = Path(filename).stem

    # Split on separators
    tokens = _TAG_SPLIT_RE.split(name.lower())

    # Filter tokens
    tags = []
    total_chars = 0

    for token in tokens:
        # Skip short tokens and stop words
        if len(token) < 2 or token in STOP_WORDS:
            continue

        # Skip if token is just numbers
        if token.isdigit():
            continue

        # Check character limit (add 1 for comma separator)
        if total_chars + len(token) + 1 > max_total_chars:
            break

        tags.append(token)
        total_chars += len(token) + 1

    return tags


def format_duration(seconds: float) -> str:
    """
    Format seconds into human-readable duration.

    Args:
        seconds: Duration in seconds

    Returns:
        Formatted string like "1h 23m 45s"
    """
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)

    if hours > 0:
        return f"{hours}h {minutes}m {secs}s"
    elif minutes > 0:
        return f"{minutes}m {secs}s"
    else:
        return f"{secs}s"


def format_file_size(size_bytes: int) -> str:
    """
    Format bytes to human readable size.

    Args:
        size_bytes: Size in bytes

    Returns:
        Formatted string like "1.5 GB"
    """
    size = float(size_bytes)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"

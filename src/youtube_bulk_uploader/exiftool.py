"""ExifTool integration for extracting and updating video metadata dates."""

import json
import logging
import os
import platform
import re
import shutil
import subprocess
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from dateutil import parser as dateutil_parser

from .utils import get_project_root

logger = logging.getLogger("youtube_uploader")

# Date fields categorized by source.
# True metadata fields are checked first.
METADATA_FIELDS = [
    "DateTimeOriginal",  # Original recording date
    "CreateDate",  # Creation date in metadata
    "MediaCreateDate",  # Media creation date
    "TrackCreateDate",  # Track creation date
    "CreationDate",  # QuickTime creation date
    "ContentCreateDate",  # Content creation date
]

# Filesystem fields used only as a last resort.
FILESYSTEM_FIELDS = [
    "FileCreateDate",  # File creation date (Windows)
    "FileModifyDate",  # File modification date
]

# Combined list for the ExifTool subprocess call.
DATE_FIELDS = METADATA_FIELDS + FILESYSTEM_FIELDS

# Module-level cache so ExifTool is discovered only once per process.
_exiftool_path: str | None = None
_exiftool_searched: bool = False

# Keep comfortably below Windows' command-line limit and POSIX ARG_MAX.  The
# character budget is deliberately conservative because paths may expand when
# encoded by the process launcher.
_MAX_COMMAND_CHARS = 28_000 if os.name == "nt" else 200_000
_MAX_FILES_PER_CALL = 500


def _chunk_path_arguments(base_command: list[str], paths: list[str]) -> list[list[str]]:
    """Split path arguments into portable command-line-sized batches."""
    batches: list[list[str]] = []
    current_batch: list[str] = []
    base_chars = sum(len(arg) + 1 for arg in base_command)
    current_chars = base_chars
    for path in paths:
        path_chars = len(path) + 1
        if current_batch and (
            len(current_batch) >= _MAX_FILES_PER_CALL
            or current_chars + path_chars > _MAX_COMMAND_CHARS
        ):
            batches.append(current_batch)
            current_batch = []
            current_chars = base_chars
        current_batch.append(path)
        current_chars += path_chars
    if current_batch:
        batches.append(current_batch)
    return batches


def find_exiftool() -> str | None:
    """
    Find the ExifTool executable, caching the result after the first search.

    Search order:
    1. Project root (bundled exiftool.exe takes priority)
    2. System PATH
    3. Common installation directories

    Returns:
        Absolute path string to the exiftool executable, or None if not found.
    """
    global _exiftool_path, _exiftool_searched

    # Return cached result on all subsequent calls.
    if _exiftool_searched:
        return _exiftool_path

    _exiftool_searched = True

    # Common names for the executable across platforms.
    names = ["exiftool.exe", "exiftool"] if os.name == "nt" else ["exiftool"]

    # 1. Check project root (prioritize bundled version).
    root = get_project_root()
    for name in names:
        path = root / name
        if path.is_file():
            try:
                result = subprocess.run(
                    [str(path), "-ver"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if result.returncode == 0:
                    version = result.stdout.strip()
                    logger.debug(f"Found bundled ExifTool: {path} (version {version})")
                    _exiftool_path = str(path)
                    return _exiftool_path
            except (OSError, subprocess.SubprocessError):
                continue

    # 2. Check system PATH.
    for name in names:
        executable = shutil.which(name)
        if not executable:
            continue
        try:
            result = subprocess.run(
                [executable, "-ver"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                logger.debug(
                    f"Found ExifTool in PATH: {name} (version {result.stdout.strip()})"
                )
                _exiftool_path = executable
                return _exiftool_path
        except (OSError, subprocess.SubprocessError):
            continue

    # 3. Check common installation paths.
    if platform.system() == "Windows":
        common_paths = [
            r"C:\Program Files\exiftool\exiftool.exe",
            r"C:\exiftool\exiftool.exe",
            os.path.expanduser(r"~\exiftool\exiftool.exe"),
        ]
    else:
        common_paths = [
            "/usr/local/bin/exiftool",
            "/usr/bin/exiftool",
            "/opt/homebrew/bin/exiftool",
        ]

    for common_path in common_paths:
        if os.path.isfile(common_path):
            try:
                result = subprocess.run(
                    [common_path, "-ver"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
            except (OSError, subprocess.SubprocessError):
                continue
            if result.returncode == 0:
                logger.debug(f"Found ExifTool at: {common_path}")
                _exiftool_path = common_path
                return _exiftool_path

    logger.debug("ExifTool not found on this system")
    return None


def is_exiftool_available() -> bool:
    """Check if ExifTool is available on the system."""
    return find_exiftool() is not None


def _parse_exiftool_date(date_str: str) -> datetime | None:
    """
    Parse a date string returned by ExifTool into a timezone-aware UTC datetime.

    ExifTool uses YYYY:MM:DD (colons as date separators) which dateutil does not
    understand — it silently substitutes today's date for the month/day components.
    We normalize to YYYY-MM-DD before parsing to avoid this.

    Naive datetimes (no timezone offset in the string) are treated as UTC, which
    is the convention used by most cameras and matches what Windows Explorer shows
    in the "Media created" property.
    """
    try:
        # Normalize ExifTool's YYYY:MM:DD date separator to YYYY-MM-DD so that
        # dateutil parses the month and day correctly.
        normalized = re.sub(r"^(\d{4}):(\d{2}):(\d{2})", r"\1-\2-\3", date_str)
        dt = dateutil_parser.parse(normalized, fuzzy=True)
        if dt.tzinfo is None:
            # No timezone offset in the string — treat as UTC (camera convention).
            dt = dt.replace(tzinfo=UTC)
        else:
            dt = dt.astimezone(UTC)
        return dt
    except (ValueError, OverflowError) as e:
        logger.debug(f"Could not parse date string '{date_str}': {e}")
        return None


def get_exiftool_dates(filepath: Path) -> dict:
    """
    Extract all date fields from a single video file using ExifTool.
    """
    results = get_batch_exiftool_dates([filepath])
    return results.get(str(Path(filepath).absolute()), {})


def get_batch_exiftool_dates(filepaths: list[Path]) -> dict[str, dict]:
    """
    Extract date fields from multiple video files in a single ExifTool call.
    """
    exiftool = find_exiftool()
    if not exiftool or not filepaths:
        return {}

    # Standardize input paths for consistent matching without collapsing
    # distinct case-sensitive paths on Linux and case-sensitive macOS volumes.
    def normalize_path(p: object) -> str:
        normalized = os.path.normpath(os.path.realpath(os.path.abspath(str(p))))
        return os.path.normcase(normalized) if os.name == "nt" else normalized

    # Create a lookup map: normalized_path -> original_path_object
    path_lookup = {normalize_path(p): p for p in filepaths}
    path_strs = [str(Path(p).absolute()) for p in filepaths]

    base_cmd = [exiftool, "-json", "-m"]
    for field in DATE_FIELDS:
        base_cmd.append(f"-{field}")

    path_batches = _chunk_path_arguments(base_cmd, path_strs)

    batch_results: dict[str, dict] = {}
    for paths in path_batches:
        try:
            cmd = base_cmd + paths
            logger.debug(f"Running ExifTool for {len(paths)} file(s)")
            timeout = max(30, len(paths) * 2)
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
            logger.debug(f"ExifTool finished with return code {result.returncode}")

            # ExifTool can return useful JSON for good files even when another
            # file in the same batch produced a non-zero status.
            data = _safe_parse_json(result.stdout)
            if data and isinstance(data, list):
                for item in data:
                    source_file = item.get("SourceFile")
                    if not source_file:
                        continue

                    norm_source = normalize_path(source_file)

                    dates = {}
                    for field in DATE_FIELDS:
                        if field in item:
                            raw_val = str(item[field])
                            if raw_val:
                                dt = _parse_exiftool_date(raw_val)
                                if dt is not None:
                                    dates[field] = dt

                    if norm_source in path_lookup:
                        original_p = path_lookup[norm_source]
                        key = str(original_p.absolute())
                        batch_results[key] = dates

            if result.returncode != 0:
                logger.warning(
                    "ExifTool reported errors for a batch: %s",
                    result.stderr.strip()[:500],
                )

        except (OSError, subprocess.SubprocessError) as e:
            logger.warning("Error running an ExifTool batch: %s", e)

    return batch_results


def _safe_parse_json(text: str) -> list[dict[str, Any]]:
    """
    Parse JSON text from ExifTool output, returning an empty list on failure.

    Args:
        text: Raw stdout from an ExifTool -json invocation.

    Returns:
        Parsed list of dictionaries, or [] on any parse error.
    """
    try:
        result = json.loads(text)
        if not isinstance(result, list):
            return []
        return result
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning(f"Failed to parse ExifTool JSON output: {e}")
        return []


def select_recording_date(
    dates: dict[str, datetime], filesystem_date: datetime
) -> tuple[datetime, str, str]:
    """Select a date using metadata-field priority and filesystem fallback.

    Filesystem values reported by ExifTool are intentionally not considered
    embedded recording metadata.  Returning the selected field keeps the UI
    and logs honest about exactly where a value came from.
    """
    now = datetime.now(UTC)
    for field in METADATA_FIELDS:
        candidate = dates.get(field)
        if candidate is None:
            continue
        if candidate.tzinfo is None:
            candidate = candidate.replace(tzinfo=UTC)
        if candidate.year < 1900 or candidate > now.replace(microsecond=0) + timedelta(
            days=2
        ):
            logger.warning("Ignoring implausible %s value: %s", field, candidate)
            continue
        return candidate, "exiftool", field
    return filesystem_date, "filesystem", "filesystem"


def get_best_date_details(filepath: Path) -> tuple[datetime, str, str]:
    """Return the selected date, broad source, and exact field name."""
    exif_dates = get_exiftool_dates(filepath)
    return select_recording_date(exif_dates, _filesystem_date(filepath))


def get_best_date(filepath: Path) -> datetime:
    """
    Get the best available date from video metadata.

    Uses the documented embedded-metadata field priority and falls back to the
    actual OS filesystem date only when no plausible metadata date exists.

    Args:
        filepath: Path to the video file.

    Returns:
        Timezone-aware UTC datetime.
    """
    best_date, source, field = get_best_date_details(filepath)
    logger.debug(
        "Selected %s date for %s from %s: %s",
        source,
        filepath.name,
        field,
        best_date,
    )
    return best_date


def update_file_dates(filepath: Path, new_date: datetime) -> bool:
    """
    Update the file's filesystem dates to match the metadata date.

    Updates modification and access times on all platforms.
    Additionally updates creation time on Windows (via ExifTool) and
    macOS (via SetFile if available).

    Args:
        filepath: Path to the video file.
        new_date: New date to set (may be timezone-aware or naive).

    Returns:
        True if successful, False otherwise.
    """
    filepath = Path(filepath)
    if new_date.tzinfo is None:
        new_date = new_date.replace(tzinfo=UTC)
    local_date = new_date.astimezone()
    timestamp = new_date.timestamp()

    try:
        # Update modification and access times (portable across all platforms).
        os.utime(filepath, (timestamp, timestamp))
        logger.debug(f"Updated modification time for {filepath.name}")

        system = platform.system()

        if system == "Windows":
            # Use the already-cached ExifTool path; no extra discovery needed.
            exiftool = find_exiftool()
            if exiftool:
                win_date_str = local_date.strftime("%Y:%m:%d %H:%M:%S")
                result = subprocess.run(
                    [
                        exiftool,
                        "-overwrite_original",
                        f"-FileCreateDate={win_date_str}",
                        str(filepath),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if result.returncode == 0:
                    logger.debug(f"Updated Windows creation time for {filepath.name}")
                else:
                    logger.debug(
                        f"Could not update Windows creation time: {result.stderr}"
                    )

        elif system == "Darwin":
            try:
                setfile_date = local_date.strftime("%m/%d/%Y %H:%M:%S")
                result = subprocess.run(
                    ["SetFile", "-d", setfile_date, str(filepath)],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if result.returncode == 0:
                    logger.debug(f"Updated macOS creation time for {filepath.name}")
            except FileNotFoundError:
                # os.utime already updated mtime; creation time is unsupported
                # without Apple's command-line developer tools.
                logger.warning(
                    "SetFile is unavailable; creation time was not changed for %s",
                    filepath.name,
                )

        logger.info(
            f"Synced file dates for {filepath.name} to "
            f"{new_date.strftime('%Y-%m-%d %H:%M:%S')}"
        )
        return True

    except Exception as e:
        logger.warning(f"Failed to update file dates for {filepath.name}: {e}")
        return False


def batch_update_file_dates(changes: list[tuple[Path, datetime]]) -> int:
    """
    Update filesystem dates for multiple files efficiently.

    On Windows, groups all files that share the same target date into a single
    ExifTool subprocess call, replacing the N-subprocess-per-file approach.
    Modification/access times are still set individually via os.utime (cheap
    syscall, no subprocess overhead).

    On macOS/Linux, falls back to calling update_file_dates per file since
    SetFile/touch do not support multi-file batching in a useful way.

    Args:
        changes: List of (filepath, new_date) pairs.

    Returns:
        Number of files successfully updated.
    """
    if not changes:
        return 0

    success_count = 0
    successful_changes: list[tuple[Path, datetime]] = []
    system = platform.system()

    # Always update mtime/atime for all files (cheap, no subprocess).
    for filepath, new_date in changes:
        try:
            if new_date.tzinfo is None:
                new_date = new_date.replace(tzinfo=UTC)
            os.utime(filepath, (new_date.timestamp(), new_date.timestamp()))
            success_count += 1
            successful_changes.append((Path(filepath), new_date))
        except Exception as e:
            logger.warning(
                f"Failed to update modification time for {filepath.name}: {e}"
            )

    if system == "Windows":
        exiftool = find_exiftool()
        if not exiftool:
            logger.debug(
                "ExifTool not available; skipping Windows FileCreateDate update"
            )
            return success_count

        # Group files by target date string to minimise subprocess count.
        date_groups: dict[str, list[Path]] = defaultdict(list)
        for filepath, new_date in successful_changes:
            date_str = new_date.astimezone().strftime("%Y:%m:%d %H:%M:%S")
            date_groups[date_str].append(Path(filepath))

        for date_str, filepaths in date_groups.items():
            base_cmd = [
                exiftool,
                "-overwrite_original",
                f"-FileCreateDate={date_str}",
            ]
            for path_batch in _chunk_path_arguments(
                base_cmd, [str(path) for path in filepaths]
            ):
                timeout = max(30, len(path_batch) * 2)
                try:
                    result = subprocess.run(
                        base_cmd + path_batch,
                        capture_output=True,
                        text=True,
                        timeout=timeout,
                        check=False,
                    )
                    if result.returncode == 0:
                        logger.debug(
                            "Batch updated FileCreateDate for %s file(s) to %s",
                            len(path_batch),
                            date_str,
                        )
                    else:
                        logger.warning(
                            "ExifTool FileCreateDate batch failed: %s",
                            result.stderr[:200],
                        )
                except (OSError, subprocess.SubprocessError) as e:
                    logger.warning("Error during batch FileCreateDate update: %s", e)

    elif system == "Darwin":
        # macOS: SetFile/touch don't batch efficiently — call per file.
        for filepath, new_date in successful_changes:
            try:
                setfile_date = new_date.astimezone().strftime("%m/%d/%Y %H:%M:%S")
                result = subprocess.run(
                    ["SetFile", "-d", setfile_date, str(filepath)],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if result.returncode != 0:
                    raise subprocess.CalledProcessError(result.returncode, "SetFile")
            except (FileNotFoundError, subprocess.CalledProcessError):
                logger.warning(
                    "Could not update macOS creation time for %s; "
                    "modification time was updated",
                    filepath.name,
                )

    return success_count


def _filesystem_date(
    filepath: Path, stat_result: os.stat_result | None = None
) -> datetime:
    """
    Return the best available filesystem date for a file as a UTC datetime.

    Args:
        filepath: Path to the file.
        stat_result: Optional pre-computed stat result. If not provided,
            filepath.stat() is called. Pass this to avoid a redundant stat()
            syscall when the caller has already stat'd the file.

    Returns:
        Timezone-aware UTC datetime.
    """
    stat_info = stat_result if stat_result is not None else filepath.stat()

    if os.name == "nt":
        timestamp = stat_info.st_ctime
    elif platform.system() == "Darwin":
        timestamp = getattr(stat_info, "st_birthtime", stat_info.st_mtime)
    else:
        timestamp = stat_info.st_mtime

    return datetime.fromtimestamp(timestamp, tz=UTC)

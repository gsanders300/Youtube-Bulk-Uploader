"""CLI entry point for YouTube Bulk Uploader."""

import platform
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, cast

import click
from rich import box
from rich.console import Console
from rich.live import Live
from rich.markup import escape
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
)
from rich.table import Table
from rich.text import Text

from .auth import (
    get_authenticated_service,
    get_default_credentials_path,
    get_default_token_path,
    validate_credentials_file,
)
from .exiftool import (
    batch_update_file_dates,
    find_exiftool,
    get_exiftool_dates,
    is_exiftool_available,
)
from .metadata import UploadSettings, VideoMetadata
from .state import UploadLedger, get_default_ledger_path
from .uploader import DEFAULT_CHUNK_SIZE, UploadResult, YouTubeUploader
from .utils import (
    format_duration,
    format_file_size,
    get_config_dir,
    get_data_dir,
    setup_logging,
)
from .video_processor import (
    discover_videos,
    get_batch_summary,
    prepare_video_batch,
    validate_videos,
)

console = Console()

try:
    _PACKAGE_VERSION = version("youtube-bulk-uploader")
except PackageNotFoundError:
    _PACKAGE_VERSION = "0+unknown"

# Minimum chunk size enforced by the YouTube resumable upload API.
_CHUNK_ALIGNMENT = 256 * 1024  # 256 KB


@dataclass
class _UploadState:
    """Mutable per-video state tracked during the upload loop."""

    status: str = "Pending"
    url: str = ""
    last_update_time: float = field(default=0.0)


def _truncate(s: str, max_len: int = 40) -> str:
    """Truncate a string with an ellipsis if it exceeds max_len characters."""
    return s[:max_len] + "..." if len(s) > max_len else s


def _format_table_filename(filename: str, max_width: int) -> str:
    """Keep the file extension visible when a table file name is shortened."""
    if len(filename) <= max_width:
        return filename
    suffix = Path(filename).suffix
    prefix_width = max_width - len(suffix) - 1
    if prefix_width < 1:
        return filename[: max_width - 1] + "…"
    return f"{filename[:prefix_width]}…{suffix}"


def _date_source_display(date_source: str) -> tuple[str, str]:
    """Return a short table label and color for the selected date source."""
    if date_source == "exiftool":
        return "EXIF", "bold cyan"
    if date_source == "user_corrected":
        return "Manual", "bold magenta"
    return "FS", "bold yellow"


def _estimate_upload_seconds(total_bytes: int, speed_mbps: float) -> float:
    """Return estimated upload duration in seconds given total size and speed."""
    bytes_per_second = (speed_mbps * 1_000_000) / 8
    return total_bytes / bytes_per_second if bytes_per_second > 0 else 0.0


def _merge_upload_tags(
    generated_tags: list[str], extra_tags: tuple[str, ...], auto_tags: bool
) -> list[str]:
    """Combine generated and user-supplied tags within YouTube's limit."""
    merged: list[str] = []
    seen: set[str] = set()
    total_chars = 0
    candidates = [*(generated_tags if auto_tags else []), *extra_tags]
    for raw_tag in candidates:
        tag = raw_tag.strip()
        normalized = tag.casefold()
        if not tag or normalized in seen:
            continue
        new_total = total_chars + len(tag) + (1 if merged else 0)
        if new_total > 500:
            raise click.ClickException(
                "Combined tags exceed the 500-character YouTube limit"
            )
        merged.append(tag)
        seen.add(normalized)
        total_chars = new_total
    return merged


def _print_upload_settings(settings: UploadSettings, auto_tags: bool) -> None:
    """Show API settings that apply to every video in the batch."""
    table = Table(title="YouTube Upload Settings")
    table.add_column("Setting", style="cyan")
    table.add_column("Value")
    rows = (
        ("Privacy", "unlisted (fixed)"),
        (
            "Audience",
            "made for kids" if settings.made_for_kids else "not made for kids",
        ),
        (
            "Altered or synthetic content",
            "yes" if settings.contains_synthetic_media else "no",
        ),
        ("Paid promotion", "yes" if settings.has_paid_product_placement else "no"),
        ("Notify subscribers", "yes" if settings.notify_subscribers else "no"),
        ("Allow embedding", "yes" if settings.embeddable else "no"),
        (
            "Public extended statistics",
            "yes" if settings.public_stats_viewable else "no",
        ),
        ("License", settings.license),
        ("Category ID", settings.category_id),
        ("Default language", settings.default_language or "channel default"),
        ("Generated tags", "enabled" if auto_tags else "disabled"),
    )
    for name, value in rows:
        table.add_row(name, value)
    console.print(table)


def _quota_skip_results(
    videos: list[VideoMetadata], previous_urls: list[str | None] | None = None
) -> list[UploadResult]:
    """Create explicit results for videos skipped after quota exhaustion."""
    urls = previous_urls or [None] * len(videos)
    return [
        UploadResult(
            video_id=url.rsplit("/", 1)[-1] if url else "",
            title=video.title,
            url=url or "",
            status="skipped" if url else "quota_exceeded",
            error_message=(
                "Already uploaded (exact file version)"
                if url
                else "Skipped: daily quota already exceeded"
            ),
        )
        for video, url in zip(videos, urls, strict=True)
    ]


def _resolve_directory(directory: str | None, create: bool = False) -> Path:
    """
    Resolve a CLI directory argument to an absolute Path.

    Defaults to ./input relative to the current working directory.
    Exits with an error if the directory does not exist and create is False.
    """
    if directory is None:
        resolved = Path.cwd() / "input"
    else:
        resolved = Path(directory).expanduser()

    if not resolved.exists():
        if create:
            console.print(f"[yellow]Directory not found:[/yellow] {resolved}")
            console.print("[dim]Creating directory...[/dim]")
            resolved.mkdir(parents=True, exist_ok=True)
        else:
            console.print(f"\n[red]Error:[/red] Directory not found: {resolved}")
            console.print(
                "[dim]Create the directory and add video files, "
                "or specify a different path.[/dim]"
            )
            sys.exit(1)

    if not resolved.is_dir():
        console.print(f"\n[red]Error:[/red] Not a directory: {resolved}")
        raise click.ClickException(f"Not a directory: {resolved}")

    return resolved.resolve()


def review_dates(metadata_list: list[VideoMetadata]) -> list[VideoMetadata]:
    """
    Interactive loop to review and correct video creation dates before uploading.
    """
    while True:
        _print_video_table(metadata_list, "Proposed Recording Dates")

        console.print("\n[bold]Options:[/bold]")
        console.print(f"  {escape('[a]')} Accept all and proceed to upload")
        console.print(f"  {escape('[q]')} Quit and cancel upload")
        console.print(f"  {escape('[number]')} Edit a specific file's date")

        choice = click.prompt("Selection", type=str).strip().lower()

        if choice == "a":
            return metadata_list
        elif choice == "q":
            console.print("\n[yellow]Upload cancelled by user.[/yellow]")
            sys.exit(0)

        try:
            idx = int(choice) - 1
        except ValueError:
            console.print(
                "  [red]Invalid selection. Enter 'a', 'q', or a file number.[/red]"
            )
            continue

        if not (0 <= idx < len(metadata_list)):
            console.print(
                f"  [red]Invalid file number. Enter a number between "
                f"1 and {len(metadata_list)}.[/red]"
            )
            continue

        m = metadata_list[idx]
        all_dates = get_exiftool_dates(m.filepath)
        available_options = list(all_dates.items())

        console.print(f"\n[bold]Editing: {escape(m.title)}[/bold]")
        proposed = m.creation_date.strftime("%Y-%m-%d %H:%M")
        console.print(f"  Current proposed: {proposed} ({m.date_source})")

        if not available_options:
            console.print("  [yellow]No metadata dates found for this file.[/yellow]")
        else:
            for i, (field, dt) in enumerate(available_options, 1):
                console.print(f"  {i}: {field} ({dt.strftime('%Y-%m-%d %H:%M')})")

        console.print(f"  {escape('[c]')} Enter custom date (YYYY-MM-DD HH:MM)")
        console.print(f"  {escape('[s]')} Skip / Keep current")

        edit_choice = click.prompt("Select date source", type=str).strip().lower()

        if edit_choice == "s":
            continue
        elif edit_choice == "c":
            date_str = click.prompt("Enter new date (YYYY-MM-DD HH:MM)", type=str)
            try:
                new_date = datetime.strptime(date_str, "%Y-%m-%d %H:%M").replace(
                    tzinfo=UTC
                )
                m.creation_date = new_date
                m.date_source = "user_corrected"
                m.date_field = "user_corrected"
                console.print(
                    f"  [green]✓ Updated to "
                    f"{new_date.strftime('%Y-%m-%d %H:%M')}[/green]"
                )
            except ValueError:
                console.print(
                    "  [red]Invalid format. Please use YYYY-MM-DD HH:MM[/red]"
                )
        elif edit_choice.isdigit():
            option_idx = int(edit_choice) - 1
            if 0 <= option_idx < len(available_options):
                field, dt = available_options[option_idx]
                m.creation_date = dt
                m.date_source = "exiftool"
                m.date_field = field
                console.print(
                    f"  [green]✓ Selected {field}: "
                    f"{dt.strftime('%Y-%m-%d %H:%M')}[/green]"
                )
            else:
                console.print("  [red]Invalid option number[/red]")
        else:
            console.print("  [red]Invalid selection[/red]")

    return metadata_list


def _build_video_table(
    metadata_list: list[VideoMetadata], title: str, terminal_width: int
) -> Table:
    """Build a metadata table that adapts its columns to terminal width."""
    if terminal_width >= 135:
        layout = "wide"
        filename_width = 32
    elif terminal_width >= 105:
        layout = "medium"
        filename_width = 24
    elif terminal_width >= 75:
        layout = "compact"
        filename_width = 12
    else:
        layout = "narrow"
        filename_width = 16

    table = Table(
        title=title,
        box=box.SIMPLE_HEAD,
        expand=False,
        pad_edge=False,
        collapse_padding=True,
        caption_style="dim",
    )
    table.add_column("#", style="dim", width=3, no_wrap=True)
    table.add_column(
        "File",
        style="cyan",
        width=filename_width,
        overflow="ellipsis",
        no_wrap=True,
    )
    table.add_column("Size", justify="right", width=9, no_wrap=True)

    if layout == "wide":
        table.add_column("FS Date (UTC)", style="yellow", width=16, no_wrap=True)
        table.add_column("EXIF Date (UTC)", style="cyan", width=16, no_wrap=True)
        table.add_column("Selected (UTC)", style="bold green", width=16, no_wrap=True)
        table.add_column(
            "From",
            width=18,
            overflow="ellipsis",
            no_wrap=True,
        )
    elif layout == "medium":
        table.add_column("FS Date (UTC)", style="yellow", width=16, no_wrap=True)
        table.add_column("EXIF Date (UTC)", style="cyan", width=16, no_wrap=True)
        table.add_column("Selected (UTC)", style="bold green", width=16, no_wrap=True)
        table.add_column(
            "From",
            width=14,
            overflow="ellipsis",
            no_wrap=True,
        )
    elif layout == "compact":
        table.add_column("FS (UTC)", style="yellow", width=10, no_wrap=True)
        table.add_column("EXIF (UTC)", style="cyan", width=10, no_wrap=True)
        table.add_column("Selected", style="bold green", width=10, no_wrap=True)
        table.add_column(
            "From",
            width=10,
            overflow="ellipsis",
            no_wrap=True,
        )
    else:
        table.add_column("FS (UTC)", style="yellow", width=10, no_wrap=True)
        table.add_column("EXIF (UTC)", style="cyan", width=10, no_wrap=True)

    color_legend = "Yellow: FS  •  Cyan: EXIF  •  Green: selected"
    table.caption = (
        f"{color_legend}  •  Widen the terminal to show selected dates."
        if layout == "narrow"
        else color_legend
    )

    for i, m in enumerate(metadata_list, 1):
        index = str(i)
        filename = Text(
            _format_table_filename(m.filepath.name, filename_width),
            overflow="ellipsis",
            no_wrap=True,
        )
        file_size = format_file_size(m.file_size)
        source_name, source_style = _date_source_display(m.date_source)
        source = Text(source_name, style=source_style, no_wrap=True)
        if layout == "wide":
            exif_display = (
                m.exif_date.strftime("%Y-%m-%d %H:%M") if m.exif_date else "N/A"
            )
            table.add_row(
                index,
                filename,
                file_size,
                m.fs_date.strftime("%Y-%m-%d %H:%M"),
                exif_display,
                m.creation_date.strftime("%Y-%m-%d %H:%M"),
                source,
            )
        elif layout == "medium":
            exif_display = (
                m.exif_date.strftime("%Y-%m-%d %H:%M") if m.exif_date else "N/A"
            )
            table.add_row(
                index,
                filename,
                file_size,
                m.fs_date.strftime("%Y-%m-%d %H:%M"),
                exif_display,
                m.creation_date.strftime("%Y-%m-%d %H:%M"),
                source,
            )
        elif layout == "compact":
            exif_display = m.exif_date.strftime("%Y-%m-%d") if m.exif_date else "N/A"
            table.add_row(
                index,
                filename,
                file_size,
                m.fs_date.strftime("%Y-%m-%d"),
                exif_display,
                m.creation_date.strftime("%Y-%m-%d"),
                source,
            )
        else:
            exif_display = m.exif_date.strftime("%Y-%m-%d") if m.exif_date else "N/A"
            table.add_row(
                index,
                filename,
                file_size,
                m.fs_date.strftime("%Y-%m-%d"),
                exif_display,
            )

    return table


def _print_video_table(metadata_list: list[VideoMetadata], title: str) -> None:
    """Print a video table for the current terminal width."""
    console.print(_build_video_table(metadata_list, title, console.width))


@click.group()
@click.version_option(version=_PACKAGE_VERSION, prog_name="youtube-uploader")
def cli() -> None:
    """YouTube Bulk Video Uploader - Upload videos in batch as unlisted."""
    pass


@cli.command()
@click.option(
    "--credentials",
    "-c",
    default=None,
    type=click.Path(),
    help="Path to OAuth credentials file (client_secrets.json)",
)
def auth(credentials: str | None) -> None:
    """
    Authenticate with YouTube API.

    Run this first to set up OAuth credentials. A browser window will open
    for authorization.
    """
    setup_logging()

    creds_path = Path(credentials) if credentials else get_default_credentials_path()

    console.print("\n[bold]YouTube API Authentication[/bold]\n")

    if not validate_credentials_file(creds_path):
        console.print(f"[red]Error:[/red] Credentials file not found at {creds_path}")
        console.print("\nTo set up credentials:")
        console.print("1. Go to https://console.cloud.google.com/")
        console.print("2. Create a project and enable YouTube Data API v3")
        console.print("3. Create OAuth 2.0 credentials (Desktop app)")
        console.print("4. Download the JSON file")
        console.print(f"5. Save it as: {creds_path}")
        sys.exit(1)

    try:
        console.print("Opening browser for authorization...")
        get_authenticated_service(str(creds_path))
        console.print("\n[green]Authentication successful![/green]")
        console.print("You can now use the upload command.")
    except Exception as e:
        console.print(f"\n[red]Authentication failed:[/red] {e}")
        sys.exit(1)


@cli.command()
def exiftool() -> None:
    """
    Check ExifTool installation status.

    ExifTool is used to extract recording dates from video metadata,
    which is more accurate than filesystem dates.
    """
    console.print("\n[bold]ExifTool Status[/bold]\n")

    if is_exiftool_available():
        exiftool_path = find_exiftool()
        if exiftool_path:
            console.print("[green]✓ ExifTool is installed[/green]")
            console.print(f"  Path: {exiftool_path}")
            try:
                result = subprocess.run(
                    [exiftool_path, "-ver"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if result.returncode == 0:
                    console.print(f"  Version: {result.stdout.strip()}")
            except Exception:
                pass
        else:
            console.print("[yellow]✗ ExifTool could not be located[/yellow]")

        console.print(
            "\n[dim]ExifTool will be used to extract recording dates "
            "from video metadata.[/dim]"
        )
    else:
        console.print("[yellow]✗ ExifTool is not installed[/yellow]")
        console.print(
            "\nWithout ExifTool, filesystem dates will be used instead of "
            "the actual recording dates embedded in video files."
        )
        console.print("\n[bold]Installation:[/bold]")

        if platform.system() == "Windows":
            console.print("  1. Download from: https://exiftool.org/")
            console.print("  2. Extract exiftool(-k).exe")
            console.print("  3. Rename to exiftool.exe")
            console.print("  4. Add to PATH or place in project folder")
        elif platform.system() == "Darwin":
            console.print("  Using Homebrew:")
            console.print("    brew install exiftool")
            console.print("\n  Or download from: https://exiftool.org/")
        else:
            console.print("  Ubuntu/Debian:")
            console.print("    sudo apt install libimage-exiftool-perl")
            console.print("\n  Fedora/RHEL:")
            console.print("    sudo dnf install perl-Image-ExifTool")
            console.print("\n  Or download from: https://exiftool.org/")


@cli.command()
def doctor() -> None:
    """Check this installation using the same logic on every platform."""
    setup_logging(log_level="WARNING")
    console.print("\n[bold]YouTube Uploader Diagnostics[/bold]\n")

    required_ok = True
    console.print(f"[green]OK[/green] Python {platform.python_version()}")

    for label, directory in (
        ("Configuration directory", get_config_dir()),
        ("Application data directory", get_data_dir()),
    ):
        try:
            directory.mkdir(parents=True, exist_ok=True)
            console.print(f"[green]OK[/green] {label}: {directory}")
        except OSError as e:
            required_ok = False
            console.print(f"[red]ERROR[/red] {label}: {e}")

    token_path = get_default_token_path()
    token_exists = token_path.exists()
    if token_exists:
        console.print(f"[green]OK[/green] OAuth token: {token_path}")
    else:
        console.print(f"[yellow]WARN[/yellow] OAuth token not created: {token_path}")

    credentials_path = get_default_credentials_path()
    credentials_valid = credentials_path.exists() and validate_credentials_file(
        credentials_path
    )
    if credentials_valid:
        console.print(f"[green]OK[/green] OAuth credentials: {credentials_path}")
    elif token_exists:
        console.print(
            "[yellow]WARN[/yellow] OAuth client file is absent; the existing "
            "token can still be used and refreshed"
        )
    else:
        required_ok = False
        console.print(f"[red]ERROR[/red] OAuth credentials missing: {credentials_path}")

    exiftool_path = find_exiftool()
    if exiftool_path:
        console.print(f"[green]OK[/green] ExifTool: {exiftool_path}")
    else:
        console.print(
            "[yellow]WARN[/yellow] ExifTool unavailable; dates use filesystem"
        )

    input_path = Path.cwd() / "input"
    if input_path.is_dir():
        console.print(f"[green]OK[/green] Input directory: {input_path}")
    else:
        console.print(f"[yellow]WARN[/yellow] Input directory missing: {input_path}")

    if not required_ok:
        raise click.ClickException("Required setup checks failed")
    console.print("\n[green]All required checks passed.[/green]")


@cli.command()
@click.argument("directory", type=click.Path(), required=False)
@click.option("--recursive", "-r", is_flag=True, help="Search subdirectories")
def sync_dates(directory: str | None, recursive: bool) -> None:
    """
    Align filesystem dates with recording dates embedded in video metadata.

    Scans for videos, compares current filesystem modification dates with metadata,
    and asks for confirmation before updating them.
    """
    setup_logging()
    target = _resolve_directory(directory, create=False)
    console.print("\n[bold]Date Synchronization[/bold]\n")
    console.print(f"Scanning: {target}")

    if not is_exiftool_available():
        console.print(
            "[red]Error:[/red] ExifTool is not installed. "
            "Please install it to sync dates."
        )
        sys.exit(1)

    videos = discover_videos(target, recursive)
    if not videos:
        console.print("[yellow]No video files found.[/yellow]")
        return

    valid, _ = validate_videos(videos)
    if not valid:
        console.print("[red]No valid video files found.[/red]")
        return

    proposed_changes: list[dict[str, Any]] = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("[cyan]Analyzing dates...", total=len(valid))

        def update_sync_progress(current: int, _total: int) -> None:
            progress.update(task, completed=current)

        metadata_list = prepare_video_batch(
            valid,
            use_exiftool=True,
            progress_callback=update_sync_progress,
        )
        for meta in metadata_list:
            video_path = meta.filepath
            current_fs_date = meta.fs_date
            if meta.date_source == "exiftool" and meta.creation_date != current_fs_date:
                proposed_changes.append(
                    {
                        "path": video_path,
                        "current": current_fs_date,
                        "proposed": meta.creation_date,
                    }
                )

    if not proposed_changes:
        console.print(
            "\n[green]All file dates are already aligned with metadata.[/green]"
        )
        return

    table = Table(title="Proposed Date Changes")
    table.add_column("File", style="cyan")
    table.add_column("Current Date", style="dim")
    table.add_column("Proposed Date", style="green")

    for change in proposed_changes:
        filename = Path(cast(str, change["path"])).name
        current_str = cast(datetime, change["current"]).strftime("%Y-%m-%d %H:%M")
        proposed_str = cast(datetime, change["proposed"]).strftime("%Y-%m-%d %H:%M")
        table.add_row(
            Text(filename),
            current_str,
            proposed_str,
        )

    console.print(table)

    if click.confirm("\nApply these changes to the filesystem?", default=True):
        changes = [
            (cast(Path, c["path"]), cast(datetime, c["proposed"]))
            for c in proposed_changes
        ]
        console.print("[cyan]Updating file dates...[/cyan]")
        updated = batch_update_file_dates(changes)
        console.print(
            f"\n[green]✓ Updated modification dates for "
            f"{updated}/{len(changes)} file(s).[/green]"
        )
    else:
        console.print("\n[yellow]Date synchronization cancelled.[/yellow]")


@cli.command()
@click.argument("directory", type=click.Path(), required=False)
@click.option("--recursive", "-r", is_flag=True, help="Search subdirectories")
@click.option(
    "--no-exiftool", is_flag=True, help="Disable ExifTool metadata extraction"
)
@click.option(
    "--speed",
    "-s",
    default=10.0,
    type=click.FloatRange(min=0.01),
    help="Assumed upload speed in Mbps for time estimation (default: 10.0)",
)
def scan(
    directory: str | None, recursive: bool, no_exiftool: bool, speed: float
) -> None:
    """
    Scan directory and show videos that would be uploaded.

    Displays extracted metadata without uploading.
    Uses ExifTool to extract recording dates from video metadata when available.

    If no directory is specified, defaults to ./input in the current directory.
    """
    setup_logging()

    # Create input directory if missing — scan is non-destructive so this is safe.
    target = _resolve_directory(directory, create=True)
    console.print(f"\n[bold]Scanning:[/bold] {target}\n")

    use_exiftool = not no_exiftool
    if use_exiftool:
        if is_exiftool_available():
            console.print(
                "[green]OK[/green] ExifTool available - using embedded video dates\n"
            )
        else:
            console.print(
                "[yellow]![/yellow] ExifTool not found - using filesystem dates"
            )
            console.print(
                "[dim]  Install ExifTool for accurate recording dates[/dim]\n"
            )

    videos = discover_videos(target, recursive)

    if not videos:
        console.print("[yellow]No video files found.[/yellow]")
        console.print(
            "Supported formats: .mp4, .mov, .avi, .mkv, .webm, .m4v, .wmv, .flv"
        )
        return

    valid, invalid = validate_videos(videos)

    if not valid:
        console.print("[red]No valid video files found.[/red]")
        return

    console.print("[cyan]Extracting metadata...[/cyan]")
    metadata_list = prepare_video_batch(
        valid,
        use_exiftool=use_exiftool,
    )

    _print_video_table(metadata_list, f"Found {len(metadata_list)} Video(s)")

    summary = get_batch_summary(metadata_list)
    exif_count = sum(1 for m in metadata_list if m.date_source == "exiftool")

    console.print(f"\n[bold]Total size:[/bold] {summary['total_size_formatted']}")
    console.print(f"[bold]Formats:[/bold] {', '.join(summary['extensions'])}")

    est_seconds = _estimate_upload_seconds(summary["total_size"], speed)
    console.print(
        f"[bold]Estimated upload time:[/bold] {format_duration(est_seconds)} "
        f"(at {speed} Mbps)"
    )

    if use_exiftool and exif_count > 0:
        console.print(
            f"[bold]Dates from EXIF:[/bold] {exif_count}/{len(metadata_list)} videos"
        )

    if invalid:
        console.print(f"\n[yellow]Skipped {len(invalid)} invalid file(s)[/yellow]")


@cli.command()
@click.argument("directory", type=click.Path(), required=False)
@click.option("--recursive", "-r", is_flag=True, help="Search subdirectories")
@click.option(
    "--dry-run",
    "-n",
    is_flag=True,
    help="Show what would be uploaded without uploading",
)
@click.option(
    "--delay",
    "-d",
    default=0,
    type=click.IntRange(min=0),
    help="Seconds between uploads (default: 0)",
)
@click.option(
    "--prefix", "-p", default=None, help="Custom prefix for video descriptions"
)
@click.option("--log-file", "-l", default=None, type=click.Path(), help="Log file path")
@click.option(
    "--credentials",
    "-c",
    default=None,
    type=click.Path(),
    help="Path to OAuth credentials file",
)
@click.option(
    "--no-exiftool", is_flag=True, help="Disable ExifTool metadata extraction"
)
@click.option(
    "--speed",
    "-s",
    default=10.0,
    type=click.FloatRange(min=0.01),
    help="Assumed upload speed in Mbps for time estimation (default: 10.0)",
)
@click.option(
    "--audience",
    type=click.Choice(["not-made-for-kids", "made-for-kids"]),
    default="not-made-for-kids",
    show_default=True,
    help="Set the required YouTube audience declaration",
)
@click.option(
    "--altered-content/--no-altered-content",
    default=False,
    show_default=True,
    help="Disclose realistic altered or synthetic content",
)
@click.option(
    "--paid-promotion/--no-paid-promotion",
    default=False,
    show_default=True,
    help="Disclose paid product placement or endorsement",
)
@click.option(
    "--notify-subscribers/--no-notify-subscribers",
    default=True,
    show_default=True,
    help="Send a new-video notification to subscribers",
)
@click.option(
    "--allow-embedding/--no-embedding",
    default=False,
    show_default=True,
    help="Allow playback on other websites (disabled by default)",
)
@click.option(
    "--public-stats/--no-public-stats",
    default=True,
    show_default=True,
    help="Show extended statistics on the watch page",
)
@click.option(
    "--license",
    "license_name",
    type=click.Choice(["youtube", "creative-common"]),
    default="youtube",
    show_default=True,
    help="Set the video license",
)
@click.option(
    "--category-id",
    type=click.IntRange(min=1),
    default=22,
    show_default=True,
    help="Set the numeric YouTube video category ID",
)
@click.option(
    "--default-language",
    default=None,
    metavar="BCP47",
    help="Set the default title and description language",
)
@click.option(
    "--tag",
    "extra_tags",
    multiple=True,
    help="Add a tag to every video; repeat this option for more tags",
)
@click.option(
    "--auto-tags/--no-auto-tags",
    default=True,
    show_default=True,
    help="Generate tags from each file name",
)
@click.option(
    "--chunk-size",
    "-cs",
    default=DEFAULT_CHUNK_SIZE // (1024 * 1024),
    type=click.IntRange(min=1, max=1024),
    help="Upload chunk size in MB (default: 64)",
)
@click.option(
    "--state-file",
    type=click.Path(path_type=Path),
    default=None,
    help="Upload ledger path (default: platform user data directory)",
)
@click.option(
    "--force",
    is_flag=True,
    help="Upload even when the exact file version is already in the ledger",
)
def upload(
    directory: str | None,
    recursive: bool,
    dry_run: bool,
    delay: int,
    prefix: str | None,
    log_file: str | None,
    credentials: str | None,
    no_exiftool: bool,
    speed: float,
    audience: str,
    altered_content: bool,
    paid_promotion: bool,
    notify_subscribers: bool,
    allow_embedding: bool,
    public_stats: bool,
    license_name: str,
    category_id: int,
    default_language: str | None,
    extra_tags: tuple[str, ...],
    auto_tags: bool,
    chunk_size: int,
    state_file: Path | None,
    force: bool,
) -> None:
    """
    Upload all videos from DIRECTORY to YouTube as unlisted.

    Uses ExifTool to extract recording dates from video metadata. Before uploading,
    you will be shown a table of proposed dates and asked to confirm or edit them.

    If no directory is specified, defaults to ./input in the current directory.

    Examples:

        uv run --locked youtube-uploader upload

        uv run --locked youtube-uploader upload ./videos

        uv run --locked youtube-uploader upload --dry-run

        uv run --locked youtube-uploader upload --prefix "Family Vacation 2024"
    """
    setup_logging(log_file=log_file)

    # Upload requires the directory to already exist — do not silently create it.
    target = _resolve_directory(directory, create=False)

    console.print("\n[bold]YouTube Bulk Uploader[/bold]\n")
    console.print(f"Directory: {target}")
    if prefix:
        console.print(f"Description prefix: {escape(prefix)}")

    use_exiftool = not no_exiftool
    if use_exiftool:
        if is_exiftool_available():
            console.print("ExifTool: [green]available[/green]")
        else:
            console.print(
                "ExifTool: [yellow]not found[/yellow] (using filesystem dates)"
            )
            console.print(
                "[dim]         Install ExifTool for accurate recording dates[/dim]"
            )
    else:
        console.print("ExifTool: [dim]disabled[/dim]")

    console.print()

    videos = discover_videos(target, recursive)

    if not videos:
        console.print("[yellow]No video files found.[/yellow]")
        return

    valid, invalid = validate_videos(videos)

    if not valid:
        console.print("[red]No valid video files to upload.[/red]")
        return

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("[cyan]Extracting metadata...", total=len(valid))

        def update_scan_progress(current: int, total: int) -> None:
            progress.update(task, completed=current)

        metadata_list = prepare_video_batch(
            valid,
            custom_prefix=prefix,
            use_exiftool=use_exiftool,
            progress_callback=update_scan_progress,
        )

    upload_settings = UploadSettings(
        category_id=str(category_id),
        default_language=(default_language.strip() if default_language else None),
        made_for_kids=audience == "made-for-kids",
        contains_synthetic_media=altered_content,
        has_paid_product_placement=paid_promotion,
        license="creativeCommon" if license_name == "creative-common" else "youtube",
        embeddable=allow_embedding,
        public_stats_viewable=public_stats,
        notify_subscribers=notify_subscribers,
    )
    for metadata in metadata_list:
        metadata.upload_settings = upload_settings
        metadata.tags = _merge_upload_tags(metadata.tags, extra_tags, auto_tags)

    summary = get_batch_summary(metadata_list)
    exif_count = sum(1 for m in metadata_list if m.date_source == "exiftool")

    count = len(metadata_list)
    if count == 0:
        console.print("[red]Metadata extraction failed for every video.[/red]")
        return
    console.print(
        f"Found [bold]{count}[/bold] video(s) ({summary['total_size_formatted']})"
    )
    _print_upload_settings(upload_settings, auto_tags)
    if notify_subscribers and count > 1:
        console.print(
            "[yellow]Warning:[/yellow] YouTube can notify subscribers for every "
            "video in this batch. Use --no-notify-subscribers to prevent this."
        )

    est_seconds = _estimate_upload_seconds(summary["total_size"], speed)
    console.print(
        f"Estimated upload time: [bold]{format_duration(est_seconds)}[/bold] "
        f"(at {speed} Mbps)"
    )

    if exif_count > 0:
        console.print(
            f"[bold]Dates from EXIF:[/bold] {exif_count}/{len(metadata_list)} videos"
        )

    if dry_run:
        console.print("\n[yellow]DRY RUN - No uploads will be performed[/yellow]\n")

        _print_video_table(metadata_list, "Videos to Upload")

        console.print("\n[dim]Run without --dry-run to upload[/dim]")
        return

    ledger_path = state_file or get_default_ledger_path()
    if not force:
        try:
            with UploadLedger(ledger_path) as ledger:
                previous_urls = [
                    ledger.successful_url(video) for video in metadata_list
                ]
        except Exception as e:
            raise click.ClickException(f"Could not use upload ledger: {e}") from e
        if all(previous_urls):
            console.print(
                "\n[cyan]Every exact file version is already uploaded.[/cyan]"
            )
            for previous_url in previous_urls:
                console.print(f"  {previous_url}")
            return

    # Interactive date review — user can edit or accept dates before upload begins.
    console.print()
    metadata_list = review_dates(metadata_list)

    console.print("\nAuthenticating with YouTube...")
    try:
        creds_path = credentials if credentials else None
        service = get_authenticated_service(creds_path)
    except FileNotFoundError as e:
        console.print(f"\n[red]Error:[/red] {e}")
        console.print("\nRun 'youtube-uploader auth' first to set up credentials.")
        sys.exit(1)
    except Exception as e:
        console.print(f"\n[red]Authentication failed:[/red] {e}")
        sys.exit(1)

    console.print("[green]Authenticated[/green]\n")

    # Align chunk size to nearest 256 KB multiple (YouTube API requirement).
    actual_chunk_size = max(
        _CHUNK_ALIGNMENT,
        (chunk_size * 1024 * 1024 // _CHUNK_ALIGNMENT) * _CHUNK_ALIGNMENT,
    )
    if actual_chunk_size != chunk_size * 1024 * 1024:
        console.print(
            f"[yellow]Note:[/yellow] Chunk size adjusted to "
            f"{actual_chunk_size // 1024} KB to meet API requirements."
        )

    uploader = YouTubeUploader(service, chunk_size=actual_chunk_size)
    results: list[UploadResult] = []
    console.print(f"Upload ledger: [dim]{ledger_path}[/dim]\n")

    # Per-video upload state (replaces three parallel lists).
    states = [_UploadState() for _ in metadata_list]

    def generate_upload_table() -> Table:
        table = Table(title="Upload Progress")
        table.add_column("#", style="dim", width=4)
        table.add_column("Title", style="cyan", max_width=40)
        table.add_column("Size", justify="right")
        table.add_column("Date", style="dim")
        table.add_column("Status", width=12, justify="center")
        table.add_column("URL/Error", style="dim")

        for i, (m, state) in enumerate(zip(metadata_list, states, strict=True), 1):
            color = "white"
            if "OK" in state.status:
                color = "green"
            elif "SKIPPED" in state.status:
                color = "cyan"
            elif "%" in state.status or "RESUMING" in state.status:
                color = "yellow"
            elif "X" in state.status:
                color = "red"

            table.add_row(
                str(i),
                Text(_truncate(m.title)),
                format_file_size(m.file_size),
                m.creation_date.strftime("%Y-%m-%d"),
                f"[{color}]{state.status}[/{color}]",
                Text(state.url),
            )
        return table

    try:
        with UploadLedger(ledger_path) as ledger:
            with Live(
                generate_upload_table(), console=console, refresh_per_second=4
            ) as live:
                for i, video in enumerate(metadata_list):
                    previous_url = None if force else ledger.successful_url(video)
                    if previous_url:
                        result = UploadResult(
                            video_id=previous_url.rsplit("/", 1)[-1],
                            title=video.title,
                            url=previous_url,
                            status="skipped",
                            error_message="Already uploaded (exact file version)",
                        )
                        results.append(result)
                        states[i].status = "SKIPPED"
                        states[i].url = previous_url
                        live.update(generate_upload_table())
                        continue

                    resume_uri = None if force else ledger.resumable_uri(video)
                    states[i].status = "RESUMING" if resume_uri else "0%"
                    live.update(generate_upload_table())

                    def _make_progress_callback(idx: int) -> Callable[[float], None]:
                        def update_progress(pct: float) -> None:
                            now = time.monotonic()
                            if now - states[idx].last_update_time >= 1.0 or pct >= 1.0:
                                states[idx].status = f"{int(pct * 100)}%"
                                states[idx].last_update_time = now
                                live.update(generate_upload_table())

                        return update_progress

                    ledger.record_started(video)

                    def persist_session(
                        uri: str, current_video: VideoMetadata = video
                    ) -> None:
                        ledger.record_session(current_video, uri)

                    result = uploader.upload_video(
                        video,
                        _make_progress_callback(i),
                        resume_uri=resume_uri,
                        session_callback=persist_session,
                    )
                    results.append(result)
                    ledger.record_result(
                        video,
                        result.status,
                        result.video_id,
                        result.url,
                        result.error_message or "",
                    )

                    if result.success:
                        states[i].status = "OK"
                        states[i].url = result.url
                    elif result.status == "quota_exceeded":
                        states[i].status = "X QUOTA"
                        states[i].url = "API Quota Exceeded"
                        remaining_videos = metadata_list[i + 1 :]
                        previous_urls = [
                            None if force else ledger.successful_url(video)
                            for video in remaining_videos
                        ]
                        skipped_results = _quota_skip_results(
                            remaining_videos, previous_urls
                        )
                        for j, skipped_result in enumerate(skipped_results, i + 1):
                            states[j].status = (
                                "SKIPPED"
                                if skipped_result.status == "skipped"
                                else "X QUOTA"
                            )
                            states[j].url = skipped_result.url
                            results.append(skipped_result)
                        live.update(generate_upload_table())
                        break
                    else:
                        states[i].status = "X ERROR"
                        error_msg = result.error_message or "Unknown error"
                        states[i].url = error_msg[:50]

                    live.update(generate_upload_table())

                    if i < len(metadata_list) - 1 and delay > 0:
                        time.sleep(delay)
    except KeyboardInterrupt:
        console.print("\n[yellow]Upload interrupted; progress is saved.[/yellow]")
        return
    except Exception as e:
        raise click.ClickException(f"Upload workflow failed: {e}") from e

    console.print("\n[bold]Upload Summary[/bold]")

    successful = [r for r in results if r.success]
    failed = [r for r in results if r.status == "failed"]
    quota = [r for r in results if r.status == "quota_exceeded"]
    skipped = [r for r in results if r.status == "skipped"]

    console.print(f"  [green]Successful:[/green] {len(successful)}")
    console.print(f"  [red]Failed:[/red] {len(failed)}")
    if quota:
        console.print(f"  [yellow]Quota exceeded:[/yellow] {len(quota)}")
    if skipped:
        console.print(f"  [cyan]Already uploaded:[/cyan] {len(skipped)}")

    if successful:
        console.print("\n[bold]Uploaded Videos:[/bold]")
        for r in successful:
            console.print(f"  {r.url}")

    if failed:
        console.print("\n[bold]Failed Videos:[/bold]")
        for r in failed:
            console.print(
                f"  [red]{escape(r.title)}:[/red] "
                f"{escape(r.error_message or 'Unknown error')}"
            )


def main() -> None:
    """Entry point for the CLI."""
    cli()


if __name__ == "__main__":
    main()

"""YouTube video upload functionality with resumable upload support."""

import json
import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httplib2
from googleapiclient.discovery import Resource
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from .metadata import VideoMetadata
from .utils import format_duration

logger = logging.getLogger("youtube_uploader")

# Retry configuration
RETRIABLE_EXCEPTIONS = (httplib2.HttpLib2Error, IOError, ConnectionResetError)
RETRIABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}
RETRIABLE_ERROR_REASONS = {
    "backendError",
    "rateLimitExceeded",
    "userRateLimitExceeded",
}
QUOTA_ERROR_REASONS = {"dailyLimitExceeded", "quotaExceeded", "uploadLimitExceeded"}
MAX_RETRIES = 10

# Default chunk size: 64MB (must be a multiple of 256KB)
DEFAULT_CHUNK_SIZE = 64 * 1024 * 1024


@dataclass
class UploadResult:
    """Result of a video upload attempt."""

    video_id: str
    title: str
    url: str
    status: str  # 'success', 'failed', 'quota_exceeded', 'skipped'
    error_message: str | None = None
    upload_time_seconds: float = 0.0

    @property
    def success(self) -> bool:
        """Check if upload was successful."""
        return self.status == "success"

    def __str__(self) -> str:
        if self.success:
            duration = format_duration(self.upload_time_seconds)
            return f"[SUCCESS] {self.title} -> {self.url} ({duration})"
        else:
            return f"[{self.status.upper()}] {self.title}: {self.error_message}"


class YouTubeUploader:
    """Handles video uploads to YouTube with resumable upload support."""

    def __init__(self, service: Resource, chunk_size: int = DEFAULT_CHUNK_SIZE):
        """
        Initialize uploader with authenticated service.

        Args:
            service: Authenticated YouTube API service.
            chunk_size: Upload chunk size in bytes (default 64MB). Must be a
                multiple of 256 KB as required by the YouTube resumable upload API.
        """
        self.service = service
        self.chunk_size = chunk_size
        logger.debug(
            f"Initialized uploader with {chunk_size // (1024 * 1024)}MB chunks"
        )

    def _build_request_body(self, metadata: VideoMetadata) -> dict:
        """
        Build the YouTube API request body from a VideoMetadata object.

        Args:
            metadata: Video metadata to encode into the request.

        Returns:
            Dictionary suitable for passing as the `body` argument to
            `youtube.videos().insert()`.
        """
        # IMPORTANT: Always enforce unlisted privacy status
        privacy_status = "unlisted"
        if metadata.privacy_status != "unlisted":
            logger.warning(
                f"Overriding privacy status from "
                f"'{metadata.privacy_status}' to 'unlisted'"
            )

        # YouTube's recordingDetails.recordingDate is typed as a date (not datetime).
        # The documented canonical format is YYYY-MM-DD (RFC 3339 date-only).
        recording_date_iso = metadata.creation_date.strftime("%Y-%m-%d")

        settings = metadata.upload_settings
        snippet = {
            "title": metadata.title[:100],  # YouTube limit
            "description": metadata.description[:5000],  # YouTube limit
            "tags": metadata.tags,
            "categoryId": settings.category_id,
        }
        if settings.default_language:
            snippet["defaultLanguage"] = settings.default_language

        body = {
            "snippet": snippet,
            "status": {
                "privacyStatus": privacy_status,  # Always unlisted
                "selfDeclaredMadeForKids": settings.made_for_kids,
                "containsSyntheticMedia": settings.contains_synthetic_media,
                "license": settings.license,
                "embeddable": settings.embeddable,
                "publicStatsViewable": settings.public_stats_viewable,
            },
            "recordingDetails": {"recordingDate": recording_date_iso},
        }
        if settings.has_paid_product_placement:
            body["paidProductPlacementDetails"] = {"hasPaidProductPlacement": True}
        return body

    def _execute_resumable_upload(
        self,
        request: Any,
        metadata: VideoMetadata,
        progress_callback: Callable[[float], None] | None = None,
        session_callback: Callable[[str], None] | None = None,
    ) -> UploadResult:
        """
        Drive a resumable upload to completion with retry and back-off.

        Polls `request.next_chunk()` in a loop, retrying on transient HTTP
        errors (5xx) and network exceptions with exponential back-off up to
        MAX_RETRIES attempts. Quota and permission errors are returned
        immediately without retrying.

        Args:
            request: A `googleapiclient` resumable upload request object.
            metadata: Metadata for the video being uploaded (used to populate
                the returned UploadResult).
            progress_callback: Optional callable that receives the current
                upload progress as a float in [0.0, 1.0].
            session_callback: Optional callable that persists the server's
                resumable-session URI. An empty string clears an expired URI.

        Returns:
            An UploadResult with status "success", "failed", or
            "quota_exceeded". The CLI may additionally create "skipped"
            results for entries already present in its persistent ledger.
        """
        response = None
        error = None
        retry = 0
        start_time = time.monotonic()
        last_session_uri = getattr(request, "resumable_uri", None)

        while response is None:
            try:
                status, response = request.next_chunk()
                # Count consecutive failures rather than failures spread over an
                # otherwise healthy multi-hour upload.
                retry = 0

                if status and progress_callback:
                    progress_callback(status.progress())

            except HttpError as e:
                reasons = self._http_error_reasons(e)
                if e.resp.status in {404, 410} and getattr(
                    request, "resumable_uri", None
                ):
                    # Resumable upload sessions expire. Clear the stale URI and
                    # let the request create a fresh session on the next attempt.
                    request.resumable_uri = None
                    request._in_error_state = False
                    error = "Expired resumable upload session"
                    logger.warning(error)
                elif e.resp.status in RETRIABLE_STATUS_CODES or reasons.intersection(
                    RETRIABLE_ERROR_REASONS
                ):
                    error = f"Retriable HTTP error {e.resp.status}"
                    logger.warning(error)
                elif e.resp.status == 403:
                    if reasons.intersection(QUOTA_ERROR_REASONS):
                        logger.error("YouTube API quota exceeded")
                        return UploadResult(
                            video_id="",
                            title=metadata.title,
                            url="",
                            status="quota_exceeded",
                            error_message=(
                                "Daily API quota exceeded. Try again tomorrow."
                            ),
                            upload_time_seconds=time.monotonic() - start_time,
                        )
                    else:
                        return UploadResult(
                            video_id="",
                            title=metadata.title,
                            url="",
                            status="failed",
                            error_message=f"Permission error: {e.resp.status}",
                            upload_time_seconds=time.monotonic() - start_time,
                        )
                else:
                    raw_content = e.content
                    error_text = (
                        raw_content.decode("utf-8", errors="replace")
                        if isinstance(raw_content, bytes)
                        else str(raw_content)
                    )
                    return UploadResult(
                        video_id="",
                        title=metadata.title,
                        url="",
                        status="failed",
                        error_message=(
                            f"HTTP error {e.resp.status}: {error_text[:200]}"
                        ),
                        upload_time_seconds=time.monotonic() - start_time,
                    )

            except RETRIABLE_EXCEPTIONS as e:
                error = f"Retriable error: {type(e).__name__}"
                logger.warning(error)
            finally:
                current_session_uri = getattr(request, "resumable_uri", None)
                if session_callback and current_session_uri != last_session_uri:
                    session_callback(current_session_uri or "")
                    last_session_uri = current_session_uri

            if error:
                retry += 1
                if retry > MAX_RETRIES:
                    logger.error(f"Max retries ({MAX_RETRIES}) exceeded")
                    return UploadResult(
                        video_id="",
                        title=metadata.title,
                        url="",
                        status="failed",
                        error_message=f"Max retries exceeded. Last error: {error}",
                        upload_time_seconds=time.monotonic() - start_time,
                    )

                # Exponential backoff
                sleep_seconds = random.uniform(0, min(2**retry, 60))
                logger.info(f"Retry {retry}/{MAX_RETRIES} in {sleep_seconds:.1f}s...")
                time.sleep(sleep_seconds)
                error = None

        # Upload successful
        upload_time = time.monotonic() - start_time
        if not isinstance(response, dict) or not response.get("id"):
            return UploadResult(
                video_id="",
                title=metadata.title,
                url="",
                status="failed",
                error_message="Upload completed without a video ID in the response.",
                upload_time_seconds=upload_time,
            )
        video_id = str(response["id"])

        logger.debug(
            f"Upload complete: {metadata.title} -> https://youtu.be/{video_id}"
        )

        return UploadResult(
            video_id=video_id,
            title=metadata.title,
            url=f"https://youtu.be/{video_id}",
            status="success",
            upload_time_seconds=upload_time,
        )

    @staticmethod
    def _http_error_reasons(error: HttpError) -> set[str]:
        """Extract stable Google API error reason codes from an HttpError."""
        try:
            raw_content = error.content
            content = (
                raw_content.decode("utf-8", errors="replace")
                if isinstance(raw_content, bytes)
                else str(raw_content)
            )
            payload = json.loads(content)
            details = payload.get("error", {}).get("errors", [])
            return {
                str(detail["reason"])
                for detail in details
                if isinstance(detail, dict) and detail.get("reason")
            }
        except (AttributeError, json.JSONDecodeError, TypeError):
            return set()

    def upload_video(
        self,
        metadata: VideoMetadata,
        progress_callback: Callable[[float], None] | None = None,
        resume_uri: str | None = None,
        session_callback: Callable[[str], None] | None = None,
    ) -> UploadResult:
        """
        Upload a single video to YouTube using the resumable upload API.

        Constructs the request body from `metadata`, initiates a resumable
        upload session, and delegates chunk transmission to
        `_execute_resumable_upload`.

        Args:
            metadata: Metadata for the video to upload.
            progress_callback: Optional callable that receives the current
                upload progress as a float in [0.0, 1.0].
            resume_uri: Previously persisted server-side resumable-session URI.
            session_callback: Callback used to persist changes to that URI.

        Returns:
            An UploadResult with status "success", "failed", or
            "quota_exceeded".
        """
        logger.debug(f"Starting upload: {metadata.title}")
        logger.debug(f"  File: {metadata.filepath}")
        logger.debug(f"  Privacy: {metadata.privacy_status}")

        try:
            current_stat = metadata.filepath.stat()
            if (
                current_stat.st_size != metadata.file_size
                or current_stat.st_mtime_ns != metadata.file_mtime_ns
            ):
                return UploadResult(
                    video_id="",
                    title=metadata.title,
                    url="",
                    status="failed",
                    error_message=(
                        "File changed after scanning; skipped to avoid uploading "
                        "an incomplete or different file."
                    ),
                )
            body = self._build_request_body(metadata)

            media = MediaFileUpload(
                str(metadata.filepath),
                chunksize=self.chunk_size,
                resumable=True,
                # Let the library detect the MIME type from the file extension.
                mimetype=None,
            )

            parts = ["snippet", "status", "recordingDetails"]
            if "paidProductPlacementDetails" in body:
                parts.append("paidProductPlacementDetails")
            request = self.service.videos().insert(
                part=",".join(parts),
                body=body,
                media_body=media,
                notifySubscribers=metadata.upload_settings.notify_subscribers,
            )

            if resume_uri:
                # google-api-python-client exposes the URI publicly but requires
                # its error-state flag to trigger a server progress query before
                # sending more bytes after a process restart.
                request.resumable_uri = resume_uri
                request._in_error_state = True

            return self._execute_resumable_upload(
                request,
                metadata,
                progress_callback,
                session_callback,
            )

        except FileNotFoundError:
            return UploadResult(
                video_id="",
                title=metadata.title,
                url="",
                status="failed",
                error_message=f"File not found: {metadata.filepath}",
            )
        except Exception as e:
            logger.exception(f"Unexpected error uploading {metadata.title}")
            return UploadResult(
                video_id="",
                title=metadata.title,
                url="",
                status="failed",
                error_message=f"Unexpected error: {str(e)}",
            )

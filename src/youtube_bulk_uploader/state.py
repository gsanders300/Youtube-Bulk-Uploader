"""Persistent upload state used to make batch runs restart-safe."""

import hashlib
import json
import os
import sqlite3
from dataclasses import asdict
from pathlib import Path

from .metadata import VideoMetadata
from .utils import get_data_dir


def get_default_ledger_path() -> Path:
    """Return the platform-appropriate upload ledger path."""
    return get_data_dir() / "uploads.sqlite3"


class UploadLedger:
    """Small SQLite ledger recording attempts and successful uploads."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else get_default_ledger_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=30)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA busy_timeout=30000")
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS uploads (
                path TEXT NOT NULL,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                title TEXT NOT NULL,
                status TEXT NOT NULL,
                video_id TEXT NOT NULL DEFAULT '',
                url TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',
                session_uri TEXT NOT NULL DEFAULT '',
                session_signature TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (path, size, mtime_ns)
            )
            """
        )
        columns = {
            str(row[1]) for row in self.connection.execute("PRAGMA table_info(uploads)")
        }
        if "session_uri" not in columns:
            self.connection.execute(
                "ALTER TABLE uploads ADD COLUMN session_uri TEXT NOT NULL DEFAULT ''"
            )
        if "session_signature" not in columns:
            self.connection.execute(
                "ALTER TABLE uploads "
                "ADD COLUMN session_signature TEXT NOT NULL DEFAULT ''"
            )
        self.connection.commit()

    @staticmethod
    def _identity(metadata: VideoMetadata) -> tuple[str, int, int]:
        resolved_path = str(metadata.filepath.resolve(strict=False))
        return (
            os.path.normcase(resolved_path) if os.name == "nt" else resolved_path,
            metadata.file_size,
            metadata.file_mtime_ns,
        )

    def successful_url(self, metadata: VideoMetadata) -> str | None:
        """Return a prior successful URL for the exact file version."""
        row = self.connection.execute(
            """
            SELECT url FROM uploads
            WHERE path = ? AND size = ? AND mtime_ns = ? AND status = 'success'
            """,
            self._identity(metadata),
        ).fetchone()
        return str(row[0]) if row else None

    @staticmethod
    def _metadata_signature(metadata: VideoMetadata) -> str:
        """Hash request metadata because it is fixed when a session is created."""
        payload = json.dumps(
            {
                "title": metadata.title,
                "description": metadata.description,
                "tags": metadata.tags,
                "creation_date": metadata.creation_date.isoformat(),
                "privacy_status": metadata.privacy_status,
                "upload_settings": asdict(metadata.upload_settings),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def record_started(self, metadata: VideoMetadata) -> None:
        """Record that an upload attempt began."""
        self._record(metadata, "uploading")

    def resumable_uri(self, metadata: VideoMetadata) -> str | None:
        """Return an unfinished resumable-session URI for this file version."""
        row = self.connection.execute(
            """
            SELECT session_uri FROM uploads
            WHERE path = ? AND size = ? AND mtime_ns = ?
              AND status != 'success' AND session_uri != ''
              AND session_signature = ?
            """,
            (*self._identity(metadata), self._metadata_signature(metadata)),
        ).fetchone()
        return str(row[0]) if row else None

    def record_session(self, metadata: VideoMetadata, session_uri: str) -> None:
        """Persist or clear the server-side resumable-session URI."""
        identity = self._identity(metadata)
        self.connection.execute(
            """
            INSERT INTO uploads (
                path, size, mtime_ns, title, status,
                session_uri, session_signature
            ) VALUES (?, ?, ?, ?, 'uploading', ?, ?)
            ON CONFLICT(path, size, mtime_ns) DO UPDATE SET
                session_uri = excluded.session_uri,
                session_signature = excluded.session_signature,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                *identity,
                metadata.title,
                session_uri,
                self._metadata_signature(metadata) if session_uri else "",
            ),
        )
        self.connection.commit()

    def record_result(
        self,
        metadata: VideoMetadata,
        status: str,
        video_id: str = "",
        url: str = "",
        error: str = "",
    ) -> None:
        """Persist an upload result immediately."""
        self._record(metadata, status, video_id, url, error)

    def _record(
        self,
        metadata: VideoMetadata,
        status: str,
        video_id: str = "",
        url: str = "",
        error: str = "",
    ) -> None:
        identity = self._identity(metadata)
        self.connection.execute(
            """
            INSERT INTO uploads (
                path, size, mtime_ns, title, status, video_id, url, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path, size, mtime_ns) DO UPDATE SET
                title = excluded.title,
                status = CASE
                    WHEN uploads.status = 'success' AND excluded.status != 'success'
                    THEN uploads.status ELSE excluded.status END,
                video_id = CASE
                    WHEN uploads.status = 'success' AND excluded.status != 'success'
                    THEN uploads.video_id ELSE excluded.video_id END,
                url = CASE
                    WHEN uploads.status = 'success' AND excluded.status != 'success'
                    THEN uploads.url ELSE excluded.url END,
                error = CASE
                    WHEN uploads.status = 'success' AND excluded.status != 'success'
                    THEN uploads.error ELSE excluded.error END,
                session_uri = CASE
                    WHEN excluded.status = 'success' THEN ''
                    ELSE uploads.session_uri END,
                session_signature = CASE
                    WHEN excluded.status = 'success' THEN ''
                    ELSE uploads.session_signature END,
                updated_at = CURRENT_TIMESTAMP
            """,
            (*identity, metadata.title, status, video_id, url, error),
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "UploadLedger":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

"""Reliability, restart-safety, and cross-platform regression tests."""

import json
import subprocess
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner
from googleapiclient.errors import HttpError

UTC = UTC


def utc_date(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=UTC)


def make_metadata(tmp_path):
    from youtube_bulk_uploader.metadata import VideoMetadata

    video = tmp_path / "video.mp4"
    video.write_bytes(b"video data")
    return VideoMetadata.from_file(video, use_exiftool=False)


@pytest.mark.parametrize(
    "arguments",
    [
        ["--help"],
        ["--version"],
        ["auth", "--help"],
        ["doctor", "--help"],
        ["exiftool", "--help"],
        ["scan", "--help"],
        ["sync-dates", "--help"],
        ["upload", "--help"],
    ],
)
def test_documented_cli_commands_are_available(arguments):
    from youtube_bulk_uploader.main import cli

    result = CliRunner().invoke(cli, arguments)

    assert result.exit_code == 0


class TestDateSelection:
    def test_uses_field_priority_instead_of_earliest_value(self):
        from youtube_bulk_uploader.exiftool import select_recording_date

        selected, source, field = select_recording_date(
            {
                "DateTimeOriginal": utc_date(2022, 1, 1),
                "CreateDate": utc_date(2020, 1, 1),
            },
            utc_date(2019, 1, 1),
        )

        assert selected == utc_date(2022, 1, 1)
        assert source == "exiftool"
        assert field == "DateTimeOriginal"

    def test_exiftool_filesystem_fields_are_only_fallback_data(self):
        from youtube_bulk_uploader.exiftool import select_recording_date

        fs_date = utc_date(2023, 5, 1)
        selected, source, field = select_recording_date(
            {"FileCreateDate": utc_date(2010, 1, 1)}, fs_date
        )

        assert selected == fs_date
        assert source == "filesystem"
        assert field == "filesystem"

    def test_naive_metadata_date_is_stamped_as_utc(self):
        from youtube_bulk_uploader.exiftool import select_recording_date

        naive = datetime(2020, 2, 3, 4, 5)
        selected, source, _field = select_recording_date(
            {"CreateDate": naive}, utc_date(2024, 1, 1)
        )

        assert source == "exiftool"
        assert selected.tzinfo is UTC


class TestExiftoolBatchReliability:
    def test_later_batch_survives_an_earlier_timeout(self, tmp_path):
        from youtube_bulk_uploader.exiftool import get_batch_exiftool_dates

        first = tmp_path / "first.mp4"
        second = tmp_path / "second.mp4"
        first.write_bytes(b"1")
        second.write_bytes(b"2")
        payload = json.dumps(
            [
                {
                    "SourceFile": str(second.absolute()),
                    "CreateDate": "2022:04:05 12:00:00",
                }
            ]
        )

        with patch(
            "youtube_bulk_uploader.exiftool.find_exiftool", return_value="exiftool"
        ):
            with patch("youtube_bulk_uploader.exiftool._MAX_FILES_PER_CALL", 1):
                with patch(
                    "youtube_bulk_uploader.exiftool.subprocess.run",
                    side_effect=[
                        subprocess.TimeoutExpired("exiftool", 30),
                        MagicMock(returncode=0, stdout=payload, stderr=""),
                    ],
                ):
                    result = get_batch_exiftool_dates([first, second])

        assert str(second.absolute()) in result


class TestUploadLedger:
    def test_success_survives_restart_and_later_failure(self, tmp_path):
        from youtube_bulk_uploader.state import UploadLedger

        metadata = make_metadata(tmp_path)
        database = tmp_path / "ledger.sqlite3"
        with UploadLedger(database) as ledger:
            ledger.record_result(
                metadata,
                "success",
                video_id="abc",
                url="https://youtu.be/abc",
            )

        with UploadLedger(database) as ledger:
            assert ledger.successful_url(metadata) == "https://youtu.be/abc"
            ledger.record_result(metadata, "failed", error="temporary failure")
            assert ledger.successful_url(metadata) == "https://youtu.be/abc"

    def test_changed_file_version_is_not_considered_uploaded(self, tmp_path):
        from youtube_bulk_uploader.metadata import VideoMetadata
        from youtube_bulk_uploader.state import UploadLedger

        original = make_metadata(tmp_path)
        database = tmp_path / "ledger.sqlite3"
        with UploadLedger(database) as ledger:
            ledger.record_result(original, "success", url="https://youtu.be/abc")
            original.filepath.write_bytes(b"different and longer video data")
            changed = VideoMetadata.from_file(original.filepath, use_exiftool=False)
            assert ledger.successful_url(changed) is None

    def test_resumable_session_survives_restart_and_clears_on_success(self, tmp_path):
        from youtube_bulk_uploader.state import UploadLedger

        metadata = make_metadata(tmp_path)
        database = tmp_path / "ledger.sqlite3"
        with UploadLedger(database) as ledger:
            ledger.record_started(metadata)
            ledger.record_session(metadata, "https://upload.example/session")

        with UploadLedger(database) as ledger:
            assert ledger.resumable_uri(metadata) == "https://upload.example/session"
            ledger.record_result(metadata, "success", url="https://youtu.be/abc")
            assert ledger.resumable_uri(metadata) is None

    def test_session_is_not_resumed_when_request_metadata_changes(self, tmp_path):
        from youtube_bulk_uploader.state import UploadLedger

        metadata = make_metadata(tmp_path)
        database = tmp_path / "ledger.sqlite3"
        with UploadLedger(database) as ledger:
            ledger.record_started(metadata)
            ledger.record_session(metadata, "https://upload.example/session")
            metadata.title = "A corrected title"
            assert ledger.resumable_uri(metadata) is None

    def test_session_is_not_resumed_when_upload_settings_change(self, tmp_path):
        from youtube_bulk_uploader.metadata import UploadSettings
        from youtube_bulk_uploader.state import UploadLedger

        metadata = make_metadata(tmp_path)
        database = tmp_path / "ledger.sqlite3"
        with UploadLedger(database) as ledger:
            ledger.record_started(metadata)
            ledger.record_session(metadata, "https://upload.example/session")
            metadata.upload_settings = UploadSettings(contains_synthetic_media=True)
            assert ledger.resumable_uri(metadata) is None


class TestUploaderReliability:
    @staticmethod
    def _http_error(status: int, reason: str) -> HttpError:
        response = MagicMock(status=status, reason="error")
        content = json.dumps(
            {"error": {"errors": [{"reason": reason}], "code": status}}
        ).encode()
        return HttpError(response, content)

    def test_structured_quota_reason_stops_upload(self, tmp_path):
        from youtube_bulk_uploader.uploader import YouTubeUploader

        request = MagicMock()
        request.next_chunk.side_effect = self._http_error(403, "quotaExceeded")
        result = YouTubeUploader(MagicMock())._execute_resumable_upload(
            request, make_metadata(tmp_path)
        )

        assert result.status == "quota_exceeded"

    def test_429_is_retried_then_succeeds(self, tmp_path):
        from youtube_bulk_uploader.uploader import YouTubeUploader

        request = MagicMock()
        request.next_chunk.side_effect = [
            self._http_error(429, "rateLimitExceeded"),
            (None, {"id": "video-id"}),
        ]
        with patch("youtube_bulk_uploader.uploader.random.uniform", return_value=0):
            with patch("youtube_bulk_uploader.uploader.time.sleep") as sleep:
                result = YouTubeUploader(MagicMock())._execute_resumable_upload(
                    request, make_metadata(tmp_path)
                )

        assert result.success
        assert result.video_id == "video-id"
        sleep.assert_called_once_with(0)

    def test_changed_file_is_rejected_before_api_request(self, tmp_path):
        from youtube_bulk_uploader.uploader import YouTubeUploader

        metadata = make_metadata(tmp_path)
        metadata.filepath.write_bytes(b"changed after the metadata scan")
        service = MagicMock()
        result = YouTubeUploader(service).upload_video(metadata)

        assert result.status == "failed"
        assert "changed after scanning" in (result.error_message or "")
        service.videos.assert_not_called()

    def test_missing_video_id_is_reported_as_failure(self, tmp_path):
        from youtube_bulk_uploader.uploader import YouTubeUploader

        request = MagicMock()
        request.next_chunk.return_value = (None, {})
        result = YouTubeUploader(MagicMock())._execute_resumable_upload(
            request, make_metadata(tmp_path)
        )

        assert result.status == "failed"
        assert "without a video ID" in (result.error_message or "")

    def test_expired_resumable_session_is_replaced(self, tmp_path):
        from youtube_bulk_uploader.uploader import YouTubeUploader

        request = MagicMock()
        request.resumable_uri = "https://upload.example/expired"
        request.next_chunk.side_effect = [
            self._http_error(404, "notFound"),
            (None, {"id": "video-id"}),
        ]
        sessions = []
        with patch("youtube_bulk_uploader.uploader.random.uniform", return_value=0):
            with patch("youtube_bulk_uploader.uploader.time.sleep"):
                result = YouTubeUploader(MagicMock())._execute_resumable_upload(
                    request,
                    make_metadata(tmp_path),
                    session_callback=sessions.append,
                )

        assert result.success
        assert sessions == [""]

    def test_upload_video_restores_persisted_session(self, tmp_path):
        from youtube_bulk_uploader.uploader import UploadResult, YouTubeUploader

        metadata = make_metadata(tmp_path)
        request = MagicMock()
        service = MagicMock()
        service.videos.return_value.insert.return_value = request
        uploader = YouTubeUploader(service)
        completed = UploadResult("id", metadata.title, "url", "success")

        with patch.object(
            uploader, "_execute_resumable_upload", return_value=completed
        ) as execute:
            result = uploader.upload_video(
                metadata, resume_uri="https://upload.example/session"
            )

        assert result is completed
        assert request.resumable_uri == "https://upload.example/session"
        assert request._in_error_state is True
        execute.assert_called_once()

    def test_upload_passes_subscriber_and_paid_promotion_settings(self, tmp_path):
        from youtube_bulk_uploader.metadata import UploadSettings
        from youtube_bulk_uploader.uploader import UploadResult, YouTubeUploader

        metadata = make_metadata(tmp_path)
        metadata.upload_settings = UploadSettings(
            has_paid_product_placement=True,
            notify_subscribers=False,
        )
        request = MagicMock()
        service = MagicMock()
        service.videos.return_value.insert.return_value = request
        uploader = YouTubeUploader(service)
        completed = UploadResult("id", metadata.title, "url", "success")

        with patch.object(
            uploader, "_execute_resumable_upload", return_value=completed
        ):
            result = uploader.upload_video(metadata)

        assert result is completed
        request_call = service.videos.return_value.insert.call_args
        assert request_call.kwargs["notifySubscribers"] is False
        assert "paidProductPlacementDetails" in request_call.kwargs["part"]


class TestPortableConfiguration:
    def test_config_and_data_directories_can_be_overridden(self, tmp_path):
        from youtube_bulk_uploader.utils import get_config_dir, get_data_dir

        config = tmp_path / "config"
        data = tmp_path / "data"
        with patch.dict(
            "os.environ",
            {
                "YOUTUBE_UPLOADER_CONFIG_DIR": str(config),
                "YOUTUBE_UPLOADER_DATA_DIR": str(data),
            },
        ):
            assert get_config_dir() == config
            assert get_data_dir() == data

    def test_cli_rejects_nonpositive_tuning_values(self):
        from youtube_bulk_uploader.main import cli

        runner = CliRunner()
        assert runner.invoke(cli, ["scan", "--speed", "0"]).exit_code != 0
        assert runner.invoke(cli, ["upload", "--chunk-size", "0"]).exit_code != 0
        assert runner.invoke(cli, ["upload", "--category-id", "0"]).exit_code != 0

    def test_upload_help_lists_current_youtube_disclosures(self):
        from youtube_bulk_uploader.main import cli

        result = CliRunner().invoke(cli, ["upload", "--help"])

        assert result.exit_code == 0
        assert "--audience" in result.output
        assert "--altered-content" in result.output
        assert "--paid-promotion" in result.output
        assert "--notify-subscribers" in result.output
        assert "--allow-embedding" in result.output
        assert "[default: no-embedding]" in result.output

    def test_token_is_written_atomically(self, tmp_path):
        from youtube_bulk_uploader.auth import save_token

        token_path = tmp_path / "nested" / "token.json"
        credentials = MagicMock()
        credentials.to_json.return_value = '{"token": "secret"}'

        save_token(credentials, token_path)

        assert token_path.read_text(encoding="utf-8") == '{"token": "secret"}'
        assert list(token_path.parent.glob(".token.json.*")) == []

    def test_doctor_accepts_existing_token_without_client_file(self, tmp_path):
        from youtube_bulk_uploader.main import cli

        token = tmp_path / "config" / "token.json"
        token.parent.mkdir()
        token.write_text("{}", encoding="utf-8")
        missing_credentials = token.parent / "client_secrets.json"

        with patch(
            "youtube_bulk_uploader.main.get_config_dir", return_value=token.parent
        ):
            with patch(
                "youtube_bulk_uploader.main.get_data_dir",
                return_value=tmp_path / "data",
            ):
                with patch(
                    "youtube_bulk_uploader.main.get_default_token_path",
                    return_value=token,
                ):
                    with patch(
                        "youtube_bulk_uploader.main.get_default_credentials_path",
                        return_value=missing_credentials,
                    ):
                        with patch(
                            "youtube_bulk_uploader.main.find_exiftool",
                            return_value=None,
                        ):
                            result = CliRunner().invoke(cli, ["doctor"])

        assert result.exit_code == 0, result.output


class TestVideoValidation:
    def test_empty_video_is_rejected(self, tmp_path):
        from youtube_bulk_uploader.video_processor import validate_videos

        video = tmp_path / "empty.mp4"
        video.touch()
        valid, invalid = validate_videos([video])

        assert valid == []
        assert invalid == [(video, "File is empty")]

    def test_metadata_preparation_uses_one_batch_exiftool_call(self, tmp_path):
        from youtube_bulk_uploader.video_processor import prepare_video_batch

        videos = []
        dates = {}
        for index in range(3):
            video = tmp_path / f"video-{index}.mp4"
            video.write_bytes(b"video")
            videos.append(video)
            dates[str(video.absolute())] = {"CreateDate": utc_date(2020, 1, 1)}

        with patch(
            "youtube_bulk_uploader.video_processor.get_batch_exiftool_dates",
            return_value=dates,
        ) as batch:
            metadata = prepare_video_batch(videos, use_exiftool=True)

        batch.assert_called_once_with(videos)
        assert len(metadata) == 3
        assert all(item.date_source == "exiftool" for item in metadata)

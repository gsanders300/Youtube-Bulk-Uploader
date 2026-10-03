"""
Comprehensive validation tests for core metadata, upload, and CLI behavior.

Coverage areas:
  - utils:        get_project_root, get_video_extensions, sanitize_filename,
                  extract_tags_from_filename, format_file_size, format_duration
  - exiftool:     _parse_exiftool_date, _filesystem_date, _safe_parse_json,
                  get_batch_exiftool_dates (mocked), batch_update_file_dates,
                  update_file_dates, get_best_date
  - metadata:     VideoMetadata.from_file (prefetched & fallback paths),
                  generate_description, get_file_creation_date
  - video_processor: discover_videos, get_batch_summary, prepare_video_batch
  - uploader:     UploadResult.success, _build_request_body
  - main helpers: _truncate, _estimate_upload_seconds, _UploadState
"""

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

UTC = UTC


def dt(year, month, day, hour=0, minute=0, second=0):
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


# ---------------------------------------------------------------------------
# utils
# ---------------------------------------------------------------------------


class TestGetProjectRoot:
    def test_returns_path_with_pyproject_toml(self):
        import youtube_bulk_uploader.utils as utils_mod
        from youtube_bulk_uploader.utils import get_project_root

        # Reset cache so we exercise the walk path
        utils_mod._project_root = None
        root = get_project_root()
        assert (root / "pyproject.toml").exists(), "pyproject.toml not found at root"

    def test_cached_on_second_call(self):
        import youtube_bulk_uploader.utils as utils_mod

        utils_mod._project_root = None
        r1 = utils_mod.get_project_root()
        r2 = utils_mod.get_project_root()
        assert r1 is r2, "Second call should return the same cached object"

    def test_returns_path_instance(self):
        from youtube_bulk_uploader.utils import get_project_root

        assert isinstance(get_project_root(), Path)


class TestGetVideoExtensions:
    def test_contains_expected_formats(self):
        from youtube_bulk_uploader.utils import get_video_extensions

        exts = get_video_extensions()
        for ext in [".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".wmv", ".flv"]:
            assert ext in exts

    def test_returns_frozenset(self):
        from youtube_bulk_uploader.utils import get_video_extensions

        assert isinstance(get_video_extensions(), frozenset)

    def test_same_object_on_repeated_calls(self):
        from youtube_bulk_uploader.utils import get_video_extensions

        assert get_video_extensions() is get_video_extensions()

    def test_extension_matching_case_sensitive(self):
        from youtube_bulk_uploader.utils import get_video_extensions

        exts = get_video_extensions()
        assert ".mov" in exts
        assert ".MOV" not in exts  # discover_videos lowercases before checking


class TestSanitizeFilename:
    def test_strips_extension(self):
        from youtube_bulk_uploader.utils import sanitize_filename

        assert sanitize_filename("my_video.mp4") == "my video"

    def test_replaces_separators(self):
        from youtube_bulk_uploader.utils import sanitize_filename

        assert sanitize_filename("my-video_file.name.mp4") == "my video file name"

    def test_collapses_spaces(self):
        from youtube_bulk_uploader.utils import sanitize_filename

        assert sanitize_filename("my  video.mp4") == "my video"

    def test_strips_whitespace(self):
        from youtube_bulk_uploader.utils import sanitize_filename

        assert sanitize_filename("  video  .mp4") == "video"

    def test_truncates_to_100_chars(self):
        from youtube_bulk_uploader.utils import sanitize_filename

        long_name = "a" * 150 + ".mp4"
        result = sanitize_filename(long_name)
        assert len(result) <= 100

    def test_empty_stem_becomes_untitled(self):
        from youtube_bulk_uploader.utils import sanitize_filename

        # Path("video.mp4").stem = "video" → "video"
        # The Untitled Video fallback is for genuinely empty stems after cleaning,
        # e.g. a filename that is all separators: "-.mp4" → stem "-" → cleaned ""
        result = sanitize_filename("-.mp4")
        assert result == "Untitled Video"

    def test_no_extension(self):
        from youtube_bulk_uploader.utils import sanitize_filename

        assert sanitize_filename("video") == "video"


class TestExtractTagsFromFilename:
    def test_splits_on_separators(self):
        from youtube_bulk_uploader.utils import extract_tags_from_filename

        # "my" and "video" are stop words and are correctly filtered out
        tags = extract_tags_from_filename("my-video_file.mp4")
        assert "file" in tags
        assert "my" not in tags  # "my" is a stop word
        assert "video" not in tags  # "video" is a stop word

    def test_filters_stop_words(self):
        from youtube_bulk_uploader.utils import extract_tags_from_filename

        tags = extract_tags_from_filename("a video and the sun.mp4")
        assert "a" not in tags
        assert "and" not in tags
        assert "the" not in tags
        assert "video" not in tags  # "video" is in STOP_WORDS
        assert "sun" in tags

    def test_filters_short_tokens(self):
        from youtube_bulk_uploader.utils import extract_tags_from_filename

        # The filter is `len(token) < 2`, so tokens of length 1 are filtered,
        # but tokens of length 2 ("ok") are allowed through.
        tags = extract_tags_from_filename("a ok big.mp4")
        assert "a" not in tags  # len 1 — filtered
        assert "ok" in tags  # len 2 — kept
        assert "big" in tags  # len 3 — kept

    def test_filters_pure_numbers(self):
        from youtube_bulk_uploader.utils import extract_tags_from_filename

        tags = extract_tags_from_filename("clip123 456.mp4")
        assert "456" not in tags

    def test_respects_char_limit(self):
        from youtube_bulk_uploader.utils import extract_tags_from_filename

        # Generate a filename with many long tags
        name = "-".join(["longword"] * 100) + ".mp4"
        tags = extract_tags_from_filename(name, max_total_chars=50)
        total = sum(len(t) + 1 for t in tags)
        assert total <= 51  # 50 + 1 slack for last tag


class TestFormatFileSize:
    def test_bytes(self):
        from youtube_bulk_uploader.utils import format_file_size

        assert format_file_size(512) == "512.0 B"

    def test_kilobytes(self):
        from youtube_bulk_uploader.utils import format_file_size

        assert format_file_size(1024) == "1.0 KB"

    def test_megabytes(self):
        from youtube_bulk_uploader.utils import format_file_size

        assert format_file_size(1024 * 1024) == "1.0 MB"

    def test_gigabytes(self):
        from youtube_bulk_uploader.utils import format_file_size

        assert "GB" in format_file_size(1024**3)


class TestFormatDuration:
    def test_seconds_only(self):
        from youtube_bulk_uploader.utils import format_duration

        assert format_duration(45) == "45s"

    def test_minutes_and_seconds(self):
        from youtube_bulk_uploader.utils import format_duration

        assert format_duration(90) == "1m 30s"

    def test_hours_minutes_seconds(self):
        from youtube_bulk_uploader.utils import format_duration

        assert format_duration(3661) == "1h 1m 1s"


# ---------------------------------------------------------------------------
# exiftool — _parse_exiftool_date (the core bug fix)
# ---------------------------------------------------------------------------


class TestParseExiftoolDate:
    def _parse(self, s):
        from youtube_bulk_uploader.exiftool import _parse_exiftool_date

        return _parse_exiftool_date(s)

    def test_colon_separated_date_parses_correctly(self):
        """YYYY:MM:DD must not be confused with today's date."""
        result = self._parse("2026:05:15 11:41:48")
        assert result is not None
        assert result.year == 2026
        assert result.month == 5
        assert result.day == 15
        assert result.hour == 11
        assert result.minute == 41

    def test_naive_datetime_stamped_as_utc(self):
        """No timezone offset → replace(tzinfo=UTC), NOT astimezone (which shifts)."""
        result = self._parse("2026:05:15 11:41:48")
        assert result is not None
        assert result.tzinfo == UTC
        # Hour must remain 11, not shifted by local offset
        assert result.hour == 11

    def test_aware_datetime_converted_to_utc(self):
        """A string with -04:00 offset should be converted: 18:00-04:00 → 22:00 UTC."""
        result = self._parse("2026:06:07 18:58:57-04:00")
        assert result is not None
        assert result.tzinfo == UTC
        assert result.hour == 22

    def test_zulu_suffix(self):
        result = self._parse("2026:05:15 11:39:59Z")
        assert result is not None
        assert result.year == 2026
        assert result.month == 5
        assert result.day == 15
        assert result.hour == 11

    def test_invalid_string_returns_none(self):
        result = self._parse("not-a-date")
        assert result is None

    def test_does_not_use_today_for_month_day(self):
        """Regression: dateutil used to substitute today's month/day."""
        today = datetime.now()
        result = self._parse("2024:03:20 09:00:00")
        assert result is not None
        # The parsed month/day must be March 20, not today
        assert result.month == 3
        assert result.day == 20
        # And definitely not today's month/day if they differ
        if today.month != 3 or today.day != 20:
            assert result.month != today.month or result.day != today.day


# ---------------------------------------------------------------------------
# exiftool — _filesystem_date
# ---------------------------------------------------------------------------


class TestFilesystemDate:
    def test_returns_aware_utc_datetime(self, tmp_path):
        from youtube_bulk_uploader.exiftool import _filesystem_date

        f = tmp_path / "test.txt"
        f.write_text("x")
        result = _filesystem_date(f)
        assert result.tzinfo is not None
        assert result.tzinfo == UTC

    def test_accepts_precomputed_stat(self, tmp_path):
        from youtube_bulk_uploader.exiftool import _filesystem_date

        f = tmp_path / "test.txt"
        f.write_text("x")
        stat = f.stat()
        result_with_stat = _filesystem_date(f, stat_result=stat)
        result_without = _filesystem_date(f)
        assert result_with_stat == result_without

    def test_stat_result_none_falls_back_to_stat(self, tmp_path):
        from youtube_bulk_uploader.exiftool import _filesystem_date

        f = tmp_path / "test.txt"
        f.write_text("hello")
        result = _filesystem_date(f, stat_result=None)
        assert isinstance(result, datetime)
        assert result.tzinfo == UTC


# ---------------------------------------------------------------------------
# exiftool — _safe_parse_json
# ---------------------------------------------------------------------------


class TestSafeParseJson:
    def _parse(self, text):
        from youtube_bulk_uploader.exiftool import _safe_parse_json

        return _safe_parse_json(text)

    def test_valid_list(self):
        result = self._parse('[{"key": "val"}]')
        assert result == [{"key": "val"}]

    def test_empty_list(self):
        assert self._parse("[]") == []

    def test_invalid_json_returns_empty(self):
        assert self._parse("not json") == []

    def test_non_list_json_returns_empty(self):
        # ExifTool always returns a list; a dict at top level is malformed output
        assert self._parse('{"key": "val"}') == []

    def test_null_returns_empty(self):
        assert self._parse("null") == []


# ---------------------------------------------------------------------------
# exiftool — get_batch_exiftool_dates (mocked subprocess)
# ---------------------------------------------------------------------------


class TestGetBatchExiftoolDates:
    def test_returns_dict_keyed_by_absolute_path(self, tmp_path):
        from youtube_bulk_uploader.exiftool import get_batch_exiftool_dates

        f = tmp_path / "video.mp4"
        f.write_text("fake")
        json_output = f"""[{{
            "SourceFile": "{f.as_posix()}",
            "CreateDate": "2025:03:10 12:00:00"
        }}]"""
        with patch("youtube_bulk_uploader.exiftool.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout=json_output, stderr=""
            )
            with patch(
                "youtube_bulk_uploader.exiftool.find_exiftool", return_value="exiftool"
            ):
                result = get_batch_exiftool_dates([f])
        key = str(f.absolute())
        assert key in result
        dates = result[key]
        assert "CreateDate" in dates
        cd = dates["CreateDate"]
        assert cd.year == 2025
        assert cd.month == 3
        assert cd.day == 10

    def test_empty_file_list_returns_empty_dict(self):
        from youtube_bulk_uploader.exiftool import get_batch_exiftool_dates

        assert get_batch_exiftool_dates([]) == {}

    def test_no_exiftool_returns_empty_dict(self, tmp_path):
        from youtube_bulk_uploader.exiftool import get_batch_exiftool_dates

        f = tmp_path / "v.mp4"
        f.write_text("x")
        with patch("youtube_bulk_uploader.exiftool.find_exiftool", return_value=None):
            assert get_batch_exiftool_dates([f]) == {}

    def test_subprocess_failure_returns_empty(self, tmp_path):
        from youtube_bulk_uploader.exiftool import get_batch_exiftool_dates

        f = tmp_path / "v.mp4"
        f.write_text("x")
        with patch("youtube_bulk_uploader.exiftool.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="err")
            with patch(
                "youtube_bulk_uploader.exiftool.find_exiftool", return_value="exiftool"
            ):
                assert get_batch_exiftool_dates([f]) == {}


# ---------------------------------------------------------------------------
# exiftool — get_best_date
# ---------------------------------------------------------------------------


class TestGetBestDate:
    def test_respects_field_priority(self, tmp_path):
        from youtube_bulk_uploader.exiftool import get_best_date

        f = tmp_path / "v.mp4"
        f.write_text("x")
        preferred = dt(2023, 6, 15)
        lower_priority = dt(2020, 1, 1)
        with patch(
            "youtube_bulk_uploader.exiftool.get_exiftool_dates",
            return_value={
                "CreateDate": lower_priority,
                "DateTimeOriginal": preferred,
            },
        ):
            with patch(
                "youtube_bulk_uploader.exiftool._filesystem_date",
                return_value=dt(2024, 1, 1),
            ):
                result = get_best_date(f)
        assert result == preferred

    def test_no_exif_returns_fs_date(self, tmp_path):
        from youtube_bulk_uploader.exiftool import get_best_date

        f = tmp_path / "v.mp4"
        f.write_text("x")
        fs = dt(2022, 5, 10)
        with patch(
            "youtube_bulk_uploader.exiftool.get_exiftool_dates", return_value={}
        ):
            with patch(
                "youtube_bulk_uploader.exiftool._filesystem_date", return_value=fs
            ):
                result = get_best_date(f)
        assert result == fs

    def test_picks_exif_over_later_fs_date(self, tmp_path):
        from youtube_bulk_uploader.exiftool import get_best_date

        f = tmp_path / "v.mp4"
        f.write_text("x")
        exif_date = dt(2021, 3, 15)
        fs_date = dt(2024, 6, 7)  # today-ish (file copied recently)
        with patch(
            "youtube_bulk_uploader.exiftool.get_exiftool_dates",
            return_value={"CreateDate": exif_date},
        ):
            with patch(
                "youtube_bulk_uploader.exiftool._filesystem_date", return_value=fs_date
            ):
                result = get_best_date(f)
        assert result == exif_date


# ---------------------------------------------------------------------------
# exiftool — batch_update_file_dates
# ---------------------------------------------------------------------------


class TestBatchUpdateFileDates:
    def test_updates_mtime_for_each_file(self, tmp_path):
        from youtube_bulk_uploader.exiftool import batch_update_file_dates

        f1 = tmp_path / "a.mp4"
        f2 = tmp_path / "b.mp4"
        f1.write_text("x")
        f2.write_text("x")
        target = dt(2022, 6, 1, 12, 0, 0)
        with patch(
            "youtube_bulk_uploader.exiftool.platform.system", return_value="Linux"
        ):
            count = batch_update_file_dates([(f1, target), (f2, target)])
        assert count == 2
        for f in [f1, f2]:
            mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=UTC)
            assert abs((mtime - target).total_seconds()) < 2

    def test_empty_list_returns_zero(self):
        from youtube_bulk_uploader.exiftool import batch_update_file_dates

        assert batch_update_file_dates([]) == 0

    def test_windows_batches_by_date(self, tmp_path):
        """On Windows, files sharing the same date should be in one ExifTool call."""
        from youtube_bulk_uploader.exiftool import batch_update_file_dates

        f1 = tmp_path / "a.mp4"
        f2 = tmp_path / "b.mp4"
        f1.write_text("x")
        f2.write_text("x")
        target = dt(2022, 6, 1, 12, 0, 0)
        with patch(
            "youtube_bulk_uploader.exiftool.platform.system", return_value="Windows"
        ):
            with patch(
                "youtube_bulk_uploader.exiftool.find_exiftool", return_value="exiftool"
            ):
                with patch("youtube_bulk_uploader.exiftool.subprocess.run") as mock_run:
                    mock_run.return_value = MagicMock(
                        returncode=0, stdout="", stderr=""
                    )
                    batch_update_file_dates([(f1, target), (f2, target)])
        # Both files share the same date → exactly 1 ExifTool subprocess call
        assert mock_run.call_count == 1
        call_args = mock_run.call_args[0][0]
        # Both file paths must appear in the single command
        assert str(f1) in call_args
        assert str(f2) in call_args

    def test_windows_separate_dates_get_separate_calls(self, tmp_path):
        from youtube_bulk_uploader.exiftool import batch_update_file_dates

        f1 = tmp_path / "a.mp4"
        f2 = tmp_path / "b.mp4"
        f1.write_text("x")
        f2.write_text("x")
        d1 = dt(2022, 1, 1)
        d2 = dt(2023, 1, 1)
        with patch(
            "youtube_bulk_uploader.exiftool.platform.system", return_value="Windows"
        ):
            with patch(
                "youtube_bulk_uploader.exiftool.find_exiftool", return_value="exiftool"
            ):
                with patch("youtube_bulk_uploader.exiftool.subprocess.run") as mock_run:
                    mock_run.return_value = MagicMock(
                        returncode=0, stdout="", stderr=""
                    )
                    batch_update_file_dates([(f1, d1), (f2, d2)])
        # Different dates → 2 ExifTool calls
        assert mock_run.call_count == 2


# ---------------------------------------------------------------------------
# metadata — generate_description
# ---------------------------------------------------------------------------


class TestGenerateDescription:
    def test_uses_stem(self):
        from youtube_bulk_uploader.metadata import generate_description

        assert generate_description("my_video.mp4") == "my_video"

    def test_custom_prefix_prepended(self):
        from youtube_bulk_uploader.metadata import generate_description

        result = generate_description("clip.mp4", custom_prefix="Family 2024")
        assert result.startswith("Family 2024\n\n")
        assert "clip" in result

    def test_truncates_to_5000(self):
        from youtube_bulk_uploader.metadata import generate_description

        long = "x" * 6000 + ".mp4"
        result = generate_description(long)
        assert len(result) <= 5000

    def test_prefix_stripped(self):
        from youtube_bulk_uploader.metadata import generate_description

        result = generate_description("v.mp4", custom_prefix="  Trip  ")
        assert result.startswith("Trip")

    def test_appends_summer_recording_time_in_eastern_time(self):
        from youtube_bulk_uploader.metadata import generate_description

        result = generate_description("clip.mp4", recording_date=dt(2026, 7, 1, 16))

        assert result.endswith("Recorded (Eastern): 2026-07-01 12:00:00 EDT")

    def test_appends_winter_recording_time_in_eastern_time(self):
        from youtube_bulk_uploader.metadata import generate_description

        result = generate_description("clip.mp4", recording_date=dt(2026, 1, 1, 17))

        assert result.endswith("Recorded (Eastern): 2026-01-01 12:00:00 EST")

    def test_treats_naive_recording_time_as_utc(self):
        from youtube_bulk_uploader.metadata import generate_description

        recording_date = datetime(2026, 7, 1, 16)
        result = generate_description("clip.mp4", recording_date=recording_date)

        assert result.endswith("Recorded (Eastern): 2026-07-01 12:00:00 EDT")

    def test_reserves_description_space_for_recording_time(self):
        from youtube_bulk_uploader.metadata import generate_description

        result = generate_description(
            "clip.mp4",
            custom_prefix="x" * 6000,
            recording_date=dt(2026, 7, 1, 16),
        )

        assert len(result) == 5000
        assert result.endswith("Recorded (Eastern): 2026-07-01 12:00:00 EDT")


# ---------------------------------------------------------------------------
# metadata — VideoMetadata.from_file
# ---------------------------------------------------------------------------


class TestVideoMetadataFromFile:
    def _make_file(self, tmp_path, name="video.mp4"):
        f = tmp_path / name
        f.write_text("fake video content")
        return f

    def test_from_file_with_prefetched_exif_picks_earliest(self, tmp_path):
        from youtube_bulk_uploader.metadata import VideoMetadata

        f = self._make_file(tmp_path)
        exif_early = dt(2021, 3, 10)
        exif_late = dt(2024, 1, 1)
        fs_date = dt(2026, 6, 7)  # filesystem = today (file was recently copied)
        with patch(
            "youtube_bulk_uploader.metadata._filesystem_date", return_value=fs_date
        ):
            meta = VideoMetadata.from_file(
                f,
                prefetched_exif_dates={
                    "CreateDate": exif_late,
                    "DateTimeOriginal": exif_early,
                },
            )
        assert meta.creation_date == exif_early
        assert meta.date_source == "exiftool"
        assert meta.exif_date == exif_early

    def test_from_file_exif_earlier_than_fs_wins(self, tmp_path):
        from youtube_bulk_uploader.metadata import VideoMetadata

        f = self._make_file(tmp_path)
        exif_date = dt(2020, 5, 15)
        fs_date = dt(2024, 6, 7)
        with patch(
            "youtube_bulk_uploader.metadata._filesystem_date", return_value=fs_date
        ):
            meta = VideoMetadata.from_file(
                f, prefetched_exif_dates={"CreateDate": exif_date}
            )
        assert meta.creation_date == exif_date
        assert meta.date_source == "exiftool"

    def test_from_file_exif_wins_even_when_fs_is_earlier(self, tmp_path):
        from youtube_bulk_uploader.metadata import VideoMetadata

        f = self._make_file(tmp_path)
        exif_date = dt(2024, 6, 7)
        fs_date = dt(2020, 1, 1)  # fs is earlier (unusual but possible)
        with patch(
            "youtube_bulk_uploader.metadata._filesystem_date", return_value=fs_date
        ):
            meta = VideoMetadata.from_file(
                f, prefetched_exif_dates={"CreateDate": exif_date}
            )
        assert meta.creation_date == exif_date
        assert meta.date_source == "exiftool"
        assert meta.date_field == "CreateDate"

    def test_from_file_empty_prefetched_uses_fs(self, tmp_path):
        from youtube_bulk_uploader.metadata import VideoMetadata

        f = self._make_file(tmp_path)
        fs_date = dt(2023, 3, 3)
        with patch(
            "youtube_bulk_uploader.metadata._filesystem_date", return_value=fs_date
        ):
            meta = VideoMetadata.from_file(f, prefetched_exif_dates={})
        assert meta.creation_date == fs_date
        assert meta.date_source == "filesystem"

    def test_from_file_no_prefetched_falls_back_to_per_file(self, tmp_path):
        from youtube_bulk_uploader.metadata import VideoMetadata

        f = self._make_file(tmp_path)
        exif_date = dt(2019, 7, 4)
        with patch(
            "youtube_bulk_uploader.metadata.get_best_date_details",
            return_value=(exif_date, "exiftool", "DateTimeOriginal"),
        ):
            with (
                patch(
                    "youtube_bulk_uploader.metadata._filesystem_date",
                    return_value=dt(2026, 1, 1),
                ),
                # Without this, the test depends on ExifTool being installed.
                patch(
                    "youtube_bulk_uploader.metadata.is_exiftool_available",
                    return_value=True,
                ),
            ):
                meta = VideoMetadata.from_file(f, prefetched_exif_dates=None)
        assert meta.creation_date == exif_date
        assert meta.date_source == "exiftool"

    def test_from_file_file_size_matches_actual(self, tmp_path):
        from youtube_bulk_uploader.metadata import VideoMetadata

        f = self._make_file(tmp_path)
        actual_size = f.stat().st_size
        with patch(
            "youtube_bulk_uploader.metadata._filesystem_date",
            return_value=dt(2023, 1, 1),
        ):
            meta = VideoMetadata.from_file(f, prefetched_exif_dates={})
        assert meta.file_size == actual_size

    def test_from_file_title_sanitized(self, tmp_path):
        from youtube_bulk_uploader.metadata import VideoMetadata

        f = self._make_file(tmp_path, "my_cool-video.mp4")
        with patch(
            "youtube_bulk_uploader.metadata._filesystem_date",
            return_value=dt(2023, 1, 1),
        ):
            meta = VideoMetadata.from_file(f, prefetched_exif_dates={})
        assert meta.title == "my cool video"

    def test_from_file_privacy_always_unlisted(self, tmp_path):
        from youtube_bulk_uploader.metadata import VideoMetadata

        f = self._make_file(tmp_path)
        with patch(
            "youtube_bulk_uploader.metadata._filesystem_date",
            return_value=dt(2023, 1, 1),
        ):
            meta = VideoMetadata.from_file(f, prefetched_exif_dates={})
        assert meta.privacy_status == "unlisted"

    def test_from_file_fs_date_stored_separately(self, tmp_path):
        from youtube_bulk_uploader.metadata import VideoMetadata

        f = self._make_file(tmp_path)
        fs_date = dt(2026, 6, 7)
        exif_date = dt(2021, 1, 1)
        with patch(
            "youtube_bulk_uploader.metadata._filesystem_date", return_value=fs_date
        ):
            meta = VideoMetadata.from_file(
                f, prefetched_exif_dates={"CreateDate": exif_date}
            )
        assert meta.fs_date == fs_date
        assert meta.creation_date == exif_date

    def test_from_file_exif_date_field_set_correctly(self, tmp_path):
        from youtube_bulk_uploader.metadata import VideoMetadata

        f = self._make_file(tmp_path)
        exif_date = dt(2021, 1, 1)
        with patch(
            "youtube_bulk_uploader.metadata._filesystem_date",
            return_value=dt(2026, 6, 7),
        ):
            meta = VideoMetadata.from_file(
                f, prefetched_exif_dates={"CreateDate": exif_date}
            )
        assert meta.exif_date == exif_date

    def test_from_file_no_exif_exif_date_is_none(self, tmp_path):
        from youtube_bulk_uploader.metadata import VideoMetadata

        f = self._make_file(tmp_path)
        with patch(
            "youtube_bulk_uploader.metadata._filesystem_date",
            return_value=dt(2023, 1, 1),
        ):
            meta = VideoMetadata.from_file(f, prefetched_exif_dates={})
        assert meta.exif_date is None

    def test_single_stat_call_performance(self, tmp_path):
        """from_file should issue exactly ONE stat() call (shared for size + date)."""
        from youtube_bulk_uploader.metadata import VideoMetadata

        f = self._make_file(tmp_path)
        f.stat()
        stat_calls = []

        original_stat = Path.stat

        def counting_stat(self_, *args, **kwargs):
            if self_ == f:
                stat_calls.append(1)
            return original_stat(self_, *args, **kwargs)

        with patch.object(Path, "stat", counting_stat):
            with patch(
                "youtube_bulk_uploader.metadata._filesystem_date",
                wraps=lambda p, stat_result=None: dt(2023, 1, 1),
            ):
                # The patched helper still accepts the precomputed stat result.
                # but we want to count how many times Path.stat is called on f
                pass

        # The real check: from_file calls filepath.stat() exactly once at the top
        stat_calls.clear()
        original_stat2 = Path.stat

        call_count = 0

        def mock_stat(self_, *args, **kwargs):
            nonlocal call_count
            if self_ == f:
                call_count += 1
            return original_stat2(self_, *args, **kwargs)

        with patch.object(Path, "stat", mock_stat):
            with patch(
                "youtube_bulk_uploader.metadata._filesystem_date",
                return_value=dt(2023, 1, 1),
            ):
                VideoMetadata.from_file(f, prefetched_exif_dates={})

        assert call_count == 1, f"Expected 1 stat() call, got {call_count}"


# ---------------------------------------------------------------------------
# metadata — get_file_creation_date
# ---------------------------------------------------------------------------


class TestGetFileCreationDate:
    def test_returns_exiftool_date_when_available(self, tmp_path):
        from youtube_bulk_uploader.metadata import get_file_creation_date

        f = tmp_path / "v.mp4"
        f.write_text("x")
        exif_date = dt(2020, 3, 15)
        with patch(
            "youtube_bulk_uploader.metadata.is_exiftool_available", return_value=True
        ):
            with patch(
                "youtube_bulk_uploader.metadata.get_best_date_details",
                return_value=(exif_date, "exiftool", "CreateDate"),
            ):
                result, source = get_file_creation_date(f)
        assert result == exif_date
        assert source == "exiftool"

    def test_falls_back_to_filesystem_when_no_exif(self, tmp_path):
        from youtube_bulk_uploader.metadata import get_file_creation_date

        f = tmp_path / "v.mp4"
        f.write_text("x")
        fs_date = dt(2023, 6, 1)
        with patch(
            "youtube_bulk_uploader.metadata.is_exiftool_available", return_value=True
        ):
            with patch(
                "youtube_bulk_uploader.metadata.get_best_date_details",
                return_value=(fs_date, "filesystem", "filesystem"),
            ):
                result, source = get_file_creation_date(f)
        assert result == fs_date
        assert source == "filesystem"

    def test_falls_back_when_exiftool_unavailable(self, tmp_path):
        from youtube_bulk_uploader.metadata import get_file_creation_date

        f = tmp_path / "v.mp4"
        f.write_text("x")
        fs_date = dt(2022, 4, 10)
        with patch(
            "youtube_bulk_uploader.metadata.is_exiftool_available", return_value=False
        ):
            with patch(
                "youtube_bulk_uploader.metadata._filesystem_date", return_value=fs_date
            ):
                result, source = get_file_creation_date(f, use_exiftool=True)
        assert result == fs_date
        assert source == "filesystem"

    def test_use_exiftool_false_skips_exiftool(self, tmp_path):
        from youtube_bulk_uploader.metadata import get_file_creation_date

        f = tmp_path / "v.mp4"
        f.write_text("x")
        fs_date = dt(2021, 9, 9)
        with patch(
            "youtube_bulk_uploader.metadata.is_exiftool_available"
        ) as mock_avail:
            with patch(
                "youtube_bulk_uploader.metadata._filesystem_date", return_value=fs_date
            ):
                result, source = get_file_creation_date(f, use_exiftool=False)
        mock_avail.assert_not_called()
        assert result == fs_date
        assert source == "filesystem"


# ---------------------------------------------------------------------------
# video_processor — discover_videos
# ---------------------------------------------------------------------------


class TestDiscoverVideos:
    def test_finds_video_files(self, tmp_path):
        from youtube_bulk_uploader.video_processor import discover_videos

        (tmp_path / "a.mp4").write_text("x")
        (tmp_path / "b.mov").write_text("x")
        (tmp_path / "c.txt").write_text("x")
        result = discover_videos(tmp_path)
        names = [p.name for p in result]
        assert "a.mp4" in names
        assert "b.mov" in names
        assert "c.txt" not in names

    def test_case_insensitive_extension(self, tmp_path):
        from youtube_bulk_uploader.video_processor import discover_videos

        (tmp_path / "clip.MOV").write_text("x")
        result = discover_videos(tmp_path)
        assert any(p.name == "clip.MOV" for p in result)

    def test_sorted_by_name(self, tmp_path):
        from youtube_bulk_uploader.video_processor import discover_videos

        (tmp_path / "z.mp4").write_text("x")
        (tmp_path / "a.mp4").write_text("x")
        (tmp_path / "m.mp4").write_text("x")
        result = discover_videos(tmp_path)
        names = [p.name for p in result]
        assert names == sorted(names, key=str.lower)

    def test_non_recursive_ignores_subdirs(self, tmp_path):
        from youtube_bulk_uploader.video_processor import discover_videos

        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "hidden.mp4").write_text("x")
        (tmp_path / "visible.mp4").write_text("x")
        result = discover_videos(tmp_path, recursive=False)
        names = [p.name for p in result]
        assert "visible.mp4" in names
        assert "hidden.mp4" not in names

    def test_recursive_finds_subdirectory_files(self, tmp_path):
        from youtube_bulk_uploader.video_processor import discover_videos

        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "deep.mp4").write_text("x")
        (tmp_path / "top.mp4").write_text("x")
        result = discover_videos(tmp_path, recursive=True)
        names = [p.name for p in result]
        assert "deep.mp4" in names
        assert "top.mp4" in names


# ---------------------------------------------------------------------------
# video_processor — get_batch_summary
# ---------------------------------------------------------------------------


class TestGetBatchSummary:
    def _make_meta(self, filepath, file_size, suffix=".mp4"):
        """Create a minimal VideoMetadata-like object."""
        from youtube_bulk_uploader.metadata import VideoMetadata

        m = MagicMock(spec=VideoMetadata)
        m.file_size = file_size
        p = MagicMock()
        p.suffix = suffix
        p.suffix.lower.return_value = suffix
        m.filepath = MagicMock()
        m.filepath.suffix = suffix
        m.filepath.suffix.lower.return_value = suffix
        return m

    def test_empty_list(self):
        from youtube_bulk_uploader.video_processor import get_batch_summary

        result = get_batch_summary([])
        assert result["count"] == 0
        assert result["total_size"] == 0

    def test_count_and_size(self, tmp_path):
        from youtube_bulk_uploader.metadata import VideoMetadata
        from youtube_bulk_uploader.video_processor import get_batch_summary

        f1 = tmp_path / "a.mp4"
        f2 = tmp_path / "b.mp4"
        f1.write_bytes(b"x" * 100)
        f2.write_bytes(b"x" * 200)
        with patch(
            "youtube_bulk_uploader.metadata._filesystem_date",
            return_value=dt(2023, 1, 1),
        ):
            m1 = VideoMetadata.from_file(f1, prefetched_exif_dates={})
            m2 = VideoMetadata.from_file(f2, prefetched_exif_dates={})
        result = get_batch_summary([m1, m2])
        assert result["count"] == 2
        assert result["total_size"] == 300

    def test_extensions_are_sorted(self, tmp_path):
        from youtube_bulk_uploader.metadata import VideoMetadata
        from youtube_bulk_uploader.video_processor import get_batch_summary

        for name in ["a.mp4", "b.mov", "c.avi"]:
            (tmp_path / name).write_bytes(b"x")
        metas = []
        with patch(
            "youtube_bulk_uploader.metadata._filesystem_date",
            return_value=dt(2023, 1, 1),
        ):
            for name in ["a.mp4", "b.mov", "c.avi"]:
                metas.append(
                    VideoMetadata.from_file(tmp_path / name, prefetched_exif_dates={})
                )
        result = get_batch_summary(metas)
        exts = result["extensions"]
        assert exts == sorted(exts), "Extensions must be in sorted order"

    def test_extensions_deduplicated(self, tmp_path):
        from youtube_bulk_uploader.metadata import VideoMetadata
        from youtube_bulk_uploader.video_processor import get_batch_summary

        for name in ["a.mp4", "b.mp4"]:
            (tmp_path / name).write_bytes(b"x")
        metas = []
        with patch(
            "youtube_bulk_uploader.metadata._filesystem_date",
            return_value=dt(2023, 1, 1),
        ):
            for name in ["a.mp4", "b.mp4"]:
                metas.append(
                    VideoMetadata.from_file(tmp_path / name, prefetched_exif_dates={})
                )
        result = get_batch_summary(metas)
        assert result["extensions"].count(".mp4") == 1


# ---------------------------------------------------------------------------
# uploader — UploadResult
# ---------------------------------------------------------------------------


class TestUploadResult:
    def test_success_true_when_status_success(self):
        from youtube_bulk_uploader.uploader import UploadResult

        r = UploadResult(video_id="abc", title="T", url="http://x", status="success")
        assert r.success is True

    def test_success_false_when_status_failed(self):
        from youtube_bulk_uploader.uploader import UploadResult

        r = UploadResult(
            video_id="", title="T", url="", status="failed", error_message="err"
        )
        assert r.success is False

    def test_success_false_when_quota_exceeded(self):
        from youtube_bulk_uploader.uploader import UploadResult

        r = UploadResult(video_id="", title="T", url="", status="quota_exceeded")
        assert r.success is False

    def test_str_success(self):
        from youtube_bulk_uploader.uploader import UploadResult

        r = UploadResult(
            video_id="abc",
            title="MyVid",
            url="http://youtu.be/abc",
            status="success",
            upload_time_seconds=10,
        )
        s = str(r)
        assert "SUCCESS" in s
        assert "MyVid" in s

    def test_str_failure(self):
        from youtube_bulk_uploader.uploader import UploadResult

        r = UploadResult(
            video_id="", title="Vid", url="", status="failed", error_message="oops"
        )
        s = str(r)
        assert "FAILED" in s
        assert "oops" in s


# ---------------------------------------------------------------------------
# uploader — _build_request_body
# ---------------------------------------------------------------------------


class TestBuildRequestBody:
    def _make_meta(self, tmp_path):
        from youtube_bulk_uploader.metadata import VideoMetadata

        f = tmp_path / "clip.mp4"
        f.write_text("x")
        with patch(
            "youtube_bulk_uploader.metadata._filesystem_date",
            return_value=dt(2023, 5, 15),
        ):
            return VideoMetadata.from_file(f, prefetched_exif_dates={})

    def test_privacy_always_unlisted(self, tmp_path):
        from youtube_bulk_uploader.uploader import YouTubeUploader

        uploader = YouTubeUploader(service=MagicMock())
        meta = self._make_meta(tmp_path)
        meta.privacy_status = "public"  # should be overridden
        body = uploader._build_request_body(meta)
        assert body["status"]["privacyStatus"] == "unlisted"

    def test_recording_date_format(self, tmp_path):
        from youtube_bulk_uploader.uploader import YouTubeUploader

        uploader = YouTubeUploader(service=MagicMock())
        meta = self._make_meta(tmp_path)
        body = uploader._build_request_body(meta)
        recording_date = body["recordingDetails"]["recordingDate"]
        # Must be YYYY-MM-DD only (no time component)
        assert len(recording_date) == 10
        assert recording_date.count("-") == 2

    def test_title_truncated_to_100(self, tmp_path):
        from youtube_bulk_uploader.uploader import YouTubeUploader

        uploader = YouTubeUploader(service=MagicMock())
        meta = self._make_meta(tmp_path)
        meta.title = "x" * 200
        body = uploader._build_request_body(meta)
        assert len(body["snippet"]["title"]) <= 100

    def test_description_truncated_to_5000(self, tmp_path):
        from youtube_bulk_uploader.uploader import YouTubeUploader

        uploader = YouTubeUploader(service=MagicMock())
        meta = self._make_meta(tmp_path)
        meta.description = "d" * 6000
        body = uploader._build_request_body(meta)
        assert len(body["snippet"]["description"]) <= 5000

    def test_description_contains_eastern_recording_time(self, tmp_path):
        from youtube_bulk_uploader.uploader import YouTubeUploader

        uploader = YouTubeUploader(service=MagicMock())
        meta = self._make_meta(tmp_path)
        body = uploader._build_request_body(meta)

        assert body["snippet"]["description"].endswith(
            "Recorded (Eastern): 2023-05-14 20:00:00 EDT"
        )

    def test_not_made_for_kids(self, tmp_path):
        from youtube_bulk_uploader.uploader import YouTubeUploader

        uploader = YouTubeUploader(service=MagicMock())
        meta = self._make_meta(tmp_path)
        body = uploader._build_request_body(meta)
        assert body["status"]["selfDeclaredMadeForKids"] is False

    def test_embedding_is_disabled_by_default(self, tmp_path):
        from youtube_bulk_uploader.uploader import YouTubeUploader

        uploader = YouTubeUploader(service=MagicMock())
        meta = self._make_meta(tmp_path)
        body = uploader._build_request_body(meta)

        assert body["status"]["embeddable"] is False

    def test_current_upload_settings_are_in_request_body(self, tmp_path):
        from youtube_bulk_uploader.metadata import UploadSettings
        from youtube_bulk_uploader.uploader import YouTubeUploader

        uploader = YouTubeUploader(service=MagicMock())
        meta = self._make_meta(tmp_path)
        meta.upload_settings = UploadSettings(
            category_id="27",
            default_language="en-US",
            made_for_kids=True,
            contains_synthetic_media=True,
            has_paid_product_placement=True,
            license="creativeCommon",
            embeddable=False,
            public_stats_viewable=False,
            notify_subscribers=False,
        )

        body = uploader._build_request_body(meta)

        assert body["snippet"]["categoryId"] == "27"
        assert body["snippet"]["defaultLanguage"] == "en-US"
        assert body["status"]["selfDeclaredMadeForKids"] is True
        assert body["status"]["containsSyntheticMedia"] is True
        assert body["status"]["license"] == "creativeCommon"
        assert body["status"]["embeddable"] is False
        assert body["status"]["publicStatsViewable"] is False
        assert body["paidProductPlacementDetails"] == {"hasPaidProductPlacement": True}


# ---------------------------------------------------------------------------
# main helpers — _truncate, _estimate_upload_seconds, _UploadState
# ---------------------------------------------------------------------------


class TestMainHelpers:
    def test_video_table_uses_terminal_width(self):
        from youtube_bulk_uploader.main import _build_video_table

        narrow = _build_video_table([], "Videos", 60)
        compact = _build_video_table([], "Videos", 80)
        medium = _build_video_table([], "Videos", 110)
        wide = _build_video_table([], "Videos", 150)

        assert [column.header for column in narrow.columns] == [
            "#",
            "File",
            "Size",
            "FS (UTC)",
            "EXIF (UTC)",
        ]
        assert [column.header for column in compact.columns][-4:] == [
            "FS (UTC)",
            "EXIF (UTC)",
            "Selected",
            "From",
        ]
        assert [column.header for column in medium.columns][-4:] == [
            "FS Date (UTC)",
            "EXIF Date (UTC)",
            "Selected (UTC)",
            "From",
        ]
        assert [column.header for column in wide.columns][-4:] == [
            "FS Date (UTC)",
            "EXIF Date (UTC)",
            "Selected (UTC)",
            "From",
        ]

    def test_date_source_labels_match_table_columns(self):
        from youtube_bulk_uploader.main import _date_source_display

        assert _date_source_display("filesystem")[0] == "FS"
        assert _date_source_display("exiftool")[0] == "EXIF"
        assert _date_source_display("user_corrected")[0] == "Manual"

    def test_table_filename_preserves_underscores_and_suffix(self):
        from youtube_bulk_uploader.main import _format_table_filename

        result = _format_table_filename("family_trip_with_long_name.mp4", 18)

        assert "_" in result
        assert result.endswith(".mp4")
        assert len(result) == 18

    def test_merge_upload_tags_combines_and_deduplicates(self):
        from youtube_bulk_uploader.main import _merge_upload_tags

        assert _merge_upload_tags(["family", "trip"], ("Trip", "2026"), True) == [
            "family",
            "trip",
            "2026",
        ]

    def test_merge_upload_tags_can_disable_generated_tags(self):
        from youtube_bulk_uploader.main import _merge_upload_tags

        assert _merge_upload_tags(["generated"], ("manual",), False) == ["manual"]

    def test_truncate_short_string_unchanged(self):
        from youtube_bulk_uploader.main import _truncate

        assert _truncate("hello") == "hello"

    def test_truncate_exact_limit_unchanged(self):
        from youtube_bulk_uploader.main import _truncate

        s = "x" * 40
        assert _truncate(s) == s

    def test_truncate_over_limit_adds_ellipsis(self):
        from youtube_bulk_uploader.main import _truncate

        s = "x" * 41
        result = _truncate(s)
        assert result == "x" * 40 + "..."

    def test_truncate_custom_max_len(self):
        from youtube_bulk_uploader.main import _truncate

        result = _truncate("hello world", max_len=5)
        assert result == "hello..."

    def test_estimate_upload_seconds_basic(self):
        from youtube_bulk_uploader.main import _estimate_upload_seconds

        # 10 Mbps = 1,250,000 bytes/sec
        # 12,500,000 bytes → 10 seconds
        result = _estimate_upload_seconds(12_500_000, 10.0)
        assert abs(result - 10.0) < 0.01

    def test_estimate_upload_seconds_zero_speed(self):
        from youtube_bulk_uploader.main import _estimate_upload_seconds

        result = _estimate_upload_seconds(1_000_000, 0.0)
        assert result == 0.0

    def test_upload_state_defaults(self):
        from youtube_bulk_uploader.main import _UploadState

        s = _UploadState()
        assert s.status == "Pending"
        assert s.url == ""
        assert s.last_update_time == 0.0

    def test_upload_state_mutable(self):
        from youtube_bulk_uploader.main import _UploadState

        s = _UploadState()
        s.status = "50%"
        s.url = "http://example.com"
        assert s.status == "50%"
        assert s.url == "http://example.com"


# ---------------------------------------------------------------------------
# Regression: quota_exceeded correctly marks remaining results
# ---------------------------------------------------------------------------


class TestQuotaExceededHandling:
    """Verify production quota handling emits a result for every remainder."""

    def test_quota_marks_remaining_results(self):
        from youtube_bulk_uploader.main import _quota_skip_results

        videos = [MagicMock(title=title) for title in ("vid2", "vid3", "vid4")]
        results = _quota_skip_results(videos)

        assert [result.title for result in results] == ["vid2", "vid3", "vid4"]
        assert all(result.status == "quota_exceeded" for result in results)


# ---------------------------------------------------------------------------
# Regression: date parsing never returns today's date for historical EXIF data
# ---------------------------------------------------------------------------


class TestDateParsingRegression:
    """End-to-end regression: batch extraction returns correct dates, not today."""

    def test_batch_exiftool_dates_not_today(self, tmp_path):
        from youtube_bulk_uploader.exiftool import get_batch_exiftool_dates

        f = tmp_path / "video.mp4"
        f.write_text("fake")
        # Simulate exiftool output with May 15 recording date
        json_output = f"""[{{
            "SourceFile": "{f.as_posix()}",
            "CreateDate": "2026:05:15 11:41:48",
            "MediaCreateDate": "2026:05:15 11:41:48",
            "FileCreateDate": "2026:06:07 18:58:57-04:00",
            "FileModifyDate": "2026:06:07 18:34:39-04:00"
        }}]"""
        with patch("youtube_bulk_uploader.exiftool.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout=json_output, stderr=""
            )
            with patch(
                "youtube_bulk_uploader.exiftool.find_exiftool", return_value="exiftool"
            ):
                result = get_batch_exiftool_dates([f])

        key = str(f.absolute())
        assert key in result
        dates = result[key]

        datetime.now(UTC)

        # CreateDate should be May 15, NOT today
        create_date = dates.get("CreateDate")
        assert create_date is not None
        assert create_date.month == 5, f"Expected month 5, got {create_date.month}"
        assert create_date.day == 15, f"Expected day 15, got {create_date.day}"

        # The minimum of all dates should be May 15, not June 7
        best = min(dates.values())
        assert best.month == 5, (
            f"Best date should be May (month 5), got month {best.month}"
        )
        assert best.year == 2026
        assert best.day == 15

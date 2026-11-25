import pytest

from bot.utils.parsers import (
    extract_youtube_url,
    format_filesize,
    parse_time_range,
    parse_timestamp,
    parse_user_request,
)


def test_extract_youtube_watch_and_strip_playlist() -> None:
    parsed = extract_youtube_url(
        "Посмотри вот это: https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=RDdQw4w9WgXcQ&t=42s!"
    )
    assert parsed is not None
    assert parsed.video_id == "dQw4w9WgXcQ"
    assert parsed.canonical_url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert parsed.is_shorts is False


def test_extract_youtu_be_and_shorts() -> None:
    short_link = extract_youtube_url("https://youtu.be/jNQXAC9IVRw?si=abc123")
    assert short_link is not None
    assert short_link.video_id == "jNQXAC9IVRw"
    assert short_link.canonical_url == "https://www.youtube.com/watch?v=jNQXAC9IVRw"
    assert short_link.is_shorts is False

    shorts = extract_youtube_url("https://youtube.com/shorts/abcdefghijk?feature=share")
    assert shorts is not None
    assert shorts.video_id == "abcdefghijk"
    assert shorts.canonical_url == "https://www.youtube.com/shorts/abcdefghijk"
    assert shorts.is_shorts is True


def test_rejects_non_youtube_urls() -> None:
    assert extract_youtube_url("https://vimeo.com/123456789") is None
    assert extract_youtube_url("https://youtube.com/watch?v=too_short") is None
    assert extract_youtube_url("просто текст без ссылок") is None


def test_parse_timestamps_and_ranges() -> None:
    assert parse_timestamp("45") == 45.0
    assert parse_timestamp("01:15") == 75.0
    assert parse_timestamp("1:02:03.5") == 3723.5

    tr = parse_time_range("01:15 – 02:30")
    assert tr.start_seconds == 75.0
    assert tr.end_seconds == 150.0
    assert tr.ytdlp_section == "*01:15-02:30"

    tr_space = parse_time_range("10 01:00", max_duration=120)
    assert tr_space.duration_seconds == 50.0


def test_parse_time_range_validation() -> None:
    with pytest.raises(ValueError):
        parse_time_range("02:00-01:00")
    with pytest.raises(ValueError):
        parse_time_range("00:10-03:00", max_duration=100)
    with pytest.raises(ValueError):
        parse_time_range("неверный формат")


def test_parse_user_request_inline_timecode() -> None:
    req = parse_user_request("Скачай https://youtu.be/dQw4w9WgXcQ 00:15-00:45 пожалуйста")
    assert req is not None
    assert req.video.video_id == "dQw4w9WgXcQ"
    assert req.time_range is not None
    assert req.time_range.start_seconds == 15.0
    assert req.time_range.end_seconds == 45.0


def test_format_filesize() -> None:
    assert format_filesize(500) == "500 Б"
    assert format_filesize(5 * 1024 * 1024) == "5.0 МБ"

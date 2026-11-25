from bot.handlers.youtube import _is_group_auto_media
from bot.services.instagram import build_cdn_fallback_urls, parse_og_metadata
from bot.utils.parsers import extract_instagram_url, parse_instagram_request


def test_extract_instagram_urls() -> None:
    reel = extract_instagram_url("Зацени https://www.instagram.com/reel/Chunk8-jurw/?igsh=abc123!")
    assert reel is not None
    assert reel.shortcode == "Chunk8-jurw"
    assert reel.kind == "reel"
    assert reel.is_reel is True
    assert reel.canonical_url == "https://www.instagram.com/reel/Chunk8-jurw/"

    user_reel = extract_instagram_url("https://instagram.com/nasa/reels/DCxyz12345/")
    assert user_reel is not None
    assert user_reel.shortcode == "DCxyz12345"
    assert user_reel.kind == "reel"
    assert user_reel.is_reel is True

    kk_post = extract_instagram_url("https://kkinstagram.com/p/aye83DjauH/")
    assert kk_post is not None
    assert kk_post.shortcode == "aye83DjauH"
    assert kk_post.kind == "p"
    assert kk_post.is_reel is False

    assert extract_instagram_url("https://www.instagram.com/nasa/") is None


def test_parse_instagram_request_with_timecode() -> None:
    req = parse_instagram_request("https://www.instagram.com/reel/Chunk8-jurw/ 00:02-00:05")
    assert req is not None
    assert req.media.shortcode == "Chunk8-jurw"
    assert req.time_range is not None
    assert req.time_range.start_seconds == 2.0
    assert req.time_range.end_seconds == 5.0


def test_group_auto_media_filter() -> None:
    assert _is_group_auto_media("https://youtube.com/shorts/abcdefghijk") is True
    assert _is_group_auto_media("https://www.instagram.com/reel/Chunk8-jurw/") is True
    assert _is_group_auto_media("https://www.youtube.com/watch?v=dQw4w9WgXcQ") is False
    assert _is_group_auto_media("https://www.instagram.com/p/aye83DjauH/") is False
    assert _is_group_auto_media("обычный текст") is False


def test_parse_og_metadata_and_cdn_fallback() -> None:
    html_sample = (
        '<meta name="twitter:title" content="@nasa"/>'
        '<meta property="og:description" content="Rocket launch &amp; more"/>'
        '<meta property="og:video" content="/videos/ABC123/1?audio=1"/>'
    )
    meta = parse_og_metadata(html_sample, "https://eeinstagram.com/reel/ABC123/")
    assert meta["twitter:title"] == "@nasa"
    assert meta["og:description"] == "Rocket launch & more"
    assert meta["resolved_video_url"] == "https://eeinstagram.com/videos/ABC123/1?audio=1"

    fb_url = "https://instagram.fcul3-1.fna.fbcdn.net/o1/v/t16/f2/m84/test.mp4?oh=123"
    candidates = build_cdn_fallback_urls(fb_url)
    assert candidates == [
        fb_url,
        "https://scontent.cdninstagram.com/o1/v/t16/f2/m84/test.mp4?oh=123",
    ]

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from aiogram.enums import ChatType
from aiogram.types import Chat, Message

from bot.config import Settings
from bot.handlers.youtube import _build_menu_keyboard, sanitize_telegram_filename
from bot.middlewares.auth import WhitelistMiddleware
from bot.services.access import AccessManager
from bot.services.downloader import (
    AuthRequiredError,
    VideoInfo,
    YouTubeDownloader,
    build_telegram_ffmpeg_cmd,
    is_auth_or_bot_error,
    is_telegram_compatible_probe,
    parse_progress_line,
    summarize_video_formats,
)
from bot.services.jobs import PendingJobStore


def test_settings_parses_allowed_user_ids() -> None:
    s = Settings(bot_token="123:abc", allowed_user_ids="1001, 2002;3003")  # type: ignore[arg-type]
    assert s.allowed_user_ids == {1001, 2002, 3003}
    assert s.max_file_size_bytes == 2000 * 1024 * 1024


def test_is_auth_or_bot_error() -> None:
    assert is_auth_or_bot_error("ERROR: Sign in to confirm you’re not a bot")
    assert is_auth_or_bot_error("ERROR: HTTP Error 403: Forbidden")
    assert not is_auth_or_bot_error("ERROR: Video unavailable")


def test_parse_progress_line() -> None:
    prog = parse_progress_line("PROG|1048576|10485760|NA|524288.0|18| 10.0%")
    assert prog is not None
    assert prog.downloaded_bytes == 1048576
    assert prog.total_bytes == 10485760
    assert prog.speed_bps == 524288.0
    assert prog.eta_seconds == 18
    assert prog.percent_str == "10.0%"

    assert parse_progress_line("[download] Destination: foo.mp4") is None


def test_summarize_video_formats() -> None:
    formats = [
        {"vcodec": "none", "acodec": "mp4a.40.2", "filesize": 5_000_000},
        {"vcodec": "avc1", "acodec": "none", "width": 640, "height": 360, "filesize": 10_000_000},
        {"vcodec": "avc1", "acodec": "none", "width": 1280, "height": 720, "filesize": 25_000_000},
        {"vcodec": "avc1", "acodec": "none", "width": 1920, "height": 1080, "filesize": 60_000_000},
    ]
    heights, estimates, is_portrait = summarize_video_formats(formats)
    assert is_portrait is False
    assert heights == [1080, 720, 360]
    assert estimates[1080] == 65_000_000
    assert estimates[720] == 30_000_000


def test_summarize_vertical_shorts_formats() -> None:
    formats = [
        {"vcodec": "none", "acodec": "mp4a.40.2", "filesize": 1_000_000},
        {"vcodec": "avc1", "acodec": "none", "width": 360, "height": 640, "filesize": 2_000_000},
        {"vcodec": "avc1", "acodec": "none", "width": 720, "height": 1280, "filesize": 5_000_000},
        {"vcodec": "avc1", "acodec": "none", "width": 1080, "height": 1920, "filesize": 9_000_000},
    ]
    heights, estimates, is_portrait = summarize_video_formats(formats)
    assert is_portrait is True
    assert heights == [1080, 720, 360]
    assert estimates[1080] == 10_000_000


async def test_with_cookie_retry_refreshes_and_retries(tmp_path: Path) -> None:
    cookie_mgr = MagicMock()

    async def _snapshot(dest: Path) -> Path:
        dest.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
        return dest

    cookie_mgr.get_cookies_snapshot = AsyncMock(side_effect=_snapshot)
    cookie_mgr.refresh_cookies = AsyncMock()

    downloader = YouTubeDownloader(downloads_dir=tmp_path, cookie_manager=cookie_mgr)

    attempts = 0

    async def _op(cookies_path: Path | None) -> str:
        nonlocal attempts
        attempts += 1
        assert cookies_path is not None and cookies_path.exists()
        if attempts == 1:
            raise AuthRequiredError("Sign in to confirm you're not a bot")
        return "ok"

    result = await downloader._with_cookie_retry(_op)
    assert result == "ok"
    assert attempts == 2
    cookie_mgr.refresh_cookies.assert_awaited_once_with(navigate=True)


async def test_whitelist_middleware_and_username_binding(tmp_path: Path) -> None:
    users_file = tmp_path / "users.json"
    access = AccessManager(users_file, admin_ids={42})
    assert "Добавлен" in access.add_target("@Friend_One")

    mw = WhitelistMiddleware(access)
    handler = AsyncMock(return_value="passed")

    # Admin always passes
    admin_msg = MagicMock(spec=Message)
    admin_msg.chat = MagicMock(spec=Chat, id=42, type=ChatType.PRIVATE)
    admin_msg.from_user = MagicMock(id=42, username="owner")
    assert await mw(handler, admin_msg, {}) == "passed"

    # Pending username binds automatically on first message
    friend_msg = MagicMock(spec=Message)
    friend_msg.chat = MagicMock(spec=Chat, id=777, type=ChatType.PRIVATE)
    friend_msg.from_user = MagicMock(id=777, username="friend_one")
    assert await mw(handler, friend_msg, {}) == "passed"
    assert 777 in access.allowed_ids
    assert "friend_one" not in access.pending_usernames

    # Even if username changes later, bound user_id still passes
    friend_renamed = MagicMock(spec=Message)
    friend_renamed.chat = MagicMock(spec=Chat, id=777, type=ChatType.PRIVATE)
    friend_renamed.from_user = MagicMock(id=777, username="new_nick")
    assert await mw(handler, friend_renamed, {}) == "passed"

    # Reload from disk preserves bound IDs
    access_reloaded = AccessManager(users_file, admin_ids={42})
    assert access_reloaded.allowed_ids[777] == "new_nick"

    # Stranger rejected in PM
    denied_msg = MagicMock(spec=Message)
    denied_msg.chat = MagicMock(spec=Chat, id=99, type=ChatType.PRIVATE)
    denied_msg.from_user = MagicMock(id=99, username="stranger")
    denied_msg.answer = AsyncMock()
    assert await mw(handler, denied_msg, {}) is None
    denied_msg.answer.assert_awaited_once()

    # Stranger in unauthorized group is silently ignored (no spam)
    unauth_group_msg = MagicMock(spec=Message)
    unauth_group_msg.chat = MagicMock(spec=Chat, id=-100555, type=ChatType.SUPERGROUP)
    unauth_group_msg.from_user = MagicMock(id=99, username="stranger")
    unauth_group_msg.answer = AsyncMock()
    assert await mw(handler, unauth_group_msg, {}) is None
    unauth_group_msg.answer.assert_not_awaited()

    # Allowed group allows any member's message and persists across reload
    assert access.add_chat(-100555, "Friends Chat") is True
    assert await mw(handler, unauth_group_msg, {}) == "passed"
    assert AccessManager(users_file, admin_ids={42}).is_chat_allowed(-100555) is True
    assert access.remove_chat(-100555) is True
    assert access.is_chat_allowed(-100555) is False

    # Admin can remove by @username
    assert "Удалён" in access.remove_target("@new_nick")
    assert 777 not in access.allowed_ids


def test_document_buttons_and_filename() -> None:
    assert sanitize_telegram_filename('Тест: видео / 1080p?*', ".mp4") == "Тест видео 1080p.mp4"

    store = PendingJobStore()
    info = VideoInfo(
        video_id="abc12345678",
        title="Test Video",
        uploader="Channel",
        duration=60,
        webpage_url="https://www.youtube.com/watch?v=abc12345678",
        is_shorts=False,
        is_portrait=False,
        is_live=False,
        available_heights=[1080, 720],
        estimated_sizes={},
    )
    job = store.create(user_id=1, chat_id=1, info=info)
    kb = _build_menu_keyboard(job)
    callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert f"dl:{job.job_id}:best_doc" in callbacks
    assert f"toggledoc:{job.job_id}" in callbacks


def test_telegram_ffmpeg_cmd_and_compat_probe(tmp_path: Path) -> None:
    src = tmp_path / "in.webm"
    dst = tmp_path / "out.mp4"
    cmd = build_telegram_ffmpeg_cmd(src, dst, crf=24, preset="medium")
    joined = " ".join(cmd)
    assert "-c:v libx264" in joined
    assert "-crf 24" in joined
    assert "-preset medium" in joined
    assert "-pix_fmt yuv420p" in joined
    assert "-profile:v high" in joined
    assert "-c:a aac -b:a 128k -ac 2" in joined
    assert "-movflags +faststart" in joined

    mp4_file = tmp_path / "ok.mp4"
    assert is_telegram_compatible_probe(
        {"video_codec": "h264", "pix_fmt": "yuv420p", "audio_codec": "aac", "audio_channels": 2},
        mp4_file,
    )
    assert not is_telegram_compatible_probe(
        {"video_codec": "vp9", "pix_fmt": "yuv420p", "audio_codec": "opus", "audio_channels": 2},
        mp4_file,
    )
    assert not is_telegram_compatible_probe(
        {"video_codec": "h264", "pix_fmt": "yuv420p10le", "audio_codec": "aac", "audio_channels": 2},
        mp4_file,
    )

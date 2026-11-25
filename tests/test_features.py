from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from aiogram.enums import ChatType
from aiogram.types import CallbackQuery, Chat, ChatMemberUpdated, Message, MessageEntity, User

from bot.handlers.commands import cmd_start, on_added_to_group
from bot.handlers.youtube import (
    _has_supported_media_url,
    _is_group_auto_media,
    _save_to_cache,
    cb_download,
    check_group_duration_limit,
    is_bot_mentioned,
)
from bot.main import cleanup_stale_downloads
from bot.services.access import AccessManager
from bot.services.alerts import AdminAlertMonitor
from bot.services.cache import CachedMedia, MediaCache
from bot.services.chrome_cookies import SessionStatus
from bot.services.downloader import AuthRequiredError, DownloadedMedia, VideoInfo, YouTubeDownloader
from bot.services.jobs import PendingJobStore, RateLimiter
from bot.services.tiktok import parse_tikwm_payload
from bot.utils.parsers import TimeRange, extract_tiktok_url, parse_tiktok_request


def test_extract_tiktok_urls_and_group_filter() -> None:
    full = extract_tiktok_url("Глянь https://www.tiktok.com/@barudakhb_/video/6984138651336838402?is_from_webapp=1")
    assert full is not None
    assert full.video_id == "6984138651336838402"
    assert full.canonical_url == "https://www.tiktok.com/@barudakhb_/video/6984138651336838402"

    short = extract_tiktok_url("https://vm.tiktok.com/ZMrabc123/")
    assert short is not None
    assert short.video_id is None
    assert short.canonical_url == "https://vm.tiktok.com/ZMrabc123/"

    assert extract_tiktok_url("https://www.tiktok.com/@barudakhb_") is None

    req = parse_tiktok_request("https://vt.tiktok.com/ZMrXYZ99/ 00:05-00:15")
    assert req is not None
    assert req.time_range is not None
    assert req.time_range.start_seconds == 5.0
    assert req.time_range.end_seconds == 15.0

    assert _is_group_auto_media("https://vm.tiktok.com/ZMrabc123/") is True
    assert _is_group_auto_media("https://www.tiktok.com/@user/video/6984138651336838402") is True


def test_media_cache_roundtrip_and_eviction(tmp_path: Path) -> None:
    cache_path = tmp_path / "media_cache.json"
    cache = MediaCache(cache_path, max_entries=2)

    k1 = MediaCache.make_key(
        platform="youtube",
        video_id="abc",
        quality="best",
        as_document=False,
        time_range=None,
    )
    k2 = MediaCache.make_key(
        platform="tiktok",
        video_id="123",
        quality="best",
        as_document=True,
        time_range=TimeRange(1.0, 5.0),
    )
    k3 = MediaCache.make_key(
        platform="instagram",
        video_id="xyz",
        quality="best",
        as_document=False,
        time_range=None,
    )

    cache.put(
        k1,
        CachedMedia("fid1", "video", "T1", "U1", 10, 1080, 1920, 1000, created_at=10.0),
    )
    cache.put(
        k2,
        CachedMedia("fid2", "document", "T2", "U2", 20, 720, 1280, 2000, created_at=20.0),
    )
    cache.put(
        k3,
        CachedMedia("fid3", "video", "T3", "U3", 30, 1080, 1920, 3000, created_at=30.0),
    )

    assert cache.count == 2
    assert cache.get(k1) is None
    assert cache.get(k2) is not None and cache.get(k2).file_id == "fid2"

    reloaded = MediaCache(cache_path)
    assert reloaded.count == 2
    assert reloaded.get(k3).file_id == "fid3"
    assert reloaded.clear() == 2
    assert reloaded.count == 0


async def test_admin_alert_monitor_session_and_disk(tmp_path: Path, monkeypatch) -> None:
    bot = MagicMock()
    bot.send_message = AsyncMock()

    monitor = AdminAlertMonitor(
        bot,
        admin_ids={100000001},
        downloads_dir=tmp_path,
        low_disk_warn_gb=3.0,
        cooldown_minutes=60,
    )

    # Normal disk (10 GB free), unauthenticated session -> 1 alert
    monkeypatch.setattr(
        "bot.services.alerts.shutil.disk_usage",
        lambda _: SimpleNamespace(total=50 * 1024**3, used=40 * 1024**3, free=10 * 1024**3),
    )
    unauth = SessionStatus(
        browser_running=True,
        authenticated=False,
        total_cookies=5,
        youtube_cookies=5,
        last_refresh_at=None,
        last_error="Logged out",
        cookies_file_exists=True,
    )
    await monitor.evaluate(unauth)
    assert bot.send_message.await_count == 1
    assert "не авторизована" in bot.send_message.call_args.args[1]

    # Repeated check within cooldown -> no spam
    await monitor.evaluate(unauth)
    assert bot.send_message.await_count == 1

    # Session recovers + disk drops below 3 GB -> recovery notice + low disk alert
    monkeypatch.setattr(
        "bot.services.alerts.shutil.disk_usage",
        lambda _: SimpleNamespace(total=50 * 1024**3, used=48 * 1024**3, free=2 * 1024**3),
    )
    auth = SessionStatus(
        browser_running=True,
        authenticated=True,
        total_cookies=50,
        youtube_cookies=25,
        last_refresh_at=None,
        last_error=None,
        cookies_file_exists=True,
    )
    await monitor.evaluate(auth)
    assert bot.send_message.await_count == 3
    sent_texts = [c.args[1] for c in bot.send_message.call_args_list]
    assert any("восстановлена" in t for t in sent_texts)
    assert any("Мало свободного места" in t for t in sent_texts)


def test_parse_tikwm_payload_and_cleanup_stale_downloads(tmp_path: Path) -> None:
    parsed = parse_tikwm_payload(
        {
            "code": 0,
            "data": {
                "id": "1234567890",
                "title": "Cool TikTok",
                "duration": 15,
                "hdplay": "https://cdn.example.com/hd.mp4",
                "play": "https://cdn.example.com/sd.mp4",
                "author": {"unique_id": "creator1"},
            },
        },
        "https://vm.tiktok.com/abc/",
    )
    assert parsed is not None
    assert parsed["id"] == "1234567890"
    assert parsed["play_url"] == "https://cdn.example.com/hd.mp4"
    assert parsed["uploader"] == "creator1"

    assert parse_tikwm_payload({"code": -1}, "https://vm.tiktok.com/abc/") is None

    stale_dir = tmp_path / "job123"
    stale_dir.mkdir()
    (stale_dir / "video.mp4").write_bytes(b"123")
    (tmp_path / "orphan.tmp").write_bytes(b"abc")
    cleanup_stale_downloads(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_is_bot_mentioned_and_supported_urls(tmp_path: Path) -> None:
    text = "@example_download_bot https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    msg = MagicMock(spec=Message)
    msg.text = text
    msg.caption = None
    msg.entities = [MessageEntity(type="mention", offset=0, length=21)]
    msg.caption_entities = None

    assert is_bot_mentioned(msg, "example_download_bot") is True
    assert is_bot_mentioned(msg, "other_bot") is False
    assert _has_supported_media_url(text) is True
    assert _is_group_auto_media(text) is False

    caption_msg = MagicMock(spec=Message)
    caption_msg.text = None
    caption_msg.caption = "Глянь @EXAMPLE_DOWNLOAD_BOT https://www.instagram.com/p/aye83DjauH/"
    caption_msg.entities = None
    caption_msg.caption_entities = None
    caption_msg.reply_to_message = None
    assert is_bot_mentioned(caption_msg, "example_download_bot") is True
    assert _has_supported_media_url(caption_msg.caption) is True

    cmd_msg = MagicMock(spec=Message)
    cmd_msg.text = "/dl@example_download_bot https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    cmd_msg.caption = None
    cmd_msg.entities = None
    cmd_msg.caption_entities = None
    cmd_msg.reply_to_message = None
    assert is_bot_mentioned(cmd_msg, "example_download_bot") is True

    # Verify ADMIN_USERNAMES auto-binds to admin_ids
    users_file = tmp_path / "users.json"
    access = AccessManager(users_file, admin_ids=set(), admin_usernames={"Boss_Admin"})
    assert access.check_and_bind(5550001, "boss_admin") is True
    assert access.is_admin(5550001) is True
    reloaded = AccessManager(users_file, admin_ids=set())
    assert reloaded.is_admin(5550001) is True


async def test_group_activation_only_by_whitelisted_user(tmp_path: Path) -> None:
    users_file = tmp_path / "users.json"
    access = AccessManager(users_file, admin_ids={100})
    access.add_target("@trusted_friend")

    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.leave_chat = AsyncMock()

    # 1. Stranger adds bot -> bot explains access is needed, then leaves without activating chat
    stranger_event = MagicMock(spec=ChatMemberUpdated)
    stranger_event.bot = bot
    stranger_event.chat = MagicMock(spec=Chat, id=-100111, title="Random Chat")
    stranger_event.from_user = MagicMock(spec=User, id=999, username="stranger")

    await on_added_to_group(stranger_event, access, bot_username="example_bot")
    assert access.is_chat_allowed(-100111) is False
    bot.send_message.assert_awaited_once()
    assert "администратору" in bot.send_message.call_args.args[1].lower()
    bot.leave_chat.assert_awaited_once_with(-100111)

    # 2. Whitelisted user (matched by @username) adds bot -> chat activated + admin notified
    bot.leave_chat.reset_mock()
    bot.send_message.reset_mock()
    friend_event = MagicMock(spec=ChatMemberUpdated)
    friend_event.bot = bot
    friend_event.chat = MagicMock(spec=Chat, id=-100222, title="Friends Chat")
    friend_event.from_user = MagicMock(spec=User, id=777, username="Trusted_Friend")

    await on_added_to_group(friend_event, access, bot_username="example_bot")
    assert access.is_chat_allowed(-100222) is True
    assert 777 in access.allowed_ids
    bot.leave_chat.assert_not_awaited()
    assert bot.send_message.await_count == 2  # admin notification + group welcome

    welcome_text = bot.send_message.call_args_list[-1].args[1].lower()
    assert "privacy" not in welcome_text
    assert "botfather" not in welcome_text
    assert "администратор" not in welcome_text
    assert "/dl@example_bot" in welcome_text


async def test_separate_help_for_private_and_group(tmp_path: Path) -> None:
    access = AccessManager(tmp_path / "users.json", admin_ids={100})
    state = MagicMock()
    state.clear = AsyncMock()

    group_msg = MagicMock(spec=Message)
    group_msg.chat = MagicMock(spec=Chat, id=-100222, type=ChatType.SUPERGROUP)
    group_msg.from_user = MagicMock(spec=User, id=100)
    group_msg.answer = AsyncMock()

    await cmd_start(group_msg, state, access, bot_username="example_bot")
    group_help = group_msg.answer.call_args.args[0]
    assert "Справка для группового чата" in group_help
    assert "/dl@example_bot" in group_help
    assert "Админ-команды" not in group_help

    pm_msg = MagicMock(spec=Message)
    pm_msg.chat = MagicMock(spec=Chat, id=100, type=ChatType.PRIVATE)
    pm_msg.from_user = MagicMock(spec=User, id=100)
    pm_msg.answer = AsyncMock()

    await cmd_start(pm_msg, state, access, bot_username="example_bot")
    pm_help = pm_msg.answer.call_args.args[0]
    assert "личных сообщениях" in pm_help
    assert "Админ-команды" in pm_help


def test_rate_limiter_and_group_duration_limits() -> None:
    limiter = RateLimiter(cooldown_seconds=20, hourly_limit=2)
    # Admins are exempt
    assert limiter.check_and_record(1, is_admin=True) is None
    assert limiter.check_and_record(1, is_admin=True) is None

    # Non-admin hits cooldown immediately after 1st download
    assert limiter.check_and_record(2, is_admin=False) is None
    cooldown_msg = limiter.check_and_record(2, is_admin=False)
    assert cooldown_msg is not None and "Подожди" in cooldown_msg

    # Hourly quota test (cooldown=0)
    q_limiter = RateLimiter(cooldown_seconds=0, hourly_limit=2)
    assert q_limiter.check_and_record(3, is_admin=False) is None
    assert q_limiter.check_and_record(3, is_admin=False) is None
    quota_msg = q_limiter.check_and_record(3, is_admin=False)
    assert quota_msg is not None and "часовой лимит" in quota_msg

    # Group duration limits (120s max, no live streams)
    assert check_group_duration_limit(120, None, max_duration_seconds=120) is None
    assert check_group_duration_limit(121, None, max_duration_seconds=120) is not None
    assert check_group_duration_limit(600, TimeRange(10.0, 70.0), max_duration_seconds=120) is None
    assert check_group_duration_limit(600, TimeRange(0.0, 150.0), max_duration_seconds=120) is not None
    assert check_group_duration_limit(30, None, is_live=True, max_duration_seconds=120) is not None


async def test_anonymous_first_then_cookies_fallback(tmp_path: Path) -> None:
    cookie_mgr = MagicMock()
    cookie_mgr.get_cookies_snapshot = AsyncMock(
        side_effect=lambda dest: (dest.write_text("# Netscape\n"), dest)[1]
    )
    cookie_mgr.refresh_cookies = AsyncMock()

    dl = YouTubeDownloader(
        downloads_dir=tmp_path,
        cookie_manager=cookie_mgr,
        pot_provider_url="http://bgutil-pot-provider:4416",
        sleep_requests=1.0,
        concurrent_fragments=2,
    )

    # Check yt-dlp args without cookies vs with cookies
    anon_args = dl._base_ytdlp_args(None)
    assert "--cookies" not in anon_args
    assert "youtubepot-bgutilhttp:base_url=http://bgutil-pot-provider:4416" in anon_args
    assert "--sleep-requests" in anon_args
    assert "--concurrent-fragments" in anon_args

    # 1. Anonymous succeeds -> cookies are NOT touched
    calls: list[Path | None] = []

    async def _anon_ok(cp: Path | None) -> str:
        calls.append(cp)
        return "ok"

    assert await dl._with_anonymous_then_cookies(_anon_ok) == "ok"
    assert calls == [None]
    cookie_mgr.get_cookies_snapshot.assert_not_awaited()

    # 2. Anonymous fails with AuthRequiredError -> falls back to cookies
    calls.clear()

    async def _needs_cookies(cp: Path | None) -> str:
        calls.append(cp)
        if cp is None:
            raise AuthRequiredError("Sign in to confirm you're not a bot")
        return "ok_with_cookies"

    assert await dl._with_anonymous_then_cookies(_needs_cookies) == "ok_with_cookies"
    assert len(calls) == 2
    assert calls[0] is None and calls[1] is not None
    cookie_mgr.get_cookies_snapshot.assert_awaited_once()


async def test_send_document_disables_type_detection_and_guards_cache(tmp_path: Path) -> None:
    cache = MediaCache(tmp_path / "media_cache.json")
    work_dir = tmp_path / "dl"
    work_dir.mkdir()
    video_file = work_dir / "sample.mp4"
    video_file.write_bytes(b"fake-mp4-data")

    media = DownloadedMedia(
        job_dir=work_dir,
        file_path=video_file,
        title="Short Clip",
        uploader="Creator",
        is_audio=False,
        width=720,
        height=1280,
        duration=15,
        size_bytes=video_file.stat().st_size,
    )

    # 1. If Telegram converted upload to sent_msg.video, do NOT poison the as_document=True cache slot
    poisoned_msg = MagicMock(spec=Message)
    poisoned_msg.document = None
    poisoned_msg.audio = None
    poisoned_msg.video = SimpleNamespace(file_id="vid_instead_of_doc")
    doc_key = MediaCache.make_key(
        platform="youtube", video_id="vid123", quality="720", as_document=True, time_range=None
    )
    _save_to_cache(
        cache,
        cache_key=doc_key,
        sent_msg=poisoned_msg,
        media=media,
        title="Short Clip",
        uploader="Creator",
        send_as_document=True,
    )
    assert cache.get(doc_key) is None

    # 2. Pre-existing poisoned cache entry (media_kind="video" under |1|) must be ignored & evicted,
    # and bot.send_document must be called with disable_content_type_detection=True.
    cache.put(
        doc_key,
        CachedMedia(
            file_id="old_video_file_id",
            media_kind="video",
            title="Short Clip",
            uploader="Creator",
            duration=15,
            width=720,
            height=1280,
            size_bytes=123,
            created_at=1.0,
        ),
    )

    job_store = PendingJobStore()
    info = VideoInfo(
        video_id="vid123",
        title="Short Clip",
        uploader="Creator",
        duration=15,
        webpage_url="https://www.youtube.com/watch?v=vid123",
        is_shorts=False,
        is_portrait=True,
        is_live=False,
        available_heights=[720],
        estimated_sizes={720: 1000},
    )
    job = job_store.create(user_id=100, chat_id=100, info=info, platform="youtube")
    job.as_document = True

    status_msg = MagicMock(spec=Message)
    status_msg.edit_text = AsyncMock()
    status_msg.delete = AsyncMock()

    callback = MagicMock(spec=CallbackQuery)
    callback.data = f"dl:{job.job_id}:720"
    callback.from_user = MagicMock(spec=User, id=100)
    callback.message = status_msg
    callback.answer = AsyncMock()

    state = MagicMock()
    state.clear = AsyncMock()

    sent_doc_msg = MagicMock(spec=Message)
    sent_doc_msg.document = SimpleNamespace(file_id="real_doc_file_id")
    sent_doc_msg.video = None
    sent_doc_msg.audio = None

    bot = MagicMock()
    bot.send_chat_action = AsyncMock()
    bot.send_document = AsyncMock(return_value=sent_doc_msg)
    bot.send_video = AsyncMock()

    downloader = MagicMock()
    downloader.download = AsyncMock(return_value=media)

    await cb_download(
        callback=callback,
        state=state,
        bot=bot,
        downloader=downloader,
        ig_downloader=MagicMock(),
        tiktok_downloader=MagicMock(),
        media_cache=cache,
        job_store=job_store,
    )

    bot.send_video.assert_not_awaited()
    bot.send_document.assert_awaited_once()
    assert bot.send_document.call_args.kwargs["disable_content_type_detection"] is True
    repaired = cache.get(doc_key)
    assert repaired is not None
    assert repaired.media_kind == "document"
    assert repaired.file_id == "real_doc_file_id"

from __future__ import annotations

import asyncio
import logging
import shutil
import sys
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeAllGroupChats, BotCommandScopeDefault

from bot.config import Settings, load_settings
from bot.handlers import commands, youtube
from bot.middlewares.auth import WhitelistMiddleware
from bot.services.access import AccessManager
from bot.services.alerts import AdminAlertMonitor
from bot.services.cache import MediaCache
from bot.services.chrome_cookies import ChromeSessionManager
from bot.services.downloader import YouTubeDownloader
from bot.services.instagram import InstagramDownloader
from bot.services.jobs import PendingJobStore, RateLimiter
from bot.services.tiktok import TikTokDownloader

logger = logging.getLogger(__name__)


def build_bot(settings: Settings) -> Bot:
    session: AiohttpSession | None = None
    if settings.local_bot_api_url:
        api = TelegramAPIServer.from_base(
            settings.local_bot_api_url,
            is_local=settings.local_bot_api_is_local,
        )
        session = AiohttpSession(
            api=api,
            timeout=settings.bot_api_timeout_seconds,
        )
        logger.info(
            "Using Local Telegram Bot API at %s (is_local=%s)",
            settings.local_bot_api_url,
            settings.local_bot_api_is_local,
        )

    return Bot(
        token=settings.bot_token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


async def wait_for_bot_api(bot: Bot, attempts: int = 15, delay: float = 2.0) -> str:
    """Wait for the Local Bot API server to accept connections and return bot username."""
    for attempt in range(1, attempts + 1):
        try:
            me = await bot.get_me()
            logger.info("Connected to Telegram Bot API as @%s (id=%s)", me.username, me.id)
            return (me.username or "").lower()
        except Exception as exc:
            if attempt == attempts:
                raise
            logger.warning(
                "Telegram Bot API not ready yet (attempt %d/%d): %s",
                attempt,
                attempts,
                exc,
            )
            await asyncio.sleep(delay)
    return ""


def cleanup_stale_downloads(downloads_dir: Path) -> None:
    """Remove any leftover temporary download directories after an unclean restart."""
    if not downloads_dir.exists():
        return
    for item in downloads_dir.iterdir():
        try:
            if item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
            else:
                item.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Could not clean stale download %s: %s", item, exc)


async def register_commands(bot: Bot) -> None:
    """Register bot commands so /dl appears in Telegram group & PM autocomplete."""
    try:
        await bot.set_my_commands(
            [
                BotCommand(command="dl", description="Скачать видео / Shorts / Reels / TikTok"),
                BotCommand(command="status", description="Статус бота и Chrome-сессии"),
                BotCommand(command="help", description="Справка"),
            ],
            scope=BotCommandScopeDefault(),
        )
        await bot.set_my_commands(
            [
                BotCommand(command="dl", description="Скачать видео по ссылке или reply"),
                BotCommand(command="help", description="Справка"),
            ],
            scope=BotCommandScopeAllGroupChats(),
        )
    except Exception as exc:
        logger.warning("Failed to set bot commands: %s", exc)


async def run() -> None:
    settings = load_settings()
    settings.downloads_dir.mkdir(parents=True, exist_ok=True)
    cleanup_stale_downloads(settings.downloads_dir)
    settings.chrome_profile_dir.mkdir(parents=True, exist_ok=True)
    settings.cookies_file.parent.mkdir(parents=True, exist_ok=True)
    settings.users_file.parent.mkdir(parents=True, exist_ok=True)
    settings.cache_file.parent.mkdir(parents=True, exist_ok=True)

    access_manager = AccessManager(
        settings.users_file,
        admin_ids=settings.effective_admin_ids,
        admin_usernames=settings.admin_usernames,
        seed_allowed_ids=settings.allowed_user_ids,
    )
    media_cache = MediaCache(settings.cache_file)
    bot = build_bot(settings)
    alert_monitor = AdminAlertMonitor(
        bot,
        admin_ids=access_manager.admin_ids,
        downloads_dir=settings.downloads_dir,
        low_disk_warn_gb=settings.low_disk_warn_gb,
        cooldown_minutes=settings.alert_cooldown_minutes,
    )
    cookie_manager = ChromeSessionManager(
        profile_dir=settings.chrome_profile_dir,
        cookies_file=settings.cookies_file,
        refresh_interval_minutes=settings.cookie_refresh_interval_minutes,
        on_status_update=alert_monitor.evaluate,
    )
    downloader = YouTubeDownloader(
        downloads_dir=settings.downloads_dir,
        cookie_manager=cookie_manager,
        max_file_size_bytes=settings.max_file_size_bytes,
        js_runtime=settings.ytdlp_js_runtime,
        pot_provider_url=settings.pot_provider_url,
        sleep_requests=settings.ytdlp_sleep_requests,
        concurrent_fragments=settings.ytdlp_concurrent_fragments,
        socket_timeout_seconds=settings.ytdlp_socket_timeout_seconds,
        fetch_timeout_seconds=settings.fetch_info_timeout_seconds,
        download_timeout_seconds=settings.download_timeout_seconds,
        video_crf=settings.video_crf,
        video_preset=settings.video_preset,
    )
    ig_downloader = InstagramDownloader(
        downloads_dir=settings.downloads_dir,
        max_file_size_bytes=settings.max_file_size_bytes,
        video_crf=settings.video_crf,
        video_preset=settings.video_preset,
    )
    tiktok_downloader = TikTokDownloader(
        downloads_dir=settings.downloads_dir,
        max_file_size_bytes=settings.max_file_size_bytes,
        socket_timeout_seconds=settings.ytdlp_socket_timeout_seconds,
        fetch_timeout_seconds=settings.fetch_info_timeout_seconds,
        download_timeout_seconds=settings.download_timeout_seconds,
        video_crf=settings.video_crf,
        video_preset=settings.video_preset,
    )
    job_store = PendingJobStore(
        max_concurrent_downloads=settings.max_concurrent_downloads,
        queue_wait_timeout_seconds=settings.queue_wait_timeout_seconds,
        queue_max_size=settings.queue_max_size,
        queue_task_gap_seconds=settings.queue_task_gap_seconds,
    )
    rate_limiter = RateLimiter(
        cooldown_seconds=settings.user_cooldown_seconds,
        hourly_limit=settings.user_hourly_limit,
    )

    dp = Dispatcher(storage=MemoryStorage())

    whitelist = WhitelistMiddleware(access_manager)
    dp.message.outer_middleware(whitelist)
    dp.callback_query.outer_middleware(whitelist)

    dp.include_router(commands.router)
    dp.include_router(youtube.router)

    dp["settings"] = settings
    dp["access_manager"] = access_manager
    dp["media_cache"] = media_cache
    dp["cookie_manager"] = cookie_manager
    dp["downloader"] = downloader
    dp["ig_downloader"] = ig_downloader
    dp["tiktok_downloader"] = tiktok_downloader
    dp["job_store"] = job_store
    dp["rate_limiter"] = rate_limiter

    await cookie_manager.start()
    try:
        detected_username = await wait_for_bot_api(bot)
        dp["bot_username"] = (
            settings.bot_username.strip().lstrip("@").lower() or detected_username
        )
        await register_commands(bot)
        await alert_monitor.evaluate(cookie_manager.status)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await cookie_manager.stop()
        await bot.session.close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        stream=sys.stdout,
    )
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped.")


if __name__ == "__main__":
    main()

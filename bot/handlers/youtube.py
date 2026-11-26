from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import Bot, F, Router
from aiogram.enums import ChatAction, ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot.config import Settings
from bot.services.access import AccessManager
from bot.services.cache import CachedMedia, MediaCache
from bot.services.downloader import (
    AuthRequiredError,
    DownloadError,
    DownloadedMedia,
    DownloadProgress,
    FileTooLargeError,
    VideoInfo,
    YouTubeDownloader,
)
from bot.services.instagram import InstagramDownloader
from bot.services.jobs import PendingJob, PendingJobStore, QueueError, RateLimiter
from bot.services.tiktok import TikTokDownloader
from bot.utils.parsers import (
    TimeRange,
    extract_instagram_url,
    extract_tiktok_url,
    extract_youtube_url,
    format_duration,
    format_filesize,
    parse_instagram_request,
    parse_tiktok_request,
    parse_time_range,
    parse_user_request,
)

logger = logging.getLogger(__name__)
router = Router(name="youtube")


class TrimStates(StatesGroup):
    waiting_for_range = State()


def sanitize_telegram_filename(title: str, suffix: str) -> str:
    """Build a readable filename for send_document without forbidden path chars."""
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", title).strip(" .")
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        cleaned = "youtube_video"
    if len(cleaned) > 120:
        cleaned = cleaned[:120].rstrip(" .")
    return f"{cleaned}{suffix}"


def _build_menu_text(job: PendingJob) -> str:
    info = job.info
    if job.platform == "instagram":
        kind_badge = "📸 <b>Instagram Reels</b>"
    elif job.platform == "tiktok":
        kind_badge = "🎵 <b>TikTok</b>"
    elif info.is_shorts:
        kind_badge = "📱 <b>YouTube Shorts</b>"
    else:
        kind_badge = "🎬 <b>YouTube Видео</b>"
    send_mode = "📄 Файл (без сжатия)" if job.as_document else "🎬 Видео (в плеере)"
    lines = [
        kind_badge,
        f"<b>{html.escape(info.title)}</b>",
        f"👤 {html.escape(info.uploader)} • ⏱ {format_duration(info.duration)}",
        f"📤 <b>Отправка:</b> {send_mode}",
    ]
    if job.time_range is not None:
        lines.append(
            f"✂️ <b>Обрезка:</b> <code>{job.time_range.label()}</code> "
            f"({format_duration(job.time_range.duration_seconds)})"
        )
    if info.is_live:
        lines.append("⚠️ Это активная трансляция; скачивание может быть ограничено.")
    lines.append("\nВыбери качество для скачивания:")
    return "\n".join(lines)


def _build_menu_keyboard(job: PendingJob) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text="🚀 Лучшее (видео)",
                callback_data=f"dl:{job.job_id}:best",
            ),
            InlineKeyboardButton(
                text="📄 Лучшее (файлом)",
                callback_data=f"dl:{job.job_id}:best_doc",
            ),
        ]
    ]

    res_buttons: list[InlineKeyboardButton] = []
    for height in job.info.available_heights:
        est = job.info.estimated_sizes.get(height)
        est_suffix = f" (~{format_filesize(est)})" if est and job.time_range is None else ""
        res_buttons.append(
            InlineKeyboardButton(
                text=f"📹 {height}p{est_suffix}",
                callback_data=f"dl:{job.job_id}:{height}",
            )
        )

    for i in range(0, len(res_buttons), 2):
        rows.append(res_buttons[i : i + 2])

    action_row = [
        InlineKeyboardButton(
            text="🎵 Аудио MP3",
            callback_data=f"dl:{job.job_id}:audio",
        ),
        InlineKeyboardButton(
            text="✂️ Изменить отрезок" if job.time_range else "✂️ Обрезать",
            callback_data=f"trim:{job.job_id}",
        ),
    ]
    rows.append(action_row)

    doc_toggle_text = "📄 Отправлять файлом: ✅" if job.as_document else "📄 Отправлять файлом: ❌"
    rows.append(
        [
            InlineKeyboardButton(
                text=doc_toggle_text,
                callback_data=f"toggledoc:{job.job_id}",
            )
        ]
    )

    footer_row: list[InlineKeyboardButton] = []
    if job.time_range is not None:
        footer_row.append(
            InlineKeyboardButton(
                text="🔄 Сбросить обрезку",
                callback_data=f"cleartrim:{job.job_id}",
            )
        )
    footer_row.append(
        InlineKeyboardButton(
            text="✖️ Отмена",
            callback_data=f"cancel:{job.job_id}",
        )
    )
    rows.append(footer_row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _safe_edit(message: Message, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
    try:
        await message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            logger.debug("edit_text failed: %s", exc)


def _is_group_auto_media(txt: str | None) -> bool:
    if not txt:
        return False
    yt = extract_youtube_url(txt)
    if yt is not None and yt.is_shorts:
        return True
    ig = extract_instagram_url(txt)
    if ig is not None and ig.is_reel:
        return True
    return extract_tiktok_url(txt) is not None


def _has_supported_media_url(txt: str | None) -> bool:
    if not txt:
        return False
    return (
        extract_youtube_url(txt) is not None
        or extract_instagram_url(txt) is not None
        or extract_tiktok_url(txt) is not None
    )


_DL_COMMANDS = {"/dl", "/v", "/get", "/download"}


def _get_message_text(message: Message) -> str:
    return (message.text or message.caption or "").strip()


def _has_dl_command(message: Message) -> bool:
    text = _get_message_text(message)
    if not text.startswith("/"):
        return False
    first_token = text.split(maxsplit=1)[0].lower()
    cmd_name = first_token.split("@", 1)[0]
    return cmd_name in _DL_COMMANDS


def is_bot_mentioned(message: Message, bot_username: str, bot_id: int | None = None) -> bool:
    """Return True if message uses /dl command, mentions @bot_username, or replies to the bot."""
    if _has_dl_command(message):
        return True

    if (
        bot_id is not None
        and message.reply_to_message is not None
        and message.reply_to_message.from_user is not None
        and message.reply_to_message.from_user.id == bot_id
    ):
        return True

    uname = bot_username.lstrip("@").lower()
    if not uname:
        return False

    target = f"@{uname}"
    text = message.text or ""
    for entity in message.entities or []:
        if entity.type == "mention":
            snippet = text[entity.offset : entity.offset + entity.length].lower()
            if snippet == target:
                return True

    caption = message.caption or ""
    for entity in message.caption_entities or []:
        if entity.type == "mention":
            snippet = caption[entity.offset : entity.offset + entity.length].lower()
            if snippet == target:
                return True

    return target in (text or caption).lower()


async def _send_from_cache(
    *,
    bot: Bot,
    chat_id: int,
    cached: CachedMedia,
    caption: str,
    reply_to_message: Message | None = None,
) -> bool:
    """Try sending a cached Telegram file_id; return False if Telegram rejects it."""
    try:
        if cached.media_kind == "document":
            if reply_to_message is not None:
                await reply_to_message.reply_document(
                    document=cached.file_id, caption=caption, parse_mode="HTML"
                )
            else:
                await bot.send_document(
                    chat_id=chat_id, document=cached.file_id, caption=caption, parse_mode="HTML"
                )
        elif cached.media_kind == "audio":
            if reply_to_message is not None:
                await reply_to_message.reply_audio(
                    audio=cached.file_id,
                    title=cached.title,
                    performer=cached.uploader or None,
                    duration=cached.duration,
                    caption=caption,
                    parse_mode="HTML",
                )
            else:
                await bot.send_audio(
                    chat_id=chat_id,
                    audio=cached.file_id,
                    title=cached.title,
                    performer=cached.uploader or None,
                    duration=cached.duration,
                    caption=caption,
                    parse_mode="HTML",
                )
        else:
            if reply_to_message is not None:
                await reply_to_message.reply_video(
                    video=cached.file_id,
                    width=cached.width,
                    height=cached.height,
                    duration=cached.duration,
                    supports_streaming=True,
                    caption=caption,
                    parse_mode="HTML",
                )
            else:
                await bot.send_video(
                    chat_id=chat_id,
                    video=cached.file_id,
                    width=cached.width,
                    height=cached.height,
                    duration=cached.duration,
                    supports_streaming=True,
                    caption=caption,
                    parse_mode="HTML",
                )
        return True
    except TelegramBadRequest as exc:
        logger.warning("Cached file_id rejected by Telegram (%s); re-downloading.", exc)
        return False


def _save_to_cache(
    media_cache: MediaCache,
    *,
    cache_key: str,
    sent_msg: Message,
    media: DownloadedMedia,
    title: str,
    uploader: str,
    send_as_document: bool,
) -> None:
    file_id: str | None = None
    media_kind = "video"
    if send_as_document:
        if not sent_msg.document:
            return
        file_id = sent_msg.document.file_id
        media_kind = "document"
    elif media.is_audio and sent_msg.audio:
        file_id = sent_msg.audio.file_id
        media_kind = "audio"
    elif sent_msg.video:
        file_id = sent_msg.video.file_id
        media_kind = "video"
    elif sent_msg.document:
        file_id = sent_msg.document.file_id
        media_kind = "document"

    if not file_id:
        return

    media_cache.put(
        cache_key,
        CachedMedia(
            file_id=file_id,
            media_kind=media_kind,
            title=title,
            uploader=uploader,
            duration=media.duration,
            width=media.width,
            height=media.height,
            size_bytes=media.size_bytes,
            created_at=time.time(),
        ),
    )


def _clamp_time_range(
    time_range: TimeRange | None,
    duration: float | None,
) -> TimeRange | None:
    if time_range is None or not duration:
        return time_range
    try:
        return parse_time_range(
            f"{time_range.start_str}-{time_range.end_str}",
            max_duration=duration,
        )
    except ValueError:
        return None


async def _try_group_cache_hit(
    *,
    bot: Bot,
    message: Message,
    status_msg: Message,
    media_cache: MediaCache,
    cache_key: str,
    badge: str,
    time_range: TimeRange | None,
) -> bool:
    cached = media_cache.get(cache_key)
    if cached is None:
        return False

    caption = f"{badge} <b>{html.escape(cached.title)}</b>"
    if time_range:
        caption += f"\n✂️ <code>{time_range.label()}</code>"
    if await _send_from_cache(
        bot=bot,
        chat_id=message.chat.id,
        cached=cached,
        caption=caption[:1000],
        reply_to_message=message,
    ):
        try:
            await status_msg.delete()
        except TelegramBadRequest:
            pass
        return True

    media_cache.delete(cache_key)
    return False


def _is_group_trigger(message: Message) -> bool:
    if _has_supported_media_url(_get_message_text(message)):
        return True
    return _has_dl_command(message)


def check_group_duration_limit(
    duration_seconds: float | int | None,
    time_range: TimeRange | None,
    *,
    is_live: bool = False,
    max_duration_seconds: int = 120,
) -> str | None:
    """Return a rejection message if a group request is a live stream or exceeds max_duration_seconds."""
    if is_live:
        return "⛔ Прямые трансляции в групповых чатах не скачиваются."
    if max_duration_seconds <= 0:
        return None

    effective = time_range.duration_seconds if time_range is not None else duration_seconds
    if effective is not None and effective > max_duration_seconds:
        return (
            f"⏱ Видео слишком длинное для группового чата ({format_duration(effective)}).\n"
            f"В группах ограничение до {format_duration(max_duration_seconds)} "
            "(отправь ссылку мне в личку или укажи короткий таймкод)."
        )
    return None


@router.message(
    F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
    F.func(_is_group_trigger),
)
async def handle_group_shorts(
    message: Message,
    bot: Bot,
    downloader: YouTubeDownloader,
    ig_downloader: InstagramDownloader,
    tiktok_downloader: TikTokDownloader,
    media_cache: MediaCache,
    job_store: PendingJobStore,
    bot_username: str = "",
    settings: Settings | None = None,
    access_manager: AccessManager | None = None,
    rate_limiter: RateLimiter | None = None,
) -> None:
    """Download media in authorized group chats (/dl, reply-to-bot, @mention, or auto Shorts/Reels/TikTok)."""
    own_text = _get_message_text(message)
    reply_text = (
        _get_message_text(message.reply_to_message)
        if message.reply_to_message is not None
        else ""
    )
    if _has_supported_media_url(own_text):
        raw_text = own_text
        mentioned = is_bot_mentioned(message, bot_username, bot_id=bot.id)
    elif _has_dl_command(message) and _has_supported_media_url(reply_text):
        raw_text = reply_text
        mentioned = True
    elif _has_dl_command(message):
        mention = f"@{bot_username}" if bot_username else ""
        cmd_hint = f"/dl{mention}" if mention else "/dl"
        await message.reply(
            f"Укажи ссылку после команды, например <code>{cmd_hint} https://youtu.be/...</code>, "
            f"или ответь командой <code>{cmd_hint}</code> на сообщение со ссылкой.",
            parse_mode="HTML",
        )
        return
    else:
        return

    logger.info(
        "Group media candidate in chat_id=%s from user_id=%s mentioned=%s",
        message.chat.id,
        getattr(message.from_user, "id", None),
        mentioned,
    )
    platform = ""
    yt_parsed = parse_user_request(raw_text)
    ig_parsed = None
    tt_parsed = None

    if yt_parsed is not None:
        if not yt_parsed.video.is_shorts and not mentioned:
            logger.info("Skipping non-Shorts YouTube link in group chat_id=%s (not mentioned)", message.chat.id)
            return
        platform = "youtube"
        label = "Shorts" if yt_parsed.video.is_shorts else "видео"
        badge = "📱" if yt_parsed.video.is_shorts else "🎬"
    elif (ig_parsed := parse_instagram_request(raw_text)) is not None:
        if not ig_parsed.media.is_reel and not mentioned:
            logger.info("Skipping non-Reel Instagram link in group chat_id=%s (not mentioned)", message.chat.id)
            return
        platform = "instagram"
        label = "Reels" if ig_parsed.media.is_reel else "Instagram"
        badge = "📸"
    elif (tt_parsed := parse_tiktok_request(raw_text)) is not None:
        platform = "tiktok"
        label = "TikTok"
        badge = "🎵"
    else:
        return

    max_group_dur = settings.group_max_duration_seconds if settings else 120
    max_group_bytes = settings.group_max_file_size_bytes if settings else 150 * 1024 * 1024
    group_task_timeout = (
        float(settings.fetch_info_timeout_seconds + settings.download_timeout_seconds)
        if settings
        else 345.0
    )
    sender_id = message.from_user.id if message.from_user else 0
    sender_is_admin = bool(access_manager and sender_id and access_manager.is_admin(sender_id))

    status_msg = await message.reply(f"⏳ Скачиваю {label}...")
    queued_notice_shown = False

    async def _on_group_queued(pos: int) -> None:
        nonlocal queued_notice_shown
        queued_notice_shown = True
        await _safe_edit(status_msg, f"🚦 В очереди (#{pos}) для {label}...")

    media: DownloadedMedia | None = None
    try:
        async with job_store.queue.slot(
            on_queued=_on_group_queued,
            task_timeout=group_task_timeout,
            task_timeout_message=f"⏳ Превышено время обработки {label}. Попробуй позже.",
        ):
            if queued_notice_shown:
                await _safe_edit(status_msg, f"⏳ Скачиваю {label}...")
            async def _on_group_stage(stage: str) -> None:
                if stage == "compressing":
                    await _safe_edit(status_msg, f"🗜 Сжимаю {label} для Telegram...")

            if platform == "instagram":
                assert ig_parsed is not None
                validated_range = ig_parsed.time_range
                cache_key = MediaCache.make_key(
                    platform="instagram",
                    video_id=ig_parsed.media.shortcode,
                    quality="best",
                    as_document=False,
                    time_range=validated_range,
                )
                if await _try_group_cache_hit(
                    bot=bot,
                    message=message,
                    status_msg=status_msg,
                    media_cache=media_cache,
                    cache_key=cache_key,
                    badge=badge,
                    time_range=validated_range,
                ):
                    return

                if dur_err := check_group_duration_limit(
                    None, validated_range, max_duration_seconds=max_group_dur
                ):
                    await _safe_edit(status_msg, dur_err)
                    return

                if rate_limiter is not None and sender_id:
                    if limit_err := rate_limiter.check_and_record(
                        sender_id, is_admin=sender_is_admin
                    ):
                        await _safe_edit(status_msg, limit_err)
                        return

                media = await ig_downloader.download(
                    shortcode=ig_parsed.media.shortcode,
                    kind=ig_parsed.media.kind,
                    canonical_url=ig_parsed.media.canonical_url,
                    quality="best",
                    time_range=validated_range,
                    compress=True,
                    on_stage=_on_group_stage,
                )
                title = media.title
                uploader = media.uploader

            elif platform == "tiktok":
                assert tt_parsed is not None
                info = await tiktok_downloader.fetch_info(tt_parsed.media.canonical_url)
                validated_range = _clamp_time_range(tt_parsed.time_range, info.duration)
                cache_key = MediaCache.make_key(
                    platform="tiktok",
                    video_id=info.video_id or tt_parsed.media.canonical_url,
                    quality="best",
                    as_document=False,
                    time_range=validated_range,
                )
                if await _try_group_cache_hit(
                    bot=bot,
                    message=message,
                    status_msg=status_msg,
                    media_cache=media_cache,
                    cache_key=cache_key,
                    badge=badge,
                    time_range=validated_range,
                ):
                    return

                if dur_err := check_group_duration_limit(
                    info.duration,
                    validated_range,
                    is_live=info.is_live,
                    max_duration_seconds=max_group_dur,
                ):
                    await _safe_edit(status_msg, dur_err)
                    return

                if rate_limiter is not None and sender_id:
                    if limit_err := rate_limiter.check_and_record(
                        sender_id, is_admin=sender_is_admin
                    ):
                        await _safe_edit(status_msg, limit_err)
                        return

                media = await tiktok_downloader.download(
                    info.webpage_url,
                    quality="best",
                    is_portrait=info.is_portrait,
                    time_range=validated_range,
                    compress=True,
                    on_stage=_on_group_stage,
                )
                title = info.title
                uploader = info.uploader

            else:
                assert yt_parsed is not None
                info = await downloader.fetch_info(
                    yt_parsed.video.canonical_url,
                    is_shorts=yt_parsed.video.is_shorts,
                )
                validated_range = _clamp_time_range(yt_parsed.time_range, info.duration)
                cache_key = MediaCache.make_key(
                    platform="youtube",
                    video_id=info.video_id,
                    quality="best",
                    as_document=False,
                    time_range=validated_range,
                )
                if await _try_group_cache_hit(
                    bot=bot,
                    message=message,
                    status_msg=status_msg,
                    media_cache=media_cache,
                    cache_key=cache_key,
                    badge=badge,
                    time_range=validated_range,
                ):
                    return

                if dur_err := check_group_duration_limit(
                    info.duration,
                    validated_range,
                    is_live=info.is_live,
                    max_duration_seconds=max_group_dur,
                ):
                    await _safe_edit(status_msg, dur_err)
                    return

                if rate_limiter is not None and sender_id:
                    if limit_err := rate_limiter.check_and_record(
                        sender_id, is_admin=sender_is_admin
                    ):
                        await _safe_edit(status_msg, limit_err)
                        return

                media = await downloader.download(
                    info.webpage_url,
                    quality="best",
                    is_portrait=info.is_portrait,
                    time_range=validated_range,
                    compress=True,
                    on_stage=_on_group_stage,
                )
                title = info.title
                uploader = info.uploader

        if dur_err := check_group_duration_limit(
            media.duration, validated_range, max_duration_seconds=max_group_dur
        ):
            await _safe_edit(status_msg, dur_err)
            return

        if max_group_bytes > 0 and media.size_bytes > max_group_bytes:
            await _safe_edit(
                status_msg,
                f"📦 Видео ({format_filesize(media.size_bytes)}) превышает лимит для групповых чатов "
                f"({format_filesize(max_group_bytes)}). Отправь ссылку мне в личку.",
            )
            return

        caption = f"{badge} <b>{html.escape(title)}</b>"
        if validated_range:
            caption += f"\n✂️ <code>{validated_range.label()}</code>"
        if len(caption) > 1000:
            caption = caption[:997] + "..."

        tg_filename = sanitize_telegram_filename(title, media.file_path.suffix)
        await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.UPLOAD_VIDEO)
        sent_msg = await message.reply_video(
            video=FSInputFile(media.file_path, filename=tg_filename),
            width=media.width,
            height=media.height,
            duration=media.duration,
            supports_streaming=True,
            caption=caption,
            parse_mode="HTML",
        )
        _save_to_cache(
            media_cache,
            cache_key=cache_key,
            sent_msg=sent_msg,
            media=media,
            title=title,
            uploader=uploader,
            send_as_document=False,
        )
        try:
            await status_msg.delete()
        except TelegramBadRequest:
            pass
    except QueueError as exc:
        await _safe_edit(status_msg, html.escape(str(exc)))
    except AuthRequiredError:
        await _safe_edit(
            status_msg,
            "❌ YouTube отклонил сессию бота — администратору нужно обновить вход через <code>chrome-auth</code>.",
        )
    except FileTooLargeError as exc:
        await _safe_edit(status_msg, f"❌ {html.escape(str(exc))}")
    except DownloadError as exc:
        await _safe_edit(
            status_msg,
            f"❌ Не удалось скачать {label}:\n<code>{html.escape(str(exc)[:300])}</code>",
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("Unexpected error in handle_group_shorts: %s", exc)
        await _safe_edit(
            status_msg,
            f"❌ Ошибка при отправке {label}:\n<code>{html.escape(str(exc)[:300])}</code>",
        )
    finally:
        if media is not None:
            media.cleanup()


async def _fetch_in_queue(
    *,
    job_store: PendingJobStore,
    status_msg: Message,
    fetch_coro_factory: Callable[[], Awaitable[VideoInfo]],
    active_text: str,
    settings: Settings | None = None,
) -> VideoInfo:
    """Serialize metadata lookup through the global FIFO queue with position feedback."""
    fetch_timeout = float(settings.fetch_info_timeout_seconds) if settings else 45.0
    queued = False

    async def _on_queued(pos: int) -> None:
        nonlocal queued
        queued = True
        await _safe_edit(status_msg, f"🚦 В очереди на получение данных (#{pos})...")

    async with job_store.queue.slot(
        on_queued=_on_queued,
        task_timeout=fetch_timeout,
        task_timeout_message=f"⏳ Превышено время получения данных о видео ({int(fetch_timeout)} сек.).",
    ):
        if queued:
            await _safe_edit(status_msg, active_text)
        return await fetch_coro_factory()


@router.message(
    F.chat.type == ChatType.PRIVATE,
    F.text.func(lambda txt: bool(txt and extract_youtube_url(txt))),
)
async def handle_youtube_link(
    message: Message,
    state: FSMContext,
    downloader: YouTubeDownloader,
    job_store: PendingJobStore,
    settings: Settings | None = None,
) -> None:
    await state.clear()
    assert message.text is not None
    assert message.from_user is not None

    parsed = parse_user_request(message.text)
    if parsed is None:
        await message.answer("Не удалось распознать ссылку на YouTube.")
        return

    status_msg = await message.answer("🔍 Получаю информацию о видео...")

    try:
        info = await _fetch_in_queue(
            job_store=job_store,
            status_msg=status_msg,
            active_text="🔍 Получаю информацию о видео...",
            settings=settings,
            fetch_coro_factory=lambda: downloader.fetch_info(
                parsed.video.canonical_url,
                is_shorts=parsed.video.is_shorts,
            ),
        )
    except QueueError as exc:
        await _safe_edit(status_msg, html.escape(str(exc)))
        return
    except AuthRequiredError as exc:
        await _safe_edit(
            status_msg,
            "❌ <b>YouTube отклонил сессию (требуется вход / проверка).</b>\n\n"
            "Автоматическое обновление cookies не помогло. Авторизуйся заново через <code>chrome-auth</code> (noVNC) "
            "и затем проверь <code>/status</code>.\n\n"
            f"<i>Детали:</i> <code>{html.escape(str(exc)[:400])}</code>",
        )
        return
    except DownloadError as exc:
        await _safe_edit(
            status_msg,
            f"❌ Не удалось получить данные видео:\n<code>{html.escape(str(exc)[:400])}</code>",
        )
        return
    except Exception as exc:
        logger.exception("Unexpected error fetching info: %s", exc)
        await _safe_edit(
            status_msg,
            f"❌ Внутренняя ошибка при получении метаданных:\n<code>{html.escape(str(exc)[:400])}</code>",
        )
        return

    validated_range: TimeRange | None = parsed.time_range
    warning_note = ""
    if validated_range is not None and info.duration:
        try:
            validated_range = parse_time_range(
                f"{validated_range.start_str}-{validated_range.end_str}",
                max_duration=info.duration,
            )
        except ValueError as exc:
            validated_range = None
            warning_note = f"\n\n⚠️ Таймкод проигнорирован: {html.escape(str(exc))}"

    job = job_store.create(
        user_id=message.from_user.id,
        chat_id=message.chat.id,
        info=info,
        time_range=validated_range,
    )

    await _safe_edit(
        status_msg,
        _build_menu_text(job) + warning_note,
        reply_markup=_build_menu_keyboard(job),
    )


@router.message(
    F.chat.type == ChatType.PRIVATE,
    F.text.func(lambda txt: bool(txt and extract_instagram_url(txt))),
)
async def handle_instagram_link(
    message: Message,
    state: FSMContext,
    ig_downloader: InstagramDownloader,
    job_store: PendingJobStore,
    settings: Settings | None = None,
) -> None:
    await state.clear()
    assert message.text is not None
    assert message.from_user is not None

    parsed = parse_instagram_request(message.text)
    if parsed is None:
        await message.answer("Не удалось распознать ссылку на Instagram.")
        return

    status_msg = await message.answer("🔍 Получаю данные Instagram Reels...")
    try:
        info = await _fetch_in_queue(
            job_store=job_store,
            status_msg=status_msg,
            active_text="🔍 Получаю данные Instagram Reels...",
            settings=settings,
            fetch_coro_factory=lambda: ig_downloader.fetch_info(
                shortcode=parsed.media.shortcode,
                kind=parsed.media.kind,
                canonical_url=parsed.media.canonical_url,
            ),
        )
    except QueueError as exc:
        await _safe_edit(status_msg, html.escape(str(exc)))
        return

    job = job_store.create(
        user_id=message.from_user.id,
        chat_id=message.chat.id,
        info=info,
        platform="instagram",
        ig_kind=parsed.media.kind,
        time_range=parsed.time_range,
    )

    await _safe_edit(
        status_msg,
        _build_menu_text(job),
        reply_markup=_build_menu_keyboard(job),
    )


@router.message(
    F.chat.type == ChatType.PRIVATE,
    F.text.func(lambda txt: bool(txt and extract_tiktok_url(txt))),
)
async def handle_tiktok_link(
    message: Message,
    state: FSMContext,
    tiktok_downloader: TikTokDownloader,
    job_store: PendingJobStore,
    settings: Settings | None = None,
) -> None:
    await state.clear()
    assert message.text is not None
    assert message.from_user is not None

    parsed = parse_tiktok_request(message.text)
    if parsed is None:
        await message.answer("Не удалось распознать ссылку на TikTok.")
        return

    status_msg = await message.answer("🔍 Получаю информацию о TikTok...")
    try:
        info = await _fetch_in_queue(
            job_store=job_store,
            status_msg=status_msg,
            active_text="🔍 Получаю информацию о TikTok...",
            settings=settings,
            fetch_coro_factory=lambda: tiktok_downloader.fetch_info(
                parsed.media.canonical_url
            ),
        )
    except QueueError as exc:
        await _safe_edit(status_msg, html.escape(str(exc)))
        return
    except DownloadError as exc:
        await _safe_edit(
            status_msg,
            f"❌ Не удалось получить данные TikTok:\n<code>{html.escape(str(exc)[:400])}</code>",
        )
        return
    except Exception as exc:
        logger.exception("Unexpected error fetching TikTok info: %s", exc)
        await _safe_edit(
            status_msg,
            f"❌ Внутренняя ошибка при получении метаданных TikTok:\n<code>{html.escape(str(exc)[:400])}</code>",
        )
        return

    validated_range = parsed.time_range
    warning_note = ""
    if validated_range is not None and info.duration:
        try:
            validated_range = parse_time_range(
                f"{validated_range.start_str}-{validated_range.end_str}",
                max_duration=info.duration,
            )
        except ValueError as exc:
            validated_range = None
            warning_note = f"\n\n⚠️ Таймкод проигнорирован: {html.escape(str(exc))}"

    job = job_store.create(
        user_id=message.from_user.id,
        chat_id=message.chat.id,
        info=info,
        platform="tiktok",
        time_range=validated_range,
    )

    await _safe_edit(
        status_msg,
        _build_menu_text(job) + warning_note,
        reply_markup=_build_menu_keyboard(job),
    )


@router.message(TrimStates.waiting_for_range, F.text)
async def handle_trim_input(
    message: Message,
    state: FSMContext,
    job_store: PendingJobStore,
    bot: Bot,
) -> None:
    assert message.text is not None
    assert message.from_user is not None

    data = await state.get_data()
    job_id = data.get("job_id")
    menu_message_id = data.get("menu_message_id")

    job = job_store.get(str(job_id), message.from_user.id) if job_id else None
    if job is None:
        await state.clear()
        await message.answer("⏳ Запрос устарел. Отправь ссылку на видео ещё раз.")
        return

    try:
        time_range = parse_time_range(message.text, max_duration=job.info.duration)
    except ValueError as exc:
        await message.answer(
            f"⚠️ {html.escape(str(exc))}\nПопробуй ещё раз (например <code>01:15-02:30</code>) или нажми /cancel.",
            parse_mode="HTML",
        )
        return

    job.time_range = time_range
    await state.clear()

    if isinstance(menu_message_id, int):
        try:
            await bot.edit_message_text(
                chat_id=job.chat_id,
                message_id=menu_message_id,
                text=_build_menu_text(job),
                parse_mode="HTML",
                reply_markup=_build_menu_keyboard(job),
                disable_web_page_preview=True,
            )
            await message.answer(f"✂️ Отрезок установлен: <code>{time_range.label()}</code>", parse_mode="HTML")
            return
        except TelegramBadRequest:
            pass

    await message.answer(
        _build_menu_text(job),
        parse_mode="HTML",
        reply_markup=_build_menu_keyboard(job),
        disable_web_page_preview=True,
    )


@router.callback_query(F.data.startswith("trim:"))
async def cb_trim(
    callback: CallbackQuery,
    state: FSMContext,
    job_store: PendingJobStore,
) -> None:
    job_id = (callback.data or "").split(":", 1)[1]
    job = job_store.get(job_id, callback.from_user.id)
    if job is None or not isinstance(callback.message, Message):
        await callback.answer("Запрос устарел, отправь ссылку заново.", show_alert=True)
        return

    await state.set_state(TrimStates.waiting_for_range)
    await state.update_data(job_id=job.job_id, menu_message_id=callback.message.message_id)
    await callback.answer()
    await callback.message.answer(
        "✂️ Отправь отрезок времени в формате <code>ММ:СС-ММ:СС</code> или <code>ЧЧ:ММ:СС-ЧЧ:ММ:СС</code>.\n"
        f"Длительность видео: <b>{format_duration(job.info.duration)}</b>\n"
        "Например: <code>00:30-01:45</code> (или /cancel для отмены).",
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("toggledoc:"))
async def cb_toggle_doc(callback: CallbackQuery, job_store: PendingJobStore) -> None:
    job_id = (callback.data or "").split(":", 1)[1]
    job = job_store.get(job_id, callback.from_user.id)
    if job is None or not isinstance(callback.message, Message):
        await callback.answer("Запрос устарел.", show_alert=True)
        return

    job.as_document = not job.as_document
    await callback.answer(
        "Теперь отправлю как файл" if job.as_document else "Теперь отправлю как видео"
    )
    await _safe_edit(
        callback.message,
        _build_menu_text(job),
        reply_markup=_build_menu_keyboard(job),
    )


@router.callback_query(F.data.startswith("cleartrim:"))
async def cb_clear_trim(callback: CallbackQuery, job_store: PendingJobStore) -> None:
    job_id = (callback.data or "").split(":", 1)[1]
    job = job_store.get(job_id, callback.from_user.id)
    if job is None or not isinstance(callback.message, Message):
        await callback.answer("Запрос устарел.", show_alert=True)
        return

    job.time_range = None
    await callback.answer("Обрезка сброшена")
    await _safe_edit(
        callback.message,
        _build_menu_text(job),
        reply_markup=_build_menu_keyboard(job),
    )


@router.callback_query(F.data.startswith("cancel:"))
async def cb_cancel(
    callback: CallbackQuery,
    state: FSMContext,
    job_store: PendingJobStore,
) -> None:
    job_id = (callback.data or "").split(":", 1)[1]
    job_store.delete(job_id)
    await state.clear()
    await callback.answer("Отменено")
    if isinstance(callback.message, Message):
        await _safe_edit(callback.message, "✖️ Загрузка отменена.")


@router.callback_query(F.data.startswith("dl:"))
async def cb_download(
    callback: CallbackQuery,
    state: FSMContext,
    bot: Bot,
    downloader: YouTubeDownloader,
    ig_downloader: InstagramDownloader,
    tiktok_downloader: TikTokDownloader,
    media_cache: MediaCache,
    job_store: PendingJobStore,
    access_manager: AccessManager | None = None,
    rate_limiter: RateLimiter | None = None,
    settings: Settings | None = None,
) -> None:
    parts = (callback.data or "").split(":")
    if len(parts) != 3:
        await callback.answer("Некорректные данные кнопки.", show_alert=True)
        return

    _, job_id, quality = parts
    job = job_store.get(job_id, callback.from_user.id)
    if job is None or not isinstance(callback.message, Message):
        await callback.answer("Запрос устарел, отправь ссылку заново.", show_alert=True)
        return

    await state.clear()
    await callback.answer("Запускаю скачивание...")
    status_msg = callback.message
    job_store.delete(job_id)

    send_as_document = job.as_document or quality == "best_doc"
    download_quality = "best" if quality == "best_doc" else quality

    quality_label = (
        "MP3 аудио"
        if download_quality == "audio"
        else ("лучшее качество" if download_quality == "best" else f"{download_quality}p")
    )
    if send_as_document:
        quality_label += " (файлом)"
    trim_info = f"\n✂️ Отрезок: <code>{job.time_range.label()}</code>" if job.time_range else ""
    header = f"🎬 <b>{html.escape(job.info.title)}</b>\n⚙️ Режим: {quality_label}{trim_info}"

    last_update_ts = 0.0

    async def _on_progress(prog: DownloadProgress) -> None:
        nonlocal last_update_ts
        now = time.monotonic()
        if now - last_update_ts < 3.5:
            return
        last_update_ts = now

        speed_txt = f"{format_filesize(prog.speed_bps)}/с" if prog.speed_bps else "—"
        eta_txt = f"{prog.eta_seconds}с" if prog.eta_seconds is not None else "—"
        size_txt = (
            f"{format_filesize(prog.downloaded_bytes)} / {format_filesize(prog.total_bytes)}"
            if prog.downloaded_bytes and prog.total_bytes
            else "—"
        )
        await _safe_edit(
            status_msg,
            f"{header}\n\n⬇️ Скачивание: <b>{html.escape(prog.percent_str)}</b>\n"
            f"📦 {size_txt} • 🚀 {speed_txt} • ⏱ ETA {eta_txt}",
        )

    async def _on_stage(stage: str) -> None:
        if stage == "compressing":
            await _safe_edit(
                status_msg,
                f"{header}\n\n🗜 Сжимаю видео для Telegram...",
            )

    cache_key = MediaCache.make_key(
        platform=job.platform,
        video_id=job.info.video_id or job.info.webpage_url,
        quality=download_quality,
        as_document=send_as_document,
        time_range=job.time_range,
    )
    caption = f"🎬 <b>{html.escape(job.info.title)}</b>"
    if job.time_range:
        caption += f"\n✂️ <code>{job.time_range.label()}</code>"
    if len(caption) > 1000:
        caption = caption[:997] + "..."

    cached = media_cache.get(cache_key)
    if cached is not None:
        if (send_as_document and cached.media_kind != "document") or (
            not send_as_document and cached.media_kind == "document"
        ):
            media_cache.delete(cache_key)
        elif await _send_from_cache(
            bot=bot,
            chat_id=job.chat_id,
            cached=cached,
            caption=caption,
        ):
            try:
                await status_msg.delete()
            except TelegramBadRequest:
                pass
            return
        else:
            media_cache.delete(cache_key)

    if rate_limiter is not None:
        is_admin = bool(access_manager and access_manager.is_admin(callback.from_user.id))
        if limit_err := rate_limiter.check_and_record(callback.from_user.id, is_admin=is_admin):
            await _safe_edit(status_msg, f"{header}\n\n{limit_err}")
            return

    dl_timeout = float(settings.download_timeout_seconds) if settings else 300.0

    async def _on_dl_queued(pos: int) -> None:
        await _safe_edit(status_msg, f"{header}\n\n🚦 В очереди на скачивание (#{pos})...")

    media = None
    try:
        async with job_store.queue.slot(
            on_queued=_on_dl_queued,
            task_timeout=dl_timeout,
            task_timeout_message=(
                f"⏳ Скачивание заняло дольше {int(dl_timeout)} сек. и было остановлено. "
                "Попробуй качество пониже или короткий отрезок."
            ),
        ):
            await _safe_edit(status_msg, f"{header}\n\n⬇️ Начинаю скачивание...")
            should_compress = not send_as_document and download_quality != "audio"
            if job.platform == "instagram":
                media = await ig_downloader.download(
                    shortcode=job.info.video_id,
                    kind=job.ig_kind,
                    canonical_url=job.info.webpage_url,
                    quality=download_quality,
                    time_range=job.time_range,
                    compress=should_compress,
                    on_stage=_on_stage,
                )
            elif job.platform == "tiktok":
                media = await tiktok_downloader.download(
                    job.info.webpage_url,
                    quality=download_quality,
                    is_portrait=job.info.is_portrait,
                    time_range=job.time_range,
                    compress=should_compress,
                    on_progress=_on_progress,
                    on_stage=_on_stage,
                )
            else:
                media = await downloader.download(
                    job.info.webpage_url,
                    quality=download_quality,
                    is_portrait=job.info.is_portrait,
                    time_range=job.time_range,
                    compress=should_compress,
                    on_progress=_on_progress,
                    on_stage=_on_stage,
                )

        await _safe_edit(
            status_msg,
            f"{header}\n\n📤 Отправляю в Telegram ({format_filesize(media.size_bytes)})...",
        )

        tg_filename = sanitize_telegram_filename(job.info.title, media.file_path.suffix)
        input_file = FSInputFile(media.file_path, filename=tg_filename)
        if send_as_document:
            await bot.send_chat_action(chat_id=job.chat_id, action=ChatAction.UPLOAD_DOCUMENT)
            sent_msg = await bot.send_document(
                chat_id=job.chat_id,
                document=input_file,
                caption=caption,
                parse_mode="HTML",
                disable_content_type_detection=True,
            )
        elif media.is_audio:
            await bot.send_chat_action(chat_id=job.chat_id, action=ChatAction.UPLOAD_DOCUMENT)
            sent_msg = await bot.send_audio(
                chat_id=job.chat_id,
                audio=input_file,
                title=job.info.title,
                performer=job.info.uploader,
                duration=media.duration,
                caption=caption,
                parse_mode="HTML",
            )
        else:
            await bot.send_chat_action(chat_id=job.chat_id, action=ChatAction.UPLOAD_VIDEO)
            sent_msg = await bot.send_video(
                chat_id=job.chat_id,
                video=input_file,
                width=media.width,
                height=media.height,
                duration=media.duration,
                supports_streaming=True,
                caption=caption,
                parse_mode="HTML",
            )

        _save_to_cache(
            media_cache,
            cache_key=cache_key,
            sent_msg=sent_msg,
            media=media,
            title=job.info.title,
            uploader=job.info.uploader,
            send_as_document=send_as_document,
        )

        try:
            await status_msg.delete()
        except TelegramBadRequest:
            pass

    except QueueError as exc:
        await _safe_edit(status_msg, f"{header}\n\n{html.escape(str(exc))}")
    except FileTooLargeError as exc:
        await _safe_edit(
            status_msg,
            f"{header}\n\n❌ {html.escape(str(exc))}\n"
            "Попробуй выбрать разрешение пониже или обрезать видео по таймкоду.",
        )
    except AuthRequiredError as exc:
        await _safe_edit(
            status_msg,
            f"{header}\n\n❌ <b>Сессия Google/YouTube требует повторного входа.</b>\n"
            "Запусти <code>chrome-auth</code> и войди через noVNC.\n\n"
            f"<code>{html.escape(str(exc)[:400])}</code>",
        )
    except DownloadError as exc:
        await _safe_edit(
            status_msg,
            f"{header}\n\n❌ Ошибка скачивания:\n<code>{html.escape(str(exc)[:500])}</code>",
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("Unexpected error during download/upload: %s", exc)
        await _safe_edit(
            status_msg,
            f"{header}\n\n❌ Ошибка отправки/обработки:\n<code>{html.escape(str(exc)[:500])}</code>",
        )
    finally:
        if media is not None:
            media.cleanup()


@router.message(F.chat.type == ChatType.PRIVATE, F.text)
async def handle_fallback_text(message: Message) -> None:
    await message.answer(
        "Отправь ссылку на YouTube, Shorts, Instagram Reels или TikTok (можно сразу с таймкодом, например `01:15-02:30`).\n"
        "Помощь: /help",
        parse_mode="Markdown",
    )

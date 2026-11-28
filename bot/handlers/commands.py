from __future__ import annotations

import html
import logging
import shutil
from datetime import timezone

from aiogram import F, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import ChatMemberUpdated, Message

from bot.config import Settings
from bot.services.access import AccessManager
from bot.services.cache import MediaCache
from bot.services.chrome_cookies import ChromeSessionManager, SessionStatus
from bot.services.jobs import PendingJobStore
from bot.services.stats import NonAdminStatsSummary, StatsTracker
from bot.utils.parsers import format_filesize

logger = logging.getLogger(__name__)
router = Router(name="commands")


def _format_status(
    status: SessionStatus,
    settings: Settings,
    media_cache: MediaCache | None = None,
    job_store: PendingJobStore | None = None,
) -> str:
    browser_icon = "🟢 Работает" if status.browser_running else "🔴 Остановлен"
    auth_icon = (
        "✅ Авторизован (Google/YouTube cookies найдены)"
        if status.authenticated
        else "⚠️ Не авторизован (запусти сервис <code>chrome-auth</code> и войди через noVNC)"
    )
    cookies_file_status = "✅ Есть" if status.cookies_file_exists else "❌ Отсутствует"
    if status.last_refresh_at:
        refreshed = status.last_refresh_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    else:
        refreshed = "ещё не выполнялось"

    disk = shutil.disk_usage(settings.downloads_dir)
    lines = [
        "<b>Статус бота и Chrome-сессии</b>",
        "",
        f"🌐 <b>Headless Chrome:</b> {browser_icon}",
        f"🔐 <b>Сессия Google:</b> {auth_icon}",
        f"🍪 <b>Файл cookies.txt:</b> {cookies_file_status} ({status.youtube_cookies} YouTube / {status.total_cookies} всего)",
        f"🔄 <b>Последнее обновление:</b> {refreshed}",
        f"💾 <b>Свободно на диске:</b> {format_filesize(disk.free)} из {format_filesize(disk.total)}",
        f"📦 <b>Лимит файла (Local Bot API):</b> {settings.max_file_size_mb} МБ",
    ]
    if media_cache is not None:
        lines.append(f"🗄 <b>Кэш file_id:</b> {media_cache.count} записей")
    if job_store is not None:
        q = job_store.queue
        lines.append(
            f"🚦 <b>Общая очередь:</b> активно {q.active_count}/{q.max_concurrent}, "
            f"в ожидании {q.waiting_count}/{q.max_queue_size}"
        )
    if status.last_error:
        lines.append(f"\n⚠️ <b>Последняя ошибка:</b> <code>{html.escape(status.last_error)}</code>")
    return "\n".join(lines)


@router.my_chat_member(
    F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
    F.new_chat_member.status.in_({ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR}),
)
async def on_added_to_group(
    event: ChatMemberUpdated,
    access_manager: AccessManager,
    bot_username: str = "",
) -> None:
    user = event.from_user
    if not user or not access_manager.check_and_bind(user.id, getattr(user, "username", None)):
        logger.warning(
            "Bot added to group chat_id=%s by unauthorized user_id=%s; leaving chat.",
            event.chat.id,
            getattr(user, "id", None),
        )
        try:
            await event.bot.send_message(
                event.chat.id,
                "⛔ У вас нет доступа к боту — чат не активирован.\n"
                "Напишите администратору, чтобы он выдал вам доступ к боту.",
                parse_mode="HTML",
            )
        except Exception:
            pass
        try:
            await event.bot.leave_chat(event.chat.id)
        except Exception:
            pass
        return

    newly_added = access_manager.add_chat(event.chat.id, event.chat.title)
    logger.info(
        "Whitelisted user %s (@%s) added bot to group chat_id=%s (%s)",
        user.id,
        getattr(user, "username", None),
        event.chat.id,
        event.chat.title,
    )
    if newly_added and not access_manager.is_admin(user.id):
        who = f"@{html.escape(user.username)}" if user.username else f"<code>{user.id}</code>"
        chat_title = html.escape(event.chat.title or str(event.chat.id))
        for admin_id in sorted(access_manager.admin_ids):
            try:
                await event.bot.send_message(
                    admin_id,
                    f"ℹ️ Пользователь {who} (<code>{user.id}</code>) добавил бота в чат "
                    f"<b>{chat_title}</b> (<code>{event.chat.id}</code>).",
                    parse_mode="HTML",
                )
            except Exception:
                pass

    dl_cmd = f"/dl@{bot_username}" if bot_username else "/dl"
    await event.bot.send_message(
        event.chat.id,
        "👋 Привет! Я помогаю скачивать видео с YouTube, YouTube Shorts, Instagram Reels и TikTok.\n\n"
        f"• Отправь <code>{dl_cmd} &lt;ссылка&gt;</code>\n"
        f"• Или ответь (reply) командой <code>{dl_cmd}</code> на сообщение со ссылкой\n\n"
        "Подробная справка: /help",
        parse_mode="HTML",
    )


def _build_group_help(bot_username: str) -> str:
    dl_cmd = f"/dl@{bot_username}" if bot_username else "/dl"
    return (
        "🎬 <b>Справка для группового чата</b>\n\n"
        "<b>Как скачать видео (YouTube, Shorts, Instagram Reels, TikTok):</b>\n"
        f"• Отправь команду с ссылкой:\n  <code>{dl_cmd} https://...</code>\n"
        f"• Или ответь (reply) командой <code>{dl_cmd}</code> на сообщение, где уже есть ссылка\n"
        "• Можно добавить таймкод обрезки после ссылки:\n"
        f"  <code>{dl_cmd} https://youtu.be/... 00:30-01:15</code>\n\n"
        "Если у бота есть доступ ко всем сообщениям в чате, ссылки на <b>Shorts</b>, <b>Reels</b> и <b>TikTok</b> он скачивает автоматически."
    )


def _build_private_help(*, is_admin: bool) -> str:
    text = (
        "👋 <b>Привет! Я скачиваю видео с YouTube, YouTube Shorts, Instagram Reels и TikTok (до 2 ГБ).</b>\n\n"
        "<b>Как пользоваться в личных сообщениях:</b>\n"
        "• Просто отправь ссылку:\n"
        "  <code>https://www.youtube.com/watch?v=...</code>\n"
        "  <code>https://youtube.com/shorts/...</code>\n"
        "  <code>https://www.instagram.com/reel/...</code>\n"
        "  <code>https://www.tiktok.com/@.../video/...</code>\n"
        "• Выбери нужное качество, формат отправки (<b>как видео</b> или <b>как файл</b> без сжатия) или <b>MP3</b>.\n"
        "• Чтобы сразу обрезать по таймкодам, добавь диапазон после ссылки:\n"
        "  <code>https://youtu.be/... 01:15-02:30</code>\n"
        "  или нажми кнопку <b>✂️ Обрезать</b>.\n\n"
        "<b>Команды:</b>\n"
        "/stop — остановить текущее скачивание\n"
        "/cancel — отменить ввод таймкода"
    )
    if is_admin:
        text += (
            "\n\n<b>Админ-команды:</b>\n"
            "/status — статус headless Chrome, сессии Google, кэша, очереди и диска\n"
            "/stats — статистика загрузок других пользователей (кроме админов)\n"
            "/stop all — принудительно остановить текущие задачи в общей очереди\n"
            "/refresh_cookies — обновить cookies из Chrome\n"
            "/clear_cache — очистить кэш Telegram file_id\n"
            "/add @username — разрешить доступ пользователю по @username или ID\n"
            "/remove @username — забрать доступ у пользователя\n"
            "/users — список пользователей с доступом\n"
            "/add_chat [chat_id] — разрешить чат\n"
            "/remove_chat [chat_id] — отключить чат\n"
            "/chats — список разрешённых чатов"
        )
    return text


@router.message(Command("start", "help"))
async def cmd_start(
    message: Message,
    state: FSMContext,
    access_manager: AccessManager,
    bot_username: str = "",
) -> None:
    await state.clear()
    if message.chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}:
        await message.answer(
            _build_group_help(bot_username),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return

    is_admin = bool(message.from_user and access_manager.is_admin(message.from_user.id))
    await message.answer(
        _build_private_help(is_admin=is_admin),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    current = await state.get_state()
    await state.clear()
    if current:
        await message.answer("Ок, ввод таймкода отменён.")
    else:
        await message.answer("Активных действий для отмены нет.")


@router.message(Command("stop"), F.chat.type == ChatType.PRIVATE)
async def cmd_stop(
    message: Message,
    command: CommandObject,
    access_manager: AccessManager,
    job_store: PendingJobStore,
) -> None:
    if not message.from_user:
        return

    is_admin = access_manager.is_admin(message.from_user.id)
    arg = (command.args or "").strip().lower()

    if is_admin and arg == "all":
        stopped = job_store.cancel_all_tasks()
        if stopped:
            await message.answer(f"🛑 Остановлено задач: <b>{stopped}</b>.", parse_mode="HTML")
        else:
            await message.answer("Активных скачиваний сейчас нет.")
        return

    stopped = job_store.cancel_user_tasks(message.from_user.id, chat_id=message.chat.id)
    if stopped:
        await message.answer("🛑 Останавливаю текущее скачивание...")
    elif is_admin and job_store.active_tasks_count > 0:
        await message.answer(
            "У тебя сейчас нет активного скачивания, но в общей очереди есть активная задача.\n"
            "Чтобы остановить её принудительно: <code>/stop all</code>.",
            parse_mode="HTML",
        )
    else:
        await message.answer("Активных скачиваний сейчас нет.")


@router.message(Command("status"))
async def cmd_status(
    message: Message,
    cookie_manager: ChromeSessionManager,
    access_manager: AccessManager,
    media_cache: MediaCache,
    settings: Settings,
    job_store: PendingJobStore | None = None,
) -> None:
    if not message.from_user or not access_manager.is_admin(message.from_user.id):
        await message.answer("⛔ Команда доступна только администратору.")
        return

    await message.answer(
        _format_status(cookie_manager.status, settings, media_cache, job_store),
        parse_mode="HTML",
    )


def _format_non_admin_stats(summary: NonAdminStatsSummary) -> str:
    if summary.total_downloads == 0:
        return "📊 Пока не было ни одной загрузки от других пользователей (кроме админов)."

    platform_labels = {
        "youtube": "YouTube",
        "instagram": "Instagram",
        "tiktok": "TikTok",
    }
    plat_parts = [
        f"{platform_labels.get(k, html.escape(k))}: {v}"
        for k, v in sorted(summary.by_platform.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    lines = [
        "📊 <b>Статистика загрузок (кроме админов)</b>",
        "",
        f"📥 <b>Всего загрузок:</b> {summary.total_downloads} "
        f"(новых: {summary.fresh_downloads}, из кэша: {summary.cache_downloads})",
        f"👥 <b>Пользователей:</b> {summary.unique_users}",
    ]
    if plat_parts:
        lines.append(f"🎬 <b>По платформам:</b> {' • '.join(plat_parts)}")

    lines.append("")
    lines.append("<b>По пользователям:</b>")
    limit = 25
    for entry in summary.users[:limit]:
        who = (
            f"@{html.escape(entry.username)} (<code>{entry.user_id}</code>)"
            if entry.username
            else f"<code>{entry.user_id}</code>"
        )
        lines.append(
            f"• {who}: <b>{entry.total_count}</b> "
            f"(новых: {entry.fresh_count}, кэш: {entry.cache_count})"
        )
    if len(summary.users) > limit:
        lines.append(f"… и ещё {len(summary.users) - limit} пользователей")
    return "\n".join(lines)


@router.message(Command("stats"))
async def cmd_stats(
    message: Message,
    access_manager: AccessManager,
    stats_tracker: StatsTracker,
) -> None:
    if not message.from_user or not access_manager.is_admin(message.from_user.id):
        await message.answer("⛔ Команда доступна только администратору.")
        return

    if message.chat.type != ChatType.PRIVATE:
        await message.answer("📊 Статистика доступна только в личных сообщениях с ботом.")
        return

    summary = stats_tracker.summarize_non_admins(
        access_manager.admin_ids,
        known_usernames=access_manager.allowed_ids,
    )
    await message.answer(_format_non_admin_stats(summary), parse_mode="HTML")


@router.message(Command("clear_cache"))
async def cmd_clear_cache(
    message: Message,
    access_manager: AccessManager,
    media_cache: MediaCache,
) -> None:
    if not message.from_user or not access_manager.is_admin(message.from_user.id):
        await message.answer("⛔ Команда доступна только администратору.")
        return

    removed = media_cache.clear()
    await message.answer(f"🗑 Кэш очищен (удалено записей: <b>{removed}</b>).", parse_mode="HTML")


@router.message(Command("refresh_cookies"))
async def cmd_refresh_cookies(
    message: Message,
    cookie_manager: ChromeSessionManager,
    access_manager: AccessManager,
    media_cache: MediaCache,
    settings: Settings,
    job_store: PendingJobStore | None = None,
) -> None:
    if not message.from_user or not access_manager.is_admin(message.from_user.id):
        await message.answer("⛔ Команда доступна только администратору.")
        return

    wait_msg = await message.answer("🔄 Обновляю страницу YouTube в headless Chrome и выгружаю cookies...")
    try:
        status = await cookie_manager.refresh_cookies(navigate=True)
        await wait_msg.edit_text(_format_status(status, settings, media_cache, job_store), parse_mode="HTML")
    except Exception as exc:
        await wait_msg.edit_text(
            f"❌ Не удалось обновить cookies:\n<code>{html.escape(str(exc))}</code>",
            parse_mode="HTML",
        )


@router.message(Command("add", "allow"))
async def cmd_add_user(
    message: Message,
    command: CommandObject,
    access_manager: AccessManager,
) -> None:
    if not message.from_user or not access_manager.is_admin(message.from_user.id):
        await message.answer("⛔ Команда доступна только администратору.")
        return

    raw_args = (command.args or "").strip()
    if not raw_args:
        await message.answer(
            "Использование: <code>/add @username</code> или <code>/add 123456789</code>\n"
            "Можно указать несколько через пробел или запятую.",
            parse_mode="HTML",
        )
        return

    tokens = [t.strip() for t in raw_args.replace(",", " ").split() if t.strip()]
    results: list[str] = []
    for token in tokens:
        try:
            results.append(access_manager.add_target(token))
        except ValueError as exc:
            results.append(f"⚠️ {html.escape(str(exc))}")

    await message.answer("\n".join(results), parse_mode="HTML")


@router.message(Command("remove", "revoke", "del"))
async def cmd_remove_user(
    message: Message,
    command: CommandObject,
    access_manager: AccessManager,
) -> None:
    if not message.from_user or not access_manager.is_admin(message.from_user.id):
        await message.answer("⛔ Команда доступна только администратору.")
        return

    raw_args = (command.args or "").strip()
    if not raw_args:
        await message.answer(
            "Использование: <code>/remove @username</code> или <code>/remove 123456789</code>",
            parse_mode="HTML",
        )
        return

    tokens = [t.strip() for t in raw_args.replace(",", " ").split() if t.strip()]
    results: list[str] = []
    for token in tokens:
        try:
            results.append(access_manager.remove_target(token))
        except ValueError as exc:
            results.append(f"⚠️ {html.escape(str(exc))}")

    await message.answer("\n".join(results), parse_mode="HTML")


@router.message(Command("users"))
async def cmd_list_users(
    message: Message,
    access_manager: AccessManager,
) -> None:
    if not message.from_user or not access_manager.is_admin(message.from_user.id):
        await message.answer("⛔ Команда доступна только администратору.")
        return

    lines = ["<b>Пользователи с доступом:</b>", ""]
    for uid, uname in sorted(access_manager.allowed_ids.items()):
        role = "👑 Админ" if access_manager.is_admin(uid) else "✅ Пользователь"
        uname_txt = f" (@{html.escape(uname)})" if uname else ""
        lines.append(f"• {role}: <code>{uid}</code>{uname_txt}")

    if access_manager.pending_usernames:
        lines.append("")
        lines.append("<b>Ожидают первого сообщения (привяжутся по @username):</b>")
        for uname in sorted(access_manager.pending_usernames):
            lines.append(f"• ⏳ <code>@{html.escape(uname)}</code>")

    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("add_chat"))
async def cmd_add_chat(
    message: Message,
    command: CommandObject,
    access_manager: AccessManager,
) -> None:
    if not message.from_user or not access_manager.is_admin(message.from_user.id):
        await message.answer("⛔ Команда доступна только администратору.")
        return

    arg = (command.args or "").strip()
    if arg:
        try:
            chat_id = int(arg)
        except ValueError:
            await message.answer("Укажи числовой ID чата, например <code>/add_chat -1001234567890</code>.", parse_mode="HTML")
            return
        title = None
    elif message.chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}:
        chat_id = message.chat.id
        title = message.chat.title
    else:
        await message.answer(
            "Отправь <code>/add_chat</code> прямо в нужном групповом чате или укажи ID: <code>/add_chat -1001234567890</code>.",
            parse_mode="HTML",
        )
        return

    newly_added = access_manager.add_chat(chat_id, title)
    label = f"{html.escape(title)} (<code>{chat_id}</code>)" if title else f"<code>{chat_id}</code>"
    if newly_added:
        await message.answer(f"✅ Чат {label} добавлен. Бот будет автоматически скачивать YouTube Shorts в этом чате.", parse_mode="HTML")
    else:
        await message.answer(f"Чат {label} уже разрешён.", parse_mode="HTML")


@router.message(Command("remove_chat"))
async def cmd_remove_chat(
    message: Message,
    command: CommandObject,
    access_manager: AccessManager,
) -> None:
    if not message.from_user or not access_manager.is_admin(message.from_user.id):
        await message.answer("⛔ Команда доступна только администратору.")
        return

    arg = (command.args or "").strip()
    if arg:
        try:
            chat_id = int(arg)
        except ValueError:
            await message.answer("Укажи числовой ID чата.", parse_mode="HTML")
            return
    elif message.chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}:
        chat_id = message.chat.id
    else:
        await message.answer("Отправь <code>/remove_chat</code> в чате или укажи ID.", parse_mode="HTML")
        return

    if access_manager.remove_chat(chat_id):
        await message.answer(f"🗑 Чат <code>{chat_id}</code> удалён из разрешённых.", parse_mode="HTML")
    else:
        await message.answer(f"Чат <code>{chat_id}</code> не найден в списке.", parse_mode="HTML")


@router.message(Command("chats"))
async def cmd_list_chats(
    message: Message,
    access_manager: AccessManager,
) -> None:
    if not message.from_user or not access_manager.is_admin(message.from_user.id):
        await message.answer("⛔ Команда доступна только администратору.")
        return

    if not access_manager.allowed_chats:
        await message.answer(
            "Список разрешённых групповых чатов пуст.\n"
            "Добавь бота в чат или выполни в чате <code>/add_chat</code>.",
            parse_mode="HTML",
        )
        return

    lines = ["<b>Разрешённые групповые чаты (автоскачивание Shorts):</b>", ""]
    for cid, title in sorted(access_manager.allowed_chats.items()):
        title_txt = f"{html.escape(title)} — " if title else ""
        lines.append(f"• {title_txt}<code>{cid}</code>")
    await message.answer("\n".join(lines), parse_mode="HTML")

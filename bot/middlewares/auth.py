from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.enums import ChatType
from aiogram.types import CallbackQuery, Chat, Message, TelegramObject

from bot.services.access import AccessManager

logger = logging.getLogger(__name__)


class WhitelistMiddleware(BaseMiddleware):
    """Allow updates from authorized users in PM and authorized group chats."""

    def __init__(self, access_manager: AccessManager) -> None:
        self.access_manager = access_manager

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = getattr(event, "from_user", None)
        if user is None:
            return None

        chat = self._extract_chat(event)
        if chat is not None and chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}:
            if self.access_manager.is_chat_allowed(chat.id) or self.access_manager.is_admin(user.id):
                return await handler(event, data)
            logger.debug(
                "Ignored update in unauthorized group chat_id=%s from user_id=%s",
                chat.id,
                user.id,
            )
            return None

        if not self.access_manager.check_and_bind(user.id, getattr(user, "username", None)):
            logger.warning(
                "Unauthorized access attempt from user_id=%s (@%s)",
                user.id,
                getattr(user, "username", None),
            )
            await self._notify_denied(event, user.id)
            return None

        return await handler(event, data)

    @staticmethod
    def _extract_chat(event: TelegramObject) -> Chat | None:
        if isinstance(event, Message):
            return event.chat
        if isinstance(event, CallbackQuery) and isinstance(event.message, Message):
            return event.message.chat
        return getattr(event, "chat", None)

    @staticmethod
    async def _notify_denied(event: TelegramObject, user_id: int) -> None:
        text = f"⛔ Доступ запрещён.\nТвой Telegram ID: <code>{user_id}</code>"
        if isinstance(event, Message):
            await event.answer(text, parse_mode="HTML")
        elif isinstance(event, CallbackQuery):
            await event.answer("⛔ Доступ запрещён", show_alert=True)

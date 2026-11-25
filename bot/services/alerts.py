from __future__ import annotations

import html
import logging
import shutil
import time
from pathlib import Path

from aiogram import Bot

from bot.services.chrome_cookies import SessionStatus
from bot.utils.parsers import format_filesize

logger = logging.getLogger(__name__)


class AdminAlertMonitor:
    """Sends rate-limited Telegram alerts to admins when Chrome auth drops or disk space runs low."""

    def __init__(
        self,
        bot: Bot,
        *,
        admin_ids: set[int],
        downloads_dir: Path,
        low_disk_warn_gb: float = 3.0,
        cooldown_minutes: int = 360,
    ) -> None:
        self.bot = bot
        self.admin_ids = set(admin_ids)
        self.downloads_dir = downloads_dir
        self.low_disk_warn_bytes = int(max(0.1, low_disk_warn_gb) * 1024 * 1024 * 1024)
        self.cooldown_seconds = max(60, cooldown_minutes * 60)

        self._last_session_alert: float | None = None
        self._last_disk_alert: float | None = None

    async def evaluate(self, status: SessionStatus) -> None:
        """Inspect Chrome session and free disk space; notify admins when attention is needed."""
        if not self.admin_ids:
            return

        now = time.monotonic()

        # 1. Google / YouTube session check
        if not status.authenticated:
            if (
                self._last_session_alert is None
                or (now - self._last_session_alert) >= self.cooldown_seconds
            ):
                self._last_session_alert = now
                err_part = (
                    f"\n<i>Ошибка:</i> <code>{html.escape(status.last_error[:300])}</code>"
                    if status.last_error
                    else ""
                )
                await self._notify_admins(
                    "⚠️ <b>Сессия Google/YouTube в Chrome не авторизована!</b>\n"
                    "Потребуется повторный вход через <code>chrome-auth</code> (noVNC) "
                    "и проверка командой /status."
                    f"{err_part}"
                )
        elif self._last_session_alert is not None:
            self._last_session_alert = None
            await self._notify_admins("✅ <b>Сессия Google/YouTube в Chrome восстановлена.</b>")

        # 2. Disk space check
        try:
            usage = shutil.disk_usage(self.downloads_dir)
        except OSError as exc:
            logger.warning("Disk usage check failed: %s", exc)
            return

        if usage.free < self.low_disk_warn_bytes:
            if (
                self._last_disk_alert is None
                or (now - self._last_disk_alert) >= self.cooldown_seconds
            ):
                self._last_disk_alert = now
                await self._notify_admins(
                    "💾 <b>Мало свободного места на диске сервера!</b>\n"
                    f"Осталось: <b>{format_filesize(usage.free)}</b> из {format_filesize(usage.total)}"
                )
        elif (
            self._last_disk_alert is not None
            and usage.free >= self.low_disk_warn_bytes + 512 * 1024 * 1024
        ):
            self._last_disk_alert = None

    async def _notify_admins(self, text: str) -> None:
        for admin_id in sorted(self.admin_ids):
            try:
                await self.bot.send_message(admin_id, text, parse_mode="HTML")
            except Exception as exc:
                logger.warning("Failed to send admin alert to %s: %s", admin_id, exc)

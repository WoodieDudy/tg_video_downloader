from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

_USERNAME_RE = re.compile(r"^@?[A-Za-z0-9_]{3,64}$")


def normalize_username(raw: str) -> str:
    cleaned = raw.strip().lstrip("@").lower()
    if not cleaned or not _USERNAME_RE.match(cleaned):
        raise ValueError(f"Некорректный username: {raw!r}")
    return cleaned


class AccessManager:
    """Dynamic whitelist manager supporting admin IDs, allowed IDs, @username binding, and group chats."""

    def __init__(
        self,
        users_file: Path,
        *,
        admin_ids: set[int],
        admin_usernames: set[str] | None = None,
        seed_allowed_ids: set[int] | None = None,
    ) -> None:
        self.users_file = users_file
        self.admin_ids: set[int] = set(admin_ids)
        self.admin_usernames: set[str] = {
            u.strip().lstrip("@").lower() for u in (admin_usernames or set()) if u.strip()
        }
        self.allowed_ids: dict[int, str | None] = {}
        self.pending_usernames: set[str] = set()
        self.allowed_chats: dict[int, str | None] = {}

        for uid in seed_allowed_ids or set():
            self.allowed_ids.setdefault(uid, None)
        for uid in self.admin_ids:
            self.allowed_ids.setdefault(uid, None)

        self._load()

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.admin_ids

    def is_chat_allowed(self, chat_id: int) -> bool:
        return chat_id in self.allowed_chats

    def add_chat(self, chat_id: int, title: str | None = None) -> bool:
        """Allow a group/supergroup chat. Returns True if newly added."""
        already = chat_id in self.allowed_chats
        self.allowed_chats[chat_id] = title or self.allowed_chats.get(chat_id)
        self._save()
        return not already

    def remove_chat(self, chat_id: int) -> bool:
        """Remove a group/supergroup chat from allowed list."""
        if chat_id in self.allowed_chats:
            self.allowed_chats.pop(chat_id, None)
            self._save()
            return True
        return False

    def check_and_bind(self, user_id: int, username: str | None) -> bool:
        """Return True if user is allowed; bind @username -> user_id on first match."""
        norm_username = username.lstrip("@").lower() if username else None

        if norm_username and norm_username in self.admin_usernames and user_id not in self.admin_ids:
            self.admin_ids.add(user_id)
            self.pending_usernames.discard(norm_username)
            self.allowed_ids[user_id] = norm_username
            self._save()
            logger.info("Bound admin username @%s to user_id=%d", norm_username, user_id)
            return True

        if user_id in self.admin_ids:
            if norm_username and self.allowed_ids.get(user_id) != norm_username:
                self.allowed_ids[user_id] = norm_username
                self._save()
            return True

        if user_id in self.allowed_ids:
            if norm_username and self.allowed_ids.get(user_id) != norm_username:
                self.allowed_ids[user_id] = norm_username
                self._save()
            return True

        if norm_username and norm_username in self.pending_usernames:
            self.pending_usernames.remove(norm_username)
            self.allowed_ids[user_id] = norm_username
            self._save()
            logger.info("Bound allowed username @%s to user_id=%d", norm_username, user_id)
            return True

        return False

    def add_target(self, target: str) -> str:
        """Add either a numeric Telegram user_id or @username."""
        token = target.strip()
        if token.lstrip("-").isdigit():
            uid = int(token)
            if uid <= 0:
                raise ValueError(f"Некорректный ID: {target!r}")
            if uid in self.allowed_ids:
                return f"ID <code>{uid}</code> уже в списке."
            self.allowed_ids[uid] = None
            self._save()
            return f"✅ Добавлен ID <code>{uid}</code>"

        uname = normalize_username(token)
        for uid, known_uname in self.allowed_ids.items():
            if known_uname == uname:
                return f"@{uname} уже привязан к ID <code>{uid}</code>."

        if uname in self.pending_usernames:
            return f"@{uname} уже ожидает первого сообщения."

        self.pending_usernames.add(uname)
        self._save()
        return f"✅ Добавлен <code>@{uname}</code> (привяжется к ID при первом сообщении боту)"

    def remove_target(self, target: str) -> str:
        """Remove a numeric user_id or @username from the whitelist."""
        token = target.strip()
        if token.lstrip("-").isdigit():
            uid = int(token)
            if uid in self.admin_ids:
                raise ValueError(f"Нельзя удалить администратора (ID {uid}).")
            if uid in self.allowed_ids:
                uname = self.allowed_ids.pop(uid)
                self._save()
                suffix = f" (@{uname})" if uname else ""
                return f"🗑 Удалён ID <code>{uid}</code>{suffix}"
            return f"ID <code>{uid}</code> не найден в списке."

        uname = normalize_username(token)
        removed = False
        if uname in self.pending_usernames:
            self.pending_usernames.remove(uname)
            removed = True

        matched_ids = [uid for uid, known in self.allowed_ids.items() if known == uname]
        for uid in matched_ids:
            if uid in self.admin_ids:
                raise ValueError(f"Нельзя удалить администратора (@{uname}).")
            self.allowed_ids.pop(uid, None)
            removed = True

        if removed:
            self._save()
            return f"🗑 Удалён <code>@{uname}</code>"
        return f"Пользователь <code>@{uname}</code> не найден в списке."

    def _load(self) -> None:
        if not self.users_file.exists():
            self._save()
            return
        try:
            raw = json.loads(self.users_file.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Could not read %s: %s", self.users_file, exc)
            return

        for uid_str in raw.get("admin_ids") or []:
            try:
                self.admin_ids.add(int(uid_str))
            except (TypeError, ValueError):
                continue

        for uid_str, uname in (raw.get("allowed_ids") or {}).items():
            try:
                uid = int(uid_str)
            except ValueError:
                continue
            self.allowed_ids[uid] = str(uname).lstrip("@").lower() if uname else None

        for uname in raw.get("pending_usernames") or []:
            try:
                self.pending_usernames.add(normalize_username(str(uname)))
            except ValueError:
                continue

        for cid_str, title in (raw.get("allowed_chats") or {}).items():
            try:
                cid = int(cid_str)
            except ValueError:
                continue
            self.allowed_chats[cid] = str(title) if title else None

        for uid in self.admin_ids:
            self.allowed_ids.setdefault(uid, None)

    def _save(self) -> None:
        self.users_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "admin_ids": sorted(self.admin_ids),
            "allowed_ids": {str(uid): uname for uid, uname in sorted(self.allowed_ids.items())},
            "pending_usernames": sorted(self.pending_usernames),
            "allowed_chats": {str(cid): title for cid, title in sorted(self.allowed_chats.items())},
        }
        tmp = self.users_file.with_name(f"{self.users_file.name}.tmp.{os.getpid()}")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, self.users_file)

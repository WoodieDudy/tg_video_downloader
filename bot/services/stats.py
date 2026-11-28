from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class UserDownloadStats:
    """Aggregated download counters for a single Telegram user."""

    user_id: int
    username: str | None = None
    fresh_count: int = 0
    cache_count: int = 0
    last_at: float = 0.0
    by_platform: dict[str, int] = field(default_factory=dict)

    @property
    def total_count(self) -> int:
        return self.fresh_count + self.cache_count


@dataclass(slots=True)
class NonAdminStatsSummary:
    """Summary of all downloads made by non-admin users."""

    total_downloads: int
    fresh_downloads: int
    cache_downloads: int
    unique_users: int
    by_platform: dict[str, int]
    users: list[UserDownloadStats]


class StatsTracker:
    """Persistent per-user download statistics stored atomically in JSON."""

    def __init__(self, stats_file: Path) -> None:
        self.stats_file = stats_file
        self._users: dict[int, UserDownloadStats] = {}
        self._load()

    def record_download(
        self,
        *,
        user_id: int,
        username: str | None = None,
        platform: str = "youtube",
        from_cache: bool = False,
    ) -> None:
        if user_id <= 0:
            return
        entry = self._users.get(user_id)
        if entry is None:
            entry = UserDownloadStats(user_id=user_id)
            self._users[user_id] = entry

        if username:
            entry.username = username.strip().lstrip("@").lower() or entry.username
        if from_cache:
            entry.cache_count += 1
        else:
            entry.fresh_count += 1
        entry.last_at = time.time()
        norm_platform = platform.strip().lower() or "other"
        entry.by_platform[norm_platform] = entry.by_platform.get(norm_platform, 0) + 1
        self._save()

    def summarize_non_admins(
        self,
        admin_ids: set[int],
        known_usernames: dict[int, str | None] | None = None,
    ) -> NonAdminStatsSummary:
        """Return aggregated download counters excluding all admin user IDs."""
        users: list[UserDownloadStats] = []
        total_fresh = 0
        total_cache = 0
        by_platform: dict[str, int] = {}

        for uid, entry in self._users.items():
            if uid in admin_ids:
                continue
            resolved_username = entry.username
            if not resolved_username and known_usernames:
                resolved_username = known_usernames.get(uid)
            copy = UserDownloadStats(
                user_id=entry.user_id,
                username=resolved_username,
                fresh_count=entry.fresh_count,
                cache_count=entry.cache_count,
                last_at=entry.last_at,
                by_platform=dict(entry.by_platform),
            )
            users.append(copy)
            total_fresh += copy.fresh_count
            total_cache += copy.cache_count
            for plat, cnt in copy.by_platform.items():
                by_platform[plat] = by_platform.get(plat, 0) + cnt

        users.sort(key=lambda item: (-item.total_count, -item.last_at, item.user_id))
        return NonAdminStatsSummary(
            total_downloads=total_fresh + total_cache,
            fresh_downloads=total_fresh,
            cache_downloads=total_cache,
            unique_users=len(users),
            by_platform=by_platform,
            users=users,
        )

    def _load(self) -> None:
        if not self.stats_file.exists():
            return
        try:
            raw: dict[str, Any] = json.loads(self.stats_file.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Could not read %s: %s", self.stats_file, exc)
            return

        for uid_str, item in (raw.get("by_user") or {}).items():
            if not isinstance(item, dict):
                continue
            try:
                uid = int(uid_str)
            except ValueError:
                continue
            raw_plats = item.get("by_platform") or {}
            plats: dict[str, int] = {}
            if isinstance(raw_plats, dict):
                for k, v in raw_plats.items():
                    try:
                        plats[str(k)] = int(v)
                    except (TypeError, ValueError):
                        continue
            uname = item.get("username")
            self._users[uid] = UserDownloadStats(
                user_id=uid,
                username=str(uname).lstrip("@").lower() if uname else None,
                fresh_count=int(item.get("fresh_count", 0) or 0),
                cache_count=int(item.get("cache_count", 0) or 0),
                last_at=float(item.get("last_at", 0.0) or 0.0),
                by_platform=plats,
            )

    def _save(self) -> None:
        self.stats_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "by_user": {
                str(uid): {
                    "username": entry.username,
                    "fresh_count": entry.fresh_count,
                    "cache_count": entry.cache_count,
                    "last_at": entry.last_at,
                    "by_platform": entry.by_platform,
                }
                for uid, entry in sorted(self._users.items())
            }
        }
        tmp = self.stats_file.with_name(f"{self.stats_file.name}.tmp.{os.getpid()}")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, self.stats_file)

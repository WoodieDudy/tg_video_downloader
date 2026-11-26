from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from pydantic import BeforeValidator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _parse_user_ids(value: Any) -> set[int]:
    if value is None or value == "":
        return set()
    if isinstance(value, (set, list, tuple)):
        return {int(v) for v in value}
    if isinstance(value, int):
        return {value}
    if isinstance(value, str):
        parts = [part.strip() for part in value.replace(";", ",").split(",")]
        return {int(part) for part in parts if part}
    raise ValueError(f"Unsupported ALLOWED_USER_IDS value: {value!r}")


AllowedUserIds = Annotated[set[int], BeforeValidator(_parse_user_ids)]


def _parse_usernames(value: Any) -> set[str]:
    if value is None or value == "":
        return set()
    if isinstance(value, (set, list, tuple)):
        items = [str(v) for v in value]
    elif isinstance(value, str):
        items = value.replace(";", ",").split(",")
    else:
        raise ValueError(f"Unsupported ADMIN_USERNAMES value: {value!r}")
    return {item.strip().lstrip("@").lower() for item in items if item.strip().lstrip("@")}


AllowedUsernames = Annotated[set[str], BeforeValidator(_parse_usernames)]


class Settings(BaseSettings):
    """Application settings loaded from environment variables / .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    bot_token: str
    bot_username: str = ""
    allowed_user_ids: AllowedUserIds = set()
    admin_user_ids: AllowedUserIds = set()
    admin_usernames: AllowedUsernames = set()

    local_bot_api_url: str | None = "http://telegram-bot-api:8081"
    local_bot_api_is_local: bool = True

    chrome_profile_dir: Path = Path("/data/chrome-profile")
    cookies_file: Path = Path("/data/cookies/youtube.txt")
    downloads_dir: Path = Path("/data/downloads")
    users_file: Path = Path("/data/state/users.json")
    cache_file: Path = Path("/data/state/media_cache.json")

    cookie_refresh_interval_minutes: int = 240
    low_disk_warn_gb: float = 3.0
    alert_cooldown_minutes: int = 360
    max_concurrent_downloads: int = 1
    queue_wait_timeout_seconds: int = 120
    queue_max_size: int = 15
    queue_task_gap_seconds: float = 2.0
    fetch_info_timeout_seconds: int = 45
    download_timeout_seconds: int = 300
    ytdlp_socket_timeout_seconds: int = 20
    max_file_size_mb: int = 2000
    group_max_duration_seconds: int = 120
    group_max_file_size_mb: int = 150
    user_cooldown_seconds: int = 20
    user_hourly_limit: int = 20
    ytdlp_js_runtime: str = "deno"
    pot_provider_url: str | None = None
    ytdlp_sleep_requests: float = 1.0
    ytdlp_concurrent_fragments: int = 2
    video_crf: int = 24
    video_preset: str = "medium"
    bot_api_timeout_seconds: int = 1800

    @property
    def effective_admin_ids(self) -> set[int]:
        # If ADMIN_USER_IDS isn't explicitly set, treat ALLOWED_USER_IDS from .env as admins.
        return self.admin_user_ids or self.allowed_user_ids

    @property
    def max_file_size_bytes(self) -> int:
        return self.max_file_size_mb * 1024 * 1024

    @property
    def group_max_file_size_bytes(self) -> int:
        return self.group_max_file_size_mb * 1024 * 1024


def load_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Domains relevant to YouTube + Google authentication & bot checks
RELEVANT_DOMAIN_SUFFIXES = (
    "youtube.com",
    "youtu.be",
    "google.com",
    "googlevideo.com",
    "googleusercontent.com",
    "gstatic.com",
)

# Auth cookies present when signed into Google / YouTube
AUTH_COOKIE_NAMES = {
    "SAPISID",
    "SID",
    "__Secure-1PSID",
    "__Secure-3PSID",
    "__Secure-1PAPISID",
    "__Secure-3PAPISID",
    "LOGIN_INFO",
}

CHROMIUM_LOCK_FILES = ("SingletonLock", "SingletonSocket", "SingletonCookie")


@dataclass(slots=True)
class SessionStatus:
    """Snapshot of the headless Chrome session and exported cookies."""

    browser_running: bool
    authenticated: bool
    total_cookies: int
    youtube_cookies: int
    last_refresh_at: datetime | None
    last_error: str | None = None
    cookies_file_exists: bool = False


def is_relevant_domain(domain: str) -> bool:
    normalized = domain.lstrip(".").lower()
    return any(
        normalized == suffix or normalized.endswith(f".{suffix}")
        for suffix in RELEVANT_DOMAIN_SUFFIXES
    )


def format_netscape_cookies(cookies: Iterable[dict[str, Any]]) -> str:
    """Convert CDP Network.getAllCookies items into Netscape cookies.txt format."""
    lines = [
        "# Netscape HTTP Cookie File",
        "# Generated automatically by yt-tg-bot from headless Chrome session.",
        "",
    ]

    for cookie in cookies:
        domain = str(cookie.get("domain", "")).strip()
        if not domain or not is_relevant_domain(domain):
            continue

        name = _sanitize_field(str(cookie.get("name", "")))
        value = _sanitize_field(str(cookie.get("value", "")))
        path = _sanitize_field(str(cookie.get("path", "/") or "/"))
        if not name:
            continue

        include_subdomains = "TRUE" if domain.startswith(".") else "FALSE"
        secure = "TRUE" if bool(cookie.get("secure", False)) else "FALSE"

        raw_expires = cookie.get("expires", -1)
        try:
            expires_num = float(raw_expires)
        except (TypeError, ValueError):
            expires_num = -1.0

        expiration = str(int(expires_num)) if expires_num > 0 else "0"
        lines.append(
            "\t".join([domain, include_subdomains, path, secure, expiration, name, value])
        )

    return "\n".join(lines) + "\n"


def has_authenticated_cookies(cookies: Iterable[dict[str, Any]]) -> bool:
    """Return True if Google/YouTube auth session cookies are present."""
    present_names: set[str] = set()
    for cookie in cookies:
        domain = str(cookie.get("domain", "")).lower()
        if "youtube.com" in domain or "google.com" in domain:
            name = str(cookie.get("name", ""))
            value = str(cookie.get("value", ""))
            if name in AUTH_COOKIE_NAMES and value:
                present_names.add(name)

    if "LOGIN_INFO" in present_names and len(present_names) >= 2:
        return True
    return bool({"SID", "SAPISID"} & present_names) and bool(
        {"__Secure-1PSID", "__Secure-3PSID", "__Secure-1PAPISID", "__Secure-3PAPISID"} & present_names
    )


def _sanitize_field(value: str) -> str:
    return value.replace("\r", "").replace("\n", "").replace("\t", "")


def write_cookies_atomically(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    tmp_path.write_text(content, encoding="utf-8")
    os.replace(tmp_path, path)


class ChromeSessionManager:
    """Keeps a persistent headless Chromium session open and exports fresh YouTube cookies."""

    def __init__(
        self,
        profile_dir: Path,
        cookies_file: Path,
        refresh_interval_minutes: int = 30,
        on_status_update: Callable[[SessionStatus], Awaitable[None]] | None = None,
    ) -> None:
        self.profile_dir = profile_dir
        self.cookies_file = cookies_file
        self.refresh_interval_seconds = max(60, refresh_interval_minutes * 60)
        self.on_status_update = on_status_update

        self._lock = asyncio.Lock()
        self._playwright: Any = None
        self._context: Any = None
        self._page: Any = None
        self._refresh_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

        self._status = SessionStatus(
            browser_running=False,
            authenticated=False,
            total_cookies=0,
            youtube_cookies=0,
            last_refresh_at=None,
            last_error=None,
            cookies_file_exists=cookies_file.exists(),
        )

    @property
    def status(self) -> SessionStatus:
        self._status.cookies_file_exists = self.cookies_file.exists()
        return self._status

    async def start(self) -> None:
        """Start headless Chromium using the persistent profile and perform initial cookie export."""
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.cookies_file.parent.mkdir(parents=True, exist_ok=True)
        self._stop_event.clear()

        async with self._lock:
            await self._launch_browser_locked()

        try:
            await self.refresh_cookies(navigate=True)
        except Exception as exc:
            logger.exception("Initial cookie refresh failed: %s", exc)
            self._status.last_error = str(exc)

        if self._refresh_task is None or self._refresh_task.done():
            self._refresh_task = asyncio.create_task(
                self._periodic_refresh_loop(), name="chrome-cookie-refresh"
            )

    async def stop(self) -> None:
        """Stop periodic refresh and cleanly close headless Chromium."""
        self._stop_event.set()
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass
            self._refresh_task = None

        async with self._lock:
            await self._close_browser_locked()

    async def refresh_cookies(self, *, navigate: bool = True) -> SessionStatus:
        """Reload YouTube in headless Chrome and export fresh cookies atomically."""
        async with self._lock:
            await self._ensure_browser_locked()
            try:
                if navigate:
                    await self._navigate_youtube_locked()

                cdp = await self._context.new_cdp_session(self._page)
                try:
                    result = await cdp.send("Network.getAllCookies")
                finally:
                    await cdp.detach()

                cookies: list[dict[str, Any]] = result.get("cookies", [])
                netscape_text = format_netscape_cookies(cookies)
                write_cookies_atomically(self.cookies_file, netscape_text)

                yt_cookies = [
                    c for c in cookies if "youtube.com" in str(c.get("domain", "")).lower()
                ]
                relevant_cookies = [
                    c for c in cookies if is_relevant_domain(str(c.get("domain", "")))
                ]
                authenticated = has_authenticated_cookies(cookies)

                self._status = SessionStatus(
                    browser_running=True,
                    authenticated=authenticated,
                    total_cookies=len(relevant_cookies),
                    youtube_cookies=len(yt_cookies),
                    last_refresh_at=datetime.now(timezone.utc),
                    last_error=None,
                    cookies_file_exists=self.cookies_file.exists(),
                )
                logger.info(
                    "Refreshed Chrome cookies: relevant=%d youtube=%d authenticated=%s",
                    len(relevant_cookies),
                    len(yt_cookies),
                    authenticated,
                )
                return self.status
            except Exception as exc:
                self._status.last_error = str(exc)
                logger.exception("Failed to refresh cookies from Chrome: %s", exc)
                # Try recovering browser on next call if context died
                if self._page is not None and self._page.is_closed():
                    await self._close_browser_locked()
                raise

    async def get_cookies_snapshot(self, destination: Path) -> Path | None:
        """Copy the latest cookies.txt to a job-local path to avoid concurrent yt-dlp writes."""
        if not self.cookies_file.exists():
            try:
                await self.refresh_cookies(navigate=True)
            except Exception:
                return None

        if not self.cookies_file.exists():
            return None

        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.cookies_file, destination)
        return destination

    async def _periodic_refresh_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self.refresh_interval_seconds
                )
                break
            except asyncio.TimeoutError:
                pass

            try:
                await self.refresh_cookies(navigate=True)
            except Exception as exc:
                logger.warning("Periodic cookie refresh failed: %s", exc)

            if self.on_status_update is not None:
                try:
                    await self.on_status_update(self.status)
                except Exception as exc:
                    logger.warning("Status update hook failed: %s", exc)

    async def _ensure_browser_locked(self) -> None:
        if self._context is not None and self._page is not None and not self._page.is_closed():
            return
        await self._close_browser_locked()
        await self._launch_browser_locked()

    async def _launch_browser_locked(self) -> None:
        from playwright.async_api import async_playwright

        self._cleanup_stale_locks()

        self._playwright = await async_playwright().start()
        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_dir),
            channel="chromium",
            headless=True,
            ignore_default_args=["--enable-automation"],
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--no-first-run",
                "--no-default-browser-check",
            ],
            viewport={"width": 1280, "height": 800},
            locale="en-US",
        )
        # Hide navigator.webdriver for extra resilience against bot checks
        await self._context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )

        pages = self._context.pages
        self._page = pages[0] if pages else await self._context.new_page()
        self._status.browser_running = True
        logger.info("Headless Chromium started with profile at %s", self.profile_dir)

    async def _navigate_youtube_locked(self) -> None:
        assert self._page is not None
        await self._page.goto(
            "https://www.youtube.com/",
            wait_until="domcontentloaded",
            timeout=30_000,
        )
        # Wait briefly for YouTube background requests that rotate __Secure-*PSIDTS cookies
        try:
            await self._page.wait_for_load_state("networkidle", timeout=6_000)
        except Exception:
            await asyncio.sleep(2.0)

        # Dismiss YouTube consent banner if present (EU/server IPs)
        await self._dismiss_consent_if_present_locked()

    async def _dismiss_consent_if_present_locked(self) -> None:
        assert self._page is not None
        if "consent.youtube.com" not in self._page.url:
            return
        for selector in (
            "button[aria-label*='Accept']",
            "button[aria-label*='Принять']",
            "form[action*='consent'] button",
        ):
            try:
                btn = self._page.locator(selector).first
                if await btn.is_visible(timeout=1_000):
                    await btn.click()
                    await asyncio.sleep(2.0)
                    break
            except Exception:
                continue

    async def _close_browser_locked(self) -> None:
        if self._context is not None:
            try:
                await self._context.close()
            except Exception:
                pass
            self._context = None
            self._page = None
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None
        self._status.browser_running = False

    def _cleanup_stale_locks(self) -> None:
        """Remove leftover Chromium Singleton* lock files if no chromium process is alive."""
        if _has_running_chromium():
            return
        for name in CHROMIUM_LOCK_FILES:
            lock_path = self.profile_dir / name
            if lock_path.exists() or lock_path.is_symlink():
                try:
                    lock_path.unlink()
                    logger.info("Removed stale Chromium lock file: %s", lock_path)
                except OSError as exc:
                    logger.warning("Could not remove %s: %s", lock_path, exc)


def _has_running_chromium() -> bool:
    """Best-effort check on Linux (/proc) whether a Chromium process is currently active."""
    proc_dir = Path("/proc")
    if not proc_dir.exists():
        return False
    my_pid = str(os.getpid())
    for entry in proc_dir.iterdir():
        if not entry.name.isdigit() or entry.name == my_pid:
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().decode("utf-8", errors="ignore")
        except OSError:
            continue
        if "chrom" in cmdline.lower() and "--user-data-dir" in cmdline:
            return True
    return False

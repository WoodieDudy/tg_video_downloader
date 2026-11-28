from __future__ import annotations

import asyncio
import secrets
import time
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from bot.services.downloader import VideoInfo
from bot.utils.parsers import TimeRange


class QueueError(Exception):
    """Base error raised by the global request queue."""


class QueueFullError(QueueError):
    """Raised when too many requests are already waiting in the global queue."""


class QueueTimeoutError(QueueError):
    """Raised when waiting for a free slot in the global queue exceeds timeout."""


class TaskTimeoutError(QueueError):
    """Raised when an active queued task exceeds its execution timeout."""


@dataclass(slots=True)
class PendingJob:
    """State for an interactive download request awaiting user button choice."""

    job_id: str
    user_id: int
    chat_id: int
    info: VideoInfo
    platform: str = "youtube"
    ig_kind: str = "reel"
    time_range: TimeRange | None = None
    as_document: bool = False
    created_at: float = field(default_factory=time.monotonic)


class RateLimiter:
    """Per-user cooldown and hourly download quota for non-admin users."""

    def __init__(self, *, cooldown_seconds: int = 20, hourly_limit: int = 20) -> None:
        self.cooldown_seconds = max(0, cooldown_seconds)
        self.hourly_limit = max(0, hourly_limit)
        self._history: dict[int, deque[float]] = defaultdict(deque)

    def check_and_record(self, user_id: int, *, is_admin: bool = False) -> str | None:
        """Return None and record usage if allowed; otherwise return a user-facing limit message."""
        if is_admin:
            return None

        now = time.monotonic()
        bucket = self._history[user_id]
        cutoff = now - 3600.0
        while bucket and bucket[0] < cutoff:
            bucket.popleft()

        if self.cooldown_seconds > 0 and bucket:
            elapsed = now - bucket[-1]
            if elapsed < self.cooldown_seconds:
                wait_sec = max(1, int(round(self.cooldown_seconds - elapsed)))
                return f"⏳ Слишком часто! Подожди {wait_sec} сек. перед следующим скачиванием."

        if self.hourly_limit > 0 and len(bucket) >= self.hourly_limit:
            return f"⏳ Достигнут часовой лимит скачиваний ({self.hourly_limit} в час). Попробуй позже."

        bucket.append(now)
        return None


class GlobalRequestQueue:
    """Strict FIFO global queue for all users with wait/execution timeouts and pacing gap."""

    def __init__(
        self,
        *,
        max_concurrent: int = 1,
        max_queue_size: int = 15,
        wait_timeout_seconds: float = 120.0,
        min_gap_seconds: float = 2.0,
    ) -> None:
        self.max_concurrent = max(1, max_concurrent)
        self.max_queue_size = max(1, max_queue_size)
        self.wait_timeout_seconds = max(0.01, float(wait_timeout_seconds))
        self.min_gap_seconds = max(0.0, float(min_gap_seconds))
        self._active = 0
        self._waiters: deque[asyncio.Future[None]] = deque()
        self._last_finished_at = 0.0

    @property
    def active_count(self) -> int:
        return self._active

    @property
    def waiting_count(self) -> int:
        return len(self._waiters)

    @property
    def is_busy(self) -> bool:
        return self._active >= self.max_concurrent or bool(self._waiters)

    async def acquire(
        self,
        *,
        on_queued: Callable[[int], Awaitable[None]] | None = None,
        wait_timeout: float | None = None,
    ) -> None:
        timeout = self.wait_timeout_seconds if wait_timeout is None else max(0.01, float(wait_timeout))

        if self._active < self.max_concurrent and not self._waiters:
            self._active += 1
        else:
            if len(self._waiters) >= self.max_queue_size:
                raise QueueFullError(
                    "⏳ Очередь переполнена, сейчас обрабатывается слишком много запросов. Попробуй через пару минут."
                )

            loop = asyncio.get_running_loop()
            fut: asyncio.Future[None] = loop.create_future()
            self._waiters.append(fut)
            position = len(self._waiters)

            if on_queued is not None:
                try:
                    await on_queued(position)
                except Exception:
                    pass

            try:
                await asyncio.wait_for(fut, timeout=timeout)
            except TimeoutError as exc:
                try:
                    self._waiters.remove(fut)
                except ValueError:
                    pass
                if fut.done() and not fut.cancelled():
                    self.release()
                raise QueueTimeoutError(
                    f"⏳ Не удалось дождаться своей очереди за {int(round(timeout))} сек. "
                    "Попробуй отправить запрос чуть позже."
                ) from exc
            except asyncio.CancelledError:
                try:
                    self._waiters.remove(fut)
                except ValueError:
                    pass
                if fut.done() and not fut.cancelled():
                    self.release()
                raise

        if self.min_gap_seconds > 0 and self._last_finished_at > 0:
            remaining = self.min_gap_seconds - (time.monotonic() - self._last_finished_at)
            if remaining > 0:
                try:
                    await asyncio.sleep(remaining)
                except BaseException:
                    self.release()
                    raise

    def release(self) -> None:
        self._last_finished_at = time.monotonic()
        self._active = max(0, self._active - 1)
        while self._active < self.max_concurrent and self._waiters:
            next_fut = self._waiters.popleft()
            if not next_fut.done():
                self._active += 1
                next_fut.set_result(None)
                break

    @asynccontextmanager
    async def slot(
        self,
        *,
        on_queued: Callable[[int], Awaitable[None]] | None = None,
        wait_timeout: float | None = None,
        task_timeout: float | None = None,
        task_timeout_message: str | None = None,
    ) -> AsyncIterator[None]:
        await self.acquire(on_queued=on_queued, wait_timeout=wait_timeout)
        try:
            if task_timeout is not None and task_timeout > 0:
                try:
                    async with asyncio.timeout(task_timeout):
                        yield
                except TimeoutError as exc:
                    raise TaskTimeoutError(
                        task_timeout_message
                        or f"⏳ Задача выполнялась дольше {int(round(task_timeout))} сек. и была остановлена."
                    ) from exc
            else:
                yield
        finally:
            self.release()


@dataclass(slots=True)
class ActiveDownloadTask:
    """Running or queued fetch/download asyncio task that can be stopped by user command."""

    task_id: str
    user_id: int
    chat_id: int
    task: asyncio.Task[Any]
    description: str = ""
    cancelled_by_user: bool = False


class PendingJobStore:
    """In-memory store of pending download requests with automatic TTL expiration."""

    def __init__(
        self,
        max_concurrent_downloads: int = 1,
        ttl_seconds: int = 7200,
        *,
        queue_wait_timeout_seconds: float = 120.0,
        queue_max_size: int = 15,
        queue_task_gap_seconds: float = 2.0,
    ) -> None:
        self._jobs: dict[str, PendingJob] = {}
        self._active_tasks: dict[str, ActiveDownloadTask] = {}
        self._ttl_seconds = ttl_seconds
        self.queue = GlobalRequestQueue(
            max_concurrent=max_concurrent_downloads,
            max_queue_size=queue_max_size,
            wait_timeout_seconds=queue_wait_timeout_seconds,
            min_gap_seconds=queue_task_gap_seconds,
        )

    @property
    def active_tasks_count(self) -> int:
        return sum(1 for item in self._active_tasks.values() if not item.task.done())

    @asynccontextmanager
    async def track_active_task(
        self,
        *,
        user_id: int,
        chat_id: int,
        description: str = "",
    ) -> AsyncIterator[ActiveDownloadTask | None]:
        current = asyncio.current_task()
        if current is None:
            yield None
            return

        task_id = secrets.token_hex(6)
        entry = ActiveDownloadTask(
            task_id=task_id,
            user_id=user_id,
            chat_id=chat_id,
            task=current,
            description=description,
        )
        self._active_tasks[task_id] = entry
        try:
            yield entry
        finally:
            self._active_tasks.pop(task_id, None)

    def cancel_user_tasks(self, user_id: int, *, chat_id: int | None = None) -> int:
        """Cancel active or queued tasks belonging to user_id (optionally scoped to chat_id)."""
        cancelled = 0
        for entry in list(self._active_tasks.values()):
            if entry.user_id != user_id:
                continue
            if chat_id is not None and entry.chat_id != chat_id:
                continue
            if entry.task.done():
                continue
            entry.cancelled_by_user = True
            if entry.task.cancel():
                cancelled += 1
        return cancelled

    def cancel_all_tasks(self) -> int:
        """Cancel all active or queued tasks across all users (admin override)."""
        cancelled = 0
        for entry in list(self._active_tasks.values()):
            if entry.task.done():
                continue
            entry.cancelled_by_user = True
            if entry.task.cancel():
                cancelled += 1
        return cancelled

    def create(
        self,
        *,
        user_id: int,
        chat_id: int,
        info: VideoInfo,
        platform: str = "youtube",
        ig_kind: str = "reel",
        time_range: TimeRange | None = None,
    ) -> PendingJob:
        self._prune()
        job_id = secrets.token_hex(6)
        job = PendingJob(
            job_id=job_id,
            user_id=user_id,
            chat_id=chat_id,
            info=info,
            platform=platform,
            ig_kind=ig_kind,
            time_range=time_range,
        )
        self._jobs[job_id] = job
        return job

    def get(self, job_id: str, user_id: int) -> PendingJob | None:
        self._prune()
        job = self._jobs.get(job_id)
        if job is None or job.user_id != user_id:
            return None
        return job

    def delete(self, job_id: str) -> None:
        self._jobs.pop(job_id, None)

    def _prune(self) -> None:
        now = time.monotonic()
        expired = [
            jid
            for jid, job in self._jobs.items()
            if now - job.created_at > self._ttl_seconds
        ]
        for jid in expired:
            self._jobs.pop(jid, None)

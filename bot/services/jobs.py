from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass, field

from bot.services.downloader import VideoInfo
from bot.utils.parsers import TimeRange


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


class PendingJobStore:
    """In-memory store of pending download requests with automatic TTL expiration."""

    def __init__(self, max_concurrent_downloads: int = 2, ttl_seconds: int = 7200) -> None:
        self._jobs: dict[str, PendingJob] = {}
        self._ttl_seconds = ttl_seconds
        self.semaphore = asyncio.Semaphore(max(1, max_concurrent_downloads))

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

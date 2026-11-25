from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from bot.utils.parsers import TimeRange

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class CachedMedia:
    """Cached Telegram file_id metadata for instant re-sending."""

    file_id: str
    media_kind: str  # "video", "document", or "audio"
    title: str
    uploader: str
    duration: int | None
    width: int | None
    height: int | None
    size_bytes: int
    created_at: float


class MediaCache:
    """Persistent JSON-backed cache mapping media requests to Telegram file_ids."""

    def __init__(self, cache_file: Path, *, max_entries: int = 2000) -> None:
        self.cache_file = cache_file
        self.max_entries = max_entries
        self._entries: dict[str, CachedMedia] = {}
        self._load()

    @property
    def count(self) -> int:
        return len(self._entries)

    @staticmethod
    def make_key(
        *,
        platform: str,
        video_id: str,
        quality: str,
        as_document: bool,
        time_range: TimeRange | None,
    ) -> str:
        trim_part = f"{time_range.start_str}-{time_range.end_str}" if time_range else ""
        doc_part = "1" if as_document else "0"
        return f"{platform}|{video_id}|{quality}|{doc_part}|{trim_part}"

    def get(self, key: str) -> CachedMedia | None:
        return self._entries.get(key)

    def put(self, key: str, entry: CachedMedia) -> None:
        self._entries[key] = entry
        if len(self._entries) > self.max_entries:
            oldest = sorted(
                self._entries.items(), key=lambda item: item[1].created_at
            )[: len(self._entries) - self.max_entries]
            for old_key, _ in oldest:
                self._entries.pop(old_key, None)
        self._save()

    def delete(self, key: str) -> bool:
        if key in self._entries:
            self._entries.pop(key, None)
            self._save()
            return True
        return False

    def clear(self) -> int:
        removed = len(self._entries)
        self._entries.clear()
        self._save()
        return removed

    def _load(self) -> None:
        if not self.cache_file.exists():
            return
        try:
            raw = json.loads(self.cache_file.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Could not load media cache %s: %s", self.cache_file, exc)
            return

        for key, item in (raw.get("entries") or {}).items():
            if not isinstance(item, dict) or not item.get("file_id"):
                continue
            try:
                self._entries[str(key)] = CachedMedia(
                    file_id=str(item["file_id"]),
                    media_kind=str(item.get("media_kind") or "video"),
                    title=str(item.get("title") or "Media"),
                    uploader=str(item.get("uploader") or ""),
                    duration=item.get("duration"),
                    width=item.get("width"),
                    height=item.get("height"),
                    size_bytes=int(item.get("size_bytes") or 0),
                    created_at=float(item.get("created_at") or time.time()),
                )
            except (TypeError, ValueError):
                continue

    def _save(self) -> None:
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "entries": {key: asdict(entry) for key, entry in self._entries.items()}
        }
        tmp = self.cache_file.with_name(f"{self.cache_file.name}.tmp.{os.getpid()}")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, self.cache_file)

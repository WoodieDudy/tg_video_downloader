from __future__ import annotations

import asyncio
import json
import logging
import shutil
import uuid
from pathlib import Path
from typing import Any

import aiohttp

from bot.services.downloader import (
    DownloadError,
    DownloadedMedia,
    FileTooLargeError,
    ProgressCallback,
    StageCallback,
    VideoInfo,
    communicate_with_timeout,
    compress_video_for_telegram,
    parse_progress_line,
    probe_media,
    summarize_video_formats,
    terminate_process,
)
from bot.utils.parsers import TimeRange

logger = logging.getLogger(__name__)


def parse_tikwm_payload(payload: dict[str, Any], original_url: str) -> dict[str, Any] | None:
    """Extract normalized video metadata and direct non-watermarked play URL from tikwm API."""
    if payload.get("code") != 0:
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None

    play_url = data.get("hdplay") or data.get("play")
    if not play_url:
        return None

    author = data.get("author") or {}
    uploader = (
        author.get("unique_id")
        or author.get("nickname")
        or "TikTok"
    )
    return {
        "id": str(data.get("id") or ""),
        "title": str(data.get("title") or "TikTok Video"),
        "uploader": str(uploader),
        "duration": data.get("duration"),
        "play_url": str(play_url),
        "music_url": str(data.get("music") or ""),
        "webpage_url": original_url,
    }


class TikTokDownloader:
    """Downloads TikTok videos without watermark via yt-dlp (--load-info-json) + tikwm fallback."""

    def __init__(
        self,
        downloads_dir: Path,
        *,
        max_file_size_bytes: int = 2000 * 1024 * 1024,
        socket_timeout_seconds: int = 20,
        fetch_timeout_seconds: int = 45,
        download_timeout_seconds: int = 300,
        video_crf: int = 24,
        video_preset: str = "medium",
    ) -> None:
        self.downloads_dir = downloads_dir
        self.max_file_size_bytes = max_file_size_bytes
        self.socket_timeout_seconds = socket_timeout_seconds
        self.fetch_timeout_seconds = fetch_timeout_seconds
        self.download_timeout_seconds = download_timeout_seconds
        self.video_crf = video_crf
        self.video_preset = video_preset
        self.downloads_dir.mkdir(parents=True, exist_ok=True)
        self._ytdlp_info_cache: dict[str, dict[str, Any]] = {}
        self._tikwm_cache: dict[str, dict[str, Any]] = {}

    async def fetch_info(self, url: str) -> VideoInfo:
        """Fetch TikTok metadata once (yt-dlp first, tikwm fallback) and cache stream info."""
        data = await self._fetch_ytdlp_json(url)
        if data is not None:
            formats = [
                fmt
                for fmt in (data.get("formats") or [])
                if fmt.get("format_note") != "watermarked" and fmt.get("format_id") != "download"
            ]
            available_heights, estimated_sizes, is_portrait = summarize_video_formats(formats)
            webpage_url = str(data.get("webpage_url") or url)
            self._ytdlp_info_cache[webpage_url] = data
            self._ytdlp_info_cache[url] = data
            # Bound in-memory cache size
            if len(self._ytdlp_info_cache) > 100:
                oldest_key = next(iter(self._ytdlp_info_cache))
                self._ytdlp_info_cache.pop(oldest_key, None)

            return VideoInfo(
                video_id=str(data.get("id") or ""),
                title=str(data.get("title") or "TikTok Video"),
                uploader=str(data.get("uploader") or data.get("creator") or "TikTok"),
                duration=data.get("duration"),
                webpage_url=webpage_url,
                is_shorts=True,
                is_portrait=is_portrait,
                is_live=False,
                available_heights=available_heights,
                estimated_sizes=estimated_sizes,
            )

        tikwm_info = await self._fetch_tikwm_info(url)
        if tikwm_info is not None:
            self._tikwm_cache[url] = tikwm_info
            return VideoInfo(
                video_id=tikwm_info["id"],
                title=tikwm_info["title"],
                uploader=tikwm_info["uploader"],
                duration=tikwm_info["duration"],
                webpage_url=url,
                is_shorts=True,
                is_portrait=True,
                is_live=False,
                available_heights=[],
                estimated_sizes={},
            )

        raise DownloadError("Не удалось получить данные видео TikTok.")

    async def download(
        self,
        url: str,
        *,
        quality: str = "best",
        is_portrait: bool = True,
        time_range: TimeRange | None = None,
        compress: bool = True,
        on_progress: ProgressCallback | None = None,
        on_stage: StageCallback | None = None,
    ) -> DownloadedMedia:
        """Download TikTok video without watermark, optionally trimming or extracting MP3."""
        job_dir = self.downloads_dir / uuid.uuid4().hex
        job_dir.mkdir(parents=True, exist_ok=True)
        cleanup_on_error = True
        is_audio = quality == "audio"

        try:
            cached_json = self._ytdlp_info_cache.pop(url, None)
            media_file: Path | None = None
            title = "TikTok Video"
            uploader = "TikTok"

            if cached_json is not None:
                title = str(cached_json.get("title") or title)
                uploader = str(cached_json.get("uploader") or uploader)
                media_file = await self._download_via_ytdlp(
                    url=url,
                    job_dir=job_dir,
                    quality=quality,
                    is_portrait=is_portrait,
                    time_range=time_range,
                    cached_json=cached_json,
                    on_progress=on_progress,
                )

            if media_file is None:
                tikwm_info = self._tikwm_cache.pop(url, None) or await self._fetch_tikwm_info(url)
                if tikwm_info is not None:
                    title = tikwm_info["title"]
                    uploader = tikwm_info["uploader"]
                    media_file = await self._download_via_tikwm(
                        tikwm_info=tikwm_info,
                        job_dir=job_dir,
                        is_audio=is_audio,
                        time_range=time_range,
                    )
                else:
                    media_file = await self._download_via_ytdlp(
                        url=url,
                        job_dir=job_dir,
                        quality=quality,
                        is_portrait=is_portrait,
                        time_range=time_range,
                        cached_json=None,
                        on_progress=on_progress,
                    )

            if media_file is None or not media_file.exists():
                raise DownloadError("Не удалось скачать видео TikTok.")

            probe = await probe_media(media_file)

            if compress and not is_audio:
                if on_stage is not None:
                    try:
                        await on_stage("compressing")
                    except Exception:
                        pass
                media_file = await compress_video_for_telegram(
                    media_file,
                    probe,
                    crf=self.video_crf,
                    preset=self.video_preset,
                )
                probe = await probe_media(media_file)

            size_bytes = media_file.stat().st_size
            if size_bytes > self.max_file_size_bytes:
                raise FileTooLargeError(size_bytes, self.max_file_size_bytes)

            cleanup_on_error = False
            return DownloadedMedia(
                job_dir=job_dir,
                file_path=media_file,
                title=probe.get("title") or title,
                uploader=probe.get("artist") or uploader,
                is_audio=is_audio,
                width=probe.get("width"),
                height=probe.get("height"),
                duration=probe.get("duration"),
                size_bytes=size_bytes,
            )
        finally:
            if cleanup_on_error:
                shutil.rmtree(job_dir, ignore_errors=True)

    async def _fetch_ytdlp_json(self, url: str) -> dict[str, Any] | None:
        cmd = [
            "yt-dlp",
            "--no-playlist",
            "--no-warnings",
            "--no-colors",
        ]
        if self.socket_timeout_seconds > 0:
            cmd.extend(["--socket-timeout", str(self.socket_timeout_seconds)])
        cmd.extend(["--dump-single-json", url])
        for attempt in range(2):
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout_b, stderr_b = await communicate_with_timeout(
                    proc, self.fetch_timeout_seconds
                )
            except TimeoutError:
                logger.warning("yt-dlp TikTok info attempt %d timed out", attempt + 1)
                continue
            if proc.returncode == 0:
                try:
                    return json.loads(stdout_b.decode("utf-8"))
                except json.JSONDecodeError:
                    return None
            logger.warning(
                "yt-dlp TikTok info attempt %d failed: %s",
                attempt + 1,
                stderr_b.decode("utf-8", errors="replace").strip(),
            )
            if attempt == 0:
                await asyncio.sleep(1.0)
        return None

    async def _download_via_ytdlp(
        self,
        *,
        url: str,
        job_dir: Path,
        quality: str,
        is_portrait: bool,
        time_range: TimeRange | None,
        cached_json: dict[str, Any] | None,
        on_progress: ProgressCallback | None,
    ) -> Path | None:
        is_audio = quality == "audio"
        output_template = str(job_dir / "tt_%(id)s.%(ext)s")
        cmd = [
            "yt-dlp",
            "--no-playlist",
            "--no-warnings",
            "--no-colors",
            "--newline",
            "--progress",
            "--progress-template",
            "download:PROG|%(progress.downloaded_bytes)s|%(progress.total_bytes)s|%(progress.total_bytes_estimate)s|%(progress.speed)s|%(progress.eta)s|%(progress._percent_str)s",
            "--restrict-filenames",
            "-o",
            output_template,
        ]
        if self.socket_timeout_seconds > 0:
            cmd.extend(["--socket-timeout", str(self.socket_timeout_seconds)])

        if is_audio:
            cmd.extend(
                [
                    "-f",
                    "ba/b[format_note!=watermarked][format_id!=download]/b",
                    "-x",
                    "--audio-format",
                    "mp3",
                    "--audio-quality",
                    "0",
                    "--add-metadata",
                ]
            )
        else:
            if quality == "best":
                fmt_expr = "bv*+ba/b[format_note!=watermarked][format_id!=download]/b"
            else:
                dim_value = int(quality)
                dim_key = "width" if is_portrait else "height"
                fmt_expr = (
                    f"bv*[{dim_key}<={dim_value}]+ba/"
                    f"b[{dim_key}<={dim_value}][format_note!=watermarked][format_id!=download]/"
                    "b[format_note!=watermarked][format_id!=download]/b"
                )
            cmd.extend(
                [
                    "-f",
                    fmt_expr,
                    "-S",
                    "res,vcodec:h264,ext:mp4:m4a",
                    "--merge-output-format",
                    "mp4",
                ]
            )

        if time_range is not None:
            cmd.extend(
                [
                    "--download-sections",
                    time_range.ytdlp_section,
                    "--force-keyframes-at-cuts",
                ]
            )

        if cached_json is not None:
            info_path = job_dir / "info.json"
            info_path.write_text(json.dumps(cached_json), encoding="utf-8")
            cmd.extend(["--load-info-json", str(info_path)])
        else:
            cmd.append(url)

        logger.info("Running TikTok yt-dlp: %s", " ".join(cmd))
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stderr_lines: list[str] = []

        async def _read_stdout() -> None:
            assert proc.stdout is not None
            while raw := await proc.stdout.readline():
                line = raw.decode("utf-8", errors="replace").strip()
                progress = parse_progress_line(line)
                if progress is not None and on_progress is not None:
                    try:
                        await on_progress(progress)
                    except Exception:
                        pass

        async def _read_stderr() -> None:
            assert proc.stderr is not None
            while raw := await proc.stderr.readline():
                line = raw.decode("utf-8", errors="replace").strip()
                if line:
                    stderr_lines.append(line)

        async def _wait_download() -> int:
            await asyncio.gather(_read_stdout(), _read_stderr())
            return await proc.wait()

        try:
            if self.download_timeout_seconds > 0:
                returncode = await asyncio.wait_for(
                    _wait_download(), timeout=self.download_timeout_seconds
                )
            else:
                returncode = await _wait_download()
        except TimeoutError:
            await terminate_process(proc)
            logger.warning("yt-dlp TikTok download timed out after %ds", self.download_timeout_seconds)
            return None
        except BaseException:
            await terminate_process(proc)
            raise
        if returncode != 0:
            logger.warning("yt-dlp TikTok download failed: %s", "\n".join(stderr_lines))
            return None

        return self._find_file(job_dir, is_audio=is_audio)

    @staticmethod
    async def _fetch_tikwm_info(url: str) -> dict[str, Any] | None:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "https://www.tikwm.com/api/",
                    params={"url": url, "hd": "1"},
                    timeout=aiohttp.ClientTimeout(total=15, sock_connect=5),
                ) as resp:
                    if resp.status != 200:
                        return None
                    payload = await resp.json(content_type=None)
                    return parse_tikwm_payload(payload, url)
        except Exception as exc:
            logger.warning("tikwm API lookup failed: %s", exc)
            return None

    async def _download_via_tikwm(
        self,
        *,
        tikwm_info: dict[str, Any],
        job_dir: Path,
        is_audio: bool,
        time_range: TimeRange | None,
    ) -> Path:
        raw_path = job_dir / f"tt_{tikwm_info['id'] or 'video'}.mp4"
        async with aiohttp.ClientSession() as session:
            async with session.get(
                tikwm_info["play_url"],
                timeout=aiohttp.ClientTimeout(total=120, sock_connect=10),
            ) as resp:
                if resp.status != 200:
                    raise DownloadError(f"Ошибка загрузки TikTok CDN (HTTP {resp.status})")
                written = 0
                with raw_path.open("wb") as out_f:
                    async for chunk in resp.content.iter_chunked(65536):
                        written += len(chunk)
                        if written > self.max_file_size_bytes:
                            raw_path.unlink(missing_ok=True)
                            raise FileTooLargeError(written, self.max_file_size_bytes)
                        out_f.write(chunk)

        media_file = raw_path
        if time_range is not None:
            trimmed = job_dir / "tt_trim.mp4"
            cmd = [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                f"{time_range.start_seconds:.3f}",
                "-to",
                f"{time_range.end_seconds:.3f}",
                "-i",
                str(media_file),
                "-c:v",
                "libx264",
                "-preset",
                "fast",
                "-crf",
                "22",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                str(trimmed),
            ]
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
            )
            await proc.communicate()
            if proc.returncode == 0 and trimmed.exists():
                media_file.unlink(missing_ok=True)
                media_file = trimmed

        if is_audio:
            mp3_path = job_dir / "tt_audio.mp3"
            cmd = [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(media_file),
                "-vn",
                "-c:a",
                "libmp3lame",
                "-q:a",
                "0",
                str(mp3_path),
            ]
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
            )
            await proc.communicate()
            if proc.returncode != 0 or not mp3_path.exists():
                raise DownloadError("Не удалось извлечь MP3 из TikTok.")
            media_file.unlink(missing_ok=True)
            media_file = mp3_path

        return media_file

    @staticmethod
    def _find_file(job_dir: Path, *, is_audio: bool) -> Path | None:
        preferred_exts = {".mp3", ".m4a", ".ogg", ".opus"} if is_audio else {".mp4", ".mkv", ".webm"}
        candidates = [
            p
            for p in job_dir.iterdir()
            if p.is_file() and not p.name.endswith((".part", ".ytdl", ".tmp", ".json"))
        ]
        if not candidates:
            return None
        preferred = [p for p in candidates if p.suffix.lower() in preferred_exts]
        return max(preferred or candidates, key=lambda p: p.stat().st_size)

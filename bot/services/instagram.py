from __future__ import annotations

import asyncio
import html
import logging
import re
import shutil
import uuid
from pathlib import Path
from urllib.parse import urljoin, urlparse

import aiohttp

from bot.services.downloader import (
    DownloadError,
    DownloadedMedia,
    FileTooLargeError,
    StageCallback,
    VideoInfo,
    compress_video_for_telegram,
    probe_media,
)
from bot.utils.parsers import TimeRange

logger = logging.getLogger(__name__)

EMBED_BOT_UA = "TelegramBot (like TwitterBot)"

_META_TAG_RE = re.compile(
    r"<meta\s+[^>]*(?:property|name)=[\"']([^\"']+)[\"'][^>]*content=[\"']([^\"']*)[\"'][^>]*>",
    re.IGNORECASE,
)
_META_TAG_REV_RE = re.compile(
    r"<meta\s+[^>]*content=[\"']([^\"']*)[\"'][^>]*(?:property|name)=[\"']([^\"']+)[\"'][^>]*>",
    re.IGNORECASE,
)


def parse_og_metadata(html_text: str, base_url: str) -> dict[str, str]:
    """Extract OpenGraph / Twitter meta tags from embed-proxy HTML."""
    meta: dict[str, str] = {}
    for key, val in _META_TAG_RE.findall(html_text):
        meta.setdefault(key.lower(), html.unescape(val).strip())
    for val, key in _META_TAG_REV_RE.findall(html_text):
        meta.setdefault(key.lower(), html.unescape(val).strip())

    for video_key in ("og:video:secure_url", "og:video", "twitter:player:stream"):
        raw_url = meta.get(video_key)
        if raw_url:
            meta["resolved_video_url"] = urljoin(base_url, raw_url)
            break
    return meta


def build_cdn_fallback_urls(url: str) -> list[str]:
    """Return direct URL followed by scontent.cdninstagram.com fallback for Meta fbcdn hosts."""
    candidates = [url]
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host and host != "scontent.cdninstagram.com" and (
        host.endswith(".fbcdn.net") or host.endswith(".cdninstagram.com")
    ):
        candidates.append(parsed._replace(netloc="scontent.cdninstagram.com").geturl())
    return candidates


def _make_title_from_meta(meta: dict[str, str], shortcode: str) -> tuple[str, str]:
    uploader = meta.get("twitter:title") or "Instagram"
    if uploader.lower() in {"fix instagram embeds", "instagram"}:
        uploader = "Instagram"

    desc = (meta.get("og:description") or "").strip()
    if not desc or desc.lower() == "post not found":
        return f"Instagram Reel {shortcode}", uploader

    first_line = desc.splitlines()[0].strip()
    title = first_line if len(first_line) >= 4 else desc.replace("\n", " ").strip()
    if len(title) > 100:
        title = title[:97].rstrip() + "..."
    return title, uploader


class InstagramDownloader:
    """Downloads public Instagram Reels without an account via embed proxies + CDN fallback."""

    def __init__(
        self,
        downloads_dir: Path,
        *,
        max_file_size_bytes: int = 2000 * 1024 * 1024,
        video_crf: int = 24,
        video_preset: str = "medium",
    ) -> None:
        self.downloads_dir = downloads_dir
        self.max_file_size_bytes = max_file_size_bytes
        self.video_crf = video_crf
        self.video_preset = video_preset
        self.downloads_dir.mkdir(parents=True, exist_ok=True)

    async def fetch_info(
        self,
        *,
        shortcode: str,
        kind: str = "reel",
        canonical_url: str | None = None,
    ) -> VideoInfo:
        """Fetch lightweight metadata via eeinstagram; never fails if post metadata is missing."""
        webpage_url = canonical_url or f"https://www.instagram.com/{kind}/{shortcode}/"
        title = f"Instagram Reel {shortcode}"
        uploader = "Instagram"

        try:
            async with aiohttp.ClientSession(headers={"User-Agent": EMBED_BOT_UA}) as session:
                proxy_url = f"https://eeinstagram.com/{kind}/{shortcode}/"
                async with session.get(
                    proxy_url,
                    timeout=aiohttp.ClientTimeout(total=8, sock_connect=4),
                ) as resp:
                    if resp.status == 200:
                        text = await resp.text(errors="replace")
                        meta = parse_og_metadata(text, proxy_url)
                        title, uploader = _make_title_from_meta(meta, shortcode)
        except Exception as exc:
            logger.debug("Instagram metadata lookup skipped for %s: %s", shortcode, exc)

        return VideoInfo(
            video_id=shortcode,
            title=title,
            uploader=uploader,
            duration=None,
            webpage_url=webpage_url,
            is_shorts=True,
            is_portrait=True,
            is_live=False,
            available_heights=[],
            estimated_sizes={},
        )

    async def download(
        self,
        *,
        shortcode: str,
        kind: str = "reel",
        canonical_url: str | None = None,
        quality: str = "best",
        time_range: TimeRange | None = None,
        compress: bool = True,
        on_stage: StageCallback | None = None,
    ) -> DownloadedMedia:
        """Download an Instagram Reel/video using kkinstagram -> eeinstagram -> yt-dlp fallback."""
        job_dir = self.downloads_dir / uuid.uuid4().hex
        job_dir.mkdir(parents=True, exist_ok=True)
        cleanup_on_error = True
        is_audio = quality == "audio"
        webpage_url = canonical_url or f"https://www.instagram.com/{kind}/{shortcode}/"

        try:
            raw_path = job_dir / f"ig_{shortcode}.mp4"
            title = f"Instagram Reel {shortcode}"
            uploader = "Instagram"

            downloaded_meta = await self._try_proxy_download(
                shortcode=shortcode, kind=kind, dest_path=raw_path
            )
            if downloaded_meta is not None:
                title, uploader = downloaded_meta
            else:
                await self._fallback_ytdlp(webpage_url, raw_path)

            media_file = raw_path
            if time_range is not None:
                trimmed = job_dir / f"ig_{shortcode}.trim.mp4"
                await self._trim_video(media_file, trimmed, time_range)
                media_file.unlink(missing_ok=True)
                media_file = trimmed

            if is_audio:
                audio_path = job_dir / f"ig_{shortcode}.mp3"
                await self._extract_mp3(media_file, audio_path)
                media_file.unlink(missing_ok=True)
                media_file = audio_path
            elif compress:
                probe = await probe_media(media_file)
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

            size_bytes = media_file.stat().st_size
            if size_bytes > self.max_file_size_bytes:
                raise FileTooLargeError(size_bytes, self.max_file_size_bytes)

            final_probe = await probe_media(media_file)
            cleanup_on_error = False
            return DownloadedMedia(
                job_dir=job_dir,
                file_path=media_file,
                title=title,
                uploader=uploader,
                is_audio=is_audio,
                width=final_probe.get("width"),
                height=final_probe.get("height"),
                duration=final_probe.get("duration"),
                size_bytes=size_bytes,
            )
        finally:
            if cleanup_on_error:
                shutil.rmtree(job_dir, ignore_errors=True)

    async def _try_proxy_download(
        self,
        *,
        shortcode: str,
        kind: str,
        dest_path: Path,
    ) -> tuple[str, str] | None:
        async with aiohttp.ClientSession(headers={"User-Agent": EMBED_BOT_UA}) as session:
            # 1. Try kkinstagram (fastest direct 302 -> CDN mp4)
            kk_url = f"https://kkinstagram.com/{kind}/{shortcode}/"
            media_url = await self._resolve_embed_video_url(session, kk_url)
            if media_url and await self._download_stream(session, media_url, dest_path):
                return f"Instagram Reel {shortcode}", "Instagram"

            # 2. Try eeinstagram (provides og:video + caption/author metadata)
            ee_url = f"https://eeinstagram.com/{kind}/{shortcode}/"
            try:
                async with session.get(
                    ee_url,
                    timeout=aiohttp.ClientTimeout(total=12, sock_connect=5),
                ) as resp:
                    if resp.status == 200:
                        html_text = await resp.text(errors="replace")
                        meta = parse_og_metadata(html_text, ee_url)
                        resolved = meta.get("resolved_video_url")
                        if resolved:
                            final_media_url = await self._resolve_embed_video_url(session, resolved)
                            if final_media_url and await self._download_stream(
                                session, final_media_url, dest_path
                            ):
                                return _make_title_from_meta(meta, shortcode)
            except Exception as exc:
                logger.warning("eeinstagram lookup failed for %s: %s", shortcode, exc)

        return None

    async def _resolve_embed_video_url(
        self,
        session: aiohttp.ClientSession,
        url: str,
    ) -> str | None:
        try:
            async with session.get(
                url,
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=12, sock_connect=5),
            ) as resp:
                if resp.status in {301, 302, 303, 307, 308}:
                    loc = resp.headers.get("Location")
                    return urljoin(url, loc) if loc else None
                if resp.status == 200:
                    ct = (resp.headers.get("Content-Type") or "").lower()
                    if ct.startswith("video/"):
                        return url
                    if "html" in ct:
                        meta = parse_og_metadata(await resp.text(errors="replace"), url)
                        return meta.get("resolved_video_url")
        except Exception as exc:
            logger.warning("Embed resolve failed for %s: %s", url, exc)
        return None

    async def _download_stream(
        self,
        session: aiohttp.ClientSession,
        media_url: str,
        dest_path: Path,
    ) -> bool:
        for candidate_url in build_cdn_fallback_urls(media_url):
            try:
                async with session.get(
                    candidate_url,
                    timeout=aiohttp.ClientTimeout(total=120, sock_connect=5),
                ) as resp:
                    if resp.status != 200:
                        continue
                    ct = (resp.headers.get("Content-Type") or "").lower()
                    if ct and not (
                        ct.startswith("video/") or ct == "application/octet-stream"
                    ):
                        logger.info(
                            "Skipping non-video Content-Type=%s from %s",
                            ct,
                            urlparse(candidate_url).hostname,
                        )
                        return False

                    written = 0
                    with dest_path.open("wb") as out_f:
                        async for chunk in resp.content.iter_chunked(65536):
                            written += len(chunk)
                            if written > self.max_file_size_bytes:
                                dest_path.unlink(missing_ok=True)
                                raise FileTooLargeError(written, self.max_file_size_bytes)
                            out_f.write(chunk)

                    if written > 0:
                        return True
            except FileTooLargeError:
                raise
            except Exception as exc:
                logger.warning(
                    "Instagram CDN fetch failed (%s): %s",
                    urlparse(candidate_url).hostname,
                    exc,
                )
                dest_path.unlink(missing_ok=True)
        return False

    @staticmethod
    async def _fallback_ytdlp(webpage_url: str, dest_path: Path) -> None:
        cmd = [
            "yt-dlp",
            "--no-playlist",
            "--no-warnings",
            "--no-colors",
            "-f",
            "bv*+ba/b",
            "--merge-output-format",
            "mp4",
            "-o",
            str(dest_path),
            webpage_url,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr_b = await proc.communicate()
        if proc.returncode != 0 or not dest_path.exists():
            stderr_txt = stderr_b.decode("utf-8", errors="replace").strip()
            last_line = stderr_txt.splitlines()[-1] if stderr_txt else "Неизвестная ошибка"
            raise DownloadError(
                "Не удалось скачать Instagram Reels без аккаунта (сервисы kkinstagram/eeinstagram не отдали видео, "
                f"а Instagram отклонил анонимный запрос).\n{last_line}"
            )

    @staticmethod
    async def _trim_video(input_path: Path, output_path: Path, time_range: TimeRange) -> None:
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
            str(input_path),
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
            str(output_path),
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr_b = await proc.communicate()
        if proc.returncode != 0 or not output_path.exists():
            raise DownloadError(
                f"Ошибка обрезки Instagram-видео: {stderr_b.decode('utf-8', errors='replace').strip()}"
            )

    @staticmethod
    async def _extract_mp3(input_path: Path, output_path: Path) -> None:
        cmd = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(input_path),
            "-vn",
            "-c:a",
            "libmp3lame",
            "-q:a",
            "0",
            str(output_path),
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr_b = await proc.communicate()
        if proc.returncode != 0 or not output_path.exists():
            raise DownloadError(
                f"Ошибка извлечения MP3: {stderr_b.decode('utf-8', errors='replace').strip()}"
            )

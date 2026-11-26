from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bot.services.chrome_cookies import ChromeSessionManager
from bot.utils.parsers import TimeRange, format_filesize

logger = logging.getLogger(__name__)

STANDARD_HEIGHTS = (2160, 1440, 1080, 720, 480, 360)

AUTH_RETRY_PATTERNS = (
    "sign in to confirm you're not a bot",
    "sign in to confirm you’re not a bot",
    "confirm you're not a bot",
    "cookies-from-browser",
    "http error 403",
    "login_required",
    "this video requires payment",
    "members-only",
    "sign in to view",
)


class DownloadError(Exception):
    """Base error raised for yt-dlp / ffmpeg download failures."""


class AuthRequiredError(DownloadError):
    """Raised when YouTube rejects cookies or requires re-authentication."""


class FileTooLargeError(DownloadError):
    """Raised when the downloaded media exceeds Telegram's upload limit."""

    def __init__(self, actual_bytes: int, limit_bytes: int) -> None:
        self.actual_bytes = actual_bytes
        self.limit_bytes = limit_bytes
        super().__init__(
            f"Размер файла ({format_filesize(actual_bytes)}) превышает лимит Telegram ({format_filesize(limit_bytes)})."
        )


@dataclass(slots=True)
class VideoInfo:
    """Metadata extracted from yt-dlp --dump-single-json."""

    video_id: str
    title: str
    uploader: str
    duration: float | None
    webpage_url: str
    is_shorts: bool
    is_portrait: bool
    is_live: bool
    available_heights: list[int]
    estimated_sizes: dict[int, int]


@dataclass(slots=True)
class DownloadProgress:
    """Parsed progress tick from yt-dlp."""

    percent_str: str
    downloaded_bytes: int | None
    total_bytes: int | None
    speed_bps: float | None
    eta_seconds: int | None


@dataclass(slots=True)
class DownloadedMedia:
    """Completed local media file ready to be sent via Telegram."""

    job_dir: Path
    file_path: Path
    title: str
    uploader: str
    is_audio: bool
    width: int | None
    height: int | None
    duration: int | None
    size_bytes: int

    def cleanup(self) -> None:
        shutil.rmtree(self.job_dir, ignore_errors=True)


ProgressCallback = Callable[[DownloadProgress], Awaitable[None]]
StageCallback = Callable[[str], Awaitable[None]]


def build_telegram_ffmpeg_cmd(
    input_path: Path,
    output_path: Path,
    *,
    crf: int = 24,
    preset: str = "medium",
) -> list[str]:
    """Build ffmpeg command tuned for Telegram playback and high visual quality at minimal size."""
    return [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(input_path),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-c:v",
        "libx264",
        "-preset",
        preset,
        "-crf",
        str(crf),
        "-profile:v",
        "high",
        "-pix_fmt",
        "yuv420p",
        "-vf",
        "scale=trunc(iw/2)*2:trunc(ih/2)*2",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-ac",
        "2",
        "-movflags",
        "+faststart",
        str(output_path),
    ]


def is_telegram_compatible_probe(probe: dict[str, Any], file_path: Path) -> bool:
    """Return True if media already uses H.264 + yuv420p + stereo AAC inside MP4."""
    return (
        file_path.suffix.lower() == ".mp4"
        and probe.get("video_codec") == "h264"
        and probe.get("pix_fmt") == "yuv420p"
        and probe.get("audio_codec") in {None, "aac"}
        and (probe.get("audio_channels") or 2) <= 2
    )


async def terminate_process(proc: asyncio.subprocess.Process) -> None:
    """Ensure a child process is stopped without leaving zombies on timeout or cancel."""
    if proc.returncode is not None:
        return
    try:
        proc.kill()
    except ProcessLookupError:
        return
    try:
        await proc.wait()
    except Exception:
        pass


async def communicate_with_timeout(
    proc: asyncio.subprocess.Process,
    timeout: float | None,
) -> tuple[bytes, bytes]:
    """Wait for subprocess output with timeout, killing the process on timeout or cancel."""
    try:
        if timeout is not None and timeout > 0:
            return await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return await proc.communicate()
    except BaseException:
        await terminate_process(proc)
        raise


async def probe_media(file_path: Path) -> dict[str, Any]:
    """Run ffprobe on a local media file and return normalized stream/format metadata."""
    cmd = [
        "ffprobe",
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_streams",
        "-show_format",
        str(file_path),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout_b, _ = await communicate_with_timeout(proc, 20.0)
    if proc.returncode != 0:
        return {}

    try:
        data = json.loads(stdout_b.decode("utf-8"))
    except json.JSONDecodeError:
        return {}

    info: dict[str, Any] = {}
    fmt = data.get("format") or {}
    if fmt.get("duration"):
        try:
            info["duration"] = int(round(float(fmt["duration"])))
        except (TypeError, ValueError):
            pass
    tags = fmt.get("tags") or {}
    if tags.get("title"):
        info["title"] = str(tags["title"])
    if tags.get("artist"):
        info["artist"] = str(tags["artist"])

    for stream in data.get("streams") or []:
        codec_type = stream.get("codec_type")
        if codec_type == "video" and "video_codec" not in info:
            info["video_codec"] = stream.get("codec_name")
            info["pix_fmt"] = stream.get("pix_fmt")
            width = stream.get("width")
            height = stream.get("height")
            side_data = stream.get("side_data_list") or []
            rotation = 0
            for item in side_data:
                if "rotation" in item:
                    try:
                        rotation = abs(int(item["rotation"]))
                    except (TypeError, ValueError):
                        pass
            if isinstance(width, int) and isinstance(height, int):
                if rotation in {90, 270}:
                    info["width"], info["height"] = height, width
                else:
                    info["width"], info["height"] = width, height
        elif codec_type == "audio" and "audio_codec" not in info:
            info["audio_codec"] = stream.get("codec_name")
            info["audio_channels"] = stream.get("channels")

    return info


async def compress_video_for_telegram(
    input_path: Path,
    probe: dict[str, Any],
    *,
    crf: int = 24,
    preset: str = "medium",
) -> Path:
    """Re-encode video to Telegram-friendly H.264/AAC MP4 with +faststart."""
    original_size = input_path.stat().st_size
    compressed_path = input_path.with_name(f"{input_path.stem}.tg.mp4")
    cmd = build_telegram_ffmpeg_cmd(
        input_path,
        compressed_path,
        crf=crf,
        preset=preset,
    )
    logger.info("Compressing video for Telegram: %s", " ".join(cmd))
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr_b = await communicate_with_timeout(proc, 180.0)
    except TimeoutError:
        logger.warning("ffmpeg compression timed out (keeping original)")
        compressed_path.unlink(missing_ok=True)
        return input_path
    except asyncio.CancelledError:
        compressed_path.unlink(missing_ok=True)
        raise
    if proc.returncode != 0 or not compressed_path.exists():
        err_txt = stderr_b.decode("utf-8", errors="replace").strip()
        logger.warning("ffmpeg compression failed (keeping original): %s", err_txt)
        compressed_path.unlink(missing_ok=True)
        return input_path

    compressed_size = compressed_path.stat().st_size
    if compressed_size >= original_size and is_telegram_compatible_probe(probe, input_path):
        logger.info(
            "Compressed file (%d B) is not smaller than compatible original (%d B); keeping original",
            compressed_size,
            original_size,
        )
        compressed_path.unlink(missing_ok=True)
        return input_path

    logger.info(
        "Compressed video from %s to %s",
        format_filesize(original_size),
        format_filesize(compressed_size),
    )
    input_path.unlink(missing_ok=True)
    return compressed_path


def is_auth_or_bot_error(stderr_text: str) -> bool:
    lowered = stderr_text.lower()
    return any(pattern in lowered for pattern in AUTH_RETRY_PATTERNS)


def parse_progress_line(line: str) -> DownloadProgress | None:
    """Parse structured yt-dlp progress line: PROG|dl|total|est|speed|eta|pct."""
    if not line.startswith("PROG|"):
        return None
    parts = line.strip().split("|")
    if len(parts) < 7:
        return None

    def _to_int(raw: str) -> int | None:
        try:
            if raw in {"", "NA", "None"}:
                return None
            return int(float(raw))
        except ValueError:
            return None

    def _to_float(raw: str) -> float | None:
        try:
            if raw in {"", "NA", "None"}:
                return None
            return float(raw)
        except ValueError:
            return None

    downloaded = _to_int(parts[1])
    total = _to_int(parts[2]) or _to_int(parts[3])
    speed = _to_float(parts[4])
    eta = _to_int(parts[5])
    percent = parts[6].strip()
    if percent in {"", "NA", "None"}:
        percent = "—"

    return DownloadProgress(
        percent_str=percent,
        downloaded_bytes=downloaded,
        total_bytes=total,
        speed_bps=speed,
        eta_seconds=eta,
    )


def summarize_video_formats(
    formats: list[dict[str, Any]],
) -> tuple[list[int], dict[int, int], bool]:
    """Return available standard resolutions, size estimates in bytes, and portrait flag."""
    best_video_by_res: dict[int, int] = {}
    best_audio_size = 0
    portrait_votes = 0
    landscape_votes = 0

    for fmt in formats:
        vcodec = fmt.get("vcodec") or "none"
        acodec = fmt.get("acodec") or "none"
        size = fmt.get("filesize") or fmt.get("filesize_approx") or 0
        try:
            size_int = int(size)
        except (TypeError, ValueError):
            size_int = 0

        if vcodec == "none" and acodec != "none":
            if size_int > best_audio_size:
                best_audio_size = size_int
            continue

        height = fmt.get("height")
        width = fmt.get("width")
        if vcodec != "none" and isinstance(height, int) and height > 0:
            if isinstance(width, int) and width > 0:
                if height > width:
                    portrait_votes += 1
                else:
                    landscape_votes += 1
                eff_res = min(width, height)
            else:
                eff_res = height

            prev = best_video_by_res.get(eff_res, 0)
            if size_int > prev:
                best_video_by_res[eff_res] = size_int

    is_portrait = portrait_votes > landscape_votes
    available: list[int] = []
    estimates: dict[int, int] = {}

    for target in STANDARD_HEIGHTS:
        # Match actual resolution near target (exact or within minor aspect rounding)
        exact_or_close = [r for r in best_video_by_res if abs(r - target) <= 20]
        if not exact_or_close:
            continue
        chosen_res = max(exact_or_close)
        available.append(target)
        v_size = best_video_by_res.get(chosen_res, 0)
        if v_size > 0:
            estimates[target] = v_size + best_audio_size

    # Fallback if video only has non-standard resolutions (e.g. 240p or 540p)
    if not available and best_video_by_res:
        for res in sorted(best_video_by_res.keys(), reverse=True):
            if res not in available:
                available.append(res)
                if best_video_by_res[res] > 0:
                    estimates[res] = best_video_by_res[res] + best_audio_size
            if len(available) >= 3:
                break

    return available, estimates, is_portrait


@dataclass(slots=True)
class _InfoCacheEntry:
    raw_json: dict[str, Any]
    used_cookies: bool
    created_at: float


class YouTubeDownloader:
    """Wrapper around yt-dlp and ffprobe with PO Token, anonymous-first, and Chrome cookie fallback."""

    _INFO_CACHE_TTL_SECONDS = 900.0

    def __init__(
        self,
        downloads_dir: Path,
        cookie_manager: ChromeSessionManager,
        *,
        max_file_size_bytes: int = 2000 * 1024 * 1024,
        js_runtime: str = "deno",
        pot_provider_url: str | None = None,
        sleep_requests: float = 1.0,
        concurrent_fragments: int = 2,
        socket_timeout_seconds: int = 20,
        fetch_timeout_seconds: int = 45,
        download_timeout_seconds: int = 300,
        video_crf: int = 24,
        video_preset: str = "medium",
    ) -> None:
        self.downloads_dir = downloads_dir
        self.cookie_manager = cookie_manager
        self.max_file_size_bytes = max_file_size_bytes
        self.js_runtime = js_runtime
        self.pot_provider_url = pot_provider_url.rstrip("/") if pot_provider_url else None
        self.sleep_requests = sleep_requests
        self.concurrent_fragments = concurrent_fragments
        self.socket_timeout_seconds = socket_timeout_seconds
        self.fetch_timeout_seconds = fetch_timeout_seconds
        self.download_timeout_seconds = download_timeout_seconds
        self.video_crf = video_crf
        self.video_preset = video_preset
        self._info_cache: dict[str, _InfoCacheEntry] = {}
        self.downloads_dir.mkdir(parents=True, exist_ok=True)

    async def fetch_info(self, url: str, *, is_shorts: bool = False) -> VideoInfo:
        """Fetch YouTube video metadata (anonymously first with PO Token, cookies as fallback)."""
        return await self._with_anonymous_then_cookies(
            lambda cookies_path: self._fetch_info_once(
                url, cookies_path=cookies_path, is_shorts=is_shorts
            )
        )

    async def download(
        self,
        url: str,
        *,
        quality: str = "best",
        is_portrait: bool = False,
        time_range: TimeRange | None = None,
        compress: bool = False,
        on_progress: ProgressCallback | None = None,
        on_stage: StageCallback | None = None,
    ) -> DownloadedMedia:
        """Download video or audio, reusing cached info.json when available to cut YouTube requests in half."""
        cached = self._pop_fresh_info_cache(url)
        if cached is not None:
            try:
                if cached.used_cookies:
                    return await self._with_cookie_retry(
                        lambda cp: self._download_once(
                            url,
                            quality=quality,
                            is_portrait=is_portrait,
                            time_range=time_range,
                            compress=compress,
                            cookies_path=cp,
                            info_json=cached.raw_json,
                            on_progress=on_progress,
                            on_stage=on_stage,
                        )
                    )
                return await self._download_once(
                    url,
                    quality=quality,
                    is_portrait=is_portrait,
                    time_range=time_range,
                    compress=compress,
                    cookies_path=None,
                    info_json=cached.raw_json,
                    on_progress=on_progress,
                    on_stage=on_stage,
                )
            except (AuthRequiredError, DownloadError) as exc:
                logger.warning(
                    "Download via cached info.json failed (%s); falling back to fresh fetch.",
                    exc,
                )

        return await self._with_anonymous_then_cookies(
            lambda cookies_path: self._download_once(
                url,
                quality=quality,
                is_portrait=is_portrait,
                time_range=time_range,
                compress=compress,
                cookies_path=cookies_path,
                info_json=None,
                on_progress=on_progress,
                on_stage=on_stage,
            )
        )

    async def _with_anonymous_then_cookies(
        self, operation: Callable[[Path | None], Awaitable[Any]]
    ) -> Any:
        """Try anonymous yt-dlp (+ PO Token) first; use Chrome cookies only if YouTube requires auth."""
        try:
            return await operation(None)
        except AuthRequiredError as anon_err:
            logger.info(
                "Anonymous YouTube request hit auth/bot gate (%s); retrying with Chrome cookies.",
                anon_err,
            )
            return await self._with_cookie_retry(operation)

    def _pop_fresh_info_cache(self, url: str) -> _InfoCacheEntry | None:
        now = time.monotonic()
        expired = [
            k
            for k, v in self._info_cache.items()
            if now - v.created_at > self._INFO_CACHE_TTL_SECONDS
        ]
        for k in expired:
            self._info_cache.pop(k, None)
        return self._info_cache.pop(url, None)

    async def _with_cookie_retry(self, operation: Callable[[Path | None], Awaitable[Any]]) -> Any:
        tmp_cookie_dir = self.downloads_dir / f".cookies-{uuid.uuid4().hex}"
        tmp_cookie_dir.mkdir(parents=True, exist_ok=True)
        try:
            snapshot = await self.cookie_manager.get_cookies_snapshot(
                tmp_cookie_dir / "cookies.txt"
            )
            try:
                return await operation(snapshot)
            except AuthRequiredError as first_err:
                logger.warning(
                    "yt-dlp hit auth/bot error, refreshing Chrome cookies and retrying: %s",
                    first_err,
                )
                try:
                    await self.cookie_manager.refresh_cookies(navigate=True)
                except Exception as refresh_exc:
                    raise AuthRequiredError(
                        f"Ошибка авторизации YouTube и не удалось обновить cookies из Chrome: {refresh_exc}"
                    ) from first_err

                refreshed_snapshot = await self.cookie_manager.get_cookies_snapshot(
                    tmp_cookie_dir / "cookies_retry.txt"
                )
                return await operation(refreshed_snapshot)
        finally:
            shutil.rmtree(tmp_cookie_dir, ignore_errors=True)

    def _base_ytdlp_args(self, cookies_path: Path | None) -> list[str]:
        args = [
            "yt-dlp",
            "--no-playlist",
            "--no-warnings",
            "--no-colors",
        ]
        if self.js_runtime:
            args.extend(["--js-runtimes", self.js_runtime])
        if self.pot_provider_url:
            args.extend(
                [
                    "--extractor-args",
                    f"youtubepot-bgutilhttp:base_url={self.pot_provider_url}",
                ]
            )
        if self.sleep_requests > 0:
            args.extend(["--sleep-requests", str(self.sleep_requests)])
        if self.concurrent_fragments > 0:
            args.extend(["--concurrent-fragments", str(self.concurrent_fragments)])
        if self.socket_timeout_seconds > 0:
            args.extend(["--socket-timeout", str(self.socket_timeout_seconds)])
        if cookies_path is not None and cookies_path.exists():
            args.extend(["--cookies", str(cookies_path)])
        return args

    async def _fetch_info_once(
        self,
        url: str,
        *,
        cookies_path: Path | None,
        is_shorts: bool,
    ) -> VideoInfo:
        cmd = [
            *self._base_ytdlp_args(cookies_path),
            "--dump-single-json",
            url,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await communicate_with_timeout(
                proc, self.fetch_timeout_seconds
            )
        except TimeoutError as exc:
            raise DownloadError(
                f"Превышено время ожидания ответа YouTube ({self.fetch_timeout_seconds} сек.)."
            ) from exc
        stderr_text = stderr_b.decode("utf-8", errors="replace").strip()

        if proc.returncode != 0:
            if is_auth_or_bot_error(stderr_text):
                raise AuthRequiredError(stderr_text or "Требуется авторизация YouTube")
            raise DownloadError(self._friendly_error(stderr_text))

        try:
            data = json.loads(stdout_b.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise DownloadError(f"Не удалось прочитать метаданные видео: {exc}") from exc

        formats = data.get("formats") or []
        available_heights, estimated_sizes, is_portrait = summarize_video_formats(formats)
        webpage_url = str(data.get("webpage_url") or url)

        entry = _InfoCacheEntry(
            raw_json=data,
            used_cookies=cookies_path is not None,
            created_at=time.monotonic(),
        )
        self._info_cache[webpage_url] = entry
        self._info_cache[url] = entry
        if len(self._info_cache) > 100:
            oldest_key = next(iter(self._info_cache))
            self._info_cache.pop(oldest_key, None)

        return VideoInfo(
            video_id=str(data.get("id", "")),
            title=str(data.get("title") or "YouTube Video"),
            uploader=str(data.get("uploader") or data.get("channel") or "YouTube"),
            duration=data.get("duration"),
            webpage_url=webpage_url,
            is_shorts=is_shorts or "/shorts/" in webpage_url,
            is_portrait=is_portrait,
            is_live=bool(data.get("is_live", False)),
            available_heights=available_heights,
            estimated_sizes=estimated_sizes,
        )

    async def _download_once(
        self,
        url: str,
        *,
        quality: str,
        is_portrait: bool,
        time_range: TimeRange | None,
        compress: bool,
        cookies_path: Path | None,
        info_json: dict[str, Any] | None = None,
        on_progress: ProgressCallback | None,
        on_stage: StageCallback | None,
    ) -> DownloadedMedia:
        job_dir = self.downloads_dir / uuid.uuid4().hex
        job_dir.mkdir(parents=True, exist_ok=True)
        cleanup_on_error = True

        try:
            is_audio = quality == "audio"
            output_template = str(job_dir / "%(title).120B [%(id)s].%(ext)s")

            cmd = [
                *self._base_ytdlp_args(cookies_path),
                "--newline",
                "--progress",
                "--progress-template",
                "download:PROG|%(progress.downloaded_bytes)s|%(progress.total_bytes)s|%(progress.total_bytes_estimate)s|%(progress.speed)s|%(progress.eta)s|%(progress._percent_str)s",
                "--restrict-filenames",
                "-o",
                output_template,
            ]

            if is_audio:
                cmd.extend(
                    [
                        "-f",
                        "ba/b",
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
                    fmt_expr = "bv*+ba/b"
                else:
                    dim_value = int(quality)
                    dim_key = "width" if is_portrait else "height"
                    fmt_expr = (
                        f"bv*[{dim_key}<={dim_value}]+ba/"
                        f"b[{dim_key}<={dim_value}]/bv*+ba/b"
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

            if info_json is not None:
                info_path = job_dir / "info.json"
                info_path.write_text(json.dumps(info_json), encoding="utf-8")
                cmd.extend(["--load-info-json", str(info_path)])
            else:
                cmd.append(url)
            logger.info("Running yt-dlp: %s", " ".join(cmd))

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
            except TimeoutError as exc:
                await terminate_process(proc)
                raise DownloadError(
                    f"Превышено время скачивания ({self.download_timeout_seconds} сек.)."
                ) from exc
            except BaseException:
                await terminate_process(proc)
                raise

            stderr_text = "\n".join(stderr_lines)

            if returncode != 0:
                if is_auth_or_bot_error(stderr_text):
                    raise AuthRequiredError(stderr_text or "Требуется авторизация YouTube")
                raise DownloadError(self._friendly_error(stderr_text))

            media_file = self._find_downloaded_file(job_dir, is_audio=is_audio)
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
                title=probe.get("title") or media_file.stem,
                uploader=probe.get("artist") or "YouTube",
                is_audio=is_audio,
                width=probe.get("width"),
                height=probe.get("height"),
                duration=probe.get("duration"),
                size_bytes=size_bytes,
            )
        finally:
            if cleanup_on_error:
                shutil.rmtree(job_dir, ignore_errors=True)

    @staticmethod
    def _find_downloaded_file(job_dir: Path, *, is_audio: bool) -> Path:
        preferred_exts = {".mp3", ".m4a", ".ogg", ".opus"} if is_audio else {".mp4", ".mkv", ".webm"}
        candidates = [
            p
            for p in job_dir.iterdir()
            if p.is_file() and not p.name.endswith((".part", ".ytdl", ".tmp", ".json"))
        ]
        if not candidates:
            raise DownloadError("Файл после скачивания не найден на диске.")

        preferred = [p for p in candidates if p.suffix.lower() in preferred_exts]
        pool = preferred or candidates
        return max(pool, key=lambda p: p.stat().st_size)

    @staticmethod
    def _friendly_error(stderr_text: str) -> str:
        if not stderr_text:
            return "Неизвестная ошибка yt-dlp."
        for line in reversed(stderr_text.splitlines()):
            if "ERROR:" in line:
                return line.split("ERROR:", 1)[1].strip()
        return stderr_text.splitlines()[-1].strip()

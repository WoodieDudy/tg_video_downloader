from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "www.youtu.be",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
}

INSTAGRAM_HOSTS = {
    "instagram.com",
    "www.instagram.com",
    "m.instagram.com",
    "kkinstagram.com",
    "www.kkinstagram.com",
    "eeinstagram.com",
    "www.eeinstagram.com",
    "ddinstagram.com",
    "www.ddinstagram.com",
    "vxinstagram.com",
    "www.vxinstagram.com",
}

TIKTOK_HOSTS = {
    "tiktok.com",
    "www.tiktok.com",
    "m.tiktok.com",
    "vm.tiktok.com",
    "vt.tiktok.com",
}

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_IG_SHORTCODE_RE = re.compile(r"^[A-Za-z0-9_-]{5,32}$")
_TT_NUMERIC_ID_RE = re.compile(r"^\d{8,25}$")
_TT_SHORT_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{5,32}$")
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_TIMECODE_TOKEN_RE = r"(?:\d{1,2}:)?\d{1,2}:\d{2}(?:\.\d{1,3})?|\d+(?:\.\d{1,3})?"
_RANGE_IN_TEXT_RE = re.compile(
    rf"(?<!\S)({_TIMECODE_TOKEN_RE})\s*[-–—]\s*({_TIMECODE_TOKEN_RE})(?!\S)"
)
_RANGE_PAIR_RE = re.compile(
    rf"^\s*({_TIMECODE_TOKEN_RE})(?:\s*[-–—]\s*|\s+)({_TIMECODE_TOKEN_RE})\s*$"
)


@dataclass(frozen=True, slots=True)
class TimeRange:
    """Validated inclusive/exclusive start..end clip range in seconds."""

    start_seconds: float
    end_seconds: float

    @property
    def duration_seconds(self) -> float:
        return self.end_seconds - self.start_seconds

    @property
    def start_str(self) -> str:
        return format_timestamp(self.start_seconds)

    @property
    def end_str(self) -> str:
        return format_timestamp(self.end_seconds)

    @property
    def ytdlp_section(self) -> str:
        return f"*{self.start_str}-{self.end_str}"

    def label(self) -> str:
        return f"{self.start_str} – {self.end_str}"


@dataclass(frozen=True, slots=True)
class ParsedYouTubeURL:
    """Canonicalized YouTube video reference."""

    video_id: str
    canonical_url: str
    original_url: str
    is_shorts: bool = False


@dataclass(frozen=True, slots=True)
class ParsedInstagramURL:
    """Canonicalized Instagram Reel / Post video reference."""

    shortcode: str
    kind: str  # "reel" or "p"
    canonical_url: str
    original_url: str

    @property
    def is_reel(self) -> bool:
        return self.kind == "reel"


@dataclass(frozen=True, slots=True)
class ParsedTikTokURL:
    """Canonicalized TikTok video reference."""

    video_id: str | None
    canonical_url: str
    original_url: str


@dataclass(frozen=True, slots=True)
class ParsedRequest:
    """Result of parsing a user's Telegram message for a YouTube URL and optional timecode."""

    video: ParsedYouTubeURL
    time_range: TimeRange | None = None


@dataclass(frozen=True, slots=True)
class ParsedInstagramRequest:
    """Result of parsing a user's message for an Instagram URL and optional timecode."""

    media: ParsedInstagramURL
    time_range: TimeRange | None = None


@dataclass(frozen=True, slots=True)
class ParsedTikTokRequest:
    """Result of parsing a user's message for a TikTok URL and optional timecode."""

    media: ParsedTikTokURL
    time_range: TimeRange | None = None


def parse_timestamp(raw: str) -> float:
    """Parse SS, MM:SS, or HH:MM:SS (with optional .ms) into seconds."""
    token = raw.strip()
    if not token:
        raise ValueError("Пустой таймкод")

    if ":" not in token:
        seconds = float(token)
        if seconds < 0:
            raise ValueError("Таймкод не может быть отрицательным")
        return seconds

    parts = token.split(":")
    if len(parts) == 2:
        minutes_str, sec_str = parts
        hours = 0
        minutes = int(minutes_str)
    elif len(parts) == 3:
        hours_str, minutes_str, sec_str = parts
        hours = int(hours_str)
        minutes = int(minutes_str)
    else:
        raise ValueError(f"Неверный формат таймкода: {raw}")

    seconds = float(sec_str)
    if hours < 0 or minutes < 0 or minutes >= 60 or seconds < 0 or seconds >= 60:
        raise ValueError(f"Недопустимое значение времени: {raw}")

    return hours * 3600 + minutes * 60 + seconds


def format_timestamp(seconds: float) -> str:
    """Format seconds into HH:MM:SS (or MM:SS when under 1 hour)."""
    total_ms = round(seconds * 1000)
    total_sec, ms = divmod(total_ms, 1000)
    hours, rem = divmod(total_sec, 3600)
    minutes, secs = divmod(rem, 60)

    if hours > 0:
        base = f"{hours:02d}:{minutes:02d}:{secs:02d}"
    else:
        base = f"{minutes:02d}:{secs:02d}"
    return f"{base}.{ms:03d}".rstrip("0").rstrip(".") if ms else base


def parse_time_range(raw: str, *, max_duration: float | None = None) -> TimeRange:
    """Parse a range like '01:15-02:30' or '1:15 2:30' into a validated TimeRange."""
    match = _RANGE_PAIR_RE.match(raw)
    if not match:
        raise ValueError(
            "Неверный формат диапазона. Используй `ММ:СС-ММ:СС` или `ЧЧ:ММ:СС-ЧЧ:ММ:СС`, например `01:15-02:30`."
        )

    start = parse_timestamp(match.group(1))
    end = parse_timestamp(match.group(2))

    if end <= start:
        raise ValueError("Время окончания должно быть больше времени начала.")

    if max_duration is not None and max_duration > 0:
        if start >= max_duration:
            raise ValueError(
                f"Начало ({format_timestamp(start)}) превышает длительность видео ({format_timestamp(max_duration)})."
            )
        if end > max_duration + 1:
            raise ValueError(
                f"Конец ({format_timestamp(end)}) превышает длительность видео ({format_timestamp(max_duration)})."
            )

    return TimeRange(start_seconds=start, end_seconds=end)


def extract_youtube_url(text: str) -> ParsedYouTubeURL | None:
    """Find and normalize the first valid YouTube / Shorts URL in text."""
    for match in _URL_RE.finditer(text):
        candidate = match.group(0).rstrip(".,);!?]>")
        parsed = _parse_single_youtube_url(candidate)
        if parsed is not None:
            return parsed
    return None


def _parse_single_youtube_url(url: str) -> ParsedYouTubeURL | None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host not in YOUTUBE_HOSTS:
        return None

    path_parts = [p for p in parsed.path.split("/") if p]
    video_id: str | None = None
    is_shorts = False

    if host in {"youtu.be", "www.youtu.be"}:
        if path_parts:
            video_id = path_parts[0]
    else:
        if not path_parts:
            return None
        head = path_parts[0].lower()
        if head == "watch":
            qs = parse_qs(parsed.query)
            video_id = qs.get("v", [None])[0]
        elif head == "shorts" and len(path_parts) >= 2:
            video_id = path_parts[1]
            is_shorts = True
        elif head in {"live", "embed", "v"} and len(path_parts) >= 2:
            video_id = path_parts[1]

    if not video_id or not _VIDEO_ID_RE.match(video_id):
        return None

    canonical = (
        f"https://www.youtube.com/shorts/{video_id}"
        if is_shorts
        else f"https://www.youtube.com/watch?v={video_id}"
    )
    return ParsedYouTubeURL(
        video_id=video_id,
        canonical_url=canonical,
        original_url=url,
        is_shorts=is_shorts,
    )


def extract_instagram_url(text: str) -> ParsedInstagramURL | None:
    """Find and normalize the first valid Instagram Reel / Post URL in text."""
    for match in _URL_RE.finditer(text):
        candidate = match.group(0).rstrip(".,);!?]>")
        parsed = _parse_single_instagram_url(candidate)
        if parsed is not None:
            return parsed
    return None


def _parse_single_instagram_url(url: str) -> ParsedInstagramURL | None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host not in INSTAGRAM_HOSTS:
        return None

    path_parts = [p for p in parsed.path.split("/") if p]
    if len(path_parts) < 2:
        return None

    head = path_parts[0].lower()
    shortcode: str | None = None
    kind: str | None = None

    if head in {"reel", "reels"}:
        kind = "reel"
        shortcode = path_parts[1]
    elif head == "p":
        kind = "p"
        shortcode = path_parts[1]
    elif len(path_parts) >= 3 and path_parts[1].lower() in {"reel", "reels", "p"}:
        # Format: instagram.com/<username>/reel/<shortcode>/
        kind = "p" if path_parts[1].lower() == "p" else "reel"
        shortcode = path_parts[2]

    if not shortcode or not kind or not _IG_SHORTCODE_RE.match(shortcode):
        return None

    canonical = f"https://www.instagram.com/{kind}/{shortcode}/"
    return ParsedInstagramURL(
        shortcode=shortcode,
        kind=kind,
        canonical_url=canonical,
        original_url=url,
    )


def extract_tiktok_url(text: str) -> ParsedTikTokURL | None:
    """Find and normalize the first valid TikTok video URL in text."""
    for match in _URL_RE.finditer(text):
        candidate = match.group(0).rstrip(".,);!?]>")
        parsed = _parse_single_tiktok_url(candidate)
        if parsed is not None:
            return parsed
    return None


def _parse_single_tiktok_url(url: str) -> ParsedTikTokURL | None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host not in TIKTOK_HOSTS:
        return None

    path_parts = [p for p in parsed.path.split("/") if p]
    if not path_parts:
        return None

    if host in {"vm.tiktok.com", "vt.tiktok.com"}:
        code = path_parts[0]
        if not _TT_SHORT_CODE_RE.match(code):
            return None
        return ParsedTikTokURL(
            video_id=None,
            canonical_url=f"https://{host}/{code}/",
            original_url=url,
        )

    # tiktok.com/t/<code>/
    if path_parts[0].lower() == "t" and len(path_parts) >= 2 and _TT_SHORT_CODE_RE.match(path_parts[1]):
        return ParsedTikTokURL(
            video_id=None,
            canonical_url=f"https://www.tiktok.com/t/{path_parts[1]}/",
            original_url=url,
        )

    # tiktok.com/@user/video/<numeric_id>
    if (
        len(path_parts) >= 3
        and path_parts[0].startswith("@")
        and path_parts[1].lower() == "video"
        and _TT_NUMERIC_ID_RE.match(path_parts[2])
    ):
        video_id = path_parts[2]
        return ParsedTikTokURL(
            video_id=video_id,
            canonical_url=f"https://www.tiktok.com/{path_parts[0]}/video/{video_id}",
            original_url=url,
        )

    return None


def _extract_inline_time_range(text: str, strip_url: str) -> TimeRange | None:
    text_without_url = text.replace(strip_url, " ")
    range_match = _RANGE_IN_TEXT_RE.search(text_without_url)
    if not range_match:
        return None
    try:
        return parse_time_range(f"{range_match.group(1)}-{range_match.group(2)}")
    except ValueError:
        return None


def parse_user_request(text: str) -> ParsedRequest | None:
    """Extract a YouTube URL and optional inline timecode range from a message."""
    video = extract_youtube_url(text)
    if video is None:
        return None

    return ParsedRequest(
        video=video,
        time_range=_extract_inline_time_range(text, video.original_url),
    )


def parse_instagram_request(text: str) -> ParsedInstagramRequest | None:
    """Extract an Instagram URL and optional inline timecode range from a message."""
    media = extract_instagram_url(text)
    if media is None:
        return None

    return ParsedInstagramRequest(
        media=media,
        time_range=_extract_inline_time_range(text, media.original_url),
    )


def parse_tiktok_request(text: str) -> ParsedTikTokRequest | None:
    """Extract a TikTok URL and optional inline timecode range from a message."""
    media = extract_tiktok_url(text)
    if media is None:
        return None

    return ParsedTikTokRequest(
        media=media,
        time_range=_extract_inline_time_range(text, media.original_url),
    )


def format_duration(seconds: float | int | None) -> str:
    if seconds is None or seconds <= 0:
        return "—"
    return format_timestamp(float(seconds))


def format_filesize(num_bytes: int | float | None) -> str:
    if not num_bytes or num_bytes <= 0:
        return "—"
    size = float(num_bytes)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if size < 1024.0 or unit == "ГБ":
            return f"{size:.1f} {unit}" if unit in {"МБ", "ГБ"} else f"{int(size)} {unit}"
        size /= 1024.0
    return f"{size:.1f} ГБ"

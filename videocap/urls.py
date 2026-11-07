from __future__ import annotations

import functools
import math
import re
from datetime import datetime
from typing import Any, Optional
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse, urlunparse

from videocap.config import YOUTUBE_EXTRACTORS, YOUTUBE_HOSTS


# Text copied from a chat or web page wraps links in these, so they cannot be part of a usable link.
TOKEN_SEPARATORS_RE = re.compile(r"[\s,，、;；|｜\"'“”‘’<>()\[\]{}（）「」『』【】《》。！？：]+")
TRAILING_PUNCTUATION = ".!?:…~"
DOMAIN_LIKE_RE = re.compile(r"^[\w-]+(\.[\w-]+)+([/?#]|$)")
# A site name in a sentence ("我在 youtube.com 看到的") or a number such as "4.5" points at no video, playlist or channel.
BARE_HOST_RE = re.compile(r"^[\w-]+(\.[\w-]+)+/?$")
EMBEDDED_SCHEME_RE = re.compile(r"(?i)https?://")
# Video and playlist links are plain ASCII, so the first other character starts text typed right after the link.
GLUED_TEXT_RE = re.compile(r"[^\x21-\x7e].*", re.S)

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
# "videoseries" and "live_stream" are 11 characters long too, but name a playlist or a channel's stream.
VIDEO_PATH_RE = re.compile(r"^/(?:shorts|live|embed|v|e)/(?!videoseries|live_stream)([A-Za-z0-9_-]{11})(?:[/?#]|$)")
VIDEO_PATH_PREFIX_RE = re.compile(r"^/(?:shorts|live|embed|v|e)/")
EMBED_PATH_RE = re.compile(r"^/embed/([A-Za-z0-9_-]+)")
STUDIO_VIDEO_PATH_RE = re.compile(r"^/video/([A-Za-z0-9_-]{11})(?:/|$)")
PLAYLIST_ID_RE = re.compile(r"^(?:PL|UU|FL|OL|LL|RD)[A-Za-z0-9_-]{12,}$")
CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
HANDLE_RE = re.compile(r"^@[\w.\-]{2,}$")
# On a line of its own, an ID with a typo still has the digits, "-" or "_" of one; plain words do not.
MISTYPED_ID_RE = re.compile(r"^(?=.*[0-9_-])[A-Za-z0-9_-]{6,}$")
CHANNEL_ROOT_RE = re.compile(r"^(/(?:@[^/]+|channel/[^/]+|c/[^/]+|user/[^/]+))/?$")
CHANNEL_PATH_PREFIXES = ("/@", "/channel/", "/c/", "/user/")

# The same pages under another name. One spelling keeps duplicates together, and yt-dlp's clip pattern has no mobile host.
HOST_ALIASES = {"m.youtube.com": "www.youtube.com", "youtubekids.com": "www.youtube.com", "www.youtubekids.com": "www.youtube.com"}
STUDIO_HOST = "studio.youtube.com"
SAME_SITE_HOSTS = frozenset({"youtube.com", "www.youtube.com"})
TRACKING_PARAMETERS = frozenset({"si", "feature", "pp"})

TIMESTAMP_PATTERN = r"(?:\d+:)?\d{1,2}:\d{1,2}(?:\.\d+)?|\d+(?:\.\d+)?"
TIME_RANGE_RE = re.compile(rf"^({TIMESTAMP_PATTERN})?\s*(?:-|~|～|到|至)\s*({TIMESTAMP_PATTERN})?$")
# What Chinese and Japanese input methods type, and the dashes chapter lists are copied with.
TIME_PUNCTUATION = str.maketrans({"：": ":", "–": "-", "—": "-", "－": "-", "〜": "~"})
PLAYLIST_ITEMS_RE = re.compile(r"^\s*-?\d+(\s*[-:]\s*-?\d+)?(\s*,\s*-?\d+(\s*[-:]\s*-?\d+)?)*\s*$")


@functools.cache
def allowed_extractors() -> tuple[Any, ...]:
    from yt_dlp.extractor import gen_extractor_classes

    # The same selection yt-dlp makes from the allowed_extractors option.
    patterns = [re.compile(pattern, re.I) for pattern in YOUTUBE_EXTRACTORS]
    return tuple(ie for ie in gen_extractor_classes() if any(pattern.fullmatch(ie.IE_NAME.lower()) for pattern in patterns))


def looks_like_video_id(token: str) -> bool:
    if not VIDEO_ID_RE.match(token):
        return False
    # Plain words such as "programming" are also 11 letters long.
    return any(char.isdigit() or char in "-_" for char in token) or token[1:] != token[1:].lower()


def expand_bare_token(token: str) -> Optional[str]:
    """Turns an ID or @handle copied without its URL into a full YouTube link."""
    if HANDLE_RE.match(token):
        return f"https://www.youtube.com/{token}"
    if CHANNEL_ID_RE.match(token):
        return f"https://www.youtube.com/channel/{token}"
    if PLAYLIST_ID_RE.match(token):
        return f"https://www.youtube.com/playlist?list={token}"
    if looks_like_video_id(token):
        return f"https://www.youtube.com/watch?v={token}"
    return None


def strip_leading_text(token: str) -> str:
    """Cuts off words typed right before a link without a space, as in "看這個https://..." or "Link:https://..."."""
    if token.startswith("//"):
        return f"https:{token}"
    match = EMBEDDED_SCHEME_RE.search(token)
    # A "/" before it means the link sits inside another address, not after some words.
    if match and "/" not in token[: match.start()]:
        return token[match.start() :]
    return token


def is_video_or_playlist_link(parsed: Any) -> bool:
    return (
        parsed.netloc == "youtu.be" or parsed.path.rstrip("/") in {"/watch", "/playlist"} or bool(VIDEO_PATH_PREFIX_RE.match(parsed.path))
    )


def rewrite_for_ytdlp(parsed: Any) -> Optional[str]:
    """Turns link shapes yt-dlp would mishandle into the ones it expects. Returns None for pages that hold nothing to download."""
    query = parse_qs(parsed.query)
    if parsed.netloc == STUDIO_HOST:
        match = STUDIO_VIDEO_PATH_RE.match(parsed.path)
        return f"https://www.youtube.com/watch?v={match.group(1)}" if match else None
    # The home page, which yt-dlp reads as the recommended feed.
    if parsed.path in {"", "/"} and "v" not in query:
        return None
    list_id = (query.get("list") or [""])[0]
    if list_id:
        embed = EMBED_PATH_RE.match(parsed.path)
        if embed and embed.group(1) == "videoseries":
            return f"https://www.youtube.com/playlist?list={list_id}"
        video_id = embed.group(1) if embed else parsed.path.strip("/") if parsed.netloc == "youtu.be" else ""
        # As a watch link, the "download the whole playlist" setting decides; yt-dlp would always take the playlist from these.
        if VIDEO_ID_RE.match(video_id):
            return f"https://www.youtube.com/watch?v={video_id}&{parsed.query}"
    return urlunparse(parsed)


def normalize_youtube_url(token: str) -> Optional[str]:
    candidate = token if "://" in token else f"https://{token}"
    try:
        parsed = urlparse(candidate)
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        return None

    has_tricky_authority = "@" in parsed.netloc or "\\" in candidate
    known_host = host in YOUTUBE_HOSTS or host in HOST_ALIASES or host == STUDIO_HOST
    if parsed.scheme not in {"http", "https"} or not known_host or has_tricky_authority or port not in {None, 80, 443}:
        return None

    # yt-dlp matches links case-sensitively, so a phone's "Youtu.be/..." only works with its host lowercased.
    parsed = parsed._replace(netloc=HOST_ALIASES.get(host, host))
    if is_video_or_playlist_link(parsed):
        parsed = urlparse(GLUED_TEXT_RE.sub("", urlunparse(parsed)))
    url = rewrite_for_ytdlp(parsed)
    # Anything else would only fail later, as a job, with yt-dlp's English "No suitable extractor".
    if url and any(ie.suitable(url) for ie in allowed_extractors()):
        return url
    return None


def extract_video_id(url: str) -> Optional[str]:
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    if host == "youtu.be":
        candidate = parsed.path.strip("/").split("/")[0]
        return candidate if VIDEO_ID_RE.match(candidate) else None
    if parsed.path.rstrip("/") == "/watch":
        candidate = (parse_qs(parsed.query).get("v") or [""])[0]
        return candidate if VIDEO_ID_RE.match(candidate) else None
    match = VIDEO_PATH_RE.match(parsed.path)
    return match.group(1) if match else None


def classify_youtube_url(url: str) -> str:
    """Returns video, playlist, channel, or other."""
    if extract_video_id(url):
        return "video"
    parsed = urlparse(url)
    if parsed.path.rstrip("/") == "/playlist" and "list" in parse_qs(parsed.query):
        return "playlist"
    if parsed.path.startswith(CHANNEL_PATH_PREFIXES):
        return "channel"
    return "other"


def url_identity(url: str, expand_playlist: bool = False) -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    # When whole playlists are wanted, a video link that names one asks for that playlist, not for the video.
    if expand_playlist and "list" in query:
        return f"list:{query['list'][0]}"
    video_id = extract_video_id(url)
    if video_id:
        return f"video:{video_id}"
    if "list" in query:
        return f"list:{query['list'][0]}"
    host = (parsed.hostname or "").lower()
    host = "youtube.com" if host in SAME_SITE_HOSTS else host
    kept = urlencode([(key, value) for key, value in parse_qsl(parsed.query) if key not in TRACKING_PARAMETERS])
    path = parsed.path.rstrip("/")
    # @handles and old channel names ignore case; channel IDs, also in a live embed's ?channel=, do not.
    if not path.startswith("/channel/"):
        path = path.lower()
    return f"{host}{path}?{kept}"


def parse_youtube_urls(raw: str, expand_playlist: bool = False) -> tuple[list[str], list[str]]:
    """Finds YouTube links in pasted text, dropping duplicates, and returns them with the entries that cannot be used."""
    urls: dict[str, str] = {}
    rejected: list[str] = []
    word_lines: list[list[tuple[str, Optional[str]]]] = []

    def add(url: str) -> None:
        urls.setdefault(url_identity(url, expand_playlist), url)

    # Links from a saved web page keep their HTML escaping, and its ";" would cut "&amp;list=..." off.
    for line in raw.replace("&amp;", "&").splitlines():
        found_link = False
        bare_tokens: list[tuple[str, Optional[str]]] = []
        for raw_token in TOKEN_SEPARATORS_RE.split(line):
            token = strip_leading_text(raw_token.strip(TRAILING_PUNCTUATION))
            if not token:
                continue
            if "://" in token or (DOMAIN_LIKE_RE.match(token) and not BARE_HOST_RE.match(token)):
                found_link = True
                url = normalize_youtube_url(token)
                if url:
                    add(url)
                else:
                    rejected.append(token)
                continue
            bare_tokens.append((token, expand_bare_token(token)))

        # Words around a link are the sentence it was pasted from, even ones shaped like an ID or @handle.
        if found_link or not bare_tokens:
            continue
        if all(expanded for _, expanded in bare_tokens):
            for _, expanded in bare_tokens:
                add(expanded)
        else:
            word_lines.append(bare_tokens)

    # A line of words is a typo worth reporting when nothing was found; next to links it is the message they came with.
    for tokens in word_lines:
        rejected.extend(token for token, expanded in tokens if not urls or (not expanded and MISTYPED_ID_RE.match(token)))

    return list(urls.values()), rejected


def describe_url_counts(urls: list[str], expand_playlist: bool = False) -> str:
    labels = {"video": "部影片", "playlist": "個播放清單", "channel": "個頻道", "other": "個其他連結"}
    counts: dict[str, int] = {}
    for url in urls:
        kind = classify_youtube_url(url)
        if expand_playlist and kind == "video" and "list" in parse_qs(urlparse(url).query):
            kind = "playlist"
        counts[kind] = counts.get(kind, 0) + 1
    return "、".join(f"{counts[kind]} {label}" for kind, label in labels.items() if kind in counts)


def apply_channel_tab(url: str, tab: str) -> str:
    """Points a channel's root URL at one tab; yt-dlp would otherwise walk every tab of the channel."""
    if tab == "all":
        return url
    parsed = urlparse(url)
    match = CHANNEL_ROOT_RE.match(parsed.path)
    if not match:
        return url
    return urlunparse(parsed._replace(path=f"{match.group(1)}/{tab}"))


def parse_timestamp(text: str) -> float:
    """Raises ValueError with a message for the user when minutes or seconds go past 59."""
    parts = text.translate(TIME_PUNCTUATION).split(":")
    if any(float(part) >= 60 for part in parts[1:]):
        raise ValueError(f"看不懂時間「{text}」：分鐘和秒數最多只到 59。")
    seconds = 0.0
    for part in parts:
        seconds = seconds * 60 + float(part)
    return seconds


def format_timestamp(seconds: float) -> str:
    if math.isinf(seconds):
        return "結尾"
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def parse_time_ranges(raw: str) -> tuple[tuple[float, float], ...]:
    """Parses clips such as "1:30-2:00, 10:00-" into (start, end) seconds. Raises ValueError with a message for the user."""
    ranges: list[tuple[float, float]] = []
    for part in re.split(r"[,，;；\n]+", raw.translate(TIME_PUNCTUATION)):
        part = part.strip()
        if not part:
            continue
        match = TIME_RANGE_RE.match(part)
        if not match or not (match.group(1) or match.group(2)):
            raise ValueError(f"看不懂時間範圍「{part}」，請用 1:30-2:00 這樣的寫法。")
        start = parse_timestamp(match.group(1)) if match.group(1) else 0.0
        end = parse_timestamp(match.group(2)) if match.group(2) else float("inf")
        if end <= start:
            raise ValueError(f"時間範圍「{part}」的結束時間要晚於開始時間。")
        ranges.append((start, end))
    return tuple(ranges)


def parse_date_filter(raw: str) -> str:
    """Normalizes a date to yt-dlp's YYYYMMDD. Raises ValueError with a message for the user."""
    cleaned = raw.strip().replace("/", "-")
    if not cleaned:
        return ""
    for pattern in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(cleaned, pattern).strftime("%Y%m%d")
        except ValueError:
            continue
    raise ValueError(f"看不懂日期「{raw}」，請用 2026-01-31 這樣的寫法。")


def validate_playlist_items(raw: str) -> str:
    cleaned = raw.strip()
    if cleaned and not PLAYLIST_ITEMS_RE.match(cleaned):
        raise ValueError(f"看不懂項目範圍「{raw}」，請用 1-5,8,10 這樣的寫法。")
    return cleaned.replace(" ", "")

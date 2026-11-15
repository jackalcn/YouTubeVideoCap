from __future__ import annotations

import functools
import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse, urlunparse

from videocap.config import YOUTUBE_EXTRACTORS
from videocap.sites import HOST_ALIASES, STUDIO_HOST, is_youtube_host, is_youtube_url, link_kind, site_of_url, strip_tracking


# Text copied from a chat or web page wraps links in these, so they cannot be part of a usable link.
TOKEN_SEPARATORS_RE = re.compile(r"[\s,，、;；|｜\"'“”‘’<>()\[\]{}（）「」『』【】《》。！？：]+")
TRAILING_PUNCTUATION = ".!?:…~"
DOMAIN_LIKE_RE = re.compile(r"^[\w-]+(\.[\w-]+)+([/?#]|$)")
# A site name in a sentence ("我在 youtube.com 看到的") or a number such as "4.5" points at no video, playlist or channel.
BARE_HOST_RE = re.compile(r"^[\w-]+(\.[\w-]+)+/?$")
EMBEDDED_SCHEME_RE = re.compile(r"(?i)https?://")
# Video and playlist links are plain ASCII, so the first other character starts text typed right after the link.
GLUED_TEXT_RE = re.compile(r"[^\x21-\x7e].*", re.S)
# Another site's link, taken whole: its parameters may hold the commas, semicolons and brackets that TOKEN_SEPARATORS_RE
# splits at, and a signed link stops working once cut. It ends at the first character that is not plain ASCII, as
# above, at a quote, a backtick or an angle bracket around it, or where a list's separator is followed by the next link.
OTHER_LINK_RE = re.compile(r"(?i)https?://(?:(?![,;|]https?://)[\x21\x23-\x3b\x3d\x3f-\x5f\x61-\x7e])+")
# What a sentence or its brackets leave at the end of a link.
OTHER_LINK_TRAILING = ".,;:!?'~*"
CLOSING_BRACKETS = {")": "(", "]": "[", "}": "{"}

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

SAME_SITE_HOSTS = frozenset({"youtube.com", "www.youtube.com"})
# YouTube's own. Other sites may give these names another meaning, so sites.strip_tracking handles their links.
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
    if parsed.scheme not in {"http", "https"} or not is_youtube_host(host) or has_tricky_authority or port not in {None, 80, 443}:
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
    """The YouTube video a link names. Other sites also have /watch?v= and /embed/ pages, which name no YouTube video."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    if not is_youtube_host(host):
        return None
    if host == "youtu.be":
        candidate = parsed.path.strip("/").split("/")[0]
        return candidate if VIDEO_ID_RE.match(candidate) else None
    if parsed.path.rstrip("/") == "/watch":
        candidate = (parse_qs(parsed.query).get("v") or [""])[0]
        return candidate if VIDEO_ID_RE.match(candidate) else None
    match = VIDEO_PATH_RE.match(parsed.path)
    return match.group(1) if match else None


def classify_youtube_url(url: str) -> str:
    """Returns video, playlist, channel, or other. Another site's link is always other: its paths mean something else."""
    if not is_youtube_url(url):
        return "other"
    if extract_video_id(url):
        return "video"
    parsed = urlparse(url)
    if parsed.path.rstrip("/") == "/playlist" and "list" in parse_qs(parsed.query):
        return "playlist"
    if parsed.path.startswith(CHANNEL_PATH_PREFIXES):
        return "channel"
    return "other"


def url_identity(url: str, expand_playlist: bool = False) -> str:
    if not is_youtube_url(url):
        # Another site's parameters mean whatever that site says, so only the known click trackers are left out.
        cleaned = urlparse(strip_tracking(url))
        return f"{cleaned.netloc.lower()}{cleaned.path}?{cleaned.query}#{cleaned.fragment}"
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


@dataclass(frozen=True)
class ParsedLinks:
    """What a pasted text holds."""

    urls: list[str] = field(default_factory=list)
    # Entries that look like links or IDs but cannot be used.
    rejected: list[str] = field(default_factory=list)
    # Other sites' links from someone who may only download from YouTube.
    blocked: list[str] = field(default_factory=list)
    # Links to services whose videos are all encrypted.
    protected: list[str] = field(default_factory=list)
    # Ordinary web pages set aside because they came mixed with real video links, such as the links in a description.
    pages: list[str] = field(default_factory=list)


class LinkScan:
    """Collects the links of a pasted text line by line, dropping duplicates and keeping the order they came in."""

    def __init__(self, expand_playlist: bool = False, other_sites: bool = False) -> None:
        self.expand_playlist = expand_playlist
        self.other_sites = other_sites
        self.urls: dict[str, str] = {}
        self.rejected: list[str] = []
        self.blocked: list[str] = []
        self.protected: list[str] = []
        self.page_links: dict[str, str] = {}
        self.word_lines: list[list[tuple[str, Optional[str]]]] = []

    def add(self, url: str) -> None:
        self.urls.setdefault(url_identity(url, self.expand_playlist), url)

    def scan_youtube_line(self, line: str, found_link: bool = False) -> None:
        bare_tokens: list[tuple[str, Optional[str]]] = []
        for raw_token in TOKEN_SEPARATORS_RE.split(line):
            token = strip_leading_text(raw_token.strip(TRAILING_PUNCTUATION))
            if not token:
                continue
            if "://" in token or (DOMAIN_LIKE_RE.match(token) and not BARE_HOST_RE.match(token)):
                found_link = True
                url = normalize_youtube_url(token)
                if url:
                    self.add(url)
                else:
                    self.rejected.append(token)
                continue
            bare_tokens.append((token, expand_bare_token(token)))

        # Words around a link are the sentence it was pasted from, even ones shaped like an ID or @handle.
        if found_link or not bare_tokens:
            return
        if all(expanded for _, expanded in bare_tokens):
            for _, expanded in bare_tokens:
                if expanded:
                    self.add(expanded)
        else:
            self.word_lines.append(bare_tokens)

    def take_other_links(self, line: str) -> tuple[str, bool]:
        """Handles the links to other sites in a line. Returns the line without them, and whether it had any."""
        found = False

        def take(match: re.Match[str]) -> str:
            nonlocal found
            link = trim_other_link(match.group(0))
            if is_youtube_url(link):
                # Left in place for the YouTube rules, which know its many shapes.
                return match.group(0)
            found = True
            self.add_other_link(link)
            return " "

        return OTHER_LINK_RE.sub(take, line), found

    def add_other_link(self, link: str) -> None:
        url = normalize_other_url(link)
        if url is None:
            self.rejected.append(link)
        elif not self.other_sites:
            self.blocked.append(url)
        elif link_kind(url) == "protected":
            self.protected.append(url)
        elif link_kind(url) == "page":
            self.page_links.setdefault(url_identity(url), url)
        else:
            self.add(url)

    def finish(self, include_pages: bool = False) -> ParsedLinks:
        pages = list(self.page_links.values())
        # Pages pasted on their own are what the person means to download; among video links they are more likely
        # the other links of a description, so they wait until asked for.
        if pages and (include_pages or not self.urls):
            for url in pages:
                self.add(url)
            pages = []
        # A line of words is a typo worth reporting when nothing was found; next to links it is the message they came with.
        for tokens in self.word_lines:
            self.rejected.extend(token for token, expanded in tokens if not self.urls or (not expanded and MISTYPED_ID_RE.match(token)))
        return ParsedLinks(list(self.urls.values()), self.rejected, self.blocked, self.protected, pages)


def trim_other_link(link: str) -> str:
    """Cuts off what the sentence around a link left at its end: its full stop, or the bracket that closes a pair
    opened before the link."""
    while link:
        last = link[-1]
        opener = CLOSING_BRACKETS.get(last)
        if last in OTHER_LINK_TRAILING or (opener and link.count(opener) < link.count(last)):
            link = link[:-1]
        else:
            break
    return link


def normalize_other_url(link: str) -> Optional[str]:
    """Another site's link as yt-dlp should get it, or None when it is not a usable web address."""
    try:
        parsed = urlparse(link)
        host = parsed.hostname or ""
        parsed.port
    except ValueError:
        return None
    # A name before "@" is a login, which also lets a link pose as another site's: https://www.youtube.com@example.com/
    if parsed.scheme.lower() not in {"http", "https"} or not host or "@" in parsed.netloc or "\\" in link:
        return None
    if parsed.scheme != parsed.scheme.lower() or parsed.netloc != parsed.netloc.lower():
        # yt-dlp matches sites case-sensitively.
        link = urlunparse(parsed._replace(scheme=parsed.scheme.lower(), netloc=parsed.netloc.lower()))
    return strip_tracking(link)


def html_unescaped_lines(raw: str) -> list[str]:
    # Links from a saved web page keep their HTML escaping, and its ";" would cut "&amp;list=..." off.
    return raw.replace("&amp;", "&").splitlines()


def parse_youtube_urls(raw: str, expand_playlist: bool = False) -> tuple[list[str], list[str]]:
    """Finds YouTube links in pasted text, dropping duplicates, and returns them with the entries that cannot be used."""
    scan = LinkScan(expand_playlist)
    for line in html_unescaped_lines(raw):
        scan.scan_youtube_line(line)
    found = scan.finish()
    return found.urls, found.rejected


def parse_links(raw: str, expand_playlist: bool = False, other_sites: bool = False, include_pages: bool = False) -> ParsedLinks:
    """Finds the links in pasted text. YouTube links are read as parse_youtube_urls reads them. Other sites' links must
    start with http:// or https://, and are used only when other_sites allows them, which is for this machine's owner:
    for anyone else they are reported as blocked."""
    scan = LinkScan(expand_playlist, other_sites)
    for line in html_unescaped_lines(raw):
        rest, found = scan.take_other_links(line)
        scan.scan_youtube_line(rest, found_link=found)
    return scan.finish(include_pages)


def describe_url_counts(urls: list[str], expand_playlist: bool = False) -> str:
    labels = {"video": "部影片", "playlist": "個播放清單", "channel": "個頻道", "other": "個其他連結"}
    counts: dict[str, int] = {}
    other_sites: dict[str, int] = {}
    for url in urls:
        if not is_youtube_url(url):
            site = site_of_url(url)
            other_sites[site] = other_sites.get(site, 0) + 1
            continue
        kind = classify_youtube_url(url)
        if expand_playlist and kind == "video" and "list" in parse_qs(urlparse(url).query):
            kind = "playlist"
        counts[kind] = counts.get(kind, 0) + 1
    parts = [f"{counts[kind]} {label}" for kind, label in labels.items() if kind in counts]
    parts.extend(f"{site} {count} 個連結" for site, count in other_sites.items())
    return "、".join(parts)


def apply_channel_tab(url: str, tab: str) -> str:
    """Points a channel's root URL at one tab; yt-dlp would otherwise walk every tab of the channel."""
    # Other sites also have /@name pages, which have no such tabs.
    if tab == "all" or not is_youtube_url(url):
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

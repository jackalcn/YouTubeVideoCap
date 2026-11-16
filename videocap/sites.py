"""Which site a link belongs to. YouTube is what the app was built for; this machine's owner may also download from
the other sites yt-dlp knows, from direct links to a media file, and from ordinary pages that hold a video."""

from __future__ import annotations

import functools
import hashlib
from typing import Any
from urllib.parse import urldefrag, urlparse, urlunparse

from videocap.config import YOUTUBE_HOSTS


# The same YouTube pages under another name. One spelling keeps duplicates together, and yt-dlp's clip pattern has no
# mobile host.
HOST_ALIASES = {"m.youtube.com": "www.youtube.com", "youtubekids.com": "www.youtube.com", "www.youtubekids.com": "www.youtube.com"}
STUDIO_HOST = "studio.youtube.com"

# Parameters that only say where a click came from. Everything else on another site's link is left exactly as it was
# written: a direct link's signature stops working when a parameter is dropped, reordered or encoded another way.
TRACKING_PREFIXES = ("utm_",)
TRACKING_NAMES = frozenset({"fbclid", "gclid", "dclid", "msclkid", "igshid", "igsh", "yclid", "mc_cid", "mc_eid"})

# A link that ends in one of these names a media file or a stream's playlist itself.
MEDIA_LINK_EXTENSIONS = frozenset(
    {".mp4", ".webm", ".mkv", ".mov", ".m4v", ".flv", ".avi", ".ts", ".m3u8", ".mpd", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".flac", ".wav"}
)
# Services whose every video is encrypted (DRM). Nothing can be downloaded from them, and nothing is tried.
PROTECTED_HOSTS = (
    "netflix.com",
    "disneyplus.com",
    "primevideo.com",
    "hulu.com",
    "max.com",
    "hbomax.com",
    "tv.apple.com",
    "peacocktv.com",
    "paramountplus.com",
    "kktv.me",
    "catchplay.com",
    "video.friday.tw",
    "myvideo.net.tw",
    "litv.tv",
    "hamivideo.hinet.net",
)
# yt-dlp's own list of such sites answers through this extractor.
PROTECTED_EXTRACTOR = "KnownDRM"
# yt-dlp names what these find after the file or the page, so two sites can give the same name.
URL_NAMED_EXTRACTORS = frozenset({"generic", "html5mediaembed"})
# How much of a file says what kind it is: enough for two packets of a transport stream.
MEDIA_SNIFF_BYTES = 512
TS_PACKET_BYTES = 188
TS_SYNC_BYTE = 0x47
# The name part of a URL-keyed ID. yt-dlp's own can be a whole address, and the ID goes into file names.
URL_KEYED_NAME_LENGTH = 40
# How the sites people ask for most are shown; yt-dlp's extractor names start with these.
SITE_NAMES = (
    ("youtube", "YouTube"),
    ("vimeo", "Vimeo"),
    ("bilibili", "Bilibili"),
    ("facebook", "Facebook"),
    ("instagram", "Instagram"),
    ("twitter", "X（Twitter）"),
    ("tiktok", "TikTok"),
    ("douyin", "抖音"),
    ("twitch", "Twitch"),
    ("dailymotion", "Dailymotion"),
    ("niconico", "Niconico"),
    ("soundcloud", "SoundCloud"),
    ("reddit", "Reddit"),
    ("bluesky", "Bluesky"),
    ("weibo", "微博"),
    ("xiaohongshu", "小紅書"),
    ("archiveorg", "Internet Archive"),
)


def host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def is_youtube_host(host: str) -> bool:
    return host in YOUTUBE_HOSTS or host in HOST_ALIASES or host == STUDIO_HOST


def is_youtube_url(url: str) -> bool:
    return is_youtube_host(host_of(url))


def is_tracking_parameter(name: str) -> bool:
    name = name.lower()
    return name.startswith(TRACKING_PREFIXES) or name in TRACKING_NAMES


def strip_tracking(url: str) -> str:
    """The link without the parameters known to track a click, and otherwise untouched."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return url
    parts = parsed.query.split("&") if parsed.query else []
    kept = [part for part in parts if not is_tracking_parameter(part.partition("=")[0])]
    if len(kept) == len(parts):
        return url
    return urlunparse(parsed._replace(query="&".join(kept)))


def is_media_link(url: str) -> bool:
    try:
        path = urlparse(url).path.lower()
    except ValueError:
        return False
    return any(path.endswith(extension) for extension in MEDIA_LINK_EXTENSIONS)


def is_protected_host(host: str) -> bool:
    return any(host == protected or host.endswith(f".{protected}") for protected in PROTECTED_HOSTS)


@functools.cache
def dedicated_extractors() -> tuple[Any, ...]:
    from yt_dlp.extractor import gen_extractor_classes

    return tuple(ie for ie in gen_extractor_classes() if ie.IE_NAME != "generic")


@functools.lru_cache(maxsize=4096)
def extractor_for(url: str) -> str:
    """The yt-dlp extractor written for this link's site, or "" when only the generic one would take the link."""
    for ie in dedicated_extractors():
        try:
            if ie.suitable(url):
                return str(ie.ie_key())
        except Exception:
            continue
    return ""


def link_kind(url: str) -> str:
    """youtube; protected (all DRM, nothing to download); known (a site yt-dlp has an extractor for); media (a direct
    link to a file or stream); or page (any other web page, which may or may not hold a video)."""
    host = host_of(url)
    if is_youtube_host(host):
        return "youtube"
    if is_protected_host(host):
        return "protected"
    if is_media_link(url):
        return "media"
    extractor = extractor_for(url)
    if extractor == PROTECTED_EXTRACTOR:
        return "protected"
    return "known" if extractor else "page"


def site_label(extractor: str, url: str = "") -> str:
    """The site's name for the person, from the yt-dlp extractor that handled the link."""
    key = extractor.lower()
    if not key or key in URL_NAMED_EXTRACTORS:
        host = host_of(url) or "其他網站"
        return f"{host}（直接連結）" if is_media_link(url) else f"{host}（一般網頁）"
    return next((name for prefix, name in SITE_NAMES if key.startswith(prefix)), extractor)


def site_of_url(url: str) -> str:
    """The site's name before anything was fetched, from the link alone."""
    if is_youtube_url(url):
        return "YouTube"
    return site_label(extractor_for(url), url)


def url_keyed_id(raw_id: str, page_url: str, media_url: str = "") -> str:
    """An ID for what a URL-named extractor found, which also says where it was found: "clip" becomes "clip-3f9a1c2e".
    Two sites' clip.mp4 would otherwise count as one video in the download archive and in file names.

    The same link always gives the same ID, so a download can be continued or recognised later. Click trackers and
    the part after "#" do not count; a signed link whose signature changed is, to this, another link."""
    page = urldefrag(strip_tracking(page_url)).url
    parts = [raw_id, page]
    try:
        media_path = urlparse(media_url).path
        page_path = urlparse(page).path
    except ValueError:
        media_path = page_path = ""
    # Two players on one page can point at files of the same name in different folders.
    if media_path and media_path != page_path:
        parts.append(media_path)
    digest = hashlib.sha1("\n".join(parts).encode("utf-8", errors="replace")).hexdigest()[:8]
    return f"{raw_id[:URL_KEYED_NAME_LENGTH]}-{digest}"


def media_extension_from_bytes(data: bytes) -> str:
    """The extension for a media file that starts with these bytes, or "" when they are no kind known here. For a
    direct link whose address has no extension and whose server calls the file plain data."""
    if data[4:8] == b"ftyp":
        brand = data[8:12]
        return "mov" if brand == b"qt  " else "m4a" if brand in (b"M4A ", b"M4B ") else "mp4"
    if data.startswith(b"\x1a\x45\xdf\xa3"):
        # Matroska and WebM start alike, and name themselves a few bytes in.
        return "webm" if b"webm" in data[:64] else "mkv"
    if data.startswith(b"OggS"):
        return "ogv" if b"theora" in data else "opus" if b"OpusHead" in data else "ogg"
    if data.startswith(b"RIFF"):
        return {b"AVI ": "avi", b"WAVE": "wav"}.get(data[8:12], "")
    if data.startswith(b"FLV"):
        return "flv"
    if data.startswith(b"fLaC"):
        return "flac"
    # An MP3 starts with its tags, or with a frame: eleven set bits, then the layer.
    if data.startswith(b"ID3") or (len(data) > 1 and data[0] == 0xFF and (data[1] & 0xE6) == 0xE2):
        return "mp3"
    if data.startswith(b"\x30\x26\xb2\x75\x8e\x66\xcf\x11"):
        return "wmv"
    if data.startswith(b"\x00\x00\x01\xba"):
        return "mpg"
    # A transport stream is a run of 188-byte packets, each starting with the same byte.
    if len(data) > TS_PACKET_BYTES and data[0] == TS_SYNC_BYTE and data[TS_PACKET_BYTES] == TS_SYNC_BYTE:
        return "ts"
    return ""

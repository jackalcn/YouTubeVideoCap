"""Which site a link belongs to. YouTube is what the app was built for; this machine's owner may also download from
the other sites yt-dlp knows, from direct links to a media file, and from ordinary pages that hold a video."""

from __future__ import annotations

import functools
from typing import Any
from urllib.parse import urlparse, urlunparse

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

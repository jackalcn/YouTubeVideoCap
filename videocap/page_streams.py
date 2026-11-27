"""Pages whose player keeps the address of its stream in a packed script.

yt-dlp finds the video in an ordinary page when the page says where it is: a <video> tag, a player's setup, an embed.
Some pages hide it. Their player script was packed (Dean Edwards' packer, eval(function(p,a,c,k,e,d){...})): the
script's words were swapped for short codes and listed apart, so ".m3u8" is written nowhere in the page, and the player
puts the address together when the page loads. Nothing in the page's text tells yt-dlp's generic extractor where the
stream is, and it says the page holds no video.

Packing only swaps words for codes. Undoing it is reading a table: no script of the page is run. The address found
that way is asked for as the page's own player would ask for it, with the page named as the Referer, which the sites
that hide their streams usually check.

Other pages keep their player in an <iframe> instead, often on another host, and sometimes in an <iframe> of that
one. yt-dlp follows an iframe only to a host it already has an extractor for, so one to any other site is left
exactly where the page is: holding no video. StreamSearch goes after it, one step at a time and only as far as the
step before failed: the embedded page's own address first, for yt-dlp to read as it reads any page, then the stream
that the embedded page's text gives away, then the pages that it embeds in turn. That smaller, dedicated document is
usually not written for a search engine to read the way the outer page may be, so it is read as plain text too, not
only for a packed script.

A page says what it likes, so what it leads to is not followed into this machine or its own network (see
StreamSearch.inside), and a search reads and tries no more than a fixed number of pages, for no longer than a fixed
time."""

from __future__ import annotations

import html
import re
import socket
import time
import urllib.request
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, replace
from typing import Any, Optional
from urllib.parse import urldefrag, urljoin, urlsplit

import yt_dlp
from yt_dlp.networking import Request
from yt_dlp.networking.exceptions import HTTPError
from yt_dlp.utils import extract_attributes

from videocap import impersonation
from videocap.errors import HUMAN_CHECK, UNREADABLE_LINK, add_overridden_referer_advice, failure_kind, humanize_human_check
from videocap.sites import host_of, is_private_address, is_private_host, is_youtube_url, link_kind, merge_http_headers


# How much of a page is read, in pieces of what size, how far past its start one packed script may reach, and how many
# of them are unpacked.
PAGE_LIMIT_BYTES = 4 * 1024 * 1024
READ_CHUNK_BYTES = 64 * 1024
PACKED_SCRIPT_LIMIT = 512 * 1024
PACKED_SCRIPTS_LIMIT = 20
# What one unpacked script may grow to. A table can be made to repeat one long word over and over.
UNPACKED_LIMIT = 4 * 1024 * 1024
# How many of the addresses that one document's scripts give away are tried, the likeliest first.
STREAM_TRIES = 3
# How many of one document's <iframe>s are followed, the likeliest first, out of how many that are looked at, and how
# many deep: the page's own, and those of the pages that they embed.
IFRAME_TRIES = 2
IFRAME_SCAN = 50
IFRAME_DEPTH = 2
# What one search may come to, whatever the numbers above are: how many embedded pages it reads, how many candidates
# it hands out to be tried, how long its own reading takes, and how long all of it takes with the trying.
SEARCH_PAGES = 6
SEARCH_TRIES = 8
SEARCH_SECONDS = 30.0
SEARCH_TOTAL_SECONDS = 90.0
# How often a page that refuses is asked for, and how long to wait before asking a check for bots again.
PAGE_ATTEMPTS = 2
CHECK_PAUSE_SECONDS = 3.0
TITLE_LIMIT = 150
# The digits of the packer's codes: 0-9 and a-z as toString(36) writes them, then the capitals.
DIGITS = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"

# The start of a packed script, in both spellings of its last parameter.
PACKER_START_RE = re.compile(r"eval\(function\(p,a,c,k,e,[dr]\)")
# The packer's call: }('code',base,count,'word|word|...'.split('|'),...
PACKED_CALL_RE = re.compile(r"\}\('(?P<code>.*?)',\s*(?P<base>\d+)\s*,\s*(?P<count>\d+)\s*,\s*'(?P<words>.*?)'\s*\.split\('\|'\)", re.S)
# A playlist's address in script text: in quotes, which may leave it relative, or written out whole.
PLAYLIST_RE = re.compile(
    r"""(?:(?P<quote>["'`])(?P<quoted>[^"'`\s<>]+?\.m3u8(?:\?[^"'`\s<>]*)?)(?P=quote)"""
    r"""|(?P<bare>https?://[^\s"'`<>\\]+?\.m3u8(?:\?[^\s"'`<>\\]*)?)(?![\w./%-]))""",
    re.I,
)
IFRAME_TAG_RE = re.compile(r"<iframe\b[^>]*>", re.I)
# Where an <iframe> keeps its address: in src, or, when a script is to fill that in as the page scrolls, in the
# attribute that the script reads it from.
IFRAME_SRC_ATTRIBUTES = ("src", "data-src", "data-lazy-src", "data-litespeed-src", "data-original")
# What a src holds until then, which is no address to follow.
PLACEHOLDER_SCHEMES = ("about:", "javascript:", "data:", "blob:")
# What tells a player's <iframe> from an advertisement's or a counter's: it may fill the screen, it may play, its
# address says what it is, and it is given the room of a player.
FULLSCREEN_ATTRIBUTES = ("allowfullscreen", "webkitallowfullscreen", "mozallowfullscreen")
PLAYER_PERMISSIONS = ("autoplay", "encrypted-media", "picture-in-picture")
PLAYER_WORD_RE = re.compile(r"embed|player|video", re.I)
PLAYER_WIDTH, PLAYER_HEIGHT = 200, 150
PIXELS_RE = re.compile(r"(\d+(?:\.\d+)?)(?:px)?")
HIDDEN_STYLE_RE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.I)
JS_ESCAPE_RE = re.compile(r"\\(?:u([0-9a-fA-F]{4})|x([0-9a-fA-F]{2})|(.))", re.S)
JS_PLAIN_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}
META_TAG_RE = re.compile(r"<meta\b[^>]*>", re.I)
TITLE_TAG_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
TITLE_PROPERTIES = ("og:title", "twitter:title")
IMAGE_PROPERTIES = ("og:image", "og:image:url", "og:image:secure_url", "twitter:image")


class UnpackedTooLarge(Exception):
    """A packed script that would unpack to more than UNPACKED_LIMIT."""


# What a candidate of a page's search is: the address of a stream that a script gave away; the address of a page that
# it embeds, for yt-dlp to read; or a link that is tried as one pasted by itself, as an embedded YouTube player's is.
STREAM = "stream"
FRAME = "frame"
PLAIN = "plain"

# What a person is told of where the video came from, in the report of a link and in the notes of a download alike.
PAGE_STREAM_DETAIL = "串流取自網頁的播放器腳本"
FRAMED_STREAM_DETAIL = "串流取自內嵌網頁（{frame}）的播放器腳本"
FRAME_DETAIL = "影片取自內嵌網頁（{frame}）"
PLAIN_DETAIL = "影片取自網頁內嵌的 YouTube 播放器"
# And of a search that found something it could not read, ahead of what the run that tried it said.
PAGE_STREAM_FOUND_BUT_FAILED = "這個網頁沒有直接列出影片，它的播放器腳本裡有串流網址，但無法讀取："
FRAMED_STREAM_FOUND_BUT_FAILED = "這個網頁沒有直接列出影片，它的內嵌網頁（{frame}）的播放器腳本裡有串流網址，但無法讀取："
FRAME_FOUND_BUT_FAILED = "這個網頁沒有直接列出影片，它的內嵌網頁（{frame}）無法讀取："
PLAIN_FOUND_BUT_FAILED = "這個網頁沒有直接列出影片，它內嵌的 YouTube 影片無法讀取："
# After a check for a person, which must come first in its message to be told for one.
CHECK_IS_ELSEWHERE = "被要求驗證的不是你貼的網頁，是它的播放器或內嵌網頁所在的網站（{host}）。"
TRIED_ALL = "已試過：{tried}，都沒有找到可以下載的影片。"
OUT_OF_TIME = "找影片花的時間超過上限，沒有再試其餘的。"
OUT_OF_TIME_AFTER = "找影片花的時間超過上限，沒有再試其餘的；已試過：{tried}。"


@dataclass(frozen=True)
class PageStream:
    """A candidate that a page's search hands out, with what the page says of its video. `page_url` is what the owner
    gave. `address` is what is tried. `referer` is the document that a browser would name as the Referer of the
    request for it: the one whose script gave a stream away, or the one that embeds a page; left "" for `page_url`
    itself. `frame` is the embedded page, however deep, that this was found in or that this is; "" for the page."""

    page_url: str
    address: str
    title: str = ""
    thumbnail: str = ""
    referer: str = ""
    kind: str = STREAM
    frame: str = ""

    def headers(self) -> dict[str, str]:
        """What a browser sends with every request for the address: the document that asked for it as the Referer,
        written as a browser writes it (the whole address to that document's own site, only the site to any other),
        and, to another site, the Origin the request comes from. That is for a player's request for its stream: a
        browser's own request for a page that another embeds names no Origin."""
        page, target = urlsplit(self.referer or self.page_url), urlsplit(self.address)
        origin = f"{page.scheme}://{page.netloc}"
        if (page.scheme, page.netloc.lower()) == (target.scheme, target.netloc.lower()):
            return {"Referer": urldefrag(self.referer or self.page_url).url}
        if self.kind == FRAME:
            return {"Referer": f"{origin}/"}
        return {"Referer": f"{origin}/", "Origin": origin}

    def source(self) -> str:
        """Where the video came from, in a sentence's worth."""
        if self.kind == PLAIN:
            return PLAIN_DETAIL
        if not self.frame:
            return PAGE_STREAM_DETAIL
        return (FRAME_DETAIL if self.kind == FRAME else FRAMED_STREAM_DETAIL).format(frame=host_of(self.frame))

    def name(self) -> str:
        """What this candidate is called in a list of what was tried."""
        if self.kind == PLAIN:
            return "內嵌的 YouTube 播放器"
        if self.kind == FRAME:
            return f"內嵌網頁 {host_of(self.frame)}"
        script = f"內嵌網頁 {host_of(self.frame)} 腳本" if self.frame else "網頁腳本"
        return f"{script}裡的串流（{host_of(self.address)}）"

    def found_but_failed(self) -> str:
        """What goes ahead of the words of the run that tried this and failed."""
        if self.kind == PLAIN:
            return PLAIN_FOUND_BUT_FAILED
        if not self.frame:
            return PAGE_STREAM_FOUND_BUT_FAILED
        return (FRAME_FOUND_BUT_FAILED if self.kind == FRAME else FRAMED_STREAM_FOUND_BUT_FAILED).format(frame=host_of(self.frame))


def holds_no_video(url: str, errors: Iterable[str]) -> bool:
    """Whether a link to an ordinary page failed in nothing but yt-dlp's word that it saw no video in the page, which a
    look at the page's own scripts may mend. A failure of any other kind says something else is wrong."""
    messages = list(errors)
    return bool(messages) and link_kind(url) == "page" and all(failure_kind(message) == UNREADABLE_LINK for message in messages)


def unescape_js(text: str) -> str:
    """The text of a JavaScript string literal, as the engine reads it: \\' and \\/ are the characters themselves."""

    def read(match: re.Match[str]) -> str:
        if match.group(1) or match.group(2):
            return chr(int(match.group(1) or match.group(2), 16))
        return JS_PLAIN_ESCAPES.get(match.group(3), match.group(3))

    return JS_ESCAPE_RE.sub(read, text)


def code_of(position: int, base: int) -> str:
    """What the packer calls the word at this position of its list: the number in this base."""
    digits = ""
    while True:
        position, rest = divmod(position, base)
        digits = DIGITS[rest] + digits
        if not position:
            return digits


def unpack(code: str, base: int, words: list[str]) -> str:
    """The script that this packed code and word list stand for. A word left empty is its own code."""
    table = {}
    for position, word in enumerate(words):
        name = code_of(position, base)
        table[name] = word or name
    size = 0

    def swap(match: re.Match[str]) -> str:
        nonlocal size
        text = table.get(match.group(0), match.group(0))
        size += len(text)
        if size > UNPACKED_LIMIT:
            raise UnpackedTooLarge
        return text

    return re.sub(r"\b\w+\b", swap, code, flags=re.ASCII)


def unpacked_scripts(page: str) -> Iterator[str]:
    """The text of each packed script in the page, unpacked: at most PACKED_SCRIPTS_LIMIT of them."""
    unpacked = 0
    for start in PACKER_START_RE.finditer(page):
        if unpacked >= PACKED_SCRIPTS_LIMIT:
            return
        call = PACKED_CALL_RE.search(page, start.end(), start.end() + PACKED_SCRIPT_LIMIT)
        if not call:
            continue
        base, count = int(call.group("base")), int(call.group("count"))
        if not 2 <= base <= len(DIGITS):
            continue
        words = unescape_js(call.group("words")).split("|")[:count]
        try:
            yield unpack(unescape_js(call.group("code")), base, words)
        except UnpackedTooLarge:
            continue
        unpacked += 1


def find_stream_addresses(page: str, page_url: str) -> list[str]:
    """The addresses of HLS playlists written in the page's packed scripts, whole, the likeliest first. A stream's own
    playlist, which lists its qualities, has a shorter path than the playlists of those qualities."""
    found: list[str] = []
    for script in unpacked_scripts(page):
        for match in PLAYLIST_RE.finditer(unescape_js(script)):
            address = urljoin(page_url, match.group("quoted") or match.group("bare"))
            if urlsplit(address).scheme in ("http", "https") and address != page_url and address not in found:
                found.append(address)
    return sorted(found, key=lambda address: urlsplit(address).path.count("/"))


def iframe_src(attributes: Mapping[str, Optional[str]]) -> str:
    """Where an <iframe> says its document is: its src, or, when that is empty or a placeholder, the attribute that a
    script is to copy into it later."""
    for name in IFRAME_SRC_ATTRIBUTES:
        value = (attributes.get(name) or "").strip()
        if value and value != "#" and not value.lower().startswith(PLACEHOLDER_SCHEMES):
            return value
    return ""


def declared_pixels(attributes: Mapping[str, Optional[str]], side: str) -> Optional[float]:
    """The width or the height that an <iframe> gives itself in pixels, by its attribute or in its own style. None
    when it gives none, or one that is no count of pixels, such as 100%: nothing is known of its size then."""
    whole = PIXELS_RE.fullmatch((attributes.get(side) or "").strip().lower())
    if whole:
        return float(whole.group(1))
    styled = re.search(rf"(?:^|;)\s*{side}\s*:\s*{PIXELS_RE.pattern}\s*(?:!important\s*)?(?:;|$)", attributes.get("style") or "", re.I)
    return float(styled.group(1)) if styled else None


def ranked_iframes(page: str, page_url: str) -> list[str]:
    """The addresses of this document's <iframe>s, resolved against it, the likeliest to hold a player first. A page's
    advertisements and counters are in iframes too, and often come before its player. One that gives itself no more
    than a pixel is left out; a hidden one comes last, as a player may be until its page is clicked; and of the rest,
    the one that may fill the screen or play, that is given a player's room, or whose address says what it is, comes
    before one that has none of that. What is alike stays in the page's order."""
    found: dict[str, tuple[bool, int, int]] = {}
    itself = urldefrag(page_url).url
    for position, tag in enumerate(IFRAME_TAG_RE.findall(page)[:IFRAME_SCAN]):
        attributes = extract_attributes(tag)
        src = iframe_src(attributes)
        address = urljoin(page_url, src) if src else ""
        parts = urlsplit(address)
        if parts.scheme not in ("http", "https") or not parts.netloc or urldefrag(address).url == itself or address in found:
            continue
        width, height = declared_pixels(attributes, "width"), declared_pixels(attributes, "height")
        if (width is not None and width <= 1) or (height is not None and height <= 1):
            continue
        allowed = (attributes.get("allow") or "").lower()
        score = 2 * (any(name in attributes for name in FULLSCREEN_ATTRIBUTES) or "fullscreen" in allowed)
        score += any(permission in allowed for permission in PLAYER_PERMISSIONS)
        score += width is not None and height is not None and width >= PLAYER_WIDTH and height >= PLAYER_HEIGHT
        score += bool(PLAYER_WORD_RE.search(parts.path))
        hidden = "hidden" in attributes or bool(HIDDEN_STYLE_RE.search(attributes.get("style") or ""))
        found[address] = (hidden, -score, position)
    return sorted(found, key=lambda address: found[address])


def find_addresses_in_iframe(page: str, page_url: str) -> list[str]:
    """The addresses an iframe's own document gives away, whole: in a packed script, as find_stream_addresses reads
    one, or written out in the open. Unlike the outer page, which a site may fill with addresses meant for a search
    engine rather than a player (see videocap.errors.refused_past_the_link), a dedicated player document of its own is
    safe to read as plain text too."""
    found = list(find_stream_addresses(page, page_url))
    for match in PLAYLIST_RE.finditer(html.unescape(page)):
        address = urljoin(page_url, match.group("quoted") or match.group("bare"))
        if urlsplit(address).scheme in ("http", "https") and address != page_url and address not in found:
            found.append(address)
    return sorted(found, key=lambda address: urlsplit(address).path.count("/"))


def tidy(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(text)).strip()[:TITLE_LIMIT]


def meta_content(page: str, properties: tuple[str, ...]) -> str:
    """The content of the first meta tag that names one of these properties, in the order of the properties."""
    tags = [extract_attributes(tag) for tag in META_TAG_RE.findall(page)]
    for wanted in properties:
        for attributes in tags:
            if (attributes.get("property") or attributes.get("name") or "").lower() == wanted and attributes.get("content"):
                return str(attributes["content"])
    return ""


def page_title(page: str) -> str:
    title = meta_content(page, TITLE_PROPERTIES)
    if not title:
        match = TITLE_TAG_RE.search(page)
        title = match.group(1) if match else ""
    return tidy(title)


def page_thumbnail(page: str, page_url: str) -> str:
    image = meta_content(page, IMAGE_PROPERTIES).strip()
    address = urljoin(page_url, image) if image else ""
    return address if urlsplit(address).scheme in ("http", "https") else ""


@dataclass(frozen=True)
class Reading:
    """What asking for a page came to: its text, and the address that it was really read from, which a redirect makes
    another than the one asked for; or how it was refused."""

    text: str = ""
    url: str = ""
    # Answered HTTP 403, and with a site's check for bots.
    forbidden: bool = False
    challenged: bool = False
    # Asked for as a browser asks. A check that turns even that away wants a person.
    as_browser: bool = False


def never() -> bool:
    return False


def read_within_limits(response: Any, stopped: Callable[[], bool]) -> Optional[bytes]:
    """The start of what a server sends, up to PAGE_LIMIT_BYTES, asked for a piece at a time so that a slow answer
    can be given up between two of them. None when it was."""
    pieces: list[bytes] = []
    size = 0
    while size < PAGE_LIMIT_BYTES:
        if stopped():
            return None
        piece = response.read(min(READ_CHUNK_BYTES, PAGE_LIMIT_BYTES - size))
        if not piece:
            break
        pieces.append(piece)
        size += len(piece)
    return b"".join(pieces)


def read_page_once(
    ydl_opts: Mapping[str, Any], url: str, other_sites: bool, headers: Optional[Mapping[str, str]] = None, stopped: Callable[[], bool] = never
) -> Reading:
    """One reading of the page. `headers` is what a browser would add to this one request: the Referer of the page
    that embeds this one. The owner's own headers are over them, as they are over everything the app sends: yt-dlp
    would put a request's headers over the run's, which are the owner's."""
    sent = merge_http_headers(headers, ydl_opts.get("http_headers") or {}) if headers else {}
    try:
        with impersonation.youtube_dl(yt_dlp.YoutubeDL, dict(ydl_opts), url, other_sites) as ydl:
            response = ydl.urlopen(Request(url, headers=sent))
            try:
                reached = str(response.url or url)
                kind = str(response.headers.get("Content-Type") or "").lower()
                if kind and not ("html" in kind or "xml" in kind or kind.startswith("text/")):
                    return Reading(url=reached)
                data = read_within_limits(response, stopped)
            finally:
                response.close()
    except HTTPError as error:
        return Reading(url=url, forbidden=error.status == impersonation.HTTP_FORBIDDEN, challenged=impersonation.is_challenge(error))
    except Exception:
        return Reading(url=url)
    if data is None:
        return Reading(url=reached)
    charset = re.search(r"charset=([\w-]+)", kind)
    try:
        return Reading(data.decode(charset.group(1) if charset else "utf-8", "replace"), reached)
    except LookupError:
        return Reading(data.decode("utf-8", "replace"), reached)


def read_page(
    ydl_opts: Mapping[str, Any], url: str, other_sites: bool, headers: Optional[Mapping[str, str]] = None, stopped: Callable[[], bool] = never
) -> Reading:
    """The page, read through yt-dlp so that the proxy, the cookies, the headers and the way of connecting are those of
    the run that found nothing in it. Without text when it cannot be read, or is not a page.

    A page that refuses (HTTP 403) is asked for once more. As a browser, when the first asking was not made as one
    and the app can pass for one: the site of an embedded page may want a browser where the page that embeds it did
    not. Otherwise only when the answer was a check for bots, and after a pause: such a check turns away some requests
    that it lets through a moment later. A second answer of the same kind is the site's own, and the page is left
    unread."""
    opts = dict(ydl_opts)
    can_pass_for_a_browser = impersonation.offered_to(url, other_sites)
    reading = Reading(url=url)
    for attempt in range(PAGE_ATTEMPTS):
        if stopped():
            break
        as_browser = can_pass_for_a_browser and "impersonate" in opts
        reading = replace(read_page_once(opts, url, other_sites, headers, stopped), as_browser=as_browser)
        if reading.text or not reading.forbidden or attempt + 1 == PAGE_ATTEMPTS:
            break
        if can_pass_for_a_browser and not as_browser:
            opts["impersonate"] = impersonation.any_browser()
        elif reading.challenged:
            time.sleep(CHECK_PAUSE_SECONDS)
        else:
            break
    return reading


def resolved_addresses(host: str) -> tuple[str, ...]:
    """The addresses that this machine finds for a name, or none when it finds nothing."""
    try:
        return tuple({str(found[4][0]) for found in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)})
    except (OSError, UnicodeError):
        return ()


def goes_through_a_proxy(ydl_opts: Mapping[str, Any]) -> bool:
    """Whether the run's requests leave through a proxy, which is then what looks a name up. yt-dlp takes its own
    option for one, "" meaning none at all, and the system's settings when it is given no option."""
    proxy = ydl_opts.get("proxy")
    return bool(proxy) if proxy is not None else bool(urllib.request.getproxies())


@dataclass(frozen=True)
class Tried:
    """A candidate that failed, with what the run that tried it said, where that run met a check for a person, and
    whatever the caller wants back with it."""

    stream: PageStream
    error: str
    verify_url: str = ""
    result: Any = None


@dataclass(frozen=True)
class Failure:
    """What a search that found nothing is told as: the message, where a check for a person is to be passed, and the
    failed try that the message is about, when it is about one."""

    error: str
    verify_url: str = ""
    tried: Optional[Tried] = None


class StreamSearch:
    """One search of the page at an address for its video, for the run that found nothing in the page.

    candidates() hands out one thing to try at a time, the likeliest first, and reads on only when it is asked for the
    next: the caller tries each, stops at the first that works, and tells failed() of each that did not. What the
    page's own packed scripts give away comes first, and is all there is when there is any. Otherwise each <iframe>
    of the page in turn: its own address, for yt-dlp to read; then, from its text, the streams that its scripts give
    away; then the same for the iframes it holds, IFRAME_DEPTH deep.

    Reading a link and downloading it search the same way and are told the same of a search that found nothing, by
    failure(), so that the one does not promise what the other does not keep."""

    def __init__(self, ydl_opts: Mapping[str, Any], url: str, other_sites: bool, should_stop: Optional[Callable[[], bool]] = None) -> None:
        self.ydl_opts = dict(ydl_opts)
        self.url = url
        self.other_sites = other_sites
        self.should_stop = should_stop or never
        self.title = self.thumbnail = ""
        # The Referer the owner typed, which is sent in place of any that the search works out.
        headers = self.ydl_opts.get("http_headers") or {}
        self.owner_referer = next((str(value).strip() for name, value in headers.items() if str(name).lower() == "referer"), "")
        self.proxied = goes_through_a_proxy(self.ydl_opts)
        self.started = time.monotonic()
        self.reading_seconds = 0.0
        self.out_of_time = False
        # How many embedded pages were read, and what was handed out, tried and failed, in that order.
        self.pages = 0
        self.handed: list[PageStream] = []
        self.tried: list[Tried] = []
        # Each check for a person that was met, by a reading of the search's own or by a try, in the order met.
        self.checks: list[Failure] = []
        # What was followed already, the page itself among it: pages that embed each other are not followed round.
        self._followed = {urldefrag(url).url}
        self._inside: dict[str, bool] = {}

    def over(self) -> bool:
        """Whether nothing more is to be started: the job was stopped, or the search has had its time, the trying of
        its candidates counted in."""
        if self.should_stop():
            return True
        if time.monotonic() - self.started >= SEARCH_TOTAL_SECONDS:
            self.out_of_time = True
            return True
        return False

    def may_read(self) -> bool:
        """Whether one more page may be read: the search's own reading has a time of its own, and what it has found
        by then may still be tried."""
        if self.over():
            return False
        if self.reading_seconds >= SEARCH_SECONDS:
            self.out_of_time = True
            return False
        return True

    def inside(self, url: str) -> bool:
        """Whether an address is in this machine or its own network: by how its host is written, or by what its name
        resolves to. The name is looked up only when no proxy stands between, since a proxy looks it up itself, and
        may find another answer. A lookup is no promise either: the request that follows asks again, and a name's
        owner can answer the second asking differently."""
        host = host_of(url)
        if host not in self._inside:
            self._inside[host] = is_private_host(host) or (not self.proxied and any(is_private_address(address) for address in resolved_addresses(host)))
        return self._inside[host]

    def leads_inside(self, address: str, document_url: str) -> bool:
        """Whether a document of the outside points at this machine or its own network, which is not followed. A
        document that is inside itself, as a page of the owner's own network is, may point where it likes."""
        return self.inside(address) and not self.inside(document_url)

    def read(self, url: str, document_url: str = "") -> Optional[Reading]:
        """The page of the search itself, or with `document_url` a page which that document embeds, asked for as a
        browser asks for an embedded page. None when it gives no text, or when a redirect took the reading inside."""
        headers = PageStream(document_url, url, kind=FRAME).headers() if document_url else None
        began = time.monotonic()

        def stopped() -> bool:
            # A page that is slow to come is given up when the time for reading runs out while it is read.
            if self.over():
                return True
            if self.reading_seconds + time.monotonic() - began >= SEARCH_SECONDS:
                self.out_of_time = True
            return self.out_of_time

        reading = read_page(self.ydl_opts, url, self.other_sites, headers, stopped)
        self.reading_seconds += time.monotonic() - began
        if reading.challenged and reading.as_browser:
            self.checks.append(Failure(humanize_human_check("", url, host_of(url) if document_url else ""), url))
        if not reading.text or self.leads_inside(reading.url, document_url or self.url):
            return None
        return reading

    def read_frame(self, frame_url: str, document_url: str) -> Optional[Reading]:
        """An embedded page's text, when the search may still read one, and its site has not asked for a person yet:
        asking again would only meet the same check."""
        checked = {host_of(check.verify_url) for check in self.checks}
        if not self.may_read() or self.pages >= SEARCH_PAGES or host_of(frame_url) in checked:
            return None
        self.pages += 1
        return self.read(frame_url, document_url)

    def may_hand(self) -> bool:
        return not self.over() and len(self.handed) < SEARCH_TRIES

    def handing(self, stream: PageStream) -> PageStream:
        self.handed.append(stream)
        return stream

    def candidates(self) -> Iterator[PageStream]:
        """What to try, one at a time. Nothing is read for a candidate before the one ahead of it was tried."""
        page = self.read(self.url)
        if page is None:
            return
        self.title, self.thumbnail = page_title(page.text), page_thumbnail(page.text, self.url)
        addresses = [address for address in find_stream_addresses(page.text, self.url) if not self.leads_inside(address, self.url)]
        for address in addresses[:STREAM_TRIES]:
            if not self.may_hand():
                return
            yield self.handing(PageStream(self.url, address, self.title, self.thumbnail))
        if not addresses:
            yield from self.frames(page.text, self.url, IFRAME_DEPTH)

    def frames(self, text: str, document_url: str, depth: int) -> Iterator[PageStream]:
        """The candidates that the <iframe>s of one document lead to, `depth` deep."""
        followed = 0
        for frame_url in ranked_iframes(text, document_url):
            if followed == IFRAME_TRIES or not self.may_hand():
                return
            if urldefrag(frame_url).url in self._followed or self.leads_inside(frame_url, document_url):
                continue
            self._followed.add(urldefrag(frame_url).url)
            followed += 1
            if is_youtube_url(frame_url):
                # YouTube's own, to be asked exactly as a link to it is: none of its text is read here.
                yield self.handing(PageStream(self.url, frame_url, kind=PLAIN, frame=frame_url))
                continue
            yield self.handing(PageStream(self.url, frame_url, self.title, self.thumbnail, document_url, FRAME, frame_url))
            framed = self.read_frame(frame_url, document_url)
            if framed is None:
                continue
            found = [address for address in find_addresses_in_iframe(framed.text, framed.url) if not self.leads_inside(address, framed.url)]
            for address in found[:STREAM_TRIES]:
                if not self.may_hand():
                    return
                yield self.handing(PageStream(self.url, address, self.title, self.thumbnail, framed.url, STREAM, framed.url))
            if depth > 1:
                yield from self.frames(framed.text, framed.url, depth - 1)

    def failed(self, stream: PageStream, error: str, verify_url: str = "", result: Any = None) -> None:
        """Told of each candidate that did not work, with what its run said."""
        tried = Tried(stream, error, verify_url, result)
        self.tried.append(tried)
        if failure_kind(error) == HUMAN_CHECK:
            self.checks.append(Failure(error, verify_url or stream.address, tried))

    def failure(self, page_error: str) -> Failure:
        """What a search that found nothing is told as, given what the page's own run said. A check for a person comes
        before anything else: it is what the person can do something about, and a later candidate that merely held
        no video says nothing against it. Then a stream that was found and could not be read, then an embedded page
        that refused. When every candidate simply held nothing, the page's own words stand, with what was tried."""
        tried_names = "、".join(dict.fromkeys(stream.name() for stream in self.handed))
        if self.checks:
            chosen = self.checks[0]
            host = host_of(chosen.verify_url)
            if host != host_of(self.url):
                chosen = replace(chosen, error=f"{chosen.error}\n\n{CHECK_IS_ELSEWHERE.format(host=host)}")
        else:
            streams = [tried for tried in self.tried if tried.stream.kind == STREAM]
            refused = [tried for tried in self.tried if tried.stream.kind != STREAM and failure_kind(tried.error) != UNREADABLE_LINK]
            about = next(iter([*streams, *refused]), None)
            if about:
                chosen = Failure(f"{about.stream.found_but_failed()}{about.error}", about.verify_url, about)
            else:
                chosen = Failure(f"{page_error}\n\n{TRIED_ALL.format(tried=tried_names)}" if tried_names and not self.out_of_time else page_error)
        error = chosen.error
        if chosen.tried and chosen.tried.stream.kind != PLAIN:
            error = add_overridden_referer_advice(error, self.owner_referer, chosen.tried.stream.headers()["Referer"])
        if self.out_of_time:
            error = f"{error}\n\n{OUT_OF_TIME_AFTER.format(tried=tried_names) if tried_names else OUT_OF_TIME}"
        return replace(chosen, error=error)

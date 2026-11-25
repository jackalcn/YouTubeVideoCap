"""Pages whose player keeps the address of its stream in a packed script.

yt-dlp finds the video in an ordinary page when the page says where it is: a <video> tag, a player's setup, an embed.
Some pages hide it. Their player script was packed (Dean Edwards' packer, eval(function(p,a,c,k,e,d){...})): the
script's words were swapped for short codes and listed apart, so ".m3u8" is written nowhere in the page, and the player
puts the address together when the page loads. Nothing in the page's text tells yt-dlp's generic extractor where the
stream is, and it says the page holds no video.

Packing only swaps words for codes. Undoing it is reading a table: no script of the page is run. The address found
that way is asked for as the page's own player would ask for it, with the page named as the Referer, which the sites
that hide their streams usually check.

Other pages keep their player in an <iframe> of its own instead, often on the site's own other host, rather than in
the page itself. yt-dlp follows an iframe only to a host it already has an extractor for, so one to any other site is
left exactly where the page is: holding no video. That smaller, dedicated document is usually not written for a
search engine to read the way the outer page may be, so once opened it is read as plain text too, not only for a
packed script."""

from __future__ import annotations

import html
import re
import time
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urldefrag, urljoin, urlsplit

import yt_dlp
from yt_dlp.networking import Request
from yt_dlp.networking.exceptions import HTTPError
from yt_dlp.utils import extract_attributes

from videocap import impersonation
from videocap.errors import UNREADABLE_LINK, failure_kind
from videocap.sites import link_kind


# How much of a page is read, how far past its start one packed script may reach, and how many of them are unpacked.
PAGE_LIMIT_BYTES = 4 * 1024 * 1024
PACKED_SCRIPT_LIMIT = 512 * 1024
PACKED_SCRIPTS_LIMIT = 20
# What one unpacked script may grow to. A table can be made to repeat one long word over and over.
UNPACKED_LIMIT = 4 * 1024 * 1024
# How many of a page's addresses are tried, the likeliest first.
STREAM_TRIES = 3
# How many of a page's own <iframe>s are opened, when neither the page nor a packed script of its own gives a stream away.
IFRAME_TRIES = 2
# How often a page that answers with a check for bots is asked for, and how long to wait before asking again.
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
# An <iframe>'s own address, however the page quotes it.
IFRAME_RE = re.compile(r"""<iframe\b[^>]*\bsrc=(["'])(?P<src>[^"'<>]+)\1""", re.I)
JS_ESCAPE_RE = re.compile(r"\\(?:u([0-9a-fA-F]{4})|x([0-9a-fA-F]{2})|(.))", re.S)
JS_PLAIN_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}
META_TAG_RE = re.compile(r"<meta\b[^>]*>", re.I)
TITLE_TAG_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
TITLE_PROPERTIES = ("og:title", "twitter:title")
IMAGE_PROPERTIES = ("og:image", "og:image:url", "og:image:secure_url", "twitter:image")


class UnpackedTooLarge(Exception):
    """A packed script that would unpack to more than UNPACKED_LIMIT."""


@dataclass(frozen=True)
class PageStream:
    """A stream that a page's player script gave away, with what the page says of it. `page_url` is what the owner
    gave: the address the stream is downloaded and remembered under, whatever document its player script was really
    read from. `referer` is that document, when it was not `page_url` itself but an iframe of the page's own: what a
    browser would name as the Referer of a request the iframe's own player makes. Left "" for `page_url` itself."""

    page_url: str
    address: str
    title: str = ""
    thumbnail: str = ""
    referer: str = ""

    def headers(self) -> dict[str, str]:
        """What the page's own player sends with every request for the stream: the document that asked for it (the
        page itself, or the iframe of its own that really did) as the Referer, written as a browser writes it (the
        whole address to that document's own site, only the site to any other), and, to another site, the Origin the
        request comes from."""
        page, target = urlsplit(self.referer or self.page_url), urlsplit(self.address)
        origin = f"{page.scheme}://{page.netloc}"
        if (page.scheme, page.netloc.lower()) == (target.scheme, target.netloc.lower()):
            return {"Referer": urldefrag(self.referer or self.page_url).url}
        return {"Referer": f"{origin}/", "Origin": origin}


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


def iframe_addresses(page: str, page_url: str) -> list[str]:
    """The addresses of this page's own <iframe>s, resolved against it, at most IFRAME_TRIES of them. yt-dlp's generic
    extractor follows an iframe only to a host it already has an extractor of its own for; a site that keeps its
    player in one instead of the page itself, to a host yt-dlp does not know, is left for find_streams to open."""
    found: list[str] = []
    for match in IFRAME_RE.finditer(page):
        address = urljoin(page_url, html.unescape(match.group("src")))
        if urlsplit(address).scheme in ("http", "https") and address not in found:
            found.append(address)
    return found[:IFRAME_TRIES]


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


def read_page_once(ydl_opts: Mapping[str, Any], url: str, other_sites: bool, headers: Optional[Mapping[str, str]] = None) -> tuple[str, bool]:
    """One reading of the page: its text, and whether it was turned away by a site's check for bots. `headers` is what a
    browser would add to this one request, such as the Referer of the page that pointed here (see iframe_addresses)."""
    try:
        with impersonation.youtube_dl(yt_dlp.YoutubeDL, dict(ydl_opts), url, other_sites) as ydl:
            response = ydl.urlopen(Request(url, headers=dict(headers or {})))
            try:
                kind = str(response.headers.get("Content-Type") or "").lower()
                if kind and not ("html" in kind or "xml" in kind or kind.startswith("text/")):
                    return "", False
                data = response.read(PAGE_LIMIT_BYTES)
            finally:
                response.close()
    except HTTPError as error:
        return "", impersonation.is_challenge(error)
    except Exception:
        return "", False
    charset = re.search(r"charset=([\w-]+)", kind)
    try:
        return data.decode(charset.group(1) if charset else "utf-8", "replace"), False
    except LookupError:
        return data.decode("utf-8", "replace"), False


def read_page(ydl_opts: Mapping[str, Any], url: str, other_sites: bool, headers: Optional[Mapping[str, str]] = None) -> str:
    """The page's text, read through yt-dlp so that the proxy, the cookies, the headers and the way of connecting are
    those of the run that found nothing in it. "" when it cannot be read, or is not a page.

    A site's check for bots turns away some requests that it lets through a moment later, as one that passed for a
    browser a second ago. So a page that answers with the check is asked for once more, after a pause; a second
    answer of the same kind is the site's own, and the page is left unread."""
    for attempt in range(PAGE_ATTEMPTS):
        page, challenged = read_page_once(ydl_opts, url, other_sites, headers)
        if page or not challenged:
            return page
        if attempt + 1 < PAGE_ATTEMPTS:
            time.sleep(CHECK_PAUSE_SECONDS)
    return ""


def find_streams(ydl_opts: Mapping[str, Any], url: str, other_sites: bool) -> list[PageStream]:
    """The streams that the page at this address gives away, the likeliest first: at most STREAM_TRIES of them. A page
    that gives none away itself, but keeps its player in an iframe of its own, is asked for only to open that: the
    stream still downloads and is remembered in the outer page's name, but is asked for as the iframe's own player
    would ask for it."""
    page = read_page(ydl_opts, url, other_sites)
    if not page:
        return []
    title, thumbnail = page_title(page), page_thumbnail(page, url)
    addresses, referer = find_stream_addresses(page, url), ""
    if not addresses:
        for frame_url in iframe_addresses(page, url):
            framed = read_page(ydl_opts, frame_url, other_sites, PageStream(url, frame_url).headers())
            nested = find_addresses_in_iframe(framed, frame_url) if framed else []
            if nested:
                addresses, referer = nested, frame_url
                break
    return [PageStream(url, address, title, thumbnail, referer) for address in addresses[:STREAM_TRIES]]

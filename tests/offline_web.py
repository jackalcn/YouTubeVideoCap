"""Small web sites on this machine, for testing links to sites other than YouTube with the real yt-dlp and ffmpeg and
no outside network: a direct media file, a page with a <video> tag, an HLS stream, addresses that refuse, and
addresses that let only a browser in."""

from __future__ import annotations

import functools
import http.server
import json
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Callable, Iterator
from urllib.parse import parse_qs, urlsplit

import pytest

from conftest import has_ffmpeg


# Two clips that differ in picture, sound and size, so a test can tell which site's file it has.
CLIP_VARIANTS = {
    "a": ("testsrc=size=320x240:rate=10", "sine=frequency=440", "2"),
    "b": ("testsrc2=size=480x270:rate=10", "sine=frequency=880", "3"),
}
REFUSED_PREFIX = "/forbidden/"
# Chrome sends this header with every request, and so does curl-cffi when yt-dlp passes for Chrome with it. yt-dlp's
# ordinary connections do not, which is how the addresses below tell a browser from a program. A real site tells them
# apart by the connection itself, which a test has no way to fake.
BROWSER_HEADER = "sec-ch-ua"
# Under this only a browser gets in. Anything else is answered as Cloudflare's check for bots answers it.
CHECKED_PREFIX = "/cf/"
# Under this only a browser gets in too, and anything else gets a bare HTTP 403 that names no reason, as a stream's
# playlist and segments do when the site's page itself is open.
BROWSERS_ONLY_PREFIX = "/browsers-only/"
# Under this no one gets in, browser or not: the check wants a person.
PERSON_ONLY_PREFIX = "/captcha/"
# Under this the check for bots turns away the first request for each address and lets the next one through, as one
# that passes only some of what it is sent does.
FLAKY_PREFIX = "/flaky/"
# Anything in a folder of this name opens only to a request that carries the cookie a passed check leaves and the User-Agent
# of the browser that passed it, as a site's check does; any other gets the check itself. See require_clearance.
VERIFIED_FOLDER = "/verified/"
CLEARANCE_COOKIE = "cf_clearance"
# Anything in a folder of this name opens only to a request that names a page of one chosen site as its Referer, as
# the stream of a page whose player checks that does, and anything else gets a bare HTTP 403. See refer_only_from.
REFERRED_FOLDER = "/referred/"
# Anything in a folder of this name is served only to a request whose address has, after its "?", a token that the
# site handed out and that is not used up, and anything else gets a bare HTTP 403: as a stream whose address a site
# signs for a short while. See add_token_page.
TOKEN_FOLDER = "/tokened/"
TOKEN_PARAMETER = "token"
CHECK_PAGE = b"<!doctype html><html><head><title>Just a moment...</title></head><body>Checking your browser</body></html>"
# The letters and digits a packer writes its codes with, which a test writes them with by its own hand.
PACKER_DIGITS = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"


def packer_code(position: int, base: int) -> str:
    return PACKER_DIGITS[position] if position < base else packer_code(position // base, base) + PACKER_DIGITS[position % base]


def pack(code: str, base: int = 36) -> str:
    """The code as Dean Edwards' packer writes it, which the players of pages that hide a stream's address often are:
    its words swapped for short codes and listed apart, in a script that puts them back and runs the result."""
    words = list(dict.fromkeys(re.findall(r"\w+", code)))
    codes = {word: packer_code(position, base) for position, word in enumerate(words)}
    swapped = re.sub(r"\b(\w+)\b", lambda match: codes[match.group(1)], code, flags=re.ASCII)
    payload = swapped.replace("\\", "\\\\").replace("'", "\\'")
    unpacker = "function(p,a,c,k,e,d){while(c--)if(k[c])p=p.replace(new RegExp('\\\\b'+c.toString(a)+'\\\\b','g'),k[c]);return p}"
    return f"eval({unpacker}('{payload}',{base},{len(words)},'{'|'.join(words)}'.split('|'),0,{{}}))"


class SiteHandler(http.server.SimpleHTTPRequestHandler):
    """Serves a folder, remembers what was asked for, and refuses under the prefixes above."""

    def __init__(self, *args: object, site: LocalSite, **kwargs: object) -> None:
        self.site = site
        super().__init__(*args, directory=str(site.root), **kwargs)  # type: ignore[arg-type]

    def log_message(self, format: str, *args: object) -> None:
        pass

    def answer(self, serve: Callable[[], None]) -> None:
        self.site.requests.append(self.path)
        self.site.referers.append((self.path, self.headers.get("Referer", ""), self.headers.get("Origin", "")))
        as_browser = self.headers.get(BROWSER_HEADER) is not None
        if as_browser:
            self.site.browser_requests.append(self.path)
        self.site.seen.append((self.path, {name.lower(): value for name, value in self.headers.items()}))
        unreferred = REFERRED_FOLDER in self.path and not self.site.accepts_referer(self.headers.get("Referer", ""))
        unverified = VERIFIED_FOLDER in self.path and not self.site.accepts_clearance(self.headers.get("Cookie", ""), self.headers.get("User-Agent", ""))
        moved_to = self.site.moved.get(self.path.split("?")[0])
        token_page = self.site.token_pages.get(self.path.split("?")[0])
        if moved_to:
            self.send_response(302, "Found")
            self.send_header("Location", moved_to)
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path.startswith(REFUSED_PREFIX) or (self.path.startswith(BROWSERS_ONLY_PREFIX) and not as_browser) or unreferred:
            self.send_error(403, "Forbidden")
        elif token_page is not None:
            self.send_page(self.site.next_text(self.path.split("?")[0]).replace("{token}", self.site.hand_out_token()))
        elif TOKEN_FOLDER in self.path and not self.site.accepts_token(self.path):
            self.send_error(403, "Forbidden")
        elif self.path.startswith(PERSON_ONLY_PREFIX) or unverified or (self.path.startswith(CHECKED_PREFIX) and not as_browser) or (
            self.path.startswith(FLAKY_PREFIX) and self.site.turns_away_first(self.path)
        ):
            self.send_check()
        else:
            serve()

    def send_page(self, text: str) -> None:
        """A page that is no file of the site's folder, made for this one request."""
        body = text.encode("utf-8")
        self.send_response(200, "OK")
        self.send_header("Content-Type", "text/html; charset=UTF-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_check(self) -> None:
        """Cloudflare's answer to what it takes for a bot: HTTP 403, this header, and a page that checks the browser."""
        self.send_response(403, "Forbidden")
        self.send_header("cf-mitigated", "challenge")
        self.send_header("Content-Type", "text/html; charset=UTF-8")
        self.send_header("Content-Length", str(len(CHECK_PAGE)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(CHECK_PAGE)

    def do_GET(self) -> None:
        self.answer(super().do_GET)

    def do_HEAD(self) -> None:
        self.answer(super().do_HEAD)


class LocalSite:
    """One site: a folder served over HTTP on a port of its own. Two of them are two different sites to yt-dlp."""

    def __init__(self, root: Path, media: WebMedia | dict[str, Path]) -> None:
        self.root = root
        self.media = media
        self.requests: list[str] = []
        # Those of them that came as from a browser.
        self.browser_requests: list[str] = []
        # Each request's path with the Referer and the Origin it came with, "" for one it came without.
        self.referers: list[tuple[str, str, str]] = []
        self.referer_origin = ""
        # Every request's path with all its headers, names in lower case.
        self.seen: list[tuple[str, dict[str, str]]] = []
        self.clearance_value = ""
        self.clearance_agent = ""
        # The addresses under FLAKY_PREFIX that have been asked for once.
        self.asked_once: set[str] = set()
        # The paths that answer with a redirect, each to its new address.
        self.moved: dict[str, str] = {}
        # The pages made anew at each asking, each with the texts it gives in turn and how often it was asked for;
        # how many fragments each token handed out is good for, in turn; and what is left of each.
        self.token_pages: dict[str, tuple[str, ...]] = {}
        self.token_page_askings: dict[str, int] = {}
        self.token_uses: tuple[int, ...] = (0,)
        self.tokens: dict[str, int] = {}
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(SiteHandler, site=self))
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def url(self, path: str) -> str:
        return f"{self.base}/{path.lstrip('/')}"

    def add_clip(self, name: str = "clip.mp4", variant: str = "a") -> str:
        """A direct link to a media file."""
        target = self.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.media[variant], target)
        return self.url(name)

    def add_rewrapped(self, name: str, variant: str = "a") -> str:
        """A clip in the container its name says, such as a .mkv: the streams are copied, so no encoder is needed."""
        target = self.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(self.media[variant]), "-c", "copy", str(target)], check=True)
        return self.url(name)

    def add_page(self, name: str = "page.html", sources: tuple[str, ...] = ("clip.mp4",), title: str = "Local page") -> str:
        """An ordinary web page that plays the given files, which must be added too."""
        return self.write_page(name, title, "".join(f'<video src="{source}" controls></video>' for source in sources))

    def add_choice_page(self, name: str, sources: tuple[tuple[str, str, int, int], ...], title: str = "Local page") -> str:
        """A page whose one player offers a video as several files, each a (file, MIME type, width, height)."""
        offered = "".join(f'<source src="{source}" type="{kind}" width="{width}" height="{height}">' for source, kind, width, height in sources)
        return self.write_page(name, title, f"<video controls>{offered}</video>")

    def write_page(self, name: str, title: str, players: str, head: str = "") -> str:
        html = f"<!doctype html><html><head><title>{title}</title>{head}</head><body><h1>{title}</h1>{players}</body></html>"
        target = self.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(html, encoding="utf-8")
        return self.url(name)

    def add_listing_page(self, name: str, stream: str, title: str = "Local page") -> str:
        """A page that names its video's stream only in what it tells search engines (a schema.org VideoObject), which
        yt-dlp's generic extractor reads."""
        data = {"@context": "https://schema.org", "@type": "VideoObject", "name": title, "contentUrl": stream}
        return self.write_page(name, title, "<p>player</p>", f'<script type="application/ld+json">{json.dumps(data)}</script>')

    def add_packed_page(self, name: str, code: str, title: str = "Local page", image: str = "", base: int = 36) -> str:
        """A page whose only player is a packed script: nothing in the page's text says where its stream is."""
        head = f'<meta property="og:image" content="{image}">' if image else ""
        return self.write_page(name, title, f"<script>{pack(code, base)}</script>", head)

    def add_iframe_page(self, name: str, frame_src: str, title: str = "Local page") -> str:
        """A page whose only player is in an <iframe> of its own: nothing in the outer page's text says where its
        stream is either."""
        return self.add_framing_page(name, f'src="{frame_src}"', title=title)

    def add_framing_page(self, name: str, *iframes: str, title: str = "Local page") -> str:
        """A page of nothing but <iframe>s, each given as the attributes of its tag, in this order."""
        return self.write_page(name, title, "".join(f"<iframe {attributes}></iframe>" for attributes in iframes))

    def add_token_page(self, name: str, *texts: str, uses: tuple[int, ...] = (1,)) -> str:
        """A page made anew each time it is asked for, with a new token wherever its text says {token}. Given
        several texts, each asking gets the next, and the last from then on. Each token is good for as many fragments
        (.ts) of what is in a folder named `tokened` as `uses` says, the first token for the first number and the
        last number from then on; a playlist there is served for as long as its token has a fragment left."""
        self.token_pages[f"/{name.lstrip('/')}"] = texts
        self.token_uses = uses
        return self.url(name)

    def next_text(self, path: str) -> str:
        asked = self.token_page_askings.get(path, 0)
        self.token_page_askings[path] = asked + 1
        texts = self.token_pages[path]
        return texts[min(asked, len(texts) - 1)]

    def hand_out_token(self) -> str:
        token = f"t{len(self.tokens) + 1}"
        self.tokens[token] = self.token_uses[min(len(self.tokens), len(self.token_uses) - 1)]
        return token

    def accepts_token(self, path: str) -> bool:
        """Whether a request has a token that still serves. A fragment uses one of the token's uses up."""
        parts = urlsplit(path)
        token = parse_qs(parts.query).get(TOKEN_PARAMETER, [""])[0]
        left = self.tokens.get(token, 0)
        if left <= 0:
            return False
        if parts.path.endswith(".ts"):
            self.tokens[token] = left - 1
        return True

    def add_long_stream(self, folder: str = "hls", fragments: int = 6) -> str:
        """An HLS stream of this many fragments of a second each: a clip's own stream has too few to stop between."""
        if not has_ffmpeg:
            pytest.skip("ffmpeg is not installed")
        target = self.root / folder
        target.mkdir(parents=True, exist_ok=True)
        sources = ["-f", "lavfi", "-i", "testsrc=size=160x120:rate=10", "-f", "lavfi", "-i", "sine=frequency=440", "-t", str(fragments)]
        # A picture to cut at each second, or the whole stream would be one fragment.
        codecs = ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "10", "-keyint_min", "10", "-sc_threshold", "0", "-c:a", "aac"]
        output = ["-f", "hls", "-hls_time", "1", "-hls_playlist_type", "vod", str(target / "index.m3u8")]
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *sources, *codecs, *output], check=True)
        return self.url(f"{folder}/index.m3u8")

    def add_redirect(self, name: str, target: str) -> str:
        """An address that sends whoever asks for it on to another."""
        self.moved[f"/{name.lstrip('/')}"] = target
        return self.url(name)

    def add_embedded_player(self, name: str, stream: str, title: str = "Embedded player") -> str:
        """A small, dedicated player document, as an <iframe> often holds one of its own: writes its stream's address
        out in the open, not packed, and not meant for a search engine to read."""
        return self.write_page(name, title, f"<p>player</p><script>var source='{stream}';</script>")

    def add_playlist(self, name: str = "hls/index.m3u8", segments: int = 3) -> str:
        """A stream's playlist, written out: enough to be read and listed, with no segment behind it."""
        lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:2", "#EXT-X-MEDIA-SEQUENCE:0", "#EXT-X-PLAYLIST-TYPE:VOD"]
        for number in range(segments):
            lines += ["#EXTINF:2.0,", f"segment{number}.ts"]
        target = self.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join([*lines, "#EXT-X-ENDLIST"]) + "\n", encoding="utf-8")
        return self.url(name)

    def refer_only_from(self, page_site: LocalSite) -> None:
        """Makes what is in a folder named `referred` open only to requests that name a page of that site as their Referer."""
        self.referer_origin = page_site.base

    def require_clearance(self, value: str = "passed", agent: str = "") -> None:
        """Makes what is in a folder named `verified` open only to a request with this clearance cookie, and with this
        User-Agent when one is given."""
        self.clearance_value, self.clearance_agent = value, agent

    def accepts_clearance(self, cookie_header: str, agent: str) -> bool:
        cookies = [part.strip() for part in cookie_header.split(";")]
        return bool(self.clearance_value) and f"{CLEARANCE_COOKIE}={self.clearance_value}" in cookies and (not self.clearance_agent or agent == self.clearance_agent)

    def headers_for(self, path: str) -> list[dict[str, str]]:
        """All the headers of each request that named this path."""
        return [headers for requested, headers in self.seen if requested.split("?")[0] == f"/{path.lstrip('/')}"]

    def turns_away_first(self, path: str) -> bool:
        """Whether this is the first request for the address, which the flaky check turns away."""
        first = path not in self.asked_once
        self.asked_once.add(path)
        return first

    def accepts_referer(self, referer: str) -> bool:
        parts = urlsplit(referer)
        return bool(self.referer_origin) and f"{parts.scheme}://{parts.netloc}" == self.referer_origin

    def referers_for(self, path: str) -> list[tuple[str, str]]:
        """The Referer and the Origin of each request that named this path."""
        return [(referer, origin) for requested, referer, origin in self.referers if requested.split("?")[0] == f"/{path.lstrip('/')}"]

    def add_stream(self, folder: str = "hls", variant: str = "a", segments_in: str = "") -> str:
        """An HLS stream: a playlist and its segments. `segments_in` puts the segments in a folder of their own, so
        they can be refused while the playlist is not."""
        target = self.root / folder
        target.mkdir(parents=True, exist_ok=True)
        command = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(self.media[variant]), "-c", "copy", "-f", "hls"]
        subprocess.run([*command, "-hls_time", "1", "-hls_playlist_type", "vod", str(target / "index.m3u8")], check=True)
        if segments_in:
            moved = self.root / segments_in
            moved.mkdir(parents=True, exist_ok=True)
            for segment in target.glob("*.ts"):
                shutil.move(str(segment), str(moved / segment.name))
            playlist = (target / "index.m3u8").read_text(encoding="utf-8")
            lines = [f"/{segments_in.strip('/')}/{line}" if line.endswith(".ts") else line for line in playlist.splitlines()]
            (target / "index.m3u8").write_bytes(("\n".join(lines) + "\n").encode("utf-8"))
        return self.url(f"{folder}/index.m3u8")

    def segments(self, folder: str = "hls") -> list[str]:
        """The paths of a stream's segments, as they are asked for."""
        return sorted(f"/{path.relative_to(self.root).as_posix()}" for path in (self.root / folder).glob("*.ts"))

    def asked_for(self, path: str) -> int:
        """How many requests named this path."""
        return sum(1 for request in self.requests if request.split("?")[0] == f"/{path.lstrip('/')}")

    def asked_as_a_browser(self, path: str) -> int:
        """How many of them came as from a browser."""
        return sum(1 for request in self.browser_requests if request.split("?")[0] == f"/{path.lstrip('/')}")

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def make_clip(target: Path, variant: str) -> Path:
    picture, sound, seconds = CLIP_VARIANTS[variant]
    command = ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", picture, "-f", "lavfi", "-i", sound, "-t", seconds]
    subprocess.run([*command, "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart", str(target)], check=True)
    return target


class WebMedia:
    """The clips every site hands out, each made the first time a site hands it out. A site of pages and playlists
    only, as most tests of a page's own scripts are, then needs no ffmpeg, which macOS Smoke runs the tests without."""

    def __init__(self, folder: Callable[[], Path]) -> None:
        self.folder = folder
        self.clips: dict[str, Path] = {}

    def __getitem__(self, variant: str) -> Path:
        if variant not in self.clips:
            if not has_ffmpeg:
                pytest.skip("ffmpeg is not installed")
            self.clips[variant] = make_clip(self.folder() / f"{variant}.mp4", variant)
        return self.clips[variant]


@pytest.fixture(name="web_media", scope="session")
def web_media_fixture(tmp_path_factory: pytest.TempPathFactory) -> WebMedia:
    """The clips every site hands out, each made once."""
    return WebMedia(functools.cache(lambda: tmp_path_factory.mktemp("web-media")))


@pytest.fixture(name="new_site")
def new_site_fixture(tmp_path_factory: pytest.TempPathFactory, web_media: WebMedia) -> Iterator[Callable[[], LocalSite]]:
    """Makes a site each time it is called, and closes them all afterwards."""
    sites: list[LocalSite] = []

    def make() -> LocalSite:
        site = LocalSite(tmp_path_factory.mktemp("web-site"), web_media)
        sites.append(site)
        return site

    yield make
    for site in sites:
        site.close()

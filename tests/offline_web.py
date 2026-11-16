"""Small web sites on this machine, for testing links to sites other than YouTube with the real yt-dlp and ffmpeg and
no outside network: a direct media file, a page with a <video> tag, an HLS stream, addresses that refuse, and
addresses that let only a browser in."""

from __future__ import annotations

import functools
import http.server
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Callable, Iterator

import pytest


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
CHECK_PAGE = b"<!doctype html><html><head><title>Just a moment...</title></head><body>Checking your browser</body></html>"


class SiteHandler(http.server.SimpleHTTPRequestHandler):
    """Serves a folder, remembers what was asked for, and refuses under the prefixes above."""

    def __init__(self, *args: object, site: LocalSite, **kwargs: object) -> None:
        self.site = site
        super().__init__(*args, directory=str(site.root), **kwargs)  # type: ignore[arg-type]

    def log_message(self, format: str, *args: object) -> None:
        pass

    def answer(self, serve: Callable[[], None]) -> None:
        self.site.requests.append(self.path)
        as_browser = self.headers.get(BROWSER_HEADER) is not None
        if as_browser:
            self.site.browser_requests.append(self.path)
        if self.path.startswith(REFUSED_PREFIX) or (self.path.startswith(BROWSERS_ONLY_PREFIX) and not as_browser):
            self.send_error(403, "Forbidden")
        elif self.path.startswith(PERSON_ONLY_PREFIX) or (self.path.startswith(CHECKED_PREFIX) and not as_browser):
            self.send_check()
        else:
            serve()

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

    def __init__(self, root: Path, media: dict[str, Path]) -> None:
        self.root = root
        self.media = media
        self.requests: list[str] = []
        # Those of them that came as from a browser.
        self.browser_requests: list[str] = []
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

    def write_page(self, name: str, title: str, players: str) -> str:
        html = f"<!doctype html><html><head><title>{title}</title></head><body><h1>{title}</h1>{players}</body></html>"
        target = self.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(html, encoding="utf-8")
        return self.url(name)

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


@pytest.fixture(name="web_media", scope="session")
def web_media_fixture(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """The clips every site hands out, made once."""
    folder = tmp_path_factory.mktemp("web-media")
    return {variant: make_clip(folder / f"{variant}.mp4", variant) for variant in CLIP_VARIANTS}


@pytest.fixture(name="new_site")
def new_site_fixture(tmp_path_factory: pytest.TempPathFactory, web_media: dict[str, Path]) -> Iterator[Callable[[], LocalSite]]:
    """Makes a site each time it is called, and closes them all afterwards."""
    sites: list[LocalSite] = []

    def make() -> LocalSite:
        site = LocalSite(tmp_path_factory.mktemp("web-site"), web_media)
        sites.append(site)
        return site

    yield make
    for site in sites:
        site.close()

"""Small web sites on this machine, for testing links to sites other than YouTube with the real yt-dlp and ffmpeg and
no outside network: a direct media file, a page with a <video> tag, an HLS stream, and addresses that refuse."""

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


class SiteHandler(http.server.SimpleHTTPRequestHandler):
    """Serves a folder, remembers what was asked for, and answers 403 under /forbidden/."""

    def __init__(self, *args: object, site: LocalSite, **kwargs: object) -> None:
        self.site = site
        super().__init__(*args, directory=str(site.root), **kwargs)  # type: ignore[arg-type]

    def log_message(self, format: str, *args: object) -> None:
        pass

    def answer(self, serve: Callable[[], None]) -> None:
        self.site.requests.append(self.path)
        if self.path.startswith(REFUSED_PREFIX):
            self.send_error(403, "Forbidden")
            return
        serve()

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

    def add_page(self, name: str = "page.html", sources: tuple[str, ...] = ("clip.mp4",), title: str = "Local page") -> str:
        """An ordinary web page that plays the given files, which must be added too."""
        players = "".join(f'<video src="{source}" controls></video>' for source in sources)
        html = f"<!doctype html><html><head><title>{title}</title></head><body><h1>{title}</h1>{players}</body></html>"
        target = self.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(html, encoding="utf-8")
        return self.url(name)

    def add_stream(self, folder: str = "hls", variant: str = "a") -> str:
        """An HLS stream: a playlist and its segments."""
        target = self.root / folder
        target.mkdir(parents=True, exist_ok=True)
        command = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(self.media[variant]), "-c", "copy", "-f", "hls"]
        subprocess.run([*command, "-hls_time", "1", "-hls_playlist_type", "vod", str(target / "index.m3u8")], check=True)
        return self.url(f"{folder}/index.m3u8")

    def asked_for(self, path: str) -> int:
        """How many requests named this path."""
        return sum(1 for request in self.requests if request.split("?")[0] == f"/{path.lstrip('/')}")

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

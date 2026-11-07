"""Runs the real yt-dlp pipeline offline: extraction returns made-up YouTube videos whose streams are local files.

Many engine bugs live in how yt-dlp's own steps (merging, keeping files, post-processors) react to the app's options,
which a stand-in for YoutubeDL cannot show.
"""

from __future__ import annotations

import copy
import io
import json
import subprocess
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import pytest
from yt_dlp import YoutubeDL
from yt_dlp.extractor import common as extractor_common
from yt_dlp.networking import Request, Response
from yt_dlp.networking.exceptions import HTTPError
from yt_dlp.postprocessor.sponsorblock import SponsorBlockPP
from yt_dlp.utils import int_or_none

import videocap.downloader as downloader_module
from conftest import has_ffmpeg


DURATION = 10
# Stream addresses on this host answer HTTP 403, as YouTube does to a client it does not accept.
FORBIDDEN_HOST = "forbidden.invalid"


def make_media(folder: Path) -> Path:
    def ffmpeg(*arguments: str) -> None:
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *arguments], check=True)

    video = ["-f", "lavfi", "-i", f"testsrc=duration={DURATION}:size=160x120:rate=10"]
    audio = ["-f", "lavfi", "-i", f"sine=frequency=440:duration={DURATION}"]
    ffmpeg(*video, "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "10", str(folder / "v.mp4"))
    ffmpeg(*audio, "-c:a", "aac", "-b:a", "64k", str(folder / "a.m4a"))
    ffmpeg(*video, "-c:v", "libvpx-vp9", "-b:v", "100k", "-deadline", "realtime", "-g", "10", str(folder / "vw.webm"))
    ffmpeg(*audio, "-c:a", "libopus", "-b:a", "48k", str(folder / "aw.webm"))
    ffmpeg("-f", "lavfi", "-i", "color=c=red:size=64x36", "-frames:v", "1", str(folder / "thumb.jpg"))
    (folder / "sub.en.vtt").write_text("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nHello\n", encoding="utf-8")
    (folder / "sub.en.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n", encoding="utf-8")
    return folder


# Named apart from the fixture name, so test modules can import these without shadowing their own parameters.
@pytest.fixture(scope="session", name="offline_media")
def offline_media_fixture(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if not has_ffmpeg:
        pytest.skip("ffmpeg is not installed")
    return make_media(tmp_path_factory.mktemp("offline_media"))


class OfflineYouTube:
    def __init__(self, media: Path) -> None:
        self.media = media
        self.videos: dict[str, dict[str, Any]] = {}
        self.sponsor_segments: dict[str, list[dict[str, Any]]] = {}
        # A scheduled stream or premiere: how many more looks it stays upcoming, and when it is due.
        self.upcoming: dict[str, tuple[int, Optional[float]]] = {}
        self.extractions: list[str] = []
        # Videos whose streams answer HTTP 403 unless yt-dlp asked YouTube through this player client.
        self.refused_unless_client: dict[str, str] = {}
        self.player_clients_asked: list[list[str]] = []

    def url_of(self, name: str) -> str:
        return (self.media / name).as_uri()

    def video_format(self, codec: str = "h264", **extra: Any) -> dict[str, Any]:
        name, format_id, vcodec = ("v.mp4", "137", "avc1.4d400c") if codec == "h264" else ("vw.webm", "248", "vp9")
        return {
            "format_id": format_id, "url": self.url_of(name), "ext": Path(name).suffix[1:], "vcodec": vcodec, "acodec": "none",
            "width": 160, "height": 120, "fps": 10, "tbr": 100, "protocol": "https", "dynamic_range": "SDR",
            "filesize": (self.media / name).stat().st_size, **extra,
        }  # fmt: skip

    def audio_format(self, codec: str = "aac", **extra: Any) -> dict[str, Any]:
        name, format_id, acodec = ("a.m4a", "140", "mp4a.40.2") if codec == "aac" else ("aw.webm", "251", "opus")
        return {
            "format_id": format_id, "url": self.url_of(name), "ext": Path(name).suffix[1:], "vcodec": "none", "acodec": acodec,
            "abr": 64, "tbr": 64, "protocol": "https", "filesize": (self.media / name).stat().st_size, **extra,
        }  # fmt: skip

    def add_video(
        self,
        video_id: str,
        title: str,
        formats: Optional[list[dict[str, Any]]] = None,
        subtitles: bool = False,
        thumbnail: bool = False,
        **extra: Any,
    ) -> str:
        url = f"https://www.youtube.com/watch?v={video_id}"
        info: dict[str, Any] = {
            "id": video_id, "title": title, "duration": DURATION, "channel": f"Channel of {video_id}",
            "upload_date": "20260101", "webpage_url": url, "live_status": "not_live",
            "formats": formats if formats is not None else [self.video_format(), self.audio_format()],
        }  # fmt: skip
        if subtitles:
            info["subtitles"] = {"en": [{"ext": "vtt", "url": self.url_of("sub.en.vtt")}, {"ext": "srt", "url": self.url_of("sub.en.srt")}]}
        if thumbnail:
            info["thumbnails"] = [{"url": self.url_of("thumb.jpg"), "id": "0"}]
        info.update(extra)
        self.videos[url] = info
        return url

    def add_upcoming(self, video_id: str, title: str, release: Optional[float], looks: int = 1, **extra: Any) -> str:
        """A video that YouTube reports as not started yet for the first `looks` extractions."""
        url = self.add_video(video_id, title, **extra)
        self.upcoming[url] = (looks, release)
        return url

    def add_playlist(self, list_id: str, video_urls: list[str], title: str = "Playlist") -> str:
        entries = [
            {"_type": "url", "ie_key": "Youtube", "url": url, "id": self.videos[url]["id"], "title": self.videos[url]["title"]}
            for url in video_urls
        ]
        url = f"https://www.youtube.com/playlist?list={list_id}"
        self.videos[url] = {"_type": "playlist", "id": list_id, "title": title, "entries": entries}
        return url

    def extract(self, extractor: Any, url: str) -> dict[str, Any]:
        if url not in self.videos:
            raise extractor_common.ExtractorError(f"[youtube] {url}: Video unavailable", expected=True)
        self.extractions.append(url)
        info = copy.deepcopy(self.videos[url])
        clients = extractor._configuration_arg("player_client")
        self.player_clients_asked.append(clients)
        working_client = self.refused_unless_client.get(str(info.get("id")))
        if working_client and working_client not in clients:
            for media_format in info.get("formats") or []:
                media_format["url"] = f"https://{FORBIDDEN_HOST}/{info['id']}/{media_format['format_id']}"
        looks_left, release = self.upcoming.get(url, (0, None))
        if looks_left:
            self.upcoming[url] = (looks_left - 1, release)
            # What YouTube's extractor does: under wait_for_video no formats yet and a warning, otherwise an error.
            info.update({"formats": [], "live_status": "is_upcoming", "release_timestamp": release})
            extractor.raise_no_formats("This live event will begin in a few moments.", expected=True)
        comments = info.pop("comments", None)
        if comments is not None and extractor.get_param("getcomments"):
            # Read the way YouTube's own extractor reads them, so the app's arguments are what decides.
            newest_first = extractor._configuration_arg("comment_sort", ["top"])[0] == "new"
            limit = int_or_none(extractor._configuration_arg("max_comments", [""])[0])
            ordered = sorted(comments, key=lambda comment: comment["timestamp" if newest_first else "like_count"], reverse=True)[:limit]
            info["__post_extractor"] = lambda: {"comments": ordered, "comment_count": len(ordered)}
        return info


@pytest.fixture(name="offline_youtube")
def offline_youtube_fixture(offline_media: Path, monkeypatch: pytest.MonkeyPatch) -> OfflineYouTube:
    site = OfflineYouTube(offline_media)
    monkeypatch.setattr(extractor_common.InfoExtractor, "extract", lambda extractor, url: site.extract(extractor, url))
    monkeypatch.setattr(
        SponsorBlockPP, "_get_sponsor_segments", lambda processor, video_id, service: copy.deepcopy(site.sponsor_segments.get(video_id, []))
    )
    real_build = downloader_module.build_ydl_options

    def build_with_file_urls(*arguments: Any, **keywords: Any) -> dict[str, Any]:
        return {**real_build(*arguments, **keywords), "enable_file_urls": True}

    monkeypatch.setattr(downloader_module, "build_ydl_options", build_with_file_urls)
    real_urlopen = YoutubeDL.urlopen

    def urlopen_refusing(ydl: YoutubeDL, request: Any) -> Any:
        url = request.url if isinstance(request, Request) else str(request)
        if urlparse(url).hostname == FORBIDDEN_HOST:
            # What YouTube answers a stream request it does not accept from this client.
            raise HTTPError(Response(io.BytesIO(b""), url, {}, status=403, reason="Forbidden"))
        return real_urlopen(ydl, request)

    monkeypatch.setattr(YoutubeDL, "urlopen", urlopen_refusing)
    return site


def probe(path: Path) -> dict[str, Any]:
    output = subprocess.run(
        ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
        capture_output=True, check=True, text=True, encoding="utf-8",
    ).stdout  # fmt: skip
    return json.loads(output)


def stream_codecs(path: Path) -> list[str]:
    return [stream["codec_name"] for stream in probe(path)["streams"]]


def duration_of(path: Path) -> float:
    return float(probe(path)["format"]["duration"])

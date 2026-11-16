"""Real downloads against YouTube and, at the end, two other sites. Skipped by default; run with: pytest -m network"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from conftest import needs_ffmpeg
from videocap.downloader import OTHER_SITE_SPONSORBLOCK_NOTE, run_download
from videocap.inspector import inspect_link, search_youtube
from videocap.library import describe_probe, get_media_duration, probe_media
from videocap.options import AccessLimits, LoginSource, job_url, options_from_spec
from videocap.settings import DownloadSettings, JobSpec


pytestmark = pytest.mark.network

# YouTube's first video: 19 seconds, with chapters and subtitles.
VIDEO = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
CHANNEL = "https://www.youtube.com/@jawed"
# A one-minute campaign film from 1960 in the Internet Archive, which yt-dlp has an extractor for.
ARCHIVE_VIDEO = "https://archive.org/details/Election_Ads/Commercial-JFK1960ElectionAdCampaignJingle.mpg"
# A five-second CC0 clip that MDN's examples use: a plain file, for yt-dlp's generic extractor.
DIRECT_LINK = "https://interactive-examples.mdn.mozilla.net/media/cc0-videos/flower.mp4"


class Progress:
    def progress(self, value: float, text: str = "") -> None:
        pass


def download(tmp_path: Path, url: str = VIDEO, **settings: object):
    spec = JobSpec(url=url, settings=DownloadSettings(**settings), output_dir=str(tmp_path))  # type: ignore[arg-type]
    outcome = run_download(job_url(spec), options_from_spec(spec, AccessLimits()), Progress())
    assert outcome["errors"] == [], outcome["errors"]
    return outcome


def test_inspect_previews_what_will_be_downloaded(tmp_path: Path) -> None:
    spec = JobSpec(url=VIDEO, settings=DownloadSettings(), output_dir=str(tmp_path))
    report = inspect_link(VIDEO, options_from_spec(spec, AccessLimits()))
    assert report["error"] == ""
    assert report["title"] == "Me at the zoo"
    assert report["selected_format"]
    assert report["selected_size"] > 0
    assert report["output_name"] == "Me at the zoo.mp4"
    assert report["format_rows"]
    assert [chapter["title"] for chapter in report["chapters"]] == ["Intro", "The cool thing", "End"]


def test_inspect_lists_a_channel(tmp_path: Path) -> None:
    spec = JobSpec(url=CHANNEL, settings=DownloadSettings(), output_dir=str(tmp_path))
    report = inspect_link(CHANNEL, options_from_spec(spec, AccessLimits()))
    assert report["error"] == ""
    assert report["is_playlist"] is True
    assert report["entries"]


def test_inspect_names_the_file_a_conversion_will_produce(tmp_path: Path) -> None:
    spec = JobSpec(url=VIDEO, settings=DownloadSettings(media_type="audio", audio_format="mp3"), output_dir=str(tmp_path))
    assert inspect_link(VIDEO, options_from_spec(spec, AccessLimits()))["output_name"] == "Me at the zoo.mp3"


def test_inspect_reports_a_removed_video_as_an_error(tmp_path: Path) -> None:
    removed = "https://www.youtube.com/watch?v=Cr381pDsSsA"
    spec = JobSpec(url=removed, settings=DownloadSettings(), output_dir=str(tmp_path))
    assert inspect_link(removed, options_from_spec(spec, AccessLimits()))["error"]


def test_a_share_link_from_inside_a_playlist_works(tmp_path: Path) -> None:
    shared = "https://youtu.be/jNQXAC9IVRw?list=PLBCF2DAC6FFB574DE&si=AbCdEf"
    spec = JobSpec(url=shared, settings=DownloadSettings(), output_dir=str(tmp_path))
    report = inspect_link(shared, options_from_spec(spec, AccessLimits()))
    assert report["error"] == ""
    assert report["title"] == "Me at the zoo"


def test_search_finds_videos() -> None:
    results = search_youtube("cats", 3, LoginSource())
    assert len(results) == 3
    assert all(result["url"].startswith("https://www.youtube.com/watch") for result in results)


@needs_ffmpeg
def test_video_with_subtitles_chapters_and_transcript(tmp_path: Path) -> None:
    outcome = download(tmp_path, subtitle_languages="en", write_transcript=True)
    item = outcome["items"][0]
    assert Path(item["path"]).suffix == ".mp4"
    assert [Path(path).suffix for path in item["subtitle_paths"]] == [".srt"]
    assert "elephants" in (tmp_path / "Me at the zoo.en.txt").read_text(encoding="utf-8")
    probe = probe_media(Path(item["path"]))
    assert probe is not None
    assert any("字幕" in detail for detail in describe_probe(probe))
    assert any("章節" in detail for detail in describe_probe(probe))


@needs_ffmpeg
@pytest.mark.parametrize("audio_format", ["mp3", "m4a", "opus"])
def test_audio_formats(tmp_path: Path, audio_format: str) -> None:
    outcome = download(tmp_path, media_type="audio", audio_format=audio_format, download_subtitles=False)
    assert Path(outcome["items"][0]["path"]).suffix == f".{audio_format}"


def test_sidecars_without_media(tmp_path: Path) -> None:
    outcome = download(tmp_path, media_type="none", subtitle_languages="en", write_description=True)
    item = outcome["items"][0]
    assert item["path"] == ""
    assert item["subtitle_paths"]
    assert not list(tmp_path.glob("*.mp4"))


@needs_ffmpeg
def test_clip_produces_one_file_per_range(tmp_path: Path) -> None:
    outcome = download(tmp_path, time_ranges="0:02-0:05, 0:10-0:12", download_subtitles=False)
    assert len(outcome["items"]) == 2
    assert len({item["path"] for item in outcome["items"]}) == 2


@needs_ffmpeg
def test_split_by_chapters(tmp_path: Path) -> None:
    outcome = download(tmp_path, split_chapters=True, download_subtitles=False)
    assert len(outcome["items"][0]["extra_paths"]) == 3


@needs_ffmpeg
def test_sponsorblock_does_not_break_a_download_without_segments(tmp_path: Path) -> None:
    outcome = download(tmp_path, sponsorblock_mode="remove", download_subtitles=False)
    assert outcome["items"]


@needs_ffmpeg
@pytest.mark.parametrize(
    ("container", "codec", "suffix"),
    [("mkv", "h264", ".mkv"), ("mp4", "best", ".mp4"), ("webm", "vp9", ".webm"), ("mkv", "av1", ".mkv")],
)
def test_containers_and_codecs(tmp_path: Path, container: str, codec: str, suffix: str) -> None:
    outcome = download(tmp_path, container=container, video_codec=codec, subtitle_languages="en")
    assert Path(outcome["items"][0]["path"]).suffix == suffix
    assert probe_media(Path(outcome["items"][0]["path"])) is not None


@needs_ffmpeg
@pytest.mark.parametrize("container", ["mp4", "mkv"])
def test_embedded_cover_and_kept_sidecars(tmp_path: Path, container: str) -> None:
    outcome = download(
        tmp_path,
        container=container,
        embed_thumbnail=True,
        write_thumbnail=True,
        write_description=True,
        write_infojson=True,
        subtitle_languages="en",
    )
    item = outcome["items"][0]
    suffixes = {Path(path).suffix for path in item["extra_paths"]}
    assert {".description", ".json"} <= suffixes
    assert suffixes & {".webp", ".jpg", ".png"}
    probe = probe_media(Path(item["path"]))
    assert probe is not None
    # MP4 holds a cover as a picture stream, MKV as an attached file.
    assert any(stream["disposition"].get("attached_pic") or stream["codec_type"] == "attachment" for stream in probe["streams"])


@needs_ffmpeg
def test_precise_clip_has_the_requested_length(tmp_path: Path) -> None:
    outcome = download(tmp_path, time_ranges="0:02-0:05", precise_cuts=True, download_subtitles=False)
    assert get_media_duration(Path(outcome["items"][0]["path"])) == pytest.approx(3.0, abs=0.5)


def test_filters_and_item_selection_on_a_channel(tmp_path: Path) -> None:
    skipped = download(tmp_path / "skipped", url=f"{CHANNEL}/videos", media_type="none", filter_title_exclude="zoo")
    assert skipped["items"] == []
    assert skipped["skipped"]

    picked = download(tmp_path / "picked", url=f"{CHANNEL}/videos", media_type="none", playlist_items="1", subtitle_languages="en")
    assert len(picked["items"]) == 1


def test_only_creative_commons_filter_checks_the_license(tmp_path: Path) -> None:
    outcome = download(tmp_path, media_type="none", filter_cc_only=True, subtitle_languages="en")
    assert outcome["items"] == []
    assert "Creative Commons" in outcome["skipped"][0]


def test_ascii_only_file_names(tmp_path: Path) -> None:
    outcome = download(tmp_path, restrict_filenames=True, download_subtitles=False)
    assert Path(outcome["items"][0]["path"]).name == "Me_at_the_zoo.mp4"


@needs_ffmpeg
def test_sponsorblock_cuts_a_video_with_real_segments(tmp_path: Path) -> None:
    # Community data for this 236-second music video marks 37 seconds at the start as non-music.
    outcome = download(
        tmp_path,
        url="https://www.youtube.com/watch?v=L_jWHffIx5E",
        quality="360p",
        sponsorblock_mode="remove",
        sponsorblock_categories=["music_offtopic"],
        download_subtitles=False,
    )
    duration = get_media_duration(Path(outcome["items"][0]["path"]))
    assert duration is not None and duration < 215


# Other sites. Only the machine's owner reaches them, which AccessLimits() is.


def test_inspect_names_the_site_of_a_video_from_elsewhere(tmp_path: Path) -> None:
    spec = JobSpec(url=ARCHIVE_VIDEO, settings=DownloadSettings(), output_dir=str(tmp_path))
    report = inspect_link(ARCHIVE_VIDEO, options_from_spec(spec, AccessLimits()))
    assert report["error"] == ""
    assert report["extractor"] == "archiveorg" and report["site"] == "Internet Archive"
    # The archive says nothing about codecs; its files are still listed, and the one that will be fetched is named.
    assert report["selected_format"] and report["format_rows"]
    assert report["selected_size"] > 1_000_000


@needs_ffmpeg
def test_a_video_from_another_site_downloads_without_the_youtube_only_steps(tmp_path: Path) -> None:
    spec = JobSpec(url=ARCHIVE_VIDEO, settings=DownloadSettings(download_subtitles=False), output_dir=str(tmp_path))
    preview = inspect_link(ARCHIVE_VIDEO, options_from_spec(spec, AccessLimits()))

    outcome = download(tmp_path, url=ARCHIVE_VIDEO, sponsorblock_mode="remove", download_subtitles=False)
    item = outcome["items"][0]
    assert item["extractor"] == "archiveorg"
    # The site's own file is kept as it is (here its sharpest copy is an Ogg video), under the name the preview gave.
    assert Path(item["path"]).name == preview["output_name"]
    assert get_media_duration(Path(item["path"])) == pytest.approx(60, abs=3)
    # SponsorBlock only knows YouTube's videos: it is left out, and the person is told.
    assert OTHER_SITE_SPONSORBLOCK_NOTE in outcome["notes"]


def test_a_direct_link_downloads_under_an_id_of_its_own(tmp_path: Path) -> None:
    spec = JobSpec(url=DIRECT_LINK, settings=DownloadSettings(), output_dir=str(tmp_path))
    report = inspect_link(DIRECT_LINK, options_from_spec(spec, AccessLimits()))
    assert report["error"] == ""
    assert report["site"] == "interactive-examples.mdn.mozilla.net（直接連結）"

    outcome = download(tmp_path, url=DIRECT_LINK, download_subtitles=False)
    item = outcome["items"][0]
    assert item["extractor"] == "generic"
    # The file's name plus a mark of the link, so another site's flower.mp4 is another video.
    assert re.fullmatch(r"flower-[0-9a-f]{8}", item["video_id"]) and item["video_id"] == report["video_id"]
    assert Path(item["path"]).name == "flower.mp4" and Path(item["path"]).stat().st_size > 100_000


def test_a_visitor_is_refused_another_site_before_anything_is_fetched(tmp_path: Path) -> None:
    spec = JobSpec(url=DIRECT_LINK, settings=DownloadSettings(), output_dir=str(tmp_path))
    with pytest.raises(ValueError, match="只開放下載 YouTube"):
        options_from_spec(spec, AccessLimits(trusted=False))

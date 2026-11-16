"""Links to sites other than YouTube, downloaded with the real yt-dlp and ffmpeg from web sites on this machine."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Optional

import pytest

from conftest import needs_ffmpeg
from offline_web import LocalSite, new_site_fixture, web_media_fixture  # noqa: F401
from offline_youtube import duration_of, probe
from videocap.downloader import UrlKeyedIdPP, run_download
from videocap.inspector import inspect_link
from videocap.options import AccessLimits, DownloadOptions, options_from_spec
from videocap.settings import DownloadSettings, JobSpec
from videocap.sites import media_extension_from_bytes


pytestmark = needs_ffmpeg
NewSite = Callable[[], LocalSite]
# What the app adds to an ID yt-dlp took from a file's or a page's name.
KEYED_ID_RE = re.compile(r".+-[0-9a-f]{8}")


class Progress:
    def __init__(self) -> None:
        self.texts: list[str] = []

    def progress(self, value: float, text: str = "") -> None:
        self.texts.append(text)


def owner_options(url: str, output_dir: Path, archive: Optional[Path] = None, **settings: Any) -> DownloadOptions:
    spec = JobSpec(url=url, settings=DownloadSettings(**settings), output_dir=str(output_dir))
    return options_from_spec(spec, AccessLimits(), archive_path=archive)


def download(url: str, output_dir: Path, archive: Optional[Path] = None, progress: Optional[Progress] = None, **settings: Any) -> Any:
    return run_download(url, owner_options(url, output_dir, archive, **settings), progress or Progress())


def inspect(url: str, output_dir: Path, **settings: Any) -> Any:
    return inspect_link(url, owner_options(url, output_dir, **settings))


def media_files(folder: Path) -> list[Path]:
    return sorted(path for path in folder.iterdir() if path.suffix in {".mp4", ".mkv", ".webm"})


# What the owner can download


def test_a_direct_link_downloads(new_site: NewSite, tmp_path: Path) -> None:
    site = new_site()
    outcome = download(site.add_clip("clip.mp4"), tmp_path)
    assert outcome["errors"] == []
    item = outcome["items"][0]
    assert Path(item["path"]).name == "clip.mp4" and duration_of(Path(item["path"])) == pytest.approx(2, abs=0.3)
    # Which extractor found it goes into the history with it.
    assert item["extractor"] == "generic" and item["webpage_url"] == site.url("clip.mp4")


def test_a_page_with_a_video_downloads(new_site: NewSite, tmp_path: Path) -> None:
    site = new_site()
    site.add_clip("clip.mp4")
    outcome = download(site.add_page("watch.html", title="My holiday"), tmp_path)
    assert outcome["errors"] == []
    saved = Path(outcome["items"][0]["path"])
    # The page's own title names the file, not the media file's.
    assert saved.stem.startswith("My holiday") and duration_of(saved) == pytest.approx(2, abs=0.3)


def test_a_stream_downloads_as_one_file(new_site: NewSite, tmp_path: Path) -> None:
    site = new_site()
    outcome = download(site.add_stream("live"), tmp_path)
    assert outcome["errors"] == []
    saved = Path(outcome["items"][0]["path"])
    assert saved.suffix == ".mp4" and duration_of(saved) == pytest.approx(2, abs=0.5)


# Two sites can name their files alike


def test_two_sites_files_of_one_name_are_two_videos(new_site: NewSite, tmp_path: Path) -> None:
    first, second = new_site(), new_site()
    first_url, second_url = first.add_clip("clip.mp4", "a"), second.add_clip("clip.mp4", "b")
    one = download(first_url, tmp_path)
    two = download(second_url, tmp_path)
    assert one["errors"] == [] and two["errors"] == []
    # yt-dlp calls both "clip"; the app's ID also says where each came from.
    first_id, second_id = one["items"][0]["video_id"], two["items"][0]["video_id"]
    assert first_id.startswith("clip-") and second_id.startswith("clip-") and first_id != second_id
    assert KEYED_ID_RE.fullmatch(first_id) and KEYED_ID_RE.fullmatch(second_id)
    files = media_files(tmp_path)
    assert [path.name for path in files] == [f"clip [{second_id}].mp4", "clip.mp4"]
    # Neither replaced the other: the clips differ in length.
    assert duration_of(tmp_path / "clip.mp4") == pytest.approx(2, abs=0.3)
    assert duration_of(tmp_path / f"clip [{second_id}].mp4") == pytest.approx(3, abs=0.3)


def test_skipping_downloaded_videos_tells_two_sites_apart(new_site: NewSite, tmp_path: Path) -> None:
    first, second = new_site(), new_site()
    first_url, second_url = first.add_clip("clip.mp4", "a"), second.add_clip("clip.mp4", "b")
    archive = tmp_path / "archive.txt"
    output = tmp_path / "out"
    one = download(first_url, output, archive, skip_archived=True)
    two = download(second_url, output, archive, skip_archived=True)
    assert one["errors"] == [] and two["errors"] == []
    assert len(one["items"]) == 1 and len(two["items"]) == 1 and len(media_files(output)) == 2
    # Each line of the archive names its own site's file. Without the suffix both would read "generic clip", and
    # the second download would have been skipped as already done.
    lines = archive.read_text(encoding="utf-8").splitlines()
    assert lines == [f"generic {one['items'][0]['video_id']}", f"generic {two['items'][0]['video_id']}"]

    again = download(first_url, output, archive, skip_archived=True)
    assert again["items"] == [] and again["archived_skips"] == 1
    assert len(media_files(output)) == 2


def test_the_same_link_is_the_same_video_whatever_trackers_it_carries(new_site: NewSite, tmp_path: Path) -> None:
    site = new_site()
    url = site.add_clip("clip.mp4")
    archive = tmp_path / "archive.txt"
    first = download(url, tmp_path / "out", archive, skip_archived=True)
    again = download(f"{url}?utm_source=share&fbclid=abc#t=1", tmp_path / "out", archive, skip_archived=True)
    assert len(first["items"]) == 1 and again["items"] == [] and again["archived_skips"] == 1


def test_downloading_the_same_link_again_keeps_one_file(new_site: NewSite, tmp_path: Path) -> None:
    site = new_site()
    url = site.add_clip("clip.mp4")
    assert download(url, tmp_path)["errors"] == []
    again = download(url, tmp_path)
    assert again["errors"] == []
    # The file already there is this same video, so no second copy with an ID in its name appears.
    assert [path.name for path in media_files(tmp_path)] == ["clip.mp4"]


def test_every_video_of_a_page_gets_its_own_id(new_site: NewSite, tmp_path: Path) -> None:
    site = new_site()
    site.add_clip("one.mp4", "a")
    site.add_clip("two.mp4", "b")
    archive = tmp_path / "archive.txt"
    outcome = download(site.add_page("both.html", sources=("one.mp4", "two.mp4")), tmp_path / "out", archive, skip_archived=True, expand_playlist=True)
    assert outcome["errors"] == []
    ids = [item["video_id"] for item in outcome["items"]]
    assert len(ids) == 2 and len(set(ids)) == 2
    # One suffix each: the step that adds it must not run twice on the same video.
    assert all(re.fullmatch(r"both-[12]-[0-9a-f]{8}", video_id) for video_id in ids)
    lines = archive.read_text(encoding="utf-8").splitlines()
    assert sorted(lines) == sorted(f"html5mediaembed {video_id}" for video_id in ids)


# A site that offers one video as whole files of more than one kind

OFFERED_FILES = (("big.mkv", "video/x-matroska", 480, 270), ("small.mp4", "video/mp4", 320, 240))


def choice_page(site: LocalSite, *only: str) -> str:
    """A page that offers the sharper clip as big.mkv and the smaller one as small.mp4, or only the files named."""
    site.add_rewrapped("big.mkv", "b")
    site.add_clip("small.mp4", "a")
    return site.add_choice_page("watch.html", tuple(source for source in OFFERED_FILES if not only or source[0] in only))


def picture_width(path: Path) -> int:
    return next(int(stream["width"]) for stream in probe(path)["streams"] if stream["codec_type"] == "video")


def test_a_site_that_has_an_mp4_gives_it_even_when_another_file_is_sharper(new_site: NewSite, tmp_path: Path) -> None:
    url = choice_page(new_site())
    preview = inspect(url, tmp_path)
    outcome = download(url, tmp_path)
    assert outcome["errors"] == []
    saved = Path(outcome["items"][0]["path"])
    # MP4 is the output container asked for, and no step would turn the sharper Matroska file into one.
    assert saved.suffix == ".mp4" and picture_width(saved) == 320
    assert preview["error"] == "" and preview["output_name"] == saved.name


def test_a_site_without_an_mp4_keeps_its_own_kind_of_file(new_site: NewSite, tmp_path: Path) -> None:
    url = choice_page(new_site(), "big.mkv")
    outcome = download(url, tmp_path)
    assert outcome["errors"] == []
    saved = Path(outcome["items"][0]["path"])
    assert saved.suffix == ".mkv" and picture_width(saved) == 480


def test_the_mkv_container_takes_the_sharpest_file(new_site: NewSite, tmp_path: Path) -> None:
    url = choice_page(new_site())
    preview = inspect(url, tmp_path, container="mkv")
    outcome = download(url, tmp_path, container="mkv")
    assert outcome["errors"] == []
    saved = Path(outcome["items"][0]["path"])
    # MKV holds whatever the site has, so nothing is given up for it.
    assert saved.suffix == ".mkv" and picture_width(saved) == 480
    assert preview["output_name"] == saved.name


# What is shown before downloading


def test_inspection_shows_the_id_and_name_the_download_will_have(new_site: NewSite, tmp_path: Path) -> None:
    site = new_site()
    url = site.add_clip("clip.mp4")
    report = inspect_link(url, owner_options(url, tmp_path))
    assert report["error"] == ""
    outcome = download(url, tmp_path)
    assert report["video_id"] == outcome["items"][0]["video_id"]
    assert report["output_name"] == Path(outcome["items"][0]["path"]).name
    assert KEYED_ID_RE.fullmatch(report["video_id"])


def test_a_direct_links_report_lists_its_one_format_with_what_is_known(new_site: NewSite, tmp_path: Path) -> None:
    site = new_site()
    report = inspect(site.add_clip("clip.mp4"), tmp_path)
    assert report["error"] == "" and report["is_playlist"] is False
    # The server says only that it is an MP4 file: no codecs, no picture size, and here not even a size.
    (row,) = report["format_rows"]
    assert (row["類型"], row["容器"], row["影像編碼"], row["音訊編碼"]) == ("影像", "mp4", "未知", "未知")
    assert row["大小"] == "" and row["解析度"] == ""
    assert report["selected_format"] == "mp4"
    # No size is shown, rather than 0 B: the page leaves out the size of a selected_size of 0.
    assert report["selected_size"] == 0


def test_a_file_that_does_not_say_what_it_is_is_named_by_how_it_starts(new_site: NewSite, tmp_path: Path) -> None:
    site = new_site()
    # No extension, so the server sends it as application/octet-stream, and yt-dlp alone would save ".unknown_video".
    url = site.add_clip("video")
    report = inspect(url, tmp_path)
    assert report["error"] == ""
    assert [(row["類型"], row["容器"]) for row in report["format_rows"]] == [("影像", "mp4")]
    assert report["output_name"] == "video.mp4"

    outcome = download(url, tmp_path)
    assert outcome["errors"] == []
    saved = Path(outcome["items"][0]["path"])
    # Under a name a player opens, and through the steps that write into the file, which fail on an unknown kind.
    assert saved.name == "video.mp4" and duration_of(saved) == pytest.approx(2, abs=0.3)
    assert not list(tmp_path.glob("*.unknown_video"))


def test_a_file_of_no_known_kind_is_not_called_unplayable(new_site: NewSite, tmp_path: Path) -> None:
    site = new_site()
    (site.root / "data").write_bytes(bytes(range(256)) * 8)
    report = inspect(site.url("data"), tmp_path)
    assert report["error"] == ""
    assert [(row["類型"], row["容器"]) for row in report["format_rows"]] == [("未知", "unknown_video")]


FILE_STARTS = [
    (b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00", "mp4"),
    (b"\x00\x00\x00\x14ftypqt  ", "mov"),
    (b"\x00\x00\x00\x1cftypM4A ", "m4a"),
    (b"\x1a\x45\xdf\xa3\x9f\x42\x86\x81\x01\x42\x82\x84webm", "webm"),
    (b"\x1a\x45\xdf\xa3\x9f\x42\x82\x88matroska", "mkv"),
    (b"OggS\x00\x02" + bytes(22) + b"\x80theora", "ogv"),
    (b"OggS\x00\x02" + bytes(22) + b"OpusHead", "opus"),
    (b"OggS\x00\x02" + bytes(22) + b"\x01vorbis", "ogg"),
    (b"RIFF\x00\x00\x00\x00AVI LIST", "avi"),
    (b"RIFF\x00\x00\x00\x00WAVEfmt ", "wav"),
    (b"FLV\x01\x05", "flv"),
    (b"fLaC\x00\x00\x00\x22", "flac"),
    (b"ID3\x04\x00", "mp3"),
    (b"\xff\xfb\x90\x64", "mp3"),
    (b"\x30\x26\xb2\x75\x8e\x66\xcf\x11\xa6\xd9", "wmv"),
    (b"\x00\x00\x01\xba\x44", "mpg"),
    (b"\x47" + bytes(187) + b"\x47" + bytes(187), "ts"),
    # Not media, or not a kind told apart here: left as yt-dlp had it.
    (b"<!doctype html><html>", ""),
    (b"RIFF\x00\x00\x00\x00WEBPVP8 ", ""),
    (b"\xff\xf1\x50\x80", ""),
    (b"\x47" + bytes(40), ""),
    (b"", ""),
]


@pytest.mark.parametrize(("start", "extension"), FILE_STARTS)
def test_a_files_first_bytes_say_what_kind_it_is(start: bytes, extension: str) -> None:
    assert media_extension_from_bytes(start) == extension


def test_every_kind_read_from_a_file_is_one_yt_dlp_will_write() -> None:
    from yt_dlp.utils._utils import _UnsafeExtensionError

    # yt-dlp refuses to save a file under an extension that is not on its list.
    assert {extension for _, extension in FILE_STARTS if extension} <= _UnsafeExtensionError.ALLOWED_EXTENSIONS


@pytest.mark.parametrize(("name", "label"), [("clip.mp4", "影像"), ("song.m4a", "音訊")])
def test_a_direct_links_progress_names_the_file_not_subtitles(new_site: NewSite, tmp_path: Path, name: str, label: str) -> None:
    site = new_site()
    progress = Progress()
    outcome = download(site.add_clip(name), tmp_path, progress=progress, download_subtitles=False)
    assert outcome["errors"] == []
    assert f"{label}下載完成" in progress.texts
    # It used to show 下載字幕... for as long as the file took: a codec yt-dlp was not told looked like a subtitle's.
    assert not any(text.startswith("下載字幕") or text == "字幕下載完成" for text in progress.texts)


def test_a_page_with_two_videos_lists_both_by_position(new_site: NewSite, tmp_path: Path) -> None:
    site = new_site()
    one, two = site.add_clip("one.mp4", "a"), site.add_clip("two.mp4", "b")
    report = inspect(site.add_page("both.html", sources=("one.mp4", "two.mp4")), tmp_path, expand_playlist=True)
    assert report["error"] == "" and report["is_playlist"] is True and report["is_nested"] is False
    entries = report["entries"]
    assert [entry["index"] for entry in entries] == [1, 2]
    # Each player's own file is its link; the page is both of theirs.
    assert [entry["url"] for entry in entries] == [one, two]
    assert all(re.fullmatch(r"both-[12]-[0-9a-f]{8}", entry["id"]) for entry in entries)
    # yt-dlp asks which entries to take before the app gives them these IDs, so they are picked by position.
    assert report["entries_have_ids"] is False


# What a failure on another site says


def test_a_refusal_from_another_site_names_that_site_and_suggests_a_login(new_site: NewSite, tmp_path: Path) -> None:
    site = new_site()
    site.add_clip("forbidden/clip.mp4")
    url = site.url("forbidden/clip.mp4")
    for error in (inspect(url, tmp_path)["error"], *download(url, tmp_path)["errors"]):
        assert error.startswith("這個網站拒絕了下載請求（HTTP 403）") and "會員登入" in error
        # Not YouTube's own advice about its player clients, cloud hosts and updates.
        assert "YouTube" not in error.partition("（原始訊息：")[0]


def test_a_refusal_while_fetching_the_file_is_the_sites_too(new_site: NewSite, tmp_path: Path) -> None:
    site = new_site()
    site.add_clip("forbidden/clip.mp4")
    # The page can be read; its player's file is refused. yt-dlp's message for that names no site at all.
    url = site.add_page("watch.html", ("forbidden/clip.mp4",))
    (error,) = download(url, tmp_path)["errors"]
    assert "[" not in error.partition("（原始訊息：")[2][:1]
    assert error.startswith("這個網站拒絕了下載請求（HTTP 403）")
    assert "YouTube" not in error.partition("（原始訊息：")[0]


def test_a_missing_file_on_another_site_says_the_link_is_gone(new_site: NewSite, tmp_path: Path) -> None:
    site = new_site()
    for url in (site.url("missing.mp4"), site.url("gone/page.html")):
        assert inspect(url, tmp_path)["error"].startswith("找不到這部影片（HTTP 404）")
        assert download(url, tmp_path)["errors"][0].startswith("找不到這部影片（HTTP 404）")


def test_a_site_that_cannot_be_reached_is_named_and_not_retried_as_a_dropped_connection(new_site: NewSite, tmp_path: Path) -> None:
    from videocap.errors import is_network_loss, is_unreachable

    site = new_site()
    url = site.add_clip("clip.mp4")
    site.close()
    (error,) = download(url, tmp_path)["errors"]
    assert error.startswith("連不上這個網站")
    # The queue's automatic retry tells this apart from a connection lost halfway.
    assert is_network_loss(error) and is_unreachable(error)


# Who may not


def test_a_visitors_link_to_another_site_is_refused_before_anything_is_fetched(new_site: NewSite, tmp_path: Path) -> None:
    site = new_site()
    url = site.add_clip("clip.mp4")
    spec = JobSpec(url=url, settings=DownloadSettings(), output_dir=str(tmp_path))
    with pytest.raises(ValueError, match="只開放下載 YouTube"):
        options_from_spec(spec, AccessLimits.for_public_host())
    assert site.requests == []


def test_options_not_made_for_the_owner_reach_no_other_site(new_site: NewSite, tmp_path: Path) -> None:
    site = new_site()
    url = site.add_clip("clip.mp4")
    # As if the check above were missing: yt-dlp itself still has only YouTube's extractors.
    options = DownloadOptions(output_dir=tmp_path, output_template="%(title)s.%(ext)s")
    outcome = run_download(url, options, Progress())
    assert outcome["items"] == [] and outcome["errors"]
    assert inspect_link(url, options)["error"]
    assert site.requests == [] and media_files(tmp_path) == []


# The step that makes the IDs


def keyed(**info: Any) -> dict[str, Any]:
    return UrlKeyedIdPP().run(info)[1]


def test_only_links_named_after_a_file_or_page_get_a_new_id() -> None:
    direct = keyed(id="clip", extractor_key="Generic", original_url="https://a.example/clip.mp4", url="https://a.example/clip.mp4", direct=True)
    assert KEYED_ID_RE.fullmatch(direct["id"]) and direct["id"].startswith("clip-")
    assert direct["_old_archive_ids"] == []
    # A site with an extractor of its own has real IDs.
    assert keyed(id="76979871", extractor_key="Vimeo", original_url="https://vimeo.com/76979871")["id"] == "76979871"
    assert keyed(id="jNQXAC9IVRw", extractor_key="Youtube", webpage_url="https://www.youtube.com/watch?v=jNQXAC9IVRw")["id"] == "jNQXAC9IVRw"


def test_an_id_is_rewritten_once_and_a_list_entry_without_one_is_left_alone() -> None:
    once = keyed(id="clip", extractor_key="Generic", original_url="https://a.example/clip.mp4")
    assert UrlKeyedIdPP().run(dict(once))[1]["id"] == once["id"]
    # yt-dlp passes a list's flat entries through the same step before they have an ID.
    flat = keyed(id=None, extractor_key="Generic", url="https://a.example/x")
    assert flat["id"] is None and "_old_archive_ids" not in flat


def test_a_redirecting_file_keeps_its_id_and_two_players_of_one_page_differ() -> None:
    link = "https://a.example/clip.mp4"
    # Where the file was finally served from may change with every request.
    first = keyed(id="clip", extractor_key="Generic", original_url=link, webpage_url="https://cdn.example/t1/clip.mp4", url="https://cdn.example/t1/clip.mp4", direct=True)
    later = keyed(id="clip", extractor_key="Generic", original_url=link, webpage_url="https://cdn.example/t2/clip.mp4", url="https://cdn.example/t2/clip.mp4", direct=True)
    assert first["id"] == later["id"]
    page = "https://a.example/watch.html"
    one = keyed(id="master", extractor_key="Generic", webpage_url=page, formats=[{"url": "https://cdn.example/one/master.m3u8"}])
    two = keyed(id="master", extractor_key="Generic", webpage_url=page, formats=[{"url": "https://cdn.example/two/master.m3u8"}])
    assert one["id"] != two["id"]


# What is kept afterwards


def test_history_and_the_already_downloaded_check_know_the_site(new_site: NewSite, tmp_path: Path) -> None:
    from videocap.services import history_rows
    from videocap.storage import AppStorage

    first, second = new_site(), new_site()
    first_url, second_url = first.add_clip("clip.mp4", "a"), second.add_clip("clip.mp4", "b")
    storage = AppStorage(tmp_path / "data")
    outcome = download(first_url, tmp_path / "out")
    storage.add_history(history_rows(outcome))
    entry = storage.list_history()[0]
    assert entry.site == "generic" and entry.url == first_url

    # The report of the same link finds that download; the other site's file of the same name is new.
    seen = inspect_link(first_url, owner_options(first_url, tmp_path / "out"))
    other = inspect_link(second_url, owner_options(second_url, tmp_path / "out"))
    assert seen["extractor"] == other["extractor"] == "generic"
    assert seen["site"] == "127.0.0.1（直接連結）"
    assert storage.find_downloads([seen["video_id"]], seen["extractor"])[seen["video_id"]].path == entry.path
    assert storage.find_downloads([other["video_id"]], other["extractor"]) == {}
    # Not to be taken for a YouTube video with that ID.
    assert storage.find_downloads([seen["video_id"]]) == {}

"""Links to sites other than YouTube, downloaded with the real yt-dlp and ffmpeg from web sites on this machine."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Optional

import pytest

from conftest import needs_ffmpeg
from offline_web import LocalSite, new_site_fixture, web_media_fixture  # noqa: F401
from offline_youtube import duration_of
from videocap.downloader import UrlKeyedIdPP, run_download
from videocap.inspector import inspect_link
from videocap.options import AccessLimits, DownloadOptions, options_from_spec
from videocap.settings import DownloadSettings, JobSpec


pytestmark = needs_ffmpeg
NewSite = Callable[[], LocalSite]
# What the app adds to an ID yt-dlp took from a file's or a page's name.
KEYED_ID_RE = re.compile(r".+-[0-9a-f]{8}")


class Progress:
    def progress(self, value: float, text: str = "") -> None:
        pass


def owner_options(url: str, output_dir: Path, archive: Optional[Path] = None, **settings: Any) -> DownloadOptions:
    spec = JobSpec(url=url, settings=DownloadSettings(**settings), output_dir=str(output_dir))
    return options_from_spec(spec, AccessLimits(), archive_path=archive)


def download(url: str, output_dir: Path, archive: Optional[Path] = None, **settings: Any) -> Any:
    return run_download(url, owner_options(url, output_dir, archive, **settings), Progress())


def media_files(folder: Path) -> list[Path]:
    return sorted(path for path in folder.iterdir() if path.suffix in {".mp4", ".mkv", ".webm"})


# What the owner can download


def test_a_direct_link_downloads(new_site: NewSite, tmp_path: Path) -> None:
    site = new_site()
    outcome = download(site.add_clip("clip.mp4"), tmp_path)
    assert outcome["errors"] == []
    item = outcome["items"][0]
    assert Path(item["path"]).name == "clip.mp4" and duration_of(Path(item["path"])) == pytest.approx(2, abs=0.3)


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

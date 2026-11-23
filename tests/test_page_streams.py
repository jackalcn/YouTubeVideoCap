"""Pages whose player keeps the address of its stream in a packed script: what is read from them, and the stream that is
then inspected and downloaded in the page's name, from web sites on this machine."""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

import pytest

import videocap.page_streams as page_streams
from conftest import needs_ffmpeg
from fake_browser import browser_fixture, real_handler, recording, stand_in_handler  # noqa: F401
from offline_web import LocalSite, new_site_fixture, pack, web_media_fixture  # noqa: F401
from offline_youtube import duration_of
from videocap import impersonation
from videocap.downloader import BROWSER_RETRY_FAILED_NOTE, BROWSER_RETRY_WORKED_NOTE, PAGE_STREAM_MESSAGE, PAGE_STREAM_NOTE, run_download
from videocap.errors import UNREADABLE_LINK, failure_kind
from videocap.inspector import PAGE_STREAM_DETAIL, PAGE_STREAM_FOUND_BUT_FAILED, inspect_link
from videocap.options import AccessLimits, DownloadOptions, LoginSource, build_base_options, options_from_spec
from videocap.page_streams import (
    PACKED_SCRIPT_LIMIT,
    PageStream,
    find_stream_addresses,
    find_streams,
    holds_no_video,
    page_thumbnail,
    page_title,
    read_page,
    unpacked_scripts,
)
from videocap.settings import DownloadSettings, JobSpec


NewSite = Callable[[], LocalSite]
PAGE = "https://site.example/watch/1"
# An ID that says where it was found, as the app gives to what a page or a direct link led to.
KEYED_ID_RE = re.compile(r".+-[0-9a-f]{8}")
QUIET = {"quiet": True, "logger": impersonation.Silent()}
NOTHING_IN_PAGE = "這個網址裡找不到可以下載的影片"


class Progress:
    def __init__(self) -> None:
        self.texts: list[str] = []

    def progress(self, value: float, text: str = "") -> None:
        self.texts.append(text)


def owner_options(url: str, folder: Path, **settings: Any) -> DownloadOptions:
    spec = JobSpec(url=url, settings=DownloadSettings(**settings), output_dir=str(folder))
    return options_from_spec(spec, AccessLimits())


def download(url: str, folder: Path, progress: Optional[Progress] = None, **settings: Any) -> Any:
    return run_download(url, owner_options(url, folder, **settings), progress or Progress())


def inspect(url: str, folder: Path, **settings: Any) -> Any:
    return inspect_link(url, owner_options(url, folder, **settings))


def player_script(*addresses: str) -> str:
    """What a page's player says, with the addresses it asks for."""
    return "var player=new Player();" + "".join(f"player.load('{address}');" for address in addresses)


def pair(new_site: NewSite) -> tuple[LocalSite, LocalSite]:
    """A site of pages and a site of streams, which lets in only the visitors of those pages."""
    page_site, stream_site = new_site(), new_site()
    stream_site.refer_only_from(page_site)
    return page_site, stream_site


@pytest.fixture(name="plain_site")
def plain_site_fixture(tmp_path: Path) -> Iterator[LocalSite]:
    """A site of text files, which needs no ffmpeg."""
    (tmp_path / "site").mkdir()
    site = LocalSite(tmp_path / "site", {})
    yield site
    site.close()


# Unpacking


SOURCE = "var source='https://cdn.example/abc-123/playlist.m3u8';var low=\"https:\\/\\/cdn.example\\/abc-123\\/360p\\/video.m3u8\";"


@pytest.mark.parametrize("base", [10, 36, 62])
def test_a_packed_script_unpacks_to_the_code_it_was_made_from(base: int) -> None:
    assert list(unpacked_scripts(f"<html><script>{pack(SOURCE, base)}</script></html>")) == [SOURCE]


def test_every_packed_script_of_a_page_is_unpacked_each_by_itself() -> None:
    # Two on one line must not run into each other.
    page = f"<script>{pack('var a=1;')}</script><p>text</p><script>{pack('var b=2;', 62)}</script>"
    assert list(unpacked_scripts(page)) == ["var a=1;", "var b=2;"]


def test_a_word_left_empty_in_the_list_is_its_own_code() -> None:
    # The packer leaves out a word that is the same as the code it would be given.
    page = "<script>eval(function(p,a,c,k,e,d){}('0 1 2',10,3,'x||z'.split('|'),0,{}))</script>"
    assert list(unpacked_scripts(page)) == ["x 1 z"]


@pytest.mark.parametrize(
    "script",
    [
        "eval(function(p,a,c,k,e,d){}('0',1,1,'x'.split('|'),0,{}))",
        "eval(function(p,a,c,k,e,d){}('0',99,1,'x'.split('|'),0,{}))",
        "eval(function(p,a,c,k,e,d){} nothing that the packer wrote after it",
    ],
    ids=["a base of one", "a base past the packer's digits", "no call at all"],
)
def test_a_script_that_is_not_the_packers_is_left_alone(script: str) -> None:
    assert list(unpacked_scripts(f"<script>{script}</script>")) == []


def test_a_script_that_would_unpack_to_too_much_is_left_alone() -> None:
    # One long word, written out again at every place a short code stands for it.
    word = "x" * (PACKED_SCRIPT_LIMIT // 2 - 1000)
    page = f"<script>eval(function(p,a,c,k,e,d){{}}('{' '.join(['0'] * 20)}',10,1,'{word}'.split('|'),0,{{}}))</script>"
    assert len(page) < PACKED_SCRIPT_LIMIT and list(unpacked_scripts(page)) == []
    # And a small one after it is still read.
    assert list(unpacked_scripts(page + f"<script>{pack('var a=1;')}</script>")) == ["var a=1;"]


# Finding the stream


def test_the_addresses_in_packed_scripts_are_found_whole_and_a_streams_own_playlist_comes_first() -> None:
    code = (
        "var a='https://cdn.example/id/720p/video.m3u8';"
        'var b="https:\\/\\/cdn.example\\/id\\/playlist.m3u8?t=1";'
        "var c='/rel/index.m3u8';"
        "var d='//other.example/x/y.m3u8';"
        "var e='https://cdn.example/preview.mp4';"
        "var f='https://cdn.example/id/playlist.m3u8?t=1';"
    )
    assert find_stream_addresses(f"<script>{pack(code)}</script>", PAGE) == [
        "https://cdn.example/id/playlist.m3u8?t=1",
        "https://site.example/rel/index.m3u8",
        "https://other.example/x/y.m3u8",
        "https://cdn.example/id/720p/video.m3u8",
    ]


@pytest.mark.parametrize(
    "code",
    [
        "var a=1;var preview='https://cdn.example/clip.mp4';",
        "var ad='ftp://cdn.example/ad.m3u8';var script='javascript:ad.m3u8';",
        "var here='https://site.example/watch/1';",
        "var words='an m3u8 playlist';",
    ],
    ids=["no playlist", "not an http address", "only the page itself", "not an address"],
)
def test_a_packed_script_without_a_playlist_gives_nothing_away(code: str) -> None:
    assert find_stream_addresses(f"<script>{pack(code)}</script>", PAGE) == []


def test_an_address_written_in_the_page_itself_is_the_job_of_ytdlps_own_extractor() -> None:
    page = "<html><script>var player='https://cdn.example/id/playlist.m3u8';</script></html>"
    assert find_stream_addresses(page, PAGE) == []


def test_a_page_is_not_trusted_with_more_than_a_few_tries(monkeypatch: pytest.MonkeyPatch) -> None:
    addresses = [f"https://cdn.example/{number}/index.m3u8" for number in range(10)]
    monkeypatch.setattr(page_streams, "read_page", lambda *args: f"<script>{pack(player_script(*addresses))}</script>")
    assert [stream.address for stream in find_streams({}, PAGE, True)] == addresses[:3]


# What the page says of its video


def test_the_pages_own_title_and_picture_name_the_video() -> None:
    page = (
        "<html><head><title> The  page &amp; its site </title>"
        '<meta property="og:title" content="What the video is called">'
        '<meta property="og:image" content="/img/cover.jpg"></head></html>'
    )
    assert page_title(page) == "What the video is called"
    assert page_thumbnail(page, PAGE) == "https://site.example/img/cover.jpg"


def test_a_page_without_a_title_in_its_meta_tags_is_named_by_its_title_tag() -> None:
    assert page_title("<title>\n  A   title\n  &amp; more </title>") == "A title & more"
    assert page_title("<p>no title</p>") == "" and page_thumbnail("<p>none</p>", PAGE) == ""


def test_a_picture_that_is_no_web_address_is_not_used() -> None:
    assert page_thumbnail('<meta property="og:image" content="data:image/png;base64,AAAA">', PAGE) == ""


def test_a_title_is_not_longer_than_a_file_name_can_bear() -> None:
    assert len(page_title(f"<title>{'long ' * 200}</title>")) <= 150


# What the page's player would send


@pytest.mark.parametrize(
    ("page", "stream", "sent"),
    [
        # To another site, as a browser sends it: only where the page is, and where the request comes from.
        ("https://site.example/watch/1?x=1#top", "https://cdn.example/a.m3u8", {"Referer": "https://site.example/", "Origin": "https://site.example"}),
        ("https://site.example:8080/watch/1", "https://site.example/a.m3u8", {"Referer": "https://site.example:8080/", "Origin": "https://site.example:8080"}),
        # To the page's own site, the page itself, as it was without the part after "#".
        ("https://site.example/watch/1?x=1#top", "https://site.example/hls/a.m3u8", {"Referer": "https://site.example/watch/1?x=1"}),
    ],
    ids=["another site", "another port", "the same site"],
)
def test_a_players_requests_carry_the_page_as_a_browser_names_it(page: str, stream: str, sent: dict[str, str]) -> None:
    assert PageStream(page, stream).headers() == sent


def test_only_the_owners_stream_run_carries_the_pages_headers() -> None:
    stream = PageStream(PAGE, "https://cdn.example/a.m3u8")
    sent = build_base_options(impersonation.Silent(), 3, LoginSource(), other_sites=True, page_stream=stream)["http_headers"]
    assert sent == {"Referer": "https://site.example/", "Origin": "https://site.example"}
    # A visitor, or a run for no page, asks as yt-dlp always has.
    assert "http_headers" not in build_base_options(impersonation.Silent(), 3, LoginSource(), other_sites=False, page_stream=stream)
    assert "http_headers" not in build_base_options(impersonation.Silent(), 3, LoginSource(), other_sites=True)


# Which failures send the app into the page


@pytest.mark.parametrize(
    ("url", "errors", "expected"),
    [
        ("https://example.com/watch/1", ["ERROR: Unsupported URL: https://example.com/watch/1"], True),
        ("https://example.com/watch/1", ["ERROR: No suitable extractor found for URL https://example.com/watch/1"], True),
        # As the app has already put it, once, in its own words.
        ("https://example.com/watch/1", [f"{NOTHING_IN_PAGE}：yt-dlp 不支援這個網站，或這個網頁裡沒有影片。（原始訊息：Unsupported URL: x）"], True),
        ("https://example.com/watch/1", ["ERROR: [generic] 1: Unable to download webpage: HTTP Error 403: Forbidden"], False),
        ("https://example.com/watch/1", ["ERROR: Unsupported URL: https://example.com/watch/1", "ERROR: [generic] 1: HTTP Error 403: Forbidden"], False),
        ("https://example.com/watch/1", [], False),
        # A stream's playlist or a file is not a page; there is no script to read.
        ("https://example.com/hls/index.m3u8", ["ERROR: Unsupported URL: https://example.com/hls/index.m3u8"], False),
        ("https://example.com/clip.mp4", ["ERROR: Unsupported URL: https://example.com/clip.mp4"], False),
        ("https://www.youtube.com/watch?v=abcdefghijk", ["ERROR: Unsupported URL: https://www.youtube.com/watch?v=abcdefghijk"], False),
    ],
)
def test_only_a_page_that_holds_no_video_gets_its_scripts_read(url: str, errors: list[str], expected: bool) -> None:
    assert holds_no_video(url, errors) is expected


def test_yt_dlps_word_for_no_video_has_a_kind_of_its_own() -> None:
    assert failure_kind("ERROR: Unsupported URL: https://example.com/about") == UNREADABLE_LINK


# Reading a page


def test_a_page_is_read_as_the_run_that_found_nothing_in_it_read_it(plain_site: LocalSite) -> None:
    url = plain_site.write_page("watch.html", "Seen", "<p>player</p>")
    assert "<title>Seen</title>" in read_page(QUIET, url, True)


@pytest.mark.parametrize("name", ["data.json", "clip.mp4", "missing.html", "forbidden/page.html"])
def test_what_is_not_a_page_that_can_be_read_is_nothing(plain_site: LocalSite, name: str) -> None:
    for path in ("data.json", "clip.mp4", "forbidden/page.html"):
        (plain_site.root / path).parent.mkdir(parents=True, exist_ok=True)
        (plain_site.root / path).write_bytes(b'{"a": 1}')
    assert read_page(QUIET, plain_site.url(name), True) == ""


def test_a_page_that_cannot_be_reached_is_nothing_either() -> None:
    assert read_page(QUIET, "http://127.0.0.1:9/page.html", True) == ""


def test_a_page_that_the_check_for_bots_turns_away_once_is_read_at_the_second_asking(plain_site: LocalSite, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(page_streams, "CHECK_PAUSE_SECONDS", 0)
    url = plain_site.write_page("flaky/watch.html", "Seen", "<p>player</p>")
    assert "<title>Seen</title>" in read_page(QUIET, url, True)
    assert plain_site.asked_for("flaky/watch.html") == 2


def test_a_page_that_always_wants_a_person_is_asked_for_twice_and_then_left(plain_site: LocalSite, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(page_streams, "CHECK_PAUSE_SECONDS", 0)
    url = plain_site.write_page("captcha/watch.html", "Never", "<p>player</p>")
    assert read_page(QUIET, url, True) == "" and plain_site.asked_for("captcha/watch.html") == 2


def test_a_refusal_that_is_no_check_is_not_asked_for_again(plain_site: LocalSite, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(page_streams, "CHECK_PAUSE_SECONDS", 0)
    url = plain_site.write_page("forbidden/watch.html", "No", "<p>player</p>")
    assert read_page(QUIET, url, True) == "" and plain_site.asked_for("forbidden/watch.html") == 1


def test_the_pause_before_the_second_asking_is_a_pause(plain_site: LocalSite, monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr(page_streams.time, "sleep", slept.append)
    read_page(QUIET, plain_site.write_page("captcha/watch.html", "Never", "<p>x</p>"), True)
    assert slept == [page_streams.CHECK_PAUSE_SECONDS] and page_streams.CHECK_PAUSE_SECONDS > 0


# A page and its stream, on two sites of this machine


@needs_ffmpeg
def test_a_stream_found_in_a_pages_script_downloads_in_the_pages_name(new_site: NewSite, tmp_path: Path) -> None:
    page_site, stream_site = pair(new_site)
    stream = stream_site.add_stream("referred/hls")
    url = page_site.add_packed_page("watch.html", player_script(stream), title="Seen on the page", image="/cover.jpg")

    progress = Progress()
    outcome = download(url, tmp_path, progress)
    assert outcome["errors"] == []
    item = outcome["items"][0]
    saved = Path(item["path"])
    # Named for the page, which says what the video is, not for a playlist called "index".
    assert saved.stem.startswith("Seen on the page") and duration_of(saved) == pytest.approx(2, abs=0.5)
    assert item["title"] == "Seen on the page" and item["webpage_url"] == url and item["thumbnail"] == page_site.url("cover.jpg")
    assert item["extractor"] == "generic" and KEYED_ID_RE.fullmatch(item["video_id"]) and item["video_id"].startswith("index-")
    assert PAGE_STREAM_NOTE.format(host="127.0.0.1") in outcome["notes"] and PAGE_STREAM_MESSAGE in progress.texts
    # The playlist and every segment were asked for as the page's own player asks: another site, so only where it is.
    segments = stream_site.segments("referred/hls")
    assert segments and all(stream_site.referers_for(segment) == [(page_site.base + "/", page_site.base)] for segment in segments)
    assert set(stream_site.referers_for("referred/hls/index.m3u8")) == {(page_site.base + "/", page_site.base)}
    # Without the page as the Referer, the same stream is refused.
    assert inspect(stream, tmp_path)["error"].startswith("這個網站拒絕了下載請求（HTTP 403）")


@needs_ffmpeg
def test_the_pages_inspection_and_its_download_agree(new_site: NewSite, tmp_path: Path) -> None:
    page_site, stream_site = pair(new_site)
    url = page_site.add_packed_page("watch.html", player_script(stream_site.add_stream("referred/hls")), title="Seen on the page", image="/cover.jpg")

    report = inspect(url, tmp_path)
    assert report["error"] == "" and report["url"] == url and report["title"] == "Seen on the page"
    assert report["thumbnail"] == page_site.url("cover.jpg") and report["is_playlist"] is False
    assert report["site"] == "127.0.0.1（一般網頁）" and report["details"][0] == PAGE_STREAM_DETAIL
    assert report["format_rows"] and report["output_name"].startswith("Seen on the page")
    outcome = download(url, tmp_path)
    # The same ID, so the preview, the archive and the library mean one video.
    assert outcome["errors"] == [] and outcome["items"][0]["video_id"] == report["video_id"]


def test_a_pages_inspection_reads_the_stream_without_ffmpeg(new_site: NewSite, tmp_path: Path) -> None:
    page_site, stream_site = pair(new_site)
    url = page_site.add_packed_page("watch.html", player_script(stream_site.add_playlist("referred/hls/index.m3u8")), title="Seen on the page")
    report = inspect(url, tmp_path)
    assert report["error"] == "" and report["title"] == "Seen on the page" and report["url"] == url
    assert KEYED_ID_RE.fullmatch(report["video_id"])


def test_a_stream_on_the_pages_own_site_is_asked_for_by_the_whole_page(new_site: NewSite, tmp_path: Path) -> None:
    site = new_site()
    site.refer_only_from(site)
    url = site.add_packed_page("watch.html", player_script(site.add_playlist("referred/hls/index.m3u8")), title="One site")
    assert inspect(url, tmp_path)["error"] == ""
    assert {referer for referer, _ in site.referers_for("referred/hls/index.m3u8")} == {url}
    # The same site is no other site, and has no Origin to name.
    assert {origin for _, origin in site.referers_for("referred/hls/index.m3u8")} == {""}


def test_what_the_page_says_is_left_alone_when_it_gives_no_stream_away(new_site: NewSite, tmp_path: Path) -> None:
    site = new_site()
    for url in (site.write_page("nothing.html", "Nothing", "<p>no player</p>"), site.add_packed_page("packed.html", "var a=1;var b='https://x.example/a.mp4';")):
        report, outcome = inspect(url, tmp_path), download(url, tmp_path)
        for error in (report["error"], *outcome["errors"]):
            assert error.startswith(NOTHING_IN_PAGE) and failure_kind(error) == UNREADABLE_LINK
        assert outcome["items"] == [] and not any("播放器腳本" in note for note in outcome["notes"])


@needs_ffmpeg
def test_a_page_with_a_video_ytdlp_can_see_is_downloaded_as_it_always_was(new_site: NewSite, tmp_path: Path) -> None:
    page_site, stream_site = pair(new_site)
    page_site.add_clip("clip.mp4")
    # A <video> tag the generic extractor reads, and a packed script that it does not. The page's own video wins.
    script = pack(player_script(stream_site.add_playlist("referred/a.m3u8")))
    url = page_site.write_page("watch.html", "Both", f'<video src="clip.mp4" controls></video><script>{script}</script>')
    outcome = download(url, tmp_path)
    assert outcome["errors"] == [] and not any("播放器腳本" in note for note in outcome["notes"])
    assert stream_site.requests == []


@needs_ffmpeg
def test_the_second_address_is_tried_when_the_first_cannot_be_read(new_site: NewSite, tmp_path: Path) -> None:
    page_site, stream_site = pair(new_site)
    working = stream_site.add_stream("referred/hls")
    # The other has the shorter path, so it is tried first, and refused.
    url = page_site.add_packed_page("watch.html", player_script(working, stream_site.url("forbidden/index.m3u8")), title="Second try")
    outcome = download(url, tmp_path)
    assert outcome["errors"] == [] and Path(outcome["items"][0]["path"]).stem.startswith("Second try")
    assert stream_site.asked_for("forbidden/index.m3u8") >= 1


def test_a_stream_that_cannot_be_read_is_what_the_page_is_told(new_site: NewSite, tmp_path: Path) -> None:
    page_site, stream_site = pair(new_site)
    url = page_site.add_packed_page("watch.html", player_script(stream_site.add_playlist("forbidden/index.m3u8")))
    report, outcome = inspect(url, tmp_path), download(url, tmp_path)
    assert report["url"] == url and report["error"].startswith(PAGE_STREAM_FOUND_BUT_FAILED)
    assert "HTTP 403" in report["error"] and outcome["items"] == [] and "HTTP 403" in outcome["errors"][0]
    # The note says where the stream came from, so the 403 is not taken for the page's own.
    assert any("播放器腳本" in note for note in outcome["notes"])


def test_the_owners_own_referer_has_the_last_word(new_site: NewSite, tmp_path: Path) -> None:
    page_site, stream_site = pair(new_site)
    url = page_site.add_packed_page("watch.html", player_script(stream_site.add_playlist("referred/hls/index.m3u8")))
    assert inspect(url, tmp_path)["error"] == ""
    report = inspect(url, tmp_path, ytdlp_extra_args="--referer http://elsewhere.example/")
    assert report["error"].startswith(PAGE_STREAM_FOUND_BUT_FAILED)
    # Over the page's, but not in place of the Origin that it also sends.
    assert ("http://elsewhere.example/", page_site.base) in stream_site.referers_for("referred/hls/index.m3u8")


def test_a_visitor_never_has_a_page_read_for_a_stream(new_site: NewSite, tmp_path: Path) -> None:
    page_site, stream_site = pair(new_site)
    url = page_site.add_packed_page("watch.html", player_script(stream_site.add_playlist("hls/index.m3u8")))
    options = replace(owner_options(url, tmp_path), other_sites=False)
    assert inspect_link(url, options)["error"] and run_download(url, options, Progress())["items"] == []
    assert stream_site.requests == []


def test_a_stream_that_was_found_in_a_page_is_not_looked_for_in_a_page_of_its_own(new_site: NewSite, tmp_path: Path) -> None:
    site = new_site()
    url = site.add_packed_page("again.html", player_script(site.add_playlist("hls/index.m3u8")))
    options = replace(owner_options(url, tmp_path), page_stream=PageStream("https://elsewhere.example/page", site.url("hls/index.m3u8")))
    assert inspect_link(url, options)["error"].startswith(NOTHING_IN_PAGE)
    assert run_download(url, options, Progress())["errors"][0].startswith(NOTHING_IN_PAGE)
    assert site.asked_for("hls/index.m3u8") == 0


# A page behind a check for browsers, whose stream also wants one


@needs_ffmpeg
def test_a_page_and_a_stream_that_both_want_a_browser_download_at_the_second_try(browser: Any, new_site: NewSite, tmp_path: Path) -> None:
    page_site, stream_site = pair(new_site)
    stream = stream_site.add_stream("browsers-only/referred/hls")
    url = page_site.add_packed_page("cf/watch.html", player_script(stream), title="Behind a check")

    report = inspect(url, tmp_path)
    assert report["error"] == "" and report["title"] == "Behind a check" and report["details"][0] == PAGE_STREAM_DETAIL
    outcome = download(url, tmp_path)
    assert outcome["errors"] == [] and Path(outcome["items"][0]["path"]).stem.startswith("Behind a check")
    # The page was refused to a program once for the inspection and once for the download, and read as a browser every
    # other time: by yt-dlp's second try, and by the look into its script.
    assert page_site.asked_for("cf/watch.html") - page_site.asked_as_a_browser("cf/watch.html") == 2
    # The stream was asked for as a browser from the start: none of it as a program first.
    segments = stream_site.segments("browsers-only/referred/hls")
    assert segments and all(stream_site.asked_for(segment) == stream_site.asked_as_a_browser(segment) for segment in segments)
    # What the first try at the page failed with is not the end of the story, and is not told as if it were.
    assert BROWSER_RETRY_FAILED_NOTE not in outcome["notes"] and any("播放器腳本" in note for note in outcome["notes"])


@needs_ffmpeg
def test_a_stream_that_wants_a_browser_after_an_open_page_is_tried_again_as_one(browser: Any, new_site: NewSite, tmp_path: Path) -> None:
    page_site, stream_site = pair(new_site)
    url = page_site.add_packed_page("watch.html", player_script(stream_site.add_stream("browsers-only/referred/hls")), title="Open page")
    outcome = download(url, tmp_path)
    assert outcome["errors"] == [] and BROWSER_RETRY_WORKED_NOTE in outcome["notes"]
    assert any("播放器腳本" in note for note in outcome["notes"]) and Path(outcome["items"][0]["path"]).stem.startswith("Open page")

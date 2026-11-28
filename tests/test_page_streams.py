"""Pages whose player keeps the address of its stream in a packed script: what is read from them, and the stream that is
then inspected and downloaded in the page's name, from web sites on this machine."""

from __future__ import annotations

import re
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

import pytest

import videocap.page_streams as page_streams
from conftest import needs_ffmpeg
from fake_browser import browser_fixture, real_handler, recording, stand_in_handler  # noqa: F401
from offline_web import CLEARANCE_COOKIE, LocalSite, new_site_fixture, pack, web_media_fixture  # noqa: F401
from offline_youtube import duration_of
import videocap.verification as verification
from videocap import impersonation
from videocap.downloader import (
    BROWSER_RETRY_FAILED_NOTE,
    BROWSER_RETRY_WORKED_NOTE,
    EMBEDDED_NOTE,
    EMBEDDED_STREAM_NOTE,
    PAGE_STREAM_MESSAGE,
    PAGE_STREAM_NOTE,
    PAGE_STREAM_TRY_MESSAGE,
    PageStreamPP,
    run_download,
    tell_of_human_check,
)
from videocap.errors import HUMAN_CHECK, REFERER_OVER_THE_PLAYERS, UNREADABLE_LINK, failure_kind, humanize_human_check, referer_advice
from videocap.inspector import inspect_link
from videocap.options import AccessLimits, DownloadOptions, LoginSource, build_base_options, options_for_candidate, options_from_spec
from videocap.page_streams import (
    CHECK_IS_ELSEWHERE,
    FRAME,
    FRAME_DETAIL,
    FRAME_FOUND_BUT_FAILED,
    FRAMED_STREAM_DETAIL,
    FRAMED_STREAM_FOUND_BUT_FAILED,
    IFRAME_SCAN,
    IFRAME_TRIES,
    OUT_OF_TIME,
    OUT_OF_TIME_AFTER,
    PACKED_SCRIPT_LIMIT,
    PAGE_STREAM_DETAIL,
    PAGE_STREAM_FOUND_BUT_FAILED,
    PLAIN,
    PLAIN_DETAIL,
    PLAIN_FOUND_BUT_FAILED,
    SEARCH_PAGES,
    STREAM,
    TRIED_ALL,
    Failure,
    PageStream,
    Reading,
    StreamSearch,
    find_addresses_in_iframe,
    find_stream_addresses,
    goes_through_a_proxy,
    holds_no_video,
    page_thumbnail,
    page_title,
    ranked_iframes,
    read_page,
    same_place,
    unpacked_scripts,
)
from videocap.page_streams import resolved_addresses as real_resolved_addresses  # Before conftest's stand-in takes its place.
from videocap.settings import DownloadSettings, JobSpec
from videocap.sites import host_of
from videocap.verification import SiteCookie, VerifiedSession


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


def test_an_iframes_own_plain_address_is_found_without_unpacking() -> None:
    page = "<p>player</p><script>var source='https://cdn.example/id/playlist.m3u8?t=1';</script>"
    assert find_addresses_in_iframe(page, PAGE) == ["https://cdn.example/id/playlist.m3u8?t=1"]


def test_an_iframes_html_escaped_address_is_read_as_the_one_it_escapes() -> None:
    page = '<a href="https://cdn.example/id/playlist.m3u8?a=1&amp;b=2">play</a>'
    assert find_addresses_in_iframe(page, PAGE) == ["https://cdn.example/id/playlist.m3u8?a=1&b=2"]


def test_an_iframes_packed_address_is_still_found() -> None:
    code = "var a='https://cdn.example/id/playlist.m3u8';"
    page = f"<script>{pack(code)}</script>"
    assert find_addresses_in_iframe(page, PAGE) == ["https://cdn.example/id/playlist.m3u8"]


# Which of a page's iframes may hold its player


def test_an_iframes_address_is_resolved_and_what_is_no_address_to_follow_is_left_out() -> None:
    page = iframes(
        'id="player" src="/player?x=1&amp;y=2"',
        "src='https://cdn.example/embed'",
        'src="javascript:void(0)"',
        'src="about:blank"',
        "",
        'src="https://cdn.example/embed"',
        'src="#"',
        f'src="{PAGE}#again"',
        'src="ftp://site.example/player"',
    )
    # Resolved against the page; a duplicate, the page itself and what is no web address dropped.
    assert ranked_iframes(page, PAGE) == ["https://site.example/player?x=1&y=2", "https://cdn.example/embed"]


def test_an_iframe_that_a_script_is_to_fill_in_later_is_read_where_the_script_would_read_it() -> None:
    page = iframes(
        'src="about:blank" data-src="/lazy/1"',
        'data-lazy-src="https://cdn.example/lazy/2"',
        'src="" data-original="/lazy/3"',
        'src="/real" data-src="/not-this"',
    )
    assert ranked_iframes(page, PAGE) == ["https://site.example/lazy/1", "https://cdn.example/lazy/2", "https://site.example/lazy/3", "https://site.example/real"]


def test_a_player_comes_before_the_advertisements_that_the_page_put_ahead_of_it() -> None:
    page = iframes(
        'src="https://ads.example/banner" width="300" height="250"',
        'src="https://ads.example/pop"',
        'src="https://frame.example/e/abc" allowfullscreen',
        'src="https://frame.example/f/def" allow="autoplay; encrypted-media"',
        'src="https://frame.example/embed/ghi" allow="fullscreen" width="640" height="360"',
    )
    assert ranked_iframes(page, PAGE) == [
        "https://frame.example/embed/ghi",
        "https://frame.example/e/abc",
        "https://ads.example/banner",
        "https://frame.example/f/def",
        "https://ads.example/pop",
    ]


@pytest.mark.parametrize(
    ("attributes", "kept"),
    [
        ('width="1" height="1"', False),
        ('width="0" height="0"', False),
        ('height="0"', False),
        ('style="width:1px;height:1px"', False),
        ('style="width: 0; height: 0"', False),
        ('style="border:0;width:1px !important"', False),
        # Nothing is known of the size of these, which is not the same as their having none.
        ("", True),
        ('width="100%" height="100%"', True),
        ('width="100%"', True),
        ('width="auto"', True),
        ('width="640"', True),
        ('style="width:100%;max-width:1px"', True),
        ('style="height:56.25vw"', True),
    ],
)
def test_only_an_iframe_that_gives_itself_no_more_than_a_pixel_is_left_out(attributes: str, kept: bool) -> None:
    assert ranked_iframes(iframes(f'src="/frame" {attributes}'), PAGE) == (["https://site.example/frame"] if kept else [])


def test_a_hidden_iframe_comes_last_and_is_not_left_out() -> None:
    page = iframes('src="/a" hidden', 'src="/b" style="display: none"', 'src="/c" style="visibility:hidden" allowfullscreen', 'src="/d"')
    assert ranked_iframes(page, PAGE) == [f"https://site.example/{name}" for name in ("d", "c", "a", "b")]


def test_only_so_many_of_a_pages_iframes_are_looked_at() -> None:
    page = iframes(*(f'src="/frame/{number}"' for number in range(IFRAME_SCAN + 5)))
    assert len(ranked_iframes(page, PAGE)) == IFRAME_SCAN


# Searching a page for its video, among pages that are text in a dictionary


FRAMED = "https://frame.example/embed/1"
DEEP = "https://deep.example/v/2"
STREAM_ADDRESS = "https://cdn.example/id/real.m3u8"
PLAYER_TEXT = f"<script>var source='{STREAM_ADDRESS}';</script>"
NO_VIDEO = f"{NOTHING_IN_PAGE}：yt-dlp 不支援這個網站，或這個網頁裡沒有影片。請確認貼的是影片所在網頁的完整網址。（原始訊息：Unsupported URL: x）"
REFUSED = "這個網站拒絕了下載請求（HTTP 403）。（原始訊息：HTTP Error 403: Forbidden）"


def iframes(*tags: str) -> str:
    """A page's <iframe>s, each given as the attributes of its tag."""
    return "".join(f"<iframe {attributes}></iframe>" for attributes in tags)


def searching(
    monkeypatch: pytest.MonkeyPatch,
    pages: dict[str, str],
    url: str = PAGE,
    opts: Optional[dict[str, Any]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    moved: Optional[dict[str, str]] = None,
) -> tuple[StreamSearch, list[str]]:
    """A search among pages that are only text here, and the addresses that it asked for, in order. `moved` says
    where the reading of an address really ended."""
    asked: list[str] = []

    def fake_read_page(ydl_opts: object, address: str, other_sites: bool, headers: object = None, stopped: object = None) -> Reading:
        asked.append(address)
        return Reading(pages.get(address, ""), (moved or {}).get(address, address))

    monkeypatch.setattr(page_streams, "read_page", fake_read_page)
    # No proxy, whatever this machine's settings are.
    return StreamSearch({"proxy": ""} if opts is None else opts, url, True, should_stop), asked


def all_failing(search: StreamSearch, error: str = NO_VIDEO) -> list[PageStream]:
    """Every candidate of a search whose every try fails the same way."""
    handed = []
    for candidate in search.candidates():
        handed.append(candidate)
        search.failed(candidate, error)
    return handed


def test_a_page_is_not_trusted_with_more_than_a_few_tries(monkeypatch: pytest.MonkeyPatch) -> None:
    addresses = [f"https://cdn.example/{number}/index.m3u8" for number in range(10)]
    search, _ = searching(monkeypatch, {PAGE: f"<script>{pack(player_script(*addresses))}</script>"})
    assert [(candidate.kind, candidate.address, candidate.frame) for candidate in all_failing(search)] == [(STREAM, address, "") for address in addresses[:3]]


def test_an_iframe_is_followed_only_when_the_page_gives_no_stream_away_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    own = f"<script>{pack(player_script('https://cdn.example/id/own.m3u8'))}</script>{iframes(f'src={FRAMED!r}')}"
    search, asked = searching(monkeypatch, {PAGE: own, FRAMED: PLAYER_TEXT})
    assert [candidate.address for candidate in all_failing(search)] == ["https://cdn.example/id/own.m3u8"] and asked == [PAGE]


def test_an_embedded_page_is_tried_by_its_own_address_before_anything_of_it_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    search, asked = searching(monkeypatch, {PAGE: "<title>Seen</title>" + iframes(f'src="{FRAMED}"'), FRAMED: PLAYER_TEXT})
    candidates = search.candidates()
    first = next(candidates)
    # For yt-dlp to read as it reads any page: asked for as the page's iframe, and nothing of it read yet.
    assert (first.kind, first.address, first.page_url, first.frame, first.title) == (FRAME, FRAMED, PAGE, FRAMED, "Seen") and asked == [PAGE]
    assert first.headers() == {"Referer": "https://site.example/"}
    search.failed(first, NO_VIDEO)
    second = next(candidates)
    # Only then its text, whose stream is asked for as the embedded page's own player would ask.
    assert asked == [PAGE, FRAMED] and (second.kind, second.address, second.page_url, second.frame) == (STREAM, STREAM_ADDRESS, PAGE, FRAMED)
    assert second.headers() == {"Referer": "https://frame.example/", "Origin": "https://frame.example"}
    assert list(candidates) == []


def test_a_page_embedded_two_deep_is_tried_too_and_one_embedded_deeper_is_not(monkeypatch: pytest.MonkeyPatch) -> None:
    deeper = "https://deeper.example/x"
    pages = {PAGE: iframes(f'src="{FRAMED}"'), FRAMED: iframes(f'src="{DEEP}"'), DEEP: iframes(f'src="{deeper}"'), deeper: PLAYER_TEXT}
    search, asked = searching(monkeypatch, pages)
    handed = all_failing(search)
    # The second is handed out though nothing in it names a stream: yt-dlp may know its player, or find a <video> in it.
    assert [(candidate.kind, candidate.address) for candidate in handed] == [(FRAME, FRAMED), (FRAME, DEEP)]
    assert handed[1].headers() == {"Referer": "https://frame.example/"} and asked == [PAGE, FRAMED, DEEP]


def test_a_stream_two_deep_is_asked_for_by_the_page_that_really_holds_its_player(monkeypatch: pytest.MonkeyPatch) -> None:
    search, _ = searching(monkeypatch, {PAGE: iframes(f'src="{FRAMED}"'), FRAMED: iframes(f'src="{DEEP}"'), DEEP: PLAYER_TEXT})
    last = all_failing(search)[-1]
    assert (last.kind, last.address, last.frame) == (STREAM, STREAM_ADDRESS, DEEP)
    assert last.headers() == {"Referer": "https://deep.example/", "Origin": "https://deep.example"}
    assert last.source() == FRAMED_STREAM_DETAIL.format(frame="deep.example")


def test_a_search_follows_only_so_many_iframes_of_a_document(monkeypatch: pytest.MonkeyPatch) -> None:
    frames = [f"https://frame.example/embed/{number}" for number in range(IFRAME_TRIES + 2)]
    search, asked = searching(monkeypatch, {PAGE: iframes(*(f'src="{frame}"' for frame in frames))})
    assert [candidate.address for candidate in all_failing(search)] == frames[:IFRAME_TRIES] and asked == [PAGE, *frames[:IFRAME_TRIES]]


def test_a_search_reads_only_so_many_embedded_pages_and_hands_out_only_so_many_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    inner = [f"https://deep.example/v/{number}" for number in range(4)]
    outer = ["https://frame.example/embed/a", "https://frame.example/embed/b"]
    pages = {PAGE: iframes(*(f'src="{frame}"' for frame in outer)), outer[0]: iframes(f'src="{inner[0]}"', f'src="{inner[1]}"'), outer[1]: iframes(f'src="{inner[2]}"', f'src="{inner[3]}"')}
    # Left alone, two iframes of the page and two of each of those: six pages.
    search, asked = searching(monkeypatch, pages)
    assert len(all_failing(search)) == 6 and len(asked) == 1 + 6 and SEARCH_PAGES == 6
    monkeypatch.setattr(page_streams, "SEARCH_PAGES", 2)
    search, asked = searching(monkeypatch, pages)
    # A page that is not read is still handed out for yt-dlp to try, but what it embeds is never learned.
    assert [candidate.address for candidate in all_failing(search)] == [outer[0], inner[0], inner[1], outer[1]] and asked == [PAGE, outer[0], inner[0]]
    monkeypatch.setattr(page_streams, "SEARCH_TRIES", 3)
    search, asked = searching(monkeypatch, pages)
    assert [candidate.address for candidate in all_failing(search)] == [outer[0], inner[0], inner[1]]


def test_a_page_that_embeds_itself_or_what_was_followed_already_is_not_followed_round(monkeypatch: pytest.MonkeyPatch) -> None:
    search, asked = searching(monkeypatch, {PAGE: iframes(f'src="{FRAMED}"'), FRAMED: iframes(f'src="{FRAMED}"', f'src="{PAGE}#top"', f'src="{DEEP}"'), DEEP: iframes(f'src="{FRAMED}"')})
    assert [candidate.address for candidate in all_failing(search)] == [FRAMED, DEEP] and asked == [PAGE, FRAMED, DEEP]


def test_an_embedded_youtube_player_is_tried_as_a_link_to_youtube_and_none_of_it_is_read(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    youtube = "https://www.youtube.com/embed/abcdefghijk"
    search, asked = searching(monkeypatch, {PAGE: "<title>Seen</title>" + iframes(f'src="{youtube}"', f'src="{FRAMED}"')})
    plain, framed = all_failing(search)
    assert (plain.kind, plain.address, plain.title, plain.source()) == (PLAIN, youtube, "", PLAIN_DETAIL) and asked == [PAGE, FRAMED]
    # Its run is a link's own: nothing of the page goes with it. The other page's run is the page's.
    options = owner_options(PAGE, tmp_path)
    assert options_for_candidate(options, plain).page_stream is None and options_for_candidate(options, framed).page_stream == framed
    # And should a stream ever be YouTube's, the page's headers still stay behind.
    assert "http_headers" not in build_base_options(impersonation.Silent(), 3, LoginSource(), other_sites=True, page_stream=PageStream(PAGE, youtube))


@pytest.mark.parametrize(("extractor", "renamed"), [("Generic", True), ("HTML5MediaEmbed", True), ("Youtube", False), ("Vimeo", False)])
def test_only_what_ytdlp_names_after_a_page_is_given_the_pages_name(extractor: str, renamed: bool) -> None:
    found = {"id": "abc", "title": "Its own", "thumbnail": "https://there.example/own.jpg", "webpage_url": "https://there.example/v/abc", "extractor_key": extractor}
    _, named = PageStreamPP(PageStream(PAGE, FRAMED, "The page's", "https://site.example/cover.jpg", kind=FRAME, frame=FRAMED)).run(dict(found))
    # A site's own extractor read the video of a player the page embeds: it is that site's, as for a link to it.
    assert named == ({**found, "title": "The page's", "webpage_url": PAGE, "original_url": PAGE} if renamed else found)


# What a page of the outside may lead to


@pytest.mark.parametrize("host", ["127.0.0.1", "192.168.1.1", "localhost", "nas", "[::1]", "2130706433", "0x7f.0.0.1", "169.254.169.254"])
def test_a_page_of_the_outside_is_not_followed_into_this_machine_or_its_network(monkeypatch: pytest.MonkeyPatch, host: str) -> None:
    # By an iframe, by a stream that its own packed script names, and by a stream that a page it embeds names.
    search, asked = searching(monkeypatch, {PAGE: iframes(f'src="http://{host}/player"')})
    assert all_failing(search) == [] and asked == [PAGE]
    search, asked = searching(monkeypatch, {PAGE: f"<script>{pack(player_script(f'http://{host}/hls/index.m3u8'))}</script>"})
    assert all_failing(search) == [] and asked == [PAGE]
    search, asked = searching(monkeypatch, {PAGE: iframes(f'src="{FRAMED}"'), FRAMED: f"<script>var source='http://{host}:8080/hls/index.m3u8';</script>"})
    assert [candidate.address for candidate in all_failing(search)] == [FRAMED]


def test_a_page_of_this_network_may_lead_where_it_likes(monkeypatch: pytest.MonkeyPatch) -> None:
    page, frame = "http://192.168.1.10/watch", "http://192.168.1.20/player"
    search, asked = searching(monkeypatch, {page: iframes(f'src="{frame}"'), frame: "<script>var source='http://nas/hls/index.m3u8';</script>"}, url=page)
    assert [candidate.address for candidate in all_failing(search)] == [frame, "http://nas/hls/index.m3u8"]


def test_a_name_that_resolves_into_this_network_is_not_followed_either(monkeypatch: pytest.MonkeyPatch) -> None:
    sneaky = "https://sneaky.example/player"
    looked_up: list[str] = []
    # One address of this network among a name's addresses is enough.
    monkeypatch.setattr(page_streams, "resolved_addresses", lambda host: looked_up.append(host) or (("93.184.216.35", "10.0.0.5") if host == "sneaky.example" else ("93.184.216.34",)))
    pages = {PAGE: iframes(f'src="{sneaky}"', f'src="{FRAMED}"'), FRAMED: PLAYER_TEXT}
    search, asked = searching(monkeypatch, pages)
    assert [candidate.address for candidate in all_failing(search)] == [FRAMED, STREAM_ADDRESS] and sneaky not in asked
    # Each name once, however often it is met.
    assert sorted(looked_up) == ["cdn.example", "frame.example", "site.example", "sneaky.example"]


def test_a_name_is_not_looked_up_past_a_proxy_which_looks_it_up_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    looked_up: list[str] = []
    monkeypatch.setattr(page_streams, "resolved_addresses", lambda host: looked_up.append(host) or ("10.0.0.5",))
    search, _ = searching(monkeypatch, {PAGE: iframes(f'src="{FRAMED}"', 'src="http://192.168.1.1/player"')}, opts={"proxy": "http://proxy.example:8080"})
    # What is written as an address of this network is still not followed.
    assert [candidate.address for candidate in all_failing(search)] == [FRAMED] and looked_up == []


def test_whether_a_run_goes_through_a_proxy_is_what_ytdlp_would_make_of_its_options(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(page_streams.urllib.request, "getproxies", lambda: {"https": "http://system.example:3128"})
    assert goes_through_a_proxy({}) and goes_through_a_proxy({"proxy": "socks5://127.0.0.1:1080"}) and not goes_through_a_proxy({"proxy": ""})
    monkeypatch.setattr(page_streams.urllib.request, "getproxies", lambda: {})
    assert not goes_through_a_proxy({})


def test_a_page_that_a_redirect_took_into_this_network_is_not_read(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = {PAGE: iframes(f'src="{FRAMED}"'), FRAMED: "<script>var source='/hls/index.m3u8';</script>"}
    search, _ = searching(monkeypatch, pages, moved={FRAMED: "http://192.168.1.5/player"})
    assert [candidate.address for candidate in all_failing(search)] == [FRAMED]
    # Moved to another address of the outside, it is read as the page that it turned out to be.
    search, _ = searching(monkeypatch, pages, moved={FRAMED: "https://deep.example/player/9"})
    last = all_failing(search)[-1]
    assert (last.address, last.frame, last.headers()) == ("https://deep.example/hls/index.m3u8", "https://deep.example/player/9", {"Referer": "https://deep.example/player/9"})


# Finding again a stream whose address stopped being served


@pytest.mark.parametrize(
    ("other", "same"),
    [
        ("https://cdn.example/id/real.m3u8?token=two&expires=9", True),
        ("https://CDN.example/id/real.m3u8", True),
        ("https://cdn.example/id/real.m3u8#later", True),
        ("https://cdn.example/id/other.m3u8?token=one", False),
        ("https://other.example/id/real.m3u8?token=one", False),
        ("http://cdn.example/id/real.m3u8?token=one", False),
    ],
)
def test_two_addresses_are_of_one_thing_whatever_comes_after_the_question_mark(other: str, same: bool) -> None:
    assert same_place(f"{STREAM_ADDRESS}?token=one", other) is same


def test_a_candidate_remembers_the_way_that_led_to_it(monkeypatch: pytest.MonkeyPatch) -> None:
    search, _ = searching(monkeypatch, {PAGE: iframes(f'src="{FRAMED}"'), FRAMED: iframes(f'src="{DEEP}"'), DEEP: PLAYER_TEXT})
    outer, inner, stream = all_failing(search)
    # Each embedded page on the way down, as the page above it gave it; none for what the page itself gave.
    assert (outer.route, inner.route, stream.route) == ((), (FRAMED,), (FRAMED, DEEP))
    search, _ = searching(monkeypatch, {PAGE: f"<script>{pack(player_script(STREAM_ADDRESS))}</script>"})
    assert all_failing(search)[0].route == ()


def test_a_stream_whose_address_ran_out_is_found_again_where_it_was_with_what_the_page_gives_it_now(monkeypatch: pytest.MonkeyPatch) -> None:
    first, second = f"{FRAMED}?payload=1", f"{FRAMED}?payload=2"
    pages = {PAGE: "<title>Seen</title>" + iframes(f'src="{first}"'), first: f"<script>var source='{STREAM_ADDRESS}?token=one';</script>"}
    search, asked = searching(monkeypatch, pages)
    stream = all_failing(search)[-1]
    assert stream.address == f"{STREAM_ADDRESS}?token=one"
    # By now the page embeds its player by another address, and the player names the stream with another token.
    pages.clear()
    pages[PAGE] = iframes('src="https://ads.example/embed/9" allowfullscreen', f'src="{second}"')
    pages[second] = f"<script>var other='https://cdn.example/id/else.m3u8?token=x';var source='{STREAM_ADDRESS}?token=two';</script>"
    del asked[:]
    fresh = search.refreshed(replace(stream, pass_query=True))
    assert fresh is not None and fresh.address == f"{STREAM_ADDRESS}?token=two"
    # The same candidate otherwise: of the same page, named as before, asked for as the player's own page would ask.
    assert (fresh.kind, fresh.page_url, fresh.title, fresh.pass_query, fresh.route) == (STREAM, PAGE, "Seen", True, stream.route)
    assert (fresh.frame, fresh.referer) == (second, second) and fresh.headers() == {"Referer": "https://frame.example/", "Origin": "https://frame.example"}
    # Only the way to it is read again: the page and the page on the way, and none other of the page's iframes.
    assert asked == [PAGE, second]


def test_a_page_whose_own_script_named_the_stream_is_asked_for_it_again(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = {PAGE: f"<script>{pack(player_script(STREAM_ADDRESS + '?token=one'))}</script>"}
    search, _ = searching(monkeypatch, pages)
    (stream,) = all_failing(search)
    pages[PAGE] = f"<script>{pack(player_script(STREAM_ADDRESS + '?token=two'))}</script>"
    assert search.refreshed(stream) == replace(stream, address=f"{STREAM_ADDRESS}?token=two")


def test_an_embedded_page_that_ytdlp_read_is_found_again_by_its_place_in_the_page(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = {PAGE: iframes(f'src="{FRAMED}?payload=1"')}
    search, _ = searching(monkeypatch, pages)
    (frame,) = all_failing(search)
    pages[PAGE] = iframes(f'src="{FRAMED}?payload=2"')
    fresh = search.refreshed(frame)
    assert fresh is not None and (fresh.kind, fresh.address, fresh.frame, fresh.referer) == (FRAME, f"{FRAMED}?payload=2", f"{FRAMED}?payload=2", PAGE)


@pytest.mark.parametrize(
    "now",
    [
        {},
        {PAGE: "<p>nothing embedded</p>"},
        {PAGE: iframes(f'src="{FRAMED}"')},
        {PAGE: iframes(f'src="{FRAMED}"'), FRAMED: "<p>no player</p>"},
        {PAGE: iframes(f'src="{FRAMED}"'), FRAMED: "<script>var source='https://cdn.example/id/other.m3u8';</script>"},
    ],
    ids=["the page cannot be read", "the page embeds nothing", "the embedded page cannot be read", "it names no stream", "it names another stream"],
)
def test_a_stream_that_the_page_no_longer_leads_to_is_not_found_again(monkeypatch: pytest.MonkeyPatch, now: dict[str, str]) -> None:
    pages = {PAGE: iframes(f'src="{FRAMED}"'), FRAMED: PLAYER_TEXT}
    search, _ = searching(monkeypatch, pages)
    stream = all_failing(search)[-1]
    pages.clear()
    pages.update(now)
    assert search.refreshed(stream) is None


def test_a_way_that_a_redirect_now_takes_into_this_network_is_not_gone_again(monkeypatch: pytest.MonkeyPatch) -> None:
    moved: dict[str, str] = {}
    search, _ = searching(monkeypatch, {PAGE: iframes(f'src="{FRAMED}"'), FRAMED: PLAYER_TEXT}, moved=moved)
    stream = all_failing(search)[-1]
    assert search.refreshed(stream) == stream
    moved[FRAMED] = "http://192.168.1.5/player"
    assert search.refreshed(stream) is None


def test_a_link_tried_by_itself_and_a_stopped_job_have_nothing_found_again(monkeypatch: pytest.MonkeyPatch) -> None:
    stop: list[bool] = []
    pages = {PAGE: iframes('src="https://www.youtube.com/embed/abcdefghijk"', f'src="{FRAMED}"'), FRAMED: PLAYER_TEXT}
    search, asked = searching(monkeypatch, pages, should_stop=lambda: bool(stop))
    plain, _, stream = all_failing(search)
    del asked[:]
    assert search.refreshed(plain) is None and asked == []
    stop.append(True)
    assert search.refreshed(stream) is None and asked == []


def test_finding_a_stream_again_is_not_counted_against_the_time_of_the_search(monkeypatch: pytest.MonkeyPatch) -> None:
    search, asked = searching(monkeypatch, {PAGE: iframes(f'src="{FRAMED}"'), FRAMED: PLAYER_TEXT})
    stream = all_failing(search)[-1]
    spent = search.reading_seconds
    # A download that this keeps going lasts far longer than a search may.
    monkeypatch.setattr(page_streams, "SEARCH_TOTAL_SECONDS", 0)
    monkeypatch.setattr(page_streams, "SEARCH_SECONDS", 0)
    assert search.refreshed(stream) == stream and search.reading_seconds == spent and asked[-2:] == [PAGE, FRAMED]


# How long a search may take, and stopping it


def test_a_search_that_has_had_its_time_starts_nothing_more_and_says_what_it_tried(monkeypatch: pytest.MonkeyPatch) -> None:
    search, asked = searching(monkeypatch, {PAGE: iframes(f'src="{FRAMED}"', f'src="{DEEP}"'), FRAMED: PLAYER_TEXT, DEEP: PLAYER_TEXT})
    candidates = search.candidates()
    search.failed(next(candidates), NO_VIDEO)
    # The time that the trying took counts, which the search itself has no other way to bound.
    monkeypatch.setattr(page_streams, "SEARCH_TOTAL_SECONDS", 0)
    assert list(candidates) == [] and asked == [PAGE]
    failure = search.failure(NO_VIDEO)
    assert failure.error == f"{NO_VIDEO}\n\n{OUT_OF_TIME_AFTER.format(tried='內嵌網頁 frame.example')}" and failure_kind(failure.error) == UNREADABLE_LINK


def test_the_time_a_search_spends_reading_is_bounded_by_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(page_streams, "SEARCH_SECONDS", 0)
    search, asked = searching(monkeypatch, {PAGE: iframes(f'src="{FRAMED}"'), FRAMED: PLAYER_TEXT})
    # What needs no more reading is still handed out to be tried; no page is read for it any more.
    assert [candidate.address for candidate in all_failing(search)] == [FRAMED] and asked == [PAGE]
    assert search.failure(NO_VIDEO).error == f"{NO_VIDEO}\n\n{OUT_OF_TIME_AFTER.format(tried='內嵌網頁 frame.example')}"


def test_a_page_that_is_slow_to_come_is_given_up_when_the_time_for_reading_runs_out(plain_site: LocalSite, monkeypatch: pytest.MonkeyPatch) -> None:
    url = plain_site.write_page("watch.html", "Slow", iframes('src="player.html"'))
    monkeypatch.setattr(page_streams, "SEARCH_SECONDS", 0)
    search = StreamSearch(QUIET, url, True)
    # Out of time before the first piece of the page: nothing is asked for at all, and the search says why.
    assert list(search.candidates()) == [] and plain_site.requests == []
    assert search.failure(NO_VIDEO).error == f"{NO_VIDEO}\n\n{OUT_OF_TIME}"


def test_what_a_name_resolves_to_is_what_this_machine_says_and_nothing_when_it_says_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    found = [(2, 1, 6, "", ("93.184.216.34", 0)), (23, 1, 6, "", ("2606:2800:220:1::1", 0, 0, 0)), (2, 1, 6, "", ("93.184.216.34", 0))]
    monkeypatch.setattr(page_streams.socket, "getaddrinfo", lambda host, port, **kwargs: found)
    assert sorted(real_resolved_addresses("site.example")) == ["2606:2800:220:1::1", "93.184.216.34"]

    def fails(host: str, port: object, **kwargs: object) -> None:
        raise page_streams.socket.gaierror("no such name")

    monkeypatch.setattr(page_streams.socket, "getaddrinfo", fails)
    assert real_resolved_addresses("nowhere.example") == ()


def test_a_stopped_job_has_nothing_asked_for(plain_site: LocalSite) -> None:
    url = plain_site.add_iframe_page("watch.html", "player.html")
    search = StreamSearch(QUIET, url, True, lambda: True)
    assert list(search.candidates()) == [] and plain_site.requests == [] and not search.out_of_time


def test_a_search_stopped_on_the_way_reads_no_further(monkeypatch: pytest.MonkeyPatch) -> None:
    stop: list[bool] = []
    search, asked = searching(monkeypatch, {PAGE: iframes(f'src="{FRAMED}"', f'src="{DEEP}"'), FRAMED: PLAYER_TEXT, DEEP: PLAYER_TEXT}, should_stop=lambda: bool(stop))
    candidates = search.candidates()
    search.failed(next(candidates), NO_VIDEO)
    stop.append(True)
    assert list(candidates) == [] and asked == [PAGE]


# What a search that found nothing is told as


def test_a_search_whose_every_candidate_held_nothing_leaves_the_pages_own_words_and_says_what_it_tried(monkeypatch: pytest.MonkeyPatch) -> None:
    search, _ = searching(monkeypatch, {PAGE: iframes(f'src="{FRAMED}"', 'src="https://www.youtube.com/embed/abcdefghijk"'), FRAMED: PLAYER_TEXT})
    all_failing(search)
    tried = "內嵌網頁 frame.example、內嵌網頁 frame.example 腳本裡的串流（cdn.example）、內嵌的 YouTube 播放器"
    # A stream that was found and held nothing is what the search is about; a page that merely held nothing is not.
    failure = search.failure(NO_VIDEO)
    assert failure.error == f"{FRAMED_STREAM_FOUND_BUT_FAILED.format(frame='frame.example')}{NO_VIDEO}" and failure.tried and failure.tried.stream.address == STREAM_ADDRESS
    search, _ = searching(monkeypatch, {PAGE: iframes(f'src="{FRAMED}"', 'src="https://www.youtube.com/embed/abcdefghijk"')})
    all_failing(search)
    failure = search.failure(NO_VIDEO)
    assert failure.error == f"{NO_VIDEO}\n\n{TRIED_ALL.format(tried=tried.replace('、內嵌網頁 frame.example 腳本裡的串流（cdn.example）', ''))}"
    assert failure.tried is None and failure.verify_url == "" and failure_kind(failure.error) == UNREADABLE_LINK
    # A page with nothing to try is told exactly what its own run said.
    search, _ = searching(monkeypatch, {PAGE: "<p>nothing</p>"})
    assert all_failing(search) == [] and search.failure(NO_VIDEO) == Failure(NO_VIDEO)


def test_a_check_for_a_person_is_what_a_failed_search_is_told_as_whatever_came_after(monkeypatch: pytest.MonkeyPatch) -> None:
    search, asked = searching(monkeypatch, {PAGE: iframes(f'src="{FRAMED}"', f'src="{DEEP}"'), DEEP: PLAYER_TEXT})
    check = humanize_human_check("ERROR: [generic] Unable to download webpage: HTTP Error 403: Forbidden", FRAMED, "frame.example")
    for candidate in search.candidates():
        search.failed(candidate, check if candidate.address == FRAMED else REFUSED, FRAMED if candidate.address == FRAMED else "")
    failure = search.failure(NO_VIDEO)
    # Its message comes first, or it would not be told for a check, and the button to pass it would not be offered.
    assert failure.error.startswith("frame.example 要求真人驗證") and failure_kind(failure.error) == HUMAN_CHECK
    assert failure.error.endswith(CHECK_IS_ELSEWHERE.format(host="frame.example")) and failure.verify_url == FRAMED
    assert failure.tried and failure.tried.stream.address == FRAMED
    # The site that asked for a person is not asked again for its page's text; the others still are.
    assert asked == [PAGE, DEEP]


def test_a_stream_that_could_not_be_read_is_told_before_an_embedded_page_that_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    search, _ = searching(monkeypatch, {PAGE: iframes(f'src="{FRAMED}"'), FRAMED: PLAYER_TEXT})
    all_failing(search, REFUSED)
    assert search.failure(NO_VIDEO).error == f"{FRAMED_STREAM_FOUND_BUT_FAILED.format(frame='frame.example')}{REFUSED}"
    search, _ = searching(monkeypatch, {PAGE: iframes(f'src="{FRAMED}"', 'src="https://www.youtube.com/embed/abcdefghijk"')})
    all_failing(search, REFUSED)
    assert search.failure(NO_VIDEO).error == f"{FRAME_FOUND_BUT_FAILED.format(frame='frame.example')}{REFUSED}"
    search, _ = searching(monkeypatch, {PAGE: iframes('src="https://www.youtube.com/embed/abcdefghijk"')})
    all_failing(search, REFUSED)
    assert search.failure(NO_VIDEO).error == f"{PLAIN_FOUND_BUT_FAILED}{REFUSED}"


def test_a_refusal_names_the_owners_referer_when_it_took_the_place_of_the_players_own(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = {PAGE: iframes(f'src="{FRAMED}"'), FRAMED: PLAYER_TEXT}
    search, _ = searching(monkeypatch, pages, opts={"proxy": "", "http_headers": {"referer": f" {PAGE} "}})
    all_failing(search, REFUSED)
    advice = REFERER_OVER_THE_PLAYERS.format(owner=PAGE, computed="https://frame.example/")
    assert search.failure(NO_VIDEO).error == f"{FRAMED_STREAM_FOUND_BUT_FAILED.format(frame='frame.example')}{REFUSED}\n\n{advice}"
    # Not for a failure that is no refusal, nor when the owner's is what the player would have sent anyway.
    search, _ = searching(monkeypatch, pages, opts={"proxy": "", "http_headers": {"Referer": PAGE}})
    all_failing(search)
    assert "Referer" not in search.failure(NO_VIDEO).error
    search, _ = searching(monkeypatch, pages, opts={"proxy": "", "http_headers": {"Referer": "https://frame.example/"}})
    all_failing(search, REFUSED)
    assert "Referer" not in search.failure(NO_VIDEO).error


def test_a_check_for_a_person_names_its_site_when_that_is_not_plainly_the_links_own(tmp_path: Path) -> None:
    refused = "ERROR: [generic] Unable to download webpage: HTTP Error 403: Forbidden"
    options = owner_options(PAGE, tmp_path)
    here = impersonation.Refusals(1, True, PAGE, [PAGE])
    assert tell_of_human_check(refused, PAGE, options, here) == (humanize_human_check(refused, PAGE), PAGE)
    assert tell_of_human_check(refused, PAGE, options, here)[0].startswith("這個網站要求真人驗證")
    # Met at a stream of the link's page on another site, or by the run for something that a page led to.
    there = impersonation.Refusals(1, True, STREAM_ADDRESS, [STREAM_ADDRESS])
    told, where = tell_of_human_check(refused, PAGE, options, there)
    assert told.startswith("cdn.example 要求真人驗證") and where == STREAM_ADDRESS and failure_kind(told) == HUMAN_CHECK
    led = replace(options, page_stream=PageStream(PAGE, FRAMED, kind=FRAME, frame=FRAMED))
    told, where = tell_of_human_check(refused, FRAMED, led, impersonation.Refusals(1, True, FRAMED, [FRAMED]))
    assert told.startswith("frame.example 要求真人驗證") and where == FRAMED
    # A check that yt-dlp had no words for, as one met while the app itself read a page, has none quoted.
    assert humanize_human_check("", FRAMED, "frame.example").endswith("通過後再試一次。") and failure_kind(humanize_human_check("", FRAMED, "frame.example")) == HUMAN_CHECK
    # A name too long for a hint to hold is left out, so that the message is still told for what it is.
    assert failure_kind(humanize_human_check(refused, FRAMED, f"{'a' * 60}.example")) == HUMAN_CHECK


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
    assert "<title>Seen</title>" in read_page(QUIET, url, True).text


@pytest.mark.parametrize("name", ["data.json", "clip.mp4", "missing.html", "forbidden/page.html"])
def test_what_is_not_a_page_that_can_be_read_is_nothing(plain_site: LocalSite, name: str) -> None:
    for path in ("data.json", "clip.mp4", "forbidden/page.html"):
        (plain_site.root / path).parent.mkdir(parents=True, exist_ok=True)
        (plain_site.root / path).write_bytes(b'{"a": 1}')
    assert read_page(QUIET, plain_site.url(name), True).text == ""


def test_a_page_that_cannot_be_reached_is_nothing_either() -> None:
    assert read_page(QUIET, "http://127.0.0.1:9/page.html", True).text == ""


def test_a_page_that_the_check_for_bots_turns_away_once_is_read_at_the_second_asking(plain_site: LocalSite, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(page_streams, "CHECK_PAUSE_SECONDS", 0)
    url = plain_site.write_page("flaky/watch.html", "Seen", "<p>player</p>")
    assert "<title>Seen</title>" in read_page(QUIET, url, True).text
    assert plain_site.asked_for("flaky/watch.html") == 2


def test_a_page_that_always_wants_a_person_is_asked_for_twice_and_then_left(plain_site: LocalSite, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(page_streams, "CHECK_PAUSE_SECONDS", 0)
    url = plain_site.write_page("captcha/watch.html", "Never", "<p>player</p>")
    assert read_page(QUIET, url, True).text == "" and plain_site.asked_for("captcha/watch.html") == 2


def test_a_refusal_that_is_no_check_is_not_asked_for_again(plain_site: LocalSite, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(page_streams, "CHECK_PAUSE_SECONDS", 0)
    url = plain_site.write_page("forbidden/watch.html", "No", "<p>player</p>")
    assert read_page(QUIET, url, True).text == "" and plain_site.asked_for("forbidden/watch.html") == 1


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
def test_a_stream_found_through_an_iframe_downloads_in_the_pages_name(new_site: NewSite, tmp_path: Path) -> None:
    page_site, stream_site = pair(new_site)
    stream = stream_site.add_stream("referred/hls")
    page_site.add_embedded_player("player.html", stream)
    url = page_site.add_iframe_page("watch.html", "player.html", title="Seen through an iframe")

    outcome = download(url, tmp_path)
    assert outcome["errors"] == []
    item = outcome["items"][0]
    saved = Path(item["path"])
    # Named for the outer page, the owner's own link, not the iframe that really held the player.
    assert saved.stem.startswith("Seen through an iframe") and duration_of(saved) == pytest.approx(2, abs=0.5)
    assert item["webpage_url"] == url and KEYED_ID_RE.fullmatch(item["video_id"])
    assert outcome["notes"][0] == EMBEDDED_STREAM_NOTE.format(source=FRAMED_STREAM_DETAIL.format(frame="127.0.0.1"), host="127.0.0.1")
    # The embedded page was asked for as the page's own iframe is: its own site, so by the whole page, and no Origin.
    assert set(page_site.referers_for("player.html")) == {(url, "")}
    # The stream as the iframe's own player would ask: another site, so only where the iframe is (the page's site here).
    segments = stream_site.segments("referred/hls")
    assert segments and all(stream_site.referers_for(segment) == [(page_site.base + "/", page_site.base)] for segment in segments)


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


def test_a_refusal_names_the_owners_referer_when_it_is_the_address_of_a_stream(new_site: NewSite, tmp_path: Path) -> None:
    page_site, stream_site = pair(new_site)
    stream = stream_site.add_playlist("referred/hls/index.m3u8")
    url = page_site.add_packed_page("watch.html", player_script(stream))
    # The owner's own Referer wins over the page's, and a stream is not a page that a player is in.
    extra = f'--add-headers "Referer: {stream}"'
    report, outcome = inspect(url, tmp_path, ytdlp_extra_args=extra), download(url, tmp_path, ytdlp_extra_args=extra)
    advice = referer_advice(stream)
    assert report["error"].startswith(PAGE_STREAM_FOUND_BUT_FAILED) and report["error"].endswith(advice)
    assert outcome["items"] == [] and outcome["errors"][0].endswith(advice)
    # Without it, or with a page there, the same site lets the stream through and has nothing to be told.
    assert inspect(url, tmp_path)["error"] == ""
    assert inspect(url, tmp_path, ytdlp_extra_args=f'--referer {page_site.url("watch.html")}')["error"] == ""


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


# The pages that a page embeds, on sites of this machine


# A browser newer than any the stand-in can pass for, and what it was given by a site whose check its owner passed.
AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/1000.0.0.0 Safari/537.36"
EMBEDDED = "127.0.0.1"


def passed(host: str) -> VerifiedSession:
    cookie = SiteCookie(CLEARANCE_COOKIE, "good", host, "/", time.time() + 3600, False, True)
    return VerifiedSession(host, (cookie,), AGENT, (), "Chrome", time.time())


def test_a_player_two_iframes_deep_is_found_and_each_request_names_what_embeds_it(new_site: NewSite, tmp_path: Path) -> None:
    page_site, middle_site, deep_site, stream_site = new_site(), new_site(), new_site(), new_site()
    # Each lets in only what comes from the one before it.
    stream_site.refer_only_from(deep_site)
    deep_site.refer_only_from(middle_site)
    middle_site.refer_only_from(page_site)
    deep = deep_site.add_embedded_player("referred/player.html", stream_site.add_playlist("referred/hls/index.m3u8"))
    middle = middle_site.add_iframe_page("referred/frame.html", deep)
    url = page_site.add_iframe_page("watch.html", middle, title="Two deep")

    report = inspect(url, tmp_path)
    assert report["error"] == "" and report["url"] == url and report["title"] == "Two deep"
    assert report["site"] == "127.0.0.1（一般網頁）" and report["details"][0] == FRAMED_STREAM_DETAIL.format(frame=EMBEDDED)
    # A page that another site embeds is asked for by that site's name alone, and a browser names no Origin for it.
    assert set(middle_site.referers_for("referred/frame.html")) == {(page_site.base + "/", "")}
    assert set(deep_site.referers_for("referred/player.html")) == {(middle_site.base + "/", "")}
    # The stream, by the page that really holds its player, as that player would ask.
    assert set(stream_site.referers_for("referred/hls/index.m3u8")) == {(deep_site.base + "/", deep_site.base)}


@needs_ffmpeg
def test_the_inspection_and_the_download_of_a_page_agree_on_what_its_iframes_led_to(new_site: NewSite, tmp_path: Path) -> None:
    page_site, middle_site, deep_site, stream_site = new_site(), new_site(), new_site(), new_site()
    stream_site.refer_only_from(deep_site)
    deep = deep_site.add_embedded_player("player.html", stream_site.add_stream("referred/hls"))
    url = page_site.add_iframe_page("watch.html", middle_site.add_iframe_page("frame.html", deep), title="Two deep")

    report = inspect(url, tmp_path)
    progress = Progress()
    logged: list[str] = []
    outcome = run_download(url, owner_options(url, tmp_path), progress, on_log=logged.append)
    item = outcome["items"][0]
    assert report["error"] == "" and outcome["errors"] == [] and Path(item["path"]).stem.startswith("Two deep")
    # One video to the preview, the archive and the library, found in the same place, told in the same words.
    assert item["video_id"] == report["video_id"] and KEYED_ID_RE.fullmatch(item["video_id"]) and item["webpage_url"] == url
    assert outcome["notes"][0] == EMBEDDED_STREAM_NOTE.format(source=report["details"][0], host=EMBEDDED)
    # The job's log says which page it was that led there, and what was tried on the way.
    tried = [f"內嵌網頁 {EMBEDDED}", f"內嵌網頁 {EMBEDDED}", f"內嵌網頁 {EMBEDDED} 腳本裡的串流（{EMBEDDED}）"]
    assert [line for line in logged if "改試" in line] == [PAGE_STREAM_TRY_MESSAGE.format(page=url, candidate=name) for name in tried]
    assert PAGE_STREAM_MESSAGE in progress.texts


@needs_ffmpeg
def test_an_embedded_page_that_ytdlp_can_read_is_never_read_for_its_scripts(new_site: NewSite, tmp_path: Path) -> None:
    page_site, frame_site = new_site(), new_site()
    frame_site.add_clip("clip.mp4")
    frame = frame_site.add_page("player.html", title="The player's own title")
    url = page_site.add_iframe_page("watch.html", frame, title="Seen on the page")

    inspect(frame, tmp_path)
    alone = frame_site.asked_for("player.html")
    report = inspect(url, tmp_path)
    # As often as yt-dlp asks for it when it is the link itself: the search did not ask once more.
    assert report["error"] == "" and frame_site.asked_for("player.html") == 2 * alone
    # Named for the page the owner gave, which yt-dlp named after a file; and said to come from the page it embeds.
    assert report["title"] == "Seen on the page" and report["url"] == url and report["details"][0] == FRAME_DETAIL.format(frame=EMBEDDED)
    outcome = download(url, tmp_path)
    item = outcome["items"][0]
    assert outcome["errors"] == [] and item["title"] == "Seen on the page" and item["webpage_url"] == url and item["video_id"] == report["video_id"]
    assert outcome["notes"][0] == EMBEDDED_NOTE.format(source=FRAME_DETAIL.format(frame=EMBEDDED)) and duration_of(Path(item["path"])) == pytest.approx(2, abs=0.5)
    # Asked for as the page's iframe, and its file as the embedded page's own player asks for it.
    assert set(frame_site.referers_for("player.html")) == {("", ""), (page_site.base + "/", "")}
    assert (frame, "") in frame_site.referers_for("clip.mp4")


@needs_ffmpeg
def test_a_page_embedded_two_deep_that_ytdlp_can_read_is_found_though_nothing_names_a_stream(new_site: NewSite, tmp_path: Path) -> None:
    page_site, middle_site, deep_site = new_site(), new_site(), new_site()
    deep_site.add_clip("clip.mp4")
    middle = middle_site.add_iframe_page("frame.html", deep_site.add_page("player.html"))
    url = page_site.add_iframe_page("watch.html", middle, title="A video tag two deep")
    outcome = download(url, tmp_path)
    assert outcome["errors"] == [] and outcome["items"][0]["title"] == "A video tag two deep"
    assert set(deep_site.referers_for("player.html")) == {(middle_site.base + "/", "")}


def test_an_embedded_pages_packed_script_gives_its_stream_away(new_site: NewSite, tmp_path: Path) -> None:
    page_site, stream_site = pair(new_site)
    page_site.add_packed_page("embed/player.html", player_script(stream_site.add_playlist("referred/hls/index.m3u8")))
    url = page_site.add_iframe_page("watch.html", "embed/player.html", title="Packed inside")
    report = inspect(url, tmp_path)
    assert report["error"] == "" and report["title"] == "Packed inside" and report["details"][0] == FRAMED_STREAM_DETAIL.format(frame=EMBEDDED)


def test_a_player_behind_two_advertisements_is_tried_first_and_they_are_never_asked_for(new_site: NewSite, tmp_path: Path) -> None:
    page_site, ad_site, stream_site = new_site(), new_site(), new_site()
    stream_site.refer_only_from(page_site)
    page_site.add_embedded_player("embed/player.html", stream_site.add_playlist("referred/hls/index.m3u8"))
    # In the page's own order the two advertisements would use up all the iframes that a search follows.
    banner, counter = f'src="{ad_site.url("banner.html")}" width="300" height="250"', f'src="{ad_site.url("count.html")}"'
    url = page_site.add_framing_page("watch.html", banner, counter, 'src="embed/player.html" allowfullscreen', title="After the ads")
    assert IFRAME_TRIES == 2 and inspect(url, tmp_path)["error"] == "" and ad_site.requests == []


def test_an_iframe_that_is_filled_in_as_the_page_scrolls_is_followed(new_site: NewSite, tmp_path: Path) -> None:
    page_site, stream_site = pair(new_site)
    page_site.add_embedded_player("player.html", stream_site.add_playlist("referred/hls/index.m3u8"))
    url = page_site.add_framing_page("watch.html", 'src="about:blank" data-src="player.html" class="lazyload"', title="Lazy")
    assert inspect(url, tmp_path)["error"] == ""


def test_an_embedded_page_that_was_moved_is_read_as_the_page_it_was_moved_to(new_site: NewSite, tmp_path: Path) -> None:
    page_site, old_site, frame_site = new_site(), new_site(), new_site()
    frame_site.refer_only_from(frame_site)
    frame_site.add_playlist("referred/hls/index.m3u8")
    # The stream is named from where the player really is, and is on that site.
    moved_to = frame_site.add_embedded_player("player.html", "/referred/hls/index.m3u8")
    url = page_site.add_iframe_page("watch.html", old_site.add_redirect("embed.html", moved_to), title="Moved")
    assert inspect(url, tmp_path)["error"] == ""
    assert set(frame_site.referers_for("referred/hls/index.m3u8")) == {(moved_to, "")} and old_site.asked_for("referred/hls/index.m3u8") == 0


def test_a_visitor_never_has_an_embedded_page_followed(new_site: NewSite, tmp_path: Path) -> None:
    page_site, frame_site = new_site(), new_site()
    url = page_site.add_iframe_page("watch.html", frame_site.add_embedded_player("player.html", frame_site.add_playlist("hls/index.m3u8")))
    options = replace(owner_options(url, tmp_path), other_sites=False)
    assert inspect_link(url, options)["error"] and run_download(url, options, Progress())["items"] == []
    assert frame_site.requests == []


def test_what_a_page_with_nothing_behind_its_iframes_is_told_says_what_was_tried(new_site: NewSite, tmp_path: Path) -> None:
    page_site, frame_site = new_site(), new_site()
    url = page_site.add_iframe_page("watch.html", frame_site.write_page("player.html", "Nothing", "<p>no player</p>"))
    report, outcome = inspect(url, tmp_path), download(url, tmp_path)
    for error in (report["error"], outcome["errors"][0]):
        assert error.startswith(NOTHING_IN_PAGE) and error.endswith(TRIED_ALL.format(tried=f"內嵌網頁 {EMBEDDED}")) and failure_kind(error) == UNREADABLE_LINK
    assert outcome["items"] == [] and outcome["verify_url"] == "" and "verify_url" not in report


def test_a_search_out_of_time_asks_for_nothing_and_says_so(new_site: NewSite, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    page_site, frame_site = new_site(), new_site()
    url = page_site.add_iframe_page("watch.html", frame_site.write_page("player.html", "Nothing", "<p>no player</p>"))
    monkeypatch.setattr(page_streams, "SEARCH_TOTAL_SECONDS", 0)
    report, outcome = inspect(url, tmp_path), download(url, tmp_path)
    for error in (report["error"], outcome["errors"][0]):
        assert error.startswith(NOTHING_IN_PAGE) and error.endswith(OUT_OF_TIME)
    assert frame_site.requests == [] and not outcome["stopped"]


def test_a_job_stopped_while_its_page_is_searched_asks_for_nothing_more(new_site: NewSite, tmp_path: Path) -> None:
    page_site, frame_site, stream_site = new_site(), new_site(), new_site()
    frame = frame_site.add_embedded_player("player.html", stream_site.add_playlist("hls/index.m3u8"))
    url = page_site.add_iframe_page("watch.html", frame)
    # Stopped once the embedded page has been asked for, which is while yt-dlp tries it.
    outcome = run_download(url, owner_options(url, tmp_path), Progress(), should_stop=lambda: bool(frame_site.requests))
    assert outcome["stopped"] and outcome["items"] == [] and stream_site.requests == []


# The owner's own Referer, and the one an embedded player would send


def test_the_owners_referer_goes_to_an_embedded_page_in_place_of_the_pages(plain_site: LocalSite) -> None:
    url = plain_site.write_page("player.html", "Player", "<p>player</p>")
    asked_by_the_page = {"Referer": "http://page.example/", "X-Asked-By": "the search"}
    assert read_page(QUIET, url, True, asked_by_the_page).text
    owners = {**QUIET, "http_headers": {"referer": "http://owner.example/watch", "X-Owner": "1"}}
    assert read_page(owners, url, True, asked_by_the_page).text
    first, second = plain_site.headers_for("player.html")
    assert (first["referer"], first["x-asked-by"]) == ("http://page.example/", "the search") and "x-owner" not in first
    # The owner has the last word on a header of the same name; what the page alone asks with still goes.
    assert (second["referer"], second["x-asked-by"], second["x-owner"]) == ("http://owner.example/watch", "the search", "1")


def test_a_refusal_says_so_when_the_owners_referer_took_the_place_of_an_embedded_players(new_site: NewSite, tmp_path: Path) -> None:
    page_site, frame_site, stream_site = new_site(), new_site(), new_site()
    stream_site.refer_only_from(frame_site)
    frame = frame_site.add_embedded_player("player.html", stream_site.add_playlist("referred/hls/index.m3u8"))
    url = page_site.add_iframe_page("watch.html", frame)
    assert inspect(url, tmp_path)["error"] == ""
    # What the app's own word on an HTTP 403 says to fill in: the page that the video is on.
    extra = f"--referer {url}"
    report, outcome = inspect(url, tmp_path, ytdlp_extra_args=extra), download(url, tmp_path, ytdlp_extra_args=extra)
    advice = REFERER_OVER_THE_PLAYERS.format(owner=url, computed=frame_site.base + "/")
    for error in (report["error"], outcome["errors"][0]):
        assert error.startswith(FRAMED_STREAM_FOUND_BUT_FAILED.format(frame=EMBEDDED)) and "HTTP 403" in error and error.endswith(advice)
    # It went to the embedded page too, and to the stream, where it was not what the stream's site lets in.
    assert (url, "") in frame_site.referers_for("player.html") and (url, frame_site.base) in stream_site.referers_for("referred/hls/index.m3u8")


# A site's check for a person, met past the page


def test_a_check_for_a_person_on_the_site_of_a_pages_own_stream_is_told_as_one(browser: Any, new_site: NewSite, tmp_path: Path) -> None:
    page_site, stream_site = new_site(), new_site()
    stream = stream_site.add_playlist("captcha/hls/index.m3u8")
    url = page_site.add_packed_page("watch.html", player_script(stream))
    report, outcome = inspect(url, tmp_path), download(url, tmp_path)
    for error, verify_url in ((report["error"], report["verify_url"]), (outcome["errors"][0], outcome["verify_url"])):
        # The check's own words come first: told for what it is, the button to pass it is offered, and at its own site.
        assert failure_kind(error) == HUMAN_CHECK and error.startswith(f"{EMBEDDED} 要求真人驗證") and verify_url == stream
        assert PAGE_STREAM_FOUND_BUT_FAILED not in error
    assert verification.opening_address(report["verify_url"]) == stream_site.base + "/"
    assert any("播放器腳本" in note for note in outcome["notes"])


def test_a_check_for_a_person_on_an_embedded_pages_site_is_told_of_that_site(browser: Any, new_site: NewSite, tmp_path: Path) -> None:
    page_site, frame_site = new_site(), new_site()
    frame = frame_site.write_page("captcha/player.html", "Never", "<p>player</p>")
    url = page_site.add_iframe_page("watch.html", frame)
    report = inspect(url, tmp_path)
    asked = frame_site.asked_for("captcha/player.html")
    outcome = download(url, tmp_path)
    for error, verify_url in ((report["error"], report["verify_url"]), (outcome["errors"][0], outcome["verify_url"])):
        assert failure_kind(error) == HUMAN_CHECK and error.startswith(f"{EMBEDDED} 要求真人驗證") and verify_url == frame
    assert report["url"] == url and outcome["items"] == []
    # Asked for as a program and then as a browser, by yt-dlp; the search did not go back to a site that wants a person.
    assert asked == 2 and frame_site.asked_for("captcha/player.html") == 2 * asked


def test_a_check_for_a_person_is_not_lost_to_a_later_page_that_merely_holds_nothing(browser: Any, new_site: NewSite, tmp_path: Path) -> None:
    page_site, checked_site, other_site = new_site(), new_site(), new_site()
    checked = checked_site.write_page("captcha/embed/player.html", "Never", "<p>player</p>")
    nothing = other_site.write_page("frame.html", "Nothing", "<p>no player</p>")
    url = page_site.add_framing_page("watch.html", f'src="{checked}"', f'src="{nothing}"')
    report, outcome = inspect(url, tmp_path), download(url, tmp_path)
    assert other_site.asked_for("frame.html") >= 2
    for error, verify_url in ((report["error"], report["verify_url"]), (outcome["errors"][0], outcome["verify_url"])):
        assert failure_kind(error) == HUMAN_CHECK and verify_url == checked


def test_a_check_passed_for_an_embedded_pages_site_is_what_the_next_try_asks_with(new_site: NewSite, tmp_path: Path) -> None:
    page_site, frame_site, stream_site = new_site(), new_site(), new_site()
    frame_site.require_clearance("good", AGENT)
    stream_site.refer_only_from(frame_site)
    frame = frame_site.add_embedded_player("verified/player.html", stream_site.add_playlist("referred/hls/index.m3u8"))
    url = page_site.add_iframe_page("watch.html", frame, title="Behind a person's check")
    assert inspect(url, tmp_path)["error"]
    # Passed in a browser window, for the site that the check was met at.
    verification.verified_sites.add(passed(host_of(frame)))
    report = inspect(url, tmp_path)
    assert report["error"] == "" and report["title"] == "Behind a person's check"
    sent = frame_site.headers_for("verified/player.html")[-1]
    assert sent["user-agent"] == AGENT and f"{CLEARANCE_COOKIE}=good" in sent["cookie"]


def test_an_embedded_page_whose_site_wants_a_browser_is_read_as_one_though_its_page_was_not(browser: Any, new_site: NewSite, tmp_path: Path) -> None:
    page_site, frame_site, stream_site = new_site(), new_site(), new_site()
    stream_site.refer_only_from(frame_site)
    frame = frame_site.add_embedded_player("browsers-only/player.html", stream_site.add_playlist("referred/hls/index.m3u8"))
    url = page_site.add_iframe_page("watch.html", frame, title="A browser's player")
    report = inspect(url, tmp_path)
    assert report["error"] == "" and report["title"] == "A browser's player" and page_site.browser_requests == []
    # Refused to a program twice, by yt-dlp's try and by the look into its text, and read as a browser after each.
    assert frame_site.asked_for("browsers-only/player.html") == 4 and frame_site.asked_as_a_browser("browsers-only/player.html") == 2


def test_a_page_read_as_a_browser_at_the_second_asking_says_that_it_was(browser: Any, plain_site: LocalSite) -> None:
    refused = plain_site.write_page("browsers-only/player.html", "Player", "<p>player</p>")
    reading = read_page(QUIET, refused, True)
    assert "<title>Player</title>" in reading.text and reading.as_browser and plain_site.asked_as_a_browser("browsers-only/player.html") == 1
    # A check that turns even a browser away wants a person, which is for the search to say.
    reading = read_page(QUIET, plain_site.write_page("captcha/player.html", "Never", "<p>x</p>"), True)
    assert (reading.text, reading.challenged, reading.as_browser) == ("", True, True) and plain_site.asked_for("captcha/player.html") == 2

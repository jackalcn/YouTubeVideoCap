"""A stream whose address is good for a short while only, on web sites of this machine: its page is asked for the
address again, and the download goes on in the same file. And a stream whose playlists and fragments are served only
with the token that its address has."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Optional

import pytest

import videocap.downloader as downloader_module
import videocap.options as options_module
from fake_browser import browser_fixture, real_handler, recording, stand_in_handler  # noqa: F401
from offline_web import LocalSite, new_site_fixture, web_media_fixture  # noqa: F401
from offline_youtube import duration_of
from videocap import impersonation
from videocap.downloader import (
    QUERY_PASSED_MESSAGE,
    QUERY_PASSED_NOTE,
    STREAM_LOST,
    STREAM_REFRESH_IN_VAIN,
    STREAM_REFRESH_LIMIT,
    STREAM_REFRESH_MESSAGE,
    STREAM_REFRESHED_NOTE,
    StreamAddressExpired,
    YtDlpLogCollector,
    refused_as_a_program,
    run_download,
)
from videocap.errors import HTTP_403, STREAM_EXPIRED, add_overridden_referer_advice, failure_kind, humanize_ytdlp_error
from videocap.inspector import inspect_link
from videocap.options import AccessLimits, DownloadOptions, LoginSource, build_base_options, build_read_options, options_from_spec, parse_extra_ytdlp_args
from videocap.page_streams import FRAME, PageStream, StreamSearch
from videocap.settings import DownloadSettings, JobSpec


NewSite = Callable[[], LocalSite]
PAGE = "https://site.example/watch/1"
STREAM_URL = "https://cdn.example/id/real.m3u8?token=one"
# What yt-dlp says of a fragment that it will ask for once more.
GOT_ERROR = "[download] Got error: HTTP Error {status}: Refused. Retrying fragment 7 (1/1)..."
# The tokens that a player's page hands out before the search reads it: yt-dlp reads the page once, to look for a video.
BEFORE_THE_SEARCH = 1


class Progress:
    def progress(self, value: float, text: str = "") -> None:
        pass


@pytest.fixture(autouse=True)
def no_pause_before_a_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(options_module, "_retry_delay", lambda n: 0.0)


def owner_options(url: str, folder: Path, **settings: Any) -> DownloadOptions:
    spec = JobSpec(url=url, settings=DownloadSettings(**{"retry_count": 1, **settings}), output_dir=str(folder))
    return options_from_spec(spec, AccessLimits())


def download(url: str, folder: Path, should_stop: Optional[Callable[[], bool]] = None, log: Optional[list[str]] = None, **settings: Any) -> Any:
    return run_download(url, owner_options(url, folder, **settings), Progress(), should_stop=should_stop, on_log=log.append if log is not None else None)


def signed_player(site: LocalSite, *uses: int, fragments: int = 6, name: str = "player.html") -> str:
    """A player's page, made anew each time it is asked for, that names a stream of its site with a token. The token
    that the search finds is good for the first of `uses` fragments, the one found after it for the next, and so on.
    The stream's playlist lists its fragments without the token, as the playlists of such sites do."""
    stream = site.add_long_stream("tokened/hls", fragments)
    text = f"<p>player</p><script>var source='{stream}?token={{token}}';</script>"
    return site.add_token_page(name, text, uses=(*[0] * BEFORE_THE_SEARCH, *uses))


def refreshes_logged(log: list[str]) -> list[str]:
    return [line for line in log if "重新取得" in line]


# What the log tells of an address that ran out


def expiring_logger(received: int = 1000) -> YtDlpLogCollector:
    logger = YtDlpLogCollector()
    logger.address_may_expire = True
    logger.data_arrived(5000)
    logger.data_arrived(5000 + received)
    return logger


@pytest.mark.parametrize("status", [401, 403, 410])
def test_a_fragment_refused_after_others_came_ends_the_run_of_a_pages_stream_at_once(status: int) -> None:
    logger = expiring_logger()
    with pytest.raises(StreamAddressExpired) as raised:
        logger.debug(GOT_ERROR.format(status=status))
    told = humanize_ytdlp_error(f"ERROR: {raised.value}", "https://cdn.example/id/real.m3u8")
    assert failure_kind(told) == STREAM_EXPIRED and f"HTTP Error {status}" in told and logger.lines[-1] == GOT_ERROR.format(status=status)


def test_only_a_refusal_after_data_of_a_stream_that_a_page_led_to_is_an_address_that_ran_out() -> None:
    # Nothing of this run came yet: what an earlier run left is not of this address.
    for logger in (expiring_logger(received=0), YtDlpLogCollector()):
        logger.debug(GOT_ERROR.format(status=403))
    # A link pasted by itself has no page to ask again.
    pasted = expiring_logger()
    pasted.address_may_expire = False
    pasted.debug(GOT_ERROR.format(status=403))
    # A failure that is no refusal of the address is tried again as it always was.
    for status in (404, 429, 500, 503):
        expiring_logger().debug(GOT_ERROR.format(status=status))
    expiring_logger().debug("[download] Got error: The read operation timed out. Retrying fragment 7 (1/1)...")


def test_what_came_in_is_counted_from_where_the_run_began() -> None:
    logger = YtDlpLogCollector()
    assert logger.bytes_received == 0
    logger.data_arrived(4096)
    assert logger.bytes_received == 0
    logger.data_arrived(6000)
    logger.data_arrived(5000)
    assert logger.bytes_received == 6000 - 4096


def test_a_run_whose_address_ran_out_is_not_tried_once_more_as_a_browser(browser: Any, tmp_path: Path) -> None:
    options = owner_options(PAGE, tmp_path)
    outcome = downloader_module.empty_outcome()
    refusals = impersonation.Refusals(forbidden=1, addresses=[STREAM_URL])
    assert refused_as_a_program(STREAM_URL, options, outcome, [("ERROR: HTTP Error 403: Forbidden", "")], refusals)
    expired = [(f"ERROR: {StreamAddressExpired('HTTP Error 403: Forbidden')}", "")]
    assert not refused_as_a_program(STREAM_URL, options, outcome, expired, refusals)


def test_a_referer_of_the_owners_is_named_for_an_address_that_ran_out_too() -> None:
    told = humanize_ytdlp_error(f"ERROR: {StreamAddressExpired('HTTP Error 403: Forbidden')}", "https://cdn.example/id/real.m3u8")
    assert "Referer" in add_overridden_referer_advice(told, PAGE, "https://frame.example/")


# Asking for a stream's playlists and fragments with the query of its address


def test_a_stream_asked_for_with_its_query_passed_on_tells_ytdlp_so() -> None:
    stream = PageStream(PAGE, STREAM_URL)
    plain = build_base_options(impersonation.Silent(), 3, LoginSource(), other_sites=True, page_stream=stream)
    passed = build_base_options(impersonation.Silent(), 3, LoginSource(), other_sites=True, page_stream=replace(stream, pass_query=True))
    assert "extractor_args" not in plain and passed["extractor_args"] == {"generic": {"variant_query": [""], "fragment_query": [""]}}
    # Never for a visitor, nor for YouTube, which no page's stream is.
    assert "extractor_args" not in build_base_options(impersonation.Silent(), 3, LoginSource(), other_sites=False, page_stream=replace(stream, pass_query=True))
    youtube = PageStream(PAGE, "https://www.youtube.com/watch?v=abcdefghijk&t=1", pass_query=True)
    assert "generic" not in build_base_options(impersonation.Silent(), 3, LoginSource(), other_sites=True, page_stream=youtube).get("extractor_args", {})


def test_the_owners_own_word_on_the_query_stays_over_the_apps(tmp_path: Path) -> None:
    typed = '--extractor-args "generic:fragment_query=key=theirs"'
    options = replace(owner_options(PAGE, tmp_path), extra_ytdlp=parse_extra_ytdlp_args(typed), page_stream=PageStream(PAGE, STREAM_URL, pass_query=True))
    assert build_read_options(options, impersonation.Silent())["extractor_args"]["generic"] == {"variant_query": [""], "fragment_query": ["key=theirs"]}


def test_a_stream_whose_fragments_want_the_token_of_its_address_downloads_at_the_second_try(new_site: NewSite, tmp_path: Path) -> None:
    page_site, player_site = new_site(), new_site()
    url = page_site.add_iframe_page("watch.html", signed_player(player_site, 3, fragments=3), title="Token on every fragment")
    log: list[str] = []
    outcome = download(url, tmp_path, log=log)
    assert outcome["errors"] == [] and duration_of(Path(outcome["items"][0]["path"])) == pytest.approx(3, abs=0.5)
    assert outcome["notes"][1] == QUERY_PASSED_NOTE and QUERY_PASSED_MESSAGE in log and refreshes_logged(log) == []
    # First as the playlist lists them, each of which the site refused, twice; then each once with the token.
    fragments = [request for request in player_site.requests if ".ts" in request]
    assert sorted(request for request in fragments if "?" not in request) == sorted([f"/tokened/hls/index{number}.ts" for number in range(3)] * 2)
    assert sorted(request for request in fragments if "?" in request) == [f"/tokened/hls/index{number}.ts?token=t2" for number in range(3)]


def test_a_stream_that_is_served_as_its_playlist_lists_it_is_not_asked_for_with_the_query_of_its_address(new_site: NewSite, tmp_path: Path) -> None:
    page_site, stream_site = new_site(), new_site()
    stream = stream_site.add_long_stream("hls", fragments=3)
    playlist = stream_site.root / "hls" / "index.m3u8"
    # Each fragment signed for itself, which the token of the address must not take the place of.
    lines = [f"{line}?token=its-own" if line.endswith(".ts") else line for line in playlist.read_text(encoding="utf-8").splitlines()]
    playlist.write_text("\n".join(lines) + "\n", encoding="utf-8")
    page_site.add_embedded_player("player.html", f"{stream}?token=of-the-address")
    url = page_site.add_iframe_page("watch.html", "player.html", title="Signed each for itself")
    outcome = download(url, tmp_path)
    assert outcome["errors"] == [] and QUERY_PASSED_NOTE not in outcome["notes"]
    fragments = [request for request in stream_site.requests if ".ts" in request]
    assert len(fragments) == 3 and all(request.endswith("?token=its-own") for request in fragments)


def test_a_stream_refused_with_and_without_its_query_is_told_as_it_was_refused_first(new_site: NewSite, tmp_path: Path) -> None:
    page_site, stream_site = new_site(), new_site()
    stream = stream_site.add_playlist("forbidden/hls/index.m3u8")
    page_site.add_embedded_player("player.html", f"{stream}?token=any")
    url = page_site.add_iframe_page("watch.html", "player.html")
    log: list[str] = []
    outcome = download(url, tmp_path, log=log)
    assert outcome["items"] == [] and failure_kind(outcome["errors"][0]) == HTTP_403 and QUERY_PASSED_NOTE not in outcome["notes"]
    assert log.count(QUERY_PASSED_MESSAGE) == 1


# A stream whose address runs out while it is downloaded


def test_a_stream_whose_address_runs_out_is_asked_of_its_page_again_and_downloads_whole(new_site: NewSite, tmp_path: Path) -> None:
    page_site, player_site = new_site(), new_site()
    url = page_site.add_iframe_page("watch.html", signed_player(player_site, 2, fragments=6), title="Signed for a while")
    log: list[str] = []
    outcome = download(url, tmp_path, log=log)
    assert outcome["errors"] == []
    saved = Path(outcome["items"][0]["path"])
    # All six fragments in one file, two from each of three addresses, under the name of the page.
    assert saved.stem.startswith("Signed for a while") and duration_of(saved) == pytest.approx(6, abs=0.5)
    assert outcome["items"][0]["webpage_url"] == url and len(outcome["items"]) == 1
    # After the note of where the stream was found: how it was kept going.
    assert outcome["notes"][1:3] == [STREAM_REFRESHED_NOTE.format(count=2), QUERY_PASSED_NOTE]
    assert refreshes_logged(log) == [STREAM_REFRESH_MESSAGE.format(count=count) for count in (1, 2)]
    # Each fragment came once: the one that an address ran out at was asked of the next address, and none was skipped.
    served = {token: 2 - left for token, left in player_site.tokens.items() if left < 2 and token != "t1"}
    assert served == {"t2": 2, "t3": 2, "t4": 2}
    # The way of asking that worked was kept: after the first address, no fragment was asked for without a token.
    bare = [request for request in player_site.requests if ".ts" in request and "?" not in request]
    assert sorted(bare) == sorted([f"/tokened/hls/index{number}.ts" for number in range(6)] * 2)
    assert bare == [request for request in player_site.requests if ".ts" in request][: len(bare)]
    # And what is not a fragment was read once for each address: the page, and the player's page.
    assert page_site.asked_for("watch.html") == 2 + 2 and player_site.asked_for("player.html") == BEFORE_THE_SEARCH + 1 + 2


def test_the_inspection_of_a_page_whose_stream_is_signed_reads_it_as_any_other(new_site: NewSite, tmp_path: Path) -> None:
    page_site, player_site = new_site(), new_site()
    url = page_site.add_iframe_page("watch.html", signed_player(player_site, 2, fragments=6), title="Signed for a while")
    report = inspect_link(url, owner_options(url, tmp_path))
    assert report["error"] == "" and report["title"] == "Signed for a while"


def test_an_address_found_again_that_brings_nothing_ends_the_download(new_site: NewSite, tmp_path: Path) -> None:
    page_site, player_site = new_site(), new_site()
    # The second address is refused from its playlist on: asking for a third would be asking for ever.
    url = page_site.add_iframe_page("watch.html", signed_player(player_site, 2, 0, fragments=6))
    log: list[str] = []
    outcome = download(url, tmp_path, log=log)
    assert outcome["items"] == [] and not outcome["stopped"] and failure_kind(outcome["errors"][0]) == HTTP_403
    assert STREAM_REFRESH_IN_VAIN.format(count=1) in outcome["notes"] and refreshes_logged(log) == [STREAM_REFRESH_MESSAGE.format(count=1)]
    assert len(player_site.tokens) == BEFORE_THE_SEARCH + 2


def test_an_address_is_found_again_only_so_often(new_site: NewSite, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(downloader_module, "STREAM_REFRESHES", 1)
    page_site, player_site = new_site(), new_site()
    url = page_site.add_iframe_page("watch.html", signed_player(player_site, 1, fragments=6))
    outcome = download(url, tmp_path)
    error = outcome["errors"][0]
    assert outcome["items"] == [] and failure_kind(error) == STREAM_EXPIRED and error.endswith(STREAM_REFRESH_LIMIT.format(count=1))
    assert len(player_site.tokens) == BEFORE_THE_SEARCH + 2


def test_a_stream_that_its_page_no_longer_leads_to_is_said_to_be_lost(new_site: NewSite, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(StreamSearch, "refreshed", lambda self, stream: None)
    page_site, player_site = new_site(), new_site()
    url = page_site.add_iframe_page("watch.html", signed_player(player_site, 2, fragments=6))
    outcome = download(url, tmp_path)
    error = outcome["errors"][0]
    assert outcome["items"] == [] and not outcome["stopped"] and failure_kind(error) == STREAM_EXPIRED and error.endswith(STREAM_LOST)
    # What came in is kept, for a later try to go on from.
    assert any(Path(path).exists() for path in outcome["partial_paths"])


def test_a_job_stopped_as_its_address_runs_out_asks_for_nothing_more(new_site: NewSite, tmp_path: Path) -> None:
    page_site, player_site = new_site(), new_site()
    url = page_site.add_iframe_page("watch.html", signed_player(player_site, 2, fragments=6))
    # Stopped once the first address has given all it had.
    spent = lambda: player_site.tokens.get("t2") == 0  # noqa: E731
    outcome = download(url, tmp_path, should_stop=spent)
    assert outcome["stopped"] and outcome["items"] == []
    assert len(player_site.tokens) == BEFORE_THE_SEARCH + 1 and page_site.asked_for("watch.html") == 2


def test_a_player_that_its_page_embeds_by_another_address_each_time_is_still_found_again(new_site: NewSite, tmp_path: Path) -> None:
    site = new_site()
    stream = site.add_long_stream("tokened/hls", 4)
    # One site for both, so the page and the player take their tokens from one row: the page's own are of no use.
    site.add_token_page("watch.html", '<title>Embedded anew</title><iframe src="/player/v2?payload={token}"></iframe>', uses=(0,))
    site.token_pages["/player/v2"] = (f"<script>var source='{stream}?token={{token}}';</script>",)
    site.token_uses = (0, 0, 0, 2, 0, 2)
    url = site.url("watch.html")
    outcome = download(url, tmp_path)
    assert outcome["errors"] == [] and duration_of(Path(outcome["items"][0]["path"])) == pytest.approx(4, abs=0.5)
    # Found by where it is, whatever its address says after the "?".
    payloads = {request.split("payload=")[1] for request in site.requests if request.startswith("/player/v2")}
    assert payloads == {"t2", "t5"} and STREAM_REFRESHED_NOTE.format(count=1) in outcome["notes"]


def test_a_stream_that_ytdlp_found_in_an_embedded_page_is_asked_of_that_page_again(new_site: NewSite, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[tuple[str, str]] = []

    def fake_download(url: str, options: DownloadOptions, *args: Any, **kwargs: Any) -> Any:
        stream = options.page_stream
        asked.append((url, stream.kind if stream else ""))
        outcome = downloader_module.empty_outcome()
        if len(asked) < 3:
            outcome["errors"] = [humanize_ytdlp_error(f"ERROR: {StreamAddressExpired('HTTP Error 403: Forbidden')}", url)]
            outcome["received_bytes"] = 1000
        else:
            outcome["items"] = [{"path": "saved.mp4"}]  # type: ignore[list-item]
        return outcome

    page_site = new_site()
    page_site.add_token_page("watch.html", '<iframe src="/embed/player.html?payload={token}"></iframe>', uses=(0,))
    url = page_site.url("watch.html")
    first = downloader_module.empty_outcome()
    first["errors"] = [humanize_ytdlp_error(f"ERROR: Unsupported URL: {url}", url)]
    monkeypatch.setattr(downloader_module, "download_with_ytdlp", fake_download)
    outcome = downloader_module.download_stream_in_page(url, owner_options(url, tmp_path), first, Progress())
    # The embedded page itself each time, by the address that the page gives it then.
    assert [kind for _, kind in asked] == [FRAME] * 3 and len({address for address, _ in asked}) == 3
    assert outcome["items"] and outcome["notes"][0].startswith("這個網頁沒有直接列出影片") and STREAM_REFRESHED_NOTE.format(count=2) in outcome["notes"]

"""模擬瀏覽器: yt-dlp passing for a browser on the sites that insist on one, and never on YouTube."""

from __future__ import annotations

import importlib
import re
import shutil
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest
import yt_dlp
from yt_dlp.networking import Request
from yt_dlp.networking.common import _REQUEST_HANDLERS
from yt_dlp.networking.exceptions import HTTPError
from yt_dlp.networking.impersonate import ImpersonateTarget

import videocap
import videocap.impersonation as impersonation
from conftest import needs_ffmpeg
from fake_browser import browser_fixture, real_handler, recording, stand_in_handler  # noqa: F401
from offline_web import LocalSite, new_site_fixture, web_media_fixture  # noqa: F401
from offline_youtube import OfflineYouTube, duration_of, offline_media_fixture, offline_youtube_fixture  # noqa: F401
from videocap.downloader import BROWSER_RETRY_FAILED_NOTE, BROWSER_RETRY_WORKED_NOTE, refused_as_a_program, run_download
from videocap.errors import HUMAN_CHECK, STREAM_REFUSED, failure_kind
from videocap.inspector import inspect_link
from videocap.options import OTHER_SITES_OWNER_ONLY_MESSAGE, AccessLimits, DownloadOptions, options_from_spec, parse_extra_ytdlp_args
from videocap.settings import DownloadSettings, JobSpec


YOUTUBE_VIDEO = "https://www.youtube.com/watch?v=abc123def45"
QUIET = {"quiet": True, "logger": impersonation.Silent()}
NewSite = Callable[[], LocalSite]


class Progress:
    def progress(self, value: float, text: str = "") -> None:
        pass


def owner_options(url: str, folder: Path, **settings: Any) -> DownloadOptions:
    spec = JobSpec(url=url, settings=DownloadSettings(**settings), output_dir=str(folder))
    return options_from_spec(spec, AccessLimits())


def download(url: str, folder: Path, **settings: Any) -> Any:
    return run_download(url, owner_options(url, folder, **settings), Progress())


def inspect(url: str, folder: Path, **settings: Any) -> Any:
    return inspect_link(url, owner_options(url, folder, **settings))


@pytest.fixture(name="data_dir")
def data_dir_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The app's data folder, where the switch for 模擬瀏覽器 is kept."""
    folder = tmp_path / "data"
    monkeypatch.setenv("VIDEOCAP_DATA_DIR", str(folder))
    return folder


@pytest.fixture(name="plain_site")
def plain_site_fixture(tmp_path: Path) -> Iterator[LocalSite]:
    """A site of text files, which needs no ffmpeg."""
    (tmp_path / "site").mkdir()
    site = LocalSite(tmp_path / "site", {})
    yield site
    site.close()


def can_impersonate(ydl: Any) -> bool:
    return bool(ydl._impersonate_target_available(ImpersonateTarget()))


def installed(monkeypatch: pytest.MonkeyPatch, handler: Any, loaded: bool = True) -> None:
    """As if curl-cffi were installed and yt-dlp had this handler for it, already taken out of yt-dlp's list."""
    monkeypatch.setattr(impersonation, "_taken", {handler.RH_KEY: handler})
    monkeypatch.setattr(impersonation, "installed_version", lambda: "0.17.0")
    monkeypatch.setattr(impersonation, "package_is_loaded", lambda: loaded)


# Kept away from YouTube


def test_an_ordinary_youtubedl_cannot_pass_for_a_browser(browser: Any) -> None:
    with yt_dlp.YoutubeDL(QUIET) as ydl:  # type: ignore[arg-type]
        assert not can_impersonate(ydl)
    assert impersonation.check() == ""
    assert browser.built == 0


@pytest.mark.parametrize(
    ("url", "other_sites", "expected"),
    [
        ("https://example.com/watch/1", True, True),
        ("https://example.com/hls/index.m3u8", True, True),
        # A visitor, whose links to other sites are refused long before this.
        ("https://example.com/watch/1", False, False),
        (YOUTUBE_VIDEO, True, False),
        ("https://youtu.be/abc123def45", True, False),
        ("https://music.youtube.com/watch?v=abc123def45", True, False),
        # Written without https://, which the app reads as YouTube's.
        ("youtube.com/watch?v=abc123def45", True, False),
        ("abc123def45", True, False),
    ],
)
def test_only_the_owners_link_to_another_site_can_pass_for_a_browser(browser: Any, url: str, other_sites: bool, expected: bool) -> None:
    with impersonation.youtube_dl(yt_dlp.YoutubeDL, dict(QUIET), url, other_sites) as ydl:
        assert can_impersonate(ydl) is expected
        assert isinstance(ydl, yt_dlp.YoutubeDL)
        assert (type(ydl) is not yt_dlp.YoutubeDL) is expected


def test_without_the_package_every_youtubedl_is_ytdlps_own() -> None:
    with impersonation.youtube_dl(yt_dlp.YoutubeDL, dict(QUIET), "https://example.com/watch/1", True) as ydl:
        assert type(ydl) is yt_dlp.YoutubeDL and not can_impersonate(ydl)


def test_without_the_package_a_refusal_is_noted_all_the_same(plain_site: LocalSite) -> None:
    refusals = impersonation.Refusals()
    refused = plain_site.url("forbidden/index.m3u8")
    with impersonation.youtube_dl(yt_dlp.YoutubeDL, dict(QUIET), plain_site.url("watch.html"), True, refusals) as ydl:
        assert not can_impersonate(ydl)
        for address in (refused, plain_site.url("missing.html")):
            with pytest.raises(HTTPError):
                ydl.urlopen(Request(address))
    # The refusal only: a missing page is another matter.
    assert refusals.forbidden == 1 and refusals.addresses == [refused] and not refusals.challenged
    # A YouTube link's run, and a visitor's, are left as they always were.
    for url, other_sites in ((YOUTUBE_VIDEO, True), (plain_site.url("watch.html"), False)):
        with impersonation.youtube_dl(yt_dlp.YoutubeDL, dict(QUIET), url, other_sites, refusals) as ydl:
            assert type(ydl) is yt_dlp.YoutubeDL


def test_a_youtube_link_is_never_told_to_impersonate_the_whole_run(browser: Any) -> None:
    whole_run = {**QUIET, "impersonate": impersonation.any_browser()}
    with impersonation.youtube_dl(yt_dlp.YoutubeDL, dict(whole_run), "https://example.com/watch/1", True) as ydl:
        assert ydl.params["impersonate"] == ImpersonateTarget()
    # yt-dlp refuses to start with the option when it has no handler for it, so this is also what keeps it starting.
    for url, other_sites in ((YOUTUBE_VIDEO, True), ("https://example.com/watch/1", False)):
        with impersonation.youtube_dl(yt_dlp.YoutubeDL, dict(whole_run), url, other_sites) as ydl:
            assert "impersonate" not in ydl.params and not can_impersonate(ydl)


def test_the_owners_own_impersonate_option_is_left_out() -> None:
    extra = parse_extra_ytdlp_args("--impersonate chrome --retries 7")
    assert "impersonate" in extra.ignored and "impersonate" not in extra.params
    assert extra.params["retries"] == 7


def test_youtube_inside_another_sites_job_is_reached_the_ordinary_way(browser: Any, plain_site: LocalSite, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("youtube/captions.vtt", "other/page.txt"):
        (plain_site.root / name).parent.mkdir(parents=True, exist_ok=True)
        (plain_site.root / name).write_bytes(b"WEBVTT\n")
    youtubes, others = plain_site.url("youtube/captions.vtt"), plain_site.url("other/page.txt")
    # The one site stands for both: what is under /youtube/ is YouTube's.
    monkeypatch.setattr(impersonation, "goes_to_youtube", lambda url: "/youtube/" in url)
    asking = {"impersonate": ImpersonateTarget()}

    with impersonation.youtube_dl(yt_dlp.YoutubeDL, dict(QUIET), plain_site.url("page"), True) as ydl:
        # What YouTube's subtitle addresses do: the request itself asks to pass for a browser.
        assert ydl.urlopen(Request(youtubes, extensions=dict(asking))).read() == b"WEBVTT\n"
        assert ydl.urlopen(youtubes).read() == b"WEBVTT\n"
        assert browser.sent == []
        assert ydl.urlopen(Request(others, extensions=dict(asking))).read() == b"WEBVTT\n"
        assert browser.sent == [others]
        # Nothing asked for it, so yt-dlp's ordinary handler took this one.
        ydl.urlopen(others)
        assert browser.sent == [others]

    browser.sent.clear()
    with impersonation.youtube_dl(yt_dlp.YoutubeDL, {**QUIET, "impersonate": ImpersonateTarget()}, plain_site.url("page"), True) as ydl:
        # The whole run passes for a browser, and still not what goes to YouTube.
        ydl.urlopen(Request(youtubes, extensions=dict(asking)))
        ydl.urlopen(youtubes)
        assert browser.sent == []
        ydl.urlopen(others)
        assert browser.sent == [others]


@pytest.mark.parametrize("domain", ["www.youtube.com", "youtu.be", "rr3---sn-abc.googlevideo.com", "i.ytimg.com", "youtubei.googleapis.com", "accounts.google.com"])
def test_youtubes_own_hosts_are_known(domain: str) -> None:
    assert impersonation.goes_to_youtube(f"https://{domain}/path")
    # Another site's host that only has the name in it, and another site's page about it.
    assert not impersonation.goes_to_youtube(f"https://{domain}.example.org/path")
    assert not impersonation.goes_to_youtube(f"https://example.org/{domain}")
    assert not impersonation.goes_to_youtube("https://notyoutube.com/watch")


def download_with_captions(youtube: OfflineYouTube, site: LocalSite, folder: Path, limits: AccessLimits) -> None:
    """Downloads a YouTube video whose subtitle address asks to be fetched as a browser would, as YouTube's extractor
    lists every one of them. The subtitle file is on `site`, so fetching it is a real request."""
    shutil.copyfile(youtube.media / "sub.en.vtt", site.root / "captions.vtt")
    url = youtube.add_video("abc123def45", "With captions")
    youtube.videos[url]["subtitles"] = {"en": [{"ext": "vtt", "url": site.url("captions.vtt"), "impersonate": True}]}
    settings = DownloadSettings(subtitle_languages="en", subtitle_format="vtt", embed_subtitles=False)
    spec = JobSpec(url=url, settings=settings, output_dir=str(folder / "out"))
    outcome = run_download(url, options_from_spec(spec, limits), Progress())

    assert outcome["errors"] == []
    (subtitle,) = outcome["items"][0]["subtitle_paths"]
    assert "Hello" in Path(subtitle).read_text(encoding="utf-8")
    assert site.asked_for("captions.vtt") == 1


@needs_ffmpeg
@pytest.mark.parametrize("limits", [AccessLimits(), AccessLimits.for_public_host()], ids=["owner", "visitor"])
def test_a_youtube_download_with_subtitles_never_meets_the_handler(
    browser: Any, offline_youtube: OfflineYouTube, plain_site: LocalSite, tmp_path: Path, limits: AccessLimits
) -> None:
    download_with_captions(offline_youtube, plain_site, tmp_path, limits)
    assert browser.built == 0 and browser.sent == []


@needs_ffmpeg
def test_left_in_ytdlps_list_the_handler_would_fetch_youtubes_subtitles(
    offline_youtube: OfflineYouTube, plain_site: LocalSite, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # What the test above guards against, shown happening: yt-dlp with the package, and without the app's care.
    handler = recording(stand_in_handler())
    monkeypatch.setitem(_REQUEST_HANDLERS, handler.RH_KEY, handler)
    download_with_captions(offline_youtube, plain_site, tmp_path, AccessLimits())
    assert handler.sent == [plain_site.url("captions.vtt")]


# The check that it is kept away, and the switch


def test_a_ytdlp_that_keeps_the_handler_in_its_list_switches_it_off(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    handler = recording(stand_in_handler())
    installed(monkeypatch, handler)
    monkeypatch.setattr(impersonation, "_taken", {})
    monkeypatch.setitem(_REQUEST_HANDLERS, handler.RH_KEY, handler)
    # As if a later yt-dlp built its handlers from somewhere this module does not reach.
    monkeypatch.setattr(impersonation, "take_from_ytdlp", lambda: None)

    impersonation.load()

    assert "也找得到模擬瀏覽器" in impersonation.problem()
    assert not impersonation.ready()
    assert impersonation.switched_off() == (impersonation.BY_CHECK, impersonation.problem())
    # No link gets the handler from the app any more.
    with impersonation.youtube_dl(yt_dlp.YoutubeDL, dict(QUIET), "https://example.com/watch/1", True) as ydl:
        assert type(ydl) is yt_dlp.YoutubeDL


def test_a_check_that_cannot_be_made_counts_as_failed(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    installed(monkeypatch, recording(stand_in_handler()))

    def renamed(ydl: Any, target: Any) -> bool:
        raise AttributeError("'YoutubeDL' object has no attribute '_impersonate_target_available'")

    monkeypatch.setattr(impersonation.PlainYoutubeDL, "_impersonate_target_available", renamed)
    impersonation.load()
    assert impersonation.problem().startswith("無法確認") and not impersonation.ready()
    assert impersonation.switched_off()[0] == impersonation.BY_CHECK


def test_ytdlp_without_what_the_module_leans_on_still_lets_the_app_open(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    installed(monkeypatch, recording(stand_in_handler()))
    monkeypatch.setattr(impersonation, "MISSING_FROM_YTDLP", "cannot import name '_REQUEST_HANDLERS'")
    impersonation.load()
    assert "_REQUEST_HANDLERS" in impersonation.problem() and not impersonation.ready()
    assert impersonation.switched_off()[0] == impersonation.BY_CHECK


def test_without_the_package_nothing_is_wrong_and_nothing_is_switched_off(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(impersonation, "_taken", {})
    monkeypatch.setattr(impersonation, "package_is_loaded", lambda: False)
    monkeypatch.setattr(impersonation, "MISSING_FROM_YTDLP", "cannot import name '_REQUEST_HANDLERS'")
    impersonation.load()
    assert impersonation.problem() == "" and not impersonation.ready()
    assert impersonation.switched_off() == ("", "") and not videocap.impersonation_switch().exists()


def test_the_owner_switches_it_off_and_on_again(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    installed(monkeypatch, recording(stand_in_handler()), loaded=False)
    impersonation.load()
    assert impersonation.ready() and impersonation.switched_off() == ("", "")

    impersonation.switch_off()
    assert not impersonation.ready() and impersonation.switched_off() == (impersonation.BY_OWNER, "")
    with impersonation.youtube_dl(yt_dlp.YoutubeDL, dict(QUIET), "https://example.com/watch/1", True) as ydl:
        assert not can_impersonate(ydl)

    impersonation.switch_on()
    assert impersonation.ready() and not videocap.impersonation_switch().exists()


def test_a_package_removed_while_the_app_runs_is_no_longer_used(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    installed(monkeypatch, recording(stand_in_handler()), loaded=False)
    monkeypatch.setattr(impersonation, "installed_version", lambda: "")
    impersonation.load()
    assert not impersonation.ready() and impersonation.problem() == ""


def test_switched_off_the_package_cannot_be_imported_at_the_next_start(data_dir: Path) -> None:
    missing = object()
    before = sys.modules.pop("curl_cffi", missing)
    try:
        videocap.keep_curl_cffi_out()
        assert "curl_cffi" not in sys.modules

        impersonation.write_switch(impersonation.BY_OWNER)
        videocap.keep_curl_cffi_out()
        # What yt-dlp does when it starts, and takes for the package not being installed.
        with pytest.raises(ImportError):
            importlib.import_module("curl_cffi")
    finally:
        sys.modules.pop("curl_cffi", None)
        if before is not missing:
            sys.modules["curl_cffi"] = before  # type: ignore[assignment]


# Refused as a program, tried once more as a browser


@needs_ffmpeg
def test_a_page_behind_a_check_for_bots_downloads_at_the_second_try(browser: Any, new_site: NewSite, tmp_path: Path) -> None:
    site = new_site()
    site.add_clip("cf/clip.mp4")
    url = site.add_page("cf/watch.html", title="Checked")
    assert inspect(url, tmp_path)["error"] == ""
    outcome = download(url, tmp_path)

    assert outcome["errors"] == [] and BROWSER_RETRY_WORKED_NOTE in outcome["notes"]
    assert duration_of(Path(outcome["items"][0]["path"])) == pytest.approx(2, abs=0.3)
    # Inspecting and downloading were each refused once and let in once.
    assert site.asked_for("cf/watch.html") == 4 and site.asked_as_a_browser("cf/watch.html") == 2
    # The page's file came as to a browser too, which yt-dlp's own switch for an ordinary page does not do.
    assert site.asked_as_a_browser("cf/clip.mp4") >= 1


@needs_ffmpeg
@pytest.mark.parametrize("folder", ["cf", "browsers-only"], ids=["answered as by Cloudflare", "a bare 403"])
@pytest.mark.parametrize("kind", ["file", "stream"])
def test_a_direct_link_only_a_browser_may_fetch_downloads_at_the_second_try(
    browser: Any, new_site: NewSite, tmp_path: Path, folder: str, kind: str
) -> None:
    site = new_site()
    url = site.add_clip(f"{folder}/clip.mp4") if kind == "file" else site.add_stream(f"{folder}/hls")
    assert inspect(url, tmp_path)["error"] == ""
    outcome = download(url, tmp_path)

    assert outcome["errors"] == [] and BROWSER_RETRY_WORKED_NOTE in outcome["notes"]
    assert duration_of(Path(outcome["items"][0]["path"])) == pytest.approx(2, abs=0.3)
    if kind == "stream":
        # The playlist again and every segment: all of them are refused to a program.
        segments = site.segments(f"{folder}/hls")
        assert segments and all(site.asked_as_a_browser(segment) == 1 for segment in segments)
        assert site.asked_as_a_browser(f"{folder}/hls/index.m3u8") >= 2


@needs_ffmpeg
@pytest.mark.parametrize("refused", ["the playlist and its segments", "the segments"])
def test_a_stream_refused_after_its_page_was_read_downloads_at_the_second_try(browser: Any, new_site: NewSite, tmp_path: Path, refused: str) -> None:
    site = new_site()
    if refused == "the segments":
        site.add_stream("hls", segments_in="browsers-only/parts")
        stream, segments = "hls/index.m3u8", site.segments("browsers-only/parts")
    else:
        site.add_stream("browsers-only/hls")
        stream, segments = "browsers-only/hls/index.m3u8", site.segments("browsers-only/hls")
    # The page is open to anyone, so nothing yt-dlp says of the failure names a check for bots.
    url = site.add_page("watch.html", (stream,), title="Open page")
    outcome = download(url, tmp_path)

    assert outcome["errors"] == [] and BROWSER_RETRY_WORKED_NOTE in outcome["notes"]
    assert duration_of(Path(outcome["items"][0]["path"])) == pytest.approx(2, abs=0.3)
    assert segments and all(site.asked_as_a_browser(segment) == 1 for segment in segments)
    assert site.asked_for("watch.html") == 2


@needs_ffmpeg
def test_a_site_that_wants_a_person_says_so_after_one_more_try(browser: Any, new_site: NewSite, tmp_path: Path) -> None:
    site = new_site()
    site.add_clip("captcha/clip.mp4")
    url = site.add_page("captcha/watch.html")
    report, outcome = inspect(url, tmp_path), download(url, tmp_path)

    for error in (report["error"], *outcome["errors"]):
        assert error.startswith("這個網站要求真人驗證") and failure_kind(error) == HUMAN_CHECK
    assert len(outcome["errors"]) == 1 and BROWSER_RETRY_FAILED_NOTE in outcome["notes"]
    # Once as a program and once as a browser, for each of the two; no third try.
    assert site.asked_for("captcha/watch.html") == 4 and site.asked_as_a_browser("captcha/watch.html") == 2


@needs_ffmpeg
def test_a_stream_that_wants_a_person_says_so_too(browser: Any, new_site: NewSite, tmp_path: Path) -> None:
    site = new_site()
    site.add_stream("captcha/hls")
    (error,) = download(site.add_page("watch.html", ("captcha/hls/index.m3u8",)), tmp_path)["errors"]
    assert error.startswith("這個網站要求真人驗證")


@needs_ffmpeg
def test_a_plain_refusal_stays_a_plain_refusal_after_one_more_try(browser: Any, new_site: NewSite, tmp_path: Path) -> None:
    site = new_site()
    url = site.add_clip("forbidden/clip.mp4")
    report, outcome = inspect(url, tmp_path), download(url, tmp_path)

    for error in (report["error"], *outcome["errors"]):
        # What a refusal has always been told: a login, an expired link, or a site that does not allow it.
        assert error.startswith("這個網站拒絕了下載請求（HTTP 403）") and "真人驗證" not in error
    assert len(outcome["errors"]) == 1 and BROWSER_RETRY_FAILED_NOTE in outcome["notes"]
    assert site.asked_for("forbidden/clip.mp4") == 4 and site.asked_as_a_browser("forbidden/clip.mp4") == 2


@needs_ffmpeg
def test_a_stream_refused_after_its_page_let_a_browser_in_is_told_as_the_streams(browser: Any, new_site: NewSite, tmp_path: Path) -> None:
    site = new_site()
    url = site.add_listing_page("cf/watch.html", site.add_playlist("forbidden/index.m3u8"))
    report, outcome = inspect(url, tmp_path), download(url, tmp_path)

    for error in (report["error"], *outcome["errors"]):
        # The first try was refused the page. What the last one was refused is what the person is told of.
        assert failure_kind(error) == STREAM_REFUSED and "真人驗證" not in error
    assert len(outcome["errors"]) == 1 and BROWSER_RETRY_FAILED_NOTE in outcome["notes"]
    # The page: once as a program and once as a browser, for each of the two; each also opens the page once more
    # afterwards, as the browser it already passed for, looking for a better stream in an iframe of its own.
    assert site.asked_for("cf/watch.html") == 6 and site.asked_as_a_browser("cf/watch.html") == 4


@needs_ffmpeg
def test_without_the_package_a_refused_link_is_tried_once(new_site: NewSite, tmp_path: Path) -> None:
    site = new_site()
    url = site.add_clip("cf/clip.mp4")
    report, outcome = inspect(url, tmp_path), download(url, tmp_path)

    for error in (report["error"], outcome["errors"][0]):
        # The owner is told where to get what the site wants.
        assert error.startswith("這個網站需要模擬瀏覽器") and impersonation.INSTALL_ADVICE in error
    assert not any("模擬瀏覽器" in note for note in outcome["notes"])
    assert site.asked_for("cf/clip.mp4") == 2 and site.browser_requests == []


def test_what_the_owner_is_told_to_do_follows_what_is_installed(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(impersonation, "installed_version", lambda: "")
    assert impersonation.advice(True) == impersonation.INSTALL_ADVICE
    # A visitor gets no other site, and has no 系統 page to install from.
    assert impersonation.advice(False) == ""

    monkeypatch.setattr(impersonation, "installed_version", lambda: "0.17.0")
    monkeypatch.setattr(impersonation, "_unsupported", "Only curl_cffi versions 0.5.10 and 0.10.x through 0.17.x are supported")
    assert impersonation.advice(True) == impersonation.UNSUPPORTED_ADVICE
    monkeypatch.setattr(impersonation, "_unsupported", "")
    assert impersonation.advice(True) == impersonation.RESTART_ADVICE
    impersonation.write_switch(impersonation.BY_OWNER)
    assert impersonation.advice(True) == impersonation.SWITCHED_OFF_ADVICE

    # Ready: nothing is left to do, and nothing is said.
    monkeypatch.setattr(impersonation, "_kept", (impersonation.leaving_youtube_alone(recording(stand_in_handler())),))
    assert impersonation.advice(True) == ""


def test_a_visitors_link_to_another_site_is_refused_before_any_request(browser: Any, plain_site: LocalSite, tmp_path: Path) -> None:
    url = plain_site.url("cf/watch.html")
    spec = JobSpec(url=url, settings=DownloadSettings(), output_dir=str(tmp_path))
    with pytest.raises(ValueError, match=re.escape(OTHER_SITES_OWNER_ONLY_MESSAGE)):
        options_from_spec(spec, AccessLimits.for_public_host())
    # And were the options made all the same, yt-dlp is told of no site but YouTube.
    outcome = run_download(url, replace(owner_options(url, tmp_path), other_sites=False), Progress())
    assert outcome["errors"] and not outcome["items"]
    assert plain_site.requests == [] and browser.built == 0


@needs_ffmpeg
def test_a_refusal_from_youtube_is_never_tried_as_a_browser(browser: Any, offline_youtube: OfflineYouTube, tmp_path: Path) -> None:
    url = offline_youtube.add_video("abc123def45", "Refused")
    offline_youtube.refused_unless_client["abc123def45"] = "a client nothing asks for"
    outcome = run_download(url, owner_options(url, tmp_path, download_subtitles=False), Progress())

    assert any("HTTP 403" in error or "403" in error for error in outcome["errors"])
    assert browser.built == 0 and not any("模擬瀏覽器" in note for note in outcome["notes"])
    # YouTube's own second try, with other player clients, still happens.
    assert len(offline_youtube.extractions) > 1


def test_what_is_tried_once_more_as_a_browser(browser: Any, tmp_path: Path) -> None:
    url = "https://example.com/watch/1"
    options = owner_options(url, tmp_path)
    nothing: Any = {"stopped": False, "items": [], "warnings": []}
    # A stream's refused segments are skipped, and all yt-dlp says in the end is this.
    failed = [("ERROR: The downloaded file is empty", "")]
    answered_403, never_refused = impersonation.Refusals(forbidden=3), impersonation.Refusals()

    assert refused_as_a_program(url, options, nothing, failed, answered_403)

    # A failure that no refusal by the site led to, such as a missing page or a full disk.
    assert not refused_as_a_program(url, options, nothing, failed, never_refused)
    # A refused extra, such as a cover picture, in a run that did not fail.
    assert not refused_as_a_program(url, options, nothing, [], answered_403)
    assert not refused_as_a_program(url, options, {**nothing, "stopped": True}, failed, answered_403)
    # A list that was partly saved is not read all over again.
    assert not refused_as_a_program(url, options, {**nothing, "items": [{"path": "saved.mp4"}]}, failed, answered_403)
    # Once: the try that passed for a browser is the last.
    assert not refused_as_a_program(url, replace(options, impersonate=True), nothing, failed, answered_403)
    assert not refused_as_a_program(url, replace(options, other_sites=False), nothing, failed, answered_403)
    assert not refused_as_a_program(YOUTUBE_VIDEO, owner_options(YOUTUBE_VIDEO, tmp_path), nothing, failed, answered_403)


# With the real package


def test_ytdlp_takes_the_installed_curl_cffi(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    handler = real_handler(monkeypatch)
    impersonation.load()
    assert impersonation.unsupported() == "" and impersonation.problem() == ""
    assert impersonation.ready() and impersonation.installed_version()
    assert not any(issubclass(listed, type(handler)) or listed is handler for listed in _REQUEST_HANDLERS.values())
    with yt_dlp.YoutubeDL(QUIET) as ydl:  # type: ignore[arg-type]
        assert not can_impersonate(ydl)
    with impersonation.youtube_dl(yt_dlp.YoutubeDL, dict(QUIET), "https://example.com/watch/1", True) as ydl:
        assert can_impersonate(ydl)

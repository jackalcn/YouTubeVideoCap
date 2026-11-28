"""A stream whose fragments fail, on a web site of this machine: a stop is seen while they do, and a stream whose
fragments keep failing is given up rather than run through to its end."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest

import videocap.options as options_module
from conftest import needs_ffmpeg
from offline_web import LocalSite, new_site_fixture, web_media_fixture  # noqa: F401
from videocap.config import FRAGMENT_FAILURES_LIMIT
from videocap.downloader import FragmentsKeepFailing, JobStopped, YtDlpLogCollector, run_download
from videocap.errors import FRAGMENTS_FAILED, HTTP_403, add_referer_advice, failure_kind, humanize_ytdlp_error, is_network_loss, referer_advice
from videocap.options import AccessLimits, DownloadOptions, options_from_spec
from videocap.settings import DownloadSettings, JobSpec


NewSite = Callable[[], LocalSite]
# What yt-dlp says of a fragment it tried again, and of one it gave up on.
GOT_ERROR = "[download] Got error: HTTP Error 403: Forbidden. Retrying fragment {n} (1/1)..."
SKIPPED = "[download] fragment not found; Skipping fragment {n} ..."


class Progress:
    def progress(self, value: float, text: str = "") -> None:
        pass


@pytest.fixture(name="plain_site")
def plain_site_fixture(tmp_path: Path) -> Iterator[LocalSite]:
    """A site of text files, which needs no ffmpeg."""
    (tmp_path / "site").mkdir()
    site = LocalSite(tmp_path / "site", {})
    yield site
    site.close()


@pytest.fixture(autouse=True)
def no_pause_before_a_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(options_module, "_retry_delay", lambda n: 0.0)


def owner_options(url: str, folder: Path, **settings: Any) -> DownloadOptions:
    spec = JobSpec(url=url, settings=DownloadSettings(**{"retry_count": 1, **settings}), output_dir=str(folder))
    return options_from_spec(spec, AccessLimits())


def stream_with_refused_fragments(site: LocalSite, refused: int, after: tuple[str, ...] = ()) -> str:
    """A stream's playlist whose first fragment comes, as it does from a site that refuses only the rest, and whose next
    `refused` fragments are refused (HTTP 403); then the ones `after`, which must be added to the site. The first is
    a few bytes of nothing, which a download that is given up never needs to be more than."""
    (site.root / "hls").mkdir(parents=True, exist_ok=True)
    (site.root / "hls" / "first.ts").write_bytes(b"\x47" + b"\xff" * 187)
    names = ["first.ts", *(f"/forbidden/{number}.ts" for number in range(1, refused + 1)), *after]
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:2", "#EXT-X-MEDIA-SEQUENCE:0", "#EXT-X-PLAYLIST-TYPE:VOD"]
    for name in names:
        lines += ["#EXTINF:1.0,", name]
    (site.root / "hls" / "index.m3u8").write_text("\n".join([*lines, "#EXT-X-ENDLIST"]) + "\n", encoding="utf-8")
    return site.url("hls/index.m3u8")


def refused_asked(site: LocalSite, refused: int) -> list[int]:
    """How often each refused fragment was asked for."""
    return [site.asked_for(f"forbidden/{number}.ts") for number in range(1, refused + 1)]


# What the log tells


def test_a_stop_is_seen_in_the_lines_of_a_download_and_of_a_wait_and_in_no_others() -> None:
    logger = YtDlpLogCollector()
    logger.should_stop = lambda: True
    for line in (GOT_ERROR.format(n=5), SKIPPED.format(n=5), "[download] Destination: a.mp4", "[wait] Remaining time until next attempt: 00:10"):
        with pytest.raises(JobStopped):
            logger.debug(line)
    # A step after the download is not cut short, which would strand its files.
    for line in ("[Merger] Merging formats into a.mp4", "[FixupM3u8] Fixing MPEG-TS in MP4 container", "[info] Downloading 1 format(s): 0"):
        logger.debug(line)
    assert logger.lines[-1] == "[info] Downloading 1 format(s): 0"


def test_fragments_given_up_one_after_another_end_the_download_with_the_last_failure() -> None:
    logger = YtDlpLogCollector()
    for number in range(1, FRAGMENT_FAILURES_LIMIT):
        logger.debug(GOT_ERROR.format(n=number))
        logger.debug(SKIPPED.format(n=number))
    with pytest.raises(FragmentsKeepFailing) as raised:
        logger.debug(SKIPPED.format(n=FRAGMENT_FAILURES_LIMIT))
    assert str(raised.value).endswith(f"{FRAGMENT_FAILURES_LIMIT} in a row could not be downloaded (HTTP Error 403: Forbidden), and the rest were not tried")
    # The line that it ended at is in the job's log, as every other is.
    assert logger.lines[-1] == SKIPPED.format(n=FRAGMENT_FAILURES_LIMIT)


def test_a_fragment_that_comes_in_between_starts_the_count_again() -> None:
    logger = YtDlpLogCollector()
    for _ in range(3):
        for number in range(1, FRAGMENT_FAILURES_LIMIT):
            logger.debug(SKIPPED.format(n=number))
        logger.data_arrived()
    assert logger.fragments_failed == 0


@pytest.mark.parametrize("lost", ["Connection reset by peer", "The read operation timed out", "[WinError 10054] An existing connection was forcibly closed"])
def test_fragments_that_failed_as_the_connection_was_lost_are_told_as_a_lost_connection(lost: str) -> None:
    told = humanize_ytdlp_error(f"ERROR: {FragmentsKeepFailing(FRAGMENT_FAILURES_LIMIT, lost)}", "https://cdn.example/hls/index.m3u8")
    # Which the queue tries again by itself, later, from the last fragment that came.
    assert is_network_loss(told) and failure_kind(told) != FRAGMENTS_FAILED


def test_a_stream_given_up_on_is_told_as_such_and_not_as_a_refusal_of_the_link() -> None:
    raised = FragmentsKeepFailing(FRAGMENT_FAILURES_LIMIT, "HTTP Error 403: Forbidden")
    told = humanize_ytdlp_error(f"ERROR: {raised}", "https://cdn.example/hls/index.m3u8")
    assert told.startswith(f"這個網站的串流片段連續 {FRAGMENT_FAILURES_LIMIT} 段下載失敗") and failure_kind(told) == FRAGMENTS_FAILED
    assert "HTTP Error 403: Forbidden" in told and failure_kind(humanize_ytdlp_error("ERROR: HTTP Error 403: Forbidden", "https://cdn.example/a.mp4")) == HTTP_403
    assert humanize_ytdlp_error(f"ERROR: {raised}", "https://www.youtube.com/watch?v=abcdefghijk").startswith("YouTube 的串流片段")
    # A Referer that is a stream's own address may be why the site refused the fragments, and is said to be.
    assert add_referer_advice(told, "https://cdn.example/hls/index.m3u8").endswith(referer_advice("https://cdn.example/hls/index.m3u8"))


# A stream of this machine whose fragments fail


def test_a_stream_whose_fragments_keep_failing_is_given_up_after_a_few_in_a_row(plain_site: LocalSite, tmp_path: Path) -> None:
    url = stream_with_refused_fragments(plain_site, refused=30)
    began = time.monotonic()
    outcome = run_download(url, owner_options(url, tmp_path), Progress())
    assert outcome["items"] == [] and not outcome["stopped"] and time.monotonic() - began < 30
    error = outcome["errors"][0]
    assert failure_kind(error) == FRAGMENTS_FAILED and "HTTP Error 403: Forbidden" in error
    # Each tried once and once again, then given up; none after the limit asked for at all.
    asked = refused_asked(plain_site, 30)
    assert asked[:FRAGMENT_FAILURES_LIMIT] == [2] * FRAGMENT_FAILURES_LIMIT and sum(asked[FRAGMENT_FAILURES_LIMIT:]) == 0


def test_a_job_stopped_while_its_fragments_fail_ends_at_once(plain_site: LocalSite, tmp_path: Path) -> None:
    url = stream_with_refused_fragments(plain_site, refused=30)
    outcome = run_download(url, owner_options(url, tmp_path), Progress(), should_stop=lambda: plain_site.asked_for("forbidden/3.ts") > 0)
    assert outcome["stopped"] and outcome["items"] == []
    # Seen at the first word of the third fragment's failure: it was not tried again, and nothing after it was asked for.
    assert refused_asked(plain_site, 30)[:4] == [2, 2, 1, 0] and sum(refused_asked(plain_site, 30)) == 5


def test_a_stop_during_the_pause_before_a_retry_does_not_wait_it_out(plain_site: LocalSite, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(options_module, "_retry_delay", lambda n: 30.0)
    url = stream_with_refused_fragments(plain_site, refused=30)
    first_refusal: list[float] = []

    def stop_once_half_a_second_into_the_pause() -> bool:
        if plain_site.asked_for("forbidden/1.ts") and not first_refusal:
            first_refusal.append(time.monotonic())
        return bool(first_refusal) and time.monotonic() - first_refusal[0] > 0.5

    began = time.monotonic()
    outcome = run_download(url, owner_options(url, tmp_path), Progress(), should_stop=stop_once_half_a_second_into_the_pause)
    assert outcome["stopped"] and time.monotonic() - began < 10
    assert refused_asked(plain_site, 2) == [1, 0]


@needs_ffmpeg
def test_a_stream_missing_fewer_fragments_in_a_row_than_the_limit_still_downloads(new_site: NewSite, tmp_path: Path) -> None:
    site = new_site()
    site.add_stream("real")
    segments = [path.rsplit("/", 1)[1] for path in site.segments("real")]
    # The real stream's own fragments, with as many refused in a row between them as are let pass.
    url = stream_with_refused_fragments(site, refused=FRAGMENT_FAILURES_LIMIT - 1, after=tuple(f"/real/{name}" for name in segments))
    outcome = run_download(url, owner_options(url, tmp_path), Progress())
    assert outcome["errors"] == [] and len(outcome["items"]) == 1 and Path(outcome["items"][0]["path"]).is_file()
    assert refused_asked(site, FRAGMENT_FAILURES_LIMIT - 1) == [2] * (FRAGMENT_FAILURES_LIMIT - 1)
    assert all(site.asked_for(f"real/{name}") == 1 for name in segments)

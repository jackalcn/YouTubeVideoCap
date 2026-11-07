from __future__ import annotations

import os
import tempfile
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

import pytest

import videocap.downloader as downloader_module
import videocap.options as options_module
import videocap.services as services_module
from offline_youtube import OfflineYouTube, offline_media_fixture, offline_youtube_fixture  # noqa: F401
from videocap.downloader import DownloadOutcome, empty_outcome
from videocap.errors import humanize_ytdlp_error
from videocap.jobs import CANCELED, DONE, FAILED, PAUSED, QUEUED, RUNNING, Job
from videocap.media_tools import ToolRequest, ToolResult
from videocap.options import AccessLimits, LoginSource
from videocap.services import LOCAL_OWNER, AppServices, DownloadPayload, temporary_cookie_file
from videocap.settings import DownloadSettings, JobSpec
from videocap.storage import PENDING_JOBS_KEY, DownloadWindow, Subscription
from videocap.subscription_import import ImportedChannel
from videocap.tooling import UpdateResult


URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"


def wait_until(condition: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not met in time")


def finished_outcome(path: Path, **overrides: Any) -> DownloadOutcome:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"video-bytes")
    outcome = empty_outcome()
    outcome["items"] = [
        {
            "title": "Title",
            "path": str(path),
            "subtitle_paths": [],
            "extra_paths": [],
            "video_id": "jNQXAC9IVRw",
            "channel": "Channel",
            "webpage_url": URL,
            "duration": 19.0,
            "upload_date": "20050424",
            "thumbnail": "",
            "quality": "mp4",
        }
    ]
    outcome.update(overrides)  # type: ignore[typeddict-item]
    return outcome


class FakeRun:
    """Replaces the real download, recording what the job would have downloaded."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.result: Callable[[str, Any, Any], DownloadOutcome] = lambda url, options, should_stop: finished_outcome(options.output_dir / "video.mp4")

    def __call__(self, url: str, options: Any, progress_ui: Any, status_ui: Any, should_stop: Any, on_log: Any) -> DownloadOutcome:
        self.calls.append((url, options))
        return self.result(url, options, should_stop)


@pytest.fixture(autouse=True)
def isolated_services(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Iterator[list[AppServices]]:
    """Keeps every AppServices of a test away from the machine's real temp folder, and stops its threads afterwards,
    because AppServices purges old download and cookie folders there."""
    temp_dir = tmp_path_factory.mktemp("temp")
    monkeypatch.setattr(tempfile, "tempdir", str(temp_dir))
    monkeypatch.setattr(services_module, "CLOUD_OUTPUT_ROOT", temp_dir / "youtube-downloader-files")
    created: list[AppServices] = []
    original_init = AppServices.__init__

    def tracking_init(self: AppServices, *args: Any, **kwargs: Any) -> None:
        created.append(self)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(AppServices, "__init__", tracking_init)
    yield created
    for services in created:
        services.shutdown()


@pytest.fixture
def fake_run(monkeypatch: pytest.MonkeyPatch) -> FakeRun:
    fake = FakeRun()
    monkeypatch.setattr(services_module, "run_download", fake)
    return fake


@pytest.fixture
def services(tmp_path: Path, fake_run: FakeRun) -> AppServices:
    return AppServices(is_public=False, data_dir=tmp_path / "data")


def make_spec(tmp_path: Path, **overrides: Any) -> JobSpec:
    return JobSpec(**{"url": URL, "settings": DownloadSettings(), "output_dir": str(tmp_path / "out"), **overrides})


def test_finished_download_is_saved_to_history(services: AppServices, tmp_path: Path) -> None:
    job = services.submit_download(make_spec(tmp_path), LOCAL_OWNER, AccessLimits())
    wait_until(lambda: job.state == DONE)
    wait_until(lambda: services.storage is not None and bool(services.storage.list_history()))
    assert services.storage is not None
    entry = services.storage.list_history()[0]
    assert entry.title == "Title"
    assert entry.size_bytes == len(b"video-bytes")


def test_failed_download_is_not_saved(services: AppServices, tmp_path: Path, fake_run: FakeRun) -> None:
    def fail(url: str, options: Any, should_stop: Any) -> DownloadOutcome:
        outcome = empty_outcome()
        outcome["errors"] = ["失敗"]
        return outcome

    fake_run.result = fail
    job = services.submit_download(make_spec(tmp_path), LOCAL_OWNER, AccessLimits())
    wait_until(lambda: job.state == FAILED)
    assert services.storage is not None
    assert services.storage.list_history() == []


def test_a_download_folder_that_cannot_be_written_fails_the_job_at_once(services: AppServices, tmp_path: Path, fake_run: FakeRun) -> None:
    blocker = tmp_path / "a-file"
    blocker.write_text("not a folder", encoding="utf-8")
    folder = blocker / "videos"
    job = services.submit_download(make_spec(tmp_path, output_dir=str(folder)), LOCAL_OWNER, AccessLimits())
    wait_until(lambda: job.state == FAILED)
    assert job.error == f"下載資料夾無法寫入：{folder}。請換一個資料夾，或檢查資料夾權限。"
    assert fake_run.calls == []


def test_a_public_visitor_never_sees_the_server_folder_that_cannot_be_written(
    tmp_path: Path, fake_run: FakeRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocker = tmp_path / "a-file"
    blocker.write_text("not a folder", encoding="utf-8")
    monkeypatch.setattr(services_module, "CLOUD_OUTPUT_ROOT", blocker / "public")
    services = AppServices(is_public=True)
    job = services.submit_download(make_spec(tmp_path), "visitor", AccessLimits.for_public_host())
    wait_until(lambda: job.state == FAILED)
    assert "無法寫入" in job.error
    assert str(tmp_path) not in job.error and "a-file" not in job.error
    assert fake_run.calls == []


def test_partly_failed_playlist_counts_as_done(services: AppServices, tmp_path: Path, fake_run: FakeRun) -> None:
    fake_run.result = lambda url, options, should_stop: finished_outcome(options.output_dir / "a.mp4", errors=["一部失敗"], failed_ids=["aaaaaaaaaaa"])
    job = services.submit_download(make_spec(tmp_path), LOCAL_OWNER, AccessLimits())
    wait_until(lambda: job.state == DONE)


def test_nothing_new_is_not_a_failure(services: AppServices, tmp_path: Path, fake_run: FakeRun) -> None:
    fake_run.result = lambda url, options, should_stop: empty_outcome()
    job = services.submit_download(make_spec(tmp_path), LOCAL_OWNER, AccessLimits())
    wait_until(lambda: job.state == DONE)


def test_bad_settings_are_refused_before_queueing(services: AppServices, tmp_path: Path) -> None:
    spec = make_spec(tmp_path, settings=DownloadSettings(time_ranges="soon"))
    with pytest.raises(ValueError):
        services.submit_download(spec, LOCAL_OWNER, AccessLimits())
    assert services.manager.list_jobs() == []


def stoppable(fake_run: FakeRun, partial: list[Path]) -> None:
    def run(url: str, options: Any, should_stop: Any) -> DownloadOutcome:
        for path in partial:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"partial")
        while not should_stop():
            time.sleep(0.005)
        outcome = empty_outcome()
        outcome["stopped"] = True
        outcome["partial_paths"] = [str(path) for path in partial]
        return outcome

    fake_run.result = run


def test_cancel_removes_partial_downloads(services: AppServices, tmp_path: Path, fake_run: FakeRun) -> None:
    partial = tmp_path / "out" / "video.f137.mp4"
    stoppable(fake_run, [partial])
    job = services.submit_download(make_spec(tmp_path), LOCAL_OWNER, AccessLimits())
    wait_until(partial.exists)
    services.manager.cancel(job.id)
    wait_until(lambda: job.state == CANCELED)
    wait_until(lambda: not partial.exists())


def test_pause_keeps_partial_downloads_for_resuming(services: AppServices, tmp_path: Path, fake_run: FakeRun) -> None:
    partial = tmp_path / "out" / "video.f137.mp4"
    stoppable(fake_run, [partial])
    job = services.submit_download(make_spec(tmp_path), LOCAL_OWNER, AccessLimits())
    wait_until(partial.exists)
    services.manager.pause(job.id)
    wait_until(lambda: job.state == PAUSED)
    assert partial.exists()


def test_unfinished_jobs_come_back_paused_after_a_restart(services: AppServices, tmp_path: Path, fake_run: FakeRun) -> None:
    stoppable(fake_run, [])
    job = services.submit_download(make_spec(tmp_path, browser="firefox"), LOCAL_OWNER, AccessLimits())
    wait_until(lambda: job.started_at is not None)
    assert services.storage is not None
    wait_until(lambda: bool(services.storage.get_value(PENDING_JOBS_KEY)))  # type: ignore[union-attr]
    saved = services.storage.get_value(PENDING_JOBS_KEY)
    assert saved[0]["spec"]["url"] == URL

    # The old process ends, and with it its hold on the data folder.
    services.shutdown()
    restarted = AppServices(is_public=False, data_dir=tmp_path / "data")
    jobs = restarted.manager.list_jobs()
    assert [restored.state for restored in jobs] == [PAUSED]
    payload = jobs[0].payload
    assert isinstance(payload, DownloadPayload)
    assert payload.spec.resuming is True
    assert jobs[0].exclusive is True
    services.manager.cancel(job.id)


def test_finished_jobs_leave_the_pending_list(services: AppServices, tmp_path: Path) -> None:
    job = services.submit_download(make_spec(tmp_path), LOCAL_OWNER, AccessLimits())
    wait_until(lambda: job.state == DONE)
    assert services.storage is not None
    wait_until(lambda: services.storage.get_value(PENDING_JOBS_KEY) == [])


def test_uploaded_cookies_exist_only_while_the_job_runs(services: AppServices, tmp_path: Path, fake_run: FakeRun) -> None:
    seen: dict[str, Any] = {}

    def run(url: str, options: Any, should_stop: Any) -> DownloadOutcome:
        seen["path"] = options.login.cookie_file
        seen["content"] = options.login.cookie_file.read_bytes()
        return finished_outcome(options.output_dir / "video.mp4")

    fake_run.result = run
    job = services.submit_download(make_spec(tmp_path), LOCAL_OWNER, AccessLimits(), cookie_bytes=b"COOKIES")
    wait_until(lambda: job.state == DONE)
    assert seen["content"] == b"COOKIES"
    assert not seen["path"].exists()
    assert job.cookie_bytes is None


def test_failed_job_forgets_the_login(services: AppServices, tmp_path: Path, fake_run: FakeRun) -> None:
    # Whoever opens the page next may press 重試; the retry takes the login from their own session instead.
    def fail(url: str, options: Any, should_stop: Any) -> DownloadOutcome:
        outcome = empty_outcome()
        outcome["errors"] = ["失敗"]
        return outcome

    fake_run.result = fail
    job = services.submit_download(make_spec(tmp_path, needs_cookie_file=True), LOCAL_OWNER, AccessLimits(), cookie_bytes=b"COOKIES")
    wait_until(lambda: job.state == FAILED)
    assert job.cookie_bytes is None
    assert job.exclusive is True


def members_playlist_with_one_failure(services: AppServices, tmp_path: Path, fake_run: FakeRun) -> Job:
    fake_run.result = lambda url, options, should_stop: finished_outcome(options.output_dir / "a.mp4", errors=["會員限定"], failed_ids=["bbbbbbbbbbb"])
    spec = make_spec(tmp_path, url="https://www.youtube.com/playlist?list=PLmembers", needs_cookie_file=True)
    job = services.submit_download(spec, LOCAL_OWNER, AccessLimits(), cookie_bytes=b"COOKIES")
    wait_until(lambda: job.state == DONE)
    return job


def record_cookie_file(fake_run: FakeRun) -> list[Any]:
    used: list[Any] = []

    def run(url: str, options: Any, should_stop: Any) -> DownloadOutcome:
        cookie_file = options.login.cookie_file
        used.append(cookie_file.read_bytes() if cookie_file else None)
        return finished_outcome(options.output_dir / "b.mp4")

    fake_run.result = run
    return used


def test_retrying_failed_videos_uses_the_sessions_login(services: AppServices, tmp_path: Path, fake_run: FakeRun) -> None:
    job = members_playlist_with_one_failure(services, tmp_path, fake_run)
    used = record_cookie_file(fake_run)
    retried = services.retry_failed_videos(job, cookie_bytes=b"COOKIES")
    assert len(retried) == 1 and retried[0].exclusive is True
    wait_until(lambda: retried[0].state == DONE)
    assert used == [b"COOKIES"]
    assert job.cookie_bytes is None and retried[0].cookie_bytes is None


def test_retrying_failed_videos_hands_on_a_login_given_back_to_the_job(services: AppServices, tmp_path: Path, fake_run: FakeRun) -> None:
    # The page's has_login_for puts the session's login back on the job before asking for a retry.
    job = members_playlist_with_one_failure(services, tmp_path, fake_run)
    used = record_cookie_file(fake_run)
    job.cookie_bytes = b"COOKIES"
    retried = services.retry_failed_videos(job)
    wait_until(lambda: retried[0].state == DONE)
    assert used == [b"COOKIES"]
    assert job.cookie_bytes is None


def test_a_job_that_needs_a_login_never_runs_without_one(services: AppServices, tmp_path: Path, fake_run: FakeRun) -> None:
    job = members_playlist_with_one_failure(services, tmp_path, fake_run)
    used = record_cookie_file(fake_run)
    retried = services.retry_failed_videos(job)
    wait_until(lambda: retried[0].state == FAILED)
    assert used == []
    assert "cookies.txt" in retried[0].status_text


def test_temporary_cookie_file_is_removed_even_after_an_error() -> None:
    with pytest.raises(RuntimeError):
        with temporary_cookie_file(b"secret") as path:
            assert path is not None
            kept = path
            raise RuntimeError
    assert not kept.exists()
    with temporary_cookie_file(None) as path:
        assert path is None


def test_retry_of_a_whole_job_resumes_in_place(services: AppServices, tmp_path: Path, fake_run: FakeRun) -> None:
    def fail(url: str, options: Any, should_stop: Any) -> DownloadOutcome:
        outcome = empty_outcome()
        outcome["errors"] = ["失敗"]
        return outcome

    fake_run.result = fail
    job = services.submit_download(make_spec(tmp_path), LOCAL_OWNER, AccessLimits())
    wait_until(lambda: job.state == FAILED)

    fake_run.result = lambda url, options, should_stop: finished_outcome(options.output_dir / "video.mp4")
    services.retry_job(job)
    wait_until(lambda: job.state == DONE)
    # A retry must not re-download what the first attempt already finished.
    assert fake_run.calls[-1][1].overwrite_existing is False


def test_resuming_a_paused_job_keeps_its_finished_videos(services: AppServices, tmp_path: Path, fake_run: FakeRun) -> None:
    stoppable(fake_run, [])
    job = services.submit_download(make_spec(tmp_path), LOCAL_OWNER, AccessLimits())
    wait_until(lambda: job.started_at is not None)
    services.manager.pause(job.id)
    wait_until(lambda: job.state == PAUSED)
    assert fake_run.calls[-1][1].overwrite_existing is True

    fake_run.result = lambda url, options, should_stop: finished_outcome(options.output_dir / "video.mp4")
    services.resume_job(job)
    wait_until(lambda: job.state == DONE)
    # Overwriting would make yt-dlp delete every finished video of the playlist and fetch it again.
    assert fake_run.calls[-1][1].overwrite_existing is False


def test_retrying_a_cancelled_playlist_resumes_all_of_it(services: AppServices, tmp_path: Path, fake_run: FakeRun) -> None:
    def cancelled(url: str, options: Any, should_stop: Any) -> DownloadOutcome:
        outcome = finished_outcome(options.output_dir / "a.mp4", errors=["一部私人影片"], failed_ids=["aaaaaaaaaaa"])
        while not should_stop():
            time.sleep(0.005)
        outcome["stopped"] = True
        return outcome

    fake_run.result = cancelled
    spec = make_spec(tmp_path, url="https://www.youtube.com/playlist?list=PLabc")
    job = services.submit_download(spec, LOCAL_OWNER, AccessLimits())
    wait_until(lambda: job.started_at is not None)
    services.manager.cancel(job.id)
    wait_until(lambda: job.state == CANCELED)

    assert services.retry_failed_videos(job) == []
    fake_run.result = lambda url, options, should_stop: finished_outcome(options.output_dir / "b.mp4")
    services.retry_job(job)
    wait_until(lambda: job.state == DONE)
    assert fake_run.calls[-1][0] == "https://www.youtube.com/playlist?list=PLabc"


def test_retry_of_a_partly_failed_playlist_only_fetches_what_failed(services: AppServices, tmp_path: Path, fake_run: FakeRun) -> None:
    fake_run.result = lambda url, options, should_stop: finished_outcome(options.output_dir / "a.mp4", errors=["一部失敗"], failed_ids=["aaaaaaaaaaa"])
    spec = make_spec(tmp_path, url="https://www.youtube.com/playlist?list=PLabc", playlist_items="1-3")
    job = services.submit_download(spec, LOCAL_OWNER, AccessLimits())
    wait_until(lambda: job.state == DONE)

    fake_run.result = lambda url, options, should_stop: finished_outcome(options.output_dir / "b.mp4")
    retried = services.retry_failed_videos(job)
    assert len(retried) == 1
    wait_until(lambda: retried[0].state == DONE)
    assert fake_run.calls[-1][0] == "https://www.youtube.com/watch?v=aaaaaaaaaaa"
    assert fake_run.calls[-1][1].playlist_items == ""


def test_public_visitors_get_private_folders_and_share_nothing(tmp_path: Path, fake_run: FakeRun, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(services_module, "CLOUD_OUTPUT_ROOT", tmp_path / "public")
    services = AppServices(is_public=True)
    assert services.storage is None

    limits = AccessLimits.for_public_host()
    spec = make_spec(tmp_path, output_dir="/etc/whatever")
    job_a = services.submit_download(spec, "visitor-a", limits)
    job_b = services.submit_download(spec, "visitor-b", limits)
    wait_until(lambda: job_a.state == DONE and job_b.state == DONE)

    used = [options.output_dir for _, options in fake_run.calls]
    assert len(set(used)) == 2
    assert all(path.parent == tmp_path / "public" for path in used)
    assert services.manager.list_jobs("visitor-a") == [job_a]

    services.manager.remove(job_a.id)
    assert not (tmp_path / "public" / job_a.id).exists()
    assert (tmp_path / "public" / job_b.id).exists()


def test_public_visitors_are_limited_to_a_few_pending_jobs(tmp_path: Path, fake_run: FakeRun, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(services_module, "CLOUD_OUTPUT_ROOT", tmp_path / "public")
    services = AppServices(is_public=True)
    stoppable(fake_run, [])
    limits = AccessLimits.for_public_host()
    jobs = []
    while services.open_job_slots("visitor", limits):
        jobs.append(services.submit_download(make_spec(tmp_path), "visitor", limits))
    assert len(jobs) == services_module.CLOUD_MAX_PENDING_PER_VISITOR
    assert services.open_job_slots("someone-else", limits) == services_module.CLOUD_MAX_PENDING_PER_VISITOR
    assert services.open_job_slots("local", AccessLimits()) is None
    for job in jobs:
        services.manager.cancel(job.id)
    wait_until(lambda: all(job.state == CANCELED for job in jobs))


def test_fresh_sessions_cannot_take_the_whole_public_queue(tmp_path: Path, fake_run: FakeRun, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(services_module, "CLOUD_OUTPUT_ROOT", tmp_path / "public")
    services = AppServices(is_public=True)
    stoppable(fake_run, [])
    limits = AccessLimits.for_public_host()
    jobs = []
    # Every new browser session is a new visitor, so one address opening session after session hits its own cap.
    for session in range(10):
        while services.open_job_slots(f"session-{session}", limits, "203.0.113.7"):
            jobs.append(services.submit_download(make_spec(tmp_path), f"session-{session}", limits, client="203.0.113.7"))
    assert len(jobs) == services_module.CLOUD_MAX_PENDING_PER_ADDRESS
    with pytest.raises(ValueError, match="已滿"):
        services.submit_download(make_spec(tmp_path), "session-99", limits, client="203.0.113.7")
    # Without a usable address only the cap for everyone together is left, and someone else still gets in.
    for session in range(10):
        while services.open_job_slots(f"anonymous-{session}", limits):
            jobs.append(services.submit_download(make_spec(tmp_path), f"anonymous-{session}", limits))
    assert len(jobs) == services_module.CLOUD_MAX_PENDING_TOTAL
    assert services.open_job_slots("latecomer", limits, "198.51.100.4") == 0
    for job in jobs:
        services.manager.cancel(job.id)
    wait_until(lambda: all(job.state == CANCELED for job in jobs))


def test_cancelling_a_public_job_deletes_its_folder(tmp_path: Path, fake_run: FakeRun, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(services_module, "CLOUD_OUTPUT_ROOT", tmp_path / "public")
    services = AppServices(is_public=True)
    limits = AccessLimits.for_public_host()

    def run(url: str, options: Any, should_stop: Any) -> DownloadOutcome:
        options.output_dir.mkdir(parents=True, exist_ok=True)
        (options.output_dir / "video.mp4.part").write_bytes(b"x")
        while not should_stop():
            time.sleep(0.005)
        outcome = empty_outcome()
        outcome["stopped"] = True
        return outcome

    fake_run.result = run
    job = services.submit_download(make_spec(tmp_path), "visitor", limits)
    wait_until(lambda: (tmp_path / "public" / job.id / "video.mp4.part").exists())
    services.manager.cancel(job.id)
    wait_until(lambda: job.state == CANCELED)
    wait_until(lambda: not (tmp_path / "public" / job.id).exists())


def test_old_public_folders_are_purged_but_known_jobs_stay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    monkeypatch.setattr(services_module, "CLOUD_OUTPUT_ROOT", tmp_path)
    for name in ("old", "old-but-running", "recent"):
        (tmp_path / name).mkdir()
    old_time = time.time() - services_module.CLOUD_RETENTION_SECONDS - 60
    os.utime(tmp_path / "old", (old_time, old_time))
    os.utime(tmp_path / "old-but-running", (old_time, old_time))
    services_module.purge_stale_output_dirs({"old-but-running"})
    assert sorted(path.name for path in tmp_path.iterdir()) == ["old-but-running", "recent"]


def test_subscription_check_uses_the_archive_and_its_limit(services: AppServices, tmp_path: Path, fake_run: FakeRun) -> None:
    assert services.storage is not None
    subscription_id = services.storage.add_subscription(
        "https://www.youtube.com/@channel/videos", "Channel", DownloadSettings(), str(tmp_path / "out"), 3, 1
    )
    subscription = services.storage.get_subscription(subscription_id)
    assert subscription is not None
    job = services.check_subscription(subscription)
    assert job is not None
    wait_until(lambda: job.state == DONE)
    _, options = fake_run.calls[-1]
    assert options.playlist_limit == 3
    assert options.archive_path == services.storage.archive_path
    wait_until(lambda: "下載 1 部" in services.storage.get_subscription(subscription_id).last_result)  # type: ignore[union-attr]


def test_subscription_is_not_queued_twice(services: AppServices, tmp_path: Path, fake_run: FakeRun) -> None:
    assert services.storage is not None
    subscription_id = services.storage.add_subscription("https://www.youtube.com/@c", "C", DownloadSettings(), str(tmp_path / "out"), 3, 1)
    subscription = services.storage.get_subscription(subscription_id)
    assert subscription is not None
    stoppable(fake_run, [])
    first = services.check_subscription(subscription)
    second = services.check_subscription(subscription)
    assert first is second
    assert first is not None
    services.manager.cancel(first.id)


def test_a_subscription_paused_by_a_restart_continues_on_the_next_check(tmp_path: Path, fake_run: FakeRun) -> None:
    services = AppServices(is_public=False, data_dir=tmp_path / "data")
    assert services.storage is not None
    subscription_id = services.storage.add_subscription("https://www.youtube.com/@c", "C", DownloadSettings(), str(tmp_path / "out"), 3, 1)
    subscription = services.storage.get_subscription(subscription_id)
    assert subscription is not None
    stoppable(fake_run, [])
    first = services.check_subscription(subscription)
    assert first is not None
    wait_until(lambda: first.started_at is not None)
    wait_until(lambda: bool(services.storage.get_value(PENDING_JOBS_KEY)))  # type: ignore[union-attr]

    # The old process ends, and with it its hold on the data folder.
    services.shutdown()
    restarted = AppServices(is_public=False, data_dir=tmp_path / "data")
    fake_run.result = lambda url, options, should_stop: finished_outcome(options.output_dir / "v.mp4")
    resumed = restarted.check_subscription(subscription)
    assert resumed is not None
    wait_until(lambda: resumed.state == DONE)
    services.manager.cancel(first.id)


def test_cookie_folders_left_by_a_killed_app_are_removed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import os

    monkeypatch.setattr(services_module.tempfile, "gettempdir", lambda: str(tmp_path))
    old = tmp_path / f"{services_module.COOKIE_DIR_PREFIX}old"
    recent = tmp_path / f"{services_module.COOKIE_DIR_PREFIX}recent"
    for folder in (old, recent):
        folder.mkdir()
        (folder / "cookies.txt").write_text("secret", encoding="utf-8")
    old_time = time.time() - services_module.STALE_COOKIE_DIR_SECONDS - 60
    os.utime(old, (old_time, old_time))
    services_module.remove_stale_cookie_dirs()
    assert not old.exists()
    assert recent.exists()


def test_cookie_folders_of_running_jobs_are_removed_at_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    with temporary_cookie_file(b"secret") as cookie_file:
        assert cookie_file is not None
        services_module.remove_active_cookie_dirs()
        assert not cookie_file.exists()


def test_due_subscriptions_run_and_others_wait(services: AppServices, tmp_path: Path, fake_run: FakeRun) -> None:
    assert services.storage is not None
    services.storage.add_subscription("https://www.youtube.com/@due", "Due", DownloadSettings(), str(tmp_path / "out"), 3, 1)
    manual = services.storage.add_subscription("https://www.youtube.com/@manual", "Manual", DownloadSettings(), str(tmp_path / "out"), 3, 0)
    assert services.run_due_subscriptions() == 1
    assert services.run_due_subscriptions() == 0
    assert services.storage.get_subscription(manual).last_checked_at is None  # type: ignore[union-attr]


def test_seeding_marks_current_videos_as_seen(services: AppServices, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(services_module, "list_recent_video_ids", lambda url, limit, login, network=None: ("Channel", ["a", "b"]))
    assert services.seed_subscription_archive("u", 50, services_module.LoginSource()) == ("Channel", 2)
    assert services.storage is not None
    assert services.storage.archived_video_ids() == {"a", "b"}


def test_tool_jobs_report_their_result(services: AppServices, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(services_module, "run_tool", lambda request, progress, should_stop: ToolResult(output_path=tmp_path / "out.mp4"))
    job: Job = services.submit_tool(ToolRequest("trim", "a.mp4"), LOCAL_OWNER)
    wait_until(lambda: job.state == DONE)
    assert job.outcome.output_path == tmp_path / "out.mp4"

    monkeypatch.setattr(services_module, "run_tool", lambda request, progress, should_stop: ToolResult(error="ffmpeg 失敗"))
    failed = services.submit_tool(ToolRequest("trim", "a.mp4"), LOCAL_OWNER)
    wait_until(lambda: failed.state == FAILED)
    assert failed.status_text == "ffmpeg 失敗"


def test_file_tools_are_for_this_machines_owner_only(services: AppServices, monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[ToolRequest] = []
    monkeypatch.setattr(services_module, "run_tool", lambda request, progress, should_stop: ran.append(request) or ToolResult())
    # A visitor's owner is their session: the tools read and write any path on this machine, and can use its GPU.
    with pytest.raises(PermissionError, match=services_module.OWNER_ONLY_MESSAGE):
        services.submit_tool(ToolRequest("burn_subtitles", "C:/Windows/win.ini", {"gpu": True}), "visitor-session")
    time.sleep(0.1)
    assert services.manager.list_jobs() == [] and ran == []


def test_retries_respect_a_public_visitors_limit(tmp_path: Path, fake_run: FakeRun, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(services_module, "CLOUD_OUTPUT_ROOT", tmp_path / "public")
    services = AppServices(is_public=True)
    limits = AccessLimits.for_public_host()

    def fail(url: str, options: Any, should_stop: Any) -> DownloadOutcome:
        outcome = empty_outcome()
        outcome["errors"] = ["失敗"]
        return outcome

    fake_run.result = fail
    failed = [services.submit_download(make_spec(tmp_path), "visitor", limits) for _ in range(services_module.CLOUD_MAX_PENDING_PER_VISITOR)]
    wait_until(lambda: all(job.state == FAILED for job in failed))

    stoppable(fake_run, [])
    retried = [job for job in failed if services.retry_job(job)]
    assert len(retried) == services_module.CLOUD_MAX_PENDING_PER_VISITOR
    extra = services.submit_download(make_spec(tmp_path), "other", limits)
    assert services.open_job_slots("visitor", limits) == 0
    services.manager.cancel(retried[0].id)
    wait_until(lambda: retried[0].state == CANCELED)
    for job in [*retried, extra]:
        services.manager.cancel(job.id)


def test_forgetting_a_visitor_removes_all_their_jobs_and_files_however_long_one_takes_to_stop(
    tmp_path: Path, fake_run: FakeRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(services_module, "CLOUD_OUTPUT_ROOT", tmp_path / "public")
    services = AppServices(is_public=True)
    limits = AccessLimits.for_public_host()
    let_go = threading.Event()

    def run(url: str, options: Any, should_stop: Any) -> DownloadOutcome:
        if url.endswith("done"):
            return finished_outcome(options.output_dir / "done.mp4")
        # A download stuck in a step that notices the stop only much later.
        (options.output_dir / "slow.mp4.part").write_bytes(b"partial")
        let_go.wait(10)
        return empty_outcome() | {"stopped": True}  # type: ignore[return-value]

    fake_run.result = run
    finished = services.submit_download(make_spec(tmp_path, url=f"{URL}&done"), "visitor", limits)
    other = services.submit_download(make_spec(tmp_path, url=f"{URL}&done"), "other", limits)
    wait_until(lambda: finished.state == DONE and other.state == DONE)
    slow = services.submit_download(make_spec(tmp_path, url=f"{URL}&slow"), "visitor", limits, cookie_bytes=b"COOKIES")
    wait_until(lambda: (services_module.CLOUD_OUTPUT_ROOT / slow.id / "slow.mp4.part").exists())
    # Waits behind the running one: a visitor runs one job at a time.
    queued = services.submit_download(make_spec(tmp_path, url=f"{URL}&done"), "visitor", limits, cookie_bytes=b"COOKIES")
    assert queued.state == QUEUED

    with pytest.raises(PermissionError):
        services.forget_owner(LOCAL_OWNER, AccessLimits())
    started = time.monotonic()
    services.forget_owner("visitor", limits)
    assert time.monotonic() - started < 1
    assert services.manager.list_jobs("visitor") == [slow]
    assert slow.cookie_bytes is None and queued.cookie_bytes is None
    assert not (services_module.CLOUD_OUTPUT_ROOT / finished.id).exists()

    let_go.set()
    wait_until(lambda: services.manager.list_jobs("visitor") == [])
    assert not (services_module.CLOUD_OUTPUT_ROOT / slow.id).exists()
    assert services.manager.list_jobs("other") == [other] and (services_module.CLOUD_OUTPUT_ROOT / other.id / "done.mp4").exists()


# Subscriptions


def subscribe(services: AppServices, tmp_path: Path, url: str, settings: Optional[DownloadSettings] = None, max_items: int = 3) -> Subscription:
    assert services.storage is not None
    subscription_id = services.storage.add_subscription(url, "T", settings or DownloadSettings(), str(tmp_path / "out"), max_items, 1)
    subscription = services.storage.get_subscription(subscription_id)
    assert subscription is not None
    return subscription


def options_of_a_check(services: AppServices, fake_run: FakeRun, subscription: Subscription) -> Any:
    job = services.check_subscription(subscription)
    assert job is not None
    wait_until(lambda: job.state == DONE)
    return fake_run.calls[-1][1]


def ids_a_check_looks_at(options: Any, video_ids: list[str]) -> list[str]:
    """Runs yt-dlp's own playlist handling on a made-up listing, offline. yt-dlp checks the archive before the filter,
    so the filter sees exactly the videos the check would treat as new."""
    from yt_dlp import YoutubeDL

    from videocap.downloader import YtDlpLogCollector
    from videocap.options import build_ydl_options

    seen: list[str] = []

    def record(info: dict[str, Any], *, incomplete: bool) -> Optional[str]:
        if info.get("_type") == "playlist" or not info.get("id"):
            return None
        seen.append(info["id"])
        return "skipped by the test"

    ydl_opts = build_ydl_options(options, True, YtDlpLogCollector())
    ydl_opts["match_filter"] = record
    entries = [
        {"_type": "url", "ie_key": "Youtube", "id": video_id, "url": f"https://www.youtube.com/watch?v={video_id}", "title": video_id}
        for video_id in video_ids
    ]
    with YoutubeDL(ydl_opts) as ydl:  # type: ignore[arg-type]
        playlist = {
            "_type": "playlist",
            "id": "PLtest",
            "title": "Test",
            "extractor": "youtube:tab",
            "extractor_key": "YoutubeTab",
            "webpage_url": "https://www.youtube.com/playlist?list=PLtest",
            "entries": entries,
        }
        ydl.process_ie_result(playlist, download=True)
    # yt-dlp may ask about the same entry more than once.
    return list(dict.fromkeys(seen))


def test_a_playlist_subscription_finds_videos_added_at_the_end(services: AppServices, tmp_path: Path, fake_run: FakeRun) -> None:
    # YouTube adds new videos to the bottom of a playlist unless its owner says otherwise.
    assert services.storage is not None
    old = [f"old{index:08d}" for index in range(7)]
    services.storage.add_to_archive(old)
    options = options_of_a_check(services, fake_run, subscribe(services, tmp_path, "https://www.youtube.com/playlist?list=PLcourse"))
    assert ids_a_check_looks_at(options, [*old, "newvideo001"]) == ["newvideo001"]
    assert options.playlist_limit == 3


def test_a_channel_subscription_catches_up_on_more_new_videos_than_one_check_takes(
    services: AppServices, tmp_path: Path, fake_run: FakeRun
) -> None:
    assert services.storage is not None
    archived = [f"old{index:08d}" for index in range(50)]
    services.storage.add_to_archive(archived)
    new = [f"new{index:08d}" for index in range(8)]
    options = options_of_a_check(services, fake_run, subscribe(services, tmp_path, "https://www.youtube.com/@channel/videos"))
    # Three per check, but the five the first check leaves must still be found by the next ones.
    assert ids_a_check_looks_at(options, [*new, *archived]) == new
    assert options.playlist_limit == 3


def test_seeding_a_playlist_covers_all_of_it(services: AppServices, monkeypatch: pytest.MonkeyPatch) -> None:
    limits: list[int] = []
    monkeypatch.setattr(services_module, "list_recent_video_ids", lambda url, limit, login, network=None: (limits.append(limit), ("T", []))[1])
    services.seed_subscription_archive("https://www.youtube.com/playlist?list=PLcourse", 50, LoginSource())
    services.seed_subscription_archive("https://www.youtube.com/@channel/videos", 50, LoginSource())
    assert limits[0] >= 10_000
    assert limits[1] == 50


def test_a_subscription_with_a_bad_setting_says_so_and_does_not_hold_up_others(
    services: AppServices, tmp_path: Path, fake_run: FakeRun
) -> None:
    assert services.storage is not None
    good = subscribe(services, tmp_path, "https://www.youtube.com/@good")
    bad = subscribe(services, tmp_path, "https://www.youtube.com/@bad", DownloadSettings(filter_date_after="2026-02-30"))
    assert services.run_due_subscriptions() == 2
    assert "2026-02-30" in services.storage.get_subscription(bad.id).last_result  # type: ignore[union-attr]
    assert services.storage.get_subscription(good.id).last_checked_at is not None  # type: ignore[union-attr]
    with pytest.raises(ValueError):
        services.validate_subscription(bad.url, bad.settings, bad.output_dir)


def test_subscriptions_ignore_settings_meant_for_a_single_video(services: AppServices, tmp_path: Path, fake_run: FakeRun) -> None:
    settings = DownloadSettings(time_ranges="0:10-0:20", playlist_items="1-2")
    services.validate_subscription("https://www.youtube.com/@c", settings, str(tmp_path / "out"))
    options = options_of_a_check(services, fake_run, subscribe(services, tmp_path, "https://www.youtube.com/@c", settings))
    assert options.time_ranges == ()
    assert options.playlist_items != "1-2"

    # Without a clip range a cut would apply, and would remove the same minutes from every new video.
    settings = DownloadSettings(remove_ranges="0:00-0:30")
    options = options_of_a_check(services, fake_run, subscribe(services, tmp_path, "https://www.youtube.com/@d", settings))
    assert options.remove_ranges == ()


def test_a_subscription_check_leaves_a_download_the_person_paused(services: AppServices, tmp_path: Path, fake_run: FakeRun) -> None:
    assert services.storage is not None
    subscription = subscribe(services, tmp_path, "https://www.youtube.com/@c")
    stoppable(fake_run, [])
    first = services.check_subscription(subscription)
    assert first is not None
    wait_until(lambda: first.started_at is not None)
    services.manager.pause(first.id)
    wait_until(lambda: first.state == PAUSED)

    assert services.check_subscription(subscription) is first
    time.sleep(0.05)
    assert first.state == PAUSED
    assert "暫停" in services.storage.get_subscription(subscription.id).last_result  # type: ignore[union-attr]
    services.manager.cancel(first.id)


def last_result_of_a_failed_check(services: AppServices, subscription: Subscription) -> str:
    assert services.storage is not None
    job = services.check_subscription(subscription)
    assert job is not None
    wait_until(lambda: job.state == FAILED)
    wait_until(lambda: services.storage.get_subscription(subscription.id).last_result != "檢查中...")  # type: ignore[union-attr]
    return services.storage.get_subscription(subscription.id).last_result  # type: ignore[union-attr]


def test_a_failed_check_says_why_rather_than_that_nothing_is_new(services: AppServices, tmp_path: Path, fake_run: FakeRun) -> None:
    missing = "找不到這個頻道，請確認網址或 @頻道代號 是否正確。"
    fake_run.result = lambda url, options, should_stop: {**empty_outcome(), "errors": [missing]}  # type: ignore[typeddict-item]
    subscription = subscribe(services, tmp_path, "https://www.youtube.com/@this-channel-does-not-exist-videocap")
    assert last_result_of_a_failed_check(services, subscription) == f"檢查失敗：{missing}"


def test_a_check_that_fails_before_it_downloads_anything_does_not_stay_checking(
    services: AppServices, tmp_path: Path, fake_run: FakeRun
) -> None:
    assert services.storage is not None
    blocker = tmp_path / "a-file"
    blocker.write_text("not a folder", encoding="utf-8")
    subscription_id = services.storage.add_subscription("https://www.youtube.com/@c", "C", DownloadSettings(), str(blocker / "videos"), 3, 1)
    subscription = services.storage.get_subscription(subscription_id)
    assert subscription is not None
    assert last_result_of_a_failed_check(services, subscription).startswith("檢查失敗：下載資料夾無法寫入")


# Surviving a restart


def test_a_scheduled_job_keeps_its_start_time_after_a_restart(services: AppServices, tmp_path: Path, fake_run: FakeRun) -> None:
    assert services.storage is not None
    start_at = time.time() + 3600
    job = services.submit_download(make_spec(tmp_path), LOCAL_OWNER, AccessLimits(), not_before=start_at)
    wait_until(lambda: bool(services.storage.get_value(PENDING_JOBS_KEY)))  # type: ignore[union-attr]

    # The old process ends, and with it its hold on the data folder.
    services.shutdown()
    restarted = AppServices(is_public=False, data_dir=tmp_path / "data")
    [restored] = restarted.manager.list_jobs()
    assert restored.not_before == pytest.approx(start_at)
    assert restored.state == QUEUED
    time.sleep(0.05)
    assert fake_run.calls == []
    services.manager.cancel(job.id)
    restarted.manager.cancel(restored.id)


def test_cancelling_a_job_restored_after_a_restart_cleans_up_and_keeps_history(
    services: AppServices, tmp_path: Path, fake_run: FakeRun
) -> None:
    assert services.storage is not None
    stream = tmp_path / "out" / "Big talk.f137.mp4"

    def paused_midway(url: str, options: Any, should_stop: Any) -> DownloadOutcome:
        outcome = finished_outcome(options.output_dir / "Video 1.mp4")
        stream.write_bytes(b"stream")
        while not should_stop():
            time.sleep(0.005)
        outcome["stopped"] = True
        outcome["partial_paths"] = [str(stream)]
        return outcome

    fake_run.result = paused_midway
    job = services.submit_download(make_spec(tmp_path, url="https://www.youtube.com/playlist?list=PLabc"), LOCAL_OWNER, AccessLimits())
    wait_until(stream.exists)
    services.manager.pause(job.id)
    wait_until(lambda: job.state == PAUSED)
    wait_until(lambda: any("outcome" in entry for entry in services.storage.get_value(PENDING_JOBS_KEY) or []))  # type: ignore[union-attr]

    # The old process ends, and with it its hold on the data folder.
    services.shutdown()
    restarted = AppServices(is_public=False, data_dir=tmp_path / "data")
    assert restarted.storage is not None
    [restored] = restarted.manager.list_jobs()
    restarted.manager.cancel(restored.id)
    assert not stream.exists()
    assert (tmp_path / "out" / "Video 1.mp4").exists()
    assert [entry.title for entry in restarted.storage.list_history()] == ["Title"]


def test_tool_jobs_come_back_paused_after_a_restart(services: AppServices, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert services.storage is not None

    def slow_tool(request: ToolRequest, progress: Any, should_stop: Any) -> ToolResult:
        while not should_stop():
            time.sleep(0.005)
        return ToolResult(stopped=True)

    monkeypatch.setattr(services_module, "run_tool", slow_tool)
    request = ToolRequest("compress", str(tmp_path / "a.mp4"), {"crf": 28, "height": 720})
    job = services.submit_tool(request, LOCAL_OWNER)
    wait_until(lambda: bool(services.storage.get_value(PENDING_JOBS_KEY)))  # type: ignore[union-attr]

    # The old process ends, and with it its hold on the data folder.
    services.shutdown()
    restarted = AppServices(is_public=False, data_dir=tmp_path / "data")
    [restored] = restarted.manager.list_jobs()
    assert restored.state == PAUSED
    assert restored.payload == request
    assert restored.label == job.label
    services.manager.cancel(job.id)


def test_the_pending_list_is_never_overwritten_by_an_older_one(
    services: AppServices, tmp_path: Path, fake_run: FakeRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = services.storage
    assert storage is not None
    original_set_value = storage.set_value
    delayed = threading.Event()

    def slow_set_value(key: str, value: Any) -> None:
        # Stands in for a thread that computed its list and then waited for the disk.
        if key == PENDING_JOBS_KEY and value == [] and not delayed.is_set():
            delayed.set()
            time.sleep(0.3)
        original_set_value(key, value)

    monkeypatch.setattr(storage, "set_value", slow_set_value)
    first = services.submit_download(make_spec(tmp_path), LOCAL_OWNER, AccessLimits())
    wait_until(lambda: first.state == DONE)
    wait_until(delayed.is_set)
    stoppable(fake_run, [])
    second = services.submit_download(make_spec(tmp_path, url=f"{URL}&second"), LOCAL_OWNER, AccessLimits())
    wait_until(lambda: second.started_at is not None)

    def saved_urls() -> list[str]:
        return [entry["spec"]["url"] for entry in storage.get_value(PENDING_JOBS_KEY) or []]

    wait_until(lambda: saved_urls() == [second.label], timeout=3)
    time.sleep(0.6)
    assert saved_urls() == [second.label]
    services.manager.cancel(second.id)


def test_queueing_many_links_stays_fast(services: AppServices, tmp_path: Path, fake_run: FakeRun) -> None:
    assert services.storage is not None
    tomorrow = time.time() + 86400
    started = time.perf_counter()
    for index in range(300):
        spec = make_spec(tmp_path, url=f"https://www.youtube.com/watch?v=v{index:010d}")
        services.submit_download(spec, LOCAL_OWNER, AccessLimits(), not_before=tomorrow)
    assert time.perf_counter() - started < 3
    wait_until(lambda: len(services.storage.get_value(PENDING_JOBS_KEY) or []) == 300)  # type: ignore[union-attr]


def test_services_stop_their_threads_on_shutdown(tmp_path: Path) -> None:
    services = AppServices(is_public=False, data_dir=tmp_path / "data")
    services.shutdown()
    for thread in services._threads:
        thread.join(timeout=3)
        assert not thread.is_alive()


# Updating yt-dlp


def test_updating_yt_dlp_waits_for_running_downloads_and_then_holds_new_ones(
    services: AppServices, tmp_path: Path, fake_run: FakeRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    installs: list[int] = []

    def install(proxy: str = "") -> UpdateResult:
        installs.append(1)
        return UpdateResult(True, "Successfully installed", restart_required=True)

    monkeypatch.setattr(services_module, "install_ytdlp_update", install)
    stoppable(fake_run, [])
    running = services.submit_download(make_spec(tmp_path), LOCAL_OWNER, AccessLimits())
    wait_until(lambda: running.started_at is not None)

    succeeded, message = services.update_ytdlp()
    assert not succeeded and "下載" in message and installs == []
    services.manager.cancel(running.id)
    wait_until(lambda: running.state == CANCELED)

    succeeded, _ = services.update_ytdlp()
    assert succeeded and services.restart_required
    held = services.submit_download(make_spec(tmp_path), LOCAL_OWNER, AccessLimits())
    time.sleep(0.1)
    assert held.state == QUEUED
    services.manager.cancel(held.id)


@pytest.mark.parametrize(
    "result",
    [UpdateResult(True, "已是最新版本（2026.10.26），不需要更新。"), UpdateResult(False, "這個 Python 版本（3.10）裝不了較新的 yt-dlp")],
)
def test_an_update_that_installs_nothing_leaves_downloads_running(
    services: AppServices, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, result: UpdateResult
) -> None:
    proxies: list[str] = []
    monkeypatch.setattr(services_module, "install_ytdlp_update", lambda proxy="": (proxies.append(proxy), result)[1])

    assert services.update_ytdlp(proxy="http://proxy.example:8080") == (result.succeeded, result.message)
    assert proxies == ["http://proxy.example:8080"]
    assert not services.restart_required
    job = services.submit_download(make_spec(tmp_path), LOCAL_OWNER, AccessLimits())
    wait_until(lambda: job.state == DONE)


# Public hosts


def write_bytes_job(size: int) -> Callable[[str, Any, Any], DownloadOutcome]:
    def run(url: str, options: Any, should_stop: Any) -> DownloadOutcome:
        options.output_dir.mkdir(parents=True, exist_ok=True)
        (options.output_dir / "video.mp4").write_bytes(b"x" * size)
        return empty_outcome()

    return run


def test_a_public_visitor_cannot_keep_more_files_than_the_quota(tmp_path: Path, fake_run: FakeRun, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(services_module, "CLOUD_OUTPUT_ROOT", tmp_path / "public")
    monkeypatch.setattr(services_module, "CLOUD_MAX_STORED_BYTES_PER_VISITOR", 3_000)
    services = AppServices(is_public=True)
    limits = AccessLimits.for_public_host()
    fake_run.result = write_bytes_job(2_000)
    slots = services_module.CLOUD_MAX_PENDING_PER_VISITOR

    first = services.submit_download(make_spec(tmp_path), "visitor", limits)
    wait_until(lambda: first.state == DONE)
    assert services.open_job_slots("visitor", limits) == slots
    second = services.submit_download(make_spec(tmp_path), "visitor", limits)
    wait_until(lambda: second.state == DONE)

    assert services.open_job_slots("visitor", limits) == 0
    assert "3 KB" in services.storage_problem("visitor", limits)
    with pytest.raises(ValueError):
        services.submit_download(make_spec(tmp_path), "visitor", limits)
    assert services.open_job_slots("someone-else", limits) == slots
    assert services.storage_problem("local", AccessLimits()) == ""

    services.manager.remove(first.id)
    assert services.open_job_slots("visitor", limits) == slots


def test_a_public_host_stops_taking_jobs_when_its_disk_is_nearly_full(
    tmp_path: Path, fake_run: FakeRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    services = AppServices(is_public=True)
    limits = AccessLimits.for_public_host()
    monkeypatch.setattr(services_module, "get_free_disk_bytes", lambda path: 1024)
    assert services.open_job_slots("visitor", limits) == 0
    assert services.storage_problem("visitor", limits)
    assert services.open_job_slots("local", AccessLimits()) is None


# Importing subscriptions


def imported(*names: str) -> list[ImportedChannel]:
    return [ImportedChannel(f"UC{name:_<22}", name) for name in names]


@pytest.fixture
def channel_reads(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, float]]:
    """Stands in for reading channels from YouTube; a channel named "broken" cannot be read."""
    reads: list[tuple[str, float]] = []

    def list_recent_video_ids(url: str, limit: int, login: Any, network: Any = None) -> tuple[str, list[str]]:
        reads.append((url, time.monotonic()))
        if "broken" in url:
            raise RuntimeError("ERROR: [youtube:tab] This channel does not exist.")
        return "From YouTube - Videos", [f"{url[-30:-7]}-v{index}" for index in range(3)]

    monkeypatch.setattr(services_module, "list_recent_video_ids", list_recent_video_ids)
    monkeypatch.setattr(services_module, "SUBSCRIPTION_IMPORT_PAUSE_SECONDS", 0.2)
    return reads


def test_importing_subscriptions_reads_one_channel_at_a_time(
    services: AppServices, tmp_path: Path, channel_reads: list[tuple[str, float]]
) -> None:
    assert services.storage is not None
    channels = imported("first", "broken", "third")
    services.storage.add_subscription(channels[2].subscription_url(), "Third", DownloadSettings(), "out", 5, 24)
    started = time.monotonic()
    progress = services.start_subscription_import(
        channels, DownloadSettings(quality="720p"), str(tmp_path / "out"), 4, 6, True, AccessLimits()
    )
    # The page that started it is not held up by YouTube.
    assert time.monotonic() - started < 0.1
    wait_until(lambda: progress.finished)

    assert progress.added == ["first"] and progress.skipped == ["third"]
    assert len(progress.failed) == 1 and progress.failed[0].startswith("broken：")
    assert len(channel_reads) == 2 and channel_reads[1][1] - channel_reads[0][1] >= 0.2
    [added] = [subscription for subscription in services.storage.list_subscriptions() if subscription.title == "first"]
    assert added.url.endswith("/videos") and (added.max_items, added.interval_hours, added.settings.quality) == (4, 6, "720p")
    # Just read, so the scheduler does not read every imported channel again at once.
    assert added.last_checked_at is not None and not added.is_due(time.time())
    assert len(services.storage.archived_video_ids()) == 3


def test_an_import_without_seeding_asks_youtube_nothing(
    services: AppServices, tmp_path: Path, channel_reads: list[tuple[str, float]]
) -> None:
    assert services.storage is not None
    progress = services.start_subscription_import(imported("one", "two"), DownloadSettings(), str(tmp_path / "out"), 5, 24, False, AccessLimits())
    wait_until(lambda: progress.finished)
    assert progress.added == ["one", "two"] and channel_reads == []
    assert all(subscription.last_checked_at is None for subscription in services.storage.list_subscriptions())


def test_an_import_can_be_stopped_and_only_one_runs_at_a_time(
    services: AppServices, tmp_path: Path, channel_reads: list[tuple[str, float]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(services_module, "SUBSCRIPTION_IMPORT_PAUSE_SECONDS", 30)
    progress = services.start_subscription_import(imported("a", "b", "c"), DownloadSettings(), str(tmp_path / "out"), 5, 24, True, AccessLimits())
    wait_until(lambda: progress.added == ["a"])
    with pytest.raises(ValueError, match="進行中"):
        services.start_subscription_import(imported("d"), DownloadSettings(), str(tmp_path / "out"), 5, 24, True, AccessLimits())
    progress.stop_requested.set()
    wait_until(lambda: progress.finished, timeout=2)
    assert progress.handled == 1 and len(channel_reads) == 1


def test_an_import_with_settings_that_cannot_run_or_from_a_visitor_is_refused(services: AppServices, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="2026-02-30"):
        services.start_subscription_import(
            imported("a"), DownloadSettings(filter_date_after="2026-02-30"), str(tmp_path / "out"), 5, 24, True, AccessLimits()
        )
    with pytest.raises(PermissionError):
        services.start_subscription_import(imported("a"), DownloadSettings(), str(tmp_path / "out"), 5, 24, True, AccessLimits.for_public_host())
    assert services.subscription_import is None


# Retention rules


def downloaded_by(storage: Any, subscription_id: Optional[int], path: Path, finished_at: float, video_id: str, *sidecars: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"video")
    for suffix in sidecars:
        path.with_name(f"{path.stem}{suffix}").write_text("sidecar", encoding="utf-8")
    storage.add_history(
        [
            {
                "video_id": video_id,
                "title": path.stem,
                "path": str(path),
                "size_bytes": path.stat().st_size,
                "finished_at": finished_at,
                "subscription_id": subscription_id,
            }
        ]
    )
    return path


def test_retention_leaves_the_sidecars_another_download_of_the_same_name_uses(services: AppServices, tmp_path: Path) -> None:
    storage = services.storage
    assert storage is not None
    subscription = subscribe(services, tmp_path, "https://www.youtube.com/@news/videos")
    folder = tmp_path / "out"
    downloaded_by(storage, subscription.id, folder / "New.mp4", 3000, "new00000001")
    song = downloaded_by(storage, subscription.id, folder / "Song.mp4", 1000, "song0000001", ".en.srt", ".jpg")
    # The person also downloaded the audio by hand, which wrote the same subtitles and cover.
    downloaded_by(storage, None, folder / "Song.m4a", 2000, "song0000001", ".en.srt", ".jpg")

    assert services.save_retention_rule(subscription.id, 1, 0, AccessLimits()) == (1, [])
    assert not song.exists()
    assert (folder / "Song.m4a").exists() and (folder / "Song.en.srt").exists() and (folder / "Song.jpg").exists()


def test_retention_never_follows_an_old_path_to_a_different_file(services: AppServices, tmp_path: Path) -> None:
    storage = services.storage
    assert storage is not None
    subscription = subscribe(services, tmp_path, "https://www.youtube.com/@news/videos")
    folder = tmp_path / "out"
    downloaded_by(storage, subscription.id, folder / "New.mp4", 3000, "new00000001")
    talk = downloaded_by(storage, subscription.id, folder / "Talk.mp4", 1000, "talk0000001")
    # The person deleted the download and later renamed a recording of their own to the same name.
    talk.unlink()
    talk.write_bytes(b"the person's own recording")

    assert services.retention_candidates(subscription, keep_latest=1) == []
    assert services.save_retention_rule(subscription.id, 1, 0, AccessLimits()) == (0, [])
    assert talk.read_bytes() == b"the person's own recording"


def test_retention_counts_each_file_and_each_video_once(services: AppServices, tmp_path: Path) -> None:
    storage = services.storage
    assert storage is not None
    subscription = subscribe(services, tmp_path, "https://www.youtube.com/@news/videos")
    folder = tmp_path / "out"
    now = time.time()
    newest = downloaded_by(storage, subscription.id, folder / "Newest.mp4", now, "newest00001")
    # The same file recorded once more under another spelling of its folder, as after the folder was edited.
    storage.add_history(
        [
            {
                "video_id": "newest00001",
                "path": f"{folder}{os.sep}.{os.sep}Newest.mp4",
                "size_bytes": newest.stat().st_size,
                "finished_at": now - 10 * 86400,
                "subscription_id": subscription.id,
            }
        ]
    )
    # One video downloaded twice, to two files.
    again = downloaded_by(storage, subscription.id, folder / "Again (2).mp4", now - 100, "again000001")
    first_copy = downloaded_by(storage, subscription.id, folder / "Again.mp4", now - 200, "again000001")
    oldest = downloaded_by(storage, subscription.id, folder / "Oldest.mp4", now - 300, "oldest00001")

    assert [Path(entry.path) for entry in services.retention_candidates(subscription, keep_latest=2)] == [oldest]
    assert services.retention_candidates(subscription, keep_days=7) == []
    assert services.save_retention_rule(subscription.id, 2, 0, AccessLimits()) == (1, [])
    assert newest.exists() and again.exists() and first_copy.exists() and not oldest.exists()


def test_a_check_keeps_only_the_newest_downloads_of_its_subscription(services: AppServices, tmp_path: Path, fake_run: FakeRun) -> None:
    storage = services.storage
    assert storage is not None
    subscription = subscribe(services, tmp_path, "https://www.youtube.com/@news/videos")
    storage.update_subscription(subscription.id, keep_latest=2)
    folder = tmp_path / "out" / "News"
    oldest = downloaded_by(storage, subscription.id, folder / "Lesson 1.mp4", 1000, "oldest00001", ".en.srt", ".webp")
    older = downloaded_by(storage, subscription.id, folder / "Older.mp4", 2000, "older000001")
    kept = downloaded_by(storage, subscription.id, folder / "Kept.mp4", 3000, "kept0000001")
    # Downloaded by hand into the same folder, with a title that looks like the deleted one's.
    by_hand = downloaded_by(storage, None, folder / "Lesson 1. Intro.mp4", 500, "byhand00001", ".en.srt")
    # Recorded for the subscription, but since moved out of its folder.
    elsewhere = downloaded_by(storage, subscription.id, tmp_path / "elsewhere" / "Moved.mp4", 10, "moved000001")

    options = options_of_a_check(services, fake_run, storage.get_subscription(subscription.id))  # type: ignore[arg-type]
    new_video = options.output_dir / "video.mp4"
    wait_until(lambda: "保留規則" in storage.get_subscription(subscription.id).last_result)  # type: ignore[union-attr]

    assert "依保留規則刪除 2 部" in storage.get_subscription(subscription.id).last_result  # type: ignore[union-attr]
    assert not oldest.exists() and not older.exists()
    assert not (folder / "Lesson 1.en.srt").exists() and not (folder / "Lesson 1.webp").exists()
    assert kept.exists() and new_video.exists() and by_hand.exists() and elsewhere.exists()
    assert (folder / "Lesson 1. Intro.en.srt").exists()
    assert {"oldest00001", "older000001"} <= storage.archived_video_ids()
    # The history still says they were downloaded.
    assert len(storage.list_history()) == 6


def test_retention_takes_the_downloaders_newer_sidecars_along(services: AppServices, tmp_path: Path) -> None:
    storage = services.storage
    assert storage is not None
    subscription = subscribe(services, tmp_path, "https://www.youtube.com/@news/videos")
    folder = tmp_path / "out"
    newer = (".comments.csv", ".live_chat.csv", ".live_chat.json", ".nfo", "-thumb.jpg", ".zh-TW.srt")
    old = downloaded_by(storage, subscription.id, folder / "Lesson 1.mp4", 1000, "old00000001", *newer)
    kept = downloaded_by(storage, subscription.id, folder / "Lesson 1. Intro.mp4", 2000, "kept0000001", *newer)
    (folder / "Lesson 1.csv").write_text("a spreadsheet of the person's own", encoding="utf-8")

    assert services.save_retention_rule(subscription.id, 1, 0, AccessLimits()) == (1, [])
    assert not old.exists()
    assert sorted(path.name for path in folder.iterdir()) == sorted(
        ["Lesson 1.csv", kept.name, *(f"{kept.stem}{suffix}" for suffix in newer)]
    )


def test_a_rule_by_age_is_shown_before_it_is_saved(services: AppServices, tmp_path: Path) -> None:
    storage = services.storage
    assert storage is not None
    subscription = subscribe(services, tmp_path, "https://www.youtube.com/@news/videos")
    folder = tmp_path / "out"
    week_old = downloaded_by(storage, subscription.id, folder / "week.mp4", time.time() - 8 * 86400, "week0000001")
    fresh = downloaded_by(storage, subscription.id, folder / "fresh.mp4", time.time() - 3600, "fresh000001")

    assert services.retention_candidates(subscription) == []
    assert [entry.path for entry in services.retention_candidates(subscription, keep_days=7)] == [str(week_old)]
    assert week_old.exists()

    with pytest.raises(PermissionError):
        services.save_retention_rule(subscription.id, 0, 7, AccessLimits.for_public_host())
    assert week_old.exists() and not storage.get_subscription(subscription.id).has_retention  # type: ignore[union-attr]

    assert services.save_retention_rule(subscription.id, 0, 7, AccessLimits()) == (1, [])
    assert not week_old.exists() and fresh.exists()
    assert storage.get_subscription(subscription.id).keep_days == 7  # type: ignore[union-attr]


# Backups


def test_a_restore_waits_until_nothing_runs(services: AppServices, tmp_path: Path, fake_run: FakeRun) -> None:
    storage = services.storage
    assert storage is not None
    storage.add_history([{"video_id": "before00001", "title": "Before", "path": "", "finished_at": 1.0}])
    backup = services.create_backup(AccessLimits())
    storage.add_history([{"video_id": "after000001", "title": "After", "path": "", "finished_at": 2.0}])

    stoppable(fake_run, [])
    running = services.submit_download(make_spec(tmp_path), LOCAL_OWNER, AccessLimits())
    wait_until(lambda: running.started_at is not None)
    with pytest.raises(ValueError, match="正在執行"):
        services.restore_backup(backup, AccessLimits())
    assert len(storage.list_history()) == 2
    services.manager.cancel(running.id)
    wait_until(lambda: running.state == CANCELED)

    assert "1 筆下載紀錄" in services.restore_backup(backup, AccessLimits())
    assert [entry.title for entry in storage.list_history()] == ["Before"]
    # Downloads are not left held afterwards.
    fake_run.result = lambda url, options, should_stop: finished_outcome(options.output_dir / "video.mp4")
    job = services.submit_download(make_spec(tmp_path), LOCAL_OWNER, AccessLimits())
    wait_until(lambda: job.state == DONE)


def test_a_check_queued_before_a_restore_never_counts_for_another_subscription(
    services: AppServices, tmp_path: Path, fake_run: FakeRun
) -> None:
    from videocap.storage import AppStorage

    storage = services.storage
    assert storage is not None
    elsewhere = AppStorage(tmp_path / "elsewhere")
    other_id = elsewhere.add_subscription("https://www.youtube.com/@other/videos", "Other", DownloadSettings(), str(tmp_path / "out"), 3, 24)
    elsewhere.write_backup(tmp_path / "backup.zip")
    mine = subscribe(services, tmp_path, "https://www.youtube.com/@mine/videos")
    assert mine.id == other_id

    # Waiting in the queue while the backup is restored: a restore holds downloads, and only refuses running ones.
    services.manager.hold("download")
    job = services.check_subscription(mine)
    assert job is not None and job.state == QUEUED
    services.restore_backup((tmp_path / "backup.zip").read_bytes(), AccessLimits())
    wait_until(lambda: job.state == DONE)
    wait_until(lambda: bool(storage.list_history()))

    # The id that was "mine" now belongs to "Other": its rule must not count, or delete, this download.
    [other] = storage.list_subscriptions()
    assert other.id == other_id and storage.subscription_history(other.id) == []
    assert other.last_result == ""


def test_backups_and_archive_imports_are_for_this_machines_owner_only(services: AppServices) -> None:
    visitor = AccessLimits.for_public_host()
    with pytest.raises(PermissionError):
        services.create_backup(visitor)
    with pytest.raises(PermissionError):
        services.restore_backup(b"", visitor)
    with pytest.raises(PermissionError):
        services.import_download_archive(b"youtube dQw4w9WgXcQ\n", visitor)
    assert services.storage is not None and services.storage.archived_video_ids() == set()


def test_a_download_archive_from_another_program_is_added(services: AppServices) -> None:
    assert services.storage is not None
    services.storage.add_to_archive(["jNQXAC9IVRw"])
    # Notepad's "Unicode" with a BOM, as a Windows tool may have saved it.
    data = "youtube dQw4w9WgXcQ\r\nyoutube jNQXAC9IVRw\r\nvimeo 76979871\r\n".encode("utf-16")
    assert services.import_download_archive(data, AccessLimits()) == (1, 1, 1)
    assert services.storage.archived_video_ids() == {"dQw4w9WgXcQ", "jNQXAC9IVRw"}


def test_a_tool_jobs_warning_survives_a_restart(tmp_path: Path) -> None:
    import json

    request = ToolRequest("burn", str(tmp_path / "a.mp4"), {"subtitle": "a.srt"})
    job = Job(id="x", owner=LOCAL_OWNER, kind="tool", label="燒錄字幕：a.mp4", payload=request)
    job.outcome = ToolResult(output_path=tmp_path / "a.burned.mp4", warning="字幕可能有些字顯示成方框")
    saved = json.loads(json.dumps(services_module.pending_entry(job)))
    restored = services_module.restored_job(saved)
    assert restored.payload == request
    assert restored.outcome == ToolResult(output_path=tmp_path / "a.burned.mp4", warning="字幕可能有些字顯示成方框")


def test_fields_a_newer_download_step_adds_survive_a_restart(tmp_path: Path) -> None:
    import json

    outcome = finished_outcome(tmp_path / "out" / "a.mp4")
    outcome["notes"] = ["已略過 2 部會員限定影片"]  # type: ignore[typeddict-unknown-key]
    outcome["not_json"] = object()  # type: ignore[typeddict-unknown-key]
    job = Job(id="x", owner=LOCAL_OWNER, kind="download", label="l", payload=DownloadPayload(make_spec(tmp_path), AccessLimits()))
    job.outcome = outcome
    saved = json.loads(json.dumps(services_module.pending_entry(job)))
    restored = services_module.restored_job(saved).outcome
    assert restored["notes"] == ["已略過 2 部會員限定影片"]
    assert "not_json" not in restored
    assert restored["items"] == outcome["items"] and restored["stopped"] is True


# One engine per data folder


def test_a_second_engine_on_the_same_data_folder_leaves_restoring_and_checking_to_the_first(
    services: AppServices, tmp_path: Path, fake_run: FakeRun
) -> None:
    assert services.storage is not None and not services.shares_data_folder
    services.storage.add_subscription("https://www.youtube.com/@due", "Due", DownloadSettings(), str(tmp_path / "out"), 3, 1)
    stoppable(fake_run, [])
    job = services.submit_download(make_spec(tmp_path), LOCAL_OWNER, AccessLimits())
    wait_until(lambda: bool(services.storage.get_value(PENDING_JOBS_KEY)))  # type: ignore[union-attr]

    second = AppServices(is_public=False, data_dir=tmp_path / "data")
    assert second.shares_data_folder
    assert second.manager.list_jobs() == []
    assert [thread.name for thread in second._threads] == []
    # Its own jobs run, but do not replace the first engine's list of unfinished ones.
    fake_run.result = lambda url, options, should_stop: finished_outcome(options.output_dir / "second.mp4")
    own = second.submit_download(make_spec(tmp_path, url=f"{URL}&second"), LOCAL_OWNER, AccessLimits())
    wait_until(lambda: own.state == DONE)
    time.sleep(0.2)
    assert [entry["spec"]["url"] for entry in services.storage.get_value(PENDING_JOBS_KEY)] == [URL]  # type: ignore[union-attr]

    services.manager.cancel(job.id)
    wait_until(lambda: job.state == CANCELED)
    services.shutdown()
    second.shutdown()
    third = AppServices(is_public=False, data_dir=tmp_path / "data")
    assert not third.shares_data_folder
    assert {thread.name for thread in third._threads} == {"videocap-pending-jobs", "videocap-subscriptions"}


def test_an_engine_in_another_process_holds_the_data_folder(tmp_path: Path, fake_run: FakeRun) -> None:
    import subprocess
    import sys

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys; from pathlib import Path; from videocap.services import acquire_engine_lock; "
            "lock = acquire_engine_lock(Path(sys.argv[1])); print('locked' if lock else 'busy', flush=True); sys.stdin.read()",
            str(data_dir),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "locked"
        assert AppServices(is_public=False, data_dir=data_dir).shares_data_folder
    finally:
        holder.communicate("", timeout=30)
    # The lock goes with the process, so the next start runs the engine.
    assert not AppServices(is_public=False, data_dir=data_dir).shares_data_folder


def test_logged_in_jobs_in_a_row_wait_and_say_why(offline_youtube: OfflineYouTube, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Each pasted link is its own job; the 5-10 s between logged-in videos must hold between jobs too.
    monkeypatch.setattr(options_module, "LOGGED_IN_MIN_SLEEP_SECONDS", 3)
    monkeypatch.setattr(options_module, "LOGGED_IN_MAX_SLEEP_SECONDS", 3)
    monkeypatch.setattr(downloader_module, "LOGIN_PACER", downloader_module.LoginPacer())
    services = AppServices(is_public=False, data_dir=tmp_path / "data")
    urls = [offline_youtube.add_video(f"jobPace{number:04d}", f"Video {number}") for number in (1, 2)]
    settings = DownloadSettings(download_subtitles=False)
    cookies = b"# Netscape HTTP Cookie File\n"
    jobs = [
        services.submit_download(make_spec(tmp_path, url=url, settings=settings), LOCAL_OWNER, AccessLimits(), cookie_bytes=cookies)
        for url in urls
    ]
    statuses: list[str] = []
    wait_until(lambda: statuses.append(jobs[1].status_text) or jobs[1].state == DONE, timeout=60)
    assert jobs[0].state == DONE
    assert any(status.startswith("登入下載：等待") and status.endswith("秒，避免帳號被限制") for status in statuses)
    assert jobs[1].started_at is not None and jobs[0].finished_at is not None


# 下載時段: when the owner's downloads may start


def closed_window() -> DownloadWindow:
    """A window that is shut whenever the test runs: it opens two hours from now."""
    opens = datetime.now() + timedelta(hours=2)
    return DownloadWindow(enabled=True, start=opens.strftime("%H:%M"), end=(opens + timedelta(hours=1)).strftime("%H:%M"))


def at(hour: int, minute: int = 0) -> float:
    return datetime(2026, 11, 7, hour, minute).timestamp()


def owner_job(state: str = QUEUED, **fields: Any) -> Job:
    return Job(id="held", owner=LOCAL_OWNER, kind=services_module.KIND_DOWNLOAD, label="x", payload=None, state=state, **fields)


def test_outside_the_window_only_the_owners_downloads_wait(
    services: AppServices, tmp_path: Path, fake_run: FakeRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = closed_window()
    services.save_download_window(window, AccessLimits())
    held = services.submit_download(make_spec(tmp_path), LOCAL_OWNER, AccessLimits())
    # A public visitor has nobody at this machine to choose their hours, and ffmpeg asks nothing of the network.
    visitor = services.submit_download(make_spec(tmp_path), "visitor", AccessLimits.for_public_host())
    monkeypatch.setattr(services_module, "run_tool", lambda request, progress, should_stop: ToolResult(output_path=tmp_path / "a.mp4"))
    tool = services.submit_tool(ToolRequest("trim", "a.mp4"), LOCAL_OWNER)
    wait_until(lambda: visitor.state == DONE and tool.state == DONE)
    assert held.state == QUEUED
    assert services.hold_reason(held) == f"等待下載時段 {window.label}"
    assert services.hold_reason(visitor) == services.hold_reason(tool) == ""

    # Kept for the next start of the app.
    restarted_storage = services_module.AppStorage(tmp_path / "data")
    assert restarted_storage.load_download_window() == window
    services.save_download_window(replace(window, enabled=False), AccessLimits())
    wait_until(lambda: held.state == DONE)


def test_the_window_is_read_from_this_computers_clock(services: AppServices) -> None:
    services.save_download_window(DownloadWindow(enabled=True, start="22:00", end="06:00"), AccessLimits())
    job = owner_job()
    assert services.hold_reason(job, now=at(21, 59)) == "等待下載時段 22:00–06:00"
    assert services.hold_reason(job, now=at(23, 0)) == ""
    assert services.hold_reason(job, now=at(3, 0)) == ""
    assert services.hold_reason(job, now=at(6, 0)) == "等待下載時段 22:00–06:00"
    # Its own start time comes first, and a job already on its way is not held back.
    assert services.hold_reason(owner_job(not_before=at(12, 0)), now=at(11, 0)) == ""
    assert services.hold_reason(owner_job(RUNNING), now=at(12, 0)) == ""


def test_subscription_checks_wait_for_the_window(services: AppServices, tmp_path: Path, fake_run: FakeRun) -> None:
    assert services.storage is not None
    subscription_id = services.storage.add_subscription("https://www.youtube.com/@due", "Due", DownloadSettings(), str(tmp_path / "out"), 3, 1)
    services.save_download_window(closed_window(), AccessLimits())
    assert services.run_due_subscriptions() == 0
    assert services.manager.list_jobs() == []
    assert services.storage.get_subscription(subscription_id).last_checked_at is None  # type: ignore[union-attr]

    services.save_download_window(DownloadWindow(), AccessLimits())
    assert services.run_due_subscriptions() == 1


def test_only_the_owner_sets_the_window(services: AppServices) -> None:
    with pytest.raises(PermissionError):
        services.save_download_window(closed_window(), AccessLimits.for_public_host())
    assert services.download_window == DownloadWindow()


def test_a_restored_backup_brings_its_window(services: AppServices, tmp_path: Path) -> None:
    window = DownloadWindow(enabled=True, start="01:00", end="05:00")
    services.save_download_window(window, AccessLimits())
    backup = services.create_backup(AccessLimits())
    services.save_download_window(DownloadWindow(), AccessLimits())
    services.restore_backup(backup, AccessLimits())
    assert services.download_window == window


# A pause after YouTube limits the requests


LIMITED = humanize_ytdlp_error("ERROR: [youtube] limited0001: Unable to download API page: HTTP Error 429: Too Many Requests")


def limited_or_blocking(fake_run: FakeRun, release: Optional[threading.Event] = None) -> None:
    """Links with "limit" in them fail as YouTube's HTTP 429 would, after the release for "slow" ones; links with
    "block" run until stopped; the rest download."""

    def run(url: str, options: Any, should_stop: Any) -> DownloadOutcome:
        if "limit" in url:
            if "slow" in url and release is not None:
                release.wait(5)
            outcome = empty_outcome()
            outcome["errors"] = [LIMITED]
            return outcome
        if "block" in url:
            while not should_stop():
                time.sleep(0.005)
            outcome = empty_outcome()
            outcome["stopped"] = True
            return outcome
        return finished_outcome(options.output_dir / f"{url[-11:]}.mp4")

    fake_run.result = run


def video(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def test_a_rate_limited_job_pauses_the_owners_queue(
    services: AppServices, tmp_path: Path, fake_run: FakeRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    limited_or_blocking(fake_run)
    running = services.submit_download(make_spec(tmp_path, url=video("blocking001")), LOCAL_OWNER, AccessLimits())
    wait_until(lambda: running.state == RUNNING)
    started = time.time()
    failed = services.submit_download(make_spec(tmp_path, url=video("limited0001")), LOCAL_OWNER, AccessLimits())
    wait_until(lambda: failed.state == FAILED)
    waiting = services.submit_download(make_spec(tmp_path, url=video("waiting0001")), LOCAL_OWNER, AccessLimits())

    # Kept, not cancelled: the running one keeps what it has, the waiting one its place.
    wait_until(lambda: running.state == PAUSED)
    time.sleep(0.05)
    assert waiting.state == QUEUED
    until = services.rate_limit_until()
    assert until is not None and started + 15 * 60 - 1 <= until <= time.time() + 15 * 60
    assert services.rate_limit_pause is not None and "429" in services.rate_limit_pause.reason
    assert services.hold_reason(waiting) == services.hold_reason(running) == services_module.RATE_LIMIT_HOLD_TEXT
    # The job that met the limit stays failed with its reason, for its 重試 button.
    assert failed.state == FAILED and services.hold_reason(failed) == ""

    # Nothing else of the owner's asks YouTube meanwhile, but a visitor and a conversion are not the owner's queue.
    assert services.storage is not None
    services.storage.add_subscription("https://www.youtube.com/@due", "Due", DownloadSettings(), str(tmp_path / "out"), 3, 1)
    assert services.run_due_subscriptions() == 0
    visitor = services.submit_download(make_spec(tmp_path, url=video("visitor0001")), "visitor", AccessLimits.for_public_host())
    monkeypatch.setattr(services_module, "run_tool", lambda request, progress, should_stop: ToolResult(output_path=tmp_path / "a.mp4"))
    tool = services.submit_tool(ToolRequest("trim", "a.mp4"), LOCAL_OWNER)
    wait_until(lambda: visitor.state == DONE and tool.state == DONE)
    services.manager.cancel(running.id)
    services.manager.cancel(waiting.id)


def test_the_queue_continues_by_itself_when_the_pause_is_over(services: AppServices, tmp_path: Path, fake_run: FakeRun) -> None:
    limited_or_blocking(fake_run)
    running = services.submit_download(make_spec(tmp_path, url=video("blocking001")), LOCAL_OWNER, AccessLimits())
    wait_until(lambda: running.state == RUNNING)
    failed = services.submit_download(make_spec(tmp_path, url=video("limited0001")), LOCAL_OWNER, AccessLimits())
    wait_until(lambda: running.state == PAUSED)
    waiting = services.submit_download(make_spec(tmp_path, url=video("waiting0001")), LOCAL_OWNER, AccessLimits())
    # One the person paused stays paused.
    by_hand = services.submit_download(make_spec(tmp_path, url=video("byhand00001")), LOCAL_OWNER, AccessLimits())
    services.manager.pause(by_hand.id)
    until = services.rate_limit_until()
    assert until is not None

    services._finish_rate_limit_pause(now=until - 1)
    assert services.rate_limit_until() == until and running.state == PAUSED
    services._finish_rate_limit_pause(now=until + 1)
    assert services.rate_limit_pause is None
    wait_until(lambda: waiting.state == DONE and running.state == RUNNING)
    assert running.payload.spec.resuming is True
    assert by_hand.state == PAUSED and failed.state == FAILED
    services.manager.cancel(running.id)


def test_continuing_by_hand_ends_the_pause(services: AppServices, tmp_path: Path, fake_run: FakeRun) -> None:
    limited_or_blocking(fake_run)
    running = services.submit_download(make_spec(tmp_path, url=video("blocking001")), LOCAL_OWNER, AccessLimits())
    wait_until(lambda: running.state == RUNNING)
    services.submit_download(make_spec(tmp_path, url=video("limited0001")), LOCAL_OWNER, AccessLimits())
    wait_until(lambda: running.state == PAUSED)
    waiting = services.submit_download(make_spec(tmp_path, url=video("waiting0001")), LOCAL_OWNER, AccessLimits())

    with pytest.raises(PermissionError):
        services.end_rate_limit_pause(AccessLimits.for_public_host())
    assert services.rate_limit_until() is not None
    services.end_rate_limit_pause(AccessLimits())
    assert services.rate_limit_until() is None
    wait_until(lambda: waiting.state == DONE and running.state == RUNNING)
    services.manager.cancel(running.id)


def test_a_second_limit_inside_the_pause_does_not_lengthen_it(services: AppServices, tmp_path: Path, fake_run: FakeRun) -> None:
    release = threading.Event()
    limited_or_blocking(fake_run, release)
    slow = services.submit_download(make_spec(tmp_path, url=video("slowlimit01")), LOCAL_OWNER, AccessLimits())
    wait_until(lambda: slow.state == RUNNING)
    services.submit_download(make_spec(tmp_path, url=video("limited0001")), LOCAL_OWNER, AccessLimits())
    wait_until(lambda: services.rate_limit_until() is not None)
    until = services.rate_limit_until()
    time.sleep(0.05)
    release.set()
    wait_until(lambda: slow.state == FAILED)
    assert services.rate_limit_until() == until


def test_a_visitors_rate_limit_pauses_nothing(tmp_path: Path, fake_run: FakeRun) -> None:
    # A public host has nobody to press 立即繼續, and each visitor's page shows their own failure.
    limited_or_blocking(fake_run)
    services = AppServices(is_public=True)
    job = services.submit_download(make_spec(tmp_path, url=video("limited0001")), "visitor", AccessLimits.for_public_host())
    wait_until(lambda: job.state == FAILED)
    assert services.rate_limit_until() is None


# 網路中斷後自動重試


DROPPED = humanize_ytdlp_error("ERROR: [WinError 10054] 遠端主機已強制關閉一個現存的連線。")


def always_failing(fake_run: FakeRun, message: str) -> None:
    def run(url: str, options: Any, should_stop: Any) -> DownloadOutcome:
        outcome = empty_outcome()
        outcome["errors"] = [message]
        return outcome

    fake_run.result = run


def test_a_lost_connection_is_retried_by_itself_with_longer_waits(services: AppServices, tmp_path: Path, fake_run: FakeRun) -> None:
    always_failing(fake_run, DROPPED)
    job = services.submit_download(make_spec(tmp_path), LOCAL_OWNER, AccessLimits())
    for attempt, delay in enumerate((120, 300, 600), start=1):
        wait_until(lambda: job.auto_retries == attempt and job.state == QUEUED)
        assert job.not_before is not None and time.time() + delay - 5 <= job.not_before <= time.time() + delay
        # Keeps finished files and partial downloads, as 重試 does.
        assert job.payload.spec.resuming is True
        assert any(f"{delay // 60} 分鐘後自動重試（第 {attempt} 次）" in line for line in job.logs)
        # As if the wait were over.
        job.not_before = time.time() - 1
    wait_until(lambda: job.state == FAILED)
    assert job.auto_retries == 3 and len(fake_run.calls) == 4
    assert services_module.first_error([job]) == DROPPED


def test_retrying_by_hand_does_not_wait_for_the_automatic_retry(services: AppServices, tmp_path: Path, fake_run: FakeRun) -> None:
    always_failing(fake_run, DROPPED)
    job = services.submit_download(make_spec(tmp_path), LOCAL_OWNER, AccessLimits())
    wait_until(lambda: job.auto_retries == 1 and job.state == QUEUED)
    fake_run.result = lambda url, options, should_stop: finished_outcome(options.output_dir / "video.mp4")
    services.retry_now(job)
    wait_until(lambda: job.state == DONE)
    assert job.auto_retries == 0


@pytest.mark.parametrize("message", [LIMITED, "這部影片是私人影片"])
def test_only_a_lost_connection_is_retried_by_itself(services: AppServices, tmp_path: Path, fake_run: FakeRun, message: str) -> None:
    # A rate limit has the queue's pause instead, and a private video would fail the same way again.
    always_failing(fake_run, message)
    job = services.submit_download(make_spec(tmp_path), LOCAL_OWNER, AccessLimits())
    wait_until(lambda: job.state == FAILED)
    time.sleep(0.05)
    assert job.state == FAILED and job.auto_retries == 0
    services.end_rate_limit_pause(AccessLimits())


def test_a_visitors_lost_connection_is_left_failed(tmp_path: Path, fake_run: FakeRun) -> None:
    # Their page may be gone by the time a retry would run.
    always_failing(fake_run, DROPPED)
    services = AppServices(is_public=True)
    job = services.submit_download(make_spec(tmp_path), "visitor", AccessLimits.for_public_host())
    wait_until(lambda: job.state == FAILED)
    time.sleep(0.05)
    assert job.state == FAILED and job.auto_retries == 0


def test_a_job_with_an_uploaded_login_is_not_retried_without_it(services: AppServices, tmp_path: Path, fake_run: FakeRun) -> None:
    # The login goes when the job stops, and only the person's page can give it back.
    always_failing(fake_run, DROPPED)
    job = services.submit_download(make_spec(tmp_path, needs_cookie_file=True), LOCAL_OWNER, AccessLimits(), cookie_bytes=b"cookies")
    wait_until(lambda: job.state == FAILED)
    time.sleep(0.05)
    assert job.state == FAILED and job.auto_retries == 0


def test_the_retry_count_survives_a_restart(tmp_path: Path) -> None:
    job = Job(
        id="x",
        owner=LOCAL_OWNER,
        kind=services_module.KIND_DOWNLOAD,
        label=URL,
        payload=DownloadPayload(make_spec(tmp_path), AccessLimits()),
        not_before=time.time() + 300,
        auto_retries=2,
    )
    entry = services_module.pending_entry(job)
    assert entry is not None and entry["auto_retries"] == 2
    assert services_module.restored_job(entry).auto_retries == 2
    # A list saved before the count existed.
    del entry["auto_retries"]
    assert services_module.restored_job(entry).auto_retries == 0

"""Notifications, sent for real over HTTP to a server on this machine that stands in for Discord, Telegram and ntfy."""

from __future__ import annotations

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterator, Optional
from urllib.parse import parse_qs, urlparse

import pytest

import videocap.notify as notify
import videocap.services as services_module
from videocap.downloader import DownloadOutcome, empty_outcome
from videocap.errors import humanize_ytdlp_error
from videocap.jobs import DONE, FAILED
from videocap.notify import NotifyTargets, masked, send_all, send_in_background, target_problems, targets_from_dict
from videocap.options import AccessLimits
from videocap.services import LOCAL_OWNER, AppServices
from videocap.settings import DownloadSettings, JobSpec
from videocap.storage import NOTIFY_TARGETS_KEY


TOKEN = "123456789:AAH-secret-token-abcdefghijklmnopqrstu"


class FakeService:
    """Records each request it receives and answers with the status the test chooses."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.status = 200
        self.delay = 0.0
        self.received = threading.Condition()

    def wait_for(self, count: int, timeout: float = 5.0) -> list[dict[str, Any]]:
        with self.received:
            assert self.received.wait_for(lambda: len(self.requests) >= count, timeout), self.requests
        return self.requests


class Handler(BaseHTTPRequestHandler):
    """Hands each request to the running test's FakeService. Outside such a test there is none, and it refuses."""

    current: Optional[FakeService] = None

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        fake = Handler.current
        if fake is None:
            self.send_response(503)
            self.end_headers()
            return
        time.sleep(fake.delay)
        with fake.received:
            fake.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
            fake.received.notify_all()
        self.send_response(fake.status)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, format: str, *args: Any) -> None:
        pass


# One server for the whole run, on a port the system picks, so two test runs at once never collide. BASE stays the
# same for the run, which lets other test files import it.
SERVER = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
SERVER.daemon_threads = True
threading.Thread(target=SERVER.serve_forever, name="test-notify-server", daemon=True).start()
PORT = SERVER.server_address[1]
BASE = f"http://127.0.0.1:{PORT}"


@pytest.fixture
def service() -> Iterator[FakeService]:
    fake = FakeService()
    Handler.current = fake
    yield fake
    Handler.current = None


@pytest.fixture(autouse=True)
def local_telegram(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(notify, "TELEGRAM_API", BASE)


def test_each_service_gets_the_message_in_its_own_form(service: FakeService) -> None:
    targets = NotifyTargets(
        discord_webhook=f"{BASE}/api/webhooks/1/abc", telegram_token=TOKEN, telegram_chat_id="-1001234", ntfy_url=f"{BASE}/my-topic"
    )
    title, message = "下載完成", "@everyone 貓咪影片\n共 1 個項目"
    assert send_all(targets, title, message) == []
    discord, telegram, ntfy = service.wait_for(3)

    assert discord["path"] == "/api/webhooks/1/abc"
    sent = json.loads(discord["body"])
    assert sent["content"] == f"**{title}**\n{message}"
    # A title from an uploader cannot ping everyone on the owner's server.
    assert sent["allowed_mentions"] == {"parse": []}
    assert discord["headers"]["User-Agent"] == "youtube-videocap"

    assert telegram["path"] == f"/bot{TOKEN}/sendMessage"
    assert json.loads(telegram["body"]) == {"chat_id": "-1001234", "text": f"{title}\n{message}"}

    assert urlparse(ntfy["path"]).path == "/my-topic"
    assert parse_qs(urlparse(ntfy["path"]).query)["title"] == [title]
    assert ntfy["body"].decode("utf-8") == message


def test_a_failure_names_the_service_but_never_the_token(service: FakeService) -> None:
    service.status = 401
    problems = send_all(NotifyTargets(telegram_token=TOKEN, telegram_chat_id="42", ntfy_url=f"{BASE}/topic"), "t", "m")
    assert problems == ["Telegram：伺服器回應 HTTP 401，請檢查設定是否正確。", "ntfy：伺服器回應 HTTP 401，請檢查設定是否正確。"]
    assert all("secret" not in problem for problem in problems)


def test_a_slow_service_times_out(service: FakeService) -> None:
    service.delay = 1.0
    [problem] = send_all(NotifyTargets(ntfy_url=f"{BASE}/topic"), "t", "m", timeout=0.2)
    assert problem.startswith("ntfy：") and ("逾時" in problem or "連不上" in problem)


def test_background_sending_returns_at_once_and_only_logs_a_failure(service: FakeService, caplog: pytest.LogCaptureFixture) -> None:
    service.status = 500
    service.delay = 0.3
    started = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="videocap.notify"):
        thread = send_in_background(NotifyTargets(ntfy_url=f"{BASE}/topic"), "t", "m")
        assert time.monotonic() - started < 0.2
        thread.join(timeout=5)
    assert "ntfy：伺服器回應 HTTP 500" in caplog.text


def test_targets_are_checked_before_they_are_saved() -> None:
    assert target_problems(NotifyTargets()) == []
    assert target_problems(
        NotifyTargets("https://discord.com/api/webhooks/123/abc-DEF_1", TOKEN, "@my_channel", "https://ntfy.sh/videocap-mine")
    ) == []
    problems = target_problems(NotifyTargets("https://example.com/hook", "not-a-token", "", "ntfy.sh"))
    assert len(problems) == 4
    assert any("Discord" in problem for problem in problems) and any("Chat ID" in problem for problem in problems)


def test_saved_targets_show_only_their_end() -> None:
    assert masked(TOKEN) == f"已設定（…{TOKEN[-4:]}）"
    assert TOKEN[:-4] not in masked(TOKEN)
    assert masked("short") == "已設定"
    # Whatever the database holds, only text fields come back.
    assert targets_from_dict({"ntfy_url": " https://ntfy.sh/a ", "telegram_token": 5, "other": "x"}) == NotifyTargets(ntfy_url="https://ntfy.sh/a")
    assert targets_from_dict(None) == NotifyTargets()


# What the engine sends


@pytest.fixture
def engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[AppServices]:
    services = AppServices(is_public=False, data_dir=tmp_path / "data")
    assert services.storage is not None
    services.storage.set_value(NOTIFY_TARGETS_KEY, NotifyTargets(ntfy_url=f"{BASE}/videocap").to_dict())
    yield services
    services.shutdown()


def fake_download(result: Callable[[str, Any], DownloadOutcome]) -> Callable[..., DownloadOutcome]:
    def run(url: str, options: Any, progress_ui: Any, status_ui: Any, should_stop: Any, on_log: Any) -> DownloadOutcome:
        return result(url, options)

    return run


def finished(url: str, options: Any) -> DownloadOutcome:
    options.output_dir.mkdir(parents=True, exist_ok=True)
    (options.output_dir / f"{url[-11:]}.mp4").write_bytes(b"video")
    outcome = empty_outcome()
    item = {"title": url[-11:], "path": str(options.output_dir / f"{url[-11:]}.mp4"), "subtitle_paths": [], "extra_paths": []}
    item.update({"video_id": url[-11:], "channel": "C", "webpage_url": url, "duration": 1.0, "upload_date": "", "thumbnail": "", "quality": ""})
    outcome["items"] = [item]  # type: ignore[list-item]
    return outcome


def failing(url: str, options: Any) -> DownloadOutcome:
    if "bad" in url:
        outcome = empty_outcome()
        outcome["errors"] = [f"這部影片是私人影片：{url[-11:]}"]
        return outcome
    return finished(url, options)


def spec(tmp_path: Path, video_id: str, **extra: Any) -> JobSpec:
    return JobSpec(url=f"https://www.youtube.com/watch?v={video_id}", settings=DownloadSettings(), output_dir=str(tmp_path / "out"), **extra)


def messages(service: FakeService) -> list[tuple[str, str]]:
    return [(parse_qs(urlparse(request["path"]).query)["title"][0], request["body"].decode("utf-8")) for request in service.requests]


def wait_for_jobs(jobs: list[Any]) -> None:
    deadline = time.time() + 5
    while not all(job.state in (DONE, FAILED) for job in jobs):
        assert time.time() < deadline
        time.sleep(0.01)


def test_a_batch_of_links_sends_one_message_when_all_of_it_is_done(
    engine: AppServices, service: FakeService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(services_module, "run_download", fake_download(failing))
    jobs = [
        engine.submit_download(spec(tmp_path, video_id), LOCAL_OWNER, AccessLimits(), batch_id="batch-1")
        for video_id in ("good0000001", "bad00000002", "good0000003")
    ]
    wait_for_jobs(jobs)
    service.wait_for(1)
    time.sleep(0.3)
    [(title, body)] = messages(service)
    assert title == "下載結束，有失敗的項目"
    assert "3 個連結：完成 2 個，共 2 個項目；失敗 1 個" in body
    assert "第一項：這部影片是私人影片：bad00000002" in body


def test_a_single_download_says_what_finished(engine: AppServices, service: FakeService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(services_module, "run_download", fake_download(failing))
    done = engine.submit_download(spec(tmp_path, "good0000001"), LOCAL_OWNER, AccessLimits())
    wait_for_jobs([done])
    service.wait_for(1)
    failed = engine.submit_download(spec(tmp_path, "bad00000002"), LOCAL_OWNER, AccessLimits())
    wait_for_jobs([failed])
    service.wait_for(2)
    assert messages(service) == [
        ("下載完成", "https://www.youtube.com/watch?v=good0000001\n共 1 個項目"),
        ("下載失敗", "https://www.youtube.com/watch?v=bad00000002\n這部影片是私人影片：bad00000002"),
    ]


def test_a_subscription_check_tells_only_of_new_videos(
    engine: AppServices, service: FakeService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = engine.storage
    assert storage is not None
    subscription_id = storage.add_subscription("https://www.youtube.com/@news/videos", "新聞台", DownloadSettings(), str(tmp_path / "out"), 5, 24)
    subscription = storage.get_subscription(subscription_id)
    assert subscription is not None

    monkeypatch.setattr(services_module, "run_download", fake_download(lambda url, options: empty_outcome()))
    quiet = engine.check_subscription(subscription)
    wait_for_jobs([quiet])
    monkeypatch.setattr(services_module, "run_download", fake_download(lambda url, options: finished("https://youtu.be/new00000001", options)))
    found = engine.check_subscription(subscription)
    wait_for_jobs([found])
    service.wait_for(1)
    time.sleep(0.3)
    assert messages(service) == [("訂閱有新影片", "「新聞台」下載了 1 部新影片")]


def test_nothing_is_sent_for_a_visitors_job_or_without_targets(
    engine: AppServices, service: FakeService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(services_module, "run_download", fake_download(finished))
    monkeypatch.setattr(services_module, "CLOUD_OUTPUT_ROOT", tmp_path / "public")
    visitor = engine.submit_download(spec(tmp_path, "visitor0001"), "visitor", AccessLimits.for_public_host())
    wait_for_jobs([visitor])
    assert engine.storage is not None
    engine.storage.set_value(NOTIFY_TARGETS_KEY, {})
    owner = engine.submit_download(spec(tmp_path, "owner000001"), LOCAL_OWNER, AccessLimits())
    wait_for_jobs([owner])
    time.sleep(0.3)
    assert service.requests == []


def test_a_slow_notification_service_does_not_hold_up_downloads(
    engine: AppServices, service: FakeService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service.delay = 2.0
    monkeypatch.setattr(services_module, "run_download", fake_download(finished))
    started = time.monotonic()
    jobs = [engine.submit_download(spec(tmp_path, f"video{index:06d}"), LOCAL_OWNER, AccessLimits()) for index in range(3)]
    wait_for_jobs(jobs)
    assert time.monotonic() - started < 1.5


def test_only_the_owner_saves_targets_or_sends_a_test(engine: AppServices, service: FakeService) -> None:
    visitor = AccessLimits.for_public_host()
    with pytest.raises(PermissionError):
        engine.save_notify_targets(NotifyTargets(ntfy_url="https://ntfy.sh/stranger"), visitor)
    with pytest.raises(PermissionError):
        engine.send_test_notification(visitor)
    with pytest.raises(ValueError, match="Discord"):
        engine.save_notify_targets(NotifyTargets(discord_webhook="https://example.com/x"), AccessLimits())
    assert engine.notify_targets() == NotifyTargets(ntfy_url=f"{BASE}/videocap")

    assert engine.send_test_notification(AccessLimits()) == []
    service.wait_for(1)
    # Earlier tests' background senders may still deliver to the shared server, so only this message is checked.
    assert ("測試通知", "YouTube影片下載平台的通知設定成功。") in messages(service)


def test_a_rate_limit_pauses_the_queue_with_one_message(
    engine: AppServices, service: FakeService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = threading.Event()

    def limited(url: str, options: Any) -> DownloadOutcome:
        if "slow" in url:
            release.wait(5)
        outcome = empty_outcome()
        outcome["errors"] = [humanize_ytdlp_error(f"ERROR: [youtube] {url[-11:]}: HTTP Error 429: Too Many Requests")]
        return outcome

    monkeypatch.setattr(services_module, "run_download", fake_download(limited))
    slow = engine.submit_download(spec(tmp_path, "slowlimit01"), LOCAL_OWNER, AccessLimits())
    first = engine.submit_download(spec(tmp_path, "limited0001"), LOCAL_OWNER, AccessLimits())
    wait_for_jobs([first])
    # A second failure within the pause, and a job that waits through it, add nothing.
    release.set()
    wait_for_jobs([slow])
    engine.submit_download(spec(tmp_path, "waiting0001"), LOCAL_OWNER, AccessLimits())
    service.wait_for(1)
    time.sleep(0.3)
    [(title, body)] = messages(service)
    until = engine.rate_limit_until()
    assert until is not None
    assert title == "下載佇列已暫停"
    assert body.startswith(f"YouTube 暫時限制了請求（HTTP 429／要求驗證），佇列已暫停，{time.strftime('%H:%M', time.localtime(until))} 後自動繼續。")
    assert "https://www.youtube.com/watch?v=limited0001：YouTube 暫時限制了請求頻率" in body
    engine.end_rate_limit_pause(AccessLimits())

"""The queue page: browser notifications, 清除我的資料 for public visitors, its groups and global actions, and how
finished jobs show warnings and notes."""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta
from datetime import time as dt_time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import streamlit as st
from streamlit.runtime.context import ContextProxy
from streamlit.testing.v1 import AppTest

import videocap.services as services_module
from offline_youtube import OfflineYouTube, offline_media_fixture, offline_youtube_fixture  # noqa: F401
from videocap.downloader import empty_outcome
from videocap.jobs import CANCELED, DONE, FAILED, PAUSED, QUEUED, RUNNING, Job, new_job_id
from videocap.media_tools import ToolResult
from videocap.options import AccessLimits
from videocap.settings import DownloadSettings, JobSpec


ROOT = Path(__file__).resolve().parents[1]
# Breaks out of a script that pasted it in as it is, and is Markdown and LaTeX for a toast.
HOSTILE_TITLE = 'Cat </script><script>window.pwned=1</script> "$5" & \'more\''
JS_NOTE = "缺少 JavaScript 執行環境，畫質可能較低"


@pytest.fixture(autouse=True)
def isolated_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VIDEOCAP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("VIDEOCAP_OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setenv("VIDEOCAP_TRUST_REMOTE", "1")
    monkeypatch.setenv("VIDEOCAP_MODE", "local")
    monkeypatch.delenv("VIDEOCAP_PASSWORD", raising=False)
    monkeypatch.delenv("VIDEOCAP_ALLOWED_HOSTS", raising=False)
    st.cache_resource.clear()
    yield
    # Jobs left waiting would otherwise keep running into the next test.
    for public in (False, True):
        manager = services(public).manager
        for job in manager.list_jobs():
            manager.cancel(job.id)
    st.cache_resource.clear()


def public_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIDEOCAP_MODE", "cloud")
    monkeypatch.delenv("VIDEOCAP_TRUST_REMOTE", raising=False)


def page_script(page_name: str) -> None:
    import importlib

    from videocap.ui.context import build_context, init_session_state

    ctx = build_context()
    init_session_state(ctx)
    module = importlib.import_module(f"videocap.ui.page_{page_name}")
    getattr(module, f"render_{page_name}_page")(ctx)


def run_page(page_name: str = "queue") -> AppTest:
    app = AppTest.from_function(page_script, kwargs={"page_name": page_name}, default_timeout=30)
    app.run()
    assert not app.exception, app.exception
    return app


def run_queue_in_main() -> AppTest:
    """The whole app, as a browser gets it, moved to the queue page."""
    from streamlit.util import calc_hash

    app = AppTest.from_file(str(ROOT / "main.py"), default_timeout=30)
    app.run()
    app._page_hash = calc_hash("queue")
    app.run()
    assert not app.exception, app.exception
    return app


def services(public: bool = False) -> Any:
    from videocap.ui.context import get_services

    return get_services(public)


def add_job(state: str, owner: str = "local", public: bool = False, **fields: Any) -> Job:
    job = Job(
        id=new_job_id(),
        owner=owner,
        kind=fields.pop("kind", "download"),
        label=fields.pop("label", "x"),
        payload=fields.pop("payload", None),
        state=state,
        **fields,
    )
    manager = services(public).manager
    with manager._lock:
        manager._jobs[job.id] = job
    return job


def finished_download(tmp_path: Path, title: str = "Fake Title", **outcome_fields: Any) -> Job:
    path = tmp_path / "video.mp4"
    path.write_bytes(b"video-bytes")
    outcome: Any = empty_outcome()
    outcome["items"] = [
        {
            "title": title, "path": str(path), "subtitle_paths": [], "extra_paths": [], "video_id": "jNQXAC9IVRw",
            "channel": "Channel", "webpage_url": "https://www.youtube.com/watch?v=jNQXAC9IVRw", "duration": 19.0,
            "upload_date": "20050424", "thumbnail": "", "quality": "mp4",
        }
    ]  # fmt: skip
    outcome.update(outcome_fields)
    return add_job(DONE, label=title, outcome=outcome, finished_at=time.time())


def button(app: AppTest, label: str) -> Any:
    return next(item for item in app.button if str(item.label) == label)


def texts(elements: Any) -> str:
    return " ".join(str(element.value) for element in elements)


def scripts(app: AppTest, needle: str) -> list[str]:
    return [element.proto.body for element in app.get("html") if needle in element.proto.body]


def wait_until(condition: Any, seconds: float = 20) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline and not condition():
        time.sleep(0.02)
    assert condition()


def notices_in(script: str) -> list[dict[str, str]]:
    match = re.search(r"for \(const notice of (.*?)\) \{", script)
    assert match, script
    return json.loads(match.group(1))


# 1 - browser desktop notifications


def test_notifications_are_off_until_turned_on() -> None:
    app = run_page()
    assert app.toggle(key="keep_browser_notify").value is False
    assert not scripts(app, "Notification")


def test_turning_notifications_on_asks_the_browser_right_after_the_click() -> None:
    app = run_page()
    app.toggle(key="keep_browser_notify").set_value(True).run()
    asking = scripts(app, "requestPermission")
    assert len(asking) == 1
    assert "if (true &&" in asking[0]
    assert 'document.cookie = "videocap_notify=1; path=/; SameSite=Lax"' in asking[0]

    # Later runs only say what the browser decided: a question outside a click is ignored or held against the site.
    app.run()
    assert "if (false &&" in scripts(app, "requestPermission")[0]

    app.toggle(key="keep_browser_notify").set_value(False).run()
    assert not scripts(app, "requestPermission")
    assert scripts(app, 'document.cookie = "videocap_notify=; max-age=0; path=/"')


def test_the_choice_lasts_through_a_refresh_of_the_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ContextProxy, "cookies", property(lambda self: {"videocap_notify": "1"}))
    app = run_page()
    assert app.toggle(key="keep_browser_notify").value is True


@pytest.mark.parametrize("enabled", [True, False])
def test_a_finished_job_notifies_the_browser_only_when_asked(enabled: bool) -> None:
    app = run_page()
    app.session_state["keep_browser_notify"] = enabled
    app.run()
    add_job(DONE, label=HOSTILE_TITLE, finished_at=time.time(), status_text="完成")
    failed = add_job(FAILED, label="Broken", finished_at=time.time() + 1, error="影片已被移除")
    failed.status_text = failed.error
    app.run()
    assert not app.exception, app.exception
    assert len(app.toast) == 2
    sent = scripts(app, "new Notification")
    if not enabled:
        assert not sent
        return
    assert len(sent) == 1
    # A title from YouTube stays text: nothing in it can end the script and start another.
    assert sent[0].count("</script>") == 1 and "<script>window.pwned" not in sent[0]
    notices = notices_in(sent[0])
    assert [(notice["title"], notice["body"]) for notice in notices] == [
        (f"完成：{HOSTILE_TITLE}", "完成"),
        ("失敗：Broken", "影片已被移除"),
    ]


def test_a_job_notifies_once_even_when_the_page_runs_again() -> None:
    app = run_page()
    app.session_state["keep_browser_notify"] = True
    app.run()
    add_job(DONE, label="Once", finished_at=time.time())
    app.run()
    assert scripts(app, "new Notification")
    app.run()
    assert not scripts(app, "new Notification")


def chromium_page(html: str, setup: str, url: str = "http://localhost/") -> Any:
    """Runs html in a real Chromium page served from an http address, so cookies work, with setup run first.
    Browsers treat localhost as secure, and allow notifications only on secure pages."""
    sync_api = pytest.importorskip("playwright.sync_api")
    playwright = sync_api.sync_playwright().start()
    try:
        browser = playwright.chromium.launch()
    except Exception as exc:
        playwright.stop()
        pytest.skip(f"Chromium is not installed: {exc}")
    page = browser.new_page()
    page.route(url, lambda route: route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html))
    page.add_init_script(setup)
    page.goto(url)
    page.wait_for_timeout(200)
    return SimpleNamespace(page=page, close=lambda: (browser.close(), playwright.stop()))


# Stands in for the browser's notifications, which a headless browser cannot show.
NOTIFICATION_STUB = """
window.shown = [];
window.asked = 0;
class StubNotification {
  constructor(title, options) { window.shown.push({title, ...options}); }
  static requestPermission() { window.asked += 1; StubNotification.permission = window.answer; return Promise.resolve(window.answer); }
}
StubNotification.permission = 'granted';
window.answer = 'denied';
window.Notification = StubNotification;
document.hasFocus = () => false;
"""


def test_a_real_browser_shows_the_title_as_it_is() -> None:
    from videocap.ui.common import browser_notice, browser_notification_script

    script = browser_notification_script([browser_notice(("job", 1.5), f"完成：{HOSTILE_TITLE}", "1 個項目完成")])
    browser = chromium_page(f"<body>{script}</body>", NOTIFICATION_STUB)
    try:
        assert browser.page.evaluate("window.pwned") is None
        assert browser.page.evaluate("window.shown") == [
            {"title": f"完成：{HOSTILE_TITLE}", "body": "1 個項目完成", "tag": "videocap-job-1.5"}
        ]
    finally:
        browser.close()


def test_a_real_browser_asks_once_and_says_when_notifications_are_blocked() -> None:
    from videocap.ui.common import notify_setup_script

    setup = NOTIFICATION_STUB.replace("permission = 'granted'", "permission = 'default'")
    browser = chromium_page(f"<body><div>{notify_setup_script(True, True, 1)}</div></body>", setup)
    try:
        assert browser.page.evaluate("window.asked") == 1
        assert "封鎖" in browser.page.inner_text(".notify-status")
        assert "videocap_notify=1" in browser.page.evaluate("document.cookie")
        browser.page.set_content(f"<div>{notify_setup_script(False, True, 2)}</div>")
        assert "videocap_notify" not in browser.page.evaluate("document.cookie")
    finally:
        browser.close()


def test_a_page_opened_over_plain_http_is_not_told_to_allow_notifications() -> None:
    from videocap.ui.common import notify_setup_script

    # A NAS or another computer on the network opens the app as http://192.168.x.x, where browsers refuse
    # notifications whatever the site settings say, and report them as blocked.
    setup = NOTIFICATION_STUB.replace("permission = 'granted'", "permission = 'denied'")
    browser = chromium_page(f"<body><div>{notify_setup_script(True, True, 1)}</div></body>", setup, url="http://192.168.1.20/")
    try:
        assert browser.page.evaluate("window.asked") == 0
        shown = browser.page.inner_text(".notify-status")
        assert "https" in shown and "改成允許" not in shown
    finally:
        browser.close()


# 2 - 清除我的資料


def test_only_public_visitors_are_offered_to_clear_their_data(monkeypatch: pytest.MonkeyPatch) -> None:
    assert not [item for item in run_page().button if item.label == "清除我的資料"]
    public_mode(monkeypatch)
    assert [item for item in run_page().button if item.label == "清除我的資料"]


def test_clearing_my_data_removes_jobs_files_login_and_cookies(
    monkeypatch: pytest.MonkeyPatch, offline_youtube: OfflineYouTube
) -> None:
    public_mode(monkeypatch)
    finished_url = offline_youtube.add_video("doneVideo01", "Done video")
    running_url = offline_youtube.add_video("slowVideo01", "Slow video")
    real_download = services_module.run_download

    def slow_for_one(url: str, options: Any, progress_ui: Any, status_ui: Any, should_stop: Any, on_log: Any) -> Any:
        if url == running_url:
            # Writes a file first, then runs until cancelled, the way a long download would.
            options.output_dir.mkdir(parents=True, exist_ok=True)
            (options.output_dir / "part.mp4.part").write_bytes(b"partial")
            while not should_stop():
                time.sleep(0.02)
            outcome = empty_outcome()
            outcome["stopped"] = True
            return outcome
        return real_download(url, options, progress_ui, status_ui, should_stop, on_log)

    monkeypatch.setattr(services_module, "run_download", slow_for_one)
    app = run_queue_in_main()
    owner = app.session_state["session_owner"]
    engine = services(True)
    limits = AccessLimits.for_public_host()
    done = engine.submit_download(JobSpec(url=finished_url, settings=DownloadSettings(), output_dir="unused"), owner, limits)
    wait_until(lambda: done.state == DONE)
    running = engine.submit_download(JobSpec(url=running_url, settings=DownloadSettings(), output_dir="unused"), owner, limits)
    wait_until(lambda: (running.payload.private_output_dir / "part.mp4.part").exists())
    folders = [done.payload.private_output_dir, running.payload.private_output_dir]
    assert any(path.suffix == ".mp4" for path in folders[0].iterdir())
    app.session_state["keep_login_method"] = "cookies"
    app.session_state["cookies_bytes"] = b"# Netscape HTTP Cookie File\n"
    app.run()

    button(app, "清除我的資料").click().run()
    assert "還沒存到電腦的檔案" in texts(app.markdown)
    assert engine.manager.list_jobs(owner), "nothing is removed before the visitor confirms"
    button(app, "確定清除").click().run()
    assert not app.exception, app.exception

    # The running download goes, with its folder, the moment it notices the cancel.
    wait_until(lambda: engine.manager.list_jobs(owner) == [] and not any(folder.exists() for folder in folders))
    assert "cookies_bytes" not in app.session_state
    assert app.session_state["session_owner"] != owner
    assert "已清除你的" in texts(app.success)
    expired = scripts(app, "max-age=0")
    assert len(expired) == 1
    assert 'document.cookie = "videocap_visitor=; max-age=0; path=/"' in expired[0]
    assert 'document.cookie = "videocap_notify=; max-age=0; path=/"' in expired[0]

    # The new ID is written on a later run, after the old one was dropped.
    app.run()
    assert not scripts(app, "max-age=0")
    assert scripts(app, f"videocap_visitor={app.session_state['session_owner']};")


def test_the_visitor_cookie_cannot_bring_the_old_jobs_back(monkeypatch: pytest.MonkeyPatch) -> None:
    public_mode(monkeypatch)
    old_owner = "0123456789abcdef0123456789abcdef"
    # The browser keeps sending the old cookie until the page's script has dropped it.
    monkeypatch.setattr(ContextProxy, "cookies", property(lambda self: {"videocap_visitor": old_owner}))
    app = run_queue_in_main()
    assert app.session_state["session_owner"] == old_owner
    add_job(DONE, owner=old_owner, public=True, label="Old job", finished_at=time.time())
    app.run()
    button(app, "清除我的資料").click().run()
    button(app, "確定清除").click().run()
    app.run()
    assert app.session_state["session_owner"] != old_owner
    assert "Old job" not in texts(app.markdown)
    assert "佇列是空的" in texts(app.info)


def test_this_machines_owner_cannot_clear_the_queue_that_way(tmp_path: Path) -> None:
    from videocap.ui.common import forget_visitor_jobs

    job = finished_download(tmp_path)
    ctx: Any = SimpleNamespace(trusted=True, owner="local", services=services())
    assert forget_visitor_jobs(ctx) is False
    assert services().manager.get(job.id) is job


def test_the_password_is_not_asked_again_after_clearing(monkeypatch: pytest.MonkeyPatch) -> None:
    public_mode(monkeypatch)
    monkeypatch.setenv("VIDEOCAP_PASSWORD", "open-sesame")
    app = AppTest.from_file(str(ROOT / "main.py"), default_timeout=30)
    app.run()
    app.text_input[0].set_value("open-sesame")
    app.button[0].click().run()
    from streamlit.util import calc_hash

    app._page_hash = calc_hash("queue")
    app.run()
    button(app, "清除我的資料").click().run()
    button(app, "確定清除").click().run()
    assert not app.exception, app.exception
    assert [item for item in app.button if item.label == "清除我的資料"]


# 3 - the queue page itself


def test_an_empty_queue_says_so_and_offers_nothing_to_press() -> None:
    app = run_page()
    assert "佇列是空的" in texts(app.info)
    assert "進行中" not in texts(app.markdown)
    assert all(button(app, label).disabled for label in ("全部暫停", "全部繼續", "清除已結束"))


def test_jobs_are_grouped_by_what_they_are_doing(tmp_path: Path) -> None:
    add_job(RUNNING, label="Running one")
    add_job(QUEUED, label="Waiting one", not_before=time.time() + 3600)
    add_job(QUEUED, label="Waiting two", not_before=time.time() + 3600)
    add_job(PAUSED, label="Paused one")
    finished_download(tmp_path, title="Finished one")
    app = run_page()
    shown = texts(app.markdown)
    order = [shown.index(text) for text in ("進行中", "Running one", "Waiting one", "Waiting two", "已暫停（1）", "Paused one", "已結束（1）", "Finished one")]
    assert order == sorted(order)
    assert "等待中 2 項" in texts(app.caption)
    assert "佇列是空的" not in texts(app.info)


def test_global_actions_pause_resume_and_clear(tmp_path: Path) -> None:
    waiting = add_job(QUEUED, label="Later", not_before=time.time() + 3600)
    done = finished_download(tmp_path)
    app = run_page()
    assert button(app, "全部繼續").disabled

    button(app, "全部暫停").click().run()
    assert waiting.state == PAUSED
    assert not button(app, "全部繼續").disabled
    button(app, "全部繼續").click().run()
    assert waiting.state == QUEUED

    button(app, "清除已結束").click().run()
    assert services().manager.get(done.id) is None
    assert button(app, "清除已結束").disabled
    assert services().manager.get(waiting.id) is waiting


# 4 - warnings and notes on finished jobs


def test_a_tool_warning_is_shown_and_toasted(tmp_path: Path) -> None:
    output = tmp_path / "clip_subtitled.mp4"
    output.write_bytes(b"video")
    app = run_page()
    job = add_job(
        DONE,
        kind="tool",
        label="燒入字幕：clip.mp4",
        outcome=ToolResult(output_path=output, warning="字幕可能顯示成 *方框*，請換字型"),
        finished_at=time.time(),
    )
    app.run()
    assert "完成，但請留意" in texts(app.toast)
    app.session_state[f"queue_result_{job.id}"] = True
    app.run()
    assert "完成：" in texts(app.success)
    # Escaped, so the asterisks stay as written instead of turning into bold text.
    assert "字幕可能顯示成 \\*方框\\*，請換字型" in texts(app.warning)


def test_notes_are_shown_as_information_and_are_not_failures(tmp_path: Path) -> None:
    from videocap.ui.common import job_status_line

    job = finished_download(tmp_path, notes=[JS_NOTE], errors=[JS_NOTE])
    app = run_page()
    assert JS_NOTE in texts(app.info)
    assert JS_NOTE not in texts(app.error)
    assert job_status_line(job) == "1 個項目完成"


def test_outcomes_from_before_notes_still_show(tmp_path: Path) -> None:
    from videocap.ui.common import job_status_line

    job = finished_download(tmp_path, errors=["第 2 部影片已被移除"])
    job.outcome.pop("notes", None)
    run_page()
    assert job_status_line(job) == "1 個項目完成，1 項失敗"


def test_a_single_download_shows_its_notes_on_the_download_page(tmp_path: Path) -> None:
    job = finished_download(tmp_path, notes=[JS_NOTE])
    job.batch_id = "batch"
    app = AppTest.from_function(page_script, kwargs={"page_name": "download"}, default_timeout=30)
    app.session_state["last_batch_id"] = "batch"
    app.run()
    assert not app.exception, app.exception
    assert JS_NOTE in texts(app.info)
    assert "下載完成" in texts(app.success)


def test_a_cancelled_job_is_not_announced_as_finished() -> None:
    app = run_page()
    app.session_state["keep_browser_notify"] = True
    app.run()
    add_job(CANCELED, label="Stopped", finished_at=time.time())
    app.run()
    assert not app.toast
    assert not scripts(app, "new Notification")


# A job waiting for a live stream to start


def waiting_for_stream() -> Job:
    from videocap.config import LIVE_WAIT_STATUS_PREFIX

    return add_job(RUNNING, label="Upcoming stream", status_text=f"{LIVE_WAIT_STATUS_PREFIX}（預定 20:00）")


def test_a_job_waiting_for_its_stream_does_not_look_stuck() -> None:
    waiting_for_stream()
    app = run_page()
    shown = texts(app.markdown)
    assert "等待開播（預定 20:00）" in shown
    # Not a download at 0 % that never moves.
    assert "進行中 · 等待開播" not in shown
    assert not app.get("progress")
    assert "開播後會自動開始" in texts(app.caption)
    assert button(app, "取消")


def test_a_job_waiting_for_its_stream_is_not_polled_every_second() -> None:
    from videocap.ui.common import polling_interval

    waiting = waiting_for_stream()
    assert polling_interval([waiting]) == "5s"
    assert polling_interval([waiting, add_job(RUNNING, label="Downloading", status_text="下載中 10%")]) == "1s"


def test_clearing_my_data_leaves_other_visitors_jobs_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    public_mode(monkeypatch)
    app = run_queue_in_main()
    mine = add_job(DONE, owner=app.session_state["session_owner"], public=True, label="Mine", finished_at=time.time())
    others = [
        add_job(DONE, owner="f" * 32, public=True, label="Theirs", finished_at=time.time()),
        add_job(PAUSED, owner="e" * 32, public=True, label="Theirs paused", cookie_bytes=b"cookies"),
    ]
    app.run()
    button(app, "清除我的資料").click().run()
    button(app, "確定清除").click().run()
    assert not app.exception, app.exception
    manager = services(True).manager
    assert manager.get(mine.id) is None
    assert [manager.get(job.id) for job in others] == others
    assert others[1].state == PAUSED and others[1].cookie_bytes == b"cookies"


def test_a_job_is_announced_on_one_page_only() -> None:
    from streamlit.util import calc_hash

    app = run_queue_in_main()
    app.session_state["keep_browser_notify"] = True
    app.run()
    add_job(DONE, label="Once", finished_at=time.time())
    app.run()
    assert len(app.toast) == 1 and scripts(app, "new Notification")
    app._page_hash = calc_hash("download")
    app.run()
    assert not app.exception, app.exception
    assert not app.toast and not scripts(app, "new Notification")


def test_the_clearing_dialog_stays_open_while_a_job_finishes(monkeypatch: pytest.MonkeyPatch) -> None:
    public_mode(monkeypatch)
    app = run_queue_in_main()
    button(app, "清除我的資料").click().run()
    add_job(DONE, owner=app.session_state["session_owner"], public=True, label="Finished meanwhile", finished_at=time.time())
    app.run()
    assert "還沒存到電腦的檔案" in texts(app.markdown)
    assert [item for item in app.button if item.label == "確定清除"]


# 排程下載 on the download page


def schedule_popover(app: AppTest) -> Any:
    return next(node for node in app.main if node.type == "popover" and node.proto.popover.label == "排程下載")


def run_with_popover_open(app: AppTest, popover_id: str) -> AppTest:
    """Runs the app as the browser would with the popover open: it keeps sending that until someone closes it by hand."""
    states = app._tree.get_widget_states()
    state = states.widgets.add()
    state.id = popover_id
    state.bool_value = True
    app._run(states)
    assert not app.exception, app.exception
    return app


def test_the_schedule_button_comes_before_the_date_whose_calendar_would_cover_it() -> None:
    # An opened popover focuses its first field, and a focused date field opens its calendar over what is below it.
    shown = [(node.type, getattr(node, "label", "")) for node in schedule_popover(run_page("download"))]
    assert shown.index(("button", "排入佇列")) < shown.index(("date_input", "日期"))


def test_queueing_a_schedule_closes_its_popover() -> None:
    app = run_page("download")
    app.text_area(key="keep_url_input").set_value("https://www.youtube.com/watch?v=jNQXAC9IVRw").run()
    opened = schedule_popover(app).proto.popover.id
    assert opened and schedule_popover(run_with_popover_open(app, opened)).proto.popover.open

    button(app, "排入佇列").click()
    run_with_popover_open(app, opened)
    assert [job.not_before is not None for job in services().manager.list_jobs()] == [True]
    assert not schedule_popover(app).proto.popover.open
    # The browser still holds the old popover as open, and sends that with the next click anywhere.
    assert not schedule_popover(run_with_popover_open(app, opened)).proto.popover.open


def test_clearing_does_not_wait_for_a_slow_download_to_stop(
    monkeypatch: pytest.MonkeyPatch, offline_youtube: OfflineYouTube
) -> None:
    public_mode(monkeypatch)
    url = offline_youtube.add_video("slowStop001", "Slow to stop")

    def slow_to_stop(url: str, options: Any, progress_ui: Any, status_ui: Any, should_stop: Any, on_log: Any) -> Any:
        options.output_dir.mkdir(parents=True, exist_ok=True)
        (options.output_dir / "part.mp4.part").write_bytes(b"partial")
        while not should_stop():
            time.sleep(0.02)
        # Like ffmpeg finishing a merge before it looks at the stop.
        time.sleep(3)
        outcome = empty_outcome()
        outcome["stopped"] = True
        return outcome

    monkeypatch.setattr(services_module, "run_download", slow_to_stop)
    app = run_queue_in_main()
    owner = app.session_state["session_owner"]
    engine = services(True)
    job = engine.submit_download(JobSpec(url=url, settings=DownloadSettings(), output_dir="unused"), owner, AccessLimits.for_public_host())
    wait_until(lambda: (job.payload.private_output_dir / "part.mp4.part").exists())
    folder = job.payload.private_output_dir
    app.run()
    button(app, "清除我的資料").click().run()
    started = time.monotonic()
    button(app, "確定清除").click().run()
    assert time.monotonic() - started < 2.5
    assert not app.exception, app.exception
    assert "已清除你的資料" in texts(app.success)
    # Gone the moment it stops, without anyone left on the page to remove it.
    wait_until(lambda: engine.manager.get(job.id) is None and not folder.exists())


# 下載時段


def closed_window() -> Any:
    from videocap.storage import DownloadWindow

    opens = datetime.now() + timedelta(hours=2)
    return DownloadWindow(enabled=True, start=opens.strftime("%H:%M"), end=(opens + timedelta(hours=1)).strftime("%H:%M"))


def test_the_owner_sets_download_hours_on_the_queue_page() -> None:
    from videocap.storage import DownloadWindow

    app = run_page()
    assert [item for item in app.expander if item.label == "下載時段"]
    assert app.toggle(key="download_window_enabled").value is False
    # Nothing to choose until the hours are turned on.
    assert app.time_input(key="download_window_start").disabled and app.time_input(key="download_window_end").disabled
    shown = texts(app.caption)
    assert "這台電腦" in shown and "不會中斷" in shown

    app.toggle(key="download_window_enabled").set_value(True).run()
    app.time_input(key="download_window_start").set_value(dt_time(22, 0)).run()
    app.time_input(key="download_window_end").set_value(dt_time(6, 30)).run()
    assert not app.exception, app.exception
    saved = DownloadWindow(enabled=True, start="22:00", end="06:30")
    assert services().download_window == saved
    assert services().storage.load_download_window() == saved
    assert not app.time_input(key="download_window_start").disabled


def test_visitors_are_not_offered_download_hours(monkeypatch: pytest.MonkeyPatch) -> None:
    public_mode(monkeypatch)
    app = run_page()
    assert not [item for item in app.expander if item.label == "下載時段"]


def test_a_download_held_by_the_window_says_until_when() -> None:
    window = closed_window()
    services().save_download_window(window, AccessLimits())
    add_job(QUEUED, label="Night download")
    add_job(RUNNING, kind="tool", label="Conversion")
    app = run_page()
    assert f"等待下載時段 {window.label}" in texts(app.markdown)
    notice = texts(app.info)
    assert f"不在下載時段（{window.label}）" in notice and f"{window.start} 自動開始" in notice


def test_no_window_notice_while_nothing_waits_for_it() -> None:
    services().save_download_window(closed_window(), AccessLimits())
    add_job(QUEUED, label="Later", not_before=time.time() + 3600)
    app = run_page()
    assert "不在下載時段" not in texts(app.info)


# 遇到 429 時自動暫停


def rate_limited_queue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Job, Job]:
    """The queue as a download that met HTTP 429 leaves it: one job the pause stopped, and one waiting through it."""
    from videocap.errors import humanize_ytdlp_error
    from videocap.services import DownloadPayload

    monkeypatch.setattr(services_module, "run_download", lambda *args: empty_outcome())
    engine = services()
    spec = JobSpec(url="https://www.youtube.com/watch?v=jNQXAC9IVRw", settings=DownloadSettings(), output_dir=str(tmp_path / "out"))
    paused = add_job(PAUSED, label="Stopped by the pause", payload=DownloadPayload(spec, AccessLimits()))
    error = humanize_ytdlp_error("ERROR: [youtube] jNQXAC9IVRw: HTTP Error 429: Too Many Requests")
    assert engine.pause_for_rate_limit(add_job(FAILED, label="Limited", error=error, finished_at=time.time()))
    engine.rate_limit_pause.paused_ids.add(paused.id)
    waiting = engine.submit_download(spec, "local", AccessLimits())
    return paused, waiting


def test_a_rate_limit_pause_says_until_when_and_can_end_now(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paused, waiting = rate_limited_queue(tmp_path, monkeypatch)
    until = services().rate_limit_until()
    app = run_page()
    clock = time.strftime("%H:%M", time.localtime(until))
    assert f"YouTube 暫時限制了請求（HTTP 429／要求驗證），佇列已暫停，{clock} 後自動繼續。" in texts(app.warning)
    shown = texts(app.markdown)
    assert shown.count(services_module.RATE_LIMIT_HOLD_TEXT) == 2
    assert waiting.state == QUEUED

    button(app, "立即繼續").click().run()
    assert not app.exception, app.exception
    assert services().rate_limit_until() is None
    wait_until(lambda: waiting.state == DONE and paused.state == DONE)
    assert "佇列已暫停" not in texts(app.warning)


def test_resume_all_also_ends_the_pause(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paused, waiting = rate_limited_queue(tmp_path, monkeypatch)
    by_hand = add_job(PAUSED, label="Paused by hand", payload=paused.payload)
    app = run_page()
    button(app, "全部繼續").click().run()
    assert not app.exception, app.exception
    assert services().rate_limit_until() is None
    wait_until(lambda: waiting.state == DONE and paused.state == DONE and by_hand.state == DONE)


def test_the_download_page_tells_of_the_pause_too(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rate_limited_queue(tmp_path, monkeypatch)
    app = run_page("download")
    assert "佇列已暫停" in texts(app.warning)
    button(app, "立即繼續").click().run()
    assert services().rate_limit_until() is None


# 網路中斷後自動重試


def test_a_download_waiting_to_retry_says_when_and_can_retry_now(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from videocap.services import DownloadPayload

    monkeypatch.setattr(services_module, "run_download", lambda *args: empty_outcome())
    spec = JobSpec(url="https://www.youtube.com/watch?v=jNQXAC9IVRw", settings=DownloadSettings(), output_dir=str(tmp_path / "out"))
    job = add_job(QUEUED, label="Dropped", payload=DownloadPayload(spec, AccessLimits()), not_before=time.time() + 115, auto_retries=1)
    scheduled = add_job(QUEUED, label="Tonight", not_before=time.time() + 3600)
    app = run_page()
    shown = texts(app.markdown)
    assert "網路錯誤，2 分鐘後自動重試（第 1 次）" in shown
    # A download someone scheduled is no retry.
    assert "排程於" in shown and scheduled.auto_retries == 0
    assert len([item for item in app.button if item.label == "重試"]) == 1

    button(app, "重試").click().run()
    assert not app.exception, app.exception
    wait_until(lambda: job.state == DONE)
    assert job.auto_retries == 0

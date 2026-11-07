"""The download page, the queue, search, the settings sidebar, and the buttons that hand files to the browser."""

from __future__ import annotations

import codecs
import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import streamlit as st
from streamlit.runtime.context import ContextProxy
from streamlit.testing.v1 import AppTest

import videocap.services as services_module
from videocap.downloader import empty_outcome
from videocap.jobs import ACTIVE_STATES, DONE, FAILED, PAUSED, QUEUED, RUNNING, Job, new_job_id


ROOT = Path(__file__).resolve().parents[1]
URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
OTHER_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
# Far from any server's own zone, so a test cannot pass by accident.
VISITOR_ZONE = "Pacific/Kiritimati"


def item(path: Path, title: str = "Fake Title", extra: list[Path] | None = None) -> dict[str, Any]:
    return {
        "title": title,
        "path": str(path),
        "subtitle_paths": [],
        "extra_paths": [str(extra_path) for extra_path in extra or []],
        "video_id": "jNQXAC9IVRw",
        "channel": "Channel",
        "webpage_url": URL,
        "duration": 19.0,
        "upload_date": "20050424",
        "thumbnail": "",
        "quality": "mp4",
    }


def fake_download(url: str, options: Any, progress_ui: Any, status_ui: Any, should_stop: Any, on_log: Any) -> Any:
    options.output_dir.mkdir(parents=True, exist_ok=True)
    path = options.output_dir / "video.mp4"
    path.write_bytes(b"video-bytes")
    subtitle = options.output_dir / "video.zh-TW.srt"
    subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n", encoding="utf-8")
    outcome = empty_outcome()
    outcome["items"] = [item(path, extra=[subtitle])]
    return outcome


def stoppable_download(url: str, options: Any, progress_ui: Any, status_ui: Any, should_stop: Any, on_log: Any) -> Any:
    """Runs until someone presses pause or cancel, the way a long download would."""
    if url == OTHER_URL:
        return fake_download(url, options, progress_ui, status_ui, should_stop, on_log)
    deadline = time.time() + 10
    while not should_stop() and time.time() < deadline:
        time.sleep(0.02)
    outcome = empty_outcome()
    outcome["stopped"] = True
    return outcome


@pytest.fixture(autouse=True)
def isolated_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VIDEOCAP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("VIDEOCAP_OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setenv("VIDEOCAP_TRUST_REMOTE", "1")
    monkeypatch.setenv("VIDEOCAP_MODE", "local")
    monkeypatch.delenv("VIDEOCAP_PASSWORD", raising=False)
    monkeypatch.delenv("VIDEOCAP_ALLOWED_HOSTS", raising=False)
    monkeypatch.setattr(services_module, "run_download", fake_download)
    monkeypatch.setattr(services_module, "CLOUD_OUTPUT_ROOT", tmp_path / "public")
    st.cache_resource.clear()
    from videocap.ui import common

    # Prepared files are counted for the whole server, across tests too.
    common._download_memory.clear()
    yield
    # Jobs left waiting would otherwise keep running into the next test.
    for services in list(SERVICES):
        for job in services.manager.list_jobs():
            services.manager.cancel(job.id)
    SERVICES.clear()
    st.cache_resource.clear()


SERVICES: list[Any] = []


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


def new_page(page_name: str) -> AppTest:
    return AppTest.from_function(page_script, kwargs={"page_name": page_name}, default_timeout=30)


def run_page(page_name: str) -> AppTest:
    app = new_page(page_name)
    app.run()
    assert not app.exception, app.exception
    return app


def services(public: bool = False) -> Any:
    from videocap.ui.context import get_services

    found = get_services(public)
    SERVICES.append(found)
    return found


def shown_label(item: Any) -> str:
    # Labels are Markdown, so file names arrive escaped.
    return str(item.label).replace("\\", "")


def button(app: AppTest, label: str) -> Any:
    return next(item for item in app.button if shown_label(item) == label)


def buttons_starting(app: AppTest, prefix: str) -> list[Any]:
    return [item for item in app.button if shown_label(item).startswith(prefix)]


def texts(elements: Any) -> str:
    return " ".join(str(element.value) for element in elements)


def everything(app: AppTest) -> str:
    return " ".join(
        texts(elements) for elements in (app.markdown, app.caption, app.success, app.info, app.warning, app.error)
    )


def wait_until(condition: Any, seconds: float = 10) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline and not condition():
        time.sleep(0.02)
    assert condition()


def download(app: AppTest, *urls: str, public: bool = False) -> AppTest:
    app.text_area(key="keep_url_input").set_value("\n".join(urls)).run()
    button(app, "開始下載").click().run()
    assert not app.exception, app.exception
    manager = services(public).manager
    wait_until(lambda: not [job for job in manager.list_jobs() if job.state in ACTIVE_STATES])
    app.run()
    assert not app.exception, app.exception
    return app


def download_buttons(app: AppTest) -> list[Any]:
    return list(app.get("download_button"))


# ui:5 - a paused or cancelled download is shown as such


def test_pausing_a_download_shows_it_paused_with_a_resume_button(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(services_module, "run_download", stoppable_download)
    app = run_page("download")
    app.text_area(key="keep_url_input").set_value(URL).run()
    button(app, "開始下載").click().run()
    manager = services().manager
    job = manager.list_jobs()[0]
    wait_until(lambda: job.state == RUNNING)
    manager.pause(job.id)
    wait_until(lambda: job.state == PAUSED)
    app.run()
    assert not app.exception, app.exception

    shown = everything(app)
    assert "沒有下載到任何檔案" not in shown
    assert "已暫停" in shown
    assert [item for item in app.button if item.label == "繼續"]


def test_a_batch_with_a_link_still_running_shows_which_link_finished(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(services_module, "run_download", stoppable_download)
    app = run_page("download")
    app.text_area(key="keep_url_input").set_value(f"{URL}\n{OTHER_URL}").run()
    button(app, "開始下載").click().run()
    manager = services().manager
    finished = next(job for job in manager.list_jobs() if OTHER_URL in job.label or job.payload.spec.url == OTHER_URL)
    wait_until(lambda: finished.state == DONE)
    app.run()
    assert not app.exception, app.exception
    # The finished link gets its own card, instead of looking like the whole batch is done.
    assert "下載完成：video.mp4" not in texts(app.success)
    assert [expander for expander in app.expander if expander.label == "結果"]


# ui:6 - a clip or item range is for one download, not for every later one


def test_clip_and_item_ranges_are_not_carried_into_the_next_visit() -> None:
    app = run_page("download")
    app.session_state["opt_time_ranges"] = "1:30-2:00"
    app.session_state["opt_playlist_items"] = "1-3"
    app.session_state["opt_remove_ranges"] = "0:00-0:30"
    download(app, URL)

    next_visit = run_page("download")
    assert next_visit.session_state["opt_time_ranges"] == ""
    assert next_visit.session_state["opt_playlist_items"] == ""
    assert next_visit.session_state["opt_remove_ranges"] == ""


def test_active_clip_and_item_ranges_are_shown_and_can_be_cleared() -> None:
    app = run_page("download")
    app.session_state["opt_time_ranges"] = "1:30-2:00"
    app.session_state["opt_playlist_items"] = "1-3"
    app.session_state["opt_remove_ranges"] = "0:00-0:30"
    app.run()
    # Markdown escapes such as "1\:30" are not what a reader sees.
    shown = everything(app).replace("\\", "")
    assert "1:30-2:00" in shown and "1-3" in shown
    assert "移除片段 0:00-0:30" in shown

    button(app, "清除片段設定").click().run()
    assert app.session_state["opt_time_ranges"] == ""
    assert app.session_state["opt_playlist_items"] == ""
    assert app.session_state["opt_remove_ranges"] == ""
    assert "1:30-2:00" not in everything(app).replace("\\", "")


def test_cutting_out_ranges_alone_is_shown_and_can_be_cleared() -> None:
    app = run_with_settings("download", remove_ranges="0:00-0:30")
    assert "移除片段 0:00-0:30" in everything(app).replace("\\", "")
    button(app, "清除片段設定").click().run()
    assert app.session_state["opt_remove_ranges"] == ""


# perf-scale:0, 1, 2 and ui:10 - files reach the browser without filling the server's memory


def test_a_file_is_read_only_when_asked_for_and_stays_tied_to_the_page() -> None:
    app = download(run_page("download"), URL)
    # Nothing is read while the page is merely drawn, and no deferred reader is left behind.
    assert not download_buttons(app)
    button(app, "下載 video.mp4（11 B）").click().run()
    assert not app.exception, app.exception

    saved = download_buttons(app)
    assert len(saved) == 1
    # A URL fixed while the page is drawn keeps the file referenced by this page, so no other visitor's page run
    # can delete it before the browser asks for it.
    assert saved[0].proto.url and not saved[0].proto.deferred_file_id
    assert "video.mp4" in saved[0].proto.label


def test_saving_a_prepared_file_releases_its_memory() -> None:
    from videocap.ui import common

    app = download(run_page("download"), URL)
    button(app, "下載 video.mp4（11 B）").click().run()
    assert common.held_download_bytes() == 11
    # What the save button's callback does; AppTest cannot click a download button.
    ticket = app.session_state[common.PREPARED_DOWNLOAD_KEY]["ticket"]
    common.release_download_memory(ticket)
    assert common.held_download_bytes() == 0


def test_the_memory_budget_refuses_new_files_while_others_hold_it(monkeypatch: pytest.MonkeyPatch) -> None:
    from videocap.ui import common

    monkeypatch.setattr(common, "DOWNLOAD_MEMORY_BUDGET", 100)
    # One file within the size limit always fits, or a large file could never be saved.
    first = common.reserve_download_memory(150)
    assert first
    assert common.reserve_download_memory(10) is None
    common.release_download_memory(first)
    second = common.reserve_download_memory(60)
    assert second
    assert common.reserve_download_memory(60) is None
    third = common.reserve_download_memory(40)
    assert third
    common.release_download_memory(second)
    common.release_download_memory(third)


def test_a_busy_server_asks_the_visitor_to_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    from videocap.ui import common

    app = download(run_page("download"), URL)
    monkeypatch.setattr(common, "DOWNLOAD_MEMORY_BUDGET", 5)
    other_visitor = common.reserve_download_memory(5)
    try:
        button(app, "下載 video.mp4（11 B）").click().run()
        assert not download_buttons(app)
        assert "請過一兩分鐘再按一次" in texts(app.warning)
    finally:
        common.release_download_memory(other_visitor)


def test_a_prepared_file_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    from videocap.ui import common

    app = download(run_page("download"), URL)
    button(app, "下載 video.mp4（11 B）").click().run()
    prepared = app.session_state[common.PREPARED_DOWNLOAD_KEY]
    prepared["expires"] = time.time() - 1
    app.session_state[common.PREPARED_DOWNLOAD_KEY] = prepared
    app.run()
    assert not download_buttons(app)
    assert common.held_download_bytes() == 0


def test_public_visitors_see_no_server_path_when_a_file_is_gone(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    public_mode(monkeypatch)
    app = download(run_page("download"), URL, public=True)
    file_button = button(app, "下載 video.mp4（11 B）")
    job = services(True).manager.list_jobs()[0]
    for path in Path(job.outcome["items"][0]["path"]).parent.iterdir():
        path.unlink()

    file_button.click().run()
    assert not app.exception, app.exception
    assert "請重新下載" in texts(app.warning)
    assert str(tmp_path) not in everything(app)
    assert not download_buttons(app)


def test_public_visitors_cannot_fetch_files_over_the_public_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    from videocap.ui import common

    public_mode(monkeypatch)
    monkeypatch.setattr(common, "CLOUD_MAX_BYTES", 5)
    app = download(run_page("download"), URL, public=True)
    assert not buttons_starting(app, "下載 video.mp4")
    assert "超過公開主機的下載上限" in texts(app.caption)


def test_bundles_are_offered_only_while_small(monkeypatch: pytest.MonkeyPatch) -> None:
    from videocap.ui import common

    app = download(run_page("download"), URL)
    assert buttons_starting(app, "全部打包下載.zip")

    monkeypatch.setattr(common, "BUNDLE_MAX_BYTES", 20)
    app.run()
    assert not buttons_starting(app, "全部打包下載.zip")
    assert buttons_starting(app, "下載 video.mp4")


def test_bundles_store_media_and_compress_text(tmp_path: Path) -> None:
    from videocap.ui.common import build_zip_bytes

    video = tmp_path / "a.mp4"
    video.write_bytes(b"\x00" * 1000)
    subtitle = tmp_path / "a.srt"
    subtitle.write_text("line\n" * 200, encoding="utf-8")
    bundle_path = tmp_path / "bundle.zip"
    bundle_path.write_bytes(build_zip_bytes([video, subtitle]))
    with zipfile.ZipFile(bundle_path) as bundle:
        kinds = {info.filename: info.compress_type for info in bundle.infolist()}
    assert kinds == {"a.mp4": zipfile.ZIP_STORED, "a.srt": zipfile.ZIP_DEFLATED}


@pytest.mark.parametrize("name", ["a.mp4", "a.mkv", "a.webm", "a.ts", "a.flv", "a.mov", "a.m4a", "a.mp3", "a.opus", "a.flac", "a.wav"])
def test_media_is_always_sent_as_audio_or_video(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from videocap.ui import common

    # A Linux host without /etc/mime.types knows few of these, and Streamlit gzips every other type on its event loop.
    monkeypatch.setattr(common.mimetypes, "guess_type", lambda *args, **kwargs: (None, None))
    assert common.download_mime(Path(name)).startswith(("audio/", "video/"))


# ui:11, platform:4 - schedules follow the visitor's clock


def visitor_in(monkeypatch: pytest.MonkeyPatch, zone: str) -> None:
    monkeypatch.setattr(ContextProxy, "timezone", property(lambda self: zone))


def test_scheduling_uses_the_visitors_time_zone(monkeypatch: pytest.MonkeyPatch) -> None:
    from videocap.ui.common import job_status_line

    visitor_in(monkeypatch, VISITOR_ZONE)
    app = run_page("download")
    app.text_area(key="keep_url_input").set_value(URL).run()
    tomorrow = (datetime.now(ZoneInfo(VISITOR_ZONE)) + timedelta(days=1)).date()
    app.date_input(key="schedule_day").set_value(tomorrow)
    app.time_input(key="schedule_time").set_value(datetime(2000, 1, 1, 21, 30).time())
    button(app, "排入佇列").click().run()
    assert not app.exception, app.exception

    job = services().manager.list_jobs()[0]
    expected = datetime(tomorrow.year, tomorrow.month, tomorrow.day, 21, 30, tzinfo=ZoneInfo(VISITOR_ZONE))
    assert job.not_before == expected.timestamp()
    assert "21:30" in texts(app.markdown)
    assert job_status_line(job).startswith("排程於")


def test_the_suggested_time_is_an_hour_from_now_on_the_visitors_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    visitor_in(monkeypatch, VISITOR_ZONE)
    app = run_page("download")
    suggested = datetime.combine(app.date_input(key="schedule_day").value, app.time_input(key="schedule_time").value)
    expected = datetime.now(ZoneInfo(VISITOR_ZONE)).replace(tzinfo=None) + timedelta(hours=1)
    assert abs((suggested - expected).total_seconds()) < 120


def test_public_visitors_are_told_to_keep_the_page_open_while_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    public_mode(monkeypatch)
    app = run_page("download")
    assert "保持這個頁面開啟" in texts(app.caption)


# ui:14, perf-scale:7 - pages poll every second only while something runs


def record_polling(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    from videocap.ui import common

    seen: list[Any] = []
    original = common.st.fragment

    def fragment(function: Any = None, *, run_every: Any = None) -> Any:
        seen.append(run_every)
        return original(function, run_every=run_every)

    monkeypatch.setattr(common.st, "fragment", fragment)
    return seen


def add_job(state: str, owner: str = "local", public: bool = False, **fields: Any) -> Job:
    job = Job(
        id=new_job_id(), owner=owner, kind=fields.pop("kind", "download"), label="x", payload=fields.pop("payload", None), state=state, **fields
    )
    manager = services(public).manager
    with manager._lock:
        manager._jobs[job.id] = job
    return job


def test_a_job_scheduled_for_later_polls_slowly(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = record_polling(monkeypatch)
    add_job(QUEUED, not_before=time.time() + 3600)
    run_page("queue")
    assert seen[-1] == "30s"


def test_a_running_job_still_polls_every_second(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = record_polling(monkeypatch)
    add_job(RUNNING)
    run_page("queue")
    assert seen[-1] == "1s"


def test_an_idle_page_does_not_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = record_polling(monkeypatch)
    run_page("queue")
    assert seen[-1] is None


# ui:15 - a failed search is not "no results"


def test_a_failed_search_does_not_say_nothing_was_found(monkeypatch: pytest.MonkeyPatch) -> None:
    import videocap.ui.page_search as page_search

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("ERROR: Unable to download API page: <urlopen error [Errno 11001] getaddrinfo failed>")

    monkeypatch.setattr(page_search, "search_youtube", broken)
    app = run_page("search")
    app.text_input(key="keep_search_query").set_value("cats")
    app.button[0].click().run()
    assert not app.exception, app.exception
    assert app.error
    assert "沒有找到" not in texts(app.info)


def test_an_empty_search_asks_for_a_keyword() -> None:
    app = run_page("search")
    app.button[0].click().run()
    assert "請輸入關鍵字" in texts(app.warning)


# ui:19 - tool jobs cannot resume, so they offer no pause


def test_tool_jobs_offer_cancel_but_no_pause() -> None:

    tool = add_job(RUNNING, kind="tool")
    app = run_page("queue")
    labels = [item.label for item in app.button]
    assert "暫停" not in labels
    assert "取消" in labels

    button(app, "全部暫停").click().run()
    assert tool.stop_mode == ""


# ui:20, perf-scale:8 - stopping an inspection stops sending links to YouTube


def test_stopping_an_inspection_drops_the_links_not_yet_started(monkeypatch: pytest.MonkeyPatch) -> None:
    import videocap.ui.page_download as page_download

    inspected: list[str] = []

    def slow_inspect(url: str, options: Any) -> Any:
        time.sleep(0.4)
        inspected.append(url)
        return {"url": url, "title": "t"}

    class StopNow(Exception):
        pass

    class Bar:
        def progress(self, *args: Any, **kwargs: Any) -> None:
            # What Streamlit does when the visitor presses Stop or leaves the page.
            raise StopNow()

        def empty(self) -> None:
            pass

    monkeypatch.setattr(page_download, "inspect_link", slow_inspect)
    monkeypatch.setattr(page_download.st, "progress", lambda *args, **kwargs: Bar())
    links = [f"https://www.youtube.com/watch?v={index:011d}" for index in range(16)]
    app = run_page("download")
    app.text_area(key="keep_url_input").set_value("\n".join(links)).run()
    started = time.time()
    button(app, "解析連結（不下載）").click().run()
    elapsed = time.time() - started
    time.sleep(1.0)
    assert elapsed < 1.5
    assert len(inspected) <= 8


# ui:21 - the uploader's own remove button logs out


def test_removing_the_uploaded_cookies_file_stops_using_it(monkeypatch: pytest.MonkeyPatch) -> None:
    import videocap.ui.sidebar as sidebar

    state: dict[str, Any] = {"cookies_upload": SimpleNamespace(name="cookies.txt", getvalue=lambda: b"# Netscape")}
    monkeypatch.setattr(sidebar, "st", SimpleNamespace(session_state=state))
    forgotten: list[str] = []
    ctx = SimpleNamespace(owner="visitor", services=SimpleNamespace(manager=SimpleNamespace(forget_login=forgotten.append)))
    sidebar.store_uploaded_cookies(ctx)  # type: ignore[arg-type]
    assert state["cookies_bytes"] == b"# Netscape"
    assert forgotten == []

    state["cookies_upload"] = None
    sidebar.store_uploaded_cookies(ctx)  # type: ignore[arg-type]
    assert state["cookies_bytes"] is None
    # security:6 - jobs waiting in the queue drop their copy as well.
    assert forgotten == ["visitor"]


# ui:22 - only people who can record are told to record


def live_report() -> dict[str, Any]:
    return {"url": URL, "title": "Live", "video_id": "jNQXAC9IVRw", "is_live": True, "is_playlist": False}


def test_public_visitors_are_not_told_to_record_a_live_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    public_mode(monkeypatch)
    app = new_page("download")
    app.session_state["keep_url_input"] = URL
    app.session_state["last_reports"] = [live_report()]
    app.run()
    assert not app.exception, app.exception
    shown = texts(app.warning)
    assert "正在直播" in shown
    assert "錄製" not in shown


def test_this_machines_owner_is_told_how_to_record_a_live_stream() -> None:
    app = new_page("download")
    app.session_state["keep_url_input"] = URL
    app.session_state["last_reports"] = [live_report()]
    app.run()
    assert "錄製" in texts(app.warning)


# ui:23 - the list= sentence says what will happen


def test_list_links_follow_the_whole_list_setting() -> None:
    app = run_page("download")
    app.text_area(key="keep_url_input").set_value(f"{URL}&list=PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf").run()
    assert "只下載該部影片" in texts(app.caption)
    app.session_state["opt_expand_playlist"] = True
    app.run()
    assert "整份清單" in texts(app.caption)
    assert "只下載該部影片" not in texts(app.caption)

    app.text_area(key="keep_url_input").set_value("https://www.youtube.com/playlist?list=PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf").run()
    assert "list 參數" not in texts(app.caption)


# ui:24 - a retried job announces its new result


def test_a_retried_job_shows_its_toast_again() -> None:
    app = download(run_page("download"), URL)
    manager = services().manager
    job = manager.list_jobs()[0]
    app.run()

    manager.restart(job.id)
    wait_until(lambda: job.state == DONE)
    app.run()
    assert "完成：" in texts(app.toast)


# perf-scale:3 - finished jobs draw their files only when opened


def finished_job(tmp_path: Path, index: int) -> Job:
    path = tmp_path / f"video{index}.mp4"
    path.write_bytes(b"x")
    outcome = empty_outcome()
    outcome["items"] = [item(path, title=f"Video {index}")]
    return add_job(DONE, outcome=outcome, finished_at=time.time() + index)


def test_the_queue_draws_finished_files_only_when_opened(tmp_path: Path) -> None:
    jobs = [finished_job(tmp_path, index) for index in range(15)]
    app = run_page("queue")
    assert not buttons_starting(app, "下載 video")
    # Only the newest few are listed until more are asked for.
    assert len([expander for expander in app.expander if expander.label == "結果與紀錄"]) == 10
    assert buttons_starting(app, "顯示更早的")

    app.session_state[f"queue_result_{jobs[-1].id}"] = True
    app.run()
    assert [shown_label(item) for item in buttons_starting(app, "下載 video")] == ["下載 video14.mp4（1 B）"]

    buttons_starting(app, "顯示更早的")[0].click().run()
    assert len([expander for expander in app.expander if expander.label == "結果與紀錄"]) == 15


# perf-scale:13 - a huge pasted or imported list is bounded and parsed once


def uploaded(name: str, data: bytes) -> Any:
    from streamlit.runtime.uploaded_file_manager import UploadedFile, UploadedFileRec

    return UploadedFile(UploadedFileRec("id", name, "text/plain", data), None)


def import_file(monkeypatch: pytest.MonkeyPatch, data: bytes, trusted: bool = True) -> dict[str, Any]:
    import videocap.ui.page_download as page_download

    state: dict[str, Any] = {"link_file_upload": uploaded("links.txt", data), "keep_url_input": ""}
    monkeypatch.setattr(page_download, "st", SimpleNamespace(session_state=state))
    page_download.append_links_from_file(SimpleNamespace(trusted=trusted))
    return state


def test_a_public_visitor_imports_only_a_handful_of_links(monkeypatch: pytest.MonkeyPatch) -> None:
    from videocap.ui.page_download import CLOUD_MAX_IMPORT_LINKS

    links = "\n".join(f"https://youtu.be/{index:011d}" for index in range(CLOUD_MAX_IMPORT_LINKS + 30))
    state = import_file(monkeypatch, links.encode(), trusted=False)
    assert len(state["keep_url_input"].splitlines()) == CLOUD_MAX_IMPORT_LINKS
    assert f"只加入前 {CLOUD_MAX_IMPORT_LINKS} 個" in state["link_file_notice"]


def test_the_link_box_is_parsed_once_per_change(monkeypatch: pytest.MonkeyPatch) -> None:
    import videocap.ui.page_download as page_download

    calls: list[str] = []
    original = page_download.parse_youtube_urls

    def counting(text: str, **kwargs: Any) -> Any:
        calls.append(text)
        return original(text, **kwargs)

    monkeypatch.setattr(page_download, "parse_youtube_urls", counting)
    app = run_page("download")
    app.text_area(key="keep_url_input").set_value(URL).run()
    before = len(calls)
    app.run()
    app.run()
    assert len(calls) == before


# links-errors:9 - list files from Excel, Notepad and PowerShell


LINKS = "https://youtu.be/jNQXAC9IVRw\r\n@NASA\r\n"


@pytest.mark.parametrize(
    "data",
    [
        LINKS.encode("utf-8"),
        codecs.BOM_UTF8 + LINKS.encode("utf-8"),
        codecs.BOM_UTF16_LE + LINKS.encode("utf-16-le"),
        codecs.BOM_UTF16_BE + LINKS.encode("utf-16-be"),
    ],
    ids=["utf-8", "utf-8-bom", "utf-16-le", "utf-16-be"],
)
def test_imported_list_files_keep_every_link_whatever_the_encoding(monkeypatch: pytest.MonkeyPatch, data: bytes) -> None:
    state = import_file(monkeypatch, data)
    assert state["keep_url_input"].splitlines() == ["https://youtu.be/jNQXAC9IVRw", "https://www.youtube.com/@NASA"]
    assert "找到 2 個" in state["link_file_notice"]


def test_imported_links_that_are_not_youtube_are_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    state = import_file(monkeypatch, b"index,title,url\n1,a,https://youtu.be/jNQXAC9IVRw\n2,b,https://vimeo.com/1\n")
    assert "另有 1 個" in state["link_file_notice"]


# links-errors:13 - comment errors in plain words


def test_comment_errors_are_explained_in_plain_words(monkeypatch: pytest.MonkeyPatch) -> None:
    import videocap.ui.page_download as page_download

    def bot_check(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("ERROR: [youtube] jNQXAC9IVRw: Sign in to confirm you're not a bot. Use --cookies-from-browser")

    monkeypatch.setattr(page_download, "fetch_comments", bot_check)
    app = new_page("download")
    app.session_state["keep_url_input"] = URL
    app.session_state["last_reports"] = [{"url": URL, "title": "t", "video_id": "jNQXAC9IVRw", "is_playlist": False}]
    app.run()
    next(item for item in app.button if item.label.startswith("取得留言")).click().run()
    assert not app.exception, app.exception
    assert "驗證身分" in texts(app.error)
    assert not texts(app.error).startswith("ERROR")


# security:9 - public visitors share a few slots for requests to YouTube


def test_public_requests_wait_for_a_free_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    import videocap.ui.page_search as page_search
    from videocap.ui import common

    public_mode(monkeypatch)
    monkeypatch.setattr(common, "PUBLIC_REQUEST_WAIT_SECONDS", 0.1)
    searched: list[str] = []
    monkeypatch.setattr(page_search, "search_youtube", lambda query, *args, **kwargs: searched.append(query) or [])
    held = [common.PUBLIC_REQUEST_SLOTS.acquire(blocking=False) for _ in range(common.PUBLIC_REQUEST_LIMIT)]
    try:
        app = run_page("search")
        app.text_input(key="keep_search_query").set_value("cats")
        app.button[0].click().run()
        assert "使用的人較多" in texts(app.warning)
        assert searched == []
    finally:
        for got in held:
            if got:
                common.PUBLIC_REQUEST_SLOTS.release()


def test_public_visitors_wait_a_moment_between_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    import videocap.ui.page_search as page_search

    public_mode(monkeypatch)
    searched: list[str] = []
    monkeypatch.setattr(page_search, "search_youtube", lambda query, *args, **kwargs: searched.append(query) or [])
    app = run_page("search")
    app.text_input(key="keep_search_query").set_value("cats")
    app.button[0].click().run()
    app.text_input(key="keep_search_query").set_value("dogs")
    app.button[0].click().run()
    assert searched == ["cats"]
    assert "請稍等幾秒" in texts(app.warning)


def test_this_machines_owner_is_not_rate_limited(monkeypatch: pytest.MonkeyPatch) -> None:
    import videocap.ui.page_search as page_search

    searched: list[str] = []
    monkeypatch.setattr(page_search, "search_youtube", lambda query, *args, **kwargs: searched.append(query) or [])
    app = run_page("search")
    for query in ("cats", "dogs"):
        app.text_input(key="keep_search_query").set_value(query)
        app.button[0].click().run()
    assert searched == ["cats", "dogs"]


def test_slots_are_returned_after_use() -> None:
    from videocap.ui import common

    ctx = SimpleNamespace(trusted=False)
    state: dict[str, Any] = {}
    original = common.st
    common.st = SimpleNamespace(session_state=state, warning=lambda *args: None)  # type: ignore[assignment]
    try:
        for _ in range(common.PUBLIC_REQUEST_LIMIT + 1):
            state.clear()
            with common.public_request_slot(ctx) as allowed:  # type: ignore[arg-type]
                assert allowed
    finally:
        common.st = original  # type: ignore[assignment]


# Round two: follow-ups from the engine fixes


def with_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> None:
    from videocap.ui import context

    monkeypatch.setattr(context, "get_ffmpeg_status", lambda: (True, "ffmpeg"))


def run_with_settings(page_name: str, **settings: Any) -> AppTest:
    # The first run loads the saved settings, so changes go in after it.
    app = run_page(page_name)
    for name, value in settings.items():
        app.session_state[f"opt_{name}"] = value
    app.run()
    assert not app.exception, app.exception
    return app


def selectbox_labels(app: AppTest) -> list[str]:
    return [str(box.label) for box in app.selectbox]


# engine:17 - a bitrate is offered only where it changes the file


@pytest.mark.parametrize(("audio_format", "offered"), [("mp3", True), ("m4a", False), ("opus", False), ("flac", False)])
def test_the_bitrate_is_offered_only_for_formats_that_are_re_encoded(
    monkeypatch: pytest.MonkeyPatch, audio_format: str, offered: bool
) -> None:
    with_ffmpeg(monkeypatch)
    app = run_with_settings("download", media_type="audio", audio_format=audio_format)
    assert ("音訊位元率（kbps）" in selectbox_labels(app)) is offered


# engine:10, engine:12 - covers and subtitles that a WebM or WAV file cannot hold


@pytest.mark.parametrize(
    ("settings", "possible"),
    [
        ({"media_type": "video", "container": "webm"}, False),
        ({"media_type": "audio", "audio_format": "wav"}, False),
        ({"media_type": "video", "container": "mp4"}, True),
        ({"media_type": "audio", "audio_format": "mp3"}, True),
    ],
)
def test_a_cover_is_not_offered_where_it_cannot_be_embedded(
    monkeypatch: pytest.MonkeyPatch, settings: dict[str, str], possible: bool
) -> None:
    with_ffmpeg(monkeypatch)
    app = run_with_settings("download", **settings)
    assert app.checkbox(key="opt_embed_thumbnail").disabled is not possible
    assert ("無法嵌入封面圖" in texts(app.caption)) is not possible


def test_webm_says_its_embedded_subtitles_are_saved_as_vtt(monkeypatch: pytest.MonkeyPatch) -> None:
    with_ffmpeg(monkeypatch)
    app = run_with_settings("download", media_type="video", container="webm", download_subtitles=True, embed_subtitles=True)
    assert ".vtt" in texts(app.caption)
    app.session_state["opt_container"] = "mp4"
    app.run()
    assert ".vtt" not in texts(app.caption)


# security:2 - visitors cannot ask for a full re-encode


def test_public_visitors_cannot_choose_precise_cuts(monkeypatch: pytest.MonkeyPatch) -> None:
    with_ffmpeg(monkeypatch)
    assert not run_page("download").checkbox(key="opt_precise_cuts").disabled
    public_mode(monkeypatch)
    assert run_page("download").checkbox(key="opt_precise_cuts").disabled


# ui:2, jobs-services:1, security:4 - retrying needs this session's login and says why it cannot start


def needs_login_spec(tmp_path: Path, url: str = "https://www.youtube.com/playlist?list=PLmembers") -> Any:
    from videocap.settings import DownloadSettings, JobSpec

    return JobSpec(url=url, settings=DownloadSettings(), output_dir=str(tmp_path / "out"), needs_cookie_file=True)


def finished_playlist_with_a_failure(tmp_path: Path) -> Job:
    from videocap.options import AccessLimits
    from videocap.services import DownloadPayload

    outcome = empty_outcome()
    outcome["errors"] = ["members only"]
    outcome["failed_ids"] = ["jNQXAC9IVRw"]
    return add_job(DONE, payload=DownloadPayload(needs_login_spec(tmp_path), AccessLimits()), outcome=outcome, finished_at=time.time())


def record_logins(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    used: list[bool] = []

    def recording(url: str, options: Any, *args: Any) -> Any:
        used.append(options.login.cookie_file is not None)
        return fake_download(url, options, *args)

    monkeypatch.setattr(services_module, "run_download", recording)
    return used


def test_retrying_failed_videos_asks_for_the_login_they_need(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    used = record_logins(monkeypatch)
    finished_playlist_with_a_failure(tmp_path)
    app = run_page("queue")
    button(app, "重試失敗的 1 部").click().run()
    assert not app.exception, app.exception
    assert "請先在「會員登入」上傳 cookies.txt" in texts(app.toast)
    assert len(services().manager.list_jobs()) == 1
    assert used == []


def test_retrying_failed_videos_uses_this_sessions_login(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    used = record_logins(monkeypatch)
    finished_playlist_with_a_failure(tmp_path)
    app = run_page("queue")
    app.session_state["keep_login_method"] = "cookies"
    app.session_state["cookies_bytes"] = b"# Netscape HTTP Cookie File\n"
    app.run()
    button(app, "重試失敗的 1 部").click().run()
    assert not app.exception, app.exception
    manager = services().manager
    wait_until(lambda: len(manager.list_jobs()) == 2 and not manager.active_jobs())
    assert used == [True]


def full_public_storage(monkeypatch: pytest.MonkeyPatch) -> str:
    message = "你在這個公開主機暫存的檔案已達上限。"
    monkeypatch.setattr(services_module.AppServices, "storage_problem", lambda self, owner, limits: "" if limits.trusted else message)
    return message


def test_a_visitor_whose_storage_is_full_is_told_so_when_downloading(monkeypatch: pytest.MonkeyPatch) -> None:
    from videocap.ui.common import QUEUE_FULL_MESSAGE

    public_mode(monkeypatch)
    message = full_public_storage(monkeypatch)
    app = run_page("download")
    app.text_area(key="keep_url_input").set_value(URL).run()
    button(app, "開始下載").click().run()
    assert not app.exception, app.exception
    assert message in texts(app.error)
    assert QUEUE_FULL_MESSAGE not in texts(app.error)


def test_a_visitor_whose_storage_is_full_is_told_so_when_retrying(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from videocap.options import AccessLimits
    from videocap.services import DownloadPayload
    from videocap.settings import DownloadSettings, JobSpec

    public_mode(monkeypatch)
    message = full_public_storage(monkeypatch)
    app = run_page("queue")
    spec = JobSpec(url=URL, settings=DownloadSettings(), output_dir=str(tmp_path / "out"))
    add_job(FAILED, owner=app.session_state["session_owner"], public=True, payload=DownloadPayload(spec, AccessLimits.for_public_host()))
    app.run()
    button(app, "重試").click().run()
    assert not app.exception, app.exception
    assert message in texts(app.toast)


# security:6 - removing the cookies also takes them from waiting jobs


def test_removing_cookies_takes_them_from_waiting_jobs() -> None:
    job = add_job(PAUSED, cookie_bytes=b"SECRET")
    app = run_page("download")
    app.session_state["keep_login_method"] = "cookies"
    app.session_state["cookies_bytes"] = b"SECRET"
    app.run()
    button(app, "移除 cookies").click().run()
    assert not app.exception, app.exception
    assert app.session_state["cookies_bytes"] is None
    assert job.cookie_bytes is None


# jobs-services:8 - downloads wait for a restart after a yt-dlp update


def test_the_queue_says_a_restart_is_needed_after_a_ytdlp_update() -> None:
    assert "重新啟動" not in texts(run_page("queue").warning)
    services().restart_required = True
    app = run_page("queue")
    assert "yt-dlp 已更新，請重新啟動 App 才能繼續下載。" in texts(app.warning)


@pytest.mark.parametrize("windows", [True, False])
def test_the_restart_notice_says_how_to_restart_a_copy_started_at_sign_in(monkeypatch: pytest.MonkeyPatch, windows: bool) -> None:
    from videocap.ui import page_system
    from videocap.ui.page_system import AUTOSTART_RESTART_STEPS

    monkeypatch.setattr(page_system, "autostart_supported", lambda: windows)
    services().restart_required = True
    assert (AUTOSTART_RESTART_STEPS in texts(run_page("queue").caption)) is windows
    assert (AUTOSTART_RESTART_STEPS in texts(run_page("download").caption)) is windows
    # Visitors cannot restart this machine's app.
    public_mode(monkeypatch)
    services(True).restart_required = True
    assert AUTOSTART_RESTART_STEPS not in texts(run_page("queue").caption)


# links-errors:14 - the list= setting decides how links are read


PLAYLIST_VARIANT = f"{URL}&list=PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf"


def test_the_whole_list_sentence_names_the_limit() -> None:
    app = run_with_settings("download", expand_playlist=True, playlist_limit=7)
    app.text_area(key="keep_url_input").set_value(PLAYLIST_VARIANT).run()
    assert "會下載整份清單（最多 7 部）" in texts(app.caption)


def test_a_video_and_its_playlist_are_both_kept_when_whole_lists_are_wanted() -> None:
    app = run_page("download")
    app.text_area(key="keep_url_input").set_value(f"{URL}\n{PLAYLIST_VARIANT}").run()
    assert "偵測到 1 部影片。" in texts(app.caption)
    app.session_state["opt_expand_playlist"] = True
    app.run()
    assert "1 部影片、1 個播放清單" in texts(app.caption)


def test_imported_files_follow_the_whole_list_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    import videocap.ui.page_download as page_download

    data = f"{URL}\n{PLAYLIST_VARIANT}\n".encode()
    state: dict[str, Any] = {"link_file_upload": uploaded("links.txt", data), "keep_url_input": "", "opt_expand_playlist": True}
    monkeypatch.setattr(page_download, "st", SimpleNamespace(session_state=state))
    page_download.append_links_from_file(SimpleNamespace(trusted=True))  # type: ignore[arg-type]
    assert state["keep_url_input"].splitlines() == [URL, PLAYLIST_VARIANT]


# links-errors:3, links-errors:4, platform:6 - list files in other encodings, and what a rejected entry is


def test_imported_big5_files_keep_their_links(monkeypatch: pytest.MonkeyPatch) -> None:
    state = import_file(monkeypatch, "我的清單\r\nhttps://youtu.be/jNQXAC9IVRw 第一集\r\n".encode("big5"))
    assert state["keep_url_input"].splitlines() == ["https://youtu.be/jNQXAC9IVRw"]


def test_rejected_entries_are_described_as_not_downloadable() -> None:
    app = run_page("download")
    app.text_area(key="keep_url_input").set_value("https://www.youtube.com/").run()
    assert "不是可下載的 YouTube 影片、播放清單或頻道連結" in texts(app.warning)


# platform:11 - Linux reads Chromium-family logins only with a desktop keyring


def test_linux_suggests_firefox_or_a_cookie_file_for_chromium_browsers(monkeypatch: pytest.MonkeyPatch) -> None:
    import videocap.ui.sidebar as sidebar
    from videocap.ui import context

    monkeypatch.setattr(context, "is_local_request", lambda *args: True)
    monkeypatch.setattr(sidebar, "sys", SimpleNamespace(platform="linux"))
    app = run_page("download")
    app.radio(key="keep_login_method").set_value("browser").run()
    app.selectbox(key="keep_login_browser").set_value("firefox").run()
    assert "Linux" not in texts(app.caption)
    app.selectbox(key="keep_login_browser").set_value("chrome").run()
    assert "請改用 Firefox 或 cookies.txt" in texts(app.caption)


# links-errors:6 - search, comments and the login check use the proxy, but only this machine's owner's


PROXY = "http://127.0.0.1:7890"


def record_network(monkeypatch: pytest.MonkeyPatch, module: Any, name: str, result: Any) -> list[Any]:
    seen: list[Any] = []

    def recording(*args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs.get("network"))
        return result

    monkeypatch.setattr(module, name, recording)
    return seen


@pytest.mark.parametrize(("public", "expected"), [(False, PROXY), (True, "")])
def test_search_uses_the_proxy_only_for_this_machines_owner(monkeypatch: pytest.MonkeyPatch, public: bool, expected: str) -> None:
    import videocap.ui.page_search as page_search

    if public:
        public_mode(monkeypatch)
    seen = record_network(monkeypatch, page_search, "search_youtube", [])
    # A visitor can bring a proxy in through an imported settings file.
    app = run_with_settings("search", proxy=PROXY, force_ipv4=True)
    app.text_input(key="keep_search_query").set_value("cats")
    app.button[0].click().run()
    assert not app.exception, app.exception
    assert [(network.proxy, network.force_ipv4) for network in seen] == [(expected, True)]


@pytest.mark.parametrize(("public", "expected"), [(False, PROXY), (True, "")])
def test_comment_export_uses_the_proxy_only_for_this_machines_owner(
    monkeypatch: pytest.MonkeyPatch, public: bool, expected: str
) -> None:
    import videocap.ui.page_download as page_download

    if public:
        public_mode(monkeypatch)
    seen = record_network(monkeypatch, page_download, "fetch_comments", [])
    app = run_with_settings("download", proxy=PROXY)
    app.session_state["keep_url_input"] = URL
    app.session_state["last_reports"] = [{"url": URL, "title": "t", "video_id": "jNQXAC9IVRw", "is_playlist": False}]
    app.run()
    next(item for item in app.button if item.label.startswith("取得留言")).click().run()
    assert not app.exception, app.exception
    assert [network.proxy for network in seen] == [expected]


@pytest.mark.parametrize(("public", "expected"), [(False, PROXY), (True, "")])
def test_the_login_check_uses_the_proxy_only_for_this_machines_owner(
    monkeypatch: pytest.MonkeyPatch, public: bool, expected: str
) -> None:
    import videocap.ui.sidebar as sidebar

    if public:
        public_mode(monkeypatch)
    seen = record_network(monkeypatch, sidebar, "check_youtube_login", ("success", "已登入"))
    app = run_with_settings("download", proxy=PROXY)
    app.session_state["keep_login_method"] = "cookies"
    app.session_state["cookies_bytes"] = b"# Netscape HTTP Cookie File\n"
    app.run()
    button(app, "確認登入狀態").click().run()
    assert not app.exception, app.exception
    assert [network.proxy for network in seen] == [expected]



# New download options, part A: what the sidebar offers, and to whom


def widget_keys(app: AppTest) -> set[str]:
    elements = [*app.checkbox, *app.selectbox, *app.text_input, *app.text_area, *app.number_input, *app.multiselect]
    return {str(element.key) for element in elements}


OWNER_ONLY_KEYS = {"opt_ytdlp_extra_args", "opt_write_nfo", "opt_save_live_chat", "opt_wait_for_live"}
EVERYONES_KEYS = {
    "opt_player_client",
    "opt_prefer_traditional_metadata",
    "opt_remove_ranges",
    "opt_thumbnails_as_jpg",
    "opt_embed_infojson",
    "opt_mtime_upload_date",
    "opt_subtitles_to_traditional",
    "opt_music_tags",
    "opt_comments_mode",
}


@pytest.mark.parametrize("public", [False, True])
def test_new_download_options_are_offered_to_whom_they_are_for(monkeypatch: pytest.MonkeyPatch, public: bool) -> None:
    with_ffmpeg(monkeypatch)
    if public:
        public_mode(monkeypatch)
    keys = widget_keys(run_with_settings("download", media_type="video"))
    assert EVERYONES_KEYS <= keys
    assert (OWNER_ONLY_KEYS <= keys) is not public
    assert not public or not OWNER_ONLY_KEYS & keys


def test_extra_ytdlp_option_mistakes_and_ignored_options_are_shown(monkeypatch: pytest.MonkeyPatch) -> None:
    app = run_with_settings("download", ytdlp_extra_args="--no-such-option")
    assert "no such option: --no-such-option" in texts(app.warning)
    assert "清空這個欄位" in texts(app.warning)
    app.session_state["opt_ytdlp_extra_args"] = '--limit-rate 1M -o "%(id)s.%(ext)s"'
    app.run()
    assert "no such option" not in texts(app.warning)
    assert "已忽略：outtmpl" in texts(app.caption)


def test_spots_are_offered_only_for_marking(monkeypatch: pytest.MonkeyPatch) -> None:
    with_ffmpeg(monkeypatch)
    app = run_with_settings("download", sponsorblock_mode="mark", sponsorblock_categories=["sponsor", "chapter"])
    assert "社群章節（影片沒有章節時補上）" in app.multiselect(key="opt_sponsorblock_categories").options
    app.session_state["opt_sponsorblock_mode"] = "remove"
    app.run()
    assert not app.exception, app.exception
    offered = app.multiselect(key="opt_sponsorblock_categories").options
    assert "社群章節（影片沒有章節時補上）" not in offered and "精華起點" not in offered
    assert "開場鋪陳與問候" in offered
    assert app.session_state["opt_sponsorblock_categories"] == ["sponsor"]


def test_video_information_can_be_embedded_only_into_mkv(monkeypatch: pytest.MonkeyPatch) -> None:
    with_ffmpeg(monkeypatch)
    app = run_with_settings("download", media_type="video", container="mp4")
    assert app.checkbox(key="opt_embed_infojson").disabled
    app.session_state["opt_container"] = "mkv"
    app.run()
    assert not app.checkbox(key="opt_embed_infojson").disabled


def test_public_visitors_save_at_most_the_public_number_of_comments(monkeypatch: pytest.MonkeyPatch) -> None:
    from videocap.config import CLOUD_MAX_COMMENTS

    public_mode(monkeypatch)
    app = run_with_settings("download", comments_mode="top", comments_max=5000)
    box = app.number_input(key="opt_comments_max")
    assert (box.value, box.max) == (CLOUD_MAX_COMMENTS, CLOUD_MAX_COMMENTS)


def test_public_visitors_wait_between_videos_at_most_the_public_number_of_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    from videocap.config import CLOUD_MAX_WAIT_SECONDS

    assert run_page("download").number_input(key="opt_sleep_seconds").max == 3600
    public_mode(monkeypatch)
    # The engine waits no longer than this for a public visitor, whatever the field says.
    box = run_page("download").number_input(key="opt_sleep_seconds")
    assert box.max == CLOUD_MAX_WAIT_SECONDS
    assert f"公開主機最多等待 {CLOUD_MAX_WAIT_SECONDS} 秒" in str(box.help)


def test_the_media_server_layout_says_how_files_are_named() -> None:
    app = run_with_settings("download", folder_rule="media_server")
    assert "Season 年份" in texts(app.caption)
    app.session_state["opt_folder_rule"] = "channel"
    app.run()
    assert "Season 年份" not in texts(app.caption)


def test_missing_deno_gives_the_same_advice_as_download_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    from videocap.errors import JS_RUNTIME_HINT
    from videocap.ui import context

    monkeypatch.setattr(context, "get_js_runtime_status", lambda: (False, "找不到 deno"))
    shown = texts(run_page("download").warning)
    assert JS_RUNTIME_HINT in shown
    assert "pip install" not in shown


@pytest.mark.parametrize("public", [False, True])
def test_the_chosen_player_client_and_titles_reach_search(monkeypatch: pytest.MonkeyPatch, public: bool) -> None:
    import videocap.ui.page_search as page_search

    if public:
        public_mode(monkeypatch)
    seen = record_network(monkeypatch, page_search, "search_youtube", [])
    app = run_with_settings("search", player_client="tv", prefer_traditional_metadata=True)
    app.text_input(key="keep_search_query").set_value("cats")
    app.button[0].click().run()
    assert not app.exception, app.exception
    assert [(network.player_client, network.metadata_language) for network in seen] == [("tv", "zh-TW")]


def test_the_music_preset_turns_on_music_tags() -> None:
    from videocap.ui.sidebar import PRESET_SELECT_KEY

    app = run_with_settings("download", player_client="tv", ytdlp_extra_args="--limit-rate 1M")
    assert app.session_state["opt_music_tags"] is False
    app.selectbox(key=PRESET_SELECT_KEY).set_value("音樂（MP3 320k＋封面）").run()
    assert not app.exception, app.exception
    assert app.session_state["opt_music_tags"] is True
    assert app.checkbox(key="opt_music_tags").value is True
    # How this machine reaches YouTube is not part of a built-in preset.
    assert app.session_state["opt_player_client"] == "tv"
    assert app.session_state["opt_ytdlp_extra_args"] == "--limit-rate 1M"


def test_applying_a_preset_names_that_preset() -> None:
    from videocap.ui.sidebar import PRESET_SELECT_KEY

    app = run_page("download")
    app.selectbox(key=PRESET_SELECT_KEY).set_value("最高畫質").run()
    assert not app.exception, app.exception
    assert "已套用「最高畫質」。" in texts(app.caption)


@pytest.mark.parametrize(
    ("key", "phrases"),
    [
        # Only a single video waits, at most a day, and the stream is then recorded whatever 遇到直播 says.
        ("opt_wait_for_live", ["單一影片", "24 小時", "錄製"]),
        ("opt_subtitles_to_traditional", ["下載字幕", "嵌入"]),
        ("opt_music_tags", ["正方形", "音訊"]),
        ("opt_write_nfo", ["另存封面圖", "-thumb.jpg"]),
    ],
)
def test_new_options_say_what_they_really_do(monkeypatch: pytest.MonkeyPatch, key: str, phrases: list[str]) -> None:
    with_ffmpeg(monkeypatch)
    help_text = str(app_checkbox_help(run_with_settings("download", media_type="video"), key))
    assert all(phrase in help_text for phrase in phrases), help_text
    assert "請同時把「遇到直播」設為錄製" not in help_text


def app_checkbox_help(app: AppTest, key: str) -> str:
    return app.checkbox(key=key).help


def test_extra_ytdlp_options_say_which_are_refused_and_what_to_use_instead() -> None:
    help_text = str(run_page("download").text_area(key="opt_ytdlp_extra_args").help)
    # Each refused or ignored kind, and the part of the app that does the same job.
    for phrase in ("--username", "會員登入", "--skip-download", "--print", "-F", "僅字幕與資訊", "--wait-for-video", "等待開播"):
        assert phrase in help_text, phrase


def test_waiting_for_a_stream_is_not_offered_without_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> None:
    from videocap.ui import context

    # The stream is recorded once it starts, which needs ffmpeg: without it the wait would end in a failure hours later.
    monkeypatch.setattr(context, "get_ffmpeg_status", lambda: (False, "找不到 ffmpeg"))
    assert run_page("download").checkbox(key="opt_wait_for_live").disabled
    with_ffmpeg(monkeypatch)
    assert not run_page("download").checkbox(key="opt_wait_for_live").disabled


def test_a_saved_preset_brings_back_the_new_options() -> None:
    from videocap.ui.sidebar import PRESET_SELECT_KEY

    chosen = {
        "music_tags": True, "thumbnails_as_jpg": True, "comments_mode": "top", "comments_max": 300,
        "subtitles_to_traditional": True, "mtime_upload_date": True, "write_nfo": True, "wait_for_live": True,
        "save_live_chat": True, "player_client": "tv", "prefer_traditional_metadata": True, "folder_rule": "media_server",
        "ytdlp_extra_args": "--limit-rate 1M",
    }  # fmt: skip
    app = run_with_settings("download", **chosen)
    app.text_input(key="preset_new_name").set_value("Mine")
    button(app, "把目前設定存成組合").click().run()
    for name in chosen:
        app.session_state[f"opt_{name}"] = run_page("download").session_state[f"opt_{name}"]
    app.run()
    app.selectbox(key=PRESET_SELECT_KEY).set_value("Mine").run()
    assert not app.exception, app.exception
    assert {name: app.session_state[f"opt_{name}"] for name in chosen} == chosen


def apply_preset(app: AppTest, name: str) -> AppTest:
    from videocap.ui.sidebar import PRESET_SELECT_KEY

    app.selectbox(key=PRESET_SELECT_KEY).set_value(name).run()
    assert not app.exception, app.exception
    return app


def test_a_preset_can_remember_the_download_folder(tmp_path: Path) -> None:
    from videocap.ui.context import OUTPUT_DIR_WIDGET_KEY
    from videocap.ui.sidebar import PRESET_FOLDER_KEY

    lessons = tmp_path / "課程影片"
    app = run_page("download")
    app.text_input(key=OUTPUT_DIR_WIDGET_KEY).set_value(str(lessons)).run()
    app.text_input(key="preset_new_name").set_value("課程")
    app.checkbox(key="preset_remember_folder").check()
    button(app, "把目前設定存成組合").click().run()
    assert not app.exception, app.exception
    assert services().storage.load_presets()["課程"][PRESET_FOLDER_KEY] == str(lessons.resolve())

    app.text_input(key=OUTPUT_DIR_WIDGET_KEY).set_value(str(tmp_path / "其他")).run()
    apply_preset(app, "課程")
    assert app.session_state[OUTPUT_DIR_WIDGET_KEY] == str(lessons.resolve())
    assert app.text_input(key=OUTPUT_DIR_WIDGET_KEY).value == str(lessons.resolve())
    assert "下載資料夾改為" in texts(app.caption)


def test_a_preset_saved_without_the_folder_leaves_the_folder_alone(tmp_path: Path) -> None:
    from videocap.ui.context import OUTPUT_DIR_WIDGET_KEY
    from videocap.ui.sidebar import PRESET_FOLDER_KEY

    app = run_page("download")
    app.text_input(key="preset_new_name").set_value("不含資料夾")
    button(app, "把目前設定存成組合").click().run()
    assert PRESET_FOLDER_KEY not in services().storage.load_presets()["不含資料夾"]
    app.text_input(key=OUTPUT_DIR_WIDGET_KEY).set_value(str(tmp_path / "其他")).run()
    apply_preset(app, "不含資料夾")
    assert app.session_state[OUTPUT_DIR_WIDGET_KEY] == str(tmp_path / "其他")


def test_a_preset_saved_before_folders_were_remembered_still_applies() -> None:
    from videocap.storage import PRESETS_KEY
    from videocap.ui.context import OUTPUT_DIR_WIDGET_KEY

    services().storage.set_value(PRESETS_KEY, {"舊組合": {"quality": "720p", "download_subtitles": False}})
    app = run_page("download")
    folder = app.session_state[OUTPUT_DIR_WIDGET_KEY]
    apply_preset(app, "舊組合")
    assert app.session_state["opt_quality"] == "720p"
    assert app.session_state["opt_download_subtitles"] is False
    assert app.session_state[OUTPUT_DIR_WIDGET_KEY] == folder
    assert "已套用「舊組合」。" in texts(app.caption)


def test_a_remembered_folder_that_cannot_be_made_is_not_used(tmp_path: Path) -> None:
    from videocap.storage import PRESETS_KEY
    from videocap.ui.context import OUTPUT_DIR_WIDGET_KEY
    from videocap.ui.sidebar import PRESET_FOLDER_KEY

    blocker = tmp_path / "blocker.txt"
    blocker.write_text("not a folder", encoding="utf-8")
    services().storage.set_value(PRESETS_KEY, {"壞資料夾": {"quality": "720p", PRESET_FOLDER_KEY: str(blocker / "videos")}})
    app = run_page("download")
    folder = app.session_state[OUTPUT_DIR_WIDGET_KEY]
    apply_preset(app, "壞資料夾")
    # The settings still apply; only the folder is kept as it was, and the page says why.
    assert app.session_state["opt_quality"] == "720p"
    assert app.session_state[OUTPUT_DIR_WIDGET_KEY] == folder
    assert "是檔案，不是資料夾" in texts(app.warning)


def test_visitors_never_get_a_folder_from_a_preset(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from videocap.ui.context import OUTPUT_DIR_WIDGET_KEY
    from videocap.ui.sidebar import PRESET_FOLDER_KEY

    public_mode(monkeypatch)
    app = new_page("download")
    app.session_state["session_presets"] = {"帶資料夾": {"quality": "720p", PRESET_FOLDER_KEY: str(tmp_path / "server")}}
    app.run()
    assert not app.exception, app.exception
    # The folder is the server's, so a visitor is not offered to remember it.
    assert "preset_remember_folder" not in [checkbox.key for checkbox in app.checkbox]
    folder = app.session_state[OUTPUT_DIR_WIDGET_KEY]
    apply_preset(app, "帶資料夾")
    assert app.session_state["opt_quality"] == "720p"
    assert app.session_state[OUTPUT_DIR_WIDGET_KEY] == folder

    app.text_input(key="preset_new_name").set_value("訪客組合")
    app.session_state["preset_remember_folder"] = True
    button(app, "把目前設定存成組合").click().run()
    assert PRESET_FOLDER_KEY not in app.session_state["session_presets"]["訪客組合"]


# Exporting a listed playlist


PLAYLIST_URL = "https://www.youtube.com/playlist?list=PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf"


def recorded_files(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, Any]]:
    """What each download button hands the browser, and under which file name."""
    from streamlit.runtime.memory_media_file_storage import MemoryMediaFileStorage

    saved: list[tuple[Any, Any]] = []
    original = MemoryMediaFileStorage.load_and_get_id

    def record(self: Any, path_or_data: Any, mimetype: str, kind: Any, filename: Any = None) -> str:
        saved.append((path_or_data, filename))
        return original(self, path_or_data, mimetype, kind, filename)

    monkeypatch.setattr(MemoryMediaFileStorage, "load_and_get_id", record)
    return saved


def playlist_report() -> dict[str, Any]:
    watch = "https://www.youtube.com/watch?v=video00000"
    return {
        "url": PLAYLIST_URL, "error": "", "title": "課程：第 1/2 季", "is_playlist": True, "is_nested": False, "entry_count": 2,
        "entries": [
            {"index": 1, "id": "video000001", "title": "第一集", "url": f"{watch}1", "duration": "1:05", "channel": "頻道",
             "views": 1234, "upload_date": "2024-01-02"},
            # Listed before upload dates were kept, as a report from earlier in this session may be.
            {"index": 2, "id": "video000002", "title": "第二集", "url": f"{watch}2", "duration": "", "channel": "", "views": None},
        ],
    }  # fmt: skip


@pytest.mark.parametrize("public", [False, True])
def test_a_listed_playlist_exports_its_videos_as_csv(public: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    saved = recorded_files(monkeypatch)
    if public:
        public_mode(monkeypatch)
    app = new_page("download")
    app.session_state["keep_url_input"] = PLAYLIST_URL
    app.session_state["last_reports"] = [playlist_report()]
    app.run()
    assert not app.exception, app.exception
    assert "匯出影片清單 CSV" in [item.proto.label for item in download_buttons(app)]
    # Named after the list, without the "/" no file name may hold.
    data = next(content for content, name in saved if name == "課程：第 1_2 季.csv")
    assert data.startswith(b"\xef\xbb\xbf")
    assert data.decode("utf-8-sig").splitlines() == [
        "標題,影片 ID,網址,長度,上傳日期,觀看數,頻道",
        "第一集,video000001,https://www.youtube.com/watch?v=video000001,1:05,2024-01-02,1234,頻道",
        "第二集,video000002,https://www.youtube.com/watch?v=video000002,,,,",
    ]


# Free space on this machine's disk


def disk_with_free(monkeypatch: pytest.MonkeyPatch, free: int) -> None:
    import shutil

    monkeypatch.setattr(shutil, "disk_usage", lambda path: SimpleNamespace(total=free * 2, used=free, free=free))


def test_the_owner_is_warned_but_still_downloads_when_the_disk_is_nearly_full(monkeypatch: pytest.MonkeyPatch) -> None:
    disk_with_free(monkeypatch, 1024**3)
    app = run_page("download")
    assert "磁碟剩餘 1.00 GB" in texts(app.caption)
    app.text_area(key="keep_url_input").set_value(URL).run()
    button(app, "開始下載").click().run()
    assert not app.exception, app.exception
    assert "下載資料夾所在的磁碟只剩 1.00 GB，可能不夠。" in texts(app.warning)
    # Small files may still fit, so the download is queued all the same.
    assert len(services().manager.list_jobs()) == 1


def test_enough_free_space_brings_no_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    disk_with_free(monkeypatch, 100 * 1024**3)
    app = run_page("download")
    assert "磁碟剩餘 100.00 GB" in texts(app.caption)
    app.text_area(key="keep_url_input").set_value(URL).run()
    button(app, "開始下載").click().run()
    assert not app.exception, app.exception
    assert "可能不夠" not in texts(app.warning)
    assert len(services().manager.list_jobs()) == 1


def test_public_visitors_hear_nothing_about_the_servers_disk(monkeypatch: pytest.MonkeyPatch) -> None:
    public_mode(monkeypatch)
    disk_with_free(monkeypatch, 1024**3)
    # The public host's own check, which refuses jobs below its own mark, is not what this test is about.
    monkeypatch.setattr(services_module, "CLOUD_MIN_FREE_BYTES", 0)
    app = run_page("download")
    assert "磁碟剩餘" not in texts(app.caption)
    app.text_area(key="keep_url_input").set_value(URL).run()
    button(app, "開始下載").click().run()
    assert not app.exception, app.exception
    assert "可能不夠" not in texts(app.warning)
    assert len(services(True).manager.list_jobs()) == 1

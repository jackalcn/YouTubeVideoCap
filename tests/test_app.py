from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

import videocap.services as services_module
from videocap.downloader import empty_outcome
from videocap.jobs import ACTIVE_STATES


ROOT = Path(__file__).resolve().parents[1]
URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
TRUSTED_PAGES = ["download", "queue", "search", "library", "subscriptions", "history", "tools", "system"]
PUBLIC_PAGES = ["download", "queue", "search", "tools", "system"]


def fake_download(url: str, options: Any, progress_ui: Any, status_ui: Any, should_stop: Any, on_log: Any) -> Any:
    options.output_dir.mkdir(parents=True, exist_ok=True)
    path = options.output_dir / "video.mp4"
    path.write_bytes(b"video-bytes")
    outcome = empty_outcome()
    outcome["items"] = [
        {
            "title": "Fake Title",
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
    return outcome


@pytest.fixture(autouse=True)
def isolated_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VIDEOCAP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("VIDEOCAP_OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setenv("VIDEOCAP_TRUST_REMOTE", "1")
    monkeypatch.setenv("VIDEOCAP_MODE", "local")
    monkeypatch.delenv("VIDEOCAP_PASSWORD", raising=False)
    monkeypatch.setattr(services_module, "run_download", fake_download)
    monkeypatch.setattr(services_module, "CLOUD_OUTPUT_ROOT", tmp_path / "public")
    st.cache_resource.clear()
    yield
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


def run_page(page_name: str) -> AppTest:
    app = AppTest.from_function(page_script, kwargs={"page_name": page_name}, default_timeout=30)
    app.run()
    assert not app.exception, app.exception
    return app


def run_main() -> AppTest:
    app = AppTest.from_file(str(ROOT / "main.py"), default_timeout=30)
    app.run()
    assert not app.exception, app.exception
    return app


def button(app: AppTest, label: str) -> Any:
    return next(item for item in app.button if item.label == label)


def wait_for_jobs(app: AppTest) -> None:
    """Runs the app again until the background jobs end, the way the page's own polling would."""
    deadline = time.time() + 10
    while time.time() < deadline:
        services = services_of(app)
        if not [job for job in services.manager.list_jobs() if job.state in ACTIVE_STATES]:
            break
        time.sleep(0.02)
    app.run()
    assert not app.exception, app.exception


def services_of(app: AppTest) -> Any:
    from videocap.ui.context import get_services

    return get_services(False)


def texts(elements: Any) -> str:
    return " ".join(str(element.value) for element in elements)


def test_app_starts_on_the_download_page() -> None:
    app = run_main()
    labels = [item.label for item in app.button]
    assert "開始下載" in labels
    assert "解析連結（不下載）" in labels


@pytest.mark.parametrize("name", TRUSTED_PAGES)
def test_each_page_renders_for_this_machines_owner(name: str) -> None:
    run_page(name)


@pytest.mark.parametrize("name", PUBLIC_PAGES)
def test_public_pages_render(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    public_mode(monkeypatch)
    run_page(name)


def test_download_flow_shows_the_result(tmp_path: Path) -> None:
    app = run_main()
    app.text_area(key="keep_url_input").set_value(f"看這個 {URL}").run()
    assert "偵測到 1 部影片" in texts(app.caption)

    button(app, "開始下載").click().run()
    assert not app.exception, app.exception
    wait_for_jobs(app)
    assert "下載完成" in texts(app.success)
    assert (tmp_path / "out" / "video.mp4").exists()


def test_finished_download_appears_in_history_and_library(tmp_path: Path) -> None:
    app = run_main()
    app.text_area(key="keep_url_input").set_value(URL).run()
    button(app, "開始下載").click().run()
    wait_for_jobs(app)

    history = run_page("history")
    assert "累計 1 個項目" in texts(history.caption)
    library = run_page("library")
    assert "1 個檔案" in texts(library.caption)


def test_rejected_links_are_named_and_nothing_starts() -> None:
    app = run_main()
    app.text_area(key="keep_url_input").set_value("https://evil.example/video").run()
    button(app, "開始下載").click().run()
    assert "不是可下載的 YouTube" in texts(app.error)
    assert services_of(app).manager.list_jobs() == []


def test_empty_input_asks_for_a_link() -> None:
    app = run_main()
    button(app, "開始下載").click().run()
    assert "請輸入至少一個" in texts(app.error)


def test_bad_time_range_is_explained_before_queueing() -> None:
    app = run_main()
    app.text_area(key="keep_url_input").set_value(URL).run()
    app.session_state["opt_time_ranges"] = "soon"
    button(app, "開始下載").click().run()
    assert "看不懂時間範圍" in texts(app.error)
    assert services_of(app).manager.list_jobs() == []


def test_settings_survive_a_session_restart() -> None:
    app = run_main()
    app.text_area(key="keep_url_input").set_value(URL).run()
    app.selectbox(key="opt_quality").set_value("720p")
    button(app, "開始下載").click().run()
    wait_for_jobs(app)

    next_visit = run_main()
    assert next_visit.selectbox(key="opt_quality").value == "720p"


def test_presets_apply_and_leave_the_proxy_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    import videocap.ui.context as context

    # The MP3 preset needs ffmpeg, which CI runners on Windows and macOS do not have.
    monkeypatch.setattr(context, "get_ffmpeg_status", lambda: (True, "ffmpeg"))
    app = run_main()
    app.session_state["opt_proxy"] = "http://proxy.local:1"
    app.selectbox(key="preset_choice").set_value("音樂（MP3 320k＋封面）").run()
    assert not app.exception, app.exception
    assert app.session_state["opt_media_type"] == "audio"
    assert app.session_state["opt_audio_format"] == "mp3"
    assert app.session_state["opt_proxy"] == "http://proxy.local:1"
    assert "已套用" in texts(app.caption)


def test_saved_preset_is_offered_right_away_and_can_be_deleted() -> None:
    app = run_main()
    app.text_input(key="preset_new_name").set_value("我的組合")
    button(app, "把目前設定存成組合").click().run()
    assert not app.exception, app.exception
    assert "我的組合" in app.selectbox(key="preset_choice").options
    assert "已儲存" in texts(app.caption)

    next_visit = run_main()
    assert "我的組合" in next_visit.selectbox(key="preset_choice").options
    button(next_visit, "刪除").click().run()
    assert "我的組合" not in next_visit.selectbox(key="preset_choice").options


def test_preset_name_cannot_replace_a_builtin() -> None:
    app = run_main()
    app.text_input(key="preset_new_name").set_value("最高畫質")
    button(app, "把目前設定存成組合").click().run()
    assert "內建組合" in texts(app.caption)


def test_link_from_the_address_bar_fills_the_input_without_starting(monkeypatch: pytest.MonkeyPatch) -> None:
    app = AppTest.from_file(str(ROOT / "main.py"), default_timeout=30)
    app.query_params["url"] = URL
    app.run()
    assert not app.exception, app.exception
    assert app.text_area(key="keep_url_input").value == URL
    assert services_of(app).manager.list_jobs() == []


def test_public_hosts_only_offer_public_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    public_mode(monkeypatch)
    from videocap.ui import context

    assert context.is_streamlit_cloud_environment() is True
    app = run_main()
    labels = {item.label for item in app.text_input}
    assert "下載資料夾" not in labels
    assert "代理伺服器" not in labels
    radio_options = [option for radio in app.radio for option in radio.options]
    assert "自訂 yt-dlp 範本" not in radio_options
    assert "使用瀏覽器的登入狀態" not in radio_options


def test_public_system_page_hides_server_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    public_mode(monkeypatch)
    app = run_page("system")
    assert "儲存空間" not in texts(app.markdown)
    assert "資料庫" not in texts(app.markdown)


@pytest.mark.parametrize("public", [False, True])
def test_public_tools_page_has_no_file_tools(public: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    import videocap.ui.context as context

    # Without ffmpeg nobody gets the file tools, which would hide the difference this checks.
    monkeypatch.setattr(context, "get_ffmpeg_status", lambda: (True, "ffmpeg"))
    if public:
        public_mode(monkeypatch)
    app = run_page("tools")
    assert bool([item for item in app.selectbox if item.label == "來源檔案"]) is not public


def test_public_download_result_does_not_reveal_the_folder(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    public_mode(monkeypatch)
    app = run_main()
    app.text_area(key="keep_url_input").set_value(URL).run()
    button(app, "開始下載").click().run()
    assert not app.exception, app.exception

    from videocap.ui.context import get_services

    services = get_services(True)
    deadline = time.time() + 10
    while time.time() < deadline and services.manager.active_jobs():
        time.sleep(0.02)
    app.run()
    assert not app.exception, app.exception
    message = texts(app.success)
    assert "下載完成" in message and "mp4" in message
    assert str(tmp_path) not in message
    assert "public" not in message


def test_password_gate_blocks_until_correct(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIDEOCAP_PASSWORD", "open-sesame")
    app = run_main()
    assert not [item for item in app.button if item.label == "開始下載"]

    app.text_input[0].set_value("wrong")
    app.button[0].click().run()
    assert "密碼不正確" in texts(app.error)
    assert not [item for item in app.button if item.label == "開始下載"]

    app.text_input[0].set_value("open-sesame")
    app.button[0].click().run()
    assert not app.exception, app.exception
    assert [item for item in app.button if item.label == "開始下載"]


def test_without_ffmpeg_the_form_only_offers_what_works(monkeypatch: pytest.MonkeyPatch) -> None:
    import videocap.ui.context as context

    monkeypatch.setattr(context, "get_ffmpeg_status", lambda: (False, "PATH 中找不到 ffmpeg"))
    app = AppTest.from_function(page_script, kwargs={"page_name": "download"}, default_timeout=30)
    # A saved format that needs converting must not break the form.
    app.session_state["opt_media_type"] = "audio"
    app.session_state["opt_audio_format"] = "mp3"
    app.session_state["settings_loaded"] = True
    app.run()
    assert not app.exception, app.exception
    assert app.selectbox(key="opt_audio_format").options == ["M4A（AAC）"]
    assert "未偵測到 ffmpeg" in texts(app.warning)
    assert app.text_input(key="opt_time_ranges").disabled


def test_scheduled_download_waits_and_shows_when(monkeypatch: pytest.MonkeyPatch) -> None:
    app = run_main()
    app.text_area(key="keep_url_input").set_value(URL).run()
    button(app, "排入佇列").click().run()
    assert not app.exception, app.exception
    jobs = services_of(app).manager.list_jobs()
    assert len(jobs) == 1
    assert jobs[0].state == "queued"
    assert jobs[0].not_before is not None and jobs[0].not_before > time.time()
    services_of(app).manager.cancel(jobs[0].id)


def open_page(app: AppTest, url_path: str) -> AppTest:
    """Moves to another page of the navigation, as clicking it would. AppTest.switch_page only knows page files."""
    from streamlit.util import calc_hash

    app._page_hash = calc_hash(url_path)
    app.run()
    assert not app.exception, app.exception
    return app


def test_any_open_page_keeps_a_public_visitors_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    import videocap.ui.app as app_module
    from videocap.jobs import Job, new_job_id
    from videocap.ui.context import get_services

    public_mode(monkeypatch)
    app = run_main()
    services = get_services(True)
    owner = app.session_state["session_owner"]
    # A paused job, on a page without a job list: only the heartbeat says the visitor is still there.
    services.manager.add_paused(Job(id=new_job_id(), owner=owner, kind="download", label="x", payload=None))
    fragments: list[tuple[Any, Any]] = []

    def record_fragment(body: Any, run_every: Any = None) -> Any:
        fragments.append((body, run_every))
        return body

    monkeypatch.setattr(app_module.st, "fragment", record_fragment)
    open_page(app, "system")
    heartbeats = [(body, every) for body, every in fragments if body.__name__ == "heartbeat"]
    assert len(heartbeats) == 1 and heartbeats[0][1] == app_module.VISITOR_HEARTBEAT_SECONDS

    services.manager._last_seen[owner] = 0.0
    heartbeats[0][0]()
    assert services.manager._last_seen[owner] > time.time() - 30


def test_typed_links_survive_moving_to_another_page() -> None:
    app = run_main()
    app.text_area(key="keep_url_input").set_value(URL).run()
    open_page(app, "queue")
    assert not app.text_area
    open_page(app, "download")
    assert app.text_area(key="keep_url_input").value == URL


def test_a_job_drawn_in_two_lists_does_not_crash_the_page(monkeypatch: pytest.MonkeyPatch) -> None:
    import videocap.ui.page_queue as page_queue
    from videocap.jobs import PAUSED

    original = page_queue.render_active_jobs

    def pause_everything_midway(ctx: Any, allow_reorder: bool = False) -> None:
        original(ctx, allow_reorder)
        # A job that pauses between drawing the running list and the paused list.
        for job in ctx.services.manager.list_jobs(ctx.owner):
            job.state = PAUSED

    monkeypatch.setattr(page_queue, "render_active_jobs", pause_everything_midway)
    from videocap.jobs import Job, new_job_id
    from videocap.ui.context import get_services

    services = get_services(False)
    job = Job(id=new_job_id(), owner="local", kind="download", label="x", payload=None, state="running")
    services.manager._jobs[job.id] = job
    run_page("queue")


def break_the_download_page(monkeypatch: pytest.MonkeyPatch) -> None:
    import videocap.ui.page_download as page_download

    def broken(ctx: Any) -> None:
        raise RuntimeError("D:/secret/path")

    monkeypatch.setattr(page_download, "render_hero", broken)


def test_public_errors_hide_the_traceback(monkeypatch: pytest.MonkeyPatch) -> None:
    public_mode(monkeypatch)
    break_the_download_page(monkeypatch)
    app = AppTest.from_file(str(ROOT / "main.py"), default_timeout=30)
    app.run()
    assert not app.exception
    assert "發生錯誤" in texts(app.error)
    assert "secret" not in texts(app.error)


def test_this_machines_owner_still_sees_the_traceback(monkeypatch: pytest.MonkeyPatch) -> None:
    break_the_download_page(monkeypatch)
    app = AppTest.from_file(str(ROOT / "main.py"), default_timeout=30)
    app.run()
    assert "D:/secret/path" in str(app.exception[0].value)

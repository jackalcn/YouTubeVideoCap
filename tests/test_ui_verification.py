"""The button under a failure that asks for a person, how the window is doing, and the system page's list of sites whose
check was passed. The windows here are stand-ins: tests/test_verification.py has the real thing, with a browser."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Optional

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

import videocap.impersonation as impersonation
import videocap.services as services_module
import videocap.ui.common as common
import videocap.verification as verification
from videocap.downloader import empty_outcome
from videocap.errors import humanize_human_check
from videocap.jobs import FAILED, Job, new_job_id
from videocap.options import AccessLimits
from videocap.services import DownloadPayload
from videocap.settings import SETTING_KEY_PREFIX, DownloadSettings, JobSpec
from videocap.ui import context
from videocap.ui.verify import (
    AWAY_FROM_THE_MACHINE,
    CANCEL_LABEL,
    CLEAR_LABEL,
    FINISH_LABEL,
    NEEDS_IMPERSONATION,
    NOTICE_KEY,
    OPEN_LABEL,
    REPORT_NEXT_STEP,
    RETRY_LABEL,
    SYSTEM_NEXT_STEP,
    asks_for_a_person,
)


PAGE = "https://site.example/watch/1"
WANTS_A_PERSON = humanize_human_check("ERROR: [generic] 1: Unable to download webpage: HTTP Error 403: Forbidden", PAGE)
FAR = time.time() + 365 * 24 * 3600
SERVICES: list[Any] = []


@pytest.fixture(autouse=True)
def isolated_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VIDEOCAP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("VIDEOCAP_OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setenv("VIDEOCAP_TRUST_REMOTE", "1")
    monkeypatch.setenv("VIDEOCAP_MODE", "local")
    monkeypatch.delenv("VIDEOCAP_PASSWORD", raising=False)
    monkeypatch.delenv("VIDEOCAP_ALLOWED_HOSTS", raising=False)
    monkeypatch.setattr(services_module, "CLOUD_OUTPUT_ROOT", tmp_path / "public")
    # The viewer is at this machine, whose screen a window would open on.
    monkeypatch.setattr(context, "is_local_request", lambda *args: True)
    monkeypatch.setattr(verification, "unavailable_reason", lambda: "")
    st.cache_resource.clear()
    yield
    SERVICES.clear()
    st.cache_resource.clear()


class StandInWindow:
    """What the page needs of a window: how it is doing, and being told it is passed or canceled."""

    def __init__(self, url: str = PAGE, state: str = verification.WAITING, message: str = "請在瀏覽器視窗裡通過驗證。", title: str = "") -> None:
        self.address = verification.opening_address(url)
        self.host = verification.host_of(self.address)
        self.state, self.message, self.title = state, message, title
        self.told: list[str] = []

    def status(self) -> verification.WindowStatus:
        return verification.WindowStatus(self.state, self.message, self.host, self.address, self.title, seconds_left=300)

    def finish(self) -> None:
        self.told.append("finish")

    def cancel(self) -> None:
        self.told.append("cancel")


def page_script(page_name: str) -> None:
    import importlib

    from videocap.ui.context import build_context, init_session_state

    ctx = build_context()
    init_session_state(ctx)
    module = importlib.import_module(f"videocap.ui.page_{page_name}")
    getattr(module, f"render_{page_name}_page")(ctx)


def run_page(page_name: str, **state: Any) -> AppTest:
    app = AppTest.from_function(page_script, kwargs={"page_name": page_name}, default_timeout=30)
    for key, value in state.items():
        app.session_state[key] = value
    app.run()
    assert not app.exception, app.exception
    return app


def labels(app: AppTest) -> list[str]:
    return [str(item.label) for item in app.button]


def button(app: AppTest, label: str) -> Any:
    return next(item for item in app.button if str(item.label) == label)


def texts(*groups: Any) -> str:
    return " ".join(str(element.value) for elements in groups for element in elements)


def report(error: str = WANTS_A_PERSON, url: str = PAGE) -> dict[str, Any]:
    return {"url": url, "error": error}


def download_page(*reports: dict[str, Any]) -> AppTest:
    return run_page("download", keep_url_input=PAGE, last_reports=list(reports))


def start_a_window(monkeypatch: pytest.MonkeyPatch, window: Optional[StandInWindow] = None) -> StandInWindow:
    """Makes the page's button open this window instead of a browser."""
    opened = window or StandInWindow()
    monkeypatch.setattr(verification, "start_window", lambda url, sites=None, proxy="": setattr(verification, "_current", opened) or opened)
    return opened


# Under a link's failure


def test_a_failure_that_asks_for_a_person_has_a_button_that_opens_the_window(monkeypatch: pytest.MonkeyPatch) -> None:
    assert asks_for_a_person(WANTS_A_PERSON) and not asks_for_a_person("這個網站拒絕了下載請求（HTTP 403）。")
    opened = start_a_window(monkeypatch)
    app = download_page(report())
    assert OPEN_LABEL in labels(app) and WANTS_A_PERSON.split("（")[0] in texts(app.error)
    button(app, OPEN_LABEL).click().run()
    assert not app.exception, app.exception
    # The window is open: the page says so, and offers what an open window can be told.
    assert verification.current_window() is opened
    assert opened.message in texts(app.info) and {FINISH_LABEL, CANCEL_LABEL} <= set(labels(app)) and OPEN_LABEL not in labels(app)


def test_the_window_goes_through_the_proxy_set_in_the_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[tuple[str, str]] = []
    opened = StandInWindow()
    monkeypatch.setattr(verification, "start_window", lambda url, sites=None, proxy="": asked.append((url, proxy)) or setattr(verification, "_current", opened) or opened)
    # The settings the person has set are not replaced by the saved ones.
    app = run_page("download", settings_loaded=True, keep_url_input=PAGE, last_reports=[report()], **{f"{SETTING_KEY_PREFIX}proxy": " http://127.0.0.1:7890 "})
    button(app, OPEN_LABEL).click().run()
    assert not app.exception, app.exception
    assert asked == [(PAGE, "http://127.0.0.1:7890")]


def test_the_window_is_told_when_the_owner_says_it_is_passed_or_cancels(monkeypatch: pytest.MonkeyPatch) -> None:
    opened = StandInWindow(title="Just a moment...")
    monkeypatch.setattr(verification, "_current", opened)
    app = download_page(report())
    assert "Just a moment..." in texts(app.info) and "5 分鐘" in texts(app.info)
    button(app, FINISH_LABEL).click().run()
    button(app, CANCEL_LABEL).click().run()
    assert opened.told == ["finish", "cancel"]


def test_a_window_for_another_site_is_said_to_be_open_and_is_not_offered_again(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(verification, "_current", StandInWindow("https://other.example/"))
    app = download_page(report())
    assert "other.example" in texts(app.caption) and OPEN_LABEL not in labels(app) and FINISH_LABEL not in labels(app)


@pytest.mark.parametrize(
    ("state", "message", "shown"),
    [
        (verification.PASSED, "已通過驗證：site.example。", "success"),
        (verification.CLOSED, "瀏覽器視窗被關閉了，沒有收到驗證。", "warning"),
        (verification.TIMED_OUT, "等太久了，視窗已關閉。", "warning"),
        (verification.FAILED, "瀏覽器視窗沒有順利開啟：x", "warning"),
    ],
)
def test_a_window_that_ended_says_how_and_the_button_is_there_to_open_another(monkeypatch: pytest.MonkeyPatch, state: str, message: str, shown: str) -> None:
    monkeypatch.setattr(verification, "_current", StandInWindow(state=state, message=message))
    app = download_page(report())
    assert message in texts(getattr(app, shown)) and OPEN_LABEL in labels(app)


def test_a_passed_check_tells_a_link_report_what_to_do_next(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(verification, "_current", StandInWindow(state=verification.PASSED, message="已通過驗證：site.example。"))
    app = download_page(report())
    assert "已通過驗證：site.example。" + REPORT_NEXT_STEP in texts(app.success)
    # A report has no job to try again: the page's own buttons do it.
    assert RETRY_LABEL not in labels(app) and OPEN_LABEL in labels(app)


def test_the_result_of_another_sites_window_is_not_said_here(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(verification, "_current", StandInWindow("https://other.example/", state=verification.PASSED, message="已通過驗證：other.example。"))
    app = download_page(report())
    assert "other.example" not in texts(app.success) and OPEN_LABEL in labels(app)


def test_a_failure_of_any_other_kind_has_no_button() -> None:
    app = download_page(report("這個網站拒絕了下載請求（HTTP 403）。"))
    assert OPEN_LABEL not in labels(app)


def test_a_machine_that_cannot_open_a_window_says_why_instead(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(verification, "unavailable_reason", lambda: verification.NO_BROWSER_MESSAGE)
    app = download_page(report())
    assert OPEN_LABEL not in labels(app) and verification.NO_BROWSER_MESSAGE in texts(app.caption)


def test_a_window_that_could_not_be_opened_says_why_after_the_press(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(url: str, sites: Any = None, proxy: str = "") -> Any:
        raise verification.VerificationUnavailable(verification.BUSY_MESSAGE)

    monkeypatch.setattr(verification, "start_window", refuse)
    app = download_page(report())
    button(app, OPEN_LABEL).click().run()
    assert not app.exception, app.exception
    assert verification.BUSY_MESSAGE in texts(app.warning) and NOTICE_KEY not in app.session_state


def test_a_viewer_away_from_the_machine_is_told_to_come_to_it(monkeypatch: pytest.MonkeyPatch) -> None:
    # Allowed to download from other sites, but not sitting at the machine the window would open on.
    monkeypatch.setattr(context, "is_local_request", lambda *args: False)
    app = download_page(report())
    assert OPEN_LABEL not in labels(app) and AWAY_FROM_THE_MACHINE in texts(app.caption)


def test_a_public_visitor_is_offered_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIDEOCAP_MODE", "cloud")
    monkeypatch.delenv("VIDEOCAP_TRUST_REMOTE", raising=False)
    app = download_page(report())
    assert OPEN_LABEL not in labels(app) and AWAY_FROM_THE_MACHINE not in texts(app.caption)


# Under a job's failure


def failed_job(error: str) -> Job:
    from videocap.ui.context import get_services

    spec = JobSpec(url=PAGE, settings=DownloadSettings(), output_dir="out")
    outcome = empty_outcome()
    outcome["errors"] = [error]
    job = Job(id=new_job_id(), owner="local", kind="download", label="x", payload=DownloadPayload(spec, AccessLimits()), state=FAILED, outcome=outcome, finished_at=time.time())
    services = get_services(False)
    SERVICES.append(services)
    with services.manager._lock:
        services.manager._jobs[job.id] = job
    return job


def result_script() -> None:
    """The result of each job in the queue, as the queue page draws it when its expander is opened."""
    from videocap.ui.common import render_job_result
    from videocap.ui.context import build_context, init_session_state

    ctx = build_context()
    init_session_state(ctx)
    for job in ctx.services.manager.list_jobs():
        render_job_result(job, ctx)


def run_results() -> AppTest:
    app = AppTest.from_function(result_script, default_timeout=30)
    app.run()
    assert not app.exception, app.exception
    return app


def test_a_job_that_failed_on_a_check_has_the_button_for_its_own_link(monkeypatch: pytest.MonkeyPatch) -> None:
    opened = start_a_window(monkeypatch)
    failed_job(WANTS_A_PERSON)
    app = run_results()
    assert OPEN_LABEL in labels(app)
    button(app, OPEN_LABEL).click().run()
    assert not app.exception, app.exception
    assert verification.current_window() is opened and FINISH_LABEL in labels(app)


def test_a_job_whose_check_was_passed_can_be_tried_again_from_its_result(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(verification, "_current", StandInWindow(state=verification.PASSED, message="已通過驗證：site.example。"))
    job = failed_job(WANTS_A_PERSON)
    retried: list[str] = []
    monkeypatch.setattr(common, "retry_job", lambda job, ctx: retried.append(job.id))
    app = run_results()
    assert "已通過驗證：site.example。" in texts(app.success) and RETRY_LABEL in labels(app)
    button(app, RETRY_LABEL).click().run()
    assert not app.exception, app.exception
    assert retried == [job.id]


def test_a_job_whose_check_was_not_passed_has_no_button_to_try_again(monkeypatch: pytest.MonkeyPatch) -> None:
    failed_job(WANTS_A_PERSON)
    assert RETRY_LABEL not in labels(run_results())
    # Nor does one whose window was for another site.
    monkeypatch.setattr(verification, "_current", StandInWindow("https://other.example/", state=verification.PASSED, message="已通過驗證：other.example。"))
    assert RETRY_LABEL not in labels(run_results())


def test_a_job_that_failed_for_another_reason_has_no_button() -> None:
    failed_job("這個網站拒絕了下載請求（HTTP 403）。")
    assert OPEN_LABEL not in labels(run_results())


# The system page


def passed(host: str, browser: str = "Chrome", expires: Optional[float] = FAR) -> verification.VerifiedSession:
    cookie = verification.SiteCookie("cf_clearance", "v", host, "/", expires)
    return verification.VerifiedSession(host, (cookie,), "Mozilla/5.0 Chrome/157.0.0.0", (), browser, time.time())


def test_the_system_page_lists_the_sites_whose_check_was_passed_and_forgets_one() -> None:
    verification.verified_sites.add(passed("site.example"))
    verification.verified_sites.add(passed("other.example", browser="Edge"))
    app = run_page("system")
    shown = texts(app.markdown, app.caption, app.get("text"))
    assert "網站驗證" in shown and "site.example（Chrome" in shown and "other.example（Edge" in shown
    button(app, CLEAR_LABEL).click().run()
    assert not app.exception, app.exception
    assert len(verification.verified_sites.all()) == 1


def test_a_site_passed_from_the_system_page_is_said_to_be_used_from_now_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(verification, "_current", StandInWindow("https://typed.example/", state=verification.PASSED, message="已通過驗證：typed.example。"))
    app = run_page("system")
    app.text_input(key="verification_system_url").set_value("https://typed.example/").run()
    assert "已通過驗證：typed.example。" + SYSTEM_NEXT_STEP in texts(app.success)


# The sidebar, where the owner's Referer is typed


def sidebar_warnings(extra: str) -> str:
    app = run_page("download", settings_loaded=True, **{f"{SETTING_KEY_PREFIX}ytdlp_extra_args": extra})
    return texts(app.sidebar.warning)


def test_a_referer_that_is_the_address_of_a_stream_is_said_to_be_wrong() -> None:
    shown = sidebar_warnings('--add-headers "Referer: https://cdn.example/abc/playlist.m3u8"')
    # The site is named, written as markdown (its dots escaped).
    assert "串流或影片檔的網址" in shown and "cdn" in shown
    assert "串流或影片檔的網址" in sidebar_warnings("--referer https://cdn.example/clip.mp4")


def test_a_referer_that_is_a_page_is_left_alone() -> None:
    assert "串流或影片檔的網址" not in sidebar_warnings("--referer https://site.example/watch/1")
    assert "串流或影片檔的網址" not in sidebar_warnings("")


def test_the_system_page_says_when_no_site_has_been_passed() -> None:
    app = run_page("system")
    assert "目前沒有已通過驗證的網站" in texts(app.caption) and OPEN_LABEL not in labels(app)


def test_the_system_page_says_when_a_passed_check_could_not_be_used_yet(monkeypatch: pytest.MonkeyPatch) -> None:
    # A check passed is carried on by the app passing for that browser, which needs 模擬瀏覽器.
    monkeypatch.setattr(impersonation, "ready", lambda: False)
    assert NEEDS_IMPERSONATION in texts(run_page("system").warning)
    monkeypatch.setattr(impersonation, "ready", lambda: True)
    assert NEEDS_IMPERSONATION not in texts(run_page("system").warning)


def test_a_site_can_be_passed_before_any_link_is_pasted(monkeypatch: pytest.MonkeyPatch) -> None:
    opened = start_a_window(monkeypatch, StandInWindow("https://typed.example/"))
    app = run_page("system")
    app.text_input(key="verification_system_url").set_value("https://typed.example/").run()
    button(app, OPEN_LABEL).click().run()
    assert not app.exception, app.exception
    assert verification.current_window() is opened and FINISH_LABEL in labels(app)
    # What is not a web address opens nothing.
    app.text_input(key="verification_system_url").set_value("typed.example").run()
    assert OPEN_LABEL not in labels(app) and FINISH_LABEL not in labels(app)


def test_the_system_page_gives_a_public_visitor_no_checks_to_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIDEOCAP_MODE", "cloud")
    monkeypatch.delenv("VIDEOCAP_TRUST_REMOTE", raising=False)
    app = run_page("system")
    assert "網站驗證" not in texts(app.markdown) and OPEN_LABEL not in labels(app)

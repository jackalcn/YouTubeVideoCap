from __future__ import annotations

import hmac
import math
import os
import threading
import time
import tomllib
import traceback
from pathlib import Path
from typing import Optional

import streamlit as st
from streamlit import config

from videocap.config import APP_NAME
from videocap.environment import is_streamlit_cloud_environment
from videocap.jobs import RUNNING
from videocap.textfiles import read_text_file
from videocap.ui.common import drop_expired_prepared_download
from videocap.ui.context import AppContext, build_context, init_session_state, remember_visitor, visitor_access
from videocap.ui.page_download import render_download_page
from videocap.ui.page_history import render_history_page
from videocap.ui.page_library import render_library_page
from videocap.ui.page_queue import render_queue_page
from videocap.ui.page_search import render_search_page
from videocap.ui.page_subscriptions import render_subscriptions_page
from videocap.ui.page_system import render_system_page
from videocap.ui.page_tools import render_tools_page
from videocap.ui.style import apply_custom_style


FAILED_LOGIN_DELAY_SECONDS = 1.0
FREE_LOGIN_ATTEMPTS = 5
# Doubles with every further wrong password, up to the maximum.
FIRST_LOGIN_LOCKOUT_SECONDS = 30
MAX_LOGIN_LOCKOUT_SECONDS = 5 * 60
# Well within the time after which a public host gives up on a visitor's jobs.
VISITOR_HEARTBEAT_SECONDS = 60
# Notepad and Windows PowerShell save UTF-8 with a BOM, which Streamlit keeps as part of the first key's name.
BOM_KEY = "\ufeffapp_password"
RECOVERY_NOTICE_KEY = "database_recovery_noticed"


class LoginThrottle:
    """Wrong passwords counted across every session. The delay above only holds back one browser tab, and a script
    can open as many sessions as it likes."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Visitor address ("" when unknown) -> wrong passwords in a row, and when the last one came.
        self._failures: dict[str, tuple[int, float]] = {}

    def seconds_to_wait(self, client: str) -> float:
        with self._lock:
            count, last = self._failures.get(client, (0, 0.0))
        if count < FREE_LOGIN_ATTEMPTS:
            return 0.0
        lockout = min(FIRST_LOGIN_LOCKOUT_SECONDS * 2.0 ** (count - FREE_LOGIN_ATTEMPTS), MAX_LOGIN_LOCKOUT_SECONDS)
        return max(last + lockout - time.time(), 0.0)

    def record_failure(self, client: str) -> None:
        now = time.time()
        with self._lock:
            # Addresses that stopped trying long ago would otherwise pile up for as long as the app runs.
            stale = [key for key, (_, last) in self._failures.items() if now - last > 2 * MAX_LOGIN_LOCKOUT_SECONDS]
            for key in stale:
                del self._failures[key]
            count, _ = self._failures.get(client, (0, 0.0))
            self._failures[client] = (count + 1, now)

    def forget(self, client: str) -> None:
        with self._lock:
            self._failures.pop(client, None)


LOGIN_THROTTLE = LoginThrottle()


def get_access_password() -> Optional[str]:
    """The configured password, "" when there is none, or None when the secrets file exists but cannot be read."""
    configured = os.getenv("VIDEOCAP_PASSWORD", "")
    if configured:
        return configured
    try:
        return str(st.secrets.get("app_password") or st.secrets.get(BOM_KEY) or "")
    except Exception:
        return read_password_from_secrets_files()


def read_password_from_secrets_files() -> Optional[str]:
    """Streamlit only reads secrets files saved as UTF-8, but Windows editors also save UTF-16 or Big5."""
    password = ""
    for name in config.get_option("secrets.files"):
        path = Path(name)
        if not path.exists():
            continue
        try:
            secrets = tomllib.loads(read_text_file(path))
        except (OSError, ValueError):
            # It may well hold the password, so a file that cannot be read keeps the app closed.
            return None
        # Like Streamlit, a later file overrides an earlier one.
        password = str(secrets.get("app_password", password))
    return password


def passes_access_gate() -> bool:
    """Asks for the shared password when one is configured, so a public address does not mean a public downloader."""
    password = get_access_password()
    if password is None:
        st.error("無法讀取 .streamlit/secrets.toml，App 暫停開放。請修正檔案格式後重新整理。")
        return False
    if not password or st.session_state.get("access_granted"):
        return True

    st.markdown(f"<div class='hero-title'>{APP_NAME}</div>", unsafe_allow_html=True)
    with st.form("access_form"):
        attempt = st.text_input("請輸入存取密碼", type="password")
        submitted = st.form_submit_button("進入", type="primary")
    if submitted:
        client = st.context.ip_address or ""
        wait = LOGIN_THROTTLE.seconds_to_wait(client)
        if wait > 0:
            st.error(f"嘗試錯誤太多次，請 {math.ceil(wait)} 秒後再試。")
            return False
        if hmac.compare_digest(attempt.encode("utf-8"), password.encode("utf-8")):
            LOGIN_THROTTLE.forget(client)
            st.session_state["access_granted"] = True
            st.rerun()
        LOGIN_THROTTLE.record_failure(client)
        # Slows down anyone trying passwords by script.
        time.sleep(FAILED_LOGIN_DELAY_SECONDS)
        st.error("密碼不正確。")
    return False


def keep_visitor_jobs_alive(ctx: AppContext) -> None:
    """A public host cancels the jobs of visitors who left. Any open page counts as still being there, including
    pages without a job list and pages whose jobs are all paused."""
    manager = ctx.services.manager
    manager.touch(ctx.owner)
    if ctx.trusted or not manager.unfinished_jobs(ctx.owner):
        return

    def heartbeat() -> None:
        manager.touch(ctx.owner)

    st.fragment(heartbeat, run_every=VISITOR_HEARTBEAT_SECONDS)()


def build_pages(ctx: AppContext) -> dict[str, st.Page]:
    def download_page() -> None:
        render_download_page(ctx)

    def queue_page() -> None:
        render_queue_page(ctx)

    def search_page() -> None:
        render_search_page(ctx)

    def library_page() -> None:
        render_library_page(ctx)

    def subscriptions_page() -> None:
        render_subscriptions_page(ctx)

    def history_page() -> None:
        render_history_page(ctx)

    def tools_page() -> None:
        render_tools_page(ctx)

    def system_page() -> None:
        render_system_page(ctx)

    running = sum(1 for job in ctx.services.manager.active_jobs(ctx.owner) if job.state == RUNNING)
    queue_title = f"佇列（{running}）" if running else "佇列"
    pages = {
        "download": st.Page(download_page, title="下載", icon=":material/download:", url_path="download", default=True),
        "queue": st.Page(queue_page, title=queue_title, icon=":material/format_list_bulleted:", url_path="queue"),
        "search": st.Page(search_page, title="搜尋", icon=":material/search:", url_path="search"),
    }
    if ctx.trusted and ctx.services.storage:
        pages["library"] = st.Page(library_page, title="媒體庫", icon=":material/video_library:", url_path="library")
        pages["subscriptions"] = st.Page(subscriptions_page, title="訂閱", icon=":material/subscriptions:", url_path="subscriptions")
        pages["history"] = st.Page(history_page, title="歷史", icon=":material/history:", url_path="history")
    pages["tools"] = st.Page(tools_page, title="工具", icon=":material/build:", url_path="tools")
    pages["system"] = st.Page(system_page, title="系統", icon=":material/settings:", url_path="system")
    return pages


def main() -> None:
    st.set_page_config(
        page_title=APP_NAME,
        page_icon="🎬",
        layout="wide",
        # Expanded on a desktop, collapsed on a phone, where it would cover the whole page.
        initial_sidebar_state="auto",
    )
    apply_custom_style()
    if not passes_access_gate():
        return

    # Streamlit's own reruns and stops are BaseExceptions, so they pass through.
    ctx: Optional[AppContext] = None
    try:
        # Starting the engine opens the database, which can fail too.
        ctx = build_context()
        init_session_state(ctx)
        remember_visitor(ctx)
        keep_visitor_jobs_alive(ctx)
        # Pages without a job list never drop an expired file themselves, and it holds server memory.
        drop_expired_prepared_download()
        show_database_recovery(ctx)
        ctx.pages = build_pages(ctx)
        page = st.navigation(list(ctx.pages.values()), position="top")
        page.run()
    except Exception as exc:
        report_page_error(ctx.trusted if ctx else visitor_access(is_streamlit_cloud_environment())[1], exc)


def show_database_recovery(ctx: AppContext) -> None:
    storage = ctx.services.storage
    # The path is this machine's, so only its owner sees it.
    if not ctx.trusted or storage is None or storage.recovered_from is None or st.session_state.get(RECOVERY_NOTICE_KEY):
        return
    st.session_state[RECOVERY_NOTICE_KEY] = True
    st.warning(f"資料庫檔案已損壞，已移到 `{storage.recovered_from}` 並建立新的資料庫；下載紀錄與訂閱需要重新設定。")


def report_page_error(trusted: bool, exc: Exception) -> None:
    # A traceback names this machine's folders, which a public visitor should not see.
    if trusted:
        st.exception(exc)
        return
    traceback.print_exception(exc)
    st.error("發生錯誤，請重新整理頁面再試一次。")

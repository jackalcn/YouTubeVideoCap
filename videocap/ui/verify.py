"""The window for passing a site's check for a person yourself: the button under a failure that asks for one, how the
window is doing, and the sites whose check was passed. See videocap.verification."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Optional

import streamlit as st

from videocap import impersonation, verification
from videocap.errors import HUMAN_CHECK, failure_kind
from videocap.options import network_options
from videocap.sites import host_of
from videocap.ui.context import AppContext, collect_settings


NOTICE_KEY = "verification_notice"
SYSTEM_URL_KEY = "verification_system_url"
OPEN_LABEL = "開啟瀏覽器視窗，自己通過驗證"
FINISH_LABEL = "我已通過驗證"
CANCEL_LABEL = "取消"
CLEAR_LABEL = "清除"
AWAY_FROM_THE_MACHINE = "要在執行 App 的這台電腦前操作，才能開啟瀏覽器視窗。"
INTRODUCTION = (
    "有些網站要求真人驗證（例如勾選「我不是機器人」）。App 不會代為通過驗證：按下按鈕會開一個瀏覽器視窗，"
    "你在裡面自己通過，App 再沿用通過後的連線資料（那個網站的 cookies 和瀏覽器的自我介紹）去解析和下載。"
    "視窗用全新的設定檔，不會讀你平常瀏覽器的任何資料；留下的資料只存在記憶體，關閉 App 就清掉。"
    "網站會在自己設定的時間後讓通過的驗證失效（常見是 30 分鐘左右），失效時再通過一次即可。"
)
NEEDS_IMPERSONATION = (
    "通過驗證後，要靠上面的「模擬瀏覽器」才能沿用：網站認得的不只是 cookies，還有連線的方式。"
    "它現在還不能用，請先安裝或啟用（已經安裝的話，重新啟動 App）。"
)


def asks_for_a_person(message: str) -> bool:
    """Whether a failure says that a site wants a person to pass its check."""
    return failure_kind(message) == HUMAN_CHECK


def can_pass_checks(ctx: AppContext) -> bool:
    # The window opens on this machine's own screen, so it is for its owner, sitting at it.
    return ctx.trusted and ctx.browser_login_allowed


def open_window(url: str, proxy: str = "") -> None:
    try:
        verification.start_window(url, proxy=proxy)
    except verification.VerificationUnavailable as error:
        st.session_state[NOTICE_KEY] = str(error)


def minutes_left(seconds: int) -> str:
    return f"{max(math.ceil(seconds / 60), 1)} 分鐘"


def describe(status: verification.WindowStatus) -> str:
    lines = [status.message]
    if status.state == verification.WAITING:
        if status.page_title:
            lines.append(f"視窗目前顯示：{status.page_title}")
        lines.append(f"最多再等 {minutes_left(status.seconds_left)}。如果網站的驗證不是勾選框，也沒有自動偵測到，通過後請按「{FINISH_LABEL}」。")
    return "\n\n".join(lines)


def _window_fragment(key: str) -> None:
    window = verification.current_window()
    status = window.status() if window else None
    if window is None or status is None or not status.active:
        # It ended while this waited to be drawn again: the page itself says how.
        st.rerun(scope="app")
        return
    # Plain text: the page's title is the site's, and is not to be read as formatting.
    st.info(describe(status), icon=":material/open_in_new:")
    with st.container(horizontal=True, gap="small"):
        st.button(FINISH_LABEL, key=f"verify_finish_{key}", on_click=window.finish, width="stretch")
        st.button(CANCEL_LABEL, key=f"verify_cancel_{key}", on_click=window.cancel, width="stretch")


def render_site_verification(ctx: AppContext, url: str, key: str) -> None:
    """Under a failure that says a site wants a person: opens a browser window for the owner to pass the check in, and
    shows how it goes. `key` tells this place from the others on the page."""
    if not url or not ctx.trusted:
        return
    if not can_pass_checks(ctx):
        st.caption(AWAY_FROM_THE_MACHINE)
        return
    notice = st.session_state.pop(NOTICE_KEY, None)
    if notice:
        st.warning(notice)
    window = verification.current_window()
    status = window.status() if window else None
    host = host_of(verification.opening_address(url))
    if status is not None and status.active:
        if status.host == host:
            st.fragment(_window_fragment, run_every="2s")(key)
        else:
            st.caption(f"已經有一個驗證視窗開著（{status.host}），請先完成或取消它。")
        return
    reason = verification.unavailable_reason()
    if reason:
        st.caption(reason)
        return
    if status is not None and status.host == host:
        (st.success if status.state == verification.PASSED else st.warning)(status.message)
    # The check is passed from the address the site sees, so the window goes the way the app's own requests do.
    proxy = network_options(collect_settings(), ctx.limits).proxy
    st.button(OPEN_LABEL, key=f"verify_open_{key}", on_click=open_window, args=(url, proxy), icon=":material/open_in_new:")


def describe_session(session: verification.VerifiedSession) -> str:
    # When it was passed, not when its cookie ends: a site's clearance cookie lasts a year, and the site stops
    # accepting it long before.
    passed_at = f"{datetime.fromtimestamp(session.captured_at):%m/%d %H:%M} 通過" if session.captured_at else "已通過"
    return f"{session.host}（{session.browser or '瀏覽器'}，{passed_at}）"


def forget_site(host: str) -> None:
    verification.verified_sites.forget(host)


def render_site_checks(ctx: AppContext) -> None:
    """On the 系統 page: the sites whose check was passed, and a window to pass one before any link is pasted."""
    if not ctx.trusted:
        return
    st.markdown("### 網站驗證")
    st.caption(INTRODUCTION)
    if not can_pass_checks(ctx):
        st.caption(AWAY_FROM_THE_MACHINE)
        return
    if not impersonation.ready():
        st.warning(NEEDS_IMPERSONATION)
    sessions = verification.verified_sites.all()
    if sessions:
        st.write("已通過驗證的網站：")
        for session in sessions:
            columns = st.columns([4, 1], vertical_alignment="center")
            columns[0].write(describe_session(session))
            columns[1].button(CLEAR_LABEL, key=f"verify_forget_{session.host}", on_click=forget_site, args=(session.host,), width="stretch")
    else:
        st.caption("目前沒有已通過驗證的網站。")
    typed: Optional[str] = st.text_input("網站網址", key=SYSTEM_URL_KEY, placeholder="https://example.com/", help="要先通過驗證的網站或影片網頁。")
    address = (typed or "").strip()
    if address.startswith(("http://", "https://")) and host_of(address):
        render_site_verification(ctx, address, key="system")

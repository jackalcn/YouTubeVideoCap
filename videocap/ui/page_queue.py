from __future__ import annotations

from datetime import time as dt_time

import streamlit as st

from videocap.config import LOCAL_MAX_CONCURRENT_JOBS
from videocap.jobs import ACTIVE_STATES, FINISHED_STATES, PAUSED, Job
from videocap.services import KIND_TOOL, WINDOW_HOLD_PREFIX
from videocap.storage import MAX_CONCURRENT_KEY, DownloadWindow
from videocap.ui.common import (
    FORGET_NOTICE_KEY,
    page_header,
    render_active_jobs,
    render_forget_visitor,
    render_job_card,
    render_job_logs,
    render_job_result,
    render_notify_toggle,
    render_restart_notice,
    resume_job,
)
from videocap.ui.context import AppContext


FINISHED_PAGE_SIZE = 10
WINDOW_ENABLED_KEY = "download_window_enabled"
WINDOW_START_KEY = "download_window_start"
WINDOW_END_KEY = "download_window_end"


def show_more_finished() -> None:
    st.session_state["queue_finished_shown"] = st.session_state.get("queue_finished_shown", FINISHED_PAGE_SIZE) + FINISHED_PAGE_SIZE


def set_concurrency(ctx: AppContext) -> None:
    value = int(st.session_state["queue_concurrency"])
    ctx.services.manager.set_max_concurrent(value)
    if ctx.services.storage:
        ctx.services.storage.set_value(MAX_CONCURRENT_KEY, value)


def render_queue_toolbar(ctx: AppContext, jobs: list[Job]) -> None:
    manager = ctx.services.manager
    columns = st.columns([2, 3], vertical_alignment="bottom")
    with columns[0]:
        if ctx.trusted:
            st.session_state.setdefault("queue_concurrency", manager.max_concurrent)
            st.slider(
                "同時下載數",
                min_value=1,
                max_value=LOCAL_MAX_CONCURRENT_JOBS,
                key="queue_concurrency",
                on_change=set_concurrency,
                args=(ctx,),
                help="使用帳號登入的任務一律逐一執行。",
            )
        render_notify_toggle()
    # Greyed out when there is nothing to act on, so a press never seems to do nothing. A row that wraps rather than
    # columns, which a phone would stack into three full-width buttons.
    states = {job.state for job in jobs}
    with columns[1], st.container(horizontal=True, gap="small"):
        if st.button(
            "全部暫停",
            width="stretch",
            disabled=not states & ACTIVE_STATES,
            help="轉檔與壓縮等工具任務無法暫停，會繼續處理。",
        ):
            for job in manager.active_jobs(ctx.owner):
                # A paused conversion would throw away its work and start over.
                if job.kind != KIND_TOOL:
                    manager.pause(job.id)
            st.rerun()
        # During a rate-limit pause the waiting jobs are held without being paused, and this lets them go too.
        rate_limited = ctx.trusted and ctx.services.rate_limit_until() is not None
        if st.button("全部繼續", width="stretch", disabled=PAUSED not in states and not rate_limited):
            for job in manager.list_jobs(ctx.owner):
                if job.state == PAUSED:
                    resume_job(job, ctx)
            if rate_limited:
                ctx.services.end_rate_limit_pause(ctx.limits)
            st.rerun()
        if st.button("清除已結束", width="stretch", disabled=not states & FINISHED_STATES):
            manager.clear_finished(ctx.owner)
            st.rerun()


def chosen_clock(key: str, saved: str) -> str:
    value = st.session_state.get(key)
    return value.strftime("%H:%M") if isinstance(value, dt_time) else saved


def save_download_window(ctx: AppContext) -> None:
    saved = ctx.services.download_window
    window = DownloadWindow(
        enabled=bool(st.session_state.get(WINDOW_ENABLED_KEY)),
        start=chosen_clock(WINDOW_START_KEY, saved.start),
        end=chosen_clock(WINDOW_END_KEY, saved.end),
    )
    ctx.services.save_download_window(window, ctx.limits)


def render_download_window(ctx: AppContext) -> None:
    # The hours belong to this machine's network, so only its owner sets them; visitors' jobs never wait for them.
    if not ctx.trusted or not ctx.services.storage:
        return
    window = ctx.services.download_window
    st.session_state.setdefault(WINDOW_ENABLED_KEY, window.enabled)
    st.session_state.setdefault(WINDOW_START_KEY, dt_time.fromisoformat(window.start))
    st.session_state.setdefault(WINDOW_END_KEY, dt_time.fromisoformat(window.end))
    with st.expander("下載時段"):
        st.toggle("只在這段時間下載", key=WINDOW_ENABLED_KEY, on_change=save_download_window, args=(ctx,))
        disabled = not st.session_state[WINDOW_ENABLED_KEY]
        # Two columns, which a phone stacks into two full-width fields.
        start_column, end_column = st.columns(2)
        with start_column:
            st.time_input("開始", key=WINDOW_START_KEY, on_change=save_download_window, args=(ctx,), disabled=disabled)
        with end_column:
            st.time_input("結束", key=WINDOW_END_KEY, on_change=save_download_window, args=(ctx,), disabled=disabled)
        st.caption(
            "時間以這台電腦的時鐘為準，可以跨過午夜（例如 22:00 到 06:00）。時段外，新的下載會排隊等候，自動的訂閱檢查也會延後，"
            "到時段開始才進行；已經在下載的不會中斷，轉檔等工具任務也不受影響。"
        )


def render_window_notice(ctx: AppContext, jobs: list[Job]) -> None:
    held = [job for job in jobs if ctx.services.hold_reason(job).startswith(WINDOW_HOLD_PREFIX)]
    if held:
        window = ctx.services.download_window
        st.info(
            f"現在不在下載時段（{window.label}），{len(held)} 項下載會在 {window.start} 自動開始。",
            icon=":material/schedule:",
        )


def render_visitor_privacy(ctx: AppContext) -> None:
    notice = st.session_state.pop(FORGET_NOTICE_KEY, None)
    if notice:
        st.success(notice)
    st.caption("這個公開主機上的任務只有你看得到，離開頁面約 5 分鐘後會自動取消，完成的檔案暫存約兩小時。")
    render_forget_visitor(ctx)


def render_queue_page(ctx: AppContext) -> None:
    page_header("下載佇列", "任務在背景執行，切換頁面或重新整理都不會中斷。")
    manager = ctx.services.manager
    render_restart_notice(ctx)
    jobs = manager.list_jobs(ctx.owner)
    render_window_notice(ctx, jobs)
    render_queue_toolbar(ctx, jobs)
    render_download_window(ctx)
    if not ctx.trusted:
        render_visitor_privacy(ctx)

    if not jobs:
        st.info("佇列是空的。到「下載」頁貼上 YouTube 連結並按「開始下載」，任務就會出現在這裡。", icon=":material/inbox:")
        if "download" in ctx.pages:
            st.page_link(ctx.pages["download"], label="前往下載頁", icon=":material/download:")
    else:
        st.markdown("### 進行中")
    render_active_jobs(ctx, allow_reorder=True)
    if jobs and not manager.active_jobs(ctx.owner):
        st.caption("目前沒有進行中的任務。")

    paused = [job for job in jobs if job.state == PAUSED]
    if paused:
        st.markdown(f"### 已暫停（{len(paused)}）")
        for job in paused:
            render_job_card(job, ctx, section="paused")

    finished = [job for job in jobs if job.state in FINISHED_STATES]
    if finished:
        st.markdown(f"### 已結束（{len(finished)}）")
        # Up to a hundred finished jobs are kept, and drawing all of them would slow every click on this page.
        shown = st.session_state.get("queue_finished_shown", FINISHED_PAGE_SIZE)
        for job in list(reversed(finished))[:shown]:
            render_job_card(job, ctx, section="finished")
            # Drawn only when opened, for the same reason: a playlist's result holds a button per file.
            expander = st.expander("結果與紀錄", key=f"queue_result_{job.id}", on_change="rerun")
            if not expander.open:
                continue
            with expander:
                render_job_result(job, ctx)
                # yt-dlp's log names the server's folders, which only this machine's owner should see.
                if ctx.trusted:
                    st.caption("詳細紀錄")
                    render_job_logs(job, ctx)
                if job.state not in ACTIVE_STATES and st.button("從清單移除", key=f"remove_{job.id}", width="stretch"):
                    manager.remove(job.id)
                    st.rerun()
        if len(finished) > shown:
            st.button(
                f"顯示更早的 {min(FINISHED_PAGE_SIZE, len(finished) - shown)} 項",
                width="stretch",
                on_click=show_more_finished,
            )

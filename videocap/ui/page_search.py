from __future__ import annotations

from typing import Any

import streamlit as st

from videocap.config import SEARCH_MAX_RESULTS
from videocap.errors import humanize_ytdlp_error
from videocap.inspector import SearchResult, search_youtube
from videocap.options import LoginSource, network_options
from videocap.services import temporary_cookie_file
from videocap.ui.common import escape_markdown, page_header, public_request_slot, render_active_jobs, render_low_disk_notice, submit_downloads
from videocap.ui.context import AppContext, collect_settings, current_browser, current_cookie_bytes
from videocap.ui.sidebar import render_settings_sidebar


def run_search(ctx: AppContext, query: str, count: int) -> bool:
    """Returns whether the search reached YouTube, so a failure is not reported as "no results"."""
    with public_request_slot(ctx) as allowed:
        if not allowed:
            return False
        with st.spinner("搜尋中..."), temporary_cookie_file(current_cookie_bytes()) as cookie_file:
            try:
                st.session_state["search_results"] = search_youtube(
                    query, count, LoginSource(cookie_file, current_browser()), network=network_options(collect_settings(), ctx.limits)
                )
            except Exception as exc:
                st.session_state["search_results"] = []
                st.error(escape_markdown(humanize_ytdlp_error(str(exc))))
                return False
    return True


def send_to_download_page(ctx: AppContext, urls: list[str]) -> None:
    existing = str(st.session_state.get("keep_url_input") or "").strip()
    st.session_state["keep_url_input"] = "\n".join(part for part in [existing, *urls] if part)
    st.switch_page(ctx.pages["download"])


def result_rows(results: list[SearchResult]) -> list[dict[str, Any]]:
    return [
        {
            "選取": False,
            "封面": result["thumbnail"],
            "標題": result["title"],
            "頻道": result["channel"],
            "長度": result["duration"],
            "觀看次數": result["views"],
            "連結": result["url"],
        }
        for result in results
    ]


def render_search_page(ctx: AppContext) -> None:
    render_settings_sidebar(ctx)
    page_header("搜尋 YouTube", "在這裡找影片，勾選後用左側設定下載。")
    render_low_disk_notice()

    with st.form("search_form"):
        query_column, count_column = st.columns([5, 1])
        query = query_column.text_input("關鍵字", key="keep_search_query")
        count = count_column.number_input("筆數", min_value=1, max_value=SEARCH_MAX_RESULTS, value=15)
        submitted = st.form_submit_button("搜尋", type="primary", width="stretch")
    searched = False
    if submitted and not query.strip():
        st.warning("請輸入關鍵字。")
    elif submitted:
        searched = run_search(ctx, query.strip(), int(count))
        # Row checkboxes from the previous search would apply to different videos.
        st.session_state["search_generation"] = st.session_state.get("search_generation", 0) + 1

    results: list[SearchResult] = st.session_state.get("search_results", [])
    if results:
        edited = st.data_editor(
            result_rows(results),
            hide_index=True,
            width="stretch",
            height=min(90 * (len(results) + 1), 640),
            row_height=80,
            disabled=["封面", "標題", "頻道", "長度", "觀看次數", "連結"],
            key=f"search_editor_{st.session_state.get('search_generation', 0)}",
            column_config={
                "選取": st.column_config.CheckboxColumn(width=60),
                "封面": st.column_config.ImageColumn(width=140),
                "頻道": st.column_config.TextColumn(width=130),
                "長度": st.column_config.TextColumn(width=70),
                "觀看次數": st.column_config.NumberColumn(format="localized", width=100),
                "連結": st.column_config.LinkColumn(display_text="開啟", width=60),
            },
        )
        chosen = [row["連結"] for row in edited if row["選取"]]
        columns = st.columns(2)
        if columns[0].button(f"下載勾選的 {len(chosen)} 部", type="primary", width="stretch", disabled=not chosen):
            jobs = submit_downloads(ctx, chosen)
            if jobs:
                st.success(f"已加入佇列：{len(jobs)} 項。")
        if columns[1].button("送到下載頁再決定", width="stretch", disabled=not chosen):
            send_to_download_page(ctx, chosen)
    elif searched:
        st.info("沒有找到符合的影片。")

    render_active_jobs(ctx)

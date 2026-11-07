from __future__ import annotations

import hashlib
from datetime import datetime, tzinfo
from functools import partial
from pathlib import Path
from typing import Any, Optional

import streamlit as st

from videocap.inspector import rows_to_csv
from videocap.storage import AppStorage, HistoryEntry
from videocap.tooling import format_bytes, format_duration
from videocap.ui.common import format_local_time, page_header, render_active_jobs, render_low_disk_notice, submit_downloads, viewer_timezone
from videocap.ui.context import AppContext
from videocap.ui.sidebar import render_settings_sidebar


HISTORY_TABLE_LIMIT = 1000


def history_rows(entries: list[HistoryEntry]) -> list[dict[str, Any]]:
    return [
        {
            "標題": entry.title,
            "頻道": entry.channel,
            "規格": entry.quality,
            "大小": format_bytes(entry.size_bytes) if entry.size_bytes else "",
            "長度": format_duration(entry.duration),
            "下載時間": format_local_time(entry.finished_at),
            "檔案還在": "是" if entry.path and Path(entry.path).exists() else "否",
            "網址": entry.url,
        }
        for entry in entries
    ]


def export_rows(entries: list[HistoryEntry], zone: Optional[tzinfo] = None) -> list[dict[str, Any]]:
    return [
        {
            "標題": entry.title,
            "頻道": entry.channel,
            "影片ID": entry.video_id,
            "網址": entry.url,
            "規格": entry.quality,
            "位元組": entry.size_bytes,
            "秒數": entry.duration,
            "上傳日期": entry.upload_date,
            "下載時間": datetime.fromtimestamp(entry.finished_at, zone).strftime("%Y-%m-%d %H:%M:%S"),
            "路徑": entry.path,
        }
        for entry in entries
    ]


def history_csv(storage: AppStorage, search: str, zone: Optional[tzinfo] = None) -> bytes:
    # The table stops at HISTORY_TABLE_LIMIT rows, but an export is often a backup and must hold every record.
    count, _ = storage.history_totals()
    return rows_to_csv(export_rows(storage.list_history(search, limit=max(count, 1)), zone))


@st.dialog("清除下載歷史")
def confirm_clear(storage: AppStorage) -> None:
    st.write("確定要清除全部下載歷史嗎？檔案本身不會被刪除。")
    if st.button("確定清除", type="primary", width="stretch"):
        storage.clear_history()
        st.rerun()


@st.dialog("重置已下載紀錄")
def confirm_clear_archive(storage: AppStorage) -> None:
    st.write("「略過已下載過的影片」和訂閱都靠這份紀錄判斷。重置後，之前抓過的影片可能會再下載一次。")
    if st.button("確定重置", type="primary", width="stretch"):
        storage.clear_archive()
        st.rerun()


def render_history_page(ctx: AppContext) -> None:
    storage = ctx.services.storage
    if storage is None:
        return
    render_settings_sidebar(ctx)
    page_header("下載歷史", "記錄每一次完成的下載，可以搜尋、匯出或重新下載。")
    render_low_disk_notice()

    count, total_size = storage.history_totals()
    st.caption(f"累計 {count} 個項目，共 {format_bytes(total_size)}；已下載紀錄 {len(storage.archived_video_ids())} 部。")

    search = st.text_input("搜尋標題、頻道或影片 ID", key="keep_history_search")
    entries = storage.list_history(search, limit=HISTORY_TABLE_LIMIT)
    if not entries:
        st.caption("沒有符合的紀錄。" if search.strip() else "還沒有下載紀錄。")
        render_active_jobs(ctx)
        return
    if len(entries) >= HISTORY_TABLE_LIMIT:
        st.caption(f"只顯示最新 {HISTORY_TABLE_LIMIT} 筆，可用搜尋縮小範圍；匯出 CSV 會包含全部符合的紀錄。")

    # Streamlit keeps a table's ticks as row numbers. After a delete, a search or a finished download the same numbers
    # would point at other records, so a different list gets a table of its own with nothing ticked.
    shown_ids = hashlib.sha1(",".join(str(entry.id) for entry in entries).encode()).hexdigest()[:16]
    selection = st.dataframe(
        history_rows(entries),
        hide_index=True,
        width="stretch",
        height=min(36 * (len(entries) + 1), 480),
        on_select="rerun",
        selection_mode="multi-row",
        key=f"history_table_{shown_ids}",
        column_config={"網址": st.column_config.LinkColumn(display_text="開啟")},
    )
    chosen = [entries[index] for index in selection.selection.rows if index < len(entries)]

    columns = st.columns(4)
    if columns[0].button(f"重新下載 {len(chosen)} 項", type="primary", width="stretch", disabled=not chosen):
        urls = list(dict.fromkeys(entry.url for entry in chosen if entry.url))
        if submit_downloads(ctx, urls):
            st.toast(f"已加入佇列：{len(urls)} 項。", icon=":material/queue:")
    if columns[1].button(f"刪除 {len(chosen)} 筆紀錄", width="stretch", disabled=not chosen):
        storage.delete_history(entry.id for entry in chosen)
        st.rerun()
    columns[2].download_button(
        "匯出 CSV",
        # Built only when clicked, since it reads every matching record. That runs outside the page, where the
        # browser's time zone is no longer known.
        data=partial(history_csv, storage, search, viewer_timezone()),
        file_name="download_history.csv",
        mime="text/csv",
        width="stretch",
        on_click="ignore",
    )
    if columns[3].button("清除全部歷史", width="stretch"):
        confirm_clear(storage)

    if st.button("重置已下載紀錄（略過與訂閱用）", width="stretch"):
        confirm_clear_archive(storage)

    render_active_jobs(ctx)

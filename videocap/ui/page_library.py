from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any

import streamlit as st
from streamlit.errors import StreamlitAPIException

from videocap.config import PREVIEW_MAX_BYTES
from videocap.jobs import FINISHED_STATES
from videocap.library import (
    BROWSER_PLAYABLE_EXTENSIONS,
    SUBTITLE_EXTENSIONS,
    THUMBNAIL_EXTENSIONS,
    LibraryEntry,
    LibraryScan,
    delete_entry,
    describe_probe,
    folder_creation_problem,
    is_inside,
    open_in_file_manager,
    probe_media,
    recycle_bin_available,
    scan_folder,
)
from videocap.subtitles import READABLE_SUBTITLE_EXTENSIONS, Cue, cues_to_text, cues_to_vtt, read_subtitle_file, search_cues
from videocap.tooling import format_bytes, get_free_disk_bytes
from videocap.ui.common import (
    BUNDLE_MAX_BYTES,
    escape_markdown,
    format_local_time,
    page_header,
    render_bundle_download_button,
    render_file_download_button,
)
from videocap.ui.context import OUTPUT_DIR_WIDGET_KEY, AppContext, current_output_dir


KIND_FILTERS = {"all": "全部", "video": "影片", "audio": "音訊"}
SORT_OPTIONS = {"newest": "最新", "name": "名稱", "largest": "檔案最大"}
TRANSCRIPT_PREVIEW_CHARS = 20000
SCAN_CACHE_KEY = "library_scan"
SCAN_GENERATION_KEY = "library_scan_generation"
# Typing a search or sorting reuses the last scan for this long; a big library takes seconds to walk.
SCAN_CACHE_SECONDS = 30
PARTIAL_NAMES_SHOWN = 20


def filter_entries(entries: list[LibraryEntry], search: str, kind: str, sort: str) -> list[LibraryEntry]:
    needle = search.strip().lower()
    chosen = [
        entry
        for entry in entries
        if (kind == "all" or entry.kind == kind) and (not needle or needle in entry.path.name.lower())
    ]
    if sort == "name":
        return sorted(chosen, key=lambda entry: entry.path.name.lower())
    if sort == "largest":
        return sorted(chosen, key=lambda entry: entry.size, reverse=True)
    return chosen


def entry_rows(entries: list[LibraryEntry], root: Path) -> list[dict[str, Any]]:
    return [
        {
            "檔名": entry.path.name,
            "類型": "影片" if entry.kind == "video" else "音訊",
            "大小": format_bytes(entry.size),
            "字幕": len(entry.subtitles),
            "修改時間": format_local_time(entry.modified_at, "%Y-%m-%d %H:%M"),
            "資料夾": str(entry.path.parent.relative_to(root)) if entry.path.parent != root else "",
        }
        for entry in entries
    ]


def forget_scan() -> None:
    st.session_state[SCAN_GENERATION_KEY] = st.session_state.get(SCAN_GENERATION_KEY, 0) + 1


def load_scan(ctx: AppContext, root: Path) -> LibraryScan:
    """Scans again after 重新整理, a delete, a finished job or a short while; otherwise reuses the last scan."""
    finished = sum(job.state in FINISHED_STATES for job in ctx.services.manager.list_jobs())
    key = (str(root), st.session_state.get(SCAN_GENERATION_KEY, 0), finished)
    cached = st.session_state.get(SCAN_CACHE_KEY)
    if cached and cached[0] == key and time.monotonic() - cached[1] < SCAN_CACHE_SECONDS:
        return cached[2]
    scan = scan_folder(root)
    st.session_state[SCAN_CACHE_KEY] = (key, time.monotonic(), scan)
    return scan


@st.dialog("刪除檔案")
def confirm_delete(entry: LibraryEntry, root: Path) -> None:
    if recycle_bin_available(entry.path.parent):
        where = "檔案會移到回收桶，需要時可以從回收桶還原。"
    else:
        where = "這個位置沒有回收桶，檔案會永久刪除，無法復原。"
    st.write(f"確定要刪除「{escape_markdown(entry.path.name)}」嗎？{where}")
    include_sidecars = False
    if entry.sidecars:
        include_sidecars = st.checkbox(f"一併刪除 {len(entry.sidecars)} 個字幕與附加檔案", value=True)
    if st.button("確定刪除", type="primary", width="stretch"):
        result = delete_entry(entry, root, include_sidecars)
        notice = f"「{escape_markdown(entry.path.name)}」{result.outcome}。"
        if result:
            notice = "；".join(result) + (f"。其餘檔案{result.outcome}。" if result.trashed or result.removed else "")
        st.session_state["library_notice"] = notice
        forget_scan()
        st.rerun()


def preview_subtitles(entry: LibraryEntry) -> dict[str, bytes]:
    """Converted here rather than by Streamlit, which only reads UTF-8 and stops the page on anything else."""
    subtitles: dict[str, bytes] = {}
    for path in entry.subtitles:
        if path.suffix.lower() not in READABLE_SUBTITLE_EXTENSIONS:
            continue
        try:
            cues = read_subtitle_file(path)
        except OSError:
            continue
        if cues:
            subtitles[path.name.removeprefix(f"{entry.path.stem}.")] = cues_to_vtt(cues).encode("utf-8")
    return subtitles


def render_media_preview(entry: LibraryEntry) -> None:
    if entry.path.suffix.lower() not in BROWSER_PLAYABLE_EXTENSIONS:
        st.caption(f"瀏覽器無法播放 {entry.path.suffix} 檔案，請用「開啟檔案」。")
        return
    if entry.size > PREVIEW_MAX_BYTES:
        st.caption(f"檔案超過 {format_bytes(PREVIEW_MAX_BYTES)}，預覽會佔用大量記憶體，請用「開啟檔案」。")
        return

    # The file only loads into memory when someone asks to watch it.
    if st.session_state.get("library_preview_path") != str(entry.path):
        if st.button("預覽", icon=":material/play_circle:", key="library_preview", width="stretch"):
            st.session_state["library_preview_path"] = str(entry.path)
            st.rerun()
        return

    if entry.kind != "video":
        st.audio(str(entry.path))
        return
    subtitles = preview_subtitles(entry)
    try:
        st.video(str(entry.path), subtitles=subtitles or None)
    except StreamlitAPIException:
        st.video(str(entry.path))
        st.caption("字幕檔無法讀取，預覽時不顯示字幕。")


@st.cache_data(max_entries=8, show_spinner=False)
def load_transcript(path: str, modified_at: float, size: int) -> tuple[list[Cue], str]:
    """Kept between reruns, because a long video's captions take a moment to read. The time and size in the key make
    a changed file read again."""
    cues = read_subtitle_file(Path(path))
    return cues, cues_to_text(cues, with_timestamps=True)


@st.fragment
def render_transcript_reader(entry: LibraryEntry) -> None:
    # A fragment, so picking a subtitle or searching in it does not reload the preview above.
    readable = [path for path in entry.subtitles if path.suffix.lower() in READABLE_SUBTITLE_EXTENSIONS]
    if not readable:
        st.caption("這些字幕的格式無法在這裡閱讀，可以下載後用播放器開啟。")
        return
    chosen = st.selectbox("閱讀字幕", options=readable, format_func=lambda path: path.name, key="library_transcript_choice")
    try:
        stat = chosen.stat()
        cues, text = load_transcript(str(chosen), stat.st_mtime, stat.st_size)
    except OSError as exc:
        st.error(escape_markdown(str(exc)))
        return
    if not text:
        st.caption("這個字幕檔裡沒有讀到文字。")
        return
    keyword = st.text_input("在字幕中搜尋", key="library_transcript_search")
    if keyword.strip():
        matches = search_cues(cues, keyword)
        st.caption(f"找到 {len(matches)} 句。")
        st.text("\n".join(matches)[:TRANSCRIPT_PREVIEW_CHARS])
    else:
        st.text(text[:TRANSCRIPT_PREVIEW_CHARS])


def sidecar_kind(entry: LibraryEntry, path: Path) -> str:
    """What a file beside a video is, in words, since names like "clip.live_chat.json" mean little to most people."""
    rest = path.name[len(entry.path.stem) :].lower() if path.name.startswith(entry.path.stem) else path.name.lower()
    suffix = path.suffix.lower()
    if rest == ".comments.csv":
        return "留言 CSV"
    if rest in (".live_chat.csv", ".live_chat.json"):
        return "聊天室紀錄"
    if suffix == ".nfo":
        return "NFO 資訊檔"
    if rest.startswith("-thumb.") and suffix in THUMBNAIL_EXTENSIONS:
        return "縮圖"
    if rest.endswith(".info.json"):
        return "完整資訊"
    if suffix == ".description":
        return "影片說明"
    if suffix in SUBTITLE_EXTENSIONS:
        return "字幕"
    if suffix in THUMBNAIL_EXTENSIONS:
        return "封面"
    if suffix == ".txt":
        return "逐字稿"
    return "附加檔案"


def render_sidecars(ctx: AppContext, entry: LibraryEntry) -> None:
    if not entry.sidecars:
        return
    with st.expander(f"字幕與附加檔案（{len(entry.sidecars)}）"):
        for index, path in enumerate(entry.sidecars):
            render_file_download_button(path, f"library_sidecar_{index}", ctx, kind=sidecar_kind(entry, path))
        if entry.subtitles:
            render_transcript_reader(entry)


def render_entry_details(ctx: AppContext, entry: LibraryEntry, root: Path) -> None:
    with st.container(border=True):
        st.markdown(f"**{escape_markdown(entry.path.name)}**")
        st.caption(f"{format_bytes(entry.size)} · {escape_markdown(str(entry.path.parent))}")
        details = st.session_state.get("library_probe_details")
        if details and details[0] == str(entry.path):
            st.caption(" · ".join(details[1]))
        else:
            probe = probe_media(entry.path)
            if probe:
                described = describe_probe(probe)
                st.session_state["library_probe_details"] = (str(entry.path), described)
                st.caption(" · ".join(described))

        # The buttons come first, so a preview that fails can never hide 刪除.
        columns = st.columns(4)
        if columns[0].button("開啟檔案", key="library_open_file", width="stretch", disabled=not ctx.can_open_files):
            problem = open_in_file_manager(entry.path)
            if problem:
                st.error(problem)
        if columns[1].button("開啟資料夾", key="library_open_folder", width="stretch", disabled=not ctx.can_open_files):
            problem = open_in_file_manager(entry.path.parent)
            if problem:
                st.error(problem)
        if columns[2].button("送到工具", key="library_send_tool", width="stretch"):
            st.session_state["keep_tool_source"] = str(entry.path)
            st.switch_page(ctx.pages["tools"])
        if columns[3].button("刪除", key="library_delete", width="stretch"):
            confirm_delete(entry, root)

        render_media_preview(entry)
        render_file_download_button(entry.path, "library_download", ctx)
        render_sidecars(ctx, entry)


def render_partial_cleanup(ctx: AppContext, root: Path, scan: LibraryScan) -> None:
    busy = bool(ctx.services.manager.unfinished_jobs())
    # A running or paused download may still merge its streams, so they are offered only when nothing is unfinished.
    partial_files = scan.partial_files if busy else {**scan.partial_files, **scan.orphan_streams}
    if not partial_files:
        return
    with st.expander(f"未完成的暫存檔：{len(partial_files)} 個，共 {format_bytes(sum(partial_files.values()))}"):
        st.caption("這些是中斷的下載或處理留下的暫存檔。繼續下載時會用到，所以有未完成的任務時無法清除。")
        names = [str(path.relative_to(root)) for path in sorted(partial_files)]
        listed = "\n".join(names[:PARTIAL_NAMES_SHOWN])
        more = f"\n…還有 {len(names) - PARTIAL_NAMES_SHOWN} 個" if len(names) > PARTIAL_NAMES_SHOWN else ""
        st.text(listed + more)
        # Checked again on the click, because a job may have started since the button was drawn.
        if st.button("清除暫存檔", key="library_clean_partial", disabled=busy, width="stretch") and not busy:
            problems: list[str] = []
            for path in partial_files:
                if not is_inside(path, root):
                    continue
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    # Usually another program still has it open; the rest can still go.
                    problems.append(path.name)
            if problems:
                st.session_state["library_notice"] = f"有 {len(problems)} 個暫存檔正在使用中，沒有刪除：{'、'.join(problems[:5])}"
            forget_scan()
            st.rerun()


def render_library_page(ctx: AppContext) -> None:
    page_header("媒體庫", "瀏覽下載資料夾裡的檔案，可以預覽、讀字幕、整理或刪除。")

    root_column, refresh_column = st.columns([5, 1], vertical_alignment="bottom")
    root_column.text_input("資料夾", key=OUTPUT_DIR_WIDGET_KEY)
    refresh_column.button("重新整理", width="stretch", on_click=forget_scan)
    root = current_output_dir(ctx)
    if not root.is_dir():
        problem = folder_creation_problem(root)
        if problem:
            st.error(escape_markdown(problem))
        else:
            st.info("這個資料夾還不存在，下載第一個檔案時會自動建立。")
        return

    notice = st.session_state.pop("library_notice", None)
    if notice:
        st.success(notice)

    scan = load_scan(ctx, root)
    entries = scan.entries
    free = get_free_disk_bytes(root)
    summary = f"{len(entries)} 個檔案，共 {format_bytes(sum(entry.size for entry in entries))}"
    if scan.media_count > len(entries):
        summary = f"共 {scan.media_count} 個檔案，只列出最新的 {len(entries)} 個（{format_bytes(sum(entry.size for entry in entries))}）"
    if free is not None:
        summary += f"；硬碟剩餘 {format_bytes(free)}"
    st.caption(summary)

    columns = st.columns([3, 1, 1])
    search = columns[0].text_input("搜尋檔名", key="keep_library_search")
    kind = columns[1].selectbox("類型", options=list(KIND_FILTERS), format_func=KIND_FILTERS.get, key="keep_library_kind")
    sort = columns[2].selectbox("排序", options=list(SORT_OPTIONS), format_func=SORT_OPTIONS.get, key="keep_library_sort")
    shown = filter_entries(entries, search, kind, sort)

    if not shown:
        st.caption("沒有符合的檔案。")
        render_partial_cleanup(ctx, root, scan)
        return

    # The table's selection is a row number. Keyed on the listed files, a new list starts with nothing selected,
    # instead of the same row now pointing at another file that 刪除 would then remove.
    listing = hashlib.sha1("\n".join(str(entry.path) for entry in shown).encode("utf-8")).hexdigest()[:12]
    selection = st.dataframe(
        entry_rows(shown, root),
        hide_index=True,
        width="stretch",
        height=min(36 * (len(shown) + 1), 420),
        on_select="rerun",
        selection_mode="single-row",
        key=f"library_table_{listing}",
    )
    rows = selection.selection.rows
    selected = shown[rows[0]] if rows and rows[0] < len(shown) else None
    if selected:
        render_entry_details(ctx, selected, root)
    else:
        st.caption("點選表格列左側的方塊查看詳細內容。")

    shown_size = sum(entry.size for entry in shown)
    if len(shown) > 1 and shown_size > BUNDLE_MAX_BYTES:
        st.caption(
            f"目前列出的檔案合計 {format_bytes(shown_size)}，超過打包下載的上限 {format_bytes(BUNDLE_MAX_BYTES)}。"
            "請在表格中點選檔案，逐一下載。"
        )
    elif len(shown) > 1 and st.checkbox("顯示打包下載", key="library_show_bundle"):
        render_bundle_download_button(
            [entry.path for entry in shown], f"打包目前列出的 {len(shown)} 個檔案.zip", "youtube_library.zip", "library_bundle", BUNDLE_MAX_BYTES
        )

    render_partial_cleanup(ctx, root, scan)

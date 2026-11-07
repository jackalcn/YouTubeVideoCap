from __future__ import annotations

import html
import json
import math
import mimetypes
import re
import tempfile
import threading
import time
import uuid
import zipfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone, tzinfo
from functools import partial
from pathlib import Path
from typing import Any, Optional, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import streamlit as st

from videocap.config import (
    LOCAL_MIN_FREE_BYTES,
    APP_NAME,
    CLOUD_MAX_BYTES,
    CLOUD_MAX_CONCURRENT_JOBS,
    LIVE_WAIT_STATUS_PREFIX,
    RECORDING_STATUS_PREFIX,
)
from videocap.downloader import DownloadedItem, DownloadOutcome
from videocap.inspector import find_login_problem
from videocap.jobs import CANCELED, DONE, FAILED, FINISHED_STATES, PAUSED, QUEUED, RUNNING, STATE_LABELS, Job
from videocap.library import open_in_file_manager
from videocap.media_tools import ToolResult
from videocap.options import LoginSource
from videocap.services import (
    KIND_DOWNLOAD,
    KIND_TOOL,
    PUBLIC_QUEUE_FULL_MESSAGE,
    RATE_LIMIT_NOTICE,
    DownloadPayload,
    temporary_cookie_file,
)
from videocap.settings import JobSpec
from videocap.storage import LAST_SETTINGS_KEY, OUTPUT_DIR_KEY
from videocap.tooling import format_bytes, get_free_disk_bytes
from videocap.ui.context import (
    NOTIFY_COOKIE,
    NOTIFY_KEY,
    AppContext,
    collect_settings,
    current_browser,
    current_cookie_bytes,
    current_output_dir,
    start_as_new_visitor,
)


# Streamlit serves a download from memory, so a file is read whole when someone asks for it.
BROWSER_DOWNLOAD_MAX_BYTES = 2 * 1024**3
# Shared by every visitor of the server. One file within the size limit always fits, so a large file is never refused
# forever; beyond that a public host's few GB of memory would run out.
DOWNLOAD_MEMORY_BUDGET = CLOUD_MAX_BYTES * 3 // 2
# A prepared file that is never saved would otherwise hold its memory until the visitor leaves.
PREPARED_DOWNLOAD_SECONDS = 5 * 60
PREPARED_DOWNLOAD_KEY = "prepared_download"
DOWNLOAD_NOTICE_KEY = "download_notice"
LOW_DISK_NOTICE_KEY = "low_disk_notice"
# Zips are built in memory, and Streamlit 1.57 gzips them again on its event loop, which freezes every page meanwhile.
BUNDLE_MAX_BYTES = 50 * 1024**2
# Streamlit gzips every type except audio and video, and a server without /etc/mime.types knows few of these.
MEDIA_MIME_TYPES = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mkv": "video/x-matroska",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".ts": "video/mp2t",
    ".flv": "video/x-flv",
    ".3gp": "video/3gpp",
    ".m4a": "audio/mp4",
    ".mp3": "audio/mpeg",
    ".opus": "audio/ogg",
    ".ogg": "audio/ogg",
    ".flac": "audio/flac",
    ".wav": "audio/wav",
    ".aac": "audio/aac",
}
# Inspections, searches and comment exports run outside the job queue, and a public host's visitors share one IP that
# YouTube answers with HTTP 429 when it sends too much.
PUBLIC_REQUEST_LIMIT = CLOUD_MAX_CONCURRENT_JOBS
PUBLIC_REQUEST_SLOTS = threading.BoundedSemaphore(PUBLIC_REQUEST_LIMIT)
PUBLIC_REQUEST_WAIT_SECONDS = 15.0
PUBLIC_REQUEST_COOLDOWN_SECONDS = 5.0
QUEUE_FULL_MESSAGE = PUBLIC_QUEUE_FULL_MESSAGE
LOGIN_NEEDED_TEXT = "這項任務需要登入：請先在「會員登入」上傳 cookies.txt，再試一次。"
NOTIFY_CHANGED_KEY = "browser_notify_changed"
NOTIFY_ASK_KEY = "browser_notify_ask"
FORGET_NOTICE_KEY = "visitor_forgotten_notice"
FORGET_DIALOG_KEY = "forget_dialog_open"

_download_memory_lock = threading.Lock()
# Ticket -> (bytes, expiry) for every prepared file, across all visitors.
_download_memory: dict[str, tuple[int, float]] = {}


def escape_markdown(text: str) -> str:
    # Video titles often contain characters Streamlit would render as formatting, such as "$" as LaTeX.
    return re.sub(r"([\\`*_{}\[\]()#+\-.!:$~|<>])", r"\\\1", text)


def page_header(title: str, subtitle: str = "") -> None:
    st.markdown(f"<div class='page-title'>{html.escape(title)}</div>", unsafe_allow_html=True)
    if subtitle:
        st.markdown(f"<div class='meta-line'>{html.escape(subtitle)}</div>", unsafe_allow_html=True)


def health_panel_html(status: str, message: str) -> str:
    green_class = "light-dot on green" if status == "green" else "light-dot"
    yellow_class = "light-dot on yellow" if status == "yellow" else "light-dot"
    red_class = "light-dot on red" if status == "red" else "light-dot"
    return (
        "<div class='health-card'>"
        "<div class='health-title'>連結健康狀態</div>"
        "<div class='health-lights'>"
        f"<div class='light-item'><span class='{green_class}'></span><span>正常</span></div>"
        f"<div class='light-item'><span class='{yellow_class}'></span><span>警告</span></div>"
        f"<div class='light-item'><span class='{red_class}'></span><span>失敗</span></div>"
        "</div>"
        f"<div class='health-message'>{html.escape(message)}</div>"
        "</div>"
    )


def viewer_timezone() -> Optional[tzinfo]:
    """The visitor's own time zone, because a public host's clock usually runs in UTC. None means this server's."""
    name = st.context.timezone
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    offset = st.context.timezone_offset
    # Browsers count minutes behind UTC, so Taiwan is -480.
    return timezone(-timedelta(minutes=offset)) if offset is not None else None


def format_local_time(timestamp: float, pattern: str = "%Y-%m-%d %H:%M") -> str:
    return datetime.fromtimestamp(timestamp, viewer_timezone()).strftime(pattern)


@contextmanager
def public_request_slot(ctx: AppContext) -> Iterator[bool]:
    """Yields whether a request to YouTube may go ahead now. Public visitors share a few slots and wait a moment between requests."""
    if ctx.trusted:
        yield True
        return
    if time.time() - st.session_state.get("last_public_request", 0.0) < PUBLIC_REQUEST_COOLDOWN_SECONDS:
        st.warning("請稍等幾秒再試。")
        yield False
        return
    if not PUBLIC_REQUEST_SLOTS.acquire(timeout=PUBLIC_REQUEST_WAIT_SECONDS):
        st.warning("目前使用的人較多，請稍候一分鐘再試。")
        yield False
        return
    try:
        yield True
    finally:
        st.session_state["last_public_request"] = time.time()
        PUBLIC_REQUEST_SLOTS.release()


def download_mime(path: Path) -> str:
    return MEDIA_MIME_TYPES.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def build_zip_bytes(paths: list[Path]) -> bytes:
    with tempfile.TemporaryFile() as spool:
        with zipfile.ZipFile(spool, mode="w") as bundle:
            used_names: set[str] = set()
            for path in paths:
                # Media is already compressed, so storing it keeps this fast; subtitles and info files shrink a lot.
                compression = zipfile.ZIP_STORED if path.suffix.lower() in MEDIA_MIME_TYPES else zipfile.ZIP_DEFLATED
                bundle.write(path, arcname=unique_archive_name(path.name, used_names), compress_type=compression)
        spool.seek(0)
        return spool.read()


def unique_archive_name(name: str, used_names: set[str]) -> str:
    # Chapter files from different videos can share a name, and a zip would keep both under it.
    candidate, number = name, 1
    while candidate in used_names:
        stem, dot, suffix = name.rpartition(".")
        candidate = f"{stem} ({number}){dot}{suffix}" if dot else f"{name} ({number})"
        number += 1
    used_names.add(candidate)
    return candidate


def reserve_download_memory(size: int) -> Optional[str]:
    """Returns a ticket for holding a file of this size in memory, or None while other visitors hold too much."""
    now = time.time()
    with _download_memory_lock:
        for ticket, (_, expires) in list(_download_memory.items()):
            if expires < now:
                del _download_memory[ticket]
        held = sum(held_size for held_size, _ in _download_memory.values())
        if held and held + size > DOWNLOAD_MEMORY_BUDGET:
            return None
        ticket = uuid.uuid4().hex
        _download_memory[ticket] = (size, now + PREPARED_DOWNLOAD_SECONDS)
        return ticket


def release_download_memory(ticket: str) -> None:
    with _download_memory_lock:
        _download_memory.pop(ticket, None)


def held_download_bytes() -> int:
    now = time.time()
    with _download_memory_lock:
        return sum(size for size, expires in _download_memory.values() if expires >= now)


def prepare_download(key: str, size: int, read: Callable[[], bytes]) -> None:
    forget_prepared_download()
    ticket = reserve_download_memory(size)
    if ticket is None:
        st.session_state[DOWNLOAD_NOTICE_KEY] = (key, "伺服器正在處理其他人的下載，請過一兩分鐘再按一次。")
        return
    try:
        data = read()
    except OSError:
        release_download_memory(ticket)
        # The error names the server's folders, which visitors must not see.
        st.session_state[DOWNLOAD_NOTICE_KEY] = (key, "檔案已不在伺服器上（暫存檔只保留約兩小時），請重新下載。")
        return
    st.session_state[PREPARED_DOWNLOAD_KEY] = {"key": key, "data": data, "ticket": ticket, "expires": time.time() + PREPARED_DOWNLOAD_SECONDS}


def forget_prepared_download() -> None:
    prepared = st.session_state.pop(PREPARED_DOWNLOAD_KEY, None)
    if prepared:
        release_download_memory(prepared["ticket"])


def drop_expired_prepared_download() -> None:
    prepared = st.session_state.get(PREPARED_DOWNLOAD_KEY)
    if prepared and prepared["expires"] < time.time():
        forget_prepared_download()


def render_download(label: str, file_name: str, mime: str, size: int, read: Callable[[], bytes], key: str) -> None:
    """Two steps: the first click reads the file, the second saves it.

    Streamlit keeps a download in memory. Read on the first click, a file costs memory only for someone about to save
    it, and its address is fixed while this page is drawn, so no other visitor's page run can delete it before the
    browser fetches it. A one-click deferred download has neither guarantee.
    """
    drop_expired_prepared_download()
    prepared = st.session_state.get(PREPARED_DOWNLOAD_KEY)
    if prepared and prepared["key"] == key:
        st.download_button(
            f"存到電腦：{file_name}",
            data=prepared["data"],
            file_name=file_name,
            mime=mime,
            key=f"{key}_save",
            type="primary",
            icon=":material/download:",
            width="stretch",
            on_click=forget_prepared_download,
        )
    else:
        st.button(label, key=key, width="stretch", on_click=prepare_download, args=(key, size, read))
    notice = st.session_state.get(DOWNLOAD_NOTICE_KEY)
    if notice and notice[0] == key:
        st.session_state.pop(DOWNLOAD_NOTICE_KEY)
        st.warning(notice[1])


def render_file_download_button(path: Path, key: str, ctx: Optional[AppContext] = None, kind: str = "") -> None:
    """Without a context the viewer is this machine's owner. The kind, such as 字幕, goes before the file name."""
    size = path.stat().st_size
    if ctx is not None and not ctx.trusted and size > CLOUD_MAX_BYTES:
        st.caption(
            f"{escape_markdown(path.name)}（{format_bytes(size)}）超過公開主機的下載上限 {format_bytes(CLOUD_MAX_BYTES)}，"
            "請改選較低畫質，或在自己的電腦上執行。"
        )
        return
    if size > BROWSER_DOWNLOAD_MAX_BYTES:
        st.caption(f"{escape_markdown(path.name)}（{format_bytes(size)}）太大，無法透過瀏覽器下載，請到資料夾取用。")
        return
    named = f"{kind}：{escape_markdown(path.name)}" if kind else f" {escape_markdown(path.name)}"
    render_download(f"下載{named}（{format_bytes(size)}）", path.name, download_mime(path), size, path.read_bytes, key)


def render_bundle_download_button(paths: list[Path], label: str, file_name: str, key: str, limit: int = BUNDLE_MAX_BYTES) -> None:
    total_size = sum(path.stat().st_size for path in paths)
    if total_size > limit:
        st.caption(f"{label}：合計 {format_bytes(total_size)}，超過打包上限 {format_bytes(limit)}，請逐一下載檔案。")
        return
    render_download(f"{label}（{format_bytes(total_size)}）", file_name, "application/zip", total_size, partial(build_zip_bytes, paths), key)


def item_paths(item: DownloadedItem) -> list[Path]:
    paths = [item["path"], *item["subtitle_paths"], *item["extra_paths"]]
    return [Path(path) for path in paths if path and Path(path).exists()]


def render_item_downloads(item: DownloadedItem, paths: list[Path], key_base: str, ctx: AppContext) -> None:
    for index, path in enumerate(paths):
        render_file_download_button(path, f"{key_base}_file_{index}", ctx)
    # A video with its subtitles is too big to bundle; the buttons above already cover it.
    if len(paths) > 1 and sum(path.stat().st_size for path in paths) <= BUNDLE_MAX_BYTES:
        stem = Path(item["path"]).stem if item["path"] else item["title"]
        safe_stem = re.sub(r'[\\/:*?"<>|]+', "_", stem)
        render_bundle_download_button(paths, label="全部打包下載.zip", file_name=f"{safe_stem}_bundle.zip", key=f"{key_base}_bundle")


def render_final_download_actions(found: list[tuple[DownloadedItem, list[Path]]], key_base: str, ctx: AppContext) -> None:
    if ctx.can_open_files:
        folder = found[0][1][0].parent
        if st.button("開啟資料夾", icon=":material/folder_open:", key=f"{key_base}_open_folder", width="stretch"):
            problem = open_in_file_manager(folder)
            if problem:
                st.error(problem)

    if len(found) == 1:
        render_item_downloads(*found[0], f"{key_base}_item_0", ctx)
    else:
        render_bundle_download_button(
            [path for _, paths in found for path in paths],
            label=f"全部打包下載.zip（{len(found)} 部）",
            file_name="youtube_downloads.zip",
            key=f"{key_base}_all_bundle",
        )
        for index, (item, paths) in enumerate(found):
            # Drawn only when opened: a playlist's hundreds of buttons would slow every rerun of the page.
            expander = st.expander(escape_markdown(item["title"]), key=f"{key_base}_item_{index}_open", on_change="rerun")
            if expander.open:
                with expander:
                    render_item_downloads(item, paths, f"{key_base}_item_{index}", ctx)

    if not ctx.trusted:
        st.caption("檔案暫存在伺服器上，請使用上方按鈕存到你的電腦。檔案只會暫存約兩小時。")


def render_download_outcome(outcome: DownloadOutcome, key_base: str, ctx: AppContext, state: str = DONE) -> None:
    notes = set(cast(dict[str, Any], outcome).get("notes") or [])
    for message in outcome["errors"]:
        if message not in notes:
            st.error(escape_markdown(message))
    if outcome["skipped"]:
        if len(outcome["skipped"]) <= 3:
            for message in outcome["skipped"]:
                st.warning(escape_markdown(message))
        else:
            with st.expander(f"略過 {len(outcome['skipped'])} 項"):
                st.markdown("\n".join(f"- {escape_markdown(message)}" for message in outcome["skipped"]))

    found = [(item, paths) for item in outcome["items"] if (paths := item_paths(item))]
    if not found:
        if outcome["items"]:
            st.warning("暫存檔案已過期，請重新下載。")
        elif state == DONE and not outcome["errors"] and not outcome["skipped"]:
            st.warning("沒有下載到任何檔案，請確認連結內容或使用「解析連結」檢查。")
        return

    if state != DONE:
        # A playlist paused or cancelled part-way keeps the videos it finished.
        st.info(f"{STATE_LABELS.get(state, state)}：已完成的 {len(found)} 部可以先存到電腦。")
    elif len(found) == 1:
        first_path = found[0][1][0]
        where = first_path if ctx.trusted else first_path.name
        st.success(f"下載完成：{escape_markdown(str(where))}")
    else:
        st.success(f"下載完成：共 {len(found)} 部")

    if outcome["subtitle_note"]:
        st.warning(outcome["subtitle_note"])

    render_final_download_actions(found, key_base, ctx)


# Job submission


def low_disk_warning(ctx: AppContext) -> str:
    # A visitor's files go to the server's own folder, which the public host checks itself before taking a job.
    if not ctx.trusted:
        return ""
    free = get_free_disk_bytes(current_output_dir(ctx))
    if free is None or free >= LOCAL_MIN_FREE_BYTES:
        return ""
    return f"下載資料夾所在的磁碟只剩 {format_bytes(free)}，可能不夠。"


def render_low_disk_notice() -> None:
    notice = st.session_state.pop(LOW_DISK_NOTICE_KEY, None)
    if notice:
        st.warning(notice)


def submit_downloads(
    ctx: AppContext,
    urls: list[str],
    *,
    format_override: str = "",
    playlist_items: str = "",
    playlist_limit: Optional[int] = None,
    time_ranges: str = "",
    not_before: Optional[float] = None,
    video_ids: Optional[list[str]] = None,
) -> list[Job]:
    """Queues one job per link, warning the owner first when the disk is nearly full. Only a warning: the videos may
    be small."""
    warning = low_disk_warning(ctx)
    jobs = queue_downloads(
        ctx,
        urls,
        format_override=format_override,
        playlist_items=playlist_items,
        playlist_limit=playlist_limit,
        time_ranges=time_ranges,
        not_before=not_before,
        video_ids=video_ids,
    )
    if jobs and warning:
        # Queueing reruns the page, which would wipe a warning drawn now.
        st.session_state[LOW_DISK_NOTICE_KEY] = warning
    return jobs


def queue_downloads(
    ctx: AppContext,
    urls: list[str],
    *,
    format_override: str = "",
    playlist_items: str = "",
    playlist_limit: Optional[int] = None,
    time_ranges: str = "",
    not_before: Optional[float] = None,
    video_ids: Optional[list[str]] = None,
) -> list[Job]:
    """Queues one job per link with the current settings. Shows what is wrong and returns nothing if it cannot."""
    settings = collect_settings()
    browser = current_browser()
    cookie_bytes = current_cookie_bytes()
    if st.session_state.get("keep_login_method") == "cookies" and cookie_bytes is None:
        st.error("請先在「會員登入」上傳 cookies.txt，或改選「不登入」。")
        return []
    if browser and not ctx.browser_login_allowed:
        st.error("讀取瀏覽器登入狀態只在本機以 localhost 開啟時提供。")
        return []
    if browser is None and st.session_state.get("keep_login_method") == "browser":
        st.error("請先選擇瀏覽器。")
        return []

    with temporary_cookie_file(cookie_bytes) as cookie_file:
        problem = find_login_problem(LoginSource(cookie_file, browser))
    if problem:
        st.error(escape_markdown(problem))
        return []

    open_slots = ctx.services.open_job_slots(ctx.owner, ctx.limits, ctx.client)
    if open_slots is not None and len(urls) > open_slots:
        st.error(queue_full_message(ctx))
        return []

    output_dir = current_output_dir(ctx)
    batch_id = uuid.uuid4().hex
    specs = [
        JobSpec(
            url=url,
            settings=settings,
            output_dir=str(output_dir),
            multiple_links=len(urls) > 1,
            format_override=format_override,
            playlist_items=playlist_items,
            playlist_limit=playlist_limit,
            time_ranges=time_ranges,
            browser=browser or "",
            needs_cookie_file=cookie_bytes is not None,
            video_ids=list(video_ids or []),
        )
        for url in urls
    ]
    try:
        for spec in specs:
            ctx.services.validate_spec(spec, ctx.limits)
    except ValueError as exc:
        st.error(escape_markdown(str(exc)))
        return []

    storage = ctx.services.storage
    if ctx.trusted and storage:
        # A clip or a few playlist items are meant for this download, not for every one after the next restart.
        storage.set_value(LAST_SETTINGS_KEY, replace(settings, time_ranges="", playlist_items="", remove_ranges="").to_dict())
        storage.set_value(OUTPUT_DIR_KEY, str(output_dir))

    jobs = [
        ctx.services.submit_download(spec, ctx.owner, ctx.limits, cookie_bytes, batch_id, not_before, client=ctx.client)
        for spec in specs
    ]
    st.session_state["last_batch_id"] = batch_id
    return jobs


# Job display


def is_waiting_to_retry(job: Job) -> bool:
    """Queued again by the engine after its connection dropped, and waiting before it tries."""
    return job.state == QUEUED and bool(job.auto_retries) and bool(job.not_before and job.not_before > time.time())


def job_status_line(job: Job) -> str:
    if is_waiting_to_retry(job):
        minutes = max(1, math.ceil((cast(float, job.not_before) - time.time()) / 60))
        return f"網路錯誤，{minutes} 分鐘後自動重試（第 {job.auto_retries} 次）"
    if job.state == QUEUED and job.not_before and job.not_before > time.time():
        return f"排程於 {format_local_time(job.not_before, '%m/%d %H:%M')} 開始"
    outcome = job.outcome if job.kind == KIND_DOWNLOAD else None
    if job.state == DONE and outcome:
        typed = cast(DownloadOutcome, outcome)
        summary = f"{len(typed['items'])} 個項目完成"
        errors = outcome_errors(job)
        if errors:
            summary += f"，{len(errors)} 項失敗"
        if typed["skipped"]:
            summary += f"，略過 {len(typed['skipped'])} 項"
        return summary
    if job.state == FAILED and outcome:
        errors = outcome_errors(job)
        return errors[0][:200] if errors else job.status_text
    return job.status_text


def outcome_errors(job: Job) -> list[str]:
    # A hint is not a failure, even where an outcome lists it among the errors too.
    notes = set(outcome_notes(job))
    return [message for message in cast(DownloadOutcome, job.outcome)["errors"] if message not in notes]


def render_job_controls(job: Job, ctx: AppContext, allow_reorder: bool = False, section: str = "") -> None:
    manager = ctx.services.manager
    buttons: list[tuple[str, str, Any]] = []
    if allow_reorder and job.state == QUEUED:
        buttons.append(("↑", "往前", partial(manager.move, job.id, -1)))
        buttons.append(("↓", "往後", partial(manager.move, job.id, 1)))
    is_recording = job.status_text.startswith(RECORDING_STATUS_PREFIX)
    # A stopped conversion deletes its unfinished output, so there is nothing to pause and keep.
    is_tool = job.kind == KIND_TOOL
    if job.state in (RUNNING, QUEUED) and not is_recording and not is_tool:
        buttons.append(("暫停", "暫停並保留已下載的部分", partial(manager.pause, job.id)))
    if job.state == PAUSED:
        buttons.append(("繼續", "從頭重新處理" if is_tool else "從中斷的地方繼續", partial(resume_job, job, ctx)))
    if is_recording:
        buttons.append(("停止", "停止錄製並保留檔案", partial(manager.cancel, job.id)))
    elif job.state in (RUNNING, QUEUED, PAUSED):
        buttons.append(("取消", "取消並刪除未完成的檔案", partial(manager.cancel, job.id)))
    if job.state in (FAILED, CANCELED):
        buttons.append(("重試", "重新下載，已完成的檔案會保留", partial(retry_job, job, ctx)))
    elif is_waiting_to_retry(job):
        buttons.append(("重試", "現在就重試，不等自動重試，已完成的檔案會保留", partial(ctx.services.retry_now, job)))
    failed_ids = cast(DownloadOutcome, job.outcome)["failed_ids"] if job.state == DONE and job.kind == KIND_DOWNLOAD and job.outcome else []
    if failed_ids:
        buttons.append((f"重試失敗的 {len(failed_ids)} 部", "只重新下載失敗的影片", partial(retry_failed_videos, job, ctx)))

    if not buttons:
        return
    # Each button gets a callback, because a fragment's script body may rerun before the click is seen. The section is
    # in the key because a job that changes state mid-run can appear in two lists of one page. A row rather than columns,
    # which a phone would stack into a tall pile of buttons under every card.
    with st.container(horizontal=True, gap="small"):
        for label, tooltip, action in buttons:
            st.button(label, help=tooltip, key=f"job_{section}_{tooltip}_{job.id}", on_click=action, width="stretch")


def queue_full_message(ctx: AppContext) -> str:
    # A visitor whose kept files fill their share must clear them; waiting for a job to finish would not help.
    return ctx.services.storage_problem(ctx.owner, ctx.limits) or QUEUE_FULL_MESSAGE


def has_login_for(job: Job) -> bool:
    """Gives a job that needs an uploaded login the one from this session. Its own copy is gone once it stops running."""
    payload = job.payload
    if isinstance(payload, DownloadPayload) and payload.spec.needs_cookie_file and job.cookie_bytes is None:
        cookie_bytes = current_cookie_bytes()
        if cookie_bytes is None:
            job.status_text = LOGIN_NEEDED_TEXT
            # A finished job's card shows its results rather than this status.
            st.toast(LOGIN_NEEDED_TEXT, icon=":material/lock:")
            return False
        job.cookie_bytes = cookie_bytes
    return True


def resume_job(job: Job, ctx: AppContext) -> None:
    if has_login_for(job):
        ctx.services.resume_job(job)


def retry_job(job: Job, ctx: AppContext) -> None:
    if has_login_for(job) and not ctx.services.retry_job(job):
        st.toast(queue_full_message(ctx), icon=":material/block:")


def retry_failed_videos(job: Job, ctx: AppContext) -> None:
    # The finished job no longer holds the login, and members-only videos would fail again without it.
    if has_login_for(job) and not ctx.services.retry_failed_videos(job):
        st.toast(queue_full_message(ctx), icon=":material/block:")


def end_rate_limit_pause(ctx: AppContext) -> None:
    ctx.services.end_rate_limit_pause(ctx.limits)


def render_rate_limit_notice(ctx: AppContext) -> None:
    # Visitors' jobs never wait for it, and only the owner may end it.
    until = ctx.services.rate_limit_until()
    if until is None or not ctx.trusted:
        return
    st.warning(RATE_LIMIT_NOTICE.format(clock=format_local_time(until, "%H:%M")), icon=":material/pause_circle:")
    st.button("立即繼續", key="rate_limit_resume", icon=":material/play_arrow:", on_click=end_rate_limit_pause, args=(ctx,))


def render_restart_notice(ctx: AppContext) -> None:
    # Both pages that queue downloads draw this, and the pause is the other reason downloads wait with nothing running.
    render_rate_limit_notice(ctx)
    # After a yt-dlp update the queue holds every download, which would otherwise look like jobs stuck for no reason.
    if not ctx.services.restart_required:
        return
    if ctx.trusted:
        st.warning("yt-dlp 已更新，請重新啟動 App 才能繼續下載。")
        # Imported here because the 系統 page imports this module.
        from videocap.ui.page_system import render_restart_steps

        render_restart_steps()
    else:
        st.warning("下載元件剛更新，主機重新啟動前無法開始下載，請稍後再試。")


def is_waiting_for_stream(job: Job) -> bool:
    return job.state == RUNNING and job.status_text.startswith(LIVE_WAIT_STATUS_PREFIX)


def render_job_card(job: Job, ctx: AppContext, allow_reorder: bool = False, section: str = "") -> None:
    waiting = is_waiting_for_stream(job)
    with st.container(border=True):
        info_column, control_column = st.columns([3, 1], vertical_alignment="center")
        with info_column:
            badge = STATE_LABELS.get(job.state, job.state)
            # A job held outside the download hours would otherwise just say 「等待中」 for hours.
            line = ctx.services.hold_reason(job) or job_status_line(job)
            # 「進行中」 beside a bar stuck at 0 % would look like a download that hangs.
            status = line if waiting or line == badge else f"{badge} · {line}"
            st.markdown(
                f"<div class='job-title'>{html.escape(job.title)}</div><div class='job-status'>{html.escape(status)}</div>",
                unsafe_allow_html=True,
            )
        with control_column:
            render_job_controls(job, ctx, allow_reorder, section)
        if waiting:
            st.caption(":material/schedule: 直播還沒開始，App 會定時再看一次，開播後會自動開始。")
        elif job.state in (RUNNING, PAUSED):
            st.progress(job.fraction)
        render_outcome_notes(job)


def render_outcome_notes(job: Job) -> None:
    notes = outcome_notes(job)
    if notes:
        # Not failures: the files are fine, but the person may want to know, for example, why the quality is lower.
        st.info("\n\n".join(escape_markdown(note) for note in notes), icon=":material/info:")


def notify_finished_jobs(ctx: AppContext) -> bool:
    """Shows a toast for each job that finished since this page last looked. Returns whether anything did."""
    # Keyed by finish time as well, because a retried job keeps its id and finishes again.
    finished = {(job.id, job.finished_at): job for job in ctx.services.manager.list_jobs(ctx.owner) if job.state in FINISHED_STATES}
    known = st.session_state.get("known_finished_jobs")
    if known is None:
        # Jobs that finished before this page opened are old news.
        st.session_state["known_finished_jobs"] = set(finished)
        return False

    new_keys = set(finished) - known
    notices: list[dict[str, str]] = []
    for finish_key in sorted(new_keys, key=lambda key: key[1] or 0.0):
        job = finished[finish_key]
        title = job.title[:60]
        warning = tool_warning(job)
        # Titles come from uploaders, and a toast renders Markdown, images included.
        if job.state == DONE and warning:
            st.toast(f"完成，但請留意：{escape_markdown(title)}", icon=":material/warning:")
            notices.append(browser_notice(finish_key, f"完成，但請留意：{title}", warning))
        elif job.state == DONE:
            st.toast(f"完成：{escape_markdown(title)}", icon=":material/check_circle:")
            notices.append(browser_notice(finish_key, f"完成：{title}", job_status_line(job)))
        elif job.state == FAILED:
            st.toast(f"失敗：{escape_markdown(title)}", icon=":material/error:")
            notices.append(browser_notice(finish_key, f"失敗：{title}", job_status_line(job)))
    st.session_state["known_finished_jobs"] = set(finished)
    if notices and st.session_state.get(NOTIFY_KEY):
        st.html(browser_notification_script(notices), unsafe_allow_javascript=True)
    return bool(new_keys)


def tool_warning(job: Job) -> str:
    result = job.outcome if job.kind == KIND_TOOL else None
    return result.warning if isinstance(result, ToolResult) else ""


def outcome_notes(job: Job) -> list[str]:
    """Hints for a download that still succeeded, such as a lower quality. Outcomes saved before notes existed have none."""
    outcome = job.outcome if job.kind == KIND_DOWNLOAD and isinstance(job.outcome, dict) else {}
    return [str(note) for note in outcome.get("notes") or []]


# Browser notifications


def script_literal(value: Any) -> str:
    """A JavaScript value for text from YouTube, which must not be able to end the <script> it sits in."""
    text = json.dumps(value, ensure_ascii=False)
    for character in "<>&  ":
        text = text.replace(character, f"\\u{ord(character):04x}")
    return text


def browser_notice(finish_key: tuple[str, Optional[float]], title: str, body: str) -> dict[str, str]:
    # The tag makes the browser show a notice once, should the page draw the script again.
    return {"tag": f"videocap-{finish_key[0]}-{finish_key[1]}", "title": title, "body": body[:200]}


def browser_notification_script(notices: list[dict[str, str]]) -> str:
    # Someone looking at the page already sees the toast.
    return (
        "<script>(() => {"
        "if (!('Notification' in window) || Notification.permission !== 'granted') return;"
        "if (document.visibilityState === 'visible' && document.hasFocus()) return;"
        f"for (const notice of {script_literal(notices)}) {{"
        # Android's Chrome only shows notifications from a service worker and throws here.
        "try { new Notification(notice.title, {body: notice.body, tag: notice.tag}); } catch (error) {}"
        "}})();</script>"
    )


def notify_setup_script(enabled: bool, ask: bool, nonce: int) -> str:
    """Remembers the choice for this browser session in a cookie, asks for permission right after the click that
    turned it on, and says what stands in the way. The nonce makes the browser run the script again."""
    if not enabled:
        return f'<script>/* {nonce} */ document.cookie = "{NOTIFY_COOKIE}=; max-age=0; path=/";</script>'
    messages = {
        "unsupported": "這個瀏覽器不支援桌面通知，完成時仍會在頁面上提示。",
        "denied": "瀏覽器封鎖了這個網站的通知：請點網址列左側的圖示，把通知改成允許。",
        "default": "瀏覽器還沒允許通知：請把開關關掉再打開，並在跳出的視窗按「允許」。",
        "insecure": "瀏覽器只在 https 或 localhost 的網址顯示桌面通知，這個網址無法使用；完成時仍會在頁面上提示。",
    }
    return (
        "<div class='meta-line notify-status'></div>"
        f"<script>/* {nonce} */ (() => {{"
        f'document.cookie = "{NOTIFY_COOKIE}=1; path=/; SameSite=Lax";'
        "const box = document.currentScript.parentNode.querySelector('.notify-status');"
        f"const messages = {script_literal(messages)};"
        # Over plain http, such as a NAS's address on the home network, browsers report notifications as blocked, and
        # no site setting can allow them.
        "const insecure = window.isSecureContext === false;"
        "const show = () => {"
        "const state = insecure ? 'insecure' : 'Notification' in window ? Notification.permission : 'unsupported';"
        "box.textContent = messages[state] || '';"
        "};"
        f"if ({'true' if ask else 'false'} && !insecure && 'Notification' in window && Notification.permission === 'default') {{"
        "Notification.requestPermission().then(show, show);"
        "} else { show(); }"
        "})();</script>"
    )


def note_notify_change() -> None:
    # A count rather than a flag, so the script of each change differs from the last and the browser runs it.
    st.session_state[NOTIFY_CHANGED_KEY] = st.session_state.get(NOTIFY_CHANGED_KEY, 0) + 1
    st.session_state[NOTIFY_ASK_KEY] = True


def render_notify_toggle() -> None:
    st.toggle(
        "完成時通知我",
        key=NOTIFY_KEY,
        on_change=note_notify_change,
        help="分頁在背景時，任務完成或失敗會跳出桌面通知。只在這個瀏覽器有效。",
    )
    # Browsers only ask for permission right after a click, so the question goes out only with the run the click started.
    changed = bool(st.session_state.pop(NOTIFY_ASK_KEY, False))
    enabled = bool(st.session_state.get(NOTIFY_KEY))
    if enabled or changed:
        st.html(notify_setup_script(enabled, changed, st.session_state.get(NOTIFY_CHANGED_KEY, 0)), unsafe_allow_javascript=True)


# Forgetting a public visitor


def forget_visitor_jobs(ctx: AppContext) -> bool:
    """Cancels and removes every job of this visitor, which deletes their files, and drops their uploaded login.
    Returns whether all of it is gone; a download still stopping is removed with its files the moment it stops."""
    if ctx.trusted:
        # The owner's jobs are this machine's downloads, which 清除已結束 and the library look after.
        return False
    ctx.services.forget_owner(ctx.owner, ctx.limits)
    return not ctx.services.manager.list_jobs(ctx.owner)


def forget_visitor(ctx: AppContext) -> None:
    if ctx.trusted:
        return
    all_gone = forget_visitor_jobs(ctx)
    forget_prepared_download()
    start_as_new_visitor()
    st.session_state[FORGET_NOTICE_KEY] = (
        "已清除你的任務、暫存檔案與上傳的 cookies。"
        if all_gone
        else "已清除你的資料；還在停止的任務會在幾秒內自行刪除檔案。"
    )


def open_forget_dialog() -> None:
    st.session_state[FORGET_DIALOG_KEY] = True


def close_forget_dialog() -> None:
    st.session_state.pop(FORGET_DIALOG_KEY, None)


# Kept open by a flag rather than by the button's click, so a job finishing meanwhile, which reruns the whole page,
# does not close it.
@st.dialog("清除我的資料", on_dismiss=close_forget_dialog)
def confirm_forget_visitor(ctx: AppContext) -> None:
    st.write("會取消你所有的任務、刪除伺服器上暫存的檔案，並移除你上傳的 cookies。還沒存到電腦的檔案之後就無法下載。")
    if st.button("確定清除", type="primary", width="stretch"):
        forget_visitor(ctx)
        st.rerun()


def render_forget_visitor(ctx: AppContext) -> None:
    if ctx.trusted:
        return
    st.button(
        "清除我的資料",
        icon=":material/delete_forever:",
        on_click=open_forget_dialog,
        help="在共用的電腦上用完時，取消任務並刪除暫存檔與上傳的 cookies。",
    )
    if st.session_state.get(FORGET_DIALOG_KEY):
        confirm_forget_visitor(ctx)


def page_progress_script(active: list[Job]) -> str:
    """Shows progress in the browser tab title, for people who keep the app in a background tab."""
    if not active:
        title = APP_NAME
    else:
        running = [job for job in active if job.state == RUNNING]
        percent = int(sum(job.fraction for job in running) / max(len(running), 1) * 100)
        title = f"[{percent}% · {len(active)} 項] {APP_NAME}"
    return f"<script>window.parent.document.title = {title!r};</script>"


def render_page_progress(active: list[Job]) -> None:
    # The title holds only numbers and the app's own name.
    st.html(page_progress_script(active), unsafe_allow_javascript=True)


def polling_interval(active: list[Job]) -> Optional[str]:
    """Progress bars move every second, but a job waiting for its turn or its start time needs only an occasional look.

    Each poll is a script run on the server, and an idle page sends nothing at all.
    """
    if not active:
        return None
    # A stream that has not started changes nothing on the page for minutes or hours.
    if any(job.state == RUNNING and not is_waiting_for_stream(job) for job in active):
        return "1s"
    now = time.time()
    if all(job.not_before and job.not_before > now for job in active):
        return "30s"
    return "5s"


def _active_jobs_fragment(ctx: AppContext, allow_reorder: bool) -> None:
    manager = ctx.services.manager
    manager.touch(ctx.owner)
    active = manager.active_jobs(ctx.owner)
    active_ids = {job.id for job in active}

    # Results, history, the end of polling and its pace all belong to the full page, so these changes rerun it.
    finished_some = st.session_state.get("seen_active_jobs", set()) - active_ids
    if finished_some or polling_interval(active) != st.session_state.get("polling_interval"):
        st.session_state["seen_active_jobs"] = active_ids
        st.rerun(scope="app")
    st.session_state["seen_active_jobs"] = active_ids

    if allow_reorder:
        # On the queue page the waiting jobs, which the arrows reorder, get a group of their own below the running ones.
        running = [job for job in active if job.state == RUNNING]
        waiting = [job for job in active if job.state != RUNNING]
        for job in running:
            render_job_card(job, ctx, allow_reorder, section="active")
        if waiting:
            st.caption(f"等待中 {len(waiting)} 項，依順序開始")
        for job in waiting:
            render_job_card(job, ctx, allow_reorder, section="active")
    else:
        for job in active:
            render_job_card(job, ctx, allow_reorder, section="active")
    render_page_progress(active)


def render_active_jobs(ctx: AppContext, allow_reorder: bool = False) -> None:
    drop_expired_prepared_download()
    active = ctx.services.manager.active_jobs(ctx.owner)
    notify_finished_jobs(ctx)
    st.session_state["seen_active_jobs"] = {job.id for job in active}
    interval = polling_interval(active)
    st.session_state["polling_interval"] = interval
    fragment = st.fragment(_active_jobs_fragment, run_every=interval)
    fragment(ctx, allow_reorder)


def render_tool_result(job: Job, ctx: Optional[AppContext] = None) -> None:
    result = cast(Optional[ToolResult], job.outcome)
    if job.error:
        st.error(escape_markdown(job.error))
    elif result and result.output_path:
        path = Path(result.output_path)
        st.success(f"完成：{escape_markdown(str(path))}")
        # Finished, but for example with subtitles drawn as boxes, which the person should not miss.
        if result.warning:
            st.warning(escape_markdown(result.warning))
        if path.exists():
            render_file_download_button(path, f"tool_result_{job.id}", ctx)


def render_job_result(job: Job, ctx: AppContext) -> None:
    if job.kind == KIND_TOOL:
        render_tool_result(job, ctx)
    elif job.outcome:
        render_download_outcome(cast(DownloadOutcome, job.outcome), f"job_{job.id}", ctx, job.state)
    elif job.error:
        st.error(escape_markdown(job.error))


def render_job_logs(job: Job, ctx: AppContext) -> None:
    logs = ctx.services.manager.job_logs(job.id)
    if logs:
        escaped_logs = "<br>".join(html.escape(line) for line in logs[-80:])
        st.markdown(f"<div class='log-box'>{escaped_logs}</div>", unsafe_allow_html=True)
    else:
        st.caption("尚無紀錄。")

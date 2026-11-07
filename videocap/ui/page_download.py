from __future__ import annotations

import html
import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import parse_qs, urlparse

import streamlit as st

from videocap.config import (
    CLOUD_ABANDON_AFTER_SECONDS,
    CLOUD_MAX_INSPECT_LINKS,
    LOCAL_MAX_INSPECT_LINKS,
    LOCAL_RUN_GUIDE_URL,
    PLAYLIST_PICKER_MAX_ITEMS,
)
from videocap.errors import humanize_ytdlp_error
from videocap.inspector import (
    LinkReport,
    fetch_comments,
    find_login_problem,
    inspect_link,
    playlist_export_rows,
    rows_to_csv,
    summarize_reports,
)
from videocap.jobs import ACTIVE_STATES, DONE, Job
from videocap.options import LoginSource, job_url, network_options, options_from_spec
from videocap.services import temporary_cookie_file
from videocap.settings import SETTING_KEY_PREFIX, JobSpec
from videocap.textfiles import decode_text
from videocap.tooling import format_bytes, get_free_disk_bytes
from videocap.ui.common import (
    escape_markdown,
    format_local_time,
    health_panel_html,
    item_paths,
    public_request_slot,
    render_active_jobs,
    render_bundle_download_button,
    render_job_card,
    render_job_result,
    render_outcome_notes,
    render_restart_notice,
    render_low_disk_notice,
    submit_downloads,
    viewer_timezone,
)
from videocap.ui.context import AppContext, collect_settings, current_browser, current_cookie_bytes, current_output_dir
from videocap.ui.sidebar import render_settings_sidebar
from videocap.urls import DOMAIN_LIKE_RE, describe_url_counts, extract_video_id, format_timestamp, parse_youtube_urls


INSPECT_WORKERS = 4
LINK_FILE_TYPES = ["txt", "csv", "md", "html"]
# Every rerun of this page reads the whole link box, and a public host's CPU is shared by all its visitors.
LOCAL_MAX_IMPORT_LINKS = 500
CLOUD_MAX_IMPORT_LINKS = 20
LOCAL_MAX_INPUT_CHARS = 100_000
CLOUD_MAX_INPUT_CHARS = 10_000
SCHEDULE_ROUND_KEY = "schedule_round"


def render_hero(ctx: AppContext) -> None:
    st.markdown("<div class='hero-title'>YouTube影片下載平台</div>", unsafe_allow_html=True)
    st.markdown(
        "<div class='hero-subtitle'>貼上影片、播放清單或頻道連結，一站式下載影片、音訊與字幕</div>",
        unsafe_allow_html=True,
    )
    st.caption(
        "請僅下載你擁有權利或已獲授權的內容（例如自己上傳的影片、Creative Commons 授權內容），"
        "並遵守 YouTube 服務條款與當地著作權法。"
    )
    if ctx.is_cloud:
        st.warning(
            "這個雲端版本的下載成功率很低：YouTube 常拒絕雲端主機 IP 的影片下載（HTTP 403），解析連結則不受影響。"
            f"要穩定下載，請[在自己的電腦上執行]({LOCAL_RUN_GUIDE_URL})。"
        )


def consume_query_link() -> None:
    """Fills the input from ?url=..., which is how the bookmarklet sends a page here. Filling only: it never starts a download."""
    link = st.query_params.get("url")
    if link:
        existing = str(st.session_state.get("keep_url_input") or "").strip()
        if link not in existing:
            st.session_state["keep_url_input"] = f"{existing}\n{link}".strip()
        del st.query_params["url"]


def clear_input() -> None:
    st.session_state["keep_url_input"] = ""
    st.session_state["last_reports"] = []


def expand_playlist_chosen() -> bool:
    return bool(st.session_state.get(f"{SETTING_KEY_PREFIX}expand_playlist"))


def append_links_from_file(ctx: AppContext) -> None:
    uploaded = st.session_state.get("link_file_upload")
    if uploaded is None:
        return
    # Excel, Notepad and Windows PowerShell save lists with a byte order mark, as UTF-16, or as Big5.
    urls, rejected = parse_youtube_urls(decode_text(uploaded.getvalue()), expand_playlist=expand_playlist_chosen())
    max_links = LOCAL_MAX_IMPORT_LINKS if ctx.trusted else CLOUD_MAX_IMPORT_LINKS
    existing = str(st.session_state.get("keep_url_input") or "").strip()
    st.session_state["keep_url_input"] = "\n".join(part for part in [existing, *urls[:max_links]] if part)
    notice = f"從 {uploaded.name} 找到 {len(urls)} 個 YouTube 連結"
    if len(urls) > max_links:
        notice += f"，只加入前 {max_links} 個"
    # Header words such as "title,url" are not worth mentioning, but a link that was dropped is.
    other_links = [entry for entry in rejected if "://" in entry or DOMAIN_LIKE_RE.match(entry)]
    if other_links:
        notice += f"；另有 {len(other_links)} 個連結不是可下載的 YouTube 連結"
    st.session_state["link_file_notice"] = notice + "。"


def parse_link_input(text: str, expand_playlist: bool) -> tuple[list[str], list[str]]:
    # Every click anywhere reruns this page, and a pasted list can hold thousands of links.
    cached = st.session_state.get("parsed_link_input")
    if cached and cached[0] == (text, expand_playlist):
        return cached[1]
    parsed = parse_youtube_urls(text, expand_playlist=expand_playlist)
    st.session_state["parsed_link_input"] = ((text, expand_playlist), parsed)
    return parsed


def list_parameter_note(ctx: AppContext, urls: list[str], expand_playlist: bool) -> str:
    if not any(extract_video_id(url) and "list" in parse_qs(urlparse(url).query) for url in urls):
        return ""
    if expand_playlist:
        limit = min(int(collect_settings().playlist_limit), ctx.limits.max_playlist)
        return f"影片網址帶有 list 參數時，會下載整份清單（最多 {limit} 部）。"
    return "影片網址帶有 list 參數時，只下載該部影片。"


def rejected_links_message(rejected: list[str]) -> str:
    # Besides other sites, these include YouTube's own pages that hold no video, such as its home page.
    return "以下內容不是可下載的 YouTube 影片、播放清單或頻道連結：" + "、".join(escape_markdown(entry) for entry in rejected)


def render_link_input(ctx: AppContext) -> tuple[list[str], list[str]]:
    st.text_area(
        "YouTube 網址",
        key="keep_url_input",
        height=110,
        max_chars=LOCAL_MAX_INPUT_CHARS if ctx.trusted else CLOUD_MAX_INPUT_CHARS,
        placeholder="每行一個，或直接貼上包含連結的整段文字\nhttps://www.youtube.com/watch?v=...\nhttps://www.youtube.com/playlist?list=...\n@頻道代號",
    )
    # The sidebar is drawn first, so this is the setting as it now stands.
    expand_playlist = expand_playlist_chosen()
    urls, rejected = parse_link_input(str(st.session_state.get("keep_url_input") or ""), expand_playlist)
    if urls:
        counts = describe_url_counts(urls, expand_playlist=expand_playlist)
        st.caption(f"偵測到 {counts}。{list_parameter_note(ctx, urls, expand_playlist)}")
    else:
        st.caption("支援影片、Shorts、播放清單與頻道連結，也可以直接貼影片 ID 或 @頻道代號。")
    if rejected:
        st.warning(rejected_links_message(rejected[:10]))
    notice = st.session_state.pop("link_file_notice", None)
    if notice:
        st.caption(notice)
    return urls, rejected


def clear_one_off_settings() -> None:
    st.session_state[f"{SETTING_KEY_PREFIX}time_ranges"] = ""
    st.session_state[f"{SETTING_KEY_PREFIX}playlist_items"] = ""
    st.session_state[f"{SETTING_KEY_PREFIX}remove_ranges"] = ""


def render_one_off_settings_notice() -> None:
    """The sidebar fields for a clip or a few playlist items sit in collapsed sections, so they are easy to forget."""
    settings = collect_settings()
    parts = []
    if settings.time_ranges.strip():
        parts.append(f"只下載片段 {settings.time_ranges.strip()}")
    if settings.remove_ranges.strip():
        # The engine ignores it beside a clip, as the sidebar's help says.
        unused = "（已填只下載片段，這一項不會使用）" if settings.time_ranges.strip() else ""
        parts.append(f"移除片段 {settings.remove_ranges.strip()}{unused}")
    if settings.playlist_items.strip():
        parts.append(f"只下載清單第 {settings.playlist_items.strip()} 項")
    if not parts:
        return
    columns = st.columns([4, 1], vertical_alignment="center")
    columns[0].info(escape_markdown(f"目前設定：{'；'.join(parts)}。"))
    columns[1].button("清除片段設定", width="stretch", on_click=clear_one_off_settings)


def close_schedule_popover() -> None:
    # A popover under a new key starts closed. Closing the same one from here would not last: the browser still holds
    # it as open and sends that back with the next click.
    st.session_state[SCHEDULE_ROUND_KEY] = st.session_state.get(SCHEDULE_ROUND_KEY, 0) + 1


def schedule_controls(ctx: AppContext) -> Optional[float]:
    """Returns the chosen start time when the schedule button is pressed."""
    # Tracks its state only so that a new key can close it after queueing; its content runs either way.
    with st.popover("排程下載", width="stretch", key=f"schedule_popover_{st.session_state.get(SCHEDULE_ROUND_KEY, 0)}", on_change="rerun"):
        # First, because an opened popover focuses its first field, and the date's calendar would open over the button.
        pressed = st.button("排入佇列", key="schedule_submit", width="stretch", on_click=close_schedule_popover)
        # The browser's clock, since a public host usually runs in UTC.
        zone = viewer_timezone()
        # Computed once, because a default that changes every minute would make the widgets forget what was picked.
        suggested = st.session_state.setdefault("schedule_suggestion", datetime.now(zone) + timedelta(hours=1))
        day = st.date_input("日期", value=suggested.date(), key="schedule_day")
        moment = st.time_input("時間", value=suggested.time().replace(second=0, microsecond=0), key="schedule_time", step=timedelta(minutes=5))
        if ctx.trusted:
            st.caption("任務會在指定時間才開始，適合排在離峰時段。App 需要保持執行。")
        else:
            st.caption(
                "任務會在指定時間才開始。這是公開主機：等待期間請保持這個頁面開啟，"
                f"關閉頁面約 {CLOUD_ABANDON_AFTER_SECONDS // 60} 分鐘後任務會自動取消。"
            )
    return datetime.combine(day, moment, tzinfo=zone).timestamp() if pressed else None


def render_free_space(ctx: AppContext) -> None:
    if not ctx.trusted:
        return
    free = get_free_disk_bytes(current_output_dir(ctx))
    if free is not None:
        st.caption(f"下載資料夾所在的磁碟剩餘 {format_bytes(free)}。")
    render_low_disk_notice()


def run_link_inspection(ctx: AppContext, urls: list[str]) -> None:
    # Every link is a request from this server to YouTube.
    max_links = LOCAL_MAX_INSPECT_LINKS if ctx.trusted else CLOUD_MAX_INSPECT_LINKS
    if len(urls) > max_links:
        st.error(f"一次最多解析 {max_links} 個連結，請分批進行。")
        return
    settings = collect_settings()
    output_dir = str(current_output_dir(ctx))
    browser = current_browser()
    specs = [JobSpec(url=url, settings=settings, output_dir=output_dir, multiple_links=len(urls) > 1, browser=browser or "") for url in urls]
    with temporary_cookie_file(current_cookie_bytes()) as cookie_file:
        problem = find_login_problem(LoginSource(cookie_file, browser))
        if problem:
            st.error(escape_markdown(problem))
            return
        try:
            options = [options_from_spec(spec, ctx.limits, cookie_file=cookie_file) for spec in specs]
        except ValueError as exc:
            st.error(escape_markdown(str(exc)))
            return

        # A playlist can list more than one download fetches, so the picker offers more to choose from.
        picker_limit = PLAYLIST_PICKER_MAX_ITEMS if ctx.trusted else ctx.limits.max_playlist
        progress = st.progress(0.0, text="解析連結中...")
        # Parallel because each link is mostly waiting on YouTube, and a batch would otherwise take minutes.
        pool = ThreadPoolExecutor(max_workers=INSPECT_WORKERS)
        try:
            # People should be able to pick from more of a playlist than the download limit lets them fetch at once.
            futures = [
                pool.submit(inspect_link, job_url(spec), replace(option, playlist_limit=picker_limit))
                for spec, option in zip(specs, options)
            ]
            reports: list[LinkReport] = []
            for index, future in enumerate(futures, start=1):
                reports.append(future.result())
                progress.progress(index / len(futures), text=f"已解析 {index}/{len(futures)} 個連結")
        finally:
            # Leaving the page or pressing Stop raises above; the links not yet started must not still go to YouTube,
            # and the page must not wait for the ones already running.
            pool.shutdown(wait=False, cancel_futures=True)
        progress.empty()
    # The channel tab setting may have changed the link, and the report list is matched against the input.
    for report, url in zip(reports, urls):
        report["url"] = url
    st.session_state["last_reports"] = reports


def already_downloaded_note(ctx: AppContext, report: LinkReport) -> Optional[str]:
    storage = ctx.services.storage
    if not (ctx.trusted and storage and report.get("video_id")):
        return None
    found = storage.find_downloads([report["video_id"]]).get(report["video_id"])
    if not found:
        return None
    return f"這部影片在 {format_local_time(found.finished_at)} 下載過：{found.path}"


def render_subtitle_tracks(report: LinkReport) -> None:
    manual_rows = report.get("manual_rows") or []
    auto_rows = report.get("auto_rows") or []

    if manual_rows:
        st.write("上傳者字幕軌")
        st.dataframe(manual_rows, width="stretch", hide_index=True)
        st.caption("可用字幕語言代碼")
        st.code(",".join(row["語言"] for row in manual_rows), language=None, wrap_lines=True)
    else:
        st.write("上傳者字幕軌：無")

    if auto_rows:
        with st.expander(f"自動產生字幕軌（{len(auto_rows)} 種語言）"):
            st.dataframe(auto_rows, width="stretch", hide_index=True)
    else:
        st.write("自動產生字幕軌：無")


def render_format_picker(ctx: AppContext, report: LinkReport, key: str) -> None:
    rows = report.get("format_rows") or []
    if not rows:
        st.caption("沒有可列出的格式。")
        return
    st.dataframe(rows, width="stretch", hide_index=True)

    video_rows = [row for row in rows if row["類型"] != "僅音訊"]
    audio_rows = [row for row in rows if row["類型"] == "僅音訊"]
    labels = {
        row["格式ID"]: " · ".join(
            part for part in (row["格式ID"], row["解析度"], row["FPS"] and f"{row['FPS']}fps", row["影像編碼"], row["音訊編碼"], row["語言"], row["大小"]) if part
        )
        for row in rows
    }
    video_ids = [row["格式ID"] for row in video_rows]
    audio_ids = ["", *[row["格式ID"] for row in audio_rows]]
    columns = st.columns(2)
    chosen_video = columns[0].selectbox("影像格式", options=["", *video_ids], format_func=lambda item: labels.get(item, "不含影像"), key=f"{key}_video")
    chosen_audio = columns[1].selectbox("音訊格式", options=audio_ids, format_func=lambda item: labels.get(item, "不另加音訊"), key=f"{key}_audio")
    needs_merge = bool(chosen_video and chosen_audio)
    format_id = "+".join(part for part in (chosen_video, chosen_audio) if part)
    if needs_merge and not ctx.ffmpeg_available:
        st.warning("合併影像與音訊需要 ffmpeg。")
    if st.button(
        "用這個格式下載",
        key=f"{key}_download",
        width="stretch",
        disabled=not format_id or (needs_merge and not ctx.ffmpeg_available),
    ):
        if submit_downloads(ctx, [report["url"]], format_override=format_id):
            st.rerun()


def render_chapter_picker(ctx: AppContext, report: LinkReport, key: str) -> None:
    chapters = report.get("chapters") or []
    rows = [
        {"下載": False, "開始": format_timestamp(chapter["start"]), "結束": format_timestamp(chapter["end"]), "章節": chapter["title"]}
        for chapter in chapters
    ]
    edited = st.data_editor(
        rows,
        hide_index=True,
        width="stretch",
        disabled=["開始", "結束", "章節"],
        key=f"{key}_chapters",
        column_config={"下載": st.column_config.CheckboxColumn(width="small")},
    )
    chosen = [chapter for chapter, row in zip(chapters, edited) if row["下載"]]
    if st.button(
        f"下載勾選的 {len(chosen)} 個章節",
        key=f"{key}_chapter_download",
        width="stretch",
        disabled=not chosen or not ctx.ffmpeg_available,
    ):
        time_ranges = ",".join(f"{chapter['start']:.3f}-{chapter['end']:.3f}" for chapter in chosen)
        if submit_downloads(ctx, [report["url"]], time_ranges=time_ranges):
            st.rerun()
    if not ctx.ffmpeg_available:
        st.caption("下載章節需要 ffmpeg。")


def render_comment_export(ctx: AppContext, report: LinkReport, key: str) -> None:
    if st.button("取得留言（最多 500 則）", key=f"{key}_comments", width="stretch"):
        with public_request_slot(ctx) as allowed, st.spinner("讀取留言中，留言多時需要一些時間..."):
            if allowed:
                with temporary_cookie_file(current_cookie_bytes()) as cookie_file:
                    try:
                        st.session_state[f"{key}_comment_rows"] = fetch_comments(
                            report["url"],
                            LoginSource(cookie_file, current_browser()),
                            network=network_options(collect_settings(), ctx.limits),
                        )
                    except Exception as exc:
                        st.error(escape_markdown(humanize_ytdlp_error(str(exc))))
    rows = st.session_state.get(f"{key}_comment_rows")
    if rows is not None:
        st.caption(f"取得 {len(rows)} 則留言。")
        st.dataframe(rows[:100], width="stretch", hide_index=True)
        st.download_button(
            "下載留言 CSV",
            data=rows_to_csv(rows),
            file_name=f"{report.get('video_id', 'comments')}_comments.csv",
            mime="text/csv",
            key=f"{key}_comments_csv",
            width="stretch",
            on_click="ignore",
        )


def render_video_report(ctx: AppContext, report: LinkReport, key: str) -> None:
    # Tracked by key, because otherwise a button inside a tab sends the person back to the first tab.
    tabs = st.tabs(["格式", "字幕", "章節", "說明與標籤", "留言"], key=f"{key}_tabs", on_change="rerun")
    with tabs[0]:
        render_format_picker(ctx, report, key)
    with tabs[1]:
        render_subtitle_tracks(report)
    with tabs[2]:
        if report.get("chapters"):
            render_chapter_picker(ctx, report, key)
        else:
            st.caption("這部影片沒有章節。")
    with tabs[3]:
        if report.get("description"):
            st.text(report["description"][:5000])
        else:
            st.caption("沒有說明。")
        if report.get("tags"):
            st.caption("標籤：" + "、".join(escape_markdown(tag) for tag in report["tags"][:40]))
        if report.get("license"):
            st.caption(f"授權：{escape_markdown(report['license'])}")
    with tabs[4]:
        render_comment_export(ctx, report, key)


def playlist_file_stem(report: LinkReport) -> str:
    # Named after the list, without what Windows refuses in a file name, so a saved export says which list it holds.
    stem = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", str(report.get("title") or "")).strip(" .")[:100]
    return stem or "playlist"


def render_playlist_report(ctx: AppContext, report: LinkReport, key: str) -> None:
    entries = report.get("entries") or []
    entry_count = report.get("entry_count")
    st.write(f"播放清單／頻道：{f'共 {entry_count} 部，' if entry_count else ''}列出 {len(entries)} 部")
    if not entries:
        return
    # Only as many as the download limit start ticked: a channel can list hundreds, and one click should not fetch them all.
    preselected = int(collect_settings().playlist_limit)
    rows = [
        {"下載": entry["index"] <= preselected, "編號": entry["index"], "標題": entry["title"], "長度": entry["duration"], "觀看次數": entry["views"]}
        for entry in entries
    ]
    edited = st.data_editor(
        rows,
        hide_index=True,
        width="stretch",
        height=min(38 * (len(rows) + 1), 420),
        disabled=["編號", "標題", "長度", "觀看次數"],
        key=f"{key}_entries",
        column_config={
            "下載": st.column_config.CheckboxColumn(width="small"),
            "編號": st.column_config.NumberColumn(width="small"),
            "觀看次數": st.column_config.NumberColumn(format="localized"),
        },
    )
    chosen = [row["編號"] for row in edited if row["下載"]]
    columns = st.columns(2)
    limit = ctx.limits.max_playlist
    if columns[0].button(f"下載勾選的 {len(chosen)} 部", key=f"{key}_pick_download", type="primary", width="stretch", disabled=not chosen):
        if len(chosen) > limit:
            st.error(f"一次最多下載 {limit} 部，請減少勾選。")
        elif report.get("is_nested"):
            # Rows here are tabs or whole playlists, so playlist item numbers would select those, not these entries.
            urls = [entry["url"] for entry in entries if entry["index"] in chosen and entry["url"]]
            if submit_downloads(ctx, urls):
                st.rerun()
        else:
            # By ID rather than by position: a channel may publish a video before the job runs, shifting every number.
            video_ids = [entry["id"] for entry in entries if entry["index"] in chosen and entry["id"]]
            if submit_downloads(ctx, [report["url"]], video_ids=video_ids, playlist_limit=len(video_ids)):
                st.rerun()
    export_rows = [dict(entry) for entry in entries]
    file_stem = playlist_file_stem(report)
    with columns[1].popover("匯出清單", width="stretch"):
        st.download_button(
            "匯出影片清單 CSV",
            data=rows_to_csv(playlist_export_rows(entries)),
            file_name=f"{file_stem}.csv",
            mime="text/csv",
            key=f"{key}_csv",
            width="stretch",
            on_click="ignore",
        )
        st.download_button(
            "匯出影片清單 JSON",
            data=json.dumps(export_rows, ensure_ascii=False, indent=2).encode("utf-8"),
            file_name=f"{file_stem}.json",
            mime="application/json",
            key=f"{key}_json",
            width="stretch",
            on_click="ignore",
        )


def render_link_report(ctx: AppContext, report: LinkReport, index: int) -> None:
    # Includes the video, so checkboxes from an earlier inspection do not carry over to a different one.
    key = f"report_{index}_{report.get('video_id', '')}"
    with st.container(border=True):
        if report.get("error"):
            st.error(f"{escape_markdown(report.get('url', ''))}\n\n{escape_markdown(report.get('error', ''))}")
            return

        thumbnail_col, info_col = st.columns([1, 2])
        if report.get("thumbnail"):
            # A channel's picture is a tall square, which would push its video list off the screen.
            thumbnail_col.image(report.get("thumbnail", ""), width=160 if report.get("is_playlist") else "stretch")

        with info_col:
            st.markdown(
                f"<div class='video-title'>{html.escape(report.get('title', ''))}</div>",
                unsafe_allow_html=True,
            )
            meta = [report.get("channel", ""), *(report.get("details") or [])]
            st.markdown(
                f"<div class='meta-line'>{html.escape(' · '.join(part for part in meta if part))}</div>",
                unsafe_allow_html=True,
            )

            if not report.get("is_playlist"):
                if report.get("is_live") and ctx.trusted:
                    st.warning("這部影片正在直播。可在左側「播放清單與頻道」選擇錄製，否則結束後才能下載。")
                elif report.get("is_live"):
                    # Recording runs for hours, so a public host does not offer it.
                    st.warning("這部影片正在直播，直播結束後才能下載。")

                note = already_downloaded_note(ctx, report)
                if note:
                    st.info(escape_markdown(note))

                resolutions = report.get("resolutions") or []
                if resolutions:
                    st.write("可用畫質：" + "、".join(f"{height}p" for height in resolutions))
                if report.get("selected_format"):
                    size = report.get("selected_size") or 0
                    size_text = f"（約 {format_bytes(size)}）" if size else ""
                    st.write(f"依目前設定將下載：{escape_markdown(report['selected_format'])}{size_text}")
                if report.get("output_name"):
                    st.caption(f"檔名：{escape_markdown(report['output_name'])}")
                if report.get("audio_languages"):
                    st.caption("可用音軌語言：" + "、".join(escape_markdown(language) for language in report["audio_languages"]))

        if report.get("is_playlist"):
            render_playlist_report(ctx, report, key)
        else:
            render_video_report(ctx, report, key)


def render_batch_results(ctx: AppContext) -> None:
    batch_id = st.session_state.get("last_batch_id")
    if not batch_id:
        return
    batch: list[Job] = [job for job in ctx.services.manager.list_jobs(ctx.owner) if job.batch_id == batch_id]
    # Links still running are drawn with the active jobs above.
    jobs = [job for job in batch if job.state not in ACTIVE_STATES]
    if not jobs:
        return
    st.markdown("### 下載結果")
    if len(batch) == 1:
        if jobs[0].state != DONE:
            # Paused, cancelled or failed: the card says which, and offers to resume or retry.
            render_job_card(jobs[0], ctx, section="batch")
        else:
            # The card would have shown these.
            render_outcome_notes(jobs[0])
        render_job_result(jobs[0], ctx)
        return

    all_paths = [path for job in jobs if job.outcome for item in job.outcome["items"] for path in item_paths(item)]
    if len(jobs) > 1 and all_paths:
        render_bundle_download_button(
            all_paths,
            label=f"全部打包下載.zip（{len(jobs)} 個連結）",
            file_name="youtube_downloads.zip",
            key=f"batch_bundle_{batch_id}",
        )
    for job in jobs:
        render_job_card(job, ctx, section="batch")
        # Drawn only when opened, so a big batch does not slow every rerun of the page.
        expander = st.expander("結果", key=f"batch_result_{job.id}", on_change="rerun")
        if expander.open:
            with expander:
                render_job_result(job, ctx)


def render_download_page(ctx: AppContext) -> None:
    consume_query_link()
    render_settings_sidebar(ctx)
    render_hero(ctx)
    render_restart_notice(ctx)

    urls, rejected = render_link_input(ctx)
    render_one_off_settings_notice()

    columns = st.columns([2, 2, 2, 2, 1])
    start_download = columns[0].button("開始下載", type="primary", width="stretch")
    inspect_only = columns[1].button("解析連結（不下載）", width="stretch")
    with columns[2]:
        scheduled_at = schedule_controls(ctx)
    with columns[3].popover("匯入清單檔", width="stretch"):
        st.file_uploader(
            "文字檔裡的 YouTube 連結都會加入上方清單",
            type=LINK_FILE_TYPES,
            key="link_file_upload",
            on_change=append_links_from_file,
            args=(ctx,),
        )
    columns[4].button("清空", width="stretch", on_click=clear_input)
    render_free_space(ctx)

    if start_download or inspect_only or scheduled_at:
        if rejected:
            st.error(rejected_links_message(rejected))
        elif not urls:
            st.error("請輸入至少一個 YouTube 網址。")
        elif inspect_only:
            with public_request_slot(ctx) as allowed:
                if allowed:
                    run_link_inspection(ctx, urls)
        elif submit_downloads(ctx, urls, not_before=scheduled_at):
            st.rerun()

    render_active_jobs(ctx)
    render_batch_results(ctx)

    # Reports for links no longer in the input are stale.
    reports = [report for report in st.session_state["last_reports"] if report.get("url") in urls]
    if reports:
        st.markdown("### 連結解析結果")
        st.markdown(health_panel_html(*summarize_reports(reports)), unsafe_allow_html=True)
        for index, report in enumerate(reports):
            render_link_report(ctx, report, index)

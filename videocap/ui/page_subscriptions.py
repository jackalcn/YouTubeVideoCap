from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

import streamlit as st

from videocap.config import LOCAL_MAX_PLAYLIST_LIMIT, SUBSCRIPTION_INTERVALS
from videocap.errors import humanize_ytdlp_error
from videocap.library import RECYCLED_TEXT, folder_creation_problem, recycle_bin_available
from videocap.options import LoginSource, network_options
from videocap.services import temporary_cookie_file
from videocap.settings import DownloadSettings
from videocap.storage import AppStorage, Subscription
from videocap.subscription_import import ImportedChannel, parse_subscription_file, subscribed_channel_ids
from videocap.ui.common import escape_markdown, format_local_time, page_header, render_active_jobs
from videocap.ui.context import AppContext, collect_settings, current_browser, current_cookie_bytes, current_output_dir
from videocap.ui.sidebar import render_settings_sidebar
from videocap.urls import classify_youtube_url, parse_youtube_urls


SUBSCRIPTION_FORM_KEYS = ("subscription_url", "subscription_max_items", "subscription_interval", "subscription_skip_existing")
SUBSCRIPTION_FORM_DONE_KEY = "subscription_form_done"
SUBSCRIPTION_NOTICES_KEY = "subscription_notices"
IMPORT_RUNNING_KEY = "subscription_import_running"
RETENTION_MODES = {"off": "不自動刪除", "latest": "只保留最新幾部", "days": "刪除下載超過幾天的影片"}


def format_time(timestamp: float | None) -> str:
    return format_local_time(timestamp, "%m/%d %H:%M") if timestamp else "尚未檢查"


def subscription_url(url: str) -> str:
    """The channel or playlist a link leads to. Raises ValueError with a message for the user."""
    # While a playlist plays, the address bar shows the video, so follow the playlist it is in. Mixes (RD...) are made
    # for each viewer and have nothing to follow.
    list_id = (parse_qs(urlparse(url).query).get("list") or [""])[0]
    if classify_youtube_url(url) == "video" and list_id and not list_id.startswith("RD"):
        url = f"https://www.youtube.com/playlist?list={list_id}"
    kind = classify_youtube_url(url)
    if kind == "video":
        raise ValueError("這是單一影片的連結。訂閱需要頻道或播放清單。")
    if kind not in ("channel", "playlist"):
        raise ValueError("這個連結不是頻道或播放清單。請貼上頻道（例如 https://www.youtube.com/@名稱）或播放清單的連結。")
    return url


def sidebar_subscription_settings() -> DownloadSettings:
    """The sidebar's settings as a subscription keeps them: without a folder rule, each channel gets a folder."""
    settings = collect_settings()
    settings.folder_rule = settings.folder_rule if settings.folder_rule != "none" else "channel"
    return settings


def typed_folder(raw: str) -> str:
    """A folder typed into a form, read the way the sidebar's 下載資料夾 is. Raises ValueError with a message for the user."""
    # Windows' 「複製路徑」 wraps the folder in quotes.
    text = raw.strip().strip('"').strip()
    if not text:
        raise ValueError("請輸入下載資料夾。")
    folder = Path(text).expanduser().resolve()
    problem = folder_creation_problem(folder)
    if problem:
        raise ValueError(problem)
    return str(folder)


def add_subscription(ctx: AppContext, storage: AppStorage, raw_url: str, max_items: int, interval: int, skip_existing: bool) -> bool:
    """Shows what went wrong and returns False, or leaves the result in SUBSCRIPTION_NOTICES_KEY and returns True."""
    urls, _ = parse_youtube_urls(raw_url)
    try:
        if not urls:
            raise ValueError("請輸入頻道或播放清單的連結。")
        url = subscription_url(urls[0])
        settings = sidebar_subscription_settings()
        output_dir = str(current_output_dir(ctx))
        # Checked now, because a setting that cannot run would otherwise fail at every check without anyone seeing it.
        ctx.services.validate_subscription(url, settings, output_dir)
    except ValueError as exc:
        st.error(escape_markdown(str(exc)))
        return False
    # Before seeding: that marks the channel's newest videos as downloaded, which the existing subscription has yet to fetch.
    if any(subscription.url == url for subscription in storage.list_subscriptions()):
        st.warning("這個連結已經訂閱過了。")
        return False

    with st.spinner("讀取頻道資訊..."):
        with temporary_cookie_file(current_cookie_bytes()) as cookie_file:
            try:
                login = LoginSource(cookie_file, current_browser())
                network = network_options(settings, ctx.limits)
                title, seen = ctx.services.seed_subscription_archive(url, max_items, login, network=network) if skip_existing else (url, 0)
            except Exception as exc:
                st.error(escape_markdown(humanize_ytdlp_error(str(exc))))
                return False
    try:
        storage.add_subscription(url, title, settings, output_dir, max_items, interval)
    except sqlite3.IntegrityError:
        st.warning("這個連結已經訂閱過了。")
        return False
    notices = [("success", f"已訂閱「{title}」。" + (f"目前的 {seen} 部影片視為已下載，之後只抓新影片。" if skip_existing else ""))]
    if len(urls) > 1:
        notices.append(("warning", f"一次只能訂閱一個連結，已略過其他 {len(urls) - 1} 個。"))
    st.session_state[SUBSCRIPTION_NOTICES_KEY] = notices
    return True


def render_add_form(ctx: AppContext, storage: AppStorage) -> None:
    # Emptied only after a successful add, so a typo or a failed lookup does not throw away what was typed.
    if st.session_state.pop(SUBSCRIPTION_FORM_DONE_KEY, False):
        for key in SUBSCRIPTION_FORM_KEYS:
            st.session_state.pop(key, None)
    with st.form("subscription_form"):
        st.markdown("**新增訂閱**")
        url = st.text_input("頻道或播放清單連結", placeholder="https://www.youtube.com/@channel/videos", key="subscription_url")
        columns = st.columns(2)
        max_items = columns[0].number_input(
            "每次最多下載幾部", min_value=1, max_value=LOCAL_MAX_PLAYLIST_LIMIT, value=5, key="subscription_max_items"
        )
        interval = columns[1].selectbox(
            "自動檢查",
            options=list(SUBSCRIPTION_INTERVALS),
            format_func=SUBSCRIPTION_INTERVALS.get,
            index=list(SUBSCRIPTION_INTERVALS).index(24),
            key="subscription_interval",
        )
        skip_existing = st.checkbox("只追蹤之後的新影片（現有影片視為已下載）", value=True, key="subscription_skip_existing")
        st.caption("會使用左側目前的下載設定與下載資料夾；沒有選資料夾分類時，自動依頻道分類。")
        submitted = st.form_submit_button("加入訂閱", type="primary", width="stretch")
    if submitted and add_subscription(ctx, storage, url, int(max_items), int(interval), skip_existing):
        st.session_state[SUBSCRIPTION_FORM_DONE_KEY] = True
        st.rerun()
    for level, message in st.session_state.pop(SUBSCRIPTION_NOTICES_KEY, []):
        getattr(st, level)(escape_markdown(message))


def start_import(ctx: AppContext, channels: list[ImportedChannel], max_items: int, interval: int, skip_existing: bool) -> bool:
    settings = sidebar_subscription_settings()
    try:
        ctx.services.start_subscription_import(
            channels,
            settings,
            str(current_output_dir(ctx)),
            max_items,
            interval,
            skip_existing,
            ctx.limits,
            cookie_bytes=current_cookie_bytes(),
            browser=current_browser(),
            network=network_options(settings, ctx.limits),
        )
    except ValueError as exc:
        st.error(escape_markdown(str(exc)))
        return False
    return True


def render_import_progress(ctx: AppContext) -> None:
    progress = ctx.services.subscription_import
    if progress is None:
        return
    if not progress.finished:
        st.progress(
            progress.handled / max(progress.total, 1),
            text=escape_markdown(f"匯入中 {progress.handled}／{progress.total}：{progress.current}"),
        )
        st.button("停止匯入", key="subscription_import_stop", on_click=progress.stop_requested.set)
        return
    summary = f"匯入完成：加入 {len(progress.added)} 個訂閱"
    if progress.skipped:
        summary += f"，{len(progress.skipped)} 個已訂閱而略過"
    if progress.handled < progress.total:
        summary += f"，{progress.total - progress.handled} 個因停止而未處理"
    st.success(escape_markdown(summary + "。"))
    if progress.failed:
        with st.expander(f"{len(progress.failed)} 個頻道無法加入，可稍後再匯入一次"):
            st.markdown("\n".join(f"- {escape_markdown(message)}" for message in progress.failed))
    if st.button("關閉匯入結果", key="subscription_import_dismiss"):
        ctx.services.subscription_import = None
        st.rerun()


def _import_progress_fragment(ctx: AppContext) -> None:
    progress = ctx.services.subscription_import
    if progress is not None and progress.finished and st.session_state.get(IMPORT_RUNNING_KEY):
        # The list of subscriptions below the form changed too, so the whole page draws again.
        st.session_state[IMPORT_RUNNING_KEY] = False
        st.rerun(scope="app")
    render_import_progress(ctx)


def render_import_section(ctx: AppContext, storage: AppStorage) -> None:
    progress = ctx.services.subscription_import
    running = progress is not None and not progress.finished
    st.session_state[IMPORT_RUNNING_KEY] = running
    with st.expander("匯入訂閱清單（Google Takeout／OPML）", expanded=progress is not None):
        st.fragment(_import_progress_fragment, run_every="2s" if running else None)(ctx)
        if running:
            st.caption("匯入在背景進行，可以先離開這一頁。")
            return
        st.caption(
            "到 takeout.google.com 只選「YouTube 和 YouTube Music」的「訂閱內容」，下載並解壓縮後上傳 subscriptions.csv；"
            "也可以上傳 RSS 閱讀器匯出的 OPML 檔。"
        )
        uploaded = st.file_uploader("訂閱清單檔案", type=["csv", "opml", "xml"], key="subscription_import_upload")
        if uploaded is None:
            return
        try:
            channels = parse_subscription_file(uploaded.getvalue())
        except ValueError as exc:
            st.error(escape_markdown(str(exc)))
            return
        subscribed = subscribed_channel_ids(subscription.url for subscription in storage.list_subscriptions())
        new_channels = [channel for channel in channels if channel.channel_id not in subscribed]
        if len(new_channels) < len(channels):
            st.caption(f"有 {len(channels) - len(new_channels)} 個頻道已經訂閱，不再列出。")
        if not new_channels:
            st.info("檔案裡的頻道都已經訂閱了。")
            return
        with st.container(height=min(36 * len(new_channels) + 16, 320)):
            # Keyed by the file, so another file starts with every channel ticked again.
            picked = [
                channel
                for channel in new_channels
                if st.checkbox(
                    escape_markdown(channel.title or channel.channel_id),
                    value=True,
                    key=f"subscription_import_pick_{uploaded.file_id}_{channel.channel_id}",
                )
            ]
        columns = st.columns(2)
        max_items = columns[0].number_input(
            "每次最多下載幾部", min_value=1, max_value=LOCAL_MAX_PLAYLIST_LIMIT, value=5, key="subscription_import_max_items"
        )
        interval = columns[1].selectbox(
            "自動檢查",
            options=list(SUBSCRIPTION_INTERVALS),
            format_func=SUBSCRIPTION_INTERVALS.get,
            index=list(SUBSCRIPTION_INTERVALS).index(24),
            key="subscription_import_interval",
        )
        skip_existing = st.checkbox("只追蹤之後的新影片（現有影片視為已下載）", value=True, key="subscription_import_skip_existing")
        st.caption("使用左側目前的下載設定與下載資料夾。每個頻道之間會稍等幾秒，以免 YouTube 暫時拒絕連線。")
        if st.button(f"加入 {len(picked)} 個訂閱", type="primary", width="stretch", disabled=not picked, key="subscription_import_start"):
            if start_import(ctx, picked, int(max_items), int(interval), skip_existing):
                st.rerun()


def check_subscription_now(ctx: AppContext, storage: AppStorage, subscription: Subscription) -> None:
    try:
        ctx.services.check_subscription(subscription)
    except ValueError as exc:
        # Saved before settings were checked on adding, so it can never run; otherwise it would stay 「檢查中...」.
        storage.update_subscription(subscription.id, last_result=f"無法檢查：{exc} 請修正左側設定，再按「編輯」勾選「套用左側目前的下載設定」。")


def save_subscription_edit(
    ctx: AppContext,
    storage: AppStorage,
    subscription: Subscription,
    title: str,
    interval: int,
    max_items: int,
    folder: str,
    use_sidebar: bool,
) -> bool:
    """Shows what went wrong and returns False, or saves and leaves a notice for the next run."""
    settings = sidebar_subscription_settings() if use_sidebar else subscription.settings
    try:
        output_dir = typed_folder(folder)
        # As when adding: settings that cannot run would fail at every check without anyone seeing it.
        ctx.services.validate_subscription(subscription.url, settings, output_dir)
    except ValueError as exc:
        st.error(escape_markdown(str(exc)))
        return False
    name = title.strip() or subscription.title or subscription.url
    storage.update_subscription(
        subscription.id, title=name, interval_hours=interval, max_items=max_items, output_dir=output_dir, settings=settings
    )
    st.session_state[SUBSCRIPTION_NOTICES_KEY] = [("success", f"已儲存「{name}」的設定。")]
    return True


def render_edit_popover(subscription: Subscription) -> Optional[tuple[str, int, int, str, bool]]:
    """The values in the form when 儲存 is pressed. Saving is left to the caller, so that an error is not drawn inside
    the row of buttons."""
    with st.popover("編輯", icon=":material/edit:", width="stretch"):
        with st.form(f"subscription_edit_{subscription.id}", border=False):
            title = st.text_input("名稱", value=subscription.title, key=f"subscription_edit_title_{subscription.id}")
            intervals = list(SUBSCRIPTION_INTERVALS)
            interval = st.selectbox(
                "自動檢查",
                options=intervals,
                format_func=lambda hours: SUBSCRIPTION_INTERVALS.get(hours, f"每 {hours} 小時"),
                index=intervals.index(subscription.interval_hours) if subscription.interval_hours in intervals else 0,
                key=f"subscription_edit_interval_{subscription.id}",
            )
            max_items = st.number_input(
                "每次最多下載幾部",
                min_value=1,
                max_value=LOCAL_MAX_PLAYLIST_LIMIT,
                value=min(max(subscription.max_items, 1), LOCAL_MAX_PLAYLIST_LIMIT),
                key=f"subscription_edit_max_items_{subscription.id}",
            )
            folder = st.text_input("下載資料夾", value=subscription.output_dir, key=f"subscription_edit_folder_{subscription.id}")
            use_sidebar = st.checkbox(
                "套用左側目前的下載設定",
                key=f"subscription_edit_use_sidebar_{subscription.id}",
                help="以左側的畫質、字幕、篩選等設定取代這個訂閱原本的設定。不勾選時保留原本的設定。",
            )
            st.caption("排隊中的檢查仍用舊設定，下一次檢查才會套用。")
            submitted = st.form_submit_button("儲存", type="primary", width="stretch")
    return (title, int(interval), int(max_items), folder, use_sidebar) if submitted else None


def retention_text(keep_latest: int, keep_days: int) -> str:
    if keep_latest:
        return f"只保留最新 {keep_latest} 部"
    if keep_days:
        return f"刪除下載超過 {keep_days} 天的影片"
    return ""


def save_retention(ctx: AppContext, subscription: Subscription, keep_latest: int, keep_days: int) -> None:
    deleted, problems = ctx.services.save_retention_rule(subscription.id, keep_latest, keep_days, ctx.limits)
    rule = retention_text(keep_latest, keep_days)
    # Asked after deleting: a recycle bin that failed is remembered, so this says what really happened.
    where = RECYCLED_TEXT if recycle_bin_available(Path(subscription.output_dir)) else "已永久刪除，這個位置沒有回收桶"
    notices = [("success", f"「{subscription.title}」的保留規則：{rule or '不自動刪除'}。" + (f"已刪除 {deleted} 部影片（{where}）。" if deleted else ""))]
    if problems:
        notices.append(("warning", "有些檔案沒有刪除：" + "；".join(problems[:5])))
    st.session_state[SUBSCRIPTION_NOTICES_KEY] = notices


def render_retention_popover(ctx: AppContext, subscription: Subscription) -> None:
    current = "latest" if subscription.keep_latest else "days" if subscription.keep_days else "off"
    with st.popover("保留規則", icon=":material/auto_delete:", width="stretch"):
        mode = st.radio(
            "自動刪除舊影片",
            options=list(RETENTION_MODES),
            format_func=RETENTION_MODES.get,
            index=list(RETENTION_MODES).index(current),
            key=f"subscription_retention_mode_{subscription.id}",
        )
        keep_latest = keep_days = 0
        if mode == "latest":
            keep_latest = int(
                st.number_input("保留幾部", min_value=1, value=subscription.keep_latest or 20, key=f"subscription_keep_latest_{subscription.id}")
            )
        elif mode == "days":
            keep_days = int(
                st.number_input("保留幾天", min_value=1, value=subscription.keep_days or 30, key=f"subscription_keep_days_{subscription.id}")
            )
        if recycle_bin_available(Path(subscription.output_dir)):
            where = "刪除的影片會移到回收桶，清空回收桶後才會空出硬碟空間。"
        else:
            where = "這個資料夾沒有回收桶，刪除的影片會永久刪除。"
        st.caption(f"只刪除這個訂閱下載、而且還在它的下載資料夾裡的影片，連同字幕等附加檔。刪掉的影片不會再被下載。每次檢查後套用。{where}")
        doomed = ctx.services.retention_candidates(subscription, keep_latest, keep_days)
        # What the person last saw. A check that finishes, or a day that passes, can change the list before the click.
        shown_key = f"subscription_retention_shown_{subscription.id}"
        shown = st.session_state.get(shown_key)
        st.session_state[shown_key] = [entry.path for entry in doomed]
        if doomed:
            names = [Path(entry.path).name for entry in doomed]
            listed = "、".join(names[:10]) + (f" 等 {len(names)} 部" if len(names) > 10 else "")
            st.warning(escape_markdown(f"現在套用會刪除 {len(doomed)} 部：{listed}"))
        elif mode != "off":
            st.caption("目前沒有要刪除的影片。")
        label = f"儲存並刪除 {len(doomed)} 部" if doomed else "儲存"
        if st.button(label, key=f"subscription_retention_save_{subscription.id}", type="primary", width="stretch"):
            if shown != st.session_state[shown_key]:
                st.warning("要刪除的影片剛剛有變動，請看過清單後再按一次。")
            else:
                save_retention(ctx, subscription, keep_latest, keep_days)
                st.rerun()


def render_subscription_card(ctx: AppContext, storage: AppStorage, subscription: Subscription) -> None:
    with st.container(border=True):
        st.markdown(f"**{escape_markdown(subscription.title or subscription.url)}**")
        interval = SUBSCRIPTION_INTERVALS.get(subscription.interval_hours, f"每 {subscription.interval_hours} 小時")
        status = f"{interval} · 上次檢查 {format_time(subscription.last_checked_at)}"
        if subscription.last_result:
            status += f"：{subscription.last_result}"
        retention = retention_text(subscription.keep_latest, subscription.keep_days)
        details = f"{status} · 每次最多 {subscription.max_items} 部" + (f" · {retention}" if retention else "")
        st.caption(escape_markdown(f"{details} · {subscription.output_dir}"))
        # A row that wraps rather than columns: a sixth of the card put 「保留規則」 on two lines, and a phone stacks
        # columns into a tall pile of buttons.
        with st.container(horizontal=True, gap="small", vertical_alignment="center"):
            enabled = st.toggle("自動檢查", value=subscription.enabled, key=f"subscription_enabled_{subscription.id}")
            check = st.button("立即檢查", key=f"subscription_check_{subscription.id}", width="stretch")
            edited = render_edit_popover(subscription)
            render_retention_popover(ctx, subscription)
            delete = st.button("刪除", key=f"subscription_delete_{subscription.id}", width="stretch")
        if enabled != subscription.enabled:
            storage.update_subscription(subscription.id, enabled=enabled)
            st.rerun()
        if check:
            check_subscription_now(ctx, storage, subscription)
            st.rerun()
        if delete:
            storage.delete_subscription(subscription.id)
            st.rerun()
        # Under the row, across the whole card. The form keeps what was typed for another try.
        if edited and save_subscription_edit(ctx, storage, subscription, *edited):
            st.rerun()


def render_subscriptions_page(ctx: AppContext) -> None:
    storage = ctx.services.storage
    if storage is None:
        return
    render_settings_sidebar(ctx)
    page_header("訂閱", "追蹤頻道或播放清單，自動下載新影片。App 需要保持執行才會自動檢查。")
    if ctx.services.shares_data_folder:
        st.warning("另一個 App 正在使用同一個資料夾，自動檢查由那一個負責，這個視窗不會自動檢查。")
    if ctx.services.downloads_held():
        # The download window or a rate-limit pause holds the owner's downloads; a check would only queue another.
        st.info("現在不在下載時段，或 YouTube 暫時限制了請求：自動檢查會等到可以下載時再進行，「立即檢查」排入的任務也會等待。")
    render_add_form(ctx, storage)
    render_import_section(ctx, storage)

    subscriptions = storage.list_subscriptions()
    if subscriptions:
        if st.button("全部立即檢查", width="stretch"):
            for subscription in subscriptions:
                check_subscription_now(ctx, storage, subscription)
            st.rerun()
        for subscription in subscriptions:
            render_subscription_card(ctx, storage, subscription)
    else:
        st.caption("還沒有訂閱。")

    render_active_jobs(ctx)

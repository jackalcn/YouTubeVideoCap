from __future__ import annotations

import json
import platform
import sys
import time
from dataclasses import replace
from functools import partial
from importlib.metadata import PackageNotFoundError, version
from typing import Any

import streamlit as st
from yt_dlp.version import __version__ as YT_DLP_VERSION

from videocap import speech
from videocap.config import (
    APP_UPDATE_CHECK_SECONDS,
    APP_UPDATED_AT,
    APP_VERSION,
    DEPLOY_MARKER,
    DEPLOY_VERIFY_MIN_COMMIT,
    LOW_DISK_WARNING_BYTES,
    NAMING_TEMPLATE,
    YT_DLP_STALE_AFTER_DAYS,
)
from videocap.environment import autostart_enabled, autostart_supported, detect_runtime_commit_short, in_docker
from videocap.inspector import check_youtube_reachable
from videocap.notify import NotifyTargets, masked
from videocap.options import network_options
from videocap.services import YTDLP_UPDATE_BUSY_MESSAGE
from videocap.settings import BUILTIN_PRESETS, DownloadSettings, settings_from_dict
from videocap.storage import LAST_SETTINGS_KEY, ytdlp_args_without_secrets
from videocap.textfiles import decode_text
from videocap.tooling import (
    PIP_BUSY_MESSAGE,
    AppUpdateCheck,
    app_repository_url,
    app_update_check_from_dict,
    check_app_update,
    check_ytdlp_update,
    days_since_ytdlp_release,
    format_bytes,
    get_ffmpeg_status,
    get_ffprobe_status,
    get_free_disk_bytes,
    get_js_runtime_status,
    install_gpu_support,
    install_speech_recognition,
    read_tool_version,
    reset_tool_status,
)
from videocap.ui.common import escape_markdown, format_local_time, page_header
from videocap.ui.context import AppContext, apply_settings_to_state, collect_settings, current_output_dir
from videocap.ui.sidebar import save_preset


# An exported settings file is a few kilobytes.
SETTINGS_UPLOAD_MAX_MB = 1
APP_UPDATE_CHECK_KEY = "app_update_check"
SPEECH_INSTALL_NOTICE_KEY = "speech_install_notice"
GPU_INSTALL_NOTICE_KEY = "gpu_install_notice"

def render_meta_badges(
    version_text: str,
    updated_at: str,
    deploy_target: str,
    runtime_commit: str,
    min_commit: str,
    deploy_marker: str,
    ytdlp_version: str,
) -> None:
    st.markdown(
        (
            "<div class='meta-panel'>"
            "<div class='meta-panel-caption'>部署概況</div>"
            "<div class='badge-row badge-row-primary'>"
            "<div class='meta-badge'>"
            "<span class='badge-dot'></span>"
            "<span class='meta-badge-label'>版本</span>"
            f"<span class='meta-badge-value'>{version_text}</span>"
            "</div>"
            "<div class='meta-badge'>"
            "<span class='badge-dot'></span>"
            "<span class='meta-badge-label'>最後更新</span>"
            f"<span class='meta-badge-value'>{updated_at}</span>"
            "</div>"
            "<div class='meta-badge'>"
            "<span class='badge-dot'></span>"
            "<span class='meta-badge-label'>部署方式</span>"
            f"<span class='meta-badge-value'>{deploy_target}</span>"
            "</div>"
            "</div>"
            "<div class='meta-grid'>"
            "<div class='meta-card'>"
            "<div class='meta-card-label'>執行中的版本</div>"
            f"<div class='meta-card-value mono'>{runtime_commit}</div>"
            "</div>"
            "<div class='meta-card'>"
            "<div class='meta-card-label'>基準版本</div>"
            f"<div class='meta-card-value mono'>{min_commit}</div>"
            "</div>"
            "<div class='meta-card'>"
            "<div class='meta-card-label'>部署標記</div>"
            f"<div class='meta-card-value mono'>{deploy_marker}</div>"
            "</div>"
            "<div class='meta-card'>"
            "<div class='meta-card-label'>yt-dlp</div>"
            f"<div class='meta-card-value mono'>{ytdlp_version}</div>"
            "</div>"
            "</div>"
            "</div>"
        ),
        unsafe_allow_html=True,
    )


def known_running_commit(commit: str) -> str:
    # detect_runtime_commit_short falls back to the baseline commit when neither git nor the host names the running one.
    return "" if commit == DEPLOY_VERIFY_MIN_COMMIT else commit


def render_app_update(ctx: AppContext, running_commit: str) -> None:
    storage = ctx.services.storage
    check = app_update_check_from_dict(storage.get_value(APP_UPDATE_CHECK_KEY)) if storage else None
    pressed = st.button("檢查 App 新版本", key="app_update_button")
    # Asks by itself only once the owner has asked, and then at most once a day whatever the answer was. A clock set
    # back must not keep an old answer for good.
    due = check is not None and not 0 <= time.time() - check.checked_at < APP_UPDATE_CHECK_SECONDS
    if pressed or due:
        with st.spinner("向 GitHub 查詢中..."):
            check = check_app_update(proxy=network_options(collect_settings(), ctx.limits).proxy)
        if storage:
            storage.set_value(APP_UPDATE_CHECK_KEY, check.to_dict())
    if check is None:
        st.caption("向 GitHub 查詢這個 App 有沒有新版本。查過一次之後，打開這一頁時每天會自動再查一次。")
        return
    render_app_update_answer(check, running_commit)


def render_app_update_answer(check: AppUpdateCheck, running_commit: str) -> None:
    if check.error:
        st.error(escape_markdown(check.error))
    else:
        # Compared now rather than when asked, so a copy updated since then is not told to update again.
        newer, approximate = check.compare(running_commit, APP_VERSION)
        guess = "這份 App 沒有 git 版本紀錄，這是用日期大略比對的結果。" if approximate else ""
        if newer:
            start_script = "start.bat" if sys.platform == "win32" else "start.sh"
            st.info(
                f"GitHub 上有新版本（最後更新 {check.latest_date}）：請重新下載 zip 或執行 git pull，再執行 {start_script}。"
                f"[開啟 GitHub 頁面]({app_repository_url()}) {guess}"
            )
        else:
            st.success(f"已是最新版。{guess}")
    st.caption(f"上次查詢：{format_local_time(check.checked_at)}。每天最多自動查詢一次。")


def package_version(name: str) -> str:
    try:
        return version(name)
    except PackageNotFoundError:
        return "未安裝"


def render_dependency_status(ctx: AppContext) -> None:
    st.markdown("### 相依工具")
    ffmpeg_ok, ffmpeg_path = get_ffmpeg_status()
    ffprobe_ok, ffprobe_path = get_ffprobe_status()
    js_ok, js_path = get_js_runtime_status()
    rows = [
        {"項目": "Python", "狀態": "正常", "版本": platform.python_version(), "位置": sys.executable},
        {"項目": "yt-dlp", "狀態": "正常", "版本": YT_DLP_VERSION, "位置": ""},
        {"項目": "Streamlit", "狀態": "正常", "版本": package_version("streamlit"), "位置": ""},
        {
            "項目": "ffmpeg",
            "狀態": "正常" if ffmpeg_ok else "缺少",
            "版本": read_tool_version(ffmpeg_path, "-version") if ffmpeg_ok else "",
            "位置": ffmpeg_path,
        },
        {"項目": "ffprobe", "狀態": "正常" if ffprobe_ok else "缺少", "版本": "", "位置": ffprobe_path},
        {
            "項目": "Deno（JavaScript）",
            "狀態": "正常" if js_ok else "缺少",
            "版本": read_tool_version(js_path, "--version") if js_ok else "",
            "位置": js_path,
        },
    ]
    if not ctx.trusted:
        # Where a tool is, or why it is missing, names the server's folders.
        rows = [{key: value for key, value in row.items() if key != "位置"} for row in rows]
    st.dataframe(rows, width="stretch", hide_index=True)
    if st.button("重新偵測", width="content"):
        reset_tool_status()
        st.rerun()


def render_ytdlp_update(ctx: AppContext) -> None:
    st.markdown("### yt-dlp 更新")
    age = days_since_ytdlp_release()
    if age is not None and age > YT_DLP_STALE_AFTER_DAYS:
        st.warning(f"目前的 yt-dlp 已發布 {age} 天。YouTube 常改版，下載失敗時請先更新。")
    else:
        st.caption(f"YouTube 常改版。目前版本 {YT_DLP_VERSION}" + (f"，已發布 {age} 天。" if age is not None else "。"))

    proxy = network_options(collect_settings(), ctx.limits).proxy
    if ctx.trusted and not ctx.services.restart_required and not get_js_runtime_status()[0]:
        # yt-dlp's deno extra brings Deno along, so installing yt-dlp again is how a missing Deno is added.
        st.warning("未偵測到 Deno，目前可用的畫質可能受限。")
        if st.button(INSTALL_DENO_LABEL):
            run_ytdlp_update(ctx, proxy)
    if st.button("檢查新版本"):
        with st.spinner("查詢中..."):
            st.session_state["update_check"] = check_ytdlp_update(proxy=proxy)
    check = st.session_state.get("update_check")
    if check is None:
        return
    if check.error:
        st.error(escape_markdown(check.error))
    elif not check.has_update:
        st.success(f"已是最新版本（{check.current}）。")
    else:
        st.info(f"有新版本：{check.latest}（目前 {check.current}）。")
        # After an update only a restart helps, which the notice at the top of the page says.
        if ctx.trusted and not ctx.services.restart_required and st.button("更新 yt-dlp", type="primary"):
            run_ytdlp_update(ctx, proxy)


def run_ytdlp_update(ctx: AppContext, proxy: str) -> None:
    with st.spinner("更新中，約需一分鐘..."):
        succeeded, output = ctx.services.update_ytdlp(proxy=proxy)
    # pip's own output goes in a code block, where nothing in it is read as formatting.
    if output in (YTDLP_UPDATE_BUSY_MESSAGE, PIP_BUSY_MESSAGE):
        st.error(output)
    elif succeeded and ctx.services.restart_required:
        st.success("更新完成。下載會先暫停，請立即重新啟動 App 才能繼續下載。")
        render_restart_steps()
        st.code(output, language=None)
    elif succeeded:
        st.success(escape_markdown(output))
    else:
        st.error("更新失敗。")
        st.code(output, language=None)


def render_speech_recognition(ctx: AppContext) -> None:
    st.markdown("### 語音辨識")
    st.caption("聽影片或音訊裡說的話，自動做成字幕，在「工具」頁的「語音辨識」分頁使用。需要另外安裝 faster-whisper（約 125 MB），只要裝一次。")
    output = st.session_state.pop(SPEECH_INSTALL_NOTICE_KEY, None)
    if output is not None:
        st.success("語音辨識安裝完成，不需要重新啟動，現在就能到「工具」頁的「語音辨識」使用。")
        st.code(output, language=None)
    if speech.is_installed():
        st.write(f"目前：**已安裝**（faster-whisper {package_version('faster-whisper')}）")
        render_gpu_support(ctx)
    else:
        st.write("目前：未安裝")
        if st.button(speech.INSTALL_BUTTON_LABEL, key="speech_install"):
            install_speech(ctx)
    models = speech.downloaded_models()
    if models:
        st.markdown("已下載的語音模型：\n" + "\n".join(f"- {name}：{format_bytes(size)}" for name, size in models.items()))
    else:
        st.caption("還沒有下載語音模型。第一次辨識時，會自動下載選用的模型。")
    st.caption(f"模型存放位置：`{speech.models_dir()}`")


def install_speech(ctx: AppContext) -> None:
    with st.spinner("安裝中，需要下載約 125 MB，可能要幾分鐘..."):
        result = install_speech_recognition(proxy=network_options(collect_settings(), ctx.limits).proxy)
    if result.succeeded and not result.restart_required:
        # Told after a rerun, so the status above already says it is installed.
        st.session_state[SPEECH_INSTALL_NOTICE_KEY] = result.message
        st.rerun()
    # pip's own output goes in a code block, where nothing in it is read as formatting.
    if result.message == PIP_BUSY_MESSAGE:
        st.error(result.message)
    elif result.succeeded:
        st.warning("語音辨識已安裝，但要重新啟動 App 才能使用。")
        render_restart_steps()
        st.code(result.message, language=None)
    else:
        st.error("安裝失敗。")
        st.code(result.message, language=None)


def render_gpu_support(ctx: AppContext) -> None:
    # Only for a card CTranslate2 sees; without one there is nothing to install.
    if not speech.cuda_device_count():
        return
    output = st.session_state.pop(GPU_INSTALL_NOTICE_KEY, None)
    if output is not None:
        st.success("顯示卡支援安裝完成，不需要重新啟動，「工具」頁的「語音辨識」現在就能用顯示卡。")
        st.code(output, language=None)
    if speech.cuda_libraries_ready():
        st.write("顯示卡支援：**已就緒**（辨識時勾選「使用顯示卡」即可）")
        return
    st.write("顯示卡支援：未安裝")
    st.caption("這台電腦有 NVIDIA 顯示卡。裝上 NVIDIA 的 cuBLAS 程式庫後，辨識可以快好幾倍。")
    if st.button(speech.GPU_INSTALL_BUTTON_LABEL, key="gpu_install"):
        install_gpu(ctx)


def install_gpu(ctx: AppContext) -> None:
    with st.spinner("安裝中，需要下載約 650 MB，可能要十幾分鐘..."):
        result = install_gpu_support(proxy=network_options(collect_settings(), ctx.limits).proxy)
    # Its earlier answer was taken before the install.
    speech.cuda_libraries_ready.cache_clear()
    if result.succeeded and speech.cuda_libraries_ready():
        st.session_state[GPU_INSTALL_NOTICE_KEY] = result.message
        st.rerun()
    if result.message == PIP_BUSY_MESSAGE:
        st.error(result.message)
    elif result.succeeded:
        st.warning("已安裝，但還是載入不了 cuBLAS。請把 NVIDIA 顯示卡的驅動程式更新到最新版，重新啟動 App 後再試。")
        st.code(result.message, language=None)
    else:
        st.error("安裝失敗。")
        st.code(result.message, language=None)


def render_restart_steps() -> None:
    # A copy started at sign-in has no window to close, so closing start.bat's window does not restart it.
    if autostart_supported():
        st.caption(AUTOSTART_RESTART_STEPS)
    if in_docker():
        st.caption(DOCKER_RESTART_STEPS)


def render_shared_data_folder(ctx: AppContext) -> None:
    if ctx.trusted and ctx.services.shares_data_folder:
        st.warning(SHARED_DATA_FOLDER_MESSAGE)


def render_restart_notice(ctx: AppContext) -> None:
    # Downloads wait from the update until the restart, so this stays up for everyone until then.
    if not ctx.services.restart_required:
        return
    if ctx.trusted:
        st.warning("yt-dlp 已更新，下載暫停中。請重新啟動 App 才能繼續下載。")
        render_restart_steps()
    else:
        st.warning("伺服器的下載工具剛更新，下載暫停中，要等 App 重新啟動後才能繼續。")


def render_connection_test(ctx: AppContext) -> None:
    st.markdown("### 連線測試")
    if st.button("測試連上 YouTube"):
        with st.spinner("測試中..."):
            reachable, message = check_youtube_reachable(network=network_options(collect_settings(), ctx.limits))
        (st.success if reachable else st.error)(escape_markdown(message))


def render_storage_status(ctx: AppContext) -> None:
    st.markdown("### 儲存空間")
    output_dir = current_output_dir(ctx)
    free = get_free_disk_bytes(output_dir)
    st.write(f"下載資料夾：`{output_dir}`")
    if free is not None:
        st.write(f"硬碟剩餘：{format_bytes(free)}")
        if free < LOW_DISK_WARNING_BYTES:
            st.warning("硬碟空間不多了，大型影片可能無法下載。")
    if ctx.services.storage:
        st.write(f"資料庫與設定：`{ctx.services.storage.data_dir}`")


def imported_settings(data: dict[str, Any]) -> DownloadSettings:
    """A settings file may come from someone else. As with presets, how this machine connects and where a template
    could write files stay as they are. So do the extra yt-dlp options, whose --exec runs any command on this machine
    and which a custom preset saved from this applies each time it is chosen."""
    current = collect_settings()
    # A clip or a few playlist items are meant for one download, not for every one after.
    imported = replace(settings_from_dict(data), time_ranges="", playlist_items="", remove_ranges="")
    imported.proxy = current.proxy
    imported.custom_template = current.custom_template
    imported.ytdlp_extra_args = current.ytdlp_extra_args
    if imported.naming_rule == NAMING_TEMPLATE:
        imported.naming_rule = current.naming_rule
    return imported


def render_settings_transfer(ctx: AppContext) -> None:
    st.markdown("### 匯出／匯入設定")
    st.caption("把目前的下載設定存成檔案，帶到其他電腦或備份。cookies 不會包含在內。")
    settings = collect_settings()
    settings.proxy = ""
    # Extra options can hold a login, a password or an Authorization header.
    settings.ytdlp_extra_args = ytdlp_args_without_secrets(settings.ytdlp_extra_args)
    st.download_button(
        "匯出目前設定",
        data=json.dumps(settings.to_dict(), ensure_ascii=False, indent=2).encode("utf-8"),
        file_name="youtube_videocap_settings.json",
        mime="application/json",
        on_click="ignore",
    )
    uploaded = st.file_uploader(
        "匯入設定檔",
        type=["json"],
        key="settings_import_upload",
        max_upload_size=SETTINGS_UPLOAD_MAX_MB,
        # The browser refuses a larger file with only Streamlit's English tooltip, before this page can say why.
        help=f"最多 {SETTINGS_UPLOAD_MAX_MB} MB。超過的檔案瀏覽器會直接拒絕。",
    )
    if uploaded is not None:
        # Anyone can upload here, and guessing the encoding of megabytes of text holds the whole server for seconds at
        # every rerun. The browser's limit can be bypassed, so check again here.
        if uploaded.size > SETTINGS_UPLOAD_MAX_MB * 1024 * 1024:
            st.error(f"這個檔案太大，不是設定檔。設定檔最多 {SETTINGS_UPLOAD_MAX_MB} MB。")
            return
        try:
            data = json.loads(decode_text(uploaded.getvalue()))
        except (ValueError, RecursionError):
            st.error("這不是有效的設定檔。")
            return
        if not isinstance(data, dict):
            st.error("這不是有效的設定檔。")
            return
        st.caption("代理伺服器、yt-dlp 檔名範本和進階 yt-dlp 參數屬於這台電腦，不會從檔案匯入。")
        # Keyed by the file, so choosing another file suggests that file's name.
        name = st.text_input("組合名稱", value=uploaded.name.removesuffix(".json"), key=f"settings_import_name_{uploaded.file_id}")
        columns = st.columns(2)
        if columns[0].button("套用到目前設定", width="stretch"):
            imported = imported_settings(data)
            apply_settings_to_state(imported)
            storage = ctx.services.storage
            if ctx.trusted and storage:
                storage.set_value(LAST_SETTINGS_KEY, imported.to_dict())
            st.success("已套用。")
        if columns[1].button("存成自訂組合", width="stretch"):
            name = name.strip()
            if not name:
                st.error("請先輸入組合名稱。")
            elif name in BUILTIN_PRESETS:
                # A custom preset would take a built-in one's place, and the manage list cannot delete built-in names.
                st.error("這個名稱是內建組合，請換一個。")
            else:
                save_preset(ctx, name, imported_settings(data))
                st.success(f"已存成「{name}」。")


def render_backup(ctx: AppContext) -> None:
    st.markdown("### 備份與還原")
    st.caption(
        "包含下載紀錄、訂閱、設定組合與已下載紀錄。通知的金鑰和未完成的任務不會放進備份。"
        "代理伺服器的帳號密碼與進階 yt-dlp 參數裡的登入資料不會放進備份。"
    )
    st.download_button(
        "備份全部資料",
        # Built only when clicked: it copies the whole database.
        data=partial(ctx.services.create_backup, ctx.limits),
        file_name=f"videocap-backup-{time.strftime('%Y%m%d-%H%M')}.zip",
        mime="application/zip",
        icon=":material/backup:",
        on_click="ignore",
    )
    with st.expander("從備份還原"):
        # Restored settings include the extra yt-dlp options, whose --exec runs any command on this machine.
        st.caption("只還原你自己建立的備份：還原會帶回備份裡的所有設定，包括進階 yt-dlp 參數。")
        uploaded = st.file_uploader("備份檔（.zip）", type=["zip"], key="backup_restore_upload")
        if uploaded is not None:
            st.warning("還原會取代目前的下載紀錄、訂閱、設定組合與已下載紀錄。影片檔案本身不受影響。")
            if st.button("確定還原", type="primary", key="backup_restore_confirm"):
                try:
                    st.success(escape_markdown(ctx.services.restore_backup(uploaded.getvalue(), ctx.limits)))
                except ValueError as exc:
                    st.error(escape_markdown(str(exc)))
    with st.expander("匯入下載紀錄檔"):
        st.caption("yt-dlp 的 --download-archive 檔，或其他下載工具匯出的同格式檔案，每行像「youtube 影片ID」。匯入後這些影片會被略過，不再下載。")
        archive = st.file_uploader("下載紀錄檔（.txt）", type=["txt"], key="archive_import_upload")
        if archive is not None and st.button("加入已下載紀錄", key="archive_import_confirm"):
            added, known, other = ctx.services.import_download_archive(archive.getvalue(), ctx.limits)
            if not added and not known:
                st.error("檔案裡沒有找到 YouTube 影片的紀錄。每行應該像「youtube dQw4w9WgXcQ」。")
            else:
                message = f"已加入 {added} 部影片" + (f"，{known} 部原本就有" if known else "") + (f"，略過 {other} 行其他網站或無法辨識的內容" if other else "")
                st.success(message + "。")


AUTOSTART_RESTART_STEPS = (
    "如果 App 是開機自動啟動、在背景執行的：請登出 Windows 再登入；或在工作管理員結束 pythonw.exe，再執行 start.bat。"
)
DOCKER_RESTART_STEPS = "在 Docker 裡：執行 docker compose restart。更新存在資料 volume 裡，重建容器也會保留。"
INSTALL_DENO_LABEL = "安裝 Deno（重新安裝 yt-dlp）"
SHARED_DATA_FOLDER_MESSAGE = (
    "這台電腦上已經有另一個 App 在使用同一個資料夾（可能是開機自動啟動的背景程式）。"
    "這個視窗不會自動檢查訂閱，重新啟動後也不會接回未完成的任務。建議關閉這個視窗，改用已經在執行的那一個。"
)
NOTIFY_FIELDS = {
    "discord_webhook": ("Discord Webhook 網址", "https://discord.com/api/webhooks/..."),
    "telegram_token": ("Telegram Bot Token", "123456789:ABC..."),
    "telegram_chat_id": ("Telegram Chat ID", "例如 123456789"),
    "ntfy_url": ("ntfy 主題網址", "https://ntfy.sh/你的主題名稱"),
}


def save_notifications(ctx: AppContext) -> None:
    # A callback, the one moment Streamlit lets the page empty the fields in the browser too.
    saved = ctx.services.notify_targets()
    values: dict[str, str] = {}
    for name in NOTIFY_FIELDS:
        typed = str(st.session_state.get(f"notify_{name}") or "").strip()
        values[name] = "" if st.session_state.get(f"notify_remove_{name}") else typed or getattr(saved, name)
    try:
        ctx.services.save_notify_targets(NotifyTargets(**values), ctx.limits)
    except ValueError as exc:
        st.session_state["notify_error"] = str(exc)
        return
    for name in NOTIFY_FIELDS:
        # Set rather than dropped: the browser would keep the whole secret in the field, and send a removed target
        # back with the next save.
        st.session_state[f"notify_{name}"] = ""
        st.session_state.pop(f"notify_remove_{name}", None)
    st.session_state["notify_notice"] = "已儲存通知設定。"


def render_notifications(ctx: AppContext) -> None:
    st.markdown("### 通知")
    st.caption("下載完成或失敗、訂閱下載到新影片，以及 YouTube 限制請求而暫停佇列時，傳訊息到 Discord、Telegram 或 ntfy。App 需要保持執行。")
    saved = ctx.services.notify_targets()
    with st.form("notify_form"):
        for name, (label, placeholder) in NOTIFY_FIELDS.items():
            current = getattr(saved, name)
            # Never filled in with the saved value: the page would send the whole token to the browser.
            st.text_input(
                label,
                type="password",
                placeholder=f"{masked(current)}，留空保持不變" if current else placeholder,
                key=f"notify_{name}",
            )
            if current:
                st.checkbox(f"移除{label}", key=f"notify_remove_{name}")
        st.form_submit_button("儲存通知設定", type="primary", on_click=save_notifications, args=(ctx,))
    error = st.session_state.pop("notify_error", None)
    if error:
        st.error(escape_markdown(error))
    notice = st.session_state.pop("notify_notice", None)
    if notice:
        st.success(notice)
    if st.button("傳送測試通知", disabled=not saved.is_set, key="notify_test"):
        with st.spinner("傳送中..."):
            problems = ctx.services.send_test_notification(ctx.limits)
        if problems:
            st.error(escape_markdown("沒有全部送出：" + "；".join(problems)))
        else:
            st.success("已送出測試通知，請到手機或電腦上查看。")


def render_autostart(ctx: AppContext) -> None:
    st.markdown("### 開機自動啟動")
    enabled = autostart_enabled()
    st.write("目前：**已開啟**" if enabled else "目前：未開啟")
    st.caption(
        "登入 Windows 時在背景啟動 App，不開任何視窗，訂閱和排程下載就會自動進行。"
        "背景執行時請直接用瀏覽器開 http://localhost:8501，不必再執行 start.bat；只有這台電腦能連線。"
    )
    if st.button("關閉開機自動啟動" if enabled else "開啟開機自動啟動", key="autostart_toggle"):
        problem = ctx.services.set_autostart(not enabled, ctx.limits)
        if problem:
            st.error(escape_markdown(problem))
        else:
            st.session_state["autostart_notice"] = (
                "已關閉。正在背景執行的 App 會繼續執行，直到登出或重新開機。" if enabled else "已開啟。下次登入 Windows 時會在背景啟動。"
            )
            st.rerun()
    notice = st.session_state.pop("autostart_notice", None)
    if notice:
        st.success(notice)


def render_system_page(ctx: AppContext) -> None:
    page_header("系統", "檢查環境、更新工具與備份設定。" if ctx.trusted else "查看相依工具、測試連線，以及匯出或匯入下載設定。")
    render_restart_notice(ctx)
    render_shared_data_folder(ctx)
    deploy_target = "雲端（main 分支自動部署）" if ctx.is_cloud else "本機"
    if ctx.trusted:
        runtime_commit = detect_runtime_commit_short()
        render_meta_badges(
            version_text=APP_VERSION,
            updated_at=APP_UPDATED_AT,
            deploy_target=deploy_target,
            runtime_commit=runtime_commit,
            min_commit=DEPLOY_VERIFY_MIN_COMMIT,
            deploy_marker=DEPLOY_MARKER,
            ytdlp_version=YT_DLP_VERSION,
        )
        render_app_update(ctx, known_running_commit(runtime_commit))
    else:
        # Which commit runs here tells a stranger exactly which code to look at.
        st.caption(f"部署方式：{deploy_target}")
    render_dependency_status(ctx)
    # A visitor cannot update, so the server does not ask PyPI on their behalf either.
    if ctx.trusted:
        render_ytdlp_update(ctx)
        # Installs into this machine's Python and names its folders, so only for the owner.
        render_speech_recognition(ctx)
    render_connection_test(ctx)
    if ctx.trusted:
        render_storage_status(ctx)
    render_settings_transfer(ctx)
    if ctx.trusted and ctx.services.storage:
        render_notifications(ctx)
        render_backup(ctx)
    # Only for someone sitting at this machine: it is their Windows sign-in that would start the app.
    if ctx.trusted and ctx.browser_login_allowed and autostart_supported():
        render_autostart(ctx)

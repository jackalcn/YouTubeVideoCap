from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import streamlit as st

from videocap.config import (
    AUDIO_BITRATE_OPTIONS,
    AUDIO_FORMAT_OPTIONS,
    CHANNEL_TAB_OPTIONS,
    CLOUD_MAX_COMMENTS,
    CLOUD_MAX_WAIT_SECONDS,
    COMMENTS_MODE_OPTIONS,
    CONTAINER_OPTIONS,
    COOKIE_EXPORT_STEPS,
    FOLDER_MEDIA_SERVER,
    FOLDER_RULES,
    LIVE_MODE_OPTIONS,
    LOGIN_BROWSER,
    LOGIN_BROWSERS,
    LOGIN_COOKIES,
    LOGIN_NONE,
    MEDIA_TYPE_OPTIONS,
    NAMING_CUSTOM,
    NAMING_TEMPLATE,
    PLAYER_CLIENT_OPTIONS,
    SPONSORBLOCK_CATEGORIES,
    SPONSORBLOCK_MODES,
    VIDEO_CODEC_OPTIONS,
    VIDEO_QUALITY_OPTIONS,
)
from videocap.errors import JS_RUNTIME_HINT
from videocap.inspector import check_youtube_login
from videocap.library import folder_creation_problem
from videocap.options import (
    BITRATE_AUDIO_FORMATS,
    MARK_ONLY_SPONSORBLOCK_CATEGORIES,
    DownloadOptions,
    LoginSource,
    embeds_thumbnail,
    network_options,
    parse_extra_ytdlp_args,
    parse_subtitle_languages,
)
from videocap.services import temporary_cookie_file
from videocap.settings import BUILTIN_PRESETS, SETTING_KEY_PREFIX, SETTING_RANGES, DownloadSettings, settings_from_dict
from videocap.storage import PRESETS_KEY
from videocap.ui.common import escape_markdown, public_request_slot
from videocap.ui.context import (
    OUTPUT_DIR_WIDGET_KEY,
    AppContext,
    apply_settings_to_state,
    collect_settings,
    current_browser,
    current_cookie_bytes,
    current_output_dir,
)


PRESET_SELECT_KEY = "preset_choice"
PRESET_PLACEHOLDER = "選擇要套用的組合..."
# Saved beside the settings in the owner's own presets, which settings_from_dict ignores.
PRESET_FOLDER_KEY = "output_dir"
PRESET_FOLDER_CHECKBOX_KEY = "preset_remember_folder"
LOGIN_METHOD_LABELS = {"none": LOGIN_NONE, "browser": LOGIN_BROWSER, "cookies": LOGIN_COOKIES}
CONNECTION_SETTINGS = ("player_client", "ytdlp_extra_args")


def opt(name: str) -> str:
    return f"{SETTING_KEY_PREFIX}{name}"


def setting(name: str) -> Any:
    return st.session_state.get(opt(name))


def all_presets(ctx: AppContext) -> dict[str, dict[str, Any]]:
    presets: dict[str, dict[str, Any]] = dict(BUILTIN_PRESETS)
    storage = ctx.services.storage
    if ctx.trusted and storage:
        presets.update(storage.load_presets())
    presets.update(st.session_state.get("session_presets", {}))
    return presets


def apply_chosen_preset(ctx: AppContext) -> None:
    # Runs as a widget callback, the one moment Streamlit allows setting other widgets' values.
    name = st.session_state.get(PRESET_SELECT_KEY)
    preset = all_presets(ctx).get(str(name))
    if preset is None:
        return
    # How to connect belongs to this machine, not to the preset.
    current = collect_settings()
    chosen = settings_from_dict(preset)
    chosen.proxy = current.proxy
    # A built-in preset says nothing about these, and choosing one should not undo how this machine reaches YouTube.
    for field_name in CONNECTION_SETTINGS:
        if field_name not in preset:
            setattr(chosen, field_name, getattr(current, field_name))
    apply_settings_to_state(chosen)
    notice = f"已套用「{name}」。"
    folder = preset.get(PRESET_FOLDER_KEY)
    # Only the owner chooses where files go on this machine; a visitor's files stay in the server's own folder.
    if ctx.trusted and isinstance(folder, str) and folder.strip():
        problem = folder_creation_problem(Path(folder).expanduser())
        if problem:
            st.session_state["preset_folder_problem"] = f"組合記住的下載資料夾無法使用，下載資料夾維持不變：{problem}"
        else:
            st.session_state[OUTPUT_DIR_WIDGET_KEY] = folder
            notice = f"已套用「{name}」，下載資料夾改為 {escape_markdown(folder)}。"
    st.session_state["preset_notice"] = notice
    st.session_state[PRESET_SELECT_KEY] = PRESET_PLACEHOLDER


def save_named_preset(ctx: AppContext) -> None:
    # A callback, so the preset list above the button already includes the new one when the page redraws.
    name = str(st.session_state.get("preset_new_name") or "").strip()
    if not name:
        st.session_state["preset_notice"] = "請先輸入組合名稱。"
    elif name in BUILTIN_PRESETS:
        st.session_state["preset_notice"] = "這個名稱是內建組合，請換一個。"
    else:
        remember_folder = ctx.trusted and bool(st.session_state.get(PRESET_FOLDER_CHECKBOX_KEY))
        save_preset(ctx, name, collect_settings(), str(current_output_dir(ctx)) if remember_folder else "")
        st.session_state["preset_new_name"] = ""
        st.session_state["preset_notice"] = f"已儲存「{name}」。"


def delete_chosen_preset(ctx: AppContext) -> None:
    name = str(st.session_state.get("preset_delete_choice") or "")
    delete_preset(ctx, name)
    st.session_state["preset_notice"] = f"已刪除「{name}」。"


def save_preset(ctx: AppContext, name: str, settings: DownloadSettings, output_dir: str = "") -> None:
    """output_dir is the folder to switch to when the preset is applied; only the owner's presets keep one."""
    storage = ctx.services.storage
    if ctx.trusted and storage:
        if output_dir:
            presets = storage.load_presets()
            presets[name] = {**settings.to_dict(), PRESET_FOLDER_KEY: output_dir}
            storage.set_value(PRESETS_KEY, presets)
        else:
            storage.save_preset(name, settings)
    else:
        # Visitors of a public host share the server, so their presets stay in their own session.
        session_presets = dict(st.session_state.get("session_presets", {}))
        session_presets[name] = settings.to_dict()
        st.session_state["session_presets"] = session_presets


def delete_preset(ctx: AppContext, name: str) -> None:
    storage = ctx.services.storage
    if ctx.trusted and storage:
        storage.delete_preset(name)
    session_presets = dict(st.session_state.get("session_presets", {}))
    session_presets.pop(name, None)
    st.session_state["session_presets"] = session_presets


def render_presets(ctx: AppContext) -> None:
    presets = all_presets(ctx)
    custom_names = [name for name in presets if name not in BUILTIN_PRESETS]
    st.session_state.setdefault(PRESET_SELECT_KEY, PRESET_PLACEHOLDER)
    folder_help = (
        "代理伺服器不會被更改；下載資料夾只在儲存組合時勾選了「一併記住目前的下載資料夾」才會更改"
        if ctx.trusted
        else "下載資料夾和代理伺服器不會被更改"
    )
    st.selectbox(
        "預設組合",
        options=[PRESET_PLACEHOLDER, *presets],
        key=PRESET_SELECT_KEY,
        on_change=apply_chosen_preset,
        args=(ctx,),
        help=f"一次套用常用的設定。{folder_help}；內建組合也不會更改 YouTube 用戶端與進階參數。",
    )
    notice = st.session_state.pop("preset_notice", None)
    if notice:
        st.caption(notice)
    folder_problem = st.session_state.pop("preset_folder_problem", None)
    if folder_problem:
        st.warning(escape_markdown(folder_problem))

    with st.popover("儲存／管理組合", width="stretch"):
        st.text_input("組合名稱", key="preset_new_name", placeholder="例如：課程影片 720p")
        if ctx.trusted:
            st.checkbox(
                "一併記住目前的下載資料夾",
                key=PRESET_FOLDER_CHECKBOX_KEY,
                help=f"套用這個組合時，下載資料夾會換成現在的：{escape_markdown(str(current_output_dir(ctx)))}",
            )
        st.button("把目前設定存成組合", key="preset_save", width="stretch", on_click=save_named_preset, args=(ctx,))
        if custom_names:
            st.selectbox("刪除自訂組合", options=custom_names, key="preset_delete_choice")
            st.button("刪除", key="preset_delete", width="stretch", on_click=delete_chosen_preset, args=(ctx,))


def render_content_settings(ctx: AppContext) -> None:
    st.markdown("### 下載內容")
    st.radio(
        "類型",
        options=list(MEDIA_TYPE_OPTIONS),
        format_func=MEDIA_TYPE_OPTIONS.get,
        horizontal=True,
        key=opt("media_type"),
    )
    media_type = setting("media_type")
    if media_type == "video":
        st.selectbox("畫質上限", options=list(VIDEO_QUALITY_OPTIONS), key=opt("quality"))
        with st.expander("編碼與容器"):
            st.selectbox("影像編碼", options=list(VIDEO_CODEC_OPTIONS), format_func=VIDEO_CODEC_OPTIONS.get, key=opt("video_codec"))
            st.selectbox("輸出容器", options=CONTAINER_OPTIONS, key=opt("container"), disabled=not ctx.ffmpeg_available)
            st.checkbox("優先選擇 HDR", key=opt("prefer_hdr"))
            st.caption("1080p 以下優先選 H.264，相容性最好。1440p 以上只有 VP9／AV1，部分播放器可能無法播放；需要時可到「工具」頁轉成 H.264。")
    elif media_type == "audio":
        # An earlier preset may have chosen a format this machine cannot produce.
        if setting("audio_format") not in ctx.audio_formats:
            st.session_state[opt("audio_format")] = ctx.audio_formats[0]
        st.selectbox("音訊格式", options=ctx.audio_formats, format_func=AUDIO_FORMAT_OPTIONS.get, key=opt("audio_format"))
        # Other formats keep YouTube's own audio as it is, so a bitrate would change nothing.
        if setting("audio_format") in BITRATE_AUDIO_FORMATS and ctx.ffmpeg_available:
            st.selectbox(
                "音訊位元率（kbps）",
                options=AUDIO_BITRATE_OPTIONS,
                key=opt("audio_bitrate"),
                help="YouTube 的音訊來源約 130–160 kbps，選更高不會增加音質，只會增加檔案大小。",
            )
    else:
        st.caption("只取得下方勾選的字幕、逐字稿、封面與資訊檔，不下載影音。")

    if media_type != "none":
        st.text_input(
            "音軌語言",
            key=opt("audio_language"),
            placeholder="留空為影片預設音軌",
            help="影片有多語言配音時可指定，例如 ja、en、zh-TW。解析連結可查看可用語言；沒有該語言時使用預設音軌。",
        )


def render_subtitle_settings(ctx: AppContext) -> None:
    with st.expander("字幕與逐字稿"):
        st.checkbox("下載字幕", key=opt("download_subtitles"))
        st.text_input(
            "字幕語言代碼",
            key=opt("subtitle_languages"),
            help="以逗號分隔，支援正規表示式，例如 ja,ko 或 zh.*；填 all 代表全部上傳者字幕。",
        )
        st.selectbox("字幕格式", options=["srt", "vtt"], key=opt("subtitle_format"))
        st.checkbox(
            "包含自動產生字幕",
            key=opt("include_auto_subtitles"),
            help="包含語音辨識與機器翻譯的字幕。YouTube 常以 HTTP 429 拒絕機器翻譯字幕，此時只會取得原語言字幕。",
        )
        if setting("include_auto_subtitles") and "all" in parse_subtitle_languages(str(setting("subtitle_languages") or "")):
            st.caption("語言填 all 時不會抓自動產生字幕，以免觸發 YouTube 限流。")
        st.checkbox(
            "簡體字幕自動轉成台灣繁體",
            key=opt("subtitles_to_traditional"),
            help="需要勾選「下載字幕」，且字幕語言包含簡體中文（例如 zh.*）。下載到的中文字幕只有簡體時，另存一份台灣用語的繁體字幕"
            "（.zh-TW），原本的字幕也會保留；勾選「嵌入字幕到影片」時，繁體字幕也會一起嵌入。",
        )
        if setting("media_type") == "video":
            st.checkbox("嵌入字幕到影片", key=opt("embed_subtitles"), disabled=not ctx.ffmpeg_available)
            if setting("container") == "webm" and setting("embed_subtitles") and setting("download_subtitles"):
                st.caption("WebM 影片只能嵌入 VTT 字幕，所以字幕會存成 .vtt，不受上方字幕格式影響。")
        st.checkbox(
            "同時產生純文字逐字稿（.txt）",
            key=opt("write_transcript"),
            help="把字幕整理成沒有時間碼的文字，並移除自動字幕的重複行。方便閱讀或貼給其他工具。",
        )


def render_clip_settings(ctx: AppContext) -> None:
    if setting("media_type") == "none":
        return
    with st.expander("片段、章節與贊助片段"):
        st.text_input(
            "只下載片段",
            key=opt("time_ranges"),
            placeholder="例如 1:30-2:00, 10:00-",
            disabled=not ctx.ffmpeg_available,
            help="以逗號分隔多段，每段各存成一個檔案；結尾留空代表到影片結束。需要 ffmpeg。",
        )
        # Re-encoding holds the server's CPU for a long time and cannot be stopped part-way, so visitors do not get it.
        st.checkbox(
            "精準剪切",
            key=opt("precise_cuts"),
            disabled=not ctx.ffmpeg_available or not ctx.limits.trusted,
            help="重新編碼切點附近的畫面，切點才會精準，但速度較慢。不勾選時會對齊到最近的關鍵影格。"
            if ctx.limits.trusted
            else "公開主機不提供精準剪切，切點會對齊到最近的關鍵影格。",
        )
        st.text_input(
            "移除片段",
            key=opt("remove_ranges"),
            placeholder="例如 0:00-0:30, 10:00-10:45",
            disabled=not ctx.ffmpeg_available,
            help="把這些時間範圍從影片中剪掉，其餘接成一個檔案。和「只下載片段」同時填時，這一欄不會使用。需要 ffmpeg。",
        )
        st.checkbox("嵌入章節", key=opt("embed_chapters"), disabled=not ctx.ffmpeg_available)
        st.checkbox("依章節分割成多個檔案", key=opt("split_chapters"), disabled=not ctx.ffmpeg_available)
        st.selectbox(
            "SponsorBlock（社群標記的業配片段）",
            options=list(SPONSORBLOCK_MODES),
            format_func=SPONSORBLOCK_MODES.get,
            key=opt("sponsorblock_mode"),
            disabled=not ctx.ffmpeg_available,
            help="查詢 sponsor.ajay.app 的社群資料。移除時會重新封裝影片，並非所有影片都有資料。",
        )
        if setting("sponsorblock_mode") != "off":
            render_sponsorblock_categories()


def render_sponsorblock_categories() -> None:
    categories = list(SPONSORBLOCK_CATEGORIES)
    if setting("sponsorblock_mode") == "remove":
        # A highlight or a community chapter is a spot in the video, which can be marked but not cut out.
        categories = [name for name in categories if name not in MARK_ONLY_SPONSORBLOCK_CATEGORIES]
        chosen = list(setting("sponsorblock_categories") or [])
        if any(name not in categories for name in chosen):
            st.session_state[opt("sponsorblock_categories")] = [name for name in chosen if name in categories]
    st.multiselect(
        "片段類型",
        options=categories,
        format_func=SPONSORBLOCK_CATEGORIES.get,
        key=opt("sponsorblock_categories"),
        help="「社群章節」與「精華起點」只能標記為章節，不能移除。",
    )


def cover_can_be_embedded() -> bool:
    # Asked of the engine, which leaves the cover out of a WebM video or WAV audio.
    chosen = DownloadOptions(
        output_dir=Path(),
        output_template="",
        media_type=str(setting("media_type")),
        container=str(setting("container")),
        audio_format=str(setting("audio_format")),
        embed_thumbnail=True,
    )
    return embeds_thumbnail(chosen)


def render_extra_file_settings(ctx: AppContext) -> None:
    with st.expander("封面與附加檔案"):
        media_type = setting("media_type")
        if media_type != "none":
            st.checkbox("寫入影片資訊（標題、頻道、日期）", key=opt("embed_metadata"), disabled=not ctx.ffmpeg_available)
            st.checkbox(
                "音樂標籤（拆出歌手與歌名、方形封面）",
                key=opt("music_tags"),
                disabled=not ctx.ffmpeg_available,
                help="把「歌手 - 歌名」或「歌手【歌名】」格式的標題拆成歌手與歌名，並去掉【MV】等字樣；拆不出來時保留原標題，"
                "歌手用頻道名稱。下載音訊時，嵌入或另存的封面會裁成正方形。",
            )
            can_embed_cover = cover_can_be_embedded()
            st.checkbox("嵌入封面圖", key=opt("embed_thumbnail"), disabled=not ctx.ffmpeg_available or not can_embed_cover)
            if not can_embed_cover:
                st.caption("WebM 影片與 WAV 音訊無法嵌入封面圖。需要封面時，請勾選下方的「另存封面圖」。")
        st.checkbox("另存封面圖", key=opt("write_thumbnail"))
        st.checkbox(
            "封面存成 JPG",
            key=opt("thumbnails_as_jpg"),
            disabled=not ctx.ffmpeg_available,
            help="YouTube 的封面多是 WebP，部分電視和舊的看圖程式打不開。勾選後另存與嵌入的封面都會是 JPG。需要 ffmpeg。",
        )
        st.checkbox("另存影片說明（.description）", key=opt("write_description"))
        st.checkbox("另存完整資訊（.info.json）", key=opt("write_infojson"))
        if media_type == "video":
            st.checkbox(
                "將影片資訊嵌入 MKV",
                key=opt("embed_infojson"),
                disabled=not ctx.ffmpeg_available or setting("container") != "mkv",
                help="把完整影片資訊附加在 MKV 檔裡，方便日後封存。只有 MKV 容器能附加；登入下載時不會附加，以免帶入登入資訊。",
            )
        render_comment_settings(ctx)


def render_comment_settings(ctx: AppContext) -> None:
    st.selectbox(
        "另存留言",
        options=list(COMMENTS_MODE_OPTIONS),
        format_func=COMMENTS_MODE_OPTIONS.get,
        key=opt("comments_mode"),
        help="把留言另存成檔案。留言很多時會多花一些時間。",
    )
    if setting("comments_mode") != "off":
        # A public host shares its few slots, so its visitors get the same cap as the comment export.
        most = SETTING_RANGES["comments_max"][1] if ctx.trusted else CLOUD_MAX_COMMENTS
        if int(setting("comments_max") or 1) > most:
            st.session_state[opt("comments_max")] = most
        st.number_input("留言數量上限", min_value=1, max_value=int(most), step=50, key=opt("comments_max"))
    if ctx.trusted:
        st.checkbox(
            "另存直播聊天室紀錄",
            key=opt("save_live_chat"),
            help="已結束的直播才有聊天室重播。聊天室很長時需要較久的時間。",
        )


def render_playlist_settings(ctx: AppContext) -> None:
    with st.expander("播放清單與頻道"):
        current_limit = int(setting("playlist_limit") or 1)
        if current_limit > ctx.limits.max_playlist:
            st.session_state[opt("playlist_limit")] = ctx.limits.max_playlist
        st.number_input("每個播放清單／頻道最多下載幾部", min_value=1, max_value=ctx.limits.max_playlist, key=opt("playlist_limit"))
        st.text_input(
            "只下載指定項目",
            key=opt("playlist_items"),
            placeholder="例如 1-5,8,10",
            help="清單中的編號，仍受上方的部數上限限制。解析播放清單後也可以直接勾選。",
        )
        st.selectbox("頻道內容", options=list(CHANNEL_TAB_OPTIONS), format_func=CHANNEL_TAB_OPTIONS.get, key=opt("channel_tab"))
        st.checkbox("反向順序（從清單尾端開始）", key=opt("playlist_reverse"))
        st.checkbox(
            "影片網址帶 list 參數時下載整份清單",
            key=opt("expand_playlist"),
            help="預設只下載網址指向的那一部影片。",
        )
        if ctx.trusted:
            st.checkbox(
                "略過已下載過的影片",
                key=opt("skip_archived"),
                help="用下載紀錄檔記住抓過的影片，適合定期更新同一份清單。",
            )
            st.selectbox(
                "遇到直播",
                options=list(LIVE_MODE_OPTIONS),
                format_func=LIVE_MODE_OPTIONS.get,
                key=opt("live_mode"),
                disabled=not ctx.ffmpeg_available,
                help="錄製會持續到直播結束，或在佇列按停止為止。停止時已錄到的內容會保留。需要 ffmpeg。",
            )
            if setting("live_mode") == "record":
                st.number_input("錄製長度上限（分鐘）", min_value=0, key=opt("live_max_minutes"), help="0 代表不限。")
            st.checkbox(
                "直播或首播尚未開始時等待開播",
                key=opt("wait_for_live"),
                # The stream is recorded once it starts, and without ffmpeg that would fail only after the wait.
                disabled=not ctx.ffmpeg_available,
                help="只對單一影片的連結有效，播放清單與頻道不會等待。任務會在佇列中等到開播，開播後直接開始錄製，"
                "不受「遇到直播」影響。預定 24 小時後才開播，或等了 24 小時仍未開播時，會停止等待。等待時會佔用一個同時下載名額。需要 ffmpeg。",
            )


def render_filter_settings(ctx: AppContext) -> None:
    with st.expander("篩選條件"):
        columns = st.columns(2)
        columns[0].number_input("最短片長（分鐘）", min_value=0, key=opt("filter_min_minutes"))
        columns[1].number_input("最長片長（分鐘）", min_value=0, key=opt("filter_max_minutes"), help="0 代表不限。")
        columns = st.columns(2)
        columns[0].text_input("上傳日期從", key=opt("filter_date_after"), placeholder="2026-01-01")
        columns[1].text_input("上傳日期到", key=opt("filter_date_before"), placeholder="2026-12-31")
        st.number_input("最少觀看次數", min_value=0, step=1000, key=opt("filter_min_views"))
        st.text_input("標題必須包含", key=opt("filter_title_include"), help="不分大小寫，支援正規表示式。" if ctx.trusted else "不分大小寫。")
        st.text_input("標題不可包含", key=opt("filter_title_exclude"))
        st.checkbox("略過 Shorts", key=opt("filter_skip_shorts"))
        st.checkbox(
            "只下載 Creative Commons 授權影片",
            key=opt("filter_cc_only"),
            help="下載前會逐一檢查每部影片的授權。",
        )


def render_output_settings(ctx: AppContext) -> None:
    with st.expander("輸出"):
        if setting("naming_rule") not in ctx.naming_rules:
            st.session_state[opt("naming_rule")] = ctx.naming_rules[0]
        st.radio("輸出檔名規則", options=ctx.naming_rules, key=opt("naming_rule"))
        if setting("naming_rule") == NAMING_CUSTOM:
            st.text_input("自訂檔名", key=opt("custom_name"))
        if setting("naming_rule") == NAMING_TEMPLATE:
            st.text_input(
                "yt-dlp 檔名範本",
                key=opt("custom_template"),
                placeholder="%(channel)s/%(upload_date)s - %(title)s.%(ext)s",
                help="語法見 yt-dlp 說明的 OUTPUT TEMPLATE 一節，可用 / 建立子資料夾。",
            )

        st.checkbox(
            "優先使用繁體中文標題與說明",
            key=opt("prefer_traditional_metadata"),
            help="作者有提供繁體中文翻譯時，檔名與影片資訊使用翻譯後的標題和說明；沒有翻譯時維持原文。",
        )

        if ctx.trusted:
            st.text_input("下載資料夾", key=OUTPUT_DIR_WIDGET_KEY)
            st.selectbox("資料夾分類", options=list(FOLDER_RULES), format_func=FOLDER_RULES.get, key=opt("folder_rule"))
            if setting("folder_rule") == FOLDER_MEDIA_SERVER:
                st.caption("檔案會放在「頻道／Season 年份」資料夾，檔名是「上傳日期 標題 [ID]」，上方的檔名規則不會使用。")
            st.checkbox(
                "產生 .nfo 資訊檔",
                key=opt("write_nfo"),
                help="在影片旁存一個 .nfo 檔，讓 Plex、Jellyfin、Kodi 顯示標題、簡介、日期與頻道。"
                "同時勾選「另存封面圖」時，也會存一張「檔名-thumb.jpg」當作縮圖。只下載字幕與資訊檔時不會產生。",
            )
        else:
            st.caption("檔案暫存於伺服器，下載完成後請使用按鈕存到你的電腦。")
        st.checkbox("覆蓋已存在的檔案", key=opt("overwrite_existing"), help="取消勾選時，已存在的檔案會直接保留，不再下載。")
        st.checkbox("檔名只用英數字元", key=opt("restrict_filenames"), help="讓檔名在舊系統或 NAS 上也能正常使用。")
        st.checkbox(
            "檔案時間改為上傳日期",
            key=opt("mtime_upload_date"),
            help="把檔案的修改時間設成影片的上傳日期，媒體庫和檔案總管依時間排序時就會照上傳順序。",
        )


def render_network_settings(ctx: AppContext) -> None:
    with st.expander("網路與效能"):
        st.slider("重試次數", min_value=1, max_value=10, key=opt("retry_count"))
        st.number_input(
            "下載限速（MB/s）",
            min_value=0.0,
            step=0.5,
            key=opt("rate_limit_mb"),
            help="0 代表不限速。每個任務各自計算。",
        )
        # Waiting holds one of a public host's few slots, so the engine waits at most this long for its visitors.
        # An imported value above it is lowered here, or Streamlit would put it back to 0.
        most_wait = SETTING_RANGES["sleep_seconds"][1] if ctx.trusted else CLOUD_MAX_WAIT_SECONDS
        if int(setting("sleep_seconds") or 0) > most_wait:
            st.session_state[opt("sleep_seconds")] = most_wait
        st.number_input(
            "每部影片之間等待（秒）",
            min_value=0,
            max_value=int(most_wait),
            key=opt("sleep_seconds"),
            help=None if ctx.trusted else f"公開主機最多等待 {CLOUD_MAX_WAIT_SECONDS} 秒。",
        )
        st.checkbox("強制使用 IPv4", key=opt("force_ipv4"))
        st.selectbox(
            "YouTube 用戶端",
            options=list(PLAYER_CLIENT_OPTIONS),
            format_func=PLAYER_CLIENT_OPTIONS.get,
            key=opt("player_client"),
            help="遇到 HTTP 403 或可選的畫質變少時，可以換一個試試。不同用戶端取得的格式與字幕可能不同。登入時 visionos 無法使用，會改用預設。",
        )
        if ctx.trusted:
            st.text_input("代理伺服器", key=opt("proxy"), placeholder="http://127.0.0.1:7890", help="公司或校園網路需要時才填。")
            render_extra_ytdlp_args()


def render_extra_ytdlp_args() -> None:
    st.text_area(
        "進階 yt-dlp 參數",
        key=opt("ytdlp_extra_args"),
        placeholder="--embed-info-json --sponsorblock-chapter-title \"[%(category_names)l]\"",
        height=80,
        help="給熟悉 yt-dlp 的人：填 yt-dlp 命令列選項，每次下載都會套用。含空格或 \\ 的值請加引號。"
        "儲存位置、檔名、登入、進度等由 App 管理的選項會被忽略。"
        "不能使用：--username 等帳號密碼登入，請改用「會員登入」；"
        "-s、--skip-download、-F、--print、-j、--get-title 等只列資訊、不下載的選項，只要字幕或資訊請把「類型」選成「僅字幕與資訊」。"
        "--wait-for-video 會被忽略，請改勾「播放清單與頻道」裡的等待開播。",
    )
    raw = str(setting("ytdlp_extra_args") or "")
    if not raw.strip():
        return
    try:
        extra = parse_extra_ytdlp_args(raw)
    except ValueError as exc:
        st.warning(str(exc))
        return
    if extra.ignored:
        st.caption(f"這些選項由 App 管理，已忽略：{'、'.join(extra.ignored)}")


def render_login_settings(ctx: AppContext) -> None:
    st.markdown("### 會員登入")
    methods = ["none", "browser", "cookies"] if ctx.browser_login_allowed else ["none", "cookies"]
    if st.session_state.get("keep_login_method") not in methods:
        st.session_state["keep_login_method"] = "none"
    st.radio(
        "YouTube 帳號",
        options=methods,
        format_func=LOGIN_METHOD_LABELS.get,
        key="keep_login_method",
        help="下載已加入會員的頻道影片、年齡限制或私人影片時需要登入。YouTube 不開放第三方用帳號密碼登入，所以這裡使用瀏覽器的登入狀態。",
    )
    method = st.session_state["keep_login_method"]
    if method == "browser":
        st.selectbox(
            "瀏覽器",
            options=[key for key in LOGIN_BROWSERS if key != "safari" or sys.platform == "darwin"],
            format_func=LOGIN_BROWSERS.get,
            key="keep_login_browser",
        )
        if sys.platform == "win32":
            st.caption("Windows 上的 Chrome、Edge 常因加密或檔案鎖定而讀不到，建議使用 Firefox 或 cookies.txt。")
        elif sys.platform.startswith("linux") and st.session_state.get("keep_login_browser") != "firefox":
            # Chromium-family browsers keep their key in the desktop's keyring, which yt-dlp cannot always open.
            st.caption("Linux 上的 Chrome、Edge 等瀏覽器有時讀不到登入資訊。如果確認登入狀態時顯示沒有登入，請改用 Firefox 或 cookies.txt。")
    elif method == "cookies":
        render_cookie_upload(ctx)
    if not ctx.is_cloud and not ctx.browser_login_allowed:
        st.caption("讀取瀏覽器登入狀態只在本機以 localhost 開啟時提供。")

    if method != "none":
        check_login = st.button(
            "確認登入狀態",
            width="stretch",
            disabled=method == "cookies" and current_cookie_bytes() is None,
        )
        if check_login:
            with public_request_slot(ctx) as allowed:
                if allowed:
                    with temporary_cookie_file(current_cookie_bytes()) as cookie_file:
                        login_level, login_message = check_youtube_login(
                            LoginSource(cookie_file, current_browser()), network=network_options(collect_settings(), ctx.limits)
                        )
                    {"success": st.success, "warning": st.warning, "error": st.error}[login_level](login_message)
        st.caption(
            "cookies 等同登入憑證：App 不會另存，上傳的暫存檔在任務結束時刪除。"
            "yt-dlp 官方提醒帳號有被暫時或永久封鎖的風險，請控制下載量；登入時任務會逐一執行，每個檔案間隔 5–10 秒。"
        )
        if ctx.is_cloud:
            st.warning("這是公開部署的主機，cookies 會傳到伺服器。會員影片建議在自己的電腦上下載。")


def forget_uploaded_cookies(ctx: AppContext) -> None:
    st.session_state["cookies_bytes"] = None
    # Queued and paused jobs hold their own copy, which must not outlive the person's choice to log out.
    ctx.services.manager.forget_login(ctx.owner)


def store_uploaded_cookies(ctx: AppContext) -> None:
    # Only a change of the uploader itself gets here, so its ✕ logs out while moving between pages does not.
    uploaded = st.session_state.get("cookies_upload")
    if uploaded is None:
        forget_uploaded_cookies(ctx)
    else:
        st.session_state["cookies_bytes"] = uploaded.getvalue()
        st.session_state["cookies_name"] = uploaded.name


def render_cookie_upload(ctx: AppContext) -> None:
    # An upload widget cannot be restored after moving to another page, so the bytes are kept for this session instead.
    uploaded = st.file_uploader(
        "cookies.txt（Netscape 格式）", type=["txt"], key="cookies_upload", on_change=store_uploaded_cookies, args=(ctx,)
    )
    if uploaded is None and st.session_state.get("cookies_bytes") is not None:
        st.caption(f"已載入 {st.session_state.get('cookies_name', 'cookies.txt')}。")
        st.button("移除 cookies", key="cookies_clear", width="stretch", on_click=forget_uploaded_cookies, args=(ctx,))
    with st.expander("如何匯出 cookies.txt"):
        st.markdown(COOKIE_EXPORT_STEPS)


def render_tool_warnings(ctx: AppContext) -> None:
    if not ctx.ffmpeg_available:
        install_hints = {"darwin": "brew install ffmpeg", "win32": "winget install Gyan.FFmpeg"}
        install_hint = install_hints.get(sys.platform, "apt install ffmpeg")
        st.warning(
            "未偵測到 ffmpeg。仍可下載，但 YouTube 的單檔格式通常只有 360p，且無法轉成 MP3、剪輯片段或嵌入字幕。"
            f"若要高畫質，請先安裝 ffmpeg（例如 {install_hint}）。"
        )
        st.caption(f"ffmpeg 狀態：{ctx.ffmpeg_status}")
    if not ctx.js_runtime_available:
        st.warning(f"未偵測到 Deno，目前可用格式可能受限。{JS_RUNTIME_HINT}")
        st.caption(f"Deno 狀態：{ctx.js_runtime_status}")


def render_settings_sidebar(ctx: AppContext) -> None:
    with st.sidebar:
        st.markdown("## 下載設定")
        render_presets(ctx)
        render_content_settings(ctx)
        render_subtitle_settings(ctx)
        render_clip_settings(ctx)
        render_extra_file_settings(ctx)
        render_playlist_settings(ctx)
        render_filter_settings(ctx)
        render_output_settings(ctx)
        render_network_settings(ctx)
        render_tool_warnings(ctx)
        render_login_settings(ctx)

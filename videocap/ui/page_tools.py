from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Optional

import streamlit as st

from videocap import speech
from videocap.chinese import ChineseConversionUnavailable, to_traditional, traditional_copy_path
from videocap.jobs import FINISHED_STATES
from videocap.library import LANGUAGE_SEGMENT_RE, VIDEO_EXTENSIONS, LibraryEntry, get_media_duration, scan_library
from videocap.media_tools import (
    BURN_FONT_SIZES,
    COMPRESS_HEIGHTS,
    COMPRESS_LEVELS,
    GIF_FRAME_RATES,
    GIF_MAX_SECONDS,
    GIF_WIDTHS,
    LOUDNESS_TARGETS,
    STYLED_SUBTITLE_SUFFIXES,
    ToolRequest,
    default_cjk_font,
    hardware_h264_encoder,
)
from videocap.services import KIND_TOOL
from videocap.subtitles import Cue, cues_to_srt, cues_to_text, cues_to_vtt, format_timestamp_short, merge_bilingual, parse_subtitles
from videocap.textfiles import decode_text
from videocap.tooling import get_ffmpeg_status
from videocap.ui.common import escape_markdown, page_header, render_active_jobs, render_job_card, render_job_result
from videocap.ui.context import AppContext, current_output_dir
from videocap.urls import parse_timestamp


SOURCE_KEY = "keep_tool_source"
SUBTITLE_OUTPUTS = {"txt": "純文字逐字稿（.txt）", "txt_timed": "含時間碼的文字（.txt）", "srt": "SRT", "vtt": "WebVTT"}
# Any visitor can upload here, and even a three-hour subtitle file is well under this.
SUBTITLE_UPLOAD_MAX_MB = 2
# The browser refuses a bigger file before this page sees it, and says so only in Streamlit's English tooltip.
SUBTITLE_UPLOAD_HELP = f"最多 {SUBTITLE_UPLOAD_MAX_MB} MB。超過的檔案瀏覽器會直接拒絕。"
REPLACEMENT_CHARACTER = chr(0xFFFD)
COMPRESS_MODES = {"level": "選擇壓縮程度", "target": "指定檔案大小"}
GPU_LABEL = "使用顯示卡加速（較快，檔案稍大）"
BURN_POSITION_LABELS ={"bottom": "下方", "top": "上方"}
BURN_BORDER_LABELS = {"outline": "文字外框", "box": "半透明底框"}
TRADITIONAL_HELP ="把簡體中文轉成台灣繁體與用語，例如「软件」變成「軟體」。人名或專有名詞偶爾會被改到，原檔不受影響。"
SPEECH_TRADITIONAL_HELP = "Whisper 常把中文寫成簡體字。勾選後會轉成台灣繁體與用語，例如「软件」變成「軟體」；人名或專有名詞偶爾會被改到。"
# The language choices that can come out as Chinese, so only these offer the conversion.
SPEECH_CHINESE_CHOICES = frozenset({"auto", "zh"})


def typed_path(raw: Any) -> Path:
    # ffmpeg runs in a scratch folder, so a relative path is turned into a full one here.
    return Path(str(raw).strip().strip('"')).expanduser().resolve()


def is_before_end(source: Path, seconds: float, label: str) -> bool:
    duration = get_media_duration(source)
    if duration is not None and seconds >= duration:
        st.error(f"{label}超過了影片長度（{format_timestamp_short(duration)}）。")
        return False
    return True


def parse_optional_time(raw: str, label: str) -> Optional[float]:
    if not raw.strip():
        return None
    try:
        return parse_timestamp(raw.strip())
    except ValueError:
        st.error(f"看不懂{label}「{raw}」，請用 1:30 或 90 這樣的寫法。")
        raise


def render_source_picker(entries: list[LibraryEntry]) -> Optional[Path]:
    """The source can be any file on this machine, so a tool also works on videos from elsewhere."""
    options = [str(entry.path) for entry in entries]
    if st.session_state.get(SOURCE_KEY) not in options:
        current = st.session_state.get(SOURCE_KEY)
        if current:
            options.insert(0, str(current))
    choice = st.selectbox(
        "來源檔案",
        options=options,
        format_func=lambda value: Path(value).name,
        key=SOURCE_KEY,
        index=0 if options else None,
        placeholder="下載資料夾裡還沒有檔案",
        accept_new_options=True,
        help="也可以直接貼上其他檔案的完整路徑。結果會存在來源檔案旁邊，不會覆蓋原檔。",
    )
    if not choice:
        return None
    path = typed_path(choice)
    if not path.is_file():
        st.error(f"找不到檔案：{escape_markdown(str(path))}")
        return None
    return path


def subtitle_candidates(entries: list[LibraryEntry], source: Path) -> list[Path]:
    for entry in entries:
        if entry.path == source:
            return entry.subtitles
    return []


def submit_tool(ctx: AppContext, tool: str, source: Path, params: Optional[dict[str, Any]] = None) -> None:
    ctx.services.submit_tool(ToolRequest(tool, str(source), params or {}), ctx.owner)
    st.rerun()


def render_trim_tool(ctx: AppContext, source: Path) -> None:
    columns = st.columns(2)
    start = columns[0].text_input("開始時間", placeholder="1:30", key="trim_start")
    end = columns[1].text_input("結束時間（留空到結尾）", placeholder="2:45", key="trim_end")
    precise = st.checkbox(
        "精準剪切（重新編碼，較慢）",
        key="trim_precise",
        help="影片會轉成 H.264（WebM 等格式存成 MP4），片段不含可開關的字幕軌。不勾選時只能從關鍵畫格開始，但會保留所有軌道。",
    )
    if st.button("開始剪輯", key="trim_run", type="primary", width="stretch"):
        try:
            start_at, end_at = parse_optional_time(start, "開始時間"), parse_optional_time(end, "結束時間")
        except ValueError:
            return
        if end_at is not None and end_at <= (start_at or 0):
            st.error("結束時間要晚於開始時間。")
            return
        if not is_before_end(source, start_at or 0, "開始時間"):
            return
        submit_tool(ctx, "trim", source, {"start": start_at, "end": end_at, "precise": precise})


def render_gpu_option(key: str) -> dict[str, Any]:
    """The GPU checkbox, shown only when this machine has a hardware encoder that works."""
    available, ffmpeg = get_ffmpeg_status()
    if not available or not hardware_h264_encoder(ffmpeg):
        return {}
    chosen = st.checkbox(GPU_LABEL, key=key, help="用顯示卡的編碼器處理，速度快很多。失敗時會自動改用處理器。")
    return {"gpu": True} if chosen else {}


def render_convert_tool(ctx: AppContext, source: Path) -> None:
    st.caption("轉成幾乎所有裝置都能播放的 MP4。1440p 以上的 VP9／AV1 影片特別需要。檔案大時需要較長時間。")
    gpu = render_gpu_option("convert_gpu")
    if st.button("轉成 H.264 MP4", key="convert_run", type="primary", width="stretch"):
        submit_tool(ctx, "to_h264", source, gpu)


def render_compress_tool(ctx: AppContext, source: Path) -> None:
    mode = st.radio("方式", options=list(COMPRESS_MODES), format_func=COMPRESS_MODES.get, horizontal=True, key="compress_mode")
    columns = st.columns(2)
    if mode == "target":
        target_mb = columns[0].number_input(
            "目標大小（MB）",
            min_value=0.1,
            max_value=100_000.0,
            value=25.0,
            step=1.0,
            key="compress_target_mb",
            help="例如通訊軟體或 Email 附件的大小上限。會分兩次處理，需要較長時間。",
        )
        params: dict[str, Any] = {"target_mb": target_mb}
    else:
        level = columns[0].selectbox("壓縮程度", options=list(COMPRESS_LEVELS), index=1, key="compress_level")
        params = {"crf": COMPRESS_LEVELS[level]}
    height = columns[1].selectbox("解析度", options=list(COMPRESS_HEIGHTS), key="compress_height", help="以影片較短的一邊計算，直式影片也一樣。")
    # Hitting a target size takes libx264's two passes, which hardware encoders do not have.
    if mode != "target":
        params.update(render_gpu_option("compress_gpu"))
    if st.button("開始壓縮", key="compress_run", type="primary", width="stretch"):
        submit_tool(ctx, "compress", source, {**params, "height": COMPRESS_HEIGHTS[height]})


def render_audio_tool(ctx: AppContext, source: Path) -> None:
    columns = st.columns(2)
    audio_format = columns[0].selectbox("格式", options=["mp3", "m4a", "opus", "flac", "wav"], key="audio_tool_format")
    bitrate = columns[1].selectbox("位元率（kbps）", options=[128, 192, 256, 320], index=1, key="audio_tool_bitrate")
    if st.button("抽出音訊", key="audio_tool_run", type="primary", width="stretch"):
        submit_tool(ctx, "extract_audio", source, {"format": audio_format, "bitrate": bitrate})


def render_subtitle_merge_tool(ctx: AppContext, source: Path, entries: list[LibraryEntry]) -> None:
    candidates = subtitle_candidates(entries, source)
    # One widget per source: with typed paths allowed, Streamlit keeps a choice even when it is not among the new
    # options, so the previous video's subtitles would be used.
    source_key = hashlib.sha1(str(source).encode("utf-8")).hexdigest()[:12]
    subtitle = st.selectbox(
        "字幕檔",
        options=[str(path) for path in candidates],
        format_func=lambda value: Path(value).name,
        key=f"merge_subtitle_{source_key}",
        index=0 if candidates else None,
        placeholder="輸入字幕檔路徑",
        accept_new_options=True,
    )
    mode = st.radio("方式", options=["embed_subtitles", "burn_subtitles"], format_func={"embed_subtitles": "嵌入（播放時可開關，速度快）", "burn_subtitles": "燒錄（永久顯示在畫面上，需重新編碼）"}.get, key="merge_mode")
    style = {**render_burn_style(subtitle), **render_gpu_option("burn_gpu")} if mode == "burn_subtitles" else {}
    if st.button("開始處理", key="merge_run", type="primary", width="stretch", disabled=not subtitle):
        path = typed_path(subtitle)
        if not path.is_file():
            st.error(f"找不到字幕檔：{escape_markdown(str(path))}")
            return
        submit_tool(ctx, mode, source, {"subtitle_path": str(path), **style})


def render_burn_style(subtitle: Optional[str]) -> dict[str, Any]:
    with st.expander("字幕樣式"):
        if subtitle and Path(subtitle).suffix.lower() not in STYLED_SUBTITLE_SUFFIXES:
            st.caption("這個字幕檔有自己的樣式，以下設定只用在 SRT／VTT 字幕。")
        columns = st.columns(2)
        size = columns[0].selectbox("字體大小", options=list(BURN_FONT_SIZES), index=list(BURN_FONT_SIZES).index("標準"), key="burn_font_size")
        font = columns[1].text_input(
            "字型", value=default_cjk_font(), placeholder="自動", key="burn_font_name", help="這台電腦上已安裝的字型名稱。預設是能顯示中文的字型。"
        )
        position = columns[0].radio("位置", options=list(BURN_POSITION_LABELS), format_func=BURN_POSITION_LABELS.get, horizontal=True, key="burn_position")
        border = columns[1].radio(
            "文字背景", options=list(BURN_BORDER_LABELS), format_func=BURN_BORDER_LABELS.get, horizontal=True, key="burn_border", help="底框在畫面很亮或很雜時比較好讀。"
        )
    # Size, position and background are sent only when changed, so libass otherwise keeps its own defaults. The font
    # is always sent, because libass's own choice, Arial, has no Chinese and falls back to a font of its picking.
    style: dict[str, Any] = {"font_name": font.strip()} if font.strip() else {}
    if size != "標準":
        style["font_size"] = BURN_FONT_SIZES[size]
    if position != "bottom":
        style["position"] = position
    if border != "outline":
        style["border"] = border
    return style


def render_frame_tool(ctx: AppContext, source: Path) -> None:
    moment = st.text_input("時間點", value="0:00", key="frame_at")
    if st.button("擷取畫面", key="frame_run", type="primary", width="stretch"):
        try:
            at = parse_optional_time(moment, "時間點")
        except ValueError:
            return
        if not is_before_end(source, at or 0, "時間點"):
            return
        submit_tool(ctx, "frame", source, {"at": at or 0})


def render_loudness_tool(ctx: AppContext, source: Path) -> None:
    st.caption("把太小聲或忽大忽小的聲音調成一致的音量，適合講座、Podcast，或音量差很多的影片。影像不會重新壓縮。")
    target = st.radio(
        "目標響度", options=list(LOUDNESS_TARGETS), horizontal=True, key="loudnorm_target", help="標準適合手機與網路影片；較安靜是電視廣播用的音量。"
    )
    if st.button("開始標準化", key="loudnorm_run", type="primary", width="stretch"):
        submit_tool(ctx, "loudnorm", source, {"loudness": LOUDNESS_TARGETS[target]})


def natural_key(path: Path) -> list[Any]:
    # "Part 2" before "Part 10".
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", path.name)]


def join_candidates(entries: list[LibraryEntry], source: Path) -> list[Path]:
    """The videos in the source's folder, by name, so parts numbered 1, 2, 3 are listed in that order."""
    beside = {entry.path for entry in entries if entry.kind == "video" and entry.path.parent == source.parent}
    if source not in beside:
        # A path typed in from outside the library: its own folder, without the folders inside it.
        try:
            beside = {path for path in source.parent.iterdir() if path.suffix.lower() in VIDEO_EXTENSIONS and path.is_file()}
        except OSError:
            beside = set()
    return sorted(beside | {source}, key=natural_key)


def render_join_tool(ctx: AppContext, source: Path, entries: list[LibraryEntry]) -> None:
    st.caption(
        "把同一個資料夾裡的幾部影片依序接成一部。第一部決定畫面大小，形狀不同的影片會等比例縮放並補上黑邊。"
        "會重新編碼成 H.264 MP4，需要一些時間。"
    )
    candidates = [str(path) for path in join_candidates(entries, source)]
    # Keyed on the files offered, so another folder, or a new file in this one, starts again from the source alone.
    listing = hashlib.sha1("\n".join(candidates).encode("utf-8")).hexdigest()[:12]
    chosen = st.multiselect(
        "要合併的檔案（依選取的順序）",
        options=candidates,
        default=[str(source)],
        format_func=lambda value: Path(value).name,
        key=f"join_files_{listing}",
        placeholder="選擇影片",
    )
    gpu = render_gpu_option("join_gpu")
    if st.button("開始合併", key="join_run", type="primary", width="stretch"):
        if len(chosen) < 2:
            st.error("請至少選兩個檔案來合併。")
            return
        submit_tool(ctx, "join", Path(chosen[0]), {"paths": chosen, **gpu})


def render_gif_tool(ctx: AppContext, source: Path) -> None:
    st.caption(f"把一小段影片做成會動的圖片，方便貼到聊天室或網頁。最長 {GIF_MAX_SECONDS} 秒；畫面越寬、每秒張數越多，檔案就越大。")
    columns = st.columns(2)
    start = columns[0].text_input("開始時間", value="0:00", key="gif_start")
    end = columns[1].text_input("結束時間", value="0:05", key="gif_end")
    width = columns[0].selectbox("寬度（像素）", options=GIF_WIDTHS, index=1, key="gif_width", help="比影片本身寬時不會放大。")
    fps = columns[1].selectbox("每秒張數", options=GIF_FRAME_RATES, index=1, key="gif_fps", help="張數越多動作越順，檔案也越大。")
    if st.button("轉成 GIF", key="gif_run", type="primary", width="stretch"):
        try:
            start_at, end_at = parse_optional_time(start, "開始時間") or 0, parse_optional_time(end, "結束時間")
        except ValueError:
            return
        if end_at is None:
            st.error("請填入結束時間。")
            return
        if end_at <= start_at:
            st.error("結束時間要晚於開始時間。")
            return
        if end_at - start_at > GIF_MAX_SECONDS:
            st.error(f"GIF 最長 {GIF_MAX_SECONDS} 秒，請縮短時間範圍。")
            return
        if not is_before_end(source, start_at, "開始時間"):
            return
        submit_tool(ctx, "gif", source, {"start": start_at, "end": end_at, "width": width, "fps": fps})


def render_speech_tool(ctx: AppContext, source: Path) -> None:
    if not speech.is_installed():
        st.info(speech.NOT_INSTALLED_MESSAGE)
        return
    st.caption("聽影片或音訊裡說的話，自動做成 SRT 字幕，適合沒有字幕的影片。完成後會出現在下方的「工具結果」，也會跟影片一起列在「媒體庫」。")
    # Asked only once installed, and only for the owner: it loads CTranslate2, and cuBLAS when there is a card.
    gpu_seen = speech.cuda_device_count() > 0
    gpu_ready = gpu_seen and speech.cuda_libraries_ready()
    models = list(speech.WHISPER_MODELS)
    recommended = speech.recommended_model(gpu_ready)
    columns = st.columns(2)
    model = columns[0].selectbox(
        "模型",
        options=models,
        index=models.index(recommended),
        format_func=lambda name: f"{name}（{speech.WHISPER_MODELS[name].size}{'，建議' if name == recommended else ''}）",
        key="speech_model",
        help="越大越準確，也越慢。",
    )
    language = columns[1].selectbox(
        "語言",
        options=list(speech.SPEECH_LANGUAGES),
        format_func=speech.SPEECH_LANGUAGES.get,
        key="speech_language",
        help="知道影片說哪種語言時直接選，比自動偵測穩定。",
    )
    choice = speech.WHISPER_MODELS[model]
    download = "模型已下載。" if speech.is_model_downloaded(model) else f"第一次使用這個模型會先下載（{choice.size}），之後就不必再下載。"
    st.caption(f"{choice.caption}{download}")
    params: dict[str, Any] = {"model": model, "language": language}
    if language in SPEECH_CHINESE_CHOICES:
        params["traditional"] = st.checkbox("簡體轉成台灣繁體", value=True, key="speech_traditional", help=SPEECH_TRADITIONAL_HELP)
    if gpu_ready:
        # Ticked, since the card is there and ready, and the recommended model counts on it.
        if st.checkbox("使用顯示卡", value=True, key="speech_gpu", help="用 NVIDIA 顯示卡辨識，快很多。無法使用時會自動改用處理器。"):
            params["gpu"] = True
    elif gpu_seen:
        st.caption(speech.GPU_MISSING_TEXT)
    if st.button("開始辨識", key="speech_run", type="primary", width="stretch"):
        submit_tool(ctx, "transcribe", source, params)


def render_media_tools(ctx: AppContext) -> None:
    entries = scan_library(current_output_dir(ctx))
    if not ctx.ffmpeg_available:
        st.warning("這些工具需要 ffmpeg。安裝後請到「系統」頁重新偵測。")
        return

    source = render_source_picker(entries)
    if source is None:
        return

    tabs = st.tabs(["剪輯", "轉檔", "壓縮", "抽音訊", "字幕合併", "擷取畫面", "音量標準化", "合併影片", "轉成 GIF", "語音辨識"])
    with tabs[0]:
        render_trim_tool(ctx, source)
    with tabs[1]:
        render_convert_tool(ctx, source)
    with tabs[2]:
        render_compress_tool(ctx, source)
    with tabs[3]:
        render_audio_tool(ctx, source)
    with tabs[4]:
        render_subtitle_merge_tool(ctx, source, entries)
    with tabs[5]:
        render_frame_tool(ctx, source)
    with tabs[6]:
        render_loudness_tool(ctx, source)
    with tabs[7]:
        render_join_tool(ctx, source, entries)
    with tabs[8]:
        render_gif_tool(ctx, source)
    with tabs[9]:
        render_speech_tool(ctx, source)


def convert_subtitle_text(content: str, output: str, traditional: bool = False) -> str:
    cues = parse_subtitles(content)
    if output == "srt":
        converted = cues_to_srt(cues)
    elif output == "vtt":
        converted = cues_to_vtt(cues)
    else:
        converted = cues_to_text(cues, with_timestamps=output == "txt_timed")
    # After the format change, so only the text that is kept goes through OpenCC; it is linear in the text's length.
    return to_traditional(converted) if traditional else converted


def read_subtitle_upload(uploaded: Any) -> Optional[str]:
    """The upload's text, or None after telling the person why it cannot be used."""
    # The browser's limit can be bypassed, so check again here.
    if uploaded.size > SUBTITLE_UPLOAD_MAX_MB * 1024 * 1024:
        st.error(f"「{escape_markdown(uploaded.name)}」太大，字幕檔最多 {SUBTITLE_UPLOAD_MAX_MB} MB。")
        return None
    content = decode_text(uploaded.getvalue())
    if REPLACEMENT_CHARACTER in content:
        st.warning(f"「{escape_markdown(uploaded.name)}」有些字無法辨識，可能不是常見的文字編碼，結果可能出現亂碼。")
    return content


def result_stem(name: str, traditional: bool) -> str:
    # "clip.zh-Hans.srt" converted to Traditional is named "clip.zh-TW", so players label it correctly.
    return (traditional_copy_path(Path(name)) if traditional else Path(name)).stem


def render_subtitle_result(converted: str, file_name: str, key: str) -> None:
    st.text_area("預覽", value=converted[:5000], height=200, disabled=True, key=f"{key}_preview_{hashlib.sha1(converted.encode('utf-8')).hexdigest()[:12]}")
    st.download_button(
        f"下載 {Path(file_name).suffix}",
        data=converted.encode("utf-8"),
        file_name=file_name,
        mime="text/plain",
        width="stretch",
        key=f"{key}_download",
        on_click="ignore",
    )


def render_format_converter() -> None:
    st.caption("把 SRT／VTT 字幕轉成其他格式或純文字。只在這個工作階段處理，不會保存。")
    uploaded = st.file_uploader(
        "字幕檔", type=["srt", "vtt"], key="subtitle_convert_upload", max_upload_size=SUBTITLE_UPLOAD_MAX_MB, help=SUBTITLE_UPLOAD_HELP
    )
    output = st.selectbox("轉成", options=list(SUBTITLE_OUTPUTS), format_func=SUBTITLE_OUTPUTS.get, key="subtitle_convert_output")
    traditional = st.checkbox("轉成台灣繁體", key="subtitle_convert_traditional", help=TRADITIONAL_HELP)
    if uploaded is None:
        return
    content = read_subtitle_upload(uploaded)
    if content is None:
        return
    try:
        converted = convert_subtitle_text(content, output, traditional)
    except ChineseConversionUnavailable as exc:
        st.error(str(exc))
        return
    if not converted:
        st.warning("這個檔案裡沒有讀到字幕內容。")
        return
    suffix = "txt" if output.startswith("txt") else output
    render_subtitle_result(converted, f"{result_stem(uploaded.name, traditional)}.{suffix}", "subtitle_convert")


def bilingual_name(top_name: str, output: str) -> str:
    # "clip.zh-Hans.srt" becomes "clip.雙語.srt": the language part no longer describes the file.
    stem = Path(top_name).stem
    base, dot, segment = stem.rpartition(".")
    if dot and LANGUAGE_SEGMENT_RE.match(segment):
        stem = base
    return f"{stem}.雙語.{output}"


def merge_subtitle_texts(top: str, bottom: str, output: str, traditional: bool = False) -> str:
    return merge_subtitle_cues(parse_subtitles(top), parse_subtitles(bottom), output, traditional)


def merge_subtitle_cues(top: list[Cue], bottom: list[Cue], output: str, traditional: bool = False) -> str:
    cues = merge_bilingual(top, bottom)
    if not cues:
        return ""
    merged = cues_to_vtt(cues) if output == "vtt" else cues_to_srt(cues)
    return to_traditional(merged) if traditional else merged


def render_bilingual_merger() -> None:
    st.caption("把兩個語言的字幕合成一個檔案，每句同時顯示兩行，適合學語言。時間不同步的句子會單獨顯示。")
    columns = st.columns(2)
    top = columns[0].file_uploader(
        "上面一行（例如中文）", type=["srt", "vtt"], key="bilingual_top_upload", max_upload_size=SUBTITLE_UPLOAD_MAX_MB, help=SUBTITLE_UPLOAD_HELP
    )
    bottom = columns[1].file_uploader(
        "下面一行（例如英文）", type=["srt", "vtt"], key="bilingual_bottom_upload", max_upload_size=SUBTITLE_UPLOAD_MAX_MB, help=SUBTITLE_UPLOAD_HELP
    )
    columns = st.columns(2)
    output = columns[0].radio("存成", options=["srt", "vtt"], format_func=SUBTITLE_OUTPUTS.get, horizontal=True, key="bilingual_output")
    traditional = columns[1].checkbox("轉成台灣繁體", key="bilingual_traditional", help=TRADITIONAL_HELP)
    if top is None or bottom is None:
        return
    top_text, bottom_text = read_subtitle_upload(top), read_subtitle_upload(bottom)
    if top_text is None or bottom_text is None:
        return
    top_cues, bottom_cues = parse_subtitles(top_text), parse_subtitles(bottom_text)
    if not top_cues and not bottom_cues:
        st.warning("這兩個檔案裡沒有讀到字幕內容。")
        return
    # Either mistake still gives a file, which would otherwise be named 雙語 without being two languages.
    for uploaded, cues in ((top, top_cues), (bottom, bottom_cues)):
        if not cues:
            st.warning(f"「{escape_markdown(uploaded.name)}」裡沒有讀到字幕，結果只有另一個檔案的字幕。")
    if top_cues == bottom_cues:
        st.warning("上下兩個檔案的字幕一樣，每句會出現兩次。請確認選的是兩種語言的字幕。")
    try:
        merged = merge_subtitle_cues(top_cues, bottom_cues, output, traditional)
    except ChineseConversionUnavailable as exc:
        st.error(str(exc))
        return
    render_subtitle_result(merged, bilingual_name(top.name, output), "bilingual")


def render_subtitle_converter() -> None:
    st.markdown("### 字幕轉換")
    convert_tab, bilingual_tab = st.tabs(["轉換格式", "雙語字幕"])
    with convert_tab:
        render_format_converter()
    with bilingual_tab:
        render_bilingual_merger()


def render_bookmarklet() -> None:
    st.markdown("### 從 YouTube 頁面一鍵送到這裡")
    base_url = (st.context.url or "").split("?")[0].rstrip("/")
    for suffix in ("/tools", "/queue", "/search", "/library", "/subscriptions", "/history", "/system"):
        base_url = base_url.removesuffix(suffix)
    bookmarklet = f"javascript:void(window.open('{base_url}/?url='+encodeURIComponent(location.href)))"
    st.caption("在瀏覽器新增一個書籤，網址填入下面這行。在 YouTube 影片頁點一下書籤，連結就會帶到下載頁（不會自動開始）。")
    st.code(bookmarklet, language=None, wrap_lines=True)


def render_tool_results(ctx: AppContext) -> None:
    jobs = [job for job in ctx.services.manager.list_jobs(ctx.owner) if job.kind == KIND_TOOL and job.state in FINISHED_STATES]
    if jobs:
        st.markdown("### 工具結果")
        for job in reversed(jobs[-5:]):
            render_job_card(job, ctx)
            # Also shows a tool's warning, such as subtitles burned in as boxes.
            render_job_result(job, ctx)


def render_tools_page(ctx: AppContext) -> None:
    page_header("工具", "處理已下載的檔案。結果存在原檔旁邊，不會覆蓋原檔。" if ctx.trusted else "字幕轉換工具。")
    if ctx.trusted:
        render_media_tools(ctx)
        render_active_jobs(ctx)
        render_tool_results(ctx)
    render_subtitle_converter()
    render_bookmarklet()

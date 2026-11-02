from __future__ import annotations

from collections.abc import Iterator, Mapping
import html
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import time
import uuid
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any, Optional, Protocol, TypedDict, cast
from urllib.parse import urlparse

import streamlit as st
import yt_dlp
from yt_dlp.postprocessor import FFmpegEmbedSubtitlePP, PostProcessor
from yt_dlp.utils import MaxDownloadsReached
from yt_dlp.version import __version__ as YT_DLP_VERSION


APP_VERSION = "v2026.11.03.1"
APP_UPDATED_AT = "2026-11-03"
DEPLOY_VERIFY_MIN_COMMIT = "7f71b98"
DEPLOY_MARKER = "build-2026-11-03-01-youtube"

YOUTUBE_HOSTS = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
        "youtube-nocookie.com",
        "www.youtube-nocookie.com",
    }
)
YOUTUBE_EXTRACTORS = ["youtube", "youtube:.*"]

VIDEO_QUALITY_OPTIONS: dict[str, Optional[int]] = {
    "最佳畫質": None,
    "2160p (4K)": 2160,
    "1440p (2K)": 1440,
    "1080p": 1080,
    "720p": 720,
    "480p": 480,
    "360p": 360,
}
DEFAULT_VIDEO_QUALITY = "1080p"
AUDIO_FORMAT_OPTIONS = ["m4a", "mp3"]
# Explicit codes, because with auto captions "zh.*" also matches tracks like zh-Hans-en, adding requests YouTube rejects.
DEFAULT_SUBTITLE_LANGUAGES = "zh-TW,zh-Hant,zh-HK,zh-CN,zh-Hans,zh,en,en-US,en-GB"
LIVE_CHAT_TRACK = "live_chat"
DEFAULT_CUSTOM_NAME = "youtube_video"

# Titles can reach 300 UTF-8 bytes, past the 255-byte filename limit of most filesystems.
TITLE_TEMPLATE = "%(title).150B"
PLAYLIST_INDEX_TEMPLATE = "%(playlist_index&{:02d} - |)s"

PLAYLIST_PREVIEW_ITEMS = 10
DEFAULT_PLAYLIST_LIMIT = 20
LOCAL_MAX_PLAYLIST_LIMIT = 200

# Streamlit serves downloads from memory, and a cloud host is shared by every visitor.
CLOUD_MAX_PLAYLIST_LIMIT = 20
CLOUD_MAX_BYTES = 1024**3
CLOUD_RETENTION_SECONDS = 2 * 60 * 60
CLOUD_OUTPUT_ROOT = Path(tempfile.gettempdir()) / "youtube-downloader-files"

POSTPROCESSOR_LABELS = {
    "Merger": "正在合併影像與音訊...",
    "ExtractAudio": "正在轉換音訊...",
    "EmbedSubtitle": "正在嵌入字幕...",
}

YTDLP_ERROR_HINTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        ("not a bot",),
        "YouTube 要求驗證身分（判定為自動化流量）。雲端主機的 IP 常被封鎖，請改在本機執行，或在「進階」上傳 cookies.txt。",
    ),
    (("confirm your age", "inappropriate for some users"), "這是年齡限制影片，請在「進階」上傳已登入帳號的 cookies.txt。"),
    (("private video",), "這是私人影片，需要有觀看權限帳號的 cookies.txt。"),
    (("members-only", "join this channel"), "這是頻道會員專屬影片，需要會員帳號的 cookies.txt。"),
    (("available in your country",), "此影片在目前所在地區無法觀看。"),
    (("drm protected",), "此影片受 DRM 保護，無法下載。"),
    (("rate-limited", "http error 429", "too many requests", "try again later"), "YouTube 暫時限制了請求頻率，請稍後再試（可能長達一小時）。"),
    (("video unavailable", "video is unavailable"), "影片無法使用，可能已被移除或設為不公開。"),
    (("will begin in", "premieres in"), "直播或首播尚未開始，開始後才能下載。"),
    (("requested format is not available", "no video formats found"), "找不到符合條件的影片格式，請改選其他畫質。"),
    (("netscape format", "failed to load cookies"), "cookies.txt 無法讀取，請確認是 Netscape 格式。"),
    (("does not have a",), "此頻道沒有這個分頁，請改貼其他分頁的網址。"),
    (("looks truncated",), "網址不完整，請重新複製完整網址。"),
    (("wrong regex for subtitlelangs",), "字幕語言代碼不是有效的正規表示式，請修改後再試。"),
    (("conversion failed", "postprocessing:"), "ffmpeg 後處理失敗（合併影音或嵌入字幕）。"),
)

SubtitleRow = dict[str, str]


class DownloadedItem(TypedDict):
    title: str
    path: str
    subtitle_paths: list[str]


class DownloadOutcome(TypedDict):
    items: list[DownloadedItem]
    errors: list[str]
    skipped: list[str]
    warnings: list[str]
    subtitle_note: str


class LinkReport(TypedDict, total=False):
    url: str
    error: str
    title: str
    channel: str
    thumbnail: str
    details: list[str]
    is_live: bool
    is_playlist: bool
    resolutions: list[int]
    manual_rows: list[SubtitleRow]
    auto_rows: list[SubtitleRow]
    entry_count: Optional[int]
    entry_titles: list[str]


@dataclass(frozen=True)
class DownloadOptions:
    output_dir: Path
    output_template: str
    audio_only: bool = False
    max_height: Optional[int] = None
    audio_format: str = AUDIO_FORMAT_OPTIONS[0]
    download_subtitles: bool = False
    subtitle_languages: tuple[str, ...] = ()
    subtitle_format: str = "srt"
    include_auto_subtitles: bool = False
    embed_subtitles: bool = False
    playlist_limit: int = DEFAULT_PLAYLIST_LIMIT
    retry_count: int = 3
    cookie_file: Optional[Path] = None
    max_bytes: Optional[int] = None


class StatusUI(Protocol):
    def info(self, message: str) -> None: ...


class ProgressUI(Protocol):
    def progress(self, value: float, text: str = "") -> None: ...


class YtDlpLogCollector:
    """Keeps yt-dlp's messages for the UI instead of the server console."""

    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def debug(self, message: str) -> None:
        pass

    def info(self, message: str) -> None:
        pass

    def warning(self, message: str) -> None:
        self.warnings.append(message)

    def error(self, message: str) -> None:
        self.errors.append(message)


class CollectDownloadedItemsPP(PostProcessor):
    """Records each finished file. Runs before subtitle embedding, so a failed embed cannot lose the video."""

    def __init__(self, items: list[DownloadedItem]) -> None:
        super().__init__()
        self.items = items

    def run(self, information: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
        requested_subtitles = information.get("requested_subtitles") or {}
        # With ignoreerrors, yt-dlp sets a subtitle's filepath even when its download failed.
        subtitle_paths = [str(track.get("filepath") or "") for track in requested_subtitles.values()]
        self.items.append(
            {
                "title": str(information.get("title") or information.get("id") or ""),
                "path": str(information["filepath"]),
                "subtitle_paths": [path for path in subtitle_paths if path and Path(path).exists()],
            }
        )
        return [], information


def probe_executable(name: str, version_flag: str, search_path: Optional[str] = None) -> tuple[bool, str]:
    executable = shutil.which(name, path=search_path)
    if not executable:
        return False, f"PATH 中找不到 {name}"

    try:
        result = subprocess.run(
            [executable, version_flag],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except Exception as exc:
        return False, f"{name} 無法執行：{exc}"

    if result.returncode != 0:
        stderr_text = (result.stderr or "").strip()
        stdout_text = (result.stdout or "").strip()
        details = stderr_text or stdout_text or "unknown error"
        return False, f"{name} 無法使用：{details}"

    return True, executable


@st.cache_resource(show_spinner=False)
def get_ffmpeg_status() -> tuple[bool, str]:
    return probe_executable("ffmpeg", "-version")


@st.cache_resource(show_spinner=False)
def get_js_runtime_status() -> tuple[bool, str]:
    # yt-dlp checks Python's scripts directory before PATH, so a pip-installed Deno works without an activated venv.
    search_path = os.pathsep.join([sysconfig.get_path("scripts"), os.environ.get("PATH", "")])
    return probe_executable("deno", "--version", search_path)


def parse_youtube_urls(raw: str) -> tuple[list[str], list[str]]:
    """Splits pasted text into YouTube URLs and rejected entries, dropping duplicates."""
    urls: list[str] = []
    rejected: list[str] = []

    for token in raw.split():
        candidate = token if "://" in token else f"https://{token}"
        try:
            parsed = urlparse(candidate)
        except ValueError:
            rejected.append(token)
            continue

        host = (parsed.hostname or "").lower()
        has_tricky_authority = "@" in parsed.netloc or "\\" in candidate
        if parsed.scheme in {"http", "https"} and host in YOUTUBE_HOSTS and not has_tricky_authority:
            urls.append(candidate)
        else:
            rejected.append(token)

    return list(dict.fromkeys(urls)), rejected


def parse_subtitle_languages(raw: str) -> tuple[str, ...]:
    languages = [part.strip() for part in raw.split(",") if part.strip()]
    if not languages:
        languages = DEFAULT_SUBTITLE_LANGUAGES.split(",")
    return tuple(languages)


def build_output_template(naming_rule: str, custom_name: str, multiple_links: bool) -> str:
    if naming_rule == "自訂名稱":
        safe_name = re.sub(r"[^\w\-.]+", "_", custom_name.strip()).strip("._") or DEFAULT_CUSTOM_NAME
        return f"{safe_name}_%(id)s.%(ext)s"

    # Same-titled videos from different links would overwrite each other, so batch names keep the video ID.
    id_suffix = " [%(id)s]" if multiple_links else ""
    date_suffix = "_%(upload_date)s" if naming_rule == "原始檔名加日期" else ""
    return f"{PLAYLIST_INDEX_TEMPLATE}{TITLE_TEMPLATE}{date_suffix}{id_suffix}.%(ext)s"


def humanize_ytdlp_error(message: str) -> str:
    cleaned = message.removeprefix("ERROR: ").strip()
    lowered = cleaned.lower()
    for markers, hint in YTDLP_ERROR_HINTS:
        if any(marker in lowered for marker in markers):
            return f"{hint}（原始訊息：{cleaned[:200]}）"
    return cleaned


def build_base_options(logger: YtDlpLogCollector, retry_count: int, cookie_file: Optional[Path]) -> dict[str, Any]:
    base_opts: dict[str, Any] = {
        "logger": logger,
        "quiet": True,
        "color": "no_color",
        # Without this, yt-dlp's generic extractor would fetch any URL the server can reach.
        "allowed_extractors": YOUTUBE_EXTRACTORS,
        "noplaylist": True,
        "retries": retry_count,
    }
    if cookie_file:
        base_opts["cookiefile"] = str(cookie_file)
    return base_opts


def build_ydl_options(options: DownloadOptions, ffmpeg_available: bool, logger: YtDlpLogCollector) -> dict[str, Any]:
    ydl_opts = build_base_options(logger, options.retry_count, options.cookie_file)
    ydl_opts.update(
        {
            "paths": {"home": str(options.output_dir)},
            "outtmpl": options.output_template,
            # Otherwise re-downloading at another quality silently keeps the old file.
            "overwrites": True,
            # A failed subtitle or playlist entry should not abort the remaining downloads.
            "ignoreerrors": True,
            # playlistend stops listing early; max_downloads caps the total across a channel's nested tabs.
            "playlistend": options.playlist_limit,
            "max_downloads": options.playlist_limit,
        }
    )

    if options.audio_only:
        ydl_opts["format"] = "ba[ext=m4a]/ba/b"
        if ffmpeg_available:
            ydl_opts["postprocessors"] = [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": options.audio_format,
                    "preferredquality": "192",
                }
            ]
        return ydl_opts

    resolution = f"res:{options.max_height}" if options.max_height else "res"
    # YouTube offers H.264 up to 1080p; preferring it keeps the MP4 playable on every device.
    ydl_opts["format_sort"] = [resolution, "fps", "vcodec:h264", "acodec:aac"]
    if ffmpeg_available:
        ydl_opts["format"] = "bv*+ba/b"
        ydl_opts["merge_output_format"] = "mp4"
    else:
        ydl_opts["format"] = "b[ext=mp4]/b"

    if options.download_subtitles:
        ydl_opts["writesubtitles"] = True
        # "all" plus auto captions means ~160 machine-translated tracks, which YouTube answers with HTTP 429.
        ydl_opts["writeautomaticsub"] = options.include_auto_subtitles and "all" not in options.subtitle_languages
        # A past live stream lists its chat replay as a subtitle track, and "all" would download it.
        ydl_opts["subtitleslangs"] = [*options.subtitle_languages, f"-{LIVE_CHAT_TRACK}"]
        ydl_opts["subtitlesformat"] = f"{options.subtitle_format}/best"

    return ydl_opts


def download_with_ytdlp(url: str, options: DownloadOptions, progress_ui: ProgressUI) -> DownloadOutcome:
    options.output_dir.mkdir(parents=True, exist_ok=True)
    ffmpeg_available, _ = get_ffmpeg_status()

    logger = YtDlpLogCollector()
    items: list[DownloadedItem] = []
    skipped: list[str] = []

    def filter_entry(info: dict[str, Any], *, incomplete: Any) -> Optional[str]:
        title = info.get("title") or info.get("id") or url
        reason = None
        if info.get("live_status") == "is_live":
            reason = f"{title}：直播進行中，結束後才能下載。"
        elif options.max_bytes and not incomplete:
            size = info.get("filesize") or info.get("filesize_approx")
            if size and size > options.max_bytes:
                reason = (
                    f"{title}：檔案約 {format_bytes(size)}，超過雲端模式上限 {format_bytes(options.max_bytes)}，"
                    "請改選較低畫質或在本機執行。"
                )
        if reason:
            skipped.append(reason)
        return reason

    def progress_hook(data: dict[str, Any]) -> None:
        info = data.get("info_dict") or {}
        index, count = info.get("playlist_index"), info.get("n_entries")
        prefix = f"[{index}/{count}] " if index and count else ""
        # Subtitle tracks also pass through this hook, and they are the only downloads without a vcodec.
        stream = {"none": "音訊", None: "字幕"}.get(info.get("vcodec"), "影像")

        status = data.get("status")
        if status == "downloading":
            total = data.get("total_bytes") or data.get("total_bytes_estimate")
            downloaded = data.get("downloaded_bytes", 0)
            speed = data.get("speed")
            if total:
                fraction = min(max(downloaded / total, 0.0), 1.0)
                speed_text = ""
                if speed:
                    speed_text = f" | {speed / (1024 * 1024):.2f} MB/s"
                progress_ui.progress(fraction, text=f"{prefix}下載{stream} {fraction * 100:.1f}%{speed_text}")
        elif status == "finished":
            progress_ui.progress(1.0, text=f"{prefix}{stream}下載完成")

    def postprocessor_hook(data: dict[str, Any]) -> None:
        label = POSTPROCESSOR_LABELS.get(str(data.get("postprocessor")))
        if label and data.get("status") == "started":
            progress_ui.progress(1.0, text=label)

    ydl_opts = build_ydl_options(options, ffmpeg_available, logger)
    ydl_opts["match_filter"] = filter_entry
    ydl_opts["progress_hooks"] = [progress_hook]
    ydl_opts["postprocessor_hooks"] = [postprocessor_hook]

    wants_embedding = (
        ffmpeg_available and options.download_subtitles and options.embed_subtitles and not options.audio_only
    )

    with yt_dlp.YoutubeDL(cast(Any, ydl_opts)) as ydl:
        ydl.add_post_processor(CollectDownloadedItemsPP(items))
        if wants_embedding:
            # Keep the sidecar files so they stay available as separate downloads.
            ydl.add_post_processor(FFmpegEmbedSubtitlePP(ydl, already_have_subtitle=True))
        try:
            ydl.extract_info(url, download=True)
        except MaxDownloadsReached:
            pass

    return {
        "items": items,
        "errors": [humanize_ytdlp_error(message) for message in dict.fromkeys(logger.errors)],
        "skipped": skipped,
        "warnings": list(dict.fromkeys(logger.warnings)),
        "subtitle_note": "",
    }


def run_download(
    urls: list[str],
    options: DownloadOptions,
    status_ui: StatusUI,
    progress_ui: ProgressUI,
) -> DownloadOutcome:
    combined: DownloadOutcome = {"items": [], "errors": [], "skipped": [], "warnings": [], "subtitle_note": ""}

    for index, url in enumerate(urls, start=1):
        status_ui.info(f"{index}/{len(urls)} 下載連結：{url}")
        try:
            outcome = download_with_ytdlp(url, options, progress_ui)
        except Exception as exc:
            combined["errors"].append(humanize_ytdlp_error(str(exc)))
            continue
        combined["items"].extend(outcome["items"])
        combined["errors"].extend(outcome["errors"])
        combined["skipped"].extend(outcome["skipped"])
        combined["warnings"].extend(outcome["warnings"])

    # The same video can arrive through two different links, and both then point at one file.
    combined["items"] = list({item["path"]: item for item in combined["items"]}.values())

    if options.download_subtitles:
        combined["subtitle_note"] = describe_subtitle_shortfall(combined)
    return combined


def describe_subtitle_shortfall(outcome: DownloadOutcome) -> str:
    matches = [re.search(r"Unable to download video subtitles for '([^']+)'", message) for message in outcome["warnings"]]
    failed_languages = list(dict.fromkeys(match.group(1) for match in matches if match))
    if failed_languages:
        return (
            f"字幕語言 {'、'.join(failed_languages)} 下載失敗。"
            "YouTube 常以 HTTP 429 拒絕機器翻譯的字幕；影片與其他字幕不受影響。"
        )

    missing_subtitles = sum(1 for item in outcome["items"] if not item["subtitle_paths"])
    if missing_subtitles:
        return f"{missing_subtitles} 部影片沒有取得字幕。可用「解析連結」查看可用語言，或勾選「包含自動產生字幕」。"
    return ""


def flatten_subtitle_tracks(track_map: Mapping[str, object] | None) -> list[SubtitleRow]:
    rows: list[SubtitleRow] = []
    if not isinstance(track_map, dict):
        return rows

    for lang in sorted(track_map.keys()):
        tracks = track_map.get(lang)
        if lang == LIVE_CHAT_TRACK or not isinstance(tracks, list) or not tracks:
            continue
        track_items = [track for track in cast(list[object], tracks) if isinstance(track, Mapping)]
        names = [str(track.get("name")) for track in track_items if track.get("name")]
        formats = [str(track.get("ext")) for track in track_items if track.get("ext")]
        rows.append({"語言": str(lang), "名稱": names[0] if names else "", "格式": ", ".join(formats)})

    return rows


def iter_video_entries(info: Mapping[str, Any]) -> Iterator[Mapping[str, Any]]:
    entries = info.get("entries")
    if entries is None:
        yield info
        return
    for entry in entries:
        if entry:
            yield from iter_video_entries(entry)


def get_thumbnail_url(info: Mapping[str, Any]) -> str:
    thumbnails = info.get("thumbnails") or []
    fallback = thumbnails[-1].get("url") if thumbnails else ""
    return str(info.get("thumbnail") or fallback or "")


def collect_resolutions(formats: list[dict[str, Any]] | None) -> list[int]:
    resolutions: set[int] = set()
    for fmt in formats or []:
        if fmt.get("vcodec") in (None, "none"):
            continue
        # YouTube names a quality after the shorter side, which matters for vertical videos.
        sides =[side for side in (fmt.get("width"), fmt.get("height")) if side]
        if sides:
            resolutions.add(int(min(sides)))
    return sorted(resolutions, reverse=True)


def format_video_details(info: Mapping[str, Any]) -> list[str]:
    details: list[str] = []
    duration = str(info.get("duration_string") or "")
    if duration:
        # yt-dlp renders videos under a minute as bare seconds.
        details.append(f"長度 {duration if ':' in duration else f'0:{duration.zfill(2)}'}")
    upload_date = str(info.get("upload_date") or "")
    if len(upload_date) == 8:
        details.append(f"上傳於 {upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:]}")
    if info.get("view_count") is not None:
        details.append(f"{info['view_count']:,} 次觀看")
    return details


def inspect_link(url: str, retry_count: int, cookie_file: Optional[Path]) -> LinkReport:
    opts = build_base_options(YtDlpLogCollector(), retry_count, cookie_file)
    opts.update({"skip_download": True, "extract_flat": "in_playlist", "playlistend": PLAYLIST_PREVIEW_ITEMS})

    try:
        with yt_dlp.YoutubeDL(cast(Any, opts)) as ydl:
            info = cast(dict[str, Any], ydl.extract_info(url, download=False))
    except Exception as exc:
        return {"url": url, "error": humanize_ytdlp_error(str(exc))}

    report: LinkReport = {
        "url": url,
        "error": "",
        "title": str(info.get("title") or info.get("id") or url),
        "channel": str(info.get("channel") or info.get("uploader") or ""),
        "thumbnail": get_thumbnail_url(info),
    }

    if "entries" in info:
        raw_entries = list(info.get("entries") or [])
        # A channel's root URL lists its tabs, so its playlist_count is not a number of videos.
        is_nested = any(entry and "entries" in entry for entry in raw_entries)
        report["is_playlist"] = True
        report["entry_count"] = None if is_nested else info.get("playlist_count")
        report["entry_titles"] = [
            str(entry.get("title") or entry.get("id") or "") for entry in iter_video_entries(info)
        ][:PLAYLIST_PREVIEW_ITEMS]
        return report

    report["is_playlist"] = False
    report["is_live"] = info.get("live_status") == "is_live"
    report["details"] = format_video_details(info)
    report["resolutions"] = collect_resolutions(info.get("formats"))
    report["manual_rows"] = flatten_subtitle_tracks(info.get("subtitles"))
    report["auto_rows"] = flatten_subtitle_tracks(info.get("automatic_captions"))
    return report


def run_link_inspection(
    urls: list[str],
    retry_count: int,
    cookie_file: Optional[Path],
    status_ui: StatusUI,
) -> list[LinkReport]:
    reports: list[LinkReport] = []
    for index, url in enumerate(urls, start=1):
        status_ui.info(f"{index}/{len(urls)} 解析連結：{url}")
        reports.append(inspect_link(url, retry_count, cookie_file))
    return reports


def summarize_reports(reports: list[LinkReport]) -> tuple[str, str]:
    if not reports:
        return "", "尚未執行健康檢查"

    failed = [report for report in reports if report.get("error")]
    if failed:
        return "red", f"{len(failed)}/{len(reports)} 個連結無法解析：{failed[0].get('error')}"
    if any(report.get("is_live") for report in reports):
        return "yellow", "連結可用，但有影片正在直播，結束後才能下載。"

    videos = [report for report in reports if not report.get("is_playlist")]
    if any(not report.get("manual_rows") and not report.get("auto_rows") for report in videos):
        return "yellow", "連結可用，但部分影片未偵測到字幕軌。"
    return "green", "連結可用，且可讀取字幕軌。" if videos else "連結可用。"


@contextmanager
def temporary_cookie_file(uploaded: Any) -> Iterator[Optional[Path]]:
    if uploaded is None:
        yield None
        return

    # Cookies are login credentials: keep them in a private directory only while the task runs.
    workdir = Path(tempfile.mkdtemp(prefix="youtube-downloader-cookies-"))
    try:
        cookie_file = workdir / "cookies.txt"
        cookie_file.write_bytes(uploaded.getvalue())
        yield cookie_file
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def is_streamlit_cloud_environment() -> bool:
    markers = [
        os.getenv("STREAMLIT_RUNTIME_ENV", ""),
        os.getenv("STREAMLIT_SHARING_MODE", ""),
        os.getenv("IS_STREAMLIT_CLOUD", ""),
        os.getenv("STREAMLIT_CLOUD", ""),
        os.getenv("STREAMLIT_ENV", ""),
        os.getenv("ENVIRONMENT", ""),
    ]
    normalized = [value.strip().lower() for value in markers if value]
    if any(value in {"cloud", "true", "1", "sharing", "production", "prod"} for value in normalized):
        return True

    host_markers = [
        os.getenv("HOSTNAME", "").lower(),
        os.getenv("HOST", "").lower(),
        os.getenv("STREAMLIT_SERVER_ADDRESS", "").lower(),
    ]
    if any("streamlit.app" in marker for marker in host_markers if marker):
        return True

    # Streamlit Cloud commonly mounts repositories under /mount/src/<repo>.
    cwd_text = str(Path.cwd()).replace("\\", "/").lower()
    return cwd_text.startswith("/mount/src")


def get_session_output_dir() -> Path:
    # A cloud host is shared by every visitor, so each session gets its own folder.
    if not st.session_state.get("session_output_dir"):
        st.session_state["session_output_dir"] = str(CLOUD_OUTPUT_ROOT / uuid.uuid4().hex)
    return Path(st.session_state["session_output_dir"])


def purge_stale_output_dirs() -> None:
    if not CLOUD_OUTPUT_ROOT.exists():
        return

    cutoff = time.time() - CLOUD_RETENTION_SECONDS
    for session_dir in CLOUD_OUTPUT_ROOT.iterdir():
        try:
            if session_dir.stat().st_mtime < cutoff:
                shutil.rmtree(session_dir, ignore_errors=True)
        except OSError:
            continue


def detect_runtime_commit_short() -> str:
    env_keys = [
        "SOURCE_COMMIT",
        "GITHUB_SHA",
        "GIT_COMMIT",
        "COMMIT_SHA",
        "STREAMLIT_GIT_SHA",
    ]

    for key in env_keys:
        value = os.getenv(key, "").strip()
        if value:
            return value[:7]

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
        commit_value = result.stdout.strip()
        if result.returncode == 0 and commit_value:
            return commit_value[:7]
    except Exception:
        pass

    return DEPLOY_VERIFY_MIN_COMMIT


def format_bytes(size: float) -> str:
    for unit in ("B", "KB", "MB"):
        if size < 1024:
            return f"{size:.0f} {unit}"
        size /= 1024
    return f"{size:.2f} GB"


def escape_markdown(text: str) -> str:
    # Video titles often contain characters Streamlit would render as formatting, such as "$" as LaTeX.
    return re.sub(r"([\\`*_{}\[\]()#+\-.!:$~|<>])", r"\\\1", text)


def build_zip_bytes(paths: list[Path]) -> bytes:
    # Video is already compressed, so storing it keeps this fast even for large files.
    with tempfile.TemporaryFile() as spool:
        with zipfile.ZipFile(spool, mode="w", compression=zipfile.ZIP_STORED) as bundle:
            for path in paths:
                bundle.write(path, arcname=path.name)
        spool.seek(0)
        return spool.read()


def append_task_log(message: str) -> None:
    timestamp = datetime.now().strftime("%H:%M:%S")
    logs: list[str] = st.session_state.setdefault("task_logs", [])
    logs.append(f"[{timestamp}] {message}")
    if len(logs) > 100:
        st.session_state["task_logs"] = logs[-100:]


def apply_custom_style() -> None:
    st.markdown(
        """
        <style>
            .stApp {
                background: linear-gradient(165deg, #f4f7fb 0%, #eef2f7 40%, #edf3f5 100%);
                color: #1f2a3d;
            }
            [data-testid="stSidebar"] {
                background: linear-gradient(180deg, #f9fbfd 0%, #f2f6fb 100%);
                border-right: 1px solid #dde6ef;
            }
            [data-testid="stSidebar"] [data-testid="stTextInput"] input,
            [data-testid="stSidebar"] [data-testid="stTextArea"] textarea,
            [data-testid="stSidebar"] [data-testid="stNumberInput"] input {
                background: #ffffff;
                color: #0f2238;
                border: 1px solid #93abc1;
                border-radius: 10px;
            }
            [data-testid="stSidebar"] [data-testid="stTextInput"] input::placeholder,
            [data-testid="stSidebar"] [data-testid="stTextArea"] textarea::placeholder {
                color: #67809b;
            }
            [data-testid="stSidebar"] [data-testid="stTextInput"] input:focus,
            [data-testid="stSidebar"] [data-testid="stTextArea"] textarea:focus,
            [data-testid="stSidebar"] [data-testid="stNumberInput"] input:focus {
                border-color: #0e6a77;
                box-shadow: 0 0 0 2px rgba(14, 106, 119, 0.18);
            }
            [data-testid="stSidebar"] div[data-baseweb="select"] > div,
            [data-testid="stSidebar"] [data-testid="stSelectbox"] [role="group"],
            [data-testid="stSidebar"] [data-baseweb="base-input"] > div {
                background: #ffffff;
                border: 1px solid #93abc1;
                color: #0f2238;
            }
            [data-testid="stSidebar"] [data-testid="stSelectbox"] [role="group"] {
                border-radius: 10px;
            }
            [data-testid="stSidebar"] div[data-baseweb="select"] > div:hover,
            [data-testid="stSidebar"] [data-testid="stSelectbox"] [role="group"]:hover,
            [data-testid="stSidebar"] [data-baseweb="base-input"] > div:hover {
                border-color: #6f8ea9;
            }
            [data-testid="stSidebar"] .stButton > button {
                height: 46px;
                border-radius: 10px;
                margin-top: 8px;
                font-weight: 600;
                letter-spacing: 0.02em;
                box-shadow: 0 4px 14px rgba(17, 34, 61, 0.08);
            }
            [data-testid="stSidebar"] .stButton > button:hover {
                transform: translateY(-1px);
            }
            [data-testid="stSidebar"] .stButton + .stButton {
                margin-top: 2px;
            }
            .hero-title {
                font-size: 56px;
                font-weight: 800;
                letter-spacing: -0.02em;
                color: #1d2b3f;
                margin-bottom: 8px;
            }
            .hero-subtitle {
                color: #5f6f85;
                font-size: 19px;
                margin-bottom: 8px;
            }
            .meta-line {
                color: #5f7088;
                font-size: 15px;
                margin-bottom: 18px;
            }
            .meta-panel {
                margin: 12px 0 22px;
                padding: 18px 20px;
                background: linear-gradient(135deg, rgba(255, 255, 255, 0.98), rgba(232, 246, 248, 0.9));
                border: 1px solid #d7e7ee;
                border-radius: 18px;
                box-shadow: 0 16px 34px rgba(18, 37, 66, 0.08);
            }
            .meta-panel-caption {
                margin-bottom: 12px;
                color: #5d738d;
                font-size: 12px;
                font-weight: 700;
                letter-spacing: 0.14em;
                text-transform: uppercase;
            }
            .badge-row {
                display: flex;
                flex-wrap: wrap;
                gap: 12px;
            }
            .badge-row-primary {
                margin-bottom: 14px;
            }
            .meta-badge {
                display: flex;
                align-items: center;
                gap: 10px;
                flex: 1 1 210px;
                min-width: 0;
                padding: 12px 14px;
                background: rgba(255, 255, 255, 0.9);
                border: 1px solid #d5e5ec;
                border-radius: 14px;
                box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.65);
            }
            .badge-dot {
                width: 10px;
                height: 10px;
                flex: 0 0 10px;
                border-radius: 999px;
                background: linear-gradient(180deg, #11a0b1 0%, #0d6e7a 100%);
                box-shadow: 0 0 0 4px rgba(17, 160, 177, 0.12);
            }
            .meta-badge-label {
                color: #5d7086;
                font-size: 12px;
                font-weight: 700;
                letter-spacing: 0.04em;
                white-space: nowrap;
            }
            .meta-badge-value {
                min-width: 0;
                color: #17314d;
                font-size: 15px;
                font-weight: 700;
            }
            .meta-grid {
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
                gap: 12px;
            }
            .meta-card {
                min-width: 0;
                padding: 14px 16px;
                background: rgba(245, 250, 252, 0.92);
                border: 1px solid #d5e4eb;
                border-radius: 14px;
            }
            .meta-card-label {
                margin-bottom: 6px;
                color: #61758f;
                font-size: 12px;
                font-weight: 700;
                letter-spacing: 0.04em;
                text-transform: uppercase;
            }
            .meta-card-value {
                color: #163049;
                font-size: 14px;
                font-weight: 700;
                line-height: 1.45;
                word-break: break-word;
            }
            .meta-card-value.mono {
                font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
                font-size: 13px;
            }
            @media (max-width: 900px) {
                .hero-title {
                    font-size: 42px;
                }
                .hero-subtitle {
                    font-size: 17px;
                }
                .meta-panel {
                    padding: 16px;
                }
                .meta-badge {
                    flex-basis: 100%;
                }
                .meta-grid {
                    grid-template-columns: 1fr;
                }
            }
            .video-title {
                color: #17314d;
                font-size: 20px;
                font-weight: 700;
                line-height: 1.35;
                margin-bottom: 6px;
            }
            .status-card {
                background: #ffffff;
                border: 1px solid #deebf3;
                border-radius: 14px;
                padding: 14px 16px;
                box-shadow: 0 10px 24px rgba(18, 34, 58, 0.06);
                margin-bottom: 10px;
            }
            .log-box {
                max-height: 280px;
                overflow-y: auto;
                background: #f8fbfd;
                border: 1px solid #d9e7f0;
                border-radius: 10px;
                padding: 12px;
                font-family: Consolas, monospace;
                font-size: 13px;
                color: #27415a;
                line-height: 1.6;
            }
            .health-card {
                background: #ffffff;
                border: 1px solid #dbe9f2;
                border-radius: 12px;
                padding: 12px 14px;
                margin-bottom: 10px;
                box-shadow: 0 8px 20px rgba(21, 43, 74, 0.06);
            }
            .health-title {
                font-size: 14px;
                color: #3b5271;
                margin-bottom: 8px;
                font-weight: 700;
            }
            .health-lights {
                display: flex;
                gap: 16px;
                align-items: center;
                margin-bottom: 8px;
            }
            .light-item {
                display: flex;
                align-items: center;
                gap: 6px;
                font-size: 12px;
                color: #4c607a;
            }
            .light-dot {
                width: 12px;
                height: 12px;
                border-radius: 50%;
                background: #cbd8e4;
                box-shadow: inset 0 0 0 1px #c3d3df;
            }
            .light-dot.on.green {
                background: #22c55e;
                box-shadow: 0 0 0 5px rgba(34, 197, 94, 0.15);
            }
            .light-dot.on.yellow {
                background: #f59e0b;
                box-shadow: 0 0 0 5px rgba(245, 158, 11, 0.18);
            }
            .light-dot.on.red {
                background: #ef4444;
                box-shadow: 0 0 0 5px rgba(239, 68, 68, 0.16);
            }
            .health-message {
                color: #2f4866;
                font-size: 13px;
            }
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_meta_badges(
    version: str,
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
            "<div class='meta-panel-caption'>Deployment Overview</div>"
            "<div class='badge-row badge-row-primary'>"
            "<div class='meta-badge'>"
            "<span class='badge-dot'></span>"
            "<span class='meta-badge-label'>版本</span>"
            f"<span class='meta-badge-value'>{version}</span>"
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
            "<div class='meta-card-label'>Runtime Commit</div>"
            f"<div class='meta-card-value mono'>{runtime_commit}</div>"
            "</div>"
            "<div class='meta-card'>"
            "<div class='meta-card-label'>Baseline Commit</div>"
            f"<div class='meta-card-value mono'>{min_commit}</div>"
            "</div>"
            "<div class='meta-card'>"
            "<div class='meta-card-label'>Deploy Marker</div>"
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


def status_card_html(message: str) -> str:
    return f"<div class='status-card'><strong>{html.escape(message)}</strong></div>"


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


def render_link_report(report: LinkReport) -> None:
    with st.container(border=True):
        if report.get("error"):
            st.error(f"{escape_markdown(report.get('url', ''))}\n\n{escape_markdown(report.get('error', ''))}")
            return

        thumbnail_col, info_col = st.columns([1, 2])
        if report.get("thumbnail"):
            thumbnail_col.image(report.get("thumbnail", ""), width="stretch")

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

            if report.get("is_playlist"):
                entry_count = report.get("entry_count")
                entry_titles = report.get("entry_titles") or []
                count_text = f"共 {entry_count} 部，" if entry_count else ""
                st.write(f"播放清單／頻道：{count_text}預覽前 {len(entry_titles)} 部")
                st.markdown("\n".join(f"- {escape_markdown(title)}" for title in entry_titles))
                return

            if report.get("is_live"):
                st.warning("這部影片正在直播，結束後才能下載。")

            resolutions = report.get("resolutions") or []
            if resolutions:
                st.write("可用畫質：" + "、".join(f"{height}p" for height in resolutions))

        render_subtitle_tracks(report)


def render_file_download_button(path: Path, key: str) -> None:
    st.download_button(
        label=f"下載 {escape_markdown(path.name)}（{format_bytes(path.stat().st_size)}）",
        # Passing the method defers reading the file until the button is clicked.
        data=path.read_bytes,
        file_name=path.name,
        mime=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
        width="stretch",
        key=key,
        on_click="ignore",
    )


def render_bundle_download_button(paths: list[Path], label: str, file_name: str, key: str, is_cloud: bool) -> None:
    total_size = sum(path.stat().st_size for path in paths)
    if is_cloud and total_size > CLOUD_MAX_BYTES:
        st.caption(f"{label}：總大小 {format_bytes(total_size)} 超過雲端模式上限，請逐一下載檔案。")
        return

    st.download_button(
        label=f"{label}（{format_bytes(total_size)}）",
        data=partial(build_zip_bytes, paths),
        file_name=file_name,
        mime="application/zip",
        width="stretch",
        key=key,
        on_click="ignore",
    )


def render_item_downloads(item: DownloadedItem, key_base: str, is_cloud: bool) -> None:
    media_path = Path(item["path"])
    subtitle_paths = [Path(path) for path in item["subtitle_paths"] if Path(path).exists()]

    render_file_download_button(media_path, f"{key_base}_media")
    for index, subtitle_path in enumerate(subtitle_paths, start=1):
        render_file_download_button(subtitle_path, f"{key_base}_subtitle_{index}")
    if subtitle_paths:
        render_bundle_download_button(
            [media_path, *subtitle_paths],
            label="下載影片+字幕.zip",
            file_name=f"{media_path.stem}_bundle.zip",
            key=f"{key_base}_bundle",
            is_cloud=is_cloud,
        )


def render_final_download_actions(items: list[DownloadedItem], is_cloud: bool) -> None:
    st.markdown("完成後下載選項")

    if len(items) == 1:
        render_item_downloads(items[0], "download_item_0", is_cloud)
    else:
        all_paths: list[Path] = []
        for item in items:
            all_paths.append(Path(item["path"]))
            all_paths.extend(Path(path) for path in item["subtitle_paths"] if Path(path).exists())
        render_bundle_download_button(
            all_paths,
            label=f"全部打包下載.zip（{len(items)} 部）",
            file_name="youtube_downloads.zip",
            key="download_all_bundle",
            is_cloud=is_cloud,
        )
        for index, item in enumerate(items):
            with st.expander(escape_markdown(item["title"])):
                render_item_downloads(item, f"download_item_{index}", is_cloud)

    if is_cloud:
        st.caption("部署環境無法直接寫入你的電腦資料夾，請使用上方按鈕下載。檔案只會暫存約兩小時。")


def render_download_outcome(outcome: DownloadOutcome, is_cloud: bool) -> None:
    for message in outcome["errors"]:
        st.error(escape_markdown(message))
    for message in outcome["skipped"]:
        st.warning(escape_markdown(message))

    items = [item for item in outcome["items"] if Path(item["path"]).exists()]
    if not items:
        if outcome["items"]:
            st.warning("暫存檔案已過期，請重新下載。")
        elif not outcome["errors"] and not outcome["skipped"]:
            st.warning("沒有下載到任何檔案，請確認連結內容或使用「解析連結」檢查。")
        return

    if len(items) == 1:
        st.success(f"下載完成：{escape_markdown(items[0]['path'])}")
    else:
        st.success(f"下載完成：共 {len(items)} 部，存放於 {escape_markdown(str(Path(items[0]['path']).parent))}")

    if outcome["subtitle_note"]:
        st.warning(outcome["subtitle_note"])

    render_final_download_actions(items, is_cloud)


def main() -> None:
    st.set_page_config(
        page_title="YouTube影片下載平台",
        page_icon="🎬",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    apply_custom_style()

    st.session_state.setdefault("output_dir_raw", str(Path.cwd() / "downloads"))
    st.session_state.setdefault("task_logs", [])
    st.session_state.setdefault("stage_progress", 0.0)
    st.session_state.setdefault("download_progress", 0.0)
    st.session_state.setdefault("last_status", "等待任務開始")
    st.session_state.setdefault("last_reports", [])
    st.session_state.setdefault("last_outcome", None)

    # Changing a widget stops a running script, and a stopped script can no longer write session state.
    # So if this flag is still set, the previous run never finished its task.
    if st.session_state.get("task_running"):
        st.session_state["task_running"] = False
        st.session_state["last_status"] = "任務已中斷：執行中變更設定或按下停止都會中斷任務，請重新開始。"
        st.session_state["stage_progress"] = 0.0
        st.session_state["download_progress"] = 0.0
        append_task_log(st.session_state["last_status"])

    is_cloud = is_streamlit_cloud_environment()
    ffmpeg_available, ffmpeg_status = get_ffmpeg_status()
    js_runtime_available, js_runtime_status = get_js_runtime_status()

    with st.sidebar:
        st.markdown("## 任務設定")
        work_mode = st.radio("作業模式", options=["單一連結", "批次連結"], index=0)
        if work_mode == "批次連結":
            raw_urls = st.text_area(
                "YouTube 網址（每行一個）",
                placeholder="https://www.youtube.com/watch?v=...\nhttps://www.youtube.com/playlist?list=...",
                height=140,
            )
        else:
            raw_urls = st.text_input("YouTube 網址", placeholder="https://www.youtube.com/watch?v=...")
        st.caption("支援影片、Shorts、播放清單與頻道連結。影片網址帶有 list 參數時，只下載該部影片。")

        max_playlist_limit = CLOUD_MAX_PLAYLIST_LIMIT if is_cloud else LOCAL_MAX_PLAYLIST_LIMIT
        playlist_limit = st.number_input(
            "每個播放清單／頻道最多下載幾部",
            min_value=1,
            max_value=max_playlist_limit,
            value=min(DEFAULT_PLAYLIST_LIMIT, max_playlist_limit),
        )

        st.markdown("### 下載內容")
        media_type = st.radio("類型", options=["影片", "僅音訊"], index=0, horizontal=True)
        audio_only = media_type == "僅音訊"
        quality_label = DEFAULT_VIDEO_QUALITY
        audio_format = AUDIO_FORMAT_OPTIONS[0]
        if audio_only:
            audio_format = st.selectbox(
                "音訊格式",
                options=AUDIO_FORMAT_OPTIONS if ffmpeg_available else AUDIO_FORMAT_OPTIONS[:1],
                index=0,
            )
        else:
            quality_options = list(VIDEO_QUALITY_OPTIONS)
            quality_label = st.selectbox(
                "畫質上限",
                options=quality_options,
                index=quality_options.index(DEFAULT_VIDEO_QUALITY),
            )
            st.caption("1080p 以下優先選 H.264，相容性最好。1440p 以上只有 VP9／AV1，部分播放器可能無法播放。")

        st.markdown("### 輸出設定")
        naming_rule = st.radio("輸出檔名規則", options=["沿用原始檔名", "原始檔名加日期", "自訂名稱"], index=0)
        custom_name = ""
        if naming_rule == "自訂名稱":
            custom_name = st.text_input("自訂檔名", value=DEFAULT_CUSTOM_NAME)

        if is_cloud:
            st.caption("雲端模式：檔案暫存於伺服器，下載完成後請使用下方按鈕存到本機。")
            output_dir_raw = ""
        else:
            output_dir_raw = st.text_input("下載資料夾", key="output_dir_raw")

        retry_count = st.slider("重試次數", min_value=1, max_value=10, value=3)

        download_subtitles = False
        subtitle_languages_raw = ""
        subtitle_format = "srt"
        include_auto_subtitles = False
        embed_subtitles = False
        if not audio_only:
            st.markdown("### 字幕設定")
            download_subtitles = st.checkbox("下載字幕", value=True)
            subtitle_languages_raw = st.text_input(
                "字幕語言代碼",
                value=DEFAULT_SUBTITLE_LANGUAGES,
                help="以逗號分隔，支援正規表示式，例如 ja,ko 或 zh.*；填 all 代表全部上傳者字幕。",
            )
            subtitle_format = st.selectbox("字幕格式", options=["srt", "vtt"], index=0)
            include_auto_subtitles = st.checkbox(
                "包含自動產生字幕",
                value=False,
                help="包含語音辨識與機器翻譯的字幕。YouTube 常以 HTTP 429 拒絕機器翻譯字幕，此時只會取得原語言字幕。",
            )
            if include_auto_subtitles and "all" in parse_subtitle_languages(subtitle_languages_raw):
                st.caption("語言填 all 時不會抓自動產生字幕，以免觸發 YouTube 限流。")
            embed_subtitles = st.checkbox("嵌入字幕到 mp4", value=True, disabled=not ffmpeg_available)

        if not ffmpeg_available:
            install_hints = {"darwin": "brew install ffmpeg", "win32": "winget install Gyan.FFmpeg"}
            install_hint = install_hints.get(sys.platform, "apt install ffmpeg")
            st.warning(
                "未偵測到 ffmpeg。仍可下載，但 YouTube 的單檔格式通常只有 360p，且無法轉成 MP3 或嵌入字幕。"
                f"若要高畫質，請先安裝 ffmpeg（例如 {install_hint}）。"
            )
            st.caption(f"ffmpeg 狀態：{ffmpeg_status}")
        if not js_runtime_available:
            st.warning(
                "未偵測到 Deno。YouTube 需要 JavaScript runtime 才能取得完整畫質，目前可用格式可能受限。"
                "請執行 pip install -r requirements.txt。"
            )
            st.caption(f"Deno 狀態：{js_runtime_status}")

        with st.expander("進階：需要登入的影片"):
            cookies_upload = st.file_uploader("cookies.txt（Netscape 格式）", type=["txt"])
            st.caption(
                "用於年齡限制、會員或私人影片，或雲端主機遇到 YouTube 機器人驗證時。"
                "cookies 等同登入憑證，請只上傳到你信任的部署環境；伺服器上的暫存檔會在任務結束時刪除。"
            )

        start_download = st.button("開始下載", type="primary", width="stretch")
        inspect_only = st.button("解析連結（不下載）", width="stretch")
        clear_logs = st.button("清除任務歷程", width="stretch")

        st.markdown("---")
        st.caption("部署維運")
        st.caption("推送到 main 分支可自動部署。")

    if clear_logs:
        st.session_state["task_logs"] = []
        st.session_state["stage_progress"] = 0.0
        st.session_state["download_progress"] = 0.0
        st.session_state["last_status"] = "等待任務開始"
        st.session_state["last_reports"] = []
        st.session_state["last_outcome"] = None
        st.success("任務歷程已清除。")

    st.markdown("<div class='hero-title'>YouTube影片下載平台</div>", unsafe_allow_html=True)
    st.markdown(
        "<div class='hero-subtitle'>貼上影片、播放清單或頻道連結，一站式下載影片、音訊與字幕</div>",
        unsafe_allow_html=True,
    )
    st.caption(
        "請僅下載你擁有權利或已獲授權的內容（例如自己上傳的影片、Creative Commons 授權內容），"
        "並遵守 YouTube 服務條款與當地著作權法。"
    )
    runtime_commit = detect_runtime_commit_short()
    render_meta_badges(
        version=APP_VERSION,
        updated_at=APP_UPDATED_AT,
        deploy_target="main 分支自動部署",
        runtime_commit=runtime_commit,
        min_commit=DEPLOY_VERIFY_MIN_COMMIT,
        deploy_marker=DEPLOY_MARKER,
        ytdlp_version=YT_DLP_VERSION,
    )

    urls, rejected_urls = parse_youtube_urls(raw_urls)

    def current_reports() -> list[LinkReport]:
        # Reports for links no longer in the input are stale.
        return [report for report in st.session_state["last_reports"] if report.get("url") in urls]

    status_placeholder = st.empty()
    health_placeholder = st.empty()
    status_placeholder.markdown(
        status_card_html(st.session_state.get("last_status", "等待任務開始")),
        unsafe_allow_html=True,
    )
    health_placeholder.markdown(health_panel_html(*summarize_reports(current_reports())), unsafe_allow_html=True)
    stage_progress_ui = st.progress(
        st.session_state.get("stage_progress", 0.0),
        text=f"流程進度: {int(st.session_state.get('stage_progress', 0.0) * 100)}%",
    )
    download_progress_ui = st.progress(
        st.session_state.get("download_progress", 0.0),
        text=f"下載進度: {int(st.session_state.get('download_progress', 0.0) * 100)}%",
    )

    with st.expander("任務歷程", expanded=False):
        logs = st.session_state.get("task_logs", [])
        if logs:
            escaped_logs = "<br>".join(html.escape(line) for line in logs[-80:])
            st.markdown(f"<div class='log-box'>{escaped_logs}</div>", unsafe_allow_html=True)
        else:
            st.caption("尚無任務紀錄。")

    class StatusBridge:
        def __init__(self, placeholder: Any, stage_ui: Any) -> None:
            self.placeholder = placeholder
            self.stage_ui = stage_ui

        def info(self, message: str) -> None:
            st.session_state["last_status"] = message
            append_task_log(message)
            step_match = re.match(r"(\d+)/(\d+)\s+(.+)", message)
            if step_match:
                current = int(step_match.group(1))
                total = max(int(step_match.group(2)), 1)
                stage_fraction = min(max((current - 1) / total, 0.0), 1.0)
                st.session_state["stage_progress"] = stage_fraction
                self.stage_ui.progress(stage_fraction, text=f"流程進度: {int(stage_fraction * 100)}%")
            self.placeholder.markdown(status_card_html(message), unsafe_allow_html=True)

    class DownloadProgressBridge:
        def __init__(self, progress_ui: Any) -> None:
            self.progress_ui = progress_ui

        def progress(self, value: float, text: str = "") -> None:
            clamped = min(max(value, 0.0), 1.0)
            st.session_state["download_progress"] = clamped
            self.progress_ui.progress(clamped, text=text or f"下載進度: {int(clamped * 100)}%")

    if start_download or inspect_only:
        if rejected_urls:
            st.error("以下內容不是 YouTube 網址：" + "、".join(escape_markdown(entry) for entry in rejected_urls))
            return
        if not urls:
            st.error("請輸入至少一個 YouTube 網址。")
            return

        st.session_state["stage_progress"] = 0.0
        st.session_state["download_progress"] = 0.0
        st.session_state["last_status"] = "任務啟動中..."
        st.session_state["task_running"] = True
        append_task_log("任務啟動")

        status_ui = StatusBridge(status_placeholder, stage_progress_ui)
        progress_ui = DownloadProgressBridge(download_progress_ui)

        try:
            with temporary_cookie_file(cookies_upload) as cookie_file:
                if inspect_only:
                    st.session_state["last_reports"] = run_link_inspection(urls, retry_count, cookie_file, status_ui)
                    status_ui.info("連結解析完成，未下載影片。")
                else:
                    st.session_state["last_outcome"] = None

                    if is_cloud:
                        output_dir = get_session_output_dir()
                        purge_stale_output_dirs()
                        shutil.rmtree(output_dir, ignore_errors=True)
                    else:
                        output_dir = Path(output_dir_raw.strip() or Path.cwd() / "downloads").expanduser().resolve()

                    outcome = run_download(
                        urls,
                        DownloadOptions(
                            output_dir=output_dir,
                            output_template=build_output_template(naming_rule, custom_name, len(urls) > 1),
                            audio_only=audio_only,
                            max_height=VIDEO_QUALITY_OPTIONS[quality_label],
                            audio_format=audio_format,
                            download_subtitles=download_subtitles,
                            subtitle_languages=parse_subtitle_languages(subtitle_languages_raw),
                            subtitle_format=subtitle_format,
                            include_auto_subtitles=include_auto_subtitles,
                            embed_subtitles=embed_subtitles,
                            playlist_limit=int(playlist_limit),
                            retry_count=retry_count,
                            cookie_file=cookie_file,
                            max_bytes=CLOUD_MAX_BYTES if is_cloud else None,
                        ),
                        status_ui,
                        progress_ui,
                    )
                    st.session_state["last_outcome"] = outcome

                    for message in [*outcome["warnings"], *outcome["skipped"], *outcome["errors"]]:
                        append_task_log(message)
                    for item in outcome["items"]:
                        append_task_log(f"下載完成: {item['path']}")

                    if outcome["items"]:
                        progress_ui.progress(1.0, text="下載進度: 100%")
                        status_ui.info("下載完成")
                    else:
                        status_ui.info("下載失敗")

            st.session_state["stage_progress"] = 1.0
            stage_progress_ui.progress(1.0, text="流程進度: 100%")
            health_placeholder.markdown(
                health_panel_html(*summarize_reports(current_reports())),
                unsafe_allow_html=True,
            )
        except Exception as exc:
            error_message = f"任務失敗：{exc}"
            append_task_log(error_message)
            st.error(escape_markdown(error_message))
        finally:
            st.session_state["task_running"] = False

    if st.session_state["last_outcome"]:
        render_download_outcome(st.session_state["last_outcome"], is_cloud)

    reports_to_show = current_reports()
    if reports_to_show:
        st.markdown("### 連結解析結果")
        for report in reports_to_show:
            render_link_report(report)


if __name__ == "__main__":
    main()

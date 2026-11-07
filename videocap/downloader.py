from __future__ import annotations

import json
import math
import os
import random
import re
import shutil
import subprocess
import threading
import time
from collections import deque
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol, TypedDict, cast

import yt_dlp
import yt_dlp.downloader.external as ytdlp_external
import yt_dlp.postprocessor.ffmpeg as ytdlp_ffmpeg
from yt_dlp.postprocessor import get_postprocessor
from yt_dlp.postprocessor.common import PostProcessor
from yt_dlp.postprocessor.ffmpeg import FFmpegPostProcessor
from yt_dlp.postprocessor.metadataparser import MetadataParserPP
from yt_dlp.utils import DownloadCancelled, MaxDownloadsReached, PostProcessingError

from videocap.chinese import (
    TRADITIONAL_LANGUAGE,
    ChineseConversionUnavailable,
    is_simplified,
    traditional_copy_path,
    write_traditional_copy,
)
from videocap.config import (
    COLLISION_ID_FIELD,
    COLLISION_ID_TEMPLATE,
    EXPIRED_LOGIN_MESSAGE,
    FALLBACK_PLAYER_CLIENTS,
    JOB_LOG_LINES,
    LIVE_CHAT_TRACK,
    LIVE_WAIT_MAX_SECONDS,
    LIVE_WAIT_RETRY_SECONDS,
    LIVE_WAIT_STATUS_PREFIX,
    SPONSORBLOCK_CATEGORIES,
    SPONSORBLOCK_CHAPTER_PREFIX,
    SPONSORBLOCK_UNPREFIXED_CATEGORIES,
)
from videocap.errors import (
    JS_RUNTIME_HINT,
    explain_missing_js_runtime,
    extract_failed_video_id,
    humanize_ytdlp_error,
    warns_of_missing_js_runtime,
)
from videocap.live import RecordingResult, record_live_stream, remove_if_empty, remove_quietly
from videocap.library import is_intermediate_stream, probe_media
from videocap.media_tools import reserve_output_path
from videocap.options import (
    DownloadOptions,
    build_late_postprocessors,
    build_ydl_options,
    entry_filter_reason,
    expected_final_extension,
    waits_for_live,
    writes_thumbnail,
)
from videocap.processes import remove_when_released, tie_to_app, tied_popen_options
from videocap.sidecars import release_time, set_file_times, write_comments_csv, write_live_chat_csv, write_music_tags, write_nfo
from videocap.subtitles import READABLE_SUBTITLE_EXTENSIONS, cues_to_srt, cues_to_vtt, cut_cues, read_subtitle_file, write_transcript
from videocap.tooling import format_bytes, format_duration, get_ffmpeg_status, get_free_disk_bytes


# Subtitle tracks also reach the progress hook, and they are the only downloads without a vcodec.
STREAM_LABELS: dict[Optional[str], str] = {"none": "音訊", None: "字幕"}
SUBTITLE_STREAM_LABEL = "字幕"
# Keyed by each step's pp_key(): its class name without "PP", and without a leading "FFmpeg". The owner's 進階 yt-dlp
# 參數 can add any of yt-dlp's own steps, so each of those has a label too.
POSTPROCESSOR_LABELS = {
    "Merger": "正在合併影像與音訊...",
    "ExtractAudio": "正在轉換音訊...",
    "EmbedSubtitle": "正在嵌入字幕...",
    "SponsorBlock": "正在查詢 SponsorBlock...",
    # Both SponsorBlock, which may only mark segments as chapters, and 移除片段 run through this step.
    "ModifyChapters": "正在剪除或標記片段...",
    "KeptFileCut": "正在剪除或標記片段...",
    "Metadata": "正在寫入影片資訊與章節...",
    "EmbedThumbnail": "正在嵌入封面...",
    "SplitChapters": "正在依章節分割...",
    "ThumbnailsConvertor": "正在轉換封面...",
    "VideoRemuxer": "正在轉換影片格式...",
    "VideoConvertor": "正在重新編碼影片...",
    "SubtitlesConvertor": "正在轉換字幕格式...",
    "Concat": "正在接合影片...",
    "CopyStream": "正在修正檔案...",
    "FixupDuplicateMoov": "正在修正檔案...",
    "FixupDuration": "正在修正檔案...",
    "FixupM3u8": "正在修正檔案...",
    "FixupM4a": "正在修正檔案...",
    "FixupStretched": "正在修正檔案...",
    "FixupTimestamp": "正在修正檔案...",
    "ScrubText": "正在整理文字...",
    "CutClipSubtitles": "正在對齊片段字幕...",
    "MetadataParser": "正在整理影片資訊...",
    "MetadataFromField": "正在整理影片資訊...",
    "MetadataFromTitle": "正在整理影片資訊...",
    "XAttrMetadata": "正在寫入檔案屬性...",
    "Exec": "正在執行自訂指令...",
    "ExecAfterDownload": "正在執行自訂指令...",
    "MoveFiles": "正在移動檔案...",
    "PastStreamChatOnly": "正在檢查直播聊天室...",
    "MusicTags": "正在整理歌手與歌名...",
    "SquareCover": "正在把封面裁成正方形...",
    "SponsorBlockNames": "正在命名 SponsorBlock 片段...",
    "WriteSidecars": "正在儲存留言、聊天室、繁中字幕與資訊檔...",
    "CollectDownloadedItems": "正在整理下載結果...",
    "RecordChapterFiles": "正在整理章節檔案...",
    "RecordFinalPath": "正在整理下載結果...",
    "TagChapterFiles": "正在寫入各章節的音樂標籤...",
}
# Not a yt-dlp step: runs once yt-dlp is done.
FILE_TIMES_LABEL = "正在把檔案時間改成上傳日期..."
ARCHIVE_SKIP_MARKER = "has already been recorded in the archive"
CUT_MISMATCH_MARKER = "real and expected durations mismatch"
# ModifyChapters only warns, in English, and keeps the whole video.
WHOLE_VIDEO_CUT_MARKER = "remove the entire video"
WHOLE_VIDEO_CUT_NOTE = (
    "要剪除的片段（移除片段或 SponsorBlock）涵蓋了整部影片，無法全部剪掉，所以保留了完整的影片。請檢查「移除片段」的時間。"
)
# A failed download leaves these beside the final name, and a cancelled job should not leave them behind.
PARTIAL_SUFFIXES = (".part", ".ytdl", ".part-Frag", ".temp")
WAIT_STEP_SECONDS = 0.25
WATCH_FFMPEG_SECONDS = 0.25
# yt-dlp's metadata step stores the video's link in these tags; the description tag may hold links to other videos.
LINK_TAGS = frozenset({"purl", "comment"})
VIDEO_ID_IN_LINK_RE = re.compile(r"(?:[?&]v=|youtu\.be/|/shorts/|/live/)([0-9A-Za-z_-]{11})")
# WriteSidecarsPP lists what it wrote here; yt-dlp leaves fields starting with "__" out of the saved .info.json.
SIDECAR_PATHS_FIELD = "__videocap_sidecars"
# Where CollectDownloadedItemsPP put this video's item, for RecordFinalPathPP.
ITEM_INDEX_FIELD = "__videocap_item_index"
# YouTube tracks that already are Taiwan Traditional, or close enough that a converted copy would add nothing.
REAL_TRADITIONAL_TRACKS = frozenset({"zh-tw", "zh-hant"})

# Music titles are read from a copy of the title, so the file name and the title tag fallback stay as they were.
MUSIC_TITLE_FIELD = "videocap_music_title"
# Bracketed words that mark a version of the video rather than the song: 【MV】, (Official Music Video), [4K].
MUSIC_VIDEO_TAG_RE = (
    r"(?i)\s*[(\[【（][^)\]】）]*?"
    r"(?:official|music\s*video|lyrics?|audio|visuali[sz]er|\bm/?v\b|歌詞|官方|完整版|動態|高畫質|高音質|\bhd\b|\b4k\b)"
    r"[^)\]】）]*[)\]】）]"
)
MUSIC_TRAILING_TAG_RE = r"(?i)\s+(?:official\s+)?(?:music\s+video|lyric\s+video|m/?v|audio)\s*$"
_INTERPRET, _REPLACE = MetadataParserPP.Actions.INTERPRET, MetadataParserPP.Actions.REPLACE
# Actions run in order and a later match wins, so the dash, the clearest separator, comes last.
MUSIC_TITLE_ACTIONS = [
    (_INTERPRET, "title", f"%({MUSIC_TITLE_FIELD})s"),
    (_REPLACE, MUSIC_TITLE_FIELD, MUSIC_VIDEO_TAG_RE, " "),
    (_REPLACE, MUSIC_TITLE_FIELD, MUSIC_TRAILING_TAG_RE, ""),
    # 周杰倫【告白氣球】, 五月天「溫柔」, 林俊傑《江南》: the song in brackets after the artist.
    (_INTERPRET, MUSIC_TITLE_FIELD, r"^\s*(?P<artist>[^「『《【]+?)\s*[「『《【]\s*(?P<track>[^」』》】]+?)\s*[」』》】]"),
    # Artist - Song, also with full-width dashes. A plain hyphen needs spaces around it, or "Jay-Z" would split.
    (_INTERPRET, MUSIC_TITLE_FIELD, r"^\s*(?P<artist>[^「『《【]+?)(?:\s+[-~]\s+|\s*[–—－]\s*)(?P<track>.+?)\s*$"),
]
SQUARE_CROP_FILTER = "crop='min(iw,ih)':'min(iw,ih)'"
LIVE_WAIT_NEEDS_FFMPEG_NOTE = "沒有等待開播：開播後要錄製直播，這需要 ffmpeg。請先安裝 ffmpeg，再到「系統」頁重新偵測後下載一次。"
# How yt-dlp words a stream or premiere that has not begun, in English, whatever the system's language.
UPCOMING_MARKERS = ("will begin in", "premieres in")
# yt-dlp's wait_for_video logs these; the countdown comes once a second when progress lines are on.
WAIT_LOG_PREFIX = "[wait]"
WAIT_COUNTDOWN_PREFIX = "[wait] Remaining time"
# yt-dlp's YouTube extractor says this whenever 優先使用繁體中文標題與說明 is on, which the person asked for.
HARMLESS_WARNING_RE = re.compile(r'Preferring "[^"]*" translated fields\.')


class OptionalItemFields(TypedDict, total=False):
    """Items saved before these existed lack them, and a restart restores those items too. A base class, because
    NotRequired is not seen in this module's string annotations."""

    # When the video went online, to the second.
    timestamp: Optional[float]


class DownloadedItem(OptionalItemFields):
    title: str
    path: str
    subtitle_paths: list[str]
    extra_paths: list[str]
    video_id: str
    channel: str
    webpage_url: str
    duration: Optional[float]
    upload_date: str
    thumbnail: str
    quality: str


class DownloadOutcome(TypedDict):
    items: list[DownloadedItem]
    errors: list[str]
    skipped: list[str]
    warnings: list[str]
    # Hints for a download that still succeeded, such as a lower quality than chosen. Not failures.
    notes: list[str]
    subtitle_note: str
    failed_ids: list[str]
    archived_skips: int
    partial_paths: list[str]
    stopped: bool


class StatusUI(Protocol):
    def info(self, message: str) -> None: ...


class ProgressUI(Protocol):
    def progress(self, value: float, text: str = "") -> None: ...


class JobStopped(DownloadCancelled):
    """Raised inside yt-dlp's callbacks. yt-dlp re-raises DownloadCancelled even with ignoreerrors, so this ends the job."""

    msg = "任務已停止"


class LoginPacer:
    """Spaces the videos downloaded with a login across the whole app, not only within one job: every pasted link,
    search result and retry is its own job, and logged-in jobs run back to back."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_activity: Optional[float] = None

    def mark(self) -> None:
        """A logged-in video is downloading, or just was."""
        with self._lock:
            self._last_activity = time.monotonic()

    def remaining(self, gap: float) -> float:
        """How long until `gap` seconds have passed since the last logged-in video."""
        with self._lock:
            if self._last_activity is None:
                return 0.0
            return max(self._last_activity + gap - time.monotonic(), 0.0)


LOGIN_PACER = LoginPacer()


class LiveWaitExpired(DownloadCancelled):
    """Ends a wait for a stream that would start, or has not started, within LIVE_WAIT_MAX_SECONDS."""


class TrackedPopen(yt_dlp.utils.Popen):
    """yt-dlp runs ffmpeg for clips and post-processing in calls that block without calling back, so neither a stop nor
    the size limit could reach them. Every process started on a watched job's thread is listed for its FfmpegWatchdog.
    Each is also tied to the app, so a crashed or killed app does not leave yt-dlp's ffmpeg running."""

    watched: dict[int, list[TrackedPopen]] = {}
    lock = threading.Lock()

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # Text from YouTube can reach ffmpeg's arguments through the owner's own steps too, not only through the info.
        if args and isinstance(args[0], (list, tuple)):
            args = (without_lone_surrogates(list(args[0])), *args[1:])
        super().__init__(*args, **tied_popen_options(kwargs))
        tie_to_app(self)
        with TrackedPopen.lock:
            processes = TrackedPopen.watched.get(threading.get_ident())
            if processes is not None:
                processes.append(self)


# Both modules look the name up on every call, so this reaches every ffmpeg they start.
ytdlp_external.Popen = TrackedPopen  # type: ignore[misc]
ytdlp_ffmpeg.Popen = TrackedPopen  # type: ignore[misc]


class FfmpegWatchdog:
    """Ends the ffmpeg runs of the job on this thread when the job is stopped, or when a clip grows past the size limit.
    What such a run was writing is incomplete, so it is removed."""

    def __init__(self, should_stop: Callable[[], bool], max_bytes: Optional[int], logger: YtDlpLogCollector) -> None:
        self.should_stop = should_stop
        self.max_bytes = max_bytes
        self.logger = logger
        self.thread_id = threading.get_ident()
        self.done = threading.Event()
        self.watcher = threading.Thread(target=self.watch, daemon=True)
        self.stopped = False
        # yt-dlp reports the ended ffmpeg as a failure; after a stop that is no news to anyone.
        self.errors_before_stop: Optional[int] = None

    def __enter__(self) -> FfmpegWatchdog:
        with TrackedPopen.lock:
            TrackedPopen.watched[self.thread_id] = []
        self.watcher.start()
        return self

    def __exit__(self, *args: object) -> None:
        self.done.set()
        # It may still be removing what an ended ffmpeg wrote.
        self.watcher.join()
        with TrackedPopen.lock:
            TrackedPopen.watched.pop(self.thread_id, None)

    def watch(self) -> None:
        while not self.done.wait(WATCH_FFMPEG_SECONDS):
            with TrackedPopen.lock:
                processes = TrackedPopen.watched.get(self.thread_id, [])
                processes[:] = [process for process in processes if process.poll() is None]
                running = [process for process in processes if is_ffmpeg_run(process)]
            for process in running:
                output = ffmpeg_output_path(process)
                if self.should_stop():
                    if self.errors_before_stop is None:
                        self.errors_before_stop = len(self.logger.errors)
                    self.stopped = True
                    end_ffmpeg(process, output)
                elif self.max_bytes and output and file_size(output) > self.max_bytes:
                    self.logger.error(f"{output.name}：檔案超過雲端模式上限 {format_bytes(self.max_bytes)}，已停止下載。")
                    self.logger.drop_next_error = True
                    end_ffmpeg(process, output)

    def errors(self) -> list[str]:
        return [message for message, _ in self.failures()]

    def failures(self) -> list[tuple[str, str]]:
        """Each error with the video it belongs to, or "" when unknown."""
        failures = list(zip(self.logger.errors, self.logger.error_videos))
        return failures[: self.errors_before_stop] if self.errors_before_stop is not None else failures


def is_ffmpeg_run(process: subprocess.Popen[Any]) -> bool:
    arguments = process.args if isinstance(process.args, list) else []
    return bool(arguments) and Path(str(arguments[0])).name.lower().startswith("ffmpeg")


def ffmpeg_output_path(process: subprocess.Popen[Any]) -> Optional[Path]:
    # yt-dlp always ends an ffmpeg command with its output, marked this way.
    last = str(cast(list[Any], process.args)[-1])
    return Path(last.removeprefix("file:")) if last.startswith("file:") else None


def file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def end_ffmpeg(process: subprocess.Popen[Any], output: Optional[Path]) -> None:
    try:
        process.kill()
        process.wait(timeout=10)
        if output:
            output.unlink(missing_ok=True)
    except (OSError, subprocess.TimeoutExpired):
        pass


class YtDlpLogCollector:
    """Keeps yt-dlp's messages for the UI instead of the server console."""

    def __init__(self, on_debug: Optional[Callable[[str], None]] = None) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.lines: deque[str] = deque(maxlen=JOB_LOG_LINES)
        self.archived_skips = 0
        self.on_debug = on_debug
        # Set when the app already reported a failure that yt-dlp is about to report again in its own words.
        self.drop_next_error = False
        # Set while waiting for a stream to start, which reaches the app only through these log lines.
        self.should_stop: Optional[Callable[[], bool]] = None
        # yt-dlp names the video in extraction errors only; a failed download or step of the video being
        # downloaded says nothing of which video it was. Each error's video, or "" when unknown.
        self.current_video = ""
        self.error_videos: list[str] = []

    def debug(self, message: str) -> None:
        if message.startswith(WAIT_LOG_PREFIX):
            if self.should_stop and self.should_stop():
                raise JobStopped()
            if message.startswith(WAIT_COUNTDOWN_PREFIX):
                # Once a second for hours would push everything else out of the job's log.
                return
        self.lines.append(message)
        if ARCHIVE_SKIP_MARKER in message:
            self.archived_skips += 1
        if self.on_debug:
            self.on_debug(message)

    def info(self, message: str) -> None:
        self.lines.append(message)

    def warning(self, message: str) -> None:
        if HARMLESS_WARNING_RE.search(message):
            return
        self.lines.append(f"WARNING: {message}")
        self.warnings.append(message)

    def error(self, message: str) -> None:
        self.lines.append(message)
        if self.drop_next_error:
            self.drop_next_error = False
            return
        self.errors.append(message)
        self.error_videos.append(extract_failed_video_id(message) or self.current_video)


def describe_item_quality(information: dict[str, Any]) -> str:
    vcodec, acodec = information.get("vcodec"), information.get("acodec")
    extension = str(information.get("ext") or "")
    if vcodec in (None, "none"):
        # After audio conversion acodec still names the source, so only the extension is certain.
        return extension
    parts = [str(information.get("resolution") or ""), str(vcodec).split(".")[0]]
    if acodec not in (None, "none"):
        parts.append(str(acodec).split(".")[0])
    parts.append(extension)
    return " · ".join(part for part in parts if part)


def item_from_info(
    information: Mapping[str, Any],
    path: str,
    subtitle_paths: Optional[list[str]] = None,
    extra_paths: Optional[list[str]] = None,
) -> DownloadedItem:
    return {
        "title": str(information.get("title") or information.get("id") or ""),
        "path": path,
        "subtitle_paths": subtitle_paths or [],
        "extra_paths": extra_paths or [],
        "video_id": str(information.get("id") or ""),
        "channel": str(information.get("channel") or information.get("uploader") or ""),
        "webpage_url": str(information.get("webpage_url") or ""),
        "duration": information.get("duration"),
        "upload_date": str(information.get("upload_date") or ""),
        "thumbnail": str(information.get("thumbnail") or ""),
        "quality": describe_item_quality(information) if path else "",
        "timestamp": information.get("timestamp"),
    }


class CollectDownloadedItemsPP(PostProcessor):
    """Records each finished file. Runs before the optional steps, so a failed embed cannot lose the video."""

    def __init__(self, items: list[DownloadedItem], keep_thumbnails: bool = False, has_media: bool = True) -> None:
        super().__init__()
        self.items = items
        self.keep_thumbnails = keep_thumbnails
        self.has_media = has_media

    def run(self, information: Any) -> tuple[list[str], Any]:
        requested_subtitles = cast(dict[str, dict[str, Any]], information.get("requested_subtitles") or {})
        # With ignoreerrors, yt-dlp sets a subtitle's filepath even when its download failed.
        subtitle_paths = [str(track.get("filepath") or "") for track in requested_subtitles.values()]

        extra_paths: list[str] = []
        if self.keep_thumbnails:
            extra_paths.extend(str(thumbnail.get("filepath") or "") for thumbnail in information.get("thumbnails") or [])
        extra_paths.append(str(information.get("infojson_filename") or ""))
        if self._downloader and self._downloader.params.get("writedescription"):
            extra_paths.append(str(self._downloader.prepare_filename(information, "description") or ""))
        extra_paths.extend(information.get(SIDECAR_PATHS_FIELD) or [])

        media_path = str(information["filepath"]) if self.has_media else ""
        if media_path and not Path(media_path).is_file():
            # With ignoreerrors yt-dlp carries on after a failed merge; check_final_file reports it.
            return [], information
        item = item_from_info(
            information,
            media_path,
            [path for path in subtitle_paths if path and Path(path).exists()],
            list(dict.fromkeys(path for path in extra_paths if path and Path(path).exists())),
        )
        information[ITEM_INDEX_FIELD] = len(self.items)
        self.items.append(item)
        if not self.has_media and self._downloader:
            # Captions often appear hours after an upload, so a video without them must be tried again next time.
            params = self._downloader.params
            # A chat replay alone is not what this video waits for.
            asked_for_subtitles = params.get("writesubtitles") and params.get("subtitleslangs") != [LIVE_CHAT_TRACK]
            wants_subtitles = asked_for_subtitles or params.get("writeautomaticsub")
            if item["subtitle_paths"] if wants_subtitles else item["extra_paths"]:
                self._downloader.record_download_archive(information)
        return [], information


class RecordFinalPathPP(PostProcessor):
    """The owner's own steps run after the file is recorded, and some rename it: --remux-video, --recode-video or -x.
    Runs after every other step, so the result, history, library and file times all point at the file that is left."""

    def __init__(self, items: list[DownloadedItem]) -> None:
        super().__init__()
        self.items = items

    def run(self, information: Any) -> tuple[list[str], Any]:
        index = information.get(ITEM_INDEX_FIELD)
        final = str(information.get("filepath") or "")
        if index is None or not os.path.isfile(final):
            return [], information
        item = self.items[index]
        if item["path"] != final:
            # Still there when the owner asked yt-dlp to keep it (-k).
            item["extra_paths"].append(item["path"])
            item["path"] = final
            item["quality"] = describe_item_quality(information)
        # Such a step may also have removed files, as --embed-subs does with the subtitles it embedded.
        item["subtitle_paths"] = [path for path in item["subtitle_paths"] if os.path.isfile(path)]
        item["extra_paths"] = list(dict.fromkeys(path for path in item["extra_paths"] if os.path.isfile(path)))
        return [], information


def sidecar_name(path: str, extension: str, expected_extension: Optional[str] = None) -> Path:
    """ "clip.mp4" becomes "clip.<extension>". yt-dlp's replace_extension refuses the two-part ones used here."""
    stem, current = os.path.splitext(path)
    if expected_extension and current[1:] != expected_extension:
        # The name carries no real extension, such as a title ending in ".js".
        stem = path
    return Path(f"{stem}.{extension}")


def writes_sidecars(options: DownloadOptions) -> bool:
    converts_subtitles = options.subtitles_to_traditional and options.download_subtitles
    describes_episode = options.write_nfo and options.media_type != "none"
    return options.comments_mode != "off" or options.save_live_chat or converts_subtitles or describes_episode


# Half of a UTF-16 pair, which YouTube leaves behind when it cuts a text at an emoji. Linux cannot put it in a file
# name or in ffmpeg's arguments (UnicodeEncodeError: surrogates not allowed), so it is dropped before anything is
# named or written. Windows passes such text through, which is why only Linux hosts failed.
LONE_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")


def without_lone_surrogates(value: Any) -> Any:
    if isinstance(value, str):
        return LONE_SURROGATE_RE.sub("", value)
    if isinstance(value, list):
        return [without_lone_surrogates(item) for item in value]
    if isinstance(value, dict):
        return {key: without_lone_surrogates(item) for key, item in value.items()}
    return value


class ScrubTextPP(PostProcessor):
    """Runs before every other step, so names, metadata and sidecars all see clean text."""

    def run(self, information: Any) -> tuple[list[str], Any]:
        for key, value in list(information.items()):
            if isinstance(value, (str, list, dict)):
                information[key] = without_lone_surrogates(value)
        return [], information


class CutClipSubtitlesPP(PostProcessor):
    """A clip's subtitle file is written for the whole video, so a player that loads it by name would show every
    line early. yt-dlp has written the file by before_dl, and the clip's own range sits in the section fields."""

    def run(self, information: Any) -> tuple[list[str], Any]:
        start = information.get("section_start")
        if start is None:
            return [], information
        end = information.get("section_end")
        for track in (information.get("requested_subtitles") or {}).values():
            path = Path(str((track or {}).get("filepath") or ""))
            if path.suffix.lower() not in READABLE_SUBTITLE_EXTENSIONS or not path.is_file():
                continue
            cues = cut_cues(read_subtitle_file(path), float(start), None if end is None else float(end))
            path.write_text(cues_to_srt(cues) if path.suffix.lower() == ".srt" else cues_to_vtt(cues), encoding="utf-8")
        return [], information


class PastStreamChatOnlyPP(PostProcessor):
    """YouTube also lists the chat of a stream still running or about to run, which yt-dlp would follow for as long
    as the stream lasts. Only a finished stream's replay is kept."""

    def run(self, information: Any) -> tuple[list[str], Any]:
        if information.get("live_status") != "was_live":
            (information.get("requested_subtitles") or {}).pop(LIVE_CHAT_TRACK, None)
        return [], information


class WriteSidecarsPP(PostProcessor):
    """Writes the app's own files beside each video. Runs just before CollectDownloadedItemsPP, which reports them.
    A file that cannot be written is a note: the video itself is fine."""

    def __init__(self, options: DownloadOptions, notes: list[str]) -> None:
        super().__init__()
        self.options = options
        self.notes = notes

    def run(self, information: Any) -> tuple[list[str], Any]:
        # Each file is caught on its own, whatever goes wrong: an error here would also lose the finished video.
        written: list[str] = information.setdefault(SIDECAR_PATHS_FIELD, [])
        title = information.get("title") or information.get("id")
        # Taken out of the subtitles, so it is neither embedded nor reported as one.
        chat = (information.get("requested_subtitles") or {}).pop(LIVE_CHAT_TRACK, None)
        chat_path = Path(str((chat or {}).get("filepath") or ""))
        if chat and chat_path.is_file():
            try:
                written.append(str(write_live_chat_csv(chat_path)))
            except Exception as exc:
                self.note(f"{title}：聊天室無法轉成表格（{exc}），已保留原始的 JSON 檔。")
                written.append(str(chat_path))
            else:
                if self.options.write_infojson:
                    written.append(str(chat_path))
                else:
                    remove_quietly(chat_path)
        if self.options.subtitles_to_traditional:
            self.add_traditional_subtitles(information)
        if self.options.comments_mode != "off":
            try:
                comments = write_comments_csv(self.sidecar_path(information, "comments.csv"), information)
            except Exception as exc:
                self.note(f"{title}：留言無法存檔（{exc}），影片不受影響。")
            else:
                if comments:
                    written.append(str(comments))
        if self.options.write_nfo and self.options.media_type != "none" and information.get("filepath"):
            try:
                written.append(str(write_nfo(self.sidecar_path(information, "nfo"), information)))
                written.extend(self.write_episode_thumb(information))
            except Exception as exc:
                self.note(f"{title}：媒體伺服器資訊檔（.nfo）無法寫入（{exc}），影片不受影響。")
        return [], information

    def write_episode_thumb(self, information: Any) -> list[str]:
        """Kodi and Jellyfin look for "<video>-thumb.jpg" beside the video, whatever format the saved cover is in."""
        if not self.options.write_thumbnail:
            return []
        covers = [str(cover.get("filepath")) for cover in information.get("thumbnails") or [] if os.path.isfile(str(cover.get("filepath") or ""))]
        if not covers:
            return []
        target = f"{os.path.splitext(str(information['filepath']))[0]}-thumb.jpg"
        if covers[-1].lower().endswith((".jpg", ".jpeg")):
            shutil.copyfile(covers[-1], target)
        else:
            converter = FFmpegPostProcessor(self._downloader)
            if not converter.available:
                return []
            write_jpg_cover(converter, covers[-1], target)
        return [target]

    def add_traditional_subtitles(self, information: Any) -> None:
        """Adds a Taiwan Traditional copy of a Simplified track as a zh-TW track of its own, before the subtitles are
        embedded and transcribed, so both get it too. A real Traditional track from YouTube is better than a copy."""
        requested = information.get("requested_subtitles") or {}
        tracks = {language: track for language, track in requested.items() if Path(str(track.get("filepath") or "")).is_file()}
        if any(language.lower() in REAL_TRADITIONAL_TRACKS for language in tracks):
            return
        # A track marked Simplified first. YouTube's plain "zh" is usually Simplified, and the original stays anyway.
        sources = sorted((language for language in tracks if is_simplified(language, plain_zh=True)), key=lambda language: not is_simplified(language))
        if not sources:
            return
        source = tracks[sources[0]]
        try:
            # A zh-TW file already there may be an earlier download's real track, kept when overwriting is off.
            copy = write_traditional_copy(Path(source["filepath"]), overwrite=self.options.overwrite_existing)
            if copy is None and traditional_copy_path(Path(source["filepath"])).is_file():
                copy = traditional_copy_path(Path(source["filepath"]))
        except ChineseConversionUnavailable as exc:
            self.note(f"{exc} 這次的字幕保留簡體。")
            return
        except Exception as exc:
            self.note(f"{information.get('title') or information.get('id')}：字幕無法轉成繁體（{exc}），已保留簡體字幕。")
            return
        if copy:
            requested[TRADITIONAL_LANGUAGE] = {**source, "filepath": str(copy)}

    def sidecar_path(self, information: Any, extension: str) -> Path:
        # Named like yt-dlp names its own sidecars: after the finished file, or after the file a sidecar-only job skips.
        if self.options.media_type != "none" and information.get("filepath"):
            return sidecar_name(str(information["filepath"]), extension)
        planned = self._downloader.prepare_filename(information) if self._downloader else str(information.get("id"))
        return sidecar_name(planned, extension, information.get("ext"))

    def note(self, message: str) -> None:
        if message not in self.notes:
            self.notes.append(message)


class MusicTagsPP(PostProcessor):
    """Splits a music video's title into artist and song with yt-dlp's MetadataParser, for the metadata step to tag.
    Without a match the tags keep the whole title, and the channel as the artist."""

    def __init__(self, downloader: Any) -> None:
        super().__init__(downloader)
        self.parser = MetadataParserPP(downloader, MUSIC_TITLE_ACTIONS)

    def set_downloader(self, downloader: Any) -> None:
        super().set_downloader(downloader)
        if downloader and hasattr(self, "parser"):
            self.parser.set_downloader(downloader)

    def run(self, information: Any) -> tuple[list[str], Any]:
        # YouTube Music names the song and artist itself, which beats any guess from the title.
        if not information.get("track"):
            self.parser.run(information)
            information.pop(MUSIC_TITLE_FIELD, None)
        if not information.get("artist"):
            information["artist"] = information.get("channel") or information.get("uploader")
        if information.get("playlist_index") and not information.get("track_number"):
            information["track_number"] = information["playlist_index"]
        return [], information


class SquareCoverPP(FFmpegPostProcessor):
    """Music players show covers square, and YouTube's 16:9 thumbnail would get black bars. Keeps the middle, as JPG."""

    def __init__(self, downloader: Any, notes: list[str]) -> None:
        super().__init__(downloader)
        self.notes = notes

    def run(self, information: Any) -> tuple[list[str], Any]:
        replaced: list[str] = []
        for thumbnail in information.get("thumbnails") or []:
            source = str(thumbnail.get("filepath") or "")
            if not source or not os.path.isfile(source):
                continue
            target, temporary = str(sidecar_name(source, "jpg")), str(sidecar_name(source, "square.jpg"))
            try:
                write_jpg_cover(self, source, temporary, ["-vf", SQUARE_CROP_FILTER])
                os.replace(temporary, target)
            except Exception as exc:
                remove_quietly(Path(temporary))
                message = f"{information.get('title') or information.get('id')}：封面無法裁成正方形（{exc}），保留原本的封面。"
                if message not in self.notes:
                    self.notes.append(message)
                continue
            if target != source:
                replaced.append(source)
                moves = information.get("__files_to_move") or {}
                if source in moves:
                    moves[target] = str(sidecar_name(moves.pop(source), "jpg"))
            thumbnail["filepath"] = target
        return replaced, information


def write_jpg_cover(processor: FFmpegPostProcessor, source: str, target: str, arguments: Optional[list[str]] = None) -> None:
    """One picture as JPG, through yt-dlp's ffmpeg runner, so a stop reaches it like any other ffmpeg of the job."""
    processor.real_run_ffmpeg(
        # Read as one picture, even when its name holds characters ffmpeg would take for a numbered sequence.
        [(source, ["-f", "image2", "-pattern_type", "none"])],
        [(target, [*(arguments or []), "-frames:v", "1", "-update", "1", "-q:v", "2"])],
    )


class SponsorBlockNamesPP(PostProcessor):
    """Names SponsorBlock's segments in zh-TW, such as 「[贊助片段] 贊助廣告」, for ModifyChapters to use as chapter
    titles. Runs right after SponsorBlock. A community chapter keeps the name its author gave it."""

    def run(self, information: Any) -> tuple[list[str], Any]:
        for chapter in information.get("sponsorblock_chapters") or []:
            category = chapter.get("category")
            if category == "chapter" or category not in SPONSORBLOCK_CATEGORIES:
                continue
            label = SPONSORBLOCK_CATEGORIES[category]
            name = label if category in SPONSORBLOCK_UNPREFIXED_CATEGORIES else f"{SPONSORBLOCK_CHAPTER_PREFIX} {label}"
            chapter["title"] = name
            # ModifyChapters reads the name from here, after merging overlapping segments.
            chapter["_categories"] = [(*entry[:3], name) for entry in chapter.get("_categories") or []]
        return [], information


class TagChapterFilesPP(PostProcessor):
    """yt-dlp copies the whole video's tags into each chapter file, so every track of a split album had its title.
    Runs after RecordChapterFilesPP. Formats mutagen cannot tag, such as MKV, keep yt-dlp's tags."""

    def __init__(self, notes: list[str]) -> None:
        super().__init__()
        self.notes = notes

    def run(self, information: Any) -> tuple[list[str], Any]:
        chapters = [chapter for chapter in information.get("chapters") or [] if os.path.isfile(str(chapter.get("filepath") or ""))]
        artist = str(information.get("artist") or information.get("channel") or information.get("uploader") or "")
        album = str(information.get("album") or information.get("track") or information.get("title") or "")
        for number, chapter in enumerate(chapters, start=1):
            tags = {"title": str(chapter.get("title") or ""), "tracknumber": f"{number}/{len(chapters)}", "album": album}
            try:
                write_music_tags(Path(chapter["filepath"]), {**tags, "artist": artist, "albumartist": artist})
            except Exception as exc:
                # The chapter files are fine without these tags.
                message = f"{information.get('title') or information.get('id')}：章節檔案無法寫入音樂標籤（{exc}），檔案不受影響。"
                if message not in self.notes:
                    self.notes.append(message)
        return [], information


class RecordChapterFilesPP(PostProcessor):
    """Runs after chapter splitting, because the chapter files do not exist before that."""

    def __init__(self, items: list[DownloadedItem]) -> None:
        super().__init__()
        self.items = items

    def run(self, information: Any) -> tuple[list[str], Any]:
        for item in reversed(self.items):
            if item["path"] == str(information.get("filepath")):
                chapter_paths = [str(chapter.get("filepath") or "") for chapter in information.get("chapters") or []]
                item["extra_paths"].extend(path for path in chapter_paths if path and Path(path).exists())
                break
        return [], information


def check_final_file(path: str) -> None:
    """A yt-dlp post hook. yt-dlp ignores a failed merge under ignoreerrors, then records the video in the download
    archive. An error here is the one thing that stops it, so a subscription tries that video again next time."""
    if not Path(path).is_file():
        raise FileNotFoundError(f"處理後的檔案不存在，可能是 ffmpeg 合併影音失敗：{Path(path).name}")


class KeptFileCutPP(PostProcessor):
    """Cuts sponsor segments, also from a file yt-dlp kept from an interrupted run, which may or may not be cut yet.
    yt-dlp skips a file that is already cut by itself, but reports one cut with other segments as an error; that file
    is left as it is, because cutting it again would remove the wrong parts."""

    def __init__(self, inner: PostProcessor) -> None:
        # The base constructor calls set_downloader, which needs this already.
        self.inner = inner
        super().__init__()

    def set_downloader(self, downloader: Any) -> None:
        super().set_downloader(downloader)
        if downloader:
            self.inner.set_downloader(downloader)

    def run(self, information: Any) -> tuple[list[str], Any]:
        before = dict(information)
        try:
            return self.inner.run(information)
        except PostProcessingError as exc:
            if information.get("__real_download") or CUT_MISMATCH_MARKER not in str(exc):
                raise
            # The step had already rewritten the chapter list for a cut it did not make.
            information.clear()
            information.update(before)
            self.report_warning(str(exc))
            return [], information


class ProgressTracker:
    """Combines yt-dlp's per-stream callbacks into one bar across a video's streams and a playlist's videos."""

    def __init__(self, progress_ui: ProgressUI, status_ui: Optional[StatusUI] = None) -> None:
        self.progress_ui = progress_ui
        self.status_ui = status_ui
        self.stream_count = 1
        self.finished_streams = 0
        self.fraction = 0.0
        self.announced_title = ""

    def start_item(self, info: dict[str, Any]) -> None:
        self.stream_count = max(len(info.get("requested_formats") or []), 1)
        self.finished_streams = 0
        title = str(info.get("title") or "")
        if title and title != self.announced_title and self.status_ui:
            self.status_ui.info(title)
        self.announced_title = title
        # Clips go through an ffmpeg process that reports nothing until it ends.
        is_clip = info.get("section_start") is not None or info.get("section_end") is not None
        self.progress_ui.progress(self.fraction, text="下載片段中，完成前無法顯示進度..." if is_clip else "下載中...")

    def _overall(self, info: dict[str, Any], item_fraction: float) -> tuple[float, str]:
        index, count = info.get("playlist_autonumber"), info.get("n_entries")
        if index and count:
            return min((index - 1 + item_fraction) / count, 1.0), f"[{index}/{count}] "
        return item_fraction, ""

    def on_stream_progress(self, data: dict[str, Any]) -> None:
        info = cast(dict[str, Any], data.get("info_dict") or {})
        stream = STREAM_LABELS.get(info.get("vcodec"), "影像")
        status = data.get("status")

        if status == "finished":
            if stream != SUBTITLE_STREAM_LABEL:
                self.finished_streams = min(self.finished_streams + 1, self.stream_count)
            item_fraction = self.finished_streams / self.stream_count
            self.fraction, prefix = self._overall(info, item_fraction)
            self.progress_ui.progress(self.fraction, text=f"{prefix}{stream}下載完成")
            return
        if status != "downloading":
            return

        downloaded = data.get("downloaded_bytes", 0)
        total = data.get("total_bytes") or data.get("total_bytes_estimate")
        if stream == SUBTITLE_STREAM_LABEL:
            # Subtitles are tiny, and their progress would only make the bar jump.
            self.progress_ui.progress(self.fraction, text=f"下載{stream}...")
            return

        parts: list[str] = []
        if total:
            stream_fraction = min(max(downloaded / total, 0.0), 1.0)
            item_fraction = (self.finished_streams + stream_fraction) / self.stream_count
            self.fraction, prefix = self._overall(info, item_fraction)
            parts.append(f"{prefix}下載{stream} {stream_fraction * 100:.1f}%")
            parts.append(f"{format_bytes(downloaded)} / {format_bytes(total)}")
        else:
            # A live stream has no known size.
            _, prefix = self._overall(info, 0.0)
            parts.append(f"{prefix}下載{stream} {format_bytes(downloaded)}")
        speed = data.get("speed")
        if speed:
            parts.append(f"{speed / (1024 * 1024):.2f} MB/s")
        eta = data.get("eta")
        if eta:
            minutes, seconds = divmod(int(eta), 60)
            parts.append(f"剩餘 {minutes:02d}:{seconds:02d}")
        self.progress_ui.progress(self.fraction, text=" · ".join(parts))


def strip_cookies_from_info_json(path: Path) -> None:
    """yt-dlp copies session cookies into each format's entry, and people share these files without thinking of that."""

    def clean(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: clean(item) for key, item in value.items() if key != "cookies"}
        if isinstance(value, list):
            return [clean(item) for item in value]
        return value

    data = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps(clean(data), ensure_ascii=False), encoding="utf-8")


def estimate_size(info: Mapping[str, Any]) -> Optional[float]:
    """yt-dlp gives a clip the size of the whole video, which would refuse a short clip of a long one."""
    size = info.get("filesize") or info.get("filesize_approx")
    start, end, duration = info.get("section_start"), info.get("section_end"), info.get("duration")
    if size and duration and (start is not None or end is not None):
        length = (end if end is not None else duration) - (start or 0)
        return size * min(max(length / duration, 0.0), 1.0)
    return size


def find_file_video_id(path: Path) -> Optional[str]:
    """Which video an earlier download holds, from its info file or the link the metadata step embedded."""
    try:
        saved_id = json.loads(path.with_suffix(".info.json").read_text(encoding="utf-8")).get("id")
        if saved_id:
            return str(saved_id)
    except (OSError, ValueError, AttributeError):
        pass
    tags = cast(dict[str, Any], ((probe_media(path) or {}).get("format") or {}).get("tags") or {})
    for name, value in tags.items():
        match = VIDEO_ID_IN_LINK_RE.search(str(value)) if name.lower() in LINK_TAGS else None
        if match:
            return match.group(1)
    return None


def remove_partial_files(paths: list[str], keep: set[str]) -> None:
    """Cleans up what a cancelled download left behind: unfinished files and streams still waiting to be merged.
    A complete file is never removed, because it may be a finished video or subtitle the job reported."""
    for raw_path in paths:
        path = Path(raw_path)
        candidates = [path.with_name(path.name + suffix) for suffix in PARTIAL_SUFFIXES]
        candidates.extend(path.parent.glob(f"{glob_escape(path.name)}.part-Frag*"))
        is_unfinished = path.name.endswith(PARTIAL_SUFFIXES) or is_intermediate_stream(path)
        if is_unfinished and raw_path not in keep:
            candidates.append(path)
        for candidate in candidates:
            # ffmpeg was killed a moment ago and may still hold the file on Windows.
            remove_when_released(candidate)


def glob_escape(name: str) -> str:
    return re.sub(r"([*?\[])", r"[\1]", name)


def finished_file_sizes(folder: Path) -> dict[str, int]:
    """What earlier runs of a job finished in its folder. Unfinished downloads are left out: finishing them reports their whole size."""
    sizes: dict[str, int] = {}
    for path in folder.rglob("*"):
        if path.name.endswith(PARTIAL_SUFFIXES) or ".part-Frag" in path.name:
            continue
        try:
            if path.is_file():
                sizes[os.path.normcase(os.path.abspath(path))] = path.stat().st_size
        except OSError:
            continue
    return sizes


def download_with_ytdlp(
    url: str,
    options: DownloadOptions,
    progress_ui: ProgressUI,
    status_ui: Optional[StatusUI] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    on_log: Optional[Callable[[str], None]] = None,
) -> DownloadOutcome:
    outcome, failures = download_once(url, options, progress_ui, status_ui, should_stop, on_log)
    forbidden = list(dict.fromkeys(video_id for message, video_id in failures if video_id and FORBIDDEN_MARKER in message.lower()))
    # A client the person picked is a choice to respect, and a stopped job is meant to stop.
    if not forbidden or options.network.player_client or outcome["stopped"]:
        return outcome
    if options_for_forbidden_retry(options, outcome) is None:
        return outcome

    clients = "、".join(FALLBACK_PLAYER_CLIENTS)
    message = f"{len(forbidden)} 部影片被 YouTube 拒絕（HTTP 403），改用其他 YouTube 用戶端（{clients}）再試一次。"
    if on_log:
        on_log(message)
    progress_ui.progress(0.0, text=message)
    first_errors = list(zip(outcome["errors"], failures))
    retried: list[str] = []
    still_failed: list[str] = []
    recovered = 0
    for video_id in forbidden:
        if should_stop and should_stop():
            outcome["stopped"] = True
            break
        # Asked again before each one, because each retry uses some of a public job's budget.
        retry_options = options_for_forbidden_retry(options, outcome)
        if retry_options is None:
            break
        retry, _ = download_once(f"https://www.youtube.com/watch?v={video_id}", retry_options, progress_ui, status_ui, should_stop, on_log)
        retried.append(video_id)
        still_failed.extend(retry["failed_ids"])
        recovered += bool(retry["items"])
        merge_outcome(outcome, retry)
        if retry["stopped"]:
            break
    # A retried video's first error is replaced by what its retry says; one left without a retry keeps its own.
    retry_errors = outcome["errors"][len(first_errors) :]
    outcome["errors"] = [text for text, (_, video_id) in first_errors if video_id not in retried] + retry_errors
    outcome["failed_ids"] = [video_id for video_id in outcome["failed_ids"] if video_id not in retried or video_id in still_failed]
    if recovered:
        outcome["notes"].append(f"{recovered} 部影片原本被 YouTube 拒絕（HTTP 403），改用其他 YouTube 用戶端後已下載完成。")
    return outcome


# yt-dlp words a refused format request this way, in English, whatever the system's language.
FORBIDDEN_MARKER = "http error 403"


def options_for_forbidden_retry(options: DownloadOptions, outcome: DownloadOutcome) -> Optional[DownloadOptions]:
    """One video at a time, with the fallback clients. None when a public visitor's job has no room left for it."""
    max_job_bytes = options.max_job_bytes
    if max_job_bytes:
        if options.resuming:
            # A resumed run counts every finished file in the job's folder itself, the earlier runs' and this one's,
            # so the retry keeps the whole budget.
            if sum(finished_file_sizes(options.output_dir).values()) >= max_job_bytes:
                return None
        else:
            # Each run counts only its own downloads, so a retry gets what the runs before it left of the budget.
            paths = [path for item in outcome["items"] for path in (item["path"], *item["subtitle_paths"], *item["extra_paths"])]
            max_job_bytes -= sum(file_size(Path(path)) for path in dict.fromkeys(paths) if path)
            if max_job_bytes <= 0:
                return None
    return replace(
        options,
        network=replace(options.network, player_client=",".join(FALLBACK_PLAYER_CLIENTS)),
        noplaylist=True,
        playlist_items="",
        filters=replace(options.filters, only_ids=frozenset()),
        wait_for_live=False,
        max_job_bytes=max_job_bytes,
    )


def merge_outcome(outcome: DownloadOutcome, retry: DownloadOutcome) -> None:
    outcome["items"].extend(retry["items"])
    outcome["errors"].extend(retry["errors"])
    outcome["failed_ids"].extend(video_id for video_id in retry["failed_ids"] if video_id not in outcome["failed_ids"])
    for key in ("skipped", "warnings", "notes", "partial_paths"):
        outcome[key].extend(entry for entry in retry[key] if entry not in outcome[key])  # type: ignore[literal-required]
    outcome["stopped"] = outcome["stopped"] or retry["stopped"]


def download_once(
    url: str,
    options: DownloadOptions,
    progress_ui: ProgressUI,
    status_ui: Optional[StatusUI] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    on_log: Optional[Callable[[str], None]] = None,
) -> tuple[DownloadOutcome, list[tuple[str, str]]]:
    """One yt-dlp run. Also returns yt-dlp's own error messages with their videos, in the order of the outcome's
    translated ones."""
    options.output_dir.mkdir(parents=True, exist_ok=True)
    ffmpeg_available, _ = get_ffmpeg_status()

    logger = YtDlpLogCollector(on_log)
    items: list[DownloadedItem] = []
    skipped: list[str] = []
    partial_paths: list[str] = []
    tracker = ProgressTracker(progress_ui, status_ui)
    # A resumed job's budget covers its earlier runs too, or each pause would start it afresh.
    kept_sizes = finished_file_sizes(options.output_dir) if options.resuming and options.max_job_bytes else {}
    job_bytes = sum(kept_sizes.values())
    recordings = LiveRecordings(options, progress_ui, should_stop)
    # A stream waited for is recorded, and without ffmpeg that would fail only after a wait of up to a day.
    wanted_wait = waits_for_live(options, url)
    wait_live = wanted_wait and ffmpeg_available

    def stop_if_requested() -> None:
        if should_stop and should_stop():
            raise JobStopped()

    started_videos: list[str] = []
    lower_quality: list[str] = []
    notes: list[str] = []
    logged_in = options.login.is_used

    def wait_before_next_video(video_id: str) -> None:
        # Done here rather than by yt-dlp, whose sleep cannot be interrupted, so a stop never waits it out.
        if video_id in started_videos or not options.wait_range[1]:
            return
        seconds = random.uniform(*options.wait_range)
        if logged_in:
            # Counted from the last logged-in video, in this job or an earlier one, so both are spaced alike.
            seconds = LOGIN_PACER.remaining(seconds)
            if not seconds:
                return
            text = f"登入下載：等待 {math.ceil(seconds)} 秒，避免帳號被限制"
        elif started_videos:
            text = f"等待 {seconds:.0f} 秒後下載下一部..."
        else:
            return
        progress_ui.progress(tracker.fraction, text=text)
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            stop_if_requested()
            time.sleep(min(WAIT_STEP_SECONDS, max(deadline - time.monotonic(), 0)))

    def avoid_name_of_another_video(info: dict[str, Any]) -> None:
        # yt-dlp decides by name alone: it would delete the other video's file, or keep it and report it as this one.
        if COLLISION_ID_TEMPLATE not in options.output_template or options.media_type == "none":
            return
        planned = Path(ydl.prepare_filename(info))
        final_extension = expected_final_extension(options, ffmpeg_available)
        candidates = [planned.with_suffix(f".{final_extension}")] if final_extension else []
        existing = next((path for path in [*candidates, planned] if path.is_file()), None)
        if existing is None:
            return
        video_id = str(info.get("id") or "")
        owner = find_file_video_id(existing)
        if owner != video_id and not (owner is None and options.resuming):
            info[COLLISION_ID_FIELD] = f" [{video_id}]"

    def filter_entry(info: dict[str, Any], *, incomplete: Any) -> Optional[str]:
        stop_if_requested()
        title = info.get("title") or info.get("id") or url
        # yt-dlp asks about the list itself too, before its entries, with only its playlist_* fields.
        is_list = info.get("_type") == "playlist" or not info.get("id")
        if options.filters.only_ids and not is_list and str(info["id"]) not in options.filters.only_ids:
            # Not one of the videos picked from the list, which is not worth a line in the report.
            return f"{title}：不在勾選的項目中。"
        reason = entry_filter_reason(info, bool(incomplete), options.filters)
        if not reason and info.get("live_status") == "is_live":
            # A stream someone waited for is meant to be recorded.
            if options.live_mode == "skip" and not wanted_wait:
                reason = f"{title}：直播進行中，結束後才能下載。"
            elif not ffmpeg_available:
                reason = f"{title}：錄製直播需要 ffmpeg。"
            elif not incomplete and (info.get("section_start") is not None or info.get("section_end") is not None):
                # A recording starts now, whatever the range; and yt-dlp asks once per range.
                reason = f"{title}：直播無法只錄製片段。要錄製請清空「只下載片段」。"
            else:
                if not incomplete:
                    recordings.start(info, lambda: Path(ydl.prepare_filename(info)))
                    # Not a reason for the person to see: it keeps yt-dlp from starting a download of its own.
                    return f"{title}：改由 App 錄製。"
                return None
        clip_start, duration = info.get("section_start"), info.get("duration")
        if not reason and not incomplete and clip_start and duration and clip_start >= duration:
            # yt-dlp would still save the last frame and report it as a finished clip.
            reason = f"{title}：片段起點 {format_duration(clip_start)} 超過影片長度 {format_duration(duration)}，略過這一段。"
        if not reason and not incomplete:
            size = estimate_size(info)
            if size and options.max_bytes and size > options.max_bytes:
                reason = (
                    f"{title}：檔案約 {format_bytes(size)}，超過雲端模式上限 {format_bytes(options.max_bytes)}，"
                    "請改選較低畫質或在本機執行。"
                )
            elif size and options.max_job_bytes and job_bytes + size > options.max_job_bytes:
                reason = f"{title}：這次任務已下載 {format_bytes(job_bytes)}，再下載會超過雲端模式的單次任務上限。"
            elif size:
                free = get_free_disk_bytes(options.output_dir)
                if free is not None and size > free:
                    reason = f"{title}：檔案約 {format_bytes(size)}，硬碟只剩 {format_bytes(free)}。"
        if reason and reason not in skipped:
            skipped.append(reason)
        if not reason and not incomplete:
            avoid_name_of_another_video(info)
            video_id = str(info.get("id") or "")
            logger.current_video = video_id
            wait_before_next_video(video_id)
            if logged_in:
                LOGIN_PACER.mark()
            if video_id not in started_videos:
                started_videos.append(video_id)
            note_lower_quality(info)
            tracker.start_item(info)
        return reason

    def note_lower_quality(info: dict[str, Any]) -> None:
        # Only reported when yt-dlp also warned that formats may be missing; the video may simply have no higher quality.
        height = info.get("height")
        if options.media_type != "video" or options.format_override or not options.max_height or not height:
            return
        if height < options.max_height:
            note = f"{info.get('title') or info.get('id')}：只取得 {height}p，低於設定的 {options.max_height}p"
            if note not in lower_quality:
                lower_quality.append(note)

    def progress_hook(data: dict[str, Any]) -> None:
        nonlocal job_bytes
        if logged_in:
            LOGIN_PACER.mark()
        # Recorded before checking for a stop, so the file this call is about gets cleaned up too.
        for key in ("tmpfilename", "filename"):
            filename = str(data.get(key) or "")
            if filename and filename not in partial_paths:
                partial_paths.append(filename)
        stop_if_requested()

        downloaded = data.get("downloaded_bytes") or 0
        if options.max_bytes and downloaded > options.max_bytes:
            # Live streams and some formats do not announce their size up front, so the earlier check cannot catch them.
            raise yt_dlp.utils.DownloadError(f"檔案超過雲端模式上限 {format_bytes(options.max_bytes)}，已停止下載。")
        if data.get("status") == "finished":
            # Already counted when the job began, if it is a file kept from an earlier run; such a file reports only its total.
            kept = kept_sizes.pop(os.path.normcase(os.path.abspath(str(data.get("filename") or ""))), None)
            job_bytes += 0 if kept is not None else downloaded or data.get("total_bytes") or 0
        tracker.on_stream_progress(data)

    def postprocessor_hook(data: dict[str, Any]) -> None:
        if logged_in and str((data.get("info_dict") or {}).get("id") or "") in started_videos:
            # Steps after a download count as its activity, so the next video's gap starts when this one is really
            # done. Steps before it (pre_process) must not, or the first video would wait for itself.
            LOGIN_PACER.mark()
        if data.get("status") != "started":
            # A step reports finishing before yt-dlp deletes its source files, so stopping then would strand them.
            return
        stop_if_requested()
        label = POSTPROCESSOR_LABELS.get(str(data.get("postprocessor")))
        if label:
            progress_ui.progress(tracker.fraction, text=label)

    ydl_opts = build_ydl_options(options, ffmpeg_available, logger)
    ydl_opts["match_filter"] = filter_entry
    ydl_opts["progress_hooks"] = [progress_hook]
    ydl_opts["postprocessor_hooks"] = [postprocessor_hook]
    def finished_video(path: str) -> None:
        check_final_file(path)
        # What fails after this, such as reading the rest of a list, is not this video's failure.
        logger.current_video = ""

    ydl_opts["post_hooks"] = [finished_video]
    if wait_live:
        ydl_opts["wait_for_video"] = LIVE_WAIT_RETRY_SECONDS
        logger.should_stop = should_stop

    has_media = options.media_type != "none"
    collector = CollectDownloadedItemsPP(items, keep_thumbnails=options.write_thumbnail, has_media=has_media)
    stopped = False
    expired_wait = ""
    watchdog = FfmpegWatchdog(should_stop or (lambda: False), options.max_bytes, logger)
    with watchdog, yt_dlp.YoutubeDL(cast(Any, ydl_opts)) as ydl:
        if wait_live:
            watch_live_wait(ydl, progress_ui)
        # With skip_download yt-dlp never reaches post_process, and before_dl is the last step it still runs.
        collect_when = "post_process" if has_media else "before_dl"
        ydl.add_post_processor(ScrubTextPP(), when="pre_process")
        if options.time_ranges and options.download_subtitles:
            ydl.add_post_processor(CutClipSubtitlesPP(), when="before_dl")
        if options.save_live_chat:
            ydl.add_post_processor(PastStreamChatOnlyPP(), when="pre_process")
        if any(step.get("key") == "SponsorBlock" for step in ydl_opts.get("postprocessors") or []):
            # yt-dlp added SponsorBlock itself, before any step added here.
            ydl.add_post_processor(SponsorBlockNamesPP(), when="after_filter")
        if options.music_tags and has_media:
            ydl.add_post_processor(MusicTagsPP(ydl), when="pre_process")
            if ffmpeg_available and options.media_type == "audio" and writes_thumbnail(options, ffmpeg_available):
                # After the cover is saved and any JPG conversion, before anything embeds it.
                ydl.add_post_processor(SquareCoverPP(ydl, notes), when="before_dl")
        if writes_sidecars(options):
            ydl.add_post_processor(WriteSidecarsPP(options, notes), when=collect_when)
        if has_media:
            ydl.add_post_processor(collector, when=collect_when)
        for definition in build_late_postprocessors(options, ffmpeg_available):
            arguments = dict(definition)
            key = arguments.pop("key")
            step = get_postprocessor(key)(ydl, **arguments)
            ydl.add_post_processor(cast(Any, KeptFileCutPP(step) if key == "ModifyChapters" else step))
        if options.split_chapters:
            ydl.add_post_processor(RecordChapterFilesPP(items))
            if options.music_tags:
                ydl.add_post_processor(TagChapterFilesPP(notes))
        add_owner_postprocessors(ydl, options)
        if not has_media:
            # Nothing here can lose a file, so the list is made after the owner's own steps, such as --convert-subs.
            ydl.add_post_processor(collector, when=collect_when)
        elif options.extra_ytdlp.postprocessors:
            ydl.add_post_processor(RecordFinalPathPP(items), when="after_move")
        try:
            ydl.extract_info(url, download=True)
        except MaxDownloadsReached:
            pass
        except JobStopped:
            stopped = True
        except LiveWaitExpired as exc:
            expired_wait = exc.msg

    stopped = stopped or watchdog.stopped
    # The same message for two videos is two failures.
    failures = list(dict.fromkeys(watchdog.failures()))
    if expired_wait:
        failures.append((expired_wait, ""))
    recorded_items, recording_problems = recordings.finish(status_ui)
    if logged_in and started_videos:
        # The next logged-in video, in this job or the next, is spaced from the end of this run's last one.
        LOGIN_PACER.mark()
    items.extend(recorded_items)
    failures.extend((problem, "") for problem in recording_problems)
    # A step that fails once a video is saved, such as an embed, does not make it one to download again.
    finished = {item["video_id"] for item in items if item["path"] or not has_media}
    failures = [(message, "" if video_id in finished else video_id) for message, video_id in failures]
    errors = [message for message, _ in failures]

    if options.mtime_upload_date and items:
        progress_ui.progress(tracker.fraction, text=FILE_TIMES_LABEL)
    for item in items:
        finish_item_sidecars(item, options, notes)

    humanized = [humanize_ytdlp_error(message) for message in errors]
    if wanted_wait and not wait_live and any(marker in message.lower() for message in errors for marker in UPCOMING_MARKERS):
        notes.append(LIVE_WAIT_NEEDS_FFMPEG_NOTE)
    if any(WHOLE_VIDEO_CUT_MARKER in warning for warning in logger.warnings):
        notes.append(WHOLE_VIDEO_CUT_NOTE)
    if warns_of_missing_js_runtime(logger.warnings):
        notes.extend(explain_js_runtime(errors, humanized, lower_quality))
    outcome: DownloadOutcome = {
        "items": items,
        "errors": humanized,
        "skipped": skipped,
        "warnings": list(dict.fromkeys(logger.warnings)),
        "notes": list(dict.fromkeys(notes)),
        "subtitle_note": "",
        "failed_ids": list(dict.fromkeys(video_id for _, video_id in failures if video_id)),
        "archived_skips": logger.archived_skips,
        "partial_paths": partial_paths,
        "stopped": stopped,
    }
    return outcome, failures


def add_owner_postprocessors(ydl: Any, options: DownloadOptions) -> None:
    """The owner's own steps from 進階 yt-dlp 參數, added the way yt-dlp adds its command line's, but after every step
    of the app's, so at each stage they see the app's result and have the last word."""
    for definition in options.extra_ytdlp.postprocessors:
        arguments = dict(definition)
        when = arguments.pop("when", "post_process")
        ydl.add_post_processor(get_postprocessor(arguments.pop("key"))(ydl, **arguments), when=when)


def watch_live_wait(ydl: Any, progress_ui: ProgressUI) -> None:
    """yt-dlp's wait_for_video sleeps until a scheduled stream or premiere begins, then looks again. This shows when
    it is due, ends the wait after LIVE_WAIT_MAX_SECONDS, and turns on the countdown lines that let a stop through."""
    # A private method, and the 系統 page updates yt-dlp: a later one may rename it or call it differently. Then yt-dlp
    # still waits by itself, only without this status or the limits, and a stop is seen when it looks again.
    original = getattr(ydl, "_wait_for_video", None)
    if not callable(original):
        return
    first_wait: list[float] = []

    def wait(*args: Any, **kwargs: Any) -> Any:
        ie_result = args[0] if args else kwargs.get("ie_result")
        if (
            not isinstance(ie_result, dict)
            or ie_result.get("formats")
            or ie_result.get("url")
            or ie_result.get("_type", "video") != "video"
        ):
            # Nothing to wait for, or not a call this understands; yt-dlp decides for itself.
            return original(*args, **kwargs)
        now = time.time()
        first_wait.append(now)
        release = ie_result.get("release_timestamp")
        release = release if isinstance(release, (int, float)) and not isinstance(release, bool) else None
        if release is not None and release - now > LIVE_WAIT_MAX_SECONDS:
            raise LiveWaitExpired(f"這場直播或首播預定 {format_release_time(release)} 開始，超過 24 小時的等待上限。請在開播前一天內再下載。")
        if now - first_wait[0] > LIVE_WAIT_MAX_SECONDS:
            raise LiveWaitExpired("已等待 24 小時仍未開播，可能延期或取消了，已停止等待。確認新的時間後再重新下載。")
        when = f"預定 {format_release_time(release)}" if release is not None else "時間未定"
        progress_ui.progress(0.0, text=f"{LIVE_WAIT_STATUS_PREFIX}（{when}）")
        ydl.params["noprogress"] = False
        try:
            return original(*args, **kwargs)
        finally:
            ydl.params["noprogress"] = True

    ydl._wait_for_video = wait


def format_release_time(timestamp: float) -> str:
    # The wait is for this machine's owner, so its clock is the one they read.
    moment = time.localtime(timestamp)
    return time.strftime("%H:%M" if moment[:3] == time.localtime()[:3] else "%m/%d %H:%M", moment)


def explain_js_runtime(raw_errors: list[str], errors: list[str], lower_quality: list[str]) -> list[str]:
    """Puts the JavaScript runtime hint on the failure a missing runtime caused. Without such a failure, the hint for
    videos that came out below the chosen quality is returned as a note, because those downloads still succeeded."""
    explain_missing_js_runtime(raw_errors, errors, [])
    if lower_quality and not any(JS_RUNTIME_HINT in message for message in errors):
        return [f"{'；'.join(lower_quality)}。{JS_RUNTIME_HINT}"]
    return []


class LiveRecordings:
    """Records each live stream on its own thread from the moment yt-dlp finds it, so its start is not lost while the
    job's other videos download. Their progress is shown once those downloads are done."""

    def __init__(self, options: DownloadOptions, progress_ui: ProgressUI, should_stop: Optional[Callable[[], bool]]) -> None:
        self.options = options
        self.progress_ui = progress_ui
        self.should_stop = should_stop
        self.started_ids: set[str] = set()
        self.running: list[tuple[dict[str, Any], threading.Thread, list[RecordingResult]]] = []
        self.show_progress = threading.Event()

    def start(self, info: dict[str, Any], planned_path: Callable[[], Path]) -> None:
        video_id = str(info.get("id") or "")
        if video_id in self.started_ids:
            return
        self.started_ids.add(video_id)
        # Never over an earlier recording of the same stream.
        output_path = reserve_output_path(planned_path())
        info = dict(info)
        results: list[RecordingResult] = []
        number = len(self.running)
        status = RecordingStatus(self, number)
        thread = threading.Thread(
            target=lambda: results.append(record_one_live_stream(info, output_path, self.options, status, self.should_stop)),
            daemon=True,
        )
        self.running.append((info, thread, results))
        thread.start()

    def is_first_running(self, number: int) -> bool:
        still_running = [index for index, (_, thread, _) in enumerate(self.running) if thread.is_alive()]
        return still_running[:1] == [number]

    def finish(self, status_ui: Optional[StatusUI]) -> tuple[list[DownloadedItem], list[str]]:
        self.show_progress.set()
        items: list[DownloadedItem] = []
        problems: list[str] = []
        for info, thread, results in self.running:
            if thread.is_alive() and status_ui:
                status_ui.info(str(info.get("title") or ""))
            thread.join()
            for recording in results:
                if recording.problem:
                    problems.append(recording.problem)
                if recording.path:
                    items.append(item_from_info(info, str(recording.path)))
        return items, problems


class RecordingStatus:
    """One status line at a time: the earliest recording still going, once the job's downloads are done."""

    def __init__(self, recordings: LiveRecordings, number: int) -> None:
        self.recordings = recordings
        self.number = number

    def progress(self, value: float, text: str = "") -> None:
        if self.recordings.show_progress.is_set() and self.recordings.is_first_running(self.number):
            self.recordings.progress_ui.progress(value, text=text)


def record_one_live_stream(
    info: dict[str, Any],
    output_path: Path,
    options: DownloadOptions,
    progress_ui: ProgressUI,
    should_stop: Optional[Callable[[], bool]],
) -> RecordingResult:
    _, ffmpeg = get_ffmpeg_status()
    sources = cast(list[dict[str, Any]], info.get("requested_formats") or [info])
    try:
        return record_live_stream(sources, output_path, ffmpeg, progress_ui, should_stop, options.live_max_seconds, options.network.proxy)
    except OSError as exc:
        remove_if_empty(output_path)
        return RecordingResult(problem=f"直播錄製失敗：{exc}")


def finish_item_sidecars(item: DownloadedItem, options: DownloadOptions, notes: list[str]) -> None:
    if options.write_transcript:
        for subtitle_path in item["subtitle_paths"]:
            try:
                transcript = write_transcript(Path(subtitle_path))
            except OSError:
                continue
            if transcript and str(transcript) not in item["extra_paths"]:
                item["extra_paths"].append(str(transcript))
    if options.login.is_used:
        for extra_path in item["extra_paths"]:
            if extra_path.endswith(".info.json"):
                try:
                    strip_cookies_from_info_json(Path(extra_path))
                except (OSError, ValueError):
                    pass
    if options.mtime_upload_date:
        # Last, because every step above writes a file.
        try:
            moment = release_time(item.get("timestamp"), item["upload_date"])
            if moment is not None:
                set_file_times([item["path"], *item["subtitle_paths"], *item["extra_paths"]], moment)
        except Exception as exc:
            # Raised here, it would lose every item of the job.
            notes.append(f"{item['title']}：檔案時間無法改成上傳日期（{exc}），檔案不受影響。")


def run_download(
    url: str,
    options: DownloadOptions,
    progress_ui: ProgressUI,
    status_ui: Optional[StatusUI] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    on_log: Optional[Callable[[str], None]] = None,
) -> DownloadOutcome:
    try:
        outcome = download_with_ytdlp(url, options, progress_ui, status_ui, should_stop, on_log)
    except Exception as exc:
        outcome = empty_outcome()
        outcome["errors"].append(humanize_ytdlp_error(str(exc)))
        return outcome

    if any("account cookies are no longer valid" in message for message in outcome["warnings"]):
        outcome["errors"].insert(0, EXPIRED_LOGIN_MESSAGE)

    # The same video can arrive through a playlist's two entries, and both then point at one file.
    with_media = {item["path"]: item for item in outcome["items"] if item["path"]}
    without_media = [item for item in outcome["items"] if not item["path"]]
    outcome["items"] = [*with_media.values(), *without_media]

    if options.download_subtitles:
        outcome["subtitle_note"] = describe_subtitle_shortfall(outcome)
    if outcome["archived_skips"]:
        outcome["skipped"].append(f"{outcome['archived_skips']} 部影片之前已下載過，依設定略過。")
    return outcome


def empty_outcome() -> DownloadOutcome:
    return {
        "items": [],
        "errors": [],
        "skipped": [],
        "warnings": [],
        "notes": [],
        "subtitle_note": "",
        "failed_ids": [],
        "archived_skips": 0,
        "partial_paths": [],
        "stopped": False,
    }


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

from __future__ import annotations

import io
import re
from collections.abc import Iterable, Iterator, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, TypedDict, cast

import yt_dlp

from videocap.config import (
    COMMENT_EXPORT_MAX,
    EXPIRED_LOGIN_MESSAGE,
    LIVE_CHAT_TRACK,
    NO_LOGIN_COOKIES_MESSAGE,
)
from videocap.downloader import YtDlpLogCollector, estimate_size
from videocap.errors import humanize_ytdlp_error
from videocap.options import (
    DownloadOptions,
    LoginSource,
    NetworkOptions,
    add_extractor_args,
    build_base_options,
    build_format_options,
    expected_final_extension,
)
from videocap.sidecars import comment_rows, write_csv
from videocap.tooling import format_bytes, format_duration, get_ffmpeg_status
from videocap.urls import classify_youtube_url


NO_FORMATS_MARKER = "no video formats found"
# The extension yt-dlp gives audio it takes out of its container without converting it.
ORIGINAL_AUDIO_EXTENSIONS = {"opus": "opus", "mp4a": "m4a", "vorbis": "ogg", "mp3": "mp3", "flac": "flac"}

SubtitleRow = dict[str, str]
FormatRow = dict[str, str]


class PlaylistEntry(TypedDict):
    index: int
    id: str
    title: str
    url: str
    duration: str
    channel: str
    views: Optional[int]
    # As 2024-01-02, or "" when the list does not say.
    upload_date: str


class ChapterRow(TypedDict):
    start: float
    end: float
    title: str


class LinkReport(TypedDict, total=False):
    url: str
    error: str
    video_id: str
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
    is_nested: bool
    entries: list[PlaylistEntry]
    format_rows: list[FormatRow]
    selected_format: str
    selected_size: int
    output_name: str
    chapters: list[ChapterRow]
    description: str
    tags: list[str]
    license: str
    audio_languages: list[str]


class SearchResult(TypedDict):
    id: str
    title: str
    url: str
    channel: str
    duration: str
    views: Optional[int]
    thumbnail: str


def flatten_subtitle_tracks(track_map: Mapping[str, object] | None) -> list[SubtitleRow]:
    rows: list[SubtitleRow] = []
    if not isinstance(track_map, dict):
        return rows

    for lang in sorted(track_map.keys()):
        tracks = track_map.get(lang)
        if lang == LIVE_CHAT_TRACK or not isinstance(tracks, list) or not tracks:
            continue
        track_items = [
            cast(Mapping[str, object], track) for track in cast(list[object], tracks) if isinstance(track, Mapping)
        ]
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
    thumbnails = cast(list[dict[str, Any]], info.get("thumbnails") or [])
    fallback = thumbnails[-1].get("url") if thumbnails else ""
    return str(info.get("thumbnail") or fallback or "")


def collect_resolutions(formats: list[dict[str, Any]] | None) -> list[int]:
    resolutions: set[int] = set()
    for fmt in formats or []:
        if fmt.get("vcodec") in (None, "none"):
            continue
        # YouTube names a quality after the shorter side, which matters for vertical videos.
        sides = [side for side in (fmt.get("width"), fmt.get("height")) if side]
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
    if info.get("availability") == "subscriber_only":
        details.append("會員專屬")
    if "creative commons" in str(info.get("license") or "").lower():
        details.append("Creative Commons 授權")
    return details


def is_downloadable_format(fmt: Mapping[str, Any]) -> bool:
    # Storyboards are preview images, not video.
    return fmt.get("ext") != "mhtml" and "storyboard" not in str(fmt.get("format_note") or "").lower()


def describe_formats(formats: list[dict[str, Any]] | None) -> list[FormatRow]:
    """Lists streams for a person to choose from, best quality first."""
    rows: list[FormatRow] = []
    for fmt in reversed(formats or []):
        if not is_downloadable_format(fmt):
            continue
        has_video, has_audio = fmt.get("vcodec") not in (None, "none"), fmt.get("acodec") not in (None, "none")
        kind = "影像＋音訊" if has_video and has_audio else "僅影像" if has_video else "僅音訊" if has_audio else ""
        if not kind:
            continue
        size = fmt.get("filesize") or fmt.get("filesize_approx")
        bitrate = fmt.get("tbr")
        rows.append(
            {
                "格式ID": str(fmt.get("format_id") or ""),
                "類型": kind,
                "解析度": str(fmt.get("resolution") or "") if has_video else "",
                "FPS": str(int(fmt["fps"])) if has_video and fmt.get("fps") else "",
                "動態範圍": str(fmt.get("dynamic_range") or "") if has_video else "",
                "容器": str(fmt.get("ext") or ""),
                "影像編碼": str(fmt.get("vcodec") or "").split(".")[0] if has_video else "",
                "音訊編碼": str(fmt.get("acodec") or "").split(".")[0] if has_audio else "",
                "語言": str(fmt.get("language") or ""),
                "大小": format_bytes(size) if size else "",
                "位元率": f"{bitrate:.0f} kbps" if bitrate else "",
                "備註": str(fmt.get("format_note") or ""),
            }
        )
    return rows


def describe_selected_format(info: Mapping[str, Any]) -> tuple[str, int]:
    """Summarizes the streams yt-dlp picked, so the report can show what a download would produce."""
    requested = cast(list[dict[str, Any]], info.get("requested_formats") or [])
    sources: list[Mapping[str, Any]] = [*requested] if requested else [info]
    size = sum(int(source.get("filesize") or source.get("filesize_approx") or 0) for source in sources)
    if not size and info.get("filesize_approx"):
        size = int(info["filesize_approx"])

    parts: list[str] = []
    for source in sources:
        if source.get("vcodec") not in (None, "none"):
            height, fps = source.get("height"), source.get("fps")
            quality = f"{height}p" if height else str(source.get("resolution") or "")
            dynamic_range = str(source.get("dynamic_range") or "SDR")
            hdr = "" if dynamic_range == "SDR" else f" {dynamic_range}"
            parts.append(f"{quality}{int(fps) if fps and fps > 30 else ''} {str(source.get('vcodec')).split('.')[0]}{hdr}")
        if source.get("acodec") not in (None, "none"):
            parts.append(str(source.get("acodec")).split(".")[0])
    return " + ".join(part for part in parts if part), size


def collect_audio_languages(formats: list[dict[str, Any]] | None) -> list[str]:
    languages = {
        str(fmt["language"])
        for fmt in formats or []
        if fmt.get("language") and fmt.get("acodec") not in (None, "none") and fmt.get("vcodec") in (None, "none")
    }
    return sorted(languages)


def is_container_entry(entry: Mapping[str, Any]) -> bool:
    """A listed entry that is itself a list: a channel tab, or a playlist on a channel's /playlists tab."""
    if "entries" in entry:
        return True
    if entry.get("_type") not in ("url", "url_transparent"):
        return False
    return entry.get("ie_key") == "YoutubeTab" or classify_youtube_url(str(entry.get("url") or "")) in ("playlist", "channel")


def entry_upload_date(entry: Mapping[str, Any]) -> str:
    """A flat list rarely has upload_date, but yt-dlp may give a timestamp, which it reads in UTC too."""
    upload_date = str(entry.get("upload_date") or "")
    if len(upload_date) == 8 and upload_date.isdigit():
        return f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:]}"
    timestamp = entry.get("timestamp")
    if isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool):
        try:
            return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d")
        except (OverflowError, OSError, ValueError):
            return ""
    return ""


def build_playlist_entries(info: Mapping[str, Any]) -> list[PlaylistEntry]:
    entries: list[PlaylistEntry] = []
    for index, entry in enumerate(iter_video_entries(info), start=1):
        video_id = str(entry.get("id") or "")
        entries.append(
            {
                "index": index,
                "id": video_id,
                "title": str(entry.get("title") or video_id),
                "url": str(entry.get("url") or entry.get("webpage_url") or ""),
                "duration": format_duration(entry.get("duration")),
                "channel": str(entry.get("channel") or entry.get("uploader") or ""),
                "views": entry.get("view_count"),
                "upload_date": entry_upload_date(entry),
            }
        )
    return entries


def playlist_export_rows(entries: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The rows of 匯出影片清單 CSV. Lists inspected earlier in a session may lack the newer keys, which stay empty."""
    return [
        {
            "標題": entry.get("title") or "",
            "影片 ID": entry.get("id") or "",
            "網址": entry.get("url") or "",
            "長度": entry.get("duration") or "",
            "上傳日期": entry.get("upload_date") or "",
            "觀看數": "" if entry.get("views") is None else entry["views"],
            "頻道": entry.get("channel") or "",
        }
        for entry in entries
    ]


def find_unplayable_reason(warnings: list[str]) -> str:
    """With ignore_no_formats_error, yt-dlp turns YouTube's reason for refusing a video into a warning just before its
    own "No video formats found!", which says nothing about why."""
    for index, warning in enumerate(warnings):
        if NO_FORMATS_MARKER in warning.lower():
            reasons = [earlier for earlier in warnings[:index] if earlier.startswith("[")]
            return reasons[-1] if reasons else warning
    return "No video formats found!"


def preview_extension(info: Mapping[str, Any], options: DownloadOptions, ffmpeg_available: bool) -> Optional[str]:
    final_extension = expected_final_extension(options, ffmpeg_available)
    if final_extension or options.media_type != "audio" or options.audio_format != "best" or not ffmpeg_available:
        return final_extension
    # Keeping the original audio still takes it out of its container, under the codec's own extension.
    codec = str(info.get("acodec") or "").split(".")[0]
    return ORIGINAL_AUDIO_EXTENSIONS.get(codec)


def inspect_link(url: str, options: DownloadOptions) -> LinkReport:
    ffmpeg_available, _ = get_ffmpeg_status()
    log = YtDlpLogCollector()
    opts = build_base_options(log, options.retry_count, options.login, options.network, options.noplaylist)
    opts.update(build_format_options(options, ffmpeg_available))
    opts.update(
        {
            "skip_download": True,
            "extract_flat": "in_playlist",
            "playlistend": options.playlist_limit,
            "ignore_no_formats_error": True,
            "outtmpl": {"default": options.output_template},
            "paths": {"home": str(options.output_dir)},
            "restrictfilenames": options.restrict_filenames,
        }
    )

    # A clip's name and size are those of the first clip, which is what the download starts with.
    clip = {"section_start": options.time_ranges[0][0], "section_end": options.time_ranges[0][1]} if options.time_ranges else {}
    has_media = options.media_type != "none"
    try:
        with yt_dlp.YoutubeDL(cast(Any, opts)) as ydl:
            info = cast(dict[str, Any], ydl.extract_info(url, download=False))
            output_name = ""
            if "entries" not in info and has_media:
                try:
                    output_path = Path(ydl.prepare_filename({**info, **clip}))
                    final_extension = preview_extension(info, options, ffmpeg_available)
                    output_name = (output_path.with_suffix(f".{final_extension}") if final_extension else output_path).name
                except Exception:
                    output_name = ""
    except Exception as exc:
        return {"url": url, "error": humanize_ytdlp_error(str(exc))}
    if "entries" not in info and not info.get("formats"):
        # Removed, private, members-only, age-gated or blocked: yt-dlp was told not to fail, so it only warned.
        return {"url": url, "error": humanize_ytdlp_error(find_unplayable_reason(log.warnings))}

    report: LinkReport = {
        "url": url,
        "error": "",
        "video_id": str(info.get("id") or ""),
        "title": str(info.get("title") or info.get("id") or url),
        "channel": str(info.get("channel") or info.get("uploader") or ""),
        "thumbnail": get_thumbnail_url(info),
    }

    if "entries" in info:
        raw_entries = list(info.get("entries") or [])
        # A channel's root URL lists its tabs, and its /playlists tab lists playlists, so in both the count is not a
        # number of videos and a picked row must be downloaded as a link of its own.
        is_nested = any(entry and is_container_entry(entry) for entry in raw_entries)
        report["is_playlist"] = True
        report["is_nested"] = is_nested
        report["entry_count"] = None if is_nested else info.get("playlist_count")
        report["entries"] = build_playlist_entries(info)
        return report

    formats = cast(Optional[list[dict[str, Any]]], info.get("formats"))
    # Sidecar-only downloads fetch no stream, whatever yt-dlp would pick by default.
    selected_format, selected_size = describe_selected_format(info) if has_media else ("", 0)
    if clip and selected_size:
        selected_size = int(estimate_size({**clip, "filesize": selected_size, "duration": info.get("duration")}) or 0)
    report["is_playlist"] = False
    report["is_live"] = info.get("live_status") == "is_live"
    report["details"] = format_video_details(info)
    report["resolutions"] = collect_resolutions(formats)
    report["manual_rows"] = flatten_subtitle_tracks(info.get("subtitles"))
    report["auto_rows"] = flatten_subtitle_tracks(info.get("automatic_captions"))
    report["format_rows"] = describe_formats(formats)
    report["selected_format"] = selected_format
    report["selected_size"] = selected_size
    report["output_name"] = output_name
    report["chapters"] = [
        {"start": float(chapter.get("start_time") or 0), "end": float(chapter.get("end_time") or 0), "title": str(chapter.get("title") or "")}
        for chapter in info.get("chapters") or []
    ]
    report["description"] = str(info.get("description") or "")
    report["tags"] = [str(tag) for tag in info.get("tags") or []]
    report["license"] = str(info.get("license") or "")
    report["audio_languages"] = collect_audio_languages(formats)
    return report


def summarize_reports(reports: list[LinkReport]) -> tuple[str, str]:
    if not reports:
        return "", "尚未執行健康檢查"

    failed = [report for report in reports if report.get("error")]
    if failed:
        return "red", f"{len(failed)}/{len(reports)} 個連結無法解析：{failed[0].get('error')}"
    if any(report.get("is_live") for report in reports):
        return "yellow", "連結可用，但有影片正在直播，結束後才能下載。"

    if any(report.get("is_playlist") and not report.get("entries") for report in reports):
        return "yellow", "連結可用，但沒有列出任何影片：可能需要先登入（例如訂閱內容、首頁），或清單是空的。"
    videos = [report for report in reports if not report.get("is_playlist")]
    if any(not report.get("manual_rows") and not report.get("auto_rows") for report in videos):
        return "yellow", "連結可用，但部分影片未偵測到字幕軌。"
    return "green", "連結可用，且可讀取字幕軌。" if videos else "連結可用。"


def search_youtube(
    query: str, count: int, login: LoginSource, retry_count: int = 3, network: Optional[NetworkOptions] = None
) -> list[SearchResult]:
    opts = build_base_options(YtDlpLogCollector(), retry_count, login, network)
    opts.update({"skip_download": True, "extract_flat": "in_playlist"})
    with yt_dlp.YoutubeDL(cast(Any, opts)) as ydl:
        info = cast(dict[str, Any], ydl.extract_info(f"ytsearch{count}:{query}", download=False))

    results: list[SearchResult] = []
    for entry in iter_video_entries(info):
        video_id = str(entry.get("id") or "")
        if not video_id:
            continue
        results.append(
            {
                "id": video_id,
                "title": str(entry.get("title") or video_id),
                "url": str(entry.get("url") or f"https://www.youtube.com/watch?v={video_id}"),
                "channel": str(entry.get("channel") or entry.get("uploader") or ""),
                "duration": format_duration(entry.get("duration")),
                "views": entry.get("view_count"),
                "thumbnail": get_thumbnail_url(entry),
            }
        )
    return results


def list_recent_video_ids(
    url: str, limit: int, login: LoginSource, network: Optional[NetworkOptions] = None
) -> tuple[str, list[str]]:
    """Reads a channel or playlist without downloading. Returns its title and the IDs of its newest videos."""
    opts = build_base_options(YtDlpLogCollector(), 3, login, network, noplaylist=False)
    opts.update({"skip_download": True, "extract_flat": "in_playlist", "playlistend": limit})
    with yt_dlp.YoutubeDL(cast(Any, opts)) as ydl:
        info = cast(dict[str, Any], ydl.extract_info(url, download=False))
    # A channel's root lists each tab separately, each already cut to the limit, so all of them count.
    video_ids = [str(entry.get("id")) for entry in iter_video_entries(info) if entry.get("id")]
    return str(info.get("title") or info.get("channel") or url), video_ids


def fetch_comments(
    url: str, login: LoginSource, limit: int = COMMENT_EXPORT_MAX, network: Optional[NetworkOptions] = None
) -> list[dict[str, Any]]:
    opts = build_base_options(YtDlpLogCollector(), 3, login, network)
    opts.update({"skip_download": True, "getcomments": True, "ignore_no_formats_error": True})
    # Added to, not replacing, the player client and language chosen in the sidebar.
    add_extractor_args(opts, {"youtube": {"max_comments": [str(limit), "all", "all", "0"], "comment_sort": ["top"]}})
    with yt_dlp.YoutubeDL(cast(Any, opts)) as ydl:
        info = cast(dict[str, Any], ydl.extract_info(url, download=False))
    # The same columns as the comments CSV saved beside a download.
    return comment_rows(info)


def rows_to_csv(rows: list[dict[str, Any]]) -> bytes:
    """Excel only reads UTF-8 CSV correctly when it starts with a byte order mark. Formulas are neutralised the same
    way as in the CSV files saved beside a download."""
    buffer = io.StringIO(newline="")
    write_csv(buffer, rows)
    # Half of an emoji, which YouTube's text sometimes holds, becomes "?" rather than failing the whole export.
    return b"\xef\xbb\xbf" + buffer.getvalue().encode("utf-8", errors="replace")


def find_login_problem(login: LoginSource) -> str:
    """Says why this login cannot work, or returns an empty string. Reads cookies only; nothing goes to YouTube."""
    if not login.is_used:
        return ""
    try:
        with yt_dlp.YoutubeDL(cast(Any, build_base_options(YtDlpLogCollector(), 1, login))) as ydl:
            if not cast(Any, ydl).get_info_extractor("Youtube").is_authenticated:
                return NO_LOGIN_COOKIES_MESSAGE
    except Exception as exc:
        return humanize_ytdlp_error(str(exc))
    return ""


def check_youtube_login(login: LoginSource, network: Optional[NetworkOptions] = None) -> tuple[str, str]:
    """Asks YouTube whether these cookies are still logged in. Returns a Streamlit alert level and a message."""
    problem = find_login_problem(login)
    if problem:
        return "error", problem

    try:
        with yt_dlp.YoutubeDL(cast(Any, build_base_options(YtDlpLogCollector(), 1, login, network))) as ydl:
            page = cast(Any, ydl).urlopen("https://www.youtube.com/").read().decode("utf-8", "replace")
            # YouTube clears LOGIN_INFO in its response when it rejects rotated cookies.
            still_authenticated = cast(Any, ydl).get_info_extractor("Youtube").is_authenticated
    except Exception as exc:
        return "error", humanize_ytdlp_error(str(exc))

    logged_in = re.search(r'"LOGGED_IN"\s*:\s*(true|false)', page)
    if not still_authenticated or (logged_in and logged_in.group(1) == "false"):
        return "error", EXPIRED_LOGIN_MESSAGE
    if logged_in:
        return "success", "已登入 YouTube。如果會員影片仍被拒絕，代表這個帳號沒有該頻道的會員資格。"
    return "warning", "讀到登入資訊，但無法向 YouTube 確認是否有效，可以直接試著下載。"


def check_youtube_reachable(network: Optional[NetworkOptions] = None) -> tuple[bool, str]:
    """Makes one request to YouTube, to tell network problems apart from problems with a particular video."""
    try:
        with yt_dlp.YoutubeDL(cast(Any, build_base_options(YtDlpLogCollector(), 1, LoginSource(), network))) as ydl:
            response = cast(Any, ydl).urlopen("https://www.youtube.com/robots.txt")
            response.read(64)
    except Exception as exc:
        return False, humanize_ytdlp_error(str(exc))
    return True, "可以連上 YouTube。"

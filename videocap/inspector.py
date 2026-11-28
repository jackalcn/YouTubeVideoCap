from __future__ import annotations

import io
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, TypedDict, cast

import yt_dlp

from videocap import impersonation
from videocap.config import (
    COMMENT_EXPORT_MAX,
    EXPIRED_LOGIN_MESSAGE,
    LIVE_CHAT_TRACK,
    NO_LOGIN_COOKIES_MESSAGE,
)
from videocap.downloader import (
    AUDIO_EXTENSIONS,
    VIDEO_EXTENSIONS,
    DirectLinkTypePP,
    PageStreamPP,
    UrlKeyedIdPP,
    YtDlpLogCollector,
    estimate_size,
    refusal_in,
    tell_of_human_check,
)
from videocap.errors import STREAM_REFUSED, add_referer_advice, failure_kind, humanize_ytdlp_error
from videocap.options import (
    DownloadOptions,
    LoginSource,
    NetworkOptions,
    add_extractor_args,
    build_base_options,
    build_format_options,
    build_read_options,
    expected_final_extension,
    options_for_candidate,
    owner_referer,
)
from videocap.page_streams import StreamSearch, holds_no_video
from videocap.sidecars import comment_rows, write_csv
from videocap.tooling import format_bytes, format_duration, get_ffmpeg_status
from videocap.sites import is_youtube_url, site_label
from videocap.urls import classify_youtube_url


NO_FORMATS_MARKER = "no video formats found"
# yt-dlp says this instead when the extractor dropped every format for being encrypted.
DRM_MARKER = "drm protected"
# What the format table says of a codec, or a kind of stream, that yt-dlp was not told.
UNKNOWN = "未知"
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
    # With an error that is a site's check for a person: the address that answered with it, which is where the check
    # is to be passed. Not always the link's own: a page's stream, or a page that it embeds, may be on another site.
    verify_url: str
    video_id: str
    # The yt-dlp extractor that read the link, in lower case, and the site's name for the person.
    extractor: str
    site: str
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
    # Whether picked entries can be downloaded by their IDs, as YouTube's can; otherwise by their positions in the list.
    entries_have_ids: bool
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


def has_codec(codec: Any) -> bool:
    # yt-dlp's "none" says a stream is not there; None, that it was not told.
    return codec not in (None, "none")


def has_picture(fmt: Mapping[str, Any]) -> Optional[bool]:
    """Whether a format holds a picture, or None when nothing says. Without codecs, as for a direct link's file or a
    site that lists only its files, the picture size or the file's extension tells."""
    vcodec = fmt.get("vcodec")
    if vcodec is not None:
        return vcodec != "none"
    extension = str(fmt.get("ext") or "").lower()
    if fmt.get("width") or fmt.get("height") or extension in VIDEO_EXTENSIONS:
        return True
    if extension in AUDIO_EXTENSIONS or has_codec(fmt.get("acodec")):
        return False
    return None


def has_sound(fmt: Mapping[str, Any]) -> Optional[bool]:
    acodec = fmt.get("acodec")
    return None if acodec is None else acodec != "none"


def stream_kind(picture: Optional[bool], sound: Optional[bool]) -> str:
    """The 類型 column. Empty for a format with neither, such as a storyboard's pictures."""
    if picture is None:
        return UNKNOWN
    if picture:
        return "影像＋音訊" if sound else "僅影像" if sound is False else "影像"
    return "" if sound is False else "僅音訊"


def codec_name(codec: Any) -> str:
    return str(codec).split(".")[0] if has_codec(codec) else UNKNOWN


def picture_size(fmt: Mapping[str, Any]) -> str:
    width, height = fmt.get("width"), fmt.get("height")
    return str(fmt.get("resolution") or (f"{width}x{height}" if width and height else ""))


def listed_formats(info: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The formats yt-dlp found. A result that is its own only format, such as a file whose server did not say what it
    is, comes without a list: yt-dlp leaves it out rather than have the result hold itself."""
    formats = cast(Optional[list[dict[str, Any]]], info.get("formats"))
    if formats:
        return formats
    return [cast(dict[str, Any], info)] if info.get("url") and info.get("_type", "video") == "video" else []


def collect_resolutions(formats: list[dict[str, Any]] | None) -> list[int]:
    resolutions: set[int] = set()
    for fmt in formats or []:
        if not has_picture(fmt):
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
        picture, sound = has_picture(fmt), has_sound(fmt)
        kind = stream_kind(picture, sound)
        if not kind:
            continue
        # A size yt-dlp was not told stays empty, rather than showing as 0 B.
        size = fmt.get("filesize") or fmt.get("filesize_approx")
        bitrate = fmt.get("tbr")
        rows.append(
            {
                "格式ID": str(fmt.get("format_id") or ""),
                "類型": kind,
                "解析度": picture_size(fmt) if picture else "",
                "FPS": str(int(fmt["fps"])) if picture and fmt.get("fps") else "",
                # yt-dlp guesses SDR for any stream not marked as sound alone, also one it knows nothing of.
                "動態範圍": str(fmt.get("dynamic_range") or "") if has_codec(fmt.get("vcodec")) else "",
                "容器": str(fmt.get("ext") or ""),
                "影像編碼": codec_name(fmt.get("vcodec")) if picture is not False else "",
                "音訊編碼": codec_name(fmt.get("acodec")) if sound is not False else "",
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
        vcodec, acodec = source.get("vcodec"), source.get("acodec")
        if has_codec(vcodec):
            height, fps = source.get("height"), source.get("fps")
            quality = f"{height}p" if height else str(source.get("resolution") or "")
            dynamic_range = str(source.get("dynamic_range") or "SDR")
            hdr = "" if dynamic_range == "SDR" else f" {dynamic_range}"
            parts.append(f"{quality}{int(fps) if fps and fps > 30 else ''} {str(vcodec).split('.')[0]}{hdr}")
        if has_codec(acodec):
            parts.append(str(acodec).split(".")[0])
        if not has_codec(vcodec) and not has_codec(acodec):
            # A file that names no codec, as a direct link's does: its picture size and its container are what is known.
            height = source.get("height") if vcodec is None else None
            parts.append(" ".join(part for part in (f"{height}p" if height else "", str(source.get("ext") or "")) if part))
    return " + ".join(part for part in parts if part), size


def collect_audio_languages(formats: list[dict[str, Any]] | None) -> list[str]:
    languages = {
        str(fmt["language"])
        for fmt in formats or []
        if fmt.get("language") and fmt.get("acodec") not in (None, "none") and fmt.get("vcodec") in (None, "none")
    }
    return sorted(languages)


def is_container_entry(entry: Mapping[str, Any]) -> bool:
    """A listed entry that is itself a list: a channel tab, or a playlist on a channel's /playlists tab. Only YouTube's
    links tell by their shape; another site's entry is a list only when yt-dlp says so."""
    if "entries" in entry or entry.get("_type") == "playlist":
        return True
    if entry.get("_type") not in ("url", "url_transparent"):
        return False
    return entry.get("ie_key") == "YoutubeTab" or classify_youtube_url(str(entry.get("url") or "")) in ("playlist", "channel")


def entry_link(entry: Mapping[str, Any], list_page: str) -> str:
    """An entry's own link. A fully read entry's "url" is the stream yt-dlp picked for it, which may expire, so its page
    is its link. The players of an ordinary page all have that page, though, so each one's file is its link."""
    if entry.get("_type") in ("url", "url_transparent"):
        return str(entry.get("url") or "")
    page = str(entry.get("webpage_url") or "")
    if page and page != list_page:
        return page
    return str(entry.get("url") or page)


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
    """Each entry with its position, and its own ID and link when it has them ("" when not). Nothing is made up from
    an ID: another site's would make no YouTube link."""
    entries: list[PlaylistEntry] = []
    list_page = str(info.get("webpage_url") or "")
    for index, entry in enumerate(iter_video_entries(info), start=1):
        video_id = str(entry.get("id") or "")
        entries.append(
            {
                "index": index,
                "id": video_id,
                "title": str(entry.get("title") or video_id),
                "url": entry_link(entry, list_page),
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
    own "No video formats found!", which says nothing about why. A video whose every format is encrypted ends with
    "This video is DRM protected" instead, which the site's own warning says first."""
    for index, warning in enumerate(warnings):
        if DRM_MARKER in warning.lower():
            return warning
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
    report = read_link(url, options)
    if report.get("error"):
        report["error"] = add_referer_advice(report["error"], owner_referer(options.extra_ytdlp))
    return report


def read_link(url: str, options: DownloadOptions) -> LinkReport:
    refusals = impersonation.Refusals()
    report, _ = inspect_once(url, options, refusals)
    if report.get("error") and refusals.forbidden and not options.impersonate and impersonation.offered_to(url, options.other_sites):
        # The site answered HTTP 403, so once more with every request made the way a browser makes it: the download does
        # the same, in downloader.download_with_ytdlp.
        options = replace(options, impersonate=True)
        refusals = impersonation.Refusals()
        report, failure = inspect_once(url, options, refusals)
        if failure and refusals.challenged:
            # yt-dlp calls it one more HTTP 403. The site's own answer said it wants a person, which no retry gets past.
            report["error"], report["verify_url"] = tell_of_human_check(failure, url, options, refusals)
            return report
    # A page that gave nothing away, or whose own page was let through while yt-dlp's pick among what it found was
    # refused (see videocap.errors.STREAM_REFUSED): either way, the page's own scripts and iframes may know a better one.
    if report.get("error") and not options.page_stream and options.other_sites and (
        holds_no_video(url, [report["error"]]) or failure_kind(report["error"]) == STREAM_REFUSED
    ):
        return inspect_stream_in_page(url, options, report)
    return report


def inspect_stream_in_page(url: str, options: DownloadOptions, report: LinkReport) -> LinkReport:
    """A page that holds no video yt-dlp can see, or whose own pick among what it saw was refused: the report of what
    a search of the page leads to instead (see videocap.page_streams.StreamSearch), which is the first of its
    candidates that can be read. When none can, the report says what the search makes of that, which is the page's own
    report when nothing better was found."""
    search = StreamSearch(build_read_options(options, impersonation.Silent()), url, options.other_sites)
    for candidate in search.candidates():
        found = inspect_link(candidate.address, options_for_candidate(options, candidate))
        if not found.get("error"):
            found["url"] = url
            found["site"] = site_label(found.get("extractor", ""), url)
            found["details"] = [candidate.source(), *found.get("details", [])]
            return found
        search.failed(candidate, found["error"], found.get("verify_url", ""))
    failure = search.failure(report["error"])
    failed: LinkReport = {"url": url, "error": failure.error}
    if failure.verify_url:
        failed["verify_url"] = failure.verify_url
    return failed


def inspect_once(url: str, options: DownloadOptions, refusals: impersonation.Refusals) -> tuple[LinkReport, str]:
    """One look at the link. Also returns yt-dlp's own words for what it failed with, or ""."""
    ffmpeg_available, _ = get_ffmpeg_status()
    log = YtDlpLogCollector()
    # The owner's own say on how requests are made, as in the download: a link that the download reads must be read here.
    opts = build_read_options(options, log)
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
        with impersonation.youtube_dl(yt_dlp.YoutubeDL, opts, url, options.other_sites, refusals) as ydl:
            if options.other_sites:
                if options.page_stream:
                    ydl.add_post_processor(PageStreamPP(options.page_stream), when="pre_process")
                # The same ID the download will give a direct link's or a page's video, and so the same file name.
                ydl.add_post_processor(UrlKeyedIdPP(), when="pre_process")
                ydl.add_post_processor(DirectLinkTypePP(), when="pre_process")
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
        advice = impersonation.advice(options.other_sites)
        return {"url": url, "error": humanize_ytdlp_error(str(exc), url, advice, refusals.addresses)}, refusal_in([str(exc)], log.warnings)
    formats = listed_formats(info)
    if "entries" not in info and not formats:
        # Removed, private, members-only, age-gated or blocked: yt-dlp was told not to fail, so it only warned.
        reason = find_unplayable_reason(log.warnings)
        advice = impersonation.advice(options.other_sites)
        return {"url": url, "error": humanize_ytdlp_error(reason, url, advice, refusals.addresses)}, refusal_in([reason], log.warnings)

    report: LinkReport = {
        "url": url,
        "error": "",
        "video_id": str(info.get("id") or ""),
        "extractor": str(info.get("extractor_key") or "").lower(),
        "site": site_label(str(info.get("extractor_key") or ""), url),
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
        # Only YouTube's videos are picked by ID, which stays put as a channel adds videos. Another site's may have no
        # ID, or one the app gives it only after yt-dlp has asked which entries to take, so those go by position.
        is_youtube = report["extractor"].startswith("youtube") if report["extractor"] else is_youtube_url(url)
        report["entries_have_ids"] = is_youtube and all(entry["id"] for entry in report["entries"])
        return report, ""

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
    return report, ""


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

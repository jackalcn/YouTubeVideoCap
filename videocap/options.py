from __future__ import annotations

import functools
import optparse
import re
import shlex
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol

import yt_dlp
from yt_dlp.options import create_parser
from yt_dlp.postprocessor.sponsorblock import SponsorBlockPP
from yt_dlp.utils import download_range_func

from videocap.config import (
    CHAPTER_FILE_TEMPLATE,
    CLOUD_MAX_BYTES,
    CLOUD_MAX_COMMENTS,
    CLOUD_MAX_JOB_BYTES,
    CLOUD_MAX_PLAYLIST_LIMIT,
    CLOUD_MAX_WAIT_SECONDS,
    COLLISION_ID_TEMPLATE,
    DEFAULT_COMMENTS_MAX,
    DEFAULT_CUSTOM_NAME,
    DEFAULT_SUBTITLE_LANGUAGES,
    FOLDER_MEDIA_SERVER,
    FOLDER_TEMPLATES,
    LIVE_CHAT_TRACK,
    LOCAL_MAX_PLAYLIST_LIMIT,
    LOGGED_IN_MAX_SLEEP_SECONDS,
    LOGGED_IN_MIN_SLEEP_SECONDS,
    MEDIA_SERVER_FILE_TEMPLATE,
    NAMING_CUSTOM,
    NAMING_DATED,
    NAMING_TEMPLATE,
    PLAYLIST_INDEX_TEMPLATE,
    PLAYER_CLIENTS_WITHOUT_COOKIES,
    PLAYLIST_PICKER_MAX_ITEMS,
    SECTION_SUFFIX_TEMPLATE,
    TITLE_TEMPLATE,
    TRADITIONAL_METADATA_LANGUAGE,
    VIDEO_QUALITY_OPTIONS,
    YOUTUBE_EXTRACTORS,
)
from videocap.settings import DownloadSettings, JobSpec
from videocap.urls import apply_channel_tab, parse_date_filter, parse_time_ranges, url_identity, validate_playlist_items


VIDEO_CODEC_SORT = {
    # YouTube offers H.264 up to 1080p; preferring it keeps the MP4 playable on every device.
    "h264": ["vcodec:h264", "acodec:aac"],
    "vp9": ["vcodec:vp9", "acodec:opus"],
    "av1": ["vcodec:av01", "acodec:opus"],
    # yt-dlp's own default order ranks the newest codecs first.
    "best": [],
}
# merge_output_format only falls back if the codecs do not fit: webm cannot hold H.264.
MERGE_OUTPUT_FORMATS = {"mp4": "mp4", "mkv": "mkv", "webm": "webm/mkv"}
# merge_output_format does nothing for a video that arrives as a single file, which needs remuxing instead.
REMUX_TARGETS = {"mkv": "mkv", "webm": "webm>webm/mkv"}
AUDIO_SOURCE_FILTERS = {"m4a": "[ext=m4a]", "opus": "[acodec^=opus]"}
# Formats whose bitrate setting has no effect, because they are lossless or kept as they are. M4A and Opus are what
# YouTube itself serves, and yt-dlp copies a matching source without re-encoding it.
FIXED_QUALITY_AUDIO_FORMATS = frozenset({"m4a", "opus", "flac", "wav", "best"})
# What the sidebar should offer a bitrate for.
BITRATE_AUDIO_FORMATS = frozenset({"mp3"})
# The language goes into a format selector string, so it must not be able to change the selector.
SAFE_LANGUAGE_RE = re.compile(r"^[A-Za-z0-9-]{1,20}$")
SAFE_SUBTITLE_LANGUAGE_RE = re.compile(r"^(all|-?[A-Za-z0-9_-]{1,20}(\.\*)?)$")
SHORTS_MAX_SECONDS = 180
FILTERED_SCAN_FACTOR = 5
# The picker shows at most PLAYLIST_PICKER_MAX_ITEMS entries; this leaves room for as many new uploads above them.
PICKED_VIDEOS_SCAN_ITEMS = 2 * PLAYLIST_PICKER_MAX_ITEMS
# A highlight or a community chapter is a spot, not a segment, so yt-dlp can mark it but refuses to remove it.
MARK_ONLY_SPONSORBLOCK_CATEGORIES = frozenset(SponsorBlockPP.NON_SKIPPABLE_CATEGORIES)
# yt-dlp's default joins English names; "name" is the segment's own, which the downloader puts in zh-TW.
SPONSORBLOCK_CHAPTER_TITLE = "%(name)s"
# The app needs these for progress, stopping, the size limits, login, the archive and where files go, so the owner's
# extra yt-dlp options may not replace them.
APP_MANAGED_YTDLP_KEYS = frozenset(
    {
        "logger",
        "progress_hooks",
        "postprocessor_hooks",
        "match_filter",
        "allowed_extractors",
        "cookiefile",
        "cookiesfrombrowser",
        "download_archive",
        "paths",
        "outtmpl",
        "overwrites",
        "max_downloads",
        # yt-dlp's own wait lets neither pause nor cancel through and never gives up; 等待開播 does both.
        "wait_for_video",
    }
)
# With any of these yt-dlp only lists information, or saves no complete file, and the job would end as done without one.
NO_FILE_YTDLP_OPTIONS = (
    ("skip_download", "--skip-download／--no-download"),
    ("listformats", "-F／--list-formats"),
    ("listsubtitles", "--list-subs"),
    ("list_thumbnails", "--list-thumbnails"),
    ("extract_flat", "--flat-playlist"),
    ("test", "--test"),
)
# These turn on --simulate too, unless --no-simulate is also given.
PRINT_YTDLP_OPTIONS = (
    ("forceprint", "--print"),
    ("forcejson", "-j／--dump-json"),
    ("dump_single_json", "-J／--dump-single-json"),
    *(
        (key, "--get-…")
        for key in ("forceurl", "forcetitle", "forceid", "forcethumbnail", "forcedescription", "forceduration", "forcefilename", "forceformat")
    ),
)


@dataclass(frozen=True)
class LoginSource:
    cookie_file: Optional[Path] = None
    browser: Optional[str] = None

    @property
    def is_used(self) -> bool:
        return bool(self.cookie_file or self.browser)


@dataclass(frozen=True)
class NetworkOptions:
    """How requests reach YouTube and what they ask it for. Every engine function that talks to YouTube takes these."""

    rate_limit_bytes: Optional[int] = None
    sleep_seconds: int = 0
    proxy: str = ""
    force_ipv4: bool = False
    # YouTube player clients yt-dlp should use instead of its own choice, comma-separated; empty leaves it to yt-dlp.
    player_client: str = ""
    # Asks YouTube for titles and descriptions the creator translated into this language.
    metadata_language: str = ""


@dataclass(frozen=True)
class ExtraYtDlpOptions:
    """What this machine's owner typed into 進階 yt-dlp 參數, already parsed by yt-dlp's own command line."""

    # Only what differs from yt-dlp's defaults, minus APP_MANAGED_YTDLP_KEYS.
    params: Mapping[str, Any] = field(default_factory=dict)
    # yt-dlp's post-processor definitions, each with its "when".
    postprocessors: tuple[Mapping[str, Any], ...] = ()
    # The yt-dlp option names that were left out because the app manages them.
    ignored: tuple[str, ...] = ()


@dataclass(frozen=True)
class EntryFilters:
    min_seconds: int = 0
    max_seconds: int = 0
    date_after: str = ""
    date_before: str = ""
    min_views: int = 0
    title_include: str = ""
    title_exclude: str = ""
    skip_shorts: bool = False
    cc_only: bool = False
    # A regular expression can take exponential time on a crafted pattern, and on a public host one visitor's
    # pattern would stall everyone, so only this machine's owner gets them.
    title_is_regex: bool = False
    # Videos picked from a list; any other entry is passed over.
    only_ids: frozenset[str] = frozenset()


@dataclass(frozen=True)
class AccessLimits:
    """What this visitor may do. A public host is shared, so its visitors must not reach the server's files or network settings."""

    trusted: bool = True
    max_playlist: int = LOCAL_MAX_PLAYLIST_LIMIT
    max_file_bytes: Optional[int] = None
    max_job_bytes: Optional[int] = None

    @classmethod
    def for_public_host(cls) -> AccessLimits:
        return cls(
            trusted=False,
            max_playlist=CLOUD_MAX_PLAYLIST_LIMIT,
            max_file_bytes=CLOUD_MAX_BYTES,
            max_job_bytes=CLOUD_MAX_JOB_BYTES,
        )


@dataclass(frozen=True)
class DownloadOptions:
    output_dir: Path
    output_template: str
    media_type: str = "video"
    max_height: Optional[int] = None
    video_codec: str = "h264"
    container: str = "mp4"
    prefer_hdr: bool = False
    format_override: str = ""
    audio_format: str = "m4a"
    audio_bitrate: str = "192"
    audio_language: str = ""
    download_subtitles: bool = False
    subtitle_languages: tuple[str, ...] = ()
    subtitle_format: str = "srt"
    include_auto_subtitles: bool = False
    embed_subtitles: bool = False
    write_transcript: bool = False
    time_ranges: tuple[tuple[float, float], ...] = ()
    precise_cuts: bool = False
    embed_chapters: bool = False
    split_chapters: bool = False
    sponsorblock_mode: str = "off"
    sponsorblock_categories: tuple[str, ...] = ()
    # Cut out of the video, the rest joined into one file.
    remove_ranges: tuple[tuple[float, float], ...] = ()
    embed_metadata: bool = False
    embed_thumbnail: bool = False
    write_thumbnail: bool = False
    write_description: bool = False
    write_infojson: bool = False
    # Attaches the .info.json to an MKV video. Never with a login, whose session cookies the info.json would carry.
    embed_infojson: bool = False
    # Converts saved and embedded covers to JPG, which more programs and TVs open than YouTube's WebP.
    thumbnails_as_jpg: bool = False
    playlist_limit: int = 20
    playlist_items: str = ""
    playlist_reverse: bool = False
    noplaylist: bool = True
    filters: EntryFilters = field(default_factory=EntryFilters)
    archive_path: Optional[Path] = None
    live_mode: str = "skip"
    live_max_seconds: Optional[int] = None
    # The pause before each video after the first, as a (shortest, longest) range in seconds.
    wait_range: tuple[float, float] = (0.0, 0.0)
    restrict_filenames: bool = False
    overwrite_existing: bool = True
    # A file this job left behind may not say which video it holds yet, and is then trusted to be this job's own.
    resuming: bool = False
    retry_count: int = 3
    network: NetworkOptions = field(default_factory=NetworkOptions)
    login: LoginSource = field(default_factory=LoginSource)
    max_bytes: Optional[int] = None
    max_job_bytes: Optional[int] = None
    # Empty unless this machine's owner typed extra yt-dlp options; a public visitor never gets any.
    extra_ytdlp: ExtraYtDlpOptions = field(default_factory=ExtraYtDlpOptions)

    # The fields below are set, and limited for public visitors, by options_from_spec. The download steps that act on
    # them belong to the downloader.

    # Saves the video's comments with it: "off", or YouTube's "top" (most liked first) or "new" (newest first) order.
    comments_mode: str = "off"
    # The most comments to save. A public visitor gets at most CLOUD_MAX_COMMENTS.
    comments_max: int = 0
    # Saves a past live stream's chat replay, the "live_chat" track yt-dlp otherwise leaves out of the subtitles.
    # Trusted only: a long chat takes many requests and a long time.
    save_live_chat: bool = False
    # Converts Simplified Chinese subtitles, and the transcript made from them, to Taiwan Traditional Chinese, keeping
    # the original track.
    subtitles_to_traditional: bool = False
    # Splits "Artist - Title" video titles into artist and title tags, and crops audio covers to a square.
    music_tags: bool = False
    # Waits for a scheduled live stream or premiere to begin, then records or downloads it. Trusted only: the wait
    # holds one of the host's few job slots.
    wait_for_live: bool = False
    # Sets each finished file's modified time to the video's upload date instead of the download time.
    mtime_upload_date: bool = False
    # Writes a Kodi-style .nfo file (title, plot, aired date, channel) next to each video for Plex, Jellyfin and Kodi.
    # Trusted only, like the media-server folder layout it goes with.
    write_nfo: bool = False


class YtDlpLogger(Protocol):
    def debug(self, message: str) -> None: ...
    def info(self, message: str) -> None: ...
    def warning(self, message: str) -> None: ...
    def error(self, message: str) -> None: ...


def parse_subtitle_languages(raw: str, allow_patterns: bool = True) -> tuple[str, ...]:
    languages = [part.strip() for part in raw.split(",") if part.strip()]
    if not allow_patterns:
        # yt-dlp treats each entry as a regular expression; a public host only takes codes and the common "zh.*" form.
        languages = [language for language in languages if SAFE_SUBTITLE_LANGUAGE_RE.match(language)]
    if not languages:
        languages = DEFAULT_SUBTITLE_LANGUAGES.split(",")
    return tuple(languages)


def build_output_template(
    naming_rule: str,
    custom_name: str,
    multiple_links: bool,
    folder_rule: str = "none",
    custom_template: str = "",
    has_sections: bool = False,
) -> str:
    folder = FOLDER_TEMPLATES.get(folder_rule, "")
    section_suffix = SECTION_SUFFIX_TEMPLATE if has_sections else ""

    if naming_rule == NAMING_TEMPLATE and custom_template.strip():
        template = custom_template.strip()
        if has_sections and "section_start" not in template and "section_number" not in template:
            # Otherwise every clip gets the same name and only the last one is kept.
            stem = template.removesuffix(".%(ext)s")
            template = f"{stem}{section_suffix}{template[len(stem):]}"
        return template
    if folder_rule == FOLDER_MEDIA_SERVER:
        # Media servers read the date in the name as the episode, so this layout decides the name as well.
        return f"{folder}{MEDIA_SERVER_FILE_TEMPLATE}{section_suffix}.%(ext)s"
    if naming_rule == NAMING_CUSTOM:
        safe_name = re.sub(r"[^\w\-.]+", "_", custom_name.strip()).strip("._") or DEFAULT_CUSTOM_NAME
        return f"{folder}{safe_name}_%(id)s{section_suffix}.%(ext)s"

    # Same-titled videos from different links would overwrite each other, so batch names keep the video ID. A single
    # link gets it only when its plain name is already taken by another video.
    id_suffix = " [%(id)s]" if multiple_links else COLLISION_ID_TEMPLATE
    date_suffix = "_%(upload_date)s" if naming_rule == NAMING_DATED else ""
    return f"{folder}{PLAYLIST_INDEX_TEMPLATE}{TITLE_TEMPLATE}{date_suffix}{id_suffix}{section_suffix}.%(ext)s"


def build_chapter_template(output_template: str) -> str:
    folder = output_template.removesuffix(".%(ext)s").replace(COLLISION_ID_TEMPLATE, "")
    if "%(id)s" not in folder:
        folder += " [%(id)s]"
    return f"{folder}/{CHAPTER_FILE_TEMPLATE}"


def build_base_options(
    logger: YtDlpLogger,
    retry_count: int,
    login: LoginSource,
    network: Optional[NetworkOptions] = None,
    noplaylist: bool = True,
) -> dict[str, Any]:
    network = network or NetworkOptions()
    base_opts: dict[str, Any] = {
        "logger": logger,
        "quiet": True,
        "color": "no_color",
        # Without this, yt-dlp's generic extractor would fetch any URL the server can reach.
        "allowed_extractors": YOUTUBE_EXTRACTORS,
        "noplaylist": noplaylist,
        "retries": retry_count,
        "fragment_retries": retry_count,
        # Retrying at once mostly earns another rejection from YouTube.
        "retry_sleep_functions": {"http": _retry_delay, "fragment": _retry_delay},
        # yt-dlp only looks for Deno unless told otherwise, and many machines already have Node.
        "js_runtimes": {"deno": {}, "node": {}, "bun": {}},
    }
    if login.cookie_file:
        base_opts["cookiefile"] = str(login.cookie_file)
    if login.browser:
        base_opts["cookiesfrombrowser"] = (login.browser,)
    if network.proxy:
        base_opts["proxy"] = network.proxy
    if network.force_ipv4:
        base_opts["source_address"] = "0.0.0.0"
    youtube_args = youtube_extractor_args(network, login)
    if youtube_args:
        base_opts["extractor_args"] = {"youtube": youtube_args}
    return base_opts


def youtube_extractor_args(network: NetworkOptions, login: LoginSource) -> dict[str, list[str]]:
    arguments: dict[str, list[str]] = {}
    clients = [client for client in network.player_client.split(",") if client]
    if login.is_used:
        # yt-dlp drops a client that cannot take cookies, and with no other client asked for it would find no formats.
        clients = [client for client in clients if client not in PLAYER_CLIENTS_WITHOUT_COOKIES]
    if clients:
        arguments["player_client"] = clients
    if network.metadata_language:
        arguments["lang"] = [network.metadata_language]
    return arguments


def merge_extractor_args(*parts: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Combines the extractor arguments several features ask for. A later part wins only for the same argument."""
    merged: dict[str, dict[str, Any]] = {}
    for part in parts:
        for extractor, arguments in part.items():
            # yt-dlp looks extractors up in lower case.
            merged.setdefault(extractor.lower(), {}).update(arguments)
    return merged


def add_extractor_args(ydl_opts: dict[str, Any], extra: Mapping[str, Mapping[str, Any]]) -> None:
    ydl_opts["extractor_args"] = merge_extractor_args(ydl_opts.get("extractor_args") or {}, extra)


def _retry_delay(n: int) -> float:
    # yt-dlp passes the retry number as the keyword argument n.
    return min(2.0**n, 30.0)


def build_format_options(options: DownloadOptions, ffmpeg_available: bool) -> dict[str, Any]:
    """Which streams to fetch. Link inspection uses this too, so its preview matches what will be downloaded."""
    if options.media_type == "none":
        # Sidecar-only downloads have no format to choose, and yt-dlp should not fail over that.
        return {"skip_download": True, "ignore_no_formats_error": True}
    if options.format_override:
        return {"format": options.format_override, "merge_output_format": MERGE_OUTPUT_FORMATS[options.container]}

    language = f"[language^={options.audio_language}]" if SAFE_LANGUAGE_RE.match(options.audio_language) else ""
    if options.media_type == "audio":
        # Without ffmpeg nothing can be converted, so M4A is the most widely playable file left.
        source_filter = AUDIO_SOURCE_FILTERS.get(options.audio_format if ffmpeg_available else "m4a", "")
        candidates = [f"ba{source_filter}{language}", f"ba{language}", "ba", "b"]
        return {"format": "/".join(dict.fromkeys(candidates))}

    resolution = f"res:{options.max_height}" if options.max_height else "res"
    # yt-dlp's own order ranks HDR first, which looks washed out on most screens, so it has to be turned down too.
    format_sort = [resolution, "fps", "hdr:12" if options.prefer_hdr else "hdr:sdr"]
    format_opts: dict[str, Any] = {"format_sort": [*format_sort, *VIDEO_CODEC_SORT[options.video_codec]]}
    if ffmpeg_available:
        audio = f"ba{language}/ba" if language else "ba"
        selectors = [f"bv*+{part}" for part in audio.split("/")]
        format_opts["format"] = "/".join([*selectors, "b"])
        format_opts["merge_output_format"] = MERGE_OUTPUT_FORMATS[options.container]
    else:
        format_opts["format"] = "b[ext=mp4]/b"
    return format_opts


def build_early_postprocessors(options: DownloadOptions, ffmpeg_available: bool) -> list[dict[str, Any]]:
    """Steps that replace or extend the downloaded file, so they run before it gets recorded."""
    steps: list[dict[str, Any]] = []
    if ffmpeg_available and options.thumbnails_as_jpg and writes_thumbnail(options, ffmpeg_available):
        # Runs before anything embeds or records the cover, so both get the JPG.
        steps.append({"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"})
    if options.media_type == "none":
        return steps
    if ffmpeg_available and options.sponsorblock_mode != "off" and options.sponsorblock_categories:
        steps.append({"key": "SponsorBlock", "categories": set(options.sponsorblock_categories), "when": "after_filter"})
    if ffmpeg_available and options.media_type == "audio":
        steps.append(
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": options.audio_format,
                "preferredquality": options.audio_bitrate,
            }
        )
    elif ffmpeg_available and options.container in REMUX_TARGETS and not options.format_override:
        steps.append({"key": "FFmpegVideoRemuxer", "preferedformat": REMUX_TARGETS[options.container]})
    return steps


def expected_final_extension(options: DownloadOptions, ffmpeg_available: bool) -> Optional[str]:
    """The extension a post-processor will give the file, when it differs from what the download starts as."""
    if not ffmpeg_available or options.format_override:
        return None
    if options.media_type == "audio" and options.audio_format != "best":
        return options.audio_format
    if options.media_type == "video" and options.container == "mkv":
        return "mkv"
    return None


def embeds_thumbnail(options: DownloadOptions) -> bool:
    # yt-dlp cannot put a cover into WebM or WAV; it fails with an English error and leaves the image files behind.
    if options.media_type == "audio":
        return options.embed_thumbnail and options.audio_format != "wav"
    return options.embed_thumbnail and options.container != "webm"


def writes_thumbnail(options: DownloadOptions, ffmpeg_available: bool) -> bool:
    return (ffmpeg_available and embeds_thumbnail(options)) or options.write_thumbnail


def embeds_infojson(options: DownloadOptions) -> bool:
    # Only MKV holds attachments. A logged-in download's info.json carries the session cookies.
    return options.embed_infojson and options.media_type == "video" and options.container == "mkv" and not options.login.is_used


def subtitle_download_format(options: DownloadOptions) -> str:
    if options.embed_subtitles and options.media_type == "video" and options.container == "webm":
        # WebM only holds WebVTT subtitles, and yt-dlp skips any other kind with nothing but a warning.
        return "vtt"
    return options.subtitle_format


def build_late_postprocessors(options: DownloadOptions, ffmpeg_available: bool) -> list[dict[str, Any]]:
    """Optional steps on the recorded file, in the order yt-dlp's own command line uses. A failure here keeps the file.
    The owner's own steps are not among them: the downloader adds those last, after its own steps at every stage."""
    if not ffmpeg_available or options.media_type == "none":
        return []

    steps: list[dict[str, Any]] = []
    if options.download_subtitles and options.embed_subtitles and options.media_type == "video":
        # Keep the sidecar files so they stay available as separate downloads.
        steps.append({"key": "FFmpegEmbedSubtitle", "already_have_subtitle": True})

    uses_sponsorblock = options.sponsorblock_mode != "off" and bool(options.sponsorblock_categories)
    if uses_sponsorblock or options.remove_ranges:
        removed = set(options.sponsorblock_categories) if options.sponsorblock_mode == "remove" else set()
        cut: dict[str, Any] = {
            "key": "ModifyChapters",
            "remove_sponsor_segments": removed,
            "force_keyframes": options.precise_cuts,
            # The downloader gives each segment its zh-TW name first; where segments overlap, the shortest one's wins.
            "sponsorblock_chapter_title": SPONSORBLOCK_CHAPTER_TITLE,
        }
        if options.remove_ranges:
            cut["remove_ranges"] = list(options.remove_ranges)
        steps.append(cut)

    add_chapters = options.embed_chapters or uses_sponsorblock
    add_infojson = embeds_infojson(options)
    # Music tags are only of use once written into the file.
    add_metadata = options.embed_metadata or options.music_tags
    if add_metadata or add_chapters or add_infojson:
        steps.append({"key": "FFmpegMetadata", "add_chapters": add_chapters, "add_metadata": add_metadata, "add_infojson": add_infojson})
    if embeds_thumbnail(options):
        steps.append({"key": "EmbedThumbnail", "already_have_thumbnail": options.write_thumbnail})
    if options.split_chapters:
        steps.append({"key": "FFmpegSplitChapters", "force_keyframes": options.precise_cuts})
    return steps


def build_ydl_options(options: DownloadOptions, ffmpeg_available: bool, logger: YtDlpLogger) -> dict[str, Any]:
    ydl_opts = build_base_options(logger, options.retry_count, options.login, options.network, options.noplaylist)
    ydl_opts.update(
        {
            "paths": {"home": str(options.output_dir)},
            "outtmpl": {"default": options.output_template, "chapter": build_chapter_template(options.output_template)},
            # Otherwise re-downloading at another quality silently keeps the old file.
            "overwrites": options.overwrite_existing,
            # A failed subtitle or playlist entry should not abort the remaining downloads.
            "ignoreerrors": True,
            # max_downloads caps the total across a channel's nested tabs. yt-dlp counts every clip as one download.
            "max_downloads": options.playlist_limit * max(len(options.time_ranges), 1),
            # Otherwise a playlist link also leaves description and thumbnail files named after the playlist.
            "allow_playlist_files": False,
            "restrictfilenames": options.restrict_filenames,
            "playlistreverse": options.playlist_reverse,
            # Progress already reaches the app through its hook, and these lines would push everything else out of the log.
            "noprogress": True,
        }
    )
    # Filtered-out videos do not count as downloads, so with filters on more of the list is read than will be kept.
    scan_count = options.playlist_limit
    if has_entry_filters(options.filters):
        scan_count = max(scan_count, min(scan_count * FILTERED_SCAN_FACTOR, PLAYLIST_PICKER_MAX_ITEMS))
    if options.filters.only_ids:
        # Picked videos may have moved down the list since it was shown, so the listing looks well past where they were.
        ydl_opts["playlistend"] = PICKED_VIDEOS_SCAN_ITEMS
    elif options.playlist_items:
        ydl_opts["playlist_items"] = options.playlist_items
    elif options.playlist_reverse:
        # yt-dlp cuts the list before reversing it, so the cut has to be taken from the end.
        ydl_opts["playlist_items"] = f"-{scan_count}:"
    else:
        # Stops listing early, instead of reading a whole channel.
        ydl_opts["playlistend"] = scan_count
    if options.filters.date_after or options.filters.date_before:
        # Gives listed videos an approximate date, so the date filter can skip them without opening each one.
        add_extractor_args(ydl_opts, {"youtubetab": {"approximate_date": [""]}})
    if options.archive_path:
        # yt-dlp never records a sidecar-only download; CollectDownloadedItemsPP does, once a file was really written.
        ydl_opts["download_archive"] = str(options.archive_path)
    if options.network.rate_limit_bytes:
        ydl_opts["ratelimit"] = options.network.rate_limit_bytes

    ydl_opts.update(build_format_options(options, ffmpeg_available))
    # The owner's own steps are not here: the downloader adds those after every step of its own.
    early_steps = build_early_postprocessors(options, ffmpeg_available)
    if early_steps:
        ydl_opts["postprocessors"] = early_steps
    final_extension = expected_final_extension(options, ffmpeg_available)
    if final_extension:
        # Lets yt-dlp recognize an already converted file when resuming.
        ydl_opts["final_ext"] = final_extension

    if ffmpeg_available and options.time_ranges and options.media_type != "none":
        ydl_opts["download_ranges"] = download_range_func(None, list(options.time_ranges))
        ydl_opts["force_keyframes_at_cuts"] = options.precise_cuts

    if options.download_subtitles or options.save_live_chat:
        ydl_opts["writesubtitles"] = True
        # "all" plus auto captions means ~160 machine-translated tracks, which YouTube answers with HTTP 429.
        include_auto = options.include_auto_subtitles and "all" not in options.subtitle_languages
        ydl_opts["writeautomaticsub"] = options.download_subtitles and include_auto
        languages = list(options.subtitle_languages) if options.download_subtitles else []
        # A past live stream lists its chat replay as a subtitle track, and "all" would download it unless asked not to.
        ydl_opts["subtitleslangs"] = [*languages, LIVE_CHAT_TRACK if options.save_live_chat else f"-{LIVE_CHAT_TRACK}"]
        # The chat replay only comes as JSON, which "best" picks.
        ydl_opts["subtitlesformat"] = f"{subtitle_download_format(options)}/best"

    if writes_thumbnail(options, ffmpeg_available):
        ydl_opts["writethumbnail"] = True
    if options.write_description:
        ydl_opts["writedescription"] = True
    if options.write_infojson:
        ydl_opts["writeinfojson"] = True
    if options.comments_mode != "off":
        ydl_opts["getcomments"] = True
        # Without replies, like the comment export on the 下載 page; yt-dlp only reads these as text.
        limit = str(options.comments_max or DEFAULT_COMMENTS_MAX)
        add_extractor_args(ydl_opts, {"youtube": {"comment_sort": [options.comments_mode], "max_comments": [limit, "all", "all", "0"]}})

    # Last, so the owner's options win over the app's choices. APP_MANAGED_YTDLP_KEYS never get here.
    for key, value in options.extra_ytdlp.params.items():
        if key == "extractor_args":
            add_extractor_args(ydl_opts, value)
        else:
            ydl_opts[key] = value
    return ydl_opts


@functools.cache
def _ytdlp_defaults() -> dict[str, Any]:
    return yt_dlp.parse_options([]).ydl_opts


def parse_extra_ytdlp_args(raw: str) -> ExtraYtDlpOptions:
    """Reads yt-dlp command-line options with yt-dlp's own parser. Raises ValueError with a message for the user."""
    if not raw.strip():
        return ExtraYtDlpOptions()
    try:
        arguments = shlex.split(raw)
    except ValueError:
        raise ValueError("進階 yt-dlp 參數的引號沒有成對，請檢查後再試。") from None
    try:
        # yt-dlp reads batch and config files while it parses, and a file named "-" would wait for keyboard input that
        # never comes, so those are refused before it sees them.
        values, leftovers = create_parser().parse_args(arguments)
        if leftovers:
            raise ValueError(f"進階 yt-dlp 參數只能填選項，「{leftovers[0]}」不是選項。要下載的網址請貼到連結欄。")
        if values.batchfile is not None or values.config_locations:
            raise ValueError("進階 yt-dlp 參數不能使用 --batch-file 或 --config-locations，請把選項直接填在這裡。")
        # yt-dlp asks for the password on the keyboard while it parses, and nobody can type it on the server.
        if values.username is not None or values.ap_username is not None:
            raise ValueError("進階 yt-dlp 參數不能使用 --username 或 --ap-username：YouTube 不接受帳號密碼登入。要登入請改用「會員登入」。")
        parsed = yt_dlp.parse_options(arguments).ydl_opts
    except optparse.OptParseError as exc:
        detail = str(exc).strip().splitlines()[-1].split("error:", 1)[-1].strip()
        raise ValueError(f"進階 yt-dlp 參數有誤：{detail}。請對照 yt-dlp 的說明修正，或清空這個欄位。") from None
    except SystemExit:
        # What --help and --version end with, after printing.
        raise ValueError("進階 yt-dlp 參數不能使用 --help 或 --version，請改填下載用的選項。") from None

    defaults = _ytdlp_defaults()
    changed = {key: value for key, value in parsed.items() if key != "postprocessors" and value != defaults.get(key)}
    no_file = [flag for key, flag in NO_FILE_YTDLP_OPTIONS if changed.get(key)]
    if changed.get("simulate"):
        no_file += list(dict.fromkeys(flag for key, flag in PRINT_YTDLP_OPTIONS if changed.get(key))) or ["-s／--simulate"]
    if no_file:
        raise ValueError(
            f"進階 yt-dlp 參數不能使用 {'、'.join(no_file)}：yt-dlp 會只列出資訊或不下載完整的檔案，任務會顯示完成卻沒有檔案。"
            "只要字幕或影片資訊，請把「類型」選成「僅字幕與資訊」。"
        )
    return ExtraYtDlpOptions(
        params={key: value for key, value in changed.items() if key not in APP_MANAGED_YTDLP_KEYS},
        postprocessors=tuple(step for step in parsed["postprocessors"] if step not in defaults["postprocessors"]),
        ignored=tuple(sorted(key for key in changed if key in APP_MANAGED_YTDLP_KEYS)),
    )


def waits_for_live(options: DownloadOptions, url: str) -> bool:
    """Only a link to one video waits: in a list, one upcoming stream would hold up every video after it."""
    return options.wait_for_live and url_identity(url, expand_playlist=not options.noplaylist).startswith("video:")


def has_entry_filters(filters: EntryFilters) -> bool:
    return filters != EntryFilters(title_is_regex=filters.title_is_regex)


def title_matches(pattern: str, title: str, as_regex: bool = True) -> bool:
    if not as_regex:
        return pattern.lower() in title.lower()
    try:
        return re.search(pattern, title, re.IGNORECASE) is not None
    except re.error:
        # Most people type plain keywords, and "C++" is not a valid regular expression.
        return pattern.lower() in title.lower()


def entry_filter_reason(info: Mapping[str, Any], incomplete: bool, filters: EntryFilters) -> Optional[str]:
    """Says why a video does not pass the filters. A playlist listing lacks some fields, so those wait for the full check."""
    title = str(info.get("title") or info.get("id") or "")
    duration = info.get("duration")

    if filters.title_include and title and not title_matches(filters.title_include, title, filters.title_is_regex):
        return f"{title}：標題不符合關鍵字。"
    if filters.title_exclude and title and title_matches(filters.title_exclude, title, filters.title_is_regex):
        return f"{title}：標題包含排除的關鍵字。"
    if duration is not None:
        if filters.min_seconds and duration < filters.min_seconds:
            return f"{title}：片長短於設定的下限。"
        if filters.max_seconds and duration > filters.max_seconds:
            return f"{title}：片長超過設定的上限。"
    views = info.get("view_count")
    if filters.min_views and views is not None and views < filters.min_views:
        return f"{title}：觀看次數低於設定的下限。"
    upload_date = str(info.get("upload_date") or "")
    if upload_date:
        if filters.date_after and upload_date < filters.date_after:
            return f"{title}：上傳日期早於設定的範圍。"
        if filters.date_before and upload_date > filters.date_before:
            return f"{title}：上傳日期晚於設定的範圍。"
    if filters.skip_shorts:
        link = str(info.get("url") or info.get("webpage_url") or "")
        width, height = info.get("width"), info.get("height")
        is_vertical = bool(width and height and height > width)
        if "/shorts/" in link or (is_vertical and duration is not None and duration <= SHORTS_MAX_SECONDS):
            return f"{title}：這是 Shorts。"
    if filters.cc_only and not incomplete and "creative commons" not in str(info.get("license") or "").lower():
        return f"{title}：不是 Creative Commons 授權。"
    return None


def options_from_spec(
    spec: JobSpec,
    limits: AccessLimits,
    cookie_file: Optional[Path] = None,
    archive_path: Optional[Path] = None,
) -> DownloadOptions:
    """Validates a job's settings and turns them into engine options. Raises ValueError with a message for the user."""
    settings = spec.settings
    time_ranges = parse_time_ranges(spec.time_ranges or settings.time_ranges)
    # Picked videos are found by ID, so positions must not narrow the list first.
    playlist_items = "" if spec.video_ids else validate_playlist_items(spec.playlist_items or settings.playlist_items)
    filters = replace(build_entry_filters(settings), title_is_regex=limits.trusted, only_ids=frozenset(spec.video_ids))

    # A visitor on a public host must not steer the server's files or outgoing connections.
    naming_rule = settings.naming_rule
    if naming_rule == NAMING_TEMPLATE and not limits.trusted:
        naming_rule = ""
    custom_template = settings.custom_template if limits.trusted else ""

    media_type = settings.media_type
    if spec.format_override or (spec.time_ranges and media_type == "none"):
        # Someone picked a stream or a chapter of this video, so they want that stream, whatever the sidebar says.
        media_type = "video"

    limit = spec.playlist_limit or settings.playlist_limit
    wait = settings.sleep_seconds if limits.trusted else min(settings.sleep_seconds, CLOUD_MAX_WAIT_SECONDS)
    remove_ranges = parse_remove_ranges(settings.remove_ranges)
    # The owner's options can run commands and write anywhere, so a public visitor's imported settings never use them.
    extra_ytdlp = parse_extra_ytdlp_args(settings.ytdlp_extra_args) if limits.trusted else ExtraYtDlpOptions()
    if cookie_file or spec.browser:
        wait_range = (max(LOGGED_IN_MIN_SLEEP_SECONDS, wait), max(LOGGED_IN_MAX_SLEEP_SECONDS, wait))
    else:
        wait_range = (float(wait), float(wait))
    network = network_options(settings, limits)
    return DownloadOptions(
        output_dir=Path(spec.output_dir),
        output_template=build_output_template(
            naming_rule,
            settings.custom_name,
            spec.multiple_links,
            settings.folder_rule if limits.trusted else "none",
            custom_template,
            has_sections=bool(time_ranges),
        ),
        media_type=media_type,
        max_height=VIDEO_QUALITY_OPTIONS.get(settings.quality),
        video_codec="vp9" if settings.container == "webm" and settings.video_codec == "h264" else settings.video_codec,
        container=settings.container,
        prefer_hdr=settings.prefer_hdr,
        format_override=spec.format_override,
        audio_format=settings.audio_format,
        audio_bitrate="0" if settings.audio_format in FIXED_QUALITY_AUDIO_FORMATS else settings.audio_bitrate,
        audio_language=settings.audio_language.strip(),
        download_subtitles=settings.download_subtitles or settings.write_transcript,
        subtitle_languages=parse_subtitle_languages(settings.subtitle_languages, allow_patterns=limits.trusted),
        subtitle_format=settings.subtitle_format,
        include_auto_subtitles=settings.include_auto_subtitles,
        # A subtitle track keeps the whole video's timing, so inside a clip it would not line up.
        embed_subtitles=settings.embed_subtitles and not time_ranges,
        write_transcript=settings.write_transcript,
        time_ranges=time_ranges,
        # A re-encode cannot be split into stoppable steps, and a shared host cannot spare the time.
        precise_cuts=settings.precise_cuts and limits.trusted,
        # A clip's chapter list would not line up with the clip.
        embed_chapters=settings.embed_chapters and not time_ranges,
        split_chapters=settings.split_chapters and not time_ranges,
        sponsorblock_mode="off" if time_ranges else settings.sponsorblock_mode,
        sponsorblock_categories=sponsorblock_categories(settings),
        # Times in a clip would not line up with the video's own.
        remove_ranges=() if time_ranges else remove_ranges,
        embed_metadata=settings.embed_metadata,
        embed_thumbnail=settings.embed_thumbnail,
        write_thumbnail=settings.write_thumbnail,
        write_description=settings.write_description,
        write_infojson=settings.write_infojson,
        embed_infojson=settings.embed_infojson and not (cookie_file or spec.browser),
        thumbnails_as_jpg=settings.thumbnails_as_jpg,
        playlist_limit=max(1, min(int(limit), limits.max_playlist)),
        playlist_items=playlist_items,
        playlist_reverse=settings.playlist_reverse,
        noplaylist=not settings.expand_playlist,
        filters=filters,
        archive_path=archive_path if limits.trusted and (settings.skip_archived or spec.use_archive) else None,
        # Recording has no size limit, which a shared host cannot afford.
        live_mode=settings.live_mode if limits.trusted else "skip",
        live_max_seconds=settings.live_max_minutes * 60 or None,
        wait_range=wait_range,
        restrict_filenames=settings.restrict_filenames,
        overwrite_existing=settings.overwrite_existing and not spec.resuming,
        resuming=spec.resuming,
        retry_count=settings.retry_count,
        network=network,
        login=LoginSource(cookie_file, spec.browser or None),
        max_bytes=limits.max_file_bytes,
        max_job_bytes=limits.max_job_bytes,
        extra_ytdlp=extra_ytdlp,
        comments_mode=settings.comments_mode,
        comments_max=settings.comments_max if limits.trusted else min(settings.comments_max, CLOUD_MAX_COMMENTS),
        save_live_chat=settings.save_live_chat and limits.trusted,
        subtitles_to_traditional=settings.subtitles_to_traditional,
        music_tags=settings.music_tags,
        wait_for_live=settings.wait_for_live and limits.trusted,
        mtime_upload_date=settings.mtime_upload_date,
        write_nfo=settings.write_nfo and limits.trusted,
    )


def sponsorblock_categories(settings: DownloadSettings) -> tuple[str, ...]:
    if settings.sponsorblock_mode == "remove":
        # Removing offers only what can be removed; a preset or an earlier choice may still hold the others.
        return tuple(name for name in settings.sponsorblock_categories if name not in MARK_ONLY_SPONSORBLOCK_CATEGORIES)
    return tuple(settings.sponsorblock_categories)


def parse_remove_ranges(raw: str) -> tuple[tuple[float, float], ...]:
    try:
        return parse_time_ranges(raw)
    except ValueError as exc:
        raise ValueError(f"移除片段：{exc}") from None


def network_options(settings: DownloadSettings, limits: AccessLimits) -> NetworkOptions:
    # Imported settings files can carry a proxy, and a public visitor must not route the server's connections.
    return NetworkOptions(
        rate_limit_bytes=int(settings.rate_limit_mb * 1024 * 1024) or None,
        sleep_seconds=settings.sleep_seconds,
        proxy=settings.proxy.strip() if limits.trusted else "",
        force_ipv4=settings.force_ipv4,
        # These only change what is asked of YouTube, so public visitors may choose them too.
        player_client="" if settings.player_client == "default" else settings.player_client,
        metadata_language=TRADITIONAL_METADATA_LANGUAGE if settings.prefer_traditional_metadata else "",
    )


def build_entry_filters(settings: DownloadSettings) -> EntryFilters:
    return EntryFilters(
        min_seconds=settings.filter_min_minutes * 60,
        max_seconds=settings.filter_max_minutes * 60,
        date_after=parse_date_filter(settings.filter_date_after),
        date_before=parse_date_filter(settings.filter_date_before),
        min_views=settings.filter_min_views,
        title_include=settings.filter_title_include.strip(),
        title_exclude=settings.filter_title_exclude.strip(),
        skip_shorts=settings.filter_skip_shorts,
        cc_only=settings.filter_cc_only,
    )


def job_url(spec: JobSpec) -> str:
    return apply_channel_tab(spec.url, spec.settings.channel_tab)

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields, replace
from typing import Any, Mapping, Optional

from videocap.config import (
    AUDIO_BITRATE_OPTIONS,
    AUDIO_FORMAT_OPTIONS,
    CHANNEL_TAB_OPTIONS,
    COMMENTS_MODE_OPTIONS,
    CONTAINER_OPTIONS,
    DEFAULT_AUDIO_BITRATE,
    DEFAULT_COMMENTS_MAX,
    DEFAULT_CUSTOM_NAME,
    DEFAULT_PLAYLIST_LIMIT,
    DEFAULT_SPONSORBLOCK_CATEGORIES,
    DEFAULT_SUBTITLE_LANGUAGES,
    DEFAULT_VIDEO_QUALITY,
    FOLDER_RULES,
    LIVE_MODE_OPTIONS,
    LOCAL_MAX_COMMENTS,
    MEDIA_TYPE_OPTIONS,
    NAMING_ORIGINAL,
    NAMING_RULES,
    PLAYER_CLIENT_OPTIONS,
    SPONSORBLOCK_CATEGORIES,
    SPONSORBLOCK_MODES,
    VIDEO_CODEC_OPTIONS,
    VIDEO_QUALITY_OPTIONS,
)


SETTING_KEY_PREFIX = "opt_"


@dataclass
class DownloadSettings:
    """Everything a person chooses before downloading. Holds only JSON values, so presets can be saved as they are."""

    media_type: str = "video"
    quality: str = DEFAULT_VIDEO_QUALITY
    video_codec: str = "h264"
    container: str = "mp4"
    prefer_hdr: bool = False
    audio_format: str = "m4a"
    audio_bitrate: str = DEFAULT_AUDIO_BITRATE
    audio_language: str = ""

    download_subtitles: bool = True
    subtitle_languages: str = DEFAULT_SUBTITLE_LANGUAGES
    subtitle_format: str = "srt"
    include_auto_subtitles: bool = False
    embed_subtitles: bool = True
    write_transcript: bool = False
    subtitles_to_traditional: bool = False

    time_ranges: str = ""
    precise_cuts: bool = False
    embed_chapters: bool = True
    split_chapters: bool = False
    sponsorblock_mode: str = "off"
    sponsorblock_categories: list[str] = field(default_factory=lambda: list(DEFAULT_SPONSORBLOCK_CATEGORIES))
    remove_ranges: str = ""

    embed_metadata: bool = True
    embed_thumbnail: bool = False
    write_thumbnail: bool = False
    write_description: bool = False
    write_infojson: bool = False
    embed_infojson: bool = False
    thumbnails_as_jpg: bool = False
    music_tags: bool = False
    comments_mode: str = "off"
    comments_max: int = DEFAULT_COMMENTS_MAX
    save_live_chat: bool = False

    playlist_limit: int = DEFAULT_PLAYLIST_LIMIT
    playlist_items: str = ""
    playlist_reverse: bool = False
    expand_playlist: bool = False
    channel_tab: str = "all"
    filter_min_minutes: int = 0
    filter_max_minutes: int = 0
    filter_date_after: str = ""
    filter_date_before: str = ""
    filter_min_views: int = 0
    filter_title_include: str = ""
    filter_title_exclude: str = ""
    filter_skip_shorts: bool = False
    filter_cc_only: bool = False
    skip_archived: bool = False
    live_mode: str = "skip"
    live_max_minutes: int = 0
    wait_for_live: bool = False

    naming_rule: str = NAMING_ORIGINAL
    custom_name: str = DEFAULT_CUSTOM_NAME
    custom_template: str = ""
    folder_rule: str = "none"
    restrict_filenames: bool = False
    overwrite_existing: bool = True
    prefer_traditional_metadata: bool = False
    mtime_upload_date: bool = False
    write_nfo: bool = False

    retry_count: int = 3
    rate_limit_mb: float = 0.0
    sleep_seconds: int = 0
    proxy: str = ""
    force_ipv4: bool = False
    player_client: str = "default"
    ytdlp_extra_args: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


SETTING_CHOICES: dict[str, Any] = {
    "media_type": MEDIA_TYPE_OPTIONS,
    "quality": VIDEO_QUALITY_OPTIONS,
    "video_codec": VIDEO_CODEC_OPTIONS,
    "container": CONTAINER_OPTIONS,
    "audio_format": AUDIO_FORMAT_OPTIONS,
    "audio_bitrate": AUDIO_BITRATE_OPTIONS,
    "subtitle_format": ["srt", "vtt"],
    "sponsorblock_mode": SPONSORBLOCK_MODES,
    "channel_tab": CHANNEL_TAB_OPTIONS,
    "live_mode": LIVE_MODE_OPTIONS,
    "naming_rule": NAMING_RULES,
    "folder_rule": FOLDER_RULES,
    "player_client": PLAYER_CLIENT_OPTIONS,
    "comments_mode": COMMENTS_MODE_OPTIONS,
}
SETTING_RANGES: dict[str, tuple[float, float]] = {
    "playlist_limit": (1, 100000),
    "filter_min_minutes": (0, 100000),
    "filter_max_minutes": (0, 100000),
    "filter_min_views": (0, 10**12),
    "retry_count": (1, 10),
    "rate_limit_mb": (0, 10000),
    "live_max_minutes": (0, 100000),
    "sleep_seconds": (0, 3600),
    "comments_max": (1, LOCAL_MAX_COMMENTS),
}
DEFAULT_SETTINGS = DownloadSettings()


def settings_from_dict(data: Mapping[str, Any]) -> DownloadSettings:
    """Builds settings from a saved or uploaded preset. Whatever is unknown or invalid falls back to its default."""
    clean: dict[str, Any] = {}
    for spec in fields(DownloadSettings):
        if spec.name not in data:
            continue
        default = getattr(DEFAULT_SETTINGS, spec.name)
        value = data[spec.name]

        if spec.name == "sponsorblock_categories":
            if isinstance(value, (list, tuple)):
                clean[spec.name] = [item for item in value if isinstance(item, str) and item in SPONSORBLOCK_CATEGORIES]
            continue
        # bool is checked first because it is also an int.
        if isinstance(default, bool):
            if isinstance(value, bool):
                clean[spec.name] = value
            continue
        if isinstance(default, (int, float)):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            low, high = SETTING_RANGES[spec.name]
            if low <= value <= high:
                clean[spec.name] = type(default)(value)
            continue
        if not isinstance(value, str):
            continue
        choices = SETTING_CHOICES.get(spec.name)
        if choices is None or value in choices:
            clean[spec.name] = value
    return DownloadSettings(**clean)


BUILTIN_PRESETS: dict[str, dict[str, Any]] = {
    "預設（1080p MP4＋字幕）": {},
    "最高畫質": {"quality": "最佳畫質", "video_codec": "best", "container": "mkv"},
    "手機／電視相容（720p H.264）": {"quality": "720p", "video_codec": "h264", "container": "mp4"},
    "省空間（480p）": {"quality": "480p", "video_codec": "h264", "download_subtitles": False},
    "音樂（MP3 320k＋封面）": {
        "media_type": "audio",
        "audio_format": "mp3",
        "audio_bitrate": "320",
        "embed_thumbnail": True,
        "music_tags": True,
        "download_subtitles": False,
    },
    "Podcast（M4A，移除贊助）": {
        "media_type": "audio",
        "audio_format": "m4a",
        "sponsorblock_mode": "remove",
        "download_subtitles": False,
    },
    "學習筆記（720p＋字幕＋逐字稿）": {
        "quality": "720p",
        "include_auto_subtitles": True,
        "write_transcript": True,
        "write_description": True,
    },
    "僅逐字稿": {
        "media_type": "none",
        "include_auto_subtitles": True,
        "write_transcript": True,
    },
    "封存（最高畫質＋全部附加檔）": {
        "quality": "最佳畫質",
        "video_codec": "best",
        "container": "mkv",
        "write_thumbnail": True,
        "write_description": True,
        "write_infojson": True,
        "include_auto_subtitles": True,
    },
}


@dataclass
class JobSpec:
    """One link to download and how. Retrying, resuming after a restart, and subscriptions all rebuild a job from this."""

    url: str
    settings: DownloadSettings
    output_dir: str
    multiple_links: bool = False
    format_override: str = ""
    playlist_items: str = ""
    playlist_limit: Optional[int] = None
    time_ranges: str = ""
    use_archive: bool = False
    # Keeps finished files and partial downloads, so a retry picks up where the last attempt stopped.
    resuming: bool = False
    browser: str = ""
    needs_cookie_file: bool = False
    subscription_id: Optional[int] = None
    # Videos picked from a list. Unlike positions, these still name the same videos after the list changes.
    video_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "settings": self.settings.to_dict()}

    def as_resuming(self) -> JobSpec:
        return replace(self, resuming=True)


def job_spec_from_dict(data: Mapping[str, Any]) -> JobSpec:
    known = {spec.name for spec in fields(JobSpec)}
    values = {key: value for key, value in data.items() if key in known and key != "settings"}
    return JobSpec(settings=settings_from_dict(data.get("settings") or {}), **values)

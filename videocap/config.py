from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Optional


APP_NAME = "YouTube影片下載平台"
APP_VERSION = "v2026.11.07.1"
APP_UPDATED_AT = "2026-11-07"
DEPLOY_VERIFY_MIN_COMMIT = "7f71b98"
DEPLOY_MARKER = "build-2026-11-07-01-youtube"
LOCAL_RUN_GUIDE_URL = "https://github.com/jackalcn/YouTubeVideoCap#在本機執行"

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
# yt-dlp names two of its YouTube extractors without the "youtube:" prefix: youtu.be links that carry list=, and live embeds.
YOUTUBE_EXTRACTORS = ["youtube", "youtube:.*", "youtubeytbe", "youtubelivestreamembed"]

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
VIDEO_CODEC_OPTIONS = {
    "h264": "相容性優先（H.264）",
    "best": "畫質優先（AV1／VP9）",
    "vp9": "VP9",
    "av1": "AV1",
}
CONTAINER_OPTIONS = ["mp4", "mkv", "webm"]
# "best" keeps whatever YouTube serves, without converting.
AUDIO_FORMAT_OPTIONS = {
    "m4a": "M4A（AAC）",
    "mp3": "MP3",
    "opus": "Opus",
    "flac": "FLAC",
    "wav": "WAV",
    "best": "原始音訊（不轉檔）",
}
AUDIO_BITRATE_OPTIONS = ["128", "192", "256", "320"]
DEFAULT_AUDIO_BITRATE = "192"
MEDIA_TYPE_OPTIONS = {"video": "影片", "audio": "僅音訊", "none": "僅字幕與資訊"}
# Explicit codes, because with auto captions "zh.*" also matches tracks like zh-Hans-en, adding requests YouTube rejects.
DEFAULT_SUBTITLE_LANGUAGES = "zh-TW,zh-Hant,zh-HK,zh-CN,zh-Hans,zh,en,en-US,en-GB"
LIVE_CHAT_TRACK = "live_chat"
DEFAULT_CUSTOM_NAME = "youtube_video"

NAMING_ORIGINAL = "沿用原始檔名"
NAMING_DATED = "原始檔名加日期"
NAMING_CUSTOM = "自訂名稱"
NAMING_TEMPLATE = "自訂 yt-dlp 範本"
NAMING_RULES = [NAMING_ORIGINAL, NAMING_DATED, NAMING_CUSTOM, NAMING_TEMPLATE]
FOLDER_MEDIA_SERVER = "media_server"
FOLDER_RULES = {
    "none": "不分資料夾",
    "channel": "依頻道",
    "playlist": "依播放清單",
    "month": "依上傳年月",
    FOLDER_MEDIA_SERVER: "媒體伺服器（Plex／Jellyfin）",
}
# Cut short like titles: Windows refuses paths over 260 characters unless long paths are turned on.
FOLDER_TEMPLATES = {
    "none": "",
    "channel": "%(channel,uploader|未知頻道).80B/",
    "playlist": "%(playlist_title,playlist|未分類).80B/",
    "month": "%(upload_date>%Y-%m|未知日期)s/",
    FOLDER_MEDIA_SERVER: "%(channel,uploader|未知頻道).80B/Season %(upload_date>%Y|0)s/",
}
# Plex and Jellyfin read a channel as a show, a year as a season and a dated name as an episode, so this layout names
# the file too. The ID keeps same-titled videos of one day apart.
MEDIA_SERVER_FILE_TEMPLATE = "%(upload_date>%Y-%m-%d|未知日期)s %(title).150B [%(id)s]"
CHANNEL_TAB_OPTIONS = {"all": "全部分頁", "videos": "影片", "shorts": "Shorts", "streams": "直播"}
LIVE_MODE_OPTIONS = {"skip": "略過直播", "record": "從現在開始錄製"}
# The queue page tells a recording from a download by this, because a recording can only be stopped, not paused.
RECORDING_STATUS_PREFIX = "錄製中"
LIVE_MIN_FREE_BYTES = 1024**3
# How long yt-dlp sleeps between looks at a stream that has not started: never more than this, and at least this,
# even once the planned time has passed and the stream is late.
LIVE_WAIT_RETRY_SECONDS = (30, 600)
# Streams and premieres get postponed or cancelled, and a waiting job holds a download slot.
LIVE_WAIT_MAX_SECONDS = 24 * 60 * 60
LIVE_WAIT_STATUS_PREFIX = "等待開播"
SPONSORBLOCK_MODES = {"off": "不使用", "mark": "標記為章節", "remove": "直接移除"}
SPONSORBLOCK_CATEGORIES = {
    "sponsor": "贊助廣告",
    "selfpromo": "自我宣傳",
    "interaction": "訂閱提醒",
    "intro": "片頭",
    "outro": "片尾",
    "preview": "預告回顧",
    "music_offtopic": "非音樂片段",
    "filler": "離題內容",
    "hook": "開場鋪陳與問候",
    # These two only mark a spot, so they can be marked as chapters but never removed.
    "chapter": "社群章節（影片沒有章節時補上）",
    "poi_highlight": "精華起點",
}
DEFAULT_SPONSORBLOCK_CATEGORIES = ["sponsor", "selfpromo", "interaction"]
# Marked segments become chapters named like 「[贊助片段] 贊助廣告」. A highlight is a spot, not a segment to skip.
SPONSORBLOCK_CHAPTER_PREFIX = "[贊助片段]"
SPONSORBLOCK_UNPREFIXED_CATEGORIES = frozenset({"poi_highlight"})
# Clients yt-dlp's README lists that YouTube serves without a PO token. "default" leaves the choice to yt-dlp.
PLAYER_CLIENT_OPTIONS = {
    "default": "預設（自動選擇）",
    "tv_samsung": "電視版（tv_samsung）",
    "tv": "電視版（tv）",
    "tv_downgraded": "舊版電視（tv_downgraded）",
    "visionos": "Apple Vision 版（visionos，不需 Deno）",
    "web_embedded": "內嵌播放器版（web_embedded，限可嵌入的影片）",
}
# yt-dlp drops these when cookies are used, and with nothing else requested the download would fail.
PLAYER_CLIENTS_WITHOUT_COOKIES = frozenset({"visionos"})
# Tried once more for videos YouTube refused with HTTP 403 under yt-dlp's default clients (tv_samsung, visionos,
# web). yt-dlp's README and client table list these as needing no PO token, and all of them take cookies. yt-dlp
# skips a client YouTube turns away ("The page needs to be reloaded", which tv and tv_downgraded got in November
# 2026), so the one that still works comes first.
FALLBACK_PLAYER_CLIENTS = ("web_embedded", "tv", "tv_downgraded")
# One of the language codes YouTube's own interface offers.
TRADITIONAL_METADATA_LANGUAGE = "zh-TW"
COMMENTS_MODE_OPTIONS = {"off": "不另存", "top": "熱門留言優先", "new": "最新留言優先"}
DEFAULT_COMMENTS_MAX = 100
LOCAL_MAX_COMMENTS = 100_000

# Titles can reach 300 UTF-8 bytes, past the 255-byte filename limit of most filesystems.
TITLE_TEMPLATE = "%(title).150B"
PLAYLIST_INDEX_TEMPLATE = "%(playlist_index&{:02d} - |)s"
# Empty unless the plain name already holds a different video; the download then sets this field to " [ID]".
COLLISION_ID_FIELD = "videocap_id_suffix"
COLLISION_ID_TEMPLATE = f"%({COLLISION_ID_FIELD}|)s"
# Chapter files go into a folder named like the video's own file, plus its ID so same-titled videos stay apart.
CHAPTER_FILE_TEMPLATE = "%(section_number)02d - %(section_title).100B.%(ext)s"
# Each clip of one video becomes its own file, so the name has to tell them apart.
SECTION_SUFFIX_TEMPLATE = " [%(section_start|0).0f 秒起]"

PLAYLIST_PICKER_MAX_ITEMS = 500
DEFAULT_PLAYLIST_LIMIT = 20
LOCAL_MAX_PLAYLIST_LIMIT = 200
SEARCH_MAX_RESULTS = 50
COMMENT_EXPORT_MAX = 500

# Streamlit serves downloads from memory, and a cloud host is shared by every visitor.
CLOUD_MAX_PLAYLIST_LIMIT = 20
CLOUD_MAX_BYTES = 1024**3
CLOUD_MAX_JOB_BYTES = 2 * 1024**3
CLOUD_RETENTION_SECONDS = 2 * 60 * 60
# Finished files wait up to CLOUD_RETENTION_SECONDS for their visitor, so without a cap a few visitors could fill the disk.
CLOUD_MAX_STORED_BYTES_PER_VISITOR = CLOUD_MAX_JOB_BYTES + CLOUD_MAX_BYTES
# Room for the jobs already running, which only check the free space before each video.
CLOUD_MIN_FREE_BYTES = CLOUD_MAX_JOB_BYTES
CLOUD_OUTPUT_ROOT = Path(tempfile.gettempdir()) / "youtube-downloader-files"
CLOUD_MAX_CONCURRENT_JOBS = 2
CLOUD_MAX_CONCURRENT_PER_VISITOR = 1
CLOUD_MAX_PENDING_PER_VISITOR = 5
# Every new browser session is a new visitor, so these bound what one person's many sessions can hold together:
# all of them coming from one network address, and everyone at once.
CLOUD_MAX_PENDING_PER_ADDRESS = 2 * CLOUD_MAX_PENDING_PER_VISITOR
CLOUD_MAX_PENDING_TOTAL = 20
CLOUD_MAX_INSPECT_LINKS = 10
# Waiting between videos holds one of the host's few download slots, so a public visitor may only wait briefly.
CLOUD_MAX_WAIT_SECONDS = 10
# The same cap as the comment export on the 下載 page.
CLOUD_MAX_COMMENTS = COMMENT_EXPORT_MAX
# Long enough for any download within the size limits; a job running longer is holding a slot for nothing.
CLOUD_MAX_JOB_SECONDS = 30 * 60
# Closed pages stop polling, and nobody will ever collect what their jobs produce.
CLOUD_ABANDON_AFTER_SECONDS = 5 * 60

LOCAL_DEFAULT_CONCURRENT_JOBS = 2
LOCAL_MAX_CONCURRENT_JOBS = 5
JOB_LOG_LINES = 300
# Per visitor, so one visitor's many jobs cannot push out, and delete, another visitor's results.
MAX_FINISHED_JOBS_KEPT = 100
LOCAL_MAX_INSPECT_LINKS = 100
# After HTTP 429 or YouTube's check for bots, the owner's queue rests this long: more requests only prolong the limit.
RATE_LIMIT_PAUSE_SECONDS = 15 * 60
# A download of the owner's that lost its connection is retried by itself this many times, after these waits.
AUTO_RETRY_LIMIT = 3
AUTO_RETRY_DELAYS_SECONDS = (120, 300, 600)

# st.video loads the whole file into memory before serving it.
PREVIEW_MAX_BYTES = 200 * 1024**2
LOW_DISK_WARNING_BYTES = 2 * 1024**3
# Below this the owner is warned, not stopped, when queueing a download: the videos may well be small. The same mark
# as the 系統 page's warning, so the two pages agree.
LOCAL_MIN_FREE_BYTES = LOW_DISK_WARNING_BYTES
# yt-dlp ships fixes for YouTube changes often, so an old copy is the first thing to suspect.
YT_DLP_STALE_AFTER_DAYS = 60
# How long the 系統 page trusts its last answer from GitHub about a newer app, and so how often it asks by itself.
APP_UPDATE_CHECK_SECONDS = 24 * 60 * 60

SUBSCRIPTION_CHECK_SECONDS = 60
SUBSCRIPTION_INTERVALS = {0: "僅手動檢查", 1: "每小時", 6: "每 6 小時", 12: "每 12 小時", 24: "每天", 168: "每週"}

LOGIN_NONE = "不登入"
LOGIN_BROWSER = "使用瀏覽器的登入狀態"
LOGIN_COOKIES = "上傳 cookies.txt"
LOGIN_BROWSERS = {
    "firefox": "Firefox",
    "chrome": "Chrome",
    "edge": "Edge",
    "brave": "Brave",
    "opera": "Opera",
    "vivaldi": "Vivaldi",
    "safari": "Safari",
}
# yt-dlp's wiki warns that using an account can get it banned, and suggests 5-10 seconds between downloads.
LOGGED_IN_MIN_SLEEP_SECONDS = 5
LOGGED_IN_MAX_SLEEP_SECONDS = 10
NO_LOGIN_COOKIES_MESSAGE = "沒有讀到 YouTube 的登入資訊。請確認這個瀏覽器已登入 YouTube，或重新匯出 cookies.txt。"
EXPIRED_LOGIN_MESSAGE = "YouTube 帳號的登入已失效，請重新在瀏覽器登入，或重新匯出 cookies.txt。"
COOKIE_EXPORT_STEPS = """
1. 開啟瀏覽器的無痕／私密視窗，登入已加入會員的 YouTube 帳號。
2. 在同一個分頁的網址列輸入 youtube.com/robots.txt。
3. 用擴充功能匯出 youtube.com 的 cookies：Chrome 用「Get cookies.txt LOCALLY」，Firefox 用「cookies.txt」。需要先在擴充功能設定允許無痕模式。
4. 關閉無痕視窗，之後不要再開啟這個工作階段，否則 cookies 可能失效。

請注意不要裝到名稱相近的「Get cookies.txt」，它曾被回報為惡意軟體。
"""


def get_data_dir() -> Path:
    configured = os.getenv("VIDEOCAP_DATA_DIR", "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".youtube-videocap"


def get_default_output_dir() -> Path:
    configured = os.getenv("VIDEOCAP_OUTPUT_DIR", "").strip()
    return Path(configured).expanduser() if configured else Path.cwd() / "downloads"

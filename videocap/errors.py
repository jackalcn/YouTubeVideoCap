from __future__ import annotations

import functools
import re
from typing import Iterable, NamedTuple, Optional

from videocap.sites import SITE_NAMES, extractor_for, host_of, is_youtube_url, site_label


JS_RUNTIME_HINT = (
    "YouTube 需要 JavaScript 執行環境（Deno）才能提供完整的畫質，這台電腦目前沒有可用的。"
    "在自己的電腦上執行時，請到「系統」頁按「安裝 Deno」。"
)
# yt-dlp only warns about these, and the failure they cause comes later in other words.
JS_RUNTIME_MARKERS = ("no supported javascript runtime", "js runtime", "n challenge solving failed", "signature solving failed")
# What a missing JavaScript runtime leads to: YouTube then offers few formats, or only ones that refuse to download.
MISSING_FORMAT_MARKERS = ("requested format is not available", "no video formats found", "http error 403")
BOT_CHECK_HINT = (
    "YouTube 要求驗證身分（判定為自動化流量），雲端主機和 VPN 的網路常遇到。請在「會員登入」登入帳號後再試；"
    "如果是在雲端主機上使用，建議改在自己的電腦上執行。"
)
RATE_LIMIT_HINT = "YouTube 暫時限制了請求頻率，請稍後再試（可能長達一小時）。"
CONNECTION_LOST_HINT = "下載途中網路連線中斷，請重新下載。"
UNREACHABLE_HINT = "連不上 YouTube，請確認網路或代理伺服器設定。"
# Named, because the queue acts on these by itself: it pauses after the first two and retries after the last two.
BOT_CHECK = "bot_check"
RATE_LIMIT = "rate_limit"
# A site that wants a person to pass its check, which the app can say only after it has tried as a browser.
HUMAN_CHECK = "human_check"
CONNECTION_LOST = "connection_lost"
UNREACHABLE = "unreachable"

# What another site's hints call it when it has no name of its own here, as for a direct link or an ordinary page.
UNNAMED_SITE = "這個網站"
# Where another site's hints send a person who must log in.
OTHER_SITE_LOGIN = "請在左側「會員登入」選擇已登入{site}的瀏覽器，或上傳 cookies.txt"
# yt-dlp starts an extractor's message with the extractor's name: "[vimeo] 76979871: ...", "[generic] clip: ...".
LEADING_TAG_RE = re.compile(r"\[([^\]]+)\]")
YOUTUBE_PREFIX = "youtube"
# The two messages that name no extractor, only the link nothing could read.
UNREADABLE_LINK_RE = re.compile(r"(?:unsupported url: |found for url )(\S+)")
# A site's name in a hint already given, which is short and holds no sentence's punctuation.
SITE_IN_HINT = r"[^，。：\n]{1,40}?"
# Chinese characters, which a Latin name is set off from by spaces.
CJK_RE = re.compile(r"[\u3400-\u9fff]")

Marker = str | re.Pattern[str]


class Hint(NamedTuple):
    """One kind of failure: what finds it in yt-dlp's message, and what the person is told."""

    kind: str
    markers: tuple[Marker, ...]
    # What a YouTube link is told, and a message that names no site; None for a failure only other sites give.
    youtube: Optional[str]
    # What another site's link is told, with {site} for the site's name; None to tell it the same as YouTube.
    other: Optional[str] = None
    # A failure, or advice, that exists only on YouTube: another site's message is left to a later hint.
    youtube_only: bool = False

    def text_for(self, site: Optional[str]) -> Optional[str]:
        """The hint for a message about this site, None meaning YouTube; None when this hint is not for that site."""
        if site is None:
            return self.youtube
        if self.youtube_only:
            return None
        template = self.other if self.other is not None else self.youtube
        return with_site(template, site) if template else None

    def begins(self, message: str) -> bool:
        """Whether humanize_ytdlp_error gave this message this hint, for whichever site."""
        if self.youtube and message.startswith(self.youtube):
            return True
        return bool(self.other and given_hint_pattern(self.other).match(message))


# Checked in order, against the lowercased message; a marker is a substring or a pattern.
YTDLP_ERROR_HINTS: tuple[Hint, ...] = (
    # File problems come first: their messages quote the file's path, and the video title in it could match a hint below.
    Hint("disk_full", ("no space left", "errno 28", "winerror 112"), "下載資料夾所在的硬碟空間不足，請清出空間後重試。"),
    Hint("file_in_use", ("being used by another process", "winerror 32"), "檔案正被其他程式使用（例如播放器），請關閉後重試。"),
    Hint(
        "no_permission",
        (re.compile(r"winerror 5\b"), re.compile(r"errno 13\b"), "permission denied"),
        "沒有權限存取檔案：檔案可能正被其他程式（例如播放器）開啟，或資料夾不允許寫入。請關閉相關程式後重試，或改用其他下載資料夾。",
    ),
    Hint(
        "path_too_long",
        (
            "file name too long",
            "errno 36",
            "winerror 206",
            # Windows without long paths turned on calls a path past 260 characters missing instead.
            re.compile(r"(?:errno 2\b|winerror 3\b|no such file or directory).*?['\"][^'\"]{240,}"),
        ),
        "檔名或路徑太長，請改用較短的下載資料夾或「自訂名稱」。",
    ),
    Hint(
        "no_ffmpeg",
        ("ffmpeg not found", "ffprobe not found", "ffmpeg is not installed", "ffmpeg could not be found"),
        "這項功能需要 ffmpeg，請先安裝後重新啟動 App。",
    ),
    # Also a post-processing error, but a network one: ffmpeg is not involved.
    Hint(
        "sponsorblock",
        ("communicate with sponsorblock",),
        "連不上 SponsorBlock 服務，請稍後再試，或在側邊欄把「SponsorBlock」改成「不使用」。",
    ),
    Hint("postprocessing", ("conversion failed", "postprocessing:"), "ffmpeg 後處理失敗（合併影音或嵌入字幕）。"),
    # yt-dlp's own lists of sites it refuses. Their words also say "will NOT be supported" and "DRM protected".
    Hint("drm_site", ("known to use drm protection",), "這個網站的影片都受 DRM 版權保護，無法下載。"),
    Hint("refused_site", ("primarily used for piracy", "this website is not supported"), "yt-dlp 不支援這個網站，所以無法下載。"),
    # Before the 403 below: Cloudflare's check is an HTTP 403 too. The app leaves out what impersonation needs, which
    # once broke subtitle downloads on Windows, and YouTube's subtitles ask for it without needing it.
    Hint(
        "impersonation",
        ("impersonat",),
        None,
        "{site}需要模擬瀏覽器（impersonation）才能下載，目前這個 App 沒有這項功能，所以無法下載這個網站的影片。",
    ),
    # Found in no message: yt-dlp words this refusal as any other HTTP 403. The try that passed for a browser saw
    # the site answer with its check all the same, and humanize_human_check says so.
    Hint(
        HUMAN_CHECK,
        (),
        None,
        "{site}要求真人驗證（例如勾選「我不是機器人」），用模擬瀏覽器連線也過不了。App 不會代為通過驗證，所以無法下載這個網站的影片。",
    ),
    Hint(
        BOT_CHECK,
        ("not a bot",),
        BOT_CHECK_HINT,
        "{site}要求驗證身分（判定為自動化流量）。" + OTHER_SITE_LOGIN + " 後再試。",
    ),
    Hint(
        "ip_blocked",
        ("your ip is likely being blocked",),
        "YouTube 封鎖了這台機器的網路位址（IP），雲端主機和 VPN 常遇到。請換個網路再試；如果是在雲端主機上使用，建議改在自己的電腦上執行。",
        "{site}封鎖了這台機器的網路位址（IP），請換個網路再試。",
    ),
    Hint(
        "age_restricted",
        ("confirm your age", "inappropriate for some users"),
        "這是年齡限制影片，請在「會員登入」登入 YouTube 帳號。",
        "這是年齡限制影片，" + OTHER_SITE_LOGIN + "。",
    ),
    Hint("private", ("private video",), "這是私人影片，請在「會員登入」用有觀看權限的帳號登入。"),
    Hint(
        "members_only",
        ("members-only", "join this channel"),
        "這是頻道會員專屬影片，請在「會員登入」用已加入此頻道會員的帳號登入。如果已經登入，代表這個帳號的會員等級不足，或登入已失效。",
        "這部影片是{site}的會員專屬內容，需要有權限的帳號：" + OTHER_SITE_LOGIN + "。",
    ),
    Hint(
        "premium",
        ("music premium", "premium members"),
        "這是 YouTube Premium 專屬內容，需要用有訂閱的帳號在「會員登入」登入。",
        "這部影片是{site}的付費會員專屬內容，需要有訂閱的帳號：" + OTHER_SITE_LOGIN + "。",
    ),
    Hint("geo_restricted", ("available in your country", "geo restrict", "geo-restrict", "from your location"), "此影片在目前所在地區無法觀看。"),
    Hint(
        "client_refused",
        ("page needs to be reloaded", "必須重新載入頁面"),
        "這個 YouTube 用戶端目前無法使用（YouTube 改了它的驗證方式）。請在側邊欄把「YouTube 用戶端」改回「預設」，或換成 web_embedded。",
        youtube_only=True,
    ),
    Hint("drm", ("drm protected",), "此影片受 DRM 保護，無法下載。"),
    Hint(
        RATE_LIMIT,
        ("rate-limited", "http error 429", "too many requests", "try again later"),
        RATE_LIMIT_HINT,
        "{site}暫時限制了請求頻率，請稍後再試。",
    ),
    Hint(
        "http_403",
        ("http error 403",),
        "YouTube 拒絕了下載請求。常見原因是這台機器的網路位址被限制（雲端主機、VPN 常見），或 yt-dlp 版本太舊。"
        "可以在「會員登入」登入帳號後再試；在自己的電腦上執行時，也可以到「系統」頁按「檢查新版本」更新 yt-dlp。",
        "{site}拒絕了下載請求（HTTP 403）。可能是需要登入（" + OTHER_SITE_LOGIN + "），"
        "連結已過期（請回到影片網頁重新複製連結），或這個網站不允許下載。",
    ),
    # YouTube's own 404s are the channel's, below; one while downloading a stream says nothing useful.
    Hint(
        "http_404",
        ("http error 404",),
        None,
        "找不到這部影片（HTTP 404）：連結可能已失效或打錯了。請回到影片所在的網頁，重新複製網址。",
    ),
    Hint(
        "range_refused",
        ("http error 416", "requested range not satisfiable"),
        "續傳失敗：這部影片的暫存檔已經對不上。請到下載資料夾刪除對應的 .part 檔後重新下載。",
    ),
    Hint(
        "unavailable",
        ("video unavailable", "video is unavailable", "no longer available", "has been removed", "has been terminated"),
        "影片無法使用，可能已被移除、設為不公開，或在主機所在地區無法觀看。",
    ),
    # After every hint above that names what the login is for: yt-dlp adds its advice about cookies to all of those.
    Hint(
        "login",
        (
            "for the authentication",
            "to provide account credentials",
            "login required",
            "requires login",
            "only available for registered users",
            "need to log in",
            "need to login",
            "must be logged in",
        ),
        "這部影片需要登入 YouTube 才能觀看，請在左側「會員登入」選擇已登入 YouTube 的瀏覽器，或上傳 cookies.txt。",
        "{site}需要登入才能下載這部影片，" + OTHER_SITE_LOGIN + "。",
    ),
    # yt-dlp names the list it was reading; these are the account's own lists, which exist only for a logged-in viewer.
    Hint(
        "personal_list",
        (re.compile(r"\[youtube:tab\] (?:wl|ll|lm):"),),
        "「稍後觀看」和「喜歡的影片」是個人清單，需要先在「會員登入」登入帳號才能下載。",
    ),
    Hint("no_channel_or_list", ("channel/playlist does not exist",), "找不到這個頻道或播放清單，請確認網址是否正確。"),
    Hint(
        "no_channel",
        ("this channel does not exist", re.compile(r"\[youtube:tab\].*http error 404")),
        "找不到這個頻道，請確認網址或 @頻道代號 是否正確。",
        "找不到這個頻道，請確認網址是否正確。",
    ),
    Hint("no_playlist", ("playlist does not exist", "playlist is private"), "找不到這個播放清單，可能已被刪除或設為不公開。"),
    Hint("upcoming", ("will begin in", "premieres in"), "直播或首播尚未開始，開始後才能下載。"),
    Hint("not_live", ("not currently live",), "這個頻道目前沒有在直播。"),
    Hint("format_unavailable", ("requested format is not available",), "找不到符合條件的影片格式，請改選其他畫質。"),
    Hint(
        "no_formats",
        ("no video formats found",),
        "YouTube 沒有提供這部影片任何可下載的格式，常見原因是 yt-dlp 版本太舊，或 YouTube 暫時限制了這台機器。請稍後再試。",
        "{site}沒有提供這部影片任何可下載的格式。可能需要登入（左側「會員登入」），或這部影片無法下載。",
    ),
    Hint("js_runtime", JS_RUNTIME_MARKERS, JS_RUNTIME_HINT, youtube_only=True),
    Hint(
        "cookies_locked",
        ("could not copy",),
        "讀不到瀏覽器的 cookies：這個瀏覽器執行時會鎖定資料。請完全關閉瀏覽器後再試，或改用 Firefox、cookies.txt。",
    ),
    Hint(
        "cookies_encrypted",
        ("failed to decrypt", "dpapi"),
        "無法解密這個瀏覽器的 cookies，新版 Chrome、Edge 在 Windows 上常有這個問題。請改用 Firefox 或 cookies.txt。",
    ),
    Hint(
        "no_cookies",
        ("cookies database",),
        "找不到這個瀏覽器的 cookies，請確認已安裝這個瀏覽器，並在裡面登入 YouTube。",
        "找不到這個瀏覽器的 cookies，請確認已安裝這個瀏覽器，並在裡面登入{site}。",
    ),
    Hint("bad_cookies_file", ("netscape format", "failed to load cookies"), "cookies.txt 無法讀取，請確認是 Netscape 格式。"),
    # For a channel's home page the tab comes from the sidebar setting, not from the pasted link.
    Hint(
        "no_channel_tab",
        ("does not have a",),
        "這個頻道沒有這個分頁（例如沒有直播或 Shorts）。貼的是頻道首頁時，請到側邊欄「播放清單與頻道」把「頻道內容」改成其他分頁或「全部分頁」；"
        "貼的是分頁網址時，請改貼頻道首頁。",
        youtube_only=True,
    ),
    Hint("truncated_link", ("looks truncated",), "網址不完整，請重新複製完整網址。"),
    Hint(
        "unreadable_link",
        ("no suitable extractor", "unsupported url"),
        "無法辨識這個網址，請從瀏覽器網址列或 YouTube 的「分享」重新複製完整連結。",
        "這個網址裡找不到可以下載的影片：yt-dlp 不支援這個網站，或這個網頁裡沒有影片。請確認貼的是影片所在網頁的完整網址。",
    ),
    Hint("bad_subtitle_languages", ("wrong regex for subtitlelangs",), "字幕語言代碼不是有效的正規表示式，請修改後再試。"),
    # Windows words these in the system's language, so only the error number can be matched.
    Hint(
        CONNECTION_LOST,
        ("winerror 10054", "connection reset", "did not get any data blocks", "content too short", re.compile(r"fragment \d+ not found")),
        CONNECTION_LOST_HINT,
    ),
    Hint(
        UNREACHABLE,
        (
            "name resolution",
            "getaddrinfo",
            "network is unreachable",
            "connection refused",
            "timed out",
            "unable to connect",
            "winerror 10060",
            "winerror 10061",
            # Python's own wrapper for any request that never reached the server, such as through a proxy that is down.
            "urlopen error",
        ),
        UNREACHABLE_HINT,
        "連不上{site}，請確認網址是否正確，以及網路或代理伺服器設定。",
    ),
    # Last, because yt-dlp adds this sentence to any error it did not expect, which is usually a change on the site.
    Hint(
        "site_changed",
        ("confirm you are on the latest version",),
        "yt-dlp 讀不懂 YouTube 這次的回應，通常是 YouTube 改版、需要更新 yt-dlp。在自己的電腦上執行時，可以到「系統」頁按「檢查新版本」更新 yt-dlp。",
        "yt-dlp 讀不懂{site}這次的回應，可能是網站改版了。可以到「系統」頁按「檢查新版本」更新 yt-dlp 後再試。",
    ),
)

YOUTUBE_ERROR_ID_RE = re.compile(r"\[youtube\]\s+([\w-]{11}):")


def with_site(template: str, site: str) -> str:
    """The hint with the site's name in it. A Latin name is set off from Chinese by spaces, as YouTube is above."""
    pieces = template.split("{site}")
    text = pieces[0]
    latin_start, latin_end = site[:1].isascii() and site[:1].isalnum(), site[-1:].isascii() and site[-1:].isalnum()
    for piece in pieces[1:]:
        before = " " if latin_start and text and CJK_RE.match(text[-1]) else ""
        after = " " if latin_end and piece and CJK_RE.match(piece[0]) else ""
        text += f"{before}{site}{after}{piece}"
    return text


@functools.lru_cache(maxsize=None)
def given_hint_pattern(template: str) -> re.Pattern[str]:
    """Finds this hint at the start of a message it was given to, whichever site it names."""
    return re.compile(SITE_IN_HINT.join(re.escape(piece) for piece in template.split("{site}")))


@functools.cache
def extractor_keys() -> dict[str, str]:
    """yt-dlp's extractors by the name their messages start with ("archive.org"), to the key a site's name is found by
    ("ArchiveOrg"). A tag that is not here is a step's, such as [download] or [Merger], and names no site."""
    from yt_dlp.extractor import gen_extractor_classes

    return {str(ie.IE_NAME).lower(): str(ie.ie_key()) for ie in gen_extractor_classes()}


def site_in_message(cleaned: str, url: str = "") -> Optional[str]:
    """The other site a message is about, as its hints name it. None for YouTube. A message that names no site, as
    one from fetching a video's file does not, is about the site of the link it came from; with no link either, it
    reads as it always has."""
    tag = LEADING_TAG_RE.match(cleaned)
    if tag:
        name = tag.group(1).lower()
        if name.startswith(YOUTUBE_PREFIX):
            return None
        key = extractor_keys().get(name)
        if key is not None:
            return site_named(key)
    link = UNREADABLE_LINK_RE.search(cleaned.lower())
    # A link without a host, as written without https://, is read as YouTube's: other sites' links need one.
    if link and host_of(link.group(1)) and not is_youtube_url(link.group(1)):
        return UNNAMED_SITE
    if host_of(url) and not is_youtube_url(url):
        return site_named(extractor_for(url) or "generic")
    return None


def site_named(key: str) -> Optional[str]:
    if key.lower().startswith(YOUTUBE_PREFIX):
        # YouTube's videos in a web archive, still YouTube's.
        return None
    label = site_label(key)
    # A direct link's or an ordinary page's extractor is named after the link, which the hint does not repeat.
    return label if label in {known for _, known in SITE_NAMES} else UNNAMED_SITE


def matches_marker(marker: Marker, lowered: str) -> bool:
    return bool(marker.search(lowered)) if isinstance(marker, re.Pattern) else marker in lowered


def matching_hint(cleaned: str, url: str = "") -> Optional[tuple[Hint, str]]:
    """The first hint whose markers are in the message and that is given for the site the message is about."""
    lowered = cleaned.lower()
    site = site_in_message(cleaned, url)
    for hint in YTDLP_ERROR_HINTS:
        if any(matches_marker(marker, lowered) for marker in hint.markers):
            text = hint.text_for(site)
            if text:
                return hint, text
    return None


def humanize_ytdlp_error(message: str, url: str = "") -> str:
    """yt-dlp's message with what to do about it in front. The link the message came from says which site it is
    about when the message itself does not."""
    cleaned = message.removeprefix("ERROR: ").strip()
    matched = matching_hint(cleaned, url)
    return f"{matched[1]}（原始訊息：{cleaned[:200]}）" if matched else cleaned


def humanize_human_check(message: str, url: str = "") -> str:
    """yt-dlp's message for a refusal that the caller knows to be a site's check for a person: the try that passed
    for a browser was answered with it."""
    cleaned = message.removeprefix("ERROR: ").strip()
    hint = next(hint for hint in YTDLP_ERROR_HINTS if hint.kind == HUMAN_CHECK)
    return f"{hint.text_for(site_in_message(cleaned, url) or UNNAMED_SITE)}（原始訊息：{cleaned[:200]}）"


def classify_failure(message: str) -> Optional[tuple[Hint, str]]:
    """The hint a failure gets, from yt-dlp's own words or from words humanize_ytdlp_error already gave it. Those start
    with their hint, and quote only the start of the original, which may have lost the part that named the reason."""
    for hint in YTDLP_ERROR_HINTS:
        if hint.begins(message):
            given = given_hint_pattern(hint.other).match(message) if hint.other else None
            return hint, given.group(0) if given else str(hint.youtube)
    return matching_hint(message.removeprefix("ERROR: ").strip())


def failure_kind(message: str) -> Optional[str]:
    """What kind of failure this is, such as "rate_limit", whichever site it happened on and whether or not it was
    already put in words for the person."""
    classified = classify_failure(message)
    return classified[0].kind if classified else None


def failure_hint(message: str) -> Optional[str]:
    """The hint this failure gets, or got."""
    classified = classify_failure(message)
    return classified[1] if classified else None


def is_rate_limited(message: str) -> bool:
    """HTTP 429 or a site's check for bots: more requests now only make it last longer."""
    return failure_kind(message) in (RATE_LIMIT, BOT_CHECK)


def is_network_loss(message: str) -> bool:
    """The connection dropped or never came up, which often passes by itself."""
    return failure_kind(message) in (CONNECTION_LOST, UNREACHABLE)


def is_unreachable(message: str) -> bool:
    """The site could not be reached at all, as with a mistyped address, rather than lost during a download."""
    return failure_kind(message) == UNREACHABLE


def warns_of_missing_js_runtime(warnings: Iterable[str]) -> bool:
    return any(marker in warning.lower() for warning in warnings for marker in JS_RUNTIME_MARKERS)


def explain_missing_js_runtime(raw_errors: list[str], errors: list[str], lower_quality: list[str]) -> None:
    """Adds the JavaScript runtime hint to the humanized errors, after yt-dlp warned that it had none.

    It goes on the first error a missing runtime causes; a private or removed video fails for its own reason. Without
    such an error, videos that came out below the chosen quality get a message of their own."""
    caused = [index for index, message in enumerate(raw_errors) if any(marker in message.lower() for marker in MISSING_FORMAT_MARKERS)]
    if caused:
        if JS_RUNTIME_HINT not in errors[caused[0]]:
            errors[caused[0]] = f"{errors[caused[0]]}\n\n{JS_RUNTIME_HINT}"
    elif lower_quality:
        errors.append(f"{'；'.join(lower_quality)}。{JS_RUNTIME_HINT}")


def extract_failed_video_id(message: str) -> Optional[str]:
    """yt-dlp names the failing video in its message, which is the only way to retry just that one."""
    match = YOUTUBE_ERROR_ID_RE.search(message)
    return match.group(1) if match else None

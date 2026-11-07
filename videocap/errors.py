from __future__ import annotations

import re
from typing import Iterable, Optional


JS_RUNTIME_HINT = (
    "YouTube 需要 JavaScript 執行環境（Deno）才能提供完整的畫質，這台電腦目前沒有可用的。"
    "在自己的電腦上執行時，請到「系統」頁按「安裝 Deno」。"
)
# yt-dlp only warns about these, and the failure they cause comes later in other words.
JS_RUNTIME_MARKERS = ("no supported javascript runtime", "js runtime", "n challenge solving failed", "signature solving failed")
# What a missing JavaScript runtime leads to: YouTube then offers few formats, or only ones that refuse to download.
MISSING_FORMAT_MARKERS = ("requested format is not available", "no video formats found", "http error 403")
# Named, because the queue acts on these by itself: it pauses after the first two and retries after the last two.
BOT_CHECK_HINT = (
    "YouTube 要求驗證身分（判定為自動化流量），雲端主機和 VPN 的網路常遇到。請在「會員登入」登入帳號後再試；"
    "如果是在雲端主機上使用，建議改在自己的電腦上執行。"
)
RATE_LIMIT_HINT = "YouTube 暫時限制了請求頻率，請稍後再試（可能長達一小時）。"
CONNECTION_LOST_HINT = "下載途中網路連線中斷，請重新下載。"
UNREACHABLE_HINT = "連不上 YouTube，請確認網路或代理伺服器設定。"

# Checked in order, against the lowercased message; a marker is a substring or a pattern.
YTDLP_ERROR_HINTS: tuple[tuple[tuple[str | re.Pattern[str], ...], str], ...] = (
    # File problems come first: their messages quote the file's path, and the video title in it could match a hint below.
    (("no space left", "errno 28", "winerror 112"), "下載資料夾所在的硬碟空間不足，請清出空間後重試。"),
    (("being used by another process", "winerror 32"), "檔案正被其他程式使用（例如播放器），請關閉後重試。"),
    (
        (re.compile(r"winerror 5\b"), re.compile(r"errno 13\b"), "permission denied"),
        "沒有權限存取檔案：檔案可能正被其他程式（例如播放器）開啟，或資料夾不允許寫入。請關閉相關程式後重試，或改用其他下載資料夾。",
    ),
    (
        (
            "file name too long",
            "errno 36",
            "winerror 206",
            # Windows without long paths turned on calls a path past 260 characters missing instead.
            re.compile(r"(?:errno 2\b|winerror 3\b|no such file or directory).*?['\"][^'\"]{240,}"),
        ),
        "檔名或路徑太長，請改用較短的下載資料夾或「自訂名稱」。",
    ),
    (
        ("ffmpeg not found", "ffprobe not found", "ffmpeg is not installed", "ffmpeg could not be found"),
        "這項功能需要 ffmpeg，請先安裝後重新啟動 App。",
    ),
    # Also a post-processing error, but a network one: ffmpeg is not involved.
    (
        ("communicate with sponsorblock",),
        "連不上 SponsorBlock 服務，請稍後再試，或在側邊欄把「SponsorBlock」改成「不使用」。",
    ),
    (("conversion failed", "postprocessing:"), "ffmpeg 後處理失敗（合併影音或嵌入字幕）。"),
    (("not a bot",), BOT_CHECK_HINT),
    (
        ("your ip is likely being blocked",),
        "YouTube 封鎖了這台機器的網路位址（IP），雲端主機和 VPN 常遇到。請換個網路再試；如果是在雲端主機上使用，建議改在自己的電腦上執行。",
    ),
    (("confirm your age", "inappropriate for some users"), "這是年齡限制影片，請在「會員登入」登入 YouTube 帳號。"),
    (("private video",), "這是私人影片，請在「會員登入」用有觀看權限的帳號登入。"),
    (
        ("members-only", "join this channel"),
        "這是頻道會員專屬影片，請在「會員登入」用已加入此頻道會員的帳號登入。如果已經登入，代表這個帳號的會員等級不足，或登入已失效。",
    ),
    (("music premium", "premium members"), "這是 YouTube Premium 專屬內容，需要用有訂閱的帳號在「會員登入」登入。"),
    (("available in your country",), "此影片在目前所在地區無法觀看。"),
    (
        ("page needs to be reloaded", "必須重新載入頁面"),
        "這個 YouTube 用戶端目前無法使用（YouTube 改了它的驗證方式）。請在側邊欄把「YouTube 用戶端」改回「預設」，或換成 web_embedded。",
    ),
    (("drm protected",), "此影片受 DRM 保護，無法下載。"),
    (("rate-limited", "http error 429", "too many requests", "try again later"), RATE_LIMIT_HINT),
    (
        ("http error 403",),
        "YouTube 拒絕了下載請求。常見原因是這台機器的網路位址被限制（雲端主機、VPN 常見），或 yt-dlp 版本太舊。"
        "可以在「會員登入」登入帳號後再試；在自己的電腦上執行時，也可以到「系統」頁按「檢查新版本」更新 yt-dlp。",
    ),
    (
        ("http error 416", "requested range not satisfiable"),
        "續傳失敗：這部影片的暫存檔已經對不上。請到下載資料夾刪除對應的 .part 檔後重新下載。",
    ),
    (
        ("video unavailable", "video is unavailable", "no longer available", "has been removed", "has been terminated"),
        "影片無法使用，可能已被移除、設為不公開，或在主機所在地區無法觀看。",
    ),
    # yt-dlp names the list it was reading; these are the account's own lists, which exist only for a logged-in viewer.
    ((re.compile(r"\[youtube:tab\] (?:wl|ll|lm):"),), "「稍後觀看」和「喜歡的影片」是個人清單，需要先在「會員登入」登入帳號才能下載。"),
    (("channel/playlist does not exist",), "找不到這個頻道或播放清單，請確認網址是否正確。"),
    (
        ("this channel does not exist", re.compile(r"\[youtube:tab\].*http error 404")),
        "找不到這個頻道，請確認網址或 @頻道代號 是否正確。",
    ),
    (("playlist does not exist", "playlist is private"), "找不到這個播放清單，可能已被刪除或設為不公開。"),
    (("will begin in", "premieres in"), "直播或首播尚未開始，開始後才能下載。"),
    (("not currently live",), "這個頻道目前沒有在直播。"),
    (("requested format is not available",), "找不到符合條件的影片格式，請改選其他畫質。"),
    (
        ("no video formats found",),
        "YouTube 沒有提供這部影片任何可下載的格式，常見原因是 yt-dlp 版本太舊，或 YouTube 暫時限制了這台機器。請稍後再試。",
    ),
    (JS_RUNTIME_MARKERS, JS_RUNTIME_HINT),
    (("could not copy",), "讀不到瀏覽器的 cookies：這個瀏覽器執行時會鎖定資料。請完全關閉瀏覽器後再試，或改用 Firefox、cookies.txt。"),
    (
        ("failed to decrypt", "dpapi"),
        "無法解密這個瀏覽器的 cookies，新版 Chrome、Edge 在 Windows 上常有這個問題。請改用 Firefox 或 cookies.txt。",
    ),
    (("cookies database",), "找不到這個瀏覽器的 cookies，請確認已安裝這個瀏覽器，並在裡面登入 YouTube。"),
    (("netscape format", "failed to load cookies"), "cookies.txt 無法讀取，請確認是 Netscape 格式。"),
    # For a channel's home page the tab comes from the sidebar setting, not from the pasted link.
    (
        ("does not have a",),
        "這個頻道沒有這個分頁（例如沒有直播或 Shorts）。貼的是頻道首頁時，請到側邊欄「播放清單與頻道」把「頻道內容」改成其他分頁或「全部分頁」；"
        "貼的是分頁網址時，請改貼頻道首頁。",
    ),
    (("looks truncated",), "網址不完整，請重新複製完整網址。"),
    (("no suitable extractor",), "無法辨識這個網址，請從瀏覽器網址列或 YouTube 的「分享」重新複製完整連結。"),
    (("wrong regex for subtitlelangs",), "字幕語言代碼不是有效的正規表示式，請修改後再試。"),
    # Windows words these in the system's language, so only the error number can be matched.
    (
        ("winerror 10054", "connection reset", "did not get any data blocks", "content too short", re.compile(r"fragment \d+ not found")),
        CONNECTION_LOST_HINT,
    ),
    (
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
    ),
    # Last, because yt-dlp adds this sentence to any error it did not expect, which is usually a YouTube change.
    (
        ("confirm you are on the latest version",),
        "yt-dlp 讀不懂 YouTube 這次的回應，通常是 YouTube 改版、需要更新 yt-dlp。在自己的電腦上執行時，可以到「系統」頁按「檢查新版本」更新 yt-dlp。",
    ),
)

YOUTUBE_ERROR_ID_RE = re.compile(r"\[youtube\]\s+([\w-]{11}):")


def matches_marker(marker: str | re.Pattern[str], lowered: str) -> bool:
    return bool(marker.search(lowered)) if isinstance(marker, re.Pattern) else marker in lowered


def matching_hint(cleaned: str) -> Optional[str]:
    lowered = cleaned.lower()
    for markers, hint in YTDLP_ERROR_HINTS:
        if any(matches_marker(marker, lowered) for marker in markers):
            return hint
    return None


def humanize_ytdlp_error(message: str) -> str:
    cleaned = message.removeprefix("ERROR: ").strip()
    hint = matching_hint(cleaned)
    return f"{hint}（原始訊息：{cleaned[:200]}）" if hint else cleaned


def failure_hint(message: str) -> Optional[str]:
    """The hint a failure gets, from yt-dlp's own words or from words humanize_ytdlp_error already gave it. Those start
    with their hint, and quote only the start of the original, which may have lost the part that named the reason."""
    for _, hint in YTDLP_ERROR_HINTS:
        if message.startswith(hint):
            return hint
    return matching_hint(message.removeprefix("ERROR: ").strip())


def is_rate_limited(message: str) -> bool:
    """HTTP 429 or YouTube's check for bots: more requests now only make it last longer."""
    return failure_hint(message) in (RATE_LIMIT_HINT, BOT_CHECK_HINT)


def is_network_loss(message: str) -> bool:
    """The connection dropped or never came up, which often passes by itself."""
    return failure_hint(message) in (CONNECTION_LOST_HINT, UNREACHABLE_HINT)


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

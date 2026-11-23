from __future__ import annotations

import pytest

from videocap.errors import (
    IMPERSONATION,
    cut_at_a_sentence,
    failure_hint,
    failure_kind,
    humanize_ytdlp_error,
    is_network_loss,
    is_rate_limited,
    is_unreachable,
)
from videocap.impersonation import INSTALL_ADVICE


# Messages as yt-dlp words them (or as Windows words them in Traditional Chinese), with the hint each should get.
@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("ERROR: [youtube:tab] @thishandledoesnotexist-zq7x: Unable to download API page: HTTP Error 404: Not Found", "找不到這個頻道"),
        ("ERROR: [youtube:tab] UCabcdefghijklmnopqrstuv: YouTube said: This channel does not exist.", "找不到這個頻道"),
        ("ERROR: [youtube] jNQXAC9IVRw: All player responses are invalid. Your IP is likely being blocked by Youtube", "封鎖"),
        (
            "ERROR: [youtube] jNQXAC9IVRw: Failed to extract any player response; please report this issue on  "
            "https://github.com/yt-dlp/yt-dlp/issues?q= , filling out the appropriate issue template. "
            "Confirm you are on the latest version using  yt-dlp -U",
            "檢查新版本",
        ),
        ("ERROR: [youtube:tab] @someone: The channel is not currently live", "沒有在直播"),
        ("ERROR: No suitable extractor found for URL https://youtu.be", "無法辨識這個網址"),
        ("ERROR: Unable to download webpage: <urlopen error [WinError 10061] 無法連線，因為目標電腦拒絕連線。>", "連不上 YouTube"),
        ("ERROR: Unable to download webpage: [WinError 10060] 連線嘗試失敗", "連不上 YouTube"),
        ("ERROR: [WinError 10054] 遠端主機已強制關閉一個現存的連線。", "連線中斷"),
        ("ERROR: Did not get any data blocks", "連線中斷"),
        ("ERROR: content too short (expected 1000 bytes and served 10)", "連線中斷"),
        ("ERROR: fragment 1 not found, unable to continue", "連線中斷"),
        ("ERROR: m3u8 download detected but ffmpeg could not be found. Please install", "ffmpeg"),
        ("ERROR: Unable to rename file: [WinError 5] 存取被拒。: 'a.part' -> 'a.mp4'", "沒有權限"),
        ("ERROR: unable to open for writing: [Errno 13] Permission denied: '/data/a.mp4'", "沒有權限"),
        ("ERROR: Postprocessing: Unable to communicate with SponsorBlock API: HTTP Error 503", "SponsorBlock"),
    ],
)
def test_common_errors_get_a_hint(message: str, expected: str) -> None:
    hint = humanize_ytdlp_error(message)
    assert expected in hint
    # The original stays visible, so a wrong guess can still be checked.
    assert "原始訊息" in hint


def test_not_found_on_a_video_download_gets_no_channel_hint() -> None:
    message = "ERROR: unable to download video data: HTTP Error 404: Not Found"
    assert humanize_ytdlp_error(message) == message.removeprefix("ERROR: ")


def test_numbers_that_merely_start_like_a_known_error_get_no_hint() -> None:
    assert humanize_ytdlp_error("ERROR: [WinError 50] 不支援此要求。") == "[WinError 50] 不支援此要求。"


@pytest.mark.parametrize(
    "message",
    ["ERROR: HTTP Error 403: Forbidden", "ERROR: [youtube] x: Sign in to confirm you're not a bot"],
)
def test_blocked_hints_do_not_assume_a_cloud_host(message: str) -> None:
    hint = humanize_ytdlp_error(message)
    # This machine's owner runs it at home; telling them to "run it on your own computer" is no help.
    assert "請改在本機執行" not in hint
    assert "請改在自己的電腦上執行" not in hint


@pytest.mark.parametrize("playlist_id", ["WL", "LL"])
def test_personal_playlists_ask_for_a_login(playlist_id: str) -> None:
    hint = humanize_ytdlp_error(f"ERROR: [youtube:tab] {playlist_id}: YouTube said: The playlist does not exist.")
    assert "登入" in hint
    assert "刪除" not in hint


def test_a_missing_channel_is_not_called_a_playlist() -> None:
    hint = humanize_ytdlp_error(
        "ERROR: [youtube:tab] @nobody: The channel/playlist does not exist and the URL redirected to youtube.com home page"
    )
    assert "頻道" in hint
    assert hint.startswith("找不到這個頻道或播放清單")


def test_no_formats_at_all_does_not_suggest_another_quality() -> None:
    hint = humanize_ytdlp_error("ERROR: [youtube] jNQXAC9IVRw: No video formats found!")
    assert "畫質" not in hint
    assert "原始訊息" in hint


def test_missing_channel_tab_points_at_the_sidebar_setting() -> None:
    hint = humanize_ytdlp_error("ERROR: [youtube:tab] @someone: This channel does not have a streams tab")
    assert "頻道內容" in hint


def test_a_video_title_in_a_file_path_does_not_pick_the_hint() -> None:
    message = (
        "ERROR: Unable to rename file: [WinError 5] 存取被拒。: "
        "'D:\\Videos\\Private Video Reveal [abcdefghijk].f137.mp4.part' -> 'D:\\Videos\\Private Video Reveal [abcdefghijk].f137.mp4'"
    )
    hint = humanize_ytdlp_error(message)
    assert "私人影片" not in hint
    assert "沒有權限" in hint


@pytest.mark.parametrize(
    "template",
    [
        "ERROR: unable to open for writing: [Errno 2] No such file or directory: '{path}'",
        "ERROR: Unable to rename file: [WinError 3] 系統找不到指定的路徑。: '{path}.part' -> '{path}'",
    ],
)
def test_an_overlong_missing_path_is_called_too_long(template: str) -> None:
    # Windows without long-path support reports a path past 260 characters as missing, not as too long.
    path = "D:\\Videos\\" + "很長的頻道名稱" * 20 + "\\" + "a" * 120 + " [abcdefghijk].mp4"
    assert humanize_ytdlp_error(template.format(path=path)).startswith("檔名或路徑太長")


def test_the_js_runtime_hint_needs_no_command_line() -> None:
    hint = humanize_ytdlp_error("[youtube] abc123def45: n challenge solving failed: Some formats may be missing.")
    assert "pip" not in hint
    assert "「系統」頁" in hint and "安裝 Deno" in hint


def test_a_short_missing_path_is_not_called_too_long() -> None:
    hint = humanize_ytdlp_error("ERROR: unable to open for writing: [Errno 2] No such file or directory: 'D:\\Videos\\a.mp4'")
    assert "太長" not in hint


def test_a_title_in_a_postprocessing_error_does_not_pick_the_hint() -> None:
    message = "ERROR: Postprocessing: Conversion failed! 'D:\\Videos\\Members-only Q&A [abcdefghijk].mp4'"
    assert humanize_ytdlp_error(message).startswith("ffmpeg 後處理失敗")


# Which failures the queue acts on by itself: a pause for rate limiting, a retry for a dropped connection.
@pytest.mark.parametrize(
    ("message", "rate_limited", "network_loss"),
    [
        ("ERROR: [youtube] jNQXAC9IVRw: Unable to download API page: HTTP Error 429: Too Many Requests", True, False),
        ("ERROR: unable to download video data: HTTP Error 429: Too Many Requests", True, False),
        ("ERROR: [youtube] jNQXAC9IVRw: Sign in to confirm you’re not a bot. Use --cookies-from-browser", True, False),
        ("ERROR: [youtube] jNQXAC9IVRw: The current session has been rate-limited by YouTube for up to an hour.", True, False),
        ("ERROR: [youtube] jNQXAC9IVRw: This content isn't available, try again later.", True, False),
        ("ERROR: Unable to download webpage: <urlopen error [WinError 10061] 無法連線，因為目標電腦拒絕連線。>", False, True),
        ("ERROR: Unable to download webpage: <urlopen error [Errno 11001] getaddrinfo failed>", False, True),
        ("ERROR: Unable to download webpage: <urlopen error Tunnel connection failed: 407 Proxy Authentication Required>", False, True),
        ("ERROR: Unable to download webpage: [WinError 10060] 連線嘗試失敗", False, True),
        ("ERROR: [WinError 10054] 遠端主機已強制關閉一個現存的連線。", False, True),
        ("ERROR: Did not get any data blocks", False, True),
        ("ERROR: content too short (expected 1000 bytes and served 10)", False, True),
        ("ERROR: fragment 1 not found, unable to continue", False, True),
        ("ERROR: [youtube] jNQXAC9IVRw: Private video. Sign in if you've been granted access to this video", False, False),
        ("ERROR: HTTP Error 403: Forbidden", False, False),
        # A title in a path names neither: the disk is full.
        ("ERROR: unable to write data: [Errno 28] No space left on device: 'D:\\Videos\\Too Many Requests, Try Again Later.mp4'", False, False),
    ],
)
def test_rate_limits_and_lost_connections_are_told_apart(message: str, rate_limited: bool, network_loss: bool) -> None:
    assert is_rate_limited(message) is rate_limited
    assert is_network_loss(message) is network_loss
    # The same once the downloader has put the message in words for the person.
    humanized = humanize_ytdlp_error(message)
    assert is_rate_limited(humanized) is rate_limited
    assert is_network_loss(humanized) is network_loss


def test_a_long_message_keeps_its_kind_after_it_is_cut_short() -> None:
    # Only the start of the original is quoted, and the part that named the reason may be past it.
    message = f"ERROR: [youtube] jNQXAC9IVRw: {'x' * 300} HTTP Error 429: Too Many Requests"
    humanized = humanize_ytdlp_error(message)
    assert "429" not in humanized
    assert is_rate_limited(humanized) and not is_network_loss(humanized)
    assert not is_rate_limited("") and not is_network_loss("")


# Other sites. yt-dlp starts an extractor's message with its name, which is how the hint knows the site.

COOKIES_HOWTO = "See  https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp  for how to manually pass cookies"
COOKIES_ADVICE = f"Use --cookies-from-browser or --cookies for the authentication. {COOKIES_HOWTO}"
CREDENTIALS_ADVICE = f"Use --cookies, --cookies-from-browser, --username and --password, --netrc-cmd, or --netrc (vimeo) to provide account credentials. {COOKIES_HOWTO}"
REPORT_ADVICE = (
    "please report this issue on  https://github.com/yt-dlp/yt-dlp/issues?q= , filling out the appropriate issue template. "
    "Confirm you are on the latest version using  yt-dlp -U"
)
CLOUDFLARE = (
    "ERROR: [generic] page: Got HTTP Error 403 caused by Cloudflare anti-bot challenge; see  https://github.com/yt-dlp/yt-dlp#impersonation  "
    'for how to install the required impersonation dependency, and try again with  --extractor-args "generic:impersonate"'
)


def hint_of(message: str) -> str:
    """The app's words, without the original quoted after them."""
    return humanize_ytdlp_error(message).partition("（原始訊息：")[0]


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("ERROR: Unsupported URL: https://example.com/about", "找不到可以下載的影片"),
        ("ERROR: No suitable extractor found for URL https://example.com/about", "找不到可以下載的影片"),
        (f"ERROR: [facebook] 1234567890: This video is only available for registered users. {COOKIES_ADVICE}", "Facebook 需要登入"),
        (f"ERROR: [vimeo] 76979871: This video is only available for registered users. {CREDENTIALS_ADVICE}", "Vimeo 需要登入"),
        (
            "ERROR: [instagram] C1a2b3c4d5e: The webpage request was redirected to the login page. "
            f"You have exceeded the rate-limit for accessing posts anonymously. {COOKIES_ADVICE}",
            "Instagram 需要登入",
        ),
        (f"ERROR: [twitter] 1700000000000000000: NSFW tweet requires authentication. {COOKIES_ADVICE}", "X（Twitter）需要登入"),
        ("ERROR: [generic] members: Login required to download: members only", "這個網站需要登入"),
        (
            "ERROR: [BiliBili] BV1xx411c7mD: This video is not available from your location due to geo restriction\n"
            "This video is available in Japan.\nYou might want to use a VPN or a proxy server (with --proxy) to workaround.",
            "所在地區無法觀看",
        ),
        ("ERROR: [vimeo] 76979871: The uploader has not made this video available in your country.", "所在地區無法觀看"),
        ("ERROR: [generic] missing: Unable to download webpage: HTTP Error 404: File not found (caused by <HTTPError 404: File not found>)", "找不到這部影片"),
        ("ERROR: [vimeo] 76979871: Unable to download JSON metadata: HTTP Error 404: Not Found (caused by <HTTPError 404: Not Found>)", "連結可能已失效或打錯了"),
        ("ERROR: [generic] clip: Unable to download webpage: HTTP Error 403: Forbidden (caused by <HTTPError 403: Forbidden>)", "這個網站拒絕了下載請求"),
        ("ERROR: [vimeo] 76979871: Unable to download JSON metadata: HTTP Error 403: Forbidden", "Vimeo 拒絕了下載請求"),
        (
            "ERROR: [DRM] watch: The requested site is known to use DRM protection. It will NOT be supported.\n"
            "Please DO NOT open an issue, unless you have evidence that the video is not DRM protected",
            "DRM",
        ),
        ("ERROR: [vimeo] 76979871: This video is DRM protected", "受 DRM 保護，無法下載"),
        (
            "ERROR: [Piracy] watch: This website is no longer supported since it has been determined to be primarily used for piracy.\n"
            "DO NOT open issues for it",
            "yt-dlp 不支援這個網站",
        ),
        (CLOUDFLARE, "這個網站需要模擬瀏覽器"),
        (
            "ERROR: [vimeo] 76979871: The extractor is attempting impersonation, but no impersonate target is available. "
            "See  https://github.com/yt-dlp/yt-dlp#impersonation  for information on installing the required dependencies",
            "Vimeo 需要模擬瀏覽器",
        ),
        ("ERROR: [vimeo] 76979871: Unable to download JSON metadata: HTTP Error 429: Too Many Requests", "Vimeo 暫時限制了請求頻率"),
        ("ERROR: [generic] Unable to download webpage: <urlopen error [Errno 11001] getaddrinfo failed>", "連不上這個網站"),
        (f"ERROR: [vimeo] 76979871: An extractor error has occurred. (caused by KeyError('config')); {REPORT_ADVICE}", "讀不懂 Vimeo 這次的回應"),
        # A name that comes with its extractor's key: archive.org is ArchiveOrg.
        ("ERROR: [archive.org] oldfilm: Unable to download JSON metadata: HTTP Error 403: Forbidden", "Internet Archive 拒絕了下載請求"),
    ],
)
def test_other_sites_errors_get_a_hint_of_their_own(message: str, expected: str) -> None:
    humanized = humanize_ytdlp_error(message)
    assert expected in humanized and "原始訊息" in humanized
    hint = hint_of(message)
    # Nothing that holds only for YouTube: its player clients, its rate limits, or its cloud-host advice.
    assert "YouTube" not in hint and "用戶端" not in hint and "雲端主機" not in hint


@pytest.mark.parametrize(
    "message",
    [
        "ERROR: [generic] clip: Unable to download webpage: HTTP Error 403: Forbidden (caused by <HTTPError 403: Forbidden>)",
        CLOUDFLARE,
    ],
)
def test_another_sites_refusal_suggests_a_login_rather_than_an_update(message: str) -> None:
    hint = hint_of(message)
    assert "檢查新版本" not in hint and "更新" not in hint
    # Nothing to install either, unless the caller says this person can: a visitor has no 系統 page.
    assert "安裝" not in hint and "pip" not in hint


def test_only_the_owner_is_told_how_to_get_past_a_site_that_wants_a_browser() -> None:
    for_owner = humanize_ytdlp_error(CLOUDFLARE, browser_advice=INSTALL_ADVICE)
    assert for_owner.startswith("這個網站需要模擬瀏覽器（impersonation）才能下載：一般的程式連線會被它擋下。請到「系統」頁安裝模擬瀏覽器後再試。（原始訊息：")
    for_visitor = humanize_ytdlp_error(CLOUDFLARE)
    assert "系統" not in for_visitor and "安裝" not in for_visitor
    # Still the same kind of failure, with or without the advice.
    assert failure_kind(for_owner) == failure_kind(for_visitor) == failure_kind(CLOUDFLARE) == IMPERSONATION
    # No other failure is about a browser, whoever asks.
    refusal = "ERROR: [generic] clip: Unable to download webpage: HTTP Error 403: Forbidden (caused by <HTTPError 403: Forbidden>)"
    assert humanize_ytdlp_error(refusal, browser_advice=INSTALL_ADVICE) == humanize_ytdlp_error(refusal)
    # YouTube's subtitles ask for impersonation without needing it, which is no failure and gets no hint.
    warning = "[youtube] jNQXAC9IVRw: The extractor specified to use impersonation for this download, but no impersonate target is available"
    assert humanize_ytdlp_error(warning, browser_advice=INSTALL_ADVICE) == warning


# How much of yt-dlp's message is quoted


def test_a_long_message_is_quoted_up_to_the_end_of_a_sentence() -> None:
    # Cut by length alone, this one stopped at "and try again with", as if the rest had been lost.
    assert humanize_ytdlp_error(CLOUDFLARE).endswith("（原始訊息：[generic] page: Got HTTP Error 403 caused by Cloudflare anti-bot challenge…）")
    short = "ERROR: [generic] clip: Unable to download webpage: HTTP Error 403: Forbidden (caused by <HTTPError 403: Forbidden>)"
    assert humanize_ytdlp_error(short).endswith(f"（原始訊息：{short.removeprefix('ERROR: ')}）")


@pytest.mark.parametrize(
    ("text", "limit", "expected"),
    [
        ("Short enough.", 40, "Short enough."),
        ("The first sentence is whole. The second one would be cut somewhere in its middle.", 50, "The first sentence is whole…"),
        # A dot inside an address ends nothing.
        ("See https://github.com/yt-dlp/yt-dlp for more; then try again with other options", 60, "See https://github.com/yt-dlp/yt-dlp for more…"),
        ("這個網站拒絕了下載請求。可能是需要登入，或連結已過期，也可能是這個網站不允許下載。", 30, "這個網站拒絕了下載請求…"),
        # No sentence ends in reach: at a word.
        ("one long line of a log without any stop in it at all that goes on", 40, "one long line of a log without any stop…"),
        # A sentence that ends in the first few words would leave almost nothing.
        ("No. " + "word " * 30, 40, "No. word word word word word word word…"),
        # Nothing to cut at: at the limit.
        ("x" * 100, 40, "x" * 40 + "…"),
    ],
)
def test_text_is_cut_at_a_sentence_then_at_a_word(text: str, limit: int, expected: str) -> None:
    assert cut_at_a_sentence(text, limit) == expected


def test_protected_and_refused_sites_are_not_worked_around() -> None:
    drm = hint_of("ERROR: [vimeo] 76979871: This video is DRM protected")
    site = hint_of("ERROR: [DRM] watch: The requested site is known to use DRM protection. It will NOT be supported.")
    for hint in (drm, site):
        assert "無法下載" in hint
        assert not any(word in hint for word in ("登入", "VPN", "代理", "cookies", "再試"))


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        # YouTube's own messages carry yt-dlp's advice about cookies too, yet each keeps the hint that names its reason.
        (f"ERROR: [youtube] jNQXAC9IVRw: Sign in to confirm you’re not a bot. {COOKIES_ADVICE}", "YouTube 要求驗證身分"),
        (f"ERROR: [youtube] jNQXAC9IVRw: Sign in to confirm your age. This video may be inappropriate for some users. {COOKIES_ADVICE}", "年齡限制"),
        (f"ERROR: [youtube] jNQXAC9IVRw: Private video. Sign in if you've been granted access to this video. {COOKIES_ADVICE}", "私人影片"),
        (
            "ERROR: [youtube] jNQXAC9IVRw: This content isn't available, try again later. The current session has been rate-limited by YouTube "
            "for up to an hour. It is recommended to use `-t sleep` to add a delay between video requests to avoid exceeding the rate limit.",
            "YouTube 暫時限制了請求頻率",
        ),
        (f"ERROR: [youtube:tab] FEsubscriptions: Login details are needed to download this content. {COOKIES_ADVICE}", "需要登入 YouTube"),
        # A tag of one of yt-dlp's own steps names no site.
        ("ERROR: [download] Got error: HTTP Error 403: Forbidden", "YouTube 拒絕了下載請求"),
        ("ERROR: unable to download video data: HTTP Error 403: Forbidden", "YouTube 拒絕了下載請求"),
        ("ERROR: Unable to download webpage: <urlopen error [Errno 11001] getaddrinfo failed>", "連不上 YouTube"),
        ("ERROR: No suitable extractor found for URL https://www.youtube.com/about", "無法辨識這個網址"),
        ("ERROR: No suitable extractor found for URL youtu.be/jNQXAC9IVRw", "無法辨識這個網址"),
        # YouTube's videos in a web archive are still YouTube's.
        ("ERROR: [web.archive:youtube] jNQXAC9IVRw: HTTP Error 403: Forbidden", "YouTube 拒絕了下載請求"),
    ],
)
def test_youtubes_messages_keep_youtubes_words(message: str, expected: str) -> None:
    hint = hint_of(message)
    assert expected in hint and "這個網站" not in hint


@pytest.mark.parametrize(
    ("youtube", "youtube_words", "other"),
    [
        ("[youtube] jNQXAC9IVRw: HTTP Error 429: Too Many Requests", "YouTube 暫時限制", "[vimeo] 76979871: HTTP Error 429: Too Many Requests"),
        ("[youtube] jNQXAC9IVRw: Sign in to confirm you're not a bot", "YouTube 要求驗證身分", "[vimeo] 76979871: Sign in to confirm you're not a bot"),
        ("[youtube] jNQXAC9IVRw: HTTP Error 403: Forbidden", "YouTube 拒絕", "[vimeo] 76979871: HTTP Error 403: Forbidden"),
        ("[youtube] jNQXAC9IVRw: No video formats found!", "YouTube 沒有提供", "[vimeo] 76979871: No video formats found!"),
        ("[youtube] jNQXAC9IVRw: Your IP is likely being blocked by Youtube", "YouTube 封鎖", "[vimeo] 76979871: Your IP is likely being blocked"),
        ("[youtube] jNQXAC9IVRw: Join this channel to get access to members-only content", "頻道會員", "[vimeo] 76979871: members-only content"),
        (f"[youtube] jNQXAC9IVRw: Oops. {REPORT_ADVICE}", "讀不懂 YouTube", f"[vimeo] 76979871: Oops. {REPORT_ADVICE}"),
    ],
)
def test_a_hint_names_the_site_its_message_came_from(youtube: str, youtube_words: str, other: str) -> None:
    youtube_hint, other_hint = hint_of(youtube), hint_of(other)
    assert youtube_words in youtube_hint and "Vimeo" not in youtube_hint
    assert "Vimeo" in other_hint and "YouTube" not in other_hint
    # One kind of failure, whichever site it named.
    assert failure_kind(youtube) == failure_kind(other) is not None


@pytest.mark.parametrize(
    "message",
    [
        "ERROR: [vimeo] 76979871: The page needs to be reloaded.",
        "ERROR: [vimeo] 76979871: This channel does not have a videos tab",
        "ERROR: [vimeo] 76979871: No supported JavaScript runtime could be found",
    ],
)
def test_youtubes_own_advice_is_not_given_for_another_site(message: str) -> None:
    hint = hint_of(message)
    assert "用戶端" not in hint and "頻道內容" not in hint and "Deno" not in hint


def test_a_404_while_downloading_a_youtube_stream_still_gets_no_hint() -> None:
    # Only another site's 404 says the link is gone; YouTube's own are its channels', which have their hint.
    assert humanize_ytdlp_error("ERROR: [youtube] jNQXAC9IVRw: HTTP Error 404: Not Found") == "[youtube] jNQXAC9IVRw: HTTP Error 404: Not Found"
    assert hint_of("ERROR: [youtube:tab] @nobody: Unable to download API page: HTTP Error 404: Not Found").startswith("找不到這個頻道")


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        # A title in a file's path cannot pick the hint, on any site.
        ("ERROR: unable to write data: [Errno 28] No space left on device: 'D:\\Videos\\Login required [clip-3f9a1c2e].mp4'", "硬碟空間不足"),
        ("ERROR: Unable to rename file: [WinError 5] 存取被拒。: 'D:\\Videos\\HTTP Error 404 [clip-3f9a1c2e].mp4.part'", "沒有權限"),
        # Cloudflare's check is an HTTP 403 that impersonation would get past.
        (CLOUDFLARE, "模擬瀏覽器"),
        # The DRM list's own words also say "will NOT be supported".
        ("ERROR: [DRM] watch: The requested site is known to use DRM protection. It will NOT be supported.", "DRM"),
        # A reason that is not a login comes before yt-dlp's advice about cookies.
        (f"ERROR: [vimeo] 76979871: This video is DRM protected. {COOKIES_ADVICE}", "DRM"),
        (f"ERROR: [vimeo] 76979871: Private video. Login if you have permissions to watch. {COOKIES_ADVICE}", "私人影片"),
        ("ERROR: [vimeo] 76979871: HTTP Error 429: Too Many Requests. Login required to avoid this", "暫時限制了請求頻率"),
    ],
)
def test_the_first_matching_hint_wins_for_other_sites_too(message: str, expected: str) -> None:
    assert expected in hint_of(message)


# What the queue acts on, for other sites: they name their site in the hint, and it is still the same kind of failure.
@pytest.mark.parametrize(
    ("message", "rate_limited", "network_loss", "unreachable"),
    [
        ("ERROR: [vimeo] 76979871: Unable to download JSON metadata: HTTP Error 429: Too Many Requests", True, False, False),
        ("ERROR: [vimeo] 76979871: Sign in to confirm you're not a bot", True, False, False),
        ("ERROR: [generic] Unable to download webpage: <urlopen error [WinError 10061] 無法連線，因為目標電腦拒絕連線。>", False, True, True),
        ("ERROR: [generic] clip: Unable to download webpage: <urlopen error [Errno 11001] getaddrinfo failed>", False, True, True),
        ("ERROR: [archive.org] oldfilm: Unable to download webpage: [WinError 10060] 連線嘗試失敗", False, True, True),
        ("ERROR: [WinError 10054] 遠端主機已強制關閉一個現存的連線。", False, True, False),
        ("ERROR: Unable to download webpage: <urlopen error [Errno 11001] getaddrinfo failed>", False, True, True),
        ("ERROR: [generic] clip: Unable to download webpage: HTTP Error 403: Forbidden", False, False, False),
        ("ERROR: [generic] missing: Unable to download webpage: HTTP Error 404: File not found", False, False, False),
        (CLOUDFLARE, False, False, False),
    ],
)
def test_other_sites_failures_keep_their_kind_once_put_in_words(message: str, rate_limited: bool, network_loss: bool, unreachable: bool) -> None:
    humanized = humanize_ytdlp_error(message)
    for text in (message, humanized):
        assert is_rate_limited(text) is rate_limited
        assert is_network_loss(text) is network_loss
        assert is_unreachable(text) is unreachable
    assert failure_kind(humanized) == failure_kind(message)
    assert failure_hint(humanized) == failure_hint(message) and humanized.startswith(str(failure_hint(message)))


def test_another_sites_long_message_keeps_its_kind_after_it_is_cut_short() -> None:
    message = f"ERROR: [vimeo] 76979871: {'x' * 300} HTTP Error 429: Too Many Requests"
    humanized = humanize_ytdlp_error(message)
    assert humanized.startswith("Vimeo 暫時限制了請求頻率") and "429" not in humanized
    assert is_rate_limited(humanized) and not is_network_loss(humanized)


def test_a_refusal_from_another_site_says_it_may_want_the_page_it_came_from() -> None:
    message = "ERROR: [generic] index: Unable to download webpage: HTTP Error 403: Forbidden (caused by <HTTPError 403: Forbidden>)"
    other = humanize_ytdlp_error(message, "https://cdn.example.com/hls/index.m3u8")
    # How the owner gets past it: the one place a stream's site is told which page it is asked for from.
    assert "Referer" in other and "--referer" in other and "進階 yt-dlp 參數" in other
    # Still the same failure when it is read again in the app's own words.
    assert failure_kind(other) == "http_403"
    # YouTube's refusal has nothing to do with the page it came from.
    youtube = humanize_ytdlp_error("ERROR: [download] Got error: HTTP Error 403: Forbidden", "https://youtu.be/jNQXAC9IVRw")
    assert "Referer" not in youtube and "referer" not in youtube


def test_yt_dlps_word_for_no_video_is_a_kind_of_its_own() -> None:
    from videocap.errors import UNREADABLE_LINK

    for message in ("ERROR: Unsupported URL: https://example.com/about", "ERROR: No suitable extractor found for URL https://example.com/about"):
        assert failure_kind(message) == UNREADABLE_LINK
        # And after the app has put it in its own words.
        assert failure_kind(humanize_ytdlp_error(message, "https://example.com/about")) == UNREADABLE_LINK


# A message that names no site

UNNAMED_REFUSAL = "ERROR: unable to download video data: HTTP Error 403: Forbidden"


@pytest.mark.parametrize(
    ("url", "start"),
    [
        ("https://vimeo.com/76979871", "Vimeo 拒絕了下載請求（HTTP 403）"),
        ("https://cdn.example.com/files/a.mp4?sig=1", "這個網站拒絕了下載請求（HTTP 403）"),
        ("https://example.com/article", "這個網站拒絕了下載請求（HTTP 403）"),
        ("https://www.youtube.com/watch?v=jNQXAC9IVRw", "YouTube 拒絕了下載請求。"),
        ("https://youtu.be/jNQXAC9IVRw", "YouTube 拒絕了下載請求。"),
        # No link is known, as for a message put in words long after: it reads as it always has.
        ("", "YouTube 拒絕了下載請求。"),
    ],
)
def test_a_failure_that_names_no_site_is_about_the_site_of_its_link(url: str, start: str) -> None:
    assert humanize_ytdlp_error(UNNAMED_REFUSAL, url).startswith(start)


def test_a_step_tag_names_no_site_either() -> None:
    message = "ERROR: [download] Got error: HTTP Error 429: Too Many Requests"
    assert humanize_ytdlp_error(message, "https://vimeo.com/76979871").startswith("Vimeo 暫時限制了請求頻率")
    assert humanize_ytdlp_error(message, "https://youtu.be/jNQXAC9IVRw").startswith("YouTube 暫時限制了請求頻率")


def test_the_site_a_message_names_counts_before_its_links() -> None:
    # A page elsewhere that plays a YouTube video fails as YouTube does.
    embedded = "ERROR: [youtube] jNQXAC9IVRw: Sign in to confirm you’re not a bot"
    assert humanize_ytdlp_error(embedded, "https://example.com/post").startswith("YouTube 要求驗證身分")
    named = "ERROR: [vimeo] 76979871: Unable to download JSON metadata: HTTP Error 403: Forbidden"
    assert humanize_ytdlp_error(named, "https://example.com/post").startswith("Vimeo 拒絕了下載請求（HTTP 403）")


def test_a_failure_keeps_its_kind_whichever_site_its_link_names() -> None:
    limited = humanize_ytdlp_error("ERROR: unable to download video data: HTTP Error 429: Too Many Requests", "https://vimeo.com/1")
    assert failure_kind(limited) == "rate_limit" and is_rate_limited(limited)

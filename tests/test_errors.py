from __future__ import annotations

import pytest

from videocap.errors import humanize_ytdlp_error, is_network_loss, is_rate_limited


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
        ("ERROR: unable to write data: [Errno 28] No space left on device: 'D:\Videos\Too Many Requests, Try Again Later.mp4'", False, False),
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

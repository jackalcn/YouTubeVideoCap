from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest
import yt_dlp

import videocap.inspector as inspector
from videocap.config import EXPIRED_LOGIN_MESSAGE, NO_LOGIN_COOKIES_MESSAGE, YOUTUBE_EXTRACTORS
from videocap.inspector import (
    collect_audio_languages,
    collect_resolutions,
    describe_formats,
    describe_selected_format,
    flatten_subtitle_tracks,
    format_video_details,
    iter_video_entries,
    rows_to_csv,
    summarize_reports,
)
from videocap.options import AccessLimits, DownloadOptions, options_from_spec
from videocap.settings import DownloadSettings, JobSpec


VIDEO_ONLY = {
    "format_id": "137",
    "vcodec": "avc1.640028",
    "acodec": "none",
    "ext": "mp4",
    "resolution": "1920x1080",
    "width": 1920,
    "height": 1080,
    "fps": 30.0,
    "dynamic_range": "SDR",
    "filesize": 1024 * 1024 * 50,
    "tbr": 2500.0,
}
AUDIO_ONLY = {"format_id": "140", "vcodec": "none", "acodec": "mp4a.40.2", "ext": "m4a", "filesize": 1024 * 1024 * 5, "language": "ja", "tbr": 128.0}
STORYBOARD = {"format_id": "sb0", "vcodec": "none", "acodec": "none", "ext": "mhtml", "format_note": "storyboard"}


def test_format_list_is_best_first_and_hides_storyboards() -> None:
    rows = describe_formats([STORYBOARD, AUDIO_ONLY, VIDEO_ONLY])
    assert [row["格式ID"] for row in rows] == ["137", "140"]
    assert rows[0]["類型"] == "僅影像"
    assert rows[0]["大小"] == "50.00 MB" or rows[0]["大小"] == "50 MB"
    assert rows[1]["類型"] == "僅音訊"
    assert rows[1]["語言"] == "ja"
    assert rows[1]["解析度"] == ""


def test_selected_format_summary_adds_up_streams() -> None:
    label, size = describe_selected_format({"requested_formats": [VIDEO_ONLY, AUDIO_ONLY]})
    assert label == "1080p avc1 + mp4a"
    assert size == 1024 * 1024 * 55


def test_selected_single_file() -> None:
    label, size = describe_selected_format({"vcodec": "vp9", "acodec": "opus", "height": 2160, "fps": 60, "filesize_approx": 9})
    assert label == "2160p60 vp9 + opus"
    assert size == 9


def test_selected_format_names_hdr() -> None:
    label, _ = describe_selected_format({"vcodec": "vp9.2", "acodec": "none", "height": 1080, "dynamic_range": "HDR10"})
    assert label == "1080p vp9 HDR10"


def test_resolutions_use_the_short_side_for_vertical_videos() -> None:
    formats = [{"vcodec": "avc1", "width": 1080, "height": 1920}, {"vcodec": "avc1", "width": 720, "height": 1280}, AUDIO_ONLY]
    assert collect_resolutions(formats) == [1080, 720]


def test_audio_languages() -> None:
    assert collect_audio_languages([AUDIO_ONLY, {**AUDIO_ONLY, "language": "en"}, VIDEO_ONLY]) == ["en", "ja"]


def test_subtitle_tracks_hide_live_chat() -> None:
    rows = flatten_subtitle_tracks({"en": [{"ext": "srt", "name": "English"}], "live_chat": [{"ext": "json"}], "de": []})
    assert rows == [{"語言": "en", "名稱": "English", "格式": "srt"}]
    assert flatten_subtitle_tracks(None) == []


def test_details_line() -> None:
    details = format_video_details(
        {"duration_string": "19", "upload_date": "20050424", "view_count": 1000, "availability": "subscriber_only", "license": "Creative Commons Attribution license"}
    )
    assert details == ["長度 0:19", "上傳於 2005-04-24", "1,000 次觀看", "會員專屬", "Creative Commons 授權"]


def test_nested_channel_tabs_flatten() -> None:
    info = {"entries": [{"entries": [{"id": "a"}, None]}, {"id": "b"}]}
    assert [entry["id"] for entry in iter_video_entries(info)] == ["a", "b"]


def test_summary_lights() -> None:
    assert summarize_reports([])[0] == ""
    assert summarize_reports([{"error": "壞了"}])[0] == "red"
    assert summarize_reports([{"is_live": True, "error": ""}])[0] == "yellow"
    assert summarize_reports([{"error": "", "manual_rows": [], "auto_rows": []}])[0] == "yellow"
    assert summarize_reports([{"error": "", "manual_rows": [{"語言": "en"}]}])[0] == "green"
    assert summarize_reports([{"error": "", "is_playlist": True, "entries": [{"id": "a"}]}]) == ("green", "連結可用。")  # type: ignore[typeddict-item]


def test_csv_opens_correctly_in_excel_and_blocks_formulas() -> None:
    data = rows_to_csv([{"標題": "=HYPERLINK(\"http://evil\")", "數字": 5, "安全": "一般文字"}])
    assert data.startswith(b"\xef\xbb\xbf")
    text = data.decode("utf-8-sig")
    assert "'=HYPERLINK" in text
    assert "一般文字" in text
    assert rows_to_csv([]) == b"\xef\xbb\xbf"


def test_search_uses_only_youtube_and_returns_watch_links(monkeypatch: Any) -> None:
    class FakeYDL:
        opts: dict[str, Any] = {}

        def __init__(self, opts: dict[str, Any]) -> None:
            FakeYDL.opts = opts

        def __enter__(self) -> FakeYDL:
            return self

        def __exit__(self, *args: object) -> bool:
            return False

        def extract_info(self, url: str, download: bool = True) -> dict[str, Any]:
            FakeYDL.opts["url"] = url
            return {"entries": [{"id": "abc123def45", "title": "T", "duration": 65, "view_count": 5, "uploader": "U", "thumbnails": [{"url": "http://thumb"}]}, {"title": "no id"}]}

    monkeypatch.setattr(inspector.yt_dlp, "YoutubeDL", FakeYDL)
    results = inspector.search_youtube("cats", 3, inspector.LoginSource())
    assert FakeYDL.opts["url"] == "ytsearch3:cats"
    assert FakeYDL.opts["allowed_extractors"] == YOUTUBE_EXTRACTORS
    assert results == [
        {
            "id": "abc123def45",
            "title": "T",
            "url": "https://www.youtube.com/watch?v=abc123def45",
            "channel": "U",
            "duration": "1:05",
            "views": 5,
            "thumbnail": "http://thumb",
        }
    ]


def test_seeding_counts_every_tab_of_a_channel(monkeypatch: Any) -> None:
    class FakeYDL:
        def __init__(self, opts: dict[str, Any]) -> None:
            pass

        def __enter__(self) -> FakeYDL:
            return self

        def __exit__(self, *args: object) -> bool:
            return False

        def extract_info(self, url: str, download: bool = True) -> dict[str, Any]:
            return {"title": "C", "entries": [{"entries": [{"id": "v1"}, {"id": "v2"}]}, {"entries": [{"id": "s1"}, {"id": "s2"}]}]}

    monkeypatch.setattr(inspector.yt_dlp, "YoutubeDL", FakeYDL)
    assert inspector.list_recent_video_ids("u", 2, inspector.LoginSource()) == ("C", ["v1", "v2", "s1", "s2"])


# ---- inspect_link and the login checks, with the real YoutubeDL minus its network requests ----


class OfflineYDL(yt_dlp.YoutubeDL):
    """The real YoutubeDL, so names and format choices are real; only what YouTube would answer is made up."""

    answer: Any = None
    page = ""

    def extract_info(self, url: str, download: bool = True, *args: Any, **kwargs: Any) -> Any:
        answer = OfflineYDL.answer
        if isinstance(answer, Exception):
            raise answer
        if callable(answer):
            return answer(self)
        return self.process_ie_result({"extractor": "youtube", "extractor_key": "Youtube", "webpage_url": url, **answer}, download=False)

    def urlopen(self, request: Any) -> Any:
        return io.BytesIO(OfflineYDL.page.encode("utf-8"))


@pytest.fixture
def offline_ydl(monkeypatch: pytest.MonkeyPatch) -> type[OfflineYDL]:
    monkeypatch.setattr(inspector.yt_dlp, "YoutubeDL", OfflineYDL)
    monkeypatch.setattr(inspector, "get_ffmpeg_status", lambda: (True, "ffmpeg"))
    return OfflineYDL


def link_options(**settings: Any) -> DownloadOptions:
    spec = JobSpec(url="https://youtu.be/abcdefghijk", settings=DownloadSettings(**settings), output_dir="out")
    return options_from_spec(spec, AccessLimits())


def stream(format_id: str, ext: str, vcodec: str, acodec: str, **extra: Any) -> dict[str, Any]:
    return {"format_id": format_id, "url": f"https://x/{format_id}", "ext": ext, "vcodec": vcodec, "acodec": acodec, "filesize": 1000, **extra}


PLAYABLE = {
    "id": "abcdefghijk",
    "title": "Song",
    "duration": 100,
    "formats": [
        stream("137", "mp4", "avc1", "none", height=1080, width=1920),
        stream("140", "m4a", "none", "mp4a.40.2"),
        stream("251", "webm", "none", "opus"),
    ],
}
PRIVATE = "[youtube] abcdefghijk: Private video. Sign in if you have been granted access to this video"


def unplayable(ydl: OfflineYDL) -> Any:
    # What YoutubeIE does when ignore_no_formats_error is set: the reason becomes a warning, and no formats remain.
    if not ydl.params.get("ignore_no_formats_error"):
        raise yt_dlp.utils.DownloadError(f"ERROR: {PRIVATE}")
    ydl.report_warning(PRIVATE)
    ydl.report_warning("No video formats found!")
    return {"id": "abcdefghijk", "title": "youtube video #abcdefghijk", "formats": []}


@pytest.mark.parametrize("media_type", ["video", "audio", "none"])
def test_a_video_that_cannot_be_played_is_reported_as_an_error(offline_ydl: type[OfflineYDL], media_type: str) -> None:
    offline_ydl.answer = unplayable
    report = inspector.inspect_link("https://youtu.be/abcdefghijk", link_options(media_type=media_type))
    assert "私人影片" in report["error"]
    assert summarize_reports([report])[0] == "red"


def test_no_formats_after_a_js_runtime_warning_explains_the_runtime(offline_ydl: type[OfflineYDL]) -> None:
    def without_runtime(ydl: OfflineYDL) -> Any:
        ydl.report_warning("[youtube] No supported JavaScript runtime could be found. Some formats may be missing.")
        ydl.report_warning("No video formats found!")
        return {"id": "abcdefghijk", "title": "Song", "formats": []}

    offline_ydl.answer = without_runtime
    report = inspector.inspect_link("https://youtu.be/abcdefghijk", link_options())
    assert "JavaScript" in report["error"] and "安裝 Deno" in report["error"]
    # A video refused for its own reason keeps only that reason.
    offline_ydl.answer = unplayable
    assert "JavaScript" not in inspector.inspect_link("https://youtu.be/abcdefghijk", link_options())["error"]


def test_an_empty_list_is_not_called_usable() -> None:
    assert summarize_reports([{"error": "", "is_playlist": True, "entries": []}])[0] == "yellow"
    assert summarize_reports([{"error": "", "is_playlist": True, "entries": [{"id": "a"}]}]) == ("green", "連結可用。")  # type: ignore[typeddict-item]


def test_a_channel_root_lists_its_tabs(offline_ydl: type[OfflineYDL]) -> None:
    offline_ydl.answer = lambda ydl: {
        "id": "UC1", "title": "Channel", "playlist_count": 2,
        "entries": [{"id": "videos", "entries": [{"id": "v1", "title": "One"}]}, {"id": "shorts", "entries": [{"id": "s1", "title": "Two"}]}],
    }  # fmt: skip
    report = inspector.inspect_link("https://www.youtube.com/@channel", link_options())
    assert report["is_nested"] is True
    assert report["entry_count"] is None
    assert [entry["id"] for entry in report["entries"]] == ["v1", "s1"]


def test_a_channels_playlists_tab_lists_playlists_to_download_one_by_one(offline_ydl: type[OfflineYDL]) -> None:
    def playlist(number: int) -> dict[str, Any]:
        return {"_type": "url", "ie_key": "YoutubeTab", "id": f"PL{number}", "title": f"P{number}",
                "url": f"https://www.youtube.com/playlist?list=PL{number}"}  # fmt: skip

    offline_ydl.answer = lambda ydl: {"id": "UC1", "title": "C - Playlists", "playlist_count": 3, "entries": [playlist(n) for n in (1, 2, 3)]}
    report = inspector.inspect_link("https://www.youtube.com/@channel/playlists", link_options())
    # Picked rows are then queued as links of their own; as positions they would select videos of the first playlist.
    assert report["is_nested"] is True
    assert [entry["url"] for entry in report["entries"]][:2] == [
        "https://www.youtube.com/playlist?list=PL1",
        "https://www.youtube.com/playlist?list=PL2",
    ]


def test_a_plain_playlist_is_not_mistaken_for_a_list_of_lists(offline_ydl: type[OfflineYDL]) -> None:
    videos = [{"_type": "url", "ie_key": "Youtube", "id": f"video{n:06d}", "url": f"https://www.youtube.com/watch?v=video{n:06d}"} for n in (1, 2)]
    offline_ydl.answer = lambda ydl: {"id": "PL1", "title": "List", "playlist_count": 2, "entries": videos}
    assert inspector.inspect_link("https://www.youtube.com/playlist?list=PL1", link_options())["is_nested"] is False


def flat_video(number: int, **fields: Any) -> dict[str, Any]:
    video_id = f"video{number:06d}"
    return {"_type": "url", "ie_key": "Youtube", "id": video_id, "url": f"https://www.youtube.com/watch?v={video_id}", **fields}


def test_a_playlists_videos_export_to_csv_with_whatever_each_one_has(offline_ydl: type[OfflineYDL]) -> None:
    videos = [
        flat_video(1, title="第一集", duration=65, view_count=1234, channel="頻道", upload_date="20240102"),
        # A flat list rarely says when a video was uploaded; yt-dlp may give a timestamp instead.
        flat_video(2, title="=HYPERLINK(\"http://evil\")", uploader="上傳者", timestamp=1704240000),
        flat_video(3),
    ]
    offline_ydl.answer = lambda ydl: {"id": "PL1", "title": "List", "playlist_count": 3, "entries": videos}
    report = inspector.inspect_link("https://www.youtube.com/playlist?list=PL1", link_options())
    rows = inspector.playlist_export_rows(report["entries"])
    watch = "https://www.youtube.com/watch?v=video00000"
    assert rows == [
        {"標題": "第一集", "影片 ID": "video000001", "網址": f"{watch}1", "長度": "1:05", "上傳日期": "2024-01-02", "觀看數": 1234, "頻道": "頻道"},
        {"標題": "=HYPERLINK(\"http://evil\")", "影片 ID": "video000002", "網址": f"{watch}2", "長度": "", "上傳日期": "2024-01-03", "觀看數": "", "頻道": "上傳者"},
        {"標題": "video000003", "影片 ID": "video000003", "網址": f"{watch}3", "長度": "", "上傳日期": "", "觀看數": "", "頻道": ""},
    ]  # fmt: skip

    data = rows_to_csv(rows)
    assert data.startswith(b"\xef\xbb\xbf")
    lines = data.decode("utf-8-sig").splitlines()
    assert lines[0] == "標題,影片 ID,網址,長度,上傳日期,觀看數,頻道"
    assert lines[1] == f"第一集,video000001,{watch}1,1:05,2024-01-02,1234,頻道"
    # A title is a stranger's text, which a spreadsheet must not run as a formula.
    assert lines[2].startswith("\"'=HYPERLINK(")
    assert lines[3] == f"video000003,video000003,{watch}3,,,,"


def test_errors_during_inspection_are_explained(offline_ydl: type[OfflineYDL]) -> None:
    offline_ydl.answer = yt_dlp.utils.DownloadError("ERROR: [youtube] abcdefghijk: Sign in to confirm you are not a bot")
    report = inspector.inspect_link("https://youtu.be/abcdefghijk", link_options())
    assert "驗證身分" in report["error"]


def test_audio_preview_names_the_converted_file(offline_ydl: type[OfflineYDL]) -> None:
    offline_ydl.answer = PLAYABLE
    report = inspector.inspect_link("https://youtu.be/abcdefghijk", link_options(media_type="audio", audio_format="mp3"))
    assert report["output_name"] == "Song.mp3"


@pytest.mark.parametrize(("audio_format", "extension"), [("best", ".opus"), ("m4a", ".m4a"), ("opus", ".opus")])
def test_original_audio_preview_names_what_the_download_produces(offline_ydl: type[OfflineYDL], audio_format: str, extension: str) -> None:
    offline_ydl.answer = PLAYABLE
    report = inspector.inspect_link("https://youtu.be/abcdefghijk", link_options(media_type="audio", audio_format=audio_format))
    assert report["output_name"] == f"Song{extension}"


def test_sidecar_only_preview_promises_no_video(offline_ydl: type[OfflineYDL]) -> None:
    offline_ydl.answer = PLAYABLE
    report = inspector.inspect_link("https://youtu.be/abcdefghijk", link_options(media_type="none"))
    assert report["error"] == ""
    assert report["selected_format"] == ""
    assert report["selected_size"] == 0
    assert report["output_name"] == ""


def test_clip_preview_names_and_sizes_the_first_clip(offline_ydl: type[OfflineYDL]) -> None:
    offline_ydl.answer = PLAYABLE
    report = inspector.inspect_link("https://youtu.be/abcdefghijk", link_options(time_ranges="0:10-0:20, 0:50-"))
    assert report["output_name"] == "Song [10 秒起].mp4"
    # A tenth of the video, so about a tenth of the 2000 bytes the two streams take.
    assert report["selected_size"] == 200


def write_cookies(path: Path, names: list[str]) -> Path:
    lines = ["# Netscape HTTP Cookie File"]
    lines += [f".youtube.com\tTRUE\t/\tTRUE\t4102444800\t{name}\tvalue" for name in names]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_cookies_without_a_youtube_login_are_named(offline_ydl: type[OfflineYDL], tmp_path: Path) -> None:
    login = inspector.LoginSource(cookie_file=write_cookies(tmp_path / "cookies.txt", ["PREF"]))
    assert inspector.find_login_problem(login) == NO_LOGIN_COOKIES_MESSAGE


def test_a_login_youtube_no_longer_accepts_is_named(offline_ydl: type[OfflineYDL], tmp_path: Path) -> None:
    login = inspector.LoginSource(cookie_file=write_cookies(tmp_path / "cookies.txt", ["LOGIN_INFO", "SAPISID"]))
    assert inspector.find_login_problem(login) == ""
    offline_ydl.page = 'ytcfg.set({"LOGGED_IN":false});'
    assert inspector.check_youtube_login(login) == ("error", EXPIRED_LOGIN_MESSAGE)
    offline_ydl.page = 'ytcfg.set({"LOGGED_IN":true});'
    assert inspector.check_youtube_login(login)[0] == "success"


def test_comment_rows(offline_ydl: type[OfflineYDL]) -> None:
    offline_ydl.answer = lambda ydl: {
        "id": "abcdefghijk",
        "comments": [{"id": "c1", "author": "A", "text": "=1+1", "like_count": None, "_time_text": "1 天前"}],
    }
    rows = inspector.fetch_comments("https://youtu.be/abcdefghijk", inspector.LoginSource())
    assert rows == [{"作者": "A", "留言": "=1+1", "按讚數": 0, "時間": "1 天前", "網址": "https://www.youtube.com/watch?v=abcdefghijk&lc=c1"}]


def test_the_comments_page_and_the_csv_beside_a_download_share_their_columns(offline_ydl: type[OfflineYDL]) -> None:
    import videocap.sidecars as sidecars

    information = {
        "id": "abcdefghijk",
        "comments": [
            {"id": "c1", "author": "A", "text": "=1+1", "like_count": None, "_time_text": "1 天前"},
            {"id": "c2", "author": "@B", "text": "好", "like_count": 3, "time_text": "2 天前"},
        ],
    }
    offline_ydl.answer = lambda ydl: information
    rows = inspector.fetch_comments("https://youtu.be/abcdefghijk", inspector.LoginSource())
    assert inspector.rows_to_csv(rows) == inspector.rows_to_csv(sidecars.comment_rows(information))
    # One mapping for both, so a column added to the CSV beside a download reaches the 留言 page too.
    assert inspector.comment_rows is sidecars.comment_rows


def test_comments_keep_the_chosen_player_client_and_language(offline_ydl: type[OfflineYDL]) -> None:
    from videocap.options import network_options

    seen: list[Any] = []

    def answer(ydl: OfflineYDL) -> Any:
        seen.append(ydl.params["extractor_args"])
        return {"id": "abcdefghijk", "comments": []}

    offline_ydl.answer = answer
    network = network_options(DownloadSettings(player_client="tv", prefer_traditional_metadata=True), AccessLimits())
    inspector.fetch_comments("https://youtu.be/abcdefghijk", inspector.LoginSource(), limit=7, network=network)
    assert seen == [
        {"youtube": {"player_client": ["tv"], "lang": ["zh-TW"], "max_comments": ["7", "all", "all", "0"], "comment_sort": ["top"]}}
    ]


def test_inspection_uses_the_chosen_player_client(offline_ydl: type[OfflineYDL]) -> None:
    seen: list[Any] = []

    def answer(ydl: OfflineYDL) -> Any:
        seen.append(ydl.get_info_extractor("Youtube")._configuration_arg("player_client"))
        return ydl.process_ie_result({"extractor": "youtube", "extractor_key": "Youtube", **PLAYABLE}, download=False)

    offline_ydl.answer = answer
    report = inspector.inspect_link("https://youtu.be/abcdefghijk", link_options(player_client="web_embedded"))
    assert report["error"] == ""
    assert seen == [["web_embedded"]]

from __future__ import annotations

import math

import pytest
import yt_dlp

from videocap.config import YOUTUBE_EXTRACTORS
from videocap.sites import link_kind, site_label, strip_tracking
from videocap.urls import (
    apply_channel_tab,
    classify_youtube_url,
    describe_url_counts,
    extract_video_id,
    format_timestamp,
    parse_date_filter,
    parse_links,
    parse_time_ranges,
    parse_timestamp,
    parse_youtube_urls,
    ParsedLinks,
    url_identity,
    validate_playlist_items,
)


def test_finds_links_and_rejects_lookalikes() -> None:
    urls, rejected = parse_youtube_urls(
        "https://youtu.be/jNQXAC9IVRw youtube.com/watch?v=jNQXAC9IVRw "
        "https://example.com/v.mp4 https://youtube.com@example.com/ https://youtu.be/jNQXAC9IVRw"
    )
    # All three links to the video count as one, whichever way they were written.
    assert urls == ["https://youtu.be/jNQXAC9IVRw"]
    assert rejected == ["https://example.com/v.mp4", "https://youtube.com@example.com/"]


@pytest.mark.parametrize(
    "bad",
    [
        "https://youtube.com.evil.example/watch?v=jNQXAC9IVRw",
        "https://evil.example/youtube.com",
        "https://youtube.com@evil.example/watch?v=jNQXAC9IVRw",
        "https://evil.example\\@youtube.com/watch",
        "ftp://youtube.com/watch?v=jNQXAC9IVRw",
        "javascript://youtube.com/%0aalert(1)",
        "file:///etc/passwd",
        "http://127.0.0.1:8501/",
        "http://169.254.169.254/latest/meta-data/",
    ],
)
def test_only_real_youtube_hosts_pass(bad: str) -> None:
    urls, rejected = parse_youtube_urls(bad)
    assert urls == []
    assert rejected


def test_pulls_links_out_of_a_sentence() -> None:
    urls, rejected = parse_youtube_urls("看這個：https://youtu.be/jNQXAC9IVRw。還有（https://www.youtube.com/watch?v=dQw4w9WgXcQ）很好看！")
    assert urls == ["https://youtu.be/jNQXAC9IVRw", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"]
    assert rejected == []


def test_markdown_link_and_trailing_punctuation() -> None:
    urls, _ = parse_youtube_urls("[video](https://youtu.be/jNQXAC9IVRw). https://youtu.be/dQw4w9WgXcQ!")
    assert urls == ["https://youtu.be/jNQXAC9IVRw", "https://youtu.be/dQw4w9WgXcQ"]


def test_a_line_without_any_link_is_reported() -> None:
    urls, rejected = parse_youtube_urls("not a link at all")
    assert urls == []
    assert rejected == ["not", "a", "link", "at", "all"]


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("jNQXAC9IVRw", "https://www.youtube.com/watch?v=jNQXAC9IVRw"),
        ("@GoogleDevelopers", "https://www.youtube.com/@GoogleDevelopers"),
        ("UC4QobU6STFB0P71PMvOGN5A", "https://www.youtube.com/channel/UC4QobU6STFB0P71PMvOGN5A"),
        ("PLBCF2DAC6FFB574DE", "https://www.youtube.com/playlist?list=PLBCF2DAC6FFB574DE"),
    ],
)
def test_expands_bare_ids(token: str, expected: str) -> None:
    assert parse_youtube_urls(token) == ([expected], [])


def test_ordinary_eleven_letter_words_are_not_video_ids() -> None:
    assert parse_youtube_urls("programming") == ([], ["programming"])


def test_same_video_with_playlist_parameter_is_one_video() -> None:
    urls, _ = parse_youtube_urls("https://www.youtube.com/watch?v=jNQXAC9IVRw&list=PLabc https://youtu.be/jNQXAC9IVRw?t=3")
    assert len(urls) == 1


@pytest.mark.parametrize(
    ("url", "video_id"),
    [
        ("https://youtu.be/jNQXAC9IVRw?si=abc", "jNQXAC9IVRw"),
        ("https://www.youtube.com/watch?v=jNQXAC9IVRw&t=4", "jNQXAC9IVRw"),
        ("https://www.youtube.com/shorts/jNQXAC9IVRw", "jNQXAC9IVRw"),
        ("https://www.youtube.com/live/jNQXAC9IVRw?feature=share", "jNQXAC9IVRw"),
        ("https://www.youtube.com/embed/jNQXAC9IVRw", "jNQXAC9IVRw"),
        ("https://www.youtube.com/playlist?list=PLabc", None),
        ("https://www.youtube.com/@handle", None),
    ],
)
def test_extract_video_id(url: str, video_id: str | None) -> None:
    assert extract_video_id(url) == video_id


def test_classify_and_count_links() -> None:
    assert classify_youtube_url("https://www.youtube.com/playlist?list=PLabc") == "playlist"
    assert classify_youtube_url("https://www.youtube.com/@handle/videos") == "channel"
    assert classify_youtube_url("https://www.youtube.com/results?search_query=a") == "other"
    summary = describe_url_counts(["https://youtu.be/jNQXAC9IVRw", "https://www.youtube.com/@handle", "https://www.youtube.com/@two"])
    assert summary == "1 部影片、2 個頻道"


def test_channel_tab_only_changes_channel_roots() -> None:
    assert apply_channel_tab("https://www.youtube.com/@handle", "shorts") == "https://www.youtube.com/@handle/shorts"
    assert apply_channel_tab("https://www.youtube.com/channel/UCabc/", "videos") == "https://www.youtube.com/channel/UCabc/videos"
    assert apply_channel_tab("https://www.youtube.com/@handle/streams", "shorts") == "https://www.youtube.com/@handle/streams"
    assert apply_channel_tab("https://youtu.be/jNQXAC9IVRw", "shorts") == "https://youtu.be/jNQXAC9IVRw"
    assert apply_channel_tab("https://www.youtube.com/@handle", "all") == "https://www.youtube.com/@handle"


def test_parse_time_ranges() -> None:
    assert parse_time_ranges("1:30-2:00, 90~95；1:00:00 到 1:00:30") == ((90.0, 120.0), (90.0, 95.0), (3600.0, 3630.0))
    start, end = parse_time_ranges("10:00-")[0]
    assert start == 600.0 and math.isinf(end)
    assert parse_time_ranges("-0:30") == ((0.0, 30.0),)
    assert parse_time_ranges("  ") == ()


@pytest.mark.parametrize("bad", ["abc", "5-3", "1:00-1:00", "-", "1:2:3:4-5"])
def test_rejects_bad_time_ranges(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_time_ranges(bad)


def test_format_timestamp() -> None:
    assert format_timestamp(65) == "1:05"
    assert format_timestamp(3665) == "1:01:05"
    assert format_timestamp(float("inf")) == "結尾"


def test_parse_date_filter() -> None:
    assert parse_date_filter("2026-01-31") == "20260131"
    assert parse_date_filter("2026/01/31") == "20260131"
    assert parse_date_filter("20260131") == "20260131"
    assert parse_date_filter("") == ""
    with pytest.raises(ValueError):
        parse_date_filter("yesterday")


def test_validate_playlist_items() -> None:
    assert validate_playlist_items(" 1-5, 8,10 ") == "1-5,8,10"
    assert validate_playlist_items("") == ""
    with pytest.raises(ValueError):
        validate_playlist_items("first five")


VIDEO = "https://youtu.be/jNQXAC9IVRw"


def allowed_extractor_names(url: str) -> list[str]:
    with yt_dlp.YoutubeDL({"allowed_extractors": YOUTUBE_EXTRACTORS, "quiet": True}) as ydl:
        return [key for key, ie in ydl._ies.items() if ie.suitable(url)]


@pytest.mark.parametrize(
    "text",
    [
        f"@Tom 你看 {VIDEO}",
        f"這是 open-source 的教學 {VIDEO}",
        f"我用 iPhone15Pro 拍的 {VIDEO}",
        f"{VIDEO} @everyone 新影片",
    ],
)
def test_words_shaped_like_ids_next_to_a_link_are_not_links(text: str) -> None:
    assert parse_youtube_urls(text) == ([VIDEO], [])


def test_a_line_of_words_and_handles_without_a_link_is_reported() -> None:
    assert parse_youtube_urls("@Tom 你看") == ([], ["@Tom", "你看"])


def test_a_line_of_bare_ids_still_expands() -> None:
    urls, rejected = parse_youtube_urls("jNQXAC9IVRw, dQw4w9WgXcQ")
    assert urls == ["https://www.youtube.com/watch?v=jNQXAC9IVRw", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"]
    assert rejected == []


def test_css_from_a_saved_page_adds_no_links() -> None:
    text = "@media screen { p { font-weight: bold; line-height: 1.4; white-space: nowrap; } }\n" + VIDEO
    assert parse_youtube_urls(text) == ([VIDEO], [])


@pytest.mark.parametrize(
    "text",
    [
        f"我在 youtube.com 看到的 {VIDEO}",
        f"Found this on YouTube.com: {VIDEO}",
        f"用 youtu.be 短網址分享 {VIDEO}",
        f"手機版 m.youtube.com 也可以 {VIDEO}",
        f"這部 4.5 分鐘的影片 {VIDEO}",
    ],
)
def test_a_site_name_in_a_sentence_is_not_a_link(text: str) -> None:
    assert parse_youtube_urls(text) == ([VIDEO], [])


@pytest.mark.parametrize(
    "text", ["youtube.com", "www.youtube.com", "https://www.youtube.com/", "https://youtu.be", "https://m.youtube.com"]
)
def test_a_youtube_home_page_alone_is_reported(text: str) -> None:
    urls, rejected = parse_youtube_urls(text)
    assert urls == []
    assert rejected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Youtu.be/jNQXAC9IVRw", "https://youtu.be/jNQXAC9IVRw"),
        ("HTTPS://WWW.YOUTUBE.COM/watch?v=jNQXAC9IVRw", "https://www.youtube.com/watch?v=jNQXAC9IVRw"),
        ("Https://www.youtube.com/watch?v=jNQXAC9IVRw", "https://www.youtube.com/watch?v=jNQXAC9IVRw"),
        ("Youtube.com/@jawed", "https://youtube.com/@jawed"),
        ("https://YouTube.com/playlist?list=PLBCF2DAC6FFB574DE", "https://youtube.com/playlist?list=PLBCF2DAC6FFB574DE"),
        ("https://www.youtube.com:443/watch?v=jNQXAC9IVRw", "https://www.youtube.com/watch?v=jNQXAC9IVRw"),
        (
            "https://m.youtube.com/clip/UgkxAbCdEfGhIjKlMnOpQrStUvWxYz012345",
            "https://www.youtube.com/clip/UgkxAbCdEfGhIjKlMnOpQrStUvWxYz012345",
        ),
    ],
)
def test_links_are_written_the_way_yt_dlp_recognizes_them(text: str, expected: str) -> None:
    assert parse_youtube_urls(text) == ([expected], [])
    assert allowed_extractor_names(expected)


@pytest.mark.parametrize(
    "text",
    [
        "https://www.youtube.com/watch/jNQXAC9IVRw",
        "https://www.youtube.com/watch?v=",
        "https://www.youtube.com:8080/watch?v=jNQXAC9IVRw",
    ],
)
def test_links_no_extractor_can_handle_are_reported(text: str) -> None:
    assert parse_youtube_urls(text) == ([], [text])


def test_every_accepted_link_has_an_extractor() -> None:
    corpus = (
        "https://youtu.be/jNQXAC9IVRw https://www.youtube.com/shorts/dQw4w9WgXcQ https://www.youtube.com/live/aaaaaaaaaaa\n"
        "https://www.youtube.com/playlist?list=PLBCF2DAC6FFB574DE https://www.youtube.com/@GoogleDevelopers/videos\n"
        "https://www.youtube.com/channel/UC4QobU6STFB0P71PMvOGN5A https://www.youtube.com/@台視新聞 https://music.youtube.com/watch?v=bbbbbbbbbbb\n"
        "https://www.youtube-nocookie.com/embed/ccccccccccc https://www.youtube.com/results?search_query=cats\n"
        "jNQXAC9IVRx @GoogleDevelopers UC4QobU6STFB0P71PMvOGN5B PLBCF2DAC6FFB574DF"
    )
    urls, rejected = parse_youtube_urls(corpus)
    assert rejected == []
    assert len(urls) == 14
    for url in urls:
        assert allowed_extractor_names(url), url


@pytest.mark.parametrize(
    "text",
    [
        f"看這個{VIDEO}",
        f"影片:{VIDEO}",
        f"Link:{VIDEO}",
        f"👉{VIDEO}",
        f"1.{VIDEO}",
        f"請下載 “{VIDEO}” 謝謝",
        f"完整版｜{VIDEO}",
    ],
)
def test_links_glued_to_the_text_before_them(text: str) -> None:
    assert parse_youtube_urls(text) == ([VIDEO], [])


@pytest.mark.parametrize(
    "bad", ["看這個https://youtube.com@evil.example/", "x:https://evil.example/youtube.com", "evil.example/https://youtu.be/jNQXAC9IVRw"]
)
def test_glued_links_still_need_a_real_youtube_host(bad: str) -> None:
    urls, rejected = parse_youtube_urls(bad)
    assert urls == []
    assert rejected


def test_text_glued_after_a_link_is_left_out() -> None:
    urls, rejected = parse_youtube_urls(
        "https://www.youtube.com/playlist?list=PLBCF2DAC6FFB574DE這個清單 https://youtu.be/dQw4w9WgXcQ謝謝 https://www.youtube.com/@台視新聞"
    )
    assert urls == [
        "https://www.youtube.com/playlist?list=PLBCF2DAC6FFB574DE",
        "https://youtu.be/dQw4w9WgXcQ",
        "https://www.youtube.com/@台視新聞",
    ]
    assert rejected == []


def test_protocol_relative_embed_link() -> None:
    assert parse_youtube_urls("//www.youtube.com/embed/jNQXAC9IVRw") == (["https://www.youtube.com/embed/jNQXAC9IVRw"], [])


def test_playlist_embeds_are_playlists() -> None:
    urls, _ = parse_youtube_urls(
        '<iframe src="https://www.youtube.com/embed/videoseries?si=x&amp;list=PLBCF2DAC6FFB574DE"></iframe>\n'
        '<iframe src="https://www.youtube-nocookie.com/embed/videoseries?list=PL6IaIsEjSbf96XFRuNccS_RuEXwNdsoEu"></iframe>'
    )
    assert urls == [
        "https://www.youtube.com/playlist?list=PLBCF2DAC6FFB574DE",
        "https://www.youtube.com/playlist?list=PL6IaIsEjSbf96XFRuNccS_RuEXwNdsoEu",
    ]
    assert describe_url_counts(urls) == "2 個播放清單"


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/embed/videoseries?list=PLBCF2DAC6FFB574DE",
        "https://www.youtube.com/embed/live_stream?channel=UC4QobU6STFB0P71PMvOGN5A",
    ],
)
def test_embed_keywords_are_not_video_ids(url: str) -> None:
    assert extract_video_id(url) is None


def test_live_embeds_of_different_channels_are_kept_apart() -> None:
    embed = "https://www.youtube.com/embed/live_stream?channel="
    # Channel IDs are case-sensitive, so these two name different channels.
    channels = ["UC4QobU6STFB0P71PMvOGN5A", "UCsXVk37bltHxD1rDPwtNM8Q", "UC4qobu6stfb0p71pmvogn5a"]
    urls, rejected = parse_youtube_urls("\n".join(f"{embed}{channel}" for channel in [*channels, channels[0]]))
    assert urls == [f"{embed}{channel}" for channel in channels]
    assert rejected == []
    assert not any(url_identity(url).startswith("video:") for url in urls)


@pytest.mark.parametrize(
    "text",
    [
        "https://www.youtube.com/embed/jNQXAC9IVRw?list=PL6IaIsEjSbf96XFRuNccS_RuEXwNdsoEu",
        "https://youtu.be/jNQXAC9IVRw?list=PL6IaIsEjSbf96XFRuNccS_RuEXwNdsoEu",
    ],
)
def test_video_links_with_a_playlist_become_watch_links(text: str) -> None:
    expected = "https://www.youtube.com/watch?v=jNQXAC9IVRw&list=PL6IaIsEjSbf96XFRuNccS_RuEXwNdsoEu"
    assert parse_youtube_urls(f"{text}\nhttps://www.youtube.com/watch?v=jNQXAC9IVRw") == ([expected], [])
    assert allowed_extractor_names(expected) == ["YoutubeTab"]


def test_an_embedded_video_in_a_playlist_downloads_only_that_video_by_default() -> None:
    url = parse_youtube_urls("https://www.youtube.com/embed/jNQXAC9IVRw?list=PL6IaIsEjSbf96XFRuNccS_RuEXwNdsoEu")[0][0]
    # Decided from the link alone, so this needs no network.
    with yt_dlp.YoutubeDL({"allowed_extractors": YOUTUBE_EXTRACTORS, "noplaylist": True, "quiet": True}) as ydl:
        result = ydl.extract_info(url, download=False, process=False)
    assert result["url"] == "https://www.youtube.com/watch?v=jNQXAC9IVRw"


def test_a_playlist_request_is_kept_apart_when_playlists_are_expanded() -> None:
    text = (
        "https://youtu.be/jNQXAC9IVRw\n"
        "https://www.youtube.com/watch?v=jNQXAC9IVRw&list=PLaaaaaaaaaaaa\n"
        "https://www.youtube.com/watch?v=jNQXAC9IVRw&list=PLbbbbbbbbbbbb"
    )
    assert len(parse_youtube_urls(text)[0]) == 1
    urls, _ = parse_youtube_urls(text, expand_playlist=True)
    assert len(urls) == 3
    assert describe_url_counts(urls, expand_playlist=True) == "1 部影片、2 個播放清單"
    assert describe_url_counts(urls) == "3 部影片"


def test_prose_lines_do_not_block_the_links_pasted_with_them() -> None:
    assert parse_youtube_urls(f"推薦這部影片\n{VIDEO}\nnot a link at all") == ([VIDEO], [])


def test_a_mistyped_id_on_its_own_line_is_still_reported() -> None:
    assert parse_youtube_urls(f"{VIDEO}\njNQXAC9IVR") == ([VIDEO], ["jNQXAC9IVR"])


def test_one_channel_written_several_ways_is_one_link() -> None:
    urls, _ = parse_youtube_urls(
        "https://www.youtube.com/@GoogleDevelopers https://youtube.com/@GoogleDevelopers "
        "https://m.youtube.com/@GoogleDevelopers https://www.youtube.com/@GoogleDevelopers/ "
        "https://www.youtube.com/@GoogleDevelopers?si=abc&feature=shared"
    )
    assert urls == ["https://www.youtube.com/@GoogleDevelopers"]
    # Different tabs and searches stay apart.
    urls, _ = parse_youtube_urls(
        "https://www.youtube.com/@GoogleDevelopers/videos https://www.youtube.com/@GoogleDevelopers/shorts "
        "https://www.youtube.com/results?search_query=a https://www.youtube.com/results?search_query=b"
    )
    assert len(urls) == 4


@pytest.mark.parametrize("text", ["1：30-2：00", "1:30–2:00", "1:30—2:00", "1:30－2:00", "1:30〜2:00"])
def test_time_ranges_typed_with_chinese_punctuation(text: str) -> None:
    assert parse_time_ranges(text) == ((90.0, 120.0),)


def test_single_timestamp_accepts_a_full_width_colon() -> None:
    assert parse_timestamp("1：30") == 90.0


@pytest.mark.parametrize("bad", ["1:75-2:00", "0:30-1:60", "1:60:00-2:00:00"])
def test_minutes_and_seconds_stop_at_59(bad: str) -> None:
    with pytest.raises(ValueError, match="59"):
        parse_time_ranges(bad)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("https://studio.youtube.com/video/jNQXAC9IVRw/edit", "https://www.youtube.com/watch?v=jNQXAC9IVRw"),
        ("studio.youtube.com/video/jNQXAC9IVRw/analytics/tab-overview/period-default", "https://www.youtube.com/watch?v=jNQXAC9IVRw"),
        ("https://www.youtubekids.com/watch?v=jNQXAC9IVRw", "https://www.youtube.com/watch?v=jNQXAC9IVRw"),
        ("https://youtubekids.com/channel/UC4QobU6STFB0P71PMvOGN5A", "https://www.youtube.com/channel/UC4QobU6STFB0P71PMvOGN5A"),
    ],
)
def test_studio_and_kids_links(text: str, expected: str) -> None:
    assert parse_youtube_urls(text) == ([expected], [])


@pytest.mark.parametrize(
    "bad",
    [
        "https://studio.youtube.com/channel/UC4QobU6STFB0P71PMvOGN5A/videos",
        "https://studio.youtube.com.evil.example/video/jNQXAC9IVRw/edit",
    ],
)
def test_other_studio_pages_are_not_videos(bad: str) -> None:
    assert parse_youtube_urls(bad) == ([], [bad])


# Other sites

WATCH_LINK = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
SIGNED_LINK = "https://cdn.example.com/v/clip.mp4?Expires=1700000000&Signature=a,b;c(d)~e%2Ff&Key-Pair-Id=K[1]|2"


def owner_links(text: str, **options: bool) -> ParsedLinks:
    return parse_links(text, other_sites=True, **options)


def test_the_owner_gets_links_to_other_sites_and_visitors_do_not() -> None:
    text = f"{WATCH_LINK}\nhttps://vimeo.com/76979871\n{SIGNED_LINK}"
    assert owner_links(text).urls == [WATCH_LINK, "https://vimeo.com/76979871", SIGNED_LINK]
    visitor = parse_links(text)
    assert visitor.urls == [WATCH_LINK]
    assert visitor.blocked == ["https://vimeo.com/76979871", SIGNED_LINK]
    assert visitor.rejected == []
    # The YouTube-only reader the subscriptions page uses is unchanged.
    assert parse_youtube_urls(text) == ([WATCH_LINK], ["https://vimeo.com/76979871", "https://cdn.example.com/v/clip.mp4?Expires=1700000000&Signature=a"])


def test_a_signed_link_keeps_every_character_of_its_parameters() -> None:
    # Commas, semicolons, brackets and bars are where YouTube's own rules split text.
    assert owner_links(SIGNED_LINK).urls == [SIGNED_LINK]
    assert owner_links(f"下載 {SIGNED_LINK} 謝謝").urls == [SIGNED_LINK]


@pytest.mark.parametrize(
    ("link", "cleaned"),
    [
        ("https://example.com/a.mp4?utm_source=x&id=7&utm_medium=y", "https://example.com/a.mp4?id=7"),
        ("https://example.com/a.mp4?fbclid=abc&token=A%2FB+C&gclid=1&igshid=2", "https://example.com/a.mp4?token=A%2FB+C"),
        ("https://example.com/a.mp4?UTM_Campaign=x", "https://example.com/a.mp4"),
        # These three are YouTube's trackers; another site may mean something else by them.
        ("https://example.com/watch?si=1&feature=share&pp=2", "https://example.com/watch?si=1&feature=share&pp=2"),
        ("https://example.com/a.mp4?b=2&a=1#t=30", "https://example.com/a.mp4?b=2&a=1#t=30"),
        ("https://example.com/a.mp4", "https://example.com/a.mp4"),
    ],
)
def test_only_known_click_trackers_are_removed_from_other_sites_links(link: str, cleaned: str) -> None:
    assert strip_tracking(link) == cleaned
    assert owner_links(link).urls == [cleaned]


@pytest.mark.parametrize(
    "text",
    [
        "https://example.com/a.mp4，請看",
        "https://example.com/a.mp4請看這個",
        "看這個https://example.com/a.mp4。",
        "影片（https://example.com/a.mp4）很棒",
        "「https://example.com/a.mp4」",
        "https://example.com/a.mp4　下一個",
        "(https://example.com/a.mp4).",
        "[影片](https://example.com/a.mp4)",
        '<a href="https://example.com/a.mp4">影片</a>',
        "see https://example.com/a.mp4, then reply",
        "'https://example.com/a.mp4'",
        "`https://example.com/a.mp4`",
    ],
)
def test_another_sites_link_ends_where_the_text_around_it_begins(text: str) -> None:
    assert owner_links(text).urls == ["https://example.com/a.mp4"]


def test_brackets_that_belong_to_a_link_stay() -> None:
    link = "https://example.com/wiki/Clip_(2024).mp4"
    assert owner_links(f"({link})").urls == [link]
    assert owner_links(link).urls == [link]


def test_links_in_a_list_without_spaces_are_separate_links() -> None:
    found = owner_links(f"https://vimeo.com/1,https://vimeo.com/2;{WATCH_LINK}|https://example.com/b.mp4")
    assert found.urls == ["https://vimeo.com/1", "https://vimeo.com/2", "https://example.com/b.mp4", WATCH_LINK]


@pytest.mark.parametrize("text", ["example.com/a.mp4", "vimeo.com/76979871", "www.example.com/video/1"])
def test_other_sites_need_the_protocol_written_out(text: str) -> None:
    # A site's name in a sentence would otherwise become something to download.
    found = owner_links(text)
    assert found.urls == [] and found.rejected == [text]


@pytest.mark.parametrize(
    "bad",
    [
        "https://www.youtube.com@evil.example/watch?v=jNQXAC9IVRw",
        "https://user:secret@example.com/a.mp4",
        "https://example.com\\@www.youtube.com/a.mp4",
        "https:///a.mp4",
        "https://example.com:port/a.mp4",
    ],
)
def test_a_link_that_hides_its_site_is_refused_for_everyone(bad: str) -> None:
    for found in (owner_links(bad), parse_links(bad)):
        assert found.urls == [] and found.blocked == [] and found.rejected


def test_other_protocols_are_not_links_to_download() -> None:
    found = owner_links("ftp://example.com/a.mp4 file:///C:/a.mp4")
    assert found.urls == [] and len(found.rejected) == 2


def test_a_page_alone_is_tried_and_among_video_links_it_waits() -> None:
    page, article = "https://example.com/watch/page.html", "https://news.example.org/story?id=5"
    alone = owner_links(f"{page}\n{article}")
    assert alone.urls == [page, article] and alone.pages == []
    # The other links of a pasted video description are rarely videos.
    mixed = owner_links(f"{WATCH_LINK}\n贊助商：{page}\n{article}")
    assert mixed.urls == [WATCH_LINK] and mixed.pages == [page, article]
    assert owner_links(f"{WATCH_LINK}\n{page}\n{article}", include_pages=True).urls == [WATCH_LINK, page, article]
    # A media file or a site yt-dlp knows is not a guess.
    assert owner_links(f"{page}\nhttps://example.com/a.m3u8").pages == [page]
    assert owner_links(f"{page}\nhttps://vimeo.com/76979871").pages == [page]


def test_services_that_encrypt_everything_are_told_apart() -> None:
    found = owner_links("https://www.netflix.com/watch/80192098\nhttps://www.disneyplus.com/video/abc\nhttps://vimeo.com/1")
    assert found.protected == ["https://www.netflix.com/watch/80192098", "https://www.disneyplus.com/video/abc"]
    assert found.urls == ["https://vimeo.com/1"]
    # A visitor is only told that other sites are not for them.
    assert len(parse_links("https://www.netflix.com/watch/80192098").blocked) == 1


def test_the_same_link_to_another_site_is_one_link() -> None:
    found = owner_links("https://vimeo.com/76979871?utm_source=a\nhttps://VIMEO.com/76979871\nhttps://vimeo.com/76979871?fbclid=x")
    assert found.urls == ["https://vimeo.com/76979871"]
    # Parameters that are not trackers may name another video.
    assert len(owner_links("https://example.com/watch?id=1\nhttps://example.com/watch?id=2").urls) == 2


def test_youtube_rules_stop_at_youtube() -> None:
    # Other sites have /watch?v=, ?list= and /@name pages too.
    assert extract_video_id("https://example.com/watch?v=jNQXAC9IVRw") is None
    assert extract_video_id("https://example.com/embed/jNQXAC9IVRw") is None
    assert classify_youtube_url("https://www.tiktok.com/@someone") == "other"
    assert classify_youtube_url("https://example.com/playlist?list=PLabcdefghijkl") == "other"
    assert apply_channel_tab("https://www.tiktok.com/@someone", "videos") == "https://www.tiktok.com/@someone"
    assert url_identity("https://a.example/watch?v=jNQXAC9IVRw") != url_identity("https://b.example/watch?v=jNQXAC9IVRw")
    assert url_identity("https://a.example/x?list=PL1") != url_identity("https://a.example/y?list=PL1")
    assert url_identity("https://a.example/x?list=PL1", expand_playlist=True) != url_identity("https://b.example/x?list=PL1", expand_playlist=True)
    # Two test sites on one machine differ only by their port.
    assert url_identity("http://127.0.0.1:8001/clip.mp4") != url_identity("http://127.0.0.1:8002/clip.mp4")


def test_counts_name_the_other_sites() -> None:
    urls = [WATCH_LINK, "https://vimeo.com/1", "https://vimeo.com/2", "https://example.com/a.mp4", "https://example.com/page"]
    assert describe_url_counts(urls) == "1 部影片、Vimeo 2 個連結、example.com（直接連結） 1 個連結、example.com（一般網頁） 1 個連結"


@pytest.mark.parametrize(
    ("url", "kind"),
    [
        (WATCH_LINK, "youtube"),
        ("https://youtu.be/jNQXAC9IVRw", "youtube"),
        ("https://vimeo.com/76979871", "known"),
        ("https://www.bilibili.com/video/BV1xx411c7mD", "known"),
        ("https://x.com/someone/status/1234567890", "known"),
        ("https://example.com/a/clip.MP4?sig=1", "media"),
        ("https://example.com/live/index.m3u8", "media"),
        ("https://example.com/watch/page.html", "page"),
        ("https://www.netflix.com/watch/80192098", "protected"),
        ("https://www.disneyplus.com/video/abc", "protected"),
    ],
)
def test_link_kinds(url: str, kind: str) -> None:
    assert link_kind(url) == kind


@pytest.mark.parametrize(
    ("extractor", "url", "label"),
    [
        ("Youtube", WATCH_LINK, "YouTube"),
        ("YoutubeTab", "https://www.youtube.com/@jawed", "YouTube"),
        ("Vimeo", "https://vimeo.com/1", "Vimeo"),
        ("BiliBili", "https://www.bilibili.com/video/BV1", "Bilibili"),
        ("Twitter", "https://x.com/a/status/1", "X（Twitter）"),
        ("TwitchVod", "https://www.twitch.tv/videos/1", "Twitch"),
        ("TedTalk", "https://www.ted.com/talks/x", "TedTalk"),
        ("Generic", "https://cdn.example.com/a.mp4?sig=1", "cdn.example.com（直接連結）"),
        ("HTML5MediaEmbed", "https://example.com/page.html", "example.com（一般網頁）"),
        ("Generic", "https://example.com/page.html", "example.com（一般網頁）"),
    ],
)
def test_site_labels(extractor: str, url: str, label: str) -> None:
    assert site_label(extractor, url) == label

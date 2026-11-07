from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yt_dlp

from offline_youtube import OfflineYouTube, duration_of, offline_media_fixture, offline_youtube_fixture, probe, stream_codecs  # noqa: F401
from videocap.config import CLOUD_MAX_JOB_SECONDS, LOGGED_IN_MIN_SLEEP_SECONDS, NAMING_TEMPLATE, YOUTUBE_EXTRACTORS
from videocap.downloader import YtDlpLogCollector
from videocap.options import (
    AccessLimits,
    DownloadOptions,
    PICKED_VIDEOS_SCAN_ITEMS,
    EntryFilters,
    LoginSource,
    build_base_options,
    build_early_postprocessors,
    build_format_options,
    build_late_postprocessors,
    build_output_template,
    build_ydl_options,
    entry_filter_reason,
    network_options,
    options_from_spec,
)
from videocap.settings import BUILTIN_PRESETS, DownloadSettings, JobSpec, job_spec_from_dict, settings_from_dict


def make_options(**overrides: object) -> DownloadOptions:
    defaults: dict[str, object] = {"output_dir": Path("out"), "output_template": "%(title)s.%(ext)s"}
    return DownloadOptions(**{**defaults, **overrides})  # type: ignore[arg-type]


def spec_options(limits: AccessLimits | None = None, **settings: object) -> DownloadOptions:
    spec = JobSpec(url="https://youtu.be/jNQXAC9IVRw", settings=DownloadSettings(**settings), output_dir="out")  # type: ignore[arg-type]
    return options_from_spec(spec, limits or AccessLimits())


def test_video_prefers_h264_mp4_under_the_quality_cap() -> None:
    options = build_format_options(make_options(max_height=1080), ffmpeg_available=True)
    assert options["format_sort"] == ["res:1080", "fps", "hdr:sdr", "vcodec:h264", "acodec:aac"]
    assert options["format"] == "bv*+ba/b"
    assert options["merge_output_format"] == "mp4"


def test_without_ffmpeg_only_single_files_are_requested() -> None:
    options = build_format_options(make_options(), ffmpeg_available=False)
    assert options["format"] == "b[ext=mp4]/b"
    assert "merge_output_format" not in options


@pytest.mark.parametrize(
    ("codec", "expected"),
    [("best", []), ("vp9", ["vcodec:vp9", "acodec:opus"]), ("av1", ["vcodec:av01", "acodec:opus"])],
)
def test_codec_choice_changes_the_sort(codec: str, expected: list[str]) -> None:
    assert build_format_options(make_options(video_codec=codec), True)["format_sort"] == ["res", "fps", "hdr:sdr", *expected]


def test_hdr_outranks_codec() -> None:
    assert build_format_options(make_options(prefer_hdr=True), True)["format_sort"][2] == "hdr:12"


def test_webm_container_avoids_h264_and_can_fall_back() -> None:
    options = spec_options(container="webm")
    assert options.video_codec == "vp9"
    assert build_format_options(options, True)["merge_output_format"] == "webm/mkv"


def test_audio_language_selects_a_dubbed_track_with_fallback() -> None:
    video = build_format_options(make_options(audio_language="ja"), True)
    assert video["format"] == "bv*+ba[language^=ja]/bv*+ba/b"
    audio = build_format_options(make_options(media_type="audio", audio_format="mp3", audio_language="ja"), True)
    assert audio["format"] == "ba[language^=ja]/ba/b"


def test_audio_language_cannot_inject_into_the_selector() -> None:
    options = build_format_options(make_options(audio_language="ja]/b[url!=x"), True)
    assert options["format"] == "bv*+ba/b"


@pytest.mark.parametrize(
    ("audio_format", "selector"),
    [
        ("m4a", "ba[ext=m4a]/ba/b"),
        ("opus", "ba[acodec^=opus]/ba/b"),
        ("mp3", "ba/b"),
        ("best", "ba/b"),
    ],
)
def test_audio_source_selection(audio_format: str, selector: str) -> None:
    assert build_format_options(make_options(media_type="audio", audio_format=audio_format), True)["format"] == selector


def test_audio_without_ffmpeg_falls_back_to_m4a_and_skips_conversion() -> None:
    options = make_options(media_type="audio", audio_format="mp3")
    assert build_format_options(options, False)["format"] == "ba[ext=m4a]/ba/b"
    assert build_early_postprocessors(options, False) == []


def test_bitrate_only_applies_where_the_audio_is_converted() -> None:
    assert spec_options(media_type="audio", audio_format="flac", audio_bitrate="320").audio_bitrate == "0"
    assert spec_options(media_type="audio", audio_format="mp3", audio_bitrate="320").audio_bitrate == "320"
    # M4A and Opus are YouTube's own audio, kept as it is, so a bitrate would change nothing.
    for audio_format in ("m4a", "opus"):
        assert spec_options(media_type="audio", audio_format=audio_format, audio_bitrate="128").audio_bitrate == "0"
    from videocap.options import BITRATE_AUDIO_FORMATS

    assert BITRATE_AUDIO_FORMATS == frozenset({"mp3"})


def test_sidecar_only_mode_skips_the_download() -> None:
    options = build_format_options(make_options(media_type="none"), True)
    assert options == {"skip_download": True, "ignore_no_formats_error": True}
    assert build_late_postprocessors(make_options(media_type="none", embed_metadata=True), True) == []


def test_explicit_format_wins() -> None:
    options = build_format_options(make_options(format_override="137+140", container="mkv"), True)
    assert options == {"format": "137+140", "merge_output_format": "mkv"}


@pytest.mark.parametrize(
    ("container", "target"),
    [("mkv", "mkv"), ("webm", "webm>webm/mkv")],
)
def test_single_file_videos_are_remuxed_into_the_chosen_container(container: str, target: str) -> None:
    steps = build_early_postprocessors(make_options(container=container), True)
    assert steps == [{"key": "FFmpegVideoRemuxer", "preferedformat": target}]


def test_mp4_and_chosen_formats_are_not_remuxed() -> None:
    assert build_early_postprocessors(make_options(container="mp4"), True) == []
    assert build_early_postprocessors(make_options(container="mkv", format_override="18"), True) == []
    assert build_early_postprocessors(make_options(container="mkv"), False) == []


def test_final_extension_lets_yt_dlp_recognize_a_finished_file() -> None:
    assert build_ydl_options(make_options(media_type="audio", audio_format="mp3"), True, YtDlpLogCollector())["final_ext"] == "mp3"
    assert build_ydl_options(make_options(container="mkv"), True, YtDlpLogCollector())["final_ext"] == "mkv"
    assert "final_ext" not in build_ydl_options(make_options(media_type="audio", audio_format="best"), True, YtDlpLogCollector())
    assert "final_ext" not in build_ydl_options(make_options(), True, YtDlpLogCollector())
    assert "final_ext" not in build_ydl_options(make_options(media_type="audio", audio_format="mp3"), False, YtDlpLogCollector())


def test_late_steps_follow_yt_dlp_order() -> None:
    steps = build_late_postprocessors(
        make_options(
            download_subtitles=True,
            embed_subtitles=True,
            sponsorblock_mode="remove",
            sponsorblock_categories=("sponsor",),
            embed_chapters=True,
            embed_metadata=True,
            embed_thumbnail=True,
            split_chapters=True,
        ),
        True,
    )
    assert [step["key"] for step in steps] == ["FFmpegEmbedSubtitle", "ModifyChapters", "FFmpegMetadata", "EmbedThumbnail", "FFmpegSplitChapters"]
    assert steps[1]["remove_sponsor_segments"] == {"sponsor"}


def test_marking_sponsors_removes_nothing_but_adds_chapters() -> None:
    steps = build_late_postprocessors(make_options(sponsorblock_mode="mark", sponsorblock_categories=("sponsor",)), True)
    assert steps[0] == {"key": "ModifyChapters", "remove_sponsor_segments": set(), "force_keyframes": False, "sponsorblock_chapter_title": "%(name)s"}
    assert steps[1]["add_chapters"] is True


def test_no_ffmpeg_means_no_late_steps() -> None:
    assert build_late_postprocessors(make_options(embed_metadata=True, embed_chapters=True), False) == []


def test_ydl_options_are_accepted_by_yt_dlp() -> None:
    options = spec_options(
        media_type="audio",
        audio_format="mp3",
        sponsorblock_mode="mark",
        embed_thumbnail=True,
        write_thumbnail=True,
        write_description=True,
        write_infojson=True,
        rate_limit_mb=2.5,
        force_ipv4=True,
    )
    ydl_opts = build_ydl_options(options, True, YtDlpLogCollector())
    assert ydl_opts["ratelimit"] == int(2.5 * 1024 * 1024)
    assert ydl_opts["source_address"] == "0.0.0.0"
    assert ydl_opts["allowed_extractors"] == YOUTUBE_EXTRACTORS
    with yt_dlp.YoutubeDL(ydl_opts):  # type: ignore[arg-type]
        pass


def test_auto_captions_are_off_with_all_languages() -> None:
    ydl_opts = build_ydl_options(
        make_options(download_subtitles=True, subtitle_languages=("all",), include_auto_subtitles=True), True, YtDlpLogCollector()
    )
    assert ydl_opts["writeautomaticsub"] is False
    assert ydl_opts["subtitleslangs"] == ["all", "-live_chat"]
    assert ydl_opts["subtitlesformat"] == "srt/best"


def test_logged_in_downloads_keep_their_distance() -> None:
    ydl_opts = build_ydl_options(make_options(login=LoginSource(browser="firefox")), True, YtDlpLogCollector())
    assert ydl_opts["cookiesfrombrowser"] == ("firefox",)
    # The app waits itself, so a stop can interrupt it.
    assert "sleep_interval" not in ydl_opts
    spec = JobSpec(url="u", settings=DownloadSettings(), output_dir="out", browser="firefox")
    assert options_from_spec(spec, AccessLimits()).wait_range == (LOGGED_IN_MIN_SLEEP_SECONDS, 10)


def test_public_visitors_cannot_hold_a_slot_by_waiting() -> None:
    assert spec_options(AccessLimits.for_public_host(), sleep_seconds=3600).wait_range == (10, 10)
    assert spec_options(sleep_seconds=3600).wait_range == (3600, 3600)


def test_login_spacing_leaves_a_public_job_most_of_its_time() -> None:
    # The waits count toward a public job's time limit, because the job holds its download slot and the one login
    # slot while it waits. Each video is waited for at most twice: once before it, and once before its 403 retry.
    spec = JobSpec(url="u", settings=DownloadSettings(sleep_seconds=3600), output_dir="out", needs_cookie_file=True)
    limits = AccessLimits.for_public_host()
    options = options_from_spec(spec, limits, cookie_file=Path("cookies.txt"))
    assert options.wait_range == (10, 10)
    assert 2 * limits.max_playlist * options.wait_range[1] <= CLOUD_MAX_JOB_SECONDS / 4


def test_clips_do_not_use_up_the_download_limit() -> None:
    options = spec_options(time_ranges="0:01-0:02,0:03-0:04,0:05-0:06", playlist_limit=1)
    ydl_opts = build_ydl_options(options, True, YtDlpLogCollector())
    assert ydl_opts["max_downloads"] == 3
    assert "download_ranges" in ydl_opts


def test_clips_need_ffmpeg() -> None:
    ydl_opts = build_ydl_options(make_options(time_ranges=((1.0, 2.0),)), False, YtDlpLogCollector())
    assert "download_ranges" not in ydl_opts


def test_clips_disable_chapter_features_that_would_not_line_up() -> None:
    options = spec_options(time_ranges="0:01-0:02", embed_chapters=True, embed_subtitles=True, split_chapters=True, sponsorblock_mode="remove")
    assert not options.embed_chapters and not options.split_chapters
    assert not options.embed_subtitles
    assert options.sponsorblock_mode == "off"


def test_a_chosen_format_or_chapter_overrides_the_sidebars_type() -> None:
    for sidebar_type in ("audio", "none"):
        spec = JobSpec(url="u", settings=DownloadSettings(media_type=sidebar_type), output_dir="out", format_override="140")
        options = options_from_spec(spec, AccessLimits())
        assert options.media_type == "video"
        assert build_early_postprocessors(options, True) == []

    spec = JobSpec(url="u", settings=DownloadSettings(media_type="none"), output_dir="out", time_ranges="0:01-0:02")
    assert options_from_spec(spec, AccessLimits()).media_type == "video"
    spec = JobSpec(url="u", settings=DownloadSettings(media_type="audio"), output_dir="out", time_ranges="0:01-0:02")
    assert options_from_spec(spec, AccessLimits()).media_type == "audio"


def test_invalid_settings_raise_readable_errors() -> None:
    with pytest.raises(ValueError, match="看不懂時間範圍"):
        spec_options(time_ranges="soon")
    with pytest.raises(ValueError, match="看不懂項目範圍"):
        spec_options(playlist_items="first")
    with pytest.raises(ValueError, match="看不懂日期"):
        spec_options(filter_date_after="last week")


def test_public_visitors_cannot_steer_the_server() -> None:
    limits = AccessLimits.for_public_host()
    options = spec_options(
        limits,
        proxy="http://attacker.example:1",
        naming_rule=NAMING_TEMPLATE,
        custom_template="../../etc/%(title)s.%(ext)s",
        folder_rule="channel",
        skip_archived=True,
        live_mode="record",
        playlist_limit=5000,
    )
    assert options.network.proxy == ""
    assert ".." not in options.output_template
    assert "%(channel" not in options.output_template
    assert options.archive_path is None
    assert options.live_mode == "skip"
    assert options.playlist_limit == limits.max_playlist
    assert options.max_bytes == limits.max_file_bytes


def test_trusted_visitors_keep_those_settings() -> None:
    spec = JobSpec(
        url="https://youtu.be/jNQXAC9IVRw",
        settings=DownloadSettings(proxy="http://proxy.local:3128", folder_rule="channel", skip_archived=True, live_mode="record"),
        output_dir="out",
    )
    options = options_from_spec(spec, AccessLimits(), archive_path=Path("archive.txt"))
    assert options.network.proxy == "http://proxy.local:3128"
    assert options.output_template.startswith("%(channel,uploader|未知頻道).80B/")
    assert options.archive_path == Path("archive.txt")
    assert options.live_mode == "record"


def test_resuming_never_overwrites() -> None:
    spec = JobSpec(url="u", settings=DownloadSettings(overwrite_existing=True), output_dir="out")
    assert options_from_spec(spec, AccessLimits()).overwrite_existing is True
    assert options_from_spec(spec.as_resuming(), AccessLimits()).overwrite_existing is False


def test_output_templates() -> None:
    assert build_output_template("沿用原始檔名", "", False) == "%(playlist_index&{:02d} - |)s%(title).150B%(videocap_id_suffix|)s.%(ext)s"
    assert " [%(id)s]" in build_output_template("沿用原始檔名", "", True)
    assert "_%(upload_date)s" in build_output_template("原始檔名加日期", "", False)
    assert build_output_template("自訂名稱", "我的 影片/../x", False).startswith("我的_影片_.._x_%(id)s")
    assert build_output_template("自訂名稱", "  ", False).startswith("youtube_video_")
    assert "秒起" in build_output_template("沿用原始檔名", "", False, has_sections=True)
    assert build_output_template(NAMING_TEMPLATE, "", False, custom_template="%(id)s.%(ext)s") == "%(id)s.%(ext)s"


def test_output_template_works_in_yt_dlp() -> None:
    template = build_output_template("沿用原始檔名", "", False, has_sections=True)
    with yt_dlp.YoutubeDL({"quiet": True, "outtmpl": template}) as ydl:  # type: ignore[arg-type]
        info = {"id": "abc", "title": "T", "ext": "mp4", "section_start": 90.0}
        assert ydl.prepare_filename(info) == "T [90 秒起].mp4"
        # Only a name already taken by another video gets the ID.
        assert ydl.prepare_filename({**info, "videocap_id_suffix": " [abc]"}) == "T [abc] [90 秒起].mp4"


def test_entry_filters() -> None:
    entry = {"title": "Cat video", "duration": 120, "view_count": 50, "upload_date": "20260115"}
    assert entry_filter_reason(entry, False, EntryFilters()) is None
    assert entry_filter_reason(entry, False, EntryFilters(min_seconds=300))
    assert entry_filter_reason(entry, False, EntryFilters(max_seconds=60))
    assert entry_filter_reason(entry, False, EntryFilters(min_views=100))
    assert entry_filter_reason(entry, False, EntryFilters(date_after="20260201"))
    assert entry_filter_reason(entry, False, EntryFilters(date_before="20260101"))
    assert entry_filter_reason(entry, False, EntryFilters(date_after="20260101", date_before="20260201")) is None
    assert entry_filter_reason(entry, False, EntryFilters(title_include="dog"))
    assert entry_filter_reason(entry, False, EntryFilters(title_include="cat")) is None
    assert entry_filter_reason(entry, False, EntryFilters(title_exclude="CAT"))


def test_retry_delay_accepts_the_keyword_yt_dlp_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    import time as time_module

    from yt_dlp.utils import RetryManager

    from videocap.options import _retry_delay

    slept: list[float] = []
    monkeypatch.setattr(time_module, "sleep", slept.append)
    RetryManager.report_retry(OSError("reset"), 2, 3, sleep_func=_retry_delay, info=lambda message: None, warn=lambda message: None)
    assert slept == [2.0]
    assert build_base_options(YtDlpLogCollector(), 3, LoginSource())["retry_sleep_functions"]["http"](n=10) == 30.0


def test_public_title_filters_are_plain_text() -> None:
    import time as time_module

    options = spec_options(AccessLimits.for_public_host(), filter_title_exclude="(.+)+QQQ")
    assert options.filters.title_is_regex is False
    started = time_module.perf_counter()
    # Long enough to take seconds as a regular expression, short enough that a regression fails rather than hangs.
    assert entry_filter_reason({"title": "a" * 24}, False, options.filters) is None
    assert time_module.perf_counter() - started < 0.1
    assert spec_options(filter_title_exclude="(.+)+QQQ").filters.title_is_regex is True


def test_public_subtitle_languages_are_codes_only() -> None:
    options = spec_options(AccessLimits.for_public_host(), subtitle_languages="en, zh.*, -live_chat, (a|aa)+$, all")
    assert options.subtitle_languages == ("en", "zh.*", "-live_chat", "all")
    assert spec_options(subtitle_languages="(a|aa)+$").subtitle_languages == ("(a|aa)+$",)


def test_sidecar_only_downloads_are_archived_by_the_app_not_by_force() -> None:
    options = make_options(media_type="none", archive_path=Path("archive.txt"))
    ydl_opts = build_ydl_options(options, True, YtDlpLogCollector())
    assert ydl_opts["download_archive"] == "archive.txt"
    assert "force_write_download_archive" not in ydl_opts


def test_title_filter_accepts_text_that_is_not_a_valid_regex() -> None:
    filters = EntryFilters(title_include="C++", title_is_regex=True)
    assert entry_filter_reason({"title": "Learn C++ today"}, False, filters) is None
    assert entry_filter_reason({"title": "Learn Rust"}, False, filters)


def test_shorts_filter() -> None:
    filters = EntryFilters(skip_shorts=True)
    assert entry_filter_reason({"title": "a", "url": "https://www.youtube.com/shorts/x"}, True, filters)
    assert entry_filter_reason({"title": "a", "duration": 30, "width": 1080, "height": 1920}, False, filters)
    assert entry_filter_reason({"title": "a", "duration": 30, "width": 1920, "height": 1080}, False, filters) is None
    assert entry_filter_reason({"title": "a", "duration": 900, "width": 1080, "height": 1920}, False, filters) is None


def test_license_is_only_checked_once_known() -> None:
    filters = EntryFilters(cc_only=True)
    assert entry_filter_reason({"title": "a"}, True, filters) is None
    assert entry_filter_reason({"title": "a"}, False, filters)
    assert entry_filter_reason({"title": "a", "license": "Creative Commons Attribution license"}, False, filters) is None


def test_settings_round_trip() -> None:
    settings = DownloadSettings(media_type="audio", audio_format="flac", rate_limit_mb=1.5, sponsorblock_categories=["intro"])
    assert settings_from_dict(settings.to_dict()) == settings


def test_uploaded_presets_cannot_break_the_form() -> None:
    settings = settings_from_dict(
        {
            "media_type": "virus",
            "quality": 1080,
            "playlist_limit": "many",
            "retry_count": 99,
            "embed_metadata": "yes",
            "sponsorblock_categories": ["sponsor", "made-up"],
            "unknown_field": True,
            "audio_format": "mp3",
        }
    )
    defaults = DownloadSettings()
    assert settings.media_type == defaults.media_type
    assert settings.quality == defaults.quality
    assert settings.playlist_limit == defaults.playlist_limit
    assert settings.retry_count == defaults.retry_count
    assert settings.embed_metadata == defaults.embed_metadata
    assert settings.sponsorblock_categories == ["sponsor"]
    assert settings.audio_format == "mp3"


def test_a_presets_remembered_folder_is_not_a_download_setting() -> None:
    saved = {**DownloadSettings(quality="720p").to_dict(), "output_dir": "D:/videos"}
    assert settings_from_dict(saved) == DownloadSettings(quality="720p")
    # Built-in presets belong to no machine, so none names a folder.
    assert not any("output_dir" in preset for preset in BUILTIN_PRESETS.values())


@pytest.mark.parametrize("name", list(BUILTIN_PRESETS))
def test_builtin_presets_are_valid(name: str) -> None:
    preset = BUILTIN_PRESETS[name]
    settings = settings_from_dict(preset)
    for key, value in preset.items():
        assert getattr(settings, key) == value, key
    options_from_spec(JobSpec(url="u", settings=settings, output_dir="out"), AccessLimits())


def test_job_spec_round_trip() -> None:
    spec = JobSpec(url="u", settings=DownloadSettings(quality="720p"), output_dir="out", format_override="18", playlist_limit=3)
    assert job_spec_from_dict(spec.to_dict()) == spec


def test_public_visitors_cannot_force_a_full_re_encode() -> None:
    # A re-encode runs in one ffmpeg call that can hold one of the host's few slots for hours.
    assert spec_options(AccessLimits.for_public_host(), time_ranges="1-", precise_cuts=True).precise_cuts is False
    assert spec_options(time_ranges="1-", precise_cuts=True).precise_cuts is True


def test_how_much_of_a_list_is_read() -> None:
    plain = build_ydl_options(make_options(playlist_limit=20), True, YtDlpLogCollector())
    assert plain["playlistend"] == 20 and "playlist_items" not in plain
    reverse = build_ydl_options(make_options(playlist_limit=20, playlist_reverse=True), True, YtDlpLogCollector())
    assert reverse["playlist_items"] == "-20:" and "playlistend" not in reverse
    filtered = build_ydl_options(make_options(playlist_limit=20, filters=EntryFilters(skip_shorts=True)), True, YtDlpLogCollector())
    assert filtered["playlistend"] == 100
    assert filtered["max_downloads"] == 20
    dated = build_ydl_options(make_options(playlist_limit=200, filters=EntryFilters(date_after="20260101")), True, YtDlpLogCollector())
    assert dated["playlistend"] == 500
    assert dated["extractor_args"] == {"youtubetab": {"approximate_date": [""]}}
    chosen = build_ydl_options(make_options(playlist_items="1:3", playlist_reverse=True), True, YtDlpLogCollector())
    assert chosen["playlist_items"] == "1:3" and chosen["playlistreverse"] is True


@pytest.mark.parametrize("extra", [{}, {"playlist_reverse": True}, {"filters": EntryFilters(skip_shorts=True)}])
def test_chosen_items_are_never_combined_with_a_list_end(extra: dict[str, object]) -> None:
    # A subscription's scan depth arrives as playlist_items; yt-dlp would log that it ignores playlistend.
    log = YtDlpLogCollector()
    ydl_opts = build_ydl_options(make_options(playlist_items="1:50", playlist_limit=5, **extra), True, log)
    assert ydl_opts["playlist_items"] == "1:50"
    assert "playlistend" not in ydl_opts and "playliststart" not in ydl_opts
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:  # type: ignore[arg-type]
        entries = yt_dlp.utils.PlaylistEntries(ydl, {"entries": [{"id": str(number)} for number in range(60)]})
        assert len(list(entries.get_requested_items())) == 50
    assert not any("playlistend" in warning for warning in log.warnings)


def test_only_this_machines_owner_can_route_requests_through_a_proxy() -> None:
    settings = DownloadSettings(proxy=" http://127.0.0.1:7890 ", force_ipv4=True)
    assert network_options(settings, AccessLimits()).proxy == "http://127.0.0.1:7890"
    # A settings file a visitor imports can carry a proxy too.
    public = network_options(settings, AccessLimits.for_public_host())
    assert public.proxy == "" and public.force_ipv4 is True


def test_picked_videos_are_looked_up_by_id_not_by_position() -> None:
    spec = JobSpec(url="u", settings=DownloadSettings(playlist_items="1-3"), output_dir="out", video_ids=["abcdefghijk"], playlist_limit=1)
    options = options_from_spec(spec, AccessLimits())
    assert options.filters.only_ids == frozenset({"abcdefghijk"})
    assert options.playlist_items == ""
    ydl_opts = build_ydl_options(options, True, YtDlpLogCollector())
    assert ydl_opts["playlistend"] == PICKED_VIDEOS_SCAN_ITEMS and "playlist_items" not in ydl_opts
    assert ydl_opts["max_downloads"] == 1
    # A queued job saved before a restart comes back with the same picks.
    assert job_spec_from_dict(spec.to_dict()).video_ids == ["abcdefghijk"]


def test_a_custom_template_keeps_each_clip_apart() -> None:
    template = build_output_template(NAMING_TEMPLATE, "", False, custom_template="%(title)s.%(ext)s", has_sections=True)
    assert template == "%(title)s [%(section_start|0).0f 秒起].%(ext)s"
    # A template that already names the clips is left as it is.
    named = "%(title)s-%(section_number)s.%(ext)s"
    assert build_output_template(NAMING_TEMPLATE, "", False, custom_template=named, has_sections=True) == named
    assert build_output_template(NAMING_TEMPLATE, "", False, custom_template="%(title)s.%(ext)s") == "%(title)s.%(ext)s"


def test_chapter_folders_follow_the_main_name() -> None:
    from videocap.options import build_chapter_template

    single = build_chapter_template(build_output_template("沿用原始檔名", "", False, folder_rule="channel"))
    assert single == "%(channel,uploader|未知頻道).80B/%(playlist_index&{:02d} - |)s%(title).150B [%(id)s]/%(section_number)02d - %(section_title).100B.%(ext)s"
    batch = build_chapter_template(build_output_template("沿用原始檔名", "", True))
    assert batch.count("%(id)s") == 1


def test_folder_names_cannot_make_paths_too_long_for_windows() -> None:
    long_title = "很長的標題" * 40
    for folder_rule in ("channel", "playlist"):
        template = build_output_template("原始檔名加日期", "", True, folder_rule=folder_rule)
        with yt_dlp.YoutubeDL({"quiet": True, "outtmpl": template}) as ydl:  # type: ignore[arg-type]
            info = {"id": "abcdefghijk", "title": long_title, "channel": long_title, "playlist_title": long_title, "playlist_index": 3, "ext": "mp4", "upload_date": "20260101"}
            name = ydl.prepare_filename(info)
        # 150 bytes of title plus 80 of folder stay well inside Windows' 260 characters, with room for the download folder.
        folder, file_name = name.split("/" if "/" in name else "\\")
        assert len(folder.encode("utf-8")) <= 80
        assert len(name) < 150


@pytest.mark.parametrize(
    "overrides",
    [{"container": "webm"}, {"media_type": "audio", "audio_format": "wav"}],
)
def test_no_cover_is_embedded_where_yt_dlp_cannot_put_one(overrides: dict[str, object]) -> None:
    # yt-dlp fails on these with an English error and leaves the image files behind.
    options = make_options(embed_thumbnail=True, **overrides)
    assert "EmbedThumbnail" not in [step["key"] for step in build_late_postprocessors(options, True)]
    assert "writethumbnail" not in build_ydl_options(options, True, YtDlpLogCollector())
    kept = make_options(embed_thumbnail=True, write_thumbnail=True, **overrides)
    assert build_ydl_options(kept, True, YtDlpLogCollector())["writethumbnail"] is True


def test_cover_is_embedded_into_containers_that_hold_one() -> None:
    for overrides in ({"container": "mp4"}, {"container": "mkv"}, {"media_type": "audio", "audio_format": "mp3"}):
        options = make_options(embed_thumbnail=True, **overrides)  # type: ignore[arg-type]
        assert "EmbedThumbnail" in [step["key"] for step in build_late_postprocessors(options, True)]


def test_webm_gets_the_only_subtitle_format_it_can_hold() -> None:
    webm = make_options(container="webm", download_subtitles=True, embed_subtitles=True, subtitle_format="srt")
    assert build_ydl_options(webm, True, YtDlpLogCollector())["subtitlesformat"] == "vtt/best"
    mp4 = make_options(container="mp4", download_subtitles=True, embed_subtitles=True, subtitle_format="srt")
    assert build_ydl_options(mp4, True, YtDlpLogCollector())["subtitlesformat"] == "srt/best"
    not_embedded = make_options(container="webm", download_subtitles=True, subtitle_format="srt")
    assert build_ydl_options(not_embedded, True, YtDlpLogCollector())["subtitlesformat"] == "srt/best"


def test_sdr_is_preferred_unless_hdr_is_asked_for() -> None:
    for codec in ("best", "av1", "h264"):
        assert "hdr:sdr" in build_format_options(make_options(video_codec=codec), True)["format_sort"]
        assert "hdr:12" in build_format_options(make_options(video_codec=codec, prefer_hdr=True), True)["format_sort"]


@pytest.mark.parametrize(
    "url",
    [
        "https://youtu.be/jNQXAC9IVRw",
        "https://youtu.be/jNQXAC9IVRw?list=PLBCF2DAC6FFB574DE&si=AbCdEf",
        "https://youtu.be/jNQXAC9IVRw?list=RDjNQXAC9IVRw",
        "https://www.youtube.com/watch?v=jNQXAC9IVRw&list=PLBCF2DAC6FFB574DE",
        "https://www.youtube.com/shorts/jNQXAC9IVRw",
        "https://www.youtube.com/live/jNQXAC9IVRw",
        "https://www.youtube.com/embed/jNQXAC9IVRw",
        "https://www.youtube-nocookie.com/embed/jNQXAC9IVRw",
        "https://music.youtube.com/watch?v=jNQXAC9IVRw",
        "https://www.youtube.com/playlist?list=PLBCF2DAC6FFB574DE",
        "https://www.youtube.com/@jawed",
        "https://www.youtube.com/channel/UC4QobU6STFB0P71PMvOGN5A",
        "https://www.youtube.com/embed/live_stream?channel=UC4QobU6STFB0P71PMvOGN5A",
    ],
)
def test_every_youtube_link_shape_has_an_allowed_extractor(url: str) -> None:
    from urllib.parse import urlparse

    from videocap.config import YOUTUBE_HOSTS
    from videocap.urls import parse_youtube_urls

    assert parse_youtube_urls(url)[0]
    assert urlparse(url).hostname in YOUTUBE_HOSTS
    with yt_dlp.YoutubeDL(build_base_options(YtDlpLogCollector(), 1, LoginSource())) as ydl:  # type: ignore[arg-type]
        assert any(extractor.suitable(url) for extractor in ydl._ies.values())


def test_other_sites_have_no_allowed_extractor() -> None:
    with yt_dlp.YoutubeDL(build_base_options(YtDlpLogCollector(), 1, LoginSource())) as ydl:  # type: ignore[arg-type]
        assert not any(extractor.suitable("https://example.com/v.mp4") for extractor in ydl._ies.values())


# ---- New download options, part A ----


def run_offline(url: str, output_dir: Path, limits: AccessLimits | None = None, **settings: Any) -> Any:
    from videocap.downloader import run_download

    class Progress:
        def progress(self, value: float, text: str = "") -> None:
            pass

    spec = JobSpec(url=url, settings=DownloadSettings(**settings), output_dir=str(output_dir))
    return run_download(url, options_from_spec(spec, limits or AccessLimits()), Progress())


def chapters_of(path: Path) -> list[dict[str, Any]]:
    output = subprocess.run(
        ["ffprobe", "-v", "error", "-show_chapters", "-of", "json", str(path)], capture_output=True, check=True, text=True, encoding="utf-8"
    ).stdout
    return json.loads(output)["chapters"]


def youtube_extractor(ydl_opts: dict[str, Any]) -> Any:
    """yt-dlp's real YouTube extractor, set up with these options, to ask how it reads its arguments."""
    ydl = yt_dlp.YoutubeDL(ydl_opts)  # type: ignore[arg-type]
    return ydl.get_info_extractor("Youtube")


# feat:16 - SponsorBlock extras


def test_every_sponsorblock_category_of_ytdlp_is_offered() -> None:
    from yt_dlp.postprocessor.sponsorblock import SponsorBlockPP

    from videocap.config import SPONSORBLOCK_CATEGORIES
    from videocap.options import MARK_ONLY_SPONSORBLOCK_CATEGORIES

    assert set(SPONSORBLOCK_CATEGORIES) == set(SponsorBlockPP.CATEGORIES)
    assert MARK_ONLY_SPONSORBLOCK_CATEGORIES == {"chapter", "poi_highlight"}
    assert settings_from_dict({"sponsorblock_categories": ["hook", "chapter", "poi_highlight"]}).sponsorblock_categories == [
        "hook",
        "chapter",
        "poi_highlight",
    ]


def test_spots_are_only_marked_never_removed() -> None:
    chosen = ["sponsor", "chapter", "poi_highlight", "hook"]
    assert spec_options(sponsorblock_mode="remove", sponsorblock_categories=chosen).sponsorblock_categories == ("sponsor", "hook")
    assert spec_options(sponsorblock_mode="mark", sponsorblock_categories=chosen).sponsorblock_categories == tuple(chosen)


def test_removed_ranges_become_a_cut() -> None:
    options = spec_options(remove_ranges="0:01-0:03, 8-")
    assert options.remove_ranges == ((1.0, 3.0), (8.0, float("inf")))
    steps = build_late_postprocessors(options, True)
    cut = next(step for step in steps if step["key"] == "ModifyChapters")
    assert cut["remove_ranges"] == [(1.0, 3.0), (8.0, float("inf"))]
    assert cut["remove_sponsor_segments"] == set()
    # A clip has its own timing, so the video's times would cut the wrong parts.
    assert spec_options(remove_ranges="0:01-0:03", time_ranges="0:00-0:05").remove_ranges == ()
    with pytest.raises(ValueError, match="移除片段：看不懂時間範圍"):
        spec_options(remove_ranges="later")


@pytest.mark.parametrize(("ranges", "kept"), [("0:02-0:05", 7.0), ("0:02-0:04, 8-", 6.0)])
def test_removed_ranges_are_cut_out_of_the_file(offline_youtube: OfflineYouTube, tmp_path: Path, ranges: str, kept: float) -> None:
    url = offline_youtube.add_video("cutVideo001", "Cut")
    outcome = run_offline(url, tmp_path, remove_ranges=ranges, download_subtitles=False)
    assert outcome["errors"] == []
    assert duration_of(Path(outcome["items"][0]["path"])) == pytest.approx(kept, abs=0.6)


@pytest.mark.parametrize(
    ("ranges", "kept"),
    [
        # Overlapping ranges are one cut.
        ("0:02-0:05, 0:04-0:07", 5.0),
        # Past the end of the video there is nothing to cut.
        ("0:20-0:30", 10.0),
        ("0:08-0:30", 8.0),
    ],
)
def test_removed_ranges_that_overlap_or_run_past_the_end(offline_youtube: OfflineYouTube, tmp_path: Path, ranges: str, kept: float) -> None:
    url = offline_youtube.add_video("cutVideo002", "Cut")
    outcome = run_offline(url, tmp_path, remove_ranges=ranges, download_subtitles=False)
    assert outcome["errors"] == []
    assert duration_of(Path(outcome["items"][0]["path"])) == pytest.approx(kept, abs=0.6)


def test_removed_ranges_and_removed_sponsors_are_cut_together(offline_youtube: OfflineYouTube, tmp_path: Path) -> None:
    url = offline_youtube.add_video("cutVideo003", "Cut")
    offline_youtube.sponsor_segments["cutVideo003"] = [
        {"segment": [1.0, 3.0], "category": "sponsor", "actionType": "skip", "videoDuration": 10, "UUID": "s1"}
    ]
    outcome = run_offline(
        url, tmp_path, remove_ranges="0:02-0:04, 0:08-", sponsorblock_mode="remove", sponsorblock_categories=["sponsor"], download_subtitles=False
    )
    assert outcome["errors"] == []
    # 1-4 (sponsor and range joined) and 8-10.
    assert duration_of(Path(outcome["items"][0]["path"])) == pytest.approx(5.0, abs=0.6)


def test_removing_the_whole_video_is_explained(offline_youtube: OfflineYouTube, tmp_path: Path) -> None:
    url = offline_youtube.add_video("cutVideo004", "Cut")
    outcome = run_offline(url, tmp_path, remove_ranges="0:00-", download_subtitles=False)
    # Nothing would be left, so the video is kept whole, and the person is told why.
    assert [Path(item["path"]).name for item in outcome["items"]] == ["Cut.mp4"]
    assert duration_of(Path(outcome["items"][0]["path"])) == pytest.approx(10.0, abs=0.6)
    assert outcome["errors"] == []
    assert len(outcome["notes"]) == 1 and "整部影片" in outcome["notes"][0]


def test_community_chapters_are_marked_with_their_own_names(offline_youtube: OfflineYouTube, tmp_path: Path) -> None:
    url = offline_youtube.add_video("chapters001", "No chapters")
    offline_youtube.sponsor_segments["chapters001"] = [
        {"segment": [3.0, 6.0], "category": "chapter", "actionType": "chapter", "videoDuration": 10, "UUID": "u", "description": "重點"}
    ]
    outcome = run_offline(url, tmp_path, sponsorblock_mode="mark", sponsorblock_categories=["chapter"], download_subtitles=False)
    assert outcome["errors"] == []
    titles = [chapter["tags"]["title"] for chapter in chapters_of(Path(outcome["items"][0]["path"]))]
    assert any("重點" in title for title in titles)
    assert duration_of(Path(outcome["items"][0]["path"])) == pytest.approx(10.0, abs=0.6)


# feat:7 - YouTube player client, and feat:15 - Traditional Chinese titles


def test_offered_player_clients_need_no_po_token_from_youtube() -> None:
    from yt_dlp.extractor.youtube._base import INNERTUBE_CLIENTS

    from videocap.config import PLAYER_CLIENT_OPTIONS, PLAYER_CLIENTS_WITHOUT_COOKIES

    offered = [client for client in PLAYER_CLIENT_OPTIONS if client != "default"]
    assert offered
    for client in offered:
        policies = INNERTUBE_CLIENTS[client]["GVS_PO_TOKEN_POLICY"].values()
        assert not any(policy.required for policy in policies), client
    assert PLAYER_CLIENTS_WITHOUT_COOKIES == {client for client in offered if not INNERTUBE_CLIENTS[client]["SUPPORTS_COOKIES"]}


def test_the_chosen_player_client_reaches_ytdlps_youtube_extractor() -> None:
    assert "extractor_args" not in build_ydl_options(spec_options(), True, YtDlpLogCollector())
    # Changing only what is asked of YouTube, so public visitors may choose it too.
    options = spec_options(AccessLimits.for_public_host(), player_client="tv")
    assert options.network.player_client == "tv"
    extractor = youtube_extractor(build_ydl_options(options, True, YtDlpLogCollector()))
    assert extractor._configuration_arg("player_client") == ["tv"]


def test_a_client_that_cannot_log_in_is_not_forced_on_a_logged_in_download() -> None:
    settings = DownloadSettings(player_client="visionos")
    network = network_options(settings, AccessLimits())
    assert build_base_options(YtDlpLogCollector(), 1, LoginSource(), network)["extractor_args"] == {"youtube": {"player_client": ["visionos"]}}
    assert "extractor_args" not in build_base_options(YtDlpLogCollector(), 1, LoginSource(browser="firefox"), network)


def test_traditional_chinese_titles_are_asked_of_youtube() -> None:
    options = spec_options(AccessLimits.for_public_host(), prefer_traditional_metadata=True)
    extractor = youtube_extractor(build_ydl_options(options, True, YtDlpLogCollector()))
    # A code YouTube does not support would make every extraction fail.
    assert extractor._preferred_lang == "zh-TW"
    assert "lang" not in build_ydl_options(spec_options(), True, YtDlpLogCollector()).get("extractor_args", {}).get("youtube", {})


def test_features_that_add_extractor_arguments_do_not_overwrite_each_other() -> None:
    options = spec_options(
        player_client="tv_samsung",
        prefer_traditional_metadata=True,
        filter_date_after="2026-01-01",
        ytdlp_extra_args="--extractor-args youtube:skip=dash --extractor-args youtubetab:skip=webpage",
    )
    assert build_ydl_options(options, True, YtDlpLogCollector())["extractor_args"] == {
        "youtube": {"player_client": ["tv_samsung"], "lang": ["zh-TW"], "skip": ["dash"]},
        "youtubetab": {"approximate_date": [""], "skip": ["webpage"]},
    }


# feat:8 - the owner's own yt-dlp options


def test_extra_ytdlp_options_are_read_by_ytdlps_own_parser() -> None:
    from videocap.options import ExtraYtDlpOptions, parse_extra_ytdlp_args

    assert parse_extra_ytdlp_args("  ") == ExtraYtDlpOptions()
    extra = parse_extra_ytdlp_args('--limit-rate 50K -o "%(id)s.%(ext)s" --cookies c.txt --max-downloads 1000 --use-extractors all')
    assert dict(extra.params) == {"ratelimit": 51200}
    assert extra.ignored == ("allowed_extractors", "cookiefile", "max_downloads", "outtmpl")


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("--no-such-option", "no such option: --no-such-option"),
        ('--format "best', "引號沒有成對"),
        ("https://www.youtube.com/watch?v=jNQXAC9IVRw", "不是選項"),
        ("--limit-rate fast", "invalid rate limit"),
        # yt-dlp would read these from the keyboard and wait forever.
        ("-a -", "--batch-file"),
        ("--config-locations -", "--config-locations"),
        ("--help", "--help"),
        ("--version", "--version"),
    ],
)
def test_extra_ytdlp_option_mistakes_are_explained(raw: str, message: str) -> None:
    from videocap.options import parse_extra_ytdlp_args

    with pytest.raises(ValueError, match="進階 yt-dlp 參數") as caught:
        parse_extra_ytdlp_args(raw)
    assert message in str(caught.value)
    # A mistake stops the job before anything is sent to YouTube.
    with pytest.raises(ValueError, match="進階 yt-dlp 參數"):
        spec_options(ytdlp_extra_args=raw)


@pytest.mark.parametrize("raw", ["--username me", "--username me --password secret", "--ap-username me"])
def test_extra_ytdlp_options_never_wait_for_a_typed_password(raw: str, monkeypatch: pytest.MonkeyPatch) -> None:
    import getpass

    from videocap.options import parse_extra_ytdlp_args

    def prompt(*args: Any, **kwargs: Any) -> str:
        # On the server nobody can type it, so the page, or the job, would wait forever.
        raise AssertionError("yt-dlp asked for a password")

    monkeypatch.setattr(getpass, "getpass", prompt)
    with pytest.raises(ValueError, match="會員登入"):
        parse_extra_ytdlp_args(raw)


@pytest.mark.parametrize(
    ("raw", "named"),
    [
        ("-s", "--simulate"),
        ("--skip-download", "--skip-download"),
        ("--no-download", "--skip-download"),
        ("-F", "--list-formats"),
        ("--list-subs", "--list-subs"),
        ("--list-thumbnails", "--list-thumbnails"),
        ("--print title", "--print"),
        ("-j", "-j"),
        ("-J", "-J"),
        ("--get-url", "--get-"),
        ("--flat-playlist", "--flat-playlist"),
        ("--test", "--test"),
    ],
)
def test_extra_ytdlp_options_that_download_nothing_are_refused(raw: str, named: str) -> None:
    from videocap.options import parse_extra_ytdlp_args

    # Otherwise the job would end as done, with no file, or only the first few kilobytes of one.
    with pytest.raises(ValueError, match="沒有檔案") as caught:
        parse_extra_ytdlp_args(raw)
    assert named in str(caught.value)


def test_printing_while_still_downloading_is_allowed() -> None:
    from videocap.options import parse_extra_ytdlp_args

    assert parse_extra_ytdlp_args("--no-simulate --print title").params["simulate"] is False


def test_the_owners_own_wait_for_video_is_left_to_the_apps_wait() -> None:
    from videocap.options import parse_extra_ytdlp_args

    # yt-dlp's own wait would not let pause or cancel through, and has no time limit.
    extra = parse_extra_ytdlp_args("--wait-for-video 60")
    assert "wait_for_video" not in extra.params and extra.ignored == ("wait_for_video",)


def test_extra_ytdlp_options_never_reach_a_public_visitor() -> None:
    raw = '--exec "echo hacked" --proxy http://attacker.example:1 --no-such-option'
    options = spec_options(AccessLimits.for_public_host(), ytdlp_extra_args=raw)
    ydl_opts = build_ydl_options(options, True, YtDlpLogCollector())
    assert "proxy" not in ydl_opts
    assert not any(step["key"] == "Exec" for step in ydl_opts.get("postprocessors", []))
    assert not any(step["key"] == "Exec" for step in build_late_postprocessors(options, True))


def test_extra_ytdlp_steps_are_left_for_the_downloader_to_add_last() -> None:
    options = spec_options(
        media_type="audio",
        audio_format="mp3",
        sponsorblock_mode="remove",
        embed_metadata=True,
        ytdlp_extra_args='--embed-info-json --exec "after_move:echo done" --replace-in-metadata title a b',
    )
    assert [step["key"] for step in options.extra_ytdlp.postprocessors] == ["MetadataParser", "FFmpegMetadata", "Exec"]
    # The downloader adds them after its own steps at each stage (tests/test_downloader.py checks the real order).
    ydl_opts = build_ydl_options(options, True, YtDlpLogCollector())
    assert [step["key"] for step in ydl_opts["postprocessors"]] == ["SponsorBlock", "FFmpegExtractAudio"]
    assert [step["key"] for step in build_late_postprocessors(options, True)] == ["ModifyChapters", "FFmpegMetadata"]
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:  # type: ignore[arg-type]
        from videocap.downloader import add_owner_postprocessors

        add_owner_postprocessors(ydl, options)
        assert [type(step).__name__ for step in ydl._pps["after_move"]] == ["ExecPP"]


def test_extra_ytdlp_options_change_a_real_download(offline_youtube: OfflineYouTube, tmp_path: Path) -> None:
    url = offline_youtube.add_video("extraOpts01", "Original title")
    outcome = run_offline(
        url,
        tmp_path,
        download_subtitles=False,
        embed_metadata=False,
        embed_chapters=False,
        ytdlp_extra_args='--replace-in-metadata title Original Changed --embed-metadata -o "elsewhere/%(id)s.%(ext)s"',
    )
    assert outcome["errors"] == []
    path = Path(outcome["items"][0]["path"])
    # The rename happened before naming; the app's own place and name won over -o.
    assert path == tmp_path / "Changed title.mp4"
    assert probe(path)["format"]["tags"]["title"] == "Changed title"
    assert not (tmp_path / "elsewhere").exists()


# feat:21 - archive options


def test_video_information_is_embedded_only_into_mkv_and_never_with_a_login() -> None:
    def embeds(options: DownloadOptions) -> bool:
        return any(step.get("add_infojson") for step in build_late_postprocessors(options, True))

    assert embeds(spec_options(container="mkv", embed_infojson=True, embed_metadata=False, embed_chapters=False))
    assert not embeds(spec_options(container="mp4", embed_infojson=True))
    assert not embeds(spec_options(container="mkv", embed_infojson=False))
    assert not embeds(spec_options(media_type="audio", container="mkv", embed_infojson=True))
    spec = JobSpec(url="u", settings=DownloadSettings(container="mkv", embed_infojson=True), output_dir="out", browser="firefox")
    logged_in = options_from_spec(spec, AccessLimits())
    assert logged_in.embed_infojson is False
    # Also when the options were built some other way.
    assert not embeds(replace(logged_in, embed_infojson=True))


def test_mkv_carries_the_video_information(offline_youtube: OfflineYouTube, tmp_path: Path) -> None:
    url = offline_youtube.add_video("infoJson001", "Archive")
    outcome = run_offline(url, tmp_path, container="mkv", embed_infojson=True, download_subtitles=False)
    assert outcome["errors"] == []
    path = Path(outcome["items"][0]["path"])
    attachments = [stream for stream in probe(path)["streams"] if stream.get("codec_type") == "attachment"]
    assert [stream["tags"]["mimetype"] for stream in attachments] == ["application/json"]
    # The temporary info.json is not left behind.
    assert sorted(child.name for child in tmp_path.iterdir()) == ["Archive.mkv"]


def test_covers_are_saved_as_jpg(offline_youtube: OfflineYouTube, tmp_path: Path) -> None:
    png = tmp_path / "media" / "cover.png"
    png.parent.mkdir()
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:size=64x36", "-frames:v", "1", str(png)], check=True)
    url = offline_youtube.add_video("jpgCover001", "Cover", thumbnails=[{"url": png.as_uri(), "id": "0"}])
    out = tmp_path / "out"
    outcome = run_offline(url, out, write_thumbnail=True, embed_thumbnail=True, thumbnails_as_jpg=True, download_subtitles=False)
    assert outcome["errors"] == []
    covers = [Path(path) for path in outcome["items"][0]["extra_paths"] if Path(path).suffix in (".png", ".jpg")]
    assert [cover.name for cover in covers] == ["Cover.jpg"]
    assert stream_codecs(covers[0]) == ["mjpeg"]
    assert not (out / "Cover.png").exists()
    # The cover inside the video is the JPG too.
    assert "mjpeg" in stream_codecs(Path(outcome["items"][0]["path"]))


def test_covers_keep_their_format_unless_asked() -> None:
    assert not any(step["key"] == "FFmpegThumbnailsConvertor" for step in build_early_postprocessors(make_options(write_thumbnail=True), True))
    sidecar_only = make_options(media_type="none", write_thumbnail=True, thumbnails_as_jpg=True)
    assert build_early_postprocessors(sidecar_only, True) == [{"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"}]
    # Nothing to convert without a cover, or without ffmpeg.
    assert build_early_postprocessors(make_options(thumbnails_as_jpg=True), True) == []
    assert build_early_postprocessors(make_options(write_thumbnail=True, thumbnails_as_jpg=True), False) == []


# feat:18 - media-server layout


def media_server_name(**info: Any) -> str:
    template = build_output_template("沿用原始檔名", "", False, folder_rule="media_server")
    with yt_dlp.YoutubeDL({"quiet": True, "outtmpl": template}) as ydl:  # type: ignore[arg-type]
        return ydl.prepare_filename({"id": "abcdefghijk", "title": "Episode", "ext": "mp4", **info}).replace("\\", "/")


def test_media_server_layout_names_shows_seasons_and_episodes() -> None:
    assert media_server_name(channel="Show", upload_date="20260131") == "Show/Season 2026/2026-01-31 Episode [abcdefghijk].mp4"
    # Without a channel or a date the file still lands somewhere sensible.
    assert media_server_name(uploader="Uploader") == "Uploader/Season 0/未知日期 Episode [abcdefghijk].mp4"
    assert media_server_name() == "未知頻道/Season 0/未知日期 Episode [abcdefghijk].mp4"
    clips = build_output_template("沿用原始檔名", "", False, folder_rule="media_server", has_sections=True)
    assert clips.endswith(" [%(id)s] [%(section_start|0).0f 秒起].%(ext)s")
    # The owner's own template still wins.
    assert build_output_template(NAMING_TEMPLATE, "", False, folder_rule="media_server", custom_template="%(id)s.%(ext)s") == "%(id)s.%(ext)s"


def test_media_server_layout_is_only_for_this_machines_owner() -> None:
    assert "Season" in spec_options(folder_rule="media_server").output_template
    assert "Season" not in spec_options(AccessLimits.for_public_host(), folder_rule="media_server").output_template


def test_media_server_layout_in_a_real_download(offline_youtube: OfflineYouTube, tmp_path: Path) -> None:
    url = offline_youtube.add_video("mediaServ01", "Pilot", channel="My Show", upload_date="20250704")
    outcome = run_offline(url, tmp_path, folder_rule="media_server", download_subtitles=False)
    assert outcome["errors"] == []
    assert Path(outcome["items"][0]["path"]) == tmp_path / "My Show" / "Season 2025" / "2025-07-04 Pilot [mediaServ01].mp4"


# Part B's setting fields


def test_part_b_settings_are_validated_and_kept_in_presets() -> None:
    chosen = DownloadSettings(
        comments_mode="new",
        comments_max=2000,
        save_live_chat=True,
        subtitles_to_traditional=True,
        music_tags=True,
        wait_for_live=True,
        mtime_upload_date=True,
        write_nfo=True,
        remove_ranges="0:01-0:02",
        player_client="tv",
        prefer_traditional_metadata=True,
        embed_infojson=True,
        thumbnails_as_jpg=True,
        ytdlp_extra_args="--limit-rate 1M",
    )
    assert settings_from_dict(chosen.to_dict()) == chosen
    broken = settings_from_dict({"comments_mode": "all", "comments_max": 0, "player_client": "android_hacked", "write_nfo": "yes"})
    defaults = DownloadSettings()
    assert (broken.comments_mode, broken.comments_max, broken.player_client, broken.write_nfo) == (
        defaults.comments_mode,
        defaults.comments_max,
        defaults.player_client,
        defaults.write_nfo,
    )
    # Everything new is off by default, so earlier downloads keep working the same way.
    assert (defaults.comments_mode, defaults.player_client, defaults.ytdlp_extra_args, defaults.remove_ranges) == ("off", "default", "", "")


def test_part_b_options_are_limited_for_public_visitors() -> None:
    settings: dict[str, Any] = {
        "comments_mode": "top",
        "comments_max": 2000,
        "save_live_chat": True,
        "wait_for_live": True,
        "write_nfo": True,
        "music_tags": True,
        "subtitles_to_traditional": True,
        "mtime_upload_date": True,
    }
    owner = spec_options(**settings)
    assert (owner.comments_max, owner.save_live_chat, owner.wait_for_live, owner.write_nfo) == (2000, True, True, True)
    public = spec_options(AccessLimits.for_public_host(), **settings)
    assert (public.comments_mode, public.comments_max) == ("top", 500)
    assert (public.save_live_chat, public.wait_for_live, public.write_nfo) == (False, False, False)
    assert (public.music_tags, public.subtitles_to_traditional, public.mtime_upload_date) == (True, True, True)


def test_the_music_preset_adds_music_tags() -> None:
    assert BUILTIN_PRESETS["音樂（MP3 320k＋封面）"]["music_tags"] is True


# feat:20 - comments and live chat


def test_comments_are_asked_for_in_the_chosen_order_and_count() -> None:
    assert "getcomments" not in build_ydl_options(spec_options(), True, YtDlpLogCollector())
    options = spec_options(comments_mode="new", comments_max=2000, player_client="tv")
    ydl_opts = build_ydl_options(options, True, YtDlpLogCollector())
    assert ydl_opts["getcomments"] is True
    extractor = youtube_extractor(ydl_opts)
    assert extractor._configuration_arg("comment_sort") == ["new"]
    # No replies, like the comment export on the 下載 page; the player client chosen is kept.
    assert extractor._configuration_arg("max_comments") == ["2000", "all", "all", "0"]
    assert extractor._configuration_arg("player_client") == ["tv"]


def test_public_visitors_get_at_most_500_comments() -> None:
    options = spec_options(AccessLimits.for_public_host(), comments_mode="top", comments_max=100000)
    extractor = youtube_extractor(build_ydl_options(options, True, YtDlpLogCollector()))
    assert extractor._configuration_arg("max_comments")[0] == "500"


def test_a_past_streams_chat_is_asked_for_only_when_wanted() -> None:
    assert build_ydl_options(spec_options(subtitle_languages="all"), True, YtDlpLogCollector())["subtitleslangs"] == ["all", "-live_chat"]
    with_subtitles = build_ydl_options(spec_options(subtitle_languages="all", save_live_chat=True), True, YtDlpLogCollector())
    assert with_subtitles["subtitleslangs"] == ["all", "live_chat"]
    chat_only = build_ydl_options(spec_options(download_subtitles=False, save_live_chat=True), True, YtDlpLogCollector())
    assert (chat_only["writesubtitles"], chat_only["writeautomaticsub"], chat_only["subtitleslangs"]) == (True, False, ["live_chat"])
    # A long chat takes thousands of requests, which a shared host cannot spare.
    public = spec_options(AccessLimits.for_public_host(), download_subtitles=False, save_live_chat=True)
    assert "writesubtitles" not in build_ydl_options(public, True, YtDlpLogCollector())


# feat:17 - music tags


def test_music_tags_are_written_even_without_the_metadata_option() -> None:
    def metadata_step(options: DownloadOptions) -> Any:
        return next((step for step in build_late_postprocessors(options, True) if step["key"] == "FFmpegMetadata"), None)

    quiet = dict(embed_metadata=False, embed_chapters=False, media_type="audio")
    assert metadata_step(spec_options(**quiet)) is None
    assert metadata_step(spec_options(**quiet, music_tags=True))["add_metadata"] is True


def test_marked_segments_get_chinese_chapter_names(offline_youtube: OfflineYouTube, tmp_path: Path) -> None:
    url = offline_youtube.add_video("sbNames0001", "Marked")
    segment = {"actionType": "skip", "videoDuration": 10, "UUID": "u", "description": ""}
    offline_youtube.sponsor_segments["sbNames0001"] = [
        {**segment, "segment": [2.0, 4.0], "category": "sponsor"},
        {**segment, "segment": [6.0, 8.0], "category": "selfpromo"},
        {**segment, "segment": [9.0, 9.0], "category": "poi_highlight", "actionType": "poi"},
    ]
    outcome = run_offline(
        url, tmp_path, sponsorblock_mode="mark", sponsorblock_categories=["sponsor", "selfpromo", "poi_highlight"], download_subtitles=False
    )
    assert outcome["errors"] == []
    titles = [chapter["tags"]["title"] for chapter in chapters_of(Path(outcome["items"][0]["path"]))]
    assert "[贊助片段] 贊助廣告" in titles
    assert "[贊助片段] 自我宣傳" in titles
    assert "精華起點" in titles
    assert not any("SponsorBlock" in title for title in titles)


def test_a_list_of_player_clients_reaches_ytdlp_as_a_list() -> None:
    from videocap.options import NetworkOptions

    network = NetworkOptions(player_client="tv,visionos,web_embedded")
    extractor = youtube_extractor(build_base_options(YtDlpLogCollector(), 1, LoginSource(), network))
    assert extractor._configuration_arg("player_client") == ["tv", "visionos", "web_embedded"]
    # The client that cannot log in is left out of a logged-in request, the others stay.
    logged_in = build_base_options(YtDlpLogCollector(), 1, LoginSource(browser="firefox"), network)
    assert logged_in["extractor_args"] == {"youtube": {"player_client": ["tv", "web_embedded"]}}


def test_fallback_player_clients_need_no_po_token_and_take_cookies() -> None:
    from yt_dlp.extractor.youtube._base import INNERTUBE_CLIENTS

    from videocap.config import FALLBACK_PLAYER_CLIENTS

    for client in FALLBACK_PLAYER_CLIENTS:
        assert not any(policy.required for policy in INNERTUBE_CLIENTS[client].get("GVS_PO_TOKEN_POLICY", {}).values()), client
        assert INNERTUBE_CLIENTS[client]["SUPPORTS_COOKIES"], client
    # Different from what yt-dlp already tried.
    assert not set(FALLBACK_PLAYER_CLIENTS) & set(yt_dlp.extractor.youtube.YoutubeIE._DEFAULT_CLIENTS)

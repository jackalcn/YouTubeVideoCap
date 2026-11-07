from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Optional

import pytest

import videocap.downloader as downloader_module
from conftest import needs_ffmpeg
from offline_youtube import OfflineYouTube, duration_of, offline_media_fixture, offline_youtube_fixture, stream_codecs  # noqa: F401
from videocap.downloader import (
    CollectDownloadedItemsPP,
    JobStopped,
    KeptFileCutPP,
    ProgressTracker,
    RecordChapterFilesPP,
    describe_subtitle_shortfall,
    download_with_ytdlp,
    empty_outcome,
    remove_partial_files,
    run_download,
    strip_cookies_from_info_json,
)
from videocap.errors import extract_failed_video_id, humanize_ytdlp_error
from videocap.options import AccessLimits, DownloadOptions, EntryFilters, LoginSource, options_from_spec
from videocap.settings import DownloadSettings, JobSpec


class Progress:
    def __init__(self) -> None:
        self.calls: list[tuple[float, str]] = []

    def progress(self, value: float, text: str = "") -> None:
        self.calls.append((value, text))


class FakeYDL:
    """Stands in for yt_dlp.YoutubeDL: runs the app's hooks and post-processors the way the real one does."""

    script: Callable[[FakeYDL], Any] = staticmethod(lambda ydl: ydl.finish_video())
    instances: list[FakeYDL] = []

    def __init__(self, opts: dict[str, Any]) -> None:
        self.opts = opts
        self.params = opts
        self.post_processors: dict[str, list[Any]] = {}
        self.output: Path = Path(opts.get("paths", {}).get("home", ".")) / "video.mp4"
        FakeYDL.instances.append(self)

    def __enter__(self) -> FakeYDL:
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def add_post_processor(self, processor: Any, when: str = "post_process") -> None:
        processor.set_downloader(self)
        self.post_processors.setdefault(when, []).append(processor)

    def prepare_filename(self, info: dict[str, Any], kind: str = "default") -> str:
        return str(self.output)

    def evaluate_outtmpl(self, template: str, info: Any) -> str:
        return template

    def to_console_title(self, *args: Any, **kwargs: Any) -> None:
        pass

    def to_screen(self, *args: Any, **kwargs: Any) -> None:
        pass

    def _wait_for_video(self, ie_result: Any = None) -> None:
        pass

    def extract_info(self, url: str, download: bool = True) -> dict[str, Any]:
        return FakeYDL.script(self)

    def finish_video(self, **extra: Any) -> dict[str, Any]:
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self.output.write_bytes(b"video")
        info = {
            "id": "abc123def45",
            "title": "Title",
            "ext": "mp4",
            "filepath": str(self.output),
            "requested_subtitles": {},
            "__real_download": True,
            **extra,
        }
        for processor in self.post_processors.get("post_process", []):
            _, info = processor.run(info)
        return info


@pytest.fixture
def fake_ydl(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(downloader_module.yt_dlp, "YoutubeDL", FakeYDL)
    monkeypatch.setattr(downloader_module, "get_ffmpeg_status", lambda: (False, "mock missing"))
    monkeypatch.setattr(FakeYDL, "script", staticmethod(lambda ydl: ydl.finish_video()))
    FakeYDL.instances = []
    return FakeYDL


def make_options(tmp_path: Path, **overrides: Any) -> DownloadOptions:
    return DownloadOptions(**{"output_dir": tmp_path / "out", "output_template": "%(title)s.%(ext)s", **overrides})


def test_download_records_the_finished_file(fake_ydl: type[FakeYDL], tmp_path: Path) -> None:
    outcome = download_with_ytdlp("https://youtu.be/abc123def45", make_options(tmp_path), Progress())
    assert [item["title"] for item in outcome["items"]] == ["Title"]
    assert outcome["items"][0]["video_id"] == "abc123def45"
    assert Path(outcome["items"][0]["path"]).exists()
    assert outcome["errors"] == []
    assert outcome["stopped"] is False


def test_without_ffmpeg_no_late_steps_are_added(fake_ydl: type[FakeYDL], tmp_path: Path) -> None:
    download_with_ytdlp(
        "u", make_options(tmp_path, download_subtitles=True, embed_subtitles=True, embed_metadata=True), Progress()
    )
    ydl = fake_ydl.instances[0]
    assert ydl.opts["format"] == "b[ext=mp4]/b"
    assert "merge_output_format" not in ydl.opts
    assert "postprocessors" not in ydl.opts
    assert [type(step).__name__ for step in ydl.post_processors["post_process"]] == ["CollectDownloadedItemsPP"]


def test_sidecar_mode_records_before_the_skipped_download(fake_ydl: type[FakeYDL], tmp_path: Path) -> None:
    download_with_ytdlp("u", make_options(tmp_path, media_type="none"), Progress())
    assert "before_dl" in fake_ydl.instances[0].post_processors
    assert "post_process" not in fake_ydl.instances[0].post_processors


def test_stop_during_a_download_ends_the_job_and_keeps_what_finished(fake_ydl: type[FakeYDL], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stop_now = {"value": False}

    def script(ydl: FakeYDL) -> Any:
        ydl.finish_video()
        stop_now["value"] = True
        ydl.opts["progress_hooks"][0]({"status": "downloading", "downloaded_bytes": 1, "tmpfilename": "x.part", "filename": "x", "info_dict": {}})
        raise AssertionError("The hook should have stopped the job")

    monkeypatch.setattr(FakeYDL, "script", staticmethod(script))
    outcome = run_download("u", make_options(tmp_path), Progress(), should_stop=lambda: stop_now["value"])
    assert outcome["stopped"] is True
    assert len(outcome["items"]) == 1


def test_errors_are_translated_and_failed_videos_identified(fake_ydl: type[FakeYDL], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def script(ydl: FakeYDL) -> Any:
        ydl.opts["logger"].error("ERROR: [youtube] abc123def45: Video unavailable")
        ydl.opts["logger"].error("ERROR: [youtube] abc123def45: Video unavailable")
        return None

    monkeypatch.setattr(FakeYDL, "script", staticmethod(script))
    outcome = run_download("u", make_options(tmp_path), Progress())
    assert len(outcome["errors"]) == 1
    assert "影片無法使用" in outcome["errors"][0]
    assert outcome["failed_ids"] == ["abc123def45"]


def test_engine_crash_becomes_an_error_message(fake_ydl: type[FakeYDL], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def script(ydl: FakeYDL) -> Any:
        raise RuntimeError("HTTP Error 429: Too Many Requests")

    monkeypatch.setattr(FakeYDL, "script", staticmethod(script))
    outcome = run_download("u", make_options(tmp_path), Progress())
    assert outcome["items"] == []
    assert "限制了請求頻率" in outcome["errors"][0]


def test_expired_login_warning_becomes_a_clear_error(fake_ydl: type[FakeYDL], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def script(ydl: FakeYDL) -> Any:
        ydl.opts["logger"].warning("The provided YouTube account cookies are no longer valid.")
        return ydl.finish_video()

    monkeypatch.setattr(FakeYDL, "script", staticmethod(script))
    outcome = run_download("u", make_options(tmp_path), Progress())
    assert "登入已失效" in outcome["errors"][0]
    assert len(outcome["items"]) == 1


def run_filter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, info: dict[str, Any], incomplete: bool = False, **options: Any) -> tuple[Optional[str], Any]:
    captured: dict[str, Any] = {}

    def script(ydl: FakeYDL) -> Any:
        captured["reason"] = ydl.opts["match_filter"](info, incomplete=incomplete)
        return None

    monkeypatch.setattr(FakeYDL, "script", staticmethod(script))
    outcome = download_with_ytdlp("u", make_options(tmp_path, **options), Progress())
    return captured["reason"], outcome


def test_live_stream_is_skipped_by_default(fake_ydl: type[FakeYDL], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reason, outcome = run_filter(tmp_path, monkeypatch, {"title": "Live", "live_status": "is_live"})
    assert reason and "直播進行中" in reason
    assert outcome["skipped"] == [reason]


def test_recording_a_live_stream_needs_ffmpeg(fake_ydl: type[FakeYDL], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reason, _ = run_filter(tmp_path, monkeypatch, {"title": "Live", "live_status": "is_live"}, live_mode="record")
    assert reason and "ffmpeg" in reason


def test_live_stream_is_recorded_by_the_app_instead_of_yt_dlp(fake_ydl: type[FakeYDL], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(downloader_module, "get_ffmpeg_status", lambda: (True, "ffmpeg"))
    recorded: dict[str, Any] = {}

    def fake_record(sources: Any, output_path: Path, ffmpeg: str, *args: Any) -> Any:
        recorded["sources"] = sources
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"recording")
        return downloader_module.RecordingResult(output_path)

    monkeypatch.setattr(downloader_module, "record_live_stream", fake_record)
    reason, outcome = run_filter(
        tmp_path,
        monkeypatch,
        {"title": "Live", "id": "abc", "live_status": "is_live", "url": "http://live", "ext": "mp4"},
        live_mode="record",
    )
    # A reason stops yt-dlp's own ffmpeg, which could not be stopped once started.
    assert reason
    assert outcome["skipped"] == []
    assert recorded["sources"][0]["url"] == "http://live"
    assert len(outcome["items"]) == 1


def test_listing_of_a_live_stream_is_not_recorded_until_complete(fake_ydl: type[FakeYDL], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(downloader_module, "get_ffmpeg_status", lambda: (True, "ffmpeg"))
    reason, outcome = run_filter(tmp_path, monkeypatch, {"title": "Live", "live_status": "is_live"}, incomplete=True, live_mode="record")
    assert reason is None
    assert outcome["items"] == []


def test_size_limit_for_public_hosts(fake_ydl: type[FakeYDL], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reason, _ = run_filter(tmp_path, monkeypatch, {"title": "Big", "filesize_approx": 2000}, max_bytes=1000)
    assert reason and "超過雲端模式上限" in reason
    # A playlist listing has no sizes yet, so this waits for the full check.
    reason, _ = run_filter(tmp_path, monkeypatch, {"title": "Big", "filesize_approx": 2000}, incomplete=True, max_bytes=1000)
    assert reason is None


def test_a_resumed_job_counts_what_its_earlier_runs_finished(fake_ydl: type[FakeYDL], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder = tmp_path / "out"
    (folder / "sub").mkdir(parents=True)
    (folder / "sub" / "first.mp4").write_bytes(b"x" * 800)
    # Unfinished downloads are counted when they finish, which reports their whole size.
    (folder / "second.mp4.part").write_bytes(b"x" * 900)
    big = {"title": "Next", "id": "abc123def45", "filesize": 300}
    reason, _ = run_filter(tmp_path, monkeypatch, big, max_job_bytes=1000, resuming=True)
    assert reason and "單次任務上限" in reason
    reason, _ = run_filter(tmp_path, monkeypatch, {**big, "filesize": 150}, max_job_bytes=1000, resuming=True)
    assert reason is None


def test_a_kept_file_reported_again_is_not_counted_twice(fake_ydl: type[FakeYDL], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    kept = tmp_path / "out" / "first.f137.mp4"
    kept.parent.mkdir(parents=True)
    kept.write_bytes(b"x" * 800)
    captured: dict[str, Any] = {}

    def script(ydl: FakeYDL) -> Any:
        # yt-dlp reports a stream kept from the earlier run as finished, with only its total.
        ydl.opts["progress_hooks"][0]({"status": "finished", "filename": str(kept), "total_bytes": 800, "info_dict": {}})
        captured["reason"] = ydl.opts["match_filter"]({"title": "Next", "id": "abc123def45", "filesize": 150}, incomplete=False)
        return None

    monkeypatch.setattr(FakeYDL, "script", staticmethod(script))
    download_with_ytdlp("u", make_options(tmp_path, max_job_bytes=1000, resuming=True), Progress())
    assert captured["reason"] is None


def test_not_enough_disk_space_skips_the_video(fake_ydl: type[FakeYDL], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(downloader_module, "get_free_disk_bytes", lambda path: 100)
    reason, _ = run_filter(tmp_path, monkeypatch, {"title": "Big", "filesize": 2000})
    assert reason and "硬碟" in reason


def test_filters_apply_during_listing(fake_ydl: type[FakeYDL], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reason, outcome = run_filter(tmp_path, monkeypatch, {"title": "Cat"}, incomplete=True, filters=EntryFilters(title_exclude="cat"))
    assert reason
    assert outcome["skipped"] == [reason]


def test_same_reason_is_reported_once(fake_ydl: type[FakeYDL], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def script(ydl: FakeYDL) -> Any:
        info = {"title": "Live", "live_status": "is_live"}
        ydl.opts["match_filter"](info, incomplete=True)
        ydl.opts["match_filter"](info, incomplete=False)
        return None

    monkeypatch.setattr(FakeYDL, "script", staticmethod(script))
    outcome = download_with_ytdlp("u", make_options(tmp_path), Progress())
    assert len(outcome["skipped"]) == 1


def test_oversized_stream_without_announced_size_is_stopped(fake_ydl: type[FakeYDL], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def script(ydl: FakeYDL) -> Any:
        ydl.opts["progress_hooks"][0]({"status": "downloading", "downloaded_bytes": 5000, "info_dict": {}})
        return None

    monkeypatch.setattr(FakeYDL, "script", staticmethod(script))
    outcome = run_download("u", make_options(tmp_path, max_bytes=1000), Progress())
    assert "超過雲端模式上限" in outcome["errors"][0]


def test_archive_skips_are_counted(fake_ydl: type[FakeYDL], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def script(ydl: FakeYDL) -> Any:
        ydl.opts["logger"].debug("[download] abc: Title has already been recorded in the archive")
        return None

    monkeypatch.setattr(FakeYDL, "script", staticmethod(script))
    outcome = run_download("u", make_options(tmp_path), Progress())
    assert outcome["archived_skips"] == 1
    assert "已下載過" in outcome["skipped"][0]


def test_duplicate_files_are_listed_once(fake_ydl: type[FakeYDL], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def script(ydl: FakeYDL) -> Any:
        ydl.finish_video()
        return ydl.finish_video()

    monkeypatch.setattr(FakeYDL, "script", staticmethod(script))
    assert len(run_download("u", make_options(tmp_path), Progress())["items"]) == 1


def test_transcript_is_written_next_to_the_subtitle(fake_ydl: type[FakeYDL], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    subtitle = tmp_path / "out" / "video.en.srt"

    def script(ydl: FakeYDL) -> Any:
        subtitle.parent.mkdir(parents=True, exist_ok=True)
        subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n", encoding="utf-8")
        return ydl.finish_video(requested_subtitles={"en": {"filepath": str(subtitle)}})

    monkeypatch.setattr(FakeYDL, "script", staticmethod(script))
    outcome = download_with_ytdlp("u", make_options(tmp_path, write_transcript=True), Progress())
    assert outcome["items"][0]["extra_paths"] == [str(subtitle.with_suffix(".txt"))]
    assert subtitle.with_suffix(".txt").read_text(encoding="utf-8") == "Hello\n"


def test_login_cookies_never_stay_in_a_saved_info_file(fake_ydl: type[FakeYDL], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    info_file = tmp_path / "out" / "video.info.json"

    def script(ydl: FakeYDL) -> Any:
        info_file.parent.mkdir(parents=True, exist_ok=True)
        info_file.write_text(json.dumps({"id": "x", "formats": [{"url": "u", "cookies": "SID=secret"}]}), encoding="utf-8")
        return ydl.finish_video(infojson_filename=str(info_file))

    monkeypatch.setattr(FakeYDL, "script", staticmethod(script))
    options = make_options(tmp_path, write_infojson=True, login=LoginSource(browser="firefox"))
    download_with_ytdlp("u", options, Progress())
    saved = info_file.read_text(encoding="utf-8")
    assert "secret" not in saved
    assert json.loads(saved)["formats"] == [{"url": "u"}]


def test_strip_cookies_keeps_everything_else(tmp_path: Path) -> None:
    path = tmp_path / "a.info.json"
    path.write_text(json.dumps({"title": "標題", "cookies": "x", "nested": [{"cookies": "y", "keep": 1}]}), encoding="utf-8")
    strip_cookies_from_info_json(path)
    assert json.loads(path.read_text(encoding="utf-8")) == {"title": "標題", "nested": [{"keep": 1}]}


def test_collector_only_lists_files_that_exist(tmp_path: Path) -> None:
    items: list[Any] = []
    (tmp_path / "a.mp4").write_bytes(b"x")
    real_subtitle = tmp_path / "a.en.srt"
    real_subtitle.write_text("x", encoding="utf-8")
    processor = CollectDownloadedItemsPP(items, keep_thumbnails=True)
    processor.run(
        {
            "title": "A",
            "filepath": str(tmp_path / "a.mp4"),
            "requested_subtitles": {"en": {"filepath": str(real_subtitle)}, "de": {"filepath": str(tmp_path / "failed.srt")}, "fr": {}},
            "thumbnails": [{"filepath": str(tmp_path / "missing.webp")}, {}],
        }
    )
    assert items[0]["subtitle_paths"] == [str(real_subtitle)]
    assert items[0]["extra_paths"] == []


def test_a_failed_merge_is_not_reported_as_a_download(tmp_path: Path) -> None:
    items: list[Any] = []
    CollectDownloadedItemsPP(items).run({"title": "A", "filepath": str(tmp_path / "never-merged.mp4")})
    assert items == []


def test_missing_final_file_raises_so_the_archive_is_not_written(tmp_path: Path) -> None:
    from videocap.downloader import check_final_file

    existing = tmp_path / "a.mp4"
    existing.write_bytes(b"x")
    check_final_file(str(existing))
    with pytest.raises(FileNotFoundError):
        check_final_file(str(tmp_path / "missing.mp4"))


def test_stopping_still_records_the_file_being_written(fake_ydl: type[FakeYDL], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def script(ydl: FakeYDL) -> Any:
        ydl.opts["progress_hooks"][0]({"status": "downloading", "tmpfilename": "v.mp4.part", "filename": "v.mp4", "info_dict": {}})
        return None

    monkeypatch.setattr(FakeYDL, "script", staticmethod(script))
    outcome = run_download("u", make_options(tmp_path), Progress(), should_stop=lambda: True)
    assert outcome["stopped"] is True
    assert outcome["partial_paths"] == ["v.mp4.part", "v.mp4"]


def test_chapter_files_attach_to_their_video(tmp_path: Path) -> None:
    chapter = tmp_path / "01.mp4"
    chapter.write_bytes(b"x")
    items: list[Any] = [{"path": "other", "extra_paths": []}, {"path": "video.mp4", "extra_paths": []}]
    RecordChapterFilesPP(items).run({"filepath": "video.mp4", "chapters": [{"filepath": str(chapter)}, {"filepath": str(tmp_path / "gone.mp4")}]})
    assert items[1]["extra_paths"] == [str(chapter)]
    assert items[0]["extra_paths"] == []


def test_cutting_sponsors_is_wrapped_for_kept_files(fake_ydl: type[FakeYDL], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(downloader_module, "get_ffmpeg_status", lambda: (True, "ffmpeg"))
    monkeypatch.setattr(FakeYDL, "script", staticmethod(lambda ydl: None))
    options = make_options(
        tmp_path, embed_metadata=True, sponsorblock_mode="remove", sponsorblock_categories=("sponsor",), overwrite_existing=False
    )
    download_with_ytdlp("u", options, Progress())
    steps = fake_ydl.instances[0].post_processors["post_process"]
    names = [type(step).__name__ for step in steps]
    assert names == ["CollectDownloadedItemsPP", "KeptFileCutPP", "FFmpegMetadataPP"]
    assert type(steps[1].inner).__name__ == "ModifyChaptersPP"


def test_a_kept_file_cut_with_other_segments_is_left_as_it_is() -> None:
    from yt_dlp.utils import PostProcessingError

    class Step(CollectDownloadedItemsPP):
        def __init__(self) -> None:
            super().__init__([])
            self.ran = 0

        def run(self, information: Any) -> tuple[list[str], Any]:
            self.ran += 1
            information["chapters"] = ["rewritten"]
            raise PostProcessingError("Cannot cut video since the real and expected durations mismatch. Different chapters may have already been removed")

    step = Step()
    guarded = KeptFileCutPP(step)
    information = {"__real_download": False, "filepath": "x", "chapters": ["original"]}
    assert guarded.run(information) == ([], {"__real_download": False, "filepath": "x", "chapters": ["original"]})
    assert step.ran == 1
    # A fresh download that cannot be cut is a real failure.
    with pytest.raises(PostProcessingError):
        guarded.run({"__real_download": True, "filepath": "x"})


def test_progress_combines_a_videos_streams_into_one_bar() -> None:
    progress = Progress()
    tracker = ProgressTracker(progress)
    tracker.start_item({"title": "T", "requested_formats": [{}, {}]})

    video = {"vcodec": "avc1", "acodec": "none"}
    audio = {"vcodec": "none", "acodec": "mp4a"}
    tracker.on_stream_progress({"status": "downloading", "downloaded_bytes": 50, "total_bytes": 100, "info_dict": video})
    assert progress.calls[-1][0] == pytest.approx(0.25)
    assert "下載影像 50.0%" in progress.calls[-1][1]
    tracker.on_stream_progress({"status": "finished", "downloaded_bytes": 100, "total_bytes": 100, "info_dict": video})
    assert progress.calls[-1][0] == pytest.approx(0.5)
    tracker.on_stream_progress({"status": "downloading", "downloaded_bytes": 50, "total_bytes": 100, "speed": 1048576, "eta": 75, "info_dict": audio})
    assert progress.calls[-1][0] == pytest.approx(0.75)
    assert "下載音訊" in progress.calls[-1][1]
    assert "1.00 MB/s" in progress.calls[-1][1]
    assert "剩餘 01:15" in progress.calls[-1][1]


def test_progress_across_a_playlist() -> None:
    progress = Progress()
    tracker = ProgressTracker(progress)
    tracker.start_item({"title": "T"})
    info = {"vcodec": "avc1", "playlist_autonumber": 2, "n_entries": 4}
    tracker.on_stream_progress({"status": "downloading", "downloaded_bytes": 50, "total_bytes": 100, "info_dict": info})
    assert progress.calls[-1][0] == pytest.approx(0.375)
    assert progress.calls[-1][1].startswith("[2/4] ")


def test_subtitle_progress_does_not_move_the_bar() -> None:
    progress = Progress()
    tracker = ProgressTracker(progress)
    tracker.start_item({"title": "T"})
    subtitle = {"vcodec": None}
    tracker.on_stream_progress({"status": "downloading", "downloaded_bytes": 50, "total_bytes": 100, "info_dict": subtitle})
    assert progress.calls[-1][0] == 0.0
    tracker.on_stream_progress({"status": "finished", "downloaded_bytes": 100, "total_bytes": 100, "info_dict": subtitle})
    assert tracker.finished_streams == 0


def test_unknown_size_shows_bytes_instead_of_a_percentage() -> None:
    progress = Progress()
    tracker = ProgressTracker(progress)
    tracker.on_stream_progress({"status": "downloading", "downloaded_bytes": 2048, "info_dict": {"vcodec": "avc1"}})
    assert "2 KB" in progress.calls[-1][1]
    assert "%" not in progress.calls[-1][1]


def test_remove_partial_files_keeps_finished_ones(tmp_path: Path) -> None:
    finished = tmp_path / "done.mp4"
    stream = tmp_path / "video.f137.mp4"
    partial = tmp_path / "video.f140.m4a.part"
    fragment = tmp_path / "video [x].ts.part-Frag3"
    for path in (finished, stream, partial, fragment):
        path.write_bytes(b"x")
    remove_partial_files([str(finished), str(stream), str(tmp_path / "video.f140.m4a"), str(tmp_path / "video [x].ts")], keep={str(finished)})
    assert finished.exists()
    assert not stream.exists()
    assert not partial.exists()
    assert not fragment.exists()


def test_cancelling_never_removes_complete_files(tmp_path: Path) -> None:
    # Subtitles pass through the progress hook too, and a file kept from an earlier run is reported as finished.
    subtitle = tmp_path / "video.en.srt"
    earlier_video = tmp_path / "earlier.mp4"
    for path in (subtitle, earlier_video):
        path.write_bytes(b"x")
    remove_partial_files([str(subtitle), str(earlier_video)], keep=set())
    assert subtitle.exists()
    assert earlier_video.exists()


def test_subtitle_shortfall_messages() -> None:
    outcome = empty_outcome()
    outcome["warnings"] = ["Unable to download video subtitles for 'de': HTTP Error 429"]
    assert "de" in describe_subtitle_shortfall(outcome)

    outcome = empty_outcome()
    outcome["items"] = [{"subtitle_paths": []}]  # type: ignore[typeddict-item]
    assert "1 部影片沒有取得字幕" in describe_subtitle_shortfall(outcome)

    outcome = empty_outcome()
    outcome["items"] = [{"subtitle_paths": ["a.srt"]}]  # type: ignore[typeddict-item]
    assert describe_subtitle_shortfall(outcome) == ""


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("ERROR: [youtube] x: Sign in to confirm you're not a bot", "驗證身分"),
        ("ERROR: Sign in to confirm your age", "年齡限制"),
        ("ERROR: Private video", "私人影片"),
        ("ERROR: Join this channel to get access to members-only content", "會員專屬"),
        ("ERROR: HTTP Error 403: Forbidden", "拒絕"),
        ("ERROR: Unable to download: HTTP Error 416: Requested Range Not Satisfiable", "續傳失敗"),
        ("ERROR: Requested format is not available", "找不到符合條件"),
        ("ERROR: [Errno 28] No space left on device", "硬碟空間不足"),
        ("ERROR: [WinError 32] being used by another process", "其他程式"),
        ("ERROR: [Errno 36] File name too long", "太長"),
        ("ERROR: <urlopen error [Errno 11001] getaddrinfo failed>", "連不上"),
        ("ERROR: This playlist does not exist", "播放清單"),
    ],
)
def test_errors_get_helpful_hints(message: str, expected: str) -> None:
    assert expected in humanize_ytdlp_error(message)


NO_JS_RUNTIME_WARNING = (
    "[youtube] No supported JavaScript runtime could be found. Only deno is enabled by default; "
    "YouTube extraction without a JS runtime has been deprecated, and some formats may be missing."
)
N_CHALLENGE_WARNING = "[youtube] abc123def45: n challenge solving failed: Some formats may be missing."


def run_with_warning(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, warning: str, error: str = "", height: int = 0, **options: Any) -> Any:
    def script(ydl: FakeYDL) -> Any:
        ydl.opts["logger"].warning(warning)
        if error:
            ydl.opts["logger"].error(error)
            return None
        info = {"id": "abc123def45", "title": "Title", "height": height}
        assert ydl.opts["match_filter"](info, incomplete=False) is None
        return ydl.finish_video(height=height)

    monkeypatch.setattr(FakeYDL, "script", staticmethod(script))
    return run_download("u", make_options(tmp_path, **options), Progress())


@pytest.mark.parametrize("warning", [NO_JS_RUNTIME_WARNING, N_CHALLENGE_WARNING])
def test_a_missing_js_runtime_explains_a_failed_download(fake_ydl: type[FakeYDL], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, warning: str) -> None:
    outcome = run_with_warning(monkeypatch, tmp_path, warning, error="ERROR: [youtube] abc123def45: Requested format is not available")
    assert outcome["notes"] == []
    assert len(outcome["errors"]) == 1
    assert "找不到符合條件" in outcome["errors"][0]
    assert "JavaScript" in outcome["errors"][0] and "安裝 Deno" in outcome["errors"][0]
    assert "pip" not in outcome["errors"][0]
    assert outcome["failed_ids"] == ["abc123def45"]


def test_a_missing_js_runtime_explains_a_lower_quality(fake_ydl: type[FakeYDL], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    outcome = run_with_warning(monkeypatch, tmp_path, N_CHALLENGE_WARNING, height=360, max_height=1080)
    assert len(outcome["items"]) == 1
    # The download succeeded, so the hint is a note rather than a failure.
    assert outcome["errors"] == []
    assert len(outcome["notes"]) == 1
    assert "360p" in outcome["notes"][0] and "JavaScript" in outcome["notes"][0]


@pytest.mark.parametrize(("warning", "height"), [(N_CHALLENGE_WARNING, 1080), ("Some other warning", 360)])
def test_a_download_at_the_chosen_quality_gets_no_js_hint(
    fake_ydl: type[FakeYDL], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, warning: str, height: int
) -> None:
    outcome = run_with_warning(monkeypatch, tmp_path, warning, height=height, max_height=1080)
    assert (outcome["errors"], outcome["notes"]) == ([], [])


def test_every_outcome_has_a_notes_list(fake_ydl: type[FakeYDL], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert empty_outcome()["notes"] == []
    monkeypatch.setattr(FakeYDL, "script", staticmethod(lambda ydl: (_ for _ in ()).throw(RuntimeError("boom"))))
    assert run_download("u", make_options(tmp_path), Progress())["notes"] == []


@pytest.mark.parametrize("also_finished", [True, False])
def test_the_js_hint_is_not_added_to_an_unrelated_failure(
    fake_ydl: type[FakeYDL], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, also_finished: bool
) -> None:
    def script(ydl: FakeYDL) -> Any:
        ydl.opts["logger"].warning(N_CHALLENGE_WARNING)
        ydl.opts["logger"].error("ERROR: [youtube] zzzzzzzzzzz: Private video. Sign in if you've been granted access")
        return ydl.finish_video() if also_finished else None

    monkeypatch.setattr(FakeYDL, "script", staticmethod(script))
    outcome = run_download("u", make_options(tmp_path), Progress())
    assert len(outcome["errors"]) == 1 and "JavaScript" not in outcome["errors"][0]


def test_unknown_errors_pass_through_without_their_prefix() -> None:
    assert humanize_ytdlp_error("ERROR: something new") == "something new"


def test_failed_video_id() -> None:
    assert extract_failed_video_id("ERROR: [youtube] abc123def45: Video unavailable") == "abc123def45"
    assert extract_failed_video_id("ERROR: no id here") is None


def test_job_stopped_passes_through_yt_dlp() -> None:
    # yt-dlp's wrappers re-raise only this family even with ignoreerrors.
    from yt_dlp.utils import DownloadCancelled

    assert issubclass(JobStopped, DownloadCancelled)


def test_waiting_between_videos_can_be_stopped(fake_ydl: type[FakeYDL], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import time as time_module

    stop = {"now": False}

    def script(ydl: FakeYDL) -> Any:
        ydl.opts["match_filter"]({"id": "first", "title": "1"}, incomplete=False)
        stop["now"] = True
        # The second video would wait an hour; the stop must end that at once.
        ydl.opts["match_filter"]({"id": "second", "title": "2"}, incomplete=False)
        raise AssertionError("The wait should have been stopped")

    monkeypatch.setattr(FakeYDL, "script", staticmethod(script))
    started = time_module.monotonic()
    outcome = run_download("u", make_options(tmp_path, wait_range=(3600.0, 3600.0)), Progress(), should_stop=lambda: stop["now"])
    assert outcome["stopped"] is True
    assert time_module.monotonic() - started < 5


def test_no_wait_before_the_first_video_or_between_clips(fake_ydl: type[FakeYDL], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    progress = Progress()

    def script(ydl: FakeYDL) -> Any:
        ydl.opts["match_filter"]({"id": "first", "title": "1"}, incomplete=False)
        ydl.opts["match_filter"]({"id": "first", "title": "1"}, incomplete=False)
        return None

    monkeypatch.setattr(FakeYDL, "script", staticmethod(script))
    download_with_ytdlp("u", make_options(tmp_path, wait_range=(3600.0, 3600.0)), progress)
    assert not any("等待" in text for _, text in progress.calls)


# ---- The real yt-dlp pipeline, run offline ----


def download_link(url: str, output_dir: Path, limits: Optional[AccessLimits] = None, **settings: Any) -> Any:
    spec = JobSpec(url=url, settings=DownloadSettings(**settings), output_dir=str(output_dir))
    return run_download(url, options_from_spec(spec, limits or AccessLimits()), Progress())


@pytest.mark.parametrize("overwrite_existing", [True, False])
def test_a_different_video_with_the_same_title_never_replaces_an_earlier_download(
    offline_youtube: OfflineYouTube, tmp_path: Path, overwrite_existing: bool
) -> None:
    first = offline_youtube.add_video("courseAAAA1", "Introduction")
    second = offline_youtube.add_video(
        "courseBBBB2", "Introduction", formats=[offline_youtube.video_format("vp9"), offline_youtube.audio_format("opus")]
    )
    first_outcome = download_link(first, tmp_path, overwrite_existing=overwrite_existing)
    first_path = Path(first_outcome["items"][0]["path"])
    assert first_path.name == "Introduction.mp4"

    second_outcome = download_link(second, tmp_path, overwrite_existing=overwrite_existing)
    second_path = Path(second_outcome["items"][0]["path"])
    assert second_path != first_path
    assert "courseBBBB2" in second_path.name
    assert stream_codecs(first_path) == ["h264", "aac"]
    assert stream_codecs(second_path) == ["vp9", "opus"]

    # The same video again still goes to its own name, so overwriting and resuming keep working.
    again = download_link(first, tmp_path, overwrite_existing=overwrite_existing)
    assert Path(again["items"][0]["path"]) == first_path
    assert sorted(path.name for path in tmp_path.iterdir()) == ["Introduction [courseBBBB2].mp4", "Introduction.mp4"]


class PauseAt(Progress):
    """Presses pause while a step runs, the way a person watching the status line would."""

    def __init__(self, label: str) -> None:
        super().__init__()
        self.label = label
        self.paused = False

    def progress(self, value: float, text: str = "") -> None:
        super().progress(value, text)
        self.paused = self.paused or text == self.label

    def should_stop(self) -> bool:
        return self.paused


@pytest.mark.parametrize(
    ("label", "settings", "final_name", "sources"),
    [
        ("正在合併影像與音訊...", {"container": "mkv"}, "Merge.mkv", ["Merge.f137.mp4", "Merge.f251.webm"]),
        ("正在轉換音訊...", {"media_type": "audio", "audio_format": "mp3"}, "Merge.mp3", ["Merge.webm", "Merge.m4a"]),
    ],
)
def test_pausing_during_a_step_leaves_no_source_streams_behind(
    offline_youtube: OfflineYouTube, tmp_path: Path, label: str, settings: dict[str, Any], final_name: str, sources: list[str]
) -> None:
    url = offline_youtube.add_video("mergeVid001", "Merge", formats=[offline_youtube.video_format(), offline_youtube.audio_format("opus")])
    spec = JobSpec(
        url=url,
        settings=DownloadSettings(download_subtitles=False, embed_metadata=False, embed_chapters=False, **settings),
        output_dir=str(tmp_path),
    )
    pause = PauseAt(label)
    paused = run_download(url, options_from_spec(spec, AccessLimits()), pause, should_stop=pause.should_stop)
    assert paused["stopped"] is True

    resumed = run_download(url, options_from_spec(spec.as_resuming(), AccessLimits()), Progress())
    assert [Path(item["path"]).name for item in resumed["items"]] == [final_name]
    leftovers = [name for name in sources if (tmp_path / name).exists()]
    assert leftovers == []


def test_sponsor_segments_are_still_cut_after_a_pause(offline_youtube: OfflineYouTube, tmp_path: Path) -> None:
    url = offline_youtube.add_video("sponsorVid1", "Sponsored")
    offline_youtube.sponsor_segments["sponsorVid1"] = [
        {"segment": [4.0, 7.0], "category": "sponsor", "actionType": "skip", "videoDuration": 10, "UUID": "u", "description": ""}
    ]
    settings = DownloadSettings(sponsorblock_mode="remove", download_subtitles=False)
    spec = JobSpec(url=url, settings=settings, output_dir=str(tmp_path))
    pause = PauseAt("正在合併影像與音訊...")
    assert run_download(url, options_from_spec(spec, AccessLimits()), pause, should_stop=pause.should_stop)["stopped"]

    resumed = run_download(url, options_from_spec(spec.as_resuming(), AccessLimits()), Progress())
    assert resumed["errors"] == []
    assert duration_of(Path(resumed["items"][0]["path"])) == pytest.approx(7.0, abs=0.6)

    # Running the same job once more must not cut the already cut file again.
    again = run_download(url, options_from_spec(spec.as_resuming(), AccessLimits()), Progress())
    assert again["errors"] == []
    assert duration_of(Path(again["items"][0]["path"])) == pytest.approx(7.0, abs=0.6)


def run_blocking_ffmpeg(output: Path, realtime: bool = True) -> Callable[[FakeYDL], Any]:
    """Behaves like yt-dlp's clip download: one ffmpeg run that reports nothing until it ends."""

    def script(ydl: FakeYDL) -> Any:
        import yt_dlp.downloader.external as external

        pacing = ["-re"] if realtime else []
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *pacing, "-f", "lavfi", "-i", "testsrc=duration=60:size=320x240:rate=25"]
        with external.Popen([*command, "-c:v", "mpeg2video", "-q:v", "1", "-f", "mpegts", f"file:{output}"]) as process:
            if process.wait() != 0:
                ydl.opts["logger"].error("ERROR: ffmpeg exited with code 1")
        return None

    return script


@needs_ffmpeg
def test_stopping_ends_a_clip_download_at_once(fake_ydl: type[FakeYDL], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import threading
    import time as time_module

    part = tmp_path / "clip.mp4.part"
    monkeypatch.setattr(FakeYDL, "script", staticmethod(run_blocking_ffmpeg(part)))
    stop = threading.Event()
    threading.Timer(1.0, stop.set).start()
    started = time_module.monotonic()
    outcome = run_download("u", make_options(tmp_path), Progress(), should_stop=stop.is_set)
    assert time_module.monotonic() - started < 10
    assert outcome["stopped"] is True
    assert outcome["errors"] == []
    assert not part.exists()


@needs_ffmpeg
def test_a_clip_growing_past_the_size_limit_is_ended(fake_ydl: type[FakeYDL], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    part = tmp_path / "clip.mp4.part"
    monkeypatch.setattr(FakeYDL, "script", staticmethod(run_blocking_ffmpeg(part, realtime=False)))
    outcome = run_download("u", make_options(tmp_path, max_bytes=200_000), Progress())
    assert len(outcome["errors"]) == 1
    assert "超過雲端模式上限" in outcome["errors"][0]
    assert outcome["stopped"] is False
    assert not part.exists()


def test_a_short_clip_of_a_long_video_fits_the_size_limit(fake_ydl: type[FakeYDL], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # yt-dlp hands each clip the size of the whole video.
    lecture = {"title": "Lecture", "id": "lecture0001", "duration": 7200, "filesize_approx": 1_600_000_000}
    reason, _ = run_filter(tmp_path, monkeypatch, {**lecture, "section_start": 600.0, "section_end": 620.0}, max_bytes=1024**3)
    assert reason is None
    reason, _ = run_filter(tmp_path, monkeypatch, {**lecture, "section_start": 0.0, "section_end": None}, max_bytes=1024**3)
    assert reason and "超過雲端模式上限" in reason


def test_a_clip_starting_after_the_video_ends_is_skipped(fake_ydl: type[FakeYDL], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    info = {"title": "Short", "id": "short000001", "duration": 30}
    reason, outcome = run_filter(tmp_path, monkeypatch, {**info, "section_start": 40.0, "section_end": None})
    assert reason and "超過影片長度" in reason
    assert outcome["skipped"] == [reason]
    reason, _ = run_filter(tmp_path, monkeypatch, {**info, "section_start": 20.0, "section_end": 25.0})
    assert reason is None


def test_transcript_only_videos_are_archived_once_their_captions_arrive(offline_youtube: OfflineYouTube, tmp_path: Path) -> None:
    url = offline_youtube.add_video("transcr0001", "Fresh upload")
    archive = tmp_path / "archive.txt"
    spec = JobSpec(
        url=url,
        settings=DownloadSettings(media_type="none", include_auto_subtitles=True, write_transcript=True),
        output_dir=str(tmp_path / "out"),
        use_archive=True,
    )

    def check() -> Any:
        return run_download(url, options_from_spec(spec, AccessLimits(), archive_path=archive), Progress())

    # YouTube has not made the captions yet, so the next check must try again.
    first = check()
    assert first["items"][0]["subtitle_paths"] == []
    assert not archive.exists() or archive.read_text(encoding="utf-8") == ""

    offline_youtube.add_video("transcr0001", "Fresh upload", subtitles=True)
    second = check()
    assert [Path(path).name for path in second["items"][0]["subtitle_paths"]] == ["Fresh upload.en.srt"]
    assert archive.read_text(encoding="utf-8").split() == ["youtube", "transcr0001"]

    third = check()
    assert third["items"] == []
    assert third["archived_skips"] == 1


def add_numbered_playlist(site: OfflineYouTube, count: int, title_of: Callable[[int], str] = lambda number: f"Video {number}") -> str:
    urls = [site.add_video(f"listVid{number:04d}", title_of(number)) for number in range(1, count + 1)]
    return site.add_playlist("PLtest", urls)


def downloaded_titles(outcome: Any) -> list[str]:
    return [item["title"] for item in outcome["items"]]


def test_reverse_order_starts_from_the_end_of_the_list(offline_youtube: OfflineYouTube, tmp_path: Path) -> None:
    url = add_numbered_playlist(offline_youtube, 8)
    outcome = download_link(url, tmp_path, media_type="none", download_subtitles=False, playlist_limit=3, playlist_reverse=True)
    assert downloaded_titles(outcome) == ["Video 8", "Video 7", "Video 6"]


def test_filters_do_not_shrink_the_number_of_downloads(offline_youtube: OfflineYouTube, tmp_path: Path) -> None:
    url = add_numbered_playlist(offline_youtube, 8, lambda number: f"Trailer {number}" if number <= 4 else f"Episode {number}")
    outcome = download_link(url, tmp_path, media_type="none", download_subtitles=False, playlist_limit=3, filter_title_exclude="Trailer")
    assert downloaded_titles(outcome) == ["Episode 5", "Episode 6", "Episode 7"]


def test_picked_videos_are_found_by_id_wherever_they_now_are(offline_youtube: OfflineYouTube, tmp_path: Path) -> None:
    url = add_numbered_playlist(offline_youtube, 8)
    spec = JobSpec(
        url=url,
        settings=DownloadSettings(media_type="none", download_subtitles=False),
        output_dir=str(tmp_path),
        video_ids=["listVid0007", "listVid0003"],
        playlist_limit=2,
    )
    outcome = run_download(url, options_from_spec(spec, AccessLimits()), Progress())
    assert sorted(downloaded_titles(outcome)) == ["Video 3", "Video 7"]
    # Passing over the videos nobody picked is not news.
    assert outcome["skipped"] == []


def test_the_job_log_is_not_flooded_with_progress_lines(offline_youtube: OfflineYouTube, tmp_path: Path) -> None:
    url = offline_youtube.add_video("progress001", "Progress")
    spec = JobSpec(url=url, settings=DownloadSettings(download_subtitles=False), output_dir=str(tmp_path))
    progress, log = Progress(), []
    run_download(url, options_from_spec(spec, AccessLimits()), progress, on_log=log.append)
    assert not [line for line in log if line.startswith("[download]") and "%" in line]
    # The progress bar still gets every update.
    assert any("下載影像" in text for _, text in progress.calls)


def test_chapter_files_of_same_titled_videos_stay_apart_and_follow_the_folder_rule(offline_youtube: OfflineYouTube, tmp_path: Path) -> None:
    chapters = [{"start_time": 0.0, "end_time": 4.0, "title": "Opening"}, {"start_time": 4.0, "end_time": 10.0, "title": "Main"}]
    urls = [offline_youtube.add_video(video_id, "Weekly live", chapters=chapters, channel="Show") for video_id in ("weekly00001", "weekly00002")]
    playlist = offline_youtube.add_playlist("PLweekly", urls)
    outcome = download_link(playlist, tmp_path, split_chapters=True, download_subtitles=False, folder_rule="channel")
    chapter_files = [extra for item in outcome["items"] for extra in item["extra_paths"]]
    assert len(chapter_files) == 4
    assert len(set(chapter_files)) == 4
    assert all(Path(path).is_file() for path in chapter_files)
    assert all(Path(path).relative_to(tmp_path).parts[0] == "Show" for path in chapter_files)


class FakeRecorder:
    """Stands in for record_live_stream: records until the job is stopped or told to end."""

    def __init__(self) -> None:
        import threading

        self.started: list[str] = []
        self.started_after_stop: list[str] = []
        self.started_event = threading.Event()
        self.end = threading.Event()

    def __call__(self, sources: Any, output_path: Path, ffmpeg: str, progress_ui: Any, should_stop: Any, *args: Any) -> Any:
        self.started.append(output_path.name)
        if should_stop and should_stop():
            self.started_after_stop.append(output_path.name)
        self.started_event.set()
        while not self.end.wait(0.05):
            if should_stop and should_stop():
                break
        output_path.write_bytes(b"recording")
        return downloader_module.RecordingResult(output_path)


def live_entry(video_id: str, **extra: Any) -> dict[str, Any]:
    return {"title": f"Live {video_id}", "id": video_id, "live_status": "is_live", "url": "http://live", "ext": "mp4", **extra}


@pytest.fixture
def recorder(fake_ydl: type[FakeYDL], monkeypatch: pytest.MonkeyPatch) -> FakeRecorder:
    monkeypatch.setattr(downloader_module, "get_ffmpeg_status", lambda: (True, "ffmpeg"))
    fake = FakeRecorder()
    monkeypatch.setattr(downloader_module, "record_live_stream", fake)
    return fake


def test_a_live_stream_in_a_list_is_recorded_at_once(recorder: FakeRecorder, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, bool] = {}

    def script(ydl: FakeYDL) -> Any:
        ydl.output = tmp_path / "out" / "Live.mp4"
        ydl.opts["match_filter"](live_entry("live0000001"), incomplete=False)
        # yt-dlp now downloads the list's other videos, which can take hours; the stream must not wait for them.
        seen["recording during other downloads"] = recorder.started_event.wait(5)
        recorder.end.set()
        return None

    monkeypatch.setattr(FakeYDL, "script", staticmethod(script))
    outcome = run_download("u", make_options(tmp_path, live_mode="record"), Progress())
    assert seen["recording during other downloads"] is True
    assert len(outcome["items"]) == 1


def test_each_live_stream_is_recorded_once_and_never_in_clips(recorder: FakeRecorder, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reasons: list[Optional[str]] = []

    def script(ydl: FakeYDL) -> Any:
        ydl.output = tmp_path / "out" / "Live.mp4"
        reasons.append(ydl.opts["match_filter"](live_entry("live0000001", section_start=10.0), incomplete=False))
        ydl.output = tmp_path / "out" / "Other.mp4"
        for _ in range(3):
            ydl.opts["match_filter"](live_entry("live0000002"), incomplete=False)
        recorder.end.set()
        return None

    monkeypatch.setattr(FakeYDL, "script", staticmethod(script))
    outcome = run_download("u", make_options(tmp_path, live_mode="record"), Progress())
    assert reasons[0] and "片段" in reasons[0]
    assert reasons[0] in outcome["skipped"]
    assert recorder.started == ["Other.mp4"]
    assert len(outcome["items"]) == 1


def test_stopping_ends_all_recordings_together(recorder: FakeRecorder, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    stop = threading.Event()

    def script(ydl: FakeYDL) -> Any:
        for video_id in ("live0000001", "live0000002"):
            ydl.output = tmp_path / "out" / f"{video_id}.mp4"
            ydl.opts["match_filter"](live_entry(video_id), incomplete=False)
        return None

    monkeypatch.setattr(FakeYDL, "script", staticmethod(script))
    threading.Timer(0.5, stop.set).start()
    outcome = run_download("u", make_options(tmp_path, live_mode="record"), Progress(), should_stop=stop.is_set)
    # Both were recording side by side, so the stop leaves two real recordings rather than an empty second one.
    assert sorted(recorder.started) == ["live0000001.mp4", "live0000002.mp4"]
    assert recorder.started_after_stop == []
    assert len(outcome["items"]) == 2


# ---- What the app writes beside a video ----


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    import csv

    # Starts with a byte order mark, which Excel needs to read UTF-8.
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


SAMPLE_COMMENTS = [
    {"id": "c1", "author": "early", "text": "first!", "like_count": 50, "timestamp": 1000, "_time_text": "1 年前"},
    {"id": "c2", "author": "sneaky", "text": "=HYPERLINK(\"http://x\")", "like_count": 1, "timestamp": 3000, "_time_text": "1 天前"},
    {"id": "c3", "author": "fan", "text": "好聽", "like_count": 9, "timestamp": 2000, "_time_text": "1 個月前"},
]


@pytest.mark.parametrize("media_type", ["video", "none"])
def test_comments_are_saved_beside_the_video(offline_youtube: OfflineYouTube, tmp_path: Path, media_type: str) -> None:
    url = offline_youtube.add_video("comments001", "Talk", comments=SAMPLE_COMMENTS)
    outcome = download_link(url, tmp_path, media_type=media_type, download_subtitles=False, comments_mode="new", comments_max=2)
    assert outcome["errors"] == []
    saved = tmp_path / "Talk.comments.csv"
    assert str(saved) in outcome["items"][0]["extra_paths"]
    rows = read_csv_rows(saved)
    # Newest first, and only as many as asked for.
    assert [row["作者"] for row in rows] == ["sneaky", "fan"]
    assert list(rows[0]) == ["作者", "留言", "按讚數", "時間", "網址"]
    # A spreadsheet must not run a stranger's comment as a formula.
    assert rows[0]["留言"].startswith("'=")
    assert rows[1]["網址"] == "https://www.youtube.com/watch?v=comments001&lc=c3"


def test_text_cut_inside_an_emoji_still_saves_the_comments_and_the_nfo(offline_youtube: OfflineYouTube, tmp_path: Path) -> None:
    from xml.etree import ElementTree

    # YouTube's JSON can hold half of an emoji's UTF-16 pair, and Python keeps it as a lone surrogate.
    comments = [{"id": "c1", "author": "cut\ud83d", "text": "好\ud83d", "like_count": 1, "timestamp": 1, "_time_text": "1 天前"}]
    url = offline_youtube.add_video("surrogate01", "Cut", comments=comments, description="說明\ud83d")
    outcome = download_link(url, tmp_path, download_subtitles=False, comments_mode="top", write_nfo=True)
    assert outcome["errors"] == [] and outcome["notes"] == []
    names = sorted(Path(path).name for path in outcome["items"][0]["extra_paths"])
    assert names == ["Cut.comments.csv", "Cut.nfo"]
    assert read_csv_rows(tmp_path / "Cut.comments.csv")[0]["留言"].startswith("好")
    assert ElementTree.parse(tmp_path / "Cut.nfo").getroot().find("plot").text.startswith("說明")  # type: ignore[union-attr]


def test_comments_are_not_fetched_unless_asked(offline_youtube: OfflineYouTube, tmp_path: Path) -> None:
    url = offline_youtube.add_video("comments002", "Quiet", comments=SAMPLE_COMMENTS)
    outcome = download_link(url, tmp_path, download_subtitles=False)
    assert outcome["items"][0]["extra_paths"] == []
    assert not list(tmp_path.glob("*.csv"))


def chat_line(offset_ms: int, renderer: str, **fields: Any) -> str:
    item = {renderer: {"authorName": {"simpleText": fields.pop("author", "viewer")}, **fields}}
    return json.dumps(
        {"replayChatItemAction": {"actions": [{"addChatItemAction": {"item": item}}], "videoOffsetTimeMsec": str(offset_ms)}},
        ensure_ascii=False,
    )


def write_chat_replay(path: Path) -> Path:
    lines = [
        chat_line(-5000, "liveChatTextMessageRenderer", author="早到的人", message={"runs": [{"text": "等開台 "}, {"emoji": {"emojiId": "😀"}}]}),
        chat_line(
            3_725_000,
            "liveChatPaidMessageRenderer",
            author="大方的人",
            message={"runs": [{"text": "加油"}, {"emoji": {"emojiId": "UCx/abc", "shortcuts": [":yougotthis:"], "isCustomEmoji": True}}]},
            purchaseAmountText={"simpleText": "NT$150.00"},
        ),
        chat_line(61_000, "liveChatPaidStickerRenderer", author="貼圖的人", purchaseAmountText={"simpleText": "NT$30.00"}),
        chat_line(62_000, "liveChatTextMessageRenderer", author="=cmd", message={"runs": [{"text": "=1+1"}]}),
        # Not something a person wrote.
        json.dumps({"replayChatItemAction": {"actions": [{"addLiveChatTickerItemAction": {}}], "videoOffsetTimeMsec": "1"}}),
        "not json",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def add_past_stream(site: OfflineYouTube, folder: Path, video_id: str = "pastLive001", live_status: str = "was_live") -> str:
    chat = write_chat_replay(folder / f"{video_id}.chat.json")
    subtitles = {
        "en": [{"ext": "srt", "url": site.url_of("sub.en.srt")}],
        "live_chat": [{"ext": "json", "url": chat.as_uri()}],
    }
    url = site.add_video(video_id, "Stream", live_status=live_status, was_live=live_status == "was_live")
    site.videos[url]["subtitles"] = subtitles
    return url


def test_a_past_streams_chat_becomes_a_readable_table(offline_youtube: OfflineYouTube, tmp_path: Path) -> None:
    url = add_past_stream(offline_youtube, tmp_path)
    out = tmp_path / "out"
    outcome = download_link(url, out, save_live_chat=True, subtitle_languages="en")
    assert outcome["errors"] == []
    item = outcome["items"][0]
    table = out / "Stream.live_chat.csv"
    assert item["extra_paths"] == [str(table)]
    # The chat is not a subtitle, so it is neither listed nor embedded as one.
    assert [Path(path).name for path in item["subtitle_paths"]] == ["Stream.en.srt"]
    assert not any("JSON" in warning for warning in outcome["warnings"])
    rows = read_csv_rows(table)
    assert list(rows[0]) == ["時間", "作者", "留言", "Super Chat 金額"]
    assert [(row["時間"], row["作者"], row["留言"], row["Super Chat 金額"]) for row in rows] == [
        ("'-0:05", "早到的人", "等開台 😀", ""),
        ("1:02:05", "大方的人", "加油:yougotthis:", "NT$150.00"),
        ("1:01", "貼圖的人", "", "NT$30.00"),
        ("1:02", "'=cmd", "'=1+1", ""),
    ]
    # YouTube's own JSON is only kept with the 影片資訊 file.
    assert not (out / "Stream.live_chat.json").exists()


def test_the_chats_json_is_kept_with_the_video_information(offline_youtube: OfflineYouTube, tmp_path: Path) -> None:
    url = add_past_stream(offline_youtube, tmp_path)
    out = tmp_path / "out"
    outcome = download_link(url, out, media_type="none", download_subtitles=False, save_live_chat=True, write_infojson=True)
    assert outcome["errors"] == []
    names = sorted(Path(path).name for path in outcome["items"][0]["extra_paths"])
    assert names == ["Stream.info.json", "Stream.live_chat.csv", "Stream.live_chat.json"]
    assert outcome["items"][0]["subtitle_paths"] == []


def test_a_chat_left_by_an_interrupted_download_still_becomes_a_table(offline_youtube: OfflineYouTube, tmp_path: Path) -> None:
    url = add_past_stream(offline_youtube, tmp_path, video_id="pastLive002")
    chat = tmp_path / "pastLive002.chat.json"
    good = chat_line(1000, "liveChatTextMessageRenderer", author="viewer", message={"runs": [{"text": "hi"}]})
    odd = [
        # YouTube's shapes change; none of these may end the job.
        json.dumps({"replayChatItemAction": {"actions": "not a list", "videoOffsetTimeMsec": "1"}}),
        json.dumps({"replayChatItemAction": {"actions": [{"addChatItemAction": {"item": {"liveChatTextMessageRenderer": "text"}}}]}}),
        json.dumps({"replayChatItemAction": {"actions": [{"addChatItemAction": {"item": {"liveChatTextMessageRenderer": {"message": {"runs": ["x", None]}}}}}]}}),
        json.dumps({"replayChatItemAction": {"actions": [{"addChatItemAction": {"item": {"liveChatTextMessageRenderer": {"message": {"runs": [{"emoji": {"shortcuts": 5, "isCustomEmoji": True}}]}}}}}]}}),
        json.dumps([1, 2]),
    ]
    chat.write_text("\n".join([good, *odd, good[: len(good) // 2]]), encoding="utf-8")
    out = tmp_path / "out"
    outcome = download_link(url, out, save_live_chat=True, subtitle_languages="en")
    assert outcome["errors"] == []
    assert [Path(item["path"]).name for item in outcome["items"]] == ["Stream.mp4"]
    rows = read_csv_rows(out / "Stream.live_chat.csv")
    assert (rows[0]["作者"], rows[0]["留言"]) == ("viewer", "hi")


def test_a_long_chat_is_turned_into_a_table_line_by_line(tmp_path: Path) -> None:
    import tracemalloc

    from videocap.sidecars import write_live_chat_csv

    line = chat_line(1000, "liveChatTextMessageRenderer", author="viewer", message={"runs": [{"text": "加油 " * 20}]})
    chat = tmp_path / "Stream.live_chat.json"
    chat.write_text((line + "\n") * 20000, encoding="utf-8")
    tracemalloc.start()
    try:
        table = write_live_chat_csv(chat)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert len(read_csv_rows(table)) == 20000
    # A stream of many hours has millions of messages; holding them all would take far more than the file itself.
    assert peak < chat.stat().st_size / 10


@pytest.mark.parametrize(
    ("name", "settings"),
    [
        ("write_nfo", {"write_nfo": True}),
        ("write_comments_csv", {"comments_mode": "top"}),
        ("write_traditional_copy", {"subtitles_to_traditional": True}),
        ("write_jpg_cover", {"media_type": "audio", "audio_format": "mp3", "embed_thumbnail": True, "music_tags": True}),
        ("write_music_tags", {"media_type": "audio", "split_chapters": True, "music_tags": True}),
        ("set_file_times", {"mtime_upload_date": True}),
    ],
)
def test_a_side_step_that_breaks_is_a_note_not_a_failed_download(
    offline_youtube: OfflineYouTube, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, settings: dict[str, Any]
) -> None:
    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("something unexpected")

    monkeypatch.setattr(downloader_module, name, broken)
    chapters = [{"start_time": 0.0, "end_time": 4.0, "title": "One"}, {"start_time": 4.0, "end_time": 10.0, "title": "Two"}]
    url = add_chinese_video(offline_youtube, tmp_path, "sideStep001", {"zh-Hans": "视频"})
    offline_youtube.videos[url].update(comments=SAMPLE_COMMENTS, chapters=chapters, thumbnails=[{"url": offline_youtube.url_of("thumb.jpg"), "id": "0"}])
    out = tmp_path / "out"
    outcome = download_link(url, out, subtitle_languages="zh-Hans", **settings)
    assert outcome["errors"] == []
    assert len(outcome["items"]) == 1 and Path(outcome["items"][0]["path"]).is_file()
    assert any("something unexpected" in note for note in outcome["notes"]), outcome["notes"]


@pytest.mark.parametrize(("save_live_chat", "live_status"), [(False, "was_live"), (True, "not_live")])
def test_the_chat_is_only_saved_for_a_past_stream_when_asked(
    offline_youtube: OfflineYouTube, tmp_path: Path, save_live_chat: bool, live_status: str
) -> None:
    url = add_past_stream(offline_youtube, tmp_path, live_status=live_status)
    out = tmp_path / "out"
    outcome = download_link(url, out, save_live_chat=save_live_chat, subtitle_languages="all")
    assert outcome["errors"] == []
    assert sorted(path.name for path in out.iterdir()) == ["Stream.en.srt", "Stream.mp4"]


def add_chinese_video(site: OfflineYouTube, folder: Path, video_id: str, tracks: dict[str, str]) -> str:
    subtitles: dict[str, Any] = {}
    for language, text in tracks.items():
        path = folder / f"{video_id}.{language}.srt"
        path.write_text(f"1\n00:00:01,000 --> 00:00:02,000\n{text}\n", encoding="utf-8")
        subtitles[language] = [{"ext": "srt", "url": path.as_uri()}]
    url = site.add_video(video_id, "Lesson")
    site.videos[url]["subtitles"] = subtitles
    return url


def test_simplified_subtitles_get_a_taiwan_traditional_copy(offline_youtube: OfflineYouTube, tmp_path: Path) -> None:
    url = add_chinese_video(offline_youtube, tmp_path, "chinese0001", {"zh-Hans": "这个软件的视频", "en": "This video"})
    out = tmp_path / "out"
    outcome = download_link(url, out, subtitles_to_traditional=True, subtitle_languages="zh-Hans,en", write_transcript=True)
    assert outcome["errors"] == []
    item = outcome["items"][0]
    copy = out / "Lesson.zh-TW.srt"
    assert sorted(Path(path).name for path in item["subtitle_paths"]) == ["Lesson.en.srt", "Lesson.zh-Hans.srt", "Lesson.zh-TW.srt"]
    # Taiwanese wording, not just Traditional characters; the original stays.
    assert "這個軟體的影片" in copy.read_text(encoding="utf-8")
    assert "这个软件的视频" in (out / "Lesson.zh-Hans.srt").read_text(encoding="utf-8")
    # The transcript and the embedded tracks get the Traditional copy too.
    assert (out / "Lesson.zh-TW.txt").read_text(encoding="utf-8").strip() == "這個軟體的影片"
    assert stream_codecs(Path(item["path"])).count("mov_text") == 3


def test_a_real_traditional_track_needs_no_copy(offline_youtube: OfflineYouTube, tmp_path: Path) -> None:
    url = add_chinese_video(offline_youtube, tmp_path, "chinese0002", {"zh-Hans": "视频", "zh-TW": "創作者自己翻的影片"})
    out = tmp_path / "out"
    outcome = download_link(url, out, subtitles_to_traditional=True, subtitle_languages="zh-TW,zh-Hans", embed_subtitles=False)
    assert outcome["errors"] == []
    assert "創作者自己翻的影片" in (out / "Lesson.zh-TW.srt").read_text(encoding="utf-8")
    assert len(outcome["items"][0]["subtitle_paths"]) == 2


def test_automatic_simplified_captions_get_a_traditional_copy_too(offline_youtube: OfflineYouTube, tmp_path: Path) -> None:
    url = add_chinese_video(offline_youtube, tmp_path, "chinese0005", {"zh-Hans": "视频"})
    video = offline_youtube.videos[url]
    video["automatic_captions"] = video.pop("subtitles")
    out = tmp_path / "out"
    outcome = download_link(url, out, subtitles_to_traditional=True, subtitle_languages="zh-Hans", include_auto_subtitles=True, embed_subtitles=False)
    assert outcome["errors"] == []
    assert sorted(Path(path).name for path in outcome["items"][0]["subtitle_paths"]) == ["Lesson.zh-Hans.srt", "Lesson.zh-TW.srt"]
    assert "影片" in (out / "Lesson.zh-TW.srt").read_text(encoding="utf-8")


def test_a_traditional_track_from_an_earlier_download_is_kept_when_asked_not_to_overwrite(
    offline_youtube: OfflineYouTube, tmp_path: Path
) -> None:
    url = add_chinese_video(offline_youtube, tmp_path, "chinese0004", {"zh-Hans": "视频", "zh-TW": "創作者自己翻的影片"})
    out = tmp_path / "out"
    first = download_link(url, out, subtitle_languages="zh-TW", embed_subtitles=False)
    assert first["errors"] == []
    # Later only the Simplified track, without overwriting what is already there.
    again = download_link(url, out, subtitles_to_traditional=True, subtitle_languages="zh-Hans", overwrite_existing=False)
    assert again["errors"] == []
    assert "創作者自己翻的影片" in (out / "Lesson.zh-TW.srt").read_text(encoding="utf-8")
    # The file that is there is still this video's Traditional track, listed and embedded with the rest.
    assert sorted(Path(path).name for path in again["items"][0]["subtitle_paths"]) == ["Lesson.zh-Hans.srt", "Lesson.zh-TW.srt"]


def test_without_opencc_the_download_still_finishes_with_a_note(
    offline_youtube: OfflineYouTube, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import videocap.chinese as chinese_module

    def missing_opencc(text: str) -> str:
        raise chinese_module.ChineseConversionUnavailable()

    monkeypatch.setattr(chinese_module, "to_traditional", missing_opencc)
    url = add_chinese_video(offline_youtube, tmp_path, "chinese0003", {"zh-CN": "视频"})
    out = tmp_path / "out"
    outcome = download_link(url, out, subtitles_to_traditional=True, subtitle_languages="zh-CN")
    assert outcome["errors"] == []
    assert len(outcome["notes"]) == 1 and "OpenCC" in outcome["notes"][0] and "保留簡體" in outcome["notes"][0]
    assert [Path(path).name for path in outcome["items"][0]["subtitle_paths"]] == ["Lesson.zh-CN.srt"]
    assert not (out / "Lesson.zh-TW.srt").exists()


# ---- Music tags ----


@pytest.mark.parametrize(
    ("title", "artist", "track"),
    [
        ("周杰倫 Jay Chou - 告白氣球 (Official Music Video)", "周杰倫 Jay Chou", "告白氣球"),
        ("【MV】五月天 MAYDAY－溫柔", "五月天 MAYDAY", "溫柔"),
        ("周杰倫 Jay Chou【告白氣球 Love Confession】Official MV", "周杰倫 Jay Chou", "告白氣球 Love Confession"),
        ("林俊傑 JJ Lin《江南》官方完整版 MV", "林俊傑 JJ Lin", "江南"),
        ("鄧紫棋「光年之外」[4K]", "鄧紫棋", "光年之外"),
        ("Daft Punk – Get Lucky", "Daft Punk", "Get Lucky"),
        # A hyphen inside a name is not a separator, and a title without one keeps the channel as the artist.
        ("Jay-Z live in Taipei", "The Channel", None),
    ],
)
def test_music_titles_are_split_into_artist_and_song(title: str, artist: str, track: Optional[str]) -> None:
    import yt_dlp

    from videocap.downloader import MusicTagsPP

    with yt_dlp.YoutubeDL({"quiet": True, "logger": downloader_module.YtDlpLogCollector()}) as ydl:  # type: ignore[arg-type]
        _, information = MusicTagsPP(ydl).run({"id": "x", "title": title, "channel": "The Channel"})
    assert (information.get("artist"), information.get("track")) == (artist, track)
    # The title itself, and so the file name, stays as YouTube has it.
    assert information["title"] == title
    assert "videocap_music_title" not in information


def test_a_song_named_by_youtube_music_is_left_alone() -> None:
    import yt_dlp

    from videocap.downloader import MusicTagsPP

    with yt_dlp.YoutubeDL({"quiet": True, "logger": downloader_module.YtDlpLogCollector()}) as ydl:  # type: ignore[arg-type]
        _, information = MusicTagsPP(ydl).run({"title": "A - B", "track": "Real Song", "artist": "Real Artist", "playlist_index": 3})
    assert (information["artist"], information["track"], information["track_number"]) == ("Real Artist", "Real Song", 3)


def format_tags(path: Path) -> dict[str, str]:
    from offline_youtube import probe

    return {key.lower(): value for key, value in probe(path)["format"].get("tags", {}).items()}


def test_music_tags_and_a_square_cover_in_a_real_mp3(offline_youtube: OfflineYouTube, tmp_path: Path) -> None:
    from offline_youtube import probe

    url = offline_youtube.add_video("musicVid001", "周杰倫 - 告白氣球【官方MV】", thumbnail=True, channel="JVR Music")
    outcome = download_link(
        url, tmp_path, media_type="audio", audio_format="mp3", embed_thumbnail=True, embed_metadata=False, music_tags=True, download_subtitles=False
    )
    assert outcome["errors"] == [] and outcome["notes"] == []
    path = Path(outcome["items"][0]["path"])
    assert path.name == "周杰倫 - 告白氣球【官方MV】.mp3"
    tags = format_tags(path)
    assert (tags["title"], tags["artist"]) == ("告白氣球", "周杰倫")
    cover = next(stream for stream in probe(path)["streams"] if stream["codec_type"] == "video")
    # YouTube's picture is 64x36 here; the middle 36x36 is kept.
    assert (cover["width"], cover["height"]) == (36, 36)
    # The temporary cover is gone, as without music tags.
    assert sorted(child.name for child in tmp_path.iterdir()) == [path.name]


def test_each_chapter_of_a_split_album_gets_its_own_title_and_number(offline_youtube: OfflineYouTube, tmp_path: Path) -> None:
    chapters = [{"start_time": 0.0, "end_time": 4.0, "title": "第一首"}, {"start_time": 4.0, "end_time": 10.0, "title": "第二首"}]
    url = offline_youtube.add_video("albumVid001", "Full Album", chapters=chapters, channel="Band")
    outcome = download_link(url, tmp_path, media_type="audio", split_chapters=True, music_tags=True, download_subtitles=False)
    assert outcome["errors"] == []
    tracks = [format_tags(Path(path)) for path in outcome["items"][0]["extra_paths"]]
    assert [(tags["title"], tags["track"]) for tags in tracks] == [("第一首", "1/2"), ("第二首", "2/2")]
    assert {(tags["album"], tags["artist"], tags["album_artist"]) for tags in tracks} == {("Full Album", "Band", "Band")}


@pytest.mark.parametrize("audio_format", ["m4a", "mp3", "opus", "flac", "wav"])
def test_music_tags_in_every_audio_format(offline_youtube: OfflineYouTube, tmp_path: Path, audio_format: str) -> None:
    url = offline_youtube.add_video(f"musicFmt{audio_format[:3]}", "Band - Song", thumbnail=True)
    outcome = download_link(
        url, tmp_path, media_type="audio", audio_format=audio_format, music_tags=True, embed_thumbnail=True, download_subtitles=False
    )
    assert outcome["errors"] == [] and outcome["notes"] == []
    assert Path(outcome["items"][0]["path"]).suffix == f".{audio_format}"


@pytest.mark.parametrize("audio_format", ["m4a", "mp3", "opus", "flac", "wav"])
def test_chapters_of_every_audio_format_are_tagged_or_left_alone(offline_youtube: OfflineYouTube, tmp_path: Path, audio_format: str) -> None:
    chapters = [{"start_time": 0.0, "end_time": 4.0, "title": "第一首"}, {"start_time": 4.0, "end_time": 10.0, "title": "第二首"}]
    url = offline_youtube.add_video(f"albumFmt{audio_format[:3]}", "Album", chapters=chapters, channel="Band")
    outcome = download_link(url, tmp_path, media_type="audio", audio_format=audio_format, split_chapters=True, music_tags=True, download_subtitles=False)
    assert outcome["errors"] == [] and outcome["notes"] == []
    tracks = [Path(path) for path in outcome["items"][0]["extra_paths"]]
    assert len(tracks) == 2 and all(track.suffix == f".{audio_format}" for track in tracks)
    if audio_format != "wav":
        from offline_youtube import probe

        # Ogg keeps its tags on the stream rather than the file.
        titles = [{**probe(track)["streams"][0].get("tags", {}), **format_tags(track)}.get("title") for track in tracks]
        assert titles == ["第一首", "第二首"]


def test_a_playlists_order_becomes_the_track_number(offline_youtube: OfflineYouTube, tmp_path: Path) -> None:
    urls = [offline_youtube.add_video(f"playTrack{number:02d}", f"Band - Song {number}") for number in (1, 2)]
    playlist = offline_youtube.add_playlist("PLalbum", urls)
    outcome = download_link(playlist, tmp_path, media_type="audio", music_tags=True, embed_metadata=False, download_subtitles=False)
    assert outcome["errors"] == []
    tags = [format_tags(Path(item["path"])) for item in outcome["items"]]
    assert [(tag["title"], tag["track"]) for tag in tags] == [("Song 1", "1"), ("Song 2", "2")]


# ---- Waiting for a scheduled stream or premiere ----


class StopWhileWaiting(Progress):
    """Presses stop a second after the job starts waiting, the way a person who changed their mind would."""

    def __init__(self) -> None:
        super().__init__()
        self.waiting_since: Optional[float] = None

    def progress(self, value: float, text: str = "") -> None:
        import time as time_module

        super().progress(value, text)
        if text.startswith("等待開播") and self.waiting_since is None:
            self.waiting_since = time_module.monotonic()

    def should_stop(self) -> bool:
        import time as time_module

        return self.waiting_since is not None and time_module.monotonic() - self.waiting_since > 1.0


@pytest.fixture
def ffmpeg_for_waiting(fake_ydl: type[FakeYDL], monkeypatch: pytest.MonkeyPatch) -> None:
    """Waiting needs ffmpeg, whose own steps would not run in FakeYDL."""
    monkeypatch.setattr(downloader_module, "get_ffmpeg_status", lambda: (True, "ffmpeg"))
    monkeypatch.setattr(downloader_module, "build_late_postprocessors", lambda options, ffmpeg_available: [])


def wait_options(url: str, output_dir: Path, **settings: Any) -> Any:
    spec = JobSpec(url=url, settings=DownloadSettings(wait_for_live=True, download_subtitles=False, **settings), output_dir=str(output_dir))
    return options_from_spec(spec, AccessLimits())


def test_waiting_for_a_stream_shows_when_it_starts_and_can_be_stopped(offline_youtube: OfflineYouTube, tmp_path: Path) -> None:
    import time as time_module

    release = time_module.time() + 3600
    url = offline_youtube.add_upcoming("upcoming001", "Concert", release, looks=99)
    progress, log = StopWhileWaiting(), []
    started = time_module.monotonic()
    outcome = run_download(url, wait_options(url, tmp_path), progress, should_stop=progress.should_stop, on_log=log.append)
    assert time_module.monotonic() - started < 10
    assert outcome["stopped"] is True
    assert outcome["errors"] == []
    expected = f"等待開播（預定 {time_module.strftime('%H:%M', time_module.localtime(release))}）"
    if time_module.localtime(release)[:3] != time_module.localtime()[:3]:
        expected = f"等待開播（預定 {time_module.strftime('%m/%d %H:%M', time_module.localtime(release))}）"
    assert expected in [text for _, text in progress.calls]
    # The countdown ticks every second, and hours of it would push everything else out of the job's log.
    assert not [line for line in log if "Remaining time" in line]


def test_a_premiere_is_downloaded_once_it_begins(offline_youtube: OfflineYouTube, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import time as time_module

    monkeypatch.setattr(downloader_module, "LIVE_WAIT_RETRY_SECONDS", (1, 1))
    # Already due but still not started, as premieres often are.
    url = offline_youtube.add_upcoming("premiere001", "Premiere", time_module.time() - 60, looks=2)
    outcome = run_download(url, wait_options(url, tmp_path), Progress())
    assert outcome["errors"] == []
    assert [Path(item["path"]).name for item in outcome["items"]] == ["Premiere.mp4"]
    assert offline_youtube.extractions.count(url) == 3


def test_a_stream_more_than_a_day_away_is_not_waited_for(offline_youtube: OfflineYouTube, tmp_path: Path) -> None:
    import time as time_module

    url = offline_youtube.add_upcoming("upcoming002", "Next week", time_module.time() + 7 * 24 * 3600, looks=99)
    started = time_module.monotonic()
    outcome = run_download(url, wait_options(url, tmp_path), Progress())
    assert time_module.monotonic() - started < 10
    assert len(outcome["errors"]) == 1 and "超過 24 小時" in outcome["errors"][0]
    assert outcome["items"] == []


def test_waiting_gives_up_after_a_day(offline_youtube: OfflineYouTube, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(downloader_module, "LIVE_WAIT_RETRY_SECONDS", (1, 1))
    monkeypatch.setattr(downloader_module, "LIVE_WAIT_MAX_SECONDS", 1.5)
    url = offline_youtube.add_upcoming("upcoming003", "Postponed", None, looks=99)
    outcome = run_download(url, wait_options(url, tmp_path), Progress())
    assert len(outcome["errors"]) == 1 and "已等待 24 小時" in outcome["errors"][0]


@pytest.mark.parametrize(
    ("url", "expand_playlist", "waits"),
    [
        ("https://www.youtube.com/watch?v=upcoming004", False, True),
        ("https://www.youtube.com/watch?v=upcoming004&list=PLx", False, True),
        # In a list, one upcoming stream would hold up every video after it.
        ("https://www.youtube.com/watch?v=upcoming004&list=PLx", True, False),
        ("https://www.youtube.com/playlist?list=PLx", True, False),
        ("https://www.youtube.com/@channel/streams", False, False),
    ],
)
@pytest.mark.usefixtures("ffmpeg_for_waiting")
def test_only_a_link_to_one_video_waits(fake_ydl: type[FakeYDL], tmp_path: Path, url: str, expand_playlist: bool, waits: bool) -> None:
    download_with_ytdlp(url, wait_options(url, tmp_path, expand_playlist=expand_playlist), Progress())
    assert ("wait_for_video" in fake_ydl.instances[0].opts) is waits


def test_a_later_yt_dlp_without_the_wait_method_still_downloads(fake_ydl: type[FakeYDL], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The 系統 page updates yt-dlp, and a later one may rename this private method.
    monkeypatch.delattr(FakeYDL, "_wait_for_video")
    url = "https://www.youtube.com/watch?v=abc123def45"
    outcome = run_download(url, wait_options(url, tmp_path), Progress())
    assert outcome["errors"] == []
    assert [item["title"] for item in outcome["items"]] == ["Title"]


@pytest.mark.usefixtures("ffmpeg_for_waiting")
def test_a_later_yt_dlp_with_another_wait_signature_still_downloads(
    fake_ydl: type[FakeYDL], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[Any, ...]] = []

    def new_wait(ydl: FakeYDL, ie_result: Any, retries: int = 0, *, reason: str = "") -> None:
        calls.append((ie_result, retries, reason))

    def script(ydl: FakeYDL) -> Any:
        ydl._wait_for_video({"id": "abc123def45", "live_status": "is_upcoming"}, 3, reason="upcoming")
        ydl._wait_for_video(ie_result={"id": "abc123def45", "formats": [{}]})
        return ydl.finish_video()

    monkeypatch.setattr(FakeYDL, "_wait_for_video", new_wait)
    monkeypatch.setattr(FakeYDL, "script", staticmethod(script))
    url = "https://www.youtube.com/watch?v=abc123def45"
    progress = Progress()
    outcome = run_download(url, wait_options(url, tmp_path), progress)
    assert outcome["errors"] == []
    assert [call[1:] for call in calls] == [(3, "upcoming"), (0, "")]
    assert any(text.startswith("等待開播") for _, text in progress.calls)


def test_a_stream_that_was_waited_for_is_recorded(recorder: FakeRecorder, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    url = "https://www.youtube.com/watch?v=live0000001"

    def script(ydl: FakeYDL) -> Any:
        ydl.output = tmp_path / "out" / "Live.mp4"
        ydl.opts["match_filter"](live_entry("live0000001"), incomplete=False)
        recorder.end.set()
        return None

    monkeypatch.setattr(FakeYDL, "script", staticmethod(script))
    # Recording is otherwise off, but waiting for a stream only makes sense to record it.
    outcome = run_download(url, wait_options(url, tmp_path), Progress())
    assert outcome["skipped"] == []
    assert recorder.started == ["Live.mp4"]


def test_without_ffmpeg_a_stream_is_not_waited_for(offline_youtube: OfflineYouTube, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import time as time_module

    # A saved preset, imported settings or a subscription can still ask to wait after ffmpeg is gone.
    monkeypatch.setattr(downloader_module, "get_ffmpeg_status", lambda: (False, "missing"))
    url = offline_youtube.add_upcoming("upcoming005", "Concert", time_module.time() + 3600, looks=99)
    # Stops a wait that should not have begun, so a regression fails instead of waiting an hour.
    progress = StopWhileWaiting()
    outcome = run_download(url, wait_options(url, tmp_path), progress, should_stop=progress.should_stop)
    assert progress.waiting_since is None
    assert outcome["stopped"] is False
    assert outcome["items"] == []
    assert len(outcome["errors"]) == 1 and outcome["errors"][0].startswith("直播或首播尚未開始，開始後才能下載。")
    assert outcome["notes"] == [downloader_module.LIVE_WAIT_NEEDS_FFMPEG_NOTE]
    assert offline_youtube.extractions.count(url) == 1


def test_without_ffmpeg_a_video_that_is_out_gets_no_wait_note(
    offline_youtube: OfflineYouTube, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(downloader_module, "get_ffmpeg_status", lambda: (False, "missing"))
    url = offline_youtube.add_video("published01", "Already out", formats=[offline_youtube.video_format(acodec="mp4a.40.2")])
    outcome = run_download(url, wait_options(url, tmp_path), Progress())
    assert outcome["errors"] == []
    assert len(outcome["items"]) == 1
    assert outcome["notes"] == []


def test_without_ffmpeg_a_stream_already_live_says_recording_needs_it(
    fake_ydl: type[FakeYDL], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = "https://www.youtube.com/watch?v=live0000001"
    reasons: list[Optional[str]] = []
    monkeypatch.setattr(FakeYDL, "script", staticmethod(lambda ydl: reasons.append(ydl.opts["match_filter"](live_entry("live0000001"), incomplete=False))))
    outcome = run_download(url, wait_options(url, tmp_path), Progress())
    assert reasons == ["Live live0000001：錄製直播需要 ffmpeg。"]
    assert "wait_for_video" not in fake_ydl.instances[0].opts
    assert outcome["skipped"] == reasons


# ---- File times ----


def test_files_carry_the_videos_own_time(offline_youtube: OfflineYouTube, tmp_path: Path) -> None:
    url = offline_youtube.add_video("fileTime001", "Old talk", subtitles=True, upload_date="20200102", timestamp=1577934000)
    outcome = download_link(url, tmp_path, mtime_upload_date=True, subtitle_languages="en", write_transcript=True, write_infojson=True)
    assert outcome["errors"] == []
    item = outcome["items"][0]
    paths = [item["path"], *item["subtitle_paths"], *item["extra_paths"]]
    assert len(paths) == 4
    assert {Path(path).stat().st_mtime for path in paths} == {1577934000}


def test_file_times_leave_other_files_in_the_folder_alone(offline_youtube: OfflineYouTube, tmp_path: Path) -> None:
    import os

    other = tmp_path / "Old talk (my notes).txt"
    other.write_text("mine", encoding="utf-8")
    os.utime(other, (1700000000, 1700000000))
    url = offline_youtube.add_video("fileTime004", "Old talk", subtitles=True, thumbnail=True, upload_date="20200102", timestamp=1577934000)
    outcome = download_link(url, tmp_path, mtime_upload_date=True, subtitle_languages="en", write_thumbnail=True, write_description=True)
    assert outcome["errors"] == []
    assert other.stat().st_mtime == 1700000000
    touched = sorted(path.name for path in tmp_path.iterdir() if path.stat().st_mtime == 1577934000)
    assert touched == ["Old talk.en.srt", "Old talk.jpg", "Old talk.mp4"]


def test_without_an_exact_time_the_upload_date_is_used(offline_youtube: OfflineYouTube, tmp_path: Path) -> None:
    from datetime import datetime, timezone

    url = offline_youtube.add_video("fileTime002", "Older talk", upload_date="20191231")
    outcome = download_link(url, tmp_path, mtime_upload_date=True, download_subtitles=False)
    modified = datetime.fromtimestamp(Path(outcome["items"][0]["path"]).stat().st_mtime, timezone.utc)
    assert modified == datetime(2019, 12, 31, 12, tzinfo=timezone.utc)


def test_files_keep_the_download_time_unless_asked(offline_youtube: OfflineYouTube, tmp_path: Path) -> None:
    import time as time_module

    url = offline_youtube.add_video("fileTime003", "New talk", upload_date="20200102", timestamp=1577934000)
    outcome = download_link(url, tmp_path, download_subtitles=False)
    assert Path(outcome["items"][0]["path"]).stat().st_mtime > time_module.time() - 600


# ---- Media-server information files ----


def test_a_media_server_gets_an_nfo_and_a_thumb_beside_the_video(offline_youtube: OfflineYouTube, tmp_path: Path) -> None:
    import subprocess
    from xml.etree import ElementTree

    webp = tmp_path / "media" / "cover.webp"
    webp.parent.mkdir()
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=green:size=64x36", "-frames:v", "1", str(webp)], check=True)
    url = offline_youtube.add_video(
        "nfoVideo001",
        "Tom & Jerry <Live>",
        channel="Cartoon \"Classics\"",
        upload_date="20250704",
        description="第一行 & <b>不是標籤</b>\n第二行\x0b",
        thumbnails=[{"url": webp.as_uri(), "id": "0"}],
    )
    out = tmp_path / "out"
    outcome = download_link(url, out, write_nfo=True, write_thumbnail=True, download_subtitles=False)
    assert outcome["errors"] == [] and outcome["notes"] == []
    video = Path(outcome["items"][0]["path"])
    nfo, thumb = video.with_suffix(".nfo"), video.with_name(f"{video.stem}-thumb.jpg")
    assert {str(nfo), str(thumb)} <= set(outcome["items"][0]["extra_paths"])
    root = ElementTree.parse(nfo).getroot()
    assert root.tag == "episodedetails"
    assert {child.tag: child.text for child in root} == {
        "title": "Tom & Jerry <Live>",
        "plot": "第一行 & <b>不是標籤</b>\n第二行",
        "aired": "2025-07-04",
        "studio": 'Cartoon "Classics"',
        "uniqueid": "nfoVideo001",
    }
    assert root.find("uniqueid").attrib == {"type": "youtube", "default": "true"}  # type: ignore[union-attr]
    assert stream_codecs(thumb) == ["mjpeg"]


def test_no_thumb_without_a_saved_cover(offline_youtube: OfflineYouTube, tmp_path: Path) -> None:
    url = offline_youtube.add_video("nfoVideo002", "Plain", thumbnail=True)
    outcome = download_link(url, tmp_path, write_nfo=True, embed_thumbnail=True, download_subtitles=False)
    assert outcome["errors"] == []
    assert sorted(child.name for child in tmp_path.iterdir()) == ["Plain.mp4", "Plain.nfo"]


def test_an_nfo_is_only_for_this_machines_owner(offline_youtube: OfflineYouTube, tmp_path: Path) -> None:
    url = offline_youtube.add_video("nfoVideo003", "Public")
    outcome = download_link(url, tmp_path, AccessLimits.for_public_host(), write_nfo=True, download_subtitles=False)
    assert outcome["errors"] == []
    assert not list(tmp_path.glob("*.nfo"))


# ---- Another player client after HTTP 403 ----


def refused_playlist(site: OfflineYouTube, working_client: str) -> str:
    urls = [site.add_video(f"forbidVid0{number}", f"Part {number}") for number in (1, 2)]
    site.refused_unless_client["forbidVid02"] = working_client
    return site.add_playlist("PLforbid", urls)


def test_videos_refused_with_403_are_tried_once_more_with_other_clients(offline_youtube: OfflineYouTube, tmp_path: Path) -> None:
    url = refused_playlist(offline_youtube, "tv")
    spec = JobSpec(url=url, settings=DownloadSettings(download_subtitles=False, expand_playlist=True), output_dir=str(tmp_path))
    log: list[str] = []
    outcome = run_download(url, options_from_spec(spec, AccessLimits()), Progress(), on_log=log.append)
    assert outcome["errors"] == [] and outcome["failed_ids"] == []
    assert sorted(item["title"] for item in outcome["items"]) == ["Part 1", "Part 2"]
    assert all(Path(item["path"]).is_file() for item in outcome["items"])
    # Only the refused video was asked for again, with clients that need no PO token.
    assert offline_youtube.extractions.count("https://www.youtube.com/watch?v=forbidVid01") == 1
    assert offline_youtube.player_clients_asked[-1] == ["web_embedded", "tv", "tv_downgraded"]
    assert any("HTTP 403" in line and "tv_downgraded" in line for line in log)
    assert len(outcome["notes"]) == 1 and "已下載完成" in outcome["notes"][0]


def test_a_video_refused_again_is_reported_once(offline_youtube: OfflineYouTube, tmp_path: Path) -> None:
    url = refused_playlist(offline_youtube, "android")
    outcome = download_link(url, tmp_path, download_subtitles=False, expand_playlist=True)
    assert [item["title"] for item in outcome["items"]] == ["Part 1"]
    assert len(outcome["errors"]) == 1 and "拒絕" in outcome["errors"][0]
    # The 重試失敗的影片 button can now find it, though yt-dlp's message does not name it.
    assert outcome["failed_ids"] == ["forbidVid02"]
    assert offline_youtube.extractions.count("https://www.youtube.com/watch?v=forbidVid02") == 2
    assert outcome["notes"] == []


def test_a_chosen_player_client_is_not_replaced(offline_youtube: OfflineYouTube, tmp_path: Path) -> None:
    url = refused_playlist(offline_youtube, "tv")
    outcome = download_link(url, tmp_path, download_subtitles=False, expand_playlist=True, player_client="tv_samsung")
    assert outcome["failed_ids"] == ["forbidVid02"]
    assert offline_youtube.extractions.count("https://www.youtube.com/watch?v=forbidVid02") == 1


def test_a_video_that_finished_is_never_retried_as_failed(offline_youtube: OfflineYouTube, tmp_path: Path) -> None:
    # A step after the download fails without naming the video, as an embed or an owner's --exec can.
    urls = [offline_youtube.add_video(f"lateFailed{number}", f"Late {number}") for number in (1, 2)]
    playlist = offline_youtube.add_playlist("PLlate", urls)
    outcome = download_link(playlist, tmp_path, download_subtitles=False, expand_playlist=True, ytdlp_extra_args='--exec "exit 1"')
    assert sorted(item["title"] for item in outcome["items"]) == ["Late 1", "Late 2"]
    assert len(outcome["errors"]) == 2
    # 重試失敗的影片 would download both again.
    assert outcome["failed_ids"] == []


def test_a_video_refused_after_it_finished_is_not_downloaded_again(
    offline_youtube: OfflineYouTube, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from yt_dlp.utils import PostProcessingError

    class RefusedLateStep(downloader_module.PostProcessor):
        def run(self, information: Any) -> Any:
            raise PostProcessingError("Unable to download webpage: HTTP Error 403: Forbidden")

    real_add = downloader_module.add_owner_postprocessors

    def add_with_refusal(ydl: Any, options: Any) -> None:
        real_add(ydl, options)
        ydl.add_post_processor(RefusedLateStep(), when="after_move")

    monkeypatch.setattr(downloader_module, "add_owner_postprocessors", add_with_refusal)
    url = offline_youtube.add_video("lateRefuse1", "Refused late")
    outcome = download_link(url, tmp_path, download_subtitles=False)
    assert [item["title"] for item in outcome["items"]] == ["Refused late"]
    assert offline_youtube.extractions.count(url) == 1
    assert outcome["failed_ids"] == []


def test_the_retries_share_what_is_left_of_a_public_jobs_budget(offline_youtube: OfflineYouTube, tmp_path: Path) -> None:
    from dataclasses import replace

    urls = [offline_youtube.add_video(f"budgetVid0{number}", f"Budget {number}") for number in (1, 2)]
    for number in (1, 2):
        offline_youtube.refused_unless_client[f"budgetVid0{number}"] = "tv"
    playlist = offline_youtube.add_playlist("PLbudget", urls)
    one_video = sum((offline_youtube.media / name).stat().st_size for name in ("v.mp4", "a.m4a"))
    spec = JobSpec(url=playlist, settings=DownloadSettings(download_subtitles=False, expand_playlist=True), output_dir=str(tmp_path))
    options = replace(options_from_spec(spec, AccessLimits()), max_job_bytes=int(one_video * 1.5))
    outcome = run_download(playlist, options, Progress())
    # Room for one of the two; the other is reported, not downloaded past the limit.
    assert [item["title"] for item in outcome["items"]] == ["Budget 1"]
    assert sum(path.stat().st_size for path in tmp_path.iterdir()) <= options.max_job_bytes
    assert any("單次任務上限" in reason for reason in outcome["skipped"])


def test_videos_left_without_a_retry_keep_their_error(offline_youtube: OfflineYouTube, tmp_path: Path) -> None:
    from dataclasses import replace

    urls = [offline_youtube.add_video(f"budgetVid0{number}", f"Budget {number}") for number in (3, 4)]
    for number in (3, 4):
        offline_youtube.refused_unless_client[f"budgetVid0{number}"] = "tv"
    playlist = offline_youtube.add_playlist("PLbudget2", urls)
    one_video = sum((offline_youtube.media / name).stat().st_size for name in ("v.mp4", "a.m4a"))
    spec = JobSpec(url=playlist, settings=DownloadSettings(download_subtitles=False, expand_playlist=True), output_dir=str(tmp_path))
    # Exactly one video's worth: once the first retry used it, the second has no room for a retry at all.
    options = replace(options_from_spec(spec, AccessLimits()), max_job_bytes=one_video)
    outcome = run_download(playlist, options, Progress())
    assert [item["title"] for item in outcome["items"]] == ["Budget 3"]
    assert outcome["failed_ids"] == ["budgetVid04"]
    assert len(outcome["errors"]) == 1 and "拒絕" in outcome["errors"][0]


def test_a_public_job_without_room_left_is_not_retried(tmp_path: Path) -> None:
    from videocap.downloader import options_for_forbidden_retry

    finished = tmp_path / "finished.mp4"
    finished.write_bytes(b"x" * 1000)
    outcome = empty_outcome()
    outcome["items"] = [{"path": str(finished), "subtitle_paths": [], "extra_paths": []}]  # type: ignore[typeddict-item]
    assert options_for_forbidden_retry(make_options(tmp_path, max_job_bytes=1000), outcome) is None
    room = options_for_forbidden_retry(make_options(tmp_path, max_job_bytes=1500), outcome)
    assert room is not None and room.max_job_bytes == 500
    assert room.network.player_client == "web_embedded,tv,tv_downgraded"
    # A resumed run counts the files in the job's folder itself, so taking them off here would count them twice.
    resumed = options_for_forbidden_retry(make_options(tmp_path, output_dir=tmp_path, max_job_bytes=1500, resuming=True), outcome)
    assert resumed is not None and resumed.max_job_bytes == 1500
    assert options_for_forbidden_retry(make_options(tmp_path, output_dir=tmp_path, max_job_bytes=1000, resuming=True), outcome) is None


def test_items_saved_before_the_timestamp_existed_still_count_as_items() -> None:
    from videocap.downloader import DownloadedItem, item_from_info

    item = item_from_info({"id": "abc123def45", "title": "T", "timestamp": 1577934000}, "a.mp4")
    assert item["timestamp"] == 1577934000
    saved_earlier = {key: value for key, value in item.items() if key != "timestamp"}
    # A restart restores a paused job's items by checking these keys.
    assert DownloadedItem.__required_keys__ <= saved_earlier.keys()


# ---- The owner's own yt-dlp steps ----


OWNER_STAGES = ("pre_process", "after_filter", "before_dl", "post_process", "after_move")


def test_the_owners_own_steps_run_after_the_apps_at_every_stage(
    offline_youtube: OfflineYouTube, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import yt_dlp

    stages: dict[str, list[str]] = {}
    real_run_all_pps = yt_dlp.YoutubeDL.run_all_pps

    def record(ydl: Any, key: str, info: Any, *, additional_pps: Any = None) -> Any:
        stages.setdefault(key, [type(step).__name__ for step in (additional_pps or []) + ydl._pps[key]])
        return real_run_all_pps(ydl, key, info, additional_pps=additional_pps)

    monkeypatch.setattr(yt_dlp.YoutubeDL, "run_all_pps", record)
    chapters = [{"start_time": 0.0, "end_time": 4.0, "title": "One"}, {"start_time": 4.0, "end_time": 10.0, "title": "Two"}]
    url = offline_youtube.add_video("ownerSteps1", "Band - Song", thumbnail=True, chapters=chapters)
    owner = " ".join(f'--parse-metadata "{when}:title:(?P<meta_{when}>.+)"' for when in OWNER_STAGES)
    outcome = download_link(
        url,
        tmp_path,
        media_type="audio",
        audio_format="mp3",
        embed_thumbnail=True,
        music_tags=True,
        split_chapters=True,
        sponsorblock_mode="mark",
        sponsorblock_categories=["sponsor"],
        save_live_chat=True,
        download_subtitles=False,
        ytdlp_extra_args=owner,
    )
    assert outcome["errors"] == []
    for when in OWNER_STAGES:
        names = stages[when]
        assert names.count("MetadataParserPP") == 1, (when, names)
        # Only the step that notes the final file name may follow; it changes nothing.
        assert set(names[names.index("MetadataParserPP") + 1 :]) <= {"RecordFinalPathPP"}, (when, names)
    # The app's own steps really were there to come after.
    assert {"MusicTagsPP", "PastStreamChatOnlyPP"} <= set(stages["pre_process"])
    assert "SponsorBlockNamesPP" in stages["after_filter"]
    assert "SquareCoverPP" in stages["before_dl"]
    assert "TagChapterFilesPP" in stages["post_process"]


@pytest.mark.parametrize(("extra", "extension"), [("--remux-video mkv", ".mkv"), ("-x --audio-format mp3", ".mp3")])
def test_the_result_names_the_file_the_owners_own_steps_left(
    offline_youtube: OfflineYouTube, tmp_path: Path, extra: str, extension: str
) -> None:
    url = offline_youtube.add_video("ownerRemux1", "Remuxed", subtitles=True, upload_date="20200102", timestamp=1577934000)
    outcome = download_link(
        url, tmp_path, ytdlp_extra_args=extra, mtime_upload_date=True, write_nfo=True, subtitle_languages="en", embed_subtitles=False
    )
    assert outcome["errors"] == []
    item = outcome["items"][0]
    path = Path(item["path"])
    assert path == tmp_path / f"Remuxed{extension}" and path.is_file()
    assert not (tmp_path / "Remuxed.mp4").exists()
    assert extension[1:] in item["quality"]
    # The steps that run once yt-dlp is done reach the final file too.
    assert path.stat().st_mtime == 1577934000
    assert str(tmp_path / "Remuxed.nfo") in item["extra_paths"]


def test_a_sidecar_only_job_lists_what_the_owners_own_steps_left(offline_youtube: OfflineYouTube, tmp_path: Path) -> None:
    url = offline_youtube.add_video("ownerSubs01", "Captions", subtitles=True)
    outcome = download_link(url, tmp_path, media_type="none", subtitle_languages="en", subtitle_format="vtt", ytdlp_extra_args="--convert-subs srt")
    assert outcome["errors"] == []
    assert outcome["items"][0]["subtitle_paths"] == [str(tmp_path / "Captions.en.srt")]
    assert sorted(child.name for child in tmp_path.iterdir()) == ["Captions.en.srt"]


def test_every_step_shows_a_status_in_chinese() -> None:
    import yt_dlp.postprocessor as ytdlp_postprocessors
    from yt_dlp.postprocessor.common import PostProcessor

    from videocap.downloader import POSTPROCESSOR_LABELS

    def is_step(value: Any) -> bool:
        return isinstance(value, type) and issubclass(value, PostProcessor) and value.__name__.endswith("PP")

    # The app's own steps, and every yt-dlp step the owner's 進階 yt-dlp 參數 could add.
    steps = [value for value in vars(downloader_module).values() if is_step(value)]
    steps += [value for value in vars(ytdlp_postprocessors).values() if is_step(value)]
    missing = sorted({step.pp_key() for step in steps} - POSTPROCESSOR_LABELS.keys())
    assert missing == []
    assert {"MusicTags", "SquareCover", "TagChapterFiles", "SponsorBlockNames", "PastStreamChatOnly", "WriteSidecars", "ThumbnailsConvertor"} <= {
        step.pp_key() for step in steps
    }
    # 移除片段 runs through ModifyChapters too, so its label must not speak of sponsors only.
    assert "贊助" not in POSTPROCESSOR_LABELS["ModifyChapters"]


def test_a_cover_conversion_shows_its_own_status(offline_youtube: OfflineYouTube, tmp_path: Path) -> None:
    url = offline_youtube.add_video("statusCov01", "Cover", thumbnail=True, upload_date="20200102")
    progress = Progress()
    spec = JobSpec(
        url=url,
        settings=DownloadSettings(write_thumbnail=True, thumbnails_as_jpg=True, mtime_upload_date=True, download_subtitles=False),
        output_dir=str(tmp_path),
    )
    outcome = run_download(url, options_from_spec(spec, AccessLimits()), progress)
    assert outcome["errors"] == []
    texts = [text for _, text in progress.calls]
    assert "正在轉換封面..." in texts and "正在把檔案時間改成上傳日期..." in texts


def test_asking_for_traditional_titles_adds_no_warning(tmp_path: Path) -> None:
    import yt_dlp

    from videocap.downloader import YtDlpLogCollector
    from videocap.options import build_ydl_options

    spec = JobSpec(url="u", settings=DownloadSettings(prefer_traditional_metadata=True), output_dir=str(tmp_path))
    logger = YtDlpLogCollector()
    with yt_dlp.YoutubeDL(build_ydl_options(options_from_spec(spec, AccessLimits()), True, logger)) as ydl:  # type: ignore[arg-type]
        # yt-dlp's real YouTube extractor warns the first time it reads the language.
        assert ydl.get_info_extractor("Youtube")._preferred_lang == "zh-TW"
    assert logger.warnings == []
    assert not any("translated fields" in line for line in logger.lines)
    logger.warning("[youtube] abc123def45: Some formats may be missing")
    assert logger.warnings == ["[youtube] abc123def45: Some formats may be missing"]


# ---- Spacing downloads that use a login ----


@pytest.fixture(autouse=True)
def fresh_login_pacer(monkeypatch: pytest.MonkeyPatch) -> downloader_module.LoginPacer:
    # The spacing spans the whole process, so a test must not wait out the logged-in download of the one before it.
    pacer = downloader_module.LoginPacer()
    monkeypatch.setattr(downloader_module, "LOGIN_PACER", pacer)
    return pacer


class TimedProgress(Progress):
    def __init__(self, stop_on: str = "") -> None:
        super().__init__()
        self.times: list[float] = []
        self.stop_on = stop_on
        self.stop = False

    def progress(self, value: float, text: str = "") -> None:
        import time as time_module

        super().progress(value, text)
        self.times.append(time_module.monotonic())
        if self.stop_on and self.stop_on in text:
            self.stop = True

    def texts(self) -> list[str]:
        return [text for _, text in self.calls]

    def first_time_after(self, marker: str) -> float:
        """When the step after the wait began, which is when the download itself started."""
        index = next(number for number, text in enumerate(self.texts()) if marker in text)
        return self.times[index + 1]


def write_login_cookies(path: Path) -> Path:
    path.write_text("# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t4102444800\tSAPISID\tvalue\n", encoding="utf-8")
    return path


def logged_in_download(
    url: str,
    output_dir: Path,
    progress: Progress,
    cookie_file: Optional[Path],
    limits: Optional[AccessLimits] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> Any:
    spec = JobSpec(url=url, settings=DownloadSettings(download_subtitles=False), output_dir=str(output_dir))
    options = options_from_spec(spec, limits or AccessLimits(), cookie_file=cookie_file)
    return run_download(url, options, progress, should_stop=should_stop)


@pytest.fixture
def short_login_spacing(monkeypatch: pytest.MonkeyPatch) -> float:
    import videocap.options as options_module

    monkeypatch.setattr(options_module, "LOGGED_IN_MIN_SLEEP_SECONDS", 3)
    monkeypatch.setattr(options_module, "LOGGED_IN_MAX_SLEEP_SECONDS", 3)
    return 3.0


LOGIN_WAIT_MARKER = "登入下載：等待"
# Counted from the last logged-in video, so the seconds left depend on how long this machine took to get here.
LOGIN_WAIT_TEXT_RE = re.compile(r"^登入下載：等待 [1-3] 秒，避免帳號被限制$")


def login_waits(progress: TimedProgress) -> list[str]:
    return [text for text in progress.texts() if LOGIN_WAIT_MARKER in text]


def test_logged_in_jobs_in_a_row_are_spaced(offline_youtube: OfflineYouTube, tmp_path: Path, short_login_spacing: float) -> None:
    import time as time_module

    # Every pasted link, ticked search result and retry is its own job, and logged-in jobs run back to back.
    cookies = write_login_cookies(tmp_path / "cookies.txt")
    first = offline_youtube.add_video("loginPace01", "First")
    second = offline_youtube.add_video("loginPace02", "Second")
    first_progress = TimedProgress()
    assert logged_in_download(first, tmp_path / "a", first_progress, cookies)["errors"] == []
    # Nothing logged-in ran before it, so the first job starts at once.
    assert not any(LOGIN_WAIT_MARKER in text for text in first_progress.texts())
    first_ended = time_module.monotonic()

    second_progress = TimedProgress()
    outcome = logged_in_download(second, tmp_path / "b", second_progress, cookies)
    assert outcome["errors"] == [] and len(outcome["items"]) == 1
    waits = login_waits(second_progress)
    assert len(waits) == 1 and LOGIN_WAIT_TEXT_RE.match(waits[0])
    assert second_progress.first_time_after(LOGIN_WAIT_MARKER) - first_ended >= short_login_spacing - 0.1


def test_two_videos_in_one_logged_in_job_are_spaced_the_same_way(
    offline_youtube: OfflineYouTube, tmp_path: Path, short_login_spacing: float
) -> None:
    cookies = write_login_cookies(tmp_path / "cookies.txt")
    urls = [offline_youtube.add_video(f"loginList{number}0", f"Video {number}") for number in (1, 2)]
    playlist = offline_youtube.add_playlist("PLloginpace", urls)
    progress = TimedProgress()
    outcome = logged_in_download(playlist, tmp_path / "out", progress, cookies)
    assert outcome["errors"] == [] and len(outcome["items"]) == 2
    waits = login_waits(progress)
    assert len(waits) == 1 and LOGIN_WAIT_TEXT_RE.match(waits[0])


def test_a_job_without_a_login_is_never_slowed(offline_youtube: OfflineYouTube, tmp_path: Path, short_login_spacing: float) -> None:
    cookies = write_login_cookies(tmp_path / "cookies.txt")
    logged_in = offline_youtube.add_video("loginPace03", "With login")
    public = offline_youtube.add_video("loginPace04", "Without login")
    assert logged_in_download(logged_in, tmp_path / "a", Progress(), cookies)["errors"] == []
    progress = TimedProgress()
    assert logged_in_download(public, tmp_path / "b", progress, None)["errors"] == []
    assert not any("等待" in text for text in progress.texts())


def test_a_public_visitors_uploaded_cookies_are_spaced_too(
    offline_youtube: OfflineYouTube, tmp_path: Path, short_login_spacing: float
) -> None:
    cookies = write_login_cookies(tmp_path / "cookies.txt")
    first = offline_youtube.add_video("loginPace05", "First")
    second = offline_youtube.add_video("loginPace06", "Second")
    limits = AccessLimits.for_public_host()
    assert logged_in_download(first, tmp_path / "a", Progress(), cookies, limits)["errors"] == []
    progress = TimedProgress()
    assert logged_in_download(second, tmp_path / "b", progress, cookies, limits)["errors"] == []
    waits = login_waits(progress)
    assert len(waits) == 1 and LOGIN_WAIT_TEXT_RE.match(waits[0])


def test_the_wait_before_a_jobs_first_logged_in_video_can_be_stopped(
    offline_youtube: OfflineYouTube, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time as time_module

    import videocap.options as options_module

    monkeypatch.setattr(options_module, "LOGGED_IN_MIN_SLEEP_SECONDS", 3600)
    monkeypatch.setattr(options_module, "LOGGED_IN_MAX_SLEEP_SECONDS", 3600)
    cookies = write_login_cookies(tmp_path / "cookies.txt")
    first = offline_youtube.add_video("loginPace07", "First")
    second = offline_youtube.add_video("loginPace08", "Second")
    assert logged_in_download(first, tmp_path / "a", Progress(), cookies)["errors"] == []

    # Pause and cancel both reach the run as a stop.
    progress = TimedProgress(stop_on=LOGIN_WAIT_MARKER)
    started = time_module.monotonic()
    outcome = logged_in_download(second, tmp_path / "b", progress, cookies, should_stop=lambda: progress.stop)
    assert time_module.monotonic() - started < 10
    assert outcome["stopped"] is True
    assert outcome["items"] == [] and outcome["errors"] == []
    assert not list((tmp_path / "b").glob("Second*"))


# Half an emoji


def test_half_an_emoji_is_dropped_before_anything_is_named_or_written() -> None:
    from videocap.downloader import ScrubTextPP

    info = {
        "title": "說明\ud83d 完",
        "tags": ["好\udc00", "ok"],
        "formats": [{"format_note": "n\ud83d", "height": 720}],
        "duration": 19,
    }
    _, cleaned = ScrubTextPP().run(info)
    assert cleaned is info
    assert cleaned["title"] == "說明 完" and cleaned["tags"] == ["好", "ok"]
    assert cleaned["formats"] == [{"format_note": "n", "height": 720}] and cleaned["duration"] == 19


def test_half_an_emoji_never_reaches_a_programs_arguments() -> None:
    import subprocess
    import sys

    from videocap.downloader import TrackedPopen

    # Linux encodes arguments as UTF-8, where half a pair raises; the text is cleaned on every platform alike.
    with TrackedPopen(
        [sys.executable, "-X", "utf8", "-c", "import sys; print(sys.argv[1])", "a\ud83db"],
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    ) as process:
        output, _ = process.communicate(timeout=30)
    assert output.strip() == "ab"


# A clip's subtitles


@needs_ffmpeg
def test_a_clips_subtitle_file_is_cut_and_timed_from_the_clips_start(offline_youtube: OfflineYouTube, tmp_path: Path) -> None:
    url = offline_youtube.add_video("clipsubs001", "Zoo", subtitles=True)
    # The fixture's only cue runs from 1 s to 2 s of the whole video.
    outcome = download_link(url, tmp_path, download_subtitles=True, time_ranges="0:01-0:03")
    assert outcome["errors"] == []
    subtitle = Path(outcome["items"][0]["subtitle_paths"][0])
    text = subtitle.read_text(encoding="utf-8")
    assert "00:00:00,000 --> 00:00:01,000" in text and "Hello" in text
    assert "00:00:01,000 --> 00:00:02,000" not in text


@needs_ffmpeg
def test_a_clip_with_no_lines_of_its_own_gets_an_empty_subtitle_file(offline_youtube: OfflineYouTube, tmp_path: Path) -> None:
    url = offline_youtube.add_video("clipsubs002", "Zoo", subtitles=True)
    outcome = download_link(url, tmp_path, download_subtitles=True, time_ranges="0:03-0:05")
    assert outcome["errors"] == []
    subtitle = Path(outcome["items"][0]["subtitle_paths"][0])
    assert "Hello" not in subtitle.read_text(encoding="utf-8")


# Comment times


@pytest.mark.parametrize(
    ("comment", "expected"),
    [
        ({"_time_text": "6 years ago"}, "6 年前"),
        ({"_time_text": "23 hours ago (edited)"}, "23 小時前（已編輯）"),
        ({"_time_text": "1 month ago", "timestamp": 1700000000}, "1 個月前"),
        ({"time_text": "1 年前"}, "1 年前"),
        ({}, ""),
    ],
)
def test_comment_times_read_in_chinese(comment: dict[str, Any], expected: str) -> None:
    from videocap.sidecars import comment_time

    assert comment_time(comment) == expected


def test_a_comment_with_only_a_timestamp_shows_its_date() -> None:
    from datetime import datetime, timezone

    from videocap.sidecars import comment_time

    assert comment_time({"timestamp": 1700000000}) == datetime.fromtimestamp(1700000000, timezone.utc).astimezone().strftime("%Y-%m-%d")

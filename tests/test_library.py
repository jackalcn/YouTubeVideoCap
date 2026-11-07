from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest
import streamlit as st
from streamlit.runtime.context import ContextProxy
from streamlit.testing.v1 import AppTest

from videocap.library import (
    delete_entry,
    describe_probe,
    find_partial_files,
    folder_creation_problem,
    folder_has_recycle_bin,
    is_inside,
    is_intermediate_stream,
    media_entry,
    recycle_bin_available,
    scan_folder,
    scan_library,
)


def make_files(root: Path, *names: str) -> None:
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * 5)


def test_titles_ending_like_a_language_keep_their_sidecars(tmp_path: Path) -> None:
    make_files(tmp_path, "Learn Node.js.mp4", "Learn Node.js.en.srt", "Learn Node.js.webp", "Learn Node.js.description", "Learn Node.js.info.json")
    entry = scan_library(tmp_path)[0]
    assert [path.name for path in entry.sidecars] == [
        "Learn Node.js.description", "Learn Node.js.en.srt", "Learn Node.js.info.json", "Learn Node.js.webp",
    ]
    delete_entry(entry, tmp_path)
    assert list(tmp_path.iterdir()) == []


NEW_SIDECARS = ("{}.comments.csv", "{}.live_chat.csv", "{}.live_chat.json", "{}.nfo", "{}-thumb.jpg", "{}.zh-TW.srt")


def test_the_downloaders_newer_sidecars_go_with_their_video(tmp_path: Path) -> None:
    lesson = [name.format("Lesson 1") for name in NEW_SIDECARS]
    intro = [name.format("Lesson 1. Intro") for name in NEW_SIDECARS]
    # Someone else's spreadsheet and a video whose title ends in "-thumb" are not sidecars to take along.
    make_files(tmp_path, "Lesson 1.mp4", *lesson, "Lesson 1. Intro.mp4", *intro, "Lesson 1.csv", "Poster-thumb.mp4", "Poster-thumb.jpg")
    entries = {entry.path.name: entry for entry in scan_library(tmp_path)}
    assert sorted(path.name for path in entries["Lesson 1.mp4"].sidecars) == sorted(lesson)
    assert sorted(path.name for path in entries["Lesson 1. Intro.mp4"].sidecars) == sorted(intro)
    assert [path.name for path in entries["Poster-thumb.mp4"].sidecars] == ["Poster-thumb.jpg"]
    assert [path.name for path in media_entry(tmp_path / "Lesson 1.mp4").sidecars] == [path.name for path in entries["Lesson 1.mp4"].sidecars]

    delete_entry(entries["Lesson 1.mp4"], tmp_path)
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted(
        ["Lesson 1.csv", "Lesson 1. Intro.mp4", *intro, "Poster-thumb.mp4", "Poster-thumb.jpg"]
    )


def test_only_youtube_format_parts_are_hidden(tmp_path: Path) -> None:
    make_files(tmp_path, "Mix.f1.mp4", "Final.fx2.mp4", "clip.f137.mp4", "clip.f251-drc.webm")
    assert {entry.path.name for entry in scan_library(tmp_path)} == {"Mix.f1.mp4", "Final.fx2.mp4"}
    assert not is_intermediate_stream(Path("Mix.f1.mp4"))


def test_rotated_phone_video_shows_its_upright_size() -> None:
    probe = {"streams": [{"codec_type": "video", "codec_name": "h264", "width": 320, "height": 180, "side_data_list": [{"rotation": -90}]}]}
    assert describe_probe(probe) == ["影像 h264 180x320"]
    probe["streams"][0]["side_data_list"] = [{"rotation": 180}]
    assert describe_probe(probe) == ["影像 h264 320x180"]


def test_only_our_own_partial_files_are_offered(tmp_path: Path) -> None:
    make_files(
        tmp_path,
        "video.mp4",
        "video.mp4.part",
        "video.f137.mp4.part-Frag12",
        "video.mp4.ytdl",
        "video.en.vtt.part",
        "clip.f140.m4a",
        "clip_h264.temp.mp4",
        "Firefox download in progress.zip.part",
        "setup.exe.part",
        "Projects/~report.docx.tmp",
        "Projects/render.temp",
        "Projects/data.tmp",
    )
    assert {path.relative_to(tmp_path).as_posix() for path in find_partial_files(tmp_path)} == {
        "video.mp4.part", "video.f137.mp4.part-Frag12", "video.mp4.ytdl", "video.en.vtt.part", "clip.f140.m4a", "clip_h264.temp.mp4",
    }
    assert [entry.path.name for entry in scan_library(tmp_path)] == ["video.mp4"]


def test_an_unfinished_gif_is_offered_for_cleanup(tmp_path: Path) -> None:
    # 轉成 GIF writes "clip.temp.gif" until it is done; someone's own GIF is not ours to delete.
    make_files(tmp_path, "clip.mp4", "clip.temp.gif", "party.gif")
    assert [path.name for path in find_partial_files(tmp_path)] == ["clip.temp.gif"]


def test_only_orphaned_streams_are_offered(tmp_path: Path) -> None:
    # A download killed mid-run leaves its streams with no saved outcome; once merged, the finished file sits beside them.
    make_files(tmp_path, "Killed.f137.mp4", "Killed.f140.m4a", "Merged.f137.mp4", "Merged.mkv", "Sub/Song.f251-drc.webm")
    scan = scan_folder(tmp_path)
    assert {path.relative_to(tmp_path).as_posix() for path in scan.orphan_streams} == {"Killed.f137.mp4", "Killed.f140.m4a", "Sub/Song.f251-drc.webm"}
    assert scan.partial_files == {}
    assert {path.name for path in find_partial_files(tmp_path)} == {"Killed.f137.mp4", "Killed.f140.m4a", "Song.f251-drc.webm"}


def test_the_newest_files_are_kept_when_the_list_is_cut(tmp_path: Path) -> None:
    for index in range(30):
        make_files(tmp_path, f"Channel {index:02d}/old {index}.mp4")
        os.utime(tmp_path / f"Channel {index:02d}/old {index}.mp4", (1000 + index, 1000 + index))
    make_files(tmp_path, "Channel 29/newest download.mp4")
    scan = scan_folder(tmp_path, limit=5)
    assert scan.media_count == 31
    assert [entry.path.name for entry in scan.entries] == ["newest download.mp4", "old 29.mp4", "old 28.mp4", "old 27.mp4", "old 26.mp4"]


class FakeRecycleBin:
    """Stands in for send2trash: moves each file into a folder of its own and remembers it, so no test ever sends
    anything to this machine's real recycle bin."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.calls = 0
        self.sent: list[Path] = []
        self.error: Exception | None = None
        # Files it refuses, as Windows refuses one that another program has open.
        self.in_use: set[Path] = set()

    def __call__(self, path: str) -> None:
        self.calls += 1
        if self.error:
            raise self.error
        if Path(path) in self.in_use:
            raise OSError(32, "The process cannot access the file because it is being used by another process", path)
        self.folder.mkdir(exist_ok=True)
        os.replace(path, self.folder / f"{len(self.sent)} {Path(path).name}")
        self.sent.append(Path(path))

    def names(self) -> list[str]:
        return sorted(path.name for path in self.sent)


def use_fake_recycle_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeRecycleBin:
    import videocap.library as library

    fake = FakeRecycleBin(tmp_path / "回收桶")
    monkeypatch.setattr(library, "send_to_trash", fake)
    # pytest's folders are inside the temp folder, which never uses the recycle bin, and this machine's drives may not
    # be the kind that has one.
    monkeypatch.setattr(library, "TEMP_FOLDER", tmp_path / "temp")
    monkeypatch.setattr(library, "folder_has_recycle_bin", lambda folder: True)
    monkeypatch.setattr(library, "_devices_without_recycle_bin", set())
    return fake


@pytest.fixture
def recycle_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeRecycleBin:
    return use_fake_recycle_bin(tmp_path, monkeypatch)


def test_deleting_sends_the_video_and_its_sidecars_to_the_recycle_bin(tmp_path: Path, recycle_bin: FakeRecycleBin) -> None:
    root = tmp_path / "videos"
    make_files(root, "clip.mp4", "clip.en.srt", "clip.webp", "clip and more.mp4")
    assert recycle_bin_available(root)
    result = delete_entry(media_entry(root / "clip.mp4"), root)
    assert result == []
    assert (result.trashed, result.removed, result.outcome) == (3, 0, "已移到回收桶")
    assert recycle_bin.names() == ["clip.en.srt", "clip.mp4", "clip.webp"]
    assert [path.name for path in root.iterdir()] == ["clip and more.mp4"]


def test_without_send2trash_files_are_deleted_for_good(tmp_path: Path, recycle_bin: FakeRecycleBin, monkeypatch: pytest.MonkeyPatch) -> None:
    import videocap.library as library

    # An install from before send2trash was required.
    monkeypatch.setattr(library, "send_to_trash", None)
    root = tmp_path / "videos"
    make_files(root, "clip.mp4", "clip.en.srt")
    assert not recycle_bin_available(root)
    result = delete_entry(media_entry(root / "clip.mp4"), root)
    assert result == []
    assert (result.trashed, result.removed, result.outcome) == (0, 2, "已永久刪除（這個位置沒有回收桶）")
    assert list(root.iterdir()) == [] and recycle_bin.calls == 0


def test_a_recycle_bin_that_refuses_falls_back_to_deleting_and_is_remembered(tmp_path: Path, recycle_bin: FakeRecycleBin) -> None:
    from send2trash.exceptions import TrashPermissionError

    root = tmp_path / "videos"
    make_files(root, "first.mp4", "first.en.srt", "second.mp4")
    # What send2trash raises on a NAS share or a Docker volume where it cannot make a trash folder.
    recycle_bin.error = TrashPermissionError(str(root / "first.mp4"))
    result = delete_entry(media_entry(root / "first.mp4"), root)
    assert result == []
    assert (result.trashed, result.removed, result.outcome) == (0, 2, "已永久刪除（這個位置沒有回收桶）")
    assert [path.name for path in root.iterdir()] == ["second.mp4"]

    # The next delete there says so before anything is deleted, and does not try again.
    calls = recycle_bin.calls
    assert not recycle_bin_available(root)
    assert delete_entry(media_entry(root / "second.mp4"), root).removed == 1
    assert recycle_bin.calls == calls


def test_a_file_in_use_is_reported_and_kept(tmp_path: Path, recycle_bin: FakeRecycleBin, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "videos"
    make_files(root, "clip.mp4", "clip.en.srt")
    locked = root / "clip.mp4"
    real_unlink = Path.unlink

    def unlink(self: Path, missing_ok: bool = False) -> None:
        if self == locked:
            raise PermissionError(32, "The process cannot access the file because it is being used by another process")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)
    recycle_bin.in_use.add(locked)
    result = delete_entry(media_entry(locked), root)
    assert len(result) == 1 and "clip.mp4 無法刪除" in result[0]
    assert (result.trashed, result.removed) == (1, 0)
    assert locked.exists() and recycle_bin.names() == ["clip.en.srt"]
    # A file in use says nothing about the recycle bin.
    assert recycle_bin_available(root)


def test_the_temp_folder_never_uses_the_recycle_bin(tmp_path: Path, recycle_bin: FakeRecycleBin, monkeypatch: pytest.MonkeyPatch) -> None:
    import videocap.library as library

    monkeypatch.setattr(library, "TEMP_FOLDER", tmp_path)
    make_files(tmp_path, "public/clip.mp4")
    assert not recycle_bin_available(tmp_path / "public")
    assert delete_entry(media_entry(tmp_path / "public" / "clip.mp4"), tmp_path).removed == 1
    assert recycle_bin.calls == 0


def test_test_files_never_reach_the_real_recycle_bin(tmp_path: Path) -> None:
    import videocap.library as library

    # Every other test that deletes relies on this: pytest's folders are in the temp folder, which deletes for good.
    assert is_inside(tmp_path, library.TEMP_FOLDER)
    assert not recycle_bin_available(tmp_path)


def test_which_folders_have_a_recycle_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import videocap.library as library

    # Windows: only a local hard drive. A network drive, a USB stick or a RAM disk deletes for good.
    for drive_type, expected in ((3, True), (4, False), (2, False), (6, False), (1, False)):
        monkeypatch.setattr(library, "windows_drive_type", lambda folder, drive_type=drive_type: drive_type)
        assert folder_has_recycle_bin(tmp_path, "win32") is expected, drive_type
    assert folder_has_recycle_bin(tmp_path, "darwin")
    # Linux: only with a desktop. In Docker or on a server, a trash folder would quietly fill the disk.
    for name in ("DISPLAY", "WAYLAND_DISPLAY", "XDG_CURRENT_DESKTOP"):
        monkeypatch.delenv(name, raising=False)
    assert not folder_has_recycle_bin(tmp_path, "linux")
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "GNOME")
    assert folder_has_recycle_bin(tmp_path, "linux")


@pytest.mark.skipif(sys.platform != "win32", reason="drive types are a Windows question")
def test_this_machines_own_drive_has_a_recycle_bin() -> None:
    assert folder_has_recycle_bin(Path.home())
    assert not folder_has_recycle_bin(Path("\\\\localhost\\c$\\Users"))


def test_recycle_bins_inside_the_library_are_not_listed(tmp_path: Path) -> None:
    make_files(
        tmp_path,
        "keep.mp4",
        "Trash talk/clip.mp4",
        "$RECYCLE.BIN/S-1-5-21-1/$RABC123.mp4",
        ".Trash-1000/files/old.mp4",
        ".Trashes/501/old.mp4",
        "#recycle/old.mp4",
        "@Recycle/old.mp4",
    )
    assert sorted(entry.path.relative_to(tmp_path).as_posix() for entry in scan_library(tmp_path)) == ["Trash talk/clip.mp4", "keep.mp4"]


def test_scan_looks_up_each_file_only_through_the_folder_listing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for index in range(50):
        make_files(tmp_path, f"folder {index % 5}/video {index}.mp4", f"folder {index % 5}/video {index}.en.srt", f"folder {index % 5}/notes {index}.docx")
    calls = []
    real_stat = os.stat
    monkeypatch.setattr(os, "stat", lambda *args, **kwargs: calls.append(args) or real_stat(*args, **kwargs))
    scan = scan_folder(tmp_path)
    assert len(scan.entries) == 50
    assert all(len(entry.sidecars) == 1 for entry in scan.entries)
    # Only the root is checked by path; every file's details come with its folder's listing.
    assert len(calls) <= 2


@pytest.fixture
def library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import videocap.services as services_module

    monkeypatch.setenv("VIDEOCAP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("VIDEOCAP_OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setenv("VIDEOCAP_TRUST_REMOTE", "1")
    monkeypatch.setenv("VIDEOCAP_MODE", "local")
    monkeypatch.delenv("VIDEOCAP_PASSWORD", raising=False)
    monkeypatch.setattr(services_module, "CLOUD_OUTPUT_ROOT", tmp_path / "public")
    st.cache_resource.clear()
    root = tmp_path / "out"
    root.mkdir()
    yield root
    st.cache_resource.clear()


class TableSelections:
    """Stands in for st.dataframe, which AppTest cannot click. Like Streamlit, it keeps a selection per widget key."""

    def __init__(self) -> None:
        self.rows: dict[str, list[int]] = {}
        self.last_key = ""
        self.real = st.dataframe

    def __call__(self, data: Any, **kwargs: Any) -> Any:
        self.last_key = kwargs["key"]
        self.real(data, **{name: value for name, value in kwargs.items() if name not in {"on_select", "selection_mode"}})
        selection = type("Selection", (), {"rows": self.rows.get(self.last_key, []), "columns": []})()
        return type("State", (), {"selection": selection})()

    def select(self, row: int) -> None:
        self.rows[self.last_key] = [row]


@pytest.fixture
def table(monkeypatch: pytest.MonkeyPatch) -> TableSelections:
    fake = TableSelections()
    monkeypatch.setattr(st, "dataframe", fake)
    return fake


def library_page_script() -> None:
    from videocap.ui.context import build_context, init_session_state
    from videocap.ui.page_library import render_library_page

    ctx = build_context()
    init_session_state(ctx)
    render_library_page(ctx)


def run_library_page() -> AppTest:
    app = AppTest.from_function(library_page_script, default_timeout=30)
    app.run()
    assert not app.exception, app.exception
    return app


def rerun(app: AppTest) -> None:
    app.run()
    assert not app.exception, app.exception


def button(app: AppTest, label: str) -> Any:
    return next(item for item in app.button if item.label == label)


def details_title(app: AppTest) -> str:
    return next((item.value for item in app.markdown if item.value.startswith("**")), "")


def test_selection_never_jumps_to_another_file(library: Path, table: TableSelections) -> None:
    make_files(library, "Alpha clip.mp4", "Bravo clip.mp4")
    os.utime(library / "Bravo clip.mp4", (1000, 1000))
    app = run_library_page()
    table.select(0)
    rerun(app)
    assert "Alpha clip" in details_title(app)

    make_files(library, "Zulu new download.mp4")
    button(app, "重新整理").click()
    rerun(app)
    # The new file now fills row 0; the old selection must not carry over to it.
    assert "Zulu" not in details_title(app)
    assert not [item for item in app.button if item.label == "刪除"]


def test_partial_cleanup_lists_only_our_files_and_survives_a_locked_one(library: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    make_files(library, "video.mp4", "video.mp4.part", "other.mp4.ytdl", "Firefox download.zip.part", "Projects/~report.docx.tmp")
    locked = library / "other.mp4.ytdl"
    real_unlink = Path.unlink

    def unlink(self: Path, missing_ok: bool = False) -> None:
        if self == locked:
            raise PermissionError(32, "The process cannot access the file because it is being used by another process")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)
    app = run_library_page()
    assert app.expander[0].label.startswith("未完成的暫存檔：2 個")
    assert "video.mp4.part" in app.text[0].value and "Firefox" not in app.text[0].value
    button(app, "清除暫存檔").click()
    rerun(app)
    assert sorted(path.relative_to(library).as_posix() for path in library.rglob("*") if path.is_file()) == [
        "Firefox download.zip.part", "Projects/~report.docx.tmp", "other.mp4.ytdl", "video.mp4",
    ]
    assert "other.mp4.ytdl" in app.success[0].value


def test_streams_are_not_offered_while_a_job_may_still_write_them(library: Path) -> None:
    from videocap.jobs import Job
    from videocap.media_tools import ToolRequest
    from videocap.services import KIND_TOOL, LOCAL_OWNER, get_shared_services

    make_files(library, "Talk.f137.mp4", "Talk.f140.m4a", "video.mp4.part")
    app = run_library_page()
    assert app.expander[0].label.startswith("未完成的暫存檔：3 個")
    assert "Talk.f137.mp4" in app.text[0].value

    request = ToolRequest("trim", str(library / "video.mp4"))
    get_shared_services(False).manager.add_paused(Job("waiting", LOCAL_OWNER, KIND_TOOL, "trim", request))
    button(app, "重新整理").click()
    rerun(app)
    assert app.expander[0].label.startswith("未完成的暫存檔：1 個")
    assert "Talk" not in app.text[0].value


def test_preview_with_a_big5_subtitle_keeps_the_page_working(library: Path, table: TableSelections) -> None:
    make_files(library, "Lesson.mp4")
    (library / "Lesson.zh-TW.srt").write_bytes("1\n00:00:01,000 --> 00:00:02,000\n大家好\n".encode("cp950"))
    app = run_library_page()
    table.select(0)
    rerun(app)
    button(app, "預覽").click()
    rerun(app)
    rerun(app)
    assert {"開啟檔案", "開啟資料夾", "送到工具", "刪除"} <= {item.label for item in app.button}


def test_times_follow_the_visitors_time_zone(library: Path, table: TableSelections, monkeypatch: pytest.MonkeyPatch) -> None:
    # A public host's clock runs in UTC; the visitor reads the time on their own clock.
    monkeypatch.setattr(ContextProxy, "timezone", property(lambda self: "America/Los_Angeles"))
    make_files(library, "Clip.mp4")
    os.utime(library / "Clip.mp4", (1_700_000_000, 1_700_000_000))
    app = run_library_page()
    assert list(app.dataframe[0].value["修改時間"]) == ["2023-11-14 14:13"]


def test_a_listing_too_big_to_bundle_says_to_download_one_by_one(library: Path, table: TableSelections, monkeypatch: pytest.MonkeyPatch) -> None:
    from videocap.ui import page_library

    make_files(library, "One.mp4", "Two.mp4", "Three.mp4")
    app = run_library_page()
    assert [item.label for item in app.checkbox] == ["顯示打包下載"]

    monkeypatch.setattr(page_library, "BUNDLE_MAX_BYTES", 12)
    rerun(app)
    assert not app.checkbox
    assert "逐一下載" in " ".join(item.value for item in app.caption)


def unused_drive() -> str:
    return next(f"{letter}:\\" for letter in "ZYXWVUTSRQPONMLKJIHG" if not Path(f"{letter}:\\").exists())


def test_a_folder_that_can_be_created_is_fine(tmp_path: Path) -> None:
    assert folder_creation_problem(tmp_path / "new" / "deeper") == ""


def test_a_folder_inside_a_file_can_never_be_created(tmp_path: Path) -> None:
    make_files(tmp_path, "Clip.mp4")
    assert "是檔案" in folder_creation_problem(tmp_path / "Clip.mp4" / "videos")
    assert "是檔案" in folder_creation_problem(tmp_path / "Clip.mp4")


@pytest.mark.skipif(sys.platform != "win32", reason="drive letters and reserved characters are Windows rules")
def test_a_missing_drive_or_reserved_characters_can_never_be_created(tmp_path: Path) -> None:
    assert "找不到磁碟" in folder_creation_problem(Path(unused_drive()) / "Videos")
    assert "不能含有" in folder_creation_problem(tmp_path / '"C:' / "Videos")


def test_the_library_says_when_a_folder_can_never_be_created(library: Path) -> None:
    from videocap.ui.context import OUTPUT_DIR_WIDGET_KEY

    make_files(library, "Clip.mp4")
    app = run_library_page()
    app.text_input(key=OUTPUT_DIR_WIDGET_KEY).set_value(str(library / "Clip.mp4" / "videos"))
    rerun(app)
    assert "是檔案" in app.error[0].value
    assert not app.info

    app.text_input(key=OUTPUT_DIR_WIDGET_KEY).set_value(str(library / "not yet"))
    rerun(app)
    assert "會自動建立" in app.info[0].value
    assert not app.error


def test_transcript_reader(library: Path, table: TableSelections) -> None:
    from test_subtitles import rolling_captions

    make_files(library, "Talk.mp4", "Styled.mp4", "Styled.en.ass")
    os.utime(library / "Styled.mp4", (1000, 1000))
    (library / "Talk.en.vtt").write_text(rolling_captions(180), encoding="utf-8")
    app = run_library_page()
    table.select(0)
    rerun(app)
    app.text_input(key="library_transcript_search").set_value("word9000 ")
    rerun(app)
    assert "找到 1 句" in " ".join(item.value for item in app.caption)
    assert "[2:30:00] word9000 and some more speech here" in app.text[-1].value

    table.select(1)
    rerun(app)
    assert "格式無法在這裡閱讀" in " ".join(item.value for item in app.caption)


def test_probing_opens_no_console_window(sample_video: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    import videocap.library as library

    options: list[dict[str, Any]] = []
    real_run = subprocess.run

    def recording_run(command: Any, **kwargs: Any) -> Any:
        options.append(kwargs)
        return real_run(command, **kwargs)

    monkeypatch.setattr(library.subprocess, "run", recording_run)
    probe = library.probe_media(sample_video)
    assert probe is not None and probe["streams"]
    expected = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    assert [kwargs["creationflags"] for kwargs in options] == [expected]

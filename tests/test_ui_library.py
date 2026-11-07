"""The 媒體庫 page: how the files beside a video are named for the person reading the list."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from test_library import FakeRecycleBin, use_fake_recycle_bin
from videocap.library import LibraryEntry


SIDECARS = {
    "Clip.zh-TW.srt": "字幕",
    "Clip.comments.csv": "留言 CSV",
    "Clip.live_chat.csv": "聊天室紀錄",
    "Clip.live_chat.json": "聊天室紀錄",
    "Clip.nfo": "NFO 資訊檔",
    "Clip-thumb.jpg": "縮圖",
    "Clip.webp": "封面",
    "Clip.info.json": "完整資訊",
    "Clip.description": "影片說明",
    "Clip.zh-TW.txt": "逐字稿",
}


@pytest.fixture
def library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    folder = tmp_path / "out"
    folder.mkdir()
    monkeypatch.setenv("VIDEOCAP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("VIDEOCAP_OUTPUT_DIR", str(folder))
    monkeypatch.setenv("VIDEOCAP_TRUST_REMOTE", "1")
    monkeypatch.setenv("VIDEOCAP_MODE", "local")
    monkeypatch.delenv("VIDEOCAP_PASSWORD", raising=False)
    st.cache_resource.clear()
    yield folder
    st.cache_resource.clear()


@pytest.fixture
def recycle_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeRecycleBin:
    return use_fake_recycle_bin(tmp_path, monkeypatch)


class FirstRowSelected:
    """AppTest cannot click a table row, so the library's table reports its first row as picked."""

    def __init__(self) -> None:
        self.real = st.dataframe

    def __call__(self, data: Any, **kwargs: Any) -> Any:
        self.real(data, **{name: value for name, value in kwargs.items() if name not in {"on_select", "selection_mode"}})
        selection = type("Selection", (), {"rows": [0], "columns": []})()
        return type("State", (), {"selection": selection})()


class DeleteDialogKeptOpen:
    """Streamlit reruns only the dialog when a button inside it is clicked, so the dialog stays open. AppTest reruns the
    whole page, where 刪除 is no longer pressed, so here it stays pressed until 確定刪除 is."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from streamlit.delta_generator import DeltaGenerator

        self.open = False
        real_column_button = DeltaGenerator.button
        real_button = st.button

        def column_button(container: Any, label: str, *args: Any, **kwargs: Any) -> bool:
            clicked = real_column_button(container, label, *args, **kwargs)
            if kwargs.get("key") != "library_delete":
                return clicked
            self.open = self.open or clicked
            return self.open

        def button(label: str, *args: Any, **kwargs: Any) -> bool:
            clicked = real_button(label, *args, **kwargs)
            if clicked and label == "確定刪除":
                self.open = False
            return clicked

        monkeypatch.setattr(DeltaGenerator, "button", column_button)
        monkeypatch.setattr(st, "button", button)


def library_page_script() -> None:
    from videocap.ui.context import build_context, init_session_state
    from videocap.ui.page_library import render_library_page

    ctx = build_context()
    init_session_state(ctx)
    render_library_page(ctx)


def test_files_beside_a_video_are_named_by_what_they_are(library: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (library / "Clip.mp4").write_bytes(b"video")
    for name in SIDECARS:
        (library / name).write_bytes(b"x")
    monkeypatch.setattr(st, "dataframe", FirstRowSelected())
    app = AppTest.from_function(library_page_script, default_timeout=30)
    app.run()
    assert not app.exception, app.exception
    # Labels are Markdown, so file names arrive escaped.
    labels = {str(item.label).replace("\\", "") for item in app.button}
    for name, kind in SIDECARS.items():
        assert f"下載{kind}：{name}（1 B）" in labels, (name, sorted(labels))
    assert "下載 Clip.mp4（5 B）" in labels


@pytest.mark.parametrize(
    ("has_recycle_bin", "plan", "outcome"),
    [
        (True, "會移到回收桶", "「Clip.mp4」已移到回收桶。"),
        (False, "會永久刪除", "「Clip.mp4」已永久刪除（這個位置沒有回收桶）。"),
    ],
)
def test_deleting_says_first_whether_the_files_go_to_the_recycle_bin(
    library: Path, recycle_bin: FakeRecycleBin, monkeypatch: pytest.MonkeyPatch, has_recycle_bin: bool, plan: str, outcome: str
) -> None:
    import videocap.library as library_module

    if not has_recycle_bin:
        monkeypatch.setattr(library_module, "send_to_trash", None)
    (library / "Clip.mp4").write_bytes(b"video")
    (library / "Clip.zh-TW.srt").write_bytes(b"x")
    (library / "Other.mp4").write_bytes(b"video")
    # Older, so the first row is the clip.
    os.utime(library / "Other.mp4", (1000, 1000))
    monkeypatch.setattr(st, "dataframe", FirstRowSelected())
    DeleteDialogKeptOpen(monkeypatch)
    app = AppTest.from_function(library_page_script, default_timeout=30)
    app.run()
    next(item for item in app.button if item.label == "刪除").click()
    app.run()
    assert not app.exception, app.exception
    said = " ".join(str(item.value) for item in app.markdown).replace("\\", "")
    assert plan in said and ("永久" in said) is not has_recycle_bin

    next(item for item in app.button if item.label == "確定刪除").click()
    app.run()
    assert not app.exception, app.exception
    assert [str(item.value).replace("\\", "") for item in app.success] == [outcome]
    assert recycle_bin.names() == (["Clip.mp4", "Clip.zh-TW.srt"] if has_recycle_bin else [])
    assert [path.name for path in library.iterdir()] == ["Other.mp4"]


@pytest.mark.parametrize(
    ("video", "sidecar", "kind"),
    [
        # A video called "Poster-thumb" owns "Poster-thumb.jpg" as its plain cover.
        ("Poster-thumb.mp4", "Poster-thumb.jpg", "封面"),
        ("Poster.mp4", "Poster-thumb.jpg", "縮圖"),
        ("Talk.mkv", "Talk.LIVE_CHAT.json", "聊天室紀錄"),
        ("Talk.mkv", "Talk.notes.md", "附加檔案"),
    ],
)
def test_names_that_look_alike_are_told_apart(video: str, sidecar: str, kind: str) -> None:
    from videocap.ui.page_library import sidecar_kind

    entry = LibraryEntry(path=Path(video), size=1, modified_at=0.0, kind="video")
    assert sidecar_kind(entry, Path(sidecar)) == kind

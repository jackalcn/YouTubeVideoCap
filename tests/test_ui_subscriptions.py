"""The subscription cards: how their buttons are laid out, and what an edit that cannot be saved shows."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Optional

import pytest
from streamlit.proto.Block_pb2 import Block as BlockProto
from streamlit.testing.v1 import AppTest

from test_library import FakeRecycleBin, use_fake_recycle_bin
from test_ui_admin import button, isolated_app, new_page, owner_services, texts  # noqa: F401
from videocap.settings import DownloadSettings


CHANNEL = "https://www.youtube.com/@somechannel/videos"


@pytest.fixture
def recycle_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeRecycleBin:
    return use_fake_recycle_bin(tmp_path, monkeypatch)


def page_with_a_subscription(tmp_path: Path) -> tuple[AppTest, int]:
    subscription_id = owner_services().storage.add_subscription(CHANNEL, "Some channel", DownloadSettings(), str(tmp_path / "old"), 5, 24)
    app = new_page("subscriptions")
    app.session_state["settings_loaded"] = True
    app.run()
    assert not app.exception, app.exception
    return app, subscription_id


def blocks_around(block: Any, target: Any) -> Optional[list[Any]]:
    """The blocks that hold target, innermost first."""
    for child in block.children.values():
        if child is target:
            return [block]
        found = blocks_around(child, target) if hasattr(child, "children") else None
        if found is not None:
            return [*found, block]
    return None


def is_wrapping_row(block: Any) -> bool:
    flex = block.proto.flex_container if block.type == "flex_container" else None
    return bool(flex and flex.wrap and flex.direction == BlockProto.FlexContainer.Direction.HORIZONTAL)


def popover(app: AppTest, label: str) -> Any:
    return next(node for node in app.main if node.type == "popover" and node.proto.popover.label == label)


def test_a_cards_buttons_share_a_row_that_wraps(tmp_path: Path) -> None:
    app, _ = page_with_a_subscription(tmp_path)
    # A sixth of the card was too narrow for 「保留規則」 on one line, and a phone stacks columns into a tall pile.
    for item in (popover(app, "編輯"), popover(app, "保留規則"), button(app, "立即檢查"), button(app, "刪除")):
        around = blocks_around(app.main, item)
        assert around and is_wrapping_row(around[0])
        assert "column" not in [block.type for block in around]


@pytest.mark.parametrize(
    ("has_recycle_bin", "plan", "outcome"),
    [
        (True, "刪除的影片會移到回收桶", "已刪除 1 部影片（已移到回收桶）"),
        (False, "會永久刪除", "已刪除 1 部影片（已永久刪除，這個位置沒有回收桶）"),
    ],
)
def test_a_retention_rule_says_where_old_videos_go_and_deletes_only_those(
    tmp_path: Path, recycle_bin: FakeRecycleBin, monkeypatch: pytest.MonkeyPatch, has_recycle_bin: bool, plan: str, outcome: str
) -> None:
    import videocap.library as library_module

    if not has_recycle_bin:
        monkeypatch.setattr(library_module, "send_to_trash", None)
    app, subscription_id = page_with_a_subscription(tmp_path)
    storage = owner_services().storage
    folder = tmp_path / "old" / "Some channel"
    folder.mkdir(parents=True)
    for name, age_days in (("老影片", 40), ("新影片", 1)):
        for suffix in (".mp4", ".zh-TW.srt"):
            (folder / f"{name}{suffix}").write_bytes(b"video")
        storage.add_history(
            [
                {
                    "video_id": name,
                    "title": name,
                    "path": str(folder / f"{name}.mp4"),
                    "size_bytes": 5,
                    "finished_at": time.time() - age_days * 86400,
                    "subscription_id": subscription_id,
                }
            ]
        )
    # Not the subscription's: a file of the same age that someone put there by hand.
    (folder / "自己放的.mp4").write_bytes(b"video")

    app.radio(key=f"subscription_retention_mode_{subscription_id}").set_value("days").run()
    assert plan in texts(app.caption)
    button(app, "儲存並刪除 1 部").click().run()
    assert not app.exception, app.exception
    assert outcome in texts(app.success)
    assert recycle_bin.names() == (["老影片.mp4", "老影片.zh-TW.srt"] if has_recycle_bin else [])
    assert sorted(path.name for path in folder.iterdir()) == ["新影片.mp4", "新影片.zh-TW.srt", "自己放的.mp4"]


def test_an_edit_that_cannot_be_saved_says_so_across_the_card_and_keeps_what_was_typed(tmp_path: Path) -> None:
    app, subscription_id = page_with_a_subscription(tmp_path)
    app.text_input(key=f"subscription_edit_title_{subscription_id}").set_value("新聞台")
    app.text_input(key=f"subscription_edit_folder_{subscription_id}").set_value("   ")
    button(app, "儲存").click().run()
    assert not app.exception, app.exception

    error = next(item for item in app.error if "請輸入下載資料夾" in item.value)
    around = blocks_around(app.main, error)
    assert around and not [block for block in around if block.type in ("column", "popover") or is_wrapping_row(block)]
    assert owner_services().storage.get_subscription(subscription_id).title == "Some channel"

    # Still there for another try, also after the page runs again for something else.
    for _ in range(2):
        assert app.text_input(key=f"subscription_edit_title_{subscription_id}").value == "新聞台"
        assert app.text_input(key=f"subscription_edit_folder_{subscription_id}").value == "   "
        app.run()

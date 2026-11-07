from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Iterator

import pytest

from videocap import chinese
from videocap.chinese import (
    SIMPLIFIED,
    TRADITIONAL,
    ChineseConversionUnavailable,
    chinese_script,
    is_simplified,
    is_traditional,
    to_traditional,
    traditional_copy_path,
    write_traditional_copy,
)
from videocap.subtitles import parse_subtitles


@pytest.fixture
def no_opencc(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # None in sys.modules makes "import opencc" fail, as on a machine that never installed it.
    monkeypatch.setitem(sys.modules, "opencc", None)
    chinese._converter.cache_clear()
    yield
    chinese._converter.cache_clear()


def test_converts_to_taiwanese_wording() -> None:
    assert to_traditional("这个软件的鼠标设置在网络视频里") == "這個軟體的滑鼠設定在網路影片裡"
    # One Simplified character can be several Traditional ones, and phrase conversion picks by context.
    assert to_traditional("头发发展") == "頭髮發展"


def test_leaves_traditional_and_other_text_alone() -> None:
    text = "大家好，歡迎收看！Hello <b>world</b> 00:00:01,000 --> 00:00:02,000 & 123"
    assert to_traditional(text) == text
    assert to_traditional("") == ""


def test_the_converter_is_loaded_once(monkeypatch: pytest.MonkeyPatch) -> None:
    import opencc

    chinese._converter.cache_clear()
    loads: list[str] = []
    real = opencc.OpenCC
    monkeypatch.setattr(opencc, "OpenCC", lambda config: loads.append(config) or real(config))
    for _ in range(3):
        to_traditional("简体")
    assert loads == ["s2twp"]
    chinese._converter.cache_clear()


def test_threads_can_convert_together() -> None:
    texts = [f"第{index}个视频的字幕" for index in range(40)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(to_traditional, texts))
    assert results == [f"第{index}個影片的字幕" for index in range(40)]


def test_missing_opencc_gives_a_clear_message(no_opencc: None, tmp_path: Path) -> None:
    with pytest.raises(ChineseConversionUnavailable) as caught:
        to_traditional("软件")
    message = str(caught.value)
    assert "OpenCC" in message and "start.bat" in message and "pip install opencc" in message
    assert not chinese.is_available()

    subtitle = tmp_path / "clip.zh-Hans.srt"
    subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\n软件\n", encoding="utf-8")
    with pytest.raises(ChineseConversionUnavailable):
        write_traditional_copy(subtitle)
    assert not (tmp_path / "clip.zh-TW.srt").exists()


def test_is_available_with_opencc() -> None:
    assert chinese.is_available()


@pytest.mark.parametrize(
    ("language", "script"),
    [
        ("zh-Hans", SIMPLIFIED),
        ("zh-CN", SIMPLIFIED),
        ("zh-SG", SIMPLIFIED),
        ("zh_cn", SIMPLIFIED),
        ("zh-Hans-orig", SIMPLIFIED),
        ("cmn-Hans-CN", SIMPLIFIED),
        ("zh-Hant", TRADITIONAL),
        ("zh-TW", TRADITIONAL),
        ("zh-HK", TRADITIONAL),
        ("zh-MO", TRADITIONAL),
        ("ZH-hant", TRADITIONAL),
        # The script outranks the region.
        ("zh-Hant-CN", TRADITIONAL),
        ("zh-Hans-HK", SIMPLIFIED),
        ("zh", ""),
        ("zh-orig", ""),
        ("en", ""),
        ("en-TW", ""),
        ("ja", ""),
        ("", ""),
        ("live_chat", ""),
    ],
)
def test_chinese_script(language: str, script: str) -> None:
    assert chinese_script(language) == script


def test_plain_zh_is_simplified_only_when_asked() -> None:
    assert chinese_script("zh", plain_zh=SIMPLIFIED) == SIMPLIFIED
    assert is_simplified("zh", plain_zh=True)
    assert not is_simplified("zh")
    # The fallback is only for a code that names nothing.
    assert not is_simplified("zh-TW", plain_zh=True)
    assert not is_simplified("zh-Latn", plain_zh=True)
    assert is_simplified("zh-Hans") and not is_traditional("zh-Hans")
    assert is_traditional("zh-TW") and not is_simplified("zh-TW")
    assert not is_traditional("zh")


def test_traditional_copy_names() -> None:
    folder = Path("videos")
    assert traditional_copy_path(folder / "clip.zh-Hans.srt") == folder / "clip.zh-TW.srt"
    assert traditional_copy_path(folder / "Node.js 入門.zh-CN.vtt") == folder / "Node.js 入門.zh-TW.vtt"
    assert traditional_copy_path(folder / "clip.zh-Hans.txt") == folder / "clip.zh-TW.txt"
    # No language in the name: the dot in a title is not one.
    assert traditional_copy_path(folder / "第 1. 集.srt") == folder / "第 1. 集.zh-TW.srt"


def test_write_traditional_copy_keeps_the_original_and_its_format(tmp_path: Path) -> None:
    original = "WEBVTT\r\n\r\n00:00:01.000 --> 00:00:02.500 align:start\r\n<c>这个视频</c>\r\n\r\n00:00:03.000 --> 00:00:04.000\r\n下载软件\r\n"
    source = tmp_path / "clip.zh-Hans.vtt"
    # Mainland tools often save GBK.
    source.write_bytes(original.encode("gb18030"))
    target = write_traditional_copy(source)
    assert target == tmp_path / "clip.zh-TW.vtt"
    assert source.read_bytes() == original.encode("gb18030")
    written = target.read_bytes().decode("utf-8")
    assert written == original.replace("这个视频", "這個影片").replace("下载软件", "下載軟體")
    assert [cue.text for cue in parse_subtitles(written)] == ["這個影片", "下載軟體"]


def test_write_traditional_copy_never_replaces_an_existing_file(tmp_path: Path) -> None:
    source = tmp_path / "clip.zh-Hans.srt"
    source.write_text("1\n00:00:01,000 --> 00:00:02,000\n软件\n", encoding="utf-8")
    real_track = tmp_path / "clip.zh-TW.srt"
    real_track.write_text("1\n00:00:01,000 --> 00:00:02,000\n創作者的翻譯\n", encoding="utf-8")
    assert write_traditional_copy(source) is None
    assert "創作者的翻譯" in real_track.read_text(encoding="utf-8")
    assert write_traditional_copy(source, overwrite=True) == real_track
    assert "軟體" in real_track.read_text(encoding="utf-8")
    # A file that already has the target name is not its own copy.
    assert write_traditional_copy(real_track, overwrite=True) is None

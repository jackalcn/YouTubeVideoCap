from __future__ import annotations

import time
from pathlib import Path

import pytest

from videocap.textfiles import decode_text, read_text_file

TRADITIONAL = "1\n00:00:01,000 --> 00:00:02,000\n這是一段繁體中文字幕，用來測試編碼。\n"
SIMPLIFIED = "1\n00:00:01,000 --> 00:00:02,000\n这是一段简体中文字幕，用来测试编码。\n"


@pytest.mark.parametrize(
    "data",
    [
        TRADITIONAL.encode("utf-8"),
        TRADITIONAL.encode("utf-8-sig"),
        TRADITIONAL.encode("utf-16"),
        TRADITIONAL.encode("utf-16-le"),
        TRADITIONAL.encode("utf-16-be"),
        TRADITIONAL.encode("utf-32"),
        TRADITIONAL.encode("cp950"),
    ],
    ids=["utf-8", "utf-8-bom", "utf-16-bom", "utf-16-le", "utf-16-be", "utf-32", "big5"],
)
def test_traditional_chinese_survives_every_common_encoding(data: bytes) -> None:
    assert decode_text(data) == TRADITIONAL


def test_simplified_chinese_from_gbk_is_not_read_as_big5() -> None:
    assert decode_text(SIMPLIFIED.encode("gb18030")) == SIMPLIFIED


def test_a_bom_does_not_end_up_in_the_text() -> None:
    assert decode_text("https://youtu.be/jNQXAC9IVRw".encode("utf-8-sig")).startswith("https://")


def test_undecodable_bytes_are_replaced_instead_of_failing() -> None:
    assert decode_text(b"ok \xff\xff\xff") == "ok ���"


def test_reads_a_file_from_disk(tmp_path: Path) -> None:
    path = tmp_path / "list.txt"
    path.write_bytes(TRADITIONAL.encode("cp950"))
    assert read_text_file(path) == TRADITIONAL


# About 10 MB, which took 11 seconds to guess when every character was scored.
LARGE_REPEATS = 10 * 1024 * 1024 // len(TRADITIONAL.encode("cp950"))


@pytest.mark.parametrize(("text", "encoding"), [(TRADITIONAL, "cp950"), (SIMPLIFIED, "gb18030")], ids=["big5", "gbk"])
def test_a_large_legacy_file_is_guessed_quickly(text: str, encoding: str) -> None:
    expected = text * LARGE_REPEATS
    data = expected.encode(encoding)
    started = time.perf_counter()
    decoded = decode_text(data)
    elapsed = time.perf_counter() - started
    assert decoded == expected
    # About 0.1 s here, against 5 s when the whole text was scored: generous for a busy machine.
    assert elapsed < 2, elapsed


@pytest.mark.parametrize(("text", "encoding"), [(TRADITIONAL, "cp950"), (SIMPLIFIED, "gb18030")], ids=["big5", "gbk"])
def test_a_character_split_where_the_guess_stops_reading_does_not_change_it(text: str, encoding: str) -> None:
    # Only two-byte characters, nothing to cut between, and once with an ASCII letter first: one of the two puts the
    # cut inside a character whatever even sample size is used.
    chinese = "".join(char for char in text if not char.isascii())
    for prefix in ("", "x"):
        expected = prefix + chinese * 3000
        assert decode_text(expected.encode(encoding)) == expected


def test_chinese_after_a_long_ascii_start_is_still_guessed() -> None:
    # A list of links with titles only further down: the first part says nothing about the encoding.
    expected = "https://www.youtube.com/watch?v=jNQXAC9IVRw\n" * 5000 + SIMPLIFIED
    assert decode_text(expected.encode("gb18030")) == expected


def test_a_file_that_is_not_big5_further_on_is_read_as_before() -> None:
    # The start reads as Big5, but a character Big5 lacks (an emoji in GB18030) comes after the part that is scored.
    data = (TRADITIONAL * 5000).encode("cp950") + "😀".encode("gb18030")
    assert decode_text(data) == data.decode("gb18030")

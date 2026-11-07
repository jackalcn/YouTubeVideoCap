from __future__ import annotations

from pathlib import Path

from videocap.subtitles import cues_to_srt, cues_to_text, cues_to_vtt, parse_subtitles, write_transcript


SRT = """1
00:00:01,000 --> 00:00:03,000
Hello <b>world</b>

2
00:00:03,500 --> 00:00:05,000
second &amp; line
continues here
"""

ROLLING_VTT = """WEBVTT
Kind: captions
Language: en

00:00:00.160 --> 00:00:02.510 align:start position:0%

so<00:00:00.480><c> this</c><00:00:00.800><c> is</c>

00:00:02.510 --> 00:00:02.520 align:start position:0%
so this is


00:00:02.520 --> 00:00:04.870 align:start position:0%
so this is
a<00:00:02.800><c> rolling</c><00:00:03.200><c> caption</c>

00:00:04.870 --> 00:00:04.880 align:start position:0%
a rolling caption


00:00:10.000 --> 00:00:11.000 align:start position:0%
after a pause
"""

CHINESE = """1
00:00:00,000 --> 00:00:01,000
大家好

2
00:00:01,000 --> 00:00:02,000
今天天氣很好
"""


def test_parse_srt() -> None:
    cues = parse_subtitles(SRT)
    assert [cue.lines for cue in cues] == [("Hello world",), ("second & line", "continues here")]
    assert cues[0].start == 1.0
    assert cues[1].end == 5.0


def test_parse_handles_windows_newlines_and_bom() -> None:
    cues = parse_subtitles(chr(0xFEFF) + SRT.replace("\n", "\r\n"))
    assert len(cues) == 2


def test_rolling_auto_captions_become_clean_text() -> None:
    assert cues_to_text(parse_subtitles(ROLLING_VTT)) == "so this is a rolling caption\n\nafter a pause\n"


def test_webvtt_cue_names_and_comments_are_not_text() -> None:
    content = """WEBVTT

NOTE made by hand
across two lines

intro
00:01.000 --> 00:02.000
Hello

00:03.000 --> 00:04.000
Bye
"""
    assert [cue.text for cue in parse_subtitles(content)] == ["Hello", "Bye"]


def test_cjk_lines_join_without_spaces() -> None:
    assert cues_to_text(parse_subtitles(CHINESE)) == "大家好今天天氣很好\n"


def test_timestamps_mark_each_paragraph() -> None:
    text = cues_to_text(parse_subtitles(ROLLING_VTT), with_timestamps=True)
    assert text.splitlines()[0].startswith("[00:00] ")
    assert "[00:10] after a pause" in text


def test_format_conversion_round_trips() -> None:
    cues = parse_subtitles(SRT)
    assert parse_subtitles(cues_to_vtt(cues)) == cues
    assert parse_subtitles(cues_to_srt(cues)) == cues
    assert cues_to_srt(cues).splitlines()[1] == "00:00:01,000 --> 00:00:03,000"
    assert cues_to_vtt(cues).startswith("WEBVTT\n\n00:00:01.000 --> 00:00:03.000")


def test_empty_input() -> None:
    assert parse_subtitles("") == []
    assert cues_to_text([]) == ""
    assert cues_to_srt([]) == ""


def test_write_transcript(tmp_path: Path) -> None:
    subtitle = tmp_path / "clip.en.srt"
    subtitle.write_text(SRT, encoding="utf-8")
    transcript = write_transcript(subtitle)
    assert transcript == tmp_path / "clip.en.txt"
    assert "Hello world" in transcript.read_text(encoding="utf-8")


def test_empty_track_writes_nothing(tmp_path: Path) -> None:
    subtitle = tmp_path / "empty.srt"
    subtitle.write_text("", encoding="utf-8")
    assert write_transcript(subtitle) is None
    assert not (tmp_path / "empty.txt").exists()


def timed(function, *args) -> float:
    import time

    started = time.perf_counter()
    function(*args)
    return time.perf_counter() - started


def test_hostile_files_parse_in_linear_time() -> None:
    # The converter is open to anyone, and these took minutes when the regexes retried every position of a line.
    cue = "1\n00:00:01,000 --> 00:00:02,000\n"
    for content in ("1" * 200_000, cue + "<" * 200_000, cue + "{\\" * 100_000, cue + "<c." * 70_000, "00:00:01.000 " * 20_000 + "-->"):
        assert timed(parse_subtitles, content) < 1.0


def test_repeated_lines_in_ordinary_subtitles_are_kept() -> None:
    content = """1
00:00:01,000 --> 00:00:02,000
No.

2
00:00:02,500 --> 00:00:03,500
No.

3
00:00:04,000 --> 00:00:05,000
Let it go

4
00:00:05,000 --> 00:00:06,000
Let it go
"""
    assert cues_to_text(parse_subtitles(content)) == "No. No. Let it go Let it go\n"


def test_korean_keeps_its_spaces_and_chinese_lines_join_without_one() -> None:
    korean = "1\n00:00:00,000 --> 00:00:01,000\n안녕하세요 여러분\n\n2\n00:00:01,000 --> 00:00:02,000\n오늘은 날씨가\n"
    assert cues_to_text(parse_subtitles(korean)) == "안녕하세요 여러분 오늘은 날씨가\n"
    chinese = "1\n00:00:00,000 --> 00:00:01,000\n我們今天要介紹\n一個新的功能\n\n2\n00:00:01,000 --> 00:00:02,000\n請大家注意\n"
    cues = parse_subtitles(chinese)
    assert cues[0].text == "我們今天要介紹一個新的功能"
    assert cues_to_text(cues) == "我們今天要介紹一個新的功能請大家注意\n"


def test_srt_quirks() -> None:
    no_blank_lines = "1\n00:00:01,000 --> 00:00:02,000\nHello\n2\n00:00:02,000 --> 00:00:03,000\nWorld\n3\n00:00:03,000 --> 00:00:04,000\nAgain\n"
    assert [cue.text for cue in parse_subtitles(no_blank_lines)] == ["Hello", "World", "Again"]
    styled = "1\n00:00:01,000 --> 00:00:02,000\n{\\an8}Top line\nif x < 5 and y > 3 then\n<font color=\"#fff\">white</font> <i>it</i>\n"
    assert parse_subtitles(styled)[0].lines == ("Top line", "if x < 5 and y > 3 then", "white it")


def test_vtt_output_escapes_markup_characters() -> None:
    cues = parse_subtitles("1\n00:00:01,000 --> 00:00:02,000\nif x &lt; 5 &amp;&amp; y\n")
    vtt = cues_to_vtt(cues)
    assert "if x &lt; 5 &amp;&amp; y" in vtt
    assert parse_subtitles(vtt) == cues
    assert "if x < 5 && y" in cues_to_srt(cues)


def rolling_captions(minutes: int) -> str:
    """Unpunctuated, continuous auto captions in YouTube's rolling layout."""
    blocks = ["WEBVTT\nKind: captions\nLanguage: en\n"]
    previous = ""
    for second in range(minutes * 60):
        line = f"word{second} and some more speech here"
        start, middle = second, second + 0.99
        blocks.append(f"{format_vtt(start)} --> {format_vtt(middle)}\n{previous}\n{line}\n")
        blocks.append(f"{format_vtt(middle)} --> {format_vtt(middle + 0.01)}\n{line}\n")
        previous = line
    return "\n".join(blocks)


def format_vtt(seconds: float) -> str:
    from videocap.subtitles import format_vtt_time

    return format_vtt_time(seconds)


def test_long_unpunctuated_captions_are_split_quickly() -> None:
    cues = parse_subtitles(rolling_captions(180))
    paragraphs = cues_to_text(cues, with_timestamps=True).split("\n\n")
    assert len(paragraphs) > 100
    assert max(len(paragraph) for paragraph in paragraphs) < 2000
    assert "word0 and some more speech here word1 " in paragraphs[0]
    assert timed(cues_to_text, cues) < 2.0


def test_search_finds_matches_anywhere_with_their_time() -> None:
    from videocap.subtitles import search_cues

    cues = parse_subtitles(rolling_captions(180))
    assert search_cues(cues, "word99999") == []
    assert search_cues(cues, "WORD9000 and") == ["[2:30:00] word9000 and some more speech here"]
    assert search_cues(cues, "  ") == []


def test_legacy_encodings_are_read(tmp_path: Path) -> None:
    from videocap.subtitles import read_subtitle_file

    content = "1\n00:00:01,000 --> 00:00:02,000\n大家好，歡迎收看\n"
    for encoding in ("cp950", "utf-16", "utf-8-sig"):
        path = tmp_path / f"{encoding}.srt"
        path.write_bytes(content.encode(encoding))
        assert read_subtitle_file(path)[0].text == "大家好，歡迎收看"


def srt(*cues: tuple[float, float, str]) -> str:
    from videocap.subtitles import Cue

    return cues_to_srt([Cue(start, end, tuple(text.split("|"))) for start, end, text in cues])


def merged_lines(top: str, bottom: str) -> list[tuple[float, float, tuple[str, ...]]]:
    from videocap.subtitles import merge_bilingual

    return [(cue.start, cue.end, cue.lines) for cue in merge_bilingual(parse_subtitles(top), parse_subtitles(bottom))]


def test_bilingual_pairs_cues_that_share_their_time() -> None:
    chinese = srt((1, 3, "大家好"), (3.5, 5, "今天天氣很好"))
    # Slightly different timing, as tracks made by different people have.
    english = srt((1.1, 3.2, "Hello everyone"), (3.4, 5.1, "Nice weather today|isn't it"))
    assert merged_lines(chinese, english) == [
        (1.0, 3.0, ("大家好", "Hello everyone")),
        (3.5, 5.0, ("今天天氣很好", "Nice weather today", "isn't it")),
    ]


def test_bilingual_keeps_cues_without_a_partner() -> None:
    chinese = srt((1, 2, "大家好"), (10, 12, "只有中文"))
    english = srt((1, 2, "Hello"), (5, 6, "English only"))
    assert merged_lines(chinese, english) == [
        (1.0, 2.0, ("大家好", "Hello")),
        (5.0, 6.0, ("English only",)),
        (10.0, 12.0, ("只有中文",)),
    ]
    assert merged_lines(chinese, "") == [(1.0, 2.0, ("大家好",)), (10.0, 12.0, ("只有中文",))]
    assert merged_lines("", english) == [(1.0, 2.0, ("Hello",)), (5.0, 6.0, ("English only",))]


def test_bilingual_joins_short_cues_under_a_long_one_and_picks_the_best_overlap() -> None:
    chinese = srt((0, 4, "這是一句很長的中文"), (4, 6, "下一句"))
    english = srt((0, 1.5, "This is"), (1.5, 3, "a long"), (3, 4.5, "sentence"), (4.6, 6, "Next one"))
    assert merged_lines(chinese, english) == [
        (0.0, 4.0, ("這是一句很長的中文", "This is a long sentence")),
        (4.0, 6.0, ("下一句", "Next one")),
    ]


def test_bilingual_still_pairs_after_a_long_cue() -> None:
    # A caption that stays on screen a long time, such as a title, must not stop the pairing of everything after it.
    top = srt((0, 100, "標題"), *((second, second + 0.9, f"第{second}句") for second in range(1, 60)))
    bottom = srt(*((second, second + 0.9, f"line {second}") for second in range(1, 60)))
    lines = merged_lines(top, bottom)
    assert (50.0, 50.9, ("第50句", "line 50")) in lines
    assert all(len(cue[2]) == 2 or cue[2] == ("標題",) for cue in lines)


def test_bilingual_removes_rolling_repeats_and_round_trips_through_vtt() -> None:
    from videocap.subtitles import merge_bilingual

    cues = merge_bilingual(parse_subtitles(ROLLING_VTT), parse_subtitles(CHINESE))
    # Both Chinese cues fall within the first caption, and join without a space.
    assert cues[0].lines == ("so this is", "大家好今天天氣很好")
    assert parse_subtitles(cues_to_vtt(cues)) == cues


def test_bilingual_merge_is_fast_on_hostile_files() -> None:
    from videocap.subtitles import merge_bilingual

    # Every cue overlaps every other one, which a naive pairing would compare one by one.
    overlapping = parse_subtitles(srt(*((index / 1000, 100_000, f"x{index}") for index in range(20_000))))
    assert timed(merge_bilingual, overlapping, overlapping) < 2.0
    assert len(merge_bilingual(overlapping, overlapping)) == 20_000


def assert_valid_srt(text: str) -> None:
    import re

    assert "\r" not in text and text.endswith("\n") and not text.endswith("\n\n")
    for number, block in enumerate(text[:-1].split("\n\n"), start=1):
        lines = block.split("\n")
        assert lines[0] == str(number)
        assert re.fullmatch(r"\d{2,}:\d{2}:\d{2},\d{3} --> \d{2,}:\d{2}:\d{2},\d{3}", lines[1]), lines[1]
        assert len(lines) > 2 and all(line.strip() for line in lines[2:]), block


def assert_valid_vtt(text: str) -> None:
    import re

    assert "\r" not in text and text.startswith("WEBVTT\n\n") and text.endswith("\n") and not text.endswith("\n\n")
    for block in text[len("WEBVTT\n\n") : -1].split("\n\n"):
        lines = block.split("\n")
        assert re.fullmatch(r"\d{2,}:\d{2}:\d{2}\.\d{3} --> \d{2,}:\d{2}:\d{2}\.\d{3}", lines[0]), lines[0]
        # A payload line holding "-->" would be read as the next cue's timing.
        assert len(lines) > 1 and all(line.strip() and "-->" not in line for line in lines[1:]), block


DECORATED_VTT = """\ufeffWEBVTT - 我的字幕
Kind: captions
Language: zh-TW

STYLE
::cue {
  color: yellow;
}

NOTE 這是註解
跨兩行，也不是字幕

REGION
id:fred
width:40%

intro
00:00:01.000 --> 00:00:03.000 align:start position:10% line:0
<v Roger>第一句 & <b>粗體</b>

2
00:00:02.500 --> 00:00:06.000 size:50%
重疊的一句
""".replace("\n", "\r\n")

# Out of order, with markup characters, and one cue on top of another.
UNORDERED_SRT = (
    "2\n00:00:05,000 --> 00:00:06,000\nlater\n\n"
    "1\n00:00:01,000 --> 00:00:03,000\nfirst a --> b <i>x</i> < 5\n\n"
    "3\n00:00:01,500 --> 00:00:02,000\nover the top\n"
)


def merged_outputs(top: str, bottom: str) -> tuple[str, str]:
    from videocap.ui.page_tools import merge_subtitle_texts

    srt_text, vtt_text = merge_subtitle_texts(top, bottom, "srt"), merge_subtitle_texts(top, bottom, "vtt")
    if srt_text:
        assert_valid_srt(srt_text)
        assert_valid_vtt(vtt_text)
        assert parse_subtitles(srt_text) == parse_subtitles(vtt_text)
    return srt_text, vtt_text


def test_bilingual_edge_cases_give_valid_files() -> None:
    from videocap.textfiles import decode_text

    for top, bottom in (("", ""), ("WEBVTT\r\n\r\n", "WEBVTT\n\nNOTE nothing here\n"), ("\ufeff", "1\n\n2\n")):
        assert merged_outputs(top, bottom) == ("", "")

    decorated = decode_text(DECORATED_VTT.encode("utf-8-sig"))
    srt_text, _ = merged_outputs(decorated, UNORDERED_SRT)
    assert [cue.lines for cue in parse_subtitles(srt_text)] == [
        ("第一句 & 粗體", "first a --> b x < 5 over the top"),
        ("重疊的一句", "later"),
    ]
    assert "STYLE" not in srt_text and "註解" not in srt_text and "fred" not in srt_text and "align:" not in srt_text
    # Only one side has cues: the other is kept as it is.
    assert [cue.lines for cue in parse_subtitles(merged_outputs("WEBVTT\n", UNORDERED_SRT)[0])] == [
        ("first a --> b x < 5",), ("over the top",), ("later",)
    ]
    # The same file twice gives each line twice, still a valid file.
    srt_text, _ = merged_outputs(UNORDERED_SRT, UNORDERED_SRT)
    assert [cue.lines for cue in parse_subtitles(srt_text)][0] == ("first a --> b x < 5", "first a --> b x < 5")


def tiny_cues(megabytes: float) -> str:
    from videocap.subtitles import format_srt_time

    blocks: list[str] = []
    size = 0
    while size < megabytes * 1024 * 1024:
        index = len(blocks)
        block = f"{index + 1}\n{format_srt_time(index / 100)} --> {format_srt_time(index / 100 + 0.01)}\n中{index % 10}\n\n"
        blocks.append(block)
        size += len(block.encode("utf-8"))
    return "".join(blocks)


def test_bilingual_merge_of_two_full_size_uploads_is_fast() -> None:
    from videocap.ui.page_tools import merge_subtitle_texts

    # The tools page allows 2 MB per file, and reruns the merge whenever a widget changes.
    content = tiny_cues(2)
    assert timed(merge_subtitle_texts, content, content, "vtt", True) < 6.0
    srt_text, vtt_text = merged_outputs(content, tiny_cues(1.9))
    assert len(parse_subtitles(srt_text)) > 40_000


def test_cues_are_cut_to_a_clip_and_timed_from_its_start() -> None:
    from videocap.subtitles import Cue, cut_cues

    cues = [Cue(1.0, 2.0, ("before",)), Cue(4.0, 6.0, ("across the start",)), Cue(7.0, 8.0, ("inside",)), Cue(9.5, 11.0, ("across the end",)), Cue(12.0, 13.0, ("after",))]
    assert cut_cues(cues, 5.0, 10.0) == [Cue(0.0, 1.0, ("across the start",)), Cue(2.0, 3.0, ("inside",)), Cue(4.5, 5.0, ("across the end",))]
    # A clip with no end runs to the end of the video.
    assert cut_cues(cues, 5.0, None)[-1] == Cue(7.0, 8.0, ("after",))
    assert cut_cues(cues, 20.0, 30.0) == []

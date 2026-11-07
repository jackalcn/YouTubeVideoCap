from __future__ import annotations

import html
import re
from dataclasses import dataclass
from pathlib import Path

from videocap.textfiles import read_text_file


# Matched only at the start of a short line, with possessive groups: the converter is open to anyone, and a regex
# that retries every position of a long line holds the GIL for minutes.
TIMING_RE = re.compile(r"(?P<start>(?:\d++:)?\d{1,2}:\d{2}[.,]\d{1,3})\s*+-->\s*+(?P<end>(?:\d++:)?\d{1,2}:\d{2}[.,]\d{1,3})")
MAX_TIMING_LINE_CHARS = 300
# Karaoke-style timestamps and the styling tags of SRT and WebVTT. Only known tags, so text such as "x < 5 and y > 3"
# survives, and [^<>] stops each attempt at the next bracket so a run of "<" stays linear.
INLINE_TAG_RE = re.compile(r"<(?:/?(?:[bius]|c|v|lang|ruby|rt|font)(?:[.\s][^<>]*)?|\d{1,2}(?::\d{2}){1,2}[.,]\d{3})>", re.IGNORECASE)
# ASS override blocks such as {\an8} that some SRT files carry.
OVERRIDE_BLOCK_RE = re.compile(r"\{\\[^{}]*\}")
VTT_BLOCK_PREFIXES = ("NOTE", "STYLE", "REGION")
BYTE_ORDER_MARK = chr(0xFEFF)
ZERO_WIDTH_SPACE = chr(0x200B)
SENTENCE_ENDS = ".!?。！？…"
PARAGRAPH_GAP_SECONDS = 4.0
# A broken or hostile file can claim any time at all; nothing real is longer than this.
MAX_CUE_SECONDS = 1_000_000
PARAGRAPH_MAX_CHARS = 600
# Auto captions have no punctuation, so without these limits a whole video becomes one paragraph.
PARAGRAPH_HARD_MAX_CHARS = 1500
PARAGRAPH_MAX_SECONDS = 60.0
# YouTube's rolling captions hold the old line on screen for a 10 ms cue before the next one scrolls in.
ROLLING_TRANSITION_SECONDS = 0.05
READABLE_SUBTITLE_EXTENSIONS = frozenset({".srt", ".vtt"})
# How many top cues a bilingual merge compares each bottom cue with. Real tracks overlap one or two at a time.
BILINGUAL_CANDIDATES = 8
# Han, kana, bopomofo, CJK punctuation and full-width forms. Korean is left out because it separates words with spaces.
CJK_RANGES = (
    (0x2E80, 0x2FDF),
    (0x3000, 0x30FF),
    (0x3100, 0x312F),
    (0x31A0, 0x31FF),
    (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF),
    (0xF900, 0xFAFF),
    (0xFE30, 0xFE4F),
    (0xFF00, 0xFFEF),
    (0x20000, 0x3134F),
)


@dataclass(frozen=True)
class Cue:
    start: float
    end: float
    lines: tuple[str, ...]

    @property
    def text(self) -> str:
        return join_text(self.lines)


def is_cjk(char: str) -> bool:
    code = ord(char)
    return any(low <= code <= high for low, high in CJK_RANGES)


def needs_cjk_join(left: str, right: str) -> bool:
    return bool(left) and bool(right) and is_cjk(left[-1]) and is_cjk(right[0])


def join_text(parts: tuple[str, ...] | list[str]) -> str:
    """Joins pieces of text with spaces, except between Chinese or Japanese characters, which use none."""
    pieces: list[str] = []
    for part in parts:
        if pieces and not needs_cjk_join(pieces[-1], part):
            pieces.append(" ")
        pieces.append(part)
    return "".join(pieces)


def parse_cue_time(text: str) -> float:
    seconds = 0.0
    for part in text.replace(",", ".").split(":"):
        seconds = seconds * 60 + float(part)
    return seconds


def format_srt_time(seconds: float) -> str:
    return format_cue_time(seconds, ",")


def format_vtt_time(seconds: float) -> str:
    return format_cue_time(seconds, ".")


def format_cue_time(seconds: float, separator: str) -> str:
    total_ms = round(seconds * 1000)
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{separator}{millis:03d}"


def clean_cue_line(line: str) -> str:
    without_tags = OVERRIDE_BLOCK_RE.sub("", INLINE_TAG_RE.sub("", line))
    return html.unescape(without_tags).replace(ZERO_WIDTH_SPACE, "").strip()


def match_timing(line: str) -> re.Match[str] | None:
    stripped = line.strip()
    if len(stripped) > MAX_TIMING_LINE_CHARS or "-->" not in stripped:
        return None
    return TIMING_RE.match(stripped)


def is_cue_number(line: str) -> bool:
    stripped = line.strip()
    return stripped.isascii() and stripped.isdigit()


def parse_subtitles(content: str) -> list[Cue]:
    """Reads SRT or WebVTT line by line, because YouTube's auto captions put blank lines inside a cue."""
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.lstrip(BYTE_ORDER_MARK).split("\n")
    cues: list[Cue] = []
    start, end = 0.0, 0.0
    text_lines: list[str] = []
    in_cue = False
    in_comment = False

    def flush() -> None:
        cleaned = tuple(text for text in map(clean_cue_line, text_lines) if text)
        if in_cue and cleaned:
            cues.append(Cue(start, end, cleaned))

    for index, line in enumerate(lines):
        match = match_timing(line)
        if match:
            # The line before a timing line is the cue's number, or its name when a blank line comes before it.
            # Some SRT files leave out the blank lines, so a lone number counts even without one.
            if text_lines and text_lines[-1].strip() and (
                is_cue_number(text_lines[-1]) or (index >= 2 and not lines[index - 2].strip())
            ):
                text_lines.pop()
            flush()
            start, end = parse_cue_time(match.group("start")), parse_cue_time(match.group("end"))
            if not (0 <= start <= MAX_CUE_SECONDS and 0 <= end <= MAX_CUE_SECONDS):
                in_cue = False
                text_lines = []
                continue
            text_lines = []
            in_cue = True
            in_comment = False
            continue

        stripped = line.strip()
        previous_blank = index == 0 or not lines[index - 1].strip()
        if previous_blank and stripped.startswith(VTT_BLOCK_PREFIXES):
            in_comment = True
        elif not stripped:
            in_comment = False
        if in_cue and not in_comment:
            text_lines.append(line)

    flush()
    return cues


def cut_cues(cues: list[Cue], start: float, end: float | None) -> list[Cue]:
    """The cues inside a clip, timed from the clip's start, so a clip's subtitle file lines up with the clip."""
    kept: list[Cue] = []
    for cue in cues:
        cue_end = cue.end if end is None else min(cue.end, end)
        if cue_end <= start or (end is not None and cue.start >= end):
            continue
        kept.append(Cue(max(cue.start - start, 0.0), cue_end - start, cue.lines))
    return kept


def remove_rolling_repeats(cues: list[Cue]) -> list[Cue]:
    """YouTube's auto captions scroll: each cue repeats the previous line on top and adds a new one, so keep only what
    is new. Ordinary subtitles can repeat a whole line on purpose ("No." / "No."), and those stay."""
    deduped: list[Cue] = []
    previous_lines: frozenset[str] = frozenset()
    for cue in cues:
        repeated = 0
        while repeated < len(cue.lines) and cue.lines[repeated] in previous_lines:
            repeated += 1
        previous_lines = frozenset(cue.lines)
        if repeated < len(cue.lines):
            deduped.append(Cue(cue.start, cue.end, cue.lines[repeated:]))
        elif cue.end - cue.start > ROLLING_TRANSITION_SECONDS:
            deduped.append(cue)
    return deduped


def cues_to_text(cues: list[Cue], with_timestamps: bool = False) -> str:
    """Joins captions into paragraphs, starting a new one at a long pause so the text stays readable."""
    paragraphs = cues_to_paragraphs(cues, with_timestamps)
    return "\n\n".join(paragraphs) + ("\n" if paragraphs else "")


def cues_to_paragraphs(cues: list[Cue], with_timestamps: bool = False) -> list[str]:
    paragraphs: list[str] = []
    current: list[str] = []
    length = 0
    current_start = 0.0
    last_end = 0.0

    def flush() -> None:
        if current:
            prefix = f"[{format_timestamp_short(current_start)}] " if with_timestamps else ""
            paragraphs.append(prefix + join_text(current))

    for cue in remove_rolling_repeats(cues):
        text = cue.text
        if current:
            gap = cue.start - last_end
            at_sentence_end = current[-1][-1:] in SENTENCE_ENDS
            too_long = (length >= PARAGRAPH_MAX_CHARS and at_sentence_end) or length >= PARAGRAPH_HARD_MAX_CHARS
            if gap >= PARAGRAPH_GAP_SECONDS or too_long or cue.start - current_start >= PARAGRAPH_MAX_SECONDS:
                flush()
                current, length = [], 0
        if not current:
            current_start = cue.start
        current.append(text)
        length += len(text) + 1
        last_end = cue.end
    flush()
    return paragraphs


def search_cues(cues: list[Cue], keyword: str) -> list[str]:
    """Each caption that holds the keyword, with its time, so a match deep in a long video is still shown."""
    needle = keyword.strip().lower()
    if not needle:
        return []
    return [f"[{format_timestamp_short(cue.start)}] {cue.text}" for cue in remove_rolling_repeats(cues) if needle in cue.text.lower()]


def format_timestamp_short(seconds: float) -> str:
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def cues_to_srt(cues: list[Cue]) -> str:
    blocks = [
        f"{index}\n{format_srt_time(cue.start)} --> {format_srt_time(cue.end)}\n" + "\n".join(cue.lines)
        for index, cue in enumerate(cues, start=1)
    ]
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def escape_vtt_text(line: str) -> str:
    # In WebVTT "<" starts a tag and "&" an entity, so a player would swallow the text after them.
    return line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def cues_to_vtt(cues: list[Cue]) -> str:
    blocks = [
        f"{format_vtt_time(cue.start)} --> {format_vtt_time(cue.end)}\n" + "\n".join(map(escape_vtt_text, cue.lines))
        for cue in cues
    ]
    return "WEBVTT\n\n" + "\n\n".join(blocks) + ("\n" if blocks else "")


def timing_match(left: Cue, right: Cue) -> float:
    """The share of their combined time that two cues are on screen together: 1 for the same timing, 0 for none. A
    long title card overlaps a short line as much as that line's own partner does, but matches it far less."""
    shared = min(left.end, right.end) - max(left.start, right.start)
    return shared / (max(left.end, right.end) - min(left.start, right.start)) if shared > 0 else 0.0


def merge_bilingual(top: list[Cue], bottom: list[Cue]) -> list[Cue]:
    """One track with both languages: each bottom cue joins the top cue whose timing matches best, under its lines
    and on its timing. Cues with no partner are kept on their own, so nothing said is lost.

    Linear after sorting: the bottom cues are walked in time order against the few top cues still on screen, and that
    set is capped, so a hostile file of thousands of overlapping cues cannot make it quadratic."""
    top = sorted(remove_rolling_repeats(top), key=lambda cue: (cue.start, cue.end))
    bottom = sorted(remove_rolling_repeats(bottom), key=lambda cue: (cue.start, cue.end))
    partners: list[list[Cue]] = [[] for _ in top]
    alone: list[Cue] = []
    # Indexes of the top cues that may still overlap, and of the next top cue not yet looked at.
    showing: list[int] = []
    upcoming = 0
    for cue in bottom:
        while upcoming < len(top) and top[upcoming].start < cue.end:
            showing.append(upcoming)
            upcoming += 1
        showing = [index for index in showing if top[index].end > cue.start][-BILINGUAL_CANDIDATES:]
        best, best_match = -1, 0.0
        for index in showing:
            match = timing_match(top[index], cue)
            if match > best_match:
                best, best_match = index, match
        if best < 0:
            alone.append(cue)
        else:
            partners[best].append(cue)

    merged = alone
    for cue, matched in zip(top, partners):
        if len(matched) == 1:
            lines = matched[0].lines
        else:
            # Several short captions under one long one read better as a single line.
            lines = (join_text([partner.text for partner in matched]),) if matched else ()
        merged.append(Cue(cue.start, cue.end, (*cue.lines, *lines)))
    return sorted(merged, key=lambda cue: (cue.start, cue.end))


def read_subtitle_file(path: Path) -> list[Cue]:
    return parse_subtitles(read_text_file(path))


def write_transcript(subtitle_path: Path) -> Path | None:
    """Saves a subtitle file as plain text next to it. Returns None when the track holds no text."""
    text = cues_to_text(read_subtitle_file(subtitle_path))
    if not text:
        return None
    transcript_path = subtitle_path.with_suffix(".txt")
    transcript_path.write_text(text, encoding="utf-8")
    return transcript_path

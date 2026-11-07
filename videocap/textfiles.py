"""Reads text files people bring from elsewhere, whatever encoding their editor saved them in."""

from __future__ import annotations

import codecs
import re
from pathlib import Path

BOMS = (
    (b"\xff\xfe\x00\x00", "utf-32"),
    (b"\x00\x00\xfe\xff", "utf-32"),
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xff\xfe", "utf-16"),
    (b"\xfe\xff", "utf-16"),
)
# Taiwanese tools still save subtitles and link lists as Big5; mainland ones as GBK.
LEGACY_ENCODINGS = ("cp950", "gb18030")
SCORE_SAMPLE_BYTES = 64 * 1024
NON_ASCII_RE = re.compile(rb"[\x80-\xff]")


def decode_text(data: bytes) -> str:
    for bom, encoding in BOMS:
        if data.startswith(bom):
            return data.decode(encoding, "replace")
    utf16 = guess_bomless_utf16(data)
    if utf16:
        return data.decode(utf16, "replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    # Scoring every character of a large file takes seconds, and the start tells as well as the whole. It starts at the
    # first non-ASCII byte, so a long ASCII header is not all it sees.
    first = NON_ASCII_RE.search(data)
    start = first.start() if first else 0
    sample = data[start : start + SCORE_SAMPLE_BYTES]
    whole = start + len(sample) == len(data)
    candidates = []
    for encoding in LEGACY_ENCODINGS:
        try:
            # Not final unless the sample is the whole file: a character cut in two at its end is held back, not refused.
            text = codecs.getincrementaldecoder(encoding)().decode(sample, final=whole)
        except UnicodeDecodeError:
            continue
        candidates.append((count_rare_characters(text, encoding), encoding))
    # sorted() keeps the order of equals, so Big5 wins a tie. A later part may still not fit the best guess.
    for _, encoding in sorted(candidates, key=lambda candidate: candidate[0]):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def guess_bomless_utf16(data: bytes) -> str:
    """Notepad's "Unicode" without a BOM: every other byte of ASCII text is zero."""
    sample = data[: 4096 - 4096 % 2]
    if len(sample) < 4:
        return ""
    half = len(sample) // 2
    even_zeros = sample[0::2].count(0) / half
    odd_zeros = sample[1::2].count(0) / half
    if odd_zeros > 0.3 and even_zeros < 0.1:
        return "utf-16-le"
    if even_zeros > 0.3 and odd_zeros < 0.1:
        return "utf-16-be"
    return ""


def count_rare_characters(text: str, encoding: str) -> int:
    """Characters outside the everyday part of the encoding, which a wrong guess produces in bulk.

    GBK read as Big5 lands in Big5's rarely used second level; Big5 read as GBK lands in
    kana, Greek and extension areas instead of GB2312's common characters.
    """
    rare = 0
    for char in text:
        if ord(char) < 0x80:
            continue
        lead, trail = (char.encode(encoding) + b"\x00")[:2]
        if encoding == "cp950":
            common = 0xA1 <= lead <= 0xC6
        else:
            common = (0xB0 <= lead <= 0xD7 or lead in (0xA1, 0xA3)) and trail >= 0xA1
        rare += not common
    return rare


def read_text_file(path: Path) -> str:
    return decode_text(path.read_bytes())

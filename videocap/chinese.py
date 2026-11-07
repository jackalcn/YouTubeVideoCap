"""Simplified to Taiwan Traditional Chinese, for subtitles and transcripts that YouTube only has in Simplified.

The API the rest of the app uses:
- to_traditional(text): converts text with Taiwanese wording (OpenCC s2twp, so 软件 becomes 軟體).
- chinese_script(language), is_simplified(language), is_traditional(language): tell subtitle language codes apart.
- write_traditional_copy(path): saves a Simplified subtitle file as "<name>.zh-TW.<ext>" next to it.
- ChineseConversionUnavailable: raised with a zh-TW message when OpenCC is not installed.
"""

from __future__ import annotations

import re
import threading
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from videocap.textfiles import read_text_file


# Simplified to Traditional with Taiwanese phrases: 软件 → 軟體, 视频 → 影片, 鼠标 → 滑鼠.
OPENCC_CONFIG = "s2twp"
TRADITIONAL_LANGUAGE = "zh-TW"
SIMPLIFIED = "simplified"
TRADITIONAL = "traditional"
CHINESE_LANGUAGES = frozenset({"zh", "cmn"})
SIMPLIFIED_SCRIPT = "hans"
TRADITIONAL_SCRIPT = "hant"
SIMPLIFIED_REGIONS = frozenset({"cn", "sg", "my"})
TRADITIONAL_REGIONS = frozenset({"tw", "hk", "mo"})
# yt-dlp marks the track a video was auto-captioned in, such as "zh-Hans-orig"; it says nothing about the script.
IGNORED_SUFFIXES = frozenset({"orig"})
# The language part of a subtitle name, such as "zh-Hans" in "clip.zh-Hans.srt".
LANGUAGE_PART_RE = re.compile(r"^[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{2,8})*$")
UNAVAILABLE_MESSAGE = "缺少簡體轉繁體的元件 OpenCC。請關閉 App 後重新執行 start.bat，它會自動安裝；或在終端機執行 pip install opencc 後重新啟動。"

_convert_lock = threading.Lock()


class ChineseConversionUnavailable(RuntimeError):
    def __init__(self) -> None:
        super().__init__(UNAVAILABLE_MESSAGE)


@lru_cache(maxsize=1)
def _converter() -> Any:
    # Loading the dictionaries takes a tenth of a second, so it happens once, and only when first needed.
    try:
        import opencc
    except ImportError as exc:
        raise ChineseConversionUnavailable() from exc
    return opencc.OpenCC(OPENCC_CONFIG)


def is_available() -> bool:
    try:
        _converter()
    except (ChineseConversionUnavailable, RuntimeError, OSError):
        return False
    return True


def to_traditional(text: str) -> str:
    """Simplified to Taiwan Traditional Chinese with Taiwanese wording. Text that is already Traditional, and anything
    that is not Chinese, stays as it is. Raises ChineseConversionUnavailable when OpenCC is not installed."""
    if not text:
        return text
    converter = _converter()
    # One shared converter, and the C library does not promise it can be used by two threads at once.
    with _convert_lock:
        return converter.convert(text)


def chinese_script(language: str, plain_zh: str = "") -> str:
    """SIMPLIFIED or TRADITIONAL for a Chinese subtitle language code, or "" when the code does not say.

    zh-Hans, zh-CN and zh-SG are Simplified; zh-Hant, zh-TW and zh-HK are Traditional. A plain "zh" names no script,
    so it gets plain_zh: YouTube's own "zh" tracks are usually Simplified, but a caller that converts files should
    not guess."""
    parts = [part.lower() for part in re.split(r"[-_]", language.strip()) if part]
    parts = [part for part in parts if part not in IGNORED_SUFFIXES]
    if not parts or parts[0] not in CHINESE_LANGUAGES:
        return ""
    subtags = parts[1:]
    # A script subtag outranks the region: zh-Hant-CN is still Traditional.
    if SIMPLIFIED_SCRIPT in subtags:
        return SIMPLIFIED
    if TRADITIONAL_SCRIPT in subtags:
        return TRADITIONAL
    if SIMPLIFIED_REGIONS.intersection(subtags):
        return SIMPLIFIED
    if TRADITIONAL_REGIONS.intersection(subtags):
        return TRADITIONAL
    return plain_zh if not subtags else ""


def is_simplified(language: str, plain_zh: bool = False) -> bool:
    return chinese_script(language, SIMPLIFIED if plain_zh else "") == SIMPLIFIED


def is_traditional(language: str) -> bool:
    return chinese_script(language) == TRADITIONAL


def subtitle_language(path: Path) -> str:
    """The language part of "clip.zh-Hans.srt", or "" when the name has none."""
    _, dot, segment = path.stem.rpartition(".")
    return segment if dot and LANGUAGE_PART_RE.match(segment) else ""


def traditional_copy_path(path: Path) -> Path:
    """ "clip.zh-Hans.srt" becomes "clip.zh-TW.srt", and "clip.srt" becomes "clip.zh-TW.srt"."""
    stem = path.stem
    if subtitle_language(path):
        stem = stem.rpartition(".")[0]
    return path.with_name(f"{stem}.{TRADITIONAL_LANGUAGE}{path.suffix}")


def write_traditional_copy(path: Path, overwrite: bool = False) -> Optional[Path]:
    """Saves a Traditional copy of a subtitle or transcript file next to it, keeping the original, because phrase
    conversion now and then changes a name. Works on any text format, since only Chinese characters change.

    Returns None when a file with that name already exists, such as a real zh-TW track, and overwrite is False.
    Raises ChineseConversionUnavailable without OpenCC, and OSError when the file cannot be read or written."""
    target = traditional_copy_path(path)
    if target == path or (target.exists() and not overwrite):
        return None
    converted = to_traditional(read_text_file(path))
    # newline="" keeps the file's own line endings instead of doubling the carriage returns on Windows.
    target.write_text(converted, encoding="utf-8", newline="")
    return target

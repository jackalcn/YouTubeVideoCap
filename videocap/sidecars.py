"""Files the app writes beside a downloaded video: comments, a readable live chat, and media-server metadata."""

from __future__ import annotations

import csv
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree
from typing import IO, Any, Iterable, Iterator, Mapping, Optional, Sequence


# Titles, comments and chat come from strangers, and a spreadsheet would run a cell starting with these as a formula.
CSV_FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value: Any) -> Any:
    if isinstance(value, str) and value.startswith(CSV_FORMULA_STARTS):
        return f"'{value}"
    return value


def write_csv(handle: IO[str], rows: Iterable[Mapping[str, Any]], fieldnames: Optional[Sequence[str]] = None) -> None:
    """Writes the rows one at a time, so a long chat never has to fit in memory. Without fieldnames, the first row's
    keys are the columns, and no rows means no header either."""
    writer: Optional[csv.DictWriter[str]] = None
    if fieldnames is not None:
        writer = csv.DictWriter(handle, fieldnames=list(fieldnames))
        writer.writeheader()
    for row in rows:
        if writer is None:
            writer = csv.DictWriter(handle, fieldnames=list(row.keys()))
            writer.writeheader()
        writer.writerow({key: csv_safe(value) for key, value in row.items()})


def open_csv(path: Path) -> IO[str]:
    # Excel only reads UTF-8 CSV correctly when it starts with a byte order mark. YouTube's text can hold half of an
    # emoji, which UTF-8 cannot, so that half becomes "?" instead of losing the whole file.
    return path.open("w", encoding="utf-8-sig", errors="replace", newline="")


# YouTube hands yt-dlp the time of a comment as English words ("6 years ago", "23 hours ago (edited)").
RELATIVE_TIME_RE = re.compile(r"^(\d+)\s+(second|minute|hour|day|week|month|year)s?\s+ago(?P<edited>\s*\(edited\))?$", re.IGNORECASE)
RELATIVE_TIME_UNITS = {"second": "秒", "minute": "分鐘", "hour": "小時", "day": "天", "week": "週", "month": "個月", "year": "年"}


def comment_time(comment: Mapping[str, Any]) -> str:
    """The words YouTube gave, in Chinese. yt-dlp also guesses a timestamp from them, which is used only when there
    are no words at all, since a guessed date would look more exact than it is."""
    text = str(comment.get("_time_text") or comment.get("time_text") or "").strip()
    match = RELATIVE_TIME_RE.match(text)
    if match:
        edited = "（已編輯）" if match.group("edited") else ""
        return f"{match.group(1)} {RELATIVE_TIME_UNITS[match.group(2).lower()]}前{edited}"
    if text:
        return text
    timestamp = comment.get("timestamp")
    if isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool) and timestamp > 0:
        return datetime.fromtimestamp(timestamp, timezone.utc).astimezone().strftime("%Y-%m-%d")
    return ""


def comment_rows(information: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The columns of the comment export on the 下載 page, so both files open the same way."""
    video_id = information.get("id")
    return [
        {
            "作者": str(comment.get("author") or ""),
            "留言": str(comment.get("text") or ""),
            "按讚數": comment.get("like_count") or 0,
            "時間": comment_time(comment),
            "網址": f"https://www.youtube.com/watch?v={video_id}&lc={comment.get('id')}",
        }
        for comment in information.get("comments") or []
    ]


def write_comments_csv(path: Path, information: Mapping[str, Any]) -> Optional[Path]:
    """None when the video has no comments, or YouTube turned them off."""
    rows = comment_rows(information)
    if not rows:
        return None
    with open_csv(path) as handle:
        write_csv(handle, rows)
    return path


# yt-dlp saves a chat replay as YouTube's own JSON, one action per line. These are the kinds of message people wrote.
CHAT_MESSAGE_RENDERERS = (
    "liveChatTextMessageRenderer",
    "liveChatPaidMessageRenderer",
    "liveChatPaidStickerRenderer",
    "liveChatMembershipItemRenderer",
)


CHAT_COLUMNS = ("時間", "作者", "留言", "Super Chat 金額")


def live_chat_rows(path: Path) -> Iterator[dict[str, str]]:
    """One row at a time, as the file is read."""
    with path.open(encoding="utf-8", errors="replace") as lines:
        for line in lines:
            try:
                rows = list(chat_rows_of(json.loads(line)))
            except (ValueError, TypeError, AttributeError, KeyError, IndexError):
                # A line cut short by an interrupted download, or a shape YouTube has not used before.
                continue
            yield from rows


def chat_rows_of(action: Any) -> Iterator[dict[str, str]]:
    replay = action.get("replayChatItemAction") if isinstance(action, dict) else None
    if not isinstance(replay, dict):
        return
    offset = replay.get("videoOffsetTimeMsec")
    for inner in replay.get("actions") or []:
        item = ((inner or {}).get("addChatItemAction") or {}).get("item") or {}
        row = chat_row(item, offset)
        if row:
            yield row


def chat_row(item: Mapping[str, Any], offset: Any) -> Optional[dict[str, str]]:
    kind = next((name for name in CHAT_MESSAGE_RENDERERS if name in item), None)
    if kind is None:
        return None
    renderer = item[kind]
    message = chat_text(renderer.get("message")) or chat_text(renderer.get("headerSubtext"))
    try:
        when = format_offset(int(offset))
    except (TypeError, ValueError):
        when = chat_text(renderer.get("timestampText"))
    return {
        "時間": when,
        "作者": chat_text(renderer.get("authorName")),
        "留言": message,
        "Super Chat 金額": chat_text(renderer.get("purchaseAmountText")),
    }


def chat_text(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    if "simpleText" in value:
        return str(value["simpleText"])
    parts: list[str] = []
    for run in value.get("runs") or []:
        emoji = run.get("emoji")
        if isinstance(emoji, dict):
            # A channel's own emoji is a picture; its shortcut such as ":yougotthis:" is the closest text.
            shortcuts = emoji.get("shortcuts") or []
            parts.append(str(shortcuts[0] if emoji.get("isCustomEmoji") and shortcuts else emoji.get("emojiId") or ""))
        else:
            parts.append(str(run.get("text") or ""))
    return "".join(parts)


def format_offset(milliseconds: int) -> str:
    """The time into the video, as on YouTube. Messages written while waiting for the stream come before 0:00."""
    sign = "-" if milliseconds < 0 else ""
    minutes, seconds = divmod(abs(milliseconds) // 1000, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{sign}{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{sign}{minutes}:{seconds:02d}"


NFO_DECLARATION = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
# XML 1.0 cannot hold these at all, even escaped, and a media server would refuse the whole file over one. Half of an
# emoji, which YouTube's text sometimes holds, cannot even be written as UTF-8.
XML_INVALID_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿]")


def nfo_text(information: Mapping[str, Any]) -> str:
    """A Kodi episodedetails file, which Jellyfin, Emby and Plex's Kodi-style agents also read."""
    root = ElementTree.Element("episodedetails")

    def add(tag: str, value: Any, **attributes: str) -> None:
        if value:
            ElementTree.SubElement(root, tag, attributes).text = XML_INVALID_RE.sub("", str(value))

    upload_date = str(information.get("upload_date") or "")
    add("title", information.get("title") or information.get("id"))
    add("plot", information.get("description"))
    if re.fullmatch(r"\d{8}", upload_date):
        add("aired", f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:]}")
    add("studio", information.get("channel") or information.get("uploader"))
    add("uniqueid", information.get("id"), type="youtube", default="true")
    ElementTree.indent(root)
    return NFO_DECLARATION + ElementTree.tostring(root, encoding="unicode") + "\n"


def write_nfo(path: Path, information: Mapping[str, Any]) -> Path:
    path.write_text(nfo_text(information), encoding="utf-8")
    return path


def release_time(timestamp: Any, upload_date: str) -> Optional[float]:
    """When the video went online: YouTube's exact time, or else its upload date."""
    if isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool):
        return float(timestamp)
    try:
        day = datetime.strptime(upload_date, "%Y%m%d")
    except ValueError:
        return None
    # Noon UTC shows as the same date in every time zone from UTC-11 to UTC+11.
    return day.replace(hour=12, tzinfo=timezone.utc).timestamp()


def set_file_times(paths: list[str], moment: float) -> None:
    for path in paths:
        try:
            if path:
                os.utime(path, (moment, moment))
        except OSError:
            # A file open in a player keeps the download time; nothing else depends on it.
            pass


def write_music_tags(path: Path, tags: Mapping[str, str]) -> bool:
    """Writes title, tracknumber, album, artist and albumartist with mutagen. False for a file it cannot tag."""
    import mutagen

    try:
        audio = mutagen.File(path, easy=True)
        if audio is None:
            return False
        if audio.tags is None:
            audio.add_tags()
        for key, value in tags.items():
            if value:
                audio[key] = value
        audio.save()
    # TypeError is what a WAV file's plain ID3 tags raise for these names, which only mutagen's easy tags know.
    except (mutagen.MutagenError, KeyError, ValueError, TypeError, OSError):
        return False
    return True


def write_live_chat_csv(json_path: Path) -> Path:
    """ "clip.live_chat.json" becomes "clip.live_chat.csv", with the time, author, message and Super Chat amount."""
    path = json_path.with_suffix(".csv")
    with open_csv(path) as handle:
        write_csv(handle, live_chat_rows(json_path), CHAT_COLUMNS)
    return path

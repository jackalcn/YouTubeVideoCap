"""Reads subscription lists people bring from elsewhere: Google Takeout's subscriptions.csv, or a feed reader's OPML."""

from __future__ import annotations

import csv
import re
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from typing import Iterable

from videocap.textfiles import decode_text
from videocap.urls import apply_channel_tab


# A channel ID is "UC" and 22 more characters of URL-safe base64, which may themselves be "-" or "_".
CHANNEL_ID_RE = re.compile(r"(?<![0-9A-Za-z_-])UC[0-9A-Za-z_-]{22}(?![0-9A-Za-z_-])")
FEED_CHANNEL_RE = re.compile(r"[?&]channel_id=(UC[0-9A-Za-z_-]{22})(?![0-9A-Za-z_-])")
CHANNEL_PATH_RE = re.compile(r"/channel/(UC[0-9A-Za-z_-]{22})(?![0-9A-Za-z_-])")
NOTHING_FOUND_MESSAGE = (
    "檔案裡沒有找到任何頻道。請上傳 Google Takeout 匯出的 subscriptions.csv，或含有 channel_id= 訂閱網址的 OPML 檔。"
)


@dataclass(frozen=True)
class ImportedChannel:
    channel_id: str
    title: str

    def subscription_url(self, channel_tab: str = "all") -> str:
        """The link a subscription checks. A channel's root walks every tab, Shorts and past streams included, so it
        follows the sidebar's 頻道內容, and the 影片 tab when that says all of them."""
        return apply_channel_tab(f"https://www.youtube.com/channel/{self.channel_id}", channel_tab if channel_tab != "all" else "videos")


def parse_subscription_file(data: bytes) -> list[ImportedChannel]:
    """Raises ValueError with a message for the user when the file holds no channel."""
    text = decode_text(data).lstrip("﻿")
    channels = parse_opml(text) if text.lstrip().startswith("<") else parse_takeout_csv(text)
    if not channels:
        raise ValueError(NOTHING_FOUND_MESSAGE)
    return channels


def parse_takeout_csv(text: str) -> list[ImportedChannel]:
    """Takeout writes "Channel Id,Channel Url,Channel Title", with the header in the account's language, so rows are
    read by what their cells hold rather than by the header's names."""
    found: list[ImportedChannel] = []
    for row in csv.reader(text.splitlines()):
        cells = [cell.strip() for cell in row]
        channel_id = next((cell for cell in cells if CHANNEL_ID_RE.fullmatch(cell)), "")
        if not channel_id:
            channel_id = next((match.group(1) for cell in cells if (match := CHANNEL_PATH_RE.search(cell))), "")
        if not channel_id:
            # The header, or a blank line.
            continue
        names = [cell for cell in cells if cell and channel_id not in cell and "://" not in cell]
        found.append(ImportedChannel(channel_id, names[-1] if names else ""))
    return unique_channels(found)


def parse_opml(text: str) -> list[ImportedChannel]:
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        raise ValueError("這個 OPML 檔的格式有誤，無法讀取。請重新匯出後再試。") from exc
    found: list[ImportedChannel] = []
    for outline in root.iter("outline"):
        attributes = {name.lower(): value for name, value in outline.attrib.items()}
        match = FEED_CHANNEL_RE.search(attributes.get("xmlurl", "")) or CHANNEL_PATH_RE.search(attributes.get("htmlurl", ""))
        if match:
            title = attributes.get("title") or attributes.get("text") or ""
            found.append(ImportedChannel(match.group(1), title.strip()))
    return unique_channels(found)


def unique_channels(channels: Iterable[ImportedChannel]) -> list[ImportedChannel]:
    first: dict[str, ImportedChannel] = {}
    for channel in channels:
        first.setdefault(channel.channel_id, channel)
    return list(first.values())


def subscribed_channel_ids(urls: Iterable[str]) -> set[str]:
    """The channel IDs that subscription links name. A link by @handle names none, so such a channel can still be
    offered again; the engine only refuses the very same link."""
    return {match.group(1) for url in urls if (match := CHANNEL_PATH_RE.search(url))}

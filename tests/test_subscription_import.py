from __future__ import annotations

import pytest

from videocap.subscription_import import (
    ImportedChannel,
    parse_subscription_file,
    subscribed_channel_ids,
)


FIRST = "UCX6OQ3DkcsbYNE6H8uQQuVA"
SECOND = "UC-lHJZR3Gqxm24_Vd_AJ5Yw"
THIRD = "UCq-Fj5jknLsUf-MWSy4_brA"

TAKEOUT_CSV = (
    "Channel Id,Channel Url,Channel Title\n"
    f"{FIRST},http://www.youtube.com/channel/{FIRST},MrBeast\n"
    f'{SECOND},http://www.youtube.com/channel/{SECOND},"Hello, 台灣"\n'
    "\n"
)


def test_takeouts_subscriptions_csv_is_read() -> None:
    assert parse_subscription_file(TAKEOUT_CSV.encode("utf-8")) == [
        ImportedChannel(FIRST, "MrBeast"),
        ImportedChannel(SECOND, "Hello, 台灣"),
    ]


@pytest.mark.parametrize("encoding", ["utf-8-sig", "utf-16", "cp950"])
def test_a_list_saved_again_by_another_program_is_read(encoding: str) -> None:
    # Takeout names its columns in the account's language, and Excel saves the file in its own encoding.
    text = f"頻道 ID,頻道網址,頻道名稱\r\n{THIRD},http://www.youtube.com/channel/{THIRD},公視新聞網\r\n"
    assert parse_subscription_file(text.encode(encoding)) == [ImportedChannel(THIRD, "公視新聞網")]


def test_opml_feeds_are_read_and_other_feeds_are_left_out() -> None:
    opml = f"""<?xml version="1.0" encoding="UTF-8"?>
<opml version="1.1">
  <body>
    <outline text="YouTube Subscriptions" title="YouTube Subscriptions">
      <outline text="MrBeast" title="MrBeast" type="rss" xmlUrl="https://www.youtube.com/feeds/videos.xml?channel_id={FIRST}"/>
      <outline text="公視" type="rss" xmlUrl="https://www.youtube.com/feeds/videos.xml?channel_id={THIRD}"/>
      <outline text="A blog" type="rss" xmlUrl="https://example.com/feed.xml"/>
      <outline text="MrBeast again" type="rss" xmlUrl="https://www.youtube.com/feeds/videos.xml?channel_id={FIRST}"/>
    </outline>
  </body>
</opml>"""
    assert parse_subscription_file(opml.encode("utf-8")) == [ImportedChannel(FIRST, "MrBeast"), ImportedChannel(THIRD, "公視")]


@pytest.mark.parametrize(
    "content",
    [b"", b"just some text\nwith lines\n", b"<opml><body><outline text='x'/></body></opml>", b"<opml><body>"],
)
def test_a_file_without_channels_says_what_to_upload(content: bytes) -> None:
    with pytest.raises(ValueError, match="subscriptions.csv|OPML"):
        parse_subscription_file(content)


def test_a_list_of_only_links_or_with_extra_columns_is_read() -> None:
    links = f"﻿https://www.youtube.com/channel/{FIRST}\r\n\r\n  https://youtube.com/channel/{SECOND}/videos  \r\n"
    assert parse_subscription_file(links.encode("utf-8")) == [ImportedChannel(FIRST, ""), ImportedChannel(SECOND, "")]
    extra = f'訂閱日期,頻道標題,頻道 ID,備註\n2024-01-01,"A, B ""C""",{THIRD},\n'
    assert parse_subscription_file(extra.encode("utf-8")) == [ImportedChannel(THIRD, 'A, B "C"')]


def test_opml_is_read_safely() -> None:
    nested = (
        '<opml version="2.0"><body><outline text="一"><outline text="二">'
        f'<outline text="Tom &amp; Jerry" xmlUrl="https://www.youtube.com/feeds/videos.xml?foo=1&amp;channel_id={FIRST}"/>'
        "</outline></outline></body></opml>"
    )
    assert parse_subscription_file(nested.encode("utf-8")) == [ImportedChannel(FIRST, "Tom & Jerry")]
    laughs = (
        '<?xml version="1.0"?><!DOCTYPE opml [<!ENTITY a "aaaaaaaaaa">'
        + "".join(f'<!ENTITY {chr(98 + level)} "{("&" + chr(97 + level) + ";") * 10}">' for level in range(8))
        + f'<!ENTITY ext SYSTEM "http://127.0.0.1:9/never">]><opml><body><outline text="&i;" xmlUrl="?channel_id={FIRST}"/></body></opml>'
    )
    with pytest.raises(ValueError):
        parse_subscription_file(laughs.encode("utf-8"))
    with pytest.raises(ValueError, match="OPML"):
        parse_subscription_file(b"<html><body>not a feed list</body>")


def test_an_imported_channel_follows_the_chosen_tab() -> None:
    channel = ImportedChannel(FIRST, "MrBeast")
    # A channel's root would walk Shorts and past streams as well.
    assert channel.subscription_url() == f"https://www.youtube.com/channel/{FIRST}/videos"
    assert channel.subscription_url("streams") == f"https://www.youtube.com/channel/{FIRST}/streams"


def test_subscribed_channels_are_known_by_their_id() -> None:
    urls = [f"https://www.youtube.com/channel/{FIRST}/videos", "https://www.youtube.com/@handle/videos", "https://www.youtube.com/playlist?list=PL1"]
    assert subscribed_channel_ids(urls) == {FIRST}

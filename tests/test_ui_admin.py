"""The owner's pages (history, subscriptions, system), the password gate, and who counts as this machine's owner."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

import videocap.services as services_module
from test_speech import downloaded_model
from test_tooling import NEWEST_DATE, latest_commit, local_day, point_at_github_stand_in
from videocap.downloader import empty_outcome


ROOT = Path(__file__).resolve().parents[1]


def fake_download(url: str, options: Any, progress_ui: Any, status_ui: Any, should_stop: Any, on_log: Any) -> Any:
    options.output_dir.mkdir(parents=True, exist_ok=True)
    path = options.output_dir / "video.mp4"
    path.write_bytes(b"video-bytes")
    outcome = empty_outcome()
    outcome["items"] = [
        {
            "title": "Fake Title",
            "path": str(path),
            "subtitle_paths": [],
            "extra_paths": [],
            "video_id": "jNQXAC9IVRw",
            "channel": "Channel",
            "webpage_url": url,
            "duration": 19.0,
            "upload_date": "20050424",
            "thumbnail": "",
            "quality": "mp4",
        }
    ]
    return outcome


@pytest.fixture(autouse=True)
def isolated_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VIDEOCAP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("VIDEOCAP_OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setenv("VIDEOCAP_TRUST_REMOTE", "1")
    monkeypatch.setenv("VIDEOCAP_MODE", "local")
    monkeypatch.delenv("VIDEOCAP_PASSWORD", raising=False)
    monkeypatch.delenv("VIDEOCAP_ALLOWED_HOSTS", raising=False)
    monkeypatch.setattr(services_module, "run_download", fake_download)
    monkeypatch.setattr(services_module, "CLOUD_OUTPUT_ROOT", tmp_path / "public")
    st.cache_resource.clear()
    yield
    st.cache_resource.clear()


def page_script(page_name: str) -> None:
    import importlib

    from videocap.ui.context import build_context, init_session_state

    ctx = build_context()
    init_session_state(ctx)
    module = importlib.import_module(f"videocap.ui.page_{page_name}")
    getattr(module, f"render_{page_name}_page")(ctx)


def new_page(page_name: str) -> AppTest:
    return AppTest.from_function(page_script, kwargs={"page_name": page_name}, default_timeout=30)


def run_page(page_name: str) -> AppTest:
    app = new_page(page_name)
    app.run()
    assert not app.exception, app.exception
    return app


def owner_services() -> Any:
    from videocap.ui.context import get_services

    return get_services(False)


def button(app: AppTest, label: str) -> Any:
    return next(item for item in app.button if item.label == label)


def labels(app: AppTest) -> list[str]:
    return [item.label for item in app.button]


def texts(elements: Any) -> str:
    return " ".join(str(element.value) for element in elements)


# History


def add_history_items(count: int, start: int = 1) -> None:
    owner_services().storage.add_history(
        {
            "video_id": f"video{number:06d}",
            "title": f"History item {number}",
            "channel": "Chan A" if number % 2 else "Chan B",
            "url": f"https://www.youtube.com/watch?v=video{number:06d}",
            # The lowest number is the newest, so it is the first row.
            "finished_at": 100000.0 - number,
        }
        for number in range(start, start + count)
    )


def history_titles() -> list[str]:
    return [entry.title for entry in owner_services().storage.list_history()]


def run_with_selection(app: AppTest, frame_id: str, rows: list[int]) -> AppTest:
    """Runs the app as the browser would after ticking rows: AppTest itself cannot tick a table's rows."""
    states = app._tree.get_widget_states()
    state = states.widgets.add()
    state.id = frame_id
    state.string_value = json.dumps({"selection": {"rows": rows, "columns": [], "cells": []}})
    app._run(states)
    assert not app.exception, app.exception
    return app


def test_deleting_history_rows_does_not_leave_other_rows_ticked() -> None:
    add_history_items(4)
    app = run_page("history")
    frame_id = app.dataframe[0].proto.id
    run_with_selection(app, frame_id, [0, 1])
    assert "刪除 2 筆紀錄" in labels(app)

    button(app, "刪除 2 筆紀錄").click()
    # The browser keeps sending the rows it last had ticked.
    run_with_selection(app, frame_id, [0, 1])
    assert history_titles() == ["History item 3", "History item 4"]
    assert "刪除 0 筆紀錄" in labels(app)
    assert button(app, "刪除 0 筆紀錄").disabled


def test_history_ticks_do_not_move_to_other_rows_when_the_list_changes() -> None:
    add_history_items(4)
    app = run_page("history")
    frame_id = app.dataframe[0].proto.id
    run_with_selection(app, frame_id, [0])
    assert "重新下載 1 項" in labels(app)

    # A download finishing puts a new record at the top of the list.
    add_history_items(1, start=0)
    run_with_selection(app, frame_id, [0])
    assert "重新下載 0 項" in labels(app)

    frame_id = app.dataframe[0].proto.id
    run_with_selection(app, frame_id, [1])
    app.text_input(key="keep_history_search").set_value("Chan B")
    run_with_selection(app, frame_id, [1])
    assert "重新下載 0 項" in labels(app)


def test_history_export_holds_every_record_and_the_table_says_it_is_cut_short() -> None:
    from videocap.ui import page_history

    add_history_items(1005)
    app = run_page("history")
    assert "只顯示最新 1000 筆" in texts(app.caption)

    exported = page_history.history_csv(owner_services().storage, "").decode("utf-8-sig")
    assert len(exported.strip().splitlines()) == 1 + 1005
    assert "History item 1005" in exported
    searched = page_history.history_csv(owner_services().storage, "item 100").decode("utf-8-sig")
    assert len(searched.strip().splitlines()) == 1 + 7


def in_new_york(monkeypatch: pytest.MonkeyPatch) -> None:
    # Far from this machine's own zone, so a time shown in the server's zone gives itself away.
    fake = FakeContext({})
    fake.timezone = "America/New_York"
    monkeypatch.setattr(st, "context", fake)


def test_history_shows_times_in_the_visitors_time_zone(monkeypatch: pytest.MonkeyPatch) -> None:
    from zoneinfo import ZoneInfo

    from videocap.ui import page_history

    in_new_york(monkeypatch)
    storage = owner_services().storage
    storage.add_history([{"video_id": "abc", "title": "Old", "url": "https://www.youtube.com/watch?v=abc", "finished_at": 86400.0}])
    app = run_page("history")
    assert list(app.dataframe[0].value["下載時間"]) == ["1970-01-01 19:00"]

    # The export is built after the page has run, where the browser's zone is no longer known, so the page hands it over.
    exported = page_history.history_csv(storage, "", ZoneInfo("America/New_York")).decode("utf-8-sig")
    assert "1970-01-01 19:00:00" in exported


def test_subscriptions_show_times_in_the_visitors_time_zone(monkeypatch: pytest.MonkeyPatch) -> None:
    from videocap.settings import DownloadSettings

    in_new_york(monkeypatch)
    storage = owner_services().storage
    subscription_id = storage.add_subscription(CHANNEL, "Chan", DownloadSettings(), "out", 5, 24)
    storage.update_subscription(subscription_id, last_checked_at=86400.0)
    app = run_page("subscriptions")
    assert "上次檢查 01/01 19:00" in texts(app.caption).replace(chr(92), "")


# Subscriptions

CHANNEL = "https://www.youtube.com/@somechannel/videos"
PLAYLIST_ID = "PLabcdefghijklmnop"


@pytest.fixture
def channel_listing(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stands in for reading a channel's newest videos from YouTube."""
    listing: dict[str, Any] = {"ids": ["OLDVIDEO001", "OLDVIDEO002"], "calls": []}

    def list_recent_video_ids(url: str, limit: int, login: Any, network: Any = None) -> tuple[str, list[str]]:
        listing["calls"].append(url)
        listing["network"] = network
        return "Some channel", list(listing["ids"])

    monkeypatch.setattr(services_module, "list_recent_video_ids", list_recent_video_ids)
    return listing


def subscription_link(app: AppTest) -> Any:
    return next(item for item in app.text_input if item.label == "頻道或播放清單連結")


def subscription_count(app: AppTest) -> Any:
    return next(item for item in app.number_input if item.label == "每次最多下載幾部")


def subscribe(app: AppTest, link: str) -> AppTest:
    subscription_link(app).set_value(link)
    button(app, "加入訂閱").click().run()
    assert not app.exception, app.exception
    return app


def test_adding_a_subscription_twice_leaves_its_new_videos_alone(channel_listing: dict[str, Any]) -> None:
    app = subscribe(run_page("subscriptions"), CHANNEL)
    assert "已訂閱「Some channel」" in texts(app.success)

    # New videos appear before the next check, and the same link is added again.
    channel_listing["ids"] = ["NEWVIDEO001", "NEWVIDEO002"]
    subscribe(app, CHANNEL)
    assert "已經訂閱過了" in texts(app.warning)
    assert channel_listing["calls"] == [CHANNEL]
    storage = owner_services().storage
    assert storage.archived_video_ids() == {"OLDVIDEO001", "OLDVIDEO002"}
    assert len(storage.list_subscriptions()) == 1


def test_a_subscription_with_a_bad_setting_is_refused(channel_listing: dict[str, Any]) -> None:
    app = new_page("subscriptions")
    app.session_state["opt_filter_date_after"] = "not-a-date"
    app.session_state["settings_loaded"] = True
    app.run()
    subscribe(app, CHANNEL)
    assert "看不懂日期" in texts(app.error)
    assert owner_services().storage.list_subscriptions() == []
    assert channel_listing["calls"] == []


def test_checking_a_subscription_with_a_bad_setting_says_why(channel_listing: dict[str, Any], tmp_path: Path) -> None:
    from videocap.settings import DownloadSettings

    storage = owner_services().storage
    out = str(tmp_path / "out")
    # Saved by an older version, which did not check the settings when subscribing.
    broken_id = storage.add_subscription(CHANNEL, "Broken", DownloadSettings(filter_date_after="not-a-date"), out, 5, 24)
    working_id = storage.add_subscription("https://www.youtube.com/@otherchannel/videos", "Working", DownloadSettings(), out, 5, 24)
    app = run_page("subscriptions")
    app.button(key=f"subscription_check_{broken_id}").click().run()
    assert not app.exception, app.exception
    assert "看不懂日期" in storage.get_subscription(broken_id).last_result
    assert "看不懂日期" in texts(app.caption)

    button(app, "全部立即檢查").click().run()
    assert not app.exception, app.exception
    jobs = owner_services().manager.list_jobs()
    assert [job.payload.spec.subscription_id for job in jobs] == [working_id]
    assert "看不懂日期" in storage.get_subscription(broken_id).last_result


def form_block(app: AppTest, form_id: str) -> Any:
    blocks = list(app.main.children.values())
    while blocks:
        block = blocks.pop()
        if block.type == "form" and block.proto.form.form_id == form_id:
            return block
        blocks.extend(getattr(block, "children", {}).values())
    raise AssertionError(f"no form {form_id}")


def test_a_failed_subscription_keeps_what_was_typed(channel_listing: dict[str, Any]) -> None:
    app = run_page("subscriptions")
    # The browser empties a form that clears on submit, which AppTest does not imitate.
    assert not form_block(app, "subscription_form").proto.form.clear_on_submit
    subscription_count(app).set_value(12)
    video = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
    subscribe(app, video)
    assert "單一影片" in texts(app.error)
    assert subscription_link(app).value == video
    assert subscription_count(app).value == 12

    subscribe(app, "")
    assert "請輸入頻道或播放清單的連結" in texts(app.error)

    subscribe(app, CHANNEL)
    assert "已訂閱" in texts(app.success)
    assert subscription_link(app).value == ""
    assert subscription_count(app).value == 5


def test_a_video_in_a_playlist_subscribes_to_the_playlist(channel_listing: dict[str, Any]) -> None:
    app = subscribe(run_page("subscriptions"), f"https://www.youtube.com/watch?v=jNQXAC9IVRw&list={PLAYLIST_ID}&index=3")
    assert not app.error
    playlist = f"https://www.youtube.com/playlist?list={PLAYLIST_ID}"
    assert [subscription.url for subscription in owner_services().storage.list_subscriptions()] == [playlist]


@pytest.mark.parametrize(
    "link",
    ["https://www.youtube.com/", "https://www.youtube.com/results?search_query=cats", "https://www.youtube.com/feed/trending"],
)
def test_only_channels_and_playlists_can_be_subscribed(link: str, channel_listing: dict[str, Any]) -> None:
    app = subscribe(run_page("subscriptions"), link)
    assert "頻道或播放清單" in texts(app.error)
    assert owner_services().storage.list_subscriptions() == []
    assert channel_listing["calls"] == []


def test_extra_links_are_named_when_subscribing(channel_listing: dict[str, Any]) -> None:
    app = subscribe(run_page("subscriptions"), f"{CHANNEL}\nhttps://www.youtube.com/@otherchannel")
    assert "已略過其他 1 個" in texts(app.warning)
    assert [subscription.url for subscription in owner_services().storage.list_subscriptions()] == [CHANNEL]


def subscriptions_page_with(values: dict[str, Any]) -> AppTest:
    app = new_page("subscriptions")
    app.session_state["settings_loaded"] = True
    for key, value in values.items():
        app.session_state[key] = value
    app.run()
    assert not app.exception, app.exception
    return app


def test_a_clip_left_in_the_sidebar_does_not_stop_a_subscription(channel_listing: dict[str, Any]) -> None:
    # Subscriptions never cut clips, so a range typed for one video earlier has nothing to do with them.
    app = subscribe(subscriptions_page_with({"opt_time_ranges": "not-a-range", "opt_playlist_items": "1-3"}), CHANNEL)
    assert not app.error
    assert "已訂閱「Some channel」" in texts(app.success)


def test_subscribing_reads_the_channel_through_the_proxy(channel_listing: dict[str, Any]) -> None:
    subscribe(subscriptions_page_with({"opt_proxy": "http://127.0.0.1:7890"}), CHANNEL)
    assert channel_listing["network"].proxy == "http://127.0.0.1:7890"


# The download folder


def output_dir_script(typed: str) -> None:
    import streamlit as st

    from videocap.ui.context import OUTPUT_DIR_WIDGET_KEY, build_context, current_output_dir

    st.session_state[OUTPUT_DIR_WIDGET_KEY] = typed
    st.text(str(current_output_dir(build_context())))


@pytest.mark.parametrize("wrap", ['"{}"', '  "{}"  ', "{}"])
def test_a_folder_pasted_with_quotes_is_used_as_it_is(wrap: str, tmp_path: Path) -> None:
    folder = tmp_path / "My Videos"
    app = AppTest.from_function(output_dir_script, kwargs={"typed": wrap.format(folder)}, default_timeout=30)
    app.run()
    assert not app.exception, app.exception
    assert app.text[0].value == str(folder.resolve())


# Who counts as this machine's owner


class FakeContext:
    """Stands in for st.context, which AppTest cannot fill with a browser's headers and cookies."""

    def __init__(self, headers: dict[str, str], ip_address: str | None = None, cookies: dict[str, str] | None = None) -> None:
        self.headers = headers
        self.ip_address = ip_address
        self.cookies = cookies or {}
        self._real = st.context

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


@pytest.mark.parametrize(
    "headers, ip_address, expected",
    [
        ({"Host": "localhost:8501"}, None, True),
        ({"Host": "127.0.0.1:8501"}, None, True),
        ({"Host": "[::1]:8501"}, None, True),
        ({"Host": "localhost:8501", "X-Forwarded-For": "203.0.113.5"}, None, False),
        ({"Host": "localhost:8501", "Forwarded": "for=203.0.113.5"}, None, False),
        ({"Host": "localhost"}, "192.168.1.9", False),
        ({"Host": "192.168.1.20:8501"}, None, False),
        # DNS rebinding: another site's name made to point at this machine.
        ({"Host": "rebind.attacker.test:8501"}, None, False),
        ({"Host": "localhost:8501", "Origin": "http://localhost:8501"}, None, True),
        ({"Host": "localhost:8501", "Origin": "http://127.0.0.1:8501"}, None, True),
        # Another site's page talking to a server whose CORS check was turned off.
        ({"Host": "localhost:8501", "Origin": "https://evil.attacker.test"}, None, False),
        ({"Host": "localhost:8501", "Origin": "null"}, None, False),
    ],
)
def test_only_this_machine_counts_as_local(headers: dict[str, str], ip_address: str | None, expected: bool) -> None:
    from videocap.ui.context import is_local_request

    assert is_local_request(headers, ip_address) is expected


def test_trusting_remote_visitors_needs_a_name_meant_for_this_app(monkeypatch: pytest.MonkeyPatch) -> None:
    from videocap.ui.context import remote_request_is_trusted

    assert remote_request_is_trusted({"Host": "localhost:8501"})
    assert remote_request_is_trusted({"Host": "127.0.0.1:8501"})
    assert not remote_request_is_trusted({"Host": "rebind.attacker.test:8501"})
    assert not remote_request_is_trusted({"Host": "nas.local:8501"})
    assert not remote_request_is_trusted({"Host": ":8501"})

    monkeypatch.setenv("VIDEOCAP_ALLOWED_HOSTS", "NAS.local, 192.168.1.20")
    assert remote_request_is_trusted({"Host": "nas.local:8501"})
    assert remote_request_is_trusted({"Host": "192.168.1.20"})
    assert not remote_request_is_trusted({"Host": "rebind.attacker.test:8501"})

    monkeypatch.delenv("VIDEOCAP_TRUST_REMOTE")
    assert not remote_request_is_trusted({"Host": "nas.local:8501"})


def test_trusting_remote_visitors_needs_a_page_from_this_app(monkeypatch: pytest.MonkeyPatch) -> None:
    from videocap.ui.context import remote_request_is_trusted

    monkeypatch.setenv("VIDEOCAP_ALLOWED_HOSTS", "nas.local")
    assert remote_request_is_trusted({"Host": "nas.local:8501", "Origin": "http://nas.local:8501"})
    assert remote_request_is_trusted({"Host": "nas.local:8501", "Origin": "http://localhost:8501"})
    assert not remote_request_is_trusted({"Host": "nas.local:8501", "Origin": "https://evil.attacker.test"})
    assert not remote_request_is_trusted({"Origin": "https://evil.attacker.test"})
    assert not remote_request_is_trusted({"Host": "nas.local:8501", "Origin": "null"})


def test_a_page_from_another_site_gets_only_the_public_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(st, "context", FakeContext({"Host": "rebind.attacker.test:8501"}, "172.17.0.1"))
    app = AppTest.from_file(str(ROOT / "main.py"), default_timeout=30)
    app.run()
    assert not app.exception, app.exception
    assert "下載資料夾" not in [item.label for item in app.text_input]

    monkeypatch.setattr(st, "context", FakeContext({"Host": "localhost:8501", "Origin": "https://evil.attacker.test"}))
    app = AppTest.from_file(str(ROOT / "main.py"), default_timeout=30)
    app.run()
    assert not app.exception, app.exception
    assert "下載資料夾" not in [item.label for item in app.text_input]

    monkeypatch.setattr(st, "context", FakeContext({"Host": "localhost:8501"}, "172.17.0.1"))
    app = AppTest.from_file(str(ROOT / "main.py"), default_timeout=30)
    app.run()
    assert "下載資料夾" in [item.label for item in app.text_input]


def owner_script() -> None:
    import streamlit as st

    from videocap.ui.context import get_session_owner

    st.text(get_session_owner())


@pytest.mark.parametrize("cookie", ["local", "0123456789abcdef0123456789abcde", "0123456789ABCDEF0123456789ABCDEF", "x" * 32])
def test_a_crafted_visitor_cookie_gets_a_fresh_id(cookie: str, monkeypatch: pytest.MonkeyPatch) -> None:
    import re

    monkeypatch.setattr(st, "context", FakeContext({"Host": "example.streamlit.app"}, "203.0.113.5", {"videocap_visitor": cookie}))
    app = AppTest.from_function(owner_script, default_timeout=30)
    app.run()
    owner = app.text[0].value
    assert owner != cookie and owner != "local"
    assert re.fullmatch(r"[0-9a-f]{32}", owner)


def test_a_returning_visitor_keeps_their_id(monkeypatch: pytest.MonkeyPatch) -> None:
    saved = "0123456789abcdef0123456789abcdef"
    monkeypatch.setattr(st, "context", FakeContext({"Host": "example.streamlit.app"}, "203.0.113.5", {"videocap_visitor": saved}))
    app = AppTest.from_function(owner_script, default_timeout=30)
    app.run()
    assert app.text[0].value == saved


def cookie_scripts(app: AppTest) -> list[str]:
    return [element.proto.body for element in app.get("html") if "videocap_visitor=" in element.proto.body]


def test_a_returning_visitors_cookie_is_renewed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIDEOCAP_MODE", "cloud")
    monkeypatch.delenv("VIDEOCAP_TRUST_REMOTE")
    saved = "0123456789abcdef0123456789abcdef"
    monkeypatch.setattr(st, "context", FakeContext({"Host": "example.streamlit.app"}, "203.0.113.5", {"videocap_visitor": saved}))
    app = AppTest.from_file(str(ROOT / "main.py"), default_timeout=30)
    app.run()
    assert not app.exception, app.exception
    # The cookie would otherwise expire two hours after the first visit, however often the visitor came back.
    scripts = cookie_scripts(app)
    assert len(scripts) == 1 and f"videocap_visitor={saved};" in scripts[0]


# The password gate


@pytest.fixture
def secrets_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Points Streamlit at a secrets file of the test's own, read afresh."""
    import os

    from streamlit import config
    from streamlit.runtime.secrets import Secrets

    path = tmp_path / ".streamlit" / "secrets.toml"
    path.parent.mkdir()
    monkeypatch.setattr(st, "secrets", Secrets())
    original = config.get_option("secrets.files")
    config.set_option("secrets.files", [str(tmp_path / "missing.toml"), str(path)])
    yield path
    config.set_option("secrets.files", original)
    # Streamlit copies top-level secrets into the environment.
    for name in ("app_password", "\ufeffapp_password"):
        os.environ.pop(name, None)


@pytest.mark.parametrize(
    "encoding, password",
    [("utf-8", "open-sesame"), ("utf-8-sig", "open-sesame"), ("utf-16", "open-sesame"), ("cp950", "芝麻開門")],
)
def test_the_password_is_read_whatever_the_file_encoding(secrets_file: Path, encoding: str, password: str) -> None:
    import videocap.ui.app as app_module

    secrets_file.write_bytes(f'# 存取密碼\napp_password = "{password}"\n'.encode(encoding))
    assert app_module.get_access_password() == password


def test_a_password_file_saved_by_notepad_keeps_the_app_locked(secrets_file: Path) -> None:
    secrets_file.write_bytes('app_password = "open-sesame"\n'.encode("utf-8-sig"))
    app = AppTest.from_file(str(ROOT / "main.py"), default_timeout=30)
    app.run()
    assert not app.exception, app.exception
    assert "開始下載" not in labels(app)
    assert "請輸入存取密碼" in [item.label for item in app.text_input]


def test_an_unreadable_secrets_file_keeps_the_app_closed(secrets_file: Path) -> None:
    import videocap.ui.app as app_module

    secrets_file.write_text('app_password = "unterminated\n', encoding="utf-8")
    assert app_module.get_access_password() is None
    secrets_file.write_bytes(b"\x00\xff" * 40)
    assert app_module.get_access_password() is None


def test_no_secrets_file_means_no_password(secrets_file: Path) -> None:
    import videocap.ui.app as app_module

    assert app_module.get_access_password() == ""
    secrets_file.write_text('other_setting = "x"\n', encoding="utf-8")
    assert app_module.get_access_password() == ""


def test_wrong_passwords_slow_down_every_session(monkeypatch: pytest.MonkeyPatch) -> None:
    import videocap.ui.app as app_module

    monkeypatch.setenv("VIDEOCAP_PASSWORD", "open-sesame")
    monkeypatch.setattr(app_module, "FAILED_LOGIN_DELAY_SECONDS", 0)
    monkeypatch.setattr(app_module, "LOGIN_THROTTLE", app_module.LoginThrottle())
    monkeypatch.setattr(st, "context", FakeContext({"Host": "example.streamlit.app"}, "203.0.113.5"))

    def try_password(password: str) -> AppTest:
        # Each AppTest is a session of its own, like a script opening a new browser tab for every guess.
        app = AppTest.from_file(str(ROOT / "main.py"), default_timeout=30)
        app.run()
        app.text_input[0].set_value(password)
        app.button[0].click().run()
        assert not app.exception, app.exception
        return app

    for _ in range(app_module.FREE_LOGIN_ATTEMPTS):
        assert "密碼不正確" in texts(try_password("wrong").error)
    app = try_password("open-sesame")
    assert "嘗試錯誤太多次" in texts(app.error)
    assert "開始下載" not in labels(app)


def test_the_login_throttle_counts_each_address_on_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    import videocap.ui.app as app_module

    throttle = app_module.LoginThrottle()
    for _ in range(app_module.FREE_LOGIN_ATTEMPTS):
        assert throttle.seconds_to_wait("203.0.113.5") == 0
        throttle.record_failure("203.0.113.5")
    assert 0 < throttle.seconds_to_wait("203.0.113.5") <= app_module.FIRST_LOGIN_LOCKOUT_SECONDS
    assert throttle.seconds_to_wait("198.51.100.7") == 0
    throttle.record_failure("203.0.113.5")
    assert app_module.FIRST_LOGIN_LOCKOUT_SECONDS < throttle.seconds_to_wait("203.0.113.5") <= 2 * app_module.FIRST_LOGIN_LOCKOUT_SECONDS

    clock = [app_module.time.time() + app_module.MAX_LOGIN_LOCKOUT_SECONDS + 1]
    monkeypatch.setattr(app_module.time, "time", lambda: clock[0])
    assert throttle.seconds_to_wait("203.0.113.5") == 0
    throttle.forget("203.0.113.5")
    throttle.record_failure("203.0.113.5")
    assert throttle.seconds_to_wait("203.0.113.5") == 0


# The system page


def system_page_with(values: dict[str, Any]) -> AppTest:
    app = new_page("system")
    app.session_state["settings_loaded"] = True
    for key, value in values.items():
        app.session_state[key] = value
    app.run()
    assert not app.exception, app.exception
    return app


def upload_settings(app: AppTest, name: str, settings: dict[str, Any]) -> AppTest:
    app.file_uploader(key="settings_import_upload").upload(name, json.dumps(settings, ensure_ascii=False).encode("utf-8-sig"), "application/json")
    app.run()
    assert not app.exception, app.exception
    return app


def test_importing_settings_keeps_this_machines_proxy_and_template() -> None:
    from videocap.config import NAMING_ORIGINAL, NAMING_TEMPLATE
    from videocap.storage import LAST_SETTINGS_KEY

    app = system_page_with({"opt_proxy": "http://127.0.0.1:7890", "opt_naming_rule": NAMING_ORIGINAL})
    shared = {
        "quality": "720p",
        "proxy": "http://stranger.example:3128",
        "naming_rule": NAMING_TEMPLATE,
        "custom_template": "C:/Windows/%(title)s.%(ext)s",
    }
    upload_settings(app, "youtube_videocap_settings.json", shared)
    button(app, "套用到目前設定").click().run()
    assert not app.exception, app.exception
    assert "已套用" in texts(app.success)
    assert app.session_state["opt_quality"] == "720p"
    assert app.session_state["opt_proxy"] == "http://127.0.0.1:7890"
    assert app.session_state["opt_naming_rule"] == NAMING_ORIGINAL
    assert app.session_state["opt_custom_template"] == ""
    saved = owner_services().storage.get_value(LAST_SETTINGS_KEY)
    assert saved["proxy"] == "http://127.0.0.1:7890" and saved["custom_template"] == ""


SHARED_COMMAND = "--exec \"calc.exe\""


def test_an_imported_settings_file_cannot_run_commands() -> None:
    from videocap.storage import LAST_SETTINGS_KEY

    # --exec runs a command after each download, so a settings file shared online must not bring one.
    app = system_page_with({"opt_ytdlp_extra_args": "--no-mtime"})
    upload_settings(app, "youtube_videocap_settings.json", {"quality": "720p", "ytdlp_extra_args": SHARED_COMMAND})
    assert "進階 yt-dlp 參數" in texts(app.caption)
    button(app, "套用到目前設定").click().run()
    assert not app.exception, app.exception
    assert app.session_state["opt_quality"] == "720p"
    assert app.session_state["opt_ytdlp_extra_args"] == "--no-mtime"
    assert owner_services().storage.get_value(LAST_SETTINGS_KEY)["ytdlp_extra_args"] == "--no-mtime"


def test_an_imported_preset_brings_no_commands_proxy_or_template() -> None:
    from videocap.config import NAMING_TEMPLATE

    app = system_page_with({"opt_ytdlp_extra_args": "--no-mtime", "opt_proxy": "http://127.0.0.1:7890"})
    shared = {
        "quality": "360p",
        "ytdlp_extra_args": SHARED_COMMAND,
        "proxy": "http://stranger.example:3128",
        "naming_rule": NAMING_TEMPLATE,
        "custom_template": "C:/Windows/%(title)s.%(ext)s",
    }
    upload_settings(app, "分享的組合.json", shared)
    button(app, "存成自訂組合").click().run()
    assert not app.exception, app.exception
    preset = owner_services().storage.load_presets()["分享的組合"]
    assert preset["quality"] == "360p"
    assert preset["ytdlp_extra_args"] == "--no-mtime"
    assert preset["proxy"] == "http://127.0.0.1:7890"
    assert preset["custom_template"] == "" and preset["naming_rule"] != NAMING_TEMPLATE

    # A saved custom preset applies its own extra options, so choosing it later must not bring the file's either.
    later = run_main_app()
    later.selectbox(key="preset_choice").set_value("分享的組合").run()
    assert not later.exception, later.exception
    assert later.session_state["opt_quality"] == "360p"
    assert "--exec" not in later.session_state["opt_ytdlp_extra_args"]
    assert later.session_state["opt_custom_template"] == ""


def test_imported_settings_leave_out_one_off_clips_and_items() -> None:
    from videocap.storage import LAST_SETTINGS_KEY

    app = system_page_with({})
    one_off = {"time_ranges": "1:30-2:00", "playlist_items": "1-3", "remove_ranges": "0:00-0:30"}
    upload_settings(app, "clip.json", {"quality": "720p", **one_off})
    button(app, "套用到目前設定").click().run()
    assert not app.exception, app.exception
    saved = owner_services().storage.get_value(LAST_SETTINGS_KEY)
    assert saved["quality"] == "720p"
    assert all(saved[name] == "" for name in one_off)
    assert all(app.session_state[f"opt_{name}"] == "" for name in one_off)

    button(app, "存成自訂組合").click().run()
    preset = owner_services().storage.load_presets()["clip"]
    assert preset["quality"] == "720p"
    assert all(preset[name] == "" for name in one_off)


def test_exported_settings_hold_no_passwords_or_login(monkeypatch: pytest.MonkeyPatch) -> None:
    offered: dict[str, Any] = {}
    real_download_button = st.download_button

    def recording_download_button(label: str, data: Any = None, **kwargs: Any) -> Any:
        # AppTest drops its media storage after each run, so the file's bytes are taken as the page hands them over.
        offered[label] = data
        return real_download_button(label, data, **kwargs)

    monkeypatch.setattr(st, "download_button", recording_download_button)
    owner_args = '--no-mtime --username me@example.com --password "hunter 2" --add-header "Authorization: Bearer abc"'
    system_page_with({"opt_ytdlp_extra_args": owner_args, "opt_proxy": "http://user:pw@127.0.0.1:7890", "opt_quality": "720p"})
    exported = json.loads(offered["匯出目前設定"].decode("utf-8"))
    assert exported["quality"] == "720p"
    assert exported["proxy"] == ""
    assert exported["ytdlp_extra_args"] == "--no-mtime"
    for secret in ("me@example.com", "hunter", "Bearer"):
        assert secret not in json.dumps(exported)


def test_a_huge_settings_file_is_refused_before_it_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    import videocap.ui.page_system as page_system

    # Guessing the encoding of 10 MB of Big5 holds the whole server for about ten seconds, at every rerun.
    monkeypatch.setattr(page_system, "decode_text", lambda data: pytest.fail("read a huge settings file"))
    monkeypatch.setenv("VIDEOCAP_MODE", "cloud")
    monkeypatch.delenv("VIDEOCAP_TRUST_REMOTE")
    app = system_page_with({})
    app.file_uploader(key="settings_import_upload").upload("settings.json", "大家好".encode("cp950") * 400_000, "application/json").run()
    assert not app.exception, app.exception
    assert "太大" in texts(app.error)


def test_the_settings_uploader_says_how_big_a_file_it_takes() -> None:
    import videocap.ui.page_system as page_system

    # The browser refuses a larger file itself, with Streamlit's English tooltip, so the page's own message never shows.
    uploader = system_page_with({}).file_uploader(key="settings_import_upload")
    assert f"最多 {page_system.SETTINGS_UPLOAD_MAX_MB} MB" in uploader.help
    assert "瀏覽器會直接拒絕" in uploader.help


def test_a_broken_tool_does_not_show_visitors_the_servers_folders(monkeypatch: pytest.MonkeyPatch) -> None:
    import videocap.ui.page_system as page_system

    hung = "ffmpeg 無法執行：Command '['/srv/videocap/bin/ffmpeg', '-version']' timed out after 5 seconds"
    monkeypatch.setattr(page_system, "get_ffmpeg_status", lambda: (False, hung))
    monkeypatch.setattr(page_system, "get_js_runtime_status", lambda: (False, "node 無法使用：/srv/videocap/node: bad ELF"))
    monkeypatch.setenv("VIDEOCAP_MODE", "cloud")
    monkeypatch.delenv("VIDEOCAP_TRUST_REMOTE")
    table = system_page_with({}).dataframe[0].value
    assert "/srv/videocap" not in table.to_string()
    assert list(table.loc[table["項目"] == "ffmpeg", "狀態"]) == ["缺少"]


@pytest.mark.parametrize(
    ("content", "quality"),
    [
        (b"", None),
        (b"\x89PNG\r\n\x1a\n\x00\x00\x00", None),
        (b"[1, 2]", None),
        (b"[" * 100_000 + b"]" * 100_000, None),
        # Wrong kinds of values fall back to the defaults.
        (b'{"quality": [1], "concurrent": "x", "title": null}', "1080p"),
        ('{"quality": "720p", "naming_rule": "原始"}'.encode("utf-16"), "720p"),
        ('{"quality": "480p", "custom_template": "下載"}'.encode("cp950"), "480p"),
        (b'{"quality": "360p", "padding": "' + b"x" * (900 * 1024) + b'"}', "360p"),
    ],
)
def test_any_settings_file_is_read_or_refused_in_words(content: bytes, quality: Any) -> None:
    app = system_page_with({})
    app.file_uploader(key="settings_import_upload").upload("settings.json", content, "application/json").run()
    assert not app.exception, app.exception
    if quality is None:
        assert "這不是有效的設定檔" in texts(app.error)
        return
    button(app, "套用到目前設定").click().run()
    assert not app.exception, app.exception
    assert "已套用" in texts(app.success)
    assert app.session_state["opt_quality"] == quality


def test_an_imported_file_cannot_take_a_builtin_presets_name() -> None:
    app = upload_settings(system_page_with({}), "最高畫質.json", {"quality": "360p"})
    button(app, "存成自訂組合").click().run()
    assert "內建組合" in texts(app.error)
    assert owner_services().storage.load_presets() == {}

    next(item for item in app.text_input if item.label == "組合名稱").set_value("我的 360p")
    button(app, "存成自訂組合").click().run()
    assert "已存成「我的 360p」" in texts(app.success)
    assert owner_services().storage.load_presets()["我的 360p"]["quality"] == "360p"


@pytest.mark.parametrize("public", [False, True])
def test_only_this_machines_owner_can_update_ytdlp(public: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    from videocap.tooling import UpdateCheck

    if public:
        monkeypatch.setenv("VIDEOCAP_MODE", "cloud")
        monkeypatch.delenv("VIDEOCAP_TRUST_REMOTE")
    # As if 「檢查新版本」 had found a newer release.
    app = system_page_with({"update_check": UpdateCheck("2020.01.01", "2099.01.01")})
    # A visitor could not update anyway, so the server does not ask PyPI for them either.
    assert ("有新版本" in texts(app.info)) is not public
    assert ("檢查新版本" in labels(app)) is not public
    assert ("更新 yt-dlp" in labels(app)) is not public


@pytest.mark.parametrize("public", [False, True])
def test_the_system_page_shows_visitors_only_what_they_can_use(public: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    from videocap.config import DEPLOY_MARKER, DEPLOY_VERIFY_MIN_COMMIT

    if public:
        monkeypatch.setenv("VIDEOCAP_MODE", "cloud")
        monkeypatch.delenv("VIDEOCAP_TRUST_REMOTE")
    app = system_page_with({})
    shown = texts(app.markdown) + texts(app.caption)
    # Which code runs here is the owner's business.
    assert ("部署概況" in shown) is not public
    assert (DEPLOY_VERIFY_MIN_COMMIT in shown) is not public and (DEPLOY_MARKER in shown) is not public
    assert "部署方式" in shown
    assert ("檢查環境、更新工具與備份設定。" in shown) is not public
    assert ("查看相依工具、測試連線，以及匯出或匯入下載設定。" in shown) is public
    # A visitor's 位置 would be blank on every row.
    assert ("位置" in app.dataframe[0].value.columns) is not public


def test_the_system_page_names_everything_in_chinese() -> None:
    app = system_page_with({})
    shown = texts(app.markdown)
    for english in ("Deployment Overview", "Runtime Commit", "Baseline Commit", "Deploy Marker"):
        assert english not in shown
    for chinese in ("部署概況", "執行中的版本", "基準版本", "部署標記"):
        assert chinese in shown
    assert "Deno（JavaScript）" in list(app.dataframe[0].value["項目"])


@pytest.fixture
def pip_runs(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Stands in for pip replacing yt-dlp, which would change this machine's own install."""
    import videocap.tooling as tooling
    from videocap.tooling import UpdateResult

    runs: list[str] = []

    def fake_install(timeout: float = 300, proxy: str = "") -> UpdateResult:
        runs.append(proxy)
        return UpdateResult(True, "Successfully installed yt-dlp-2099.1.1", restart_required=True)

    monkeypatch.setattr(services_module, "install_ytdlp_update", fake_install)
    monkeypatch.setattr(tooling, "install_ytdlp_update", fake_install)
    return runs


def offered_update(values: dict[str, Any] | None = None) -> AppTest:
    from videocap.tooling import UpdateCheck

    return system_page_with({"update_check": UpdateCheck("2020.01.01", "2099.01.01"), **(values or {})})


def test_ytdlp_is_not_replaced_while_videos_download(pip_runs: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    from videocap.services import YTDLP_UPDATE_BUSY_MESSAGE

    manager = owner_services().manager
    monkeypatch.setattr(manager, "hold", lambda kind: ["a running download"])
    app = offered_update()
    button(app, "更新 yt-dlp").click().run()
    assert not app.exception, app.exception
    assert YTDLP_UPDATE_BUSY_MESSAGE in texts(app.error)
    assert pip_runs == []
    assert not owner_services().restart_required


def test_after_updating_ytdlp_the_system_page_asks_for_a_restart(pip_runs: list[str]) -> None:
    app = offered_update({"opt_proxy": "http://127.0.0.1:7890"})
    button(app, "更新 yt-dlp").click().run()
    assert not app.exception, app.exception
    assert pip_runs == ["http://127.0.0.1:7890"]
    assert "更新完成。下載會先暫停，請立即重新啟動 App 才能繼續下載。" in texts(app.success)
    assert owner_services().restart_required

    # Every later visit says so too, until the restart.
    app = system_page_with({})
    assert "重新啟動" in texts(app.warning)


# 語音辨識 on the system page


@pytest.fixture
def speech_installs(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stands in for pip installing faster-whisper, which would change this machine's own install."""
    import videocap.speech as speech
    import videocap.ui.page_system as page_system
    from videocap.tooling import UpdateResult

    machine: dict[str, Any] = {
        "installed": False,
        "proxies": [],
        "result": UpdateResult(True, "Successfully installed faster-whisper-1.2.1"),
        "cuda": 0,
        "cublas": False,
        "gpu_proxies": [],
        "gpu_result": UpdateResult(True, "Successfully installed nvidia-cublas-cu12-12.9.2.10"),
        # Whether cuBLAS loads once pip is done; a driver too old for it would not.
        "cublas_after_install": True,
    }

    def fake_install(timeout: float = 900, proxy: str = "") -> UpdateResult:
        machine["proxies"].append(proxy)
        machine["installed"] = machine["result"].succeeded
        return machine["result"]

    def fake_gpu_install(timeout: float = 1800, proxy: str = "") -> UpdateResult:
        machine["gpu_proxies"].append(proxy)
        machine["cublas"] = machine["gpu_result"].succeeded and machine["cublas_after_install"]
        return machine["gpu_result"]

    def cublas_ready() -> bool:
        return machine["cublas"]

    # The real one remembers its answer, which the page clears after an install.
    cublas_ready.cache_clear = lambda: None  # type: ignore[attr-defined]
    monkeypatch.setattr(page_system, "install_speech_recognition", fake_install)
    monkeypatch.setattr(page_system, "install_gpu_support", fake_gpu_install)
    monkeypatch.setattr(speech, "is_installed", lambda: machine["installed"])
    monkeypatch.setattr(speech, "cuda_device_count", lambda: machine["cuda"])
    monkeypatch.setattr(speech, "cuda_libraries_ready", cublas_ready)
    return machine


def speech_section(app: AppTest) -> str:
    return texts(app.markdown) + texts(app.caption) + texts(app.info) + texts(app.success) + texts(app.error) + texts(app.warning)


def test_the_owner_installs_speech_recognition_from_the_system_page(speech_installs: dict[str, Any], tmp_path: Path) -> None:
    from videocap.speech import INSTALL_BUTTON_LABEL

    app = system_page_with({"opt_proxy": "http://127.0.0.1:7890"})
    assert "語音辨識" in texts(app.markdown) and "未安裝" in speech_section(app)
    button(app, INSTALL_BUTTON_LABEL).click().run()
    assert not app.exception, app.exception
    assert speech_installs["proxies"] == ["http://127.0.0.1:7890"]
    assert "不需要重新啟動" in texts(app.success)
    assert "Successfully installed faster-whisper-1.2.1" in texts(app.code)
    # Installed now, so the button is gone.
    assert "已安裝" in speech_section(app) and INSTALL_BUTTON_LABEL not in labels(app)
    assert str(tmp_path / "data" / "models" / "whisper") in speech_section(app)


def test_a_failed_speech_install_shows_what_pip_said(speech_installs: dict[str, Any]) -> None:
    from videocap.speech import INSTALL_BUTTON_LABEL
    from videocap.tooling import UpdateResult

    speech_installs["result"] = UpdateResult(False, "ERROR: No matching distribution found for faster-whisper>=1.1")
    app = system_page_with({})
    button(app, INSTALL_BUTTON_LABEL).click().run()
    assert not app.exception, app.exception
    assert "安裝失敗" in texts(app.error)
    assert "No matching distribution" in texts(app.code)
    assert INSTALL_BUTTON_LABEL in labels(app)


def test_a_speech_install_waits_for_another_pip(speech_installs: dict[str, Any]) -> None:
    from videocap.speech import INSTALL_BUTTON_LABEL
    from videocap.tooling import PIP_BUSY_MESSAGE, UpdateResult

    speech_installs["result"] = UpdateResult(False, PIP_BUSY_MESSAGE)
    app = system_page_with({})
    button(app, INSTALL_BUTTON_LABEL).click().run()
    assert PIP_BUSY_MESSAGE in texts(app.error) and texts(app.code) == ""


def test_a_ytdlp_update_waits_for_another_pip(monkeypatch: pytest.MonkeyPatch) -> None:
    from videocap.tooling import PIP_BUSY_MESSAGE, UpdateResult

    monkeypatch.setattr(services_module, "install_ytdlp_update", lambda timeout=300, proxy="": UpdateResult(False, PIP_BUSY_MESSAGE))
    app = offered_update()
    button(app, "更新 yt-dlp").click().run()
    assert not app.exception, app.exception
    assert PIP_BUSY_MESSAGE in texts(app.error) and texts(app.code) == ""
    assert not owner_services().restart_required


def test_a_speech_install_python_cannot_see_yet_asks_for_a_restart(speech_installs: dict[str, Any]) -> None:
    from videocap.speech import INSTALL_BUTTON_LABEL
    from videocap.tooling import UpdateResult

    speech_installs["result"] = UpdateResult(True, "Successfully installed faster-whisper-1.2.1", restart_required=True)
    speech_installs["installed"] = False
    app = system_page_with({})
    button(app, INSTALL_BUTTON_LABEL).click().run()
    assert not app.exception, app.exception
    assert "重新啟動" in texts(app.warning)


def test_the_system_page_lists_the_downloaded_speech_models(speech_installs: dict[str, Any], tmp_path: Path) -> None:
    speech_installs["installed"] = True
    downloaded_model(tmp_path / "data", "tiny", model_bytes=3 * 1024 * 1024 - 24)
    app = system_page_with({})
    shown = speech_section(app)
    assert "已安裝" in shown and "tiny" in shown and "3 MB" in shown
    assert str(tmp_path / "data" / "models" / "whisper") in shown


def test_the_owner_adds_gpu_support_when_cuda_sees_a_card_without_cublas(speech_installs: dict[str, Any]) -> None:
    from videocap.speech import GPU_INSTALL_BUTTON_LABEL

    speech_installs.update(installed=True, cuda=1)
    app = system_page_with({"opt_proxy": "http://127.0.0.1:7890"})
    assert "顯示卡支援：未安裝" in speech_section(app)
    button(app, GPU_INSTALL_BUTTON_LABEL).click().run()
    assert not app.exception, app.exception
    assert speech_installs["gpu_proxies"] == ["http://127.0.0.1:7890"]
    assert "顯示卡支援安裝完成" in texts(app.success) and "不需要重新啟動" in texts(app.success)
    assert "Successfully installed nvidia-cublas-cu12" in texts(app.code)
    assert "顯示卡支援：**已就緒**" in speech_section(app) and GPU_INSTALL_BUTTON_LABEL not in labels(app)


def test_gpu_support_that_still_cannot_load_says_why(speech_installs: dict[str, Any]) -> None:
    from videocap.speech import GPU_INSTALL_BUTTON_LABEL

    speech_installs.update(installed=True, cuda=1, cublas_after_install=False)
    app = system_page_with({})
    button(app, GPU_INSTALL_BUTTON_LABEL).click().run()
    assert not app.exception, app.exception
    assert "驅動程式" in texts(app.warning)
    assert "Successfully installed nvidia-cublas-cu12" in texts(app.code)


def test_a_failed_gpu_install_shows_what_pip_said(speech_installs: dict[str, Any]) -> None:
    from videocap.speech import GPU_INSTALL_BUTTON_LABEL
    from videocap.tooling import UpdateResult

    speech_installs.update(installed=True, cuda=1, gpu_result=UpdateResult(False, "ERROR: Could not install packages due to an OSError"))
    app = system_page_with({})
    button(app, GPU_INSTALL_BUTTON_LABEL).click().run()
    assert "安裝失敗" in texts(app.error) and "OSError" in texts(app.code)


@pytest.mark.parametrize(("cuda", "cublas", "shown"), [(0, False, ""), (1, True, "顯示卡支援：**已就緒**")])
def test_gpu_support_is_offered_only_for_a_card_that_lacks_it(speech_installs: dict[str, Any], cuda: int, cublas: bool, shown: str) -> None:
    from videocap.speech import GPU_INSTALL_BUTTON_LABEL

    speech_installs.update(installed=True, cuda=cuda, cublas=cublas)
    app = system_page_with({})
    assert GPU_INSTALL_BUTTON_LABEL not in labels(app)
    assert (shown in speech_section(app)) if shown else "顯示卡支援" not in speech_section(app)


@pytest.mark.parametrize("public", [False, True])
def test_only_this_machines_owner_sees_speech_recognition(public: bool, speech_installs: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    import videocap.speech as speech
    from videocap.speech import INSTALL_BUTTON_LABEL

    if public:
        monkeypatch.setenv("VIDEOCAP_MODE", "cloud")
        monkeypatch.delenv("VIDEOCAP_TRUST_REMOTE")
        monkeypatch.setattr(speech, "is_installed", lambda: pytest.fail("a visitor's page looked for faster-whisper"))
    app = system_page_with({})
    assert ("語音辨識" in texts(app.markdown)) is not public
    assert (INSTALL_BUTTON_LABEL in labels(app)) is not public



def test_a_second_app_on_the_same_data_folder_is_named(tmp_path: Path) -> None:
    from videocap.services import acquire_engine_lock, release_engine_lock

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    # As if the copy started at sign-in were already running.
    lock = acquire_engine_lock(data_dir)
    assert lock is not None
    try:
        app = system_page_with({})
        assert "已經有另一個 App 在使用同一個資料夾" in texts(app.warning)
        app = run_page("subscriptions")
        assert "自動檢查由那一個負責" in texts(app.warning)
    finally:
        release_engine_lock(lock)


def test_alone_on_its_data_folder_the_app_says_nothing_about_it() -> None:
    app = system_page_with({})
    assert "另一個 App" not in texts(app.warning)


@pytest.mark.skipif(sys.platform != "win32", reason="the hidden copy is started by Windows")
def test_the_restart_notice_says_how_to_restart_a_hidden_copy(pip_runs: list[str]) -> None:
    app = offered_update()
    button(app, "更新 yt-dlp").click().run()
    assert "工作管理員結束 pythonw.exe" in texts(app.caption)
    app = system_page_with({})
    assert "登出 Windows 再登入" in texts(app.caption)


def test_the_connection_test_goes_through_the_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    import videocap.ui.page_system as page_system

    networks: list[Any] = []

    def check_youtube_reachable(network: Any = None) -> tuple[bool, str]:
        networks.append(network)
        return True, "可以連上 YouTube。"

    monkeypatch.setattr(page_system, "check_youtube_reachable", check_youtube_reachable)
    app = system_page_with({"opt_proxy": "http://127.0.0.1:7890"})
    button(app, "測試連上 YouTube").click().run()
    assert not app.exception, app.exception
    assert [network.proxy for network in networks] == ["http://127.0.0.1:7890"]
    assert "可以連上 YouTube" in texts(app.success)


# Checking GitHub for a newer version of the app


RUNNING_COMMIT = "abc1234"
OTHER_SHA = "def5678" + "0" * 33


@pytest.fixture
def github(monkeypatch: pytest.MonkeyPatch) -> Any:
    # The test's own server, so no test can reach the real GitHub.
    return point_at_github_stand_in(monkeypatch)


@pytest.fixture
def running_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    import videocap.ui.page_system as page_system

    monkeypatch.setattr(page_system, "detect_runtime_commit_short", lambda: RUNNING_COMMIT)


def check_app(app: AppTest) -> AppTest:
    button(app, "檢查 App 新版本").click().run()
    assert not app.exception, app.exception
    return app


def test_a_newer_app_on_github_is_named_with_how_to_update(github: Any, running_commit: None) -> None:
    github.answer = latest_commit(OTHER_SHA, NEWEST_DATE)
    app = system_page_with({})
    # Nobody asked yet, so GitHub is not asked either.
    assert github.requests == []
    shown = texts(check_app(app).info)
    assert f"GitHub 上有新版本（最後更新 {local_day(NEWEST_DATE)}）：請重新下載 zip 或執行 git pull，再執行 start." in shown
    assert "https://github.com/jackalcn/YouTubeVideoCap" in shown
    assert "大略" not in shown
    assert len(github.requests) == 1


def test_the_running_app_is_named_the_newest(github: Any, running_commit: None) -> None:
    github.answer = latest_commit(RUNNING_COMMIT + "f" * 33, NEWEST_DATE)
    app = check_app(system_page_with({}))
    assert "已是最新版" in texts(app.success)
    assert "GitHub 上有新版本" not in texts(app.info)


def test_a_failed_app_check_says_why_on_the_page(github: Any, running_commit: None) -> None:
    github.answer = {"status": 403, "headers": {"X-RateLimit-Remaining": "0"}, "json": {"message": "API rate limit exceeded"}}
    app = check_app(system_page_with({}))
    assert "次數" in texts(app.error)


def test_the_app_check_is_kept_for_a_day_and_then_made_again_once(github: Any, running_commit: None) -> None:
    from videocap.ui.page_system import APP_UPDATE_CHECK_KEY

    github.answer = latest_commit(OTHER_SHA, NEWEST_DATE)
    check_app(system_page_with({}))
    assert len(github.requests) == 1
    # Within the day the page shows the answer without being asked and without asking GitHub.
    app = system_page_with({})
    assert "GitHub 上有新版本" in texts(app.info)
    assert len(github.requests) == 1

    storage = owner_services().storage
    storage.set_value(APP_UPDATE_CHECK_KEY, {**storage.get_value(APP_UPDATE_CHECK_KEY), "checked_at": time.time() - 25 * 60 * 60})
    github.answer = latest_commit(RUNNING_COMMIT + "f" * 33, NEWEST_DATE)
    app = system_page_with({})
    assert "已是最新版" in texts(app.success)
    assert len(github.requests) == 2
    # Once a day at most, even when the page is drawn again at once.
    app.run()
    assert len(github.requests) == 2


def test_a_failed_automatic_check_waits_a_day_too(github: Any, running_commit: None) -> None:
    from videocap.ui.page_system import APP_UPDATE_CHECK_KEY

    owner_services().storage.set_value(APP_UPDATE_CHECK_KEY, {"latest_commit": OTHER_SHA, "latest_date": "2026-11-01", "checked_at": 1.0})
    github.answer = {"status": 500, "json": {}}
    app = system_page_with({})
    assert "HTTP 500" in texts(app.error)
    system_page_with({})
    assert len(github.requests) == 1


def test_without_git_the_app_is_compared_by_date_and_says_so(github: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    import videocap.ui.page_system as page_system
    from videocap.config import DEPLOY_VERIFY_MIN_COMMIT

    # What detect_runtime_commit_short gives when neither git nor the host names the running commit.
    monkeypatch.setattr(page_system, "detect_runtime_commit_short", lambda: DEPLOY_VERIFY_MIN_COMMIT)
    monkeypatch.setattr(page_system, "APP_VERSION", "v2026.11.01.1")
    app = check_app(system_page_with({}))
    assert "GitHub 上有新版本" in texts(app.info) and "大略" in texts(app.info)

    monkeypatch.setattr(page_system, "APP_VERSION", "v2026.12.01.1")
    app = system_page_with({})
    assert "已是最新版" in texts(app.success) and "大略" in texts(app.success)


def test_the_app_check_goes_through_the_owners_proxy(monkeypatch: pytest.MonkeyPatch, running_commit: None) -> None:
    import videocap.ui.page_system as page_system
    from videocap.tooling import AppUpdateCheck

    proxies: list[str] = []

    def check_app_update(timeout: float = 10, proxy: str = "") -> AppUpdateCheck:
        proxies.append(proxy)
        return AppUpdateCheck(error="連不上 GitHub。", checked_at=time.time())

    monkeypatch.setattr(page_system, "check_app_update", check_app_update)
    check_app(system_page_with({"opt_proxy": "http://127.0.0.1:7890"}))
    assert proxies == ["http://127.0.0.1:7890"]


def test_visitors_get_no_app_check(github: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIDEOCAP_MODE", "cloud")
    monkeypatch.delenv("VIDEOCAP_TRUST_REMOTE")
    app = system_page_with({})
    assert "檢查 App 新版本" not in labels(app)
    assert github.requests == []


# The whole app


def open_page(app: AppTest, url_path: str) -> AppTest:
    """Moves to another page of the navigation, as clicking it would. AppTest.switch_page only knows page files."""
    from streamlit.util import calc_hash

    app._page_hash = calc_hash(url_path)
    app.run()
    assert not app.exception, app.exception
    return app


def failing_engine(is_public: bool) -> Any:
    raise PermissionError(r"[WinError 5] 存取被拒。: 'C:\Users\owner\.youtube-videocap\videocap.sqlite3'")


def test_a_startup_error_shows_visitors_no_server_folders(lan_visitor: None, monkeypatch: pytest.MonkeyPatch) -> None:
    import videocap.ui.context as context

    monkeypatch.setattr(context, "get_shared_services", failing_engine)
    app = run_main_app()
    assert not app.exception
    assert "發生錯誤" in texts(app.error)
    assert "youtube-videocap" not in texts(app.error) + texts(app.markdown)


def test_a_startup_error_shows_the_owner_what_went_wrong(monkeypatch: pytest.MonkeyPatch) -> None:
    import videocap.ui.context as context

    monkeypatch.setattr(context, "get_shared_services", failing_engine)
    app = AppTest.from_file(str(ROOT / "main.py"), default_timeout=30)
    app.run()
    assert "存取被拒" in texts(app.exception)


def test_a_damaged_database_is_named_once(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "videocap.sqlite3").write_bytes(b"this is not a database" * 200)
    app = run_main_app()
    warning = texts(app.warning)
    assert "資料庫檔案已損壞" in warning
    moved = owner_services().storage.recovered_from
    assert moved is not None and moved.name in warning

    app.run()
    assert not app.exception, app.exception
    assert "資料庫檔案已損壞" not in texts(app.warning)


def test_a_damaged_database_is_not_named_to_visitors(lan_visitor: None, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "videocap.sqlite3").write_bytes(b"this is not a database" * 200)
    app = run_main_app()
    assert "資料庫檔案已損壞" not in texts(app.warning)


def test_an_expired_prepared_download_is_dropped_on_a_page_without_a_job_list() -> None:
    import time

    from videocap.ui import common

    ticket = common.reserve_download_memory(1000)
    app = AppTest.from_file(str(ROOT / "main.py"), default_timeout=30)
    app.session_state[common.PREPARED_DOWNLOAD_KEY] = {"key": "k", "data": b"x" * 1000, "ticket": ticket, "expires": time.time() - 1}
    open_page(app, "system")
    assert common.PREPARED_DOWNLOAD_KEY not in app.session_state
    assert ticket not in common._download_memory


# Visitors: someone else on the network of a private install, and anyone on a public host


def video_links(count: int) -> str:
    return "\n".join(f"https://www.youtube.com/watch?v=vid{number:08d}" for number in range(count))


def run_main_app() -> AppTest:
    app = AppTest.from_file(str(ROOT / "main.py"), default_timeout=30)
    app.run()
    assert not app.exception, app.exception
    return app


@pytest.fixture
def lan_visitor(monkeypatch: pytest.MonkeyPatch) -> None:
    """A private install opened from another device: the services keep saved data, but the visitor is not the owner.
    AppTest never looks like this machine itself, so without VIDEOCAP_TRUST_REMOTE every session is such a visitor."""
    monkeypatch.delenv("VIDEOCAP_TRUST_REMOTE")


def pages_script() -> None:
    import streamlit as st

    from videocap.ui.app import build_pages
    from videocap.ui.context import build_context

    ctx = build_context()
    st.text(f"{ctx.trusted} {','.join(build_pages(ctx))}")


def test_a_lan_visitor_gets_only_the_public_pages(lan_visitor: None) -> None:
    app = AppTest.from_function(pages_script, default_timeout=30)
    app.run()
    assert not app.exception, app.exception
    assert app.text[0].value == "False download,queue,search,tools,system"


def test_a_lan_visitors_downloads_stay_out_of_the_owners_data(lan_visitor: None, monkeypatch: pytest.MonkeyPatch) -> None:
    import threading
    import time

    from videocap.storage import LAST_SETTINGS_KEY, PENDING_JOBS_KEY

    storage = owner_services().storage
    storage.set_value(LAST_SETTINGS_KEY, {"quality": "720p"})
    release = threading.Event()

    def held_download(*args: Any) -> Any:
        release.wait(10)
        return fake_download(*args)

    monkeypatch.setattr(services_module, "run_download", held_download)
    app = run_main_app()
    # The owner's last settings are not the visitor's starting point.
    assert app.selectbox(key="opt_quality").value != "720p"
    app.text_area(key="keep_url_input").set_value("https://www.youtube.com/watch?v=jNQXAC9IVRw").run()
    button(app, "開始下載").click().run()
    assert not app.exception, app.exception
    jobs = owner_services().manager.list_jobs()
    assert len(jobs) == 1 and jobs[0].owner != "local"
    # A visitor's job must not come back after a restart as one of the owner's.
    assert storage.get_value(PENDING_JOBS_KEY, []) == []

    release.set()
    deadline = time.time() + 10
    while time.time() < deadline and owner_services().manager.active_jobs():
        time.sleep(0.02)
    assert storage.list_history() == []
    assert storage.get_value(LAST_SETTINGS_KEY) == {"quality": "720p"}


def test_a_lan_visitors_presets_stay_in_their_session(lan_visitor: None) -> None:
    app = run_main_app()
    app.text_input(key="preset_new_name").set_value("訪客組合")
    button(app, "把目前設定存成組合").click().run()
    assert not app.exception, app.exception
    assert "訪客組合" in app.selectbox(key="preset_choice").options
    assert owner_services().storage.load_presets() == {}


@pytest.fixture
def public_visitor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIDEOCAP_MODE", "cloud")
    monkeypatch.delenv("VIDEOCAP_TRUST_REMOTE")


def public_services() -> Any:
    from videocap.ui.context import get_services

    return get_services(True)


def test_a_public_visitor_can_inspect_only_a_few_links_at_once(public_visitor: None, monkeypatch: pytest.MonkeyPatch) -> None:
    import videocap.ui.page_download as page_download
    from videocap.config import CLOUD_MAX_INSPECT_LINKS

    inspected: list[str] = []
    monkeypatch.setattr(page_download, "inspect_link", lambda url, *args, **kwargs: inspected.append(url))
    app = run_main_app()
    app.text_area(key="keep_url_input").set_value(video_links(CLOUD_MAX_INSPECT_LINKS + 1)).run()
    button(app, "解析連結（不下載）").click().run()
    assert not app.exception, app.exception
    assert f"一次最多解析 {CLOUD_MAX_INSPECT_LINKS} 個連結" in texts(app.error)
    assert inspected == []


def test_a_public_visitors_queue_has_a_limit(public_visitor: None) -> None:
    from videocap.config import CLOUD_MAX_PENDING_PER_VISITOR
    from videocap.jobs import Job, new_job_id
    from videocap.ui.common import QUEUE_FULL_MESSAGE

    app = run_main_app()
    owner = app.session_state["session_owner"]
    manager = public_services().manager
    for _ in range(CLOUD_MAX_PENDING_PER_VISITOR - 1):
        manager.add_paused(Job(id=new_job_id(), owner=owner, kind="download", label="x", payload=None))
    app.text_area(key="keep_url_input").set_value(video_links(2)).run()
    button(app, "開始下載").click().run()
    assert not app.exception, app.exception
    assert QUEUE_FULL_MESSAGE in texts(app.error)
    assert len(manager.list_jobs(owner)) == CLOUD_MAX_PENDING_PER_VISITOR - 1


def test_a_public_visitors_imported_wait_is_cut_to_what_the_host_allows(public_visitor: None) -> None:
    from videocap.config import CLOUD_MAX_WAIT_SECONDS

    app = AppTest.from_file(str(ROOT / "main.py"), default_timeout=30)
    open_page(app, "system")
    # A settings file made on someone's own computer, where waiting ten minutes between videos is fine.
    upload_settings(app, "settings.json", {"sleep_seconds": 600})
    button(app, "套用到目前設定").click().run()
    open_page(app, "download")
    box = app.number_input(key="opt_sleep_seconds")
    assert (box.value, box.max) == (CLOUD_MAX_WAIT_SECONDS, CLOUD_MAX_WAIT_SECONDS)


def test_choosing_cookies_without_a_file_is_explained(public_visitor: None) -> None:
    app = run_main_app()
    app.radio(key="keep_login_method").set_value("cookies").run()
    app.text_area(key="keep_url_input").set_value(video_links(1)).run()
    button(app, "開始下載").click().run()
    assert not app.exception, app.exception
    assert "請先在「會員登入」上傳 cookies.txt" in texts(app.error)
    assert public_services().manager.list_jobs() == []


def test_a_restored_job_that_needs_a_login_waits_for_one() -> None:
    from videocap.jobs import PAUSED, Job, new_job_id
    from videocap.options import AccessLimits
    from videocap.services import DownloadPayload
    from videocap.settings import DownloadSettings, JobSpec

    # Restored after a restart: the uploaded cookies were never saved, so the job no longer has them.
    spec = JobSpec("https://www.youtube.com/watch?v=jNQXAC9IVRw", DownloadSettings(), "out", needs_cookie_file=True)
    job = Job(id=new_job_id(), owner="local", kind="download", label="members only", payload=DownloadPayload(spec, AccessLimits()))
    owner_services().manager.add_paused(job)
    app = run_page("queue")
    button(app, "繼續").click().run()
    assert not app.exception, app.exception
    assert job.state == PAUSED
    assert "需要登入" in job.status_text


@pytest.mark.parametrize("public", [False, True])
def test_a_missing_deno_can_be_installed_from_the_system_page(public: bool, pip_runs: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    import videocap.ui.page_system as page_system

    monkeypatch.setattr(page_system, "get_js_runtime_status", lambda: (False, "找不到 deno"))
    if public:
        monkeypatch.setenv("VIDEOCAP_MODE", "cloud")
        monkeypatch.delenv("VIDEOCAP_TRUST_REMOTE")
    app = system_page_with({})
    # The download error's advice names this button, so it must exist even when yt-dlp itself is current.
    assert (page_system.INSTALL_DENO_LABEL in labels(app)) is not public
    if public:
        return
    button(app, page_system.INSTALL_DENO_LABEL).click().run()
    assert not app.exception, app.exception
    assert pip_runs == [""]
    assert "更新完成" in texts(app.success)


def test_in_docker_the_restart_advice_names_the_container(pip_runs: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    import videocap.ui.page_system as page_system

    monkeypatch.setenv("VIDEOCAP_IN_DOCKER", "1")
    app = offered_update()
    button(app, "更新 yt-dlp").click().run()
    assert not app.exception, app.exception
    assert page_system.DOCKER_RESTART_STEPS in texts(app.caption)

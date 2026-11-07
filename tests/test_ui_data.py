"""The owner's data pages: editing, importing and pruning subscriptions, backups, notifications and autostart."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from streamlit.testing.v1 import AppTest

from test_ui_admin import button, isolated_app, new_page, owner_services, run_page, texts  # noqa: F401
# local_telegram points Telegram at the test's own server, so no test can reach the real one.
from test_notify import local_telegram, service  # noqa: F401
from videocap.settings import DownloadSettings


CHANNEL = "https://www.youtube.com/@somechannel/videos"


def page_with(page_name: str, values: dict[str, Any]) -> AppTest:
    app = new_page(page_name)
    app.session_state["settings_loaded"] = True
    for key, value in values.items():
        app.session_state[key] = value
    app.run()
    assert not app.exception, app.exception
    return app


# Editing a subscription


def saved_subscription(tmp_path: Path, **settings: Any) -> int:
    return owner_services().storage.add_subscription(CHANNEL, "Some channel", DownloadSettings(**settings), str(tmp_path / "old"), 5, 24)


def edit(app: AppTest, subscription_id: int, **values: Any) -> AppTest:
    for name, value in values.items():
        widget = {"title": app.text_input, "folder": app.text_input, "interval": app.selectbox, "max_items": app.number_input}.get(
            name, app.checkbox
        )
        widget(key=f"subscription_edit_{name}_{subscription_id}").set_value(value)
    button(app, "儲存").click().run()
    assert not app.exception, app.exception
    return app


def test_editing_a_subscription_keeps_its_download_settings(tmp_path: Path) -> None:
    subscription_id = saved_subscription(tmp_path, quality="720p")
    app = page_with("subscriptions", {"opt_quality": "360p"})
    new_folder = tmp_path / "news"
    edit(app, subscription_id, title="新聞台", interval=6, max_items=12, folder=f'"{new_folder}"')
    assert "已儲存「新聞台」的設定" in texts(app.success)

    saved = owner_services().storage.get_subscription(subscription_id)
    assert (saved.title, saved.interval_hours, saved.max_items) == ("新聞台", 6, 12)
    assert saved.output_dir == str(new_folder.resolve())
    assert saved.settings.quality == "720p"
    assert "每 6 小時" in texts(app.caption)


def test_a_subscription_can_take_the_sidebars_settings(tmp_path: Path) -> None:
    subscription_id = saved_subscription(tmp_path, quality="720p")
    app = page_with("subscriptions", {"opt_quality": "360p", "opt_folder_rule": "none", "opt_time_ranges": "0:10-0:20"})
    edit(app, subscription_id, use_sidebar=True, folder=str(tmp_path / "old"))
    settings = owner_services().storage.get_subscription(subscription_id).settings
    assert settings.quality == "360p"
    # As when adding: without a folder rule each channel still gets a folder of its own.
    assert settings.folder_rule == "channel"


def test_an_edit_with_a_setting_that_cannot_run_is_refused(tmp_path: Path) -> None:
    subscription_id = saved_subscription(tmp_path, quality="720p")
    app = page_with("subscriptions", {"opt_quality": "360p", "opt_filter_date_after": "not-a-date"})
    edit(app, subscription_id, use_sidebar=True, max_items=9)
    assert "看不懂日期" in texts(app.error)
    saved = owner_services().storage.get_subscription(subscription_id)
    assert (saved.settings.quality, saved.max_items) == ("720p", 5)

    edit(app, subscription_id, use_sidebar=False, folder="   ")
    assert "請輸入下載資料夾" in texts(app.error)
    assert owner_services().storage.get_subscription(subscription_id).output_dir == str(tmp_path / "old")


# Importing subscriptions

FIRST = "UCX6OQ3DkcsbYNE6H8uQQuVA"
SECOND = "UC-lHJZR3Gqxm24_Vd_AJ5Yw"
THIRD = "UCq-Fj5jknLsUf-MWSy4_brA"


@pytest.fixture
def channel_reads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    import videocap.services as services_module

    reads: list[str] = []

    def list_recent_video_ids(url: str, limit: int, login: Any, network: Any = None) -> tuple[str, list[str]]:
        reads.append(url)
        return "From YouTube", [f"{url[-20:-7]}{index}" for index in range(2)]

    monkeypatch.setattr(services_module, "list_recent_video_ids", list_recent_video_ids)
    monkeypatch.setattr(services_module, "SUBSCRIPTION_IMPORT_PAUSE_SECONDS", 0.05)
    return reads


def wait_for_import() -> None:
    import time

    deadline = time.time() + 10
    while not owner_services().subscription_import.finished:
        assert time.time() < deadline
        time.sleep(0.02)


def test_a_takeout_list_is_imported_in_the_background(tmp_path: Path, channel_reads: list[str]) -> None:
    storage = owner_services().storage
    storage.add_subscription(f"https://www.youtube.com/channel/{THIRD}/videos", "Already here", DownloadSettings(), "out", 5, 24)
    csv_text = (
        "Channel Id,Channel Url,Channel Title\n"
        f"{FIRST},http://www.youtube.com/channel/{FIRST},頻道一\n"
        f"{SECOND},http://www.youtube.com/channel/{SECOND},頻道二\n"
        f"{THIRD},http://www.youtube.com/channel/{THIRD},頻道三\n"
    )
    app = page_with("subscriptions", {"opt_quality": "480p", "keep_output_dir": str(tmp_path / "subs")})
    app.file_uploader(key="subscription_import_upload").upload("subscriptions.csv", csv_text.encode("utf-8-sig"), "text/csv").run()
    assert not app.exception, app.exception
    assert "有 1 個頻道已經訂閱" in texts(app.caption)
    picks = [box for box in app.checkbox if box.key.startswith("subscription_import_pick_")]
    assert [box.label for box in picks] == ["頻道一", "頻道二"]

    picks[1].uncheck().run()
    button(app, "加入 1 個訂閱").click().run()
    assert not app.exception, app.exception
    wait_for_import()
    # Another visit, as if the person came back later.
    app = page_with("subscriptions", {})
    assert "匯入完成：加入 1 個訂閱" in texts(app.success)
    added = next(subscription for subscription in storage.list_subscriptions() if subscription.title == "頻道一")
    assert added.url == f"https://www.youtube.com/channel/{FIRST}/videos"
    assert added.output_dir == str((tmp_path / "subs").resolve()) and added.settings.quality == "480p"
    assert channel_reads == [added.url]

    button(app, "關閉匯入結果").click().run()
    assert owner_services().subscription_import is None


def test_a_file_that_is_not_a_subscription_list_is_explained() -> None:
    app = page_with("subscriptions", {})
    app.file_uploader(key="subscription_import_upload").upload("notes.csv", b"name,phone\nAmy,0912\n", "text/csv").run()
    assert not app.exception, app.exception
    assert "Google Takeout" in texts(app.error)


def test_a_running_import_shows_its_progress_and_can_be_stopped(channel_reads: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    import videocap.services as services_module
    from videocap.options import AccessLimits
    from videocap.subscription_import import ImportedChannel

    monkeypatch.setattr(services_module, "SUBSCRIPTION_IMPORT_PAUSE_SECONDS", 30)
    progress = owner_services().start_subscription_import(
        [ImportedChannel(FIRST, "頻道一"), ImportedChannel(SECOND, "頻道二")], DownloadSettings(), "out", 5, 24, True, AccessLimits()
    )
    import time

    deadline = time.time() + 10
    while progress.added != ["頻道一"]:
        assert time.time() < deadline
        time.sleep(0.02)
    app = page_with("subscriptions", {})
    assert "匯入中 1／2" in " ".join(bar.proto.text for bar in app.get("progress"))
    # Nothing else can be imported meanwhile.
    assert app.get("file_uploader") == []

    app.button(key="subscription_import_stop").click().run()
    wait_for_import()
    app = page_with("subscriptions", {})
    assert "1 個因停止而未處理" in texts(app.success)


# Retention rules


def test_a_retention_rule_shows_what_it_deletes_before_it_is_saved(tmp_path: Path) -> None:
    import time

    storage = owner_services().storage
    subscription_id = saved_subscription(tmp_path)
    folder = tmp_path / "old" / "Some channel"
    folder.mkdir(parents=True)
    for name, age_days in (("老影片", 40), ("新影片", 1)):
        (folder / f"{name}.mp4").write_bytes(b"video")
        storage.add_history(
            # A real download records its size, which retention checks before it deletes the file.
            [
                {
                    "video_id": name,
                    "title": name,
                    "path": str(folder / f"{name}.mp4"),
                    "size_bytes": 5,
                    "finished_at": time.time() - age_days * 86400,
                    "subscription_id": subscription_id,
                }
            ]
        )

    app = page_with("subscriptions", {})
    assert "現在套用會刪除" not in texts(app.warning)
    app.radio(key=f"subscription_retention_mode_{subscription_id}").set_value("days").run()
    assert "現在套用會刪除 1 部：老影片" in texts(app.warning)
    assert (folder / "老影片.mp4").exists()

    button(app, "儲存並刪除 1 部").click().run()
    assert not app.exception, app.exception
    assert "已刪除 1 部影片" in texts(app.success)
    assert not (folder / "老影片.mp4").exists() and (folder / "新影片.mp4").exists()
    assert storage.get_subscription(subscription_id).keep_days == 30
    assert "刪除下載超過 30 天的影片" in texts(app.caption)


def test_a_retention_rule_deletes_only_what_the_page_showed(tmp_path: Path) -> None:
    import time

    storage = owner_services().storage
    subscription_id = saved_subscription(tmp_path)
    folder = tmp_path / "old" / "Some channel"
    folder.mkdir(parents=True)

    def downloaded(name: str, age_days: float) -> Path:
        path = folder / f"{name}.mp4"
        path.write_bytes(b"video")
        storage.add_history([{"video_id": name, "title": name, "path": str(path), "finished_at": time.time() - age_days * 86400, "subscription_id": subscription_id}])
        return path

    shown = downloaded("老影片", 40)
    app = page_with("subscriptions", {})
    app.radio(key=f"subscription_retention_mode_{subscription_id}").set_value("days").run()
    assert "現在套用會刪除 1 部：老影片" in texts(app.warning)
    # A check finishes while the person reads the list, adding a file the rule would also take.
    unseen = downloaded("剛下載但很舊", 50)
    button(app, "儲存並刪除 1 部").click().run()
    assert not app.exception, app.exception
    assert shown.exists() and unseen.exists()
    assert "請看過清單後再按一次" in texts(app.warning)
    assert "現在套用會刪除 2 部" in texts(app.warning)

    button(app, "儲存並刪除 2 部").click().run()
    assert "已刪除 2 部影片" in texts(app.success)
    assert not shown.exists() and not unseen.exists()


# Backups


def download_labels(app: AppTest) -> list[str]:
    return [element.proto.label for element in app.get("download_button")]


def test_the_owner_can_restore_a_backup_from_the_system_page() -> None:
    from videocap.options import AccessLimits

    storage = owner_services().storage
    storage.add_subscription(CHANNEL, "備份前的訂閱", DownloadSettings(), "out", 5, 24)
    backup = owner_services().create_backup(AccessLimits())
    storage.delete_subscription(storage.list_subscriptions()[0].id)

    app = page_with("system", {})
    assert "備份全部資料" in download_labels(app)
    app.file_uploader(key="backup_restore_upload").upload("videocap-backup.zip", backup, "application/zip").run()
    assert "會取代目前的下載紀錄" in texts(app.warning)
    app.button(key="backup_restore_confirm").click().run()
    assert not app.exception, app.exception
    assert "已還原備份" in texts(app.success)
    assert [subscription.title for subscription in storage.list_subscriptions()] == ["備份前的訂閱"]

    app.file_uploader(key="backup_restore_upload").upload("photo.zip", b"not a zip", "application/zip").run()
    app.button(key="backup_restore_confirm").click().run()
    assert "不是這個 App 的備份檔" in texts(app.error)


def test_the_backup_section_says_what_is_left_out_and_what_a_restore_brings_back() -> None:
    app = page_with("system", {})
    captions = texts(app.caption)
    assert "代理伺服器的帳號密碼與進階 yt-dlp 參數裡的登入資料不會放進備份。" in captions
    # A backup from someone else could bring --exec back with its settings.
    assert "只還原你自己建立的備份：還原會帶回備份裡的所有設定，包括進階 yt-dlp 參數。" in captions


def test_a_restore_needs_its_own_click_for_each_file() -> None:
    from videocap.options import AccessLimits

    storage = owner_services().storage
    storage.add_subscription(CHANNEL, "備份裡的訂閱", DownloadSettings(), "out", 5, 24)
    backup = owner_services().create_backup(AccessLimits())
    storage.delete_subscription(storage.list_subscriptions()[0].id)

    app = page_with("system", {})
    app.file_uploader(key="backup_restore_upload").upload("photo.zip", b"not a zip", "application/zip").run()
    app.button(key="backup_restore_confirm").click().run()
    assert "不是這個 App 的備份檔" in texts(app.error)
    # Choosing another file after a confirmed attempt restores nothing until 確定還原 is pressed again.
    app.file_uploader(key="backup_restore_upload").upload("videocap-backup.zip", backup, "application/zip").run()
    assert not app.exception, app.exception
    assert not app.error and not app.success
    assert storage.list_subscriptions() == []
    app.button(key="backup_restore_confirm").click().run()
    assert [subscription.title for subscription in storage.list_subscriptions()] == ["備份裡的訂閱"]


def test_a_download_archive_is_imported_from_the_system_page() -> None:
    app = page_with("system", {})
    upload = "youtube dQw4w9WgXcQ\nsoundcloud 12345\n".encode("utf-8")
    app.file_uploader(key="archive_import_upload").upload("archive.txt", upload, "text/plain").run()
    app.button(key="archive_import_confirm").click().run()
    assert not app.exception, app.exception
    assert "已加入 1 部影片，略過 1 行" in texts(app.success)
    assert owner_services().storage.archived_video_ids() == {"dQw4w9WgXcQ"}

    app.file_uploader(key="archive_import_upload").upload("notes.txt", b"hello\n", "text/plain").run()
    app.button(key="archive_import_confirm").click().run()
    assert "沒有找到 YouTube 影片的紀錄" in texts(app.error)


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("youtube dQw4w9WgXcQ\r\nyoutube jNQXAC9IVRw\r\n".encode("utf-16"), "已加入 2 部影片"),
        ("youtube dQw4w9WgXcQ\n# 備註\n".encode("cp950"), "已加入 1 部影片，略過 1 行"),
        (b"", "沒有找到 YouTube 影片的紀錄"),
        (b"PK\x03\x04\x00\x00\xff\xfe binary", "沒有找到 YouTube 影片的紀錄"),
        (b"youtube dQw4w9WgXcQ\n" * 300_000, "已加入 1 部影片"),
    ],
)
def test_any_download_archive_file_is_read_or_explained(content: bytes, message: str) -> None:
    app = page_with("system", {})
    app.file_uploader(key="archive_import_upload").upload("archive.txt", content, "text/plain").run()
    app.button(key="archive_import_confirm").click().run()
    assert not app.exception, app.exception
    assert message in texts(app.success) + texts(app.error)


def test_visitors_get_no_backups(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VIDEOCAP_TRUST_REMOTE")
    app = page_with("system", {})
    assert "備份全部資料" not in download_labels(app)
    assert not [widget for widget in app.get("file_uploader") if widget.proto.label.startswith(("備份檔", "下載紀錄檔"))]


# Notifications


def all_text(app: AppTest) -> str:
    """Everything the page sent to the browser, so a test can tell that a secret never left the server."""
    return str(app._tree)


def test_notification_targets_are_saved_and_never_shown_whole(service: Any) -> None:  # noqa: F811 (the fixture)
    from test_notify import BASE, TOKEN

    app = page_with("system", {})
    assert button(app, "傳送測試通知").disabled
    app.text_input(key="notify_discord_webhook").set_value("https://example.com/not-discord")
    button(app, "儲存通知設定").click().run()
    assert "Discord Webhook 網址不對" in texts(app.error)
    assert not owner_services().notify_targets().is_set

    app.text_input(key="notify_discord_webhook").set_value("")
    app.text_input(key="notify_telegram_token").set_value(TOKEN)
    app.text_input(key="notify_telegram_chat_id").set_value("123456789")
    app.text_input(key="notify_ntfy_url").set_value(f"{BASE}/videocap")
    button(app, "儲存通知設定").click().run()
    assert not app.exception, app.exception
    assert "已儲存通知設定" in texts(app.success)
    assert owner_services().notify_targets().telegram_token == TOKEN

    app = page_with("system", {})
    assert app.text_input(key="notify_telegram_token").value == ""
    assert f"…{TOKEN[-4:]}" in app.text_input(key="notify_telegram_token").placeholder
    assert TOKEN not in all_text(app) and TOKEN[:20] not in all_text(app)

    button(app, "傳送測試通知").click().run()
    assert "已送出測試通知" in texts(app.success)
    assert sorted(request["path"].split("?")[0] for request in service.wait_for(2)) == [f"/bot{TOKEN}/sendMessage", "/videocap"]

    # Left empty, a field keeps what was saved; ticking 移除 clears it.
    app.checkbox(key="notify_remove_ntfy_url").check()
    button(app, "儲存通知設定").click().run()
    saved = owner_services().notify_targets()
    assert (saved.telegram_token, saved.ntfy_url) == (TOKEN, "")


def browser_holds(app: AppTest, held: dict[str, str]) -> dict[str, str]:
    """AppTest forgets typed text after each run, but a browser keeps what a field holds unless the page sets it.
    Puts that back, as the browser would send it with the next save."""
    for widget in app.text_input:
        if widget.key in held:
            if widget.proto.set_value:
                held[widget.key] = widget.value
            else:
                widget.set_value(held[widget.key])
    return held


def test_a_saved_or_removed_notification_target_leaves_its_field_empty() -> None:
    from test_notify import BASE

    # Never sent anywhere: the test only saves it.
    webhook = "https://discord.com/api/webhooks/123456789012345678/FAKEtokenFAKEtokenFAKEtoken-zZ9x"
    app = page_with("system", {})
    held = {f"notify_{name}": "" for name in ("discord_webhook", "telegram_token", "telegram_chat_id", "ntfy_url")}
    app.text_input(key="notify_discord_webhook").set_value(webhook)
    held["notify_discord_webhook"] = webhook
    button(app, "儲存通知設定").click().run()
    assert not app.exception, app.exception
    assert "已儲存通知設定" in texts(app.success)
    browser_holds(app, held)
    # Otherwise the whole webhook stays in the field, one click on the eye icon away.
    assert held["notify_discord_webhook"] == ""
    assert "…zZ9x" in app.text_input(key="notify_discord_webhook").placeholder

    app.checkbox(key="notify_remove_discord_webhook").check()
    button(app, "儲存通知設定").click().run()
    browser_holds(app, held)
    assert owner_services().notify_targets().discord_webhook == ""

    # The next save sends whatever the fields still hold, which must not bring the removed webhook back.
    app.text_input(key="notify_ntfy_url").set_value(f"{BASE}/videocap")
    button(app, "儲存通知設定").click().run()
    assert not app.exception, app.exception
    saved = owner_services().notify_targets()
    assert (saved.discord_webhook, saved.ntfy_url) == ("", f"{BASE}/videocap")


def test_visitors_get_no_notification_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VIDEOCAP_TRUST_REMOTE")
    app = page_with("system", {})
    assert "傳送測試通知" not in [item.label for item in app.button]
    assert not [widget for widget in app.text_input if widget.key and widget.key.startswith("notify_")]


# Starting with Windows


@pytest.mark.skipif(sys.platform != "win32", reason="the Startup folder is Windows'")
def test_starting_with_windows_is_switched_on_the_system_page(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import streamlit as st

    import videocap.environment as environment
    from test_ui_admin import FakeContext

    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData"))
    app_dir = tmp_path / "app"
    (app_dir / ".venv" / "Scripts").mkdir(parents=True)
    (app_dir / ".venv" / "Scripts" / "pythonw.exe").write_bytes(b"")
    monkeypatch.setattr(environment, "APP_DIR", app_dir)
    monkeypatch.setattr(st, "context", FakeContext({"Host": "localhost:8501"}))

    app = page_with("system", {})
    assert "目前：未開啟" in texts(app.markdown)
    app.button(key="autostart_toggle").click().run()
    assert not app.exception, app.exception
    assert "已開啟" in texts(app.success) and "目前：**已開啟**" in texts(app.markdown)
    assert environment.autostart_shortcut().exists()
    assert environment.autostart_shortcut().is_relative_to(tmp_path)

    app.button(key="autostart_toggle").click().run()
    assert "已關閉" in texts(app.success)
    assert not environment.autostart_shortcut().exists()


def test_starting_with_windows_is_offered_only_at_this_machine() -> None:
    # The owner over the network, as with VIDEOCAP_TRUST_REMOTE: it is not their Windows sign-in.
    app = page_with("system", {})
    assert "autostart_toggle" not in [item.key for item in app.button]

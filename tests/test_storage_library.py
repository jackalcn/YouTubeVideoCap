from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import pytest

from videocap.library import delete_entry, find_partial_files, is_inside, is_intermediate_stream, scan_library
from videocap.settings import DownloadSettings
from videocap.storage import DOWNLOAD_WINDOW_KEY, AppStorage, DownloadWindow


def history_entry(video_id: str, title: str = "T", **extra: object) -> dict[str, object]:
    return {"video_id": video_id, "title": title, "channel": "C", "url": "u", "path": f"{video_id}.mp4", "size_bytes": 10, **extra}


def test_values_survive_a_reopen(tmp_path: Path) -> None:
    AppStorage(tmp_path).set_value("key", {"a": [1, "二"]})
    assert AppStorage(tmp_path).get_value("key") == {"a": [1, "二"]}
    assert AppStorage(tmp_path).get_value("missing", "fallback") == "fallback"


def test_presets(tmp_path: Path) -> None:
    storage = AppStorage(tmp_path)
    storage.save_preset("我的", DownloadSettings(quality="720p"))
    assert storage.load_presets()["我的"]["quality"] == "720p"
    storage.delete_preset("我的")
    assert storage.load_presets() == {}


def test_history_search_and_totals(tmp_path: Path) -> None:
    storage = AppStorage(tmp_path)
    storage.add_history([history_entry("a", "貓咪影片", finished_at=100.0), history_entry("b", "dog video", finished_at=200.0)])
    assert [entry.video_id for entry in storage.list_history()] == ["b", "a"]
    assert [entry.video_id for entry in storage.list_history("貓")] == ["a"]
    assert storage.history_totals() == (2, 20)

    storage.delete_history([storage.list_history("貓")[0].id])
    assert [entry.video_id for entry in storage.list_history()] == ["b"]
    storage.clear_history()
    assert storage.list_history() == []


def test_downloading_to_the_same_path_replaces_its_entry(tmp_path: Path) -> None:
    storage = AppStorage(tmp_path)
    storage.add_history([history_entry("a", "first", finished_at=1.0)])
    storage.add_history([history_entry("a", "again", finished_at=2.0)])
    assert [entry.title for entry in storage.list_history()] == ["again"]

    # Sidecar-only downloads have no path, and they are separate downloads.
    storage.add_history([history_entry("b", path="")])
    storage.add_history([history_entry("c", path="")])
    assert len(storage.list_history()) == 3


def test_history_search_is_not_sql(tmp_path: Path) -> None:
    storage = AppStorage(tmp_path)
    storage.add_history([history_entry("a")])
    assert storage.list_history("' OR '1'='1") == []
    assert len(storage.list_history()) == 1


def test_find_downloads_returns_the_latest(tmp_path: Path) -> None:
    storage = AppStorage(tmp_path)
    storage.add_history([history_entry("a", "old", finished_at=1.0), history_entry("a", "new", finished_at=2.0)])
    assert storage.find_downloads(["a", "b", ""])["a"].title == "new"
    assert storage.find_downloads([]) == {}


def test_subscriptions(tmp_path: Path) -> None:
    storage = AppStorage(tmp_path)
    subscription_id = storage.add_subscription("https://www.youtube.com/@a", "A", DownloadSettings(quality="720p"), "out", 5, 6)
    subscription = storage.get_subscription(subscription_id)
    assert subscription is not None
    assert subscription.settings.quality == "720p"

    now = time.time()
    assert subscription.is_due(now)
    storage.update_subscription(subscription_id, last_checked_at=now, last_result="ok", not_a_column="x")
    subscription = storage.get_subscription(subscription_id)
    assert subscription is not None
    assert not subscription.is_due(now + 3600)
    assert subscription.is_due(now + 6 * 3600)
    assert subscription.last_result == "ok"

    storage.update_subscription(subscription_id, enabled=False)
    subscription = storage.get_subscription(subscription_id)
    assert subscription is not None
    assert not subscription.is_due(now + 100 * 3600)

    storage.delete_subscription(subscription_id)
    assert storage.list_subscriptions() == []


def test_a_subscriptions_settings_can_be_replaced(tmp_path: Path) -> None:
    storage = AppStorage(tmp_path)
    subscription_id = storage.add_subscription("https://www.youtube.com/@a", "A", DownloadSettings(quality="720p"), "out", 5, 6)
    storage.update_subscription(subscription_id, settings=DownloadSettings(quality="480p", media_type="audio"), max_items=9)
    reopened = AppStorage(tmp_path).get_subscription(subscription_id)
    assert reopened is not None
    assert (reopened.settings.quality, reopened.settings.media_type, reopened.max_items) == ("480p", "audio", 9)

    # Anything but settings is ignored rather than written as text the next read cannot parse.
    storage.update_subscription(subscription_id, settings={"quality": "360p"})
    assert storage.get_subscription(subscription_id).settings.quality == "480p"  # type: ignore[union-attr]


def test_manual_subscription_is_never_due(tmp_path: Path) -> None:
    storage = AppStorage(tmp_path)
    subscription = storage.get_subscription(storage.add_subscription("u", "A", DownloadSettings(), "out", 5, 0))
    assert subscription is not None
    assert not subscription.is_due(time.time())


@pytest.mark.parametrize(
    ("start", "end", "clock", "is_open"),
    [
        ("02:00", "07:00", "01:59", False),
        ("02:00", "07:00", "02:00", True),
        ("02:00", "07:00", "06:59", True),
        ("02:00", "07:00", "07:00", False),
        # Across midnight, as a night on a home network usually is.
        ("22:00", "06:00", "21:59", False),
        ("22:00", "06:00", "22:00", True),
        ("22:00", "06:00", "23:30", True),
        ("22:00", "06:00", "00:00", True),
        ("22:00", "06:00", "05:59", True),
        ("22:00", "06:00", "06:00", False),
        ("22:00", "06:00", "12:00", False),
        # The same start and end is the whole day, not never.
        ("03:00", "03:00", "12:00", True),
    ],
)
def test_a_download_window_is_open_only_between_its_times(start: str, end: str, clock: str, is_open: bool) -> None:
    hour, minute = (int(part) for part in clock.split(":"))
    moment = datetime(2026, 11, 7, hour, minute, 30)
    assert DownloadWindow(enabled=True, start=start, end=end).is_open(moment) is is_open
    assert DownloadWindow(enabled=False, start=start, end=end).is_open(moment) is True


def test_the_download_window_is_off_until_saved_and_survives_a_reopen(tmp_path: Path) -> None:
    assert AppStorage(tmp_path).load_download_window() == DownloadWindow()
    assert DownloadWindow().enabled is False
    AppStorage(tmp_path).save_download_window(DownloadWindow(enabled=True, start="22:00", end="06:00"))
    assert AppStorage(tmp_path).load_download_window() == DownloadWindow(enabled=True, start="22:00", end="06:00")
    assert DownloadWindow(enabled=True, start="22:00", end="06:00").label == "22:00–06:00"


@pytest.mark.parametrize(
    "saved",
    ["on", None, {"enabled": True, "start": "25:00", "end": "06:00"}, {"enabled": "yes", "start": "01:00", "end": "06:00"}, {"enabled": True}],
)
def test_a_damaged_download_window_reads_as_off(tmp_path: Path, saved: object) -> None:
    # A window that holds downloads back for no reason the person can see would be worse than none.
    storage = AppStorage(tmp_path)
    storage.set_value(DOWNLOAD_WINDOW_KEY, saved)
    assert storage.load_download_window() == DownloadWindow()


def test_archive_uses_yt_dlp_format(tmp_path: Path) -> None:
    storage = AppStorage(tmp_path)
    assert storage.add_to_archive(["a", "b", "a"]) == 2
    assert storage.add_to_archive(["a", "c"]) == 1
    assert storage.archive_path.read_text(encoding="utf-8") == "youtube a\nyoutube b\nyoutube c\n"
    assert storage.archived_video_ids() == {"a", "b", "c"}
    storage.clear_archive()
    assert storage.archived_video_ids() == set()


def test_adding_to_the_archive_reads_it_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    storage = AppStorage(tmp_path)
    storage.add_to_archive([f"old{index}" for index in range(100)])
    reads: list[int] = []
    original = storage.archived_video_ids
    monkeypatch.setattr(storage, "archived_video_ids", lambda: (reads.append(1), original())[1])
    assert storage.add_to_archive([f"new{index}" for index in range(150)]) == 150
    assert len(reads) == 1


def test_replacing_history_by_path_uses_an_index(tmp_path: Path) -> None:
    storage = AppStorage(tmp_path)
    with sqlite3.connect(storage.db_path) as connection:
        plan = connection.execute("EXPLAIN QUERY PLAN DELETE FROM history WHERE path = ?", ("a.mp4",)).fetchall()
    assert any("USING INDEX" in str(row[-1]) for row in plan)


def test_a_damaged_database_is_moved_aside_and_replaced(tmp_path: Path) -> None:
    healthy = AppStorage(tmp_path)
    healthy.set_value("key", "value")
    with healthy.db_path.open("r+b") as database:
        database.write(b"\0" * 100)

    storage = AppStorage(tmp_path)
    assert storage.recovered_from is not None
    assert storage.recovered_from.exists() and storage.recovered_from.parent == tmp_path
    assert storage.get_value("key") is None
    storage.set_value("key", "fresh")
    assert AppStorage(tmp_path).recovered_from is None


def make_files(root: Path, *names: str) -> None:
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * 5)


def test_scan_pairs_files_by_name(tmp_path: Path) -> None:
    make_files(
        tmp_path,
        "clip.mp4",
        "clip.en.srt",
        "clip.zh-TW.vtt",
        "clip.en.txt",
        "clip.webp",
        "clip_cookies.txt",
        "song.mp3",
        "unfinished.mp4.part",
        "merging.f137.mp4",
        "merging.f140.m4a",
        "notes.txt",
        "sub/nested.mkv",
    )
    entries = {entry.path.name: entry for entry in scan_library(tmp_path)}
    assert set(entries) == {"clip.mp4", "song.mp3", "nested.mkv"}
    assert [path.name for path in entries["clip.mp4"].sidecars] == ["clip.en.srt", "clip.en.txt", "clip.webp", "clip.zh-TW.vtt"]
    assert [path.name for path in entries["clip.mp4"].subtitles] == ["clip.en.srt", "clip.zh-TW.vtt"]
    assert entries["song.mp3"].kind == "audio"
    assert entries["clip.mp4"].kind == "video"


def test_similar_titles_do_not_share_sidecars(tmp_path: Path) -> None:
    make_files(tmp_path, "Lesson 1.mp4", "Lesson 1.en.srt", "Lesson 1. Intro.mp4", "Lesson 1. Intro.en.srt", "Lesson 1. Intro.info.json")
    entries = {entry.path.name: entry for entry in scan_library(tmp_path)}
    assert [path.name for path in entries["Lesson 1.mp4"].sidecars] == ["Lesson 1.en.srt"]
    assert [path.name for path in entries["Lesson 1. Intro.mp4"].sidecars] == ["Lesson 1. Intro.en.srt", "Lesson 1. Intro.info.json"]

    delete_entry(entries["Lesson 1.mp4"], tmp_path)
    assert (tmp_path / "Lesson 1. Intro.en.srt").exists()
    assert (tmp_path / "Lesson 1. Intro.info.json").exists()


def test_sidecar_owner_names() -> None:
    from videocap.library import sidecar_owner_stem

    assert sidecar_owner_stem("clip.en.srt") == "clip"
    assert sidecar_owner_stem("clip.zh-TW.vtt") == "clip"
    assert sidecar_owner_stem("clip.es-419.srt") == "clip"
    assert sidecar_owner_stem("clip.webp") == "clip"
    assert sidecar_owner_stem("clip.info.json") == "clip"
    assert sidecar_owner_stem("1-1.1. Openings.zh-TW.vtt") == "1-1.1. Openings"
    assert sidecar_owner_stem("Lesson 1. Intro.en.srt") == "Lesson 1. Intro"


def test_scan_sorts_newest_first_and_handles_missing_folder(tmp_path: Path) -> None:
    make_files(tmp_path, "old.mp4", "new.mp4")
    os.utime(tmp_path / "old.mp4", (1000, 1000))
    assert [entry.path.name for entry in scan_library(tmp_path)] == ["new.mp4", "old.mp4"]
    assert scan_library(tmp_path / "missing") == []


def test_intermediate_streams_are_recognized() -> None:
    assert is_intermediate_stream(Path("title.f137.mp4"))
    assert is_intermediate_stream(Path("title.f251-drc.webm"))
    assert not is_intermediate_stream(Path("title.mp4"))
    assert not is_intermediate_stream(Path("1-1.1. Openings.mp4"))
    assert not is_intermediate_stream(Path("the.final.cut.mp4"))


def test_delete_removes_sidecars_but_never_outside_the_library(tmp_path: Path) -> None:
    library = tmp_path / "library"
    make_files(library, "clip.mp4", "clip.en.srt", "keep.mp4")
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"x")

    entry = next(item for item in scan_library(library) if item.path.name == "clip.mp4")
    assert delete_entry(entry, library) == []
    assert not (library / "clip.mp4").exists()
    assert not (library / "clip.en.srt").exists()
    assert (library / "keep.mp4").exists()

    entry = next(item for item in scan_library(library))
    entry.path = outside
    problems = delete_entry(entry, library)
    assert problems and outside.exists()


def test_is_inside_rejects_traversal(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    assert is_inside(root / "a.mp4", root)
    assert not is_inside(root / ".." / "a.mp4", root)


def test_find_partial_files(tmp_path: Path) -> None:
    make_files(tmp_path, "a.mp4.part", "b.mp4", "c.mp4.ytdl")
    assert {path.name for path in find_partial_files(tmp_path)} == {"a.mp4.part", "c.mp4.ytdl"}


# Backups


def filled_storage(folder: Path, label: str) -> AppStorage:
    from videocap.storage import NOTIFY_TARGETS_KEY, PENDING_JOBS_KEY

    storage = AppStorage(folder)
    storage.add_history([history_entry(f"{label}-video", f"{label} 的影片")])
    storage.add_subscription(f"https://www.youtube.com/@{label}", f"{label} 頻道", DownloadSettings(quality="720p"), "out", 5, 24)
    storage.save_preset(f"{label} 組合", DownloadSettings(quality="480p"))
    storage.add_to_archive([f"{label}archived"])
    storage.set_value(NOTIFY_TARGETS_KEY, {"telegram_token": f"{label}-SECRET-TOKEN"})
    storage.set_value(PENDING_JOBS_KEY, [{"label": f"{label} 的任務"}])
    return storage


def test_a_backup_restores_everything_but_this_machines_secrets_and_jobs(tmp_path: Path) -> None:
    import json
    import zipfile

    from videocap.storage import NOTIFY_TARGETS_KEY, PENDING_JOBS_KEY

    old = filled_storage(tmp_path / "old", "舊")
    backup = tmp_path / "backup.zip"
    old.write_backup(backup)
    with zipfile.ZipFile(backup) as bundle:
        assert set(bundle.namelist()) == {"videocap-backup.json", "videocap.sqlite3", "settings.json", "download_archive.txt"}
        assert b"SECRET-TOKEN" not in bundle.read("videocap.sqlite3")
        assert "舊 組合" in json.loads(bundle.read("settings.json"))["presets"]

    new = filled_storage(tmp_path / "new", "新")
    assert new.restore_backup(backup) == (1, 1)
    assert [entry.title for entry in new.list_history()] == ["舊 的影片"]
    assert [subscription.title for subscription in new.list_subscriptions()] == ["舊 頻道"]
    assert list(new.load_presets()) == ["舊 組合"]
    assert new.archived_video_ids() == {"舊archived"}
    assert new.get_value(NOTIFY_TARGETS_KEY) == {"telegram_token": "新-SECRET-TOKEN"}
    assert new.get_value(PENDING_JOBS_KEY) == [{"label": "新 的任務"}]
    # The original is untouched, and the restored copy works after a reopen.
    assert old.get_value(NOTIFY_TARGETS_KEY) == {"telegram_token": "舊-SECRET-TOKEN"}
    assert AppStorage(tmp_path / "new").list_subscriptions()[0].settings.quality == "720p"


def test_a_backup_keeps_out_the_passwords_in_proxies_and_yt_dlp_options(tmp_path: Path) -> None:
    import zipfile

    from videocap.storage import LAST_SETTINGS_KEY

    proxy = "http://bob:hunter2@proxy.example:3128"
    extra = "--username bob --password=hunter2 -N 4 --add-header 'Authorization: Bearer hunter2'"
    secret = DownloadSettings(proxy=proxy, ytdlp_extra_args=extra)
    storage = AppStorage(tmp_path / "data")
    storage.set_value(LAST_SETTINGS_KEY, secret.to_dict())
    storage.save_preset("公司網路", secret)
    storage.add_subscription("https://www.youtube.com/@news", "新聞", secret, "out", 5, 24)
    backup = tmp_path / "backup.zip"
    storage.write_backup(backup)
    with zipfile.ZipFile(backup) as bundle:
        for name in bundle.namelist():
            assert b"hunter2" not in bundle.read(name), name

    # Restored here, this machine's own passwords stay; elsewhere they must be typed again.
    storage.restore_backup(backup)
    assert storage.get_value(LAST_SETTINGS_KEY)["proxy"] == proxy
    assert storage.load_presets()["公司網路"]["ytdlp_extra_args"] == extra
    assert storage.list_subscriptions()[0].settings.proxy == proxy
    elsewhere = AppStorage(tmp_path / "elsewhere")
    elsewhere.restore_backup(backup)
    [subscription] = elsewhere.list_subscriptions()
    assert subscription.settings.proxy == "http://proxy.example:3128"
    assert subscription.settings.ytdlp_extra_args == "-N 4"
    assert elsewhere.get_value(LAST_SETTINGS_KEY)["proxy"] == "http://proxy.example:3128"


def test_a_backup_is_consistent_while_downloads_are_recorded(tmp_path: Path) -> None:
    import threading
    import zipfile

    storage = AppStorage(tmp_path / "data")
    stop = threading.Event()

    def record() -> None:
        number = 0
        while not stop.is_set():
            storage.add_history([history_entry(f"busy{number}")])
            number += 1

    writer = threading.Thread(target=record)
    writer.start()
    try:
        for attempt in range(3):
            storage.write_backup(tmp_path / f"backup{attempt}.zip")
    finally:
        stop.set()
        writer.join()
    for attempt in range(3):
        with zipfile.ZipFile(tmp_path / f"backup{attempt}.zip") as bundle:
            (tmp_path / "check.sqlite3").write_bytes(bundle.read("videocap.sqlite3"))
        connection = sqlite3.connect(tmp_path / "check.sqlite3")
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        connection.close()


def zip_of(path: Path, files: dict[str, bytes]) -> Path:
    import zipfile

    with zipfile.ZipFile(path, "w") as bundle:
        for name, content in files.items():
            bundle.writestr(name, content)
    return path


MANIFEST = b'{"app": "youtube-videocap", "format": 1}'


@pytest.mark.parametrize(
    "files, message",
    [
        (None, "不是這個 App 的備份檔"),
        ({"photo.jpg": b"x"}, "不是這個 App 的備份檔"),
        ({"videocap-backup.json": b'{"app": "something-else"}', "videocap.sqlite3": b""}, "不是這個 App 的備份檔"),
        ({"videocap-backup.json": b"not json", "videocap.sqlite3": b""}, "不是這個 App 的備份檔"),
        ({"videocap-backup.json": MANIFEST, "videocap.sqlite3": b"this is not a database" * 100}, "已損壞"),
    ],
)
def test_a_file_that_is_not_a_good_backup_changes_nothing(tmp_path: Path, files: dict[str, bytes] | None, message: str) -> None:
    storage = filled_storage(tmp_path / "data", "現有")
    backup = tmp_path / "upload.zip"
    if files is None:
        backup.write_bytes(b"PK but not really a zip")
    else:
        zip_of(backup, files)
    with pytest.raises(ValueError, match=message):
        storage.restore_backup(backup)
    assert [entry.title for entry in storage.list_history()] == ["現有 的影片"]
    assert storage.archived_video_ids() == {"現有archived"}


def test_a_backup_of_another_database_or_an_oversized_one_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import videocap.storage as storage_module

    storage = filled_storage(tmp_path / "data", "現有")
    other = tmp_path / "other.sqlite3"
    connection = sqlite3.connect(other)
    connection.execute("CREATE TABLE notes (text TEXT)")
    connection.commit()
    connection.close()
    with pytest.raises(ValueError, match="已損壞"):
        storage.restore_backup(zip_of(tmp_path / "other.zip", {"videocap-backup.json": MANIFEST, "videocap.sqlite3": other.read_bytes()}))

    storage.write_backup(tmp_path / "good.zip")
    monkeypatch.setattr(storage_module, "BACKUP_MAX_FILE_BYTES", 1000)
    with pytest.raises(ValueError, match="大得不合理"):
        storage.restore_backup(tmp_path / "good.zip")
    assert [entry.title for entry in storage.list_history()] == ["現有 的影片"]


OLD_SCHEMA = """
CREATE TABLE history (id INTEGER PRIMARY KEY AUTOINCREMENT, video_id TEXT NOT NULL DEFAULT '', title TEXT NOT NULL DEFAULT '',
    channel TEXT NOT NULL DEFAULT '', url TEXT NOT NULL DEFAULT '', path TEXT NOT NULL DEFAULT '',
    size_bytes INTEGER NOT NULL DEFAULT 0, quality TEXT NOT NULL DEFAULT '', duration REAL,
    upload_date TEXT NOT NULL DEFAULT '', thumbnail TEXT NOT NULL DEFAULT '', finished_at REAL NOT NULL);
CREATE TABLE subscriptions (id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT NOT NULL UNIQUE, title TEXT NOT NULL DEFAULT '',
    settings TEXT NOT NULL DEFAULT '{}', output_dir TEXT NOT NULL DEFAULT '', max_items INTEGER NOT NULL DEFAULT 5,
    interval_hours INTEGER NOT NULL DEFAULT 0, enabled INTEGER NOT NULL DEFAULT 1, last_checked_at REAL,
    last_result TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL);
CREATE TABLE app_values (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO history (video_id, title, finished_at) VALUES ('old', '舊紀錄', 1.0);
INSERT INTO subscriptions (url, title, created_at) VALUES ('https://www.youtube.com/@old', '舊訂閱', 1.0);
"""


def old_database(path: Path) -> Path:
    connection = sqlite3.connect(path)
    connection.executescript(OLD_SCHEMA)
    connection.close()
    return path


def test_an_older_database_gains_the_new_columns_and_keeps_its_data(tmp_path: Path) -> None:
    storage = AppStorage(old_database(tmp_path / "videocap.sqlite3").parent)
    [old_entry] = storage.list_history()
    assert (old_entry.title, old_entry.subscription_id) == ("舊紀錄", None)
    [subscription] = storage.list_subscriptions()
    assert (subscription.title, subscription.keep_latest, subscription.keep_days) == ("舊訂閱", 0, 0)

    storage.add_history([history_entry("new", subscription_id=subscription.id, finished_at=5.0), history_entry("other", finished_at=9.0)])
    assert [entry.video_id for entry in storage.subscription_history(subscription.id)] == ["new"]
    connection = sqlite3.connect(storage.db_path)
    plan = connection.execute("EXPLAIN QUERY PLAN SELECT * FROM history WHERE subscription_id = ?", (1,)).fetchall()
    connection.close()
    assert any("USING INDEX" in str(row[-1]) for row in plan)
    # Opening it again finds nothing more to add.
    assert AppStorage(tmp_path).list_subscriptions()[0].keep_latest == 0


def test_a_backup_from_an_older_version_gains_the_new_columns(tmp_path: Path) -> None:
    old = old_database(tmp_path / "old.sqlite3")
    storage = AppStorage(tmp_path / "data")
    storage.add_to_archive(["leftover123"])
    storage.restore_backup(zip_of(tmp_path / "old.zip", {"videocap-backup.json": MANIFEST, "videocap.sqlite3": old.read_bytes()}))
    [subscription] = storage.list_subscriptions()
    assert subscription.keep_latest == 0
    storage.add_history([history_entry("new", subscription_id=subscription.id)])
    assert len(storage.subscription_history(subscription.id)) == 1
    # The backup had no archive, so none is left from before.
    assert storage.archived_video_ids() == set()


def test_a_backup_from_a_newer_version_with_more_columns_and_tables_still_works(tmp_path: Path) -> None:
    newer = filled_storage(tmp_path / "newer", "新版")
    connection = sqlite3.connect(newer.db_path)
    connection.executescript(
        "ALTER TABLE history ADD COLUMN rating INTEGER NOT NULL DEFAULT 5;"
        "ALTER TABLE subscriptions ADD COLUMN color TEXT NOT NULL DEFAULT 'red';"
        "CREATE TABLE playlists (id INTEGER PRIMARY KEY, name TEXT);"
    )
    connection.close()
    newer.write_backup(tmp_path / "newer.zip")

    storage = filled_storage(tmp_path / "data", "現有")
    assert storage.restore_backup(tmp_path / "newer.zip") == (1, 1)
    [entry] = storage.list_history()
    assert entry.title == "新版 的影片" and storage.find_downloads(["新版-video"])["新版-video"].title == "新版 的影片"
    [subscription] = storage.list_subscriptions()
    assert subscription.title == "新版 頻道" and storage.get_subscription(subscription.id) == subscription


def test_a_backup_whose_tables_lack_needed_columns_is_refused(tmp_path: Path) -> None:
    broken = tmp_path / "broken.sqlite3"
    connection = sqlite3.connect(broken)
    connection.executescript("CREATE TABLE history (id INTEGER); CREATE TABLE subscriptions (id INTEGER); CREATE TABLE app_values (key TEXT);")
    connection.close()
    storage = filled_storage(tmp_path / "data", "現有")
    with pytest.raises(ValueError, match="已損壞"):
        storage.restore_backup(zip_of(tmp_path / "broken.zip", {"videocap-backup.json": MANIFEST, "videocap.sqlite3": broken.read_bytes()}))
    assert [entry.title for entry in storage.list_history()] == ["現有 的影片"]


def test_a_restore_that_fails_halfway_leaves_the_old_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    old = filled_storage(tmp_path / "old", "舊")
    old.write_backup(tmp_path / "backup.zip")
    storage = filled_storage(tmp_path / "data", "現有")
    real_write_bytes = Path.write_bytes

    def disk_full(self: Path, data: bytes) -> int:
        if self.parent == storage.data_dir:
            raise OSError(28, "No space left on device")
        return real_write_bytes(self, data)

    monkeypatch.setattr(Path, "write_bytes", disk_full)
    with pytest.raises(OSError):
        storage.restore_backup(tmp_path / "backup.zip")
    monkeypatch.undo()
    assert [entry.title for entry in storage.list_history()] == ["現有 的影片"]
    assert storage.archived_video_ids() == {"現有archived"}
    assert [path.name for path in storage.data_dir.iterdir() if path.name.startswith(".")] == []


def test_a_download_archive_from_yt_dlp_is_read() -> None:
    from videocap.storage import archive_ids_from_text

    text = "youtube dQw4w9WgXcQ\nYoutube jNQXAC9IVRw\n\nvimeo 123456\nyoutube too-short\nyoutube dQw4w9WgXcQ\n"
    assert archive_ids_from_text(text) == (["dQw4w9WgXcQ", "jNQXAC9IVRw", "dQw4w9WgXcQ"], 2)

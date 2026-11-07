from __future__ import annotations

import json
import os
import re
import shlex
import sqlite3
import tempfile
import threading
import time
import zipfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Generator, Iterable, Optional

from videocap.settings import DownloadSettings, settings_from_dict


SCHEMA = """
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    channel TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    path TEXT NOT NULL DEFAULT '',
    size_bytes INTEGER NOT NULL DEFAULT 0,
    quality TEXT NOT NULL DEFAULT '',
    duration REAL,
    upload_date TEXT NOT NULL DEFAULT '',
    thumbnail TEXT NOT NULL DEFAULT '',
    finished_at REAL NOT NULL,
    -- The subscription whose check downloaded it, so its retention rule knows which files are its own.
    subscription_id INTEGER
);
CREATE INDEX IF NOT EXISTS history_video_id ON history (video_id);
CREATE INDEX IF NOT EXISTS history_finished_at ON history (finished_at);
-- Every saved download first deletes the old entry for its path.
CREATE INDEX IF NOT EXISTS history_path ON history (path);

CREATE TABLE IF NOT EXISTS subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL DEFAULT '',
    settings TEXT NOT NULL DEFAULT '{}',
    output_dir TEXT NOT NULL DEFAULT '',
    max_items INTEGER NOT NULL DEFAULT 5,
    interval_hours INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_checked_at REAL,
    last_result TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    -- Retention: keep only the newest this many downloads, and delete those older than this many days. 0 is off.
    keep_latest INTEGER NOT NULL DEFAULT 0,
    keep_days INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS app_values (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""
LAST_SETTINGS_KEY = "last_settings"
PRESETS_KEY = "presets"
PENDING_JOBS_KEY = "pending_jobs"
MAX_CONCURRENT_KEY = "max_concurrent"
OUTPUT_DIR_KEY = "output_dir"
NOTIFY_TARGETS_KEY = "notify_targets"
DOWNLOAD_WINDOW_KEY = "download_window"
CLOCK_RE = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")
ARCHIVE_FILE_NAME = "download_archive.txt"
DATABASE_FILE_NAME = "videocap.sqlite3"
BACKUP_MANIFEST_NAME = "videocap-backup.json"
BACKUP_SETTINGS_NAME = "settings.json"
BACKUP_APP_ID = "youtube-videocap"
# Each file in a backup, unpacked. A history of a million downloads is far smaller; a zip bomb is not.
BACKUP_MAX_FILE_BYTES = 1024**3
# Left out of a backup and kept through a restore: notification targets hold bot tokens, and unfinished jobs belong to
# the machine and folders they were queued on.
# The App update check is this machine's answer from GitHub; another copy asks for its own.
MACHINE_ONLY_KEYS = (PENDING_JOBS_KEY, NOTIFY_TARGETS_KEY, "app_update_check")
BACKUP_TABLES = frozenset({"history", "subscriptions", "app_values"})
# yt-dlp options whose value is a login, a password, a key or a header such as Authorization, which saved settings
# may hold and a backup leaves out, as it leaves out a proxy's user name and password.
SECRET_YTDLP_OPTIONS = frozenset(
    {
        "-u", "--username", "-p", "--password", "-2", "--twofactor", "--video-password", "--ap-username", "--ap-password",
        "--add-header", "--add-headers", "--cookies", "--client-certificate-key", "--client-certificate-password", "--netrc-cmd",
    }
)
YOUTUBE_ID_RE = re.compile(r"[0-9A-Za-z_-]{11}")
NOT_A_BACKUP_MESSAGE = "這不是這個 App 的備份檔。請選擇「備份全部資料」下載的 .zip 檔。"
DAMAGED_BACKUP_MESSAGE = "備份裡的資料庫已損壞，沒有還原。請改用其他備份檔。"
# A file that is not a database, or one whose pages are broken. Errors such as a locked database pass by themselves.
DAMAGED_DATABASE_ERRORS = frozenset({"SQLITE_NOTADB", "SQLITE_CORRUPT"})
HISTORY_COLUMNS = (
    "video_id", "title", "channel", "url", "path", "size_bytes", "quality", "duration", "upload_date", "thumbnail", "subscription_id"
)
EDITABLE_SUBSCRIPTION_FIELDS = frozenset(
    {"title", "settings", "output_dir", "max_items", "interval_hours", "enabled", "last_checked_at", "last_result", "keep_latest", "keep_days"}
)
NULLABLE_HISTORY_COLUMNS = frozenset({"duration", "subscription_id"})
# Columns added after the first release. CREATE TABLE IF NOT EXISTS leaves an older database's tables as they were.
ADDED_COLUMNS = {
    "history": {"subscription_id": "INTEGER"},
    "subscriptions": {"keep_latest": "INTEGER NOT NULL DEFAULT 0", "keep_days": "INTEGER NOT NULL DEFAULT 0"},
}


@dataclass
class HistoryEntry:
    id: int
    video_id: str
    title: str
    channel: str
    url: str
    path: str
    size_bytes: int
    quality: str
    duration: Optional[float]
    upload_date: str
    thumbnail: str
    finished_at: float
    subscription_id: Optional[int] = None


@dataclass
class Subscription:
    id: int
    url: str
    title: str
    settings: DownloadSettings
    output_dir: str
    max_items: int
    interval_hours: int
    enabled: bool
    last_checked_at: Optional[float]
    last_result: str
    created_at: float
    keep_latest: int = 0
    keep_days: int = 0

    @property
    def has_retention(self) -> bool:
        return bool(self.keep_latest or self.keep_days)

    def is_due(self, now: float) -> bool:
        if not self.enabled or not self.interval_hours:
            return False
        return self.last_checked_at is None or now - self.last_checked_at >= self.interval_hours * 3600


@dataclass(frozen=True)
class DownloadWindow:
    """When the owner's downloads may start, in this computer's own time, for a home network that is busy by day."""

    enabled: bool = False
    start: str = "02:00"
    end: str = "07:00"

    @property
    def label(self) -> str:
        return f"{self.start}–{self.end}"

    def is_open(self, moment: datetime) -> bool:
        if not self.enabled:
            return True
        start, end, now = clock_minutes(self.start), clock_minutes(self.end), moment.hour * 60 + moment.minute
        if start == end:
            # A window as long as the day, rather than one that never opens.
            return True
        if start < end:
            return start <= now < end
        # Across midnight, such as 22:00–06:00.
        return now >= start or now < end


def clock_minutes(text: str) -> int:
    hours, minutes = text.split(":")
    return int(hours) * 60 + int(minutes)


def download_window_from_dict(data: Any) -> DownloadWindow:
    # Anything else is off: a window that holds downloads back for a reason nobody can see is worse than none.
    if not isinstance(data, dict) or not isinstance(data.get("enabled"), bool):
        return DownloadWindow()
    start, end = data.get("start"), data.get("end")
    if not all(isinstance(value, str) and CLOCK_RE.fullmatch(value) for value in (start, end)):
        return DownloadWindow()
    return DownloadWindow(enabled=data["enabled"], start=str(start), end=str(end))


class AppStorage:
    """History, subscriptions, and preferences for a machine's own install. Public hosts do not use this, so visitors share nothing."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / DATABASE_FILE_NAME
        self.archive_path = self.data_dir / ARCHIVE_FILE_NAME
        self._write_lock = threading.Lock()
        # Where a damaged database was moved, so the page can tell the person. None when the database was fine.
        self.recovered_from: Optional[Path] = None
        try:
            self._create_schema()
        except sqlite3.DatabaseError as exc:
            if getattr(exc, "sqlite_errorname", "") not in DAMAGED_DATABASE_ERRORS:
                raise
            # Otherwise every page fails, downloads included, and nothing says which file is to blame.
            self.recovered_from = self._move_damaged_database()
            self._create_schema()

    def _create_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(SCHEMA)
            add_missing_columns(connection)

    def _move_damaged_database(self) -> Path:
        moved = self.db_path.with_name(f"{self.db_path.name}.damaged-{time.strftime('%Y%m%d-%H%M%S')}")
        for suffix in ("", "-journal", "-wal", "-shm"):
            source = Path(f"{self.db_path}{suffix}")
            if source.exists():
                source.replace(Path(f"{moved}{suffix}"))
        return moved

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        # A connection per call, because job threads and every Streamlit session reach this at once.
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    # Preferences

    def get_value(self, key: str, default: Any = None) -> Any:
        with self._connect() as connection:
            row = connection.execute("SELECT value FROM app_values WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except ValueError:
            return default

    def set_value(self, key: str, value: Any) -> None:
        with self._write_lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO app_values (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value, ensure_ascii=False)),
            )

    def load_presets(self) -> dict[str, dict[str, Any]]:
        presets = self.get_value(PRESETS_KEY, {})
        return presets if isinstance(presets, dict) else {}

    def save_preset(self, name: str, settings: DownloadSettings) -> None:
        presets = self.load_presets()
        presets[name] = settings.to_dict()
        self.set_value(PRESETS_KEY, presets)

    def delete_preset(self, name: str) -> None:
        presets = self.load_presets()
        presets.pop(name, None)
        self.set_value(PRESETS_KEY, presets)

    def load_download_window(self) -> DownloadWindow:
        return download_window_from_dict(self.get_value(DOWNLOAD_WINDOW_KEY))

    def save_download_window(self, window: DownloadWindow) -> None:
        self.set_value(DOWNLOAD_WINDOW_KEY, asdict(window))

    # History

    def add_history(self, entries: Iterable[dict[str, Any]]) -> None:
        def column_value(entry: dict[str, Any], column: str) -> Any:
            value = entry.get(column)
            if value is None and column not in NULLABLE_HISTORY_COLUMNS:
                return 0 if column == "size_bytes" else ""
            return value

        rows = [
            (*[column_value(entry, column) for column in HISTORY_COLUMNS], entry.get("finished_at") or time.time())
            for entry in entries
        ]
        if not rows:
            return
        placeholders = ", ".join("?" for _ in range(len(HISTORY_COLUMNS) + 1))
        path_index = HISTORY_COLUMNS.index("path")
        with self._write_lock, self._connect() as connection:
            # Downloading to the same path replaced the file, so it also replaces the file's old entry.
            connection.executemany("DELETE FROM history WHERE path = ?", [(row[path_index],) for row in rows if row[path_index]])
            connection.executemany(
                f"INSERT INTO history ({', '.join(HISTORY_COLUMNS)}, finished_at) VALUES ({placeholders})",
                rows,
            )

    def list_history(self, search: str = "", limit: int = 1000) -> list[HistoryEntry]:
        query = "SELECT * FROM history"
        parameters: list[Any] = []
        if search.strip():
            query += " WHERE title LIKE ? OR channel LIKE ? OR video_id LIKE ?"
            pattern = f"%{search.strip()}%"
            parameters = [pattern, pattern, pattern]
        query += " ORDER BY finished_at DESC, id DESC LIMIT ?"
        parameters.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [HistoryEntry(**known_columns(row, HistoryEntry)) for row in rows]

    def find_downloads(self, video_ids: Iterable[str]) -> dict[str, HistoryEntry]:
        """Returns the latest download of each video, so a report can say it is already on disk."""
        ids = [video_id for video_id in dict.fromkeys(video_ids) if video_id]
        if not ids:
            return {}
        placeholders = ", ".join("?" for _ in ids)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM history WHERE video_id IN ({placeholders}) ORDER BY finished_at ASC, id ASC", ids
            ).fetchall()
        return {row["video_id"]: HistoryEntry(**known_columns(row, HistoryEntry)) for row in rows}

    def subscription_history(self, subscription_id: int) -> list[HistoryEntry]:
        """What a subscription's checks downloaded, newest first."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM history WHERE subscription_id = ? ORDER BY finished_at DESC, id DESC", (subscription_id,)
            ).fetchall()
        return [HistoryEntry(**known_columns(row, HistoryEntry)) for row in rows]

    def history_totals(self) -> tuple[int, int]:
        with self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS count, COALESCE(SUM(size_bytes), 0) AS size FROM history").fetchone()
        return int(row["count"]), int(row["size"])

    def delete_history(self, entry_ids: Iterable[int]) -> None:
        ids = list(entry_ids)
        if not ids:
            return
        placeholders = ", ".join("?" for _ in ids)
        with self._write_lock, self._connect() as connection:
            connection.execute(f"DELETE FROM history WHERE id IN ({placeholders})", ids)

    def clear_history(self) -> None:
        with self._write_lock, self._connect() as connection:
            connection.execute("DELETE FROM history")

    # Subscriptions

    def add_subscription(
        self, url: str, title: str, settings: DownloadSettings, output_dir: str, max_items: int, interval_hours: int
    ) -> int:
        with self._write_lock, self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO subscriptions (url, title, settings, output_dir, max_items, interval_hours, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (url, title, json.dumps(settings.to_dict(), ensure_ascii=False), output_dir, max_items, interval_hours, time.time()),
            )
            return int(cursor.lastrowid or 0)

    @staticmethod
    def _subscription_from_row(row: sqlite3.Row) -> Subscription:
        values = known_columns(row, Subscription)
        try:
            saved_settings = json.loads(values["settings"])
        except ValueError:
            saved_settings = {}
        values["settings"] = settings_from_dict(saved_settings if isinstance(saved_settings, dict) else {})
        values["enabled"] = bool(values["enabled"])
        return Subscription(**values)

    def list_subscriptions(self) -> list[Subscription]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM subscriptions ORDER BY created_at DESC, id DESC").fetchall()
        return [self._subscription_from_row(row) for row in rows]

    def get_subscription(self, subscription_id: int) -> Optional[Subscription]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM subscriptions WHERE id = ?", (subscription_id,)).fetchone()
        return self._subscription_from_row(row) if row else None

    def update_subscription(self, subscription_id: int, **values: Any) -> None:
        updates = {key: value for key, value in values.items() if key in EDITABLE_SUBSCRIPTION_FIELDS}
        if "settings" in updates:
            settings = updates.pop("settings")
            if isinstance(settings, DownloadSettings):
                # Saved the way add_subscription saves them, so _subscription_from_row reads both alike.
                updates["settings"] = json.dumps(settings.to_dict(), ensure_ascii=False)
        if not updates:
            return
        assignments = ", ".join(f"{key} = ?" for key in updates)
        with self._write_lock, self._connect() as connection:
            connection.execute(
                f"UPDATE subscriptions SET {assignments} WHERE id = ?",
                (*updates.values(), subscription_id),
            )

    def delete_subscription(self, subscription_id: int) -> None:
        with self._write_lock, self._connect() as connection:
            connection.execute("DELETE FROM subscriptions WHERE id = ?", (subscription_id,))

    # Download archive

    def archived_video_ids(self) -> set[str]:
        """yt-dlp's archive file holds one "youtube <id>" per line."""
        if not self.archive_path.exists():
            return set()
        lines = self.archive_path.read_text(encoding="utf-8", errors="replace").splitlines()
        return {line.split()[1] for line in lines if len(line.split()) == 2}

    def add_to_archive(self, video_ids: Iterable[str]) -> int:
        existing = self.archived_video_ids()
        new_ids = [video_id for video_id in dict.fromkeys(video_ids) if video_id not in existing]
        if new_ids:
            with self._write_lock, self.archive_path.open("a", encoding="utf-8") as archive:
                archive.writelines(f"youtube {video_id}\n" for video_id in new_ids)
        return len(new_ids)

    def clear_archive(self) -> None:
        with self._write_lock:
            self.archive_path.unlink(missing_ok=True)

    # Backups

    def write_backup(self, target: Path) -> None:
        """A zip of the database, the download archive, and the presets and settings as readable JSON. The database
        is copied with SQLite's online backup, so jobs writing meanwhile cannot leave it half-written."""
        with tempfile.TemporaryDirectory(prefix="videocap-backup-") as workdir:
            copy = Path(workdir) / DATABASE_FILE_NAME
            self.copy_database(copy)
            # Read from the copy, which has had its passwords taken out.
            connection = sqlite3.connect(copy)
            try:
                saved = dict(connection.execute("SELECT key, value FROM app_values").fetchall())
            finally:
                connection.close()
            settings = {key: json_or_none(saved.get(key)) for key in (PRESETS_KEY, LAST_SETTINGS_KEY, OUTPUT_DIR_KEY, MAX_CONCURRENT_KEY)}
            manifest = {"app": BACKUP_APP_ID, "format": 1, "created_at": time.time()}
            with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
                bundle.writestr(BACKUP_MANIFEST_NAME, json.dumps(manifest))
                bundle.write(copy, DATABASE_FILE_NAME)
                bundle.writestr(BACKUP_SETTINGS_NAME, json.dumps(settings, ensure_ascii=False, indent=2))
                if self.archive_path.exists():
                    bundle.write(self.archive_path, ARCHIVE_FILE_NAME)

    def copy_database(self, target: Path) -> None:
        source = sqlite3.connect(self.db_path, timeout=10)
        destination = sqlite3.connect(target)
        try:
            source.backup(destination)
            destination.executemany("DELETE FROM app_values WHERE key = ?", [(key,) for key in MACHINE_ONLY_KEYS])
            edit_saved_settings(destination, settings_without_secrets)
            destination.commit()
            # Deleted rows stay readable in the file's free pages until it is rewritten.
            destination.execute("VACUUM")
        finally:
            destination.close()
            source.close()

    def restore_backup(self, backup: Path) -> tuple[int, int]:
        """Replaces the database and download archive with a backup's. Returns how many downloads and subscriptions
        it holds. Raises ValueError with a message for the person when the file is not a usable backup."""
        with tempfile.TemporaryDirectory(prefix="videocap-restore-") as workdir:
            database, archive = unpack_backup(backup, Path(workdir))
            # Written beside the archive before the database changes, so a full disk stops the restore while the old
            # data is still whole, and only a rename is left for after.
            staged = self.archive_path.with_name(f".{self.archive_path.name}.restoring")
            with self._write_lock:
                try:
                    if archive is not None:
                        staged.write_bytes(archive.read_bytes())
                    placeholders = ", ".join("?" for _ in MACHINE_ONLY_KEYS)
                    with self._connect() as connection:
                        kept = connection.execute(f"SELECT key, value FROM app_values WHERE key IN ({placeholders})", MACHINE_ONLY_KEYS).fetchall()
                        put_back = secrets_put_back(saved_settings(connection))
                    source = sqlite3.connect(database)
                    destination = sqlite3.connect(self.db_path, timeout=10)
                    try:
                        # Into the open database rather than over its file, which Windows refuses while anything has it open.
                        source.backup(destination)
                        destination.executemany("DELETE FROM app_values WHERE key = ?", [(key,) for key in MACHINE_ONLY_KEYS])
                        destination.executemany("INSERT INTO app_values (key, value) VALUES (?, ?)", [tuple(row) for row in kept])
                        edit_saved_settings(destination, put_back)
                        destination.commit()
                    finally:
                        destination.close()
                        source.close()
                    if archive is None:
                        self.archive_path.unlink(missing_ok=True)
                    else:
                        os.replace(staged, self.archive_path)
                finally:
                    staged.unlink(missing_ok=True)
        # A backup from an older version lacks the newer columns.
        self._create_schema()
        with self._connect() as connection:
            history = connection.execute("SELECT COUNT(*) FROM history").fetchone()[0]
            subscriptions = connection.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0]
        return int(history), int(subscriptions)


def add_missing_columns(connection: sqlite3.Connection) -> None:
    for table, columns in ADDED_COLUMNS.items():
        existing = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
        for column, definition in columns.items():
            if column in existing:
                continue
            try:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            except sqlite3.OperationalError as exc:
                # Another app process opening the same database may have added it a moment ago.
                if "duplicate column" not in str(exc):
                    raise
    # Here rather than in SCHEMA, which runs before an older database has the column.
    connection.execute("CREATE INDEX IF NOT EXISTS history_subscription_id ON history (subscription_id)")


def unpack_backup(backup: Path, workdir: Path) -> tuple[Path, Optional[Path]]:
    """Checks a backup and unpacks its database and download archive. Raises ValueError with a message for the person."""
    try:
        with zipfile.ZipFile(backup) as bundle:
            members = {info.filename: info for info in bundle.infolist()}
            if BACKUP_MANIFEST_NAME not in members or DATABASE_FILE_NAME not in members:
                raise ValueError(NOT_A_BACKUP_MESSAGE)
            if any(info.file_size > BACKUP_MAX_FILE_BYTES for info in members.values()):
                raise ValueError("備份檔裡的檔案大得不合理，可能已損壞，沒有還原。")
            manifest = json.loads(bundle.read(BACKUP_MANIFEST_NAME))
            if not isinstance(manifest, dict) or manifest.get("app") != BACKUP_APP_ID:
                raise ValueError(NOT_A_BACKUP_MESSAGE)
            database = workdir / DATABASE_FILE_NAME
            database.write_bytes(bundle.read(DATABASE_FILE_NAME))
            archive: Optional[Path] = None
            if ARCHIVE_FILE_NAME in members:
                archive = workdir / ARCHIVE_FILE_NAME
                archive.write_bytes(bundle.read(ARCHIVE_FILE_NAME))
    except ValueError as exc:
        if isinstance(exc, json.JSONDecodeError):
            raise ValueError(NOT_A_BACKUP_MESSAGE) from exc
        raise
    except (zipfile.BadZipFile, KeyError, OSError, RuntimeError, EOFError) as exc:
        # RuntimeError is what zipfile raises for an encrypted member.
        raise ValueError(NOT_A_BACKUP_MESSAGE) from exc
    check_backup_database(database)
    return database, archive


def check_backup_database(database: Path) -> None:
    try:
        connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)
        try:
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError(DAMAGED_BACKUP_MESSAGE)
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            if not BACKUP_TABLES <= tables:
                raise ValueError(DAMAGED_BACKUP_MESSAGE)
            # Columns added later are added after the restore; any other missing one would break every page.
            for table, needed in required_columns().items():
                if not needed <= {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}:
                    raise ValueError(DAMAGED_BACKUP_MESSAGE)
        finally:
            connection.close()
    except sqlite3.DatabaseError as exc:
        raise ValueError(DAMAGED_BACKUP_MESSAGE) from exc


def required_columns() -> dict[str, set[str]]:
    """Each table's columns in SCHEMA, less those an older database gains when opened."""
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(SCHEMA)
        return {
            table: {row[1] for row in connection.execute(f"PRAGMA table_info({table})")} - set(ADDED_COLUMNS.get(table, {}))
            for table in BACKUP_TABLES
        }
    finally:
        connection.close()


def json_or_none(text: Optional[str]) -> Any:
    try:
        return json.loads(text) if text is not None else None
    except ValueError:
        return None


def saved_settings(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every saved set of download settings: the last used, each preset, and each subscription's."""
    found: list[Any] = []
    rows = dict(connection.execute("SELECT key, value FROM app_values WHERE key IN (?, ?)", (LAST_SETTINGS_KEY, PRESETS_KEY)).fetchall())
    found.append(json_or_none(rows.get(LAST_SETTINGS_KEY)))
    presets = json_or_none(rows.get(PRESETS_KEY))
    found.extend(presets.values() if isinstance(presets, dict) else [])
    found.extend(json_or_none(row[0]) for row in connection.execute("SELECT settings FROM subscriptions"))
    return [settings for settings in found if isinstance(settings, dict)]


def edit_saved_settings(connection: sqlite3.Connection, change: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
    """Applies change to every set saved_settings finds, in this connection's open transaction."""

    def dumped(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False)

    for key in (LAST_SETTINGS_KEY, PRESETS_KEY):
        row = connection.execute("SELECT value FROM app_values WHERE key = ?", (key,)).fetchone()
        value = json_or_none(row[0]) if row else None
        if not isinstance(value, dict):
            continue
        if key == PRESETS_KEY:
            value = {name: change(preset) if isinstance(preset, dict) else preset for name, preset in value.items()}
        else:
            value = change(value)
        connection.execute("UPDATE app_values SET value = ? WHERE key = ?", (dumped(value), key))
    for subscription_id, text in connection.execute("SELECT id, settings FROM subscriptions").fetchall():
        value = json_or_none(text)
        if isinstance(value, dict):
            connection.execute("UPDATE subscriptions SET settings = ? WHERE id = ?", (dumped(change(value)), subscription_id))


def proxy_without_login(proxy: str) -> str:
    """"http://user:password@host:3128" without "user:password@"."""
    scheme, separator, rest = proxy.partition("://")
    if not separator:
        scheme, rest = "", proxy
    host, slash, path = rest.partition("/")
    return f"{scheme}{separator}{host.rpartition('@')[2]}{slash}{path}"


def ytdlp_args_without_secrets(raw: str) -> str:
    """The owner's extra yt-dlp options less any login, password or header, or "" when they cannot be read."""
    try:
        tokens = shlex.split(raw)
    except ValueError:
        return ""
    kept: list[str] = []
    skip_value = False
    for token in tokens:
        if skip_value:
            skip_value = False
        elif token in SECRET_YTDLP_OPTIONS:
            skip_value = True
        elif token.startswith("--") and token.partition("=")[0] in SECRET_YTDLP_OPTIONS:
            continue
        elif not token.startswith("--") and len(token) > 2 and token[:2] in SECRET_YTDLP_OPTIONS:
            # A short option with its value attached, such as "-pSECRET".
            continue
        else:
            kept.append(token)
    return raw if kept == tokens else shlex.join(kept)


def settings_without_secrets(settings: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(settings)
    if isinstance(cleaned.get("proxy"), str):
        cleaned["proxy"] = proxy_without_login(cleaned["proxy"])
    if isinstance(cleaned.get("ytdlp_extra_args"), str):
        cleaned["ytdlp_extra_args"] = ytdlp_args_without_secrets(cleaned["ytdlp_extra_args"])
    return cleaned


def secrets_put_back(current: list[dict[str, Any]]) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """Gives restored settings back the passwords a backup leaves out, where this machine's own settings have them,
    so restoring on the same machine keeps its proxy and yt-dlp logins working."""
    originals: dict[tuple[str, str], str] = {}
    for settings in current:
        cleaned = settings_without_secrets(settings)
        for field_name in ("proxy", "ytdlp_extra_args"):
            # Not onto a blank one, which the backup may have had blank all along.
            if isinstance(settings.get(field_name), str) and cleaned[field_name] and cleaned[field_name] != settings[field_name]:
                originals.setdefault((field_name, cleaned[field_name]), settings[field_name])

    def put_back(settings: dict[str, Any]) -> dict[str, Any]:
        restored = dict(settings)
        for field_name in ("proxy", "ytdlp_extra_args"):
            value = restored.get(field_name)
            if isinstance(value, str) and (field_name, value) in originals:
                restored[field_name] = originals[(field_name, value)]
        return restored

    return put_back


def known_columns(row: sqlite3.Row, target: type) -> dict[str, Any]:
    """A row's values for a dataclass. A database a newer version wrote, or restored from its backup, can have
    columns this version does not know."""
    names = {spec.name for spec in fields(target)}
    return {key: row[key] for key in row.keys() if key in names}


def archive_ids_from_text(text: str) -> tuple[list[str], int]:
    """The YouTube IDs in a yt-dlp download archive, one "youtube <id>" per line, and how many other lines it has,
    such as other sites' videos."""
    video_ids: list[str] = []
    other = 0
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        if len(parts) == 2 and parts[0].lower() == "youtube" and YOUTUBE_ID_RE.fullmatch(parts[1]):
            video_ids.append(parts[1])
        else:
            other += 1
    return video_ids, other

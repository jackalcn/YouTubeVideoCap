from __future__ import annotations

import atexit
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import types
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import IO, Any, Generator, Optional, cast

from videocap.config import (
    AUTO_RETRY_DELAYS_SECONDS,
    AUTO_RETRY_LIMIT,
    CLOUD_ABANDON_AFTER_SECONDS,
    CLOUD_MAX_CONCURRENT_JOBS,
    CLOUD_MAX_CONCURRENT_PER_VISITOR,
    CLOUD_MAX_JOB_SECONDS,
    CLOUD_MAX_PENDING_PER_ADDRESS,
    CLOUD_MAX_PENDING_PER_VISITOR,
    CLOUD_MAX_PENDING_TOTAL,
    CLOUD_MAX_STORED_BYTES_PER_VISITOR,
    CLOUD_MIN_FREE_BYTES,
    CLOUD_OUTPUT_ROOT,
    CLOUD_RETENTION_SECONDS,
    LOCAL_DEFAULT_CONCURRENT_JOBS,
    RATE_LIMIT_PAUSE_SECONDS,
    SUBSCRIPTION_CHECK_SECONDS,
    get_data_dir,
)
from videocap.downloader import DownloadedItem, DownloadOutcome, empty_outcome, remove_partial_files, run_download
from videocap.environment import disable_autostart, enable_autostart
from videocap.errors import humanize_ytdlp_error, is_network_loss, is_rate_limited
from videocap.inspector import list_recent_video_ids
from videocap.jobs import (
    CANCELED,
    DONE,
    FAILED,
    PAUSED,
    QUEUED,
    RUNNING,
    STOP_PAUSE,
    STOPPED,
    UNFINISHED_STATES,
    Job,
    JobContext,
    JobManager,
    new_job_id,
)
from videocap.library import delete_entry, is_inside, media_entry
from videocap.media_tools import TOOL_LABELS, ToolRequest, ToolResult, run_tool
from videocap.notify import NotifyTargets, send_all, send_in_background, target_problems, targets_from_dict
from videocap.options import AccessLimits, DownloadOptions, LoginSource, NetworkOptions, job_url, options_from_spec
from videocap.settings import DownloadSettings, JobSpec, job_spec_from_dict
from videocap.storage import (
    LAST_SETTINGS_KEY,
    MAX_CONCURRENT_KEY,
    NOTIFY_TARGETS_KEY,
    PENDING_JOBS_KEY,
    AppStorage,
    DownloadWindow,
    HistoryEntry,
    Subscription,
    archive_ids_from_text,
)
from videocap.subscription_import import ImportedChannel, subscribed_channel_ids
from videocap.textfiles import decode_text
from videocap.tooling import can_write_folder, format_bytes, get_free_disk_bytes, install_ytdlp_update
from videocap.urls import classify_youtube_url


KIND_DOWNLOAD = "download"
KIND_TOOL = "tool"
LOCAL_OWNER = "local"

# How far back a channel check looks, which is also how many videos subscribing marks as seen. Looking past the
# per-check limit lets later checks catch up when more new videos came out than one check downloads.
SUBSCRIPTION_CHANNEL_DEPTH = 50
# YouTube adds new videos to the end of a playlist unless its owner chooses otherwise, so a check reads all of it.
# Videos already in the archive are skipped from the listing itself, without a request each.
SUBSCRIPTION_PLAYLIST_DEPTH = 100_000
# Queueing a long list changes the queue hundreds of times in a row; this many seconds of changes share one save.
PENDING_SAVE_SECONDS = 0.5
LOGIN_NEEDED_MESSAGE = "這項任務需要登入：請先在「會員登入」上傳 cookies.txt，再按「重試」。"
YTDLP_UPDATE_BUSY_MESSAGE = "還有影片正在下載。請等下載完成或先暫停，再更新 yt-dlp。"
PUBLIC_OUTPUT_NOT_WRITABLE_MESSAGE = "這個公開主機的下載資料夾暫時無法寫入，請稍後再試，或在自己的電腦上執行。"
OWNER_ONLY_MESSAGE = "只有這台電腦的使用者可以使用這項功能。"
WINDOW_HOLD_PREFIX = "等待下載時段"
RATE_LIMIT_HOLD_TEXT = "YouTube 暫時限制請求，稍後自動繼續"
# Told on the pages and in the notification alike, with the time the queue continues.
RATE_LIMIT_NOTICE = "YouTube 暫時限制了請求（HTTP 429／要求驗證），佇列已暫停，{clock} 後自動繼續。"
ENGINE_LOCK_FILE_NAME = "engine.lock"
# Between channels when importing many subscriptions, each of which reads its channel from YouTube.
SUBSCRIPTION_IMPORT_PAUSE_SECONDS = 3.0
PUBLIC_QUEUE_FULL_MESSAGE = (
    f"這個公開主機的下載佇列已滿：每人最多排 {CLOUD_MAX_PENDING_PER_VISITOR} 項，所有人合計最多 {CLOUD_MAX_PENDING_TOTAL} 項。"
    "請等目前的任務完成後再試，或在自己的電腦上執行。"
)


COOKIE_DIR_PREFIX = "youtube-downloader-cookies-"
# Older than any job could run: the app that made them was killed before it could clean up.
STALE_COOKIE_DIR_SECONDS = 12 * 60 * 60
active_cookie_dirs: set[Path] = set()


@contextmanager
def temporary_cookie_file(cookie_bytes: Optional[bytes]) -> Generator[Optional[Path], None, None]:
    if cookie_bytes is None:
        yield None
        return

    # Cookies are login credentials: keep them in a private directory only while the task runs.
    workdir = Path(tempfile.mkdtemp(prefix=COOKIE_DIR_PREFIX))
    active_cookie_dirs.add(workdir)
    try:
        cookie_file = workdir / "cookies.txt"
        cookie_file.write_bytes(cookie_bytes)
        yield cookie_file
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        active_cookie_dirs.discard(workdir)


def acquire_engine_lock(data_dir: Path) -> Optional[IO[bytes]]:
    """Locks the data folder for this process's engine, or returns None when another process holds it. The operating
    system drops the lock when the process ends, however it ends, so a crash leaves nothing stale behind."""
    try:
        handle = (data_dir / ENGINE_LOCK_FILE_NAME).open("a+b")
    except OSError:
        return None
    try:
        if sys.platform == "win32":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def release_engine_lock(handle: IO[bytes]) -> None:
    try:
        if sys.platform == "win32":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
    finally:
        # Closing the file also drops an flock.
        handle.close()


def require_owner(limits: AccessLimits) -> None:
    """For what reaches this machine's own data or settings. Pages hide these from visitors, and this holds even if
    a page forgets to."""
    if not limits.trusted:
        raise PermissionError(OWNER_ONLY_MESSAGE)


@atexit.register
def remove_active_cookie_dirs() -> None:
    # Job threads are daemons, so on exit their finally blocks never run.
    for workdir in list(active_cookie_dirs):
        shutil.rmtree(workdir, ignore_errors=True)


def remove_stale_cookie_dirs() -> None:
    cutoff = time.time() - STALE_COOKIE_DIR_SECONDS
    for workdir in Path(tempfile.gettempdir()).glob(f"{COOKIE_DIR_PREFIX}*"):
        try:
            if workdir.is_dir() and workdir.stat().st_mtime < cutoff:
                shutil.rmtree(workdir, ignore_errors=True)
        except OSError:
            continue


@dataclass
class SubscriptionImport:
    """Progress of adding many subscriptions at once, read by any page while the import runs in the background."""

    total: int
    added: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    current: str = ""
    finished: bool = False
    stop_requested: threading.Event = field(default_factory=threading.Event)

    @property
    def handled(self) -> int:
        return len(self.added) + len(self.skipped) + len(self.failed)


@dataclass
class RateLimitPause:
    """The owner's queue resting after YouTube limited its requests. Kept in memory only: a restart brings the
    unfinished jobs back paused anyway."""

    reason: str
    until: float
    # Jobs this pause stopped mid-download, which it continues when it ends. Ones the person paused stay paused.
    paused_ids: set[str] = field(default_factory=set)


@dataclass
class DownloadPayload:
    spec: JobSpec
    limits: AccessLimits
    # A public visitor's files live in a folder of their own, which is deleted along with the job.
    private_output_dir: Optional[Path] = None


class AppServices:
    """Everything shared across Streamlit sessions: the job queue, and on private installs the saved data."""

    def __init__(self, is_public: bool, data_dir: Optional[Path] = None) -> None:
        self.is_public = is_public
        self.storage: Optional[AppStorage] = None if is_public else AppStorage(data_dir or get_data_dir())
        max_concurrent = CLOUD_MAX_CONCURRENT_JOBS if is_public else LOCAL_DEFAULT_CONCURRENT_JOBS
        if self.storage:
            saved_concurrency = self.storage.get_value(MAX_CONCURRENT_KEY)
            if isinstance(saved_concurrency, int) and saved_concurrency > 0:
                max_concurrent = saved_concurrency
        # Set once yt-dlp was replaced on disk: downloads then wait for a restart.
        self.restart_required = False
        # Kept in memory too, because the queue asks it about every waiting job at every tick.
        self.download_window = self.storage.load_download_window() if self.storage else DownloadWindow()
        self.rate_limit_pause: Optional[RateLimitPause] = None
        self._rate_limit_lock = threading.Lock()
        self.subscription_import: Optional[SubscriptionImport] = None
        self._notify_lock = threading.Lock()
        self._notified_batches: set[frozenset[tuple[str, Optional[float]]]] = set()
        self._stopping = threading.Event()
        self._pending_changed = threading.Event()
        self.manager = JobManager(
            self._run_job,
            max_concurrent=max_concurrent,
            per_owner_limit=CLOUD_MAX_CONCURRENT_PER_VISITOR,
            # Only jobs of visitors who may not touch this machine's files: a private install also has those on a LAN.
            abandon_after=CLOUD_ABANDON_AFTER_SECONDS,
            retention_seconds=CLOUD_RETENTION_SECONDS,
            max_restricted_seconds=CLOUD_MAX_JOB_SECONDS,
            on_finished=self._on_job_finished,
            on_queue_change=self._save_pending_jobs,
            on_forget=self._on_job_forgotten,
            on_housekeeping=self._housekeep,
            hold_reason=self.hold_reason,
        )
        self._threads: list[threading.Thread] = []
        self._engine_lock = acquire_engine_lock(self.storage.data_dir) if self.storage else None
        # Another app process already runs the engine for this data folder. Both restoring the unfinished jobs, or
        # both checking subscriptions, would download everything twice, and each would overwrite the other's list of
        # unfinished jobs. This one then only runs what is queued in it.
        self.shares_data_folder = self.storage is not None and self._engine_lock is None
        if not self.shares_data_folder:
            self._restore_pending_jobs()
        purge_stale_output_dirs(known_job_ids=set())
        remove_stale_cookie_dirs()
        if self.storage and not self.shares_data_folder:
            self._start_thread(self._pending_save_loop, "videocap-pending-jobs")
            self._start_thread(self._subscription_loop, "videocap-subscriptions")

    def _start_thread(self, target: Any, name: str) -> None:
        thread = threading.Thread(target=target, name=name, daemon=True)
        thread.start()
        self._threads.append(thread)

    def shutdown(self) -> None:
        """Stops the background threads and lets another process take the data folder. Running jobs finish on their own."""
        self._stopping.set()
        self._pending_changed.set()
        self.manager.shutdown()
        if self._engine_lock:
            release_engine_lock(self._engine_lock)
            self._engine_lock = None

    # Submitting work

    def open_job_slots(self, owner: str, limits: AccessLimits, client: str = "") -> Optional[int]:
        """How many more jobs this visitor may queue, or None when there is no limit."""
        if limits.trusted:
            return None
        if self.storage_problem(owner, limits):
            return 0
        restricted = [job for job in self.manager.unfinished_jobs() if job.restricted]
        slots = [
            CLOUD_MAX_PENDING_PER_VISITOR - len(self.manager.unfinished_jobs(owner)),
            CLOUD_MAX_PENDING_TOTAL - len(restricted),
        ]
        if client:
            slots.append(CLOUD_MAX_PENDING_PER_ADDRESS - sum(1 for job in restricted if job.client == client))
        return max(min(slots), 0)

    def storage_problem(self, owner: str, limits: AccessLimits) -> str:
        """Why the files kept on a public host leave no room for this visitor's next job, or "" when there is room."""
        if limits.trusted:
            return ""
        free = get_free_disk_bytes(CLOUD_OUTPUT_ROOT)
        if free is not None and free < CLOUD_MIN_FREE_BYTES:
            return "這個公開主機的儲存空間暫時不足，請稍後再試，或在自己的電腦上執行。"
        stored = sum(
            directory_size(job.payload.private_output_dir)
            for job in self.manager.list_jobs(owner)
            if isinstance(job.payload, DownloadPayload) and job.payload.private_output_dir
        )
        if stored >= CLOUD_MAX_STORED_BYTES_PER_VISITOR:
            return (
                f"你在這個公開主機暫存的檔案已達 {format_bytes(CLOUD_MAX_STORED_BYTES_PER_VISITOR)} 上限。"
                "請先存下需要的檔案，再到「下載佇列」把已結束的任務從清單移除。"
            )
        return ""

    def validate_spec(self, spec: JobSpec, limits: AccessLimits) -> DownloadOptions:
        """Checks a job's settings at once, so the person hears about a bad time range now rather than from a failed job."""
        return options_from_spec(spec, limits, archive_path=self.storage.archive_path if self.storage else None)

    def submit_download(
        self,
        spec: JobSpec,
        owner: str,
        limits: AccessLimits,
        cookie_bytes: Optional[bytes] = None,
        batch_id: str = "",
        not_before: Optional[float] = None,
        client: str = "",
    ) -> Job:
        self.validate_spec(spec, limits)
        problem = self.storage_problem(owner, limits)
        if problem:
            raise ValueError(problem)
        if self.open_job_slots(owner, limits, client) == 0:
            raise ValueError(PUBLIC_QUEUE_FULL_MESSAGE)
        job_id = new_job_id()
        private_dir = None if limits.trusted else CLOUD_OUTPUT_ROOT / job_id
        if private_dir:
            spec = replace(spec, output_dir=str(private_dir))
        job = Job(
            id=job_id,
            owner=owner,
            kind=KIND_DOWNLOAD,
            label=spec.url,
            payload=DownloadPayload(spec, limits, private_dir),
            batch_id=batch_id,
            restricted=not limits.trusted,
            client=client,
            exclusive=bool(cookie_bytes or spec.browser or spec.needs_cookie_file),
            cookie_bytes=cookie_bytes,
            not_before=not_before,
        )
        return self.manager.submit(job)

    def submit_tool(self, request: ToolRequest, owner: str) -> Job:
        """The file tools read and write any path on this machine, so only its owner may queue them; every trusted
        visitor is LOCAL_OWNER, and the tools page hides them from everyone else."""
        if owner != LOCAL_OWNER:
            raise PermissionError(OWNER_ONLY_MESSAGE)
        job = Job(
            id=new_job_id(),
            owner=owner,
            kind=KIND_TOOL,
            label=f"{TOOL_LABELS.get(request.tool, request.tool)}：{Path(request.input_path).name}",
            payload=request,
        )
        return self.manager.submit(job)

    def resume_job(self, job: Job) -> None:
        """Continues a paused job. Finished files and partial downloads are kept, and only the rest is fetched."""
        if isinstance(job.payload, DownloadPayload):
            # Otherwise pausing and resuming would let a public job grow past every limit.
            problem = self.storage_problem(job.owner, job.payload.limits)
            if problem:
                job.status_text = problem
                return
            job.payload = replace(job.payload, spec=job.payload.spec.as_resuming())
        self.manager.resume(job.id)

    def retry_job(self, job: Job) -> bool:
        """Runs a failed or cancelled job again, keeping whatever it already finished. False when the visitor's queue is full."""
        payload = job.payload
        if isinstance(payload, DownloadPayload):
            if self.open_job_slots(job.owner, payload.limits, job.client) == 0:
                return False
            payload = replace(payload, spec=payload.spec.as_resuming())
        # Someone chose to try again, so a dropped connection gets its automatic retries afresh.
        job.auto_retries = 0
        self.manager.restart(job.id, payload)
        return True

    def retry_now(self, job: Job) -> None:
        """重試 on a download waiting for its automatic retry: it starts at once instead."""
        job.auto_retries = 0
        self.manager.start_now(job.id)

    def _retry_after_network_loss(self, job: Job) -> bool:
        """Queues a failed download of the owner's again a while later, after its connection dropped, keeping what it
        already has. Returns whether it did. A visitor's page may be gone by then, and a rate limit has its pause."""
        payload = job.payload
        if job.owner != LOCAL_OWNER or not isinstance(payload, DownloadPayload) or job.auto_retries >= AUTO_RETRY_LIMIT:
            return False
        if payload.spec.needs_cookie_file:
            # Its uploaded login went when it stopped, and the retry would fail for want of it instead.
            return False
        errors = job_errors([job])
        if not any(is_network_loss(message) for message in errors) or any(is_rate_limited(message) for message in errors):
            return False
        delay = AUTO_RETRY_DELAYS_SECONDS[min(job.auto_retries, len(AUTO_RETRY_DELAYS_SECONDS) - 1)]
        job.auto_retries += 1
        # The error itself is cleared by the restart; the log keeps it.
        job.logs.append(f"網路錯誤，{delay // 60} 分鐘後自動重試（第 {job.auto_retries} 次）：{errors[0]}")
        if payload.spec.subscription_id and self.storage:
            self.storage.update_subscription(payload.spec.subscription_id, last_result="網路中斷，稍後自動重試")
        self.manager.restart(job.id, replace(payload, spec=payload.spec.as_resuming()), not_before=time.time() + delay)
        return True

    def retry_failed_videos(self, job: Job, cookie_bytes: Optional[bytes] = None) -> list[Job]:
        """For a finished playlist with a few failures: queues only those videos instead of the whole playlist again.

        A playlist that needed a login needs it for the retry too: pass the session's cookies, or put them on the job
        first as has_login_for does. The finished job only holds them long enough to hand them on."""
        login = cookie_bytes if cookie_bytes is not None else job.cookie_bytes
        job.cookie_bytes = None
        payload = job.payload
        outcome = cast(Optional[DownloadOutcome], job.outcome)
        if job.state != DONE or not isinstance(payload, DownloadPayload) or not outcome or not outcome["failed_ids"]:
            return []
        open_slots = self.open_job_slots(job.owner, payload.limits, job.client)
        if open_slots is not None and open_slots < len(outcome["failed_ids"]):
            return []
        retried: list[Job] = []
        for video_id in outcome["failed_ids"]:
            spec = replace(
                payload.spec, url=f"https://www.youtube.com/watch?v={video_id}", multiple_links=True, playlist_items="", video_ids=[]
            )
            retried.append(self.submit_download(spec, job.owner, payload.limits, login, job.batch_id, client=job.client))
        outcome["errors"] = []
        outcome["failed_ids"] = []
        return retried

    def forget_owner(self, owner: str, limits: AccessLimits) -> None:
        """清除我的資料 for a visitor: drops their login, cancels every job of theirs and removes it with its private
        folder, at once or, for a job still stopping, the moment it stops."""
        if limits.trusted or owner == LOCAL_OWNER:
            # The owner's jobs are this machine's downloads, which 清除已結束 and the library look after.
            raise PermissionError("只有公開網站的訪客可以清除自己的資料。")
        self.manager.forget_owner(owner)

    def update_ytdlp(self, proxy: str = "") -> tuple[bool, str]:
        """Installs the newest yt-dlp, only while nothing downloads. Afterwards downloads wait for a restart: the
        running app keeps part of the old copy in memory and reads the rest from disk, and YouTube rejects the mix."""
        if self.manager.hold(KIND_DOWNLOAD):
            if not self.restart_required:
                self.manager.release(KIND_DOWNLOAD)
            return False, YTDLP_UPDATE_BUSY_MESSAGE
        result = install_ytdlp_update(proxy=proxy)
        # When pip found nothing newer to install, the running copy stays usable.
        if result.restart_required:
            self.restart_required = True
        elif not self.restart_required:
            self.manager.release(KIND_DOWNLOAD)
        return result.succeeded, result.message

    # When the owner's downloads may start

    def save_download_window(self, window: DownloadWindow, limits: AccessLimits) -> None:
        require_owner(limits)
        if self.storage:
            self.storage.save_download_window(window)
        self.download_window = window
        # A wider window lets waiting jobs start now rather than at the next tick.
        self.manager.wake()

    def hold_reason(self, job: Job, now: Optional[float] = None) -> str:
        """Why a waiting download of this machine's owner may not start yet, or "", and for a job the rate-limit
        pause stopped, why it waits. A public visitor's job is never held, because nobody at this machine is there
        for them and their page may be gone; tools need no network."""
        if job.kind != KIND_DOWNLOAD or job.owner != LOCAL_OWNER:
            return ""
        now = time.time() if now is None else now
        pause = self.rate_limit_pause
        limited = pause is not None and now < pause.until
        if job.state == PAUSED:
            return RATE_LIMIT_HOLD_TEXT if pause and limited and job.id in pause.paused_ids else ""
        if job.state != QUEUED or (job.not_before and job.not_before > now):
            # Its own start time comes first, and says so on its card.
            return ""
        if limited:
            return RATE_LIMIT_HOLD_TEXT
        window = self.download_window
        return "" if window.is_open(datetime.fromtimestamp(now)) else f"{WINDOW_HOLD_PREFIX} {window.label}"

    def downloads_held(self, now: Optional[float] = None) -> bool:
        """Whether the owner's downloads wait now, so that a subscription check would only queue another one."""
        now = time.time() if now is None else now
        return self.rate_limit_until(now) is not None or not self.download_window.is_open(datetime.fromtimestamp(now))

    def rate_limit_until(self, now: Optional[float] = None) -> Optional[float]:
        """When the rate-limit pause ends, or None when none holds."""
        pause = self.rate_limit_pause
        return pause.until if pause and (time.time() if now is None else now) < pause.until else None

    def pause_for_rate_limit(self, job: Job) -> bool:
        """Rests the owner's whole download queue after YouTube limited this job: waiting jobs stay waiting, running
        ones are paused and kept, and all of them continue by themselves later. False when a pause already holds,
        which a second failure neither lengthens nor announces again."""
        now = time.time()
        with self._rate_limit_lock:
            current = self.rate_limit_pause
            if current and now < current.until:
                return False
            # One that ran out but was not ended yet still owes its jobs their continuation.
            pause = RateLimitPause(first_error([job]), now + RATE_LIMIT_PAUSE_SECONDS, set(current.paused_ids) if current else set())
            # In place before anything is paused, so the slot a paused job frees goes to nobody.
            self.rate_limit_pause = pause
        for other in self.manager.active_jobs(LOCAL_OWNER):
            payload = other.payload
            if other.kind != KIND_DOWNLOAD or other.state != RUNNING:
                continue
            if isinstance(payload, DownloadPayload) and payload.spec.needs_cookie_file:
                # Its uploaded login goes when it stops, and only the person's own page can give it back.
                continue
            self.manager.pause(other.id)
            # A live recording cannot pause, and goes on.
            if other.stop_mode == STOP_PAUSE:
                pause.paused_ids.add(other.id)
        clock = time.strftime("%H:%M", time.localtime(pause.until))
        self._notify("下載佇列已暫停", f"{RATE_LIMIT_NOTICE.format(clock=clock)}\n{job.title}：{pause.reason}")
        return True

    def end_rate_limit_pause(self, limits: AccessLimits) -> None:
        """立即繼續 and 全部繼續: the owner chooses not to wait."""
        require_owner(limits)
        with self._rate_limit_lock:
            pause, self.rate_limit_pause = self.rate_limit_pause, None
        if pause:
            self._continue_after_pause(pause)

    def _finish_rate_limit_pause(self, now: Optional[float] = None) -> None:
        # Waiting jobs start the moment the time is up; this continues the paused ones, at the next housekeeping.
        with self._rate_limit_lock:
            pause = self.rate_limit_pause
            if pause is None or (time.time() if now is None else now) < pause.until:
                return
            self.rate_limit_pause = None
        self._continue_after_pause(pause)

    def _continue_after_pause(self, pause: RateLimitPause) -> None:
        # A copy: the pause may still be stopping jobs on the thread whose failure started it.
        for job_id in list(pause.paused_ids):
            job = self.manager.get(job_id)
            if job and job.state == PAUSED:
                self.resume_job(job)
        self.manager.wake()

    # Backups

    def create_backup(self, limits: AccessLimits) -> bytes:
        require_owner(limits)
        if not self.storage:
            raise ValueError(OWNER_ONLY_MESSAGE)
        with tempfile.TemporaryDirectory(prefix="videocap-backup-") as workdir:
            target = Path(workdir) / "backup.zip"
            self.storage.write_backup(target)
            return target.read_bytes()

    def restore_backup(self, data: bytes, limits: AccessLimits) -> str:
        """Replaces history, subscriptions, settings and the download archive with a backup's. Refused while anything
        runs, which would write to the database midway. Raises ValueError with a message for the person."""
        require_owner(limits)
        if not self.storage:
            raise ValueError(OWNER_ONLY_MESSAGE)
        if self.subscription_import and not self.subscription_import.finished:
            raise ValueError("訂閱匯入還在進行中。請等它完成或先停止，再還原備份。")
        kinds = (KIND_DOWNLOAD, KIND_TOOL)
        # Held first, so a queued job cannot start between the check and the swap.
        running = [job for kind in kinds for job in self.manager.hold(kind)]
        try:
            if running:
                raise ValueError("還有任務正在執行。請等它們完成或先暫停，再還原備份。")
            with tempfile.TemporaryDirectory(prefix="videocap-restore-") as workdir:
                backup = Path(workdir) / "backup.zip"
                backup.write_bytes(data)
                history, subscriptions = self.storage.restore_backup(backup)
            self._repoint_subscription_jobs()
        finally:
            for kind in kinds:
                # After a yt-dlp update downloads stay held until the restart.
                if kind != KIND_DOWNLOAD or not self.restart_required:
                    self.manager.release(kind)
        saved_concurrency = self.storage.get_value(MAX_CONCURRENT_KEY)
        if isinstance(saved_concurrency, int) and saved_concurrency > 0:
            self.manager.set_max_concurrent(saved_concurrency)
        self.download_window = self.storage.load_download_window()
        self.manager.wake()
        return f"已還原備份：{history} 筆下載紀錄、{subscriptions} 個訂閱，以及設定組合與已下載紀錄。"

    def _repoint_subscription_jobs(self) -> None:
        """After a restore, a subscription's number may name another subscription. A check still waiting keeps its
        link, by URL, to the subscription that queued it, if the backup has it, so its downloads never count toward
        another one's retention rule."""
        if not self.storage:
            return
        restored = {subscription.url: subscription.id for subscription in self.storage.list_subscriptions()}
        for job in self.manager.unfinished_jobs(LOCAL_OWNER):
            payload = job.payload
            if isinstance(payload, DownloadPayload) and payload.spec.subscription_id:
                spec = replace(payload.spec, subscription_id=restored.get(payload.spec.url))
                job.payload = replace(payload, spec=spec)
        self._save_pending_jobs()

    def import_download_archive(self, data: bytes, limits: AccessLimits) -> tuple[int, int, int]:
        """Adds a yt-dlp download archive's YouTube videos to this app's, so they are skipped from now on. Returns how
        many were new, how many were already there, and how many lines were not YouTube videos."""
        require_owner(limits)
        if not self.storage:
            raise ValueError(OWNER_ONLY_MESSAGE)
        video_ids, other = archive_ids_from_text(decode_text(data))
        unique = list(dict.fromkeys(video_ids))
        added = self.storage.add_to_archive(unique)
        return added, len(unique) - added, other

    # Running

    def _run_job(self, job: Job, context: JobContext) -> str:
        if job.kind == KIND_TOOL:
            return self._run_tool_job(job, context)
        return self._run_download_job(job, context)

    def _run_download_job(self, job: Job, context: JobContext) -> str:
        payload = cast(DownloadPayload, job.payload)
        spec = payload.spec
        if spec.needs_cookie_file and job.cookie_bytes is None:
            # Logged out, every members-only video would fail again without saying why.
            job.error = LOGIN_NEEDED_MESSAGE
            return FAILED
        if not can_write_folder(Path(spec.output_dir)):
            # Otherwise yt-dlp fails each video with a raw Errno 13, which on a public host also names the server's folder.
            job.error = (
                f"下載資料夾無法寫入：{spec.output_dir}。請換一個資料夾，或檢查資料夾權限。"
                if payload.limits.trusted
                else PUBLIC_OUTPUT_NOT_WRITABLE_MESSAGE
            )
            return FAILED
        with temporary_cookie_file(job.cookie_bytes) as cookie_file:
            options = options_from_spec(
                spec,
                payload.limits,
                cookie_file=cookie_file,
                archive_path=self.storage.archive_path if self.storage else None,
            )
            outcome = run_download(job_url(spec), options, context, context, context.should_stop, context.log)
        job.outcome = outcome

        if outcome["stopped"]:
            return STOPPED
        return DONE if outcome["items"] or not outcome["errors"] else FAILED

    def _run_tool_job(self, job: Job, context: JobContext) -> str:
        result = run_tool(cast(ToolRequest, job.payload), context, context.should_stop)
        job.outcome = result
        if result.stopped:
            return STOPPED
        if result.error:
            job.error = result.error
            return FAILED
        return DONE

    def _on_job_finished(self, job: Job) -> None:
        if job.kind != KIND_DOWNLOAD:
            return
        payload = cast(DownloadPayload, job.payload)
        outcome = cast(Optional[DownloadOutcome], job.outcome)
        if job.state == FAILED and self._retry_after_network_loss(job):
            # Waiting again rather than finished: no history, no message, and its batch is not over yet.
            return
        if job.state == CANCELED:
            if payload.private_output_dir:
                shutil.rmtree(payload.private_output_dir, ignore_errors=True)
            elif outcome:
                remove_partial_files(outcome["partial_paths"], keep=reported_paths(outcome))

        # A cancelled playlist keeps the videos it finished, so those belong in the history too.
        subscription_id = payload.spec.subscription_id
        if job.state in (DONE, CANCELED) and self.storage and payload.limits.trusted and outcome and not job.history_saved:
            job.history_saved = True
            self.storage.add_history(history_rows(outcome, subscription_id))

        if subscription_id and self.storage and job.state in (DONE, FAILED):
            # 「沒有新影片」 would read as a check that worked, such as for a channel that does not exist. A check that
            # failed before it had an outcome, such as for an unwritable folder, would otherwise stay 「檢查中...」.
            if job.state == FAILED or not outcome:
                summary = f"檢查失敗：{first_error([job])}"
            else:
                summary = f"下載 {len(outcome['items'])} 部" if outcome["items"] else "沒有新影片"
                if outcome["errors"]:
                    summary += f"，{len(outcome['errors'])} 項失敗"
            subscription = self.storage.get_subscription(subscription_id)
            if subscription and subscription.has_retention:
                deleted, problems = self.apply_retention(subscription)
                if deleted:
                    summary += f"，依保留規則刪除 {deleted} 部"
                if problems:
                    summary += f"，{len(problems)} 個檔案無法刪除"
            self.storage.update_subscription(subscription_id, last_result=summary)

        # Also a playlist that finished only some of its videos: the rest of the queue would meet the same limit.
        rate_limited = (
            job.owner == LOCAL_OWNER and job.state in (DONE, FAILED) and any(is_rate_limited(message) for message in job_errors([job]))
        )
        if rate_limited:
            self.pause_for_rate_limit(job)
        # The pause's own message names this failure, and a failure within the pause is no news.
        if payload.limits.trusted and job.state in (DONE, FAILED) and not (rate_limited and job.state == FAILED):
            self._notify_finished(job)

    def set_autostart(self, enabled: bool, limits: AccessLimits) -> str:
        """Starts the app hidden at each Windows sign-in, or stops doing so. Returns a problem for the person, or ""."""
        require_owner(limits)
        if self.is_public:
            return OWNER_ONLY_MESSAGE
        return enable_autostart() if enabled else disable_autostart()

    # Notifications

    def notify_targets(self) -> NotifyTargets:
        return targets_from_dict(self.storage.get_value(NOTIFY_TARGETS_KEY)) if self.storage else NotifyTargets()

    def save_notify_targets(self, targets: NotifyTargets, limits: AccessLimits) -> None:
        """Raises ValueError with what is wrong, in words for the person."""
        require_owner(limits)
        if not self.storage:
            raise ValueError(OWNER_ONLY_MESSAGE)
        problems = target_problems(targets)
        if problems:
            raise ValueError(" ".join(problems))
        self.storage.set_value(NOTIFY_TARGETS_KEY, targets.to_dict())

    def send_test_notification(self, limits: AccessLimits) -> list[str]:
        """Sends at once, so the page can say which target failed. Returns the problems."""
        require_owner(limits)
        return send_all(self.notify_targets(), "測試通知", "YouTube影片下載平台的通知設定成功。", self._owner_proxy())

    def _owner_proxy(self) -> str:
        # The network the owner set for YouTube is often the only way out of a company or school network.
        saved = self.storage.get_value(LAST_SETTINGS_KEY) if self.storage else None
        proxy = saved.get("proxy") if isinstance(saved, dict) else ""
        return proxy if isinstance(proxy, str) else ""

    def _notify(self, title: str, message: str) -> None:
        targets = self.notify_targets()
        if targets.is_set:
            send_in_background(targets, title, message, self._owner_proxy())

    def _notify_finished(self, job: Job) -> None:
        """One message per subscription check that found something, and one per batch of links once all of it is done,
        so queueing a hundred links does not send a hundred messages."""
        payload = cast(DownloadPayload, job.payload)
        subscription_id = payload.spec.subscription_id
        if subscription_id:
            subscription = self.storage.get_subscription(subscription_id) if self.storage else None
            name = subscription.title if subscription else payload.spec.url
            items = len(cast(DownloadOutcome, job.outcome)["items"]) if job.outcome else 0
            if items:
                self._notify("訂閱有新影片", f"「{name}」下載了 {items} 部新影片{error_summary([job])}")
            elif job.state == FAILED:
                self._notify("訂閱檢查失敗", f"「{name}」：{first_error([job])}")
            return

        batch = [item for item in self.manager.list_jobs(job.owner) if job.batch_id and item.batch_id == job.batch_id] or [job]
        if any(item.state in UNFINISHED_STATES for item in batch):
            return
        with self._notify_lock:
            # Two jobs of a batch finishing together would both see it complete. A retry finishes later, so it differs.
            snapshot = frozenset((item.id, item.finished_at) for item in batch)
            if snapshot in self._notified_batches:
                return
            self._notified_batches.add(snapshot)
        done = [item for item in batch if item.state == DONE]
        failed = [item for item in batch if item.state == FAILED]
        files = sum(len(cast(DownloadOutcome, item.outcome)["items"]) for item in done if item.outcome)
        if len(batch) == 1:
            if job.state == DONE:
                self._notify("下載完成", f"{job.title}\n共 {files} 個項目{error_summary(batch)}")
            else:
                self._notify("下載失敗", f"{job.title}\n{first_error(batch)}")
            return
        title = "下載完成" if not failed else "下載結束，有失敗的項目"
        message = f"{len(batch)} 個連結：完成 {len(done)} 個，共 {files} 個項目"
        if failed:
            message += f"；失敗 {len(failed)} 個"
        self._notify(title, message + error_summary(batch))

    def _on_job_forgotten(self, job: Job) -> None:
        if job.kind == KIND_DOWNLOAD:
            payload = cast(DownloadPayload, job.payload)
            if payload.private_output_dir:
                shutil.rmtree(payload.private_output_dir, ignore_errors=True)

    def _housekeep(self) -> None:
        purge_stale_output_dirs({job.id for job in self.manager.list_jobs()})
        self._finish_rate_limit_pause()

    # Surviving a restart

    def _save_pending_jobs(self) -> None:
        # One thread does the writing, so an older list can never land after a newer one, and a burst of changes
        # costs one write instead of one per job.
        if self.storage:
            self._pending_changed.set()

    def _pending_save_loop(self) -> None:
        while True:
            self._pending_changed.wait()
            if self._stopping.is_set():
                return
            self._pending_changed.clear()
            try:
                self._write_pending_jobs()
            except Exception:
                # A full disk or a locked database: the next change of the queue tries again.
                pass
            if self._stopping.wait(PENDING_SAVE_SECONDS):
                return

    def _write_pending_jobs(self) -> None:
        if self.storage:
            entries = [pending_entry(job) for job in self.manager.unfinished_jobs()]
            self.storage.set_value(PENDING_JOBS_KEY, [entry for entry in entries if entry])

    def _restore_pending_jobs(self) -> None:
        if not self.storage:
            return
        saved = self.storage.get_value(PENDING_JOBS_KEY, [])
        now = time.time()
        for entry in saved if isinstance(saved, list) else []:
            try:
                job = restored_job(entry)
            except (KeyError, TypeError, ValueError, AttributeError):
                continue
            payload = job.payload
            needs_upload = isinstance(payload, DownloadPayload) and payload.spec.needs_cookie_file
            if job.not_before and job.not_before > now and not needs_upload:
                # Someone chose when this should run, so it still runs then.
                self.manager.submit(job)
            else:
                self.manager.add_paused(job)

    # Subscriptions

    def _subscription_loop(self) -> None:
        while not self._stopping.wait(SUBSCRIPTION_CHECK_SECONDS):
            try:
                self.run_due_subscriptions()
            except Exception:
                # A failed check is retried at the next tick, and must not end the scheduler.
                pass

    def run_due_subscriptions(self) -> int:
        if not self.storage:
            return 0
        now = time.time()
        if self.downloads_held(now):
            # Still due afterwards, so the first tick once downloads may start checks them.
            return 0
        due = [subscription for subscription in self.storage.list_subscriptions() if subscription.is_due(now)]
        for subscription in due:
            try:
                self.check_subscription(subscription)
            except Exception:
                # One broken subscription must not hold up the others.
                continue
        return len(due)

    def validate_subscription(self, url: str, settings: DownloadSettings, output_dir: str) -> None:
        """Raises ValueError with a message for the person when these settings would make every check fail."""
        self.validate_spec(subscription_spec(url, settings, output_dir, 1), AccessLimits())

    def check_subscription(self, subscription: Subscription) -> Optional[Job]:
        """Queues a job for the subscription's newest videos. The download archive makes it skip those already fetched."""
        if not self.storage:
            return None
        self.storage.update_subscription(subscription.id, last_checked_at=time.time(), last_result="檢查中...")
        for job in self.manager.unfinished_jobs(LOCAL_OWNER):
            payload = job.payload
            if isinstance(payload, DownloadPayload) and payload.spec.subscription_id == subscription.id:
                if job.state == PAUSED and job.restored:
                    # Left paused by a restart, it would otherwise block every later check.
                    self.resume_job(job)
                elif job.state == PAUSED:
                    self.storage.update_subscription(subscription.id, last_result="上次的下載已暫停，請到「下載佇列」按「繼續」。")
                return job

        spec = subscription_spec(subscription.url, subscription.settings, subscription.output_dir, subscription.max_items, subscription.id)
        try:
            return self.submit_download(spec, LOCAL_OWNER, AccessLimits())
        except ValueError as exc:
            self.storage.update_subscription(subscription.id, last_result=f"設定有誤：{exc}")
            return None

    def seed_subscription_archive(
        self, url: str, limit: int, login: LoginSource, network: Optional[NetworkOptions] = None
    ) -> tuple[str, int]:
        """Marks a channel's current videos as already seen, so subscribing does not download its whole back catalogue."""
        # At least as far as a check looks, or the check would take the rest for new videos.
        title, video_ids = list_recent_video_ids(url, max(limit, subscription_depth(url, limit)), login, network)
        if self.storage:
            self.storage.add_to_archive(video_ids)
        return title, len(video_ids)

    def retention_candidates(
        self, subscription: Subscription, keep_latest: Optional[int] = None, keep_days: Optional[int] = None, now: Optional[float] = None
    ) -> list[HistoryEntry]:
        """What a retention rule would delete now: the subscription's own downloads still in its folder, past the
        newest keep_latest or downloaded more than keep_days ago. Without arguments, the rule the subscription has."""
        if not self.storage:
            return []
        keep_latest = subscription.keep_latest if keep_latest is None else keep_latest
        keep_days = subscription.keep_days if keep_days is None else keep_days
        if not keep_latest and not keep_days:
            return []
        root = Path(subscription.output_dir)
        # Newest first, one row per file: after the folder was edited, the same file can be recorded under two
        # spellings, and its older row must not make the file count as old.
        on_disk: dict[str, HistoryEntry] = {}
        for entry in self.storage.subscription_history(subscription.id):
            # A file moved away or deleted by hand is no longer the subscription's to count or delete.
            if entry.path and is_inside(Path(entry.path), root) and is_recorded_file(Path(entry.path), entry.size_bytes):
                on_disk.setdefault(file_identity(Path(entry.path)), entry)
        # "Keep the newest N" counts videos: one downloaded twice is still one.
        ranks: dict[str, int] = {}
        for entry in on_disk.values():
            ranks.setdefault(entry.video_id or f"history-{entry.id}", len(ranks))
        cutoff = (now or time.time()) - keep_days * 86400
        return [
            entry
            for entry in on_disk.values()
            if (keep_latest and ranks[entry.video_id or f"history-{entry.id}"] >= keep_latest) or (keep_days and entry.finished_at < cutoff)
        ]

    def apply_retention(self, subscription: Subscription) -> tuple[int, list[str]]:
        """Deletes what the subscription's rule says, with each video's sidecars. Returns how many videos went, and
        the problems. Their IDs stay in the download archive, so the next check does not fetch them again."""
        if not self.storage:
            return 0, []
        doomed = self.retention_candidates(subscription)
        root = Path(subscription.output_dir)
        problems: list[str] = []
        deleted = 0
        for entry in doomed:
            try:
                found = delete_entry(media_entry(Path(entry.path)), root)
            except OSError as exc:
                found = [f"{Path(entry.path).name} 無法刪除：{exc}"]
            problems.extend(found)
            deleted += not Path(entry.path).exists()
        self.storage.add_to_archive(entry.video_id for entry in doomed if entry.video_id)
        return deleted, problems

    def save_retention_rule(self, subscription_id: int, keep_latest: int, keep_days: int, limits: AccessLimits) -> tuple[int, list[str]]:
        """Saves a subscription's rule and applies it at once, as the page showed before it was saved."""
        require_owner(limits)
        if not self.storage:
            return 0, []
        self.storage.update_subscription(subscription_id, keep_latest=max(keep_latest, 0), keep_days=max(keep_days, 0))
        subscription = self.storage.get_subscription(subscription_id)
        return self.apply_retention(subscription) if subscription else (0, [])

    def start_subscription_import(
        self,
        channels: list[ImportedChannel],
        settings: DownloadSettings,
        output_dir: str,
        max_items: int,
        interval_hours: int,
        skip_existing: bool,
        limits: AccessLimits,
        cookie_bytes: Optional[bytes] = None,
        browser: Optional[str] = None,
        network: Optional[NetworkOptions] = None,
    ) -> SubscriptionImport:
        """Adds many subscriptions in the background. Seeding reads each channel from YouTube, so the channels are read
        one at a time with a pause between them. Raises ValueError with a message for the person."""
        require_owner(limits)
        if not self.storage:
            raise ValueError(OWNER_ONLY_MESSAGE)
        running = self.subscription_import
        if running and not running.finished:
            raise ValueError("上一批匯入還在進行中，請等它完成或先停止。")
        if channels:
            # The same settings go to every channel, so one check covers them all.
            self.validate_subscription(channels[0].subscription_url(settings.channel_tab), settings, output_dir)
        progress = SubscriptionImport(total=len(channels))
        self.subscription_import = progress
        self._start_thread(
            lambda: self._import_subscriptions(
                progress, channels, settings, output_dir, max_items, interval_hours, skip_existing, cookie_bytes, browser, network
            ),
            "videocap-subscription-import",
        )
        return progress

    def _import_subscriptions(
        self,
        progress: SubscriptionImport,
        channels: list[ImportedChannel],
        settings: DownloadSettings,
        output_dir: str,
        max_items: int,
        interval_hours: int,
        skip_existing: bool,
        cookie_bytes: Optional[bytes],
        browser: Optional[str],
        network: Optional[NetworkOptions],
    ) -> None:
        storage = cast(AppStorage, self.storage)
        asked_youtube = False
        try:
            with temporary_cookie_file(cookie_bytes) as cookie_file:
                login = LoginSource(cookie_file, browser)
                for channel in channels:
                    name = channel.title or channel.channel_id
                    progress.current = name
                    url = channel.subscription_url(settings.channel_tab)
                    existing = [subscription.url for subscription in storage.list_subscriptions()]
                    if url in existing or channel.channel_id in subscribed_channel_ids(existing):
                        progress.skipped.append(name)
                        continue
                    seen: Optional[int] = None
                    if skip_existing:
                        # YouTube answers a burst of listings with HTTP 429, which would fail the rest of the list.
                        if asked_youtube and self._wait_or_stop(progress, SUBSCRIPTION_IMPORT_PAUSE_SECONDS):
                            break
                        asked_youtube = True
                        try:
                            title, seen = self.seed_subscription_archive(url, max_items, login, network)
                        except Exception as exc:
                            progress.failed.append(f"{name}：{humanize_ytdlp_error(str(exc))}")
                            continue
                        name = channel.title or title
                    if self._wait_or_stop(progress, 0):
                        break
                    try:
                        subscription_id = storage.add_subscription(url, name, settings, output_dir, max_items, interval_hours)
                    except sqlite3.IntegrityError:
                        progress.skipped.append(name)
                        continue
                    if seen is not None:
                        # Just read, so the scheduler need not read every imported channel again within the minute.
                        storage.update_subscription(
                            subscription_id, last_checked_at=time.time(), last_result=f"匯入時把現有 {seen} 部影片視為已下載"
                        )
                    progress.added.append(name)
        except Exception as exc:
            progress.failed.append(f"匯入中斷：{exc}")
        finally:
            progress.current = ""
            progress.finished = True

    def _wait_or_stop(self, progress: SubscriptionImport, seconds: float) -> bool:
        """Waits, and says whether the import should end: someone pressed stop, or the app is shutting down."""
        deadline = time.time() + seconds
        while not (progress.stop_requested.is_set() or self._stopping.is_set()):
            remaining = deadline - time.time()
            if remaining <= 0:
                return False
            progress.stop_requested.wait(min(remaining, 0.2))
        return True


# Streamlit re-imports an app module when its file changes, so the one engine is kept in a module of its own.
SHARED_SERVICES_MODULE = "_videocap_shared_services"


def _shared_holder() -> types.ModuleType:
    holder = sys.modules.setdefault(SHARED_SERVICES_MODULE, types.ModuleType(SHARED_SERVICES_MODULE))
    holder.__dict__.setdefault("lock", threading.Lock())
    holder.__dict__.setdefault("instances", {})
    return holder


def get_shared_services(is_public: bool) -> AppServices:
    """The process's one engine. Not kept in st.cache_resource: any visitor can clear that cache with one message, and
    the next page would then start a second engine beside the first, with its own queue, limits and scheduler."""
    holder = _shared_holder()
    with holder.lock:
        services = holder.instances.get(is_public)
        if services is None:
            services = holder.instances[is_public] = AppServices(is_public=is_public)
        return cast(AppServices, services)


def reset_shared_services() -> None:
    """Stops the shared engine so that the next get_shared_services() builds a new one. For tests."""
    holder = _shared_holder()
    with holder.lock:
        for services in holder.instances.values():
            services.shutdown()
        holder.instances.clear()


def subscription_depth(url: str, max_items: int) -> int:
    if classify_youtube_url(url) == "playlist":
        return SUBSCRIPTION_PLAYLIST_DEPTH
    return max(max_items, SUBSCRIPTION_CHANNEL_DEPTH)


def subscription_spec(url: str, settings: DownloadSettings, output_dir: str, max_items: int, subscription_id: Optional[int] = None) -> JobSpec:
    return JobSpec(
        url=url,
        # Meant for one video: on every new video a clip range would cut it, and an item list would pick the wrong ones.
        settings=replace(settings, time_ranges="", playlist_items="", remove_ranges=""),
        output_dir=output_dir,
        multiple_links=True,
        # How far the check looks. playlist_limit then caps how many of the new videos one check downloads.
        playlist_items=f"1:{subscription_depth(url, max_items)}",
        playlist_limit=max_items,
        use_archive=True,
        subscription_id=subscription_id,
    )


def pending_entry(job: Job) -> Optional[dict[str, Any]]:
    """What a restart needs to put a job back. A public visitor's jobs are left out: their page is gone by then."""
    entry: dict[str, Any] = {"label": job.title, "batch_id": job.batch_id, "not_before": job.not_before, "auto_retries": job.auto_retries}
    if job.kind == KIND_TOOL:
        if job.owner != LOCAL_OWNER:
            return None
        entry = {**entry, "label": job.label, "kind": KIND_TOOL, "tool": asdict(cast(ToolRequest, job.payload))}
        if isinstance(job.outcome, ToolResult):
            result = job.outcome
            entry["result"] = {"output_path": str(result.output_path or ""), "error": result.error, "warning": result.warning}
        return entry
    payload = cast(DownloadPayload, job.payload)
    if not payload.limits.trusted:
        return None
    entry["spec"] = payload.spec.to_dict()
    outcome = cast(Optional[DownloadOutcome], job.outcome)
    if outcome:
        # A paused job's earlier run: without it, cancelling after a restart could neither clean up nor keep history.
        entry["outcome"] = {
            "items": outcome["items"],
            "partial_paths": outcome["partial_paths"],
            "notes": text_entries(outcome.get("notes")),
            **extra_outcome_fields(outcome),
        }
    return entry


def text_entries(saved: Any) -> list[str]:
    return [entry for entry in saved if isinstance(entry, str)] if isinstance(saved, list) else []


def extra_outcome_fields(outcome: DownloadOutcome) -> dict[str, Any]:
    """Fields a newer download step added to the outcome, such as notes for the person, kept through a restart as
    long as they can be saved as JSON."""
    extra: dict[str, Any] = {}
    for key, value in outcome.items():
        if key in DownloadOutcome.__annotations__:
            continue
        try:
            json.dumps(value)
        except (TypeError, ValueError):
            continue
        extra[key] = value
    return extra


def restored_job(entry: dict[str, Any]) -> Job:
    not_before = entry.get("not_before")
    scheduled = float(not_before) if isinstance(not_before, (int, float)) else None
    batch_id = str(entry.get("batch_id") or "")
    if entry.get("kind") == KIND_TOOL:
        tool = entry["tool"]
        request = ToolRequest(str(tool["tool"]), str(tool["input_path"]), dict(tool.get("params") or {}))
        label = str(entry.get("label") or TOOL_LABELS.get(request.tool, request.tool))
        job = Job(id=new_job_id(), owner=LOCAL_OWNER, kind=KIND_TOOL, label=label, payload=request, batch_id=batch_id, not_before=scheduled)
        job.outcome = restored_tool_result(entry.get("result"))
        return job

    spec = job_spec_from_dict(entry["spec"]).as_resuming()
    retries = entry.get("auto_retries")
    return Job(
        id=new_job_id(),
        owner=LOCAL_OWNER,
        kind=KIND_DOWNLOAD,
        label=str(entry.get("label") or spec.url),
        payload=DownloadPayload(spec, AccessLimits()),
        batch_id=batch_id,
        exclusive=bool(spec.browser or spec.needs_cookie_file),
        not_before=scheduled,
        # A retry waiting at the restart still counts toward the limit.
        auto_retries=retries if isinstance(retries, int) and retries >= 0 else 0,
        outcome=restored_outcome(entry.get("outcome")),
    )


def restored_outcome(saved: Any) -> Optional[DownloadOutcome]:
    if not isinstance(saved, dict):
        return None
    outcome = empty_outcome()
    outcome["items"] = [
        cast(DownloadedItem, item)
        for item in saved.get("items") or []
        if isinstance(item, dict) and DownloadedItem.__required_keys__ <= item.keys()
    ]
    outcome["partial_paths"] = text_entries(saved.get("partial_paths"))
    outcome["notes"] = text_entries(saved.get("notes"))
    outcome["stopped"] = True
    for key, value in saved.items():
        if key not in DownloadOutcome.__annotations__:
            outcome[key] = value  # type: ignore[literal-required]
    return outcome


def restored_tool_result(saved: Any) -> Optional[ToolResult]:
    if not isinstance(saved, dict):
        return None
    output_path = saved.get("output_path")
    return ToolResult(
        output_path=Path(output_path) if isinstance(output_path, str) and output_path else None,
        error=str(saved.get("error") or ""),
        # Finished, but with a problem worth telling, such as subtitles drawn as boxes.
        warning=str(saved.get("warning") or ""),
    )


def reported_paths(outcome: DownloadOutcome) -> set[str]:
    """Every file the job reported as done, including subtitles, which also pass through the progress hook."""
    return {path for item in outcome["items"] for path in (item["path"], *item["subtitle_paths"], *item["extra_paths"]) if path}


def job_errors(jobs: list[Job]) -> list[str]:
    errors: list[str] = []
    for job in jobs:
        outcome = cast(Optional[DownloadOutcome], job.outcome)
        errors.extend(outcome["errors"] if outcome and outcome["errors"] else [job.error] if job.error else [])
    return errors


def first_error(jobs: list[Job]) -> str:
    errors = job_errors(jobs)
    return errors[0][:300] if errors else "沒有說明原因，請到「下載佇列」查看紀錄。"


def error_summary(jobs: list[Job]) -> str:
    errors = job_errors(jobs)
    return f"\n{len(errors)} 項失敗，第一項：{errors[0][:300]}" if errors else ""


def history_rows(outcome: DownloadOutcome, subscription_id: Optional[int] = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in outcome["items"]:
        path = Path(item["path"]) if item["path"] else None
        size = path.stat().st_size if path and path.exists() else 0
        rows.append(
            {
                "video_id": item["video_id"],
                "title": item["title"],
                "channel": item["channel"],
                "url": item["webpage_url"],
                "path": item["path"],
                "size_bytes": size,
                "quality": item["quality"],
                "duration": item["duration"],
                "upload_date": item["upload_date"],
                "thumbnail": item["thumbnail"],
                "subscription_id": subscription_id,
            }
        )
    return rows


def is_recorded_file(path: Path, size_bytes: int) -> bool:
    """Whether the file a download recorded is still there. Another size means another file, such as one the person
    renamed into place after deleting the download, which is not the subscription's to delete. A size of 0 is a
    row that recorded none."""
    try:
        return path.is_file() and (not size_bytes or path.stat().st_size == size_bytes)
    except OSError:
        return False


def file_identity(path: Path) -> str:
    """The same for every spelling of one file's path, such as another letter case or slash on Windows."""
    try:
        return os.path.normcase(str(path.resolve()))
    except OSError:
        return os.path.normcase(os.path.abspath(path))


def directory_size(folder: Path) -> int:
    total = 0
    for path in folder.rglob("*") if folder.is_dir() else []:
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            continue
    return total


def purge_stale_output_dirs(known_job_ids: set[str]) -> None:
    if not CLOUD_OUTPUT_ROOT.exists():
        return

    cutoff = time.time() - CLOUD_RETENTION_SECONDS
    for job_dir in CLOUD_OUTPUT_ROOT.iterdir():
        try:
            if job_dir.name not in known_job_ids and job_dir.stat().st_mtime < cutoff:
                shutil.rmtree(job_dir, ignore_errors=True)
        except OSError:
            continue

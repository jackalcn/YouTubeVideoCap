from __future__ import annotations

import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from videocap.config import JOB_LOG_LINES, MAX_FINISHED_JOBS_KEPT, RECORDING_STATUS_PREFIX


QUEUED = "queued"
RUNNING = "running"
PAUSED = "paused"
DONE = "done"
FAILED = "failed"
CANCELED = "canceled"

ACTIVE_STATES = frozenset({QUEUED, RUNNING})
UNFINISHED_STATES = frozenset({QUEUED, RUNNING, PAUSED})
FINISHED_STATES = frozenset({DONE, FAILED, CANCELED})
STATE_LABELS = {
    QUEUED: "等待中",
    RUNNING: "進行中",
    PAUSED: "已暫停",
    DONE: "完成",
    FAILED: "失敗",
    CANCELED: "已取消",
}
# Returned by a runner that ended because someone asked it to; the manager knows whether that was a pause or a cancel.
STOPPED = "stopped"
STOP_CANCEL = "cancel"
STOP_PAUSE = "pause"
TICK_SECONDS = 1.0
HOUSEKEEPING_SECONDS = 30.0


@dataclass
class Job:
    id: str
    owner: str
    kind: str
    label: str
    payload: Any
    batch_id: str = ""
    # A visitor on a public host: limited to a few running jobs, so one person cannot take the whole server.
    restricted: bool = False
    # The visitor's network address, when it tells people apart. A new session is a new owner, but not a new address.
    client: str = ""
    # Using an account: yt-dlp's wiki warns against parallel downloads, so these run one at a time.
    exclusive: bool = False
    cookie_bytes: Optional[bytes] = None
    state: str = QUEUED
    fraction: float = 0.0
    status_text: str = "等待中"
    current_item: str = ""
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    not_before: Optional[float] = None
    # Time spent running before the last pause, so pausing and resuming does not restart a public job's time limit.
    run_seconds: float = 0.0
    # Put back paused by a restart rather than by a person, so a subscription check may continue it.
    restored: bool = False
    # How often the engine retried this by itself after the network dropped, so it gives up in the end.
    auto_retries: int = 0
    outcome: Any = None
    error: str = ""
    stop_mode: str = ""
    logs: deque[str] = field(default_factory=lambda: deque(maxlen=JOB_LOG_LINES))
    history_saved: bool = False

    @property
    def title(self) -> str:
        return self.current_item or self.label


class JobContext:
    """What a running job can reach. Bridges yt-dlp's callbacks to the job without letting them touch Streamlit."""

    def __init__(self, job: Job) -> None:
        self.job = job

    def info(self, message: str) -> None:
        self.job.current_item = message
        self.job.logs.append(message)

    def progress(self, value: float, text: str = "") -> None:
        self.job.fraction = min(max(value, 0.0), 1.0)
        if text:
            self.job.status_text = text

    def log(self, message: str) -> None:
        self.job.logs.append(message)

    def should_stop(self) -> bool:
        return bool(self.job.stop_mode)


Runner = Callable[[Job, JobContext], str]


class JobManager:
    """Runs jobs on background threads, so a Streamlit rerun or a closed page does not interrupt a download."""

    def __init__(
        self,
        runner: Runner,
        max_concurrent: int = 2,
        per_owner_limit: Optional[int] = None,
        abandon_after: Optional[float] = None,
        retention_seconds: Optional[float] = None,
        max_restricted_seconds: Optional[float] = None,
        on_finished: Optional[Callable[[Job], None]] = None,
        on_queue_change: Optional[Callable[[], None]] = None,
        on_forget: Optional[Callable[[Job], None]] = None,
        on_housekeeping: Optional[Callable[[], None]] = None,
        hold_reason: Optional[Callable[[Job, float], str]] = None,
    ) -> None:
        self.runner = runner
        self.max_concurrent = max_concurrent
        self.per_owner_limit = per_owner_limit
        self.abandon_after = abandon_after
        self.retention_seconds = retention_seconds
        self.max_restricted_seconds = max_restricted_seconds
        self.on_finished = on_finished
        self.on_queue_change = on_queue_change
        self.on_forget = on_forget
        self.on_housekeeping = on_housekeeping
        # Why a waiting job may not start yet, such as outside the owner's download hours; "" lets it start. Asked at
        # every tick, so a job starts by itself once the reason is gone.
        self.hold_reason = hold_reason
        self._lock = threading.RLock()
        self._jobs: dict[str, Job] = {}
        self._last_seen: dict[str, float] = {}
        self._ticker: Optional[threading.Thread] = None
        self._last_housekeeping = 0.0
        self._stopping = threading.Event()
        self._held_kinds: set[str] = set()
        # Jobs to remove the moment they stop, for a visitor who asked for all their data to go.
        self._forget_when_stopped: set[str] = set()

    # Queue management

    def submit(self, job: Job) -> Job:
        with self._lock:
            self._jobs[job.id] = job
            self._last_seen.setdefault(job.owner, time.time())
            self._ensure_ticker()
        self._dispatch()
        self._queue_changed()
        return job

    def add_paused(self, job: Job) -> Job:
        """Puts a job back after a restart without starting it, because its owner may no longer want it."""
        job.state = PAUSED
        job.status_text = "App 重新啟動前未完成，可繼續下載"
        job.restored = True
        with self._lock:
            self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def list_jobs(self, owner: Optional[str] = None) -> list[Job]:
        with self._lock:
            return [job for job in self._jobs.values() if owner is None or job.owner == owner]

    def active_jobs(self, owner: Optional[str] = None) -> list[Job]:
        return [job for job in self.list_jobs(owner) if job.state in ACTIVE_STATES]

    def unfinished_jobs(self, owner: Optional[str] = None) -> list[Job]:
        return [job for job in self.list_jobs(owner) if job.state in UNFINISHED_STATES]

    def cancel(self, job_id: str) -> None:
        self._stop(job_id, STOP_CANCEL)

    def pause(self, job_id: str) -> None:
        self._stop(job_id, STOP_PAUSE)

    def _stop(self, job_id: str, mode: str) -> None:
        finished_job: Optional[Job] = None
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            if job.state == RUNNING:
                # A pause must not undo a cancel the job has not noticed yet, and a live recording cannot pick up
                # where it stopped, so pausing one would end it.
                if mode == STOP_PAUSE and (job.stop_mode == STOP_CANCEL or job.status_text.startswith(RECORDING_STATUS_PREFIX)):
                    return
                # The running thread notices at its next callback.
                job.stop_mode = mode
                job.status_text = "正在暫停..." if mode == STOP_PAUSE else "正在取消..."
            elif mode == STOP_PAUSE and job.state == QUEUED:
                job.state = PAUSED
                job.status_text = STATE_LABELS[PAUSED]
                job.cookie_bytes = None
            elif mode == STOP_CANCEL and job.state in (QUEUED, PAUSED):
                job.state = CANCELED
                job.status_text = STATE_LABELS[CANCELED]
                job.finished_at = time.time()
                job.cookie_bytes = None
                finished_job = job
        if finished_job and self.on_finished:
            self._call_quietly(self.on_finished, finished_job)
        self._dispatch()
        self._queue_changed()

    def resume(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.state != PAUSED:
                return
            job.state = QUEUED
            job.stop_mode = ""
            job.status_text = STATE_LABELS[QUEUED]
            job.restored = False
            self._ensure_ticker()
        self._dispatch()
        self._queue_changed()

    def restart(self, job_id: str, payload: Any = None, not_before: Optional[float] = None) -> None:
        """Runs a finished job again in place, keeping its position and history in the list."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.state not in FINISHED_STATES:
                return
            if payload is not None:
                job.payload = payload
            if not_before is not None:
                job.not_before = not_before
            job.state = QUEUED
            job.fraction = 0.0
            job.status_text = STATE_LABELS[QUEUED]
            job.stop_mode = ""
            job.outcome = None
            job.error = ""
            job.started_at = None
            job.finished_at = None
            job.run_seconds = 0.0
            job.history_saved = False
            self._ensure_ticker()
        self._dispatch()
        self._queue_changed()

    def start_now(self, job_id: str) -> None:
        """Lets a waiting job start without waiting for its start time, such as a retry the engine scheduled."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.state != QUEUED:
                return
            job.not_before = None
            self._ensure_ticker()
        self._dispatch()
        self._queue_changed()

    def wake(self) -> None:
        """Starts what may start now, after something outside the queue changed, such as the download hours."""
        with self._lock:
            self._ensure_ticker()
        self._dispatch()

    def remove(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.state in ACTIVE_STATES:
                return
            del self._jobs[job_id]
            self._forget_when_stopped.discard(job_id)
        if self.on_forget:
            self._call_quietly(self.on_forget, job)
        self._queue_changed()

    def clear_finished(self, owner: Optional[str] = None) -> int:
        finished = [job for job in self.list_jobs(owner) if job.state in FINISHED_STATES]
        for job in finished:
            self.remove(job.id)
        return len(finished)

    def move(self, job_id: str, offset: int) -> None:
        """Swaps a waiting job with its neighbour among the waiting jobs. Running and finished jobs keep their place."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.state != QUEUED:
                return
            ids = list(self._jobs)
            waiting = [item_id for item_id in ids if self._jobs[item_id].state == QUEUED and self._jobs[item_id].owner == job.owner]
            target = waiting.index(job_id) + offset
            if not 0 <= target < len(waiting):
                return
            first, second = ids.index(job_id), ids.index(waiting[target])
            ids[first], ids[second] = ids[second], ids[first]
            self._jobs = {item_id: self._jobs[item_id] for item_id in ids}
        self._queue_changed()

    def set_max_concurrent(self, value: int) -> None:
        self.max_concurrent = max(1, value)
        self._dispatch()

    def touch(self, owner: str) -> None:
        """Records that this visitor's page is still open."""
        with self._lock:
            self._last_seen[owner] = time.time()

    def forget_login(self, owner: str) -> None:
        """Drops the uploaded login from all of a visitor's jobs, for when they remove their cookies.txt."""
        with self._lock:
            for job in self._jobs.values():
                if job.owner == owner:
                    job.cookie_bytes = None

    def forget_owner(self, owner: str) -> None:
        """Drops a visitor's login, cancels all their jobs and removes them. One still running is removed as soon as
        it stops, however long that takes, so nothing of theirs is left behind."""
        with self._lock:
            jobs = [job for job in self._jobs.values() if job.owner == owner]
            for job in jobs:
                job.cookie_bytes = None
                self._forget_when_stopped.add(job.id)
        for job in jobs:
            self.cancel(job.id)
            self.remove(job.id)

    def hold(self, kind: str) -> list[Job]:
        """Keeps jobs of this kind waiting in the queue, for example downloads while yt-dlp is replaced. Returns
        those already running, which the hold cannot stop."""
        with self._lock:
            self._held_kinds.add(kind)
            return [job for job in self._jobs.values() if job.kind == kind and job.state == RUNNING]

    def release(self, kind: str) -> None:
        with self._lock:
            self._held_kinds.discard(kind)
        self._dispatch()

    def shutdown(self) -> None:
        """Stops the background thread. Running jobs finish on their own threads."""
        self._stopping.set()

    def job_logs(self, job_id: str) -> list[str]:
        with self._lock:
            job = self._jobs.get(job_id)
            return list(job.logs) if job else []

    # Scheduling

    def _ensure_ticker(self) -> None:
        if self._ticker is None or not self._ticker.is_alive():
            self._ticker = threading.Thread(target=self._tick_loop, name="videocap-jobs", daemon=True)
            self._ticker.start()

    def _tick_loop(self) -> None:
        # Scheduled jobs and housekeeping have no event to wake them.
        while not self._stopping.wait(TICK_SECONDS):
            self._dispatch()
            if time.time() - self._last_housekeeping >= HOUSEKEEPING_SECONDS:
                self._last_housekeeping = time.time()
                self._housekeep()

    def _can_start(self, job: Job, running: list[Job], now: float) -> bool:
        if job.not_before and job.not_before > now:
            return False
        if job.kind in self._held_kinds:
            return False
        if self.hold_reason and self.hold_reason(job, now):
            return False
        if job.exclusive and any(item.exclusive for item in running):
            return False
        if job.restricted and self.per_owner_limit is not None:
            same_visitor = sum(1 for item in running if item.owner == job.owner or (job.client and item.client == job.client))
            if same_visitor >= self.per_owner_limit:
                return False
        return True

    def _dispatch(self) -> None:
        now = time.time()
        with self._lock:
            running = [job for job in self._jobs.values() if job.state == RUNNING]
            for job in self._jobs.values():
                if len(running) >= self.max_concurrent:
                    break
                if job.state == QUEUED and self._can_start(job, running, now):
                    job.state = RUNNING
                    job.started_at = now
                    job.status_text = "準備中..."
                    running.append(job)
                    threading.Thread(target=self._run_job, args=(job,), name=f"videocap-{job.id[:8]}", daemon=True).start()

    def _run_job(self, job: Job) -> None:
        context = JobContext(job)
        result_state = FAILED
        try:
            result_state = self.runner(job, context)
        except Exception as exc:
            job.error = str(exc)
            job.logs.append(f"任務失敗：{exc}")

        with self._lock:
            stop_mode = job.stop_mode
            job.stop_mode = ""
            if result_state == STOPPED:
                job.state = PAUSED if stop_mode == STOP_PAUSE else CANCELED
            else:
                # The job may have finished just as someone pressed stop, and then there is nothing left to stop.
                job.state = result_state
            job.status_text = job.error or STATE_LABELS[job.state]
            if job.state in FINISHED_STATES:
                job.finished_at = time.time()
            if job.state == DONE:
                job.fraction = 1.0
            if job.started_at:
                job.run_seconds += time.time() - job.started_at
            # An uploaded login belongs to the browser session that uploaded it. Resuming or retrying takes it from
            # that session again, so a job someone left behind cannot be rerun with their login.
            job.cookie_bytes = None

        if self.on_finished:
            self._call_quietly(self.on_finished, job)
        with self._lock:
            forgotten = job.id in self._forget_when_stopped
        if forgotten:
            self.remove(job.id)
        self._dispatch()
        self._queue_changed()

    def _housekeep(self) -> None:
        now = time.time()
        with self._lock:
            jobs = list(self._jobs.values())
            last_seen = dict(self._last_seen)

        if self.abandon_after is not None:
            for job in jobs:
                seen = last_seen.get(job.owner)
                if job.restricted and job.state in UNFINISHED_STATES and seen is not None and now - seen > self.abandon_after:
                    self.cancel(job.id)

        if self.max_restricted_seconds is not None:
            for job in jobs:
                if job.restricted and job.state == RUNNING and job.started_at:
                    if job.run_seconds + now - job.started_at > self.max_restricted_seconds:
                        self.cancel(job.id)

        finished = sorted(
            (job for job in jobs if job.state in FINISHED_STATES and job.finished_at), key=lambda job: job.finished_at or 0.0
        )
        expired = [
            job
            for job in finished
            if job.restricted and self.retention_seconds and now - (job.finished_at or now) > self.retention_seconds
        ]
        # Grouped in one pass: a public host can hold thousands of visitors, and this runs on the dispatch thread.
        finished_by_owner: defaultdict[str, list[Job]] = defaultdict(list)
        for job in finished:
            finished_by_owner[job.owner].append(job)
        overflow: list[Job] = []
        for owned in finished_by_owner.values():
            overflow.extend(owned[: max(len(owned) - MAX_FINISHED_JOBS_KEPT, 0)])
        for job in {item.id: item for item in [*expired, *overflow]}.values():
            self.remove(job.id)

        # Every visitor without the cookie counts as new, so their entries would otherwise pile up forever.
        keep_seconds = max(self.abandon_after or 0.0, HOUSEKEEPING_SECONDS)
        with self._lock:
            owners = {job.owner for job in self._jobs.values()}
            for owner, seen in list(self._last_seen.items()):
                if owner not in owners and now - seen > keep_seconds:
                    del self._last_seen[owner]

        if self.on_housekeeping:
            self._call_quietly(self.on_housekeeping)

    def _queue_changed(self) -> None:
        if self.on_queue_change:
            self._call_quietly(self.on_queue_change)

    @staticmethod
    def _call_quietly(callback: Callable[..., None], *args: Any) -> None:
        # A failure in saving history or cleaning up must not take down the job thread.
        try:
            callback(*args)
        except Exception:
            pass


def new_job_id() -> str:
    return uuid.uuid4().hex

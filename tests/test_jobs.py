from __future__ import annotations

import threading
import time
from typing import Callable

from videocap.config import RECORDING_STATUS_PREFIX
from videocap.jobs import CANCELED, DONE, FAILED, PAUSED, QUEUED, RUNNING, STOPPED, Job, JobContext, JobManager, new_job_id


def make_job(owner: str = "me", **overrides: object) -> Job:
    return Job(id=new_job_id(), owner=owner, kind="test", label="job", payload=None, **overrides)  # type: ignore[arg-type]


def wait_until(condition: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not met in time")


class BlockingRunner:
    """A job that runs until the test releases it or asks it to stop."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.started: list[str] = []
        self.lock = threading.Lock()

    def __call__(self, job: Job, context: JobContext) -> str:
        with self.lock:
            self.started.append(job.id)
        while not self.release.is_set():
            if context.should_stop():
                return STOPPED
            time.sleep(0.005)
        return DONE


def test_job_runs_and_reports_progress() -> None:
    def runner(job: Job, context: JobContext) -> str:
        context.info("Title")
        context.progress(0.5, "half")
        context.log("line")
        return DONE

    manager = JobManager(runner)
    job = manager.submit(make_job())
    wait_until(lambda: job.state == DONE)
    assert job.current_item == "Title"
    assert job.fraction == 1.0
    assert manager.job_logs(job.id) == ["Title", "line"]


def test_runner_exception_marks_the_job_failed() -> None:
    def runner(job: Job, context: JobContext) -> str:
        raise RuntimeError("boom")

    manager = JobManager(runner)
    job = manager.submit(make_job())
    wait_until(lambda: job.state == FAILED)
    assert job.error == "boom"
    assert job.status_text == "boom"


def test_concurrency_limit_and_order() -> None:
    runner = BlockingRunner()
    manager = JobManager(runner, max_concurrent=2)
    jobs = [manager.submit(make_job()) for _ in range(4)]
    wait_until(lambda: len(runner.started) == 2)
    time.sleep(0.05)
    assert runner.started == [jobs[0].id, jobs[1].id]
    assert jobs[2].state == QUEUED
    runner.release.set()
    wait_until(lambda: all(job.state == DONE for job in jobs))


def test_raising_the_limit_starts_waiting_jobs() -> None:
    runner = BlockingRunner()
    manager = JobManager(runner, max_concurrent=1)
    jobs = [manager.submit(make_job()) for _ in range(2)]
    wait_until(lambda: jobs[0].state == RUNNING)
    assert jobs[1].state == QUEUED
    manager.set_max_concurrent(2)
    wait_until(lambda: jobs[1].state == RUNNING)
    runner.release.set()
    wait_until(lambda: jobs[1].state == DONE)


def test_cancel_running_and_waiting_jobs() -> None:
    runner = BlockingRunner()
    finished: list[str] = []
    manager = JobManager(runner, max_concurrent=1, on_finished=lambda job: finished.append(job.state))
    running, waiting = manager.submit(make_job()), manager.submit(make_job())
    wait_until(lambda: running.state == RUNNING)

    manager.cancel(waiting.id)
    assert waiting.state == CANCELED
    manager.cancel(running.id)
    wait_until(lambda: running.state == CANCELED)
    assert finished.count(CANCELED) == 2


def test_cancel_forgets_login_data() -> None:
    runner = BlockingRunner()
    manager = JobManager(runner, max_concurrent=1)
    running = manager.submit(make_job(cookie_bytes=b"secret"))
    waiting = manager.submit(make_job(cookie_bytes=b"secret"))
    wait_until(lambda: running.state == RUNNING)
    manager.cancel(waiting.id)
    manager.cancel(running.id)
    wait_until(lambda: running.state == CANCELED)
    assert running.cookie_bytes is None and waiting.cookie_bytes is None


def test_every_stopped_job_forgets_login_data() -> None:
    # A login uploaded in one browser session must not stay on a job that someone else may resume or retry later.
    runner = BlockingRunner()
    manager = JobManager(runner, max_concurrent=1)
    running = manager.submit(make_job(cookie_bytes=b"secret"))
    waiting = manager.submit(make_job(cookie_bytes=b"secret"))
    wait_until(lambda: running.state == RUNNING)
    manager.pause(waiting.id)
    manager.pause(running.id)
    wait_until(lambda: running.state == PAUSED)
    assert running.cookie_bytes is None and waiting.cookie_bytes is None

    failed = JobManager(lambda job, context: FAILED).submit(make_job(cookie_bytes=b"secret"))
    wait_until(lambda: failed.state == FAILED)
    assert failed.cookie_bytes is None


def test_forget_login_clears_an_owners_jobs_only() -> None:
    runner = BlockingRunner()
    manager = JobManager(runner, max_concurrent=1)
    running = manager.submit(make_job("me", cookie_bytes=b"mine"))
    waiting = manager.submit(make_job("me", cookie_bytes=b"mine"))
    theirs = manager.submit(make_job("you", cookie_bytes=b"theirs"))
    manager.forget_login("me")
    assert running.cookie_bytes is None and waiting.cookie_bytes is None
    assert theirs.cookie_bytes == b"theirs"
    manager.cancel(theirs.id)
    runner.release.set()


def test_a_pause_never_turns_a_cancel_into_a_pause() -> None:
    runner = BlockingRunner()
    manager = JobManager(runner)
    job = manager.submit(make_job())
    wait_until(lambda: job.state == RUNNING)
    with manager._lock:
        # Holding the lock keeps the runner from noticing the cancel before the pause arrives, as a busy download would.
        manager.cancel(job.id)
        manager.pause(job.id)
    wait_until(lambda: job.state != RUNNING)
    assert job.state == CANCELED


def test_pausing_and_resuming_does_not_restart_a_public_jobs_time_limit() -> None:
    runner = BlockingRunner()
    manager = JobManager(runner, max_restricted_seconds=60)
    job = manager.submit(make_job("visitor", restricted=True))
    wait_until(lambda: job.state == RUNNING)
    job.started_at = time.time() - 50
    manager.pause(job.id)
    wait_until(lambda: job.state == PAUSED)
    manager.resume(job.id)
    wait_until(lambda: job.state == RUNNING)
    job.started_at = time.time() - 20
    manager._housekeep()
    wait_until(lambda: job.state == CANCELED)


def test_pause_leaves_a_live_recording_running() -> None:
    # A recording cannot continue where it stopped, so stopping it would end it for good.
    runner = BlockingRunner()
    manager = JobManager(runner)
    job = manager.submit(make_job())
    wait_until(lambda: job.state == RUNNING)
    job.status_text = f"{RECORDING_STATUS_PREFIX} 0:10 · 1 MB"
    manager.pause(job.id)
    time.sleep(0.05)
    assert job.state == RUNNING and job.stop_mode == ""
    manager.cancel(job.id)
    wait_until(lambda: job.state == CANCELED)


def test_pause_and_resume() -> None:
    runner = BlockingRunner()
    manager = JobManager(runner, max_concurrent=1)
    job = manager.submit(make_job())
    wait_until(lambda: job.state == RUNNING)
    manager.pause(job.id)
    wait_until(lambda: job.state == PAUSED)
    assert job.finished_at is None

    runner.release.set()
    manager.resume(job.id)
    wait_until(lambda: job.state == DONE)
    assert runner.started.count(job.id) == 2


def test_pausing_a_waiting_job_lets_the_next_one_run() -> None:
    runner = BlockingRunner()
    manager = JobManager(runner, max_concurrent=1)
    first, second, third = (manager.submit(make_job()) for _ in range(3))
    wait_until(lambda: first.state == RUNNING)
    manager.pause(second.id)
    assert second.state == PAUSED
    runner.release.set()
    wait_until(lambda: third.state == DONE)
    assert second.state == PAUSED


def test_a_job_that_finishes_as_someone_presses_stop_stays_finished() -> None:
    entered, proceed = threading.Event(), threading.Event()

    def runner(job: Job, context: JobContext) -> str:
        entered.set()
        proceed.wait(5)
        return DONE

    manager = JobManager(runner)
    job = manager.submit(make_job())
    entered.wait(5)
    manager.pause(job.id)
    proceed.set()
    wait_until(lambda: job.state == DONE)


def test_restart_runs_a_finished_job_again() -> None:
    calls: list[int] = []

    def runner(job: Job, context: JobContext) -> str:
        calls.append(1)
        return FAILED if len(calls) == 1 else DONE

    manager = JobManager(runner)
    job = manager.submit(make_job())
    wait_until(lambda: job.state == FAILED)
    manager.restart(job.id, payload="new")
    wait_until(lambda: job.state == DONE)
    assert job.payload == "new"
    assert len(calls) == 2


def test_move_reorders_only_waiting_jobs() -> None:
    runner = BlockingRunner()
    manager = JobManager(runner, max_concurrent=1)
    running = manager.submit(make_job())
    wait_until(lambda: running.state == RUNNING)
    second, third, fourth = (manager.submit(make_job()) for _ in range(3))

    manager.move(fourth.id, -1)
    assert [job.id for job in manager.list_jobs()] == [running.id, second.id, fourth.id, third.id]
    manager.move(second.id, -1)  # Already first among the waiting jobs.
    assert [job.id for job in manager.list_jobs()][1] == second.id
    manager.move(running.id, 1)  # Running jobs stay put.
    assert manager.list_jobs()[0].id == running.id

    # Each swap must keep every job's data attached to its own ID.
    assert all(manager.get(job.id) is job for job in [running, second, third, fourth])

    runner.release.set()
    wait_until(lambda: third.state == DONE)
    assert runner.started == [running.id, second.id, fourth.id, third.id]


def test_scheduled_job_waits_for_its_time() -> None:
    manager = JobManager(lambda job, context: DONE)
    job = manager.submit(make_job(not_before=time.time() + 1.2))
    time.sleep(0.2)
    assert job.state == QUEUED
    wait_until(lambda: job.state == DONE, timeout=4)


def test_account_jobs_run_one_at_a_time() -> None:
    runner = BlockingRunner()
    manager = JobManager(runner, max_concurrent=3)
    first, second = manager.submit(make_job(exclusive=True)), manager.submit(make_job(exclusive=True))
    third = manager.submit(make_job())
    wait_until(lambda: third.state == RUNNING)
    assert first.state == RUNNING
    assert second.state == QUEUED
    runner.release.set()
    wait_until(lambda: second.state == DONE)


def test_restricted_visitor_cannot_take_every_slot() -> None:
    runner = BlockingRunner()
    manager = JobManager(runner, max_concurrent=3, per_owner_limit=1)
    a1 = manager.submit(make_job("a", restricted=True))
    a2 = manager.submit(make_job("a", restricted=True))
    b1 = manager.submit(make_job("b", restricted=True))
    wait_until(lambda: b1.state == RUNNING)
    assert a1.state == RUNNING
    assert a2.state == QUEUED
    runner.release.set()
    wait_until(lambda: a2.state == DONE)


def test_new_sessions_from_one_address_share_its_running_slot() -> None:
    runner = BlockingRunner()
    manager = JobManager(runner, max_concurrent=3, per_owner_limit=1)
    # Two sessions of one person are two owners, but the same address.
    first = manager.submit(make_job("session-1", restricted=True, client="203.0.113.7"))
    second = manager.submit(make_job("session-2", restricted=True, client="203.0.113.7"))
    other = manager.submit(make_job("someone-else", restricted=True, client="198.51.100.4"))
    wait_until(lambda: other.state == RUNNING)
    assert first.state == RUNNING
    assert second.state == QUEUED
    runner.release.set()
    wait_until(lambda: second.state == DONE)


def test_jobs_are_listed_per_owner() -> None:
    manager = JobManager(lambda job, context: DONE)
    mine, theirs = manager.submit(make_job("me")), manager.submit(make_job("you"))
    assert manager.list_jobs("me") == [mine]
    assert manager.list_jobs("you") == [theirs]
    assert len(manager.list_jobs()) == 2


def test_remove_and_clear_finished_keep_active_jobs() -> None:
    runner = BlockingRunner()
    forgotten: list[str] = []
    manager = JobManager(runner, max_concurrent=1, on_forget=lambda job: forgotten.append(job.id))
    done = manager.submit(make_job())
    runner.release.set()
    wait_until(lambda: done.state == DONE)
    runner.release.clear()
    running = manager.submit(make_job())
    wait_until(lambda: running.state == RUNNING)

    manager.remove(running.id)
    assert manager.get(running.id) is running
    assert manager.clear_finished("me") == 1
    assert manager.get(done.id) is None
    assert forgotten == [done.id]
    manager.cancel(running.id)


def test_abandoned_jobs_are_cancelled_and_old_ones_forgotten() -> None:
    runner = BlockingRunner()
    manager = JobManager(runner, abandon_after=10, retention_seconds=100)
    running = manager.submit(make_job("gone", restricted=True))
    wait_until(lambda: running.state == RUNNING)
    manager._last_seen["gone"] = time.time() - 60
    manager._housekeep()
    wait_until(lambda: running.state == CANCELED)

    running.finished_at = time.time() - 1000
    manager._housekeep()
    assert manager.get(running.id) is None


def test_this_machines_jobs_run_on_with_the_page_closed() -> None:
    runner = BlockingRunner()
    manager = JobManager(runner, abandon_after=10, retention_seconds=100)
    running = manager.submit(make_job("local"))
    wait_until(lambda: running.state == RUNNING)
    manager._last_seen["local"] = time.time() - 6000
    manager._housekeep()
    assert running.state == RUNNING

    runner.release.set()
    wait_until(lambda: running.state == DONE)
    running.finished_at = time.time() - 6000
    manager._housekeep()
    assert manager.get(running.id) is running


def test_paused_restored_job_does_not_start_by_itself() -> None:
    manager = JobManager(lambda job, context: DONE)
    job = manager.add_paused(make_job())
    time.sleep(0.05)
    assert job.state == PAUSED
    manager.resume(job.id)
    wait_until(lambda: job.state == DONE)


def test_one_visitor_cannot_push_out_anothers_results(monkeypatch) -> None:
    import videocap.jobs as jobs_module

    monkeypatch.setattr(jobs_module, "MAX_FINISHED_JOBS_KEPT", 2)
    manager = JobManager(lambda job, context: DONE)
    theirs = manager.submit(make_job("them"))
    mine = [manager.submit(make_job("me")) for _ in range(4)]
    wait_until(lambda: all(job.state == DONE for job in [theirs, *mine]))
    for index, job in enumerate([theirs, *mine]):
        job.finished_at = 1000.0 + index
    manager._housekeep()
    assert manager.get(theirs.id) is theirs
    assert [job.id for job in manager.list_jobs("me")] == [job.id for job in mine[2:]]


def test_public_jobs_have_a_time_limit() -> None:
    runner = BlockingRunner()
    manager = JobManager(runner, max_restricted_seconds=60)
    public = manager.submit(make_job("visitor", restricted=True))
    local = manager.submit(make_job("local"))
    wait_until(lambda: public.state == RUNNING and local.state == RUNNING)
    public.started_at = local.started_at = time.time() - 120
    manager._housekeep()
    wait_until(lambda: public.state == CANCELED)
    assert local.state == RUNNING
    runner.release.set()


def test_housekeeping_forgets_visitors_who_left_and_have_no_jobs() -> None:
    manager = JobManager(lambda job, context: DONE, abandon_after=10)
    job = manager.submit(make_job("stays"))
    wait_until(lambda: job.state == DONE)
    manager.touch("left")
    manager.touch("still-here")
    manager._last_seen["left"] = manager._last_seen["stays"] = time.time() - 60
    manager._housekeep()
    assert set(manager._last_seen) == {"stays", "still-here"}


def test_housekeeping_stays_fast_with_many_visitors(monkeypatch) -> None:
    import videocap.jobs as jobs_module

    monkeypatch.setattr(jobs_module, "MAX_FINISHED_JOBS_KEPT", 1)
    manager = JobManager(lambda job, context: DONE, retention_seconds=10_000)
    now = time.time()
    for index in range(8000):
        job = make_job(f"visitor-{index}", restricted=True)
        job.state, job.finished_at = CANCELED, now - index * 0.001
        manager._jobs[job.id] = job
    started = time.perf_counter()
    manager._housekeep()
    assert time.perf_counter() - started < 1.0
    assert len(manager.list_jobs()) == 8000


def test_held_jobs_wait_until_released() -> None:
    runner = BlockingRunner()
    manager = JobManager(runner, max_concurrent=3)
    running = manager.submit(make_job())
    wait_until(lambda: running.state == RUNNING)
    assert manager.hold("test") == [running]
    held = manager.submit(make_job())
    other = manager.submit(Job(id=new_job_id(), owner="me", kind="other", label="other", payload=None))
    wait_until(lambda: other.state == RUNNING)
    assert held.state == QUEUED
    manager.release("test")
    wait_until(lambda: held.state == RUNNING)
    runner.release.set()


def test_a_hold_reason_keeps_jobs_waiting_and_is_asked_again_by_itself() -> None:
    runner = BlockingRunner()
    closed = threading.Event()
    closed.set()
    asked: list[float] = []

    def hold_reason(job: Job, now: float) -> str:
        asked.append(now)
        return "等待下載時段 02:00–07:00" if closed.is_set() and job.owner == "me" else ""

    manager = JobManager(runner, max_concurrent=3, hold_reason=hold_reason)
    held = manager.submit(make_job("me"))
    free = manager.submit(make_job("visitor"))
    wait_until(lambda: free.state == RUNNING)
    time.sleep(0.05)
    assert held.state == QUEUED
    # Nothing tells the queue the window opened: its own tick asks again.
    closed.clear()
    wait_until(lambda: held.state == RUNNING)
    assert asked and all(isinstance(now, float) for now in asked)
    runner.release.set()


def test_wake_starts_what_a_hold_no_longer_keeps() -> None:
    runner = BlockingRunner()
    reasons = ["hold"]
    manager = JobManager(runner, hold_reason=lambda job, now: reasons[0])
    job = manager.submit(make_job())
    time.sleep(0.05)
    assert job.state == QUEUED
    reasons[0] = ""
    manager.wake()
    assert job.state == RUNNING
    runner.release.set()


def test_a_restart_can_wait_and_start_now_skips_the_wait() -> None:
    runner = BlockingRunner()
    runner.release.set()
    manager = JobManager(lambda job, context: FAILED)
    job = manager.submit(make_job())
    wait_until(lambda: job.state == FAILED)
    manager.runner = runner
    manager.restart(job.id, not_before=time.time() + 3600)
    time.sleep(0.05)
    assert job.state == QUEUED and job.not_before is not None
    manager.start_now(job.id)
    wait_until(lambda: job.state == DONE)
    assert job.not_before is None
    # Only a waiting job: a finished one is retried with restart.
    manager.start_now(job.id)
    assert job.state == DONE


def test_shutdown_stops_the_background_thread() -> None:
    manager = JobManager(lambda job, context: DONE)
    manager.submit(make_job())
    assert manager._ticker is not None
    manager.shutdown()
    manager._ticker.join(timeout=3)
    assert not manager._ticker.is_alive()

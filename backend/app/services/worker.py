"""One queue, one worker thread: the local model server has a single slot.

The worker owns the live Task objects, persists every change to the tasks
table, updates the related job row, and publishes events for the SSE stream.

Built for long unattended batches: the queue is ordered by priority (pasted
JDs first, then screened jobs by fit rank); before each task the worker makes
sure the model is up, starting it with the user's launcher and pausing the
queue while it is down; a full generation that fails is tried once more; and
`recover` puts back the jobs a restart left queued or half-done.
"""
import itertools
import queue
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from ..db.models import Job, RagRun, Screening, Task as TaskRow
from . import screen
from .pipeline import Pipeline, Task, launch_with_shell
from .runs import run_summary

PRIORITY_MANUAL = 0          # pasted JDs, decisions and interview prep from the page
PRIORITY_SCREENED = 100      # + fit rank for jobs the autopilot queued
MAX_ATTEMPTS = 2
MODEL_POLL_SECONDS = 15

TASK_TO_JOB_STATUS = {"queued": "queued", "running": "processing", "done": "done", "failed": "failed", "blocked": "blocked",
                      "cancelled": "cancelled"}


class EventBus:
    """Fan-out of JSON-able events to SSE subscribers."""

    def __init__(self):
        self._subscribers = set()
        self._lock = threading.Lock()

    def subscribe(self):
        q = queue.Queue(maxsize=500)
        with self._lock:
            self._subscribers.add(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            self._subscribers.discard(q)

    def publish(self, event_type, data):
        with self._lock:
            targets = list(self._subscribers)
        for q in targets:
            try:
                q.put_nowait({"type": event_type, "data": data})
            except queue.Full:
                pass  # a stalled listener loses events rather than blocking the worker


class Worker:
    def __init__(self, settings, session_factory, runner=None, events=None, launch=launch_with_shell):
        self.settings = settings
        self.session_factory = session_factory
        self.events = events or EventBus()
        self.launch = launch
        kwargs = {"runner": runner} if runner is not None else {}
        self.pipeline = Pipeline(settings, on_change=self._persist, **kwargs)
        self.tasks = {}                    # id -> live Task (recent, in memory)
        self.current = None
        self.paused = ""                   # why the queue is waiting, e.g. the model is offline
        self._queue = queue.PriorityQueue()
        self._order = itertools.count()    # first in, first out among equal priorities
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    # -- lifecycle ------------------------------------------------------------
    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="rag-worker", daemon=True)
            self._thread.start()

    def stop(self):
        """Stop taking work and finish the current task before releasing the DB.

        Signalling alone leaves a race with the task's last database write.
        Waiting also prevents a stopped worker from reopening a disposed pool.
        Shutdown can therefore wait for an active generation to complete.
        """
        self._stop.set()
        self._queue.put((-1, next(self._order), None, None))
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join()

    def _loop(self):
        while not self._stop.is_set():
            _priority, _order, task, fn = self._queue.get()
            if task is None:
                break
            if task.status != "queued":     # cancelled while waiting
                continue
            if not self._wait_for_model():  # stopping: the job stays queued and `recover` picks it up
                break
            if task.status != "queued":     # cancelled while the queue was paused
                continue
            with self._lock:
                self.current = task
            try:
                fn(task)
            finally:
                with self._lock:
                    if self.current is task:
                        self.current = None
                self._retry_if_failed(task)
                self.events.publish("queue", self.queue_status())

    def _wait_for_model(self):
        """True once the model answers; False when the worker is stopping.
        Starts the user's launcher once per start-up timeout while it waits."""
        launched_at = None
        while not self._stop.is_set():
            online, info = self.model_online()
            if online:
                if self.paused:
                    self.paused = ""
                    self.events.publish("queue", self.queue_status())
                return True
            launcher = self.settings.model_launcher
            if launcher and (launched_at is None
                             or time.time() - launched_at > self.settings.model_start_timeout_seconds):
                self.launch(launcher)
                launched_at = time.time()
            reason = "模型离线，队列暂停" + ("，已尝试启动模型" if launcher else "；请启动本地模型")
            if reason != self.paused:
                self.paused = reason
                self.events.publish("queue", self.queue_status())
            self._stop.wait(MODEL_POLL_SECONDS)
        return False

    def _retry_if_failed(self, task):
        """One more attempt for a full generation that failed on its own (not
        cancelled, not blocked by location): most failures are a model answer
        that did not validate, which a fresh run usually gets past."""
        if not self.settings.retry_failed_generation or task.kind != "full" or task.status != "failed" \
                or task.cancel_requested or task.attempt >= MAX_ATTEMPTS or task.jd_path is None:
            return
        self.submit_full(jd_name=task.jd_name, with_interview=task.with_interview, job_id=task.job_id,
                         jd_path=task.jd_path, priority=task.priority, attempt=task.attempt + 1)

    def recover(self):
        """After a restart: close the task rows that were queued or running
        (the process that owned them is gone) and queue their jobs again.
        Returns the number of jobs queued again."""
        now = datetime.now(timezone.utc)
        again = []
        with self.session_factory() as session:
            for row in session.scalars(select(TaskRow).where(TaskRow.kind != "tick",
                                                             TaskRow.status.in_(("queued", "running")))).all():
                row.status = "failed"
                row.error = "后端重启时任务未完成" + ("，岗位已重新排队" if row.kind == "full" else "，可重新发起")
                row.finished_at = now
            for job in session.scalars(select(Job).where(Job.status.in_(("queued", "processing")))
                                       .order_by(Job.first_seen)).all():
                if job.jd_path and Path(job.jd_path).is_file():
                    job.status = "queued"
                    again.append((job.id, job.company or job.title, Path(job.jd_path), self._priority_of(session, job)))
                else:
                    job.status = "failed"
                    job.note = (job.note + "\n" if job.note else "") + "后端重启时任务未完成，且没有 JD 文件可以重跑"
            session.commit()
        for job_id, name, path, priority in again:
            self.submit_full(jd_name=name, job_id=job_id, jd_path=path, priority=priority)
        return len(again)

    def generate_ready(self):
        """Queue every job the screening left "ready", best fit first. -> the number queued."""
        picked = []
        with self.session_factory() as session:
            for job in session.scalars(select(Job).where(Job.status == "ready").order_by(Job.first_seen)).all():
                if job.jd_path and Path(job.jd_path).is_file():
                    job.status = "queued"
                    picked.append((self._priority_of(session, job), job.id, job.company or job.title, Path(job.jd_path)))
            session.commit()
        for priority, job_id, name, path in sorted(picked, key=lambda p: (p[0], p[1])):
            self.submit_full(jd_name=name, job_id=job_id, jd_path=path, priority=priority)
        return len(picked)

    def _priority_of(self, session, job):
        if job.source == "manual":
            return PRIORITY_MANUAL
        latest = session.scalar(select(Screening).where(Screening.job_id == job.id)
                                .order_by(Screening.created_at.desc(), Screening.id.desc()))
        fit = (latest.rule_result or {}).get("fit") if latest is not None else None
        return PRIORITY_SCREENED + screen.fit_rank(fit, self.settings)

    # -- submission -------------------------------------------------------------
    def submit_full(self, jd_text=None, jd_name="", with_interview=False, job_id=None, jd_path=None,
                    priority=PRIORITY_MANUAL, attempt=1):
        task = Task("full", jd_name=jd_name, with_interview=with_interview, job_id=job_id, jd_path=jd_path)
        task.attempt = attempt
        self._enqueue(task, lambda t: self.pipeline.run_full(t, jd_text), priority)
        return task

    def submit_interview(self, run_dir, job_id=None):
        task = Task("interview", jd_name=run_dir.name, with_interview=True, run_dir=run_dir, job_id=job_id)
        self._enqueue(task, self.pipeline.run_interview, PRIORITY_MANUAL)
        return task

    def _enqueue(self, task, fn, priority):
        task.priority = priority
        self.tasks[task.id] = task
        self._persist(task)
        self._queue.put((priority, next(self._order), task, fn))
        self.events.publish("queue", self.queue_status())

    def cancel(self, task_id):
        task = self.tasks.get(task_id)
        if task is None:
            return None
        return self.pipeline.cancel(task)

    def queue_status(self):
        with self._lock:
            current = self.current
        queued = [t for t in self.tasks.values() if t.status == "queued"]
        waiting = [t.id for t in sorted(queued, key=lambda t: t.priority)]
        return {"current": current.to_dict() if current else None, "waiting": waiting, "paused": self.paused}

    def model_online(self):
        return self.pipeline.model_online()

    # -- persistence ------------------------------------------------------------
    def _persist(self, task):
        """Mirror the live task into the database and tell the page."""
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            row = session.get(TaskRow, task.id)
            if row is None:
                row = TaskRow(id=task.id, kind=task.kind, job_id=task.job_id)
                session.add(row)
            row.status = task.status
            row.stages = [dict(s) for s in task.stages]
            row.run_id = task.run_dir.name if task.run_dir else None
            row.error = task.error or ""
            if task.status == "running" and row.started_at is None:
                row.started_at = now
            if task.status in ("done", "failed", "blocked", "cancelled"):
                row.finished_at = now
            if task.job_id is not None:
                job = session.get(Job, task.job_id)
                if job is not None:
                    if task.kind == "full" and task.status in TASK_TO_JOB_STATUS:
                        job.status = TASK_TO_JOB_STATUS[task.status]
                    if task.jd_path and not job.jd_path:
                        job.jd_path = str(task.jd_path)
            if task.run_dir and task.run_dir.is_dir() and task.status in ("done", "failed", "blocked"):
                summary = run_summary(task.run_dir)
                run = session.scalar(select(RagRun).where(RagRun.run_id == task.run_dir.name))
                if run is None:
                    run = RagRun(run_id=task.run_dir.name, job_id=task.job_id)
                    session.add(run)
                run.run_status = summary["run_status"] or task.status
                run.counts = summary["counts"]
                run.files = summary["downloads"]
            session.commit()
        payload = task.to_dict()
        if task.run_dir and task.run_dir.is_dir():
            payload["run"] = run_summary(task.run_dir)
        self.events.publish("task", payload)

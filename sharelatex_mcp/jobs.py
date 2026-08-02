"""Background job store for long-running MCP operations.

Large ``write``/``edit`` calls run on daemon worker threads so the MCP tool
returns immediately with a ``job_id``; clients poll ``get_job_status`` or block
on ``wait_job``.  Daemon workers let the stdio server exit even if a job is
still running.
"""

from __future__ import annotations

import copy
import logging
import queue
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# A progress callback receives (done, total, message); identical in shape to
# MCP's report_progress notification so it can be wired through directly.
ProgressCallback = Callable[[int, int, str | None], None]

DEFAULT_TTL_SECONDS = 600.0
DEFAULT_QUEUE_LIMIT = 100

QUEUED = "queued"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
NOT_FOUND = "not-found"


@dataclass
class Job:
    job_id: str
    operation: str
    project_id: str
    status: str = QUEUED
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    result: dict[str, Any] | None = None
    error: str | None = None


class JobStore:
    """Thread-safe registry of background jobs with a daemon worker pool."""

    def __init__(
        self,
        max_workers: int = 4,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        queue_limit: int = DEFAULT_QUEUE_LIMIT,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._queue: queue.Queue[tuple[Job, Callable[[], dict[str, Any]]]] = queue.Queue(
            maxsize=max(1, queue_limit)
        )
        self._workers: list[threading.Thread] = []
        for _ in range(max(1, max_workers)):
            worker = threading.Thread(
                target=self._worker_loop,
                name="sharelatex-job",
                daemon=True,
            )
            worker.start()
            self._workers.append(worker)

    def submit(self, operation: str, project_id: str, fn: Callable[[], dict[str, Any]]) -> str:
        """Queue *fn* for execution and return a new ``job_id`` immediately.

        Raises ``RuntimeError`` when the queue is full (too many queued jobs).
        """
        job_id = secrets.token_urlsafe(12)
        job = Job(job_id=job_id, operation=operation, project_id=project_id)
        with self._lock:
            self._evict_expired_locked()
            self._jobs[job_id] = job
        try:
            self._queue.put_nowait((job, fn))
        except queue.Full as exc:
            with self._lock:
                self._jobs.pop(job_id, None)
            raise RuntimeError("background job queue is full; retry later") from exc
        return job_id

    def status(self, job_id: str) -> dict[str, Any] | None:
        """Return a snapshot dict for *job_id*, or ``None`` if unknown/expired."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            if job.finished_at is not None and time.time() - job.finished_at > self._ttl_seconds:
                self._jobs.pop(job_id, None)
                return None
            snapshot = {
                "job_id": job.job_id,
                "operation": job.operation,
                "project_id": job.project_id,
                "status": job.status,
                "created_at": job.created_at,
                "finished_at": job.finished_at,
                "result": job.result,
                "error": job.error,
            }
        # Deep-copy outside the global lock: results are append-only once set, so
        # no other thread mutates them, and a large result should not stall
        # every other submit/status under the lock.
        snapshot["result"] = copy.deepcopy(snapshot["result"])
        return snapshot

    def wait(self, job_id: str, timeout: float = 30.0) -> dict[str, Any]:
        """Block until *job_id* finishes or *timeout* elapses.

        Returns a snapshot; a ``timed_out`` key is set when the deadline was
        reached before the job finished.
        """
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            snapshot = self.status(job_id)
            if snapshot is None:
                return {"job_id": job_id, "status": NOT_FOUND}
            if snapshot["status"] in {SUCCEEDED, FAILED}:
                return snapshot
            if time.monotonic() >= deadline:
                snapshot = dict(snapshot)
                snapshot["timed_out"] = True
                return snapshot
            time.sleep(0.05)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _worker_loop(self) -> None:
        while True:
            job, fn = self._queue.get()
            try:
                self._run(job, fn)
            except BaseException:  # pragma: no cover - defensive
                # A BaseException must never permanently kill a worker thread.
                logger.exception("Worker crashed for job %s", job.job_id)

    def _run(self, job: Job, fn: Callable[[], dict[str, Any]]) -> None:
        with self._lock:
            job.status = RUNNING
        try:
            result = fn()
        except BaseException as exc:
            # Catch BaseException so the job is marked FAILED (not left RUNNING
            # forever) even if a worker is interrupted by a BaseException.
            logger.exception("Job %s (%s) failed", job.job_id, job.operation)
            with self._lock:
                job.status = FAILED
                job.error = str(exc)
                job.finished_at = time.time()
            return
        with self._lock:
            job.status = SUCCEEDED
            job.result = result
            job.finished_at = time.time()
        logger.info(
            "Job %s (%s) finished in %.2fs",
            job.job_id, job.operation, job.finished_at - job.created_at,
        )

    def _evict_expired_locked(self) -> None:
        # Only evict *finished* jobs that are older than the TTL; queued/running
        # jobs are never dropped so their results are never lost.
        now = time.time()
        expired = [
            job_id
            for job_id, job in self._jobs.items()
            if job.finished_at is not None and now - job.finished_at > self._ttl_seconds
        ]
        for job_id in expired:
            del self._jobs[job_id]
        if expired:
            logger.debug("Evicted %d expired job(s)", len(expired))

from __future__ import annotations

import time

from sharelatex_mcp.jobs import JobStore


def test_job_runs_and_returns_result() -> None:
    store = JobStore(max_workers=1)
    job_id = store.submit("write", "0" * 24, lambda: {"changed": True})

    result = store.wait(job_id, timeout=5.0)

    assert result["status"] == "succeeded"
    assert result["job_id"] == job_id
    assert result["operation"] == "write"
    assert result["result"] == {"changed": True}
    assert "timed_out" not in result


def test_job_failure_captures_error() -> None:
    store = JobStore(max_workers=1)

    def boom() -> dict:
        raise RuntimeError("boom")

    job_id = store.submit("write", "0" * 24, boom)

    result = store.wait(job_id, timeout=5.0)

    assert result["status"] == "failed"
    assert "boom" in result["error"]
    assert result["result"] is None


def test_wait_returns_timed_out_snapshot_while_queued_or_running() -> None:
    import threading

    release = threading.Event()
    store = JobStore(max_workers=1)

    def slow() -> dict:
        release.wait(5.0)
        return {"changed": True}

    job_id = store.submit("write", "0" * 24, slow)

    result = store.wait(job_id, timeout=0.05)

    assert result["status"] in {"queued", "running"}
    assert result["timed_out"] is True
    release.set()
    assert store.wait(job_id, timeout=5.0)["status"] == "succeeded"


def test_status_tracks_queued_to_running() -> None:
    import threading

    started = threading.Event()
    release = threading.Event()
    store = JobStore(max_workers=1)

    def slow() -> dict:
        started.set()
        release.wait(5.0)
        return {"changed": True}

    job_id = store.submit("write", "0" * 24, slow)

    assert started.wait(2.0)
    assert store.status(job_id)["status"] in {"queued", "running"}
    release.set()
    assert store.wait(job_id, timeout=5.0)["status"] == "succeeded"


def test_submit_raises_when_queue_is_full() -> None:
    import threading

    started = threading.Event()
    release = threading.Event()
    store = JobStore(max_workers=1, queue_limit=1)

    def slow() -> dict:
        started.set()
        release.wait(5.0)
        return {"changed": True}

    first = store.submit("write", "0" * 24, slow)
    assert started.wait(2.0)  # the single worker is now busy
    second = store.submit("write", "0" * 24, slow)  # occupies the single queue slot

    try:
        store.submit("write", "0" * 24, slow)
    except RuntimeError as exc:
        assert "queue is full" in str(exc)
    else:
        raise AssertionError("expected RuntimeError on full queue")

    release.set()
    store.wait(first, timeout=5.0)
    store.wait(second, timeout=5.0)


def test_unknown_job_reports_not_found() -> None:
    store = JobStore(max_workers=1)

    assert store.wait("does-not-exist", timeout=0.0)["status"] == "not-found"
    assert store.status("does-not-exist") is None


def test_expired_jobs_are_evicted() -> None:
    store = JobStore(max_workers=1, ttl_seconds=0.05)
    job_id = store.submit("write", "0" * 24, lambda: {"changed": True})
    store.wait(job_id, timeout=5.0)

    time.sleep(0.1)
    # A later submit triggers eviction of the now-expired job
    store.submit("write", "0" * 24, lambda: {"changed": True})

    assert store.status(job_id) is None


def test_running_job_is_never_evicted() -> None:
    import threading

    started = threading.Event()
    release = threading.Event()
    store = JobStore(max_workers=1, ttl_seconds=0.5)

    def slow() -> dict:
        started.set()
        release.wait(5.0)
        return {"changed": True}

    job_id = store.submit("write", "0" * 24, slow)
    assert started.wait(2.0)

    time.sleep(0.6)  # far past the TTL while the job is still running
    store.submit("write", "0" * 24, lambda: {"changed": True})  # triggers eviction

    assert store.status(job_id) is not None  # still running, not dropped

    release.set()
    assert store.wait(job_id, timeout=5.0)["status"] == "succeeded"

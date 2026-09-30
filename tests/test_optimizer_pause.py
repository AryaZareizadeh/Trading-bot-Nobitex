import threading
import time

from optimizer import OptimizerJob


def _job():
    job = OptimizerJob("btcusdt", "AdvRSI", "60", 420, 100, 3)
    job.status = "running"
    return job


def _wait_for_status(job, expected):
    deadline = time.monotonic() + 2
    while job.status != expected and time.monotonic() < deadline:
        time.sleep(0.01)
    assert job.status == expected


def test_pause_waits_at_generation_boundary_then_resumes():
    job = _job()
    assert job.pause()
    assert job.status == "pausing"
    thread = threading.Thread(target=job._wait_if_paused)
    thread.start()
    try:
        _wait_for_status(job, "paused")
        assert thread.is_alive()
        assert job.resume()
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert job.status == "running"
        assert job.paused_seconds >= 0
    finally:
        job.cancel()
        thread.join(timeout=2)


def test_cancel_releases_paused_worker():
    job = _job()
    assert job.pause()
    thread = threading.Thread(target=job._wait_if_paused)
    thread.start()
    try:
        _wait_for_status(job, "paused")
        job.cancel()
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert job._cancel
    finally:
        job.cancel()
        thread.join(timeout=2)


def test_pause_and_resume_api_for_both_optimizer_modes(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from bot_manager import BotManager
    from optimizer import OptimizerManager
    from server import create_app
    from storage import Store

    ordinary = _job()
    tester = _job()
    tester.train_pct = 70
    jobs = {ordinary.id: ordinary, tester.id: tester}
    monkeypatch.setattr(OptimizerManager, "get_job", lambda self, job_id: jobs.get(job_id))
    manager = BotManager(Store(tmp_path / "pause.sqlite3"), tmp_path)
    app = create_app(manager=manager, offline=True, security=False)
    with TestClient(app) as client:
        for job, path in ((ordinary, "run"), (tester, "tester/run")):
            base = f"/api/optimizer/{path}/{job.id}"
            assert client.post(base + "/pause").status_code == 200
            assert job.status == "pausing"
            assert client.post(base + "/pause").status_code == 409
            assert client.post(base + "/resume").status_code == 200
            assert job.status == "running"
            assert client.post(base + "/resume").status_code == 409

import asyncio
import threading
import pytest


def test_research_queue_uses_dedicated_workers_and_is_bounded():
    from research_scheduler import ResearchLane, ResearchBusy
    async def run():
        lane = ResearchLane("test", workers=1, max_pending=2)
        release = threading.Event()
        entered = threading.Event()
        def blocking():
            entered.set()
            release.wait(3)
            return threading.current_thread().name
        first = asyncio.create_task(lane.run("a", blocking))
        try:
            while not entered.is_set():
                await asyncio.sleep(.01)
            second = asyncio.create_task(lane.run("b", lambda: 2))
            await asyncio.sleep(.03)
            assert lane.status() == {"running": 1, "queued": 1, "capacity": 1, "queue_limit": 2}
            with pytest.raises(ResearchBusy):
                await lane.run("c", lambda: 3)
            with pytest.raises(ResearchBusy):
                await lane.run("a", lambda: 4)
            live_name = await asyncio.to_thread(lambda: threading.current_thread().name)
            assert "research" not in live_name
            release.set()
            assert "research-test" in await first
            assert await second == 2
            assert lane.status()["running"] == lane.status()["queued"] == 0
        finally:
            release.set()
            await asyncio.gather(first, return_exceptions=True)
            lane.close()
    asyncio.run(run())


def test_cancelled_waiter_does_not_run_or_release_another_slot():
    from research_scheduler import ResearchLane, ResearchCancelled
    async def run():
        lane = ResearchLane("cancel-test", workers=1, max_pending=3)
        release = threading.Event()
        cancel = threading.Event()
        calls = []
        first = asyncio.create_task(lane.run("a", lambda: release.wait(3)))
        try:
            while lane.status()["running"] != 1:
                await asyncio.sleep(.01)
            queued = asyncio.create_task(lane.run("b", lambda: calls.append("b"), cancel=cancel))
            await asyncio.sleep(.03)
            cancel.set()
            with pytest.raises(ResearchCancelled):
                await queued
            assert calls == [] and lane.status()["running"] == 1
            release.set()
            await first
        finally:
            release.set()
            await asyncio.gather(first, return_exceptions=True)
            lane.close()
    asyncio.run(run())


def test_repeated_task_cancellation_retains_worker_ownership_until_exit():
    from research_scheduler import ResearchLane
    async def run():
        lane = ResearchLane("repeated-cancel", workers=1, max_pending=2)
        release = threading.Event()
        task = asyncio.create_task(lane.run("a", lambda: release.wait(3)))
        try:
            while lane.status()["running"] != 1:
                await asyncio.sleep(.01)
            task.cancel()
            await asyncio.sleep(.02)
            task.cancel()
            await asyncio.sleep(.02)
            assert lane.status()["running"] == 1
            assert not task.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert lane.status()["running"] == 0
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            lane.close()
    asyncio.run(run())

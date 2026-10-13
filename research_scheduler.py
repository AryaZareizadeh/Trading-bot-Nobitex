"""Bounded fair admission before work reaches a dedicated research executor."""
import asyncio
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from functools import partial
import threading


class ResearchBusy(ValueError):
    pass


class ResearchCancelled(RuntimeError):
    pass


class ResearchLane:
    def __init__(self, name, *, workers=1, max_pending=16):
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix=f"research-{name}")
        self._lock = threading.Lock()
        self._waiting = deque()
        self._owners = set()
        self._running = 0
        self.capacity, self.queue_limit = workers, max_pending

    def status(self):
        with self._lock:
            return dict(running=self._running, queued=len(self._waiting),
                        capacity=self.capacity, queue_limit=self.queue_limit)

    async def run(self, owner, function, *args, cancel=None, **kwargs):
        ticket = object()
        with self._lock:
            if owner in self._owners or len(self._owners) >= self.queue_limit:
                raise ResearchBusy("Research capacity is busy; retry after an active run finishes")
            self._owners.add(owner)
            self._waiting.append(ticket)
        acquired = False
        handed_off = False
        try:
            while True:
                if cancel is not None and cancel.is_set():
                    raise ResearchCancelled("Research cancelled while queued")
                with self._lock:
                    if self._waiting[0] is ticket and self._running < self.capacity:
                        self._waiting.popleft()
                        self._running += 1
                        acquired = True
                        break
                # Waiting never consumes a default HTTP executor worker.
                await asyncio.sleep(.02)
            worker = self._pool.submit(partial(function, *args, **kwargs))
            def completed(_):
                with self._lock:
                    self._running -= 1
                    self._owners.remove(owner)
            handed_off = True
            worker.add_done_callback(completed)
            future = asyncio.wrap_future(worker)
            try:
                return await asyncio.shield(future)
            except asyncio.CancelledError:
                if cancel is not None:
                    cancel.set()
                # A cancelled coroutine cannot release capacity while its worker
                # still modifies artifacts or consumes the global training RNG.
                while not future.done():
                    try:
                        await asyncio.shield(future)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if future.done() and not future.cancelled():
                    future.exception()  # Retrieve worker failure during cancellation.
                raise
        finally:
            if not handed_off:
                with self._lock:
                    if acquired:
                        self._running -= 1
                    else:
                        self._waiting.remove(ticket)
                    self._owners.remove(owner)

    def close(self):
        self._pool.shutdown(wait=True, cancel_futures=True)


TRAINING = ResearchLane("training", workers=1, max_pending=16)
SPOT_RESEARCH = ResearchLane("spot", workers=2, max_pending=16)
PREDICTION = ResearchLane("prediction", workers=2, max_pending=16)


def queue_status():
    return {"training": TRAINING.status(), "spot": SPOT_RESEARCH.status(), "prediction": PREDICTION.status()}

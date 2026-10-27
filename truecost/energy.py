"""Measured GPU energy, attributed to model calls by time-share.

A sampler thread reads each GPU's cumulative energy counter (NVML) every `period` seconds. The energy
used on a GPU during each interval is split equally across the calls in flight on that GPU; with no
call in flight it is booked as idle. Summed over a run, attributed + idle equals the meter reading.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict


class EnergyMeter:
    def __init__(self, gpus: list[int] | None = None, period: float = 0.25):
        self.period = period
        self._lock = threading.Lock()
        self._inflight: dict[int, set[str]] = defaultdict(set)
        self._acc: dict[str, float] = {}
        self.idle_j: dict[int, float] = defaultdict(float)
        self.total_j: dict[int, float] = defaultdict(float)
        self.available = False
        self._handles = {}
        try:
            import pynvml as nv  # nvidia-ml-py

            nv.nvmlInit()
            n = nv.nvmlDeviceGetCount()
            self._nv = nv
            for i in (gpus if gpus is not None else range(n)):
                h = nv.nvmlDeviceGetHandleByIndex(i)
                nv.nvmlDeviceGetTotalEnergyConsumption(h)  # GPUs without a cumulative counter are skipped
                self._handles[i] = h
        except Exception:
            pass
        self._last = {}
        for i in list(self._handles):
            try:
                self._last[i] = self._read(i)
            except Exception:
                del self._handles[i]
        self.available = bool(self._handles)
        self._tainted: set[str] = set()  # calls whose energy missed a failed sample: reported as unknown
        self._stop = threading.Event()
        self._thread = None

    def _read(self, gpu: int) -> float:
        """Cumulative energy in joules."""
        return self._nv.nvmlDeviceGetTotalEnergyConsumption(self._handles[gpu]) / 1000.0

    def start(self) -> "EnergyMeter":
        if self.available and self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="truecost-nvml", daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self.period):
            self.tick()

    def tick(self) -> None:
        for gpu in self._handles:
            self._sample(gpu)

    def _sample(self, gpu: int) -> None:
        """Read the counter and book the energy since the last read. Read and booking share one lock,
        so samples from the thread and from begin/end never double-count or drop an interval. A failed read
        (driver hiccup) books nothing for that interval rather than crashing the caller."""
        with self._lock:
            try:
                now = self._read(gpu)
            except Exception:
                self._tainted |= self._inflight.get(gpu, set())
                return
            delta = max(0.0, now - self._last[gpu])
            self._last[gpu] = now
            self.total_j[gpu] += delta
            calls = self._inflight.get(gpu)
            if calls:
                share = delta / len(calls)
                for cid in calls:
                    self._acc[cid] = self._acc.get(cid, 0.0) + share
            else:
                self.idle_j[gpu] += delta

    def begin(self, call_id: str, gpu: int | None) -> None:
        if gpu is None or gpu not in self._handles:
            return
        self._sample(gpu)  # energy before this call starts belongs to others (or idle)
        with self._lock:
            self._inflight[gpu].add(call_id)
            self._acc.setdefault(call_id, 0.0)

    def end(self, call_id: str, gpu: int | None) -> float | None:
        """Stop attributing to a call and return its energy in joules (None if not metered)."""
        if gpu is None or gpu not in self._handles:
            return None
        self._sample(gpu)  # book the partial interval, so short calls are not rounded to zero
        with self._lock:
            self._inflight[gpu].discard(call_id)
            j = self._acc.pop(call_id, 0.0)
            if call_id in self._tainted:
                self._tainted.discard(call_id)
                return None
            return j

    def snapshot(self) -> dict:
        with self._lock:
            return {"total_j": dict(self.total_j), "idle_j": dict(self.idle_j),
                    "inflight": {g: len(c) for g, c in self._inflight.items()}, "ts": time.time()}

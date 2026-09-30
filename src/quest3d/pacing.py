"""Monotonic presentation deadlines without accumulating sleep overshoot.

This only paces a caller. It does not create frames, change their timestamps,
queue old work or turn repeat publications into new AI results.
"""
from __future__ import annotations

import math
from numbers import Real
import time
from typing import Callable


class FramePacer:
    """Create just before the first tick; call wait_next after each tick.

    A late frame runs once immediately. Missed deadlines are discarded rather
    than replayed as a catch-up burst. Small scheduler oversleeps retain the
    original phase instead of moving every following deadline later.
    """

    def __init__(self, fps: float, *, clock_ns: Callable[[], int] = time.perf_counter_ns,
                 sleep: Callable[[float], None] = time.sleep):
        if isinstance(fps, bool) or not isinstance(fps, Real):
            raise ValueError("Frame rate must be a finite positive number")
        try:
            rate = float(fps)
        except OverflowError as exc:
            raise ValueError("Frame rate must be representable") from exc
        if not math.isfinite(rate) or rate <= 0 or rate > 1_000_000_000:
            raise ValueError("Frame rate must be positive and representable in nanoseconds")
        period = 1_000_000_000 / rate
        if not math.isfinite(period):
            raise ValueError("Frame interval is not representable")
        self.period_ns = round(period)
        self._clock_ns, self._sleep = clock_ns, sleep
        self._last_now_ns = clock_ns()
        self.next_deadline_ns = self._last_now_ns + self.period_ns
        self.skipped_intervals = 0
        self.clock_resets = 0
        self.last_wait: dict[str, int] | None = None

    def _now(self) -> int:
        now = self._clock_ns()
        if now < self._last_now_ns:
            # perf_counter is monotonic, but reset defensively for a replaced
            # clock or an invalid runtime clock. Never sleep until an old epoch.
            self.next_deadline_ns = now + self.period_ns
            self.clock_resets += 1
        self._last_now_ns = now
        return now

    def wait_next(self) -> None:
        resets_before = self.clock_resets
        now = self._now()
        entered = now
        initial_deadline = self.next_deadline_ns
        requested_wait = sleep_calls = 0
        while now < self.next_deadline_ns:
            wait_ns = self.next_deadline_ns - now
            requested_wait += wait_ns
            sleep_calls += 1
            self._sleep(wait_ns / 1_000_000_000)
            now = self._now()
        deadline = self.next_deadline_ns
        skipped = (now - self.next_deadline_ns) // self.period_ns
        self.skipped_intervals += skipped
        self.next_deadline_ns += (skipped + 1) * self.period_ns
        # Reuse the clock reads that already control pacing. Instrumentation
        # must not change the number of reads or introduce additional waits.
        self.last_wait = {
            "entered_ns": entered,
            "initial_deadline_ns": initial_deadline,
            "deadline_ns": deadline,
            "wake_ns": now,
            "requested_wait_ns": requested_wait,
            "sleep_calls": sleep_calls,
            "lateness_ns": now - deadline,
            "skipped_intervals": skipped,
            "clock_resets": self.clock_resets - resets_before,
        }

    def snapshot(self) -> dict[str, int] | None:
        """Return the last completed wait, without reading the clock again.

        ``deadline_ns`` follows a defensive clock reset, if one occurred.
        ``requested_wait_ns`` is the sum of sleep requests, not elapsed sleep
        time; early wake-ups can make these different.
        """
        return dict(self.last_wait) if self.last_wait is not None else None

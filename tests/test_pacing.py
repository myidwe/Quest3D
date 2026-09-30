"""Deadline behavior using a controlled clock, without real waits or hardware."""
import math

import pytest

from quest3d.pacing import FramePacer


class Clock:
    def __init__(self, *, start=0, oversleep=0):
        self.now = start
        self.oversleep = oversleep
        self.sleeps = []

    def read(self):
        return self.now

    def sleep(self, seconds):
        assert seconds > 0
        self.sleeps.append(seconds)
        self.now += round(seconds * 1_000_000_000) + self.oversleep

    def advance_ms(self, milliseconds):
        self.now += round(milliseconds * 1_000_000)


def pacer(clock, fps=50):
    return FramePacer(fps, clock_ns=clock.read, sleep=clock.sleep)


@pytest.mark.parametrize("fps", [0, -1, True, False, "30", None,
                                 math.nan, math.inf, -math.inf, 1e10, 1e-310, 10 ** 400])
def test_invalid_rate_is_rejected_before_clock_or_sleep(fps):
    def forbidden(*args):
        pytest.fail("Invalid settings must not start pacing")
    with pytest.raises(ValueError):
        FramePacer(fps, clock_ns=forbidden, sleep=forbidden)


def test_first_tick_is_immediate_and_work_time_is_part_of_the_interval():
    clock = Clock(start=3_000_000_000)
    timer = pacer(clock)
    assert clock.sleeps == []
    assert clock.now == 3_000_000_000
    clock.advance_ms(7)
    timer.wait_next()
    assert clock.now == 3_020_000_000
    assert clock.sleeps == [.013]


def test_changing_work_cost_keeps_the_same_phase():
    clock = Clock()
    timer = pacer(clock)
    starts = [clock.now]
    for cost in (3, 17, 2, 19, 6):
        clock.advance_ms(cost)
        timer.wait_next()
        starts.append(clock.now)
    assert starts == [i * 20_000_000 for i in range(6)]
    assert timer.skipped_intervals == 0


def test_sleep_overshoot_does_not_accumulate_over_many_frames():
    clock = Clock(oversleep=2_000_000)
    timer = pacer(clock, 30)
    for frame in range(1, 10_001):
        clock.advance_ms(4)
        timer.wait_next()
        assert clock.now == frame * timer.period_ns + 2_000_000
    assert timer.skipped_intervals == 0


def test_slow_frame_discards_backlog_then_waits_for_the_next_deadline():
    clock = Clock()
    timer = pacer(clock)
    clock.advance_ms(75)
    timer.wait_next()
    assert clock.now == 75_000_000
    assert clock.sleeps == []
    assert timer.skipped_intervals == 2
    # No catch-up burst of three queued ticks at 75ms.
    timer.wait_next()
    assert clock.now == 80_000_000
    timer.wait_next()
    assert clock.now == 100_000_000
    assert clock.sleeps == [.005, .02]


def test_sustained_overload_does_not_add_sleep_or_build_a_queue():
    clock = Clock()
    timer = pacer(clock, 30)
    for frame in range(1, 201):
        clock.advance_ms(45)
        timer.wait_next()
        assert clock.now == frame * 45_000_000
        assert clock.now < timer.next_deadline_ns <= clock.now + timer.period_ns
    assert clock.sleeps == []
    assert timer.skipped_intervals > 0


def test_long_sleep_overshoot_also_discards_missed_deadlines():
    clock = Clock(oversleep=85_000_000)
    timer = pacer(clock)
    timer.wait_next()
    assert clock.now == 105_000_000
    assert timer.skipped_intervals == 4
    clock.oversleep = 0
    timer.wait_next()
    assert clock.now == 120_000_000
    assert clock.sleeps == [.02, .015]


def test_early_wake_waits_for_the_remaining_interval():
    clock = Clock()
    requests = []
    def early_sleep(seconds):
        requests.append(seconds)
        clock.now += min(round(seconds * 1e9), 7_000_000)
    timer = FramePacer(50, clock_ns=clock.read, sleep=early_sleep)
    timer.wait_next()
    assert clock.now == 20_000_000
    assert requests == [.02, .013, .006]


def test_clock_reversal_between_ticks_starts_a_new_epoch():
    clock = Clock(start=1_000_000_000)
    timer = pacer(clock)
    timer.wait_next()
    clock.now = 10_000_000
    timer.wait_next()
    assert clock.now == 30_000_000
    assert timer.next_deadline_ns == 50_000_000
    assert timer.clock_resets == 1
    assert max(clock.sleeps) == .02


def test_clock_reversal_while_sleeping_never_waits_for_the_old_epoch():
    clock = Clock(start=5_000_000_000)
    calls = []
    def reset_once(seconds):
        calls.append(seconds)
        if len(calls) == 1:
            clock.now = 0
        else:
            clock.sleep(seconds)
    timer = FramePacer(50, clock_ns=clock.read, sleep=reset_once)
    timer.wait_next()
    assert clock.now == 20_000_000
    assert calls == [.02, .02]
    assert timer.clock_resets == 1


def test_exact_deadline_needs_no_sleep_and_advances_once():
    clock = Clock()
    timer = pacer(clock)
    clock.advance_ms(20)
    timer.wait_next()
    assert clock.sleeps == []
    assert timer.next_deadline_ns == 40_000_000
    assert timer.skipped_intervals == 0


def test_wait_snapshot_has_exact_deadline_and_preserves_none_return():
    clock = Clock(start=1_000_000_000, oversleep=2_000_000)
    timer = pacer(clock)
    assert timer.snapshot() is None
    clock.advance_ms(7)
    assert timer.wait_next() is None
    assert timer.snapshot() == {
        "entered_ns": 1_007_000_000,
        "initial_deadline_ns": 1_020_000_000,
        "deadline_ns": 1_020_000_000,
        "wake_ns": 1_022_000_000,
        "requested_wait_ns": 13_000_000,
        "sleep_calls": 1,
        "lateness_ns": 2_000_000,
        "skipped_intervals": 0,
        "clock_resets": 0,
    }
    snapshot = timer.snapshot()
    snapshot["wake_ns"] = -1
    assert timer.snapshot()["wake_ns"] == 1_022_000_000


def test_instrumentation_preserves_clock_read_count_and_early_wakes():
    clock = Clock()
    reads = []
    def read():
        reads.append(clock.now)
        return clock.now
    def sleep(seconds):
        clock.now += min(round(seconds * 1e9), 7_000_000)
    timer = FramePacer(50, clock_ns=read, sleep=sleep)
    assert reads == [0]
    timer.wait_next()
    assert reads == [0, 0, 7_000_000, 14_000_000, 20_000_000]
    snapshot = timer.snapshot()
    assert len(reads) == 5
    assert snapshot["sleep_calls"] == 3
    assert snapshot["requested_wait_ns"] == 39_000_000
    assert snapshot["lateness_ns"] == 0
    clock.advance_ms(55)
    timer.wait_next()
    assert reads[-1] == 75_000_000
    assert len(reads) == 6
    assert timer.snapshot()["sleep_calls"] == 0
    assert timer.snapshot()["skipped_intervals"] == 1
    assert timer.snapshot()["lateness_ns"] == 35_000_000


def test_snapshot_marks_clock_reset_instead_of_negative_elapsed_sleep():
    clock = Clock(start=5_000_000_000)
    calls = []
    def reset_once(seconds):
        calls.append(seconds)
        if len(calls) == 1:
            clock.now = 0
        else:
            clock.sleep(seconds)
    timer = FramePacer(50, clock_ns=clock.read, sleep=reset_once)
    timer.wait_next()
    snapshot = timer.snapshot()
    assert snapshot["clock_resets"] == 1
    assert snapshot["initial_deadline_ns"] == 5_020_000_000
    assert snapshot["deadline_ns"] == 20_000_000
    assert snapshot["wake_ns"] == 20_000_000
    assert snapshot["requested_wait_ns"] == 40_000_000
    assert snapshot["sleep_calls"] == 2

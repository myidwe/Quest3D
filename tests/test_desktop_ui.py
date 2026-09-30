"""UI intent/state regressions; no Tk window, worker, GPU, or live process."""

from types import SimpleNamespace

import pytest

from quest3d.desktop_ui import DesktopWindow, _availability, _monitor_choices, _parse_depth


class Value:
    def __init__(self, value=None): self.value = value
    def set(self, value): self.value = value
    def get(self): return self.value


class Controller:
    def __init__(self):
        self.calls = []
        self.accept = True
        self.snapshot = {"phase": "running", "busy": False, "running": True,
                         "host_running": True, "mode": "3d", "profile": "comfort", "depth_percent": 1.31}
    def command(self, action, **kwargs):
        self.calls.append((action, kwargs))
        return self.accept
    def get_snapshot(self): return dict(self.snapshot)


def view():
    # Drive the production event handlers without creating a desktop window.
    window = DesktopWindow.__new__(DesktopWindow)
    window.controller = Controller()
    window._snapshot = dict(window.controller.snapshot)
    window._queued = False
    window._transient_notice = False
    window._editing_depth = False
    window._depth_dirty = False
    window._setting_depth = False
    window._destroyed = False
    window._after_id = None
    window._depth = Value("1.31")
    window._notice = Value("")
    window._comfort = Value(True)
    window._pin = Value("")
    window._monitor = Value("")
    window._monitors = {}
    window.renders = []
    window._render = lambda snapshot: window.renders.append(dict(snapshot))
    window.scheduled, window.cancelled = [], []
    window.root = SimpleNamespace(after=lambda ms, callback: window.scheduled.append((ms, callback)) or "timer",
                                  after_cancel=lambda handle: window.cancelled.append(handle))
    return window


@pytest.mark.parametrize("text,expected", [("1.3125", 1.3125), (" 1.31% ", 1.31), ("0", 0), ("4", 4)])
def test_numeric_apply_keeps_precision_and_accepts_bounds(text, expected):
    assert _parse_depth(text) == expected


@pytest.mark.parametrize("value", ["", "nan", "inf", "-inf", "1.2.3", "-0.01", "4.001", None, True])
def test_invalid_depth_is_rejected_without_coercion(value):
    with pytest.raises(ValueError):
        _parse_depth(value)


@pytest.mark.parametrize("phase", ["starting", "stopping", "unknown", None])
def test_ambiguous_or_transition_state_cannot_start_stop_or_control(phase):
    state = _availability(dict(phase=phase, busy=False, running=True, host_running=True))
    assert not any((state.start, state.stop, state.control, state.monitor))


def test_monitor_requires_stopped_pc_and_owned_error_can_be_stopped():
    stopped = _availability(dict(phase="idle", busy=False, running=False, host_running=False))
    assert stopped.start and stopped.monitor and not stopped.stop and not stopped.control
    partial = _availability(dict(phase="error", busy=False, running=False, host_running=True))
    assert partial.stop and not partial.start and not partial.monitor and not partial.control
    busy = _availability(dict(phase="running", busy=True, running=True, host_running=True))
    assert not any((busy.start, busy.stop, busy.control, busy.monitor))


@pytest.mark.parametrize("with_ready", [False, True])
def test_ready_producer_can_start_missing_host_or_explicitly_stop(with_ready):
    window = view()
    window._snapshot.update(host_running=False, error=None)
    if with_ready:
        window._snapshot["ready"] = True
    state = _availability(window._snapshot)
    assert state.start and state.stop and state.control and not state.monitor
    window._primary()
    assert window.controller.calls == [("start", {})], "Continue the existing producer through host startup"
    assert window._snapshot["profile"] == "comfort" and window._snapshot["mode"] == "3d"
    window._queued = False
    window._stop()
    assert window.controller.calls[-1] == ("stop", {})


@pytest.mark.parametrize("phase", ["running", "error"])
def test_partial_start_failure_keeps_stop_but_blocks_retry_and_controls(phase):
    window = view()
    window._snapshot.update(phase=phase, host_running=False, ready=False, error="화면 준비 실패")
    state = _availability(window._snapshot)
    assert state.stop and not any((state.start, state.control, state.monitor))
    assert not window._adjust_depth(0.05)
    assert not window._control(mode="2d")
    window._primary()
    assert window.controller.calls == [("stop", {})]


@pytest.mark.parametrize("running,host_running", [(True, False), (False, True), (True, True), (False, False)])
def test_conflict_can_stop_only_confirmed_managed_processes(running, host_running):
    window = view()
    window._snapshot.update(phase="conflict", running=running, host_running=host_running, ready=False)
    state = _availability(window._snapshot)
    assert state.stop == (running or host_running)
    assert not any((state.start, state.control, state.monitor))
    window._primary()
    assert window.controller.calls == ([("stop", {})] if running or host_running else [])


def test_running_partial_error_can_refresh_without_switching_monitor_or_restarting():
    window = view()
    window._snapshot.update(phase="error", host_running=False, error="전송 상태 확인 필요", ready=False)
    window._refresh()
    assert window.controller.calls == [("refresh", {})]
    assert window._snapshot["running"] and window._snapshot["profile"] == "comfort"
    window._refresh()
    assert len(window.controller.calls) == 1
    window._queued = False
    window._snapshot["busy"] = True
    window._refresh()
    assert len(window.controller.calls) == 1


def test_depth_buttons_send_delta_not_stale_absolute_depth():
    window = view()
    window._snapshot["depth_percent"] = 1.31
    window.controller.snapshot["depth_percent"] = 1.61
    assert window._adjust_depth(0.05)
    assert window.controller.calls == [("control", {"depth_delta": 0.05})]
    assert window._snapshot["depth_percent"] == 1.31, "Enqueued request is not an applied acknowledgement"
    assert not window._adjust_depth(-0.05)
    assert len(window.controller.calls) == 1, "A second click before the next snapshot must not enqueue again"
    window._poll()
    assert window.scheduled[0][0] == 500
    assert window.renders[-1]["depth_percent"] == 1.61


def test_editing_and_dirty_text_survive_poll_until_explicit_apply_or_escape():
    window = view()
    window._set_editing(True)
    window._depth.set("1.3125")
    window._depth_changed()
    window._sync_depth(1.61)
    assert window._depth.get() == "1.3125"
    window._set_editing(False)
    window._sync_depth(1.71)
    assert window._depth.get() == "1.3125", "An unfinished edit survives focus leaving the entry"
    window._apply_depth()
    assert window.controller.calls == [("control", {"depth_percent": 1.3125})]
    assert not window._depth_dirty
    window._snapshot["depth_percent"] = 1.81
    window._depth.set("draft")
    window._depth_changed()
    assert window._reset_depth() == "break"
    assert window._depth.get() == "1.81" and not window._depth_dirty


def test_invalid_or_busy_apply_does_not_drop_user_edit():
    window = view()
    window._depth.set("nan")
    window._depth_changed()
    window._apply_depth()
    assert not window.controller.calls and window._depth_dirty and window._notice.get()
    window._depth.set("1.3125")
    window.controller.accept = False
    window._apply_depth()
    assert window._depth_dirty and not window._queued
    assert window._depth.get() == "1.3125"


def test_mode_and_contour_requests_are_narrow_and_do_not_optimistically_apply():
    window = view()
    window._comfort.set(False)  # Tk has toggled the checkbox before invoking its command.
    window._toggle_comfort()
    assert window.controller.calls == [("control", {"profile": "linear"})]
    assert window._comfort.get() is True and window._snapshot["profile"] == "comfort"
    window._poll()
    window._control(mode="2d")
    assert window.controller.calls[-1] == ("control", {"mode": "2d"})
    assert window._snapshot["mode"] == "3d"


def test_monitor_identity_is_not_confused_by_duplicate_labels():
    choices = _monitor_choices([
        {"device_name": "DISPLAY1", "label": "모니터", "index": 1},
        {"device_name": "DISPLAY2", "label": "모니터", "index": 2},
        {"device_name": "DISPLAY3", "label": "모니터", "index": 3}])
    assert len(choices) == 3 and set(choices.values()) == {"DISPLAY1", "DISPLAY2", "DISPLAY3"}
    window = view()
    window._monitors = choices
    window._monitor.set(next(label for label, device in choices.items() if device == "DISPLAY2"))
    window._select_monitor()
    assert not window.controller.calls, "Running capture cannot switch monitors"
    window._snapshot.update(phase="idle", running=False, host_running=False, selected_monitor="DISPLAY1")
    window._select_monitor()
    assert window.controller.calls == [("select_monitor", {"device_name": "DISPLAY2"})]


def test_failed_snapshot_never_enables_actions_or_claims_connection():
    window = view()
    def failed(): raise RuntimeError("상태 파일을 읽을 수 없음")
    window.controller.get_snapshot = failed
    window._poll()
    snapshot = window.renders[-1]
    assert snapshot["phase"] == "conflict" and snapshot["busy"]
    assert not _availability(snapshot).start and not _availability(snapshot).stop
    assert "connected" not in snapshot and window.scheduled[0][0] == 500


def test_minimize_and_close_callbacks_do_not_directly_change_process_state():
    window = view()
    hidden = []
    window.on_hide = lambda: hidden.append(True)
    window.root.state = lambda: "iconic"
    window._on_unmap(SimpleNamespace(widget=object()))
    assert not hidden
    window._on_unmap(SimpleNamespace(widget=window.root))
    assert hidden == [True]
    window._after_id = "timer"
    window.close()
    window._poll()
    assert window.cancelled == ["timer"] and not window.scheduled
    assert not window.controller.calls, "The application callback owns worker shutdown"


def test_log_guide_and_diagnostics_are_controller_commands():
    window = view()
    for action in ("open_guide", "open_logs", "export_diagnostics", "open_diagnostics"):
        assert window._dispatch(action)
        assert window.controller.calls[-1] == (action, {})
        window._poll()
    assert window._notice.get() == "", "A queued notice must not remain after later snapshots forever"


@pytest.mark.parametrize("value", ["", "123", "12345", "1a34", "１２３４", "12 4"])
def test_pair_requires_exactly_four_ascii_digits_without_dispatch(value):
    window = view()
    window._snapshot["pairing_available"] = True
    window._pin.set(value)
    assert window._pair() == "break"
    assert not window.controller.calls
    assert window._pin.get() == value
    assert "4자리" in window._notice.get()


@pytest.mark.parametrize("updates", [{"host_running": False}, {"pairing_available": False}, {"busy": True}])
def test_pair_cannot_run_without_verified_available_host(updates):
    window = view()
    window._snapshot["pairing_available"] = True
    window._snapshot.update(updates)
    window._pin.set("0123")
    window._pair()
    assert not window.controller.calls


def test_pair_preserves_leading_zero_and_clears_pin_only_after_acceptance():
    window = view()
    window._snapshot["pairing_available"] = True
    window._pin.set("0123")
    window.controller.accept = False
    window._pair()
    assert window._pin.get() == "0123"
    window.controller.accept = True
    window._pair()
    assert window.controller.calls[-1] == ("pair", {"pin": "0123"})
    assert window._pin.get() == ""
    window._pin.set("9876")
    window._pair()
    assert len(window.controller.calls) == 2, "An enqueued approval cannot be duplicated"

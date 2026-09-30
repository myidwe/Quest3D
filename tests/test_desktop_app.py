"""Application lifecycle with fake Tk/shell/controller objects, never real UI.

These tests cover ownership decisions and callback ordering, not Windows tray
registration, Tk rendering, process execution, or headset streaming.
"""
from __future__ import annotations

import copy
import queue
import sys
from types import SimpleNamespace

import pytest

from quest3d import desktop


class FakeWidget:
    def __init__(self, parent=None, *, events=None, **kwargs):
        self.parent = parent
        self.events = events if events is not None else parent.events
        self.destroyed = False
        self.kwargs = kwargs
        self.protocols = {}
        self.lifts = 0
        self.withdrawn = False
        self.scheduled = []

    def _alive(self):
        if self.destroyed:
            raise RuntimeError("fake Tk: a destroyed window was accessed")

    def pack(self, **kwargs):
        self._alive()

    def title(self, *args):
        self._alive()

    def resizable(self, *args):
        self._alive()

    def transient(self, *args):
        self._alive()

    def protocol(self, name, callback):
        self.protocols[name] = callback

    def update_idletasks(self):
        self._alive()

    def geometry(self, value):
        self._alive()

    def grab_set(self):
        self._alive()

    def grab_release(self):
        self._alive()

    def winfo_rootx(self):
        return 30

    def winfo_rooty(self):
        return 40

    def winfo_exists(self):
        return not self.destroyed

    def lift(self):
        self._alive()
        self.lifts += 1

    def deiconify(self):
        self._alive()
        self.withdrawn = False

    def focus_force(self):
        self._alive()

    def withdraw(self):
        self._alive()
        self.withdrawn = True

    def after(self, delay, callback):
        self._alive()
        self.scheduled.append((delay, callback))
        return str(len(self.scheduled))

    def after_cancel(self, token):
        self._alive()

    def destroy(self):
        self._alive()
        self.destroyed = True
        self.events.append("root.destroy" if self.parent is None else "dialog.destroy")

    def iconbitmap(self, value):
        self._alive()

    def mainloop(self):
        self._alive()
        self.events.append("mainloop")


class FakeController:
    def __init__(self, events):
        self.events = events
        self.state = dict(phase="running", busy=False, running=True, host_running=True, error=None)
        self.commands = []
        self.accept = True

    def get_snapshot(self):
        return copy.deepcopy(self.state)

    def command(self, action, **kwargs):
        self.commands.append((action, kwargs))
        if self.accept:
            self.state["busy"] = True
        return self.accept

    def close(self):
        self.events.append("controller.close")


class FakeShell:
    def __init__(self, events):
        self.events = events
        self.tray_available = True
        self.fail_close = False

    def close(self):
        self.events.append("shell.close")
        if self.fail_close:
            raise RuntimeError("shell join still pending; owner retained")


@pytest.fixture
def rig(monkeypatch):
    events, buttons, dialogs, messages = [], [], [], []
    app = desktop.DesktopApp.__new__(desktop.DesktopApp)
    app.events = queue.Queue()
    app.root = FakeWidget(events=events)
    app.controller = FakeController(events)
    app.shell = FakeShell(events)
    app.window = SimpleNamespace(close=lambda: events.append("window.close"))
    app.primary = True
    app.closing = False
    app.dialog = None
    # __new__ fixture follows the production object's explicit finish guard.
    app.finished = False

    def dialog(parent, **kwargs):
        value = FakeWidget(parent, **kwargs)
        dialogs.append(value)
        return value

    def button(parent, **kwargs):
        value = FakeWidget(parent, **kwargs)
        buttons.append(value)
        return value

    monkeypatch.setattr(desktop.tk, "Tk", lambda: pytest.fail("A real Tk root must never be created"))
    monkeypatch.setattr(desktop.tk, "Toplevel", dialog)
    monkeypatch.setattr(desktop.ttk, "Frame", FakeWidget)
    monkeypatch.setattr(desktop.ttk, "Label", FakeWidget)
    monkeypatch.setattr(desktop.ttk, "Button", button)
    monkeypatch.setattr(desktop.messagebox, "showinfo", lambda *args, **kwargs: messages.append(("info", args)))
    monkeypatch.setattr(desktop.messagebox, "showerror", lambda *args, **kwargs: messages.append(("error", args)))

    def choose(text):
        selected = next(b for b in buttons if b.kwargs["text"] == text)
        assert selected.kwargs.get("state", "normal") != "disabled"
        selected.kwargs["command"]()

    return SimpleNamespace(app=app, events=events, dialogs=dialogs, buttons=buttons,
                           messages=messages, choose=choose, monkeypatch=monkeypatch)


def test_hide_preserves_stream_and_controller_ownership(rig):
    rig.app.hide()
    assert rig.app.root.withdrawn
    assert not rig.app.controller.commands and not rig.events


def test_missing_tray_keeps_window_accessible(rig):
    rig.app.shell.tray_available = False
    rig.app.hide()
    assert not rig.app.root.withdrawn
    assert rig.messages[0][0] == "info"
    assert not rig.app.controller.commands and not rig.events


def test_busy_exit_does_not_close_or_submit_stop(rig):
    rig.app.controller.state["busy"] = True
    rig.app.request_exit()
    assert rig.messages and not rig.dialogs
    assert not rig.app.controller.commands and not rig.events
    assert not rig.app.root.destroyed


def test_initial_inspection_must_finish_before_idle_exit(rig):
    rig.app.controller.state.update(phase="checking", busy=False, running=False, host_running=False)
    rig.app.request_exit()
    assert rig.messages and not rig.app.root.destroyed
    assert not rig.events and not rig.app.controller.commands


def test_idle_exit_releases_shell_before_destroying_tk(rig):
    rig.app.controller.state.update(running=False, host_running=False)
    rig.app.request_exit()
    assert rig.events == ["shell.close", "window.close", "controller.close", "root.destroy"]
    assert not rig.app.controller.commands


def test_live_exit_uses_one_dialog_and_cancel_keeps_stream(rig):
    rig.app.request_exit()
    rig.app.request_exit()
    assert len(rig.dialogs) == 1 and rig.dialogs[0].lifts == 1
    rig.choose("취소")
    assert rig.app.dialog is None
    assert not rig.app.root.destroyed and not rig.app.controller.commands
    assert rig.events == ["dialog.destroy"]


def test_dialog_close_button_is_cancel(rig):
    rig.app.request_exit()
    rig.dialogs[0].protocols["WM_DELETE_WINDOW"]()
    assert rig.app.dialog is None and not rig.app.controller.commands
    assert not rig.app.root.destroyed


def test_dialog_hide_keeps_pipeline_running(rig):
    rig.app.request_exit()
    rig.choose("트레이로 보내기")
    assert rig.app.root.withdrawn and not rig.app.controller.commands
    assert rig.events == ["dialog.destroy"]


def test_dialog_disables_hide_without_tray(rig):
    rig.app.shell.tray_available = False
    rig.app.request_exit()
    button = next(b for b in rig.buttons if b.kwargs["text"] == "트레이로 보내기")
    assert button.kwargs["state"] == "disabled"


def test_stop_and_exit_waits_for_actual_backend_completion(rig):
    rig.app.request_exit()
    rig.choose("중지하고 종료")
    assert rig.app.controller.commands == [("stop", {})]
    assert rig.app.closing and not rig.app.root.destroyed
    rig.app.request_exit()
    assert len(rig.app.controller.commands) == 1
    rig.app._wait_stop()
    assert not rig.app.root.destroyed
    assert [ms for ms, _ in rig.app.root.scheduled] == [250, 250]
    rig.app.controller.state.update(busy=False, running=False, host_running=False, error=None)
    rig.app._wait_stop()
    assert rig.app.root.destroyed
    assert rig.events[-4:] == ["shell.close", "window.close", "controller.close", "root.destroy"]


@pytest.mark.parametrize("state", [dict(running=True, host_running=False, error=None),
    dict(running=False, host_running=True, error=None),
    dict(running=False, host_running=False, error="cleanup unconfirmed")])
def test_unconfirmed_stop_keeps_app_accessible_for_recovery(rig, state):
    rig.app.closing = True
    rig.app.controller.state.update(busy=False, **state)
    rig.app._wait_stop()
    assert not rig.app.closing and not rig.app.root.destroyed
    assert rig.messages[0][0] == "error"
    assert not rig.events


def test_rejected_stop_submission_does_not_begin_exit(rig):
    rig.app.controller.accept = False
    rig.app.stop_and_exit()
    assert not rig.app.closing and not rig.app.root.scheduled
    assert not rig.app.root.destroyed


def test_hide_is_ignored_while_waiting_for_stop(rig):
    rig.app.closing = True
    rig.app.hide()
    assert not rig.app.root.withdrawn and not rig.messages


def test_shell_close_failure_preserves_ui_and_allows_retry(rig):
    rig.app.closing = True
    rig.app.controller.state.update(running=False, host_running=False, error=None)
    rig.app.shell.fail_close = True
    try:
        rig.app._wait_stop()
    except RuntimeError as exc:
        # Callback may propagate to Tk's error reporter, but the exit latch must
        # be released so a real retained shell owner can be joined on retry.
        assert "owner retained" in str(exc)
    assert not rig.app.closing, "failed shell join permanently disabled Exit"
    assert not rig.app.root.destroyed
    assert rig.events == ["shell.close"]
    rig.app.shell.fail_close = False
    rig.app.request_exit()
    assert rig.app.root.destroyed


def test_tray_exit_does_not_process_later_events_or_reschedule_destroyed_root(rig):
    rig.app.controller.state.update(running=False, host_running=False)
    rig.app.events.put("exit")
    rig.app.events.put("show")
    rig.app.events.put("exit")
    rig.app._events()
    assert rig.app.root.destroyed
    assert rig.events.count("root.destroy") == 1
    assert not rig.app.root.scheduled


def test_finish_is_idempotent_after_success(rig):
    rig.app.finish()
    rig.app.finish()
    assert rig.events == ["shell.close", "window.close", "controller.close", "root.destroy"]


def test_shell_callbacks_only_enqueue_until_tk_event_dispatch(rig, tmp_path, monkeypatch):
    calls = []
    callbacks = {}

    def shell_factory(directory, on_show, on_stop, on_exit):
        callbacks.update(show=on_show, stop=on_stop, exit=on_exit)
        calls.append("shell-created")
        return SimpleNamespace(start=lambda: False, notified_existing=True, last_error=None)

    monkeypatch.setitem(sys.modules, "quest3d.capture", SimpleNamespace(enable_per_monitor_dpi_awareness=lambda: calls.append("dpi")))
    monkeypatch.setitem(sys.modules, "quest3d.desktop_backend", SimpleNamespace(DesktopController=lambda *args: pytest.fail("Secondary instance must not create a backend")))
    monkeypatch.setitem(sys.modules, "quest3d.desktop_shell", SimpleNamespace(ShellIntegration=shell_factory))
    monkeypatch.setitem(sys.modules, "quest3d.desktop_ui", SimpleNamespace(DesktopWindow=lambda *args, **kwargs: pytest.fail("Secondary instance must not create UI")))
    app = desktop.DesktopApp(tmp_path)
    assert not app.primary and app.root is None
    callbacks["show"]()
    callbacks["stop"]()
    callbacks["exit"]()
    assert [app.events.get_nowait() for _ in range(3)] == ["show", "stop", "exit"]
    app.run()
    assert calls == ["dpi", "shell-created"]

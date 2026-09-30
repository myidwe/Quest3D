"""Real Qt signals/timers; fake Windows shell, controller and visible window.

No live host, tray registration, desktop window or process termination.
"""
from __future__ import annotations

import copy
import sys
from types import SimpleNamespace

import pytest

pytest.importorskip('PySide6.QtCore')
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QSignalSpy

from quest3d import desktop_qt
from quest3d.desktop_qt_adapter import DesktopQtAdapter


class Controller:
    def __init__(self, events):
        self.events = events
        self.state = dict(phase='running', busy=False, running=True, ready=True,
                          host_running=True, error=None, pairing_available=True)
        self.calls = []
        self.accept = True
        self.state_error = False
        self.command_error = False

    def get_snapshot(self):
        if self.state_error:
            raise RuntimeError('private backend details')
        return copy.deepcopy(self.state)

    def command(self, action, **kwargs):
        if self.command_error:
            raise RuntimeError('private backend details')
        self.calls.append((action, kwargs))
        if self.accept:
            self.state['busy'] = True
        return self.accept

    def close(self):
        self.events.append('controller.close')


class Shell:
    def __init__(self, events):
        self.events = events
        self.tray_available = True
        self.close_error = False

    def close(self):
        self.events.append('shell.close')
        if self.close_error:
            raise RuntimeError('retained owner')


class Window:
    def __init__(self, events):
        self.events = events
        self.hidden = False
        self.closed = False
        self.state = Qt.WindowState.WindowNoState
        self.normal_restores = 0

    def _alive(self):
        assert not self.closed, 'A closed QML window was accessed'

    def windowState(self):
        self._alive()
        return self.state

    def showNormal(self):
        self._alive()
        self.state = Qt.WindowState.WindowNoState
        self.normal_restores += 1
        self.hidden = False

    def show(self):
        self._alive()
        self.hidden = False

    def raise_(self):
        self._alive()

    def requestActivate(self):
        self._alive()

    def hide(self):
        self._alive()
        self.hidden = True

    def close(self):
        self._alive()
        self.closed = True
        self.events.append('window.close')


@pytest.fixture(scope='module')
def application():
    yield QCoreApplication.instance() or QCoreApplication([])


@pytest.fixture
def rig(application):
    events = []
    controller = Controller(events)
    adapter = DesktopQtAdapter(controller, poll=False)
    app = SimpleNamespace(quit=lambda: events.append('application.quit'))
    shell = Shell(events)
    lifecycle = desktop_qt.QtDesktopLifecycle(app, controller, adapter, shell)
    window = lifecycle.window = Window(events)
    yield SimpleNamespace(events=events, controller=controller, adapter=adapter, shell=shell,
                          lifecycle=lifecycle, window=window)
    lifecycle._stop_timer.stop()
    adapter.close()
    lifecycle.deleteLater()
    adapter.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_qml_hide_signal_uses_real_adapter_and_preserves_stream(rig):
    rig.adapter.requestHide()
    assert rig.window.hidden
    assert not rig.controller.calls and not rig.events


def test_qml_exit_signal_opens_single_prompt_and_cancel_keeps_stream(rig):
    spy = QSignalSpy(rig.lifecycle.closePromptChanged)
    rig.adapter.requestExit()
    rig.adapter.requestExit()
    assert rig.lifecycle.closePrompt
    assert spy.count() == 1
    rig.lifecycle.cancelClose()
    assert not rig.lifecycle.closePrompt
    assert not rig.controller.calls and not rig.events


def test_missing_tray_restores_hidden_window_and_uses_adapter_notice(rig):
    rig.window.hidden = True
    rig.shell.tray_available = False
    rig.lifecycle.hide()
    assert not rig.window.hidden
    assert rig.adapter.notice == '트레이 사용 불가 · 창 유지'
    assert rig.adapter.noticeKind == 'error'
    assert not rig.controller.calls and not rig.events


def test_minimized_qt_enum_restores_window(rig):
    rig.window.state = Qt.WindowState.WindowMinimized
    rig.lifecycle.show()
    assert rig.window.normal_restores == 1
    rig.window.state = Qt.WindowState.WindowMaximized
    rig.lifecycle.show()
    assert rig.window.state == Qt.WindowState.WindowMaximized


@pytest.mark.parametrize('state', [dict(busy=True), dict(phase='checking'),
                                  dict(phase='starting'), dict(phase='stopping')])
def test_busy_or_transition_exit_remains_accessible_without_stop(rig, state):
    rig.controller.state.update(state)
    rig.window.hidden = True
    rig.lifecycle.requestClose()
    assert not rig.window.hidden and not rig.lifecycle.closePrompt
    assert not rig.events and not rig.controller.calls
    assert rig.adapter.notice == '작업 진행 중 · 완료 후 종료 가능'


def test_idle_exit_releases_shell_then_disposes_and_quits_once(rig):
    rig.controller.state.update(phase='idle', running=False, host_running=False)
    rig.lifecycle.requestClose()
    rig.lifecycle.finish()
    assert rig.events == ['shell.close', 'controller.close', 'window.close', 'application.quit']
    assert rig.lifecycle.allowClose
    assert rig.adapter._closed
    assert not rig.adapter._poll_timer.isActive()


def test_stop_and_exit_waits_for_authoritative_backend_completion(rig):
    rig.lifecycle.requestClose()
    rig.lifecycle.stopAndExit()
    assert rig.controller.calls == [('stop', {})]
    assert rig.lifecycle._closing and rig.lifecycle._stop_timer.isActive()
    assert not rig.lifecycle.closePrompt
    rig.lifecycle.stopAndExit()
    rig.lifecycle.requestClose()
    rig.lifecycle.hide()
    rig.lifecycle.shellEvent('stop')
    rig.lifecycle._wait_stop()
    assert len(rig.controller.calls) == 1
    assert not rig.events and not rig.window.closed and not rig.window.hidden
    rig.controller.state.update(phase='idle', busy=False, running=False, host_running=False)
    rig.lifecycle._wait_stop()
    assert rig.lifecycle.allowClose and rig.window.closed
    assert not rig.lifecycle._stop_timer.isActive()


@pytest.mark.parametrize('state', [dict(running=True, host_running=False),
    dict(running=False, host_running=True), dict(running=False, host_running=False, error='cleanup failed'),
    dict(running=False, host_running=False, phase='checking')])
def test_unconfirmed_stop_keeps_ui_owned_and_allows_retry(rig, state):
    rig.lifecycle.stopAndExit()
    rig.controller.state.update(busy=False, **state)
    rig.lifecycle._wait_stop()
    assert not rig.lifecycle._closing and not rig.lifecycle._stop_timer.isActive()
    assert not rig.window.closed and not rig.window.hidden
    assert not rig.events
    assert rig.adapter.notice == '송출 종료 미확인 · PC 상태 확인 필요'


@pytest.mark.parametrize('failure', ['rejected', 'raised'])
def test_stop_submission_failure_remains_recoverable(rig, failure):
    rig.lifecycle.requestClose()
    rig.controller.accept = failure != 'rejected'
    rig.controller.command_error = failure == 'raised'
    rig.lifecycle.stopAndExit()
    assert not rig.lifecycle._closing and not rig.lifecycle._stop_timer.isActive()
    assert rig.lifecycle.closePrompt
    assert not rig.events
    assert rig.adapter.notice == '송출 중지 요청 실패 · 재시도 필요'
    assert 'private' not in rig.adapter.notice


def test_stale_prompt_does_not_start_stop_after_backend_already_stopped(rig):
    rig.lifecycle.requestClose()
    rig.controller.state.update(phase='idle', running=False, host_running=False)
    rig.lifecycle.stopAndExit()
    assert not rig.controller.calls
    assert rig.window.closed


def test_shell_join_failure_preserves_recovery_and_retry(rig):
    rig.controller.state.update(phase='idle', running=False, host_running=False)
    rig.shell.close_error = True
    rig.lifecycle.requestClose()
    assert not rig.lifecycle._finished and not rig.lifecycle.allowClose
    assert not rig.adapter._closed and not rig.window.closed
    assert rig.events == ['shell.close']
    assert rig.adapter.notice == '트레이 종료 미확인 · 종료 재시도 필요'
    rig.shell.close_error = False
    rig.lifecycle.requestClose()
    assert rig.events == ['shell.close', 'shell.close', 'controller.close', 'window.close', 'application.quit']


def test_post_exit_events_never_touch_closed_window_or_backend(rig):
    rig.controller.state.update(phase='idle', running=False, host_running=False)
    rig.lifecycle.requestClose()
    for event in ('show', 'hide', 'stop', 'exit'):
        rig.lifecycle.shellEvent(event)
    rig.lifecycle.show()
    rig.lifecycle.hide()
    rig.lifecycle.requestClose()
    rig.lifecycle.stopAndExit()
    rig.lifecycle._wait_stop()
    assert not rig.controller.calls
    assert rig.events.count('application.quit') == 1


def test_tray_stop_uses_fresh_state_and_command_gate(rig):
    rig.controller.state.update(phase='conflict')
    rig.lifecycle.shellEvent('stop')
    assert rig.controller.calls == [('stop', {})]
    rig.lifecycle.shellEvent('stop')
    assert len(rig.controller.calls) == 1
    assert not rig.lifecycle._closing


@pytest.mark.parametrize('malformed', [None, {}, dict(busy=False, running=False, host_running=False),
    dict(phase='unknown', busy=False, running=False, host_running=False),
    dict(phase='idle', busy=False, running=None, host_running=False)])
def test_incomplete_state_cannot_release_owner(rig, malformed):
    rig.controller.state = malformed
    rig.lifecycle.requestClose()
    assert not rig.events and not rig.controller.calls
    assert not rig.window.closed
    assert rig.adapter.notice == 'PC 상태 확인 실패 · 잠시 후 재시도'


def test_snapshot_failure_during_shutdown_releases_close_latch_only(rig):
    rig.lifecycle.stopAndExit()
    rig.controller.state_error = True
    rig.lifecycle._wait_stop()
    assert not rig.lifecycle._closing and not rig.lifecycle._stop_timer.isActive()
    assert not rig.events and not rig.window.closed
    assert 'private' not in rig.adapter.notice


def _main_fakes(monkeypatch, tmp_path, *, primary, notified=True):
    events = []
    shell = SimpleNamespace(start=lambda: primary, notified_existing=notified, last_error=None,
                            close=lambda: events.append('shell.close'))
    callbacks = []
    def shell_factory(root, *values):
        callbacks.extend(values)
        return shell
    monkeypatch.setattr(desktop_qt, 'ROOT', tmp_path)
    monkeypatch.setattr(desktop_qt.os, 'chdir', lambda path: None)
    monkeypatch.setitem(sys.modules, 'quest3d.desktop_shell', SimpleNamespace(ShellIntegration=shell_factory))
    monkeypatch.setitem(sys.modules, 'quest3d.desktop_backend', SimpleNamespace(
        DesktopController=lambda directory: pytest.fail('Secondary instance created a controller')))
    monkeypatch.setattr(desktop_qt, 'QGuiApplication', lambda *args: pytest.fail('Secondary instance created Qt UI'))
    return events, callbacks


def test_secondary_launch_only_notifies_existing_owner(monkeypatch, tmp_path):
    events, callbacks = _main_fakes(monkeypatch, tmp_path, primary=False)
    assert desktop_qt.main(['--root', str(tmp_path)]) == 0
    for callback in callbacks:
        callback()  # Shell callbacks can only enqueue, even before Qt exists.
    assert events == []


def test_secondary_notification_failure_is_reported_without_controller(monkeypatch, tmp_path):
    events, _ = _main_fakes(monkeypatch, tmp_path, primary=False, notified=False)
    with pytest.raises(RuntimeError, match='기존 창 열기 실패'):
        desktop_qt.main(['--root', str(tmp_path)])
    assert events == []


def test_first_window_failure_releases_owned_shell(monkeypatch, tmp_path):
    events, _ = _main_fakes(monkeypatch, tmp_path, primary=True)
    monkeypatch.setattr(desktop_qt, 'QGuiApplication', lambda *args: (_ for _ in ()).throw(RuntimeError('Qt unavailable')))
    with pytest.raises(RuntimeError, match='Qt unavailable'):
        desktop_qt.main(['--root', str(tmp_path)])
    assert events == ['shell.close']


def _primary_qt_fakes(monkeypatch, tmp_path, *, window_ready):
    events, _ = _main_fakes(monkeypatch, tmp_path, primary=True)
    controller = Controller(events)
    controller.state.update(phase='idle', running=False, host_running=False)
    monkeypatch.setitem(sys.modules, 'quest3d.desktop_backend', SimpleNamespace(
        DesktopController=lambda directory: controller))
    captures = {}
    app = SimpleNamespace(**{name: lambda *args: None for name in (
        'setApplicationName', 'setApplicationDisplayName', 'setQuitOnLastWindowClosed',
        'setWindowIcon', 'setFont')})
    app.quit = lambda: events.append('application.quit')
    def execute():
        captures['lifecycle'].requestClose()
        return 0
    app.exec = execute
    monkeypatch.setattr(desktop_qt, 'QGuiApplication', lambda *args: app)
    context = SimpleNamespace(setContextProperty=lambda name, value: captures.update({name: value}))
    window = Window(events)
    engine = SimpleNamespace(rootContext=lambda: context, load=lambda path: None,
                             rootObjects=lambda: [window] if window_ready else [])
    monkeypatch.setattr(desktop_qt, 'QQmlApplicationEngine', lambda: engine)
    return events, captures


def test_qml_load_failure_closes_controller_and_retained_shell(monkeypatch, tmp_path, application):
    events, captures = _primary_qt_fakes(monkeypatch, tmp_path, window_ready=False)
    with pytest.raises(RuntimeError, match='앱 화면 로드 실패'):
        desktop_qt.main(['--root', str(tmp_path)])
    assert events == ['controller.close', 'shell.close']
    assert captures['bridge']._closed


def test_main_does_not_repeat_successful_lifecycle_cleanup(monkeypatch, tmp_path, application):
    events, captures = _primary_qt_fakes(monkeypatch, tmp_path, window_ready=True)
    assert desktop_qt.main(['--root', str(tmp_path)]) == 0
    assert events == ['shell.close', 'controller.close', 'window.close', 'application.quit']
    assert captures['lifecycle'].allowClose


def test_qt_log_handler_restored_after_qml_failure(monkeypatch, tmp_path, application):
    events, _ = _primary_qt_fakes(monkeypatch, tmp_path, window_ready=False)
    previous = lambda *args: None
    installed = []
    monkeypatch.setattr(desktop_qt, 'qInstallMessageHandler', lambda handler: installed.append(handler) or previous)
    with pytest.raises(RuntimeError, match='앱 화면 로드 실패'):
        desktop_qt.main(['--root', str(tmp_path)])
    assert installed == [desktop_qt._qt_message, previous]
    assert events == ['controller.close', 'shell.close']


def test_qt_log_handler_restored_even_when_shell_cleanup_fails(monkeypatch, tmp_path):
    _main_fakes(monkeypatch, tmp_path, primary=True)
    previous = lambda *args: None
    installed = []
    shell = SimpleNamespace(start=lambda: True, close=lambda: (_ for _ in ()).throw(RuntimeError('retained shell')))
    monkeypatch.setitem(sys.modules, 'quest3d.desktop_shell', SimpleNamespace(ShellIntegration=lambda *args: shell))
    monkeypatch.setattr(desktop_qt, 'qInstallMessageHandler', lambda handler: installed.append(handler) or previous)
    monkeypatch.setattr(desktop_qt, 'QGuiApplication', lambda *args: (_ for _ in ()).throw(RuntimeError('Qt unavailable')))
    with pytest.raises(RuntimeError, match='retained shell'):
        desktop_qt.main(['--root', str(tmp_path)])
    assert installed == [desktop_qt._qt_message, previous]


def test_secondary_instance_does_not_replace_qt_log_handler(monkeypatch, tmp_path):
    _main_fakes(monkeypatch, tmp_path, primary=False)
    monkeypatch.setattr(desktop_qt, 'qInstallMessageHandler', lambda *args: pytest.fail('A secondary instance changed Qt logging'))
    assert desktop_qt.main(['--root', str(tmp_path)]) == 0


def test_repeated_tray_open_and_hide_does_not_recreate_or_close_services(rig):
    for _ in range(100):
        rig.adapter.requestHide()
        assert rig.window.hidden
        rig.lifecycle.shellEvent('show')
        assert not rig.window.hidden
    assert rig.events == [] and rig.controller.calls == []
    assert not rig.adapter._closed

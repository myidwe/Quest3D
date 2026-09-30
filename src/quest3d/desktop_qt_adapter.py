"""Qt/QML adapter for the existing serialized desktop controller.

No process, capture, model, network or filesystem ownership lives in this class.
Commands only enqueue work; the following controller snapshot is authoritative.
"""
from __future__ import annotations

import copy
import math

from PySide6.QtCore import QObject, Property, QThread, QTimer, Qt, Signal, Slot


_DEPTH_MODEL_IDS = frozenset(('depth_anything_v2_small', 'distill_any_depth_small'))
_AI_QUALITY_IDS = frozenset(('standard', 'quality'))


def _available_audio_outputs(snapshot: dict) -> set[str]:
    rows = snapshot.get('audio_outputs')
    if not isinstance(rows, list):
        return set()
    return {row['id'] for row in rows if isinstance(row, dict)
            and isinstance(row.get('id'), str) and row['id'] in ('pc', 'quest', 'both')
            and row.get('available') is True}


def _available_ai_qualities(snapshot: dict) -> set[str]:
    rows = snapshot.get('ai_qualities')
    if not isinstance(rows, list):
        return set()
    return {row['id'] for row in rows if isinstance(row, dict)
            and isinstance(row.get('id'), str) and row['id'] in _AI_QUALITY_IDS}


def _available_depth_models(snapshot: dict) -> set[str]:
    rows = snapshot.get('depth_models')
    if not isinstance(rows, list):
        return set()
    return {row['id'] for row in rows if isinstance(row, dict)
            and isinstance(row.get('id'), str) and row['id'] in _DEPTH_MODEL_IDS
            and row.get('available') is True}


def availability(snapshot: dict, queued: bool = False) -> dict:
    idle = not snapshot.get('busy', False) and not queued
    phase = snapshot.get('phase')
    running = bool(snapshot.get('running'))
    host = bool(snapshot.get('host_running'))
    ready = bool(snapshot.get('ready', running))
    stopped = not running and not host
    resume_host = phase == 'running' and running and ready and not host and not snapshot.get('error')
    start = idle and ((phase in ('idle', 'error') and stopped) or resume_host)
    stop = idle and phase in ('idle', 'running', 'error', 'conflict') and not stopped
    return dict(
        canStart=start, canStop=stop,
        canControl=idle and phase in ('idle', 'running', 'error') and running and ready,
        canSelectMonitor=idle and phase in ('idle', 'running', 'error') and stopped,
        canSelectOutputProfile=idle and phase in ('idle', 'running', 'error') and stopped,
        canSelectAiQuality=(idle and phase in ('idle', 'error') and stopped
                            and bool(_available_ai_qualities(snapshot))),
        canSelectAudioOutput=(idle and phase in ('idle', 'error') and stopped
                              and bool(_available_audio_outputs(snapshot))),
        canSelectDepthModel=(idle and not snapshot.get('depth_model_switching', False)
            and bool(_available_depth_models(snapshot))
            and ((phase in ('idle', 'error') and stopped)
                 or (phase == 'running' and running and bool(snapshot.get('ready'))))),
        canPair=idle and host and bool(snapshot.get('pairing_available')),
        canUtility=idle, canOpenDiagnostics=idle and bool(snapshot.get('last_diagnostics')),
        primaryAction='start' if start or (not running and not host) or resume_host else 'stop',
    )


def _number(value, minimum, maximum) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and minimum <= value <= maximum


class DesktopQtAdapter(QObject):
    """Construct and use on the Qt GUI thread. Shell callbacks use postShellEvent.

    QML example: adapter.state.mode / adapter.setMode("3d"). A false command
    result means nothing was queued. An accepted PIN is never retained here;
    the QML field must clear itself when pair() returns true.
    """

    snapshotChanged = Signal()
    noticeChanged = Signal()
    uiEvent = Signal(str)
    _shellEvent = Signal(str)
    POLL_MS = 250

    def __init__(self, controller, parent=None, *, poll=True):
        super().__init__(parent)
        self._controller = controller
        self._snapshot = {}
        self._state = {}
        self._queued = False
        self._closed = False
        self._notice = ''
        self._notice_kind = 'info'
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(self.POLL_MS)
        self._poll_timer.timeout.connect(self.poll)
        self._notice_timer = QTimer(self)
        self._notice_timer.setSingleShot(True)
        self._notice_timer.setInterval(4000)
        self._notice_timer.timeout.connect(self.clearNotice)
        self._shellEvent.connect(self._deliver_shell_event, Qt.ConnectionType.QueuedConnection)
        self.poll()
        if poll:
            self._poll_timer.start()

    @Property('QVariantMap', notify=snapshotChanged)
    def state(self):
        return copy.deepcopy(self._state)

    @Property('QVariantMap', notify=snapshotChanged)
    def snapshot(self):
        return copy.deepcopy(self._state)

    @Property(str, notify=noticeChanged)
    def notice(self):
        return self._notice

    @Property(str, notify=noticeChanged)
    def noticeKind(self):
        return self._notice_kind

    def _assert_gui_thread(self):
        if QThread.currentThread() != self.thread():
            raise RuntimeError('DesktopQtAdapter must run on its Qt GUI thread')

    def _publish(self):
        state = {**copy.deepcopy(self._snapshot), **availability(self._snapshot, self._queued or self._closed)}
        state['commandPending'] = self._queued
        if state != self._state:
            self._state = state
            self.snapshotChanged.emit()

    @Slot()
    def poll(self):
        self._assert_gui_thread()
        if self._closed:
            return
        try:
            snapshot = self._controller.get_snapshot()
            if not isinstance(snapshot, dict):
                raise TypeError()
        except Exception:
            # Preserve no claimed ownership or connection when state is unknown.
            snapshot = dict(phase='conflict', busy=True, running=False, ready=False,
                host_running=False, connected=None, pairing_available=False,
                message='PC 상태 확인 실패',
                error='PC 상태 확인 실패 · 잠시 후 재시도')
        self._snapshot = copy.deepcopy(snapshot)
        self._queued = False
        self._publish()

    def _set_notice(self, text, kind='info'):
        self._notice_timer.stop()
        self._notice, self._notice_kind = text, kind
        self.noticeChanged.emit()
        if text and kind != 'error':
            self._notice_timer.start()

    @Slot(str, str)
    def setNotice(self, text, kind='info'):
        """Present a lifecycle notice without changing the controller snapshot."""
        self._assert_gui_thread()
        if not self._closed:
            self._set_notice(str(text), 'error' if kind == 'error' else 'info')

    @Slot()
    def clearNotice(self):
        self._assert_gui_thread()
        self._notice_timer.stop()
        if self._notice:
            self._notice = ''
            self.noticeChanged.emit()

    def _valid_command(self, action, values):
        gate = availability(self._snapshot, self._queued or self._closed)
        if action in ('start', 'stop'):
            return not values and gate['canStart' if action == 'start' else 'canStop']
        if action in ('refresh', 'open_guide', 'open_logs', 'export_diagnostics', 'open_diagnostics'):
            return not values and gate['canOpenDiagnostics' if action == 'open_diagnostics' else 'canUtility']
        if action == 'pair':
            pin = values.get('pin')
            return (gate['canPair'] and set(values) == {'pin'} and isinstance(pin, str)
                    and len(pin) == 4 and pin.isascii() and pin.isdigit())
        if action == 'select_monitor':
            device = values.get('device_name')
            return (gate['canSelectMonitor'] and set(values) == {'device_name'} and isinstance(device, str)
                    and any(isinstance(row, dict) and row.get('device_name') == device
                            for row in self._snapshot.get('monitors', [])))
        if action == 'select_output_profile':
            profile = values.get('output_profile')
            return (gate['canSelectOutputProfile'] and set(values) == {'output_profile'}
                    and isinstance(profile, str)
                    and any(isinstance(row, dict) and row.get('id') == profile
                            for row in self._snapshot.get('output_profiles', [])))
        if action == 'select_depth_model':
            model = values.get('depth_model')
            return (gate['canSelectDepthModel'] and set(values) == {'depth_model'}
                    and isinstance(model, str) and model in _available_depth_models(self._snapshot))
        if action == 'select_ai_quality':
            profile = values.get('ai_quality')
            return (gate['canSelectAiQuality'] and set(values) == {'ai_quality'}
                    and isinstance(profile, str) and profile in _available_ai_qualities(self._snapshot))
        if action == 'select_audio_output':
            mode = values.get('audio_output')
            return (gate['canSelectAudioOutput'] and set(values) == {'audio_output'}
                    and isinstance(mode, str) and mode in _available_audio_outputs(self._snapshot))
        if action != 'control' or not gate['canControl'] or not values:
            return False
        if set(values) - {'mode', 'depth_delta', 'depth_percent', 'profile'}:
            return False
        if 'mode' in values and values['mode'] not in ('2d', '3d'):
            return False
        if 'profile' in values and values['profile'] not in ('comfort', 'linear'):
            return False
        if 'depth_delta' in values and (not _number(values['depth_delta'], -.05, .05)
                                         or values['depth_delta'] not in (-.05, .05)):
            return False
        if 'depth_percent' in values and not _number(values['depth_percent'], 0, 4):
            return False
        return not ('depth_delta' in values and 'depth_percent' in values)

    @Slot(str, 'QVariantMap', result=bool)
    def command(self, action, values):
        self._assert_gui_thread()
        if self._closed:
            return False
        if not isinstance(action, str) or not isinstance(values, dict) or not self._valid_command(action, values):
            self._set_notice('요청 적용 불가 · 상태·입력값 확인 필요', 'error')
            return False
        try:
            accepted = bool(self._controller.command(action, **values))
        except Exception:
            # Exceptions from a PIN path may contain its value. Never mirror it.
            self._set_notice('요청 전달 실패 · 재시도 필요', 'error')
            return False
        if not accepted:
            self._set_notice('작업 진행 중 · 완료 후 재시도', 'error')
            return False
        self._queued = True
        self._set_notice('적용 확인 중')
        self._publish()
        return True

    @Slot(result=bool)
    def primary(self):
        action = availability(self._snapshot, self._queued or self._closed)['primaryAction']
        return self.command(action, {})

    @Slot(result=bool)
    def stop(self):
        return self.command('stop', {})

    @Slot(str, result=bool)
    def setMode(self, value):
        return self.command('control', {'mode': value})

    @Slot(float, result=bool)
    def adjustDepth(self, delta):
        return self.command('control', {'depth_delta': delta})

    @Slot(float, result=bool)
    def setDepth(self, value):
        return self.command('control', {'depth_percent': value})

    @Slot(bool, result=bool)
    def setComfort(self, enabled):
        return self.command('control', {'profile': 'comfort' if enabled else 'linear'})

    @Slot(str, result=bool)
    def selectMonitor(self, device):
        return self.command('select_monitor', {'device_name': device})

    @Slot(str, result=bool)
    def selectOutputProfile(self, profile):
        return self.command('select_output_profile', {'output_profile': profile})

    @Slot(str, result=bool)
    def selectDepthModel(self, model):
        return self.command('select_depth_model', {'depth_model': model})

    @Slot(str, result=bool)
    def selectAiQuality(self, profile):
        return self.command('select_ai_quality', {'ai_quality': profile})

    @Slot(str, result=bool)
    def selectAudioOutput(self, mode):
        return self.command('select_audio_output', {'audio_output': mode})

    @Slot(str, result=bool)
    def pair(self, pin):
        return self.command('pair', {'pin': pin})

    @Slot(result=bool)
    def refresh(self):
        return self.command('refresh', {})

    @Slot(result=bool)
    def openGuide(self):
        return self.command('open_guide', {})

    @Slot(result=bool)
    def openLogs(self):
        return self.command('open_logs', {})

    @Slot(result=bool)
    def exportDiagnostics(self):
        return self.command('export_diagnostics', {})

    @Slot(result=bool)
    def openDiagnostics(self):
        return self.command('open_diagnostics', {})

    @Slot()
    def requestHide(self):
        self._assert_gui_thread()
        if not self._closed:
            self.uiEvent.emit('hide')

    @Slot()
    def requestExit(self):
        self._assert_gui_thread()
        if not self._closed:
            self.uiEvent.emit('exit')

    def postShellEvent(self, action: str):
        """May be called from the native Win32 tray thread; emits only a signal."""
        if action in ('show', 'stop', 'exit'):
            self._shellEvent.emit(action)

    @Slot(str)
    def _deliver_shell_event(self, action):
        self._assert_gui_thread()
        if not self._closed:
            self.uiEvent.emit(action)

    @Slot()
    def close(self):
        """Stop adapter timers only. The app owns stream shutdown and disposal."""
        self._assert_gui_thread()
        self._closed = True
        self._poll_timer.stop()
        self._notice_timer.stop()
        self._publish()

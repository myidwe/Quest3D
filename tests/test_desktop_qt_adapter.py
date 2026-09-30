"""Exercise the real Qt bridge without launching a desktop, GPU or host."""
from __future__ import annotations

import copy
import threading

import pytest

QtCore = pytest.importorskip("PySide6.QtCore")
from PySide6.QtCore import QCoreApplication, QEvent, QThread, QUrl
from PySide6.QtQml import QQmlComponent, QQmlEngine
from PySide6.QtTest import QSignalSpy

from quest3d.desktop_qt_adapter import DesktopQtAdapter, availability


class Controller:
    def __init__(self):
        self.snapshot = dict(phase="running", busy=False, running=True, ready=True,
            host_running=True, pairing_available=True, mode="3d", depth_percent=1.31,
            profile="comfort", monitors=[dict(device_name="DISPLAY1", label="Monitor 1")],
            selected_monitor="DISPLAY1", connected=True, last_diagnostics="",
            output_profile='quest2', output_profiles=[dict(id='quest2', label='Quest 2'),
                                                     dict(id='quest3', label='Quest 3')],
            ai_quality='standard', ai_qualities=[dict(id='standard',label='Standard'),dict(id='quality',label='Quality')],
            depth_model='depth_anything_v2_small', depth_models=[
                dict(id='depth_anything_v2_small', label='DAv2 Small (기본)', available=True),
                dict(id='distill_any_depth_small', label='DAD Small (비교)', available=True)])
        self.calls = []
        self.accept = True
        self.failure = None
        self.snapshot_failure = False
        self.closed = False

    def get_snapshot(self):
        if self.snapshot_failure:
            raise OSError("private controller error")
        return copy.deepcopy(self.snapshot)

    def command(self, action, **values):
        if self.failure:
            raise self.failure
        self.calls.append((action, values))
        return self.accept

    def close(self):
        self.closed = True


@pytest.fixture(scope="module")
def application():
    app = QCoreApplication.instance() or QCoreApplication([])
    yield app


@pytest.fixture
def bridge(application):
    controller = Controller()
    adapter = DesktopQtAdapter(controller, poll=False)
    yield adapter, controller
    adapter.close()
    adapter.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_snapshot_is_authoritative_and_unchanged_poll_emits_nothing(bridge):
    adapter, controller = bridge
    spy = QSignalSpy(adapter.snapshotChanged)
    adapter.poll()
    assert spy.count() == 0
    assert adapter.setMode("2d")
    assert controller.calls == [("control", {"mode": "2d"})]
    assert adapter.state["mode"] == "3d"
    assert adapter.state["commandPending"]
    assert not adapter.state["canControl"]
    controller.snapshot.update(mode="2d", busy=True)
    adapter.poll()
    assert adapter.state["mode"] == "2d"
    assert not adapter.state["canControl"]
    controller.snapshot["busy"] = False
    adapter.poll()
    assert adapter.state["canControl"]


def test_double_click_cannot_queue_duplicate_work(bridge):
    adapter, controller = bridge
    assert adapter.stop()
    assert not adapter.stop()
    assert controller.calls == [("stop", {})]


def test_depth_delta_does_not_overwrite_stale_authoritative_value(bridge):
    adapter, controller = bridge
    controller.snapshot["depth_percent"] = 1.46
    assert adapter.adjustDepth(.05)
    assert controller.calls == [("control", {"depth_delta": .05})]
    assert adapter.state["depth_percent"] == 1.31


@pytest.mark.parametrize("value", [-.01, 4.001, float("nan"), float("inf"), True, "1.31"])
def test_bad_absolute_depth_never_reaches_controller(bridge, value):
    adapter, controller = bridge
    assert not adapter.command("control", {"depth_percent": value})
    assert not controller.calls


@pytest.mark.parametrize("value", [0, .1, -.1, float("nan")])
def test_only_supported_fine_depth_steps_are_sent(bridge, value):
    adapter, controller = bridge
    assert not adapter.adjustDepth(value)
    assert not controller.calls


@pytest.mark.parametrize("value", [0, 1.3125, 4])
def test_absolute_depth_keeps_precision_and_inclusive_bounds(bridge, value):
    adapter, controller = bridge
    assert adapter.setDepth(value)
    assert controller.calls == [("control", {"depth_percent": value})]


@pytest.mark.parametrize("values", [{"mode": "stereo"}, {"profile": "aggressive"},
    {"depth_delta": .05, "depth_percent": 1.31}, {"shutdown": True}, {}])
def test_unknown_or_ambiguous_controls_never_reach_controller(bridge, values):
    adapter, controller = bridge
    assert not adapter.command("control", values)
    assert not controller.calls


def test_pairing_keeps_leading_zero_but_retains_no_pin(bridge):
    adapter, controller = bridge
    assert adapter.pair("0123")
    assert controller.calls == [("pair", {"pin": "0123"})]
    assert "0123" not in repr(adapter.state)
    assert "0123" not in adapter.notice
    assert "0123" not in repr(vars(adapter) | {"_controller": None})


@pytest.mark.parametrize("pin", ["１２３４", "123", "12345", "12 4", 1234, "1e03"])
def test_invalid_pin_never_reaches_controller(bridge, pin):
    adapter, controller = bridge
    assert not adapter.command("pair", {"pin": pin})
    assert not controller.calls


def test_pair_exceptions_never_surface_sensitive_values(bridge):
    adapter, controller = bridge
    controller.failure = RuntimeError("failed PIN 4321 private-host-password")
    assert not adapter.pair("4321")
    assert "4321" not in adapter.notice
    assert "private-host-password" not in adapter.notice
    assert not adapter.state["commandPending"]


def test_snapshot_copies_do_not_expose_mutable_backend_or_adapter_data(bridge):
    adapter, controller = bridge
    exposed = adapter.state
    exposed["monitors"][0]["device_name"] = "MODIFIED"
    exposed["mode"] = "2d"
    assert adapter.snapshot["monitors"][0]["device_name"] == "DISPLAY1"
    assert controller.snapshot["monitors"][0]["device_name"] == "DISPLAY1"
    assert adapter.state["mode"] == "3d"


def test_monitor_selection_requires_stopped_pc_and_known_device(bridge):
    adapter, controller = bridge
    assert not adapter.selectMonitor("DISPLAY1")
    controller.snapshot.update(phase="idle", running=False, ready=False, host_running=False,
                               pairing_available=False)
    adapter.poll()
    assert not adapter.selectMonitor("untrusted device")
    assert adapter.selectMonitor("DISPLAY1")
    assert controller.calls == [("select_monitor", {"device_name": "DISPLAY1"})]


def test_headset_selection_requires_stopped_pc_and_authoritative_profile(bridge):
    adapter, controller = bridge
    assert not adapter.selectOutputProfile('quest3')
    controller.snapshot.update(phase='idle', running=False, ready=False, host_running=False)
    adapter.poll()
    assert not adapter.selectOutputProfile('quest4')
    assert adapter.selectOutputProfile('quest3')
    assert controller.calls == [('select_output_profile', {'output_profile': 'quest3'})]
    assert adapter.state['output_profile'] == 'quest2'
    assert not adapter.state['canSelectOutputProfile']
    assert not adapter.selectOutputProfile('quest2')
    controller.snapshot['output_profile'] = 'quest3'
    adapter.poll()
    assert adapter.state['output_profile'] == 'quest3'
    assert adapter.selectOutputProfile('quest2')
    assert controller.calls[-1] == ('select_output_profile', {'output_profile': 'quest2'})


@pytest.mark.parametrize('state,allowed', [
    (dict(phase='idle', running=False, ready=False, host_running=False), True),
    (dict(phase='error', running=False, ready=False, host_running=False), True),
    (dict(phase='running', running=True, ready=True), True),
    (dict(phase='running', running=True, ready=True, host_running=False), True),
    (dict(phase='running', running=True, ready=False), False),
    (dict(phase='running', running=True, ready=None), False),
    (dict(phase='running', running=False, ready=True, host_running=False), False),
    (dict(phase='error', running=True, ready=True), False),
    (dict(phase='idle', running=False, ready=False, host_running=True), False),
    (dict(phase='conflict'), False), (dict(phase='starting'), False),
    (dict(phase='stopping'), False), (dict(phase='unknown'), False),
    (dict(busy=True), False), (dict(depth_model_switching=True), False),
])
def test_depth_model_selection_state_gate(bridge, state, allowed):
    adapter, controller = bridge
    controller.snapshot.update(state)
    adapter.poll()
    assert adapter.state['canSelectDepthModel'] is allowed
    assert adapter.selectDepthModel('distill_any_depth_small') is allowed
    assert controller.calls == ([('select_depth_model', {'depth_model': 'distill_any_depth_small'})]
                                if allowed else [])


@pytest.mark.parametrize('values', [{}, {'depth_model': None}, {'depth_model': 1},
    {'depth_model': True}, {'depth_model': []}, {'depth_model': 'unknown'},
    {'depth_model': 'distill_any_depth_small', 'mode': '3d'}])
def test_malformed_depth_model_payload_never_reaches_controller(bridge, values):
    adapter, controller = bridge
    assert not adapter.command('select_depth_model', values)
    assert not controller.calls


@pytest.mark.parametrize('catalog', [None, {}, 'distill_any_depth_small', [], [None],
    [dict(id='distill_any_depth_small', available=False)],
    [dict(id='distill_any_depth_small', available='true')],
    [dict(id='distill_any_depth_small', available=1)],
    [dict(id=['distill_any_depth_small'], available=True)],
    [dict(id='unknown', available=True)]])
def test_missing_or_malformed_depth_model_catalog_disables_selection(bridge, catalog):
    adapter, controller = bridge
    controller.snapshot['depth_models'] = catalog
    adapter.poll()
    assert not adapter.state['canSelectDepthModel']
    assert not adapter.selectDepthModel('distill_any_depth_small')
    assert not controller.calls


def test_missing_dad_blocks_only_that_entry(bridge):
    adapter, controller = bridge
    controller.snapshot['depth_models'][1]['available'] = False
    adapter.poll()
    assert adapter.state['canSelectDepthModel']
    assert not adapter.selectDepthModel('distill_any_depth_small')
    assert adapter.selectDepthModel('depth_anything_v2_small')
    assert controller.calls == [('select_depth_model', {'depth_model': 'depth_anything_v2_small'})]


def test_depth_model_selection_waits_for_authoritative_snapshot_and_blocks_duplicates(bridge):
    adapter, controller = bridge
    assert adapter.selectDepthModel('distill_any_depth_small')
    assert adapter.state['depth_model'] == 'depth_anything_v2_small'
    assert adapter.state['commandPending']
    assert not adapter.state['canSelectDepthModel']
    assert not adapter.selectDepthModel('distill_any_depth_small')
    controller.snapshot['depth_model_switching'] = True
    adapter.poll()
    assert adapter.state['depth_model'] == 'depth_anything_v2_small'
    assert not adapter.state['canSelectDepthModel']
    assert not adapter.selectDepthModel('depth_anything_v2_small')
    controller.snapshot.update(depth_model='distill_any_depth_small', depth_model_switching=False)
    adapter.poll()
    assert adapter.state['depth_model'] == 'distill_any_depth_small'
    assert adapter.state['canSelectDepthModel']
    assert adapter.selectDepthModel('depth_anything_v2_small')
    assert controller.calls == [
        ('select_depth_model', {'depth_model': 'distill_any_depth_small'}),
        ('select_depth_model', {'depth_model': 'depth_anything_v2_small'})]


def test_rejected_depth_model_and_closed_adapter_preserve_selection(bridge):
    adapter, controller = bridge
    controller.accept = False
    assert not adapter.selectDepthModel('distill_any_depth_small')
    assert adapter.state['depth_model'] == 'depth_anything_v2_small'
    assert not adapter.state['commandPending']
    adapter.close()
    assert not adapter.state['canSelectDepthModel']
    assert not adapter.selectDepthModel('distill_any_depth_small')
    assert len(controller.calls) == 1


def test_owned_partial_start_allows_stop_but_not_controls_or_new_start(bridge):
    adapter, controller = bridge
    controller.snapshot.update(phase="error", host_running=False, ready=False,
                               pairing_available=False, error="producer not ready")
    adapter.poll()
    assert not adapter.state["canStart"]
    assert not adapter.state["canControl"]
    assert not adapter.state["canPair"]
    assert adapter.stop()
    assert controller.calls == [("stop", {})]


def test_ready_producer_can_start_missing_host(bridge):
    adapter, controller = bridge
    controller.snapshot.update(host_running=False, pairing_available=False)
    adapter.poll()
    assert adapter.state["canStart"]
    assert adapter.state["canStop"]
    assert adapter.primary()
    assert controller.calls == [("start", {})]


def test_unknown_snapshot_does_not_claim_connection_or_allow_mutations(bridge):
    adapter, controller = bridge
    controller.snapshot_failure = True
    adapter.poll()
    assert adapter.state["connected"] is None
    assert not any(adapter.state[key] for key in (
        "canStart", "canStop", "canControl", "canSelectMonitor", "canSelectOutputProfile", "canPair"))
    assert "private controller error" not in adapter.state["error"]
    assert not adapter.stop()
    controller.snapshot_failure = False
    adapter.poll()
    assert adapter.state["canControl"]


@pytest.mark.parametrize("phase", ["starting", "stopping", "unexpected", None])
def test_transition_or_unknown_phase_blocks_stream_controls(phase):
    gates = availability(dict(phase=phase, running=True, ready=True, host_running=True))
    assert not any(gates[key] for key in ("canStart", "canStop", "canControl", "canSelectMonitor", "canSelectOutputProfile"))


def test_close_disables_actions_and_timers_without_disposing_controller(bridge):
    adapter, controller = bridge
    assert adapter.setComfort(False)
    adapter.close()
    assert not adapter._poll_timer.isActive()
    assert not adapter._notice_timer.isActive()
    assert not adapter.stop()
    assert not adapter._notice_timer.isActive()
    assert not controller.closed
    assert controller.calls == [("control", {"profile": "linear"})]


def test_rejected_controller_request_does_not_set_pending(bridge):
    adapter, controller = bridge
    controller.accept = False
    assert not adapter.setMode("2d")
    assert not adapter.state["commandPending"]
    assert adapter.state["mode"] == "3d"
    assert adapter.noticeKind == "error"


def test_native_shell_event_is_delivered_on_qt_thread(bridge, application):
    adapter, controller = bridge
    deliveries = []
    adapter.uiEvent.connect(lambda action: deliveries.append((action, QThread.currentThread())))
    worker = threading.Thread(target=lambda: adapter.postShellEvent("show"))
    worker.start()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert deliveries == []
    application.processEvents()
    assert deliveries == [("show", adapter.thread())]
    adapter.close()
    adapter.postShellEvent("exit")
    application.processEvents()
    assert len(deliveries) == 1


def test_qml_can_bind_state_and_call_typed_slots(bridge, application):
    adapter, controller = bridge
    engine = QQmlEngine()
    engine.rootContext().setContextProperty("desktop", adapter)
    component = QQmlComponent(engine)
    component.setData(b'''import QtQml
QtObject {
    property string displayedMode: desktop.state.mode
    property bool controlsEnabled: desktop.state.canControl
    function chooseFlat() { return desktop.setMode("2d") }
}''', QUrl())
    assert component.isReady(), [error.toString() for error in component.errors()]
    obj = component.create()
    assert obj is not None
    assert obj.property("displayedMode") == "3d"
    assert obj.property("controlsEnabled")
    assert obj.chooseFlat()
    assert controller.calls == [("control", {"mode": "2d"})]
    assert not obj.property("controlsEnabled")
    controller.snapshot["mode"] = "2d"
    adapter.poll()
    assert obj.property("displayedMode") == "2d"
    obj.deleteLater()
    engine.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

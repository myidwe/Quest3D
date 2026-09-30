"""Actual Qt adapter gates and acknowledgement; no processes or capture."""
import pytest

from test_desktop_qt_adapter import application, bridge


@pytest.mark.parametrize('phase,running,host,busy,allowed', [
    ('idle',False,False,False,True), ('error',False,False,False,True),
    ('running',True,True,False,False), ('running',True,False,False,False),
    ('error',False,True,False,False), ('idle',False,False,True,False),
    ('conflict',False,False,False,False), ('checking',False,False,False,False),
    ('starting',False,False,False,False), ('stopping',False,False,False,False)])
def test_quality_lifecycle_gate(bridge, phase, running, host, busy, allowed):
    adapter, controller = bridge
    controller.snapshot.update(phase=phase,running=running,host_running=host,busy=busy)
    adapter.poll()
    assert adapter.state['canSelectAiQuality'] is allowed
    assert adapter.selectAiQuality('quality') is allowed
    assert controller.calls == ([('select_ai_quality', {'ai_quality':'quality'})] if allowed else [])


@pytest.mark.parametrize('values', [dict(ai_quality=None),dict(ai_quality=True),dict(ai_quality=322),
    dict(ai_quality='unknown'),dict(ai_quality='quality',ai_size=322),{},dict(ai_quality=[])])
def test_quality_argument_schema_is_exact(bridge, values):
    adapter, controller = bridge
    controller.snapshot.update(phase='idle',running=False,host_running=False)
    adapter.poll()
    assert not adapter.command('select_ai_quality', values)
    assert not controller.calls


def test_quality_waits_for_authoritative_state_and_rejects_duplicate(bridge):
    adapter, controller = bridge
    controller.snapshot.update(phase='idle',running=False,host_running=False)
    adapter.poll()
    assert adapter.selectAiQuality('quality')
    assert adapter.state['ai_quality'] == 'standard'
    assert not adapter.state['canSelectAiQuality']
    assert not adapter.selectAiQuality('quality')
    controller.snapshot['ai_quality'] = 'quality'
    adapter.poll()
    assert adapter.state['ai_quality'] == 'quality'
    assert adapter.state['canSelectAiQuality']


def test_backend_rejection_keeps_quality_selection(bridge):
    adapter, controller = bridge
    controller.snapshot.update(phase='idle',running=False,host_running=False)
    controller.accept = False
    adapter.poll()
    assert not adapter.selectAiQuality('quality')
    assert adapter.state['ai_quality'] == 'standard'
    assert not adapter.state['commandPending']


@pytest.mark.parametrize('rows', [None, {}, 'quality', [], [None], [{'id':[]}], [{'id':'unknown'}]])
def test_unknown_choice_not_exposed_by_old_snapshot(bridge, rows):
    adapter, controller = bridge
    controller.snapshot.update(phase='idle',running=False,host_running=False,ai_qualities=rows)
    adapter.poll()
    assert not adapter.state['canSelectAiQuality']
    assert not adapter.selectAiQuality('quality')
    assert not controller.calls

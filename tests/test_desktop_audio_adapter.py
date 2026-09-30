"""System output command gate and acknowledgement on the actual Qt adapter."""
import pytest
from test_desktop_qt_adapter import application, bridge


@pytest.mark.parametrize('running,host,busy,phase,allowed', [
    (False,False,False,'idle',True), (True,True,False,'running',False),
    (False,True,False,'error',False), (True,False,False,'running',False),
    (False,False,True,'idle',False), (False,False,False,'conflict',False)])
def test_sound_gate_and_acknowledgement(bridge, running, host, busy, phase, allowed):
    adapter, controller = bridge
    controller.snapshot.update(running=running,host_running=host,busy=busy,phase=phase,
        audio_output='pc',audio_outputs=[dict(id='pc',available=True),dict(id='quest',available=True)])
    adapter.poll()
    assert adapter.selectAudioOutput('quest') is allowed
    assert adapter.state['audio_output'] == 'pc'
    if allowed:
        assert not adapter.selectAudioOutput('quest')
        controller.snapshot['audio_output'] = 'quest'
        adapter.poll()
        assert adapter.state['audio_output'] == 'quest'


@pytest.mark.parametrize('values', [dict(audio_output='quest'),dict(audio_output=[]),
    dict(audio_output='invalid'),dict(audio_output='pc',endpoint='injected')])
def test_sound_unavailable_and_unexpected_arguments_rejected(bridge, values):
    adapter, controller = bridge
    controller.snapshot.update(phase='idle',running=False,host_running=False,
        audio_outputs=[dict(id='pc',available=True),dict(id='quest',available=False)])
    adapter.poll()
    assert not adapter.command('select_audio_output', values)
    assert not controller.calls

"""Output policy faults and lifecycle integration without changing any device."""
import copy
import json
from types import SimpleNamespace

import pytest

from quest3d import audio_output as audio
from quest3d import desktop_backend as backend
from test_desktop_backend import rig, write_json


PC = '{0.0.0.00000000}.{11111111-1111-1111-1111-111111111111}'
VIRTUAL = '{0.0.0.00000000}.{22222222-2222-2222-2222-222222222222}'


def inventory():
    return dict(defaults_before={role: {'id': PC} for role in ('console', 'multimedia', 'communications')},
        active_render_endpoints=[dict(id=PC, name='Speakers', muted=False, volume_scalar=.7),
                                dict(id=VIRTUAL, name='스피커(Steam Streaming Speakers)', muted=False, volume_scalar=1)])


def test_default_migration_preserves_existing_quality_and_pc_needs_no_driver(monkeypatch):
    original = {**backend.DEFAULTS, 'depth_percent':1.9, 'ai_quality':'quality', 'output_profile':'quest3'}
    original.pop('audio_output')
    assert backend.validated_preferences(original) == {**original, 'audio_output':'pc'}
    monkeypatch.setattr(audio, 'inventory', lambda *_: pytest.fail('Default must not inspect Windows'))
    assert audio.plan('pc') == dict(audio_output='pc', audio_enabled=False, audio_endpoint=None)


@pytest.mark.parametrize('value', [None, True, 1, [], {}, 'Quest', 'invalid'])
def test_invalid_setting_preserved(tmp_path, value):
    path = tmp_path/'config/desktop.json'
    write_json(path, {**backend.DEFAULTS, 'audio_output': value})
    before = path.read_bytes()
    controller = backend.DesktopController(tmp_path, start_worker=False)
    with pytest.raises(RuntimeError):
        controller._select_audio_output('pc')
    assert path.read_bytes() == before


def test_shared_output_never_forces_a_device_and_quest_selects_speakers():
    data = inventory()
    original = copy.deepcopy(data)
    assert audio.plan('both', data)['audio_endpoint'] is None
    assert audio.plan('quest', data)['audio_endpoint'] == VIRTUAL
    assert data == original


@pytest.mark.parametrize('fault', ['missing', 'microphone', 'duplicate', 'muted', 'zero', 'nan',
    'bad_id', 'different_roles', 'already_virtual', 'missing_default'])
def test_unavailable_virtual_route_refused_without_mutation(fault):
    data = inventory()
    target = data['active_render_endpoints'][1]
    if fault == 'missing': data['active_render_endpoints'].pop()
    elif fault == 'microphone': target['name'] = '스피커(Steam Streaming Microphone)'
    elif fault == 'duplicate': data['active_render_endpoints'].append(dict(target))
    elif fault == 'muted': target['muted'] = True
    elif fault == 'zero': target['volume_scalar'] = 0
    elif fault == 'nan': target['volume_scalar'] = float('nan')
    elif fault == 'bad_id': target['id'] = VIRTUAL + '\nstream_audio=disabled'
    elif fault == 'different_roles': data['defaults_before']['multimedia']['id'] = VIRTUAL
    elif fault == 'already_virtual':
        for role in data['defaults_before'].values(): role['id'] = VIRTUAL
    else: data['defaults_before'].pop('console')
    with pytest.raises(ValueError): audio.plan('quest', data)


def test_missing_probe_keeps_default_selectable(tmp_path):
    rows = audio.options(tmp_path)
    assert rows[0]['available'] and not rows[1]['available'] and not rows[2]['available']
    assert rows[1]['reason']


def test_output_roundtrip_preserves_all_video_preferences(rig, monkeypatch):
    rig.state.live.clear()
    monkeypatch.setattr(audio, 'inventory', lambda *_: inventory())
    original = dict(rig.c.preferences)
    for mode in ('quest', 'both', 'pc'):
        rig.c._select_audio_output(mode)
        assert rig.c.preferences == {**original, 'audio_output':mode}
        assert json.loads(rig.c.settings_path.read_text())['audio_output'] == mode
        assert rig.c.get_snapshot()['audio_output'] == mode


@pytest.mark.parametrize('live', ['both', 'producer', 'host'])
def test_streaming_output_change_refused(rig, live):
    if live == 'producer': rig.state.live.pop(rig.state.host['pid'])
    if live == 'host': rig.state.live.pop(rig.state.producer['pid'])
    with pytest.raises(RuntimeError, match='송출 중지'):
        rig.c._select_audio_output('quest')
    assert rig.c.preferences['audio_output'] == 'pc'


def test_unavailable_and_disk_failure_keep_saved_choice(rig, monkeypatch):
    rig.state.live.clear()
    with pytest.raises(ValueError): rig.c._select_audio_output('quest')
    assert rig.c.preferences['audio_output'] == 'pc'
    monkeypatch.setattr(audio, 'inventory', lambda *_: inventory())
    rig.c._inspect()
    monkeypatch.setattr(rig.c, '_inspect', lambda: None)
    monkeypatch.setattr(rig.c, '_save_preferences', lambda: (_ for _ in ()).throw(OSError('disk full')))
    with pytest.raises(OSError): rig.c._select_audio_output('quest')
    assert rig.c.preferences['audio_output'] == rig.c.get_snapshot()['audio_output'] == 'pc'


def test_active_audio_uses_actual_host_record_not_new_preference(rig):
    rig.c.preferences['audio_output'] = 'quest'
    rig.c._inspect()
    assert rig.c.get_snapshot()['active_audio_output'] == 'pc'
    assert rig.c.get_snapshot()['audio_output'] == 'quest'
    path = rig.c.root/'artifacts/host/dev/process.json'
    write_json(path, {**json.loads(path.read_text()), 'audio_output':'both'})
    rig.c._inspect()
    assert rig.c.get_snapshot()['active_audio_output'] == 'both'


def test_recovery_rejects_foreign_directory(tmp_path):
    runtime = tmp_path/'artifacts/host/runtime-test'
    runtime.mkdir(parents=True)
    foreign = tmp_path/'audio-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
    foreign.mkdir()
    with pytest.raises(ValueError, match='경로'):
        audio.recover_stopped_host(tmp_path, runtime, {'audio_directory':str(foreign)})


def test_recovery_passes_exact_owner_and_surfaces_pending(tmp_path, monkeypatch):
    from quest3d import audio_recovery as recovery
    runtime = tmp_path/'artifacts/host/runtime-test'
    directory = runtime/('audio-' + 'a'*32)
    directory.mkdir(parents=True)
    (directory/'route.json').write_text('{}')
    record = dict(audio_directory=str(directory), process_id=23, owner_creation_filetime='777')
    fake = SimpleNamespace(__enter__=None)
    class Backend:
        def __init__(self, **kwargs): assert kwargs == {'allow_changes':True}
        def __enter__(self): return fake
        def __exit__(self, *_): pass
    calls = []
    outcome = {'status':'already_restored'}
    def recover(path, backend, **kwargs):
        calls.append(kwargs)
        return outcome
    monkeypatch.setattr(recovery, 'WindowsAudioBackend', Backend)
    monkeypatch.setattr(recovery, 'recover_journal', recover)
    assert audio.recover_stopped_host(tmp_path, runtime, record) == outcome
    assert calls[-1]['expected_owner'] == (23, '777') and calls[-1]['apply'] is True
    outcome['status'] = 'restore_pending'
    with pytest.raises(ValueError, match='복구 확인'):
        audio.recover_stopped_host(tmp_path, runtime, record)
    attempts = iter([{'status':'refused','error':'Journal is locked or inaccessible'}, {'status':'already_restored'}])
    monkeypatch.setattr(recovery, 'recover_journal', lambda *a, **k: next(attempts))
    waits=[]
    monkeypatch.setattr(audio.time, 'sleep', waits.append)
    assert audio.recover_stopped_host(tmp_path, runtime, record)['status'] == 'already_restored'
    assert waits == [.15]


def test_audio_error_is_visible_without_claiming_video_failure_and_resets_on_reconnect(rig):
    path = rig.c.root/'artifacts/host/dev/process.json'
    write_json(path, {**json.loads(path.read_text()), 'audio_output':'quest', 'started_at':'2026-09-30T03:00:00.000+09:00'})
    log = rig.c.root/'artifacts/host/dev/sunshine.log'
    log.write_text('[2026-09-30 02:59:00.000]: Error: Unable to initialize audio capture.\n')
    rig.c._inspect()
    assert not rig.c.get_snapshot()['audio_error'], 'Previous host errors must be ignored'
    log.write_text('[2026-09-30 03:00:01.000]: Info: CLIENT CONNECTED\n'
                   '[2026-09-30 03:00:02.000]: Error: Unable to initialize audio capture.\n')
    rig.c._inspect()
    assert rig.c.get_snapshot()['audio_error'] and rig.c.get_snapshot()['ready']
    log.write_text('[2026-09-30 03:00:03.000]: Info: CLIENT DISCONNECTED\n')
    rig.c._inspect()
    assert not rig.c.get_snapshot()['audio_error']
    log.write_text('[2026-09-30 03:00:04.000]: Info: CLIENT CONNECTED\n'
                   '[2026-09-30 03:00:05.000]: Info: Opus initialized: 48 kHz\n')
    rig.c._inspect()
    assert rig.c._audio_capture_state == 'ready' and not rig.c.get_snapshot()['audio_error']


@pytest.mark.parametrize('mode', ['pc','quest','both'])
def test_selected_output_reaches_both_rebind_and_launch_without_changing_producer(rig, monkeypatch, mode):
    old_producer = dict(rig.state.producer)
    rig.state.live.pop(202)
    rig.c.preferences['audio_output'] = mode
    monkeypatch.setattr(audio, 'inventory', lambda *_: inventory())
    monkeypatch.setattr(rig.c, '_preflight', lambda: rig.monitor)
    calls=[]
    def run(argv,label,**kwargs):
        calls.append((list(map(str,argv)),label))
        if label == 'host-start': rig.state.live[202] = rig.state.host
        return rig.c._last_log_dir/(label+'.log')
    monkeypatch.setattr(rig.c,'_run',run)
    rig.c._start()
    assert [label for _,label in calls] == ['host-check','host-bind','host-start']
    for argv,_ in calls:
        if mode == 'pc': assert '-AudioOutput' not in argv
        else: assert argv[argv.index('-AudioOutput')+1] == mode
    assert rig.c._producer == old_producer

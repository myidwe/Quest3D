"""AI-size policy, persistence and actual metadata; temporary files, no GPU/host."""
import copy
import json

import pytest

from quest3d import desktop_backend as backend
from test_desktop_backend import rig, write_json


def test_old_settings_preserve_all_values_and_new_default():
    old = {**backend.DEFAULTS, 'depth_model': backend.DAD_DEPTH_MODEL,
           'depth_percent': 1.85, 'profile': 'comfort', 'output_profile': 'quest3'}
    old.pop('ai_quality')
    assert backend.validated_preferences(old) == {**old, 'ai_quality': 'standard'}
    assert 'ai_quality' not in old
    assert backend.ai_quality_status('standard')['ai_input_text'] == '다음 시작 · 빠른 처리'
    assert backend.ai_quality_status('quality')['ai_input_text'] == '다음 시작 · 세부 분석'
    assert next(row['label'] for row in backend.ai_quality_options() if row['id'] == 'quality') == 'Quality · Preview'


@pytest.mark.parametrize('value', [None, 322, True, [], {}, 'Quality', 'ultra'])
def test_invalid_quality_rejected_without_rewriting_existing_file(tmp_path, value):
    original = {**backend.DEFAULTS, 'ai_quality': value, 'depth_percent': 1.85}
    target = tmp_path/'config/desktop.json'
    write_json(target, original)
    before = target.read_bytes()
    controller = backend.DesktopController(tmp_path, start_worker=False)
    try:
        assert controller._prefs_error
        with pytest.raises(RuntimeError):
            controller._select_ai_quality('quality')
        assert target.read_bytes() == before
    finally:
        controller.close()


@pytest.mark.parametrize('quality,size', [('standard', 280), ('quality', 322)])
@pytest.mark.parametrize('headset,dimensions', [('quest2', (1920,1080)), ('quest3', (2048,1152))])
def test_profile_launch_changes_only_ai_size_and_preserves_user_view(rig, quality, size, headset, dimensions):
    c = rig.c
    c.preferences.update(ai_quality=quality, depth_model=backend.DAD_DEPTH_MODEL,
                         depth_percent=1.85, profile='comfort', output_profile=headset, mode='3d')
    arguments = c._producer_argv(rig.monitor, rig.directory, hdr=False)
    get = lambda key: arguments[arguments.index(key)+1]
    assert int(get('--ai-size')) == size
    assert (int(get('--eye-width')), int(get('--eye-height'))) == dimensions
    assert float(get('--disparity')) / dimensions[0] * 100 == pytest.approx(1.85)
    assert get('--depth-model') == backend.DAD_DEPTH_MODEL
    assert get('--mode') == '3d' and get('--disparity-profile') == 'comfort'
    assert get('--fps') == '60' and get('--depth-execution') == 'cuda-graph'
    assert '--fused-depth-resize' in arguments and '--fused-colour-fit' in arguments


def test_stopped_quality_roundtrip_without_process_start(rig):
    rig.state.live.clear()
    c = rig.c
    original = copy.deepcopy(c.preferences)
    for quality in ('quality', 'standard'):
        c._select_ai_quality(quality)
        assert c.preferences == {**original, 'ai_quality': quality}
        assert c.get_snapshot()['ai_quality'] == quality
        assert c.get_snapshot()['active_ai_quality'] is None
        saved = json.loads(c.settings_path.read_text())
        assert saved['ai_quality'] == quality
        reopened = backend.DesktopController(c.root, start_worker=False)
        try:
            assert reopened.preferences == c.preferences
        finally:
            reopened.close()


@pytest.mark.parametrize('live', ['both', 'producer', 'host'])
def test_live_or_orphan_host_rejects_quality_without_saving_change(rig, live):
    if live == 'producer':
        rig.state.live.pop(rig.state.host['pid'])
    if live == 'host':
        rig.state.live.pop(rig.state.producer['pid'])
    with pytest.raises(RuntimeError, match='송출 중지'):
        rig.c._select_ai_quality('quality')
    assert rig.c.preferences['ai_quality'] == 'standard'


def test_conflict_and_helper_prevent_selection(rig, monkeypatch):
    c = rig.c
    monkeypatch.setattr(c, '_inspect', lambda: None)
    c._set(phase='conflict', running=False)
    with pytest.raises(RuntimeError, match='송출 중지'):
        c._select_ai_quality('quality')
    c._set(phase='idle')
    monkeypatch.setattr(c, '_check_pending_helper', lambda: (_ for _ in ()).throw(RuntimeError('helper active')))
    with pytest.raises(RuntimeError, match='helper active'):
        c._select_ai_quality('quality')
    assert c.preferences['ai_quality'] == 'standard'


def test_save_failure_restores_preference_and_visible_choice(rig, monkeypatch):
    rig.state.live.clear()
    rig.c._inspect()
    original = copy.deepcopy(rig.c.preferences)
    monkeypatch.setattr(rig.c, '_inspect', lambda: None)
    monkeypatch.setattr(rig.c, '_save_preferences', lambda: (_ for _ in ()).throw(OSError('disk full')))
    with pytest.raises(OSError, match='disk full'):
        rig.c._select_ai_quality('quality')
    assert rig.c.preferences == original
    assert rig.c.get_snapshot()['ai_quality'] == 'standard'


def test_reopened_app_adopts_real_profile_and_tensor_shape(rig):
    write_json(rig.directory/'status.json', {**rig.status, 'ai_size': 322, 'ai_input_shape': [322,574]})
    rig.c._inspect()
    snapshot = rig.c.get_snapshot()
    assert snapshot['ai_quality'] == snapshot['active_ai_quality'] == 'quality'
    assert snapshot['ai_input_shape'] == [322,574]
    assert snapshot['ai_input_text'] == '실제 AI 입력 574 × 322 · 요청 322 px'
    assert rig.c.preferences['ai_quality'] == 'quality'


@pytest.mark.parametrize('actual_size', [None, 350])
def test_unknown_active_size_never_overwrites_next_start_setting(rig, actual_size):
    rig.c.preferences['ai_quality'] = 'quality'
    status = dict(rig.status)
    if actual_size is not None:
        status.update(ai_size=actual_size, ai_input_shape=[350,630])
    write_json(rig.directory/'status.json', status)
    rig.c._inspect()
    snapshot = rig.c.get_snapshot()
    assert snapshot['active_ai_quality'] is None
    assert snapshot['ai_quality'] == rig.c.preferences['ai_quality'] == 'quality'
    if actual_size is None:
        assert snapshot['ai_input_text'] == '실행 중 AI 설정 확인 전 · 다음 시작 Quality · Preview'
    else:
        assert snapshot['ai_input_text'] == '실제 AI 입력 630 × 350 · 요청 350 px'


@pytest.mark.parametrize('metadata', [dict(ai_size=True), dict(ai_size=321), dict(ai_size=None),
    dict(ai_input_shape=[322]), dict(ai_input_shape=[322,True]), dict(ai_input_shape='322x574')])
def test_invalid_actual_metadata_does_not_claim_quality(rig, metadata):
    write_json(rig.directory/'status.json', {**rig.status, **metadata})
    assert rig.c._read_session_status(rig.directory) is None
    assert rig.c._status_error
    assert rig.c.preferences['ai_quality'] == 'standard'


def test_requested_size_does_not_fabricate_actual_input():
    value = backend.ai_quality_status('standard', {'ai_size':322})
    assert value['active_ai_quality'] == 'quality'
    assert value['ai_input_shape'] is None
    assert value['ai_input_text'] == 'AI 짧은 변 322 px · 실제 입력 확인 전'

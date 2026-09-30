"""No hardware: persisted choice, live acknowledged switch, and unavailable files."""
import copy
import hashlib
import json
from pathlib import Path

import pytest

from quest3d import desktop_backend as backend
from quest3d.model_choice import DEFAULT_DEPTH_MODEL as BASE, DAD_DEPTH_MODEL as DAD
from test_desktop_backend import rig, write_json


def rows():return [dict(id=key,label=key,available=True) for key in (BASE,DAD)]


def test_saved_dad_and_depth_are_retained_and_unknown_model_rejected():
    value={**backend.DEFAULTS,'depth_model':DAD,'depth_percent':2.3,'profile':'comfort'}
    assert backend.validated_preferences(value)==value
    for bad in ('trt',None,[],1):
        with pytest.raises(ValueError):backend.validated_preferences({**value,'depth_model':bad})


def test_model_availability_requires_actual_matching_hash(tmp_path):
    data=b'controlled fixture, not a real model';folder=tmp_path/'models';folder.mkdir()
    target=folder/'dad.safetensors';target.write_bytes(data)
    write_json(tmp_path/'config/models.json',{DAD:dict(filename=target.name,bytes=len(data),sha256=hashlib.sha256(data).hexdigest())})
    c=backend.DesktopController(tmp_path,start_worker=False)
    try:
        available={r['id']:r['available'] for r in c._depth_model_options()}
        assert available=={BASE:False,DAD:True}
        target.write_bytes(data+b'changed')
        assert not next(r for r in c._depth_model_options() if r['id']==DAD)['available']
    finally:c.close()


def test_stopped_selection_persists_without_starting_any_process(rig,monkeypatch):
    c=rig.c;monkeypatch.setattr(c,'_inspect',lambda:None)
    c._set(running=False);c._host=None;c._producer=None
    monkeypatch.setattr(c,'_depth_model_options',rows)
    original=copy.deepcopy(c.preferences)
    c._select_depth_model(DAD)
    assert c.preferences=={**original,'depth_model':DAD}
    assert json.loads(c.settings_path.read_text())['depth_model']==DAD
    assert c.get_snapshot()['depth_model']==DAD


def test_missing_choice_preserves_saved_configuration(rig,monkeypatch):
    c=rig.c;monkeypatch.setattr(c,'_inspect',lambda:None)
    monkeypatch.setattr(c,'_depth_model_options',lambda:[dict(id=key,available=key==BASE) for key in (BASE,DAD)])
    original=copy.deepcopy(c.preferences)
    with pytest.raises(RuntimeError,match='모델 파일'):
        c._select_depth_model(DAD)
    assert c.preferences==original


def test_live_choice_uses_existing_control_not_a_restart(rig,monkeypatch):
    c=rig.c;monkeypatch.setattr(c,'_inspect',lambda:None);c._set(running=True)
    monkeypatch.setattr(c,'_depth_model_options',rows)
    calls=[];monkeypatch.setattr(c,'_control',lambda **kwargs:calls.append(kwargs))
    c._select_depth_model(DAD)
    assert calls==[dict(depth_model=DAD)]


def test_start_enables_comparison_only_when_both_models_are_verified(rig,monkeypatch):
    c=rig.c;monkeypatch.setattr(c,'_depth_model_options',rows)
    args=c._producer_argv(rig.monitor,rig.directory,hdr=False)
    assert '--enable-dad-comparison' in args
    assert args[args.index('--depth-model')+1]==BASE
    monkeypatch.setattr(c,'_depth_model_options',lambda:[dict(id=BASE,available=True),dict(id=DAD,available=False)])
    assert '--enable-dad-comparison' not in c._producer_argv(rig.monitor,rig.directory,hdr=False)

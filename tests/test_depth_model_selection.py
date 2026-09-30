"""Real control/worker/synthesis contracts; synthetic inference is not GPU evidence."""
from contextlib import nullcontext
from dataclasses import replace
import threading
import time
from types import SimpleNamespace

import pytest

from quest3d import capture, depth_choice, session
from quest3d.model_choice import DEFAULT_DEPTH_MODEL as BASE, DAD_DEPTH_MODEL as DAD
from quest3d.session_control import atomic_json, read_json, send_control, validate_request
from test_session import _frame, _synthetic_depth, _wait


@pytest.mark.parametrize('invalid', ['unknown', '', None, 1, {}, []])
def test_unknown_model_request_is_rejected(invalid):
    with pytest.raises(ValueError):
        validate_request(dict(session_id='s',request_id='r',mode='3d',disparity=4,
                              depth_model=invalid),'s',320)


def test_legacy_or_unprepared_producer_cannot_accept_model_switch(tmp_path):
    state=dict(session_id='s',running=True,requested_mode='3d',disparity=4,eye_width=320,revision=3)
    atomic_json(tmp_path/'status.json',state)
    with pytest.raises(RuntimeError,match='not prepared'):
        send_control(tmp_path,depth_model=DAD)
    assert not (tmp_path/'request.json').exists()
    state['available_depth_models']=[BASE]
    atomic_json(tmp_path/'status.json',state)
    with pytest.raises(RuntimeError,match='not prepared'):
        send_control(tmp_path,depth_model=DAD)


def test_same_static_source_switch_resets_raw_scale_history(tmp_path):
    class Engine:
        model_id=BASE
        def __init__(self,size):pass
        def select_model(self,value):self.model_id=value
        def infer(self,bgra,**values):
            depth=_synthetic_depth(bgra,**values)
            return replace(depth,tensor=depth.tensor if self.model_id==BASE else depth.tensor*20+10)
        def close(self):pass
    worker=session.LatestAIWorker(eye_width=320,eye_height=180,ai_size=280,directory=tmp_path,
                                  engine_factory=Engine)
    frame=_frame()
    try:
        _wait(lambda:worker.ready)
        for revision,model,expected in [(0,BASE,(0,1)),(1,DAD,(10,30)),(2,BASE,(0,1))]:
            worker.submit(frame,revision,4,depth_model=model)
            output=_wait(lambda: p if (p:=worker.snapshot()[0]) and p.revision==revision else None)
            assert output.depth_model==model
            assert output.stereo.scene_reset
            assert output.stereo.depth_range==pytest.approx(expected)
            assert output.source is frame and output.stereo.frame_id==frame.frame_id
    finally:worker.close()


def test_live_switch_acknowledges_new_model_and_keeps_stop_2d_independent(monkeypatch,tmp_path):
    prepared=[];closed=[];capture_started=threading.Event();dad_entered=threading.Event();release=threading.Event()
    errors=[];published=[]
    class Engine:
        def __init__(self,size,*,model_id,**kwargs):self.model_id=model_id
        def prepare_execution(self,w,h):
            assert not capture_started.is_set()
            prepared.append(self.model_id)
        def execution_status(self):return {'effective':'cuda-graph','model_id':self.model_id}
        def infer(self,bgra,**values):
            if self.model_id==DAD and not release.is_set():
                dad_entered.set();assert release.wait(4)
            depth=_synthetic_depth(bgra,**values)
            return replace(depth,tensor=depth.tensor if self.model_id==BASE else depth.tensor*20+10)
        def close(self):closed.append(self.model_id)
    class Capture:
        dropped_frames=0
        def __init__(self,**kwargs):self.frame=None
        def __enter__(self):
            assert prepared==[BASE,DAD]
            capture_started.set();return self
        def __exit__(self,*args):pass
        def grab(self,**kwargs):
            if self.frame is None:self.frame=_frame();return self.frame
            raise TimeoutError()
    class Publisher:
        skipped=0;epoch=11
        def publish(self,bgra,**kwargs):published.append(kwargs);return True
        def close(self):pass
    class Stream:
        def wait_stream(self,other):pass
    monkeypatch.setattr(depth_choice,'DepthEngine',Engine)
    monkeypatch.setattr(session,'GPUDesktopCapture',Capture)
    monkeypatch.setattr(session,'FramePublisher',Publisher)
    monkeypatch.setattr(session,'ARTIFACT_DIR',tmp_path/'routing')
    monkeypatch.setattr(capture,'list_monitors',lambda:[SimpleNamespace(index=1,bounds=_frame().geometry.bounds)])
    monkeypatch.setattr(session.torch.cuda,'Stream',Stream)
    monkeypatch.setattr(session.torch.cuda,'current_stream',Stream)
    monkeypatch.setattr(session.torch.cuda,'stream',lambda _:nullcontext())
    directory=tmp_path/'session'
    args=SimpleNamespace(output=str(directory),mode='3d',disparity=4,eye_width=320,eye_height=180,
        ai_size=280,monitor=1,rect=None,seconds=12,fps=60,cpu_threads=1,max_frame_age_ms=200,
        depth_execution='cuda-graph',depth_model=BASE,enable_dad_comparison=True,hide_cursor=True)
    def run():
        try:session.serve(args)
        except BaseException as exc:errors.append(exc)
    thread=threading.Thread(target=run);thread.start()
    def status():
        try:return read_json(directory/'status.json')
        except FileNotFoundError:return {}
    try:
        _wait(lambda:status().get('effective_mode')=='3d')
        old=status();switch=send_control(directory,depth_model=DAD)
        assert dad_entered.wait(3)
        _wait(lambda:status().get('seen_request')==switch['request_id'])
        assert status().get('applied_request')!=switch['request_id']
        assert status()['depth_model_switching']
        mono=send_control(directory,mode='2d')
        _wait(lambda:status().get('applied_request')==mono['request_id'])
        assert status()['effective_mode']=='2d' and not release.is_set()
        release.set()
        stereo=send_control(directory,mode='3d')
        _wait(lambda:status().get('applied_request')==stereo['request_id'])
        assert status()['effective_depth_model']==DAD
        assert status()['stream_epoch']==old['stream_epoch'] and status()['session_id']==old['session_id']
        back=send_control(directory,depth_model=BASE)
        _wait(lambda:status().get('applied_request')==back['request_id'])
        assert status()['effective_depth_model']==BASE
        assert prepared==[BASE,DAD], 'Switch unexpectedly recaptured CUDA graphs'
    finally:
        release.set()
        if status().get('running'):send_control(directory,stop=True)
        thread.join(5)
    assert not thread.is_alive() and not errors
    assert set(closed)=={BASE,DAD}
    assert status()['running'] is False and status()['ai_error'] is None

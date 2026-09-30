"""CPU fake-engine lifecycle and active-model checks; no CUDA startup."""
import threading
from types import SimpleNamespace

import pytest

from quest3d import depth_choice

B='depth_anything_v2_small'
D='distill_any_depth_small'


@pytest.fixture
def engines(monkeypatch):
    events=[];created={};fail={}
    class Engine:
        def __init__(self,size,*,model_id,**options):
            events.append(('build',model_id,size,options))
            if fail.get('build')==model_id:raise RuntimeError('injected build failure')
            self.model_id=model_id;created[model_id]=self
        def prepare_execution(self,w,h):
            events.append(('prepare',self.model_id,w,h))
            if fail.get('prepare')==self.model_id:raise RuntimeError('injected prepare failure')
            return {'effective':'cuda-graph'}
        def infer(self,*args,**kwargs):
            events.append(('infer',self.model_id,args,kwargs))
            callback=fail.get('during_infer')
            if callback:callback()
            return SimpleNamespace(model=self.model_id,frame_id=kwargs.get('frame_id'))
        def execution_status(self):return {'effective':'cuda-graph','model':{'model_id':self.model_id}}
        def close(self):
            events.append(('close',self.model_id))
            if fail.get('close')==self.model_id:raise RuntimeError('injected close failure')
    monkeypatch.setattr(depth_choice,'DepthEngine',Engine)
    return events,created,fail


def test_default_allocates_only_baseline(engines):
    events,created,_=engines
    wrapper=depth_choice.SelectableDepthEngine(280)
    assert tuple(created)==(B,) and wrapper.model_id==B and wrapper.model_ids==(B,)
    wrapper.prepare_execution(2560,1440)
    wrapper.infer('rgb',frame_id=9,generation=3)
    wrapper.close();wrapper.close()
    assert [x[:2] for x in events].count(('close',B))==1


def test_all_models_prewarmed_then_only_selected_engine_infers(engines):
    events,_,_=engines
    wrapper=depth_choice.SelectableDepthEngine(280,model_ids=(B,D),execution_mode='cuda-graph',reuse_constants=True,fused_resize=True)
    wrapper.prepare_execution(2560,1440)
    wrapper.prepare_execution(2560,1440)
    assert [x[1] for x in events if x[0]=='prepare']==[B,D]
    assert wrapper.execution_status()['prewarmed_models']==[B,D]
    wrapper.select_model(D)
    assert wrapper.infer('same rgb',frame_id=19,generation=4).model==D
    wrapper.select_model(B)
    assert wrapper.infer('new rgb',frame_id=20,generation=4).model==B
    assert [x[1] for x in events if x[0]=='infer']==[D,B]
    assert wrapper.execution_status()['available_models']==[B,D]
    with pytest.raises(RuntimeError,match='recapture'):wrapper.prepare_execution(1920,1080)
    wrapper.close()


@pytest.mark.parametrize('operation',['infer','select'])
def test_unprepared_choices_cannot_be_used(engines,operation):
    wrapper=depth_choice.SelectableDepthEngine(280,model_ids=(B,D))
    with pytest.raises(RuntimeError,match='Prepare every'):
        wrapper.infer(None) if operation=='infer' else wrapper.select_model(D)
    wrapper.close()


@pytest.mark.parametrize('ids',[(),[B],(B,B),('unknown',),([],),None])
def test_invalid_choices_fail_before_constructing_engine(engines,ids):
    events,_,_=engines
    with pytest.raises(ValueError):depth_choice.SelectableDepthEngine(280,model_ids=ids)
    assert not events


@pytest.mark.parametrize('phase',['build','prepare'])
def test_second_model_failure_releases_successful_owners(engines,phase):
    events,_,fail=engines;fail[phase]=D
    with pytest.raises(RuntimeError,match='injected'):
        wrapper=depth_choice.SelectableDepthEngine(280,model_ids=(B,D))
        wrapper.prepare_execution(2560,1440)
    closed=[x[1] for x in events if x[0]=='close']
    assert closed==([B] if phase=='build' else [D,B])


def test_failed_close_does_not_abandon_other_model_and_can_retry(engines):
    events,_,fail=engines
    wrapper=depth_choice.SelectableDepthEngine(280,model_ids=(B,D));fail['close']=D
    with pytest.raises(RuntimeError):wrapper.close()
    assert [x[1] for x in events if x[0]=='close']==[D,B]
    with pytest.raises(RuntimeError):wrapper.prepare_execution(2560,1440)
    fail.clear();wrapper.close()
    assert [x[1] for x in events if x[0]=='close']==[D,B,D]


def test_cross_thread_switch_refused_and_no_release_during_infer(engines):
    _,_,fail=engines
    wrapper=depth_choice.SelectableDepthEngine(280,model_ids=(B,D))
    wrapper.prepare_execution(2560,1440);wrapper.select_model(B)
    errors=[]
    def wrong_thread():
        try:wrapper.select_model(D)
        except RuntimeError as exc:errors.append(str(exc))
    thread=threading.Thread(target=wrong_thread);thread.start();thread.join(2)
    assert errors and wrapper.model_id==B
    def try_close():
        with pytest.raises(RuntimeError,match='Stop the AI worker'):wrapper.close()
    fail['during_infer']=try_close
    wrapper.infer('rgb');wrapper.close()


def test_unprepared_model_selection_preserves_active_choice(engines):
    wrapper=depth_choice.SelectableDepthEngine(280)
    wrapper.prepare_execution(2560,1440)
    with pytest.raises(ValueError):wrapper.select_model(D)
    assert wrapper.model_id==B
    wrapper.close()

"""Selectable immutable checkpoints: local setup, exact identity and strict load."""
import hashlib
import io
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest
import torch

from quest3d import assets, depth

DEFAULT = 'depth_anything_v2_small'
DAD = 'distill_any_depth_small'


@pytest.fixture
def local_models(monkeypatch, tmp_path):
    data = b'explicit test model bytes'
    shared = dict(repository='owner/repo', revision='pinned', bytes=len(data),
                  sha256=hashlib.sha256(data).hexdigest(), source_commit='runtime-pin',
                  source_repository='https://example.invalid/runtime')
    models = {DEFAULT:dict(shared,filename='baseline.pth'),
              DAD:dict(shared,filename='dad.safetensors',repository_filename='small/model.safetensors',
                       weight_format='safetensors',runtime_source_model_id=DEFAULT)}
    (tmp_path/'config').mkdir()
    (tmp_path/'config/models.json').write_text(json.dumps(models),encoding='utf-8')
    monkeypatch.setattr(assets,'ROOT',tmp_path)
    monkeypatch.setattr(assets,'MODEL_DIR',tmp_path/'models')
    return tmp_path, models, data


def test_defaults_and_explicit_path_signature_preserved(local_models):
    root, models, data = local_models
    assert assets.model_spec() == models[DEFAULT]
    path=root/'explicit.pth';path.write_bytes(data)
    assert assets.verified_model(path) == path
    assert assets.verified_model(path,model_id=DAD) == path
    assert assets.model_spec(DAD)['repository_filename']=='small/model.safetensors'


@pytest.mark.parametrize('bad',[None,1,'unknown','../baseline',[],{}])
def test_unknown_model_rejected_before_cuda_or_download(monkeypatch,bad):
    monkeypatch.setattr(torch.cuda,'is_available',lambda:pytest.fail('CUDA queried'))
    monkeypatch.setattr(assets.urllib.request,'urlopen',lambda *_a,**_k:pytest.fail('Network accessed'))
    with pytest.raises(ValueError): assets.model_spec(bad)
    with pytest.raises(ValueError): assets.setup_model(bad)
    with pytest.raises(ValueError): depth.DepthEngine(model_id=bad)


def test_missing_dad_identifies_explicit_install_command(local_models):
    with pytest.raises(FileNotFoundError,match='setup-model --model-id distill_any_depth_small'):
        assets.verified_model(model_id=DAD)


def test_official_subpath_download_and_separate_receipt(local_models,monkeypatch):
    root, models, data = local_models
    (root/'models').mkdir();old=root/'models/receipt.json';old.write_bytes(b'baseline receipt')
    requests=[]
    def open_url(request,timeout):
        requests.append(request.full_url)
        return io.BytesIO(data)
    monkeypatch.setattr(assets.urllib.request,'urlopen',open_url)
    receipt=assets.setup_model(DAD)
    assert requests==['https://huggingface.co/owner/repo/resolve/pinned/small/model.safetensors']
    assert receipt['model_id']==DAD and receipt['downloaded']
    assert (root/'models/dad.safetensors').read_bytes()==data
    assert old.read_bytes()==b'baseline receipt'
    assert json.loads((root/f'models/receipt-{DAD}.json').read_text())['sha256']==models[DAD]['sha256']
    monkeypatch.setattr(assets.urllib.request,'urlopen',lambda *_a,**_k:pytest.fail('Unnecessary download'))
    assert assets.setup_model(DAD)['downloaded'] is False


def test_default_setup_still_uses_default_filename_and_receipt(local_models,monkeypatch):
    root, _, data=local_models
    monkeypatch.setattr(assets.urllib.request,'urlopen',lambda *_a,**_k:io.BytesIO(data))
    result=assets.setup_model()
    assert result['model_id']==DEFAULT
    assert (root/'models/baseline.pth').read_bytes()==data
    assert (root/'models/receipt.json').is_file()


@pytest.mark.parametrize('existing',[False,True])
def test_corrupt_weight_is_not_promoted_or_silently_replaced(local_models,monkeypatch,existing):
    root, _, data=local_models
    (root/'models').mkdir();target=root/'models/dad.safetensors'
    if existing:target.write_bytes(b'corrupt')
    monkeypatch.setattr(assets.urllib.request,'urlopen',
                        (lambda *_a,**_k:pytest.fail('Existing corrupt file overwritten')) if existing
                        else (lambda *_a,**_k:io.BytesIO(b'corrupt')))
    with pytest.raises(ValueError,match='checksum'):assets.setup_model(DAD)
    assert not (root/f'models/receipt-{DAD}.json').exists()
    assert target.read_bytes()==b'corrupt' if existing else not target.exists()


def test_dad_source_uses_verified_runtime_not_training_repository(local_models):
    root, _, _ = local_models
    source=root/'third_party/depth-anything-v2';file=source/'depth_anything_v2/dpt.py'
    file.parent.mkdir(parents=True);file.write_bytes(b'# runtime source')
    manifest={'source_commit':'runtime-pin','files':{'depth_anything_v2/dpt.py':assets.sha256_file(file)}}
    (root/'config/depth-source.json').write_text(json.dumps(manifest),encoding='utf-8')
    assert assets.verified_model_source(root,model_id=DAD)==source
    file.write_bytes(b'changed')
    with pytest.raises(ValueError):assets.verified_model_source(root,model_id=DAD)


def test_weight_loader_uses_safetensors_and_strict_complete_state(local_models,monkeypatch):
    root, _, data=local_models
    path=root/'dad.safetensors';path.write_bytes(data)
    calls=[]
    fake=SimpleNamespace(load_file=lambda file,device:(calls.append((file,device)) or {'weight':torch.ones(1)}))
    monkeypatch.setitem(sys.modules,'safetensors.torch',fake)
    model=SimpleNamespace(load_state_dict=lambda state,strict:(calls.append(('strict',strict,list(state))) or
                          SimpleNamespace(missing_keys=[],unexpected_keys=[])))
    monkeypatch.setattr(torch,'load',lambda *_a,**_k:pytest.fail('DAD loaded as pickle'))
    metadata=depth.load_depth_weights(model,path,model_id=DAD)
    assert calls==[(str(path),'cpu'),('strict',True,['weight'])]
    assert metadata['model_id']==DAD and metadata['strict_load']=={'missing':[],'unexpected':[]}


def test_baseline_loader_preserves_cpu_weights_only_and_strict(local_models,monkeypatch):
    root, _, data=local_models
    path=root/'baseline.pth';path.write_bytes(data)
    calls=[]
    monkeypatch.setattr(torch,'load',lambda file,**kw:(calls.append((file,kw)) or {'weight':torch.ones(1)}))
    model=SimpleNamespace(load_state_dict=lambda state,strict:(calls.append(('strict',strict)) or
                          SimpleNamespace(missing_keys=[],unexpected_keys=[])))
    metadata=depth.load_depth_weights(model,path)
    assert calls==[(path,{'map_location':'cpu','weights_only':True}),('strict',True)]
    assert metadata['model_id']==DEFAULT and metadata['weight_format']=='torch'


def test_missing_safetensors_dependency_has_explicit_error(local_models,monkeypatch):
    root, _, data=local_models
    path=root/'dad.safetensors';path.write_bytes(data)
    monkeypatch.setitem(sys.modules,'safetensors.torch',None)
    with pytest.raises(RuntimeError,match='safetensors==0.6.2'):
        depth.load_depth_weights(torch.nn.Linear(1,1),path,model_id=DAD)


def test_actual_pinned_dad_cpu_load_matches_all_model_keys():
    # Validation-only location. Product code never refers to experiment paths.
    root=Path(__file__).resolve().parents[1]
    path=root/'artifacts/experiments/depth-models-20260915/weights/dad/model.safetensors'
    if not path.exists():pytest.skip('Optional fixed local checkpoint fixture absent')
    source=assets.verified_model_source()
    sys.path.insert(0,str(source))
    from depth_anything_v2.dpt import DepthAnythingV2
    model=DepthAnythingV2(encoder='vits',features=64,out_channels=[48,96,192,384])
    metadata=depth.load_depth_weights(model,path,model_id=DAD)
    assert metadata['sha256']=='56a173c0e1b5045bf6296a5c1fb16eace0bbde2eddc24b37532cb1774ac09caa'
    assert metadata['strict_load']=={'missing':[],'unexpected':[]}
    assert sum(p.numel() for p in model.parameters())==24785089
    model.eval()
    torch.set_num_threads(2)
    with torch.inference_mode():
        output=model(torch.linspace(-1,1,3*56*98).reshape(1,3,56,98))
    assert output.shape==(1,56,98) and torch.isfinite(output).all()
    assert not torch.cuda.is_initialized()


def test_dad_does_not_drop_missing_or_extra_checkpoint_keys(local_models,monkeypatch):
    root, _, data=local_models
    path=root/'dad.safetensors';path.write_bytes(data)
    for state in ({},{'wrong':torch.ones(1)}):
        monkeypatch.setitem(sys.modules,'safetensors.torch',SimpleNamespace(load_file=lambda *_a,**_k:state))
        with pytest.raises(RuntimeError):depth.load_depth_weights(torch.nn.Linear(1,1),path,model_id=DAD)


def test_engine_rejects_already_imported_foreign_checkout_before_allocation(monkeypatch,tmp_path):
    monkeypatch.setattr(torch.cuda,'is_available',lambda:True)
    monkeypatch.setattr(torch.cuda,'Event',lambda **_k:pytest.fail('GPU event allocated'))
    monkeypatch.setattr(depth,'verified_model_source',lambda **_k:tmp_path/'verified')
    package=ModuleType('depth_anything_v2');package.__path__=[]
    util=ModuleType('depth_anything_v2.util');util.__path__=[]
    foreign=ModuleType('depth_anything_v2.dpt');foreign.__file__=str(tmp_path/'unverified/dpt.py')
    class FakeModel:
        def __init__(self,**_kwargs):pytest.fail('Unverified network constructed')
    FakeModel.__module__=foreign.__name__
    foreign.DepthAnythingV2=FakeModel
    transform=ModuleType('depth_anything_v2.util.transform');transform.Resize=object
    for module in (package,util,foreign,transform):monkeypatch.setitem(sys.modules,module.__name__,module)
    with pytest.raises(RuntimeError,match='different depth model checkout'):
        depth.DepthEngine(model_id=DAD)

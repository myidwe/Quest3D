"""Compare current default pixels with frozen pre-polish stereo, CPU only."""
import importlib.util
import hashlib
import json
from pathlib import Path
import sys
from datetime import datetime

import numpy as np
import torch
from quest3d.depth import DepthResult
from quest3d.stereo import StereoSynthesizer

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT/'artifacts/diagnostics/contour-polish-20260910-a'
spec = importlib.util.spec_from_file_location('quest3d._frozen_polish_stereo', OUT/'baseline/stereo.py')
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
torch.set_num_threads(2)
rows=[]
for source_size in ((36,64),(17,93),(97,23)):
  for method in ('backward','forward'):
    old=module.StereoSynthesizer(64,36,2,resize_filter='bicubic-aa',stereo_method=method)
    new=StereoSynthesizer(64,36,2,resize_filter='bicubic-aa',stereo_method=method)
    for frame_id in range(1,4):
        rng=np.random.default_rng(87+frame_id)
        bgra=rng.integers(0,256,(*source_size,4),dtype=np.uint8)
        bgra[:,:,3]=255
        raw=torch.from_numpy(rng.random((1,7,13),dtype=np.float32))
        depth=DepthResult(frame_id,frame_id//3,raw,(7,13),0,0)
        kw=dict(frame_id=frame_id,generation=frame_id//3)
        a=old.synthesize(bgra,depth,**kw)
        b=new.synthesize(bgra,depth,**kw)
        assert np.array_equal(a.bgra,b.bgra)
        assert a.content_rect==b.content_rect and a.depth_range==b.depth_range and a.scene_reset==b.scene_reset
        assert b.depth_refinement=='none' and b.depth_refinement_reason is None
        assert np.array_equal(old.original_2d(bgra,**kw).bgra,new.original_2d(bgra,**kw).bgra)
        rows.append(dict(source_size=list(source_size),method=method,frame_id=frame_id,identical=True))
status=json.loads((ROOT/'artifacts/session-forward-strict-20260910-a/status.json').read_text(encoding='utf-8'))
hashes={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in (
    ROOT/'src/quest3d/stereo.py',ROOT/'src/quest3d/session.py',ROOT/'src/quest3d/cli.py',ROOT/'src/quest3d/depth_edges.py',ROOT/'src/quest3d/depth_edges_cuda.py',ROOT/'src/quest3d/shaders/depth_edges.cu',ROOT/'src/quest3d/depth_temporal.py',ROOT/'src/quest3d/depth.py',ROOT/'src/quest3d/forward_warp.py',ROOT/'src/quest3d/forward_warp_cuda.py',ROOT/'src/quest3d/shaders/forward_warp.cu')}
report=dict(at=datetime.now().astimezone().isoformat(),scope=__doc__,cases=rows,hashes=hashes,
            live_status=status,live_changed=False,installed_apk_changed=False,host_changed=False)
(OUT/'final-checkpoint.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
print(json.dumps(dict(cases=len(rows),pixel_identical=True,status={k:status.get(k) for k in ('session_id','running','requested_mode','effective_mode','revision','disparity','depth_refinement','ai_error','error')}),indent=2))

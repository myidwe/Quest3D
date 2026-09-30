"""Compare complete RGB fit/depth-upsample/warp/packing/readback on a real saved pair.

Repeated saved-frame latency excludes AI, capture, network and Quest. The live
desktop continues separately. Never interpret reciprocal latency as product FPS.
"""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import time

import numpy as np
from PIL import Image
import torch

from quest3d.depth import DepthResult
from quest3d.stereo import StereoSynthesizer


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',type=Path,required=True)
    p.add_argument('--depth',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--disparity',type=float,default=13.2)
    args=p.parse_args()
    root=Path(__file__).resolve().parents[2]
    output=args.output.resolve();output.relative_to(root/'artifacts')
    output.mkdir(parents=True,exist_ok=False)
    provenance=json.loads((args.depth.parent/'replay.json').read_text(encoding='utf-8'))
    source_hash=hashlib.sha256(args.source.read_bytes()).hexdigest()
    if provenance['source_sha256']!=source_hash:raise ValueError('Source/depth provenance differs')
    torch.set_num_threads(4)
    rgb=np.array(Image.open(args.source).convert('RGB'))
    bgra=np.empty((*rgb.shape[:2],4),np.uint8);bgra[:,:,:3]=rgb[:,:,::-1];bgra[:,:,3]=255
    pixels=torch.from_numpy(bgra).cuda()
    raw=torch.from_numpy(np.load(args.depth,allow_pickle=False)).cuda()
    depth=DepthResult(1,0,raw,tuple(raw.shape[-2:]),0,0)
    options={'backward':('backward','none'),'forward-cuda':('forward-cuda','none'),
             'forward-cuda-guided':('forward-cuda','guided')}
    synths={name:StereoSynthesizer(1920,1080,args.disparity,resize_filter='bicubic-aa',
        stereo_method=method,depth_refinement=refinement) for name,(method,refinement) in options.items()}
    files=('src/quest3d/stereo.py','src/quest3d/forward_warp.py','src/quest3d/forward_warp_cuda.py',
           'src/quest3d/shaders/forward_warp.cu')
    hashes=lambda:{f:hashlib.sha256((root/f).read_bytes()).hexdigest() for f in files}
    report=dict(started_at=datetime.now().astimezone().isoformat(),source_sha256=source_hash,
        depth_sha256=hashlib.sha256(args.depth.read_bytes()).hexdigest(),source_before=hashes(),
        scope=__doc__,disparity=args.disparity,samples=[],warmup_per_method=3)
    try:
        for synth in synths.values():
            for _ in range(3):synth.synthesize(pixels,depth,frame_id=1,generation=0)
        torch.cuda.synchronize();torch.cuda.reset_peak_memory_stats()
        for repeat in range(20):
            names=tuple(synths) if repeat%2==0 else tuple(reversed(synths))
            for name in names:
                start=time.perf_counter()
                result=synths[name].synthesize(pixels,depth,frame_id=1,generation=0)
                wall=(time.perf_counter()-start)*1000
                report['samples'].append(dict(repeat=repeat,method=name,wall_ms=wall))
                assert result.frame_id==1 and result.generation==0 and result.mode=='3d'
                assert result.content_rect==(0,0,1920,1080) and result.bgra.shape==(1080,3840,4)
                assert np.all(result.bgra[:,:,3]==255)
                if repeat==0:Image.fromarray(result.bgra[:,:,[2,1,0]]).save(output/f'{name}-sbs.png')
        report['summary']={name:dict(zip(('p50','p95','max'),np.percentile(
            [s['wall_ms'] for s in report['samples'] if s['method']==name],[50,95,100]).tolist())) for name in synths}
        report['peak_allocated_mib']=torch.cuda.max_memory_allocated()/2**20
        report['source_input_unchanged']=bool(np.array_equal(pixels.cpu().numpy(),bgra))
    finally:
        for synth in synths.values():synth.close()
    report['source_after']=hashes()
    report['code_stable']=report['source_before']==report['source_after']
    report['finished_at']=datetime.now().astimezone().isoformat()
    (output/'benchmark.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='samples'},indent=2))
    if not report['code_stable']:raise RuntimeError('Source changed during comparison')


if __name__=='__main__':main()

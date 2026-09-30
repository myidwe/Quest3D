"""Interleaved local inference size comparison on one preserved frame.

Warm repeated-frame microbenchmark under live desktop contention. This does not
measure network, motion quality, presentation FPS or sustainable Quest latency.
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

from quest3d.depth import DepthEngine


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    args=p.parse_args()
    root=Path(__file__).resolve().parents[2]
    output=args.output.resolve()
    output.relative_to(root/'artifacts')
    output.mkdir(parents=True,exist_ok=False)
    torch.set_num_threads(4)
    rgb=np.array(Image.open(args.source).convert('RGB'))
    bgra=np.empty((*rgb.shape[:2],4),np.uint8)
    bgra[:,:,:3]=rgb[:,:,::-1];bgra[:,:,3]=255
    pixels=torch.from_numpy(bgra).cuda()
    engines={size:DepthEngine(size) for size in (280,350,392)}
    report=dict(started_at=datetime.now().astimezone().isoformat(),
        source_sha256=hashlib.sha256(args.source.read_bytes()).hexdigest(),
        scope='Warm repeated preserved-frame microbenchmark with live desktop contention; no stream changes',
        warmup_calls_per_size=3, samples=[])
    for engine in engines.values():
        for _ in range(3): engine.infer(pixels,frame_id=1,generation=0)
    torch.cuda.synchronize();torch.cuda.reset_peak_memory_stats()
    for repeat in range(20):
        sizes=(280,350,392) if repeat%2==0 else (392,350,280)
        for size in sizes:
            start=time.perf_counter()
            depth=engines[size].infer(pixels,frame_id=1,generation=0)
            torch.cuda.synchronize()
            report['samples'].append(dict(repeat=repeat,size=size,
                preprocess_ms=depth.preprocess_ms,inference_ms=depth.inference_ms,
                wall_ms=(time.perf_counter()-start)*1000))
    report['summary']={str(size):{key:dict(zip(('p50','p95','max'),
        np.percentile([s[key] for s in report['samples'] if s['size']==size],[50,95,100]).tolist()))
        for key in ('preprocess_ms','inference_ms','wall_ms')} for size in engines}
    report['process_peak_allocated_mib']=torch.cuda.max_memory_allocated()/2**20
    report['finished_at']=datetime.now().astimezone().isoformat()
    (output/'benchmark.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='samples'},indent=2))


if __name__=='__main__':main()

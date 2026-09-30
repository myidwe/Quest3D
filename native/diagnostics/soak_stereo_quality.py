"""Bounded real-AI replay soak without touching the live session or desktop.

Two preserved real RGB frames, controlled pans/cuts, actual pinned inference and
production fit/warp/readback. This tests PC computation over time, not real video
motion accuracy, live capture, transport, Quest decoder, or optical fusion.
"""
import argparse
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import subprocess
import time

import numpy as np
from PIL import Image
import psutil
import torch

from quest3d.depth import DepthEngine
from quest3d.stereo import StereoSynthesizer


def stamp():return datetime.now().astimezone().isoformat()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source-a',type=Path,required=True)
    p.add_argument('--source-b',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--seconds',type=int,default=300,choices=range(30,601))
    args=p.parse_args()
    root=Path(__file__).resolve().parents[2]
    out=args.output.resolve();out.relative_to(root/'artifacts')
    out.mkdir(parents=True,exist_ok=False)
    free=int(subprocess.check_output(['nvidia-smi','--query-gpu=memory.free',
        '--format=csv,noheader,nounits'],text=True).strip())
    if free<1800:raise RuntimeError('Insufficient free GPU memory for isolated soak')
    torch.set_num_threads(4)
    files=['src/quest3d/depth.py','src/quest3d/stereo.py','src/quest3d/forward_warp.py',
           'src/quest3d/forward_warp_cuda.py','src/quest3d/shaders/forward_warp.cu']
    hashes=lambda:{f:hashlib.sha256((root/f).read_bytes()).hexdigest() for f in files}
    report=dict(started_at=stamp(),scope=__doc__,source_code_before=hashes(),
        requested_seconds=args.seconds,requested_serial_pacing_fps=30,free_vram_before_mib=free,
        live_settings_written=False,capture=False,quest_verified=False,sources={})
    sources=[]
    for name,path in [('a',args.source_a),('b',args.source_b)]:
        rgb=np.array(Image.open(path).convert('RGB'))
        if rgb.shape!=(1440,2560,3):raise ValueError('Replay sources must both be2560x1440')
        bgra=np.empty((1440,2560,4),np.uint8);bgra[:,:,:3]=rgb[:,:,::-1];bgra[:,:,3]=255
        sources.append(torch.from_numpy(bgra).cuda())
        report['sources'][name]=dict(path=str(path.resolve()),sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    engine=DepthEngine(280)
    synth=StereoSynthesizer(1920,1080,13.2,resize_filter='bicubic-aa',
                           stereo_method='forward-cuda',depth_refinement='none')
    columns=torch.arange(2560,device='cuda')
    process=psutil.Process()
    records=[];memory=[];previous_source=None;source_cuts=[]
    started=time.perf_counter();last_notice=-30;frame_id=0;error=None
    torch.cuda.reset_peak_memory_stats()
    try:
        with (out/'frames.jsonl').open('x',encoding='utf-8') as log:
            while time.perf_counter()-started<args.seconds:
                if (out/'STOP').exists():break
                tick=time.perf_counter();elapsed=tick-started;phase=elapsed%60
                source_index=0 if phase<40 else 1
                moving=10<=phase<40 or phase>=50
                shift=round(5*math.sin(elapsed*1.7)) if moving else 0
                frame=sources[source_index].index_select(1,(columns-shift).clamp(0,2559))
                frame_id+=1
                infer_start=time.perf_counter()
                depth=engine.infer(frame,frame_id=frame_id,generation=0)
                stereo_start=time.perf_counter()
                stereo=synth.synthesize(frame,depth,frame_id=frame_id,generation=0)
                done=time.perf_counter()
                assert stereo.frame_id==depth.frame_id==frame_id and stereo.generation==0
                assert stereo.mode=='3d' and stereo.content_rect==(0,0,1920,1080)
                assert stereo.bgra.shape==(1080,3840,4)
                cut=previous_source is not None and previous_source!=source_index
                row=dict(frame_id=frame_id,elapsed=elapsed,source=source_index,moving=moving,shift=shift,
                    known_source_cut=cut,scene_reset=stereo.scene_reset,depth_range=stereo.depth_range,
                    preprocess_ms=depth.preprocess_ms,inference_ms=depth.inference_ms,
                    stereo_readback_ms=(done-stereo_start)*1000,total_ms=(done-infer_start)*1000,
                    deadline_overrun=(done-tick)>1/30,warmup=frame_id<=10)
                if cut:source_cuts.append(dict(frame_id=frame_id,detected=stereo.scene_reset,elapsed=elapsed))
                if frame_id%60==0:
                    assert np.all(stereo.bgra[:,:,3]==255)
                    expected=sources[source_index].index_select(1,(columns-shift).clamp(0,2559))
                    assert torch.equal(frame,expected),'Source input mutated'
                    del expected
                    mono=synth.original_2d(frame,frame_id=frame_id,generation=0)
                    assert np.array_equal(mono.bgra[:,:1920],mono.bgra[:,1920:])
                    assert mono.mode=='2d'
                    del mono
                del stereo,depth,frame
                if frame_id%30==0:
                    memory.append(dict(elapsed=elapsed,rss_bytes=process.memory_info().rss,
                        allocated=torch.cuda.memory_allocated(),reserved=torch.cuda.memory_reserved()))
                records.append(row);log.write(json.dumps(row)+'\n')
                previous_source=source_index
                if elapsed-last_notice>=30:
                    log.flush();last_notice=elapsed
                    print(json.dumps(dict(elapsed=elapsed,frames=frame_id,last_total_ms=row['total_ms'],
                        allocated_mib=torch.cuda.memory_allocated()/2**20)),flush=True)
                time.sleep(max(0,tick+1/30-time.perf_counter()))
    except BaseException as exc:
        error=f'{type(exc).__name__}: {exc}'
        raise
    finally:
        try:synth.close()
        except Exception as exc:
            error=f'{error or ""}; cleanup: {type(exc).__name__}: {exc}'
            raise
        finally:
            measured=[r for r in records if not r['warmup']]
            report.update(finished_at=stamp(),elapsed_seconds=time.perf_counter()-started,
                error=error,frames=frame_id,stopped_by_file=(out/'STOP').exists(),source_cuts=source_cuts,
                memory=memory,peak_allocated_bytes=torch.cuda.max_memory_allocated(),source_code_after=hashes())
            report['code_stable']=report['source_code_after']==report['source_code_before']
            report['timings_ms']={key:dict(zip(('p50','p95','max'),np.percentile(
                [r[key] for r in measured],[50,95,100]).tolist())) for key in
                ('preprocess_ms','inference_ms','stereo_readback_ms','total_ms')} if measured else {}
            report['deadline_overruns']=sum(r['deadline_overrun'] for r in measured)
            report['limits']=['controlled source transformations, not real video ground truth',
                'serial no-backlog work; not the live latest-frame worker scheduling test',
                'all computation competes with current desktop GPU usage',
                'no capture, encoder, network, Quest or physical viewing measurements']
            (out/'summary.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    if not report['code_stable']:raise RuntimeError('Source changed during soak')
    print(json.dumps({k:v for k,v in report.items() if k!='memory'},indent=2),flush=True)


if __name__=='__main__':main()

"""Replay a preserved source through real local depth and visibility candidates.

This is an offline source replay, not a live Quest verification or latency test.
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
import torch.nn.functional as F

from quest3d.depth import DepthEngine, DepthResult
from quest3d.depth_refine import guided_upsample_depth
from quest3d.forward_warp import synthesize_forward
from quest3d.stereo import StereoSynthesizer

ROOT = Path(__file__).resolve().parents[2]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--ai-size', type=int, default=280)
    p.add_argument('--disparity', type=float, default=13.2)
    p.add_argument('--guided', action='store_true')
    p.add_argument('--method', choices=('forward','forward-cuda'), default='forward')
    p.add_argument('--depth',type=Path,help='Reuse the real AI tensor preserved from this exact source frame')
    args = p.parse_args()
    output = args.output.resolve()
    output.relative_to((ROOT/'artifacts').resolve())
    output.mkdir(parents=True, exist_ok=False)
    torch.set_num_threads(4)
    report = dict(started_at=datetime.now().astimezone().isoformat(),
                  scope='Preserved source replay, exact same RGB for AI and stereo; no live publication',
                  source=str(args.source.resolve()), source_sha256=hashlib.sha256(args.source.read_bytes()).hexdigest(),
                  ai_size=args.ai_size, disparity_px=args.disparity, method=args.method,
                  guided=args.guided, live_control_written=False)
    rgb = np.array(Image.open(args.source).convert('RGB'))
    bgra = np.empty((*rgb.shape[:2],4),dtype=np.uint8)
    bgra[:,:,:3] = rgb[:,:,::-1]
    bgra[:,:,3] = 255
    pixels = torch.from_numpy(bgra).cuda()
    if args.depth:
        metadata=json.loads((args.depth.parent/'replay.json').read_text(encoding='utf-8'))
        if metadata['source_sha256'] != report['source_sha256'] or metadata['ai_size'] != args.ai_size:
            raise ValueError('Saved depth is not bound to this exact source and AI size')
        tensor=torch.from_numpy(np.load(args.depth,allow_pickle=False)).cuda()
        depth=DepthResult(1,0,tensor,tuple(tensor.shape[-2:]),0,0)
        report['replayed_depth_sha256']=hashlib.sha256(args.depth.read_bytes()).hexdigest()
        report['replayed_depth_provenance']=str(args.depth.parent/'replay.json')
    else:
        engine = DepthEngine(args.ai_size)
        # First inference is cold and excluded from timing interpretation.
        depth = engine.infer(pixels, frame_id=1, generation=0)
    raw = depth.tensor.float()
    np.save(output/'depth.npy', raw.cpu().numpy(), allow_pickle=False)
    normalized = ((raw-raw.min())/(raw.max()-raw.min()).clamp_min(1e-6)).clamp(0,1)
    Image.fromarray((normalized[0].cpu().numpy()*255).round().astype(np.uint8)).save(output/'depth.png')
    report['raw_depth_shape'] = list(raw.shape)
    report['cold_preprocess_ms'] = depth.preprocess_ms
    report['cold_inference_ms'] = depth.inference_ms
    synth = StereoSynthesizer(1920,1080,args.disparity,resize_filter='bicubic-aa')
    baseline = synth.synthesize(pixels,depth,frame_id=1,generation=0)
    Image.fromarray(baseline.bgra[:,:,[2,1,0]]).save(output/'backward-sbs.png')
    fitted,rect = synth._fit(pixels)
    image = fitted[:,:,:3].permute(2,0,1)[None].float()/255
    d = F.interpolate(normalized[:,None],size=image.shape[-2:],mode='bilinear',align_corners=True)[0,0]
    if args.guided:
        d = guided_upsample_depth(image,normalized)
    Image.fromarray((d.cpu().numpy()*255).round().astype(np.uint8)).save(output/'eye-depth.png')
    torch.cuda.synchronize()
    torch.cuda.reset_peak_memory_stats()
    started=time.perf_counter()
    if args.method=='forward-cuda':
        from quest3d.forward_warp_cuda import CudaForwardWarper
        with CudaForwardWarper() as owner:
            candidate=owner(image,d,args.disparity,.5)
            report['compiled_kernel']=owner.metadata
    else:
        candidate = synthesize_forward(image,d,args.disparity,.5)
    torch.cuda.synchronize()
    report['forward_one_call_ms'] = (time.perf_counter()-started)*1000
    report['timing_scope'] = ('One call with module compile and close, live desktop contention; NOT warm throughput'
        if args.method=='forward-cuda' else 'One call, live desktop contention, not warm throughput')
    report['process_peak_allocated_mib'] = torch.cuda.max_memory_allocated()/2**20
    eyes = (candidate.eyes.clamp(0,1)*255).round().byte().permute(0,2,3,1).cpu().numpy()[:,:,:,::-1]
    Image.fromarray(np.concatenate(list(eyes),axis=1)).save(output/'forward-sbs.png')
    holes=candidate.hole_mask.cpu().numpy()
    filled=candidate.filled_mask.cpu().numpy()
    reconstructed=getattr(candidate,'reconstructed_mask',None)
    report['reconstructed_fraction_per_eye']=(reconstructed.cpu().numpy().mean(axis=(1,2)).tolist()
        if reconstructed is not None else None)
    estimated=getattr(candidate,'estimated_donor_mask',None)
    report['estimated_donor_fraction_per_eye']=(estimated.cpu().numpy().mean(axis=(1,2)).tolist()
        if estimated is not None else None)
    for eye,name in enumerate(('left','right')):
        Image.fromarray(eyes[eye]).save(output/f'forward-{name}.png')
        Image.fromarray((holes[eye]*255).astype(np.uint8)).save(output/f'holes-{name}.png')
        Image.fromarray(((holes[eye]&~filled[eye])*255).astype(np.uint8)).save(output/f'unfilled-{name}.png')
    report['hole_fraction_per_eye']=holes.mean(axis=(1,2)).tolist()
    report['unfilled_fraction_per_eye']=(holes&~filled).mean(axis=(1,2)).tolist()
    report['source_unchanged']=bool(np.array_equal(pixels.cpu().numpy(),bgra))
    report['finite_output']=bool(torch.isfinite(candidate.eyes).all())
    report['same_frame_id']=depth.frame_id==baseline.frame_id==1
    report['content_rect']=rect
    report['source_code_hashes']={name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in (
        'src/quest3d/stereo.py','src/quest3d/forward_warp.py','src/quest3d/depth_refine.py','src/quest3d/depth.py')}
    report['finished_at']=datetime.now().astimezone().isoformat()
    (output/'replay.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report,indent=2))


if __name__=='__main__':
    main()

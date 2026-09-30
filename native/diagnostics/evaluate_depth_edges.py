"""Same preserved real RGB/depth comparison; not a capture/Quest quality proof."""
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

from quest3d.depth_edges import edge_aware_upsample_depth
from quest3d.forward_warp_cuda import CudaForwardWarper
from quest3d.stereo import StereoSynthesizer

ROOT = Path(__file__).resolve().parents[2]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--iterations', type=int, default=24)
    args = parser.parse_args()
    args.output.resolve().relative_to((ROOT/'artifacts').resolve())
    args.output.mkdir(parents=True, exist_ok=False)
    torch.set_num_threads(4)
    source = ROOT/'artifacts/diagnostics/target-scene-20260910/source-gdi.png'
    depth_path = source.parent/'replay-280-a/depth.npy'
    provenance = json.loads((depth_path.parent/'replay.json').read_text(encoding='utf-8'))
    assert provenance['source_sha256'] == sha(source) and provenance['ai_size'] == 280
    rgb = np.asarray(Image.open(source).convert('RGB'))
    bgra = np.empty((*rgb.shape[:2], 4), np.uint8)
    bgra[..., :3], bgra[..., 3] = rgb[..., ::-1], 255
    raw = torch.from_numpy(np.load(depth_path, allow_pickle=False)).cuda()
    low = ((raw-raw.min())/(raw.max()-raw.min()).clamp_min(1e-6)).clamp(0, 1)
    fit, rect = StereoSynthesizer(1920, 1080, 13.2, resize_filter='bicubic-aa')._fit(torch.from_numpy(bgra).cuda())
    image = fit[..., :3].permute(2, 0, 1)[None].float()/255
    guide = image.flip(1)
    funcs = {'bilinear': lambda: F.interpolate(low[:, None], size=(1080, 1920), mode='bilinear', align_corners=True)[0, 0],
             'edge': lambda: edge_aware_upsample_depth(guide, low)}
    report = dict(started_at=datetime.now().astimezone().isoformat(), source_sha256=sha(source),
                  depth_sha256=sha(depth_path), provenance=str(depth_path.parent/'replay.json'),
                  scope='Same saved real RGB + AI depth; offline only; live producer competes for GPU',
                  disparity_px=13.2, eye_size=[1920, 1080], ai_shape=list(low.shape), content_rect=rect,
                  live_changed=False, samples={key: [] for key in funcs})
    outputs = {}
    for _ in range(3):
        for fn in funcs.values():
            fn()
    torch.cuda.synchronize()
    torch.cuda.reset_peak_memory_stats()
    for i in range(args.iterations):
        for name in (list(funcs) if i % 2 == 0 else list(reversed(funcs))):
            torch.cuda.synchronize()
            start = time.perf_counter_ns()
            outputs[name] = funcs[name]()
            torch.cuda.synchronize()
            report['samples'][name].append((time.perf_counter_ns()-start)/1e6)
    report['depth_only_ms'] = {key: dict(p50=float(np.percentile(values, 50)), p95=float(np.percentile(values, 95)), max=max(values)) for key, values in report['samples'].items()}
    report['peak_allocated_mib'] = torch.cuda.max_memory_allocated()/2**20
    delta = (outputs['edge']-outputs['bilinear']).cpu().numpy()
    report['depth_delta'] = dict(mean_abs=float(np.abs(delta).mean()), max_abs=float(np.abs(delta).max()),
                               changed_fraction=float((np.abs(delta)>1e-5).mean()),
                               full_disparity_delta_max=float(np.abs(delta).max()*13.2))
    # ROI coordinates in the original 2560x1440 source, fitted at 0.75 scale.
    # These broad regions contain the user-described characters/rail; no GT claim.
    rois = {'characters': (960, 400, 1660, 1050), 'rail': (0, 550, 2560, 900)}
    with CudaForwardWarper() as owner:
        report['projector'] = owner.metadata
        for name, depth in outputs.items():
            result = owner(image, depth, 13.2, .5)
            eyes = (result.eyes.clamp(0, 1)*255).round().byte().permute(0, 2, 3, 1).cpu().numpy()[..., ::-1]
            report[name+'_unfilled'] = (result.hole_mask & ~result.filled_mask).sum().item()
            np.save(args.output/(name+'-depth.npy'), depth.cpu().numpy(), allow_pickle=False)
            for index, eye in enumerate(('left', 'right')):
                output = Image.fromarray(eyes[index])
                output.save(args.output/f'{name}-{eye}.png')
                for roi, bounds in rois.items():
                    output.crop(tuple(round(v*.75) for v in bounds)).save(args.output/f'{name}-{eye}-{roi}.png')
    report['hashes'] = {str(p.relative_to(ROOT)): sha(p) for p in [ROOT/'src/quest3d/depth_edges.py', ROOT/'src/quest3d/stereo.py', ROOT/'src/quest3d/forward_warp_cuda.py', ROOT/'src/quest3d/shaders/forward_warp.cu']}
    report['finished_at'] = datetime.now().astimezone().isoformat()
    (args.output/'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps({key: value for key, value in report.items() if key != 'samples'}, indent=2))


if __name__ == '__main__':
    main()

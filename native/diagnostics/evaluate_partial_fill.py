"""Contained same-geometry colour-fill experiment; no runtime/source modifications."""
import argparse
from datetime import datetime
import hashlib
import importlib.util
import json
from pathlib import Path
import time

import numpy as np
from PIL import Image
import torch
import torch.nn.functional as F

from quest3d.forward_warp_cuda import CudaForwardWarper
from quest3d.stereo import StereoSynthesizer

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.resolve().relative_to(ROOT/'artifacts')
    args.output.mkdir(parents=True, exist_ok=False)
    torch.set_num_threads(4)
    shader_path = args.output/'partial_fill.cu'
    shader = (ROOT/'src/quest3d/shaders/forward_warp.cu').read_text(encoding='utf-8')
    old = 'if (selected >= 0) value += raw_colour[donor_offset + channel * plane] * remaining[pixel];'
    assert shader.count(old) == 1
    new = '''if (selected >= 0) {
            float donor_value = value + raw_colour[donor_offset + channel * plane] * remaining[pixel];
            float reconstruction = value / fmaxf(coverage, COVERAGE_EPS);
            float confidence = fminf(1.0f, fmaxf(0.0f, (coverage - 0.75f) / 0.20f));
            confidence = confidence * confidence * (3.0f - 2.0f * confidence);
            if (!isfinite(nearest[pixel]) || !isfinite(farthest[pixel])
                    || nearest[pixel] - farthest[pixel] > donor_depth_tolerance) confidence = 0.0f;
            value = donor_value + confidence * (reconstruction - donor_value);
        }'''
    shader_path.write_text(shader.replace(old, new), encoding='utf-8')
    wrapper = (ROOT/'src/quest3d/forward_warp_cuda.py').read_text(encoding='utf-8')
    wrapper = wrapper.replace('from .forward_warp import', 'from quest3d.forward_warp import')
    wrapper = wrapper.replace('(Path(__file__).parent / "shaders/forward_warp.cu")', 'Path('+repr(str(shader_path.resolve()))+')')
    module_path = args.output/'partial_fill_wrapper.py'
    module_path.write_text(wrapper, encoding='utf-8')
    spec = importlib.util.spec_from_file_location('partial_fill_experiment', module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = ROOT/'artifacts/diagnostics/target-scene-20260910/source-gdi.png'
    depth_path = source.parent/'replay-280-a/depth.npy'
    digest = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    provenance = json.loads((depth_path.parent/'replay.json').read_text(encoding='utf-8'))
    assert provenance['source_sha256'] == digest(source)
    rgb = np.asarray(Image.open(source).convert('RGB'))
    bgra = np.empty((*rgb.shape[:2], 4), np.uint8)
    bgra[..., :3], bgra[..., 3] = rgb[..., ::-1], 255
    low = torch.from_numpy(np.load(depth_path, allow_pickle=False)).cuda()
    low = ((low-low.min())/(low.max()-low.min()).clamp_min(1e-6)).clamp(0, 1)
    fitted, _ = StereoSynthesizer(1920, 1080, 13.2, resize_filter='bicubic-aa')._fit(torch.from_numpy(bgra).cuda())
    image = fitted[..., :3].permute(2, 0, 1)[None].float()/255
    depth = F.interpolate(low[:, None], (1080, 1920), mode='bilinear', align_corners=True)[0, 0]
    report = dict(at=datetime.now().astimezone().isoformat(), scope=__doc__, source_sha256=digest(source),
                  depth_sha256=digest(depth_path), shader_sha256=digest(shader_path), live_changed=False,
                  limitation='Experiment masks still carry original ABI semantics; do not interpret reconstruction mask as covering the new blend.')
    images = {}
    for name, factory in [('baseline', CudaForwardWarper), ('coverage', module.CudaForwardWarper)]:
        with factory() as owner:
            start = time.perf_counter()
            result = owner(image, depth, 13.2, .5)
            report[name+'_cold_call_ms'] = (time.perf_counter()-start)*1000
            report[name+'_unfilled'] = int((result.hole_mask & ~result.filled_mask).sum())
            eyes = (result.eyes.clamp(0, 1)*255).round().byte().permute(0, 2, 3, 1).cpu().numpy()[..., ::-1]
            images[name] = eyes
            for index, eye in enumerate(('left', 'right')):
                output = Image.fromarray(eyes[index])
                output.save(args.output/f'{name}-{eye}.png')
                output.crop((720, 300, 1245, 788)).save(args.output/f'{name}-{eye}-characters.png')
    report['changed_fraction_per_eye'] = np.any(images['coverage'] != images['baseline'], axis=-1).mean(axis=(1,2)).tolist()
    (args.output/'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()

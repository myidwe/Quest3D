"""Real sm75 reference parity and completion-lifetime checks for an OFF candidate."""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import time
from unittest.mock import patch

import numpy as np
import torch
from quest3d.depth_edges import edge_aware_upsample_depth
from quest3d.depth_edges_cuda import CudaDepthEdges

ROOT = Path(__file__).resolve().parents[2]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    args.output.resolve().relative_to(ROOT/'artifacts')
    args.output.mkdir(parents=True, exist_ok=False)
    torch.set_num_threads(4)
    report = dict(at=datetime.now().astimezone().isoformat(), scope=__doc__, cases=[], live_changed=False)
    shapes = [(33, 129, 5, 17), (35, 79, 7, 11), (1, 41, 1, 9), (31, 1, 5, 1), (1, 1, 1, 1), (17, 29, 17, 29)]
    with CudaDepthEdges() as owner:
        report['module'] = owner.metadata
        for index, (h, w, lh, lw) in enumerate(shapes):
            rng = torch.Generator().manual_seed(128+index)
            rgb = torch.rand((1, 3, h, w), generator=rng)
            depth = torch.rand((1, lh, lw), generator=rng)
            for strength, limit in ((.75, .04), (0., .04), (1., .02), (.75, 0.)):
                expected = edge_aware_upsample_depth(rgb, depth, strength=strength, max_correction=limit)
                rgb_device, depth_device = rgb.cuda(), depth.cuda()
                actual = owner(rgb_device, depth_device, strength=strength, max_correction=limit).cpu()
                error = float((actual-expected).abs().max())
                torch.testing.assert_close(actual, expected, rtol=0, atol=2e-6)
                assert torch.equal(rgb_device.cpu(), rgb) and torch.equal(depth_device.cpu(), depth)
                assert owner._inflight is None
                report['cases'].append(dict(shape=[h,w,lh,lw], strength=strength, limit=limit, max_abs_error=error))
        # Actual workload parity, same saved real depth and fitted RGB from the previous evaluation.
        from PIL import Image
        source = ROOT/'artifacts/diagnostics/target-scene-20260910/source-gdi.png'
        rgb_np = np.asarray(Image.open(source).convert('RGB'))
        rgb = torch.from_numpy(rgb_np.copy()).cuda().permute(2,0,1)[None].float()
        rgb = torch.nn.functional.interpolate(rgb, (1080,1920), mode='bicubic', align_corners=False, antialias=True).round().clamp(0,255)/255
        raw = torch.from_numpy(np.load(source.parent/'replay-280-a/depth.npy',allow_pickle=False)).cuda()
        depth = (raw-raw.min())/(raw.max()-raw.min())
        expected = edge_aware_upsample_depth(rgb, depth)
        actual = owner(rgb, depth)
        report['real_max_abs_error'] = float((actual-expected).abs().max())
        torch.testing.assert_close(actual, expected, rtol=0, atol=3e-5)
        times=[]
        for i in range(33):
            start=time.perf_counter_ns()
            owner(rgb,depth)
            if i>=3:times.append((time.perf_counter_ns()-start)/1e6)
        report['real_depth_only_ms'] = dict(zip(('p50','p95','max'), np.percentile(times,[50,95,100]).tolist()))
    # Inject failure only at the completion receipt AFTER real launches.
    owner = CudaDepthEdges()
    rgb = torch.rand(1,3,33,129,device='cuda')
    depth = torch.rand(1,5,17,device='cuda')
    actual_stream = torch.cuda.current_stream()
    class MissingReceipt:
        cuda_stream = actual_stream.cuda_stream
        def synchronize(self):
            raise RuntimeError('injected missing completion receipt')
    with patch('torch.cuda.current_stream', return_value=MissingReceipt()):
        try:
            owner(rgb,depth)
            raise AssertionError('failure not raised')
        except RuntimeError as exc:
            assert 'missing completion receipt' in str(exc)
    assert owner.faulted and owner._inflight is not None and owner.module.value
    retained = owner._inflight
    with patch('torch.cuda.synchronize', side_effect=RuntimeError('injected close receipt failure')):
        try:
            owner.close()
            raise AssertionError('close failure not raised')
        except RuntimeError as exc:
            assert 'close receipt failure' in str(exc)
    assert owner._inflight is retained and owner.module.value and not owner.closed
    report['retained_bundle_entries_after_failures'] = len(retained)
    owner.close()
    assert owner._inflight is None and owner.closed and not owner.module.value
    report['actual_completion_and_close'] = 'PASS'
    report['hashes'] = {str(path.relative_to(ROOT)):hashlib.sha256(path.read_bytes()).hexdigest() for path in (
        ROOT/'src/quest3d/depth_edges.py', ROOT/'src/quest3d/depth_edges_cuda.py', ROOT/'src/quest3d/shaders/depth_edges.cu')}
    (args.output/'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='cases'}, indent=2))


if __name__=='__main__':main()

"""Capture one real HDR desktop frame and its real AI/baseline stereo for replay.

Does not publish, change the live session, or claim headset/performance validation.
"""
from dataclasses import asdict
from datetime import datetime
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--monitor', type=int, default=1)
    parser.add_argument('--disparity', type=float, default=13.2)
    args = parser.parse_args()
    output = args.output.resolve()
    output.relative_to((ROOT / 'artifacts').resolve())
    output.mkdir(parents=True, exist_ok=False)
    package = ROOT / 'artifacts/capture/hdr-experimental/package-0d8bea69406e'
    sys.path.insert(0, str(package.resolve(strict=True)))
    import numpy as np
    import torch
    from PIL import Image
    from quest3d.assets import model_spec
    from quest3d.capture import GPUDesktopCapture
    from quest3d.depth import DepthEngine
    from quest3d.session_control import read_json
    from quest3d.stereo import StereoSynthesizer
    torch.set_num_threads(4)
    live = ROOT / 'artifacts/session-quality-1080-sharp-20260910-a/status.json'
    before = read_json(live)
    free = int(subprocess.check_output(['nvidia-smi', '--query-gpu=memory.free',
        '--format=csv,noheader,nounits'], text=True).strip())
    if free < 2500:
        raise RuntimeError('Insufficient free VRAM for one additional bounded model probe')
    report = dict(started_at=datetime.now().astimezone().isoformat(),
        scope='One real WGC/HDR fused frame; real pinned depth; baseline stereo replay only, no live publication',
        live_session_before=before['session_id'], live_mode_before=before['requested_mode'],
        free_vram_before_mib=free, model=model_spec(), disparity_px=args.disparity, passed=False)
    try:
        with GPUDesktopCapture(monitor=args.monitor, experimental_hdr=True, hdr_tonemap='fused') as capture:
            frame = capture.grab(timeout_seconds=5)
            pixels = frame.bgra
            report['capture'] = dict(frame_id=frame.frame_id, generation=frame.geometry_generation,
                received_ns=frame.captured_ns, shape=list(pixels.shape), color_profile=capture.color_profile,
                monitor=args.monitor, backend=capture.backend)
        original = pixels.cpu().numpy()
        np.save(output / 'source-bgra.npy', original, allow_pickle=False)
        Image.fromarray(original[:, :, [2, 1, 0]]).save(output / 'source.png')
        engine = DepthEngine(280)
        depth = engine.infer(pixels, frame_id=frame.frame_id, generation=frame.geometry_generation)
        raw = depth.tensor.cpu().numpy()
        np.save(output / 'depth-280.npy', raw, allow_pickle=False)
        report['depth'] = dict(frame_id=depth.frame_id, generation=depth.generation,
            input_shape=list(depth.input_shape), tensor_shape=list(raw.shape), min=float(raw.min()), max=float(raw.max()),
            preprocess_ms=depth.preprocess_ms, inference_ms=depth.inference_ms,
            timing_scope='One cold-model call under live contention; not a benchmark')
        norm = (raw[0]-raw.min()) / max(float(raw.max()-raw.min()), 1e-6)
        Image.fromarray(np.round(norm*255).astype(np.uint8)).save(output / 'depth-280.png')
        synth = StereoSynthesizer(1920, 1080, args.disparity, resize_filter='bicubic-aa')
        stereo = synth.synthesize(pixels, depth, frame_id=frame.frame_id, generation=frame.geometry_generation)
        Image.fromarray(stereo.bgra[:, :, [2,1,0]]).save(output / 'baseline-sbs.png')
        Image.fromarray(stereo.bgra[:, :1920, [2,1,0]]).save(output / 'baseline-left.png')
        Image.fromarray(stereo.bgra[:, 1920:, [2,1,0]]).save(output / 'baseline-right.png')
        report['same_source_depth_frame'] = stereo.frame_id == depth.frame_id == frame.frame_id
        report['source_unchanged'] = bool(np.array_equal(pixels.cpu().numpy(), original))
        report['baseline_stereo_mode'] = stereo.mode
        report['files'] = {p.name:hashlib.sha256(p.read_bytes()).hexdigest()
                           for p in output.iterdir() if p.is_file()}
        report['source_hashes'] = {name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest()
                                  for name in ('src/quest3d/stereo.py','src/quest3d/depth.py','src/quest3d/capture.py')}
        report['passed'] = report['same_source_depth_frame'] and report['source_unchanged'] and stereo.mode == '3d'
    finally:
        after = read_json(live)
        report['live_session_after'] = after['session_id']
        report['live_mode_after'] = after['requested_mode']
        report['live_control_written'] = False
        report['finished_at'] = datetime.now().astimezone().isoformat()
        (output/'capture.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report,indent=2))


if __name__ == '__main__':
    main()

"""Run independent visible-interval regression cases on the actual CUDA path.

Synthetic geometry is ground truth only for these tiny known scenes. This does
not establish real AI depth accuracy, temporal video quality, or Quest fusion.
"""
import argparse
from datetime import datetime
import hashlib
import importlib.util
import json
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    output = args.output.resolve()
    output.relative_to(root / 'artifacts')
    output.mkdir(parents=True, exist_ok=False)
    import torch
    from quest3d.forward_warp_cuda import CudaForwardWarper
    oracle_path = root / 'tests/test_forward_temporal_visibility.py'
    spec = importlib.util.spec_from_file_location('temporal_visibility_oracle', oracle_path)
    oracle = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = oracle
    spec.loader.exec_module(oracle)
    torch.set_num_threads(2)
    files = ['src/quest3d/forward_warp.py', 'src/quest3d/forward_warp_cuda.py',
             'src/quest3d/shaders/forward_warp.cu', str(oracle_path.relative_to(root))]
    hashes = lambda: {f: hashlib.sha256((root / f).read_bytes()).hexdigest() for f in files}
    report = dict(started_at=datetime.now().astimezone().isoformat(), scope=__doc__,
                  source_before=hashes(), cases=[], status='RUNNING')
    try:
        with CudaForwardWarper(device=0) as owner:
            report['compiler'] = owner.metadata
            for cases, reference in [
                (oracle.flat_phase_cases, oracle.flat_plane_oracle),
                (oracle.thin_phase_cases, oracle.visible_interval_oracle),
                (oracle.threshold_cases, oracle.visible_interval_oracle),
                (oracle.small_separation_cases, oracle.visible_interval_oracle),
            ]:
                for case in cases():
                    expected, coverage = reference(case)
                    image, depth = case.image.cuda(), case.depth.cuda()
                    actual = owner(image, depth, case.disparity, case.convergence)
                    oracle.assert_visibility_result(actual, expected, coverage, atol=8e-6)
                    assert torch.equal(image.cpu(), case.image)
                    assert torch.equal(depth.cpu(), case.depth)
                    assert torch.isfinite(actual.eyes).all()
                    report['cases'].append(dict(name=case.name, status='PASS'))
        report['status'] = 'PASS'
    except BaseException as exc:
        report['status'] = 'FAIL'
        report['error'] = f'{type(exc).__name__}: {exc}'
        raise
    finally:
        report['source_after'] = hashes()
        report['code_stable'] = report['source_before'] == report['source_after']
        report['finished_at'] = datetime.now().astimezone().isoformat()
        with (output / 'result.json').open('x', encoding='utf-8') as stream:
            json.dump(report, stream, indent=2)
    assert report['code_stable'], 'Sources changed during actual CUDA verification'
    print(json.dumps(dict(status=report['status'], cases=len(report['cases']),
                         code_stable=report['code_stable'], compiler=report['compiler']), indent=2))


if __name__ == '__main__':
    main()

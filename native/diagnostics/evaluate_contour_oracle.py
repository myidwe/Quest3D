"""Independent visible-footprint GT for bounded interpolation, synthetic CPU only."""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
import torch
import torch.nn.functional as F

from quest3d.depth_edges import edge_aware_upsample_depth
from quest3d.forward_warp import synthesize_forward

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'tests'))
from test_forward_temporal_visibility import VisibilityCase, visible_interval_oracle


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.resolve().relative_to(ROOT/'artifacts')
    args.output.mkdir(parents=True, exist_ok=False)
    torch.set_num_threads(2)
    rows = []
    for kind in ('aligned-step', 'wrong-colour-step', 'pole1', 'pole2', 'pole3', 'low-contrast-pole'):
        rgb = torch.empty(1, 3, 33, 129)
        rgb[:, 0] = .05
        rgb[:, 1] = .15
        rgb[:, 2] = .8
        truth = torch.full((33, 129), .2)
        low = torch.full((1, 5, 17), .2)
        if 'step' in kind:
            truth[:, 68:] = .8
            low[:, :, 9:] = .8
            # Known geometry stays at x68; wrong-colour is deliberately x71.
            start = 68 if kind == 'aligned-step' else 71
            rgb[:, 0, :, start:] = .9
            rgb[:, 1, :, start:] = .1
            rgb[:, 2, :, start:] = .05
        else:
            width = int(kind[-1]) if kind.startswith('pole') else 2
            truth[:, 64:64+width] = .8
            low[:, :, 8] = .8
            if kind == 'low-contrast-pole':
                rgb[:, :, :, 64:64+width] += .02
            else:
                rgb[:, 0, :, 64:64+width] = .9
                rgb[:, 1, :, 64:64+width] = .1
                rgb[:, 2, :, 64:64+width] = .05
        estimates = {'bilinear': F.interpolate(low[:, None], (33, 129), mode='bilinear', align_corners=True)[0, 0],
                     'bounded-edge': edge_aware_upsample_depth(rgb, low)}
        for phase in (-.375, 0., .375):
            case = VisibilityCase(kind, rgb[:, :, 16:17], truth[16:17], 13.2, .5-phase/6.6, phase)
            expected, coverage = visible_interval_oracle(case)
            known = coverage >= 1-1e-12
            known[:, :10] = known[:, -10:] = False
            for name, depth in estimates.items():
                output = synthesize_forward(case.image, depth[16:17], case.disparity, case.convergence)
                actual = output.eyes[:, :, 0].numpy()
                mae = [float(np.abs(actual[eye, :, known[eye]]-expected[eye, :, known[eye]]).mean()) for eye in range(2)]
                rows.append(dict(case=kind, phase=phase, method=name, known_visible_mae_per_eye=mae,
                                 known_visible_pixels_per_eye=known.sum(1).tolist(),
                                 depth_mae=float((depth-truth).abs().mean()),
                                 holes=int(output.hole_mask.sum()), unfilled=int((output.hole_mask & ~output.filled_mask).sum())))
    report = dict(at=datetime.now().astimezone().isoformat(), scope=__doc__, rows=rows,
                  limitations='Known visible intervals only. Negative controls may worsen. Not AI depth or headset fusion proof.',
                  hashes={str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in (
                      ROOT/'src/quest3d/depth_edges.py', ROOT/'tests/test_forward_temporal_visibility.py', Path(__file__))})
    (args.output/'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()

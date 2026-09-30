"""Known infinite blue plane behind a red pole: independently evaluate fill bias."""
import argparse
from datetime import datetime
import importlib.util
import json
from pathlib import Path
import sys

import numpy as np
import torch

from quest3d.forward_warp_cuda import CudaForwardWarper

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'tests'))
from test_forward_temporal_visibility import VisibilityCase, visible_interval_oracle


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.resolve().relative_to(ROOT/'artifacts')
    args.output.mkdir(parents=True, exist_ok=False)
    spec = importlib.util.spec_from_file_location('partial_fill_oracle_candidate', args.candidate)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    rows = []
    with CudaForwardWarper() as baseline, module.CudaForwardWarper() as candidate:
      for model_case in ('exact-pole', 'soft-pole', 'soft-wide'):
        for phase in sorted({*(step/32 for step in range(-16,17)), -.2501, -.2499, -.0501, -.0499, .0499,.0501,.2499,.2501}):
            image = torch.zeros((1,3,1,64),dtype=torch.float32)
            image[:,2] = 1
            image[:,:,0,30] = torch.tensor([1.,0.,0.])
            depth = torch.zeros((1,64),dtype=torch.float32)
            depth[0,30] = 1
            if model_case == 'soft-wide':
                image[:,:,:,27:34] = torch.tensor([1.,0.,0.])[None,:,None,None]
                depth[:,27:34] = 1
            case = VisibilityCase('known-blue-background-pole',image,depth,4.,.5-phase/2,phase)
            expected, coverage = visible_interval_oracle(case)
            # Here the missing background is KNOWN by scene construction. This
            # deliberately goes beyond the original visible-only warp oracle.
            expected[:,2] += 1-coverage
            inferred_depth = depth.clone()
            if model_case != 'exact-pole':
                # Independent scene geometry is unchanged. Simulate a low-res
                # depth model's broad foreground transition, as in live AI.
                ramp = torch.tensor([.125,.25,.375,.5,.625,.75,.875,1.,.875,.75,.625,.5,.375,.25,.125])
                inferred_depth[:,23:38] = ramp
            results=[]
            for name, owner in (('baseline',baseline),('coverage',candidate)):
                result = owner(image.cuda(),inferred_depth.cuda(),case.disparity,case.convergence)
                actual=result.eyes.cpu().numpy()[:,:,0]
                assert np.isfinite(actual).all()
                error = np.abs(actual[:,:,10:-10]-expected[:,:,10:-10])
                rows.append(dict(case=model_case, phase=phase, method=name, max_abs_error=float(error.max()),
                                 mae_per_eye=error.mean(axis=(1,2)).tolist(),
                                 red_mass_per_eye=actual[:,0,10:-10].sum(1).tolist(),
                                 true_red_mass_per_eye=expected[:,0,10:-10].sum(1).tolist()))
                results.append(result)
            assert torch.equal(results[0].hole_mask,results[1].hole_mask)
            # Changes outside holes would silently alter exact observed content.
            known=(~results[0].hole_mask)[:,None].expand_as(results[0].eyes)
            assert torch.equal(results[0].eyes[known],results[1].eyes[known])
    summary={name:dict(max_abs_error=max(r['max_abs_error'] for r in rows if r['method']==name),
                        max_red_mass_bias=max(abs(v-t) for r in rows if r['method']==name for v,t in zip(r['red_mass_per_eye'],r['true_red_mass_per_eye']))) for name in ('baseline','coverage')}
    summary['per_case']={case:{name:dict(mean_error=float(np.mean([r['mae_per_eye'] for r in rows if r['case']==case and r['method']==name])),max_red_mass_bias=max(abs(v-t) for r in rows if r['case']==case and r['method']==name for v,t in zip(r['red_mass_per_eye'],r['true_red_mass_per_eye']))) for name in ('baseline','coverage')} for case in ('exact-pole','soft-pole','soft-wide')}
    report=dict(at=datetime.now().astimezone().isoformat(),scope=__doc__,rows=rows,summary=summary,
                checks='raw holes identical; fully covered observed colour identical; outputs finite',live_changed=False)
    (args.output/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='rows'},indent=2))


if __name__=='__main__':main()

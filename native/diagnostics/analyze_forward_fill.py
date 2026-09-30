"""Historical CPU diagnostic of partial footprint gaps; no live changes.

Candidate reconstruction is deliberately isolated here. It connects consecutive
source samples only across small projected gaps with similar depth and colour.
Entire disocclusion holes remain the production bounded-background-fill problem.
The fill injection requires the exact pre-reconstruction projection revision;
newer production fallbacks would otherwise contaminate the historical policies.
"""
import os
os.environ["CUDA_VISIBLE_DEVICES"] = "-1"
os.environ.setdefault("OMP_NUM_THREADS", "2")
os.environ.setdefault("MKL_NUM_THREADS", "2")

import argparse
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import time

import numpy as np
from PIL import Image
import torch
import torch.nn.functional as F

import quest3d.forward_warp as production


ROOT = Path(__file__).resolve().parents[2]
HISTORICAL_PROJECTION_SHA256 = "c6541d612cdf5a376aef94b182bf1a9ae8f60945b454d181d2f0df93da395058"


def _require_historical_projection():
    actual = hashlib.sha256(Path(production.__file__).read_bytes()).hexdigest()
    if actual != HISTORICAL_PROJECTION_SHA256:
        raise RuntimeError(
            "Historical fill diagnostic requires forward_warp.py SHA256 "
            f"{HISTORICAL_PROJECTION_SHA256}; found {actual}. New production fill "
            "must not be mistaken for the pre-reconstruction baseline. Use the "
            "preserved analysis artifacts; do not replace live source to rerun.")


def _stats(values):
    x = np.asarray(values, dtype=np.float64).reshape(-1)
    if not len(x):
        return {"count": 0}
    return {"count": len(x), "min": float(x.min()), "p50": float(np.quantile(x, .5)),
            "p95": float(np.quantile(x, .95)), "max": float(x.max()), "mean": float(x.mean())}


def continuous_gap_support(image, depth, disparity, convergence, *,
                           max_gap=.25, depth_limit=.02, colour_limit=.10):
    """Independent scalar, bounded neighbouring-sample surface reconstruction.

    Returns integrated RGB and coverage of supported gaps. Existing projected
    endpoints are unchanged. The limits are diagnostic assumptions, not tuned
    product defaults. Each source edge is considered exactly once; no recursion.
    """
    if image.device.type != "cpu" or depth.device.type != "cpu":
        raise ValueError("This diagnostic must run only on CPU")
    rgb, d = image[0].permute(1, 2, 0).numpy(), depth.numpy()
    height, width = d.shape
    mass = np.zeros((2, height, width), np.float32)
    colour = np.zeros((2, height, width, 3), np.float32)
    zsum = np.zeros_like(mass)
    accepted_edges = [0, 0]
    for eye, sign in enumerate((1., -1.)):
        for y in range(height):
            for x in range(width - 1):
                delta = float(d[y, x + 1]) - float(d[y, x])
                gap = sign * delta * disparity / 2
                if not (1e-6 < gap <= max_gap and abs(delta) <= depth_limit):
                    continue
                if float(np.max(np.abs(rgb[y, x + 1] - rgb[y, x]))) > colour_limit:
                    continue
                p0 = x + sign * (float(d[y, x]) - convergence) * disparity / 2
                p1 = x + 1 + sign * (float(d[y, x + 1]) - convergence) * disparity / 2
                begin, end = p0 + .5, p1 - .5
                accepted_edges[eye] += 1
                # At most two destination pixels because max_gap < one pixel.
                for target in range(math.floor(begin + .5), math.floor(end + .5) + 1):
                    if not 0 <= target < width:
                        continue
                    a, b = max(begin, target - .5), min(end, target + .5)
                    if b <= a:
                        continue
                    weight = b - a
                    t = ((a + b) / 2 - p0) / (p1 - p0)
                    c = rgb[y, x] * (1 - t) + rgb[y, x + 1] * t
                    z = float(d[y, x]) * (1 - t) + float(d[y, x + 1]) * t
                    mass[eye, y, target] += weight
                    colour[eye, y, target] += weight * c
                    zsum[eye, y, target] += weight * z
    return (torch.from_numpy(mass), torch.from_numpy(colour), torch.from_numpy(zsum),
            {"accepted_source_edges": accepted_edges, "max_gap_px": max_gap,
             "normalized_depth_limit": depth_limit, "max_rgb_component_delta": colour_limit})


def candidate_forward(image, depth, disparity, convergence, policy="baseline"):
    """Execute actual production projection with process-local diagnostic fill.

    The source module on disk is untouched and its function is restored in
    finally. Reconstructed mask is separate from true background donor filling;
    original hole_mask is never cleared. This is not a production API change.
    """
    _require_historical_projection()
    if policy not in {"baseline", "normalize_unfilled", "near_full_donors", "tiny_original_donors", "continuous_pairs", "partial_donor_fallback"}:
        raise ValueError(policy)
    if image.device.type != "cpu" or depth.device.type != "cpu":
        raise ValueError("CPU only")
    support = continuous_gap_support(image, depth, disparity, convergence) if policy == "continuous_pairs" else None
    observed = {}
    original_fill = production._background_fill

    def fill(colour, holes, remaining, nearest, farthest, tolerance, bound):
        coverage = 1 - remaining
        normalized = colour / coverage.clamp_min(1e-6)[..., None]
        single = torch.isfinite(nearest) & torch.isfinite(farthest) & ((nearest - farthest) <= tolerance)
        reconstructed = torch.zeros_like(holes)
        work = colour
        candidate_holes = holes
        if policy in {"near_full_donors", "tiny_original_donors"}:
            limit = .001 if policy == "tiny_original_donors" else .05
            reconstructed = holes & single & (remaining <= limit) & (coverage > 0)
            work = torch.where(reconstructed[..., None], normalized, colour)
            candidate_holes = holes & ~reconstructed
        elif policy == "continuous_pairs":
            mass, csum, zsum, _ = support
            pair_depth = zsum / mass.clamp_min(1e-6)
            reconstructed = (holes & (remaining <= .25) & (coverage > 0)
                             & (mass >= remaining - 1e-5) & (mass > 1e-6)
                             & torch.isfinite(nearest) & torch.isfinite(farthest)
                             & ((nearest - farthest) <= .02)
                             & ((nearest - pair_depth).abs() <= .02))
            replacement = colour + csum / mass.clamp_min(1e-6)[..., None] * remaining[..., None]
            work = torch.where(reconstructed[..., None], replacement, colour)
            candidate_holes = holes & ~reconstructed
        if policy == "partial_donor_fallback":
            # Donors are original projected samples, normalized by observed
            # coverage. No newly filled pixel can extend this bounded pool.
            _, height, width, _ = colour.shape
            columns = torch.arange(width).view(1, 1, width).expand(2, height, width)
            donor = (coverage > 1e-6) & single
            left = torch.cummax(torch.where(donor, columns, -1), -1).values
            right = torch.cummin(torch.where(donor, columns, width).flip(-1), -1).values.flip(-1)
            # Prefer another background sample before own-pixel extrapolation.
            left = F.pad(left[..., :-1], (1, 0), value=-1)
            right = F.pad(right[..., 1:], (0, 1), value=width)
            left_exists, right_exists = left >= 0, right < width
            ld, rd = columns-left, right-columns
            left_ok, right_ok = left_exists & (ld <= bound), right_exists & (rd <= bound)
            li, ri = left.clamp(0, width-1), right.clamp(0, width-1)
            lz, rz = nearest.gather(-1, li), nearest.gather(-1, ri)
            prefer_left = (lz < rz-tolerance) | (((lz-rz).abs() <= tolerance) & (ld <= rd))
            use_left = left_ok & (~right_ok | prefer_left)
            selected = torch.where(use_left, li, ri)
            selected_z = torch.where(use_left, lz, rz)
            bounded = (left_ok & right_ok) | (left_ok & ~right_exists) | (right_ok & ~left_exists)
            behind = ~torch.isfinite(nearest) | (selected_z <= nearest+tolerance)
            background_filled = holes & bounded & behind
            donor_rgb = normalized.gather(2, selected[..., None].expand(2, height, width, 3))
            final = colour + donor_rgb * (remaining*background_filled)[..., None]
            # Explicit approximation: a partly observed pixel keeps its own
            # colour average only when bounded farther donor filling failed.
            # Never invent a value for an entirely uncovered pixel this way.
            reconstructed = holes & ~background_filled & (coverage > 1e-6)
            final = torch.where(reconstructed[..., None], normalized, final)
        else:
            final, background_filled = original_fill(work, candidate_holes, remaining, nearest, farthest, tolerance, bound)
        if policy == "normalize_unfilled":
            reconstructed = holes & ~background_filled & (coverage > 0)
            final = torch.where(reconstructed[..., None], normalized, final)
        observed.update(remaining=remaining.clone(), coverage=coverage.clone(),
                        raw_colour=colour.clone(), nearest=nearest.clone(), farthest=farthest.clone(),
                        original_donor=(~holes & single), reconstructed=reconstructed,
                        background_filled=background_filled.clone(), tolerance=tolerance, fill_bound=bound)
        return final, background_filled | reconstructed if policy == "partial_donor_fallback" else background_filled

    production._background_fill = fill
    try:
        result = production.synthesize_forward(image, depth, disparity, convergence)
    finally:
        production._background_fill = original_fill
    if support is not None:
        observed["support_parameters"] = support[3]
    return result, observed


def _save_rgb(path, eye):
    Image.fromarray((eye.permute(1, 2, 0).clamp(0, 1).numpy() * 255).round().astype(np.uint8)).save(path)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source", type=Path, required=True)
    p.add_argument("--depth", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--disparity", type=float, default=13.2)
    p.add_argument("--global-strips", action="store_true", help="Preserve full1920 horizontal coordinate/float32 ULP; sample8 source rows")
    p.add_argument("--policies", nargs="+", choices=("baseline", "normalize_unfilled", "near_full_donors", "tiny_original_donors", "continuous_pairs", "partial_donor_fallback"),
                   default=("baseline", "normalize_unfilled", "near_full_donors", "continuous_pairs"))
    args = p.parse_args()
    _require_historical_projection()
    output = args.output.resolve()
    output.relative_to(ROOT / "artifacts" / "diagnostics" / "forward-fill-investigation")
    output.mkdir(parents=True, exist_ok=False)
    torch.set_num_threads(2)
    assert not torch.cuda.is_initialized()
    source_hashes = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in
                     (args.source, args.depth, ROOT / "src/quest3d/forward_warp.py")}
    rgb = np.array(Image.open(args.source).convert("RGB"))
    # Same production bicubic-aa/round/clamp, independently on CPU. RGB ordering
    # is immaterial to geometry and is preserved for diagnostic image export.
    image = torch.from_numpy(rgb).permute(2, 0, 1)[None].float()
    image = F.interpolate(image, (1080, 1920), mode="bicubic", align_corners=False, antialias=True)
    image = image.round().clamp(0, 255) / 255
    raw = torch.from_numpy(np.load(args.depth, allow_pickle=False)).float()
    normalized = ((raw - raw.min()) / (raw.max() - raw.min()).clamp_min(1e-6)).clamp(0, 1)
    depth = F.interpolate(normalized[:, None], (1080, 1920), mode="bilinear", align_corners=True)[0, 0]
    report = {"scope": "Preserved actual AI depth, CPU crops only; no new AI/GPU/capture/live writes",
              "started_at": datetime.now().astimezone().isoformat(), "source_sha256": source_hashes,
              "cpu_threads": 2, "disparity": args.disparity, "eye_size": [1920, 1080], "crops": {}}
    crops = {"person": (810, 370, 180, 150), "rail": (970, 510, 200, 150), "background": (370, 270, 200, 120)}
    if args.global_strips:
        crops = {"person": (0, 390, 1920, 8), "rail": (0, 560, 1920, 8), "background": (0, 300, 1920, 8)}
    sample_x = {"person": (810, 990), "rail": (970, 1170), "background": (370, 570)}
    for name, (x, y, w, h) in crops.items():
        local_image, local_depth = image[:, :, y:y+h, x:x+w].clone(), depth[y:y+h, x:x+w].clone()
        core = (slice(None), slice(None), slice(*sample_x[name])) if args.global_strips else (slice(None), slice(16, -16), slice(16, -16))
        case = {"crop_xywh": [x, y, w, h], "statistics_exclude_16px_border": not args.global_strips,
                "horizontal_float32_coordinates": "global1920" if args.global_strips else "local_crop_origin",
                "sample_x_range": sample_x[name] if args.global_strips else [16, w-16], "policies": {}}
        _save_rgb(output / f"{name}-source.png", local_image[0])
        for policy in args.policies:
            started = time.perf_counter()
            result, diag = candidate_forward(local_image, local_depth, args.disparity, .5, policy)
            holes = result.hole_mask[core]
            filled = result.filled_mask[core]
            background_filled = diag["background_filled"][core]
            reconstructed = diag["reconstructed"][core]
            remaining = diag["remaining"][core]
            unresolved = holes & ~filled & ~reconstructed
            case["policies"][policy] = {
                "cpu_call_ms_not_throughput": (time.perf_counter()-started)*1000,
                "raw_hole_fraction": holes.float().mean().item(),
                "background_filled_fraction": background_filled.float().mean().item(),
                "reported_filled_fraction": filled.float().mean().item(),
                "surface_reconstructed_fraction": reconstructed.float().mean().item(),
                "unresolved_fraction": unresolved.float().mean().item(),
                "complete_uncovered_fraction": (remaining >= 1-1e-6).float().mean().item(),
                "missing_area_mean_per_pixel_before_fill": remaining.mean().item(),
                "missing_area_sum_pixels_before_fill": remaining.sum().item(),
                "unresolved_missing_area_mean_per_pixel": (remaining * unresolved).mean().item(),
                "original_fully_covered_donor_fraction": diag["original_donor"][core].float().mean().item(),
                "remaining_fraction_in_original_holes": _stats(remaining[holes].numpy()),
                "remaining_fraction_unresolved": _stats(remaining[unresolved].numpy()),
                "holes_by_missing_width": {str(edge): int((holes & (remaining <= edge)).sum()) for edge in (.0001,.00025,.001,.01,.05,.10,.25,.50)},
                "finite_output": bool(torch.isfinite(result.eyes).all()),
                "support_parameters": diag.get("support_parameters"),
            }
            _save_rgb(output / f"{name}-{policy}-left.png", result.eyes[0])
            if policy == "baseline":
                np.savez_compressed(output/f"{name}-baseline-state.npz",
                                    remaining=diag["remaining"].numpy(),nearest=diag["nearest"].numpy(),
                                    farthest=diag["farthest"].numpy(),raw_colour=diag["raw_colour"].numpy(),
                                    holes=result.hole_mask.numpy(),filled=result.filled_mask.numpy(),
                                    source=local_image.numpy(),depth=local_depth.numpy())
        report["crops"][name] = case
    report["source_unchanged"] = all(hashlib.sha256(Path(path).read_bytes()).hexdigest()==digest for path,digest in source_hashes.items())
    report["cuda_initialized"] = torch.cuda.is_initialized()
    report["finished_at"] = datetime.now().astimezone().isoformat()
    report["diagnostic_script_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    (output / "analysis.json").write_text(json.dumps(report, indent=2)+"\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()

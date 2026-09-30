"""Experimental, locally gated joint-bilateral depth interpolation.

This stateless candidate changes only interpolation between existing low-depth
samples. It does not estimate new geometry, repair model mistakes, reduce global
disparity, or reuse another frame's depth. RGB and depth must describe the same
frame/content rectangle. Their outermost pixel centres coincide, matching the
existing bilinear ``align_corners=True`` path; singleton axes are constant.

The spatial tent has radius one LOW pixel, so at most four neighbours of a 3x3
neighbourhood contribute. RGB Gaussian weights reweight that same convex support;
no farther depth or filled pixel becomes a donor. A smooth gate requires all of:
low-depth curvature and range, differing donor colours, and a sharp actual RGB
edge within one low-cell width. Linear planes, constant depth, and flat RGB keep
the bilinear baseline. Exact low-grid sample centres remain anchors.

This is NOT the RGB linear guided filter. A colour edge displaced *within* a low
cell can still pull depth the wrong way; correlated texture and nonlinear model
noise can still imprint. Low-grid RGB point samples can miss narrow objects.
Convex bounds are not a proof of correct geometry or temporal stability. Evaluate
negative controls and actual eye projections before selecting this candidate.

All operations remain on the input CPU/CUDA device. Validation performs one scalar
device sync; interpolation/gathers have no host readback. CUDA compatibility is
not a performance claim: this Torch candidate launches multiple kernels and has
full-resolution working tensors. No stream/device/global policy is changed.
"""

import math

import torch
import torch.nn.functional as F


_DEPTH_GATE = (0.03, 0.12)  # Normalized inverse-depth units, NOT model confidence.
_COLOUR_GATE = (0.06, 0.20)  # Maximum normalized channel range among low donors.
_RGB_EDGE_GATE = (0.04, 0.16)  # Maximum adjacent full-resolution channel change.
_COLOUR_SIGMA = 0.10


def _smooth_gate(value, limits):
    weight = ((value - limits[0]) / (limits[1] - limits[0])).clamp(0, 1)
    return weight.square() * (3 - 2 * weight)


def _validate(rgb, low_depth, strength, max_correction):
    if not isinstance(rgb, torch.Tensor) or not isinstance(low_depth, torch.Tensor):
        raise ValueError("RGB and low depth must be tensors")
    if rgb.ndim != 4 or rgb.shape[:2] != (1, 3) or min(rgb.shape[-2:]) < 1:
        raise ValueError("RGB must have nonempty shape [1, 3, H, W]")
    if low_depth.ndim != 3 or low_depth.shape[0] != 1 or min(low_depth.shape[-2:]) < 1:
        raise ValueError("Low depth must have nonempty shape [1, h, w]")
    if rgb.dtype != torch.float32 or low_depth.dtype != torch.float32:
        raise ValueError("RGB and low depth must be float32")
    if rgb.device != low_depth.device or rgb.device.type not in ("cpu", "cuda"):
        raise ValueError("RGB and low depth must share a CPU or CUDA device")
    if any(out < low for out, low in zip(rgb.shape[-2:], low_depth.shape[-2:])):
        raise ValueError("Output must not downsample low depth")
    if (isinstance(strength, bool) or not isinstance(strength, (int, float))
            or not math.isfinite(strength) or not 0 <= strength <= 1):
        raise ValueError("Strength must be a finite number in [0, 1]")
    if (isinstance(max_correction, bool) or not isinstance(max_correction, (int, float))
            or not math.isfinite(max_correction) or not 0 <= max_correction <= 1
            or 0 < max_correction < torch.finfo(torch.float32).tiny):
        raise ValueError("Max correction must be zero or a normal finite float32 in (0, 1]")
    valid = (torch.isfinite(rgb).all() & (rgb >= 0).all() & (rgb <= 1).all()
             & torch.isfinite(low_depth).all()
             & (low_depth >= 0).all() & (low_depth <= 1).all())
    if not bool(valid):
        raise ValueError("RGB and low depth must be finite and in [0, 1]")


def _curvature(depth):
    """Interior second differences; boundary padding must not invent curvature."""
    result = torch.zeros_like(depth)
    if depth.shape[0] >= 3:
        result[1:-1] = (depth[:-2] - 2 * depth[1:-1] + depth[2:]).abs()
    if depth.shape[1] >= 3:
        horizontal = (depth[:, :-2] - 2 * depth[:, 1:-1] + depth[:, 2:]).abs()
        result[:, 1:-1] = torch.maximum(result[:, 1:-1], horizontal)
    return result


def _edge_nearby(rgb, low_size):
    """Max actual adjacent RGB contrast in one low cell, in high pixel units."""
    height, width = rgb.shape[-2:]
    edge = rgb.new_zeros((1, 1, height, width))
    if width > 1:
        horizontal = (rgb[..., 1:] - rgb[..., :-1]).abs().amax(1, keepdim=True)
        edge = torch.maximum(F.pad(horizontal, (1, 0)), F.pad(horizontal, (0, 1)))
    if height > 1:
        vertical = (rgb[..., 1:, :] - rgb[..., :-1, :]).abs().amax(1, keepdim=True)
        edge = torch.maximum(edge, torch.maximum(F.pad(vertical, (0, 0, 1, 0)),
                                                  F.pad(vertical, (0, 0, 0, 1))))
    ry = math.ceil((height - 1) / (low_size[0] - 1)) if low_size[0] > 1 else 0
    rx = math.ceil((width - 1) / (low_size[1] - 1)) if low_size[1] > 1 else 0
    # Separable maximum avoids a large square pooling window at high scale.
    if rx:
        edge = F.max_pool2d(edge, (1, 2 * rx + 1), stride=1, padding=(0, rx))
    if ry:
        edge = F.max_pool2d(edge, (2 * ry + 1, 1), stride=1, padding=(ry, 0))
    return edge[0, 0]


@torch.inference_mode()
def edge_aware_upsample_depth(rgb: torch.Tensor, low_depth: torch.Tensor, *,
                              strength: float = 0.75,
                              max_correction: float = 0.04) -> torch.Tensor:
    """Return float32 [H,W], locally blending RGB-weighted and bilinear depth.

    Inputs are float32 RGB [1,3,H,W] and normalized inverse depth [1,h,w], both
    finite in [0,1] on the same device; H>=h and W>=w. Strength 0..1 blends only
    the gated interpolation candidate, never rescales the depth/disparity range.
    Max correction smoothly bounds the FINAL gated delta in normalized depth
    with limit*tanh(delta/limit); .04 means under .528 full-disparity pixels at
    disparity 13.2. It is not a global disparity reduction or evidence of model
    accuracy. Positive limits below float32's smallest normal value are rejected
    so scalar conversion/flush-to-zero cannot turn delta/limit into division by
    zero. Zero strength/correction and no-upsample calls return the baseline
    after validation. No discrete correction clipping threshold is introduced.
    Output is independent storage; input tensors (including strided views) stay
    unchanged. The fixed gate thresholds are heuristics, not confidence values.
    """
    _validate(rgb, low_depth, strength, max_correction)
    size, low_size = tuple(rgb.shape[-2:]), tuple(low_depth.shape[-2:])
    with torch.autocast(device_type=rgb.device.type, enabled=False):
        baseline = F.interpolate(low_depth[:, None], size=size, mode="bilinear",
                                 align_corners=True)[0, 0]
        if strength == 0 or max_correction == 0 or size == low_size:
            return baseline.clone()
        guide = F.interpolate(rgb, size=low_size, mode="bilinear", align_corners=True)[0]
        source = low_depth[0]
        curvature = _curvature(source)
        height, width = size
        # Multiplication uses the same endpoint ratio as align_corners=True.
        y = torch.arange(height, device=rgb.device, dtype=torch.float32) * (
            (low_size[0] - 1) / (height - 1) if height > 1 else 0)
        x = torch.arange(width, device=rgb.device, dtype=torch.float32) * (
            (low_size[1] - 1) / (width - 1) if width > 1 else 0)
        y0, x0 = y.floor().long(), x.floor().long()
        fy, fx = y - y0, x - x0
        numerator, denominator = torch.zeros_like(baseline), torch.zeros_like(baseline)
        curve = torch.zeros_like(baseline)
        dmin, dmax = torch.ones_like(baseline), torch.zeros_like(baseline)
        cmin, cmax = torch.ones_like(rgb[0]), torch.zeros_like(rgb[0])
        for oy, wy in ((0, 1 - fy), (1, fy)):
            iy = (y0 + oy).clamp_max(low_size[0] - 1)[:, None]
            for ox, wx in ((0, 1 - fx), (1, fx)):
                ix = (x0 + ox).clamp_max(low_size[1] - 1)[None, :]
                spatial = wy[:, None] * wx[None, :]
                active = spatial > 0
                donor, colour = source[iy, ix], guide[:, iy, ix]
                distance2 = (rgb[0] - colour).square().mean(0)
                weight = spatial * torch.exp(distance2 * (-0.5 / _COLOUR_SIGMA**2))
                numerator += weight * donor
                denominator += weight
                curve += spatial * curvature[iy, ix]
                dmin = torch.minimum(dmin, torch.where(active, donor, 1))
                dmax = torch.maximum(dmax, torch.where(active, donor, 0))
                cmin = torch.minimum(cmin, torch.where(active[None], colour, 1))
                cmax = torch.maximum(cmax, torch.where(active[None], colour, 0))
        blend = strength * _smooth_gate(curve, _DEPTH_GATE)
        blend *= _smooth_gate(dmax - dmin, _DEPTH_GATE)
        blend *= _smooth_gate((cmax - cmin).amax(0), _COLOUR_GATE)
        blend *= _smooth_gate(_edge_nearby(rgb, low_size), _RGB_EDGE_GATE)
        # If every guide match underflows, preserve the baseline; do not amplify
        # an unsupported colour hypothesis. All proposal depths stay in the
        # original spatial donors' convex hull, including foreground peaks.
        supported = denominator > 1e-12
        proposal = (numerator / denominator.clamp_min(1e-12)).clamp(min=dmin, max=dmax)
        delta = blend * (proposal - baseline)
        correction = max_correction * torch.tanh(delta / max_correction)
        result = (baseline + correction).clamp(min=dmin, max=dmax)
        return torch.where((blend > 0) & supported, result, baseline).clamp(0, 1)

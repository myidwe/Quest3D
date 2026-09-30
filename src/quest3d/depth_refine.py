"""Optional RGB guided depth upsampling; explicit comparison only, off by default.

This is the RGB linear model from the guided filter, evaluated on the low-depth
grid and reconstructed with the full-size RGB guide (fast guided filtering).
Reference: He and Sun, https://arxiv.org/abs/1505.00996 . It adds no model weights.

All resampling uses bilinear ``align_corners=True`` to match the existing depth
upsample and grid_sample coordinates in StereoSynthesizer. Low pixel (0, 0) and
(h-1, w-1) correspond to the outermost full-size pixel centers; a singleton axis
is constant. RGB must cover the same frame/content rectangle as the depth. This
module cannot verify frame identity or correct model errors, occlusion ordering,
or structures missing from the low-resolution depth.

The radius is measured in LOW-resolution pixels. A larger radius can erase thin
structures; correlated RGB texture can enter depth, and a displaced RGB edge can
pull a depth edge to the wrong place. Clamping prevents invalid depth values, not
these artifacts. Compare to bilinear before opting in; this is not a sharpening
filter for the color video, nor a guaranteed quality improvement.
"""

import math

import torch
import torch.nn.functional as F


def _validate_depth(low_depth: torch.Tensor) -> None:
    if not isinstance(low_depth, torch.Tensor) or low_depth.ndim != 3 or low_depth.shape[0] != 1:
        raise ValueError("low_depth must have shape [1, h, w]")
    if min(low_depth.shape[-2:]) < 1 or not low_depth.is_floating_point():
        raise ValueError("low_depth must be nonempty and floating point")
    if low_depth.device.type not in ("cpu", "cuda"):
        raise ValueError("low_depth must be on CPU or CUDA")
    if not bool(torch.isfinite(low_depth).all()) or not bool(((low_depth >= 0) & (low_depth <= 1)).all()):
        raise ValueError("low_depth must be finite and normalized to [0, 1]")


def _validate_size(size: tuple[int, int], low_size: tuple[int, int]) -> None:
    if (not isinstance(size, tuple) or len(size) != 2
            or any(type(value) is not int or value < 1 for value in size)):
        raise ValueError("output_size must be a positive integer (height, width) tuple")
    if any(out < low for out, low in zip(size, low_size)):
        raise ValueError("output_size must not downsample the low-depth grid")


def _resize(value: torch.Tensor, size: tuple[int, int]) -> torch.Tensor:
    return F.interpolate(value, size=size, mode="bilinear", align_corners=True)


@torch.inference_mode()
def bilinear_upsample_depth(low_depth: torch.Tensor, output_size: tuple[int, int]) -> torch.Tensor:
    """Existing depth-resize baseline, returning [H, W] on the input device.

    Float16/bfloat16 are computed in float32; float64 remains float64. Content
    validation synchronizes CUDA with the host. This helper does not select a
    GPU, move a tensor, or change StereoSynthesizer's existing default.
    """
    _validate_depth(low_depth)
    _validate_size(output_size, tuple(low_depth.shape[-2:]))
    dtype = torch.float64 if low_depth.dtype == torch.float64 else torch.float32
    with torch.autocast(device_type=low_depth.device.type, enabled=False):
        return _resize(low_depth.to(dtype)[:, None], output_size)[0, 0].clamp(0, 1)


def _box_mean(value: torch.Tensor, radius: int) -> torch.Tensor:
    # Truncated windows at the image boundary: no zero-depth or invented RGB
    # samples, and no requirement that a tiny image exceed the window diameter.
    return F.avg_pool2d(value, 2 * radius + 1, stride=1, padding=radius,
                        count_include_pad=False)


@torch.inference_mode()
def guided_upsample_depth(rgb: torch.Tensor, low_depth: torch.Tensor, *,
                          radius: int = 2, epsilon: float = 0.01) -> torch.Tensor:
    """Return finite normalized depth [H, W] from RGB [1, 3, H, W].

    Both inputs must be finite floating-point tensors in [0, 1] on the same CPU
    or CUDA device. H/W must be at least h/w. RGB is the actual color guide, not
    model-normalized channels or BGR-dependent luminance. The symmetric RGB
    covariance treats channel permutations equivalently. Computation/output use
    float32, or float64 if either input is float64; autocast is disabled locally.

    radius: 1..4 low-grid pixels. epsilon: 1e-4..1, in squared normalized RGB
    units; regularizes the 3x3 color covariance, including flat/singular guides.
    These bounds do not guarantee the correct separation of foreground objects.

    Input finite/range checks and torch.linalg.solve can synchronize a CUDA
    stream with the host. CUDA compatibility is not a throughput claim: measure
    the complete candidate before any per-frame production integration. No
    implicit device transfers, state, temporal reuse, or validation bypass exist.
    """
    _validate_depth(low_depth)
    if (not isinstance(rgb, torch.Tensor) or rgb.ndim != 4 or tuple(rgb.shape[:2]) != (1, 3)
            or min(rgb.shape[-2:]) < 1 or not rgb.is_floating_point()):
        raise ValueError("rgb must be a nonempty floating-point tensor [1, 3, H, W]")
    if rgb.device != low_depth.device:
        raise ValueError("rgb and low_depth must be on the same device")
    size = tuple(rgb.shape[-2:])
    _validate_size(size, tuple(low_depth.shape[-2:]))
    if not bool(torch.isfinite(rgb).all()) or not bool(((rgb >= 0) & (rgb <= 1)).all()):
        raise ValueError("rgb must be finite and normalized to [0, 1]")
    if type(radius) is not int or not 1 <= radius <= 4:
        raise ValueError("radius must be an integer in [1, 4] low-grid pixels")
    if (isinstance(epsilon, bool) or not isinstance(epsilon, (int, float))
            or not math.isfinite(epsilon) or not 1e-4 <= epsilon <= 1):
        raise ValueError("epsilon must be finite and in [1e-4, 1]")

    dtype = torch.float64 if torch.float64 in (rgb.dtype, low_depth.dtype) else torch.float32
    with torch.autocast(device_type=rgb.device.type, enabled=False):
        guide = rgb.to(dtype)
        depth = low_depth.to(dtype)[:, None]
        low_guide = _resize(guide, tuple(low_depth.shape[-2:]))
        mean_rgb = _box_mean(low_guide, radius)
        mean_depth = _box_mean(depth, radius)
        covariance_depth = _box_mean(low_guide * depth, radius) - mean_rgb * mean_depth

        # Fit depth = a.RGB + b independently in each low-grid window.
        products = (low_guide[:, :, None] * low_guide[:, None, :]).flatten(1, 2)
        covariance = _box_mean(products, radius).unflatten(1, (3, 3))
        covariance = covariance - mean_rgb[:, :, None] * mean_rgb[:, None, :]
        matrix = covariance.permute(0, 3, 4, 1, 2)
        matrix = matrix + epsilon * torch.eye(3, device=rgb.device, dtype=dtype)
        rhs = covariance_depth.permute(0, 2, 3, 1).unsqueeze(-1)
        a = torch.linalg.solve(matrix, rhs).squeeze(-1).permute(0, 3, 1, 2)
        b = mean_depth - (a * mean_rgb).sum(dim=1, keepdim=True)
        coefficients = _resize(_box_mean(torch.cat((a, b), dim=1), radius), size)
        result = (coefficients[:, :3] * guide).sum(dim=1) + coefficients[:, 3]
        return result[0].clamp(0, 1)

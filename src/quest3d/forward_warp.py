"""Horizontal forward stereo projection with explicit visibility and missing area.

Each source pixel has a one-pixel footprint. Its projected footprint covers a
suffix of one destination pixel and a prefix of the next. Depth peeling resolves
the nearest surface first, against the *remaining interval* of each destination
pixel. Summing splat weights alone would incorrectly call overlapping prefixes
fully covered. This implementation retains that missing-area distinction.

Projection resolves exact depth order; only equal-depth contributors share a
coverage-weighted reconstruction group. A depth-difference threshold here would
create a visible colour jump when moving surfaces cross that threshold. The
0.25-eye-pixel tolerance below is used only for choosing approximate hole donors.

Hole filling first copies a bounded, fully covered, approximately single-surface
donor from the same row, preferring the farther of the nearest left/right donors.
Only unresolved partial pixels then normalize their own observed colour coverage.
Finally, still-complete holes may use a normalized donor from original observed
single-surface coverage under the same bounded farther-donor policy. These are
separately marked estimates; no fill becomes a donor and none recovers hidden RGB.

All working tensors stay on the input device; there is no global sort, device
setting change, or GPU execution at import. Value validation performs one scalar
device synchronization per call. The bounded peeling loop has no host readback.
This is a correctness-first implementation, not a real-time performance claim.
"""

from dataclasses import dataclass
import math

import torch


@dataclass(frozen=True)
class ForwardWarpResult:
    eyes: torch.Tensor  # [2, 3, H, W], float32, [0, 1]
    hole_mask: torch.Tensor  # [2, H, W], missing/partial coverage BEFORE filling
    filled_mask: torch.Tensor  # [2, H, W], donor fill OR observed-coverage reconstruction
    reconstructed_mask: torch.Tensor | None = None  # [2, H, W], normalization fallback only
    estimated_donor_mask: torch.Tensor | None = None  # [2, H, W], original partial donor -> complete hole


_COVERAGE_EPS = 1e-6
_DONOR_SURFACE_DISPLACEMENT_PX = 0.25


def _validate_metadata(image, inverse_depth, disparity_px, convergence):
    """Keep the public shape/type/device/scalar contract without reading pixels."""
    if not isinstance(image, torch.Tensor) or not isinstance(inverse_depth, torch.Tensor):
        raise ValueError("Image and inverse depth must be tensors")
    if image.ndim != 4 or image.shape[:2] != (1, 3) or min(image.shape[2:]) < 1:
        raise ValueError("Image must have nonempty shape [1, 3, H, W]")
    if inverse_depth.shape != image.shape[2:]:
        raise ValueError("Inverse depth must have shape [H, W] matching image")
    if image.dtype != torch.float32 or inverse_depth.dtype != torch.float32:
        raise ValueError("Image and inverse depth must be float32")
    if image.device != inverse_depth.device or image.device.type not in ("cpu", "cuda"):
        raise ValueError("Image and inverse depth must share a CPU or CUDA device")
    if isinstance(disparity_px, bool) or not isinstance(disparity_px, (int, float)):
        raise ValueError("Disparity must be a finite nonnegative number")
    if (not math.isfinite(disparity_px) or disparity_px < 0
            or disparity_px > torch.finfo(torch.float32).max / 4):
        raise ValueError("Disparity must be a finite nonnegative number")
    if isinstance(convergence, bool) or not isinstance(convergence, (int, float)):
        raise ValueError("Convergence must be a finite number in [0, 1]")
    if not math.isfinite(convergence) or not 0 <= convergence <= 1:
        raise ValueError("Convergence must be a finite number in [0, 1]")


def _validate(image, inverse_depth, disparity_px, convergence):
    _validate_metadata(image, inverse_depth, disparity_px, convergence)
    # Combine checks so the public fallible boundary has one scalar sync, rather
    # than one for every predicate. Do not use asynchronous device assertions:
    # bad input must raise normally rather than poison the CUDA context.
    valid = (torch.isfinite(image).all() & (image >= 0).all() & (image <= 1).all()
             & torch.isfinite(inverse_depth).all()
             & (inverse_depth >= 0).all() & (inverse_depth <= 1).all())
    if not bool(valid):
        raise ValueError("Image and inverse depth must be finite and in [0, 1]")


def _background_fill(colour, holes, remaining, nearest_depth, farthest_depth,
                     depth_tolerance, max_distance, *, donor_mask=None, donor_colour=None):
    """Return premultiplied colour and truthful fill mask, without recursion.

    The primary call uses original fully covered pixels. The explicit secondary
    call supplies original observed-coverage donors and their normalized colour.
    Neither can use filled pixels to extend a copy beyond max_distance. At an
    exposed frame edge the one available side may be used; an internal one-sided
    candidate with its other donor outside the bound is left unfilled.
    """
    _, height, width, _ = colour.shape
    device = colour.device
    columns = torch.arange(width, device=device).view(1, 1, width).expand(2, height, width)
    original_donor = ~holes if donor_mask is None else donor_mask
    donor = (original_donor & torch.isfinite(nearest_depth) & torch.isfinite(farthest_depth)
             & ((nearest_depth - farthest_depth) <= depth_tolerance))
    left = torch.cummax(torch.where(donor, columns, -1), dim=-1).values
    right = torch.cummin(torch.where(donor, columns, width).flip(-1), dim=-1).values.flip(-1)
    left_exists, right_exists = left >= 0, right < width
    left_distance, right_distance = columns - left, right - columns
    left_ok = left_exists & (left_distance <= max_distance)
    right_ok = right_exists & (right_distance <= max_distance)
    left_index, right_index = left.clamp(0, width - 1), right.clamp(0, width - 1)
    left_depth = nearest_depth.gather(-1, left_index)
    right_depth = nearest_depth.gather(-1, right_index)
    # Depth wins over distance. Distance only breaks a coplanar tie.
    prefer_left = ((left_depth < right_depth - depth_tolerance)
                   | (((left_depth - right_depth).abs() <= depth_tolerance)
                      & (left_distance <= right_distance)))
    use_left = left_ok & (~right_ok | prefer_left)
    selected = torch.where(use_left, left_index, right_index)
    selected_depth = torch.where(use_left, left_depth, right_depth)
    both_bounded = left_ok & right_ok
    exposed_edge = (left_ok & ~right_exists) | (right_ok & ~left_exists)
    # A partially covered foreground must not receive an even nearer donor.
    behind_existing = (~torch.isfinite(nearest_depth)
                       | (selected_depth <= nearest_depth + depth_tolerance))
    filled = holes & (both_bounded | exposed_edge) & behind_existing
    original_colour = colour if donor_colour is None else donor_colour
    selected_colour = original_colour.gather(2, selected[..., None].expand(2, height, width, 3))
    colour = colour + selected_colour * (remaining * filled)[..., None]
    return colour, filled


@torch.inference_mode()
def synthesize_forward(image: torch.Tensor, inverse_depth: torch.Tensor,
                       disparity_px: float, convergence: float) -> ForwardWarpResult:
    """Project the immutable RGB/depth pair to left/right eyes on its device.

    Left projection is x + (depth - convergence) * disparity / 2; right is the
    opposite. Out-of-frame contributions are discarded, not edge-clamped.
    Masks retain partial coverage as well as entirely uncovered pixels. Filling
    never clears hole_mask. A true filled_mask means background donor fill or
    original observed-coverage normalization, not that hidden RGB is known.
    reconstructed_mask identifies only the latter fallback; a pixel's own
    coverage <= 1e-6 is never amplified. estimated_donor_mask separately identifies
    complete/near-zero-coverage holes borrowing normalized original donor RGB.

    Peeling is bounded by min(W, ceil(disparity / 2) + 2), the maximum possible
    source-pixel contributors to a destination pixel. Memory is O(H*W), while
    worst-case scatter work grows with that bound. Donor tolerance is expressed
    in eye pixels; it does not merge or reorder projected visibility layers.
    """
    _validate(image, inverse_depth, disparity_px, convergence)
    height, width = inverse_depth.shape
    device = image.device
    if disparity_px == 0:
        empty = torch.zeros((2, height, width), dtype=torch.bool, device=device)
        return ForwardWarpResult(image.expand(2, -1, -1, -1).clone(), empty,
                                 empty.clone(), empty.clone(), empty.clone())

    pixels = 2 * height * width
    columns = torch.arange(width, device=device, dtype=torch.float32)
    signs = torch.tensor((1.0, -1.0), dtype=torch.float32, device=device)[:, None, None]
    projected = columns + signs * (inverse_depth[None] - convergence) * (disparity_px / 2)
    lower = projected.floor()
    fraction = projected - lower
    destination_x = torch.stack((lower, lower + 1), dim=-1)
    valid = ((destination_x >= 0) & (destination_x < width)).reshape(-1)
    row_base = torch.arange(2 * height, device=device).reshape(2, height, 1, 1) * width
    destination = (destination_x.to(torch.int64) + row_base).reshape(-1).clamp(0, pixels - 1)
    # Local destination pixel coordinates: lower tap is [fraction, 1], upper
    # tap is [0, fraction]. Zero-length taps never compete in the z buffer.
    begin = torch.stack((fraction, torch.zeros_like(fraction)), dim=-1).reshape(-1)
    end = torch.stack((torch.ones_like(fraction), fraction), dim=-1).reshape(-1)
    depth = inverse_depth[None, :, :, None].expand(2, height, width, 2).reshape(-1)
    rgb = image[0].permute(1, 2, 0)[None, :, :, None, :].expand(2, height, width, 2, 3).reshape(-1, 3)
    active = valid & ((end - begin) > _COVERAGE_EPS)
    remaining_begin = torch.zeros(pixels, device=device, dtype=torch.float32)
    remaining_end = torch.ones(pixels, device=device, dtype=torch.float32)
    colour = torch.zeros((pixels, 3), device=device, dtype=torch.float32)
    nearest = torch.full((pixels,), -torch.inf, device=device, dtype=torch.float32)
    farthest = torch.full((pixels,), torch.inf, device=device, dtype=torch.float32)
    donor_depth_tolerance = 2 * _DONOR_SURFACE_DISPLACEMENT_PX / disparity_px
    max_layers = min(width, math.ceil(disparity_px / 2) + 2)

    for _ in range(max_layers):
        overlap_begin = torch.maximum(begin, remaining_begin[destination])
        overlap_end = torch.minimum(end, remaining_end[destination])
        overlap = (overlap_end - overlap_begin).clamp_min(0)
        eligible = active & (overlap > _COVERAGE_EPS)
        z = torch.full((pixels,), -torch.inf, device=device, dtype=torch.float32)
        z.scatter_reduce_(0, destination, torch.where(eligible, depth, -torch.inf),
                          reduce="amax", include_self=True)
        front = eligible & (depth == z[destination])
        weights = torch.where(front, overlap, 0.0)
        weight_sum = torch.zeros(pixels, device=device, dtype=torch.float32).scatter_add_(0, destination, weights)
        rgb_sum = torch.zeros((pixels, 3), device=device, dtype=torch.float32).scatter_add_(
            0, destination[:, None].expand(-1, 3), rgb * weights[:, None])

        prefix_end = torch.zeros(pixels, device=device, dtype=torch.float32)
        prefix_end.scatter_reduce_(0, destination,
                                   torch.where(front & (begin == 0), end, 0.0),
                                   reduce="amax", include_self=True)
        suffix_begin = torch.ones(pixels, device=device, dtype=torch.float32)
        suffix_begin.scatter_reduce_(0, destination,
                                     torch.where(front & (end == 1), begin, 1.0),
                                     reduce="amin", include_self=True)
        next_begin = torch.minimum(remaining_end, torch.maximum(remaining_begin, prefix_end))
        next_end = torch.maximum(next_begin, torch.minimum(remaining_end, suffix_begin))
        covered = ((remaining_end - remaining_begin) - (next_end - next_begin)).clamp_min(0)
        colour.add_(rgb_sum / weight_sum.clamp_min(_COVERAGE_EPS)[:, None] * covered[:, None])
        contributed = covered > _COVERAGE_EPS
        nearest = torch.maximum(nearest, torch.where(contributed, z, -torch.inf))
        farthest = torch.minimum(farthest, torch.where(contributed, z, torch.inf))
        remaining_begin, remaining_end = next_begin, next_end
        active = active & ~front

    remaining = (remaining_end - remaining_begin).reshape(2, height, width).clamp(0, 1)
    holes = remaining > _COVERAGE_EPS
    colour = colour.reshape(2, height, width, 3)
    original_projected_colour = colour
    fill_bound = min(width, math.ceil(disparity_px / 2) + 2)
    colour, background_filled = _background_fill(
        colour, holes, remaining, nearest.reshape(2, height, width),
        farthest.reshape(2, height, width), donor_depth_tolerance, fill_bound)
    coverage = 1 - remaining
    reconstructed = holes & ~background_filled & (coverage > _COVERAGE_EPS)
    # Background donors retain priority. This last resort removes black
    # premultiplication loss using only a pixel's own observed RGB; it is not
    # a visibility proof or an estimate of the missing background's true colour.
    colour = torch.where(reconstructed[..., None],
                         colour / coverage.clamp_min(_COVERAGE_EPS)[..., None], colour)
    filled = background_filled | reconstructed
    complete_holes = holes & ~filled & (coverage <= _COVERAGE_EPS)
    colour, estimated_donor = _background_fill(
        colour, complete_holes, remaining, nearest.reshape(2, height, width),
        farthest.reshape(2, height, width), donor_depth_tolerance, fill_bound,
        donor_mask=coverage > _COVERAGE_EPS,
        donor_colour=original_projected_colour / coverage.clamp_min(_COVERAGE_EPS)[..., None])
    filled = filled | estimated_donor
    return ForwardWarpResult(colour.clamp(0, 1).permute(0, 3, 1, 2).contiguous(),
                             holes, filled, reconstructed, estimated_donor)

"""Shared normalized-depth mapping for an explicit stereo disparity trial.

Depth is relative inverse depth, not metres or object-size confidence. The
caller supplies finite normalized floating depth after its existing upsample.
There is no range re-normalization, spatial filter, history or device readback.
"""
from __future__ import annotations

import torch


DISPARITY_PROFILES = ("linear", "comfort")


def validate_disparity_profile(profile: str) -> str:
    if not isinstance(profile, str) or profile not in DISPARITY_PROFILES:
        raise ValueError("Disparity profile must be linear or comfort")
    return profile


def map_disparity_depth(depth: torch.Tensor, convergence: float, profile: str
                        ) -> tuple[torch.Tensor, float]:
    """Return one depth map and zero-disparity plane for both eyes.

    ``comfort`` uses f(z)=.75+.5*(z-.75) below .75, else z, and f(c) for
    convergence. Thus order is nondecreasing, local slope above .75 is unchanged,
    and the original convergence plane still has zero disparity. Compression
    affects near-object/background separation and can flatten genuine detail;
    the profile name is not evidence of wearer benefit. Floating-point neighbours
    may coincide after rounding, but are never deliberately bucketed/reordered.

    ``linear`` returns the exact input object and scalar without tensor math.
    ``comfort`` allocates its output and never mutates depth or a source view.
    The caller owns normalized-depth/convergence validation; no per-frame GPU
    scalar readback is added here.
    """
    validate_disparity_profile(profile)
    if profile == "linear":
        return depth, convergence
    mapped = torch.where(depth < 0.75, 0.75 + 0.5 * (depth - 0.75), depth)
    mapped_convergence = 0.75 + 0.5 * (convergence - 0.75) if convergence < 0.75 else convergence
    return mapped, mapped_convergence

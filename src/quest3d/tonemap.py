"""Explicit experimental scRGB HDR to SDR policy; no temporal auto exposure.

scRGB uses linear BT.709 primaries; on HDR displays 1.0 represents 80 nits.
Normalize against the queried SDR white, preserve the lower range, then use a
smooth rational shoulder on max RGB. This is a product policy, not a claim of
display calibration or a standardized perceptual gamut mapping implementation.
"""
import math

import torch


@torch.inference_mode()
def scrgb_to_bgra8(rgba, *, sdr_white_scale, knee=.75):
    if (not isinstance(sdr_white_scale, (float, int)) or isinstance(sdr_white_scale, bool)
            or not .001 <= sdr_white_scale <= 100 or not math.isfinite(sdr_white_scale)):
        raise ValueError("A finite, measured scRGB SDR white scale is required")
    if not 0 < knee < 1 or not math.isfinite(knee):
        raise ValueError("Tone-map knee must be between zero and one")
    if (not isinstance(rgba, torch.Tensor) or rgba.ndim != 3 or rgba.shape[2] != 4
            or rgba.dtype not in (torch.float16, torch.float32)):
        raise ValueError("Expected HWC float16/float32 linear scRGB RGBA")
    # Extended scRGB can contain negative components. This initial policy clips
    # negative gamut excursions explicitly; perceptual gamut mapping is pending.
    rgb = torch.nan_to_num(rgba[..., :3].float(), nan=0, posinf=65504, neginf=0).clamp_min(0)
    rgb = rgb / sdr_white_scale
    peak = rgb.amax(dim=-1, keepdim=True)
    distance = (peak - knee).clamp_min(0)
    mapped_peak = torch.where(peak <= knee, peak,
                              knee + (1 - knee) * distance / (distance + 1 - knee))
    rgb = rgb * (mapped_peak / peak.clamp_min(1e-8))
    srgb = torch.where(rgb <= .0031308, rgb * 12.92,
                       1.055 * rgb.clamp_min(0).pow(1 / 2.4) - .055)
    pixels = srgb.mul(255).round().clamp(0, 255).to(torch.uint8)
    output = torch.empty_like(rgba, dtype=torch.uint8)
    output[..., 0], output[..., 1], output[..., 2] = pixels[..., 2], pixels[..., 1], pixels[..., 0]
    output[..., 3] = 255
    return output.contiguous()

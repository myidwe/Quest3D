"""Experimental float colour fit for the existing bicubic-AA stereo path.

Capture is already uint8. The current stereo path additionally rounds its fitted
colour to uint8, converts it back to float for projection, and rounds the eyes
again. This helper removes only the middle quantization. It cannot restore
capture/HDR/codec information or promise a visible sharpness improvement.

Geometry, bicubic antialias/align_corners=False, and overshoot clamping match the
existing fit. Only BGR channels are interpolated; alpha is not a synthesis input.
Original 2D must keep its existing independent path. This helper does not change
AI input, depth, disparity, projection, frame selection or the CPU frame bridge.

Callers must retain the existing RGB/depth frame-and-generation validation and
provide an immutable capture frame during the call. Every invocation allocates
owned float output: no cache, reused buffer, alias with source, or device change.
CUDA operations follow normal Torch current-stream lifetime rules; no custom
kernel, synchronization, or GPU work occurs at import.
"""

from dataclasses import dataclass

import torch
import torch.nn.functional as F


@dataclass(frozen=True)
class FloatColourFit:
    image: torch.Tensor  # Independently owned [1,3,Hfit,Wfit] float32 BGR, [0,1].
    content_rect: tuple[int, int, int, int]  # x,y,width,height inside one eye.


@torch.inference_mode()
def fit_bgra_float(bgra: torch.Tensor, eye_width: int, eye_height: int) -> FloatColourFit:
    """Fit an actual uint8 BGRA Tensor with bicubic AA, without uint8 rounding.

    This explicit candidate supports Torch CPU/CUDA only; it does not substitute
    Torch adaptive-area sampling for NumPy/cv2 INTER_AREA. Output always uses new
    storage, including same-size input. Values outside 0..255 from cubic ringing
    are clamped before normalization, exactly as in the existing fit. The caller
    owns final eye quantization and padding/opaque alpha, after projection.
    """
    if not isinstance(bgra, torch.Tensor) or bgra.ndim != 3 or bgra.shape[2] != 4:
        raise ValueError("Expected a HWC BGRA tensor")
    if bgra.dtype != torch.uint8 or min(bgra.shape[:2]) < 1:
        raise ValueError("Expected nonempty uint8 BGRA")
    if bgra.device.type not in ("cpu", "cuda"):
        raise ValueError("BGRA must be on CPU or CUDA")
    if type(eye_width) is not int or type(eye_height) is not int or min(eye_width, eye_height) < 1:
        raise ValueError("Eye width and height must be positive integers")
    height, width = bgra.shape[:2]
    scale = min(eye_width / width, eye_height / height)
    fitted_w, fitted_h = max(1, round(width * scale)), max(1, round(height * scale))
    x, y = (eye_width - fitted_w) // 2, (eye_height - fitted_h) // 2
    with torch.autocast(device_type=bgra.device.type, enabled=False):
        # uint8 -> float32 guarantees new storage even at identical dimensions.
        # Planar conversion also avoids interpolating the discarded alpha plane.
        colour = bgra[:, :, :3].permute(2, 0, 1)[None].to(
            dtype=torch.float32, memory_format=torch.contiguous_format)
        if (fitted_h, fitted_w) != (height, width):
            colour = F.interpolate(colour, size=(fitted_h, fitted_w), mode="bicubic",
                                   align_corners=False, antialias=True)
        # In-place operations affect only this call's owned float allocation.
        colour.clamp_(0, 255).div_(255)
    return FloatColourFit(colour, (x, y, fitted_w, fitted_h))

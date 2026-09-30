"""Exact, opt-in sparse sampling for the existing scene-change thumbnail.

The supported midpoint geometry uses ordinary Torch operations on a small view
of the immutable capture. There is no custom CUDA kernel, module, cached tensor,
stream, synchronization, or import-time GPU initialization. All output storage
is owned by the current call and follows normal Torch current-stream rules.
"""

import torch
import torch.nn.functional as F


@torch.inference_mode()
def tensor_scene_thumbnail(bgra: torch.Tensor, *, sparse_midpoint: bool = False
                           ) -> torch.Tensor:
    """Return the existing float32 BGR [3,18,32] scene thumbnail.

    ``sparse_midpoint=True`` avoids the full-frame float conversion only for
    uint8 inputs up to 8192 pixels per axis whose height/18 and width/32 are
    positive even integers. With
    align_corners=False, every output coordinate then lies exactly halfway
    between four input pixels. Their integer sum (0..1020) and multiplication
    by .25 are exact in float32, irrespective of reduction/FMA order. The last
    division remains the same Torch float32 /255 operation as the old path.

    Other geometries/dtypes retain the original interpolation expression. In
    particular, no general bilinear rounding, upsampling boundary, or cv2 path
    is approximated. Source pixels/strides/alpha are never changed. Input must
    remain immutable until the calling stream has consumed it, as before.
    """
    if (not isinstance(bgra, torch.Tensor) or bgra.ndim != 3
            or bgra.shape[2] != 4 or min(bgra.shape[:2]) < 1):
        raise ValueError("Expected a nonempty HWC BGRA tensor")
    if bgra.device.type not in ("cpu", "cuda"):
        raise ValueError("BGRA must be on CPU or CUDA")
    if type(sparse_midpoint) is not bool:
        raise ValueError("sparse_midpoint must be a boolean")
    height, width = bgra.shape[:2]
    if (not sparse_midpoint or bgra.dtype != torch.uint8 or max(height, width) > 8192
            or height % 36 or width % 64):
        return F.interpolate(bgra[:, :, :3].permute(2, 0, 1)[None].float(),
                             size=(18, 32), mode="bilinear", align_corners=False)[0] / 255.0

    step_y, step_x = height // 18, width // 32
    first_y, first_x = step_y // 2 - 1, step_x // 2 - 1
    # unfold only builds a strided view. The float allocation below contains
    # 3*18*32*2*2 elements (27 KiB), even for a 1440p or 2160p capture.
    samples = (bgra[first_y:, first_x:, :3]
               .unfold(0, 2, step_y).unfold(1, 2, step_x))
    thumbnail = samples.permute(2, 0, 1, 3, 4).float().sum(dim=(-2, -1))
    return thumbnail.mul_(0.25).div_(255.0)

"""CUDA resampling with coefficients from the pinned OpenCV reference.

PyTorch bicubic uses slightly different coordinate rounding than OpenCV. Four
periodic basis channels recover OpenCV's four-tap coefficients once per size,
without transferring captured pixels to the CPU or allocating an identity matrix.
"""

from functools import lru_cache

import cv2
import numpy as np
import torch


@lru_cache(maxsize=32)
def _axis_coefficients(source: int, target: int):
    if source < 1 or target < 1:
        raise ValueError("Resize dimensions must be positive")
    basis = np.zeros((1, source, 4), dtype=np.float64)
    basis[0, np.arange(source), np.arange(source) % 4] = 1
    response = cv2.resize(basis, (target, 1), interpolation=cv2.INTER_CUBIC)[0]
    position = ((np.arange(target) + 0.5) * source / target - 0.5).astype(np.float32)
    index = np.floor(position).astype(np.int64)[:, None] + np.arange(-1, 3)
    index = np.clip(index, 0, source - 1)
    coefficient = np.take_along_axis(response, index % 4, axis=1)
    # At an edge, basis responses already contain the combined replicated taps.
    for tap in range(1, 4):
        duplicate = (index[:, tap, None] == index[:, :tap]).any(axis=1)
        coefficient[duplicate, tap] = 0
    return index, coefficient.astype(np.float32)


class ReferenceCubicResize:
    def __init__(self):
        self.cache = {}

    def _coefficients(self, source: int, target: int, device):
        key = (source, target, str(device))
        if key not in self.cache:
            if len(self.cache) >= 16:
                self.cache.clear()
            indices, weights = _axis_coefficients(source, target)
            self.cache[key] = (torch.from_numpy(indices).to(device),
                               torch.from_numpy(weights).to(device))
        return self.cache[key]

    def __call__(self, image: torch.Tensor, width: int, height: int) -> torch.Tensor:
        """Return HWC float32; input is HWC on the desired device."""
        sh, sw = image.shape[:2]
        image = image.float()
        if sw != width:
            ix, wx = self._coefficients(sw, width, image.device)
            horizontal = image[:, ix[:, 0]] * wx[None, :, 0, None]
            for tap in range(1, 4):
                horizontal.add_(image[:, ix[:, tap]] * wx[None, :, tap, None])
        else:
            horizontal = image
        if sh != height:
            iy, wy = self._coefficients(sh, height, image.device)
            result = horizontal[iy[:, 0]] * wy[:, 0, None, None]
            for tap in range(1, 4):
                result.add_(horizontal[iy[:, tap]] * wy[:, tap, None, None])
            return result
        return horizontal

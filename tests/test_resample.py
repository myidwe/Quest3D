"""Compare independently to OpenCV, including border replication and odd sizes."""
import cv2
import numpy as np
import pytest
import torch

from quest3d.resample import ReferenceCubicResize


@pytest.mark.parametrize("source,target", [
    ((144, 256), (28, 50)), ((143, 257), (29, 51)),
    ((28, 50), (144, 256)), ((7, 9), (3, 5)),
    ((1, 2), (9, 7)), ((3, 1), (1, 13)), ((19, 19), (19, 19)),
])
def test_cubic_reference_coefficients_match_opencv(source, target):
    rng = np.random.default_rng(501)
    pixels = rng.integers(0, 256, size=(*source, 3), dtype=np.uint8)
    expected = cv2.resize(pixels.astype(np.float64) / 255.0,
                          target[::-1], interpolation=cv2.INTER_CUBIC)
    actual = ReferenceCubicResize()(torch.from_numpy(pixels), *target[::-1]).numpy() / 255.0
    np.testing.assert_allclose(actual, expected, rtol=1e-5, atol=3e-5)

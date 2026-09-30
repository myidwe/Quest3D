"""Output fusion preserves identity, metadata, 2D behavior and error reporting."""
from types import SimpleNamespace

import numpy as np
import pytest
import torch

from quest3d import forward_warp_cuda, session
from quest3d.depth import DepthResult
from quest3d.stereo import StereoSynthesizer


def inputs():
    source = np.zeros((18, 32, 4), dtype=np.uint8)
    source[:, :, 3] = 255
    depth = DepthResult(7, 3, torch.arange(45, dtype=torch.float32).reshape(1, 5, 9), (5, 9), 0, 0)
    return source, depth


def forbidden(*args, **kwargs):
    raise AssertionError("Unexpected reference projector / packer invocation")


def test_packed_result_keeps_geometry_metadata_and_array_owner(monkeypatch):
    source, depth = inputs()
    buffer = torch.zeros((18, 64, 4), dtype=torch.uint8)
    buffer[:, :, 3] = 255
    buffer[:, :32, 0] = 123
    buffer[:, 32:, 2] = 231
    calls = []

    def packed(image, inverse_depth, disparity, convergence, ew, eh, rect, *, projection_depth_range):
        assert projection_depth_range == (0.375, 1.0)
        calls.append((disparity, convergence, ew, eh, rect))
        return SimpleNamespace(cpu_bgra=buffer)

    monkeypatch.setattr(forward_warp_cuda, "synthesize_forward_packed_cuda", packed)
    monkeypatch.setattr(forward_warp_cuda, "synthesize_forward_cuda", forbidden)
    synth = StereoSynthesizer(32, 18, 1, stereo_method="forward-cuda", fused_output=True,
                              disparity_profile="comfort")
    output = synth.synthesize(source, depth, frame_id=7, generation=3)
    assert calls == [(1, output.effective_convergence, 32, 18, (0, 0, 32, 18))]
    assert (output.frame_id, output.generation, output.mode) == (7, 3, "3d")
    assert output.output_backend == "forward-fill-pack-cuda" and synth._stereo_packer is None
    assert output.disparity_profile == "comfort" and output.scene_reset
    assert np.shares_memory(output.bgra, buffer.numpy())
    np.testing.assert_array_equal(output.bgra, buffer.numpy())


def test_fused_failure_propagates_without_unreported_quality_fallback(monkeypatch):
    source, depth = inputs()
    def fail(*args, **kwargs):
        raise RuntimeError("injected final output failure")
    monkeypatch.setattr(forward_warp_cuda, "synthesize_forward_packed_cuda", fail)
    monkeypatch.setattr(forward_warp_cuda, "synthesize_forward_cuda", forbidden)
    synth = StereoSynthesizer(32, 18, 1, stereo_method="forward-cuda", fused_output=True)
    with pytest.raises(RuntimeError, match="injected final output failure"):
        synth.synthesize(source, depth, frame_id=7, generation=3)
    assert synth._stereo_packer is None


def test_identity_and_zero_depth_bypass_packed_gpu(monkeypatch):
    source, depth = inputs()
    monkeypatch.setattr(forward_warp_cuda, "synthesize_forward_packed_cuda", forbidden)
    synth = StereoSynthesizer(32, 18, 0, stereo_method="forward-cuda", fused_output=True)
    with pytest.raises(ValueError, match="different RGB frame"):
        synth.synthesize(source, depth, frame_id=8, generation=3)
    result = synth.synthesize(source, depth, frame_id=7, generation=3)
    assert result.mode == "2d" and result.output_backend == "torch"
    np.testing.assert_array_equal(result.bgra[:, :32], source)
    np.testing.assert_array_equal(result.bgra[:, 32:], source)


@pytest.mark.parametrize("value,method", [(1, "forward-cuda"), ("yes", "forward-cuda"), (True, "forward"), (True, "backward")])
def test_fused_output_rejects_invalid_mode_before_opening_capture(tmp_path, value, method):
    with pytest.raises(ValueError, match="Fused output"):
        StereoSynthesizer(stereo_method=method, fused_output=value)
    with pytest.raises(ValueError, match="Fused stereo output"):
        session.serve(SimpleNamespace(output=str(tmp_path), stereo_method=method, fused_stereo_output=value))

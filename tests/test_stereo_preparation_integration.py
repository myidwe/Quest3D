"""Fast preparation keeps rejection, metadata, 2D and cleanup boundaries."""
from types import SimpleNamespace

import numpy as np
import pytest
import torch

from quest3d import forward_warp_cuda, session
from quest3d.depth import DepthResult
from quest3d.stereo import StereoSynthesizer


def inputs():
    source = torch.arange(18 * 32 * 4).remainder(256).to(torch.uint8).reshape(18, 32, 4)
    source[:, :, 3] = 255
    depth = DepthResult(7, 3, torch.arange(45, dtype=torch.float32).reshape(1, 5, 9), (5, 9), 0, 0)
    return source, depth


@pytest.mark.parametrize("reject", [False, True])
def test_fused_validation_is_requested_and_rejection_propagates(monkeypatch, reject):
    source, depth = inputs()
    called = []
    def packed(image, inverse_depth, disparity, convergence, ew, eh, rect, *, fused_validation):
        assert fused_validation is True
        called.append(rect)
        if reject:
            raise ValueError("Image and inverse depth must be finite and in [0, 1]")
        return SimpleNamespace(cpu_bgra=torch.zeros((eh, ew * 2, 4), dtype=torch.uint8))
    monkeypatch.setattr(forward_warp_cuda, "synthesize_forward_packed_cuda", packed)
    synth = StereoSynthesizer(32, 18, 1, stereo_method="forward-cuda", fused_output=True, fused_validation=True)
    if reject:
        with pytest.raises(ValueError, match="finite and in"):
            synth.synthesize(source, depth, frame_id=7, generation=3)
    else:
        result = synth.synthesize(source, depth, frame_id=7, generation=3)
        assert result.validation_backend == "cuda-range"
        assert (result.frame_id, result.generation, result.content_rect) == (7, 3, (0, 0, 32, 18))
    assert called == [(0, 0, 32, 18)] and synth._stereo_packer is None


def test_cpu_float_colour_keeps_reference_and_reports_selection():
    source, depth = inputs()
    options = dict(stereo_method="forward", colour_precision="float", resize_filter="bicubic-aa")
    normal = StereoSynthesizer(32, 18, 1, **options).synthesize(source, depth, frame_id=7, generation=3)
    synth = StereoSynthesizer(32, 18, 1, **options, fused_colour_fit=True)
    candidate = synth.synthesize(source, depth, frame_id=7, generation=3)
    np.testing.assert_array_equal(candidate.bgra, normal.bgra)
    assert candidate.colour_fit_backend == "torch" and candidate.colour_fit_reason == "non_cuda_source"
    assert synth._colour_fitter is None


def test_original_2d_and_identity_checks_precede_new_gpu_helpers(monkeypatch):
    source, depth = inputs()
    def forbidden(*args, **kwargs):
        pytest.fail("2D or mismatched frame entered packed GPU code")
    monkeypatch.setattr(forward_warp_cuda, "synthesize_forward_packed_cuda", forbidden)
    synth = StereoSynthesizer(32, 18, 0, stereo_method="forward-cuda", fused_output=True,
        fused_validation=True, colour_precision="float", resize_filter="bicubic-aa", fused_colour_fit=True)
    with pytest.raises(ValueError, match="different RGB frame"):
        synth.synthesize(source, depth, frame_id=8, generation=3)
    result = synth.synthesize(source, depth, frame_id=7, generation=3)
    assert result.mode == "2d" and result.validation_backend == "not-used"
    assert synth._colour_fitter is None and synth._stereo_packer is None
    np.testing.assert_array_equal(result.bgra[:, :32], source.numpy())


def test_colour_cleanup_failure_keeps_owner_and_other_cleanup_runs(monkeypatch):
    closed = []
    class Owner:
        fail = True
        def close(self):
            closed.append("colour")
            if self.fail:
                raise RuntimeError("unfinished colour work")
    synth = StereoSynthesizer(stereo_method="forward-cuda")
    owner = synth._colour_fitter = Owner()
    monkeypatch.setattr(forward_warp_cuda, "close_cached_forward_warp_cuda", lambda: closed.append("forward"))
    with pytest.raises(RuntimeError, match="colour fit cleanup"):
        synth.close()
    assert synth._colour_fitter is owner and closed == ["colour", "forward"]
    owner.fail = False
    synth.close()
    assert synth._colour_fitter is None and closed == ["colour", "forward", "colour", "forward"]


@pytest.mark.parametrize("option,value", [("fused_validation", True), ("fused_validation", 1),
                                          ("fused_colour_fit", True), ("fused_colour_fit", "on")])
def test_invalid_preparation_modes_rejected_before_capture(tmp_path, option, value):
    with pytest.raises(ValueError, match="Fused"):
        StereoSynthesizer(**{option: value})
    flag = "fused_forward_validation" if option == "fused_validation" else option
    with pytest.raises(ValueError, match="Fused"):
        session.serve(SimpleNamespace(output=str(tmp_path), **{flag: value}))

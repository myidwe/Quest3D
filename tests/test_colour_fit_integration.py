"""CPU integration of explicit colour precision; no real capture/model/GPU."""

from dataclasses import replace
import hashlib
import json
import sys
import time
from types import SimpleNamespace

import numpy as np
import pytest
import torch

from quest3d import cli, colour_fit, forward_warp, session
from quest3d.capture import CapturedFrame
from quest3d.depth import DepthResult
from quest3d.geometry import ScreenRect, SourceGeometry
from quest3d.session_control import send_control
from quest3d.stereo import StereoSynthesizer


@pytest.fixture(autouse=True)
def _cpu_only(monkeypatch):
    previous = torch.get_num_threads()
    torch.set_num_threads(2)
    monkeypatch.setattr(torch.cuda, "_lazy_init", lambda: pytest.fail("GPU initialization is forbidden"))
    yield
    torch.set_num_threads(previous)


def _source():
    y, x = torch.meshgrid(torch.arange(48), torch.arange(64), indexing="ij")
    return torch.stack(((7*x+y)%256, (x+3*y)%256, (x*y)%256, torch.full_like(x, 255)), -1).to(torch.uint8)


def _depth(frame_id=7, generation=3):
    value = torch.linspace(0, 1, 9*13).reshape(1, 9, 13)
    return DepthResult(frame_id, generation, value, (9, 13), 0., 0.)


def _synth(**options):
    return StereoSynthesizer(48, 36, disparity_px=1, resize_filter="bicubic-aa", **options)


@pytest.mark.parametrize("kind,resize,method,expected", [
    ("tensor", "area", "backward", "38bec2c64a96b0393b5aab5b26d53079af3168d3523721a92ae231fc8d1c5ddf"),
    ("tensor", "area", "forward", "ff26d04a938c26d433d7aa43c7b81bdda49c7354dd62ff0b3c0a2db724d3273b"),
    ("tensor", "bicubic-aa", "backward", "d306d4904f502ad34ba4ca37946655167b78bbb683c5c996605d936e925e897c"),
    ("tensor", "bicubic-aa", "forward", "cae0bda5eaa5340e4e16c5458cd300732f4b845979fc08cb702f456ebc13141b"),
    ("numpy", "area", "backward", "a820344dc3bd5456353fe0a4648b9ee941acf2f77e37be5ebdfa78ba79f17425"),
    ("numpy", "area", "forward", "c8bf4886e48e8190c819d575265de9d8bcbee548178e13049a6bdba85c88d012"),
    ("numpy", "bicubic-aa", "backward", "d306d4904f502ad34ba4ca37946655167b78bbb683c5c996605d936e925e897c"),
    ("numpy", "bicubic-aa", "forward", "cae0bda5eaa5340e4e16c5458cd300732f4b845979fc08cb702f456ebc13141b"),
])
def test_default_output_matches_frozen_before_source_bytes(kind, resize, method, expected):
    # Generated with the unedited f85ab6ee... stereo.py and pinned Torch2.7.1
    # CPU2/OpenCV4.11 before this integration. No external artifact is required
    # to run the test; changing an expected hash needs a deliberate review.
    source = _source()
    synth = StereoSynthesizer(48, 36, disparity_px=1, resize_filter=resize, stereo_method=method)
    result = synth.synthesize(source if kind == "tensor" else source.numpy(), _depth(), frame_id=7, generation=3)
    assert hashlib.sha256(result.bgra.tobytes()).hexdigest() == expected


@pytest.mark.parametrize("method", ["backward", "forward"])
def test_default_and_explicit_uint8_preserve_existing_pixels_and_do_not_call_helper(monkeypatch, method):
    monkeypatch.setattr(colour_fit, "fit_bgra_float", lambda *a, **kw: pytest.fail("Float fit was not requested"))
    source, depth = _source(), _depth()
    implicit = _synth(stereo_method=method).synthesize(source, depth, frame_id=7, generation=3)
    explicit = _synth(stereo_method=method, colour_precision="uint8").synthesize(source, depth, frame_id=7, generation=3)
    np.testing.assert_array_equal(implicit.bgra, explicit.bgra)
    assert implicit.colour_precision == explicit.colour_precision == "uint8"
    assert implicit.colour_precision_reason is explicit.colour_precision_reason is None


def test_float_fit_reaches_actual_projector_with_identical_depth_and_original_storage(monkeypatch):
    source, depth = _source(), _depth()
    before_source, before_depth = source.clone(), depth.tensor.clone()
    seen = []
    real = forward_warp.synthesize_forward
    def record(image, inverse, *args):
        seen.append((image.clone(), inverse.clone()))
        return real(image, inverse, *args)
    monkeypatch.setattr(forward_warp, "synthesize_forward", record)
    baseline = _synth(stereo_method="forward")
    selected = _synth(stereo_method="forward", colour_precision="float")
    old = baseline.synthesize(source, depth, frame_id=7, generation=3)
    monkeypatch.setattr(selected, "_fit", lambda *a: pytest.fail("Float 3D still performs the old quantized fit"))
    result = selected.synthesize(source, depth, frame_id=7, generation=3)
    expected = colour_fit.fit_bgra_float(source, 48, 36)
    assert torch.equal(seen[1][0], expected.image)
    assert torch.equal(seen[0][1], seen[1][1])
    assert result.content_rect == old.content_rect == expected.content_rect
    assert result.colour_precision == "float" and result.colour_precision_reason is None
    assert result.frame_id == old.frame_id == 7 and result.generation == old.generation == 3
    assert torch.equal(source, before_source) and torch.equal(depth.tensor, before_depth)
    assert not torch.equal(seen[0][0], seen[1][0]), "fixture must exercise removed middle rounding"


@pytest.mark.parametrize("as_numpy", [False, True])
def test_original_2d_and_zero_disparity_never_run_float_fit(monkeypatch, as_numpy):
    source = _source().numpy() if as_numpy else _source()
    monkeypatch.setattr(colour_fit, "fit_bgra_float", lambda *a, **kw: pytest.fail("2D must remain independent"))
    selected = _synth(colour_precision="float")
    original = selected.original_2d(source, frame_id=7, generation=3)
    baseline = _synth().original_2d(source, frame_id=7, generation=3)
    selected.disparity_px = 0
    zero = selected.synthesize(source, _depth(), frame_id=7, generation=3)
    np.testing.assert_array_equal(original.bgra, baseline.bgra)
    np.testing.assert_array_equal(zero.bgra, baseline.bgra)
    assert zero.colour_precision == original.colour_precision == "uint8"
    assert zero.colour_precision_reason == original.colour_precision_reason == "original_2d"


@pytest.mark.parametrize("disparity", [0, 1])
@pytest.mark.parametrize("bad", [{"frame_id": 8}, {"generation": 4}])
def test_frame_binding_is_rejected_before_fit_even_for_zero_disparity(monkeypatch, disparity, bad):
    monkeypatch.setattr(colour_fit, "fit_bgra_float", lambda *a, **kw: pytest.fail("Invalid frame reached fit"))
    synth = _synth(colour_precision="float")
    synth.disparity_px = disparity
    with pytest.raises(ValueError, match="different RGB frame"):
        synth.synthesize(_source(), _depth(**bad), frame_id=7, generation=3)


@pytest.mark.parametrize("case,reason", [("filter", "unsupported_resize_filter"),
    ("numpy", "non_tensor_source"), ("dtype", "unsupported_source_dtype"),
    ("refinement", "depth_refinement_active")])
def test_unsupported_combinations_report_byte_identical_uint8_fallback(monkeypatch, case, reason):
    source = _source()
    options = {"resize_filter": "area" if case == "filter" else "bicubic-aa",
               "depth_refinement": "guided" if case == "refinement" else "none"}
    if case == "numpy":
        source = source.numpy()
    elif case == "dtype":
        source = source.float()
    monkeypatch.setattr(colour_fit, "fit_bgra_float", lambda *a, **kw: pytest.fail("Unsupported float fit ran"))
    baseline = StereoSynthesizer(48, 36, disparity_px=1, **options)
    selected = StereoSynthesizer(48, 36, disparity_px=1, colour_precision="float", **options)
    actual = selected.synthesize(source, _depth(), frame_id=7, generation=3)
    old = baseline.synthesize(source, _depth(), frame_id=7, generation=3)
    np.testing.assert_array_equal(actual.bgra, old.bgra)
    assert actual.colour_precision == "uint8" and actual.colour_precision_reason == reason


def test_device_mismatch_eligibility_is_metadata_only_without_gpu_or_transfer():
    # Only test the decision with a meta tensor; real cross-device rendering
    # belongs to root's hardware validation, not this CPU contract test.
    synth = _synth(colour_precision="float")
    assert synth._select_colour_precision(_source(), torch.empty(1, 9, 13, device="meta")) == (
        "uint8", "source_device_mismatch")


@pytest.mark.parametrize("bad", [None, True, "fp16", [], 1])
def test_unknown_precision_rejected(bad):
    with pytest.raises(ValueError, match="Colour precision"):
        _synth(colour_precision=bad)


@pytest.mark.parametrize("command", ["run", "serve"])
@pytest.mark.parametrize("precision", [None, "uint8", "float"])
def test_cli_exposes_explicit_choice_and_preserves_default(monkeypatch, command, precision):
    called = []
    monkeypatch.setattr(cli, "capture_pipeline", lambda args: called.append(args) or {})
    monkeypatch.setattr(session, "serve", lambda args: called.append(args) or {})
    argv = ["quest3d", command] + (["--colour-precision", precision] if precision else [])
    monkeypatch.setattr(sys, "argv", argv)
    assert cli.main() == 0
    assert called[0].colour_precision == (precision or "uint8")


@pytest.mark.parametrize("entry", [cli.capture_pipeline, session.serve])
@pytest.mark.parametrize("options", [{"inline_rect": "0,0,16,16"}, {"file_av_clock": True}])
def test_unintegrated_paths_reject_float_before_capture_or_output(entry, options, tmp_path):
    output = tmp_path / "uncreated"
    with pytest.raises(ValueError, match="Float colour precision requires"):
        entry(SimpleNamespace(output=str(output), colour_precision="float", **options))
    assert not output.exists()


@pytest.mark.parametrize("tensor_source", [False, True])
def test_session_reports_configured_effective_and_2d_while_ai_blocked(monkeypatch, tmp_path, tensor_source):
    import test_session
    from test_session import _SessionHarness, _wait
    real_serve, original_frame = session.serve, test_session._frame
    def select(args):
        args.colour_precision = "float"
        return real_serve(args)
    if tensor_source:
        def tensor_frame(*args, **kwargs):
            frame = original_frame(*args, **kwargs)
            return replace(frame, bgra=torch.from_numpy(frame.bgra.copy()))
        monkeypatch.setattr(test_session, "_frame", tensor_frame)
    monkeypatch.setattr(session, "serve", select)
    harness = _SessionHarness(monkeypatch, tmp_path, resize_filter="bicubic-aa")
    try:
        assert harness.saw_3d.wait(3) and harness.ai_blocked.wait(3)
        request = send_control(tmp_path, mode="2d")
        state = _wait(lambda: value if (value := harness.status()).get("applied_request") == request["request_id"] else None)
        assert state["colour_precision"] == "float" and state["effective_colour_precision"] == "uint8"
        assert state["colour_precision_reason"] == "original_2d"
        assert not harness.allow_ai.is_set()
    finally:
        harness.close()
    ai_rows = [json.loads(line) for line in (tmp_path / "ai/frames.jsonl").read_text().splitlines()]
    display = [json.loads(line) for line in (tmp_path / "presentation.jsonl").read_text().splitlines()]
    summary = json.loads((tmp_path / "ai/summary.json").read_text())
    expected = "float" if tensor_source else "uint8"
    reason = None if tensor_source else "non_tensor_source"
    assert ai_rows and all(row["colour_precision"] == "float" for row in ai_rows)
    assert all(row["effective_colour_precision"] == expected and row["colour_precision_reason"] == reason for row in ai_rows)
    assert any(row["mode"] == "3d" and row["effective_colour_precision"] == expected for row in display)
    assert all(row["colour_precision"] == "float" for row in display)
    assert summary["colour_precision"] == "float" and summary["error"] is None


@pytest.mark.parametrize("mode", ["2d", "3d"])
def test_capture_pipeline_records_actual_precision_without_real_capture(monkeypatch, tmp_path, mode):
    from quest3d import capture, cursor, depth
    source = _source()
    class Capture:
        dropped_frames = 0
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *exc): pass
        def grab(self):
            return CapturedFrame(source, time.perf_counter_ns(), "synthetic", 7, 3,
                SourceGeometry(ScreenRect(0, 0, 64, 48), 3))
    class Engine:
        def __init__(self, *args): pass
        def infer(self, image, *, frame_id, generation):
            assert image is source
            return _depth(frame_id, generation)
    class Cursor:
        def sample_and_composite(self, frame, source): return frame
        def snapshot(self): return {"enabled": False}
        def close(self): pass
    monkeypatch.setattr(capture, "DesktopCapture", Capture)
    monkeypatch.setattr(depth, "DepthEngine", Engine)
    monkeypatch.setattr(cursor.DesktopCursorOverlay, "from_capture", lambda *a, **kw: Cursor())
    for name in ("reset_peak_memory_stats", "memory_allocated", "max_memory_allocated", "max_memory_reserved"):
        monkeypatch.setattr(torch.cuda, name, lambda *a, **kw: 0)
    args = SimpleNamespace(output=str(tmp_path), stereo_method="forward", depth_refinement="none",
        colour_precision="float", resize_filter="bicubic-aa", cpu_threads=2, mode=mode,
        ai_size=280, fp32=True, eye_width=48, eye_height=36, disparity=1, publish=False,
        record=False, capture_backend="mss", monitor=1, rect=None, seconds=0, warmup=0, fps=30, hide_cursor=True)
    summary = cli.capture_pipeline(args)
    row = json.loads((tmp_path / "frames.jsonl").read_text().splitlines()[0])
    expected, reason = ("float", None) if mode == "3d" else ("uint8", "original_2d")
    for record in (row, summary):
        assert record["colour_precision"] == "float"
        assert record["effective_colour_precision"] == expected and record["colour_precision_reason"] == reason
    assert summary["error"] is None

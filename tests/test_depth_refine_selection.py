"""CPU-only selection/identity regressions; synthetic RGB/depth, no quality claim."""

from dataclasses import replace
import json
import sys
import time
from types import SimpleNamespace

import numpy as np
import pytest
import torch
import torch.nn.functional as F

from quest3d import cli, depth_refine, forward_warp, session
from quest3d.capture import CapturedFrame
from quest3d.geometry import ScreenRect, SourceGeometry
from quest3d.session_control import send_control
from quest3d.stereo import StereoSynthesizer
from test_stereo_method import _depth, _image


@pytest.fixture(autouse=True)
def _cpu_threads():
    old = torch.get_num_threads()
    torch.set_num_threads(2)
    yield
    torch.set_num_threads(old)


@pytest.mark.parametrize("method", ["backward", "forward"])
def test_none_is_bitwise_existing_path_and_does_not_call_guided(monkeypatch, method):
    def forbidden(*args, **kwargs):
        pytest.fail("none must retain the existing bilinear path")
    monkeypatch.setattr(depth_refine, "guided_upsample_depth", forbidden)
    source, depth = _image(), _depth()
    implicit = StereoSynthesizer(64, 36, disparity_px=2, stereo_method=method)
    explicit = StereoSynthesizer(64, 36, disparity_px=2, stereo_method=method, depth_refinement="none")
    a = implicit.synthesize(source, depth, frame_id=7, generation=3)
    b = explicit.synthesize(source, depth, frame_id=7, generation=3)
    assert implicit.depth_refinement == "none"
    np.testing.assert_array_equal(a.bgra, b.bgra)
    assert (a.content_rect, a.depth_range, a.scene_reset) == (b.content_rect, b.depth_range, b.scene_reset)


@pytest.mark.parametrize("tensor_input", [False, True])
@pytest.mark.parametrize("method", ["backward", "forward"])
def test_guided_receives_matching_fitted_rgb_and_normalized_low_depth_and_drives_both_eyes(
        monkeypatch, tensor_input, method):
    source = _image(47, 47)
    if tensor_input:
        source = torch.from_numpy(source)
    depth = _depth()
    guidance, projections, backward_grids = [], [], []
    refine = depth_refine.guided_upsample_depth
    project = forward_warp.synthesize_forward
    sample = F.grid_sample

    def record_guide(rgb, low, **options):
        result = refine(rgb, low, **options)
        guidance.append((rgb.clone(), low.clone(), options, result.clone()))
        return result

    def record_project(image, inverse_depth, disparity, convergence):
        projections.append(inverse_depth.clone())
        return project(image, inverse_depth, disparity, convergence)

    def record_sample(image, grid, **options):
        backward_grids.append(grid.clone())
        return sample(image, grid, **options)

    monkeypatch.setattr(depth_refine, "guided_upsample_depth", record_guide)
    monkeypatch.setattr(forward_warp, "synthesize_forward", record_project)
    monkeypatch.setattr(F, "grid_sample", record_sample)
    synth = StereoSynthesizer(64, 36, disparity_px=2, stereo_method=method,
                              resize_filter="bicubic-aa", depth_refinement="guided")
    before = source.clone() if tensor_input else source.copy()
    output = synth.synthesize(source, depth, frame_id=7, generation=3)
    assert len(guidance) == 1
    rgb, normalized, options, refined = guidance[0]
    fitted, _ = synth._fit(source)
    bgr = fitted[:, :, :3] if tensor_input else torch.from_numpy(fitted[:, :, :3].copy())
    expected_rgb = bgr.permute(2, 0, 1)[None].float().flip(1) / 255
    torch.testing.assert_close(rgb, expected_rgb, rtol=0, atol=0)
    torch.testing.assert_close(normalized, (depth.tensor - 2) / 4)
    assert rgb.shape == (1, 3, 36, 36) and normalized.shape == (1, 7, 11)
    assert refined.shape == (36, 36) and refined.device.type == "cpu"
    assert options == {"radius": 2, "epsilon": 0.01}
    if method == "forward":
        assert not backward_grids and len(projections) == 1
        torch.testing.assert_close(projections[0], refined.float(), rtol=0, atol=0)
    else:
        assert not projections and len(backward_grids) == 1
        grid = backward_grids[0]
        base_x = torch.linspace(-1, 1, 36)[None, :].expand(36, 36)
        shift = (refined - 0.5) * 2 / 35
        torch.testing.assert_close(grid[0, :, :, 0], base_x - shift)
        torch.testing.assert_close(grid[1, :, :, 0], base_x + shift)
    assert output.bgra.shape == (36, 128, 4) and output.content_rect == (14, 0, 36, 36)
    assert output.mode == "3d" and output.frame_id == 7 and output.generation == 3
    for eye in (output.bgra[:, :64], output.bgra[:, 64:]):
        assert not np.any(eye[:, :14, :3]) and not np.any(eye[:, 50:, :3])
        assert np.all(eye[:, :, 3] == 255)
    if tensor_input:
        assert torch.equal(source, before)
    else:
        np.testing.assert_array_equal(source, before)


@pytest.mark.parametrize("method", ["backward", "forward", "forward-cuda"])
def test_guided_zero_disparity_remains_original_2d_without_refining(monkeypatch, method):
    def forbidden(*args, **kwargs):
        pytest.fail("original 2D must not call guided refinement")
    monkeypatch.setattr(depth_refine, "guided_upsample_depth", forbidden)
    synth = StereoSynthesizer(64, 36, disparity_px=0, stereo_method=method, depth_refinement="guided")
    source = _image(41, 53)
    actual = synth.synthesize(source, _depth(), frame_id=7, generation=3)
    expected = synth.original_2d(source, frame_id=7, generation=3)
    np.testing.assert_array_equal(actual.bgra, expected.bgra)
    assert actual.mode == "2d" and actual.depth_range is None


@pytest.mark.parametrize("disparity", [0, 2])
@pytest.mark.parametrize("change", [{"frame_id": 8}, {"generation": 4}])
def test_refinement_cannot_bypass_frame_or_generation_binding(monkeypatch, disparity, change):
    def forbidden(*args, **kwargs):
        pytest.fail("refinement ran before identity validation")
    monkeypatch.setattr(depth_refine, "guided_upsample_depth", forbidden)
    with pytest.raises(ValueError, match="different RGB frame or geometry"):
        StereoSynthesizer(64, 36, disparity_px=disparity, depth_refinement="guided").synthesize(
            _image(), replace(_depth(), **change), frame_id=7, generation=3)


@pytest.mark.parametrize("method", ["backward", "forward"])
@pytest.mark.parametrize("disparity", [0, 2])
def test_guided_flat_mask_is_rejected_before_any_refinement(monkeypatch, method, disparity):
    def forbidden(*args, **kwargs):
        pytest.fail("unsupported flat mask reached refinement")
    monkeypatch.setattr(depth_refine, "guided_upsample_depth", forbidden)
    synth = StereoSynthesizer(64, 36, disparity_px=disparity, stereo_method=method, depth_refinement="guided")
    with pytest.raises(ValueError, match="Guided depth refinement does not support flat_mask"):
        synth.synthesize(_image(), _depth(), frame_id=7, generation=3, flat_mask=np.zeros((36, 64), dtype=bool))


@pytest.mark.parametrize("value", [None, "bilinear", "GUIDED", True, 2])
def test_refinement_selection_rejects_invalid_values(value):
    with pytest.raises(ValueError, match="Depth refinement"):
        StereoSynthesizer(depth_refinement=value)


@pytest.mark.parametrize("command", ["run", "serve"])
@pytest.mark.parametrize("selection", [None, "none", "guided"])
def test_cli_refinement_selection_and_default(monkeypatch, command, selection, capsys):
    called = []
    arguments = ["quest3d", command]
    if selection is not None:
        arguments += ["--depth-refinement", selection]
    monkeypatch.setattr(sys, "argv", arguments)
    monkeypatch.setattr(cli, "capture_pipeline", lambda args: called.append(args) or {})
    monkeypatch.setattr(session, "serve", lambda args: called.append(args) or {})
    assert cli.main() == 0 and len(called) == 1
    assert called[0].depth_refinement == (selection or "none")


@pytest.mark.parametrize("command", ["run", "serve"])
def test_cli_guided_inline_rejected_before_dispatch(monkeypatch, command, capsys):
    def forbidden(*args):
        pytest.fail("unsupported guided route started")
    monkeypatch.setattr(sys, "argv", ["quest3d", command, "--depth-refinement", "guided",
                                      "--inline-rect", "0,0,64,36"])
    monkeypatch.setattr(cli, "capture_pipeline", forbidden)
    monkeypatch.setattr(session, "serve", forbidden)
    with pytest.raises(SystemExit) as failure:
        cli.main()
    assert failure.value.code == 2 and "Guided depth refinement" in capsys.readouterr().err


@pytest.mark.parametrize("entry", [cli.capture_pipeline, session.serve])
@pytest.mark.parametrize("combination", [{"inline_rect": "0,0,64,36"}, {"file_av_clock": True}])
def test_direct_unsupported_guided_options_rejected_before_resources(entry, combination, tmp_path):
    output = tmp_path / "must-not-exist"
    args = SimpleNamespace(output=str(output), depth_refinement="guided", **combination)
    with pytest.raises(ValueError, match="Guided depth refinement requires"):
        entry(args)
    assert not output.exists()


@pytest.mark.parametrize("selection,method", [(None, "backward"), ("guided", "backward"), ("guided", "forward")])
def test_actual_session_selection_metrics_and_2d_control_while_ai_blocked(monkeypatch, tmp_path, selection, method):
    from test_session import _SessionHarness, _wait
    real_serve = session.serve
    real_refine = depth_refine.guided_upsample_depth
    refinements = []

    def select(args):
        args.stereo_method = method
        if selection is not None:
            args.depth_refinement = selection
        return real_serve(args)

    def record(rgb, low, **options):
        value = real_refine(rgb, low, **options)
        refinements.append(value.shape)
        return value

    monkeypatch.setattr(session, "serve", select)
    monkeypatch.setattr(depth_refine, "guided_upsample_depth", record)
    harness = _SessionHarness(monkeypatch, tmp_path)
    try:
        assert harness.saw_3d.wait(3) and harness.ai_blocked.wait(3)
        before = len(refinements)
        assert bool(before) == (selection == "guided")
        request = send_control(tmp_path, mode="2d")
        state = _wait(lambda: s if (s := harness.status()).get("applied_request") == request["request_id"] else None)
        assert state["effective_mode"] == "2d" and state["depth_refinement"] == (selection or "none")
        assert state["stereo_method"] == method and not harness.allow_ai.is_set()
        assert len(refinements) == before, "independent original 2D must not wait for or run refinement"
        image = harness.published[-1][0]
        np.testing.assert_array_equal(image[:, :320], image[:, 320:])
    finally:
        harness.close()
    ai_rows = [json.loads(line) for line in (tmp_path / "ai/frames.jsonl").read_text().splitlines()]
    presentation = [json.loads(line) for line in (tmp_path / "presentation.jsonl").read_text().splitlines()]
    summary = json.loads((tmp_path / "ai/summary.json").read_text())
    assert ai_rows and presentation
    assert all(row["depth_refinement"] == (selection or "none") for row in ai_rows + presentation)
    assert summary["depth_refinement"] == (selection or "none")
    assert not harness.status()["running"] and harness.publisher_closed


def test_run_refinement_reaches_actual_cpu_pipeline_and_metrics(monkeypatch, tmp_path):
    from quest3d import capture, cursor, depth

    class Capture:
        dropped_frames = 0
        def __init__(self, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            pass
        def grab(self):
            return CapturedFrame(_image(), time.perf_counter_ns(), "synthetic", 7, 3,
                                 SourceGeometry(ScreenRect(0, 0, 64, 36), 3))

    class Engine:
        def __init__(self, *args):
            pass
        def infer(self, image, *, frame_id, generation):
            return _depth(frame_id, generation)

    class Cursor:
        def sample_and_composite(self, frame, source):
            return frame
        def snapshot(self):
            return {"enabled": False}
        def close(self):
            pass

    monkeypatch.setattr(capture, "DesktopCapture", Capture)
    monkeypatch.setattr(depth, "DepthEngine", Engine)
    monkeypatch.setattr(cursor.DesktopCursorOverlay, "from_capture", lambda *a, **kw: Cursor())
    for name in ("reset_peak_memory_stats", "memory_allocated", "max_memory_allocated", "max_memory_reserved"):
        monkeypatch.setattr(torch.cuda, name, lambda *a, **kw: 0)
    args = SimpleNamespace(output=str(tmp_path), stereo_method="backward", depth_refinement="guided",
        resize_filter="area", cpu_threads=2, mode="3d", ai_size=280, fp32=True,
        eye_width=64, eye_height=36, disparity=2, publish=False, record=False,
        capture_backend="mss", monitor=1, rect=None, seconds=0, warmup=0, fps=30, hide_cursor=True)
    summary = cli.capture_pipeline(args)
    row = json.loads((tmp_path / "frames.jsonl").read_text().splitlines()[0])
    assert summary["error"] is None and summary["depth_refinement"] == "guided"
    assert row["mode"] == "3d" and row["depth_refinement"] == "guided"
    assert row["stereo_method"] == "backward"

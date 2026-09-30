"""CPU integration of explicit edge choices; CUDA entry points are test doubles.

No actual capture, model, GPU, compositor, or visual-quality claim is made here.
"""

import builtins
from dataclasses import replace
from functools import partial
import json
import sys
import time
from types import ModuleType, SimpleNamespace

import numpy as np
import pytest
import torch
import torch.nn.functional as F

from quest3d import cli, depth_edges, forward_warp, session
from quest3d.capture import CapturedFrame
from quest3d.depth import DepthResult
from quest3d.geometry import ScreenRect, SourceGeometry
from quest3d.session_control import send_control
from quest3d.stereo import StereoSynthesizer
from test_stereo_method import _depth, _image


@pytest.fixture(autouse=True)
def _cpu_only(monkeypatch):
    previous = torch.get_num_threads()
    torch.set_num_threads(2)
    monkeypatch.setattr(torch.cuda, "_lazy_init", lambda: pytest.fail("GPU initialization is forbidden"))
    yield
    torch.set_num_threads(previous)


def _cuda_fakes(monkeypatch, *, refine=None, project=None, close_edge=None, close_warp=None):
    def forbidden(*args, **kwargs):
        pytest.fail("Unrequested CUDA operation")

    edge = ModuleType("quest3d.depth_edges_cuda")
    edge.edge_aware_upsample_depth_cuda = refine or forbidden
    edge.close_cached_depth_edges_cuda = close_edge or (lambda: None)
    warp = ModuleType("quest3d.forward_warp_cuda")
    warp.synthesize_forward_cuda = project or forbidden
    warp.close_cached_forward_warp_cuda = close_warp or (lambda: None)
    monkeypatch.setitem(sys.modules, edge.__name__, edge)
    monkeypatch.setitem(sys.modules, warp.__name__, warp)


def _forbid_refiner_imports(monkeypatch):
    original = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name.rsplit(".", 1)[-1] in ("depth_edges", "depth_edges_cuda", "forward_warp_cuda"):
            pytest.fail(f"Lazy path imported {name}")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)


@pytest.mark.parametrize("method", ["backward", "forward"])
def test_default_none_retains_existing_bytes_without_loading_edges(monkeypatch, method):
    _forbid_refiner_imports(monkeypatch)
    source, depth = _image(), _depth()
    implicit = StereoSynthesizer(64, 36, disparity_px=2, stereo_method=method)
    explicit = StereoSynthesizer(64, 36, disparity_px=2, stereo_method=method, depth_refinement="none")
    a = implicit.synthesize(source, depth, frame_id=7, generation=3)
    b = explicit.synthesize(source, depth, frame_id=7, generation=3)
    assert implicit.depth_refinement == "none"
    np.testing.assert_array_equal(a.bgra, b.bgra)
    assert (a.content_rect, a.scene_reset, a.depth_range) == (b.content_rect, b.scene_reset, b.depth_range)


@pytest.mark.parametrize("selection", ["edge-aware", "edge-cuda"])
@pytest.mark.parametrize("method", ["backward", "forward", "forward-cuda"])
def test_constructor_original_2d_and_zero_disparity_do_not_load_refinement(monkeypatch, selection, method):
    _forbid_refiner_imports(monkeypatch)
    synth = StereoSynthesizer(64, 36, disparity_px=0, stereo_method=method, depth_refinement=selection)
    source = _image(47, 47)
    original = synth.original_2d(source, frame_id=7, generation=3)
    zero = synth.synthesize(source, _depth(), frame_id=7, generation=3)
    np.testing.assert_array_equal(zero.bgra, original.bgra)
    assert zero.mode == "2d" and zero.depth_range is None
    assert zero.depth_refinement == original.depth_refinement == "none"
    assert zero.depth_refinement_reason is None


@pytest.mark.parametrize("selection", ["edge-aware", "edge-cuda"])
@pytest.mark.parametrize("disparity", [0, 2])
@pytest.mark.parametrize("change", [{"frame_id": 8}, {"generation": 4}])
def test_edge_cannot_bypass_exact_rgb_depth_identity(monkeypatch, selection, disparity, change):
    _forbid_refiner_imports(monkeypatch)
    with pytest.raises(ValueError, match="different RGB frame or geometry"):
        StereoSynthesizer(64, 36, disparity_px=disparity, depth_refinement=selection).synthesize(
            _image(), replace(_depth(), **change), frame_id=7, generation=3)


@pytest.mark.parametrize("selection", ["edge-aware", "edge-cuda"])
@pytest.mark.parametrize("disparity", [0, 2])
def test_flat_mask_rejected_before_edge_import(monkeypatch, selection, disparity):
    _forbid_refiner_imports(monkeypatch)
    with pytest.raises(ValueError, match="flat_mask"):
        StereoSynthesizer(64, 36, disparity_px=disparity, depth_refinement=selection).synthesize(
            _image(), _depth(), frame_id=7, generation=3, flat_mask=np.zeros((36, 64), dtype=bool))


@pytest.mark.parametrize("selection", ["edge-aware", "edge-cuda"])
@pytest.mark.parametrize("method", ["backward", "forward", "forward-cuda"])
@pytest.mark.parametrize("tensor_input", [False, True])
def test_exact_rgb_normalization_refined_field_and_aspect_reach_both_eyes(
        monkeypatch, selection, method, tensor_input):
    source = _image(47, 47)
    source = torch.from_numpy(source) if tensor_input else source
    before = source.clone() if tensor_input else source.copy()
    depth = _depth(dtype=torch.float64)
    refined_calls, projection_calls, grids = [], [], []
    real_project, real_sample = forward_warp.synthesize_forward, F.grid_sample

    def refine(rgb, low):
        # Deliberately different from bilinear, so silently ignoring the refiner
        # cannot satisfy the projector/grid assertions below.
        result = torch.linspace(.2, .7, rgb.shape[-1])[None].expand(rgb.shape[-2], -1).clone()
        refined_calls.append((rgb.clone(), low.clone(), result))
        return result

    def project(image, inverse_depth, disparity, convergence):
        projection_calls.append((image.clone(), inverse_depth.clone(), disparity, convergence))
        return real_project(image, inverse_depth, disparity, convergence)

    def sample(image, grid, **kwargs):
        grids.append(grid.clone())
        return real_sample(image, grid, **kwargs)

    monkeypatch.setattr(depth_edges, "edge_aware_upsample_depth", refine)
    monkeypatch.setattr(forward_warp, "synthesize_forward", project)
    monkeypatch.setattr(F, "grid_sample", sample)
    _cuda_fakes(monkeypatch, refine=refine, project=project)
    synth = StereoSynthesizer(64, 36, disparity_px=2, convergence=.4,
                              resize_filter="bicubic-aa", stereo_method=method, depth_refinement=selection)
    output = synth.synthesize(source, depth, frame_id=7, generation=3)
    assert len(refined_calls) == 1
    rgb, normalized, refined = refined_calls[0]
    fitted, _ = synth._fit(source)
    fitted = fitted if tensor_input else torch.from_numpy(fitted.copy())
    expected_bgr = fitted[:, :, :3].permute(2, 0, 1)[None].float() / 255
    torch.testing.assert_close(rgb, expected_bgr.flip(1), rtol=0, atol=0)
    torch.testing.assert_close(normalized, ((depth.tensor - 2) / 4).float(), rtol=0, atol=0)
    assert rgb.shape == (1, 3, 36, 36) and normalized.shape == (1, 7, 11)
    assert rgb.dtype == normalized.dtype == torch.float32
    if method == "backward":
        assert not projection_calls and len(grids) == 1
        base_x = torch.linspace(-1, 1, 36)[None].expand(36, 36)
        shift = (refined - .4) * 2 / 35
        torch.testing.assert_close(grids[0][0, :, :, 0], base_x - shift)
        torch.testing.assert_close(grids[0][1, :, :, 0], base_x + shift)
    else:
        assert not grids and len(projection_calls) == 1
        image, actual_depth, disparity, convergence = projection_calls[0]
        torch.testing.assert_close(image, expected_bgr, rtol=0, atol=0)
        torch.testing.assert_close(actual_depth, refined, rtol=0, atol=0)
        assert disparity == 2 and convergence == .4
    assert output.content_rect == (14, 0, 36, 36) and output.bgra.shape == (36, 128, 4)
    assert output.frame_id == 7 and output.generation == 3 and output.mode == "3d"
    assert output.depth_refinement == selection and output.depth_refinement_reason is None
    for eye in (output.bgra[:, :64], output.bgra[:, 64:]):
        assert not np.any(eye[:, :14, :3]) and not np.any(eye[:, 50:, :3])
        assert np.all(eye[:, :, 3] == 255)
    if tensor_input:
        assert torch.equal(source, before)
    else:
        np.testing.assert_array_equal(source, before)


@pytest.mark.parametrize("selection", ["edge-aware", "edge-cuda"])
@pytest.mark.parametrize("source_size,low_size", [((128, 16), (12, 20)), ((16, 128), (20, 12))])
@pytest.mark.parametrize("method", ["backward", "forward"])
def test_one_downsampled_axis_preserves_bilinear_bytes_and_reports_reason(
        monkeypatch, selection, source_size, low_size, method):
    _forbid_refiner_imports(monkeypatch)
    source = _image(*source_size)
    low = torch.linspace(2, 6, low_size[0] * low_size[1]).reshape(1, *low_size)
    depth = DepthResult(7, 3, low, low_size, 0, 0)
    baseline = StereoSynthesizer(64, 64, disparity_px=2, stereo_method=method).synthesize(
        source, depth, frame_id=7, generation=3)
    actual = StereoSynthesizer(64, 64, disparity_px=2, stereo_method=method,
                               depth_refinement=selection).synthesize(source, depth, frame_id=7, generation=3)
    np.testing.assert_array_equal(actual.bgra, baseline.bgra)
    assert actual.content_rect == baseline.content_rect
    assert actual.depth_refinement == "none" and actual.depth_refinement_reason == "downsampled_content"


@pytest.mark.parametrize("command", ["run", "serve"])
@pytest.mark.parametrize("selection", [None, "none", "edge-aware", "edge-cuda"])
def test_cli_defaults_and_explicit_edge_dispatch(monkeypatch, command, selection):
    arguments = ["quest3d", command] + (["--depth-refinement", selection] if selection else [])
    called = []
    monkeypatch.setattr(sys, "argv", arguments)
    monkeypatch.setattr(cli, "capture_pipeline", lambda args: called.append(args) or {})
    monkeypatch.setattr(session, "serve", lambda args: called.append(args) or {})
    assert cli.main() == 0 and len(called) == 1
    assert called[0].depth_refinement == (selection or "none")


@pytest.mark.parametrize("selection", ["edge-aware", "edge-cuda"])
@pytest.mark.parametrize("command,options", [
    ("run", ["--inline-rect", "0,0,64,36"]),
    ("serve", ["--inline-rect", "0,0,64,36"]),
    ("serve", ["--file", "unopened.mp4", "--file-av-clock"]),
])
def test_cli_exclusions_fail_before_dispatch(monkeypatch, capsys, selection, command, options):
    def forbidden(*args):
        pytest.fail("Excluded source path was started")
    monkeypatch.setattr(sys, "argv", ["quest3d", command, "--depth-refinement", selection, *options])
    monkeypatch.setattr(cli, "capture_pipeline", forbidden)
    monkeypatch.setattr(session, "serve", forbidden)
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2 and "enlarged desktop" in capsys.readouterr().err


@pytest.mark.parametrize("selection", ["edge-aware", "edge-cuda"])
@pytest.mark.parametrize("entry", [cli.capture_pipeline, session.serve])
@pytest.mark.parametrize("options", [{"inline_rect": "0,0,64,36"}, {"file_av_clock": True}])
def test_direct_exclusions_fail_before_creating_output(entry, selection, options, tmp_path):
    output = tmp_path / "must-not-exist"
    with pytest.raises(ValueError, match="enlarged desktop"):
        entry(SimpleNamespace(output=str(output), depth_refinement=selection, **options))
    assert not output.exists()


@pytest.mark.parametrize("edge_fails,warp_fails", [(True, False), (False, True), (True, True)])
def test_close_attempts_both_owners_reports_each_failure_and_allows_retry(monkeypatch, edge_fails, warp_fails):
    calls = []
    failures = {"edge": edge_fails, "warp": warp_fails}

    def close(name):
        calls.append(name)
        if failures[name]:
            raise RuntimeError(f"{name} completion unconfirmed")

    _cuda_fakes(monkeypatch, close_edge=lambda: close("edge"), close_warp=lambda: close("warp"))
    synth = StereoSynthesizer(depth_refinement="edge-cuda", stereo_method="forward-cuda")
    with pytest.raises(RuntimeError) as exc:
        synth.close()
    assert calls == ["edge", "warp"]
    for name, failed in failures.items():
        if failed:
            assert f"{name} completion unconfirmed" in str(exc.value)
    failures.update(edge=False, warp=False)
    synth.close()
    assert calls == ["edge", "warp", "edge", "warp"]


@pytest.mark.parametrize("selection", ["edge-aware", "edge-cuda"])
@pytest.mark.parametrize("control", [{"mode": "2d"}, {"mode": "3d", "disparity": 0}])
def test_actual_worker_telemetry_and_original_2d_remain_independent_of_blocked_ai(
        monkeypatch, tmp_path, selection, control):
    from test_session import _SessionHarness, _wait
    real_serve, real_refine = session.serve, depth_edges.edge_aware_upsample_depth
    refinements, closes = [], []

    def refine(rgb, low):
        value = real_refine(rgb, low)
        refinements.append(value.shape)
        return value

    def serve(args):
        args.depth_refinement = selection
        args.stereo_method = "forward-cuda"
        return real_serve(args)

    monkeypatch.setattr(session, "serve", serve)
    monkeypatch.setattr(depth_edges, "edge_aware_upsample_depth", refine)
    _cuda_fakes(monkeypatch, refine=refine, project=forward_warp.synthesize_forward,
                close_edge=lambda: closes.append("edge"), close_warp=lambda: closes.append("warp"))
    harness = _SessionHarness(monkeypatch, tmp_path)
    try:
        assert harness.saw_3d.wait(3) and harness.ai_blocked.wait(3)
        before = len(refinements)
        assert before > 0
        request = send_control(tmp_path, **control)
        state = _wait(lambda: value if (value := harness.status()).get("applied_request") == request["request_id"] else None)
        assert state["effective_mode"] == "2d" and state["depth_refinement"] == selection
        assert state["effective_depth_refinement"] == "none" and state["depth_refinement_reason"] is None
        assert not harness.allow_ai.is_set() and len(refinements) == before
        image = harness.published[-1][0]
        np.testing.assert_array_equal(image[:, :320], image[:, 320:])
    finally:
        harness.close()
    ai_rows = [json.loads(line) for line in (tmp_path / "ai/frames.jsonl").read_text().splitlines()]
    presentations = [json.loads(line) for line in (tmp_path / "presentation.jsonl").read_text().splitlines()]
    summary = json.loads((tmp_path / "ai/summary.json").read_text())
    assert ai_rows and presentations and all(row["depth_refinement"] == selection for row in ai_rows + presentations)
    assert all(row["effective_depth_refinement"] == selection for row in ai_rows)
    for row in presentations:
        assert row["effective_depth_refinement"] == (selection if row["mode"] == "3d" else "none")
        assert row["depth_refinement_reason"] is None
    assert summary["depth_refinement"] == selection and summary["error"] is None
    assert closes == (["edge", "warp"] if selection == "edge-cuda" else ["warp"])
    assert harness.publisher_closed and not harness.status()["running"]


@pytest.mark.parametrize("selection", ["edge-aware", "edge-cuda"])
def test_actual_worker_reports_downsample_fallback_without_edge_call(monkeypatch, tmp_path, selection):
    from test_session import _wait

    class Engine:
        def __init__(self, *args):
            pass
        def infer(self, image, *, frame_id, generation):
            tensor = torch.linspace(2, 6, 12 * 20).reshape(1, 12, 20)
            return DepthResult(frame_id, generation, tensor, (12, 20), 0, 0)

    def forbidden(*args, **kwargs):
        pytest.fail("Downsampled content reached edge refinement")

    monkeypatch.setattr(depth_edges, "edge_aware_upsample_depth", forbidden)
    _cuda_fakes(monkeypatch)
    worker = session.LatestAIWorker(eye_width=64, eye_height=64, ai_size=280, directory=tmp_path,
        engine_factory=Engine, synth_factory=partial(StereoSynthesizer, depth_refinement=selection))
    source = CapturedFrame(_image(128, 16), time.perf_counter_ns(), "synthetic", 7, 3,
                           SourceGeometry(ScreenRect(0, 0, 128, 16), 3))
    try:
        worker.submit(source, 5, 2)
        result = _wait(lambda: worker.snapshot()[0])
        assert result.source is source and result.revision == 5
        assert result.stereo.depth_refinement == "none"
        assert result.stereo.depth_refinement_reason == "downsampled_content"
    finally:
        worker.close()
    row = json.loads((tmp_path / "frames.jsonl").read_text().splitlines()[0])
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert row["depth_refinement"] == selection and row["effective_depth_refinement"] == "none"
    assert row["depth_refinement_reason"] == "downsampled_content"
    assert row["frame_id"] == 7 and row["capture_ns"] == source.captured_ns and row["revision"] == 5
    assert summary["error"] is None and not worker.thread.is_alive()


@pytest.mark.parametrize("selection", ["edge-aware", "edge-cuda"])
@pytest.mark.parametrize("mode", ["2d", "3d"])
def test_actual_run_pipeline_selection_and_lazy_2d(monkeypatch, tmp_path, selection, mode):
    from quest3d import capture, cursor, depth
    refinements, closes, inferences = [], [], []
    real_refine = depth_edges.edge_aware_upsample_depth

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
            assert mode == "3d"
        def infer(self, image, *, frame_id, generation):
            inferences.append((frame_id, generation))
            return _depth(frame_id, generation)

    class Cursor:
        def sample_and_composite(self, frame, source):
            return frame
        def snapshot(self):
            return {"enabled": False}
        def close(self):
            pass

    def refine(rgb, low):
        refinements.append((rgb.shape, low.shape))
        return real_refine(rgb, low)

    monkeypatch.setattr(capture, "DesktopCapture", Capture)
    monkeypatch.setattr(depth, "DepthEngine", Engine)
    monkeypatch.setattr(depth_edges, "edge_aware_upsample_depth", refine)
    monkeypatch.setattr(cursor.DesktopCursorOverlay, "from_capture", lambda *a, **kw: Cursor())
    _cuda_fakes(monkeypatch, refine=refine, project=forward_warp.synthesize_forward,
                close_edge=lambda: closes.append("edge"), close_warp=lambda: closes.append("warp"))
    for name in ("reset_peak_memory_stats", "memory_allocated", "max_memory_allocated", "max_memory_reserved"):
        monkeypatch.setattr(torch.cuda, name, lambda *a, **kw: 0)
    args = SimpleNamespace(output=str(tmp_path), stereo_method="forward-cuda", depth_refinement=selection,
        resize_filter="area", cpu_threads=2, mode=mode, ai_size=280, fp32=True,
        eye_width=64, eye_height=36, disparity=2, publish=False, record=False,
        capture_backend="mss", monitor=1, rect=None, seconds=0, warmup=0, fps=30, hide_cursor=True)
    summary = cli.capture_pipeline(args)
    row = json.loads((tmp_path / "frames.jsonl").read_text().splitlines()[0])
    assert bool(refinements) == bool(inferences) == (mode == "3d")
    assert row["mode"] == mode and row["depth_refinement"] == selection
    assert row["effective_depth_refinement"] == (selection if mode == "3d" else "none")
    assert row["depth_refinement_reason"] is None
    assert summary["error"] is None and not summary["cleanup_errors"]
    assert summary["depth_refinement"] == selection
    assert closes == (["edge", "warp"] if selection == "edge-cuda" else ["warp"])

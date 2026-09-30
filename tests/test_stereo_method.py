"""CPU integration of the explicit forward experiment; no actual capture/model/GPU."""

from dataclasses import replace
import json
from types import SimpleNamespace
import sys
import time

import cv2
import numpy as np
import pytest
import torch

from quest3d import cli, forward_warp, session
from quest3d.capture import CapturedFrame
from quest3d.depth import DepthResult
from quest3d.geometry import ScreenRect, SourceGeometry
from quest3d.session_control import send_control
from quest3d.stereo import StereoSynthesizer


@pytest.fixture(autouse=True)
def _cpu_threads():
    old = torch.get_num_threads()
    torch.set_num_threads(2)
    yield
    torch.set_num_threads(old)


def _image(width=64, height=36):
    y, x = np.indices((height, width))
    return np.stack(((x * 3 + y) % 256, (x + y * 2) % 256,
                     (x * 7 + y * 5) % 256, np.full_like(x, 255)), axis=-1).astype(np.uint8)


def _depth(frame_id=7, generation=3, dtype=torch.float32):
    tensor = torch.linspace(2, 6, 7 * 11, dtype=dtype).reshape(1, 7, 11)
    return DepthResult(frame_id, generation, tensor, (7, 11), 0, 0)


def test_default_and_explicit_backward_are_identical():
    source, depth = _image(), _depth()
    default = StereoSynthesizer(64, 36, disparity_px=2)
    explicit = StereoSynthesizer(64, 36, disparity_px=2, stereo_method="backward")
    assert default.stereo_method == explicit.stereo_method == "backward"
    a = default.synthesize(source, depth, frame_id=7, generation=3)
    b = explicit.synthesize(source, depth, frame_id=7, generation=3)
    np.testing.assert_array_equal(a.bgra, b.bgra)
    assert (a.mode, a.depth_range, a.content_rect, a.scene_reset) == (
        b.mode, b.depth_range, b.content_rect, b.scene_reset)


@pytest.mark.parametrize("method", [None, "guided", "FORWARD", 0, True])
def test_unknown_method_is_rejected(method):
    with pytest.raises(ValueError, match="Stereo method"):
        StereoSynthesizer(stereo_method=method)


@pytest.mark.parametrize("tensor_input", [False, True])
@pytest.mark.parametrize("resize_filter", ["area", "bicubic-aa"])
def test_forward_receives_fitted_color_and_corner_aligned_float_depth_then_packs(monkeypatch, tensor_input, resize_filter):
    source = _image(47, 47)
    if tensor_input:
        source = torch.from_numpy(source)
    depth = _depth(dtype=torch.float64)
    calls = []
    actual_project = forward_warp.synthesize_forward

    def record(image, inverse_depth, disparity, convergence):
        result = actual_project(image, inverse_depth, disparity, convergence)
        calls.append((image.clone(), inverse_depth.clone(), disparity, convergence, result))
        return result

    monkeypatch.setattr(forward_warp, "synthesize_forward", record)
    synth = StereoSynthesizer(64, 36, disparity_px=2, convergence=0.4,
                              resize_filter=resize_filter, stereo_method="forward")
    output = synth.synthesize(source, depth, frame_id=7, generation=3)
    assert len(calls) == 1
    image, inverse, disparity, convergence, result = calls[0]
    assert image.shape == (1, 3, 36, 36) and inverse.shape == (36, 36)
    assert image.dtype == inverse.dtype == torch.float32
    assert image.device.type == inverse.device.type == "cpu"
    assert disparity == 2 and convergence == 0.4
    assert inverse[0, 0] == 0 and inverse[-1, -1] == 1
    # This low depth is a plane. Endpoint-aligned interpolation must retain its
    # independent horizontal/vertical contributions on the rectangular grid.
    expected = (torch.linspace(0, 66, 36)[:, None] + torch.linspace(0, 10, 36)[None, :]) / 76
    torch.testing.assert_close(inverse, expected)
    assert output.bgra.shape == (36, 128, 4) and output.content_rect == (14, 0, 36, 36)
    expected_eyes = (result.eyes * 255).round().to(torch.uint8).permute(0, 2, 3, 1).numpy()
    for number, eye in enumerate((output.bgra[:, :64], output.bgra[:, 64:])):
        assert np.all(eye[:, :, 3] == 255)
        assert not np.any(eye[:, :14, :3]) and not np.any(eye[:, 50:, :3])
        np.testing.assert_array_equal(eye[:, 14:50, :3], expected_eyes[number])
    assert output.frame_id == 7 and output.generation == 3 and output.mode == "3d"
    assert output.scene_reset and output.depth_range == (2.0, 6.0)


def test_actual_forward_foreground_projects_to_correct_eye_direction():
    source = np.zeros((36, 128, 4), dtype=np.uint8)
    source[:, :, 3] = 255
    source[:, 59:64, 2] = 255  # a red foreground, flat black background
    raw = torch.zeros((1, 36, 128))
    raw[:, :, 59:64] = 1
    output = StereoSynthesizer(128, 36, disparity_px=4, stereo_method="forward").synthesize(
        source, DepthResult(7, 3, raw, (36, 128), 0, 0), frame_id=7, generation=3)
    assert np.flatnonzero(output.bgra[18, :128, 2] > 128).mean() == pytest.approx(62)
    assert np.flatnonzero(output.bgra[18, 128:, 2] > 128).mean() == pytest.approx(60)


@pytest.mark.parametrize("disparity", [0, 2])
@pytest.mark.parametrize("change", [{"frame_id": 8}, {"generation": 4}])
def test_forward_never_bypasses_rgb_depth_identity(monkeypatch, disparity, change):
    def forbidden(*args):
        pytest.fail("projector ran before frame identity validation")
    monkeypatch.setattr(forward_warp, "synthesize_forward", forbidden)
    synth = StereoSynthesizer(64, 36, disparity_px=disparity, stereo_method="forward")
    with pytest.raises(ValueError, match="different RGB frame or geometry"):
        synth.synthesize(_image(), replace(_depth(), **change), frame_id=7, generation=3)


def test_forward_zero_disparity_is_exact_original_2d_without_projector(monkeypatch):
    def forbidden(*args):
        pytest.fail("zero-disparity 2D must not run the forward projector")
    monkeypatch.setattr(forward_warp, "synthesize_forward", forbidden)
    source = _image(41, 53)
    synth = StereoSynthesizer(64, 36, disparity_px=0, stereo_method="forward")
    output = synth.synthesize(source, _depth(), frame_id=7, generation=3)
    expected = synth.original_2d(source, frame_id=7, generation=3)
    np.testing.assert_array_equal(output.bgra, expected.bgra)
    assert output.mode == "2d" and output.depth_range is None


@pytest.mark.parametrize("disparity", [0, 2])
def test_forward_flat_mask_is_explicitly_unsupported_even_for_zero(disparity):
    synth = StereoSynthesizer(64, 36, disparity_px=disparity, stereo_method="forward")
    with pytest.raises(ValueError, match="does not support flat_mask"):
        synth.synthesize(_image(), _depth(), frame_id=7, generation=3,
                         flat_mask=np.zeros((36, 64), dtype=bool))


def test_forward_retains_depth_range_smoothing_and_scene_generation_reset():
    synth = StereoSynthesizer(64, 36, disparity_px=2, stereo_method="forward")
    source = _image()
    first = synth.synthesize(source, _depth(), frame_id=7, generation=3)
    changed = replace(_depth(8), tensor=_depth().tensor + 1)
    second = synth.synthesize(source, changed, frame_id=8, generation=3)
    third = synth.synthesize(source, replace(changed, frame_id=9, generation=4), frame_id=9, generation=4)
    assert first.scene_reset and not second.scene_reset and third.scene_reset
    assert second.depth_range == pytest.approx((2.1, 6.1))
    assert third.depth_range == (3.0, 7.0)


@pytest.mark.parametrize("command", ["run", "serve"])
@pytest.mark.parametrize("method", [None, "backward", "forward"])
def test_cli_parses_explicit_method_and_preserves_backward_default(monkeypatch, command, method, capsys):
    calls = []
    argv = ["quest3d", command]
    if method is not None:
        argv.extend(["--stereo-method", method])
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(cli, "capture_pipeline", lambda args: calls.append(args) or {})
    monkeypatch.setattr(session, "serve", lambda args: calls.append(args) or {})
    assert cli.main() == 0
    assert len(calls) == 1 and calls[0].stereo_method == (method or "backward")


@pytest.mark.parametrize("command", ["run", "serve"])
def test_cli_rejects_inline_forward_before_dispatch(monkeypatch, command, capsys):
    def forbidden(args):
        pytest.fail("unsupported path reached capture/session startup")
    monkeypatch.setattr(sys, "argv", ["quest3d", command, "--stereo-method", "forward",
                                      "--inline-rect", "0,0,64,36"])
    monkeypatch.setattr(cli, "capture_pipeline", forbidden)
    monkeypatch.setattr(session, "serve", forbidden)
    with pytest.raises(SystemExit) as stopped:
        cli.main()
    assert stopped.value.code == 2 and "enlarged desktop path" in capsys.readouterr().err


@pytest.mark.parametrize("entry", [cli.capture_pipeline, session.serve])
def test_direct_namespace_rejects_inline_forward_before_any_resource(entry, tmp_path):
    directory = tmp_path / "must-not-be-created"
    args = SimpleNamespace(output=str(directory), stereo_method="forward", inline_rect="0,0,64,36")
    with pytest.raises(ValueError, match="enlarged desktop path"):
        entry(args)
    assert not directory.exists()


def test_separate_file_clock_cannot_silently_ignore_forward(tmp_path):
    args = SimpleNamespace(output=str(tmp_path / "unused"), stereo_method="forward", file_av_clock=True)
    with pytest.raises(ValueError, match="enlarged desktop path"):
        session.serve(args)


@pytest.mark.parametrize("method", [None, "forward"])
def test_session_actual_worker_2d_control_status_and_metrics(monkeypatch, tmp_path, method):
    from test_session import _SessionHarness, _wait
    actual_serve = session.serve

    def select_method(args):
        if method is not None:
            args.stereo_method = method
        return actual_serve(args)

    monkeypatch.setattr(session, "serve", select_method)
    harness = _SessionHarness(monkeypatch, tmp_path)
    try:
        assert harness.saw_3d.wait(3)
        assert harness.ai_blocked.wait(3)
        assert harness.status()["stereo_method"] == (method or "backward")
        request = send_control(tmp_path, mode="2d")
        state = _wait(lambda: value if (value := harness.status()).get("applied_request") == request["request_id"] else None)
        assert state["effective_mode"] == "2d" and state["stereo_method"] == (method or "backward")
        assert not harness.allow_ai.is_set()
    finally:
        harness.close()
    rows = [json.loads(line) for line in (tmp_path / "ai/frames.jsonl").read_text().splitlines()]
    presentation = [json.loads(line) for line in (tmp_path / "presentation.jsonl").read_text().splitlines()]
    summary = json.loads((tmp_path / "ai/summary.json").read_text())
    assert rows and all(row["stereo_method"] == (method or "backward") for row in rows)
    assert presentation and all(row["stereo_method"] == (method or "backward") for row in presentation)
    assert summary["stereo_method"] == (method or "backward")
    assert harness.publisher_closed and not harness.status()["running"]


def test_run_actual_cpu_stereo_records_method_with_injected_capture_and_engine(monkeypatch, tmp_path):
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
    monkeypatch.setattr(cursor.DesktopCursorOverlay, "from_capture", lambda *args, **kwargs: Cursor())
    for name in ("reset_peak_memory_stats", "memory_allocated", "max_memory_allocated", "max_memory_reserved"):
        monkeypatch.setattr(torch.cuda, name, lambda *args, **kwargs: 0)
    args = SimpleNamespace(output=str(tmp_path), stereo_method="forward", resize_filter="area",
        cpu_threads=2, mode="3d", ai_size=280, fp32=True, eye_width=64, eye_height=36, disparity=2,
        publish=False, record=False, capture_backend="mss", monitor=1, rect=None,
        seconds=0, warmup=0, fps=30, hide_cursor=True)
    summary = cli.capture_pipeline(args)
    rows = [json.loads(line) for line in (tmp_path / "frames.jsonl").read_text().splitlines()]
    assert summary["stereo_method"] == "forward" and summary["error"] is None
    assert len(rows) == 1 and rows[0]["stereo_method"] == "forward" and rows[0]["mode"] == "3d"
    assert cv2.imread(str(tmp_path / "last-sbs.png"), cv2.IMREAD_UNCHANGED).shape == (36, 128, 4)

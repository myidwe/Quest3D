"""CPU CLI/control contracts for explicit comfort; synthetic pixels are not AI evidence."""

import json
import sys
import time
from types import SimpleNamespace

import numpy as np
import pytest
import torch

from quest3d import cli, session, session_control
from quest3d.bridge import CAPTURE_RECEIPT, FULL_SBS, ORIGINAL_2D
from quest3d.capture import CapturedFrame
from quest3d.depth import DepthResult
from quest3d.geometry import ScreenRect, SourceGeometry


@pytest.fixture(autouse=True)
def cpu_only(monkeypatch):
    old_threads = torch.get_num_threads()
    torch.set_num_threads(2)
    monkeypatch.setattr(torch.cuda, "_lazy_init", lambda: pytest.fail("No GPU in control regression"))
    yield
    torch.set_num_threads(old_threads)


def request(**changes):
    value = dict(session_id="test-session", request_id="test-request", mode="3d", disparity=4,
                 expected_revision=7)
    value.update(changes)
    return value


def write_status(directory, **changes):
    value = dict(session_id="test-session", running=True, requested_mode="3d", disparity=4,
                 eye_width=320, revision=7, applied_request=None)
    value.update(changes)
    session_control.atomic_json(directory / "status.json", value)
    return (directory / "status.json").read_bytes()


@pytest.mark.parametrize("profile", [None, True, False, 0, 1, [], {}, "", "unknown"])
def test_payload_profile_requires_exact_known_string(profile):
    with pytest.raises(ValueError, match="Disparity profile"):
        session_control.validate_request(request(disparity_profile=profile), "test-session", 320)


@pytest.mark.parametrize("profile", ["linear", "comfort"])
def test_valid_profile_preserves_payload_and_revision(profile):
    value = request(disparity_profile=profile)
    assert session_control.validate_request(value, "test-session", 320) is value
    assert value["expected_revision"] == 7


@pytest.mark.parametrize("revision", [True, -1, 2**32, 7.0, "7"])
def test_new_profile_does_not_relax_revision_validation(revision):
    with pytest.raises(ValueError, match="Expected revision"):
        session_control.validate_request(request(disparity_profile="comfort", expected_revision=revision),
                                         "test-session", 320)


def test_unknown_field_and_wrong_session_still_rejected():
    with pytest.raises(ValueError, match="Unsupported control fields"):
        session_control.validate_request(request(disparity_profile="comfort", new_command="run"),
                                         "test-session", 320)
    with pytest.raises(ValueError, match="different session"):
        session_control.validate_request(request(disparity_profile="comfort"), "replacement-session", 320)


@pytest.mark.parametrize("current_profile", [None, "linear", "comfort"])
def test_ordinary_controls_omit_profile_and_do_not_ack_or_change_status(tmp_path, current_profile):
    before = write_status(tmp_path, **({"disparity_profile": current_profile} if current_profile else {}))
    result = session_control.send_control(tmp_path, mode="2d", disparity=3)
    actual = session_control.read_json(tmp_path / "request.json")
    assert "disparity_profile" not in actual
    assert actual["mode"] == "2d" and actual["disparity"] == 3
    assert actual["expected_revision"] == 7 and actual["request_id"] == result["request_id"]
    assert (tmp_path / "status.json").read_bytes() == before
    assert result["submitted"] and "not an acknowledgement" in result["note"]


@pytest.mark.parametrize("profile", ["linear", "comfort"])
def test_explicit_profiles_require_capable_producer_and_preserve_pending_request(tmp_path, profile):
    before = write_status(tmp_path)
    pending = b'{"previous": "untouched"}\n'
    (tmp_path / "request.json").write_bytes(pending)
    with pytest.raises(RuntimeError, match="producer does not support disparity profiles"):
        session_control.send_control(tmp_path, disparity_profile=profile)
    assert (tmp_path / "request.json").read_bytes() == pending
    assert (tmp_path / "status.json").read_bytes() == before


@pytest.mark.parametrize("profile", ["linear", "comfort"])
def test_explicit_profile_is_atomic_optional_payload(tmp_path, profile):
    before = write_status(tmp_path, disparity_profile="comfort")
    result = session_control.send_control(tmp_path, disparity_profile=profile)
    value = session_control.read_json(tmp_path / "request.json")
    assert value == dict(session_id="test-session", request_id=result["request_id"], mode="3d",
                         disparity=4, stop=False, expected_revision=7, disparity_profile=profile)
    assert (tmp_path / "status.json").read_bytes() == before
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("bad", [True, 3, [], {}, "unknown"])
def test_invalid_explicit_control_does_not_create_request(tmp_path, bad):
    write_status(tmp_path, disparity_profile="linear")
    with pytest.raises(ValueError, match="Disparity profile"):
        session_control.send_control(tmp_path, disparity_profile=bad)
    assert not (tmp_path / "request.json").exists()


@pytest.mark.parametrize("command", ["run", "serve"])
@pytest.mark.parametrize("profile", [None, "linear", "comfort"])
def test_cli_run_and_serve_default_linear_and_explicit_choice(monkeypatch, command, profile):
    seen = []
    monkeypatch.setattr(cli, "capture_pipeline", lambda args: seen.append(args) or {})
    monkeypatch.setattr(session, "serve", lambda args: seen.append(args) or {})
    monkeypatch.setattr(sys, "argv", ["quest3d", command] + (["--disparity-profile", profile] if profile else []))
    assert cli.main() == 0
    assert seen[0].disparity_profile == (profile or "linear")


@pytest.mark.parametrize("profile", [None, "linear", "comfort"])
def test_cli_control_default_is_none(monkeypatch, tmp_path, profile):
    seen = []
    monkeypatch.setattr(session_control, "send_control", lambda directory, **kw: seen.append((directory, kw)) or {})
    monkeypatch.setattr(sys, "argv", ["quest3d", "control", "--session", str(tmp_path)] +
                        (["--disparity-profile", profile] if profile else []))
    assert cli.main() == 0
    assert seen[0][0] == tmp_path and seen[0][1]["disparity_profile"] == profile


@pytest.mark.parametrize("command", ["run", "serve", "control"])
def test_cli_unknown_profile_rejected_before_dispatch(monkeypatch, command):
    monkeypatch.setattr(sys, "argv", ["quest3d", command, "--disparity-profile", "automatic"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2


@pytest.mark.parametrize("options", [{"inline_rect": "0,0,16,16"}, {"file_av_clock": True}])
def test_direct_capture_excluded_paths_rejected_before_resources(tmp_path, options):
    out = tmp_path / "never-created"
    with pytest.raises(ValueError, match="Comfort disparity profile requires"):
        cli.capture_pipeline(SimpleNamespace(disparity_profile="comfort", output=str(out), **options))
    assert not out.exists()


@pytest.mark.parametrize("options", [["run", "--inline-rect", "0,0,16,16"],
                                    ["serve", "--file", "movie.mp4", "--file-av-clock"]])
def test_cli_excluded_paths_rejected_before_dispatch(monkeypatch, options):
    monkeypatch.setattr(sys, "argv", ["quest3d", *options, "--disparity-profile", "comfort"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2


def test_profile_control_stale_cas_cannot_replace_current_setting(monkeypatch, tmp_path):
    from test_session import _SessionHarness, _wait
    harness = _SessionHarness(monkeypatch, tmp_path, initial_mode="2d", second_action="static")
    try:
        _wait(lambda: harness.published)
        changed = session_control.send_control(tmp_path, disparity_profile="comfort")
        current = _wait(lambda: s if (s := harness.status()).get("applied_request") == changed["request_id"] else None)
        assert current["revision"] == 1 and current["disparity_profile"] == "comfort"
        stale = request(session_id=current["session_id"], disparity_profile="linear", expected_revision=0)
        session_control.atomic_json(tmp_path / "request.json", stale)
        rejected = _wait(lambda: s if (s := harness.status()).get("rejected_request") == stale["request_id"] else None)
        assert rejected["revision"] == 1 and rejected["disparity_profile"] == "comfort"
        assert rejected["requested_mode"] == "2d" and rejected["disparity"] == 4
        ordinary = session_control.send_control(tmp_path, disparity=3)
        updated = _wait(lambda: s if (s := harness.status()).get("applied_request") == ordinary["request_id"] else None)
        assert updated["revision"] == 2 and updated["disparity_profile"] == "comfort"
    finally:
        harness.close()


@pytest.mark.parametrize("profile", ["linear", "comfort"])
@pytest.mark.parametrize("mode,disparity", [("3d", 1), ("2d", 1), ("3d", 0)])
def test_real_cpu_capture_synthesis_metadata_pair_flags_and_2d(monkeypatch, tmp_path, profile, mode, disparity):
    from quest3d import bridge, capture, cursor, depth
    y, x = np.indices((48, 64))
    source = np.stack(((7*x+y)%256, (x+3*y)%256, (x*y)%256, np.full_like(x, 255)), -1).astype(np.uint8)
    original = source.copy()
    captured_ns = time.perf_counter_ns()
    published = []
    class Capture:
        dropped_frames = 0
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def grab(self):
            return CapturedFrame(source, captured_ns, "synthetic", 7, 3,
                                 SourceGeometry(ScreenRect(0, 0, 64, 48), 3))
    class Engine:
        def __init__(self, *args): pass
        def infer(self, image, *, frame_id, generation):
            assert image is source
            return DepthResult(frame_id, generation, torch.linspace(0, 1, 9*13).reshape(1, 9, 13),
                               (9, 13), 0, 0)
    class Cursor:
        def sample_and_composite(self, result, source_frame): return result
        def snapshot(self): return {"enabled": False}
        def close(self): pass
    class Publisher:
        epoch = 321
        skipped = 0
        def publish(self, image, **metadata):
            published.append((image.copy(), metadata))
            return True
        def close(self): pass
    monkeypatch.setattr(capture, "DesktopCapture", Capture)
    monkeypatch.setattr(depth, "DepthEngine", Engine)
    monkeypatch.setattr(cursor.DesktopCursorOverlay, "from_capture", lambda *a, **kw: Cursor())
    monkeypatch.setattr(bridge, "FramePublisher", Publisher)
    for name in ("reset_peak_memory_stats", "memory_allocated", "max_memory_allocated", "max_memory_reserved"):
        monkeypatch.setattr(torch.cuda, name, lambda *a, **kw: 0)
    args = SimpleNamespace(output=str(tmp_path), stereo_method="forward", depth_refinement="none",
        colour_precision="uint8", disparity_profile=profile, resize_filter="area", cpu_threads=2, mode=mode,
        ai_size=280, fp32=True, eye_width=48, eye_height=36, disparity=disparity, publish=True,
        record=False, capture_backend="mss", monitor=1, rect=None, seconds=0, warmup=0, fps=30, hide_cursor=True)
    summary = cli.capture_pipeline(args)
    assert summary["error"] is None
    row = json.loads((tmp_path / "frames.jsonl").read_text().splitlines()[0])
    original_2d = mode == "2d" or disparity == 0
    effective = "linear" if original_2d else profile
    reason = "original_2d" if original_2d and profile == "comfort" else None
    convergence = None if original_2d else (.625 if profile == "comfort" else .5)
    for record in (row, summary):
        assert record["disparity_profile"] == profile
        assert record["effective_disparity_profile"] == effective
        assert record["disparity_profile_reason"] == reason
        assert record["effective_convergence"] == convergence
    image, metadata = published[0]
    assert metadata["flags"] == FULL_SBS | CAPTURE_RECEIPT | (ORIGINAL_2D if original_2d else 0)
    assert metadata["frame_id"] == 7 and metadata["generation"] == 3 and metadata["capture_ns"] == captured_ns
    assert image.shape == (36, 96, 4) and np.all(image[..., 3] == 255)
    assert np.array_equal(image[:, :48], image[:, 48:]) == original_2d
    assert np.array_equal(source, original)

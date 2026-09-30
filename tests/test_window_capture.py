"""Policy/transition fault injection. Real HWND→CUDA proof is the owned-window probe."""
from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace
import threading

import pytest
import torch

from quest3d.display_color import DisplayColor
from quest3d.geometry import ScreenRect
from quest3d.source_identity import SourceIdentity, SourceKind
from quest3d.window_capture import (
    GPUWindowCapture, WindowCaptureFailed, WindowCaptureUnavailable, _WindowBuffer, _WindowFrameGate,
)
from quest3d.window_sources import SourceExclusions, TrackedWindow, WindowIdentity, WindowObservation


IDENTITY = WindowIdentity(123, 10, 1234567890, 11, "fixture_class")
COLOR = DisplayColor("DISPLAY2", "hdr", True, 8, 3000, 240, 3.0, {})


def tracked(*, generation=1, reasons=(), valid=True, **changes):
    observation = WindowObservation(123, 1, IDENTITY, frame_bounds=ScreenRect(-1000, 10, 800, 600),
        client_bounds=ScreenRect(-990, 40, 780, 560), window_bounds=ScreenRect(-1008, 2, 816, 616),
        monitor_bounds=ScreenRect(-1920, 0, 1920, 1080), monitor_device="DISPLAY2", dpi=144,
        top_level=True, visible=True, minimized=False, cloaked=False, foreground=False,
        virtual_desktop=ScreenRect(-1920, 0, 3840, 1080))
    observation = replace(observation, **changes)
    return TrackedWindow(observation, generation, valid, None if valid else "window_identity_lost_or_reused",
                         observation.frame_bounds, False, reasons, True)


def gate():
    value = _WindowFrameGate(100)
    value.update(tracked(), [COLOR], 1000)
    return value


@pytest.mark.parametrize("kwargs", [{}, {"experimental_window": False}, {"experimental_window": 1}])
def test_explicit_opt_in_required_before_native_import(kwargs):
    with pytest.raises(ValueError, match="explicit"):
        GPUWindowCapture(IDENTITY, **kwargs)


@pytest.mark.parametrize("identity", [replace(IDENTITY, process_id=True), replace(IDENTITY, process_created_filetime=0),
                                      replace(IDENTITY, thread_id=-1), replace(IDENTITY, class_name="")])
def test_invalid_exact_identity_rejected(identity):
    with pytest.raises(ValueError, match="identity"):
        GPUWindowCapture(identity, experimental_window=True)


def test_selected_output_excluded_but_output_covering_source_allowed():
    with pytest.raises(ValueError, match="excluded"):
        GPUWindowCapture(IDENTITY, experimental_window=True, exclusions=SourceExclusions(hwnds={123}))
    value = gate()
    value.update(tracked(reasons=("occlusion_possible", "output_overlap_recursion_risk"),
                         occlusion="possible", output_overlap=True), [COLOR], 1001)
    value.check_frame(width=800, height=600, received_ns=1101)
    assert value.reason is None


def test_negative_coordinates_fresh_content_barrier_and_generation():
    value = gate()
    with pytest.raises(WindowCaptureUnavailable, match="fresh_stable"):
        value.check_frame(width=800, height=600, received_ns=1099)
    value.check_frame(width=800, height=600, received_ns=1100)
    assert value.geometry.bounds.left == -1000
    previous = value.generation
    value.update(tracked(generation=2, frame_bounds=ScreenRect(-900, 10, 800, 600)), [COLOR], 1200)
    with pytest.raises(WindowCaptureUnavailable, match="generation"):
        value.check_frame(width=800, height=600, received_ns=1400, generation=previous)


@pytest.mark.parametrize("reason", ["minimized", "cloaked_or_unknown", "not_visible", "geometry_changed_during_query",
                                    "bounds_unavailable", "cross_monitor_or_outside_monitor", "dpi_or_monitor_unknown"])
def test_reversible_states_block_and_require_new_frame_after_resume(reason):
    value = gate()
    value.update(tracked(generation=2, reasons=(reason,)), [COLOR], 1200)
    with pytest.raises(WindowCaptureUnavailable, match=reason):
        value.check_frame(width=800, height=600, received_ns=1500)
    value.update(tracked(generation=3), [COLOR], 1500)
    with pytest.raises(WindowCaptureUnavailable, match="fresh_stable"):
        value.check_frame(width=800, height=600, received_ns=1400)
    value.check_frame(width=800, height=600, received_ns=1600)


def test_native_restore_content_size_mismatch_invalidates_previous_generation():
    value = gate()
    previous = value.generation
    with pytest.raises(WindowCaptureUnavailable, match="content_size"):
        value.check_frame(width=146, height=28, received_ns=1200)
    value.reject_content_shape(1200)
    assert value.generation > previous
    with pytest.raises(WindowCaptureUnavailable, match="generation"):
        value.check_frame(width=800, height=600, received_ns=1400, generation=previous)


def test_source_lifecycle_loss_is_terminal_after_matching_identity_returns():
    value = gate()
    value.update(tracked(valid=False), [COLOR], 1200)
    value.update(tracked(), [COLOR], 1500)
    with pytest.raises(WindowCaptureFailed, match="identity"):
        value.check_frame(width=800, height=600, received_ns=1600)


@pytest.mark.parametrize("colors", [[], [COLOR, COLOR], [replace(COLOR, active_mode="wcg")],
                                    [replace(COLOR, hdr_enabled=None)], [replace(COLOR, sc_rgb_sdr_white_scale=None)],
                                    [replace(COLOR, sc_rgb_sdr_white_scale=float("nan"))]])
def test_unknown_or_duplicate_color_never_guesses_white(colors):
    value = gate()
    value.update(tracked(), colors, 1200)
    with pytest.raises(WindowCaptureUnavailable):
        value.check_frame(width=800, height=600, received_ns=1400)


def test_spanning_is_rechecked_independently_of_tracker_reasons():
    value = gate()
    value.update(tracked(frame_bounds=ScreenRect(-100, 10, 800, 600)), [COLOR], 1200)
    assert value.reason == "cross_monitor_or_outside_monitor"


def test_sdr_known_scale_one_and_color_change_invalidate_old_frame():
    value = gate()
    previous = value.generation
    sdr = replace(COLOR, active_mode="sdr", hdr_enabled=False, sc_rgb_sdr_white_scale=1.0)
    value.update(tracked(), [sdr], 1200)
    assert value.color == sdr and value.generation > previous
    value.check_frame(width=800, height=600, received_ns=1400)


def test_display_query_failure_is_resumable_but_not_stale_profile():
    value = gate()
    value.update(tracked(), [], 1200, color_error="API failed")
    assert value.color is None and value.reason == "display_color_query_failed"
    value.update(tracked(), [COLOR], 1500)
    value.check_frame(width=800, height=600, received_ns=1600)


def test_geometry_change_during_mapper_discards_result_and_preserves_receipt():
    # CPU tensor and blocking fake mapper inject one transition; not GPU proof.
    capture = GPUWindowCapture(IDENTITY, experimental_window=True)
    capture._opened = True
    capture._gate = gate()
    capture._latest = _WindowBuffer(torch.zeros((600, 800, 4), dtype=torch.float16), 1200, 9_000_000,
                                    1, 91, capture._gate.geometry, COLOR,
                                    capture._frame_source_identity(capture._gate.geometry))
    changed = False
    def refresh():
        if changed:
            capture._gate.update(tracked(generation=2), [COLOR], 1500)
    capture._refresh = refresh
    def mapper(pixels, **_):
        nonlocal changed
        changed = True
        return pixels.to(torch.uint8)
    capture._mapper = mapper
    with pytest.raises(WindowCaptureUnavailable, match="generation"):
        capture.grab(timeout_seconds=.1)
    assert capture.last_frame_diagnostics is None
    assert capture.input_authorized is False


def test_raw_future_timestamp_never_replaces_receipt_in_returned_frame():
    capture = GPUWindowCapture(IDENTITY, experimental_window=True)
    capture._opened = True
    capture._gate = gate()
    capture._refresh = lambda: None
    capture._latest = _WindowBuffer(torch.zeros((600, 800, 4), dtype=torch.float16), 1200, 9000000,
                                    1, 91, capture._gate.geometry, COLOR,
                                    capture._frame_source_identity(capture._gate.geometry))
    capture._mapper = lambda pixels, **_: pixels.to(torch.uint8)
    frame = capture.grab(timeout_seconds=.1)
    assert frame.captured_ns == 1200
    assert capture.last_frame_diagnostics["system_relative_time_ns"] == 9000000
    assert capture.last_frame_diagnostics["raw_wgc_to_receive_ms"] < 0
    assert capture.last_frame_diagnostics["raw_wgc_time_used_for_latency"] is False


def test_close_waits_for_owned_mapper_and_is_idempotent():
    capture = GPUWindowCapture(IDENTITY, experimental_window=True)
    operations = []
    capture._capture = SimpleNamespace(_inner=SimpleNamespace(request_stop=lambda: operations.append("stop")))
    capture._mapper = SimpleNamespace(close=lambda: operations.append("close_mapper"))
    capture._tracker = SimpleNamespace(__exit__=lambda *_: operations.append("close_tracker"))
    capture.close()
    capture.close()
    assert operations == ["stop", "close_mapper", "close_tracker"]


def test_native_close_timeout_keeps_gpu_and_tracker_resources_owned():
    capture = GPUWindowCapture(IDENTITY, experimental_window=True)
    operations = []
    capture._capture = SimpleNamespace(_inner=SimpleNamespace(request_stop=lambda: operations.append("requested")))
    capture._worker = SimpleNamespace(join=lambda timeout: operations.append(("join", timeout)), is_alive=lambda: True)
    capture._mapper = SimpleNamespace(close=lambda: pytest.fail("Do not free resources under a live worker"))
    capture._tracker = SimpleNamespace(__exit__=lambda *_: pytest.fail("Keep tracking until worker exits"))
    with pytest.raises(RuntimeError, match="5 seconds"):
        capture.close()
    assert operations == ["requested", ("join", 5)]


def test_native_cleanup_failure_is_not_reported_as_success_or_freed():
    capture = GPUWindowCapture(IDENTITY, experimental_window=True)
    capture._cleanup_error = RuntimeError("CUDA unregister failed")
    capture._worker = SimpleNamespace(join=lambda _: None, is_alive=lambda: False)
    capture._mapper = SimpleNamespace(close=lambda: pytest.fail("Keep failed cleanup resources owned"))
    with pytest.raises(RuntimeError, match="cleanup failed; resources retained"):
        capture.close()
    assert capture._mapper is not None and capture._worker is not None


def test_successful_close_retains_error_message_without_worker_traceback():
    capture = GPUWindowCapture(IDENTITY, experimental_window=True)
    try:
        raise WindowCaptureFailed("source lifetime lost")
    except WindowCaptureFailed as error:
        capture._error = error
    assert capture._error.__traceback__ is not None
    capture.close()
    assert str(capture._error) == "source lifetime lost"
    assert capture._error.__traceback__ is None and capture._error.__context__ is None


def make_buffered_capture():
    """CPU pixels inject metadata transitions; this fixture never proves live capture."""
    capture = GPUWindowCapture(IDENTITY, experimental_window=True)
    capture._opened = True
    capture._gate = gate()
    capture._refresh = lambda: None
    capture._mapper = lambda pixels, **_: pixels.to(torch.uint8)
    capture._latest = _WindowBuffer(torch.zeros((600, 800, 4), dtype=torch.float16), 1200, 9_000_000,
        1, 91, capture._gate.geometry, COLOR, capture._frame_source_identity(capture._gate.geometry))
    return capture


def test_selection_is_nonzero_uint64_unique_per_selection_and_read_only(monkeypatch):
    values = iter((0, (1 << 64) - 2))
    monkeypatch.setattr("quest3d.window_capture.secrets.randbelow", lambda _: next(values))
    first = GPUWindowCapture(IDENTITY, experimental_window=True)
    second = GPUWindowCapture(IDENTITY, experimental_window=True)
    assert (first.selection_id, second.selection_id) == (1, (1 << 64) - 1)
    assert first.source_id != second.source_id
    with pytest.raises(AttributeError):
        first.identity = replace(IDENTITY, hwnd=999)
    with pytest.raises(AttributeError):
        first.selection_id = second.selection_id


def test_returned_frame_carries_exact_frozen_window_snapshot_and_negative_parent():
    capture = make_buffered_capture()
    expected = capture._latest.source_identity
    frame = capture.grab(timeout_seconds=.1)
    assert frame.source_identity is expected
    assert expected == SourceIdentity(SourceKind.WINDOW, 10, capture.selection_id, 123, 1234567890,
                                     ScreenRect(-1000, 10, 800, 600))
    assert SourceIdentity.unpack(expected.pack()) == expected
    expected.validate_region((-1000, 10, 800, 600))
    with pytest.raises(FrozenInstanceError):
        expected.parent_bounds = ScreenRect(0, 0, 800, 600)
    assert capture.input_authorized is False


@pytest.mark.parametrize("transition", [
    {"frame_bounds": ScreenRect(-900, 10, 800, 600)},
    {"frame_bounds": ScreenRect(-1000, 10, 640, 480)},
    {"reasons": ("minimized",), "minimized": True},
])
def test_transition_rejects_old_pending_frame_without_relabelling(transition):
    capture = make_buffered_capture()
    pending = capture._latest
    original = pending.source_identity.pack()
    capture._gate.update(tracked(generation=2, **transition), [COLOR], 1500)
    with pytest.raises(WindowCaptureUnavailable):
        capture.grab(timeout_seconds=.1)
    assert pending.source_identity.pack() == original
    assert capture.last_frame_diagnostics is None


def test_move_creates_new_snapshot_while_already_returned_frame_keeps_old_bounds():
    capture = make_buffered_capture()
    old = capture.grab(timeout_seconds=.1)
    original = old.source_identity.pack()
    capture._gate.update(tracked(generation=2, frame_bounds=ScreenRect(-900, 10, 800, 600)), [COLOR], 1500)
    capture._latest = replace(capture._latest, frame_id=2, received_ns=1700,
        geometry=capture._gate.geometry, source_identity=capture._frame_source_identity(capture._gate.geometry))
    new = capture.grab(timeout_seconds=.1)
    assert new.source_identity.parent_bounds.left == -900
    assert new.source_identity.selection_id == old.source_identity.selection_id
    assert new.geometry_generation > old.geometry_generation
    assert old.source_identity.pack() == original


def test_new_selection_cannot_accept_old_buffer_even_for_identical_hwnd_and_generation():
    first = make_buffered_capture()
    second = make_buffered_capture()
    second._latest = first._latest
    with pytest.raises(WindowCaptureFailed, match="buffer_source_identity"):
        second.grab(timeout_seconds=.1)
    assert second.last_frame_diagnostics is None


@pytest.mark.parametrize("changes", [{"process_id": 11}, {"native_handle": 124},
                                      {"creation_filetime": 1234567891},
                                      {"parent_bounds": ScreenRect(-900, 10, 800, 600)}])
def test_buffer_with_mismatched_lifetime_or_parent_is_rejected(changes):
    capture = make_buffered_capture()
    capture._latest = replace(capture._latest, source_identity=replace(capture._latest.source_identity, **changes))
    with pytest.raises(WindowCaptureFailed, match="buffer_source_identity"):
        capture.grab(timeout_seconds=.1)


def test_reused_hwnd_and_close_never_update_returned_snapshot():
    capture = make_buffered_capture()
    frame = capture.grab(timeout_seconds=.1)
    original = frame.source_identity.pack()
    capture._gate.update(tracked(generation=2, valid=False, identity=replace(IDENTITY, process_created_filetime=1)),
                         [COLOR], 1500)
    capture._gate.update(tracked(generation=3), [COLOR], 1600)
    with pytest.raises(WindowCaptureFailed, match="identity"):
        capture.grab(timeout_seconds=.1)
    capture._mapper = None
    capture.close()
    assert frame.source_identity.pack() == original


def test_snapshot_requires_matching_fresh_observation_identity():
    capture = make_buffered_capture()
    capture._gate.update(tracked(identity=replace(IDENTITY, process_id=42)), [COLOR], 1500)
    with pytest.raises(WindowCaptureFailed, match="observation_identity"):
        capture._frame_source_identity(capture._gate.geometry)

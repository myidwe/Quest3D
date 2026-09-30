"""Real +quest3 HWND adapter→fused tone mapper, manipulating only owned GDI windows."""
import argparse
import faulthandler
from dataclasses import asdict
from datetime import datetime
import json
from pathlib import Path
import time
import sys
import traceback

import torch
from window_fixture import ThreadedOwnedWindowFixture
from quest3d.window_capture import GPUWindowCapture, WindowCaptureFailed, WindowCaptureUnavailable
from quest3d.window_sources import SourceExclusions
from quest3d.source_identity import SourceKind, SourceIdentity


parser = argparse.ArgumentParser()
parser.add_argument("--output", type=Path,
    default=Path("artifacts/capture/window-experimental/adapter-verification.json"))
args = parser.parse_args()
original_excepthook = sys.excepthook
def report_exception(kind, error, trace):
    args.output.with_suffix(".error.txt").write_text("".join(traceback.format_exception(kind, error, trace)), encoding="utf-8")
    original_excepthook(kind, error, trace)
sys.excepthook = report_exception
faulthandler.dump_traceback_later(12, repeat=True)
records = []


def take(capture, fixture, source, timeout=5):
    deadline = time.monotonic() + timeout
    blocked = []
    while time.monotonic() < deadline:
        fixture.paint(source)
        fixture.pump(.025)
        try:
            return capture.grab(timeout_seconds=.08), blocked
        except (WindowCaptureUnavailable, TimeoutError) as exc:
            if not blocked or blocked[-1] != str(exc):
                blocked.append(str(exc))
    raise TimeoutError(f"No accepted own-window frame: {capture.status}; waits={blocked}")


def record(event, frame, capture, waits):
    print(event, flush=True)
    h, w = frame.bgra.shape[:2]
    center = frame.bgra[h // 3:2 * h // 3, w // 3:2 * w // 3, :3].float().mean(dim=(0, 1)).tolist()
    assert frame.bgra.is_cuda and frame.bgra.dtype == torch.uint8 and frame.bgra.is_contiguous()
    assert (w, h) == (frame.geometry.bounds.width, frame.geometry.bounds.height)
    assert frame.captured_ns == capture.last_frame_diagnostics["received_ns"]
    snapshot = frame.source_identity
    assert isinstance(snapshot, SourceIdentity) and snapshot.kind is SourceKind.WINDOW
    assert snapshot.process_id == capture.identity.process_id
    assert snapshot.native_handle == capture.identity.hwnd
    assert snapshot.creation_filetime == capture.identity.process_created_filetime
    assert snapshot.selection_id == capture.selection_id and snapshot.selection_id > 0
    assert snapshot.parent_bounds == frame.geometry.bounds
    assert SourceIdentity.unpack(snapshot.pack()) == snapshot
    assert capture.input_authorized is False
    return dict(event=event, width=w, height=h, frame_id=frame.frame_id,
                geometry=asdict(frame.geometry), center_bgr=center, color=capture.color_profile,
                waits=waits, status=capture.status, color_ms=frame.color_processing_ms,
                source_identity=asdict(snapshot), source_identity_wire=snapshot.pack().hex())


with ThreadedOwnedWindowFixture() as fixture:
    source = fixture.create()
    identity = fixture.provider.observe(source).identity
    assert identity is not None
    # Source and excluded output belong to this test. Do not exclude the PID,
    # since that would also (correctly) deny the selected source in this process.
    cover = fixture.create(left=2680, top=180, width=460, height=340, color=0)
    with GPUWindowCapture(identity, experimental_window=True, exclusions=SourceExclusions(hwnds={cover})) as capture:
        print("Opened exact HWND adapter; awaiting fresh owned source", flush=True)
        frame, waits = take(capture, fixture, source)
        records.append(record("excluded_output_cover_does_not_block_hwnd", frame, capture, waits))
        native_wrapper = capture._capture
        original_stop = native_wrapper.stop
        def traced_stop():
            print(f"Adapter stop: lease={native_wrapper.lease_stats()}, bridge_registered={bool(native_wrapper._bridge and native_wrapper._bridge.resource)}", flush=True)
            return original_stop()
        native_wrapper.stop = traced_stop
        assert capture.status["output_overlap"]
        assert min(records[-1]["center_bgr"]) > 200
        first_generation = frame.geometry_generation
        first_frame = frame
        first_snapshot = frame.source_identity.pack()
        old_pixels = frame.bgra.clone()
        original_pixels = frame.bgra
        fixture.close_window(cover)
        fixture.move(source, 2780, 260, 638, 414)
        frame, waits = take(capture, fixture, source)
        records.append(record("resize", frame, capture, waits))
        assert frame.geometry_generation > first_generation
        assert frame.source_identity.selection_id == first_frame.source_identity.selection_id
        assert frame.source_identity.parent_bounds != first_frame.source_identity.parent_bounds
        assert first_frame.source_identity.pack() == first_snapshot
        assert frame.geometry.bounds == fixture.provider.observe(source).frame_bounds
        assert torch.equal(original_pixels, old_pixels)

        fixture.show(source, 6)
        fixture.pump(.03)
        try:
            capture.grab(timeout_seconds=.08)
            raise AssertionError("Minimized source must not return the previous frame")
        except WindowCaptureUnavailable as exc:
            assert "minimized" in str(exc) or "not_visible" in str(exc)
            records.append(dict(event="minimized_blocked", status=capture.status, reason=str(exc)))

        fixture.show(source, 4)  # SW_SHOWNOACTIVATE restores size without activation.
        fixture.move(source, 2500, 200, 420, 300)
        fixture.pump(.03)
        try:
            capture.grab(timeout_seconds=.08)
            raise AssertionError("Spanning source must not guess an SDR white")
        except WindowCaptureUnavailable as exc:
            assert "cross_monitor" in str(exc)
            records.append(dict(event="spanning_color_blocked", status=capture.status, reason=str(exc)))

        fixture.move(source, 300, 220, 420, 300)
        frame, waits = take(capture, fixture, source)
        records.append(record("primary_monitor_resumed", frame, capture, waits))
        assert capture.color_profile["display"]["sc_rgb_sdr_white_scale"] == 4.8
        assert max(abs(a - b) for a, b in zip(records[0]["center_bgr"], records[-1]["center_bgr"])) <= 1
        print("Closing owned source HWND", flush=True)
        print(f"Before source close lease={native_wrapper.lease_stats()}", flush=True)
        fixture.close_window(source)
        print("Owned source destroyed; checking terminal result", flush=True)
        try:
            capture.grab(timeout_seconds=.1)
            raise AssertionError("Closed source must not return an old frame or reselect")
        except WindowCaptureFailed as exc:
            records.append(dict(event="closed_terminal", reason=str(exc), status=capture.status))
    print("Capture context closed", flush=True)
    assert not capture._worker and capture._mapper is None and capture._tracker is None
    assert first_frame.source_identity.pack() == first_snapshot
    replacement = fixture.create(left=300, top=220)
    replacement_identity = fixture.provider.observe(replacement).identity
    with GPUWindowCapture(replacement_identity, experimental_window=True) as next_capture:
        frame, waits = take(next_capture, fixture, replacement)
        records.append(record("new_selection_after_source_close", frame, next_capture, waits))
        assert frame.source_identity.selection_id != first_frame.source_identity.selection_id
        assert first_frame.source_identity.pack() == first_snapshot
        fixture.close_window(replacement)
    foreground_unchanged = fixture.foreground_before == int(fixture.user.GetForegroundWindow() or 0)
    assert foreground_unchanged
    print("Foreground check complete", flush=True)

result = dict(verified_at=datetime.now().astimezone().isoformat(), actual_owned_hwnd_capture=True,
    actual_cuda_tone_mapping=True, same_gdi_white_across_two_outputs_within_one_code_value=True,
    user_windows_modified=False, user_window_pixels_saved=False, windows_hdr_changed=False,
    foreground_unchanged=foreground_unchanged, input_authorized=False, quest_display_verified=False,
    color_accuracy_generalized=False, ai_inference_in_this_probe=False,
    worker_mapper_tracker_cleaned_up=True, records=records)
result["immutable_source_identity_verified"] = True
result["bridge_transport_verified"] = False
args.output.parent.mkdir(parents=True, exist_ok=True)
args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
faulthandler.cancel_dump_traceback_later()
print(json.dumps(result, indent=2))

"""Opt-in actual owned-window capture worker verification; no host or input changes."""
import argparse
from dataclasses import asdict
from datetime import datetime
import gc
import hashlib
import importlib.metadata
import json
from pathlib import Path
import sys
import time

import numpy as np
import psutil

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "native/capture"))
from window_fixture import ThreadedOwnedWindowFixture
from quest3d.window_capture import WindowCaptureFailed, WindowCaptureUnavailable
from quest3d.window_process_capture import ProcessWindowCapture
from quest3d.window_sources import SourceExclusions


def snapshot():
    process = psutil.Process()
    memory = process.memory_info()
    return dict(handles=process.num_handles(), threads=process.num_threads(), rss_bytes=memory.rss,
                private_commit_bytes=memory.private)


def take(capture, fixture, hwnd, timeout=8):
    deadline = time.monotonic() + timeout
    waits = []
    while time.monotonic() < deadline:
        fixture.paint(hwnd)
        try:
            return capture.grab(timeout_seconds=.1), waits
        except (TimeoutError, WindowCaptureUnavailable) as exc:
            if not waits or waits[-1] != str(exc):
                waits.append(str(exc))
    raise TimeoutError(f"No own frame: {capture.status}; waits={waits}")


def record(event, frame, capture):
    identity = frame.source_identity
    assert identity.selection_id == capture.selection_id
    assert (identity.process_id, identity.native_handle, identity.creation_filetime) == (
        capture.identity.process_id, capture.identity.hwnd, capture.identity.process_created_filetime)
    assert identity.parent_bounds == frame.geometry.bounds
    assert frame.bgra.shape == (frame.geometry.bounds.height, frame.geometry.bounds.width, 4)
    assert frame.bgra.dtype == np.uint8 and frame.bgra.flags.owndata
    center = frame.bgra[frame.bgra.shape[0] // 2, frame.bgra.shape[1] // 2, :3].tolist()
    assert all(230 <= value <= 245 for value in center), center
    return dict(event=event, frame_id=frame.frame_id, capture_ns=frame.captured_ns,
        source_identity=asdict(identity), geometry=asdict(frame.geometry), center_bgr=center,
        pixels_sha256=hashlib.sha256(frame.bgra.tobytes()).hexdigest(), diagnostics=capture.last_frame_diagnostics)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cycles", type=int, default=10)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("Preserve previous evidence")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report = dict(created=datetime.now().isoformat(), cycles=[], parent_wc_cuda=importlib.metadata.version("wc-cuda"),
        input_enabled=False, ai_connected=False, zero_copy=False)
    capture = None
    try:
        with ThreadedOwnedWindowFixture() as fixture:
            hwnd = fixture.create()
            previous_selection = None
            report["parent_before"] = snapshot()
            for cycle in range(args.cycles):
                identity = fixture.provider.observe(hwnd).identity
                started = time.perf_counter_ns()
                capture = ProcessWindowCapture(identity, experimental_window=True)
                capture.__enter__()
                row = dict(cycle=cycle, child_pid=capture.status["child_pid"], records=[])
                report["cycles"].append(row)
                frame, waits = take(capture, fixture, hwnd)
                row["first_frame_ms"] = (time.perf_counter_ns() - started) / 1e6
                row["records"].append(record("first", frame, capture))
                row["waits"] = waits
                assert previous_selection is None or previous_selection != capture.selection_id
                previous_selection = capture.selection_id
                old_pixels = frame.bgra.copy()
                old_identity = frame.source_identity.pack()
                if cycle == 0:
                    cover = fixture.create(left=2680, top=180, width=500, height=360, color=0)
                    current, _ = take(capture, fixture, hwnd)
                    row["records"].append(record("fully_occluded_changing_source", current, capture))
                    covered_hash = row["records"][-1]["pixels_sha256"]
                    # Explicitly prove changing original pixels under the cover,
                    # not merely another timestamp for an identical white image.
                    for _ in range(8):
                        changing, _ = take(capture, fixture, hwnd)
                        covered = record("occluded_new_pixels", changing, capture)
                        if covered["pixels_sha256"] != covered_hash:
                            row["records"].append(covered)
                            break
                    else:
                        raise AssertionError("Covered source did not deliver changed stripe pixels")
                    fixture.close_window(cover)
                    fixture.move(hwnd, 2750, 220, 630, 410)
                    current, _ = take(capture, fixture, hwnd)
                    row["records"].append(record("resize", current, capture))
                    assert current.geometry_generation > frame.geometry_generation
                    fixture.show(hwnd, 6)
                    try:
                        capture.grab(timeout_seconds=.1)
                        raise AssertionError("Minimized source returned an old frame")
                    except WindowCaptureUnavailable:
                        pass
                    fixture.show(hwnd, 4)  # Restore without activating the owned source.
                    fixture.move(hwnd, 2700, 200, 420, 300)
                    current, _ = take(capture, fixture, hwnd)
                    row["records"].append(record("restore", current, capture))
                    fixture.close_window(hwnd)
                    try:
                        capture.grab(timeout_seconds=.1)
                        raise AssertionError("Closed source returned an old frame")
                    except WindowCaptureFailed:
                        pass
                assert frame.source_identity.pack() == old_identity and np.array_equal(frame.bgra, old_pixels)
                capture.close()
                row["close"] = capture.close_result
                assert capture.close_result["normal_exit"] and not capture.close_result["forced"]
                assert not psutil.pid_exists(row["child_pid"])
                try:
                    capture.grab(timeout_seconds=.1)
                    raise AssertionError("Closed child returned an old frame")
                except WindowCaptureFailed:
                    pass
                del capture, frame, old_pixels
                capture = None
                gc.collect()
                row["parent_after"] = snapshot()
                print(json.dumps(row), flush=True)
                args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
                if cycle == 0:
                    hwnd = fixture.create()
            report["parent_after_all"] = snapshot()
        report["parent_after_fixture"] = snapshot()
        assert importlib.metadata.version("wc-cuda") == report["parent_wc_cuda"] == "0.1.2+quest1"
        report["passed"] = True
        return 0
    except Exception as exc:
        report["error"] = repr(exc)
        if capture:
            report["failure_status"] = capture.status
        print(report["error"], flush=True)
        return 1
    finally:
        if capture:
            try:
                capture.close()
            except Exception as exc:
                report["cleanup_error"] = repr(exc)
            report["cleanup_result"] = capture.close_result
        report["completed"] = datetime.now().isoformat()
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())

"""Explicit actual monitor→pinned CPU→CUDA/tone-map/optional V2 proof.

Only an owned fixture's small ROI is inspected/exported as numeric checks. No
screen image, video, audio, input event, shared bridge, or host session is made.
"""
import argparse
import ctypes as C
from ctypes import wintypes as W
from dataclasses import asdict
from datetime import datetime
import gc
import hashlib
import json
from pathlib import Path
import sys
import time

import numpy as np
import psutil
import torch

from quest3d.capture import list_monitors
from quest3d.dxgi_capture import DXGISnapshotCapture, DLL_PATH, DLL_SHA256, TIMESTAMP_KIND
from quest3d.input_policy import frame_input_decision
from quest3d.stereo import StereoSynthesizer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "native/capture"))
from window_fixture import ThreadedOwnedWindowFixture


def resources():
    process = psutil.Process()
    memory = process.memory_info()
    return dict(handles=process.num_handles(), private_bytes=memory.private, rss_bytes=memory.rss,
                cuda_allocated_bytes=torch.cuda.memory_allocated(), cuda_reserved_bytes=torch.cuda.memory_reserved())


def decision(frame, output, now):
    return frame_input_decision(opt_in=True, protocol=3, requested_mode="2d", output=output,
                                 source=frame, current_frame=frame, now_ns=now)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--experimental-dxgi", action="store_true", required=True)
    parser.add_argument("--monitor", type=int, default=1)
    parser.add_argument("--frames", type=int, default=40)
    parser.add_argument("--with-ai", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 20 <= args.frames <= 40:
        raise ValueError("Bounded 20..40 actual frames required")
    if args.output.exists():
        raise FileExistsError("Preserve previous probe evidence")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    monitor = next(m for m in list_monitors() if m.index == args.monitor)
    engine = None
    if args.with_ai:
        from quest3d.depth import DepthEngine
        engine = DepthEngine()
        # Warm the established network before taking actual timed desktop frames.
        warm = torch.zeros((monitor.bounds.height, monitor.bounds.width, 4), dtype=torch.uint8, device="cuda")
        engine.infer(warm, frame_id=0, generation=0)
        del warm
    synthesizer = StereoSynthesizer(320, 180, disparity_px=0)
    report = dict(created=datetime.now().astimezone().isoformat(), dll_sha256=hashlib.sha256(DLL_PATH.read_bytes()).hexdigest(),
        expected_dll_sha256=DLL_SHA256, source_monitor=args.monitor, frames=[], ai_connected=args.with_ai,
        input_policy_opt_in_test_only=True, os_input_injected=False, bridge_published=False,
        host_runtime_changed=False, pixels_saved=False, hdr_settings_changed=False, resource_before=resources())
    try:
        with ThreadedOwnedWindowFixture() as fixture:
            b = monitor.bounds
            hwnd = fixture.create(left=b.left+64, top=b.top+64, width=320, height=240, single_paint=True)
            # Raise only our own test HWND with SWP_NOACTIVATE. Another normal
            # desktop window may otherwise cover a newly shown inactive window.
            fixture.move(hwnd, b.left+64, b.top+64, 320, 240)
            time.sleep(.1)
            observation = fixture.provider.observe(hwnd)
            own_bounds = observation.identity and observation.frame_bounds
            # The provider's full physical bounds are used only to locate our
            # own center. No user's window position or pixels enter the report.
            if own_bounds is None:
                raise RuntimeError("Own fixture has no physical bounds")
            fixture.user.WindowFromPoint.argtypes = [W.POINT]
            fixture.user.WindowFromPoint.restype = W.HWND
            report["fixture_before"] = fixture.paint_observation(hwnd)
            with DXGISnapshotCapture(monitor=args.monitor, experimental_dxgi=args.experimental_dxgi) as capture:
                first = capture.grab()
                retained = first.bgra.clone()
                old_2d = synthesizer.original_2d(first.bgra, frame_id=first.frame_id, generation=first.geometry_generation)
                time.sleep(.65)
                old_decision = decision(first, old_2d, time.perf_counter_ns())
                assert not old_decision.enabled and old_decision.reason == "capture_not_fresh"
                report["old_frame_after_650ms"] = asdict(old_decision)
                report["qpc_envelopes"] = capture.qpc_envelopes
                report["color_profile"] = capture.color_profile
                report["resource_baseline"] = resources()
                previous_id, previous_time = first.frame_id, first.captured_ns
                for index in range(args.frames):
                    started = time.perf_counter_ns()
                    frame = capture.grab()
                    assert frame.frame_id > previous_id and frame.captured_ns > previous_time
                    assert frame.timestamp_kind == TIMESTAMP_KIND
                    assert frame.source_identity == first.source_identity
                    assert frame.geometry == first.geometry
                    assert frame.native_metadata.frame_id == frame.frame_id
                    center_x = own_bounds.left + own_bounds.width//2 - b.left
                    center_y = own_bounds.top + own_bounds.height//2 - b.top
                    hit = int(fixture.user.WindowFromPoint(W.POINT(center_x+b.left, center_y+b.top)) or 0)
                    report["fixture_hit_test"] = dict(hwnd=hwnd, hit=hit, bounds=asdict(own_bounds),
                        point=[center_x+b.left, center_y+b.top], monitor_bounds=asdict(b),
                        observation=asdict(fixture.provider.observe(hwnd)))
                    assert hit == hwnd, report["fixture_hit_test"]
                    # Copy only a tiny fixture patch to CPU; whole user pixels
                    # remain in temporary capture/AI tensors and are never saved.
                    center = frame.bgra[center_y-2:center_y+3, center_x-2:center_x+3].cpu().numpy()
                    assert center.shape == (5,5,4) and np.all(center[...,3] == 255)
                    assert np.all((center[...,:3] >= 230) & (center[...,:3] <= 246)), center[0,0].tolist()
                    depth = None
                    if engine is not None:
                        depth = engine.infer(frame.bgra, frame_id=frame.frame_id, generation=frame.geometry_generation)
                        assert depth.frame_id == frame.frame_id and torch.isfinite(depth.tensor).all().item()
                    original = synthesizer.original_2d(frame.bgra, frame_id=frame.frame_id, generation=frame.geometry_generation)
                    np.testing.assert_array_equal(original.bgra[:,:320],original.bgra[:,320:])
                    permission = decision(frame,original,time.perf_counter_ns())
                    assert permission.enabled, permission.reason
                    assert torch.equal(first.bgra,retained), "Reusable buffer overwrote old GPU output"
                    report["frames"].append(dict(index=index, frame_id=frame.frame_id, captured_ns=frame.captured_ns,
                        native=asdict(frame.native_metadata), metrics=asdict(frame.metrics),
                        input_decision=asdict(permission), source_identity=asdict(frame.source_identity),
                        geometry=asdict(frame.geometry), own_center_bgra=center[2,2].tolist(),
                        depth_preprocess_ms=depth.preprocess_ms if depth else None,
                        depth_inference_ms=depth.inference_ms if depth else None,
                        iteration_ms=(time.perf_counter_ns()-started)/1e6, resources=resources()))
                    previous_id, previous_time = frame.frame_id, frame.captured_ns
                report["resource_final_retained"] = resources()
            assert torch.equal(first.bgra,retained), "Closing capture invalidated caller-owned output"
            report["fixture_after"] = fixture.paint_observation(hwnd)
            assert report["fixture_after"]["wm_paint_count"] == report["fixture_before"]["wm_paint_count"]
            report["retained_gpu_output_unchanged_after_close"] = True
            report["resource_after_close_with_outputs_retained"] = resources()
        report["completed"] = True
    except BaseException as exc:
        report["completed"] = False
        report["error"] = repr(exc)
        raise
    finally:
        args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False)+"\n", encoding="utf-8")
        print(args.output)


if __name__ == "__main__":
    main()

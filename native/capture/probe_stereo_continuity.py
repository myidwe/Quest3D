"""Opt-in real owned-HWND WGC/model/stereo continuity probe; no live session writes.

The diagnostic worker delay is an explicit fault injection before real inference.
No synthetic depth, OS input, Sunshine/Quest connection or output image is used.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime
import gc
import hashlib
import json
from pathlib import Path
import subprocess
import threading
import time

import numpy as np
import psutil
import torch

from window_fixture import ThreadedOwnedWindowFixture
from quest3d.capture import list_monitors, list_windows
from quest3d.depth import DepthEngine
from quest3d.paths import ROOT
from quest3d.session import LatestAIWorker, _DesktopProcessedSelector, select_processed
from quest3d.stereo import StereoSynthesizer
from quest3d.window_capture import WindowCaptureUnavailable
from quest3d.window_process_capture import ProcessWindowCapture


MAX_AGE_MS = 200
PROTECTED = {34980: "python.exe", 8124: "sunshine.exe"}


def digest(value):
    return hashlib.sha256(value).hexdigest()


def pixels_sha(value):
    return digest(memoryview(np.ascontiguousarray(value)).cast("B"))


def gpu_state():
    row = subprocess.check_output(["nvidia-smi", "--query-gpu=memory.total,memory.used,memory.free,utilization.gpu",
        "--format=csv,noheader,nounits"], text=True, creationflags=subprocess.CREATE_NO_WINDOW).strip()
    total, used, free, utilization = map(int, row.split(","))
    return dict(total_mib=total, used_mib=used, free_mib=free, gpu_utilization_percent=utilization)


def protected_state():
    rows = {}
    for pid, expected in PROTECTED.items():
        process = psutil.Process(pid)
        assert process.name().lower() == expected
        rows[str(pid)] = dict(name=process.name(), executable=process.exe(), created=process.create_time(),
                             command_sha256=digest(json.dumps(process.cmdline()).encode()))
    active = ROOT / "artifacts/active-session.json"
    windows = [asdict(window) for window in list_windows() if window.process_id in PROTECTED]
    for window in windows:
        window.pop("title", None)
    return dict(processes=rows, active_session_sha256=digest(active.read_bytes()), windows=windows)


def own_resources():
    process = psutil.Process()
    return dict(handles=process.num_handles(), threads=process.num_threads(), rss_bytes=process.memory_info().rss,
                children=[dict(pid=child.pid, created=child.create_time()) for child in process.children(recursive=True)])


def timing_summary(values):
    if not values:
        return None
    return dict(samples=len(values), p50_ms=float(np.percentile(values, 50)),
                p95_ms=float(np.percentile(values, 95)), max_ms=float(max(values)))


class InferenceObservation:
    def __init__(self):
        self.arm = threading.Event()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.rows = []
        self.lock = threading.Lock()
        self.delay_frame_id = None
        self.delay_entered_ns = None

    def factory(self, size):
        observation = self

        class ObservedRealDepth(DepthEngine):
            def infer(self, bgra, *, frame_id, generation):
                delayed = observation.arm.is_set() and not observation.entered.is_set()
                delay_start = time.perf_counter_ns()
                if delayed:
                    observation.delay_frame_id = frame_id
                    observation.delay_entered_ns = delay_start
                    observation.entered.set()
                    if not observation.release.wait(4):
                        raise TimeoutError("Diagnostic delay was not explicitly released")
                delay_ms = (time.perf_counter_ns() - delay_start) / 1e6 if delayed else 0.0
                before = pixels_sha(bgra)
                result = super().infer(bgra, frame_id=frame_id, generation=generation)
                assert result.tensor.is_cuda and bool(torch.isfinite(result.tensor).all())
                check_start = time.perf_counter_ns()
                depth_cpu = result.tensor.detach().cpu().numpy()
                row = dict(frame_id=frame_id, generation=generation, input_sha256=before,
                    input_unchanged=pixels_sha(bgra) == before, depth_sha256=pixels_sha(depth_cpu),
                    actual_cuda_depth=True, input_shape=list(result.input_shape),
                    preprocess_ms=result.preprocess_ms, inference_ms=result.inference_ms,
                    diagnostic_depth_readback_hash_ms=(time.perf_counter_ns() - check_start) / 1e6,
                    injected_delay_ms=delay_ms, delayed=delayed)
                assert row["input_unchanged"] and result.frame_id == frame_id and result.generation == generation
                with observation.lock:
                    observation.rows.append(row)
                return result

        return ObservedRealDepth(size)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seconds", type=int, default=30)
    args = parser.parse_args()
    if not 15 <= args.seconds <= 40:
        parser.error("--seconds must be 15..40")
    output = args.output.resolve()
    output.relative_to(ROOT.resolve())
    output.mkdir(parents=True, exist_ok=False)
    result = dict(status="running", created=datetime.now().astimezone().isoformat(),
        max_frame_age_ms=MAX_AGE_MS, normal_seconds=args.seconds, actual_wgc=False, actual_model=False,
        zero_copy=False, private_capture_only=True, output_publisher=False, network_started=False,
        quest_verified=False, audio_verified=False, input_enabled=False, images_saved=False,
        scope="Owned HWND WGC/CUDA readback/private IPC, actual pinned V2 Small and stereo, production PC selectors; no presentation device")
    capture = worker = fixture = None
    observation = InferenceObservation()
    records, source_rows, paints, pairs = [], {}, [], {}
    error = None
    try:
        result["gpu_before"] = gpu_state()
        assert result["gpu_before"]["free_mib"] >= 2500, "Insufficient isolated GPU headroom"
        result["protected_before"] = protected_state()
        result["resources_before"] = own_resources()
        result["source_sha256"] = {str(path.relative_to(ROOT)): digest(path.read_bytes()) for path in (
            Path(__file__), ROOT / "src/quest3d/session.py", ROOT / "src/quest3d/depth.py",
            ROOT / "src/quest3d/stereo.py", ROOT / "native/capture/window_fixture.py")}
        torch.set_num_threads(4)
        primary = next(monitor for monitor in list_monitors()[1:] if monitor.is_primary)
        fixture = ThreadedOwnedWindowFixture()
        fixture.__enter__()
        result["foreground_before"] = fixture.foreground_before
        hwnd = fixture.create(left=primary.bounds.right - 700, top=primary.bounds.top + 120,
                              width=640, height=400, single_paint=True)
        result["owned_hwnd"] = hwnd
        identity = fixture.provider.observe(hwnd).identity
        assert identity is not None and identity.process_id == psutil.Process().pid
        result["owned_identity"] = asdict(identity)
        capture = ProcessWindowCapture(identity, experimental_window=True)
        capture.__enter__()
        result["capture_namespace"] = capture.prefix
        result["capture_open_status"] = capture.status
        worker = LatestAIWorker(eye_width=1280, eye_height=720, ai_size=280,
                               directory=output / "ai", engine_factory=observation.factory)
        mono = StereoSynthesizer(1280, 720, disparity_px=0)
        selector = _DesktopProcessedSelector()
        current = original = None
        phase, normal_start_ns = "warmup", None
        started = time.monotonic()
        next_paint = next_tick = next_progress = started
        stall_started_ns = None
        last_pair_sequence = 0
        with (output / "selection.jsonl").open("x", encoding="utf-8", buffering=65536) as selection_log:
            while time.monotonic() - started < 80:
                now = time.monotonic()
                if now < next_tick:
                    time.sleep(min(next_tick - now, .01))
                    continue
                next_tick = now + 1 / 30
                if now >= next_paint:
                    fixture.paint(hwnd)
                    paints.append(dict(host_ns=time.perf_counter_ns(), phase=phase,
                                       observation=fixture.paint_observation(hwnd)))
                    next_paint = now + (.05 if phase == "stall" else 1.0)
                grab_started = time.perf_counter_ns()
                try:
                    fresh = capture.grab(timeout_seconds=.01)
                except (TimeoutError, WindowCaptureUnavailable):
                    fresh = None
                if fresh is not None:
                    current, original = fresh, None
                    source_rows[current.frame_id] = dict(frame_id=current.frame_id, capture_ns=current.captured_ns,
                        generation=current.geometry_generation, source_id=current.source_id,
                        source_object_id=id(current), source_identity=asdict(current.source_identity),
                        geometry=asdict(current.geometry), rgb_sha256=pixels_sha(current.bgra),
                        grab_wall_ms=(time.perf_counter_ns() - grab_started) / 1e6,
                        worker_copy_costs=capture.last_frame_diagnostics)
                    assert current.source_identity.native_handle == hwnd
                    result["actual_wgc"] = True
                    worker.submit(current, 0, 12)
                if current is None:
                    if now - started > 15:
                        raise TimeoutError("Owned source supplied no actual frame")
                    continue
                processed, ai_error, ready = worker.snapshot()
                if ai_error:
                    raise RuntimeError(ai_error)
                selected_ns = time.perf_counter_ns()
                options = dict(requested_mode="3d", revision=0, current_frame=current,
                               now_ns=selected_ns, max_age_ms=MAX_AGE_MS)
                selected, reason = selector.select(processed, **options)
                legacy, legacy_reason = select_processed(processed, **options)
                continuity = selector.snapshot()
                if selected is None:
                    if original is None:
                        original = mono.original_2d(current.bgra, frame_id=current.frame_id,
                                                    generation=current.geometry_generation)
                    source, stereo = current, original
                else:
                    source, stereo = selected.source, selected.stereo
                    assert selected.stereo.frame_id == source.frame_id
                    assert selected.stereo.generation == source.geometry_generation
                    if selected.completion_sequence not in pairs:
                        source_hash = pixels_sha(source.bgra)
                        assert source_hash == source_rows[source.frame_id]["rgb_sha256"]
                        pairs[selected.completion_sequence] = dict(frame_id=source.frame_id,
                            generation=source.geometry_generation, capture_ns=source.captured_ns,
                            rgb_sha256=source_hash, stereo_sha256=pixels_sha(stereo.bgra),
                            source_object_id=id(source), processed_object_id=id(selected),
                            eye_differing_components=int(np.count_nonzero(stereo.bgra[:, :1280, :3] != stereo.bgra[:, 1280:, :3])))
                    pair_record = pairs[selected.completion_sequence]
                    assert pair_record["capture_ns"] == source.captured_ns
                    if continuity["decision"] == "held_matching_pair":
                        assert selected is processed and source is not current
                        assert source.frame_id < current.frame_id and legacy is None
                        assert pixels_sha(source.bgra) == pair_record["rgb_sha256"]
                        assert pixels_sha(stereo.bgra) == pair_record["stereo_sha256"]
                        assert 0 <= continuity["hold_age_ms"] <= MAX_AGE_MS
                    last_pair_sequence = max(last_pair_sequence, selected.completion_sequence)
                record = dict(phase=phase, selection_ns=selected_ns, source_frame_id=source.frame_id,
                    source_ns=source.captured_ns, current_source_frame_id=current.frame_id,
                    current_source_ns=current.captured_ns, mode=stereo.mode, fallback_reason=reason,
                    legacy_mode="3d" if legacy else "2d", legacy_fallback_reason=legacy_reason,
                    selected_ai_sequence=selected.completion_sequence if selected else None,
                    observed_ai_completions=worker.frames, stereo_continuity=continuity)
                records.append(record)
                selection_log.write(json.dumps(record) + "\n")
                if phase == "warmup" and worker.frames >= 5 and continuity["decision"] == "observed_current_pair":
                    phase, normal_start_ns = "normal", selected_ns
                    result["warmup"] = dict(completions=worker.frames, completed_ns=selected_ns)
                    result["gpu_after_warmup"] = gpu_state()
                    next_paint = time.monotonic() + 1
                elif phase == "normal" and selected_ns - normal_start_ns >= args.seconds * 1e9:
                    # Arm only after the current frame was really observed and
                    # its actual capture has aged past the unchanged limit.
                    if continuity["decision"] == "observed_current_pair" and selected_ns - current.captured_ns > 250_000_000:
                        phase, stall_started_ns = "stall", selected_ns
                        result["stall_anchor_sequence"] = last_pair_sequence
                        observation.arm.set()
                        next_paint = time.monotonic()
                elif phase == "stall":
                    if selected_ns - stall_started_ns > 4e9:
                        raise TimeoutError("Real worker did not reach bounded stall expiry")
                    if observation.entered.is_set() and selected_ns - observation.delay_entered_ns >= 450_000_000:
                        assert selected is None and reason == "stale_depth_frame"
                        held = [row for row in records if row["phase"] == "stall" and row["stereo_continuity"]["decision"] == "held_matching_pair"]
                        assert held, "No actual old pair was held before expiry"
                        assert len({row["stereo_continuity"]["last_same_frame_observed_ns"] for row in held}) == 1
                        assert len({row["selected_ai_sequence"] for row in held}) == 1
                        assert len({row["current_source_frame_id"] for row in held}) >= 2
                        result["stall_expiry_observed_ns"] = selected_ns
                        observation.release.set()
                        phase = "recovery"
                        next_paint = time.monotonic() + 1
                elif phase == "recovery" and selected and selected.completion_sequence > result["stall_anchor_sequence"]:
                    result["recovery"] = record
                    break
                if now >= next_progress:
                    print(json.dumps(dict(phase=phase, seconds=round(now - started, 2),
                        captures=len(source_rows), ai_completions=worker.frames,
                        selected=stereo.mode, decision=continuity["decision"])), flush=True)
                    next_progress = now + 5
            else:
                raise TimeoutError("Probe exceeded its bounded presentation duration")
        normal = [row for row in records if row["phase"] == "normal"]
        held = [row for row in normal if row["stereo_continuity"]["decision"] == "held_matching_pair"]
        assert held, "Real source never met old-static-pair transition conditions"
        assert result.get("recovery") and any(row["eye_differing_components"] > 0 for row in pairs.values())
        result["normal"] = dict(selection_ticks=len(normal), real_repaints=sum(row["phase"] == "normal" for row in paints),
            source_frame_count=len({row["current_source_frame_id"] for row in normal}),
            legacy_stale_2d_ticks=sum(row["legacy_fallback_reason"] == "stale_depth_frame" for row in normal),
            revised_stale_2d_ticks=sum(row["fallback_reason"] == "stale_depth_frame" for row in normal),
            held_ticks=len(held), distinct_held_pairs=len({row["selected_ai_sequence"] for row in held}),
            hold_age=timing_summary([row["stereo_continuity"]["hold_age_ms"] for row in held]))
        result["actual_model"] = True
        result["status"] = "passed"
    except BaseException as exc:
        error = exc
        result.update(status="failed", error=f"{type(exc).__name__}: {exc}")
    finally:
        observation.release.set()
        cleanup_errors = []
        for name, resource, action in (("ai_worker", worker, lambda: worker.close()),
                                       ("capture", capture, lambda: capture.close()),
                                       ("fixture", fixture, lambda: fixture.__exit__())):
            if resource is not None:
                try:
                    action()
                except BaseException as exc:
                    cleanup_errors.append(f"{name}: {type(exc).__name__}: {exc}")
        if capture:
            result["capture_close"] = capture.close_result
        if worker:
            result["ai_worker_joined"] = not worker.thread.is_alive()
            result["ai_completed"] = worker.frames
        if fixture:
            result["fixture_joined"] = not fixture.thread.is_alive()
            result["foreground_after"] = int(fixture.user.GetForegroundWindow() or 0)
            result["foreground_preserved"] = result["foreground_after"] == result.get("foreground_before")
            result["owned_window_destroyed"] = not fixture.user.IsWindow(result.get("owned_hwnd", 0))
        try:
            result["protected_after"] = protected_state()
            result["protected_preserved"] = result["protected_after"] == result.get("protected_before")
            result["gpu_after"] = gpu_state()
            gc.collect()
            result["resources_after_cleanup"] = own_resources()
        except BaseException as exc:
            cleanup_errors.append(f"preservation: {type(exc).__name__}: {exc}")
        if (cleanup_errors or not result.get("protected_preserved") or
                not result.get("foreground_preserved", True) or
                (capture and not capture.close_result.get("normal_exit"))):
            result["status"] = "failed"
        result["cleanup_errors"] = cleanup_errors
        result["completed"] = datetime.now().astimezone().isoformat()
        result["copy_scope"] = {
            "capture": "Native WGC lease copy and color processing; child GPU->CPU readback, private v3 shared publication, parent raw BGRA copy; per-frame costs in source-frames.json",
            "ai": "Parent CPU BGRA preprocessing includes smaller tensor upload; CUDA V2 Small; GPU stereo synthesis and readback in ai/frames.jsonl",
            "diagnostic": "Extra depth tensor readback/hash per real inference and RGB/SBS hashes for identity; observations in depth-observations.json; measured PC total includes instrumentation",
            "excluded": "No video encoder, network, Quest display, audio or cursor composition"}
        delayed_ids = {row["frame_id"] for row in observation.rows if row["delayed"]}
        ai_path = output / "ai/frames.jsonl"
        if ai_path.exists():
            ai_rows = [json.loads(line) for line in ai_path.read_text(encoding="utf-8").splitlines()]
            clean = [row for row in ai_rows if not row["warmup"] and row["frame_id"] not in delayed_ids]
            result["normal_ai_timings_ms_excluding_warmup_and_injected_delay"] = {
                key: timing_summary([row[key] for row in clean]) for key in
                ("preprocess_ms", "inference_ms", "stereo_readback_ms", "pc_total_ms")}
            result["injected_delay_frames"] = sorted(delayed_ids)
            result["warning_ai_summary"] = "ai/summary.json is raw worker output including diagnostic delay; use filtered timings here for ordinary measured processing"
        for name, value in (("source-frames.json", list(source_rows.values())), ("paints.json", paints),
                            ("pairs.json", pairs), ("depth-observations.json", observation.rows),
                            ("verification.json", result)):
            (output / name).write_text(json.dumps(value, indent=2), encoding="utf-8")
        print(json.dumps(result, indent=2), flush=True)
    if error:
        raise error
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())

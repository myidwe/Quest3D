"""One real HDR desktop frame, repeated real depth/stereo, no image publication.

This runs beside the existing stream. Results are contended repeat-processing
costs, not unique capture FPS, maximum FPS, encoder performance or Quest quality.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import gc
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def gpu_state():
    query = "name,driver_version,memory.total,memory.used,memory.free,utilization.gpu,temperature.gpu"
    value = subprocess.check_output(["nvidia-smi", "--query-gpu=" + query, "--format=csv,noheader,nounits"],
        text=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW).strip().splitlines()
    if len(value) != 1:
        raise RuntimeError("Probe requires the known single GPU")
    name, driver, total, used, free, utilization, temperature = [x.strip() for x in value[0].split(",")]
    return dict(name=name, driver=driver, total_mib=int(total), used_mib=int(used), free_mib=int(free),
                utilization_percent=int(utilization), temperature_c=int(temperature))


def protected_state(pids):
    import psutil
    result = {}
    for pid in pids:
        process = psutil.Process(pid)
        result[str(pid)] = dict(name=process.name(), created=process.create_time(),
            executable=process.exe(), command_sha256=hashlib.sha256(json.dumps(process.cmdline()).encode()).hexdigest())
    return dict(processes=result, active_session_sha256=sha(ROOT / "artifacts/active-session.json"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True, help="Existing unpacked pinned wc_cuda+quest2")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--protect-pid", type=int, action="append", required=True)
    parser.add_argument("--samples", type=int, default=30, choices=range(1, 31))
    args = parser.parse_args()
    package = args.package.resolve(strict=True)
    output = args.output.resolve()
    output.relative_to((ROOT / "artifacts").resolve())
    output.mkdir(parents=True, exist_ok=False)
    sys.path.insert(0, str(package))
    import numpy as np
    import psutil
    import torch
    from quest3d.capture import GPUDesktopCapture, list_monitors
    from quest3d.depth import DepthEngine
    from quest3d.stereo import StereoSynthesizer
    from quest3d.assets import model_spec

    # Match the production serve default. The retained -b run used 2 and is
    # reported separately; do not silently treat it as an identical setup.
    torch.set_num_threads(4)
    tracked = [Path(__file__), ROOT / "src/quest3d/capture.py", ROOT / "src/quest3d/depth.py",
               ROOT / "src/quest3d/stereo.py", ROOT / "src/quest3d/tonemap_cuda.py", ROOT / "config/models.json"]
    before_hashes = {str(p.relative_to(ROOT)): sha(p) for p in tracked}
    report = dict(started_at=datetime.now().astimezone().isoformat(), started_monotonic_ns=time.perf_counter_ns(), own_pid=psutil.Process().pid,
        torch_cpu_threads=torch.get_num_threads(), torch_interop_threads=torch.get_num_interop_threads(),
        source_images_saved=False, output_images_saved=False, pixel_arrays_saved=False,
        visible_windows_created=False, input_injected=False, live_stream_modified=False,
        actual_unique_source_frames_used=0, inference_repetitions=0, warmup_per_profile=3,
        measured_per_profile=args.samples, profiles=[], passed=False,
        scope="One real HDR WGC frame reused; actual AI+stereo repeated with the live stream running. Not unique capture/max FPS or Quest quality.")
    capture = frame = pixels = reference = engine = synth = depth = stereo = None
    def own_memory():
        return dict(allocated_bytes=torch.cuda.memory_allocated(), reserved_bytes=torch.cuda.memory_reserved(),
                    peak_allocated_bytes=torch.cuda.max_memory_allocated(), peak_reserved_bytes=torch.cuda.max_memory_reserved())
    def timing(rows, key):
        values = [row[key] for row in rows]
        return dict(p50_ms=float(np.percentile(values, 50)), p95_ms=float(np.percentile(values, 95)),
                    max_ms=max(values), mean_ms=float(np.mean(values)))
    def producer_snapshot():
        active = json.loads((ROOT / "artifacts/active-session.json").read_text())
        return json.loads((Path(active["directory"]) / "status.json").read_text())
    try:
        report["protected_before"] = protected_state(args.protect_pid)
        report["live_producer_before"] = producer_snapshot()
        report["gpu_before"] = gpu_state()
        if report["gpu_before"]["free_mib"] < 2500:
            raise RuntimeError("Insufficient GPU headroom for an additional bounded probe")
        primary = next(m for m in list_monitors() if m.is_primary)
        capture = GPUDesktopCapture(monitor=primary.index, experimental_hdr=True, hdr_tonemap="fused")
        begin = time.perf_counter_ns()
        capture.__enter__()
        entered = time.perf_counter_ns()
        frame = capture.grab(timeout_seconds=5)
        acquired = time.perf_counter_ns()
        pixels = frame.bgra
        assert pixels.is_cuda and pixels.dtype == torch.uint8 and pixels.ndim == 3
        report["actual_unique_source_frames_used"] = 1
        report["capture"] = dict(monitor=primary.device_name, monitor_index=primary.index,
            source_shape=list(pixels.shape), frame_id=frame.frame_id,
            geometry_generation=frame.geometry_generation, captured_ns=frame.captured_ns,
            timestamp_kind=capture.timestamp_kind, backend=capture.backend, color_profile=capture.color_profile,
            initialize_ms=(entered-begin)/1e6, wait_copy_tonemap_ms=(acquired-entered)/1e6,
            reported_color_processing_ms=frame.color_processing_ms,
            capture_readback_performed=False)
        capture.close()
        report["capture"].update(callback_frames_seen=capture._frame_id,
            callbacks_overwritten=capture.dropped_frames, native_copy_skipped=capture.native_frames_skipped_during_copy,
            worker_closed=capture._worker is None)
        capture = None
        # This diagnostic clone checks original-source immutability, stays on
        # GPU, and is outside the timed production calls for every profile.
        begin = time.perf_counter_ns()
        reference = pixels.clone()
        torch.cuda.synchronize()
        report["diagnostic_source_clone"] = dict(bytes=pixels.numel()*pixels.element_size(),
            device_copy_ms=(time.perf_counter_ns()-begin)/1e6, excluded_from_profile_timings=True)
        begin = time.perf_counter_ns()
        engine = DepthEngine(280)
        report["model_initialize_ms"] = (time.perf_counter_ns()-begin)/1e6
        report["model"] = model_spec()
        report["capture_copies"] = [
            "WGC D3D11 FP16 texture -> owned CUDA allocation via pinned wc_cuda native texture lease",
            "Production fused scRGB -> BGRA8 CUDA transform once for the consumed capture",
            "One diagnostic CUDA clone for source immutability, outside measured calls",
            "Each actual infer preprocesses the same full RGB and uploads no CPU source frame",
            "Each production synthesize includes CUDA packed BGRA -> pinned CPU output copy and stream synchronization",
            "No shared-memory publication, Sunshine upload/NVENC, network or Quest path is executed by this probe",
        ]
        for width, height, disparity in ((1280,720,18.4), (1600,900,23.0), (1920,1080,27.6)):
            synth = StereoSynthesizer(width, height, disparity_px=disparity)
            profile = dict(eye_size=[width,height], packed_size=[width*2,height], disparity_px=disparity,
                relative_disparity=disparity/width, measured=[], warmup=[], gpu_before=gpu_state())
            report["profiles"].append(profile)
            for index in range(3+args.samples):
                if index == 3:
                    torch.cuda.synchronize()
                    torch.cuda.reset_peak_memory_stats()
                    profile["memory_after_warmup"] = own_memory()
                begin = time.perf_counter_ns()
                depth = engine.infer(pixels, frame_id=frame.frame_id, generation=frame.geometry_generation)
                inferred = time.perf_counter_ns()
                stereo = synth.synthesize(pixels, depth, frame_id=frame.frame_id, generation=frame.geometry_generation)
                completed = time.perf_counter_ns()
                report["inference_repetitions"] += 1
                # CPU output validation is outside measured infer/synthesize.
                assert stereo.bgra.shape == (height, width*2, 4) and stereo.mode == "3d"
                assert stereo.frame_id == frame.frame_id and stereo.generation == frame.geometry_generation
                eyes_different = not np.array_equal(stereo.bgra[:, :width, :3], stereo.bgra[:, width:, :3])
                assert eyes_different, "Real stereo output has identical eyes on this source"
                row = dict(index=index, started_ns=begin, completed_ns=completed, original_frame_id=frame.frame_id,
                    original_capture_ns=frame.captured_ns, ai_input_shape=list(depth.input_shape),
                    preprocess_ms=depth.preprocess_ms, inference_gpu_ms=depth.inference_ms,
                    infer_wall_ms=(inferred-begin)/1e6, stereo_with_readback_ms=(completed-inferred)/1e6,
                    total_ms=(completed-begin)/1e6, output_bytes=stereo.bgra.nbytes,
                    output_shape_verified=True, eyes_different=eyes_different,
                    source_pair_identity_verified=True)
                profile["warmup" if index < 3 else "measured"].append(row)
                depth = stereo = None
            profile["memory_measured_peak"] = own_memory()
            profile["gpu_after"] = gpu_state()
            profile["source_pixels_unchanged"] = bool(torch.equal(pixels, reference))
            assert profile["source_pixels_unchanged"]
            profile["timings"] = {key:timing(profile["measured"], key) for key in
                ("preprocess_ms","inference_gpu_ms","infer_wall_ms","stereo_with_readback_ms","total_ms")}
            synth = None
            gc.collect()
            torch.cuda.empty_cache()  # Only this probe's unused allocator cache, outside measurements.
        report["passed"] = True
    except BaseException as error:
        report["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        if capture is not None:
            capture.close()
        capture = frame = pixels = reference = engine = synth = depth = stereo = None
        gc.collect()
        if torch.cuda.is_initialized():
            torch.cuda.synchronize()
            torch.cuda.empty_cache()
            report["own_cuda_after_cleanup"] = own_memory()
        report["protected_after"] = protected_state(args.protect_pid)
        report["protected_unchanged"] = report.get("protected_before") == report["protected_after"]
        report["live_producer_after"] = producer_snapshot()
        report["gpu_after_cleanup"] = gpu_state()
        report["source_sha256"] = before_hashes
        report["source_after_sha256"] = {str(p.relative_to(ROOT)): sha(p) for p in tracked}
        report["source_unchanged"] = before_hashes == report["source_after_sha256"]
        report["passed"] = bool(report["passed"] and report["protected_unchanged"] and report["source_unchanged"])
        report["finished_at"] = datetime.now().astimezone().isoformat()
        report["finished_monotonic_ns"] = time.perf_counter_ns()
        (output / "verification.json").write_text(json.dumps(report, indent=2)+"\n", encoding="utf-8")
    print(json.dumps(dict(passed=report["passed"], source_frames=report["actual_unique_source_frames_used"],
        inference_repetitions=report["inference_repetitions"], profiles=[dict(eye_size=p["eye_size"],
        timings=p["timings"], memory=p["memory_measured_peak"]) for p in report["profiles"]],
        protected_unchanged=report["protected_unchanged"], directory=str(output)), indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

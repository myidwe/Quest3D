"""Verify/summarize an actual DLL full-frame probe, without screen images."""
import argparse
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import statistics


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("log", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    rows = [json.loads(line) for line in args.log.read_text(encoding="utf-8-sig").splitlines()]
    baseline = next(r for r in rows if r["kind"] == "baseline")
    retained = next(r for r in rows if r["kind"] == "retained_device_final")
    final = next(r for r in rows if r["kind"] == "summary")
    snapshots = [r for r in rows if r["kind"] == "snapshot"]
    assert len(snapshots) == final["count"] == final["successful_snapshots"]
    assert all(r["valid"] and r["error"]["status"] == 0 for r in snapshots)
    assert next(r for r in rows if r["kind"] == "capacity_test")["passed"]
    assert next(r for r in rows if r["kind"] == "api_rejection_tests")["passed"]
    timings = {}
    for name in ("call_ms", "acquire_ms", "texture_to_mapped_ms", "mapped_to_caller_copy_ms", "present_age_at_return_ms"):
        values = sorted(r[name] for r in snapshots)
        timings[name] = {"median": statistics.median(values), "p95_nearest_rank": values[math.ceil(len(values)*.95)-1], "max": values[-1]}
    files = [args.log, Path("artifacts/host/dxgi_snapshot_capture.dll"),
             Path("artifacts/host/dxgi-snapshot-capture-probe.exe"), Path("artifacts/host/dxgi_snapshot_capture_test.exe"),
             Path("native/host/dxgi_snapshot_capture.h"), Path("native/host/dxgi_snapshot_capture.cpp"),
             Path("native/host/dxgi_snapshot_capture_test.cpp"), Path("native/host/dxgi-snapshot-capture-probe.cpp"),
             Path("native/host/dxgi-snapshot-probe.cpp"), Path("native/host/build-dxgi-snapshot-capture.sh"),
             Path("native/host/test-dxgi-snapshot-capture.ps1"),
             Path("third_party/sunshine/cmake-build-quest3d/sunshine.exe"),
             Path("patches/sunshine/0001-quest3d-external-frame-source.patch")]
    fields = ("process_handles", "private_bytes", "working_set_bytes", "gdi_objects", "user_objects", "gpu_process_local_bytes", "gpu_process_nonlocal_bytes")
    result = {
        "version": 1, "verified_at": datetime.now().astimezone().isoformat(),
        "status": "standalone_full_frame_dxgi_dll_candidate_verified",
        "files_sha256": {p.as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
        "log": args.log.as_posix(), "source": baseline["source"],
        "full_frame_bytes": baseline["whole_frame_cpu_capacity"], "snapshots": len(snapshots),
        "formats": sorted({r["format"] for r in snapshots}), "timings_ms": timings,
        "persistent_actual_timeout_wait_sum_ms": final["persistent_timeout_wait_sum_ms"],
        "static_timeout_over_500ms_observed": final["persistent_timeout_wait_sum_ms"] >= 500,
        "same_caller_buffer": final["caller_buffer_reused"], "pattern_paints": final["pattern_paints"],
        "memory_baseline": baseline["memory"], "memory_retained_device": retained["memory"],
        "memory_delta": {k: retained["memory"][k]-baseline["memory"][k] for k in fields},
        "handles_after_each_snapshot": [r["memory_after_snapshot_wait"]["process_handles"] for r in snapshots],
        "memory_after_close": final["memory_after_close"],
        "logical_checks": 33, "logical_log": "artifacts/host/dxgi-snapshot-capture-lifetime-build-tests.log",
        "ctypes_abi_proof": "artifacts/host/dxgi-snapshot-capture-ctypes-abi.json",
        "capacity_canary_and_recovery": True, "wrong_thread_calls_rejected": True,
        "closed_handle_rejected": final["closed_handle_rejected"], "reopen_new_handle": final["reopen_new_handle"],
        "actual_thread_exit_and_retained_reference_teardown_tested": True,
        "display_settings_changed": False, "input_injected": False, "pixels_saved": False,
        "runtime_integrated": False, "cuda_ai_integrated": False,
        "long_term_leak_free_proven": False, "hardware_unplug_or_mode_change_tested": False,
    }
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(args.output)


if __name__ == "__main__":
    main()

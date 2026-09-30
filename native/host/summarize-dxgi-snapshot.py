"""Summarize bounded DXGI logs without loading or storing any screen image."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import statistics


def summarize(path: Path) -> dict:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines()]
    meta = next(row for row in rows if row["kind"] == "metadata")
    baseline = next(row for row in rows if row["kind"] == "baseline")
    final = next(row for row in rows if row["kind"] == "summary")
    frames = [row for row in rows if row["kind"] == "snapshot"]
    timings = {}
    for field in ("open_ms", "acquire_ms", "roi_readback_ms", "close_ms", "iteration_ms"):
        values = sorted(row[field] for row in frames if field in row)
        if values:
            timings[field] = {
                "count": len(values), "min": values[0], "median": statistics.median(values),
                "p95_nearest_rank": values[math.ceil(len(values) * .95) - 1], "max": values[-1],
            }
    quiet_span = quiet_wait = max_span = max_wait = 0.0
    quiet_start = None
    for row in (row for row in rows if row["kind"] == "persistent"):
        if row.get("acquire_hr") == "0x887a0027":
            if quiet_start is None:
                quiet_start = int(row["acquire_started_qpc_ns"])
            quiet_span = (int(row["acquire_returned_qpc_ns"]) - quiet_start) / 1e6
            quiet_wait += row["acquire_ms"]
            max_span, max_wait = max(max_span, quiet_span), max(max_wait, quiet_wait)
        else:
            quiet_start, quiet_wait = None, 0.0
    memory_fields = ("process_handles", "gdi_objects", "user_objects", "private_bytes",
                     "working_set_bytes", "gpu_process_local_bytes", "gpu_process_nonlocal_bytes")
    memory_delta = {key: final["final_memory"][key] - baseline["memory"][key]
                    for key in memory_fields if key in final["final_memory"] and key in baseline["memory"]}
    return {
        "log": path.as_posix(), "log_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "source": {key: meta[key] for key in ("monitor_ordinal", "display_name", "source_hmonitor",
                                                "physical_bounds", "rotation", "color_space", "bits_per_color")},
        "snapshots_requested": meta["count"], "snapshots_recorded": len(frames),
        "summary": final, "persistent_longest_timeout_span_ms": max_span,
        "persistent_longest_timeout_wait_sum_ms": max_wait,
        "persistent_quiet_500ms_observed": max_wait >= 500,
        "all_snapshots_valid": len(frames) == meta["count"] and all(
            row.get("open_hr") == row.get("acquire_hr") == row.get("release_hr") == "0x00000000"
            and row.get("own_pattern_valid") is True for row in frames),
        "formats": sorted({row["format"] for row in frames if "format" in row}),
        "distinct_own_sample_hashes": len({row["sample_hash_fnv64"] for row in frames if "sample_hash_fnv64" in row}),
        "all_samples_match_previous": bool(frames) and all(row.get("same_pattern_samples_as_previous") is True for row in frames),
        "timings_ms": timings, "memory_baseline": baseline["memory"], "memory_delta": memory_delta,
        "handles_after_each_close": [row["memory_after_close_and_wait"]["process_handles"] for row in frames],
        "success_is_actual_acquire_and_own_roi_and_release": True,
        "runtime_integrated": False, "zero_copy_ai_integrated": False, "long_run_leak_free_proven": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("logs", nargs="+", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = {"version": 1, "runs": [summarize(path) for path in args.logs]}
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(args.output)


if __name__ == "__main__":
    main()

"""Read retained actual probe evidence and verify RGB/depth/pair identity joins."""
import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import psutil


def summarize(values):
    return {"samples": len(values), "p50_ms": float(np.percentile(values, 50)),
            "p95_ms": float(np.percentile(values, 95)), "max_ms": float(max(values))} if values else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    directory = args.directory.resolve()
    def read(name):
        return json.loads((directory / name).read_text(encoding="utf-8"))
    verification = read("verification.json")
    assert verification["status"] == "passed"
    sources = {row["frame_id"]: row for row in read("source-frames.json")}
    depths = {row["frame_id"]: row for row in read("depth-observations.json")}
    pairs = read("pairs.json")
    selected = [json.loads(line) for line in (directory / "selection.jsonl").read_text(encoding="utf-8").splitlines()]
    ai = [json.loads(line) for line in (directory / "ai/frames.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(depths) == len(ai) == verification["ai_completed"]
    for row in ai:
        source, depth = sources[row["frame_id"]], depths[row["frame_id"]]
        assert row["capture_ns"] == source["capture_ns"]
        assert depth["input_sha256"] == source["rgb_sha256"] and depth["input_unchanged"]
        assert depth["generation"] == source["generation"] and depth["actual_cuda_depth"]
    for pair in pairs.values():
        source, depth = sources[pair["frame_id"]], depths[pair["frame_id"]]
        assert pair["capture_ns"] == source["capture_ns"]
        assert pair["rgb_sha256"] == source["rgb_sha256"] == depth["input_sha256"]
        assert pair["generation"] == source["generation"] == depth["generation"]
    held = [row for row in selected if row["stereo_continuity"]["decision"] == "held_matching_pair"]
    differing = 0
    for row in held:
        pair = pairs[str(row["selected_ai_sequence"])]
        current = sources[row["current_source_frame_id"]]
        assert pair["frame_id"] == row["source_frame_id"] < current["frame_id"]
        assert row["source_ns"] == pair["capture_ns"] < current["capture_ns"] == row["current_source_ns"]
        anchor = row["stereo_continuity"]["last_same_frame_observed_ns"]
        assert row["stereo_continuity"]["hold_age_ms"] == (row["selection_ns"] - anchor) / 1e6
        assert 0 <= row["selection_ns"] - anchor <= 200_000_000
        assert row["mode"] == "3d" and row["legacy_fallback_reason"] == "stale_depth_frame"
        differing += pair["rgb_sha256"] != current["rgb_sha256"]
    stall = [row for row in selected if row["phase"] == "stall"]
    stall_held = [row for row in stall if row["stereo_continuity"]["decision"] == "held_matching_pair"]
    anchors = {row["stereo_continuity"]["last_same_frame_observed_ns"] for row in stall_held}
    assert len(anchors) == 1
    anchor = anchors.pop()
    expired = [row for row in stall if row["fallback_reason"] == "stale_depth_frame"]
    assert expired and all(row["selection_ns"] - anchor > 200_000_000 for row in expired)
    assert all(row["source_frame_id"] == row["current_source_frame_id"] for row in expired)
    normal_ids = {row["current_source_frame_id"] for row in selected if row["phase"] == "normal"}
    normal_sources = [sources[frame_id] for frame_id in normal_ids]
    copy_costs = {}
    for label, keys in {
        "capture_grab_wall": ("grab_wall_ms",),
        "parent_raw_mapping_copy": ("worker_copy_costs", "parent_copy_ms"),
        "child_gpu_to_cpu_readback": ("worker_copy_costs", "child_metrics", "readback_wall_ms"),
        "child_shared_publish": ("worker_copy_costs", "child_metrics", "shared_publish_wall_ms"),
    }.items():
        values = []
        for source in normal_sources:
            value = source
            for key in keys:
                value = value[key]
            values.append(value)
        copy_costs[label] = summarize(values)
    ended = [verification["owned_identity"]["process_id"], verification["capture_open_status"]["child_pid"],
             verification["capture_open_status"]["launcher_pid"]]
    assert all(not psutil.pid_exists(pid) for pid in ended), "Owned diagnostic PID still exists (or was reused); inspect lifetime"
    output = dict(status="passed", scope="Artifact joins and post-exit owned PID absence; no new GPU or capture run",
        real_depth_records=len(depths), completed_pair_records=len(pairs), held_ticks=len(held),
        held_with_different_current_rgb_checksum=differing,
        stall_held_ticks=len(stall_held), stall_distinct_new_frames=len({row["current_source_frame_id"] for row in stall_held}),
        stall_first_expired_after_last_same_frame_ms=(expired[0]["selection_ns"] - anchor) / 1e6,
        injected_delay_ms=[row["injected_delay_ms"] for row in depths.values() if row["delayed"]],
        capture_copy_costs_ms=copy_costs, actual_ai_shapes=sorted({tuple(row["input_shape"]) for row in depths.values()}),
        owned_pids_absent=ended, verification_sha256=hashlib.sha256((directory / "verification.json").read_bytes()).hexdigest())
    target = directory / "identity-analysis.json"
    with target.open("x", encoding="utf-8") as file:
        json.dump(output, file, indent=2)
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()

"""Prove process containment of the WGC pool leak on one owned HWND.

This measures native captures and process exit, not cross-process frame transfer,
CUDA, AI, or product integration. Evidence is separate for every created child.
"""
import argparse
from dataclasses import asdict
from datetime import datetime
import gc
import hashlib
import importlib.util
import json
from pathlib import Path
import threading
import time

import psutil
from window_fixture import ThreadedOwnedWindowFixture

spec = importlib.util.spec_from_file_location("lifetime_probe", Path(__file__).with_name("probe-window-lifetime.py"))
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def snapshot(process):
    memory = process.memory_info()
    return dict(handles=process.num_handles(), threads=process.num_threads(),
        private_commit_bytes=memory.private, rss_bytes=memory.rss)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--cycles", type=int, default=5)
    parser.add_argument("--stage", choices=["running", "d3d"], default="running")
    args = parser.parse_args()
    if args.output_dir.exists():
        raise FileExistsError("Choose a fresh evidence directory")
    if not 2 <= args.cycles <= 10:
        raise ValueError("Use 2..10 bounded child processes")
    args.output_dir.mkdir(parents=True)
    # Explicit name matches the pinned local build, not an arbitrary executable.
    executable = Path("third_party/wc_cuda/target/release/deps/_wc_cuda-e96466e6351ce2f9.exe").resolve()
    report = dict(created=datetime.now().isoformat(), cycles=[],
        scope=f"native {args.stage}; same owned HWND; 20 iterations per child; no frame IPC/CUDA/AI",
        actual_wgc_frames=args.stage == "running",
        executable=str(executable), executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest())
    process = psutil.Process()
    print("Starting bounded owned-window process containment probe", flush=True)
    gc.collect()
    report["before_fixture_create"] = snapshot(process)
    try:
        with ThreadedOwnedWindowFixture() as fixture:
            hwnd = fixture.create()
            identity = fixture.provider.observe(hwnd).identity
            report["source"] = asdict(identity)
            gc.collect()
            report["before_first_child"] = snapshot(process)
            for cycle in range(args.cycles):
                assert fixture.provider.observe(hwnd).identity == identity
                fixture.paint(hwnd)
                gc.collect()
                before = snapshot(process)
                result = probe.run_stage(executable, args.stage, "fresh", "dispatcher", hwnd, hwnd, fixture)
                child_pid = result["child_pid"]
                evidence = args.output_dir / f"child-{cycle:02d}.json"
                evidence.write_text(json.dumps(result, indent=2), encoding="utf-8")
                summary = dict(cycle=cycle, child_pid=child_pid, exit_code=result["exit_code"],
                    evidence=str(evidence), failure=result["failure"], before=before,
                    first_frame_ms=result["spawn_to_first_actual_frame_observed_ms"],
                    child_first_handles=result["records"][0]["handles"] if result["records"] else None,
                    child_last_handles=result["records"][-1]["handles"] if result["records"] else None,
                    child_elapsed_seconds=result["elapsed_seconds"])
                del result
                gc.collect()  # Drop diagnostic Popen/report references after real wait/exit.
                summary["after_reap"] = snapshot(process)
                summary["settled_samples"] = []
                for delay in (.25, .75):
                    time.sleep(delay)
                    summary["settled_samples"].append(snapshot(process))
                summary["python_threads"] = [thread.name for thread in threading.enumerate()]
                summary["child_still_exists"] = psutil.pid_exists(child_pid)
                report["cycles"].append(summary)
                print(json.dumps(summary), flush=True)
                if summary["failure"] or summary["child_still_exists"]:
                    return 1
            report["after_last_child"] = snapshot(process)
        gc.collect()
        report["after_fixture_close"] = snapshot(process)
        report["after_fixture_settled"] = []
        for delay in (.25, .75, 2):
            time.sleep(delay)
            report["after_fixture_settled"].append(snapshot(process))
        report["final_python_threads"] = [thread.name for thread in threading.enumerate()]
        baseline = report["cycles"][0]["after_reap"]
        final = report["cycles"][-1]["after_reap"]
        report["post_first_cycle_to_last_delta"] = {key: final[key] - baseline[key] for key in baseline}
        baseline = report["cycles"][0]["settled_samples"][-1]
        final = report["cycles"][-1]["settled_samples"][-1]
        report["settled_post_first_cycle_to_last_delta"] = {key: final[key] - baseline[key] for key in baseline}
        report["product_frame_transfer_verified"] = False
        report["in_process_pool_leak_fixed"] = False
        return 0
    finally:
        report["completed"] = datetime.now().isoformat()
        (args.output_dir / "summary.json").write_text(json.dumps(report, indent=2), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())

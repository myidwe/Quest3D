"""Isolate actual HWND WGC stages, only on this process's generated fixture.

Each stage is a fresh native child. Observe its handles/threads after joined work,
then acknowledge the observation. A timeout is failure, never cleanup success.
No CUDA, foreign pixels, production wheel, routing, or live host is touched.
"""
import argparse
from dataclasses import asdict
from datetime import datetime
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import threading
import time

import psutil
from window_fixture import ThreadedOwnedWindowFixture


def run_stage(executable, stage, mode, pool_mode, hwnd, other_hwnd, fixture):
    started = time.monotonic()
    env = dict(os.environ, QUEST3D_PROBE_STAGE=stage, QUEST3D_PROBE_THREAD_MODE=mode,
               QUEST3D_PROBE_POOL_MODE=pool_mode, QUEST3D_PROBE_HWND=str(hwnd),
               QUEST3D_PROBE_OTHER_HWND=str(other_hwnd), QUEST3D_PROBE_PID=str(os.getpid()))
    test = "actual_hwnd_reused_pool_lifetime" if stage in ("reuse", "resize") else "actual_hwnd_stage_lifetime"
    child = subprocess.Popen([str(executable), "--ignored", "--nocapture", test],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", env=env,
        creationflags=subprocess.CREATE_NO_WINDOW)
    lines = queue.Queue()
    def collect():
        try:
            for line in child.stdout:
                lines.put(line)
        finally:
            lines.put(None)
    reader = threading.Thread(target=collect, daemon=True)
    reader.start()
    observed, output = [], []
    first_frame_ms = None
    failure = None
    try:
        while True:
            remaining = 45 - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError("Native stage exceeded bounded diagnostic deadline")
            line = lines.get(timeout=remaining)
            if line is None:
                break
            output.append(line)
            if "Q3D_ACTUAL_FRAME" in line and first_frame_ms is None:
                first_frame_ms = (time.monotonic() - started) * 1000
            match = re.search(r"Q3D_LIFETIME iteration=(\d+) handles=(\d+)", line)
            if match:
                process = psutil.Process(child.pid)
                observed.append(dict(iteration=int(match[1]), native_handles=int(match[2]),
                    handles=process.num_handles(), threads=process.num_threads(),
                    rss_bytes=process.memory_info().rss, elapsed_seconds=time.monotonic() - started))
                if stage == "resize":
                    wide = int(match[1]) % 2 == 0
                    fixture.move(hwnd, 2700, 200, 630 if wide else 420, 410 if wide else 300)
                child.stdin.write("observed\n")
                child.stdin.flush()
        code = child.wait(timeout=3)
        if code != 0 or len(observed) != (40 if stage in ("reuse", "resize") else 20):
            raise RuntimeError(f"Stage failed: exit={code}, rows={len(observed)}")
    except Exception as exc:
        failure = repr(exc)
    finally:
        if child.poll() is None:
            child.terminate()  # Only the exact child created above; a failed stage.
            child.wait(timeout=5)
        reader.join(timeout=3)
        child.stdin.close()
        child.stdout.close()
    result = dict(stage=stage, thread_mode=mode, pool_mode=pool_mode, child_pid=child.pid,
        exit_code=child.returncode, elapsed_seconds=time.monotonic() - started,
        records=observed, output="".join(output), failure=failure)
    result["spawn_to_first_actual_frame_observed_ms"] = first_frame_ms
    if observed:
        result["post_warmup_handle_delta"] = observed[-1]["handles"] - observed[0]["handles"]
        result["thread_range"] = [min(x["threads"] for x in observed), max(x["threads"] for x in observed)]
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--executable", type=Path, default=Path(
        "third_party/wc_cuda/target/release/deps/_wc_cuda-e96466e6351ce2f9.exe"))
    parser.add_argument("--stages", nargs="+", default=["d3d", "item", "queue", "direct", "pool", "session", "running"])
    parser.add_argument("--thread-modes", nargs="+", choices=["fresh", "shared"], default=["fresh"])
    parser.add_argument("--pool-mode", choices=["dispatcher", "free"], default="dispatcher")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("Keep prior evidence; choose a new output path")
    report = dict(created=datetime.now().isoformat(), scope="generated owned HWND only; no CUDA",
        executable=str(args.executable.resolve()), stages=[])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with ThreadedOwnedWindowFixture() as fixture:
            hwnd = fixture.create()
            other_hwnd = fixture.create(left=3170, top=200, width=530, height=380, color=0x004080)
            report["source"] = asdict(fixture.provider.observe(hwnd).identity)
            report["other_source"] = asdict(fixture.provider.observe(other_hwnd).identity)
            for mode in args.thread_modes:
                for stage in args.stages:
                    fixture.paint(hwnd)
                    result = run_stage(args.executable.resolve(), stage, mode, args.pool_mode, hwnd, other_hwnd, fixture)
                    report["stages"].append(result)
                    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
                    print(json.dumps({key: result.get(key) for key in
                        ("stage", "thread_mode", "post_warmup_handle_delta", "thread_range", "failure")}), flush=True)
                    if result["failure"]:
                        return 1
    finally:
        report["completed"] = datetime.now().isoformat()
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

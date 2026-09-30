"""One initial paint; no source animation/repaint/resize or timestamp substitution."""
import argparse
from dataclasses import asdict
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time

import psutil
from window_fixture import ThreadedOwnedWindowFixture


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("Preserve existing evidence; choose a new output")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    executable = Path("third_party/wc_cuda/target/release/deps/_wc_cuda-e96466e6351ce2f9.exe").resolve()
    report = dict(created=datetime.now().isoformat(), records=[], scope="owned static HWND, native FP16, no CUDA or product input",
        executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(), cuda_lease_validated=False,
        input_permission_enabled=False)
    child = None
    logs = []
    try:
        with ThreadedOwnedWindowFixture() as fixture:
            hwnd = fixture.create(single_paint=True)
            report["identity"] = asdict(fixture.provider.observe(hwnd).identity)
            initial_paint = fixture.paint_observation(hwnd)
            report["initial_paint"] = initial_paint
            assert initial_paint["wm_paint_count"] == 1 and initial_paint["phase"] == 0
            env = dict(os.environ, QUEST3D_PROBE_HWND=str(hwnd), QUEST3D_PROBE_PID=str(os.getpid()))
            child = subprocess.Popen([str(executable), "--ignored", "--nocapture", "actual_static_same_size_refresh"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", env=env, creationflags=subprocess.CREATE_NO_WINDOW)
            lines = queue.Queue()
            def collect():
                try:
                    for line in child.stdout:
                        lines.put(line)
                finally:
                    lines.put(None)
            reader = threading.Thread(target=collect, daemon=True)
            reader.start()
            deadline = time.monotonic() + 100
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Native static-refresh probe did not finish")
                line = lines.get(timeout=remaining)
                if line is None:
                    break
                logs.append(line)
                if line.startswith("Q3D_REFRESH "):
                    data = json.loads(line[len("Q3D_REFRESH "):])
                    process = psutil.Process(child.pid)
                    data["os_threads"] = process.num_threads()
                    data["private_commit_bytes"] = process.memory_info().private
                    data["paint_observation"] = fixture.paint_observation(hwnd)
                    assert data["paint_observation"] == initial_paint, "Source painted after the one initial paint"
                    report["records"].append(data)
                    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
                    print(json.dumps(data), flush=True)
                    child.stdin.write("observed\n")
                    child.stdin.flush()
            report["exit_code"] = child.wait(timeout=3)
            reader.join(timeout=3)
            assert report["exit_code"] == 0 and 20 <= len(report["records"]) <= 25
            report["final_paint"] = fixture.paint_observation(hwnd)
            assert report["final_paint"] == initial_paint
            report["actual_refresh_frames"] = sum(not item["timed_out"] for item in report["records"])
            report["no_new_frame_count"] = sum(item["timed_out"] for item in report["records"])
            report["same_size_refresh_verified"] = report["no_new_frame_count"] == 0
            report["existing_inner_handler_lease_guard_verified"] = any(
                item["lease_held"] and not item["timed_out"] for item in report["records"])
            if not report["same_size_refresh_verified"]:
                return 2  # Valid negative experiment, never a successful feature.
        return 0
    except Exception as exc:
        report["failure"] = repr(exc)
        report["same_size_refresh_verified"] = False
        print(report["failure"], flush=True)
        return 1
    finally:
        if child is not None:
            if child.poll() is None:
                child.terminate()  # Only our child; failure, never a successful stop.
                child.wait(timeout=5)
                report["forced_child_stop"] = True
            report["exit_code"] = child.returncode
            child.stdin.close()
            child.stdout.close()
        report["completed"] = datetime.now().isoformat()
        report["native_output"] = "".join(logs)
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())

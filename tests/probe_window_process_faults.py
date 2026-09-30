"""Own-window/own-process faults only; intentional crashes never count as clean exit."""
import argparse
from dataclasses import asdict
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import psutil

from probe_window_process_capture import ThreadedOwnedWindowFixture, take, record
from quest3d.window_capture import WindowCaptureFailed
from quest3d.window_process_capture import ProcessWindowCapture
from quest3d.window_sources import WindowIdentity


def helper(identity_json):
    capture = ProcessWindowCapture(WindowIdentity(**json.loads(identity_json)), experimental_window=True)
    capture.__enter__()
    print("FAULTREADY " + json.dumps(capture.status), flush=True)
    if sys.stdin.readline().strip() != "exit-own-parent":
        capture.close()
        return 2
    os._exit(77)  # Deliberately bypass __exit__: Job ownership must contain child.


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--helper")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.helper:
        return helper(args.helper)
    if not args.output or args.output.exists():
        raise FileExistsError("Use a new evidence output path")
    report = dict(created=datetime.now().isoformat(), input_enabled=False, intentional_faults=True)
    capture = process = None
    try:
        with ThreadedOwnedWindowFixture() as fixture:
            hwnd = fixture.create()
            identity = fixture.provider.observe(hwnd).identity
            capture = ProcessWindowCapture(identity, experimental_window=True)
            capture.__enter__()
            frame, _ = take(capture, fixture, hwnd)
            report["before_fault"] = record("actual_before_kill", frame, capture)
            frozen_hash = hashlib.sha256(frame.bgra.tobytes()).hexdigest()
            child = psutil.Process(capture.status["child_pid"])
            started = time.perf_counter_ns()
            child.terminate()  # Only the actual child created immediately above.
            child.wait(5)
            try:
                capture.grab(timeout_seconds=.1)
                raise AssertionError("Dead child returned buffered pixels")
            except WindowCaptureFailed as exc:
                report["dead_child_grab"] = str(exc)
            try:
                capture.close()
                raise AssertionError("Crashed worker was reported as clean")
            except WindowCaptureFailed as exc:
                report["crash_close_error"] = str(exc)
            report["crash_close"] = capture.close_result
            report["crash_detection_and_cleanup_ms"] = (time.perf_counter_ns() - started) / 1e6
            assert not capture.close_result["normal_exit"] and capture.close_result["actual_worker_exited"]
            assert hashlib.sha256(frame.bgra.tobytes()).hexdigest() == frozen_hash
            capture = None

            process = subprocess.Popen([sys.executable, "-u", str(Path(__file__).resolve()),
                "--helper", json.dumps(asdict(identity), separators=(",", ":"))],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, creationflags=subprocess.CREATE_NO_WINDOW)
            # Helper bootstrap is bounded by ProcessWindowCapture startup (20s).
            lines = []
            for line in process.stdout:
                if line.startswith("FAULTREADY "):
                    state = json.loads(line[len("FAULTREADY "):])
                    break
                lines.append(line[-2048:])
            else:
                raise AssertionError(f"Own helper ended before ready: {lines[-8:]}")
            actual_worker = psutil.Process(state["child_pid"])
            assert actual_worker.is_running()
            report["parent_death_before"] = state
            started = time.perf_counter_ns()
            process.stdin.write("exit-own-parent\n")
            process.stdin.flush()
            assert process.wait(5) == 77
            actual_worker.wait(5)
            report["parent_death_job_reaped_worker_ms"] = (time.perf_counter_ns() - started) / 1e6
            report["parent_death_normal_exit"] = False
            report["parent_death_expected_exit"] = 77
            report["worker_alive_after_parent_death"] = actual_worker.is_running()
            assert not report["worker_alive_after_parent_death"]
        report["passed"] = True
        return 0
    except Exception as exc:
        report["error"] = repr(exc)
        return 1
    finally:
        if capture:
            try:
                capture.close()
            except Exception as exc:
                report["cleanup_error"] = repr(exc)
        if process:
            if process.poll() is None:
                process.kill()
                process.wait(5)
                report["helper_forced_cleanup"] = True
            process.stdin.close()
            process.stdout.close()
            process._handle.Close()
        report["completed"] = datetime.now().isoformat()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report), flush=True)


if __name__ == "__main__":
    raise SystemExit(main())

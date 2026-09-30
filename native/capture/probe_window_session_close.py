"""Destroy an owned HWND during real serve/WGC/V2 Small, at most five cycles.

Only private publisher/active-session destinations and an observational capture
factory wrapper are installed. No production source, live session, host, input,
Quest or dependency is changed. Every cycle retains its failure evidence.
"""
from __future__ import annotations

import argparse
import ctypes as C
from ctypes import wintypes as W
from contextlib import ExitStack
import json
from pathlib import Path
import threading
import time
import traceback
from types import SimpleNamespace
import uuid

import psutil

from probe_window_session import PrivateObservedPublisher, preserve_state, sha
from window_fixture import ThreadedOwnedWindowFixture
from quest3d import session
from quest3d.capture import list_monitors
from quest3d.paths import ROOT
from quest3d.session_control import read_json, send_control
from quest3d.window_capture import WindowCaptureFailed
from quest3d.window_process_capture import _WinAPI


def protected_state(pids):
    state = preserve_state()
    state["producer"].sort(key=lambda item: item["pid"])
    state["explicit_processes"] = []
    for pid in pids:
        process = psutil.Process(pid)
        state["explicit_processes"].append(dict(pid=pid, name=process.name(),
            birth=process.create_time(), command_sha256=sha(json.dumps(process.cmdline()).encode())))
    return state


def run_cycle(output, index, minimized, pids):
    output.mkdir(exist_ok=False)
    directory = output / "session"
    result = dict(index=index, minimized_before_destroy=minimized, passed=False,
                  stages=[], exceptions=[], helper_errors=[], input_enabled=False,
                  quest_verified=False, audio_verified=False, encoder_verified=False)
    publisher, artifact_dir, selected_capture = session.FramePublisher, session.ARTIFACT_DIR, session.selected_window_capture
    capture_refs, handles = [], []
    thread = None
    api = _WinAPI()
    api.k.GetExitCodeProcess.argtypes = [W.HANDLE, C.POINTER(W.DWORD)]
    api.k.GetExitCodeProcess.restype = W.BOOL
    PrivateObservedPublisher.prefix = "Local\\Quest3D.Verify.WindowSession." + uuid.uuid4().hex
    PrivateObservedPublisher.rows, PrivateObservedPublisher.closed = [], False
    result["namespace"] = PrivateObservedPublisher.prefix

    def selected(args):
        capture = selected_capture(args)
        capture_refs.append(capture)
        return capture

    def status():
        try:
            return read_json(directory / "status.json")
        except FileNotFoundError:
            return {}

    def stop_private_session():
        if thread is None or not thread.is_alive():
            return
        result["helper_stop_requested"] = True
        try:
            send_control(directory, stop=True)
        except (FileNotFoundError, RuntimeError):
            pass
        thread.join(15)
        if thread.is_alive():
            # The exact owned capture's Job remains the containment boundary.
            # Any need for this cleanup is a failed cycle, never normal exit.
            result["helper_capture_close_requested"] = True
            for capture in capture_refs:
                try:
                    capture.close()
                except BaseException:
                    result["helper_errors"].append(traceback.format_exc())
            thread.join(15)
        if thread.is_alive():
            raise RuntimeError("Owned session thread remained live after explicit capture cleanup")

    try:
        result["protected_before"] = protected_state(pids)
        session.FramePublisher, session.selected_window_capture = PrivateObservedPublisher, selected
        session.ARTIFACT_DIR = output / "private-active"
        session.ARTIFACT_DIR.mkdir()
        with ExitStack() as stack:
            fixture = stack.enter_context(ThreadedOwnedWindowFixture())
            result["foreground_before"] = fixture.foreground_before
            monitor = next(m for m in list_monitors()[1:] if m.is_primary)
            hwnd = fixture.create(left=monitor.bounds.right - 700, top=monitor.bounds.top + 120,
                                  width=640, height=400, single_paint=True)
            result["owned_hwnd"] = hwnd
            options = SimpleNamespace(output=str(directory), window=hwnd, experimental_window=True,
                bridge_protocol=3, mode="3d", disparity=12, cpu_threads=4, eye_width=1280,
                eye_height=720, ai_size=280, seconds=75, fps=30, max_frame_age_ms=200,
                monitor=1, rect=None, file=None, enable_input=False, hide_cursor=True)

            def serve():
                try:
                    result["serve_return"] = session.serve(options)
                except BaseException as error:
                    result["exceptions"].append(dict(type=type(error).__name__, message=str(error),
                        terminal_window_failure=isinstance(error, WindowCaptureFailed), traceback=traceback.format_exc()))

            thread = threading.Thread(target=serve, name=f"Private window destruction {index}")
            thread.start()
            # On setup failure, stop capture while the fixture UI still exists.
            stack.callback(stop_private_session)

            def wait_for(predicate, label, timeout=25, paint=True):
                deadline, next_paint = time.monotonic() + timeout, 0
                while time.monotonic() < deadline:
                    current = status()
                    if result["exceptions"]:
                        raise RuntimeError("Session failed before intended destruction")
                    if predicate(current):
                        result["stages"].append(dict(label=label, host_ns=time.perf_counter_ns(), status=current))
                        print(f"cycle {index}: {label}", flush=True)
                        return current
                    if paint and time.monotonic() >= next_paint:
                        fixture.paint(hwnd)
                        next_paint = time.monotonic() + .3
                    time.sleep(.02)
                raise TimeoutError(label)

            first = wait_for(lambda state: state.get("effective_mode") == "3d" and state.get("ai_frames", 0) >= 2
                and any(not row["eyes_equal"] for row in PrivateObservedPublisher.rows), "real WGC and V2 Small are publishing 3D")
            worker = first["window_capture"]
            result["worker_before_destroy"] = worker
            # Independently retained read-only process handles survive production
            # close(), so a 'closed' message cannot conceal the actual exit code.
            for role, pid, expected_birth in (("worker", worker["child_pid"], worker["child_creation_filetime"]),
                                              ("launcher", worker["launcher_pid"], None)):
                assert pid not in pids
                handle = api.k.OpenProcess(0x101000, False, pid)  # query + synchronize only
                if not handle:
                    raise C.WinError(C.get_last_error())
                birth = api.birth(handle)
                handles.append((role, pid, birth, handle))
                if expected_birth is not None:
                    assert birth == expected_birth
            if minimized:
                fixture.show(hwnd, 6)
                wait_for(lambda state: state.get("capture_unavailable") and state.get("effective_mode") == "2d",
                         "owned source is minimized with retained 2D", paint=False)
            assert thread.is_alive() and len(capture_refs) == 1
            result["destroy_requested_ns"] = time.perf_counter_ns()
            fixture.close_window(hwnd)
            result["destroy_returned_ns"] = time.perf_counter_ns()
            result["destroy_verified"] = not bool(fixture.user.IsWindow(hwnd))
            assert result["destroy_verified"]
            # Do not send Stop: production must see the actual source loss.
            thread.join(20)
            result["natural_terminal_completion"] = not thread.is_alive()
            if thread.is_alive():
                stop_private_session()
            result["final_status"] = status()
            result["close_result"] = capture_refs[0].close_result
            result["selected_capture_count"] = len(capture_refs)
            result["publisher_closed"] = PrivateObservedPublisher.closed
            result["live_ai_threads"] = [t.name for t in threading.enumerate() if t.name == "Quest3D AI"]
            result["process_exits"] = []
            for role, pid, birth, handle in handles:
                signaled = api.k.WaitForSingleObject(handle, 3000) == 0
                code = W.DWORD()
                if not api.k.GetExitCodeProcess(handle, C.byref(code)):
                    raise C.WinError(C.get_last_error())
                result["process_exits"].append(dict(role=role, pid=pid, birth=birth, signaled=signaled,
                    exit_code=int(code.value), exit_hex=f"0x{code.value:08X}"))
            result["foreground_after"] = int(fixture.user.GetForegroundWindow() or 0)
        result["protected_after"] = protected_state(pids)
        assert result["protected_after"] == result["protected_before"]
        assert result["natural_terminal_completion"] and not result.get("helper_stop_requested", False)
        assert len(result["exceptions"]) == 1 and result["exceptions"][0]["terminal_window_failure"]
        assert result["final_status"].get("running") is False
        assert result["publisher_closed"] and not result["live_ai_threads"]
        assert result["selected_capture_count"] == 1
        assert all(process["signaled"] and process["exit_code"] == 0 for process in result["process_exits"])
        assert result["close_result"]["normal_exit"] and not result["close_result"]["forced"]
        assert result["foreground_after"] == result["foreground_before"]
        result["passed"] = True
    except BaseException:
        result["helper_errors"].append(traceback.format_exc())
    finally:
        try:
            stop_private_session()
        except BaseException:
            result["helper_errors"].append(traceback.format_exc())
        for _, _, _, handle in handles:
            api.k.CloseHandle(handle)
        session.FramePublisher, session.ARTIFACT_DIR, session.selected_window_capture = publisher, artifact_dir, selected_capture
        rows = PrivateObservedPublisher.rows
        (output / "ipc-observation.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        result["actual_ipc_frames"] = len(rows)
        result["actual_unequal_eye_frames"] = sum(not row["eyes_equal"] for row in rows)
        try:
            result["protected_finally"] = protected_state(pids)
            result["protected_unchanged"] = result.get("protected_before") == result["protected_finally"]
        except BaseException:
            result["helper_errors"].append(traceback.format_exc())
            result["protected_unchanged"] = False
        (output / "verification.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: result.get(key) for key in ("index", "passed", "actual_ipc_frames", "process_exits", "protected_unchanged")}))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cycles", type=int, choices=range(1, 6), default=5)
    parser.add_argument("--protected-pids", type=int, nargs=3, default=[37200, 33436, 42148])
    args = parser.parse_args()
    output = args.output.resolve()
    output.relative_to((ROOT / "artifacts").resolve())
    output.mkdir(parents=True, exist_ok=False)
    report = dict(source_sha256={str(path.relative_to(ROOT)): sha(path.read_bytes()) for path in (
        Path(__file__), ROOT / "native/capture/probe_window_session.py", ROOT / "src/quest3d/session.py",
        ROOT / "src/quest3d/window_process_capture.py", ROOT / "src/quest3d/window_capture.py",
        ROOT / "src/quest3d/window_worker.py")}, cycles=[])
    for index in range(1, args.cycles + 1):
        result = run_cycle(output / f"cycle-{index}", index, index % 2 == 0, args.protected_pids)
        report["cycles"].append(result)
        (output / "verification.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        if not result.get("protected_unchanged") or not result.get("natural_terminal_completion"):
            break
        if any(not process["signaled"] for process in result.get("process_exits", [])):
            break
    report["completed_cycles"] = len(report["cycles"])
    report["clean_terminal_cycles"] = sum(cycle["passed"] for cycle in report["cycles"])
    report["access_violation_cycles"] = sum(any(process["exit_code"] == 0xC0000005 for process in cycle.get("process_exits", [])) for cycle in report["cycles"])
    (output / "verification.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("completed_cycles", "clean_terminal_cycles", "access_violation_cycles")}))
    raise SystemExit(0 if report["clean_terminal_cycles"] == args.cycles else 1)


if __name__ == "__main__":
    main()

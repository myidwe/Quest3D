"""Owned HWND -> production serve -> real V2 Small -> private v3 IPC.

Only publisher namespace and active-session directory are redirected. No live
host, network, input or Quest state is changed; no screenshots are saved.
"""
from __future__ import annotations

import argparse
import ctypes as C
from contextlib import ExitStack
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import threading
import time
import traceback
from types import SimpleNamespace
import uuid

import numpy as np
import psutil

from window_fixture import ThreadedOwnedWindowFixture
from quest3d import session
from quest3d.bridge import FramePublisher, ORIGINAL_2D, CAPTURE_RECEIPT, INPUT_ENABLED
from quest3d.bridge_v3 import SourceFrameHeader, HEADER_SIZE_V3
from quest3d.capture import list_monitors
from quest3d.paths import ROOT
from quest3d.session_control import read_json, send_control
from quest3d.window_process_capture import _WinAPI, CAPACITY


def sha(data):
    return hashlib.sha256(data).hexdigest()


def preserve_state():
    active = ROOT / "artifacts/active-session.json"
    host = ROOT / "artifacts/host/dev/process.json"
    directory = Path(read_json(active)["directory"])
    producer_candidates = []
    for process in psutil.process_iter(["pid", "name", "cmdline", "create_time"]):
        command = process.info["cmdline"] or []
        if (process.info["name"] or "").lower() == "python.exe" and any(
                str(directory.name) in arg for arg in command):
            producer_candidates.append(dict(pid=process.pid, birth=process.create_time(),
                                            command_sha256=sha(json.dumps(command).encode())))
    assert producer_candidates, "No live producer found; re-evaluate isolation before running"
    process = psutil.Process(read_json(host)["process_id"])
    assert process.name().lower() == "sunshine.exe"
    return dict(active_sha256=sha(active.read_bytes()), host_state_sha256=sha(host.read_bytes()),
                producer=producer_candidates, host=dict(pid=process.pid, birth=process.create_time()))


class PrivateObservedPublisher(FramePublisher):
    """Actual publisher plus independently opened read-only Win32 mapping view."""
    prefix = None
    rows = []
    closed = False

    def __init__(self, *, version):
        assert version == 3 and self.prefix.startswith("Local\\Quest3D.Verify.WindowSession.")
        super().__init__(self.prefix, version=version)
        self.reader_api = _WinAPI()
        self.reader_handle = self.reader_api.k.OpenFileMappingW(4, False, self.prefix + ".v3")
        if not self.reader_handle:
            raise C.WinError(C.get_last_error())
        self.reader_view = self.reader_api.k.MapViewOfFile(self.reader_handle, 4, 0, 0, CAPACITY)
        if not self.reader_view:
            raise C.WinError(C.get_last_error())

    def publish(self, bgra, **metadata):
        success = super().publish(bgra, **metadata)
        if not success:
            return False
        started = time.perf_counter_ns()
        assert self.kernel.WaitForSingleObject(self.mutex, 1000) == 0
        try:
            header = SourceFrameHeader.unpack(C.string_at(self.reader_view, HEADER_SIZE_V3))
            payload = C.string_at(self.reader_view + HEADER_SIZE_V3, header.width * header.height * 4)
        finally:
            self.kernel.ReleaseMutex(self.mutex)
        image = np.frombuffer(payload, np.uint8).reshape(header.height, header.width, 4)
        assert header.frame_id == metadata["frame_id"]
        assert header.source_identity == metadata["source_identity"]
        assert sha(payload) == sha(memoryview(np.ascontiguousarray(bgra)).cast("B"))
        assert not header.flags & INPUT_ENABLED
        half = header.width // 2
        self.rows.append(dict(host_ns=time.perf_counter_ns(), header=asdict(header),
            payload_sha256=sha(payload), eyes_equal=bool(np.array_equal(image[:, :half], image[:, half:])),
            diagnostic_readback_hash_ms=(time.perf_counter_ns() - started) / 1e6))
        return True

    def close(self):
        if getattr(self, "reader_view", None):
            self.reader_api.k.UnmapViewOfFile(self.reader_view)
            self.reader_view = None
        if getattr(self, "reader_handle", None):
            self.reader_api.k.CloseHandle(self.reader_handle)
            self.reader_handle = None
        super().close()
        type(self).closed = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.relative_to((ROOT / "artifacts").resolve())
    output.mkdir(parents=True, exist_ok=False)
    directory = output / "session"
    result = dict(passed=False, quest_verified=False, audio_verified=False, input_enabled=False,
        encoder_verified=False, zero_copy=False, private_ipc=True, stages=[], errors=[])
    PrivateObservedPublisher.prefix = "Local\\Quest3D.Verify.WindowSession." + uuid.uuid4().hex
    PrivateObservedPublisher.rows = []
    PrivateObservedPublisher.closed = False
    result["namespace"] = PrivateObservedPublisher.prefix
    result["source_sha256"] = {str(p.relative_to(ROOT)): sha(p.read_bytes()) for p in (
        Path(__file__), ROOT / "src/quest3d/session.py", ROOT / "src/quest3d/window_session.py",
        ROOT / "src/quest3d/window_process_capture.py", ROOT / "src/quest3d/depth.py")}
    original_publisher, original_artifacts = session.FramePublisher, session.ARTIFACT_DIR
    thread = None
    try:
        result["protected_before"] = preserve_state()
        session.FramePublisher = PrivateObservedPublisher
        session.ARTIFACT_DIR = output / "private-active"
        session.ARTIFACT_DIR.mkdir()
        with ExitStack() as stack:
            fixture = stack.enter_context(ThreadedOwnedWindowFixture())
            result["foreground_before"] = fixture.foreground_before
            monitor = next(m for m in list_monitors()[1:] if m.is_primary)
            left, top = monitor.bounds.right - 700, monitor.bounds.top + 120
            hwnd = fixture.create(left=left, top=top, width=640, height=400, single_paint=True)
            result["owned_hwnd"] = hwnd
            options = SimpleNamespace(output=str(directory), window=hwnd, experimental_window=True,
                bridge_protocol=3, mode="3d", disparity=12, cpu_threads=4, eye_width=1280,
                eye_height=720, ai_size=280, seconds=90, fps=30, max_frame_age_ms=200,
                monitor=1, rect=None, file=None, enable_input=False)

            def run():
                try:
                    result["final_status"] = session.serve(options)
                except BaseException:
                    result["errors"].append(traceback.format_exc())

            thread = threading.Thread(target=run, name="Private real window session")
            thread.start()

            def stop_before_window_teardown():
                if thread.is_alive():
                    deadline = time.monotonic() + 3
                    while thread.is_alive() and time.monotonic() < deadline:
                        try:
                            send_control(directory, stop=True)
                            break
                        except (FileNotFoundError, RuntimeError):
                            time.sleep(.02)
                    thread.join(15)
                    if thread.is_alive():
                        raise RuntimeError("Private session did not stop before fixture teardown")

            stack.callback(stop_before_window_teardown)

            def wait_for(predicate, label, timeout=15, paint=True):
                deadline, next_paint = time.monotonic() + timeout, 0
                while time.monotonic() < deadline:
                    if result["errors"]:
                        raise AssertionError(result["errors"][-1])
                    try:
                        status = read_json(directory / "status.json")
                    except FileNotFoundError:
                        status = {}
                    if predicate(status):
                        result["stages"].append(dict(label=label, host_ns=time.perf_counter_ns(), status=status))
                        print(label, flush=True)
                        return status
                    if paint and time.monotonic() >= next_paint:
                        fixture.paint(hwnd)
                        next_paint = time.monotonic() + .3
                    time.sleep(.02)
                raise TimeoutError(label)

            first = wait_for(lambda s: s.get("effective_mode") == "3d" and s.get("ai_frames", 0) >= 3,
                             "actual WGC + V2 Small + unequal eye output", timeout=25)
            assert any(not r["eyes_equal"] and not r["header"]["flags"] & ORIGINAL_2D
                       for r in PrivateObservedPublisher.rows)
            selection = PrivateObservedPublisher.rows[-1]["header"]["source_identity"]["selection_id"]
            generation = PrivateObservedPublisher.rows[-1]["header"]["generation"]
            fixture.show(hwnd, 6)  # Minimize this owned window only.
            unavailable = wait_for(lambda s: s.get("capture_unavailable") and s.get("effective_mode") == "2d",
                                   "minimize flattens retained RGB", paint=False)
            rows_start = len(PrivateObservedPublisher.rows)
            control = send_control(directory, mode="2d")
            wait_for(lambda s: s.get("applied_request") == control["request_id"],
                     "2D control remains responsive while minimized", paint=False)
            hidden = PrivateObservedPublisher.rows[rows_start:]
            assert hidden and all(r["eyes_equal"] and r["header"]["flags"] & ORIGINAL_2D
                and not r["header"]["flags"] & CAPTURE_RECEIPT for r in hidden)
            assert len({r["header"]["capture_ns"] for r in hidden}) == 1
            fixture.show(hwnd, 4)  # Restore most recent size without activation.
            fixture.move(hwnd, left - 30, top + 30, 700, 430)
            wait_for(lambda s: s.get("capture_unavailable") is None and s.get("effective_mode") == "2d",
                     "restore supplies validated new geometry")
            control = send_control(directory, mode="3d")
            wait_for(lambda s: s.get("applied_request") == control["request_id"] and s.get("effective_mode") == "3d",
                     "restored RGB obtains its own 3D result")
            latest = PrivateObservedPublisher.rows[-1]["header"]
            assert latest["generation"] > generation
            assert latest["source_identity"]["selection_id"] == selection
            assert latest["source_identity"]["native_handle"] == hwnd
            assert latest["flags"] & CAPTURE_RECEIPT
            send_control(directory, stop=True)
            thread.join(12)
            assert not thread.is_alive() and not result["errors"]
            assert not result["final_status"]["running"] and not result["final_status"]["error"]
            assert PrivateObservedPublisher.closed
            child = result["final_status"]["window_capture"]
            result["capture_closed"] = child
            result["foreground_after"] = int(fixture.user.GetForegroundWindow() or 0)
            assert result["foreground_after"] == result["foreground_before"]
        result["protected_after"] = preserve_state()
        assert result["protected_after"] == result["protected_before"]
        result["passed"] = True
    except BaseException:
        result["errors"].append(traceback.format_exc())
    finally:
        if thread and thread.is_alive():
            try:
                send_control(directory, stop=True)
            except Exception:
                result["errors"].append(traceback.format_exc())
            thread.join(15)
        session.FramePublisher, session.ARTIFACT_DIR = original_publisher, original_artifacts
        rows = PrivateObservedPublisher.rows
        (output / "ipc-observation.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        result["actual_ipc_frames"] = len(rows)
        result["actual_unequal_eye_frames"] = sum(not r["eyes_equal"] for r in rows)
        result["diagnostic_readback_ms"] = dict(p50=float(np.percentile([r["diagnostic_readback_hash_ms"] for r in rows], 50)),
            p95=float(np.percentile([r["diagnostic_readback_hash_ms"] for r in rows], 95))) if rows else None
        (output / "verification.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: result[k] for k in ("passed", "actual_ipc_frames", "actual_unequal_eye_frames", "errors")}))
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

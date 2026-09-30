"""Private subprocess entry point. Receives one source selection and never restarts it."""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time

from .bridge import CAPTURE_RECEIPT, FramePublisher
from .window_capture import GPUWindowCapture, WindowCaptureFailed, WindowCaptureUnavailable
from .window_process_capture import MAX_MESSAGE, PRIVATE_PREFIX, WIRE_PREFIX, _WinAPI
from .window_sources import SourceExclusions, WindowIdentity, Win32WindowProvider


def validate_start(value):
    if not isinstance(value, dict) or set(value) != {"type", "nonce", "prefix", "identity", "exclusions", "device"}:
        raise ValueError("Invalid worker startup fields")
    if value["type"] != "start" or not isinstance(value["nonce"], str) or not re.fullmatch(r"[0-9a-f]{64}", value["nonce"]):
        raise ValueError("Invalid worker startup token")
    if not isinstance(value["prefix"], str) or not re.fullmatch(re.escape(PRIVATE_PREFIX) + r"[0-9a-f]{64}", value["prefix"]):
        raise ValueError("Worker requires a new private mapping prefix")
    identity = WindowIdentity(**value["identity"])
    exclusions = SourceExclusions(**value["exclusions"])
    GPUWindowCapture(identity, experimental_window=True, exclusions=exclusions, device=value["device"])
    return identity, exclusions


def main():
    line = sys.stdin.buffer.readline(MAX_MESSAGE + 1)
    if len(line) > MAX_MESSAGE or not line.endswith(b"\n"):
        raise ValueError("Missing or oversized private worker request")
    request = json.loads(line)
    identity, exclusions = validate_start(request)
    nonce = request["nonce"]
    win = _WinAPI()
    birth = win.birth(win.k.GetCurrentProcess())
    stop = threading.Event()
    protocol_failure = []

    def send(kind, **data):
        value = dict(type=kind, nonce=nonce, pid=os.getpid(), birth=birth, **data)
        payload = WIRE_PREFIX + json.dumps(value, separators=(",", ":"))
        if len(payload.encode()) > MAX_MESSAGE - 1:
            raise ValueError("Worker state exceeded its wire limit")
        print(payload, flush=True)

    def commands():
        while not stop.is_set():
            try:
                data = sys.stdin.buffer.readline(MAX_MESSAGE + 1)
                if not data:  # Parent pipe closed: stop capture; no orphan loop.
                    stop.set()
                    return
                if len(data) > MAX_MESSAGE or not data.endswith(b"\n"):
                    raise ValueError("Invalid stop message length")
                value = json.loads(data)
                if value != {"type": "stop", "nonce": nonce}:
                    raise ValueError("Only this worker's stop command is accepted")
                stop.set()
            except Exception as exc:
                protocol_failure.append(str(exc))
                stop.set()

    # The venv executable can be a redirector with a different PID. The parent
    # verifies this real process lifetime and Job membership before CUDA/WGC.
    send("bootstrap")
    arm_line = sys.stdin.buffer.readline(MAX_MESSAGE + 1)
    if len(arm_line) > MAX_MESSAGE or not arm_line.endswith(b"\n"):
        raise ValueError("Worker was not armed by its parent")
    if json.loads(arm_line) != dict(type="armed", nonce=nonce, pid=os.getpid(), birth=birth):
        raise ValueError("Worker cleanup Job was not acknowledged for this exact process")
    command_thread = threading.Thread(target=commands, name="Private worker stop", daemon=False)
    command_thread.start()
    failure = None
    source_ended = False
    capture = publisher = None
    publication = None
    metrics = None
    try:
        publisher = FramePublisher(request["prefix"], version=3)
        capture = GPUWindowCapture(identity, experimental_window=True, exclusions=exclusions, device=request["device"])
        capture.__enter__()
        send("hello", selection_id=capture.selection_id, source_id=capture.source_id, epoch=publisher.epoch)
        last_sent = 0.0
        while not stop.is_set():
            blocked = None
            try:
                frame = capture.grab(timeout_seconds=.1)
            except WindowCaptureUnavailable as exc:
                blocked = str(exc)
                frame = None
            except TimeoutError:
                frame = None
            had_frame = frame is not None
            if frame is not None:
                readback_start = time.perf_counter_ns()
                pixels = frame.bgra.to(device="cpu", non_blocking=False).numpy()
                readback_ms = (time.perf_counter_ns() - readback_start) / 1e6
                publish_start = time.perf_counter_ns()
                b = frame.geometry.bounds
                success = publisher.publish(pixels, frame_id=frame.frame_id, capture_ns=frame.captured_ns,
                    generation=frame.geometry_generation, flags=CAPTURE_RECEIPT,
                    source_rect=(b.left, b.top, b.width, b.height), content_rect=(0, 0, b.width, b.height),
                    source_identity=frame.source_identity)
                if success:
                    publication = [frame.frame_id, frame.geometry_generation]
                    metrics = dict(color_submission_ms=frame.color_processing_ms, readback_wall_ms=readback_ms,
                        shared_publish_wall_ms=(time.perf_counter_ns() - publish_start) / 1e6,
                        payload_bytes=pixels.nbytes, raw_wgc=capture.last_frame_diagnostics,
                        zero_copy=False, upload_ms=None)
                del frame, pixels
            if time.monotonic() - last_sent >= .08 or had_frame:
                status = capture.status
                state = status["state"]
                if state == "ready" and publication is None:
                    state = "waiting"
                if blocked:
                    state = "waiting"
                send("state", state=state, reason=blocked or status["reason"], publication=publication,
                    generation=status["generation"], metrics=metrics, source_status=status, cleanup_ok=False)
                last_sent = time.monotonic()
        if protocol_failure:
            raise ValueError(protocol_failure[0])
    except BaseException as exc:
        if isinstance(exc, WindowCaptureFailed):
            try:
                observation = Win32WindowProvider(include_titles=False).observe(identity.hwnd, include_occlusion=False)
                source_ended = observation.identity != identity
            except Exception:
                pass
        failure = None if source_ended else f"{type(exc).__name__}: {exc}"
        try:
            send("state", state="closed" if source_ended else "failed",
                 reason="source_lifetime_ended" if source_ended else failure,
                 publication=None, cleanup_ok=False, source_ended=source_ended)
        except (BrokenPipeError, OSError):
            pass
    finally:
        cleanup_error = None
        try:
            if capture:
                capture.close()
        except BaseException as exc:
            cleanup_error = f"{type(exc).__name__}: {exc}"
        try:
            if publisher:
                publisher.close()
        except BaseException as exc:
            cleanup_error = cleanup_error or f"{type(exc).__name__}: {exc}"
        # Source loss can finish capture while the command reader still owns
        # stdin's buffered-read lock. Announce a terminal, unacknowledged state
        # first: ProcessWindowCapture.close() sends Stop and closes its pipe
        # before waiting for us. That Stop/EOF is the reader's exit rendezvous.
        # A parent that has not closed yet leaves this worker pending in its Job;
        # capture cleanup alone cannot authorize interpreter finalization.
        try:
            send("state", state="failed" if cleanup_error or failure else "closed",
                 reason=cleanup_error or failure, publication=None, cleanup_ok=False,
                 source_ended=source_ended)
        except (BrokenPipeError, OSError):
            pass
        stop.set()
        command_thread.join()
        if protocol_failure:
            failure = failure or f"ValueError: {protocol_failure[0]}"
        try:
            send("state", state="failed" if cleanup_error else "closed", reason=cleanup_error or failure,
                 publication=None, cleanup_ok=cleanup_error is None, source_ended=source_ended)
        except (BrokenPipeError, OSError):
            pass
    return 1 if failure or cleanup_error else 0


if __name__ == "__main__":
    raise SystemExit(main())

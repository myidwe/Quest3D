"""Local development control transport, replaced by paired Quest UI in P2.

Messages are inert JSON, scoped to a random live session. No remote listener or
shell command is exposed. Atomic replacement prevents partially read requests.
"""
from pathlib import Path
import json
import math
import os
import time
import uuid
import sys

from .model_choice import validate_depth_model


def expected_output_mode(mode, disparity):
    """Zero stereo strength is successfully applied as unwarped original 2D."""
    return "2d" if mode == "2d" or disparity == 0 else "3d"


def read_json(path: Path, *, max_bytes=65536):
    """Read one bounded snapshot without blocking atomic replacement on Windows."""
    deadline = time.monotonic() + .05
    while True:
        try:
            if sys.platform == "win32":
                import ctypes
                from ctypes import wintypes
                import msvcrt
                kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                               ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
                kernel.CreateFileW.restype = wintypes.HANDLE
                # READ | WRITE | DELETE sharing is essential: ordinary Python
                # open() omits DELETE and races with ReplaceFile/MoveFileEx.
                handle = kernel.CreateFileW(str(path), 0x80000000, 7, None, 3, 0x80, None)
                if handle == ctypes.c_void_p(-1).value:
                    raise ctypes.WinError(ctypes.get_last_error())
                try:
                    descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
                except BaseException:
                    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
                    kernel.CloseHandle(handle)
                    raise
                with os.fdopen(descriptor, "rb") as stream:
                    payload = stream.read(max_bytes + 1)
            else:
                with path.open("rb") as stream:
                    payload = stream.read(max_bytes + 1)
            break
        except OSError as exc:
            if getattr(exc, "winerror", None) not in (5, 32, 33) or time.monotonic() >= deadline:
                raise
            time.sleep(.001)
    if len(payload) > max_bytes:
        raise ValueError(f"JSON snapshot exceeds {max_bytes} bytes")
    return json.loads(payload.decode("utf-8"))


def atomic_json(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        pending.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        deadline = time.monotonic() + .05
        delay = .001
        while True:
            try:
                os.replace(pending, path)
                break
            except OSError as exc:
                # Ordinary Windows readers can briefly omit FILE_SHARE_DELETE.
                # Do not spin forever or retry unrelated filesystem failures.
                if getattr(exc, "winerror", None) not in (5, 32, 33) or time.monotonic() >= deadline:
                    raise
                time.sleep(delay)
                delay = min(delay * 2, .008)
    finally:
        pending.unlink(missing_ok=True)


def validate_request(request: dict, session_id: str, eye_width: int) -> dict:
    if not isinstance(request, dict) or request.get("session_id") != session_id:
        raise ValueError("Control request belongs to a different session")
    if set(request) - {"session_id", "request_id", "mode", "disparity", "disparity_profile", "depth_model", "stop", "paused", "seek_seconds", "expected_revision"}:
        raise ValueError("Unsupported control fields")
    if not isinstance(request.get("request_id"), str) or not request["request_id"]:
        raise ValueError("Control request must have an identifier")
    if request.get("mode") not in ("2d", "3d"):
        raise ValueError("Mode must be 2d or 3d")
    disparity = request.get("disparity")
    if isinstance(disparity, bool) or not isinstance(disparity, (int, float)):
        raise ValueError("Disparity must be a finite number")
    if not 0 <= disparity <= eye_width * .04 or not math.isfinite(disparity):
        raise ValueError("Disparity is outside the supported range")
    if "disparity_profile" in request:
        profile = request["disparity_profile"]
        if not isinstance(profile, str) or profile not in ("linear", "comfort"):
            raise ValueError("Disparity profile must be linear or comfort")
    if "depth_model" in request:
        validate_depth_model(request["depth_model"])
    if not isinstance(request.get("stop", False), bool):
        raise ValueError("Stop must be a boolean")
    if "paused" in request and not isinstance(request["paused"], bool):
        raise ValueError("Paused must be a boolean")
    if "seek_seconds" in request:
        from .media import seconds_ns
        seconds_ns(request["seek_seconds"])
    if "expected_revision" in request:
        expected = request["expected_revision"]
        if isinstance(expected, bool) or not isinstance(expected, int) or not 0 <= expected <= 0xFFFFFFFF:
            raise ValueError("Expected revision must be an unsigned 32-bit integer")
    return request


def send_control(directory: Path, *, mode=None, disparity=None, stop=False, paused=None, seek_seconds=None,
                 disparity_profile=None, depth_model=None) -> dict:
    status = read_json(directory / "status.json")
    if not status.get("running"):
        raise RuntimeError("The session is not running")
    request = {
        "session_id": status["session_id"], "request_id": uuid.uuid4().hex,
        "mode": mode or status["requested_mode"],
        "disparity": status["disparity"] if disparity is None else disparity,
        "stop": stop,
    }
    if "revision" in status:
        request["expected_revision"] = status["revision"]
    if paused is not None:
        request["paused"] = paused
    if seek_seconds is not None:
        request["seek_seconds"] = seek_seconds
    if disparity_profile is not None:
        request["disparity_profile"] = disparity_profile
    if depth_model is not None:
        request["depth_model"] = validate_depth_model(depth_model)
        if depth_model not in status.get("available_depth_models", []):
            raise RuntimeError("The running producer has not prepared this depth model")
    validate_request(request, status["session_id"], status["eye_width"])
    if disparity_profile is not None and "disparity_profile" not in status:
        raise RuntimeError("The running producer does not support disparity profiles; "
                           "omit --disparity-profile or restart a compatible producer")
    atomic_json(directory / "request.json", request)
    return {"submitted": True, "request_id": request["request_id"],
            "note": "Applied state is reported in status.json; submission alone is not an acknowledgement."}

"""Recover only journal-owned Windows default audio roles after host death.

No API in this module changes endpoint mute, volume, format or visibility. The
default CLI action is a dry run. Actual recovery requires --apply and a valid
project-local journal whose owner is demonstrably no longer running.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import copy
import ctypes as ct
from ctypes import wintypes as wt
from dataclasses import dataclass
import json
import os
from pathlib import Path
import sys
import time
from typing import Callable, Protocol
import uuid

ARTIFACT_ROOT = Path(__file__).resolve().parents[2] / "artifacts"
PHASES = {"planned", "active", "restore_pending", "restored"}
TERMINAL = {"restored", "already_original", "external_preserved", "never_changed"}
RECOVERY_STATES = TERMINAL | {"restoring", "failed", "original_unavailable", "query_failed", "coupled_roles_conflict"}


class RecoveryError(RuntimeError):
    """Fail closed when ownership, journal validity or access is uncertain."""


class AudioBackend(Protocol):
    def current(self, role: int) -> str: ...
    def endpoint_active(self, endpoint: str) -> bool: ...
    def set_default(self, role: int, endpoint: str) -> None: ...


@dataclass(frozen=True)
class OwnerState:
    state: str
    detail: str = ""


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RecoveryError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def validate_journal(raw: bytes) -> dict:
    """Strictly accept only the current journal schema; never infer lost originals."""
    if not raw or len(raw) > 65536:
        raise RecoveryError("Journal must contain 1..65536 bytes")
    try:
        record = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeError, ValueError) as error:
        raise RecoveryError("Malformed journal JSON") from error
    if not isinstance(record, dict) or type(record.get("schema")) is not int or record["schema"] != 1:
        raise RecoveryError("Unsupported journal schema")
    if type(record.get("owner_pid")) is not int or not 0 < record["owner_pid"] <= 0xFFFFFFFF:
        raise RecoveryError("Missing or invalid owner PID")
    created = record.get("owner_creation_filetime")
    if not isinstance(created, str) or not created.isascii() or not created.isdecimal() or not 0 < len(created) <= 20 or not 0 < int(created) < 2**64:
        raise RecoveryError("Missing or invalid owner creation time")
    if not isinstance(record.get("phase"), str) or record["phase"] not in PHASES:
        raise RecoveryError("Unknown journal phase")
    if "watchdog_armed" in record and type(record["watchdog_armed"]) is not bool:
        raise RecoveryError("Invalid watchdog readiness state")
    if record.get("watchdog_armed") is False and record["phase"] == "active":
        raise RecoveryError("Unarmed journal cannot own an active route")
    for field in ("format_changes", "mute_changes", "volume_changes"):
        if record.get(field) is not False:
            raise RecoveryError(f"Cannot recover a journal that may have {field}")
    roles = record.get("roles")
    if not isinstance(roles, list) or len(roles) != 3:
        raise RecoveryError("Expected exactly three audio roles")
    seen = set()
    for role in roles:
        if not isinstance(role, dict) or type(role.get("role")) is not int or role["role"] not in range(3) or role["role"] in seen:
            raise RecoveryError("Invalid or duplicate audio role")
        seen.add(role["role"])
        if type(role.get("applied")) is not bool:
            raise RecoveryError("Missing applied ownership flag")
        if record.get("watchdog_armed") is False and role["applied"]:
            raise RecoveryError("Unarmed journal cannot own applied endpoint changes")
        for field in ("original", "target"):
            value = role.get(field)
            if not isinstance(value, str) or not 0 < len(value) <= 2048 or any(ord(char) < 32 for char in value):
                raise RecoveryError(f"Invalid {field} endpoint ID")
        if "recovery_status" in role and (not isinstance(role["recovery_status"], str) or role["recovery_status"] not in RECOVERY_STATES):
            raise RecoveryError("Unknown previous recovery outcome")
        if role["applied"] and (record["phase"] in {"planned", "restored"} or role["original"] == role["target"]):
            raise RecoveryError("Contradictory applied ownership state")
        if record["phase"] == "restored" and role.get("recovery_status", "restored") not in TERMINAL:
            raise RecoveryError("Restored journal contains a pending role")
    record["roles"] = sorted(roles, key=lambda role: role["role"])
    return record


def _kernel():
    if os.name != "nt":
        raise RecoveryError("Windows is required")
    library = ct.WinDLL("kernel32", use_last_error=True)
    library.CloseHandle.argtypes = [wt.HANDLE]
    library.CloseHandle.restype = wt.BOOL
    library.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    library.OpenProcess.restype = wt.HANDLE
    library.GetProcessTimes.argtypes = [wt.HANDLE] + [ct.POINTER(wt.FILETIME)] * 4
    library.GetProcessTimes.restype = wt.BOOL
    library.GetExitCodeProcess.argtypes = [wt.HANDLE, ct.POINTER(wt.DWORD)]
    library.GetExitCodeProcess.restype = wt.BOOL
    library.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
    library.WaitForSingleObject.restype = wt.DWORD
    library.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ct.c_void_p, wt.DWORD, wt.DWORD, wt.HANDLE]
    library.CreateFileW.restype = wt.HANDLE
    return library


def process_identity(pid: int) -> tuple[int, int, bool] | None:
    """Return (creation FILETIME, exit code, running); None means a nonexistent PID."""
    kernel = _kernel()
    handle = kernel.OpenProcess(0x101000, False, pid)  # QUERY_LIMITED_INFORMATION | SYNCHRONIZE
    if not handle:
        error = ct.get_last_error()
        if error == 87:  # ERROR_INVALID_PARAMETER: no such process
            return None
        raise RecoveryError(f"Cannot inspect owner process; Win32 error {error}")
    try:
        created, exited, user, system = (wt.FILETIME() for _ in range(4))
        code = wt.DWORD()
        if not kernel.GetProcessTimes(handle, ct.byref(created), ct.byref(exited), ct.byref(system), ct.byref(user)) or not kernel.GetExitCodeProcess(handle, ct.byref(code)):
            raise RecoveryError(f"Cannot inspect owner lifetime; Win32 error {ct.get_last_error()}")
        wait = kernel.WaitForSingleObject(handle, 0)
        if wait not in (0, 258):
            raise RecoveryError(f"Cannot determine owner termination; Win32 error {ct.get_last_error()}")
        return (created.dwHighDateTime << 32) | created.dwLowDateTime, code.value, wait == 258
    finally:
        kernel.CloseHandle(handle)


def owner_state(pid: int, created: str) -> OwnerState:
    """PID reuse cannot make a new unrelated process own an old journal."""
    try:
        identity = process_identity(pid)
    except RecoveryError as error:
        return OwnerState("unknown", str(error))
    if identity is None:
        return OwnerState("dead", "PID no longer exists")
    if identity[0] != int(created):
        return OwnerState("pid_reused", "Creation FILETIME differs from the recorded owner")
    return OwnerState("alive" if identity[2] else "dead", f"exit_code={identity[1]}")


@contextmanager
def journal_lock(path: Path):
    """Use the same exclusive delete-on-close sidecar protocol as the host."""
    kernel = _kernel()
    handle = kernel.CreateFileW(str(path) + ".lock", 0xC0010000, 1, None, 4, 0x04000100, None)
    if handle == ct.c_void_p(-1).value:
        raise RecoveryError(f"Journal is locked or inaccessible; Win32 error {ct.get_last_error()}")
    try:
        yield
    finally:
        kernel.CloseHandle(handle)


def write_journal(path: Path, record: dict) -> None:
    """Flush before atomic replacement; bounded sharing retries preserve prior data."""
    data = json.dumps(record, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8") + b"\n"
    validate_journal(data)
    temporary = path.with_name(path.name + ".recovery-" + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        deadline = time.monotonic() + 0.05
        while True:
            try:
                os.replace(temporary, path)
                break
            except OSError as error:
                if getattr(error, "winerror", None) not in (5, 32, 33) or time.monotonic() >= deadline:
                    raise RecoveryError(f"Cannot commit recovery journal: {error}") from error
                time.sleep(0.001)
    finally:
        temporary.unlink(missing_ok=True)


def _role_plan(role: dict, backend: AudioBackend, all_roles: list[dict]) -> dict:
    result = {"role": role["role"], "original": role["original"], "target": role["target"]}
    if role.get("recovery_status") in TERMINAL:
        return {**result, "status": "completed_earlier", "previous_status": role["recovery_status"]}
    try:
        current = backend.current(role["role"])
        result["current"] = current
        if role["original"] == role["target"]:
            result["status"] = "never_changed"
        elif current == role["original"]:
            result["status"] = "already_original"
        elif current != role["target"]:
            result["status"] = "external_preserved"
        elif not backend.endpoint_active(role["original"]):
            result["status"] = "original_unavailable"
        else:
            result["status"] = "would_restore"
            # The tested Windows policy interface updates console + multimedia
            # together. A per-role loop must not mistake its own side effect for
            # a user change or overwrite an unowned peer role.
            for group in getattr(backend, "coupled_roles", ()):
                if role["role"] not in group:
                    continue
                for peer in all_roles:
                    if peer["role"] not in group or peer["role"] == role["role"]:
                        continue
                    peer_current = backend.current(peer["role"])
                    if (peer["original"] != role["original"]
                            or peer_current not in {peer["original"], peer["target"]}
                            or (peer.get("recovery_status") in TERMINAL and peer_current != peer["original"])):
                        result.update(status="coupled_roles_conflict", error="Restoring this role could overwrite a coupled role with a different original or external selection")
    except Exception as error:
        result.update(status="query_failed", error=str(error))
    return result


def recover_journal(
    path: Path,
    backend: AudioBackend,
    *,
    apply: bool = False,
    allowed_root: Path = ARTIFACT_ROOT,
    inspect_owner: Callable[[int, str], OwnerState] = owner_state,
    lock=journal_lock,
    writer=write_journal,
    expected_owner: tuple[int, str] | None = None,
) -> dict:
    """Recover role by role, preserving external choices and durable retry state."""
    path = Path(path)
    report = {"journal": str(path), "apply": apply, "attempted_roles": [], "changed_roles": [], "roles": []}
    if not path.is_absolute() or path.suffix.lower() != ".json":
        return {**report, "status": "invalid_path", "error": "Require an absolute .json path"}
    try:
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(allowed_root.resolve(strict=True)) or not resolved.is_file():
            return {**report, "status": "invalid_path", "error": "Journal must remain in the project artifact directory"}
        with lock(resolved):
            with resolved.open("rb") as source:
                record = validate_journal(source.read(65537))
            if expected_owner is not None and (record["owner_pid"], record["owner_creation_filetime"]) != expected_owner:
                return {**report, "status": "owner_replaced", "error": "Journal owner changed after watchdog wait"}
            state = inspect_owner(record["owner_pid"], record["owner_creation_filetime"])
            report["owner"] = {"state": state.state, "detail": state.detail}
            if state.state not in {"dead", "pid_reused"}:
                return {**report, "status": "blocked_owner"}
            if record["phase"] == "restored":
                return {**report, "status": "already_restored"}
            if record.get("watchdog_armed") is False:
                # The new native gate cannot call COM until an armed record is
                # durable. Preserve even a user's later selection of the target.
                if apply:
                    untouched = copy.deepcopy(record)
                    untouched["phase"] = "restored"
                    for role in untouched["roles"]:
                        role["recovery_status"] = "never_changed"
                    writer(resolved, untouched)
                return {**report, "status": "restored" if apply else "dry_run", "unarmed": True}
            planned = [_role_plan(role, backend, record["roles"]) for role in record["roles"]]
            report["roles"] = planned
            if not apply:
                return {**report, "status": "dry_run", "pending": any(role["status"] in {"query_failed", "original_unavailable", "coupled_roles_conflict"} for role in planned)}
            recovery = copy.deepcopy(record)
            recovery["phase"] = "restore_pending"
            recovery["recovery_pid"] = os.getpid()
            # Preflight persistence failure must happen before any SetDefaultEndpoint.
            writer(resolved, recovery)
            for index, role in enumerate(recovery["roles"]):
                outcome = _role_plan(role, backend, recovery["roles"])
                report["roles"][index] = outcome
                if outcome["status"] == "completed_earlier":
                    continue
                if outcome["status"] == "would_restore":
                    role["recovery_status"] = "restoring"
                    writer(resolved, recovery)
                    # Windows has no compare-and-swap for endpoint policy. Recheck
                    # after persistence, immediately before the only mutation.
                    latest = _role_plan(role, backend, recovery["roles"])
                    if latest["status"] != "would_restore":
                        outcome = latest
                    else:
                        try:
                            report["attempted_roles"].append(role["role"])
                            backend.set_default(role["role"], role["original"])
                            report["changed_roles"].append(role["role"])
                            current = backend.current(role["role"])
                            outcome = {**outcome, "current_after": current, "status": "restored" if current == role["original"] else "external_preserved"}
                        except Exception as error:
                            outcome = {**outcome, "status": "failed", "error": str(error)}
                report["roles"][index] = outcome
                role["recovery_status"] = outcome["status"]
                if outcome["status"] in TERMINAL:
                    role["applied"] = False
                writer(resolved, recovery)
            pending = any(role.get("recovery_status") not in TERMINAL for role in recovery["roles"])
            recovery["phase"] = "restore_pending" if pending else "restored"
            writer(resolved, recovery)
            return {**report, "status": recovery["phase"]}
    except FileNotFoundError as error:
        return {**report, "status": "missing_journal", "error": str(error)}
    except (RecoveryError, OSError, ValueError, TypeError) as error:
        return {**report, "status": "refused", "error": str(error)}


def wait_for_owner_exit(
    path: Path, *, timeout: float = 300, allowed_root: Path = ARTIFACT_ROOT,
    inspect_owner=owner_state, clock=time.monotonic, sleep=time.sleep, on_wait=None,
) -> dict:
    """Read-only watchdog wait; recovery rechecks ownership after obtaining its lock."""
    if not 0 < timeout <= 86400:
        raise RecoveryError("Watch timeout must be 1..86400 seconds")
    path = Path(path)
    if not path.is_absolute() or path.suffix.lower() != ".json":
        raise RecoveryError("Watch requires an absolute .json journal path")
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(allowed_root.resolve(strict=True)):
        raise RecoveryError("Watch journal must remain in the project artifact directory")
    deadline = clock() + timeout
    expected_owner = None
    announced = False
    while True:
        try:
            with resolved.open("rb") as source:
                record = validate_journal(source.read(65537))
            identity = (record["owner_pid"], record["owner_creation_filetime"])
            if expected_owner is not None and identity != expected_owner:
                return {"status": "owner_replaced", "error": "A newer session owns this journal; the old watcher stopped"}
            expected_owner = identity
            state = inspect_owner(*identity)
            if state.state in {"dead", "pid_reused"}:
                return {"status": "owner_exited", "owner_identity": identity, "owner": {"state": state.state, "detail": state.detail}}
            if state.state != "alive":
                return {"status": "blocked_owner", "owner": {"state": state.state, "detail": state.detail}}
            if on_wait is not None and not announced:
                on_wait({"status": "watching_owner", "owner_pid": identity[0], "owner_creation_filetime": identity[1]})
                announced = True
        except OSError as error:
            if getattr(error, "winerror", None) not in (5, 32, 33):
                raise
        remaining = deadline - clock()
        if remaining <= 0:
            return {"status": "watch_timeout", "error": "Owner is still alive or journal access remains uncertain"}
        sleep(min(0.05, remaining))


class _GUID(ct.Structure):
    _fields_ = [("data1", ct.c_uint32), ("data2", ct.c_uint16), ("data3", ct.c_uint16), ("data4", ct.c_ubyte * 8)]

    @classmethod
    def parse(cls, value: str):
        return cls.from_buffer_copy(uuid.UUID(value).bytes_le)


def _call(pointer, slot: int, *typed_args, result=ct.c_int32):
    table = ct.cast(pointer, ct.POINTER(ct.POINTER(ct.c_void_p))).contents
    signature = ct.WINFUNCTYPE(result, ct.c_void_p, *(kind for kind, _ in typed_args))
    return signature(table[slot])(pointer, *(value for _, value in typed_args))


def _check(status: int, operation: str):
    if status < 0:
        raise RecoveryError(f"{operation}: HRESULT=0x{status & 0xFFFFFFFF:08x}")


class WindowsAudioBackend:
    """Small Core Audio binding; private policy interface is loaded only for apply."""

    coupled_roles = ((0, 1),)  # Observed during the actual 2026-09-09 device test.

    def __init__(self, *, allow_changes: bool = False):
        if os.name != "nt":
            raise RecoveryError("Windows is required")
        self.allow_changes = allow_changes
        self.enumerator = ct.c_void_p()
        self.policy = ct.c_void_p()
        self.ole = ct.OleDLL("ole32")
        self.ole.CoInitializeEx.argtypes = [ct.c_void_p, wt.DWORD]
        self.ole.CoInitializeEx.restype = ct.c_int32
        self.ole.CoCreateInstance.argtypes = [ct.POINTER(_GUID), ct.c_void_p, wt.DWORD, ct.POINTER(_GUID), ct.POINTER(ct.c_void_p)]
        self.ole.CoCreateInstance.restype = ct.c_int32
        self.ole.CoTaskMemFree.argtypes = [ct.c_void_p]
        self.ole.CoTaskMemFree.restype = None
        _check(self.ole.CoInitializeEx(None, 0), "CoInitializeEx")
        self.initialized = True
        try:
            self.enumerator = self._create("bcde0395-e52f-467c-8e3d-c4579291692e", "a95664d2-9614-4f35-a746-de8db63617e6")
        except BaseException:
            self.close()
            raise

    def _create(self, clsid: str, iid: str):
        result = ct.c_void_p()
        _check(self.ole.CoCreateInstance(ct.byref(_GUID.parse(clsid)), None, 23, ct.byref(_GUID.parse(iid)), ct.byref(result)), "CoCreateInstance")
        return result

    @contextmanager
    def _device(self, endpoint: str):
        pointer = ct.c_void_p()
        _check(_call(self.enumerator, 5, (wt.LPCWSTR, endpoint), (ct.POINTER(ct.c_void_p), ct.byref(pointer))), "GetDevice")
        try:
            yield pointer
        finally:
            _call(pointer, 2, result=wt.ULONG)

    def _id(self, pointer) -> str:
        raw = ct.c_void_p()
        _check(_call(pointer, 5, (ct.POINTER(ct.c_void_p), ct.byref(raw))), "GetId")
        try:
            return ct.wstring_at(raw)
        finally:
            self.ole.CoTaskMemFree(raw)

    def current(self, role: int) -> str:
        pointer = ct.c_void_p()
        _check(_call(self.enumerator, 4, (ct.c_int, 0), (ct.c_int, role), (ct.POINTER(ct.c_void_p), ct.byref(pointer))), "GetDefaultAudioEndpoint")
        try:
            return self._id(pointer)
        finally:
            _call(pointer, 2, result=wt.ULONG)

    def endpoint_active(self, endpoint: str) -> bool:
        try:
            with self._device(endpoint) as pointer:
                state = wt.DWORD()
                _check(_call(pointer, 6, (ct.POINTER(wt.DWORD), ct.byref(state))), "GetState")
                return state.value == 1
        except RecoveryError:
            return False

    def set_default(self, role: int, endpoint: str) -> None:
        if not self.allow_changes:
            raise RecoveryError("Read-only backend refuses SetDefaultEndpoint")
        if type(role) is not int or role not in range(3) or not self.endpoint_active(endpoint):
            raise RecoveryError("Restoration target role or endpoint is unavailable")
        if not self.policy:
            self.policy = self._create("870af99c-171d-4f9e-af0d-e63df40c2bc9", "f8679f50-850a-41cf-9c72-430f290290c8")
        _check(_call(self.policy, 13, (wt.LPCWSTR, endpoint), (ct.c_int, role)), "SetDefaultEndpoint")

    def snapshot(self) -> dict:
        """Read every active render endpoint's ID, mute and master volume."""
        defaults = [self.current(role) for role in range(3)]
        collection = ct.c_void_p()
        _check(_call(self.enumerator, 3, (ct.c_int, 0), (wt.DWORD, 1), (ct.POINTER(ct.c_void_p), ct.byref(collection))), "EnumAudioEndpoints")
        endpoints = []
        try:
            count = wt.UINT()
            _check(_call(collection, 3, (ct.POINTER(wt.UINT), ct.byref(count))), "GetCount")
            for index in range(count.value):
                device, volume = ct.c_void_p(), ct.c_void_p()
                _check(_call(collection, 4, (wt.UINT, index), (ct.POINTER(ct.c_void_p), ct.byref(device))), "Item")
                try:
                    iid = _GUID.parse("5cdf2c82-841e-4546-9722-0cf74078229a")
                    _check(_call(device, 3, (ct.POINTER(_GUID), ct.byref(iid)), (wt.DWORD, 23), (ct.c_void_p, None), (ct.POINTER(ct.c_void_p), ct.byref(volume))), "Activate endpoint volume")
                    muted, scalar = wt.BOOL(), ct.c_float()
                    _check(_call(volume, 15, (ct.POINTER(wt.BOOL), ct.byref(muted))), "GetMute")
                    _check(_call(volume, 9, (ct.POINTER(ct.c_float), ct.byref(scalar))), "GetMasterVolumeLevelScalar")
                    endpoints.append({"id": self._id(device), "muted": bool(muted.value), "volume_scalar": scalar.value})
                finally:
                    if volume:
                        _call(volume, 2, result=wt.ULONG)
                    _call(device, 2, result=wt.ULONG)
        finally:
            _call(collection, 2, result=wt.ULONG)
        return {"default_roles": defaults, "active_render_endpoints": sorted(endpoints, key=lambda row: row["id"])}

    def close(self):
        for field in ("policy", "enumerator"):
            pointer = getattr(self, field, None)
            if pointer:
                _call(pointer, 2, result=wt.ULONG)
                setattr(self, field, ct.c_void_p())
        if getattr(self, "initialized", False):
            self.ole.CoUninitialize()
            self.initialized = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", type=Path)
    parser.add_argument("--apply", action="store_true", help="Recover journal-owned default endpoint roles")
    parser.add_argument("--snapshot", action="store_true", help="Read-only endpoint/mute/volume inventory")
    parser.add_argument("--watch-owner", action="store_true", help="Wait for this journal owner to exit, then recover")
    parser.add_argument("--timeout", type=float, default=300, help="Watch deadline in seconds, at most 86400")
    args = parser.parse_args(argv)
    if args.snapshot and (args.journal or args.apply or args.watch_owner):
        parser.error("--snapshot cannot be combined with journal recovery")
    if not args.snapshot and not args.journal:
        parser.error("An explicit --journal path is required")
    if args.watch_owner and not args.apply:
        parser.error("--watch-owner requires --apply")
    try:
        expected_owner = None
        if args.watch_owner:
            waited = wait_for_owner_exit(args.journal, timeout=args.timeout,
                                         on_wait=lambda event: print(json.dumps(event), file=sys.stderr, flush=True))
            if waited["status"] != "owner_exited":
                print(json.dumps(waited, ensure_ascii=False))
                return 2
            expected_owner = tuple(waited["owner_identity"])
        with WindowsAudioBackend(allow_changes=args.apply) as backend:
            before = backend.snapshot()
            if args.snapshot:
                result = {"status": "snapshot", "read_only": True, **before}
            else:
                result = recover_journal(args.journal, backend, apply=args.apply, expected_owner=expected_owner)
                result["before"] = before
                result["after"] = backend.snapshot()
                result["mute_volume_unchanged"] = before["active_render_endpoints"] == result["after"]["active_render_endpoints"]
            print(json.dumps(result, ensure_ascii=False, allow_nan=False))
            good = result["status"] in {"snapshot", "already_restored", "restored", "dry_run"} and not result.get("pending", False) and result.get("mute_volume_unchanged", True)
            return 0 if good else 2
    except (RecoveryError, OSError, ValueError) as error:
        print(json.dumps({"status": "refused", "error": str(error), "apply": args.apply}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

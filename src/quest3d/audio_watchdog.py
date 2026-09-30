"""Register one host lifetime before permitting journaled native audio routing.

The launcher must explicitly authorize --apply. No endpoint is changed while
the host is alive. Recovery reuses the existing journal ownership policy.
"""
from __future__ import annotations

import argparse
import ctypes as ct
from ctypes import wintypes as wt
import json
import os
from pathlib import Path
import re
import sys
import time
import uuid

from . import audio_recovery as recovery


class ProcessLifetime:
    """Keep a handle to the exact creation FILETIME, independent of PID reuse."""
    def __init__(self, pid: int, created: str):
        self.kernel = recovery._kernel()
        self.handle = self.kernel.OpenProcess(0x101000, False, pid)
        if not self.handle:
            if ct.get_last_error() == 87:
                return
            raise recovery.RecoveryError("Cannot open registered host process")
        try:
            values = [wt.FILETIME() for _ in range(4)]
            if not self.kernel.GetProcessTimes(self.handle, *(ct.byref(value) for value in values)):
                raise recovery.RecoveryError("Cannot read registered host creation time")
            actual = (values[0].dwHighDateTime << 32) | values[0].dwLowDateTime
            if actual != int(created):
                self.close()  # The unrelated replacement PID is never watched or signaled.
        except BaseException:
            self.close()
            raise

    def state(self):
        if not self.handle:
            return recovery.OwnerState("dead")
        result = self.kernel.WaitForSingleObject(self.handle, 0)
        if result not in (0, 258):
            return recovery.OwnerState("unknown", "Registered process wait failed")
        return recovery.OwnerState("alive" if result == 258 else "dead")

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class WatchdogSignals:
    """READY and a mutex owned by this exact watchdog main thread."""
    def __init__(self, token: str):
        if not re.fullmatch(r"[0-9a-f]{32}", token):
            raise recovery.RecoveryError("Invalid watchdog launch token")
        self.kernel = recovery._kernel()
        self.ready = self.live = None
        self.owned = False
        self.ready_owned = False
        for name in ("CreateEventW", "CreateMutexW"):
            getattr(self.kernel, name).restype = wt.HANDLE
        self.kernel.CreateEventW.argtypes = [ct.c_void_p, wt.BOOL, wt.BOOL, wt.LPCWSTR]
        self.kernel.CreateMutexW.argtypes = [ct.c_void_p, wt.BOOL, wt.LPCWSTR]
        for name in ("SetEvent", "ResetEvent", "ReleaseMutex"):
            getattr(self.kernel, name).argtypes = [wt.HANDLE]
            getattr(self.kernel, name).restype = wt.BOOL
        try:
            self.ready = self.kernel.CreateEventW(None, True, False, "Local\\Quest3D.Audio.Ready." + token)
            if not self.ready or ct.get_last_error() == 183:
                raise recovery.RecoveryError("Watchdog READY name is unavailable or reused")
            self.ready_owned = True
            self.live = self.kernel.CreateMutexW(None, False, "Local\\Quest3D.Audio.Live." + token)
            if not self.live or ct.get_last_error() == 183 or self.kernel.WaitForSingleObject(self.live, 0) != 0:
                raise recovery.RecoveryError("Watchdog lifetime name is unavailable or reused")
            self.owned = True
        except BaseException:
            self.close()
            raise

    def arm(self):
        if not self.kernel.SetEvent(self.ready):
            raise recovery.RecoveryError("Cannot acknowledge the planned route")

    def close(self):
        if self.ready and self.ready_owned:
            self.kernel.ResetEvent(self.ready)
        if self.owned:
            self.kernel.ReleaseMutex(self.live)
            self.owned = False
        for field in ("ready", "live"):
            handle = getattr(self, field)
            if handle:
                self.kernel.CloseHandle(handle)
                setattr(self, field, None)


def watch_registered_owner(path: Path, identity: tuple[int, str], *, inspect_lifetime,
                           signal_ready, allowed_root=recovery.ARTIFACT_ROOT,
                           sleep=time.sleep, clock=time.monotonic, on_event=None) -> dict:
    """Wait for a plan before READY; keep watching for the entire host lifetime."""
    path = Path(path)
    root = Path(allowed_root).resolve(strict=True)
    if not path.is_absolute() or path.suffix.lower() != ".json" or not path.parent.is_dir():
        raise recovery.RecoveryError("Require a prepared absolute journal path")
    if not path.resolve().is_relative_to(root):
        raise recovery.RecoveryError("Journal must remain inside project artifacts")
    armed = False
    seen = False
    inaccessible_since = None
    while True:
        owner = inspect_lifetime()
        if owner.state in {"dead", "pid_reused"}:
            return {"status": "owner_exited" if seen or path.exists() else "no_route",
                    "owner_identity": identity, "armed": armed}
        if owner.state != "alive":
            return {"status": "blocked_owner", "pending": True}
        try:
            if not path.resolve().is_relative_to(root):
                raise recovery.RecoveryError("Journal path changed outside artifacts")
            with path.open("rb") as stream:
                record = recovery.validate_journal(stream.read(65537))
            inaccessible_since = None
            seen = True
            if (record["owner_pid"], record["owner_creation_filetime"]) != identity:
                return {"status": "owner_replaced", "pending": True}
            if not armed:
                if record["phase"] != "planned" or record.get("watchdog_armed") is not False:
                    return {"status": "late_registration", "pending": True}
                signal_ready()
                armed = True
                if on_event:
                    on_event({"status": "watchdog_ready", "owner_pid": identity[0]})
        except FileNotFoundError:
            if seen:
                return {"status": "missing_journal", "pending": True}
        except OSError as error:
            if getattr(error, "winerror", None) not in (5, 32, 33):
                raise
            # Never assert readiness from an unreadable record. If previously
            # armed, stop protection and let the native health gate roll back.
            if inaccessible_since is None:
                inaccessible_since = clock()
            if armed and clock() - inaccessible_since > 0.5:
                return {"status": "journal_inaccessible", "pending": True}
        sleep(0.05)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--owner-pid", type=int, required=True)
    parser.add_argument("--owner-created", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--events-path", type=Path)
    parser.add_argument("--result-path", type=Path)
    args = parser.parse_args(argv)
    if not args.apply:
        parser.error("A watchdog may authorize native routing only with explicit --apply")
    if not 0 < args.owner_pid <= 0xFFFFFFFF or not re.fullmatch(r"[0-9]{1,20}", args.owner_created) or not 0 < int(args.owner_created) < 2**64:
        parser.error("Invalid registered owner identity")
    lifetime = signals = None
    def emit(value, *, result=False):
        path = args.result_path if result else args.events_path
        if path is None:
            print(json.dumps(value, ensure_ascii=False, allow_nan=False),
                  file=sys.stdout if result else sys.stderr, flush=True)
            return
        if not path.is_absolute() or not path.resolve().is_relative_to(recovery.ARTIFACT_ROOT.resolve()):
            raise recovery.RecoveryError("Watchdog output path must remain inside artifacts")
        if result:
            temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
            try:
                with temporary.open("x", encoding="utf-8") as output:
                    json.dump(value, output, ensure_ascii=False, allow_nan=False)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
        else:
            with path.open("a", encoding="utf-8") as output:
                output.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n")
                output.flush()
    try:
        identity = (args.owner_pid, args.owner_created)
        lifetime = ProcessLifetime(*identity)
        signals = WatchdogSignals(args.token)
        notify = lambda event: emit(event)
        notify({"status": "watchdog_started", "owner_pid": args.owner_pid})
        waited = watch_registered_owner(args.journal, identity, inspect_lifetime=lifetime.state,
                                         signal_ready=signals.arm, on_event=notify)
        signals.close()
        if waited["status"] == "owner_exited":
            with recovery.WindowsAudioBackend(allow_changes=True) as backend:
                result = recovery.recover_journal(args.journal, backend, apply=True, expected_owner=identity)
        else:
            result = waited
        emit(result, result=True)
        return 0 if result["status"] in {"no_route", "restored", "already_restored"} else 2
    except (recovery.RecoveryError, OSError, ValueError) as error:
        emit({"status": "refused", "error": str(error)}, result=True)
        return 2
    finally:
        if signals:
            signals.close()
        if lifetime:
            lifetime.close()


if __name__ == "__main__":
    raise SystemExit(main())

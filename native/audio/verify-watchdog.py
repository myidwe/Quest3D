"""Exercise native READY gates and real child lifetimes with an inert audio backend."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import uuid

from quest3d import audio_recovery as recovery
from quest3d import audio_watchdog as watchdog

ROOT = Path(__file__).resolve().parents[2]


class InertBackend:
    """Represent a later external selection; never access Windows audio APIs."""
    calls = 0
    def current(self, _): return "user-selected-device"
    def endpoint_active(self, _): return True
    def set_default(self, *_):
        self.calls += 1
        raise AssertionError("This verification must never change an endpoint")


def worker(directory, pid, created, token):
    lifetime = watchdog.ProcessLifetime(pid, created)
    signals = watchdog.WatchdogSignals(token)
    try:
        result = watchdog.watch_registered_owner(directory / "route.json", (pid, created),
            inspect_lifetime=lifetime.state, signal_ready=signals.arm)
        signals.close()
        if result["status"] == "owner_exited":
            backend = InertBackend()
            result = recovery.recover_journal(directory / "route.json", backend,
                apply=True, expected_owner=(pid, created))
            assert backend.calls == 0
        (directory / "result.json").write_text(json.dumps(result), encoding="utf-8")
        return 0 if result["status"] in {"restored", "already_restored", "no_route"} else 2
    finally:
        signals.close()
        lifetime.close()


class Child:
    def __init__(self, argv, **kwargs):
        self.process = subprocess.Popen(argv, cwd=ROOT, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **kwargs)
        self.lines = queue.Queue()
        def read():
            for line in self.process.stdout:
                self.lines.put(line.strip())
        self.reader = threading.Thread(target=read, daemon=True)
        self.reader.start()

    def expect(self, value, timeout=8):
        actual = self.lines.get(timeout=timeout)
        assert actual == value, (actual, value)

    def command(self, value):
        self.process.stdin.write(value + "\n")
        self.process.stdin.flush()

    def close(self):
        if self.process.poll() is None:
            # Only this function's own probe/watchdog child may be killed.
            self.process.kill()
        self.process.wait(timeout=8)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            stream.close()


def scenario(directory: Path, kind: str):
    directory.mkdir()
    token = uuid.uuid4().hex
    environment = dict(os.environ, QUEST3D_AUDIO_WATCHDOG_TOKEN=token)
    probe = Child([str(ROOT / "native/audio/dist/audio_watchdog_probe.exe"),
                   "250" if kind == "ready_timeout" else "5000"], env=environment)
    watcher = None
    started = time.monotonic()
    try:
        probe.expect("STARTED")
        if kind == "ready_timeout":
            probe.expect("REFUSED")
            assert probe.process.wait(timeout=3) == 3
            elapsed = time.monotonic() - started
            assert elapsed < 3
            return {"case": kind, "elapsed_ms": elapsed * 1000, "status": "passed"}
        pid = probe.process.pid
        created = str(recovery.process_identity(pid)[0])
        journal = directory / "route.json"
        if kind == "never_routed":
            # Exercise the actual CLI's entire no-route lifetime: no COM backend
            # is constructed because no journal exists when the owner exits.
            watcher = Child([sys.executable, "-m", "quest3d.audio_watchdog", "--journal", str(journal),
                "--owner-pid", str(pid), "--owner-created", created, "--token", token, "--apply",
                "--events-path", str(directory / "events.jsonl"), "--result-path", str(directory / "result.json")])
            deadline = time.monotonic() + 5
            while not (directory / "events.jsonl").exists() and time.monotonic() < deadline:
                time.sleep(.01)
            assert (directory / "events.jsonl").exists()
            probe.process.kill()
            probe.process.wait(timeout=3)
        else:
            record = {"schema": 1, "owner_pid": pid, "owner_creation_filetime": created,
                "phase": "planned", "watchdog_armed": False, "format_changes": False,
                "mute_changes": False, "volume_changes": False,
                "roles": [{"role": role, "original": "original", "target": "virtual", "applied": False}
                          for role in range(3)]}
            recovery.write_journal(journal, record)
            watcher = Child([sys.executable, str(Path(__file__).resolve()), "--worker", str(directory),
                             str(pid), created, token])
            probe.expect("READY")
            probe.command("health")
            probe.expect("ALIVE")
            if kind == "owner_crash":
                record.update(watchdog_armed=True, phase="active")
                for role in record["roles"]: role["applied"] = True
                recovery.write_journal(journal, record)
                probe.process.kill()
            elif kind == "watchdog_crash":
                watcher.process.kill()
                watcher.process.wait(timeout=3)
                probe.command("health")
                probe.expect("DEAD")
                probe.command("exit")
            else:
                probe.command("exit")
            probe.process.wait(timeout=3)
        if kind == "watchdog_crash":
            backend = InertBackend()
            result = recovery.recover_journal(journal, backend, apply=True, expected_owner=(pid, created))
            assert backend.calls == 0
        else:
            assert watcher.process.wait(timeout=8) == 0, watcher.process.stderr.read()
            result = json.loads((directory / "result.json").read_text(encoding="utf-8"))
        assert result["status"] in {"no_route", "restored"}, result
        assert result.get("changed_roles", []) == []
        if kind == "owner_crash":
            assert all(role["recovery_status"] == "external_preserved"
                       for role in json.loads(journal.read_text())["roles"])
        return {"case": kind, "elapsed_ms": (time.monotonic() - started) * 1000,
                "status": "passed", "recovery_status": result["status"], "actual_routing_changed": False}
    finally:
        probe.close()
        if watcher: watcher.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", nargs=4)
    args = parser.parse_args()
    if args.worker:
        directory, pid, created, token = args.worker
        return worker(Path(directory), int(pid), created, token)
    directory = ROOT / "artifacts/audio" / ("watchdog-" + time.strftime("%Y%m%d-%H%M%S"))
    directory.mkdir()
    results = [scenario(directory / kind, kind) for kind in
               ("normal_exit", "owner_crash", "watchdog_crash", "ready_timeout", "never_routed")]
    summary = {"tests": len(results), "results": results, "actual_routing_changed": False,
               "backend": "inert", "native_probe": str(ROOT / "native/audio/dist/audio_watchdog_probe.exe")}
    (directory / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps({"tests": len(results), "result": str(directory / "summary.json"), "actual_routing_changed": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

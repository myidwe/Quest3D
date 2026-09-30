"""Bounded crash-owner/watchdog test. Default mode never changes audio routing.

--route-once is a separately authorized real routing test: launch the watchdog,
change to the explicitly selected endpoint, immediately crash only our child,
verify restoration, and preserve a recovery journal if anything fails.
"""

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

from quest3d.audio_recovery import ARTIFACT_ROOT, WindowsAudioBackend, journal_lock, process_identity, recover_journal, write_journal


def line_with_timeout(stream, timeout=10):
    result = queue.Queue()
    threading.Thread(target=lambda: result.put(stream.readline()), daemon=True).start()
    value = result.get(timeout=timeout)
    if not value:
        raise RuntimeError("Child ended before the expected protocol message")
    return json.loads(value)


def owner_child(args):
    with WindowsAudioBackend(allow_changes=args.route_once) as backend:
        before = backend.snapshot()
        expected = json.loads(args.expected.read_text(encoding="utf-8"))
        if before != expected:
            raise RuntimeError("Audio state changed before this test; no routing was started")
        if args.route_once and (not args.target or not backend.endpoint_active(args.target)):
            raise RuntimeError("Explicit target endpoint is unavailable")
        record = {
            "schema": 1, "owner_pid": os.getpid(), "owner_creation_filetime": str(process_identity(os.getpid())[0]),
            "phase": "planned", "format_changes": False, "mute_changes": False, "volume_changes": False,
            "roles": [{"role": role, "original": original, "target": args.target if args.route_once else original, "applied": False}
                      for role, original in enumerate(before["default_roles"])],
        }
        with journal_lock(args.journal):
            write_journal(args.journal, record)
            print(json.dumps({"status": "owner_ready", "owner_pid": os.getpid()}), flush=True)
            if sys.stdin.readline().strip() != "begin":
                raise RuntimeError("Test was not started")
            if args.route_once:
                for role in record["roles"]:
                    current = backend.current(role["role"])
                    if current not in {role["original"], role["target"]}:
                        raise RuntimeError("User changed an endpoint during test setup")
                    if current != role["target"]:
                        backend.set_default(role["role"], role["target"])
                    role["applied"] = role["original"] != role["target"]
                    record["phase"] = "active"
                    write_journal(args.journal, record)
            print(json.dumps({"status": "owner_routed" if args.route_once else "owner_noop", "state": backend.snapshot()}), flush=True)
            sys.stdin.readline()
            # Intentional abnormal exit tests OS lock release rather than Python
            # context-manager restoration; our watchdog has already acknowledged.
            os._exit(19)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--route-once", action="store_true")
    parser.add_argument("--target")
    parser.add_argument("--owner-child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--journal", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--expected", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.owner_child:
        owner_child(args)
        return 0
    if args.route_once and not args.target:
        parser.error("Real routing test requires an explicit --target endpoint ID")
    directory = ARTIFACT_ROOT / "audio" / ("recovery-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
    directory.mkdir(parents=True)
    journal = directory / "route.json"
    expected = directory / "before.json"
    child = watchdog = None
    changed_started = None
    report = {"mode": "route_once" if args.route_once else "no_route", "journal": str(journal), "fixture": True}
    with WindowsAudioBackend(allow_changes=args.route_once) as backend:
        before = backend.snapshot()
        expected.write_text(json.dumps(before), encoding="utf-8")
        command = [sys.executable, __file__, "--owner-child", "--journal", str(journal), "--expected", str(expected)]
        if args.route_once:
            command += ["--route-once", "--target", args.target]
        try:
            child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, creationflags=0x08000000)
            report["owner_ready"] = line_with_timeout(child.stdout)
            watchdog = subprocess.Popen([sys.executable, "-m", "quest3d.audio_recovery", "--journal", str(journal), "--apply", "--watch-owner", "--timeout", "10"],
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, creationflags=0x08000000)
            report["watchdog_ready"] = line_with_timeout(watchdog.stderr)
            if report["watchdog_ready"]["status"] != "watching_owner":
                raise RuntimeError("Watchdog did not acknowledge the live child")
            changed_started = time.perf_counter()
            child.stdin.write("begin\n")
            child.stdin.flush()
            report["owner_action"] = line_with_timeout(child.stdout, timeout=3)
            child.communicate("exit\n", timeout=3)
            report["owner_exit_code"] = child.returncode
            output, errors = watchdog.communicate(timeout=5)
            report["watchdog_exit_code"] = watchdog.returncode
            report["watchdog"] = json.loads(output)
            report["watchdog_stderr"] = errors
            report["action_to_recovery_ms"] = (time.perf_counter() - changed_started) * 1000
            if child.returncode != 19 or watchdog.returncode != 0 or report["watchdog"]["status"] != "restored":
                raise RuntimeError("Crash recovery did not complete successfully")
        except Exception as error:
            report["error"] = str(error)
        finally:
            # Only terminate helpers created above. Never locate/kill a user host.
            if child and child.poll() is None:
                child.kill()
                child.communicate(timeout=5)
            if watchdog and watchdog.poll() is None:
                try:
                    output, errors = watchdog.communicate(timeout=5)
                    report["watchdog_after_cleanup"] = {"output": output, "stderr": errors, "exit_code": watchdog.returncode}
                except subprocess.TimeoutExpired:
                    watchdog.kill()
                    watchdog.communicate(timeout=5)
            if journal.exists():
                # Guarded fallback uses the same ownership checks after child death.
                report["final_recovery"] = recover_journal(journal, backend, apply=True)
            after = backend.snapshot()
            report["before"] = before
            report["after"] = after
            report["state_restored"] = before == after
            report["quest_audio_verified"] = False
            report["av_sync_verified"] = False
            (directory / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if "error" not in report and report["state_restored"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

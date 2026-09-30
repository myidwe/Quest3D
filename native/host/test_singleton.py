"""Exercise the actual Windows guard using only owned helper processes.

The real host is tested only while another process holds its test-port mutex;
normal startup must exit before display/audio/input/network initialization.
"""
import argparse
import ctypes
from ctypes import wintypes
import json
from pathlib import Path
import queue
import secrets
import shutil
import subprocess
import tempfile
import threading


def run(exe, args, *, cwd=None):
    return subprocess.run([str(exe), *map(str, args)], cwd=cwd, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)


def hold(exe, port):
    child = subprocess.Popen([str(exe), "--hold", str(port)], text=True,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT,
                             creationflags=subprocess.CREATE_NO_WINDOW)
    ready = queue.Queue()
    threading.Thread(target=lambda: ready.put(child.stdout.readline()), daemon=True).start()
    try:
        if ready.get(timeout=5).strip() != "READY":
            raise RuntimeError("Guard helper did not acquire the isolated test port")
    except BaseException:
        child.kill()
        child.wait(timeout=5)
        raise
    return child


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--probe", type=Path, required=True)
    parser.add_argument("--host", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    args = parser.parse_args()
    probe, host = args.probe.resolve(strict=True), args.host.resolve(strict=True)
    output = Path(tempfile.mkdtemp(prefix="singleton-", dir=args.artifacts.resolve(strict=True)))
    other = output / "other-runtime-probe.exe"
    shutil.copy2(probe, other)
    port = 61000 + secrets.randbelow(3000)
    checks = []
    child = hold(probe, port)
    try:
        assert run(probe, ["--probe", port]).returncode == 9
        checks.append("same_port_blocked")
        assert run(other, ["--probe", port]).returncode == 9
        checks.append("different_runtime_same_port_blocked")
        assert run(other, ["--probe", port + 1]).returncode == 0
        checks.append("different_port_independent")
        config = output / "sunshine.conf"
        config.write_text("\n".join([
            "capture = quest3d", f"port = {port}", "keyboard = disabled",
            "mouse = disabled", "controller = disabled", "upnp = disabled",
            "stream_audio = disabled", "dd_configuration_option = disabled",
            f"credentials_file = {(output / 'credentials.json').as_posix()}",
            f"log_path = {(output / 'host.log').as_posix()}", "",
        ]), encoding="utf-8")
        blocked = run(host, [config], cwd=host.parent)
        (output / "blocked-startup.log").write_text(blocked.stdout, encoding="utf-8")
        assert blocked.returncode == 9 and "Quest3D base port" in blocked.stdout
        checks.append("actual_host_rejects_before_platform_init")
        for command in ["--help", "--version"]:
            result = run(host, [config, command], cwd=host.parent)
            assert result.returncode == 0
            if command == "--version":
                assert "2026.906.222525" in result.stdout
            checks.append(command + "_coexists")
        assert run(host, [config, "--creds", "isolated-test", secrets.token_hex(16)],
                   cwd=host.parent).returncode == 0
        assert (output / "credentials.json").is_file()
        checks.append("isolated_credentials_command_coexists")
        child.communicate("\n", timeout=5)
        assert child.returncode == 0
        assert run(other, ["--probe", port]).returncode == 0
        checks.append("normal_exit_releases")
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)

    child = hold(other, port)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.OpenMutexW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    retained = kernel.OpenMutexW(0x00100000, False, f"Global\\Quest3D.Sunshine.Port.{port}")
    try:
        assert retained
        child.kill()  # Only the exact Popen helper created above; never a host/user process.
        child.wait(timeout=5)
        assert run(probe, ["--probe", port]).returncode == 0
        checks.append("forced_exit_abandoned_mutex_recovers")
    finally:
        if retained:
            kernel.CloseHandle(retained)
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
    report = {"checks": checks, "passed": len(checks), "port": port,
              "artifact_directory": str(output), "real_server_started": False,
              "user_process_terminated": False}
    (args.artifacts / "singleton-verification.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    main()

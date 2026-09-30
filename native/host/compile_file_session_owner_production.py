"""Compile the actual non-test stream TU with pinned Sunshine settings, without linking/deploying."""
from pathlib import Path
import ctypes
import hashlib
import json
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "third_party/sunshine"
BASE = SOURCE / "cmake-build-quest3d"
OUT = ROOT / "artifacts/host/cmake-build-file-owner-20260910-a"
OUT.mkdir(parents=True, exist_ok=True)
paths = [SOURCE / "src/stream.cpp", SOURCE / "src/stream.h", SOURCE / "src/quest3d_file_session_owner.h"]
before = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
entry = next(item for item in json.loads((BASE / "compile_commands.json").read_text())
             if item["file"].endswith("/stream.cpp") and "/tests/" not in item["output"])
shell = ctypes.WinDLL("shell32", use_last_error=True)
shell.CommandLineToArgvW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
shell.CommandLineToArgvW.restype = ctypes.POINTER(ctypes.c_wchar_p)
count = ctypes.c_int()
pointer = shell.CommandLineToArgvW(entry["command"], ctypes.byref(count))
arguments = [pointer[index] for index in range(count.value)]
kernel = ctypes.WinDLL("kernel32")
kernel.LocalFree.argtypes = [ctypes.c_void_p]
kernel.LocalFree(pointer)
arguments[0] = str(ROOT / "native/host/tools/msys64/ucrt64/bin/c++.exe")
assert not any("SUNSHINE_TESTS" in item for item in arguments)
output = OUT / "stream-production.obj"
arguments[arguments.index("-o") + 1] = str(output)
attempt = 1
while (OUT / f"production-compile-{attempt}.log").exists():
    attempt += 1
result = subprocess.run(arguments, cwd=BASE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
(OUT / f"production-compile-{attempt}.log").write_bytes(result.stdout)
after = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
proof = dict(exit_code=result.returncode, source_before=before, source_after=after, source_unchanged=before == after,
             test_macro=False, original_command=entry["command"], actual_arguments=arguments,
             object_sha256=hashlib.sha256(output.read_bytes()).hexdigest() if result.returncode == 0 else None)
(OUT / f"production-verification-{attempt}.json").write_text(json.dumps(proof, indent=2) + "\n")
print(result.stdout.decode("utf-8", errors="replace"))
print(json.dumps(dict(exit_code=result.returncode, source_unchanged=before == after, attempt=attempt)))
raise SystemExit(result.returncode if before == after else 1)

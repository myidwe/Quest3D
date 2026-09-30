"""Link an interoperability server to the frozen, previously verified actual nvhttp object."""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "third_party/sunshine"
BASE = SOURCE / "cmake-build-quest3d"
OUT = ROOT / "artifacts/host/cmake-build-file-audio-interop-20260910-a"
OUT.mkdir(parents=True, exist_ok=True)
COMPILER = ROOT / "native/host/tools/msys64/ucrt64/bin/c++.exe"
commands = json.loads((BASE / "compile_commands.json").read_text())
entry = next(item for item in commands if item["file"].endswith("/nvhttp.cpp") and "/tests/" in item["output"])


def split_windows(command):
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    shell.CommandLineToArgvW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
    shell.CommandLineToArgvW.restype = ctypes.POINTER(ctypes.c_wchar_p)
    count = ctypes.c_int()
    pointer = shell.CommandLineToArgvW(command, ctypes.byref(count))
    result = [pointer[index] for index in range(count.value)]
    kernel = ctypes.WinDLL("kernel32")
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree(pointer)
    return result


def run(argv, name):
    print(name, flush=True)
    result = subprocess.run(argv, cwd=BASE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    log = OUT / (name + ".log")
    attempt = 1
    while log.exists():
        attempt += 1
        log = OUT / (name + f"-{attempt}.log")
    log.write_bytes(result.stdout)
    if result.returncode:
        print(result.stdout[-12000:].decode("utf-8", errors="replace"))
        raise SystemExit(result.returncode)


frozen = ROOT / "artifacts/host/cmake-build-file-audio-20260910-a"
prior = json.loads((frozen / "build-verification.json").read_text())
for name in ("third_party\\sunshine\\src\\nvhttp.cpp", "third_party\\sunshine\\src\\quest3d_file_audio_stream.h"):
    assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == prior["source_sha256"][name]
objects = {"nvhttp": frozen / "nvhttp.obj"}
for name, source in (("fixture", ROOT / "native/host/file_audio_interop_server.cpp"),):
    arguments = split_windows(entry["command"])
    arguments[0] = str(COMPILER)
    arguments[arguments.index("-c") + 1] = str(source)
    output = OUT / (name + ".obj")
    arguments[arguments.index("-o") + 1] = str(output)
    arguments = [arg for arg in arguments if not arg.startswith(("-DSUNSHINE_TEST_BIN_DIR=", "-DSUNSHINE_ASSETS_DIR="))]
    arguments += [f'-DSUNSHINE_TEST_BIN_DIR="{OUT.as_posix()}"', f'-DSUNSHINE_ASSETS_DIR="{OUT.as_posix()}/assets"',
                  f'-DFILE_AUDIO_VECTOR_DIR="{(ROOT / "artifacts/audio/file-audio-vectors-20260910-a").as_posix()}"']
    if "--link-only" not in sys.argv and not ("--fixture-only" in sys.argv and name == "nvhttp"):
        run(arguments, name + "-compile")
    objects[name] = output

ninja = (BASE / "build.ninja").read_text()
block = re.search(r"^build tests/test_sunshine\.exe: .*?(?=\n\n)", ninja, re.M | re.S).group(0)
first, *rest = block.splitlines()
inputs = first.split(": ", 1)[1].split(" | ", 1)[0].split()[1:]
inputs = [path for path in inputs if "/test_sunshine.dir/" not in path or "/__/" in path]
old_nvhttp = "tests/CMakeFiles/test_sunshine.dir/__/src/nvhttp.cpp.obj"
assert old_nvhttp in inputs
inputs[inputs.index(old_nvhttp)] = objects["nvhttp"].as_posix()
inputs.append(objects["fixture"].as_posix())
variables = dict(line.strip().split(" = ", 1) for line in rest if " = " in line)
response = OUT / "link.rsp"
response.write_text("\n".join('"' + path.replace("\\", "/") + '"' for path in inputs) + "\n" + variables["LINK_LIBRARIES"] + "\n")
executable = OUT / "test_sunshine.exe"
run([str(COMPILER), *split_windows("unused " + variables.get("FLAGS", ""))[1:],
     *split_windows("unused " + variables.get("LINK_FLAGS", ""))[1:], "@" + str(response), "-o", str(executable)], "fixture-link")
source_paths = [SOURCE / "src/nvhttp.cpp", SOURCE / "src/quest3d_file_audio_stream.h", ROOT / "native/host/file_audio_interop_server.cpp", Path(__file__)]
proof = dict(source_sha256={str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in source_paths},
    executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(), frozen_nvhttp_object_sha256=hashlib.sha256(objects["nvhttp"].read_bytes()).hexdigest(), shared_build_modified=False,
    private_output=str(OUT), actual_runtime_verified=False)
latest = OUT / "build-verification.json"
if latest.exists():
    history = 1
    while (OUT / f"build-verification-prior-{history}.json").exists():
        history += 1
    (OUT / f"build-verification-prior-{history}.json").write_bytes(latest.read_bytes())
latest.write_text(json.dumps(proof, indent=2) + "\n")
print(str(executable), flush=True)

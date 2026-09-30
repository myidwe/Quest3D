"""Build isolated native IPC CLI/gtest using the pinned workspace MSYS2 UCRT64."""
from pathlib import Path
import hashlib
import json
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "third_party/sunshine"
OUT = ROOT / "artifacts/host/cmake-build-file-audio-ipc/tests"
OUT.mkdir(parents=True, exist_ok=True)
MSYS = ROOT / "native/host/tools/msys64/msys2_shell.cmd"


def build(source, output, extra=()):
    arguments = ["-std=c++20", "-O0", "-g", "-Wall", "-Wextra", "-Werror", "-DNOMINMAX", "-DWIN32_LEAN_AND_MEAN",
                 "-I" + SOURCE.as_posix(), str(source), *extra, "-lcrypto", "-o", str(output)]
    if output.stem == "test_sunshine":
        arguments.append("--coverage")
    response = OUT / (output.stem + ".rsp")
    response.write_text("\n".join('"' + str(arg).replace("\\", "/") + '"' for arg in arguments))
    command = "/ucrt64/bin/c++ @" + "'" + response.as_posix() + "'"
    result = subprocess.run([str(MSYS), "-defterm", "-here", "-no-start", "-ucrt64", "-c", command],
                            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    log = OUT / (output.stem + "-build.log")
    index = 1
    while log.exists():
        index += 1
        log = OUT / (output.stem + f"-build-{index}.log")
    log.write_bytes(result.stdout)
    print(result.stdout.decode(errors="replace"))
    if result.returncode:
        raise SystemExit(result.returncode)


if "--tests-only" not in sys.argv:
    build(ROOT / "native/host/file_audio_ipc_reader_cli.cpp", OUT / "file_audio_ipc_reader_cli.exe")
test = ROOT / "native/host/file_audio_ipc_test.cpp"
if test.exists():
    # Pin the checked-out gtest headers and prebuilt library used by Sunshine itself.
    headers = SOURCE / "third-party/lizardbyte-common/third-party/googletest/googletest/include"
    assert (headers / "gtest/gtest.h").exists()
    build(test, OUT / "test_sunshine.exe", ["-I" + headers.as_posix(), "-DGTEST_HAS_PTHREAD=0",
          str(SOURCE / "cmake-build-quest3d/lib/libgtest.a")])
paths = [SOURCE / "src/quest3d_file_audio_ipc.h", SOURCE / "src/platform/windows/quest3d_file_audio_ipc_reader.h",
         SOURCE / "src/quest3d_file_audio_stream.h", ROOT / "native/host/file_audio_ipc_reader_cli.cpp", Path(__file__),
         ROOT / "native/host/run_file_audio_ipc_fixture.py"]
if test.exists(): paths.append(test)
proof = {"source_sha256": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths},
         "binary_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in OUT.glob("*.exe")},
         "compiler": "workspace MSYS2 UCRT64", "shared_build_modified": False, "runtime_verified": False}
previous = OUT / "build-verification.json"
if previous.exists():
    index = 1
    while (OUT / f"build-verification-prior-{index}.json").exists():
        index += 1
    (OUT / f"build-verification-prior-{index}.json").write_bytes(previous.read_bytes())
previous.write_text(json.dumps(proof, indent=2) + "\n")
print(OUT)

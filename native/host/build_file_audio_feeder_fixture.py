"""Build only the new synchronous feeder fixture with pinned workspace Windows tools."""
from pathlib import Path
import hashlib
import json
import subprocess

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "third_party/sunshine"
OUT = ROOT / "artifacts/host/cmake-build-file-audio-feeder/tests"
OUT.mkdir(parents=True, exist_ok=True)
MSYS = ROOT / "native/host/tools/msys64/msys2_shell.cmd"
HEADERS = SOURCE / "third-party/lizardbyte-common/third-party/googletest/googletest/include"
EXE = OUT / "test_sunshine.exe"
paths = [SOURCE / "src/platform/windows/quest3d_file_audio_feeder.h",
         SOURCE / "src/platform/windows/quest3d_file_audio_ipc_reader.h",
         SOURCE / "src/quest3d_file_audio_ipc.h", SOURCE / "src/quest3d_file_audio_stream.h",
         ROOT / "native/host/file_audio_feeder_test.cpp", Path(__file__),
         ROOT / "native/host/run_file_audio_feeder_fixture.py",
         ROOT / "artifacts/audio/file-audio-vectors-20260910-a/first.wire",
         ROOT / "artifacts/audio/file-audio-vectors-20260910-a/first.pcm"]
before = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
arguments = ["-std=c++20", "-O0", "-g", "-Wall", "-Wextra", "-Werror", "-DNOMINMAX",
             "-DWIN32_LEAN_AND_MEAN", "-DGTEST_HAS_PTHREAD=0", "--coverage",
             '-DFILE_AUDIO_VECTOR_DIR="'+(ROOT / "artifacts/audio/file-audio-vectors-20260910-a").as_posix()+'"',
             "-I"+SOURCE.as_posix(), "-I"+HEADERS.as_posix(),
             str(ROOT / "native/host/file_audio_feeder_test.cpp"),
             str(SOURCE / "cmake-build-quest3d/lib/libgtest.a"), "-lcrypto", "-o", str(EXE)]
rsp = OUT / "build.rsp"
rsp.write_text("\n".join('"'+str(value).replace("\\", "/").replace('"', '\\"')+'"' for value in arguments))
result = subprocess.run([str(MSYS), "-defterm", "-here", "-no-start", "-ucrt64", "-c",
                         "/ucrt64/bin/c++ @'"+rsp.as_posix()+"'"], cwd=ROOT,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
attempt = 1
while (OUT / f"build-{attempt}.log").exists(): attempt += 1
(OUT / f"build-{attempt}.log").write_bytes(result.stdout)
print(result.stdout.decode(errors="replace"))
if result.returncode: raise SystemExit(result.returncode)
after = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
assert before == after
proof = {"source_sha256": after, "executable_sha256": hashlib.sha256(EXE.read_bytes()).hexdigest(),
         "compiler": "workspace MSYS2 UCRT64", "shared_build_modified": False,
         "scope": "synchronous feeder only; no new scheduler/auth/coordinator"}
(OUT / f"build-{attempt}.json").write_text(json.dumps(proof, indent=2)+"\n")
(OUT / "build-verification.json").write_text(json.dumps(proof, indent=2)+"\n")
print(EXE)

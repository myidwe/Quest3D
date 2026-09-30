"""Build the isolated Windows file-child fixture using the fixed workspace MSYS2 toolchain."""
from pathlib import Path
import hashlib
import json
import subprocess

ROOT=Path(__file__).resolve().parents[2]
SOURCE=ROOT/"third_party/sunshine"
OUT=ROOT/"artifacts/host/cmake-build-file-child/tests"; OUT.mkdir(parents=True,exist_ok=True)
EXE=OUT/"test_sunshine.exe"
headers=SOURCE/"third-party/lizardbyte-common/third-party/googletest/googletest/include"
args=["-std=c++20","-O0","-g","-Wall","-Wextra","-Werror","-DNOMINMAX","-DWIN32_LEAN_AND_MEAN","-DGTEST_HAS_PTHREAD=0","--coverage",
      "-I"+SOURCE.as_posix(),"-I"+headers.as_posix(),
      '-DFILE_CHILD_PYTHON=L"'+(ROOT/".venv/Scripts/python.exe").as_posix()+'"',
      '-DFILE_CHILD_FIXTURE=L"'+(ROOT/"native/host/file_child_fixture.py").as_posix()+'"',
      '-DFILE_CHILD_ROOT=L"'+ROOT.as_posix()+'"',
      str(ROOT/"native/host/file_child_test.cpp"),str(SOURCE/"cmake-build-quest3d/lib/libgtest.a"),"-lbcrypt","-ladvapi32","-o",str(EXE)]
response=OUT/"build.rsp"
response.write_text("\n".join('"'+str(v).replace("\\","/").replace('"','\\"')+'"' for v in args))
paths=[SOURCE/"src/platform/windows/quest3d_file_child.h",ROOT/"native/host/file_child_fixture.py",ROOT/"native/host/file_child_test.cpp",Path(__file__)]
paths.extend(ROOT / name for name in ("src/quest3d/file_worker.py", "src/quest3d/file_worker_protocol.py",
    "src/quest3d/media_playout.py", "src/quest3d/file_session.py",
    "artifacts/audio/file-audio-vectors-20260910-a/known-av.nut"))
before={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
result=subprocess.run([str(ROOT/"native/host/tools/msys64/msys2_shell.cmd"),"-defterm","-here","-no-start","-ucrt64","-c",
                       "/ucrt64/bin/c++ @'"+response.as_posix()+"'"],cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=60)
attempt=1
while (OUT/f"build-{attempt}.log").exists(): attempt+=1
(OUT/f"build-{attempt}.log").write_bytes(result.stdout); print(result.stdout.decode(errors="replace"))
if result.returncode: raise SystemExit(result.returncode)
after={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}; assert before==after
proof={"source_sha256":after,"executable_sha256":hashlib.sha256(EXE.read_bytes()).hexdigest(),"shared_build_modified":False,
       "scope":"private inherited pipe/process/job component; current user session only"}
(OUT/f"build-{attempt}.json").write_text(json.dumps(proof,indent=2)+"\n")
(OUT/"build-verification.json").write_text(json.dumps(proof,indent=2)+"\n"); print(EXE)

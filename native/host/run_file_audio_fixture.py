"""Execute only the private loopback fixture and preserve each attempt's evidence."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import psutil

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "artifacts/host/cmake-build-file-audio-20260910-a"


def protected():
    files = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in (
        ROOT / "artifacts/active-session.json", ROOT / "artifacts/host/dev/process.json")}
    processes = []
    for pid in (37200, 33436, 42148):
        process = psutil.Process(pid)
        processes.append(dict(pid=pid, birth=process.create_time(), command_sha256=hashlib.sha256(json.dumps(process.cmdline()).encode()).hexdigest()))
    return dict(files=files, processes=processes)


before = protected()
build = json.loads((OUT / "build-verification.json").read_text())
source_before = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in build["source_sha256"]}
attempt = 2
while (OUT / f"runtime-{attempt}.log").exists():
    attempt += 1
env = dict(os.environ, GCOV_PREFIX=str(OUT / f"coverage-{attempt}"))
env["PATH"] = str(ROOT / "native/host/tools/msys64/ucrt64/bin") + os.pathsep + env["PATH"]
argv = [str(OUT / "test_sunshine.exe"), f"--gtest_output=xml:results-{attempt}.xml", *sys.argv[1:]]
try:
    result = subprocess.run(argv, cwd=OUT, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
    output, code = result.stdout, result.returncode
except subprocess.TimeoutExpired as error:
    output = error.stdout or b""
    output = output.decode(errors="replace") if isinstance(output, bytes) else output
    output += "\nOwned private fixture exceeded 60 seconds and was terminated; this is a failed run.\n"
    code = -1
(OUT / f"runtime-{attempt}.log").write_text(output)
after = protected()
source_after = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in source_before}
proof = dict(exit_code=code, protected_before=before, protected_after=after, protected_unchanged=before == after,
    source_before=source_before, source_after=source_after,
    source_matches_build=source_before == source_after == build["source_sha256"],
    tests_xml=str(OUT / f"results-{attempt}.xml"), executable_sha256=hashlib.sha256((OUT / "test_sunshine.exe").read_bytes()).hexdigest())
(OUT / f"runtime-verification-{attempt}.json").write_text(json.dumps(proof, indent=2) + "\n")
print(output[-16000:])
print(json.dumps(dict(exit_code=code, protected_unchanged=before == after, attempt=attempt)))
raise SystemExit(code if before == after and proof["source_matches_build"] and proof["executable_sha256"] == build["executable_sha256"] else 1)

"""Record actual Windows child process and private pipe results and coverage."""
from pathlib import Path
import gzip
import hashlib
import json
import os
import subprocess
import xml.etree.ElementTree as ET
from verify_file_child_transcript import verify

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "artifacts/host/cmake-build-file-child/tests"
build = json.loads((OUT / "build-verification.json").read_text())
before = {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in build["source_sha256"]}
assert before == build["source_sha256"]
EXE = OUT / "test_sunshine.exe"
binary = hashlib.sha256(EXE.read_bytes()).hexdigest()
assert binary == build["executable_sha256"]
attempt=1
while (OUT / f"run-{attempt}").exists(): attempt += 1
RUN = OUT / f"run-{attempt}"; RUN.mkdir()
data = OUT / "test_sunshine-file_child_test.gcda"
if data.exists(): data.rename(RUN / "previous.gcda")
env = dict(os.environ)
env["PATH"] = str(ROOT / "native/host/tools/msys64/ucrt64/bin")+os.pathsep+env["PATH"]
result = subprocess.run([str(EXE), "--gtest_output=xml:"+str(RUN / "results.xml")], cwd=RUN, env=env,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
(RUN / "runtime.log").write_bytes(result.stdout)
print(result.stdout.decode(errors="replace"))
coverage = subprocess.run([str(ROOT / "native/host/tools/msys64/ucrt64/bin/gcov.exe"), "-j", "-b",
                          str(OUT / "test_sunshine-file_child_test.gcno")], cwd=RUN, env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
(RUN / "coverage.log").write_bytes(coverage.stdout)
metrics={}
if coverage.returncode == 0:
    raw=json.loads(gzip.decompress((RUN / "test_sunshine-file_child_test.gcov.json.gz").read_bytes()))
    for value in raw["files"]:
        if value["file"].endswith("quest3d_file_child.h"):
            lines=value["lines"]; branches=[b for line in lines for b in line.get("branches", [])]
            metrics={"lines": len(lines), "covered_lines": sum(v["count"]>0 for v in lines),
                     "branches": len(branches), "taken_branches": sum(v["count"]>0 for v in branches),
                     "uncovered_lines": [v["line_number"] for v in lines if not v["count"]]}
after={p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in before}
transcript_proof = verify(RUN) if result.returncode == 0 else {"passed": False}
proof={"tests": ET.parse(RUN / "results.xml").getroot().attrib, "exit_code": result.returncode,
       "gcov_exit_code": coverage.returncode, "source_before": before, "source_after": after,
       "source_unchanged": before==after, "executable_sha256": binary, "coverage": metrics,
       "actual_worker_transcript": transcript_proof,
       "scope": "actual Windows user-session child/job and bounded raw pipes; no authenticated session or FilePCM readiness proof"}
(RUN / "verification.json").write_text(json.dumps(proof, indent=2)+"\n")
print(json.dumps(metrics, indent=2)); print(RUN)
raise SystemExit(result.returncode or coverage.returncode or int(before!=after))


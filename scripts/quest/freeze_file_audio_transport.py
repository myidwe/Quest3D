"""Freeze isolated transport evidence; never writes a source/cache/deployment."""
import hashlib
import json
from pathlib import Path

root = Path(__file__).resolve().parents[2]
out = root / "artifacts/quest/file-audio-transport"
digest = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
results = json.loads((out / "results.json").read_text())
no_exceptions = json.loads((out / "results-no-exceptions.json").read_text())
android = json.loads((out / "android-compile.json").read_text())
assert (out / "SOURCE_SHA256SUMS").read_bytes() == (out / "SOURCE_SHA256SUMS.after").read_bytes()
for line in (out / "SOURCE_SHA256SUMS").read_text().splitlines():
    expected, path = line.split("  ", 1)
    assert digest(Path(path)) == expected, path
for entry in android["results"]:
    assert entry["exit_code"] == 0 and digest(root / entry["source"]) == entry["sha256"]
for report in (results, no_exceptions):
    assert all(row["joined"] for row in report["results"])
    assert next(row for row in report["results"] if row["mode"] == "actual-first")["pcm_frames"] == 5003
    assert next(row for row in report["results"] if row["mode"] == "actual-seek")["pcm_frames"] == 4043
    assert next(row for row in report["results"] if row["mode"] == "incremental")["state"] == 3
    assert next(row for row in report["results"] if row["mode"] == "joinfail")["worker_error"] != 0
paths = [root / "docs/build/FILE_AUDIO_TRANSPORT.md", root / "scripts/quest/compile_file_audio_transport_android.py", Path(__file__)]
paths += [Path(line.split("  ", 1)[1]) for line in (out / "SOURCE_SHA256SUMS").read_text().splitlines()]
paths += [out / name for name in ("results.json", "results-no-exceptions.json", "test.log", "test-no-exceptions.log",
    "SOURCE_SHA256SUMS", "android-compile.json", "curl_http_client-android.o", "file_audio_transport-android.o",
    "file-audio-client", "file-audio-client-no-exceptions", "tls-client")]
paths += [root / "artifacts/audio/file-audio-vectors-20260910-a" / name
          for name in ("manifest.json", "first.wire", "first.pcm", "seek.wire", "seek.pcm")]
proof = dict(passed=True, scope="Production incremental Curl/Decoder over real private mTLS; Android whole-TU compile",
    cases_exceptions=len(results["results"]), cases_no_exceptions=len(no_exceptions["results"]),
    old_curl_regressions=True, android_ndk="29.0.14206865", android_api=29,
    host_endpoint_integration=False, sink_integration=False, product_ready=False, quest_verified=False,
    sha256={str(path.relative_to(root)).replace("\\", "/"): digest(path) for path in paths})
(out / "verification.json").write_text(json.dumps(proof, indent=2) + "\n")
print("Transport evidence frozen", digest(out / "verification.json"))

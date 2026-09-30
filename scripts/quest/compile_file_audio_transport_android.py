"""Compile current network TUs using existing pinned Android flags; cache read-only."""
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess

root = Path(__file__).resolve().parents[2]
cache = Path(os.environ.get("QUEST_BUILD_CACHE", Path.home() / ".cache/quest_to_3d-quest-build"))
source = root / "third_party/nightfall/addons/nightfall-stream"
ninja = (cache / "source/addons/nightfall-stream/build/android/build.ninja").read_text()
block = ninja.split("build CMakeFiles/nightfall-stream.dir/src/network/curl_http_client.cpp.o:", 1)[1].split("\nbuild ", 1)[0]
args = []
for key in ("DEFINES", "FLAGS", "INCLUDES"):
    line = next(line for line in block.splitlines() if line.startswith(f"  {key} = "))
    args.extend(shlex.split(line.split(" = ", 1)[1]))
args = [arg.replace(str(cache / "source/addons/nightfall-stream/src"), str(source / "src"))
        .replace(str(cache / "source/addons/nightfall-stream/include"), str(source / "include")) for arg in args]
compiler = cache / "android-sdk/ndk/29.0.14206865/toolchains/llvm/prebuilt/linux-x86_64/bin/aarch64-linux-android29-clang++"
out = root / "artifacts/quest/file-audio-transport"
out.mkdir(parents=True, exist_ok=True)
results = []
for name in ("curl_http_client", "file_audio_transport"):
    path = source / f"src/network/{name}.cpp"
    command = [str(compiler), *args, "-c", str(path), "-o", str(out / f"{name}-android.o")]
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    (out / f"{name}-android.log").write_text(result.stdout + f"\nexit_code={result.returncode}\n")
    print(name, f"exit_code={result.returncode}", f"warnings={result.stdout.count('warning:')}")
    results.append(dict(source=str(path.relative_to(root)), sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                        exit_code=result.returncode, command=command))
    if result.returncode:
        print(result.stdout); raise SystemExit(result.returncode)
(out / "android-compile.json").write_text(json.dumps(dict(
    scope="Whole current C++ TUs, NDK29/API29; no Android link/runtime/Quest proof", results=results), indent=2) + "\n")

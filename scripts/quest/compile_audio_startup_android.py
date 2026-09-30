"""Compile authoritative audio TUs with pinned Android flags; cache is read-only."""
import os
from pathlib import Path
import shlex
import subprocess

root = Path(__file__).resolve().parents[2]
cache = Path(os.environ.get("QUEST_BUILD_CACHE", Path.home() / ".cache/quest_to_3d-quest-build"))
build = cache / "source/addons/nightfall-stream/build/android"
source = root / "third_party/nightfall/addons/nightfall-stream"
ninja = (build / "build.ninja").read_text()
out = root / "artifacts/quest/audio-startup"
out.mkdir(parents=True, exist_ok=True)
compiler = cache / "android-sdk/ndk/29.0.14206865/toolchains/llvm/prebuilt/linux-x86_64/bin/aarch64-linux-android29-clang++"
for name in ("miniaudio_backend", "audio_renderer"):
    relative = f"src/audio/{name}.cpp"
    block = ninja.split(f"build CMakeFiles/nightfall-stream.dir/{relative}.o:", 1)[1].split("\nbuild ", 1)[0]
    args = []
    for key in ("DEFINES", "FLAGS", "INCLUDES"):
        line = next(line for line in block.splitlines() if line.startswith(f"  {key} = "))
        args.extend(shlex.split(line.split(" = ", 1)[1]))
    args = [arg.replace(str(cache / "source/addons/nightfall-stream/src"), str(source / "src"))
            .replace(str(cache / "source/addons/nightfall-stream/include"), str(source / "include")) for arg in args]
    command = [str(compiler), *args, "-c", str(source / relative), "-o", str(out / f"{name}-android.o")]
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    (out / f"{name}-android.log").write_text(result.stdout + f"\nexit_code={result.returncode}\n")
    print(name, f"exit_code={result.returncode}", f"warnings={result.stdout.count('warning:')}")
    if result.returncode:
        print(result.stdout)
        raise SystemExit(result.returncode)

"""Compile the authoritative TextureUploader TU using pinned Android flags.

Reads shared build metadata only. All output stays in workspace artifacts.
"""
import os
from pathlib import Path
import shlex
import subprocess

root = Path(__file__).resolve().parents[2]
cache = Path(os.environ.get('QUEST_BUILD_CACHE', Path.home() / '.cache/quest_to_3d-quest-build'))
build = cache / 'source/addons/nightfall-stream/build/android'
source = root / 'third_party/nightfall/addons/nightfall-stream'
text = (build / 'build.ninja').read_text()
block = text.split('build CMakeFiles/nightfall-stream.dir/src/video/texture_uploader.cpp.o:', 1)[1].split('\nbuild ', 1)[0]
args = []
for key in ('DEFINES', 'FLAGS', 'INCLUDES'):
    line = next(line for line in block.splitlines() if line.startswith(f'  {key} = '))
    args.extend(shlex.split(line.split(' = ', 1)[1]))
args = [arg.replace(str(cache / 'source/addons/nightfall-stream/src'), str(source / 'src'))
        .replace(str(cache / 'source/addons/nightfall-stream/include'), str(source / 'include')) for arg in args]
out = root / 'artifacts/quest/gles-retirement'
out.mkdir(parents=True, exist_ok=True)
compiler = cache / 'android-sdk/ndk/29.0.14206865/toolchains/llvm/prebuilt/linux-x86_64/bin/aarch64-linux-android29-clang++'
command = [str(compiler), *args, '-c', str(source / 'src/video/texture_uploader.cpp'), '-o', str(out / 'texture_uploader.o')]
result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
(out / 'android-compile.log').write_text(result.stdout + f'\nexit_code={result.returncode}\n')
print(result.stdout)
raise SystemExit(result.returncode)

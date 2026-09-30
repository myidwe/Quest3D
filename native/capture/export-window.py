"""Export quest2→quest3 without touching earlier candidate patches/locks/wheels."""
import difflib
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "third_party/wc_cuda"
FILES = ("Cargo.toml", "python/wc_cuda/__init__.py", "src/lib.rs")
REVISION = "6f6c6eaed91f36f0e937f1da92cf5cfc35a9bfcc"
if subprocess.check_output(["git", "-C", str(SOURCE), "rev-parse", "HEAD"], text=True).strip() != REVISION:
    raise RuntimeError("Unexpected upstream source; files preserved")
with tempfile.TemporaryDirectory(prefix="quest2-reference-", dir=ROOT / "artifacts/capture") as directory:
    reference = Path(directory)
    if not reference.resolve().is_relative_to((ROOT / "artifacts/capture").resolve()):
        raise RuntimeError("Temporary reference escaped capture workspace")
    for name in FILES:
        target = reference / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(subprocess.check_output(["git", "-C", str(SOURCE), "show", f"HEAD:{name}"]))
    for patch in ("wc_cuda-quest1.patch", "wc_cuda-quest2-hdr.patch"):
        subprocess.run(["git", "apply", "--no-index", str(ROOT / "native/capture" / patch)], cwd=reference, check=True)
    changes = []
    for name in FILES:
        changes.extend(difflib.unified_diff((reference / name).read_text(encoding="utf-8").splitlines(keepends=True),
            (SOURCE / name).read_text(encoding="utf-8").splitlines(keepends=True), fromfile=f"a/{name}", tofile=f"b/{name}"))
    patch = ROOT / "native/capture/wc_cuda-quest3-window.patch"
    patch.write_text("".join(changes), encoding="utf-8", newline="\n")
    subprocess.run(["git", "-C", str(SOURCE), "apply", "--reverse", "--check", str(patch)], check=True)
old = (ROOT / "native/capture/Cargo.hdr.lock").read_text(encoding="utf-8")
assert old.count('version = "0.1.2+quest2"') == 1
new = old.replace('version = "0.1.2+quest2"', 'version = "0.1.2+quest3"')
registry_package = '''name = "windows-capture"
version = "2.0.0-alpha.7"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "6dcd037f0a621bbbb3d6a6b895ddd5b0d691b008b2f9d6980e2d367b16ab5bf6"
'''
assert new.count(registry_package) == 1
new = new.replace(registry_package, 'name = "windows-capture"\nversion = "2.0.0-alpha.7"\n')
current = SOURCE / "Cargo.lock"
if current.exists() and current.read_text(encoding="utf-8") not in (old, new):
    raise RuntimeError("Unexpected checkout Cargo.lock; preserved")
(ROOT / "native/capture/Cargo.window.lock").write_text(new, encoding="utf-8", newline="\n")
current.write_text(new, encoding="utf-8", newline="\n")
print(patch)

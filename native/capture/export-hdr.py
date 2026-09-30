"""Export the candidate patch without editing the selected quest1 patch or wheel."""
from pathlib import Path
import difflib
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "third_party/wc_cuda"
FILES = ("Cargo.toml", "python/wc_cuda/__init__.py", "src/lib.rs")
REVISION = "6f6c6eaed91f36f0e937f1da92cf5cfc35a9bfcc"
if subprocess.check_output(["git", "-C", str(SOURCE), "rev-parse", "HEAD"], text=True).strip() != REVISION:
    raise RuntimeError("Unexpected upstream revision; existing source preserved")

with tempfile.TemporaryDirectory(prefix="quest1-reference-", dir=ROOT / "artifacts/capture") as directory:
    reference = Path(directory)
    if not reference.resolve().is_relative_to((ROOT / "artifacts/capture").resolve()):
        raise RuntimeError("Temporary reference left the capture workspace")
    for name in FILES:
        target = reference / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(subprocess.check_output(["git", "-C", str(SOURCE), "show", f"HEAD:{name}"]))
    subprocess.run(["git", "apply", "--no-index", str(ROOT / "native/capture/wc_cuda-quest1.patch")],
                   cwd=reference, check=True)
    changes = []
    for name in FILES:
        before = (reference / name).read_text(encoding="utf-8").splitlines(keepends=True)
        after = (SOURCE / name).read_text(encoding="utf-8").splitlines(keepends=True)
        changes.extend(difflib.unified_diff(before, after, fromfile=f"a/{name}", tofile=f"b/{name}"))
    patch = ROOT / "native/capture/wc_cuda-quest2-hdr.patch"
    patch.write_text("".join(changes), encoding="utf-8", newline="\n")
    subprocess.run(["git", "-C", str(SOURCE), "apply", "--reverse", "--check", str(patch)], check=True)

# Only the local package version changes; all third-party versions/checksums stay pinned.
lock = (ROOT / "native/capture/Cargo.lock").read_text(encoding="utf-8")
assert lock.count('version = "0.1.2+quest1"') == 1
lock = lock.replace('version = "0.1.2+quest1"', 'version = "0.1.2+quest2"')
(ROOT / "native/capture/Cargo.hdr.lock").write_text(lock, encoding="utf-8", newline="\n")
(SOURCE / "Cargo.lock").write_text(lock, encoding="utf-8", newline="\n")
print(patch)

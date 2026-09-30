"""Read-only artifact/adapter contract check; no capture, install or process changes."""
import hashlib
import json
from pathlib import Path
import zipfile

from quest3d.window_capture import GPUWindowCapture

root = Path(__file__).resolve().parents[2]
folder = root / "artifacts/capture/window-experimental"
wheel = folder / "wc_cuda-0.1.2+quest3-cp310-abi3-win_amd64.whl"
digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
assert digest(wheel) == GPUWindowCapture.expected_wheel_sha256
package = folder / ("package-" + GPUWindowCapture.expected_wheel_sha256[:12])
with zipfile.ZipFile(wheel) as archive:
    for name, expected in GPUWindowCapture._package_hashes.items():
        assert hashlib.sha256(archive.read("wc_cuda/" + name)).hexdigest() == expected
        assert digest(package / "wc_cuda" / name) == expected
for name, expected in {
    "vendor/wheels/wc_cuda-0.1.2+quest1-cp310-abi3-win_amd64.whl":
        "3c16f7a52956b391e4d6e481f64691ff4511b84e0233343eb1df8cb9f0883017",
    "artifacts/capture/hdr-experimental/wc_cuda-0.1.2+quest2-cp310-abi3-win_amd64.whl":
        "0d8bea69406e930dc424694061ed109489cc230b6948721e18ec5838505a4496",
}.items():
    assert digest(root / name) == expected
print(json.dumps(dict(artifact_adapter_match=True, wheel_sha256=digest(wheel), package=str(package),
    native_sha256=GPUWindowCapture._package_hashes["_wc_cuda.pyd"],
    quest1_and_quest2_preserved=True, default_product_promoted=False,
    resource_stability_verified=False), indent=2))

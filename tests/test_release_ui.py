"""Release regressions: missing UI files must not produce a successful install."""
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("release_ui", ROOT / "scripts/release/verify_installed_ui.py")
ui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ui)


@pytest.fixture
def ui_tree(tmp_path):
    assets = tmp_path / "resources/ui"
    entries = {}
    for name in sorted(ui.REQUIRED_ASSETS | {"icons/power.svg"}):
        path = assets / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(("test fixture: " + name).encode())
        entries[name] = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size}
    (assets / "ASSET_MANIFEST.json").write_text(json.dumps({"files": entries}), "utf-8")
    (assets / "brand").mkdir()
    (assets / "brand/quest3d-mark.png").write_bytes(b"brand fixture")
    (tmp_path / "resources/desktop.ico").write_bytes(b"icon fixture")
    (assets / "brand/provenance.json").write_text(json.dumps({"kind": "generated-ui-asset", "file": "quest3d-mark.png", "bytes": 13, "sha256": hashlib.sha256(b"brand fixture").hexdigest(), "desktop_icon": {"file": "resources/desktop.ico", "sha256": hashlib.sha256(b"icon fixture").hexdigest()}}), "utf-8")
    (tmp_path / "resources/desktop").mkdir()
    (tmp_path / "resources/desktop/Main.qml").write_text("import QtQuick\nWindow {}", "utf-8")
    license_root = tmp_path / "scripts/release/licenses"
    license_root.mkdir(parents=True)
    for name in ("Qt-LGPL-3.0-only.txt", "Qt-GPL-3.0-only.txt", "Qt-GPL-2.0-only.txt"):
        (license_root / name).write_text("fixture license", "utf-8")
    packages = []
    for name, checksum in ui.QT_WHEELS.items():
        packages.append(f'[[package]]\nname = "{name}"\nversion = "6.8.3"\nwheels = [{{url = "https://example.invalid/{name}-win_amd64.whl", hash = "sha256:{checksum}"}}]\n')
    (tmp_path / "uv.lock").write_text("\n".join(packages), "utf-8")
    return tmp_path


def test_ui_requires_actual_qml_and_exact_dependency_lock(ui_tree):
    result = ui.verify_resources(ui_tree)
    assert result == {"asset_files": 6, "brand_verified": True, "qml_files": 1, "qt_version": "6.8.3"}


@pytest.mark.parametrize("name", ["resources/desktop/Main.qml", "resources/desktop.ico", "resources/ui/fonts/Pretendard-Medium.otf", "resources/ui/icons/LICENSE-Lucide.txt", "resources/ui/brand/quest3d-mark.png", "scripts/release/licenses/Qt-LGPL-3.0-only.txt"])
def test_missing_app_resource_or_license_fails_install_validation(ui_tree, name):
    (ui_tree / name).unlink()
    with pytest.raises((FileNotFoundError, ValueError)):
        ui.verify_resources(ui_tree)


def test_changed_font_fails_even_when_file_is_present(ui_tree):
    (ui_tree / "resources/ui/fonts/Pretendard-Regular.otf").write_bytes(b"truncated font")
    with pytest.raises(ValueError, match="checksum mismatch"):
        ui.verify_resources(ui_tree)


def test_changed_brand_file_fails(ui_tree):
    (ui_tree / "resources/ui/brand/quest3d-mark.png").write_bytes(b"wrong brand")
    with pytest.raises(ValueError, match="Brand resource checksum"):
        ui.verify_resources(ui_tree)


def test_omitting_attribution_from_manifest_is_not_accepted(ui_tree):
    path = ui_tree / "resources/ui/ASSET_MANIFEST.json"
    data = json.loads(path.read_text("utf-8"))
    del data["files"]["icons/LICENSE-Lucide.txt"]
    path.write_text(json.dumps(data), "utf-8")
    with pytest.raises(ValueError, match="licenses"):
        ui.verify_resources(ui_tree)


@pytest.mark.parametrize("old,new", [("6.8.3", "6.9.0"), (ui.QT_WHEELS["shiboken6"], "0" * 64)])
def test_changed_qt_version_or_download_fails(ui_tree, old, new):
    lock = ui_tree / "uv.lock"
    lock.write_text(lock.read_text("utf-8").replace(old, new), "utf-8")
    with pytest.raises(ValueError, match="Pinned Qt wheel"):
        ui.verify_resources(ui_tree)


def test_manifest_cannot_reference_file_outside_ui_tree(ui_tree):
    path = ui_tree / "resources/ui/ASSET_MANIFEST.json"
    data = json.loads(path.read_text("utf-8"))
    data["files"]["../../uv.lock"] = {"bytes": 1, "sha256": "0" * 64}
    path.write_text(json.dumps(data), "utf-8")
    with pytest.raises(ValueError, match="Unsafe"):
        ui.verify_resources(ui_tree)


def test_source_payload_preserves_qml_fonts_and_attribution(ui_tree):
    bundle_spec = importlib.util.spec_from_file_location("release_ui_bundle", ROOT / "scripts/release/build_bundle.py")
    bundle = importlib.util.module_from_spec(bundle_spec)
    bundle_spec.loader.exec_module(bundle)
    payload = bundle.Payload(ui_tree, ui_tree / "payload")
    payload.tree(ui_tree / "resources", "resources", source_only=True)
    for name in ui.REQUIRED_ASSETS:
        assert (payload.destination / "resources/ui" / name).read_bytes() == (ui_tree / "resources/ui" / name).read_bytes()
    assert (payload.destination / "resources/desktop/Main.qml").exists()


@pytest.mark.parametrize("missing", ui.REQUIRED_CUDA_SOURCES)
def test_missing_packaged_cuda_source_is_rejected(tmp_path, missing):
    shader_root = tmp_path / "src/quest3d/shaders"
    shader_root.mkdir(parents=True)
    for name in ui.REQUIRED_CUDA_SOURCES:
        if name != missing:
            (shader_root / name).write_text("// fixture", "utf-8")
    with pytest.raises(FileNotFoundError):
        ui.verify_runtime_sources(tmp_path)


def test_filtered_payload_keeps_all_real_runtime_cuda_sources(tmp_path):
    bundle_spec = importlib.util.spec_from_file_location("release_cuda_bundle", ROOT / "scripts/release/build_bundle.py")
    bundle = importlib.util.module_from_spec(bundle_spec)
    bundle_spec.loader.exec_module(bundle)
    payload = bundle.Payload(ROOT, tmp_path / "payload")
    payload.tree(ROOT / "src/quest3d", "src/quest3d", source_only=True)
    assert ui.verify_runtime_sources(payload.destination) == ui.verify_runtime_sources(ROOT)

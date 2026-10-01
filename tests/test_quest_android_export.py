"""Unsigned Android export boundaries, without native tools or device access."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import zipfile
import io

import pytest

RELEASE = Path(__file__).resolve().parents[1] / "scripts/release"
sys.path.insert(0, str(RELEASE))
spec = importlib.util.spec_from_file_location("quest_android_export", RELEASE / "prepare_quest_android_export.py")
export = importlib.util.module_from_spec(spec)
spec.loader.exec_module(export)
sys.path.remove(str(RELEASE))

PRESET = '''[preset.0]
name="NightfallDev"
export_path="./old.apk"
[preset.0.options]
version/code=1
version/name=""
package/unique_name="app.questto3d.client.debug"
package/name="Quest 3D Desktop (Dev)"
package/signed=true
graphics/opengl_debug=true
'''


def sha(data):
    return hashlib.sha256(data).hexdigest()


def inputs(tmp_path, monkeypatch):
    source = tmp_path / "source"
    files = {
        "project/export_presets.cfg": PRESET.encode(),
        "project/" + export.source_tool.XR_DESCRIPTOR: b"source descriptor\n",
        "project/android/src/main/java/com/godot/game/GodotApp.java": b"// own Java input\n",
        "project/android/src/main/java/com/godot/game/CodecCapabilityDiagnostics.java": b"// diagnostics input\n",
    }
    for name, data in files.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (source / "source-manifest.json").write_text(json.dumps({"files": {name: sha(data) for name, data in files.items()}}), "utf-8")
    native = tmp_path / "native"
    outputs = {}
    for name in (
        "project/addons/nightfall-stream/bin/android/libnightfall-stream.android.template_release.arm64.so",
        "project/extensions/nightfall-xr/bin/android/libnightfall-xr.android.template_release.arm64.so",
    ):
        path = native / name
        path.parent.mkdir(parents=True, exist_ok=True)
        data = ("native fixture " + name).encode()
        path.write_bytes(data)
        outputs[name] = {"sha256": sha(data), "bytes": len(data)}
    (native / "native-build-summary.json").write_text(json.dumps({"native_build_verified": True, "exit_code": 0, "outputs": outputs}), "utf-8")
    vendor = tmp_path / "vendor.zip"
    with zipfile.ZipFile(vendor, "w") as archive:
        for name in export.VENDOR_MEMBERS:
            data = b"pinned vendor fixture"
            if name.endswith(".aar"):
                fixture = io.BytesIO()
                with zipfile.ZipFile(fixture, "w") as aar:
                    aar.writestr("jni/arm64-v8a/libgodotopenxrvendors.so", b"pinned vendor fixture")
                    aar.writestr("classes.jar", b"retained Java fixture")
                data = fixture.getvalue()
            archive.writestr("asset/" + name, data)
    monkeypatch.setattr(export, "VENDOR_SHA", export.source_tool.sha(vendor))
    template = tmp_path / "template.zip"
    with zipfile.ZipFile(template, "w") as archive:
        archive.writestr("libs/release/godot-lib.template_release.aar", b"pinned template fixture")
        archive.writestr("build.gradle", b"// gradle input\n")
    return source, native, vendor, template


def test_export_copies_only_bound_own_native_and_preserves_inputs(tmp_path, monkeypatch):
    args = inputs(tmp_path, monkeypatch)
    source, native, vendor, template = args
    original = {p: p.read_bytes() for parent in (source, native) for p in parent.rglob("*") if p.is_file()}
    output = tmp_path / "export"
    result = export.prepare(*args, output)
    assert result["own_native"] == 2 and not result["public_release_ready"]
    record = json.loads((output / "android-export-preparation.json").read_text())
    assert not record["signed"] and not record["vendor_built_from_source"]
    assert not record["godot_java_aar_rebuilt_from_source"]
    assert record["package"] == "app.questto3d.client"
    preset = (output / "project/export_presets.cfg").read_text()
    assert "package/signed=false" in preset and 'package/unique_name="app.questto3d.client"' in preset
    assert "old.apk" not in preset
    assert 'strictly "1.1.54"' in (output / "project/android/build/build.gradle").read_text()
    assert (output / "project/android/.build_version").read_bytes() == b"4.7.stable\n"
    assert (output / "project" / export.VENDOR_MEMBERS[-1]).is_file()
    assert all(p.read_bytes() == data for p, data in original.items())
    before = (output / "android-export-preparation.json").read_bytes()
    with pytest.raises(FileExistsError):
        export.prepare(*args, output)
    assert (output / "android-export-preparation.json").read_bytes() == before


def test_template_stamp_conflict_preserves_existing_input(tmp_path, monkeypatch):
    _, _, vendor, _ = inputs(tmp_path, monkeypatch)
    output = tmp_path / "export"
    stamp = output / "project/android/.build_version"
    stamp.parent.mkdir(parents=True)
    stamp.write_bytes(b"4.6.stable\n")
    with pytest.raises(ValueError, match="stamp differs"):
        export.complete_template_installation(output, vendor)
    assert stamp.read_bytes() == b"4.6.stable\n"
    assert not (output / "template-installation-stamp.json").exists()


def test_native_tamper_cannot_be_exported(tmp_path, monkeypatch):
    args = inputs(tmp_path, monkeypatch)
    source, native, _, _ = args
    next(native.glob("project/**/bin/android/*.so")).write_bytes(b"changed payload")
    output = tmp_path / "export"
    with pytest.raises(ValueError, match="native changed"):
        export.prepare(*args, output)
    assert not (output / "android-export-preparation.json").exists()


def test_public_vendor_native_and_preview_version_are_bound(tmp_path, monkeypatch):
    args = inputs(tmp_path, monkeypatch)
    public = tmp_path / "public-vendor"
    public.mkdir()
    native = public / "rebuilt.so"
    native.write_bytes(b"fresh public-header vendor")
    record = {"exit_code": 0, "vendor_built_from_source": True,
              "meta_preview_headers_selected": False,
              "native": {"path": "rebuilt.so", "sha256": sha(native.read_bytes())}}
    (public / "vendor-build-summary.json").write_text(json.dumps(record))
    output = tmp_path / "export"
    export.prepare(*args, output, public, "0.1.0-preview")
    proof = json.loads((output / "android-export-preparation.json").read_text())
    assert proof["vendor_built_from_source"] is True
    assert proof["public_vendor_binding"]["native_sha256"] == sha(native.read_bytes())
    assert (output / "project" / export.VENDOR_MEMBERS[0]).read_bytes() == native.read_bytes()
    with zipfile.ZipFile(output / "project" / export.VENDOR_MEMBERS[1]) as derived:
        assert derived.read("jni/arm64-v8a/libgodotopenxrvendors.so") == native.read_bytes()
        assert derived.read("classes.jar") == b"retained Java fixture"
    with zipfile.ZipFile(output / "official-vendor-android-release.aar") as preserved:
        assert preserved.read("jni/arm64-v8a/libgodotopenxrvendors.so") != native.read_bytes()
    assert proof["public_vendor_binding"]["aar_derivation"]["native_sha256"] == sha(native.read_bytes())
    assert 'version/name="0.1.0-preview"' in (output / "project/export_presets.cfg").read_text()


def test_tampered_public_vendor_cannot_create_success_receipt(tmp_path, monkeypatch):
    args = inputs(tmp_path, monkeypatch)
    public = tmp_path / "public-vendor"
    public.mkdir()
    native = public / "rebuilt.so"
    native.write_bytes(b"changed vendor")
    (public / "vendor-build-summary.json").write_text(json.dumps({
        "exit_code": 0, "vendor_built_from_source": True, "meta_preview_headers_selected": False,
        "native": {"path": "rebuilt.so", "sha256": sha(b"original vendor")}}))
    output = tmp_path / "export"
    with pytest.raises(ValueError, match="vendor input changed"):
        export.prepare(*args, output, public, "0.1.0-preview")
    assert not (output / "android-export-preparation.json").exists()


def test_unverified_native_and_unpinned_vendor_fail_before_output(tmp_path, monkeypatch):
    args = inputs(tmp_path, monkeypatch)
    _, native, vendor, _ = args
    report = native / "native-build-summary.json"
    original = report.read_bytes()
    report.write_text(json.dumps({"native_build_verified": False, "exit_code": 1}))
    output = tmp_path / "export"
    with pytest.raises(ValueError, match="must pass"):
        export.prepare(*args, output)
    assert not output.exists()
    report.write_bytes(original)
    vendor.write_bytes(b"changed vendor")
    with pytest.raises(ValueError, match="vendor ZIP changed"):
        export.prepare(*args, output)
    assert not output.exists()


@pytest.mark.parametrize("member", ["../escape", "/absolute", "C:/drive", "NUL.txt", "case", "symlink"])
def test_unsafe_template_cannot_write_outside_candidate(tmp_path, member):
    template = tmp_path / "template.zip"
    with zipfile.ZipFile(template, "w") as archive:
        if member == "case":
            archive.writestr("file.txt", b"one")
            archive.writestr("FILE.txt", b"two")
        elif member == "symlink":
            entry = zipfile.ZipInfo("link")
            entry.create_system = 3
            entry.external_attr = 0o120777 << 16
            archive.writestr(entry, "../escape")
        else:
            archive.writestr(member, b"payload")
    output = tmp_path / "new"
    with pytest.raises(ValueError):
        export.extract_template(template, output)
    assert not output.exists()

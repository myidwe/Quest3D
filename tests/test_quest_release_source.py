"""Release source boundaries and signing lifecycle, without hardware or keys."""
import importlib.util
import json
from pathlib import Path
import sys
import zipfile
import shutil

import pytest

RELEASE = Path(__file__).resolve().parents[1] / "scripts/release"


def load(name):
    spec = importlib.util.spec_from_file_location(name, RELEASE / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


source = load("prepare_quest_source")
signing = load("sign_quest_release")


@pytest.mark.parametrize("name", ["../file.gd", "x/../../file.gd", "C:/file.gd", "file.gd:stream", "x\\file.gd", "CON.txt", "app_state.cfg", "Signing/key.jks", "src/main.gd."])
def test_source_rejects_private_and_unsafe_names(name):
    with pytest.raises(ValueError):
        source.safe_name(name)


def test_copy_preserves_product_settings_but_excludes_private_files(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    export = '[preset.0.options]\npackage/unique_name="app.questto3d.client.debug"\nkeystore/debug="private.keystore"\nkeystore/debug_password="fixture-secret"\n'
    (project / "export_presets.cfg").write_text(export)
    (project / "app_state.cfg").write_text("pairing fixture")
    (project / "release.jks").write_bytes(b"private fixture")
    (project / "main.gd").write_text("extends Node\n")
    (project / "bin").mkdir()
    (project / "bin/cache.json").write_text("{}")
    out = tmp_path / "public"
    out.mkdir()
    count, sanitized = source.copy_project(project, out)
    assert count == 2 and sanitized == ["export_presets.cfg"]
    assert set(file.name for file in out.iterdir()) == {"main.gd", "export_presets.cfg"}
    public = (out / "export_presets.cfg").read_text()
    assert "fixture-secret" not in public and "private.keystore" not in public
    assert 'package/unique_name="app.questto3d.client.debug"' in public
    assert 'keystore/debug=""' in public


def test_copy_retains_extensionless_vendor_license_notices(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    for name in ("LICENSE-SDK", "LICENSE-LOADER", "NOTICE.thirdparty", "LICENCE"):
        (project / name).write_text("license fixture")
    out = tmp_path / "public"
    out.mkdir()
    count, _ = source.copy_project(project, out)
    assert count == 4
    assert {p.name for p in out.iterdir()} == {"LICENSE-SDK", "LICENSE-LOADER", "NOTICE.thirdparty", "LICENCE"}


def test_manifest_detects_tampering_missing_and_extra_nested_manifest(tmp_path):
    (tmp_path / "main.gd").write_text("extends Node\n")
    manifest = {"files": {"main.gd": source.sha(tmp_path / "main.gd")}, "source_complete": False}
    (tmp_path / "source-manifest.json").write_text(json.dumps(manifest))
    assert source.verify_manifest(tmp_path) == manifest
    extra = tmp_path / "nested"
    extra.mkdir()
    (extra / "source-manifest.json").write_text("unreviewed file")
    with pytest.raises(ValueError, match="hash mismatch"):
        source.verify_manifest(tmp_path)
    (extra / "source-manifest.json").unlink()
    (tmp_path / "main.gd").write_text("changed")
    with pytest.raises(ValueError, match="hash mismatch"):
        source.verify_manifest(tmp_path)
    (tmp_path / "main.gd").unlink()
    with pytest.raises(ValueError, match="membership"):
        source.verify_manifest(tmp_path)


def test_apk_native_extra_abi_is_rejected_even_with_same_bytes(tmp_path, monkeypatch):
    apk = tmp_path / "fixture.apk"
    raw = b"native fixture"
    monkeypatch.setattr(source, "NATIVE_SHA", {"one.so": source.hashlib.sha256(raw).hexdigest()})
    with zipfile.ZipFile(apk, "w") as archive:
        archive.writestr("lib/arm64-v8a/one.so", raw)
        archive.writestr("lib/x86_64/one.so", raw)
    monkeypatch.setattr(source, "APK_SHA", source.sha(apk))
    with pytest.raises(ValueError, match="paths or ABI"):
        source.verify_apk(apk)


def test_preparation_repairs_only_exact_mdns_delta_without_touching_original(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    project = root / "artifacts/quest/level-settings-20260930/project"
    stream = project / "addons/nightfall-stream"
    (stream / "src/network").mkdir(parents=True)
    (project / "main.gd").write_text("extends Node\n")
    (stream / "src/network/mdns_browser.cpp").write_text("old source\n")
    original = source.sha(stream / "src/network/mdns_browser.cpp")
    build = project.parent / "build"
    build.mkdir()
    with zipfile.ZipFile(build / "candidate-signed.apk", "w") as archive:
        archive.writestr("assets/" + source.XR_DESCRIPTOR, XR_DESCRIPTOR_TEXT)
    monkeypatch.setattr(source, "APK_SHA", source.sha(build / "candidate-signed.apk"))
    monkeypatch.setattr(source, "verify_apk", lambda path: source.NATIVE_SHA)
    (build / "export-sources.json").write_text(json.dumps({"main.gd": source.sha(project / "main.gd")}))
    baseline = root / "artifacts/quest/quest3-scan-20260914/native"
    baseline.mkdir(parents=True)
    (baseline / "source-baseline.json").write_text(json.dumps({"src/network/mdns_browser.cpp": {"published_sha256": original}}))
    hashes = {}
    for name in source.MDNS_SHA:
        file = root / "third_party/nightfall/addons/nightfall-stream" / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("exact reviewed source for " + name + "\n")
        hashes[name] = source.sha(file)
    monkeypatch.setattr(source, "MDNS_SHA", hashes)
    scripts = root / "scripts/quest"
    scripts.mkdir(parents=True)
    for name in ("versions.lock.json", "downloads.lock.json", "env.sh", "setup_sdk.sh", "prepare.sh", "bootstrap.py", "build_native.sh", "build_xr.sh", "build_stream.sh", "build_apk.sh"):
        (scripts / name).write_text("preserved build input\n")
    out = root / "artifacts/publication/source-fixture"
    result = source.prepare(root, out)
    assert result["pass"] and result["apk_unchanged"] and not result["source_complete"]
    assert source.sha(stream / "src/network/mdns_browser.cpp") == original
    public = out / "public-source"
    for name, digest in hashes.items():
        assert source.sha(public / "project/addons/nightfall-stream" / name) == digest
    manifest = source.verify_manifest(public)
    assert (public / "project" / source.XR_DESCRIPTOR).read_text() == XR_DESCRIPTOR_TEXT
    assert manifest["apk_metadata"]["signing_kind"] == "development"
    assert not manifest["clean_build_verified"] and not manifest["public_release_ready"]
    with pytest.raises(ValueError, match="preserved"):
        source.prepare(root, out)


XR_DESCRIPTOR_TEXT = '''[configuration]
entry_symbol = "nightfall_xr_library_init"
[libraries]
android.arm64.single.debug = "./android/libnightfall-xr.android.template_debug.arm64.so"
android.arm64.single.release = "./android/libnightfall-xr.android.template_release.arm64.so"
'''


def test_only_source_descriptor_is_permitted_under_xr_bin(tmp_path):
    project = tmp_path / "project"
    directory = project / "extensions/nightfall-xr/bin"
    directory.mkdir(parents=True)
    (directory / "nightfall-xr.gdextension").write_text(XR_DESCRIPTOR_TEXT)
    (directory / "nightfall-xr.gdextension.uid").write_text("uid://fixture")
    (directory / "runtime.json").write_text("{}")
    (directory / "android").mkdir()
    (directory / "android/unsafe.gdextension").write_text("unreviewed")
    (directory / "android/native.so").write_bytes(b"binary fixture")
    output = tmp_path / "output"
    count, _ = source.copy_project(project, output)
    assert count == 2
    assert sorted(p.relative_to(output).as_posix() for p in output.rglob("*") if p.is_file()) == sorted(source.SOURCE_DIRECTORY_EXCEPTIONS)
    assert source.safe_name("project/" + source.XR_DESCRIPTOR).endswith("nightfall-xr.gdextension")
    with pytest.raises(ValueError):
        source.safe_name("extensions/nightfall-xr/bin/other.gdextension")


def test_pinned_apk_descriptor_restoration_preserves_apk_and_rejects_conflict(tmp_path):
    apk = tmp_path / "input.apk"
    with zipfile.ZipFile(apk, "w") as archive:
        archive.writestr("assets/" + source.XR_DESCRIPTOR, XR_DESCRIPTOR_TEXT)
    before = apk.read_bytes()
    output = tmp_path / "source"
    result = source.restore_xr_descriptor(apk, output)
    assert result["restored"] and not result["uid_reconstructed"]
    assert apk.read_bytes() == before
    assert not source.restore_xr_descriptor(apk, output)["restored"]
    (output / "project" / source.XR_DESCRIPTOR).write_text("conflicting descriptor")
    with pytest.raises(ValueError, match="differs"):
        source.restore_xr_descriptor(apk, output)
    assert apk.read_bytes() == before


@pytest.mark.parametrize("payload", [None, "invalid descriptor"])
def test_absent_or_invalid_apk_descriptor_fails_closed(tmp_path, payload):
    apk = tmp_path / "input.apk"
    with zipfile.ZipFile(apk, "w") as archive:
        if payload is not None:
            archive.writestr("assets/" + source.XR_DESCRIPTOR, payload)
    with pytest.raises(ValueError):
        source.restore_xr_descriptor(apk, tmp_path / "output")


def test_native_evidence_does_not_accept_an_unmatched_vendor_claim(tmp_path):
    evidence = tmp_path / "evidence"
    (evidence / "vendor-input-check").mkdir(parents=True)
    (evidence / "native-input-check").mkdir()
    (evidence / "vendor-input-check/verification.json").write_text(json.dumps({"stripped_matches_apk": False}))
    (evidence / "native-input-check/verification.json").write_text("{}")
    with pytest.raises(ValueError, match="Vendor evidence"):
        source.attach_native_evidence(tmp_path, evidence, tmp_path / "public", {})


def public_spec():
    return {"unsigned_apk_sha256": "1" * 64, "source_manifest_sha256": "2" * 64,
            "apk_metadata": {"package": signing.PUBLIC_PACKAGE, "version_code": 1,
                             "version_name": "0.1.0-preview", "certificate_sha256": "3" * 64, "signing_kind": "release"}}


@pytest.mark.parametrize("field,value", [("package", "app.questto3d.client.debug"), ("version_code", True),
                                         ("version_code", 0), ("signing_kind", "development"),
                                         ("certificate_sha256", source.CERTIFICATE_SHA)])
def test_public_signing_refuses_debug_or_invalid_identity(field, value):
    spec = public_spec()
    spec["apk_metadata"][field] = value
    with pytest.raises(ValueError):
        signing.specification(spec)


def test_actual_badging_debuggable_is_recognized():
    raw = "package: name='app.questto3d.client' versionCode='2' versionName='0.1.1'\napplication-debuggable\n"
    assert signing.apk_badging(raw) == {"package": signing.PUBLIC_PACKAGE, "version_code": 2,
                                       "version_name": "0.1.1", "debuggable": True}


def test_audit_reports_unresolved_source_gate_and_refuses_signing(tmp_path, monkeypatch):
    apk = tmp_path / "public-unsigned.apk"
    apk.write_bytes(b"new public build fixture")
    candidate = tmp_path / "source"
    candidate.mkdir()
    manifest = {"files": {}, "apk_sha256": source.sha(apk), "source_complete": False,
                "clean_build_verified": False, "dependency_notices_verified": False}
    (candidate / "source-manifest.json").write_text(json.dumps(manifest))
    spec = public_spec()
    spec.update(unsigned_apk_sha256=source.sha(apk), source_manifest_sha256=source.sha(candidate / "source-manifest.json"))
    spec_path = tmp_path / "build-spec.json"
    spec_path.write_text(json.dumps(spec))
    monkeypatch.setattr(signing, "tool", lambda command: b"package: name='app.questto3d.client' versionCode='1' versionName='0.1.0-preview'\n")
    report = signing.audit(spec_path, apk, candidate, tmp_path / "aapt2")
    assert not report["ready_to_sign"] and not report["signed"] and not report["published"]
    assert set(report["remaining_gates"]) == {"source_complete", "clean_build_verified", "dependency_notices_verified"}
    monkeypatch.setattr(signing.getpass, "getpass", lambda prompt: pytest.fail("Must not request a password before source gates pass"))
    with pytest.raises(ValueError, match="must pass"):
        signing.sign(report, apk, tmp_path / "out", tmp_path / "java", tmp_path / "signer.jar", tmp_path / "zipalign", tmp_path / "key.jks", "release")
    assert not (tmp_path / "out").exists()


def test_signing_passes_passwords_only_through_stdin_and_records_public_hash(tmp_path, monkeypatch):
    report = {"ready_to_sign": True, "apk_metadata": {"certificate_sha256": "3" * 64}, "signed": False, "published": False}
    apk = tmp_path / "unsigned.apk"
    with zipfile.ZipFile(apk, "w") as archive:
        archive.writestr("assets/main.gdc", b"audited compiled product fixture")
    report["unsigned_apk_sha256"] = source.sha(apk)
    paths = {name: tmp_path / name for name in ("java", "signer.jar", "zipalign", "key.jks")}
    for path in paths.values():
        path.write_bytes(b"fixture")
    argv_seen, stdin_seen = [], []
    secret = "unit-test-password"
    monkeypatch.setattr(signing.getpass, "getpass", lambda prompt: secret)

    class Process:
        returncode = 0

        def __init__(self, argv, **kwargs):
            argv_seen.extend(argv)
            assert secret not in str(argv) and "env" not in kwargs
            self.argv = argv

        def communicate(self, raw):
            stdin_seen.append(raw)
            destination = Path(self.argv[self.argv.index("--out") + 1])
            shutil.copyfile(self.argv[-1], destination)
            with zipfile.ZipFile(destination, "a") as archive:
                archive.writestr("META-INF/FIXTURE.RSA", b"signature metadata fixture")
            return b"", b""

    monkeypatch.setattr(signing.subprocess, "Popen", Process)
    monkeypatch.setattr(signing.subprocess, "run", lambda *args, **kwargs: type("Unsigned", (), {"returncode": 1})())

    def fake_tool(argv):
        assert secret not in str(argv)
        if "--print-certs" in argv:
            return ("Signer #1 certificate SHA-256 digest: " + "3" * 64 + "\n").encode()
        if "-c" not in argv:
            shutil.copyfile(argv[-2], argv[-1])
        return b""

    monkeypatch.setattr(signing, "tool", fake_tool)
    out = tmp_path / "signed"
    result = signing.sign(report, apk, out, paths["java"], paths["signer.jar"], paths["zipalign"], paths["key.jks"], "release")
    assert stdin_seen == [(secret + "\n" + secret + "\n").encode()]
    assert "stdin" in argv_seen and secret not in json.dumps(result)
    assert result["signed"] and not result["published"]
    assert result["product_payload_unchanged"]
    with zipfile.ZipFile(out / "Quest3D-Quest.apk") as archive:
        assert archive.read("assets/main.gdc") == b"audited compiled product fixture"
    assert secret not in (out / "signing-result.json").read_text()


def test_signed_source_binding_preserves_unsigned_manifest_and_requires_same_source(tmp_path):
    original = tmp_path / "unsigned-source"
    original.mkdir()
    (original / "main.gd").write_text("extends Node\n")
    manifest = {"apk_sha256": "1" * 64, "files": {"main.gd": source.sha(original / "main.gd")}}
    (original / "source-manifest.json").write_text(json.dumps(manifest))
    before = (original / "source-manifest.json").read_bytes()
    report = {"signed": True, "source_manifest_sha256": source.sha(original / "source-manifest.json"),
              "unsigned_apk_sha256": "1" * 64, "apk_sha256": "2" * 64, "apk_metadata": public_spec()["apk_metadata"]}
    out = tmp_path / "signed-source"
    result = signing.bind_signed_source(original, out, report)
    assert result["original_source_preserved"] and (original / "source-manifest.json").read_bytes() == before
    signed = source.verify_manifest(out)
    assert signed["apk_sha256"] == "2" * 64 and signed["unsigned_apk_sha256"] == "1" * 64
    assert signed["apk_metadata"]["signing_kind"] == "release"
    (original / "main.gd").write_text("unexpected later mutation")
    with pytest.raises(ValueError, match="hash mismatch"):
        signing.bind_signed_source(original, tmp_path / "second", report)


def test_signing_refuses_apk_changed_since_audit_before_password_or_output(tmp_path, monkeypatch):
    apk = tmp_path / "changed.apk"
    apk.write_bytes(b"mutated after audit")
    paths = [tmp_path / name for name in ("java", "signer.jar", "zipalign", "key.jks")]
    for path in paths:
        path.write_bytes(b"fixture")
    report = {"ready_to_sign": True, "unsigned_apk_sha256": "1" * 64}
    monkeypatch.setattr(signing.getpass, "getpass", lambda prompt: pytest.fail("Must not ask for key password after input mutation"))
    with pytest.raises(ValueError, match="changed after"):
        signing.sign(report, apk, tmp_path / "out", paths[0], paths[1], paths[2], paths[3], "release")
    assert not (tmp_path / "out").exists()


def test_payload_comparison_excludes_only_signature_metadata(tmp_path):
    original = tmp_path / "unsigned.apk"
    with zipfile.ZipFile(original, "w") as archive:
        archive.writestr("assets/main.gdc", b"product")
        archive.writestr("META-INF/library.kotlin_module", b"module")
    signed = tmp_path / "signed.apk"
    shutil.copyfile(original, signed)
    with zipfile.ZipFile(signed, "a") as archive:
        archive.writestr("META-INF/RELEASE.SF", b"signature")
        archive.writestr("META-INF/RELEASE.RSA", b"certificate")
    assert signing.apk_payload(original) == signing.apk_payload(signed)
    mutated = tmp_path / "mutated.apk"
    with zipfile.ZipFile(mutated, "w") as archive:
        archive.writestr("assets/main.gdc", b"changed product")
        archive.writestr("META-INF/library.kotlin_module", b"module")
    assert signing.apk_payload(original) != signing.apk_payload(mutated)

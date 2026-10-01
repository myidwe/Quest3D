"""Distribution boundaries: a private working tree must never become a blanket archive."""
import importlib.util
import json
from pathlib import Path
import zipfile

import pytest

spec = importlib.util.spec_from_file_location("release_bundle", Path(__file__).parents[1] / "scripts/release/build_bundle.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


@pytest.mark.parametrize("name", ["../credentials.json", "/absolute.py", "C:/file.py", "src\\file.py", "src/../file.py", "src//file.py", "CON.txt", "src/LPT1", "src/x. /file.py", "src/x./file.py"])
def test_archive_path_rejects_windows_escape_and_alias(name):
    with pytest.raises(ValueError):
        release.relative_name(name)


@pytest.mark.parametrize("name", ["config/desktop.json", "config/credentials.json", "data/app_state.cfg", "key.pem", "debug.keystore", "signing/key.txt", "Signing/key.txt", "foo/__pycache__/x.pyc", "captures/a.png", "video.mp4", "recording.wav", "Desktop.lnk", "sunshine.conf"])
def test_public_payload_rejects_private_state(name, tmp_path):
    payload = release.Payload(tmp_path, tmp_path / "payload")
    with pytest.raises(ValueError):
        payload.put(name, b"private")


def test_manifest_detects_tamper_and_unexpected_file(tmp_path):
    payload = release.Payload(tmp_path, tmp_path / "payload")
    payload.put("src/test.py", b"print('hello')")
    payload.manifest(release="test", metadata={})
    assert len(release.verify_directory(payload.destination)["files"]) == 1
    (payload.destination / "src/test.py").write_bytes(b"changed")
    with pytest.raises(ValueError, match="hash mismatch"):
        release.verify_directory(payload.destination)
    (payload.destination / "src/test.py").write_bytes(b"print('hello')")
    (payload.destination / "extra.txt").write_bytes(b"new")
    with pytest.raises(ValueError, match="Unexpected"):
        release.verify_directory(payload.destination)


def test_archive_round_trip_and_no_existing_overwrite(tmp_path):
    payload = release.Payload(tmp_path, tmp_path / "payload")
    payload.put("src/a.py", b"x=1\n")
    payload.manifest(release="test", metadata={"published": False})
    archive = tmp_path / "payload.zip"
    release.zip_payload(payload.destination, archive)
    release.safe_extract(archive, tmp_path / "extracted")
    assert release.verify_directory(tmp_path / "extracted")["metadata"]["published"] is False
    with pytest.raises(FileExistsError):
        release.safe_extract(archive, tmp_path / "extracted")


def test_case_collisions_rejected(tmp_path):
    payload = release.Payload(tmp_path, tmp_path / "payload")
    payload.put("src/a.py", b"a")
    with pytest.raises(ValueError, match="duplicate"):
        payload.put("src/A.py", b"b")


def test_zip_traversal_fails_before_creating_destination(tmp_path):
    archive = tmp_path / "malicious.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("../outside.txt", "bad")
    with pytest.raises(ValueError):
        release.safe_extract(archive, tmp_path / "extracted")
    assert not (tmp_path / "extracted").exists()
    assert not (tmp_path / "outside.txt").exists()


def test_source_tree_omits_cache_and_secrets(tmp_path):
    source = tmp_path / "input"
    (source / "__pycache__").mkdir(parents=True)
    (source / "__pycache__/code.py").write_text("no")
    (source / "code.py").write_text("yes")
    (source / "credentials.json").write_text("secret")
    (source / "video.mp4").write_bytes(b"private")
    payload = release.Payload(tmp_path, tmp_path / "payload")
    payload.tree(source, "src", source_only=True)
    assert sorted(p.relative_to(payload.destination).as_posix() for p in payload.destination.rglob("*") if p.is_file()) == ["src/code.py"]


def test_checksum_pin_checked_before_copy(tmp_path):
    file = tmp_path / "file.txt"
    file.write_text("unexpected")
    payload = release.Payload(tmp_path, tmp_path / "payload")
    with pytest.raises(ValueError, match="Pinned hash"):
        payload.copy(file, "file.txt", "0" * 64)
    assert not (payload.destination / "file.txt").exists()


def test_unknown_runtime_asset_requires_review_instead_of_silent_omission(tmp_path):
    (tmp_path / "model-lookup.blob").write_bytes(b"runtime data")
    with pytest.raises(ValueError, match="Unclassified runtime asset"):
        release.validate_runtime_tree(tmp_path)


def test_reviewed_source_can_be_given_as_relative_path(tmp_path, monkeypatch):
    source = tmp_path / "reviewed.txt"
    source.write_text("public source")
    monkeypatch.chdir(tmp_path)
    payload = release.Payload(tmp_path, tmp_path / "payload")
    payload.copy(Path("reviewed.txt"), "reviewed.txt")
    assert (payload.destination / "reviewed.txt").read_text() == "public source"


def test_local_device_install_helpers_are_excluded_but_product_build_scripts_remain(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ("install-quest-precision-reviewed.py", "configure-quest3-network-reviewed.ps1", "desktop-qt-preview.py", "snapshot-ui-baseline.py", "sync_reviewed_delta.py", "build_native.sh", "probe.gdextension"):
        (scripts / name).write_text("fixture", "utf-8")
    payload = release.Payload(tmp_path, tmp_path / "payload")
    payload.tree(scripts, "scripts", source_only=True, skip_files=release.LOCAL_ONLY_SCRIPTS)
    assert {path.name for path in (payload.destination / "scripts").iterdir()} == {"sync_reviewed_delta.py", "build_native.sh", "probe.gdextension"}


def quest_source(tmp_path, apk_sha):
    source = tmp_path / "quest"
    source.mkdir()
    (source / "main.gd").write_text("# fixture source\n")
    manifest = {"apk_sha256": apk_sha, "files": {"main.gd": release.digest(source / "main.gd")}}
    (source / "source-manifest.json").write_text(json.dumps(manifest))
    return source


def test_quest_source_must_match_the_exact_apk_and_all_listed_files(tmp_path):
    source = quest_source(tmp_path, "1" * 64)
    release.validate_quest_source(source, "1" * 64)
    with pytest.raises(ValueError, match="not bound"):
        release.validate_quest_source(source, "2" * 64)
    (source / "main.gd").write_text("changed after review")
    with pytest.raises(ValueError, match="hash mismatch"):
        release.validate_quest_source(source, "1" * 64)


def test_apk_source_mismatch_fails_before_creating_any_release(tmp_path):
    source = quest_source(tmp_path, "1" * 64)
    apk = tmp_path / "candidate.apk"
    apk.write_bytes(b"fixture apk")
    output = tmp_path / "release"
    with pytest.raises(ValueError, match="not bound"):
        release.main(["--output", str(output), "--sources", "--quest-source", str(source),
                      "--quest-apk", str(apk), "--quest-sha256", release.digest(apk)])
    assert not output.exists()


def test_quest_install_metadata_requires_exact_reviewed_package_version_and_identity():
    source = {"apk_sha256": "1" * 64, "apk_metadata": {
        "package": "app.questto3d.client.debug", "version_code": 1, "version_name": "1.0.0",
        "certificate_sha256": "2" * 64, "signing_kind": "development"}}
    value = release.quest_install_metadata(source, "1" * 64)
    assert value["package"] == "app.questto3d.client.debug" and value["version_code"] == 1
    assert value["preserve_data"] is True and value["signing_kind"] == "development"
    with pytest.raises(ValueError, match="not bound"):
        release.quest_install_metadata(source, "3" * 64)
    source["apk_metadata"]["version_code"] = True
    with pytest.raises(ValueError, match="version"):
        release.quest_install_metadata(source, "1" * 64)


def test_source_bundle_selection_skips_test_runtime_cache_and_local_network_plan(tmp_path):
    source = tmp_path / "input"
    (source / "tests/.cache/fixture").mkdir(parents=True)
    (source / "tests/.cache/fixture/private.py").write_text("local execution state")
    (source / "native").mkdir()
    (source / "native/dev-firewall.ps1").write_text("personal LAN defaults")
    (source / "native/configure-installed-network.ps1").write_text("portable product network setup")
    payload = release.Payload(tmp_path, tmp_path / "payload")
    payload.tree(source, "sources", source_only=True, skip_dirs=release.SKIP_DIRS,
                 skip_files=release.LOCAL_ONLY_DIAGNOSTICS)
    assert {p.relative_to(payload.destination).as_posix() for p in payload.destination.rglob("*") if p.is_file()} == {"sources/native/configure-installed-network.ps1"}


def test_source_bundle_omits_local_quest_signing_and_runtime_helpers(tmp_path):
    source = tmp_path / "scripts"
    source.mkdir()
    local_names = (
        "quest-compact-verify-runtime.sh", "quest-display-verify-runtime.sh",
        "quest-level-verify-runtime.sh", "quest-pointer-verify-runtime.sh",
        "quest3-verify-runtime.sh", "quest3-sign.sh",
    )
    for name in local_names:
        (source / name).write_text("developer-only execution and signing path")
    (source / "sign_quest_release.py").write_text("public explicit external key selection")
    (source / "build_native.sh").write_text("portable build instructions")
    payload = release.Payload(tmp_path, tmp_path / "payload")
    payload.tree(source, "scripts", source_only=True, skip_files=release.LOCAL_ONLY_SCRIPTS)
    assert {p.name for p in (payload.destination / "scripts").iterdir()} == {"sign_quest_release.py", "build_native.sh"}


def test_private_machine_audio_diagnostic_is_not_in_a_public_source_tree(tmp_path):
    source = tmp_path / "native"
    source.mkdir()
    (source / "run_file_audio_interop.py").write_text("private interface address and local experiment")
    (source / "pairing_host.py").write_text("portable pairing helper")
    payload = release.Payload(tmp_path, tmp_path / "payload")
    payload.tree(source, "native", source_only=True, skip_files=release.LOCAL_ONLY_DIAGNOSTICS)
    assert {p.name for p in (payload.destination / "native").iterdir()} == {"pairing_host.py"}


def test_quest_only_download_has_the_html_guide_brand_image(tmp_path):
    root = tmp_path / "root"
    names = ["README.md", "README.en.md", "LICENSE", "THIRD_PARTY_NOTICES.md",
             "CONTRIBUTING.md", "SECURITY.md", "CHANGELOG.md",
             "scripts/release/install-quest.ps1", "scripts/release/quest-install-ui.ps1",
             "scripts/release/installer-launcher.ps1"]
    names += ["docs/" + name for name in release.PUBLIC_RELEASE_DOCS]
    for name in names:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("public fixture", "utf-8")
    guide = '<img src="../resources/ui/brand/quest3d-mark.png">'
    (root / "docs/DESKTOP_USER_GUIDE.html").write_text(guide, "utf-8")
    brand = root / "resources/ui/brand/quest3d-mark.png"
    brand.parent.mkdir(parents=True)
    brand.write_bytes(b"synthetic image resource")
    apk = root / "fixture.apk"
    apk.write_bytes(b"synthetic APK bytes; never installed")
    sha = release.digest(apk)
    metadata = {"apk_sha256": sha, "apk_metadata": {
        "package": "app.questto3d.client.debug", "version_code": 1,
        "certificate_sha256": "1" * 64, "signing_kind": "development"}}
    out = tmp_path / "out"
    out.mkdir()
    release.build_quest(root, out, "test", apk, sha, metadata)
    with zipfile.ZipFile(out / "Sterevi-Quest-test.zip") as archive:
        assert archive.read("docs/DESKTOP_USER_GUIDE.html").decode() == guide
        assert archive.read("resources/ui/brand/quest3d-mark.png") == brand.read_bytes()


def public_quest_fixture(root):
    names = ["README.md", "README.en.md", "LICENSE", "THIRD_PARTY_NOTICES.md",
             "CONTRIBUTING.md", "SECURITY.md", "CHANGELOG.md",
             "scripts/release/install-quest.ps1", "scripts/release/quest-install-ui.ps1",
             "scripts/release/installer-launcher.ps1", "resources/ui/brand/quest3d-mark.png"]
    names += ["docs/" + name for name in release.PUBLIC_RELEASE_DOCS]
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"public fixture")
    apk = root / "public.apk"
    apk.write_bytes(b"synthetic public APK; never installed")
    source = root / "reviewed-source"
    source.mkdir()
    notice = source / "NOTICES.md"
    notice.write_bytes(b"Actual dependency attribution fixture\n")
    record = {"apk_sha256": release.digest(apk), "apk_metadata": {
        "package": "app.questto3d.client", "version_code": 1,
        "certificate_sha256": "2" * 64, "signing_kind": "release"},
        "source_complete": True, "clean_build_verified": True,
        "dependency_notices_verified": True,
        "files": {"NOTICES.md": release.digest(notice)},
        "binary_notice_files": ["NOTICES.md"]}
    return apk, source, record


def test_public_quest_archive_delivers_exact_reviewed_dependency_notices(tmp_path):
    root = tmp_path / "root"
    apk, source, record = public_quest_fixture(root)
    out = tmp_path / "out"
    out.mkdir()
    release.build_quest(root, out, "public-test", apk, release.digest(apk), record, source)
    with zipfile.ZipFile(out / "Sterevi-Quest-public-test.zip") as archive:
        assert archive.read("notices/quest/NOTICES.md") == (source / "NOTICES.md").read_bytes()
        assert json.loads(archive.read("distribution-manifest.json"))["metadata"]["quest_dependency_notices_verified"] is True


@pytest.mark.parametrize("failure", ["missing-gate", "no-list", "no-source"])
def test_public_quest_without_reviewed_notice_inputs_is_refused_before_output(tmp_path, failure):
    root = tmp_path / "root"
    apk, source, record = public_quest_fixture(root)
    if failure == "missing-gate":
        record["dependency_notices_verified"] = "true"
    elif failure == "no-list":
        record["binary_notice_files"] = []
    else:
        source = None
    out = tmp_path / "out"
    with pytest.raises(ValueError, match="verified sources and actual binary notices"):
        release.build_quest(root, out, "public-test", apk, release.digest(apk), record, source)
    assert not out.exists()


def test_changed_notice_cannot_be_shipped_under_a_valid_source_hash(tmp_path):
    root = tmp_path / "root"
    apk, source, record = public_quest_fixture(root)
    (source / "NOTICES.md").write_bytes(b"different attribution")
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(ValueError, match="Pinned hash mismatch"):
        release.build_quest(root, out, "public-test", apk, release.digest(apk), record, source)
    assert not (out / "Sterevi-Quest-public-test.zip").exists()


def test_final_byte_gate_blocks_private_owner_inside_nested_apk(tmp_path, monkeypatch):
    monkeypatch.setenv("USERNAME", "synthetic-owner-account")
    apk = tmp_path / "client.apk"
    with zipfile.ZipFile(apk, "w") as archive:
        archive.writestr("lib/arm64-v8a/stream.so", b"ELF synthetic-owner-account")
    installer = tmp_path / "installer.zip"
    with zipfile.ZipFile(installer, "w") as archive:
        archive.write(apk, "client.apk")
    report = tmp_path / "audit.json"
    with pytest.raises(ValueError, match="Final-byte privacy gate failed"):
        release.verify_release_privacy([installer], output=report)
    result = json.loads(report.read_text())
    assert result["private_marker_gate_passed"] is False
    assert any(item["path"].endswith("stream.so") for item in result["findings"])
    assert "synthetic-owner-account" not in report.read_text()

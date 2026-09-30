"""Verify actual archive bytes and prevent mixed-version native/APK releases."""
import hashlib
import importlib.util
import json
from pathlib import Path
import zipfile

import pytest

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("asset_review", ROOT / "scripts/release/review_assets.py")
review = importlib.util.module_from_spec(spec)
spec.loader.exec_module(review)


def build_set(tmp_path, *, apk_binding=None, host_binding=None):
    release = "test-preview"
    files = tmp_path / "assets"
    files.mkdir()
    apk = b"APK fixture bytes"
    apk_sha = hashlib.sha256(apk).hexdigest()
    host = b"native host fixture bytes"
    host_sha = hashlib.sha256(host).hexdigest()
    source = b"# corresponding source\n"
    quest_record = {"apk_sha256": apk_binding or apk_sha,
        "apk_metadata": {"package": "app.questto3d.client.debug", "version_code": 1,
                         "version_name": "1.0.0", "certificate_sha256": "2" * 64, "signing_kind": "development"},
        "source_complete": False, "clean_build_verified": False,
        "files": {"main.gd": hashlib.sha256(source).hexdigest()}}
    for label, kind in review.KINDS.items():
        payload = review.bundle.Payload(tmp_path, tmp_path / label)
        if label == "Desktop":
            payload.put("src/main.py", b"print('hello')\n")
            payload.put("artifacts/host/runtime-public/sunshine.exe", host)
            payload.put("config/distribution.json", json.dumps({"schema": 1, "host_runtime": "artifacts/host/runtime-public", "host_sha256": host_sha}).encode())
            meta = {"kind": kind, "host_sha256": host_sha}
        elif label == "Quest":
            payload.put("Quest3D-Quest.apk", apk)
            # Installer references the actual APK, not the deliberately wrong source.
            install = review.bundle.quest_install_metadata(quest_record | {"apk_sha256": apk_sha}, apk_sha)
            payload.put("quest-install.json", json.dumps(install).encode())
            meta = {"kind": kind, "apk_sha256": apk_sha}
        else:
            payload.put("sources/quest/main.gd", source)
            payload.put("sources/quest/source-manifest.json", json.dumps(quest_record).encode())
            payload.put("sources/sunshine/provenance.json", json.dumps({"host_binary_sha256": host_binding or host_sha,
                         "binary_source_rebuild_verified": False}).encode())
            meta = {"kind": kind}
        payload.manifest(release=release, metadata=meta)
        review.bundle.zip_payload(payload.destination, files / f"Quest3D-{label}-{release}.zip")
    return files, release


def test_integrity_and_cross_binding_do_not_claim_hardware_or_public_readiness(tmp_path):
    directory, release = build_set(tmp_path)
    result = review.review(directory, release)
    assert result["available_archive_integrity_verified"]
    assert result["binary_source_cross_binding_verified"]
    assert not result["ready_for_public_release"] and not result["published"]
    assert not any(result["source_and_signing"].values())
    assert "fresh_windows_install" in result["pending_acceptance"]


@pytest.mark.parametrize("keyword", ["apk_binding", "host_binding"])
def test_mixed_source_or_host_is_rejected_even_when_all_zip_hashes_are_valid(tmp_path, keyword):
    directory, release = build_set(tmp_path, **{keyword: "4" * 64})
    with pytest.raises(ValueError, match="cross-binding mismatch"):
        review.review(directory, release)


def test_missing_asset_is_incomplete_and_not_a_complete_release(tmp_path):
    directory, release = build_set(tmp_path)
    (directory / f"Quest3D-Quest-{release}.zip").rename(directory / "unrelated.zip")
    result = review.review(directory, release)
    assert result["missing_assets"] == ["Quest"]
    assert not result["binary_source_cross_binding_verified"] and not result["ready_for_public_release"]


def test_unlisted_zip_entry_is_rejected(tmp_path):
    directory, release = build_set(tmp_path)
    path = directory / f"Quest3D-Desktop-{release}.zip"
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr("personal-note.txt", "unreviewed")
    with pytest.raises(ValueError, match="omits or adds"):
        review.review(directory, release)


def test_content_change_with_stale_embedded_manifest_is_rejected(tmp_path):
    directory, release = build_set(tmp_path)
    path = directory / f"Quest3D-Desktop-{release}.zip"
    with zipfile.ZipFile(path) as original:
        entries = {info.filename: original.read(info) for info in original.infolist()}
    entries["src/main.py"] = b"changed"
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    with pytest.raises(ValueError, match="checksum mismatch"):
        review.review(directory, release)


def test_different_host_bytes_cannot_hide_behind_matching_metadata_strings(tmp_path):
    directory, release = build_set(tmp_path)
    path = directory / f"Quest3D-Desktop-{release}.zip"
    with zipfile.ZipFile(path) as original:
        entries = {info.filename: original.read(info) for info in original.infolist()}
    name = "artifacts/host/runtime-public/sunshine.exe"
    entries[name] = b"different actual native executable"
    manifest = json.loads(entries["distribution-manifest.json"])
    manifest["files"][name] = {"bytes": len(entries[name]), "sha256": hashlib.sha256(entries[name]).hexdigest()}
    entries["distribution-manifest.json"] = json.dumps(manifest).encode()
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    # Every internal entry hash is valid, but the source belongs to another host.
    with pytest.raises(ValueError, match="host and native source cross-binding"):
        review.review(directory, release)

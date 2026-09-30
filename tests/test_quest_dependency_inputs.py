"""Publication input boundaries using local, bounded dependency fixtures."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest

RELEASE = Path(__file__).resolve().parents[1] / "scripts/release"


def load(name):
    sys.path.insert(0, str(RELEASE))
    try:
        spec = importlib.util.spec_from_file_location(name, RELEASE / (name + ".py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(RELEASE))


notice = load("audit_quest_maven_notices")
vendor = load("fetch_quest_vendor_build_inputs")
apk = load("verify_quest_unsigned_apk")


def pom(cache, name, text):
    path = cache / "caches/modules-2/files-2.1/example" / name / "1" / "fixture" / (name + ".pom")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('<project xmlns="http://maven.apache.org/POM/4.0.0">' + text + '</project>')
    return path


def report(path, name="child"):
    path.write_text(json.dumps({"exit_code": 0, "resolved": True, "runtime_dependencies": [
        {"group": "example", "name": name, "version": "1"}]}))


PARENT = '<parent><groupId>example</groupId><artifactId>parent</artifactId><version>1</version></parent>'
LICENSE = '<licenses><license><name>Apache-2.0</name><url>https://www.apache.org/licenses/LICENSE-2.0</url></license></licenses>'


def test_license_inheritance_records_parent_without_claiming_notice_completion(tmp_path):
    cache = tmp_path / "cache"
    pom(cache, "child", PARENT)
    parent = pom(cache, "parent", LICENSE)
    source = tmp_path / "resolved.json"
    report(source)
    output = tmp_path / "audit"
    result = notice.audit(source, cache, output)
    record = json.loads((output / "maven-notices-review.json").read_text())
    assert result["all_pom_license_declarations_present"] is True
    assert not record["dependency_notices_verified"] and not record["public_release_ready"]
    assert record["modules"][0]["license_pom_ancestry"][0]["pom_sha256"] == hashlib.sha256(parent.read_bytes()).hexdigest()


def test_cyclic_license_ancestry_is_rejected(tmp_path):
    cache = tmp_path / "cache"
    pom(cache, "child", PARENT)
    pom(cache, "parent", '<parent><groupId>example</groupId><artifactId>child</artifactId><version>1</version></parent>')
    source = tmp_path / "resolved.json"
    report(source)
    output = tmp_path / "audit"
    with pytest.raises(ValueError, match="Cyclic"):
        notice.audit(source, cache, output)
    assert not (output / "maven-notices-review.json").exists()


@pytest.mark.parametrize("change", ["truncated", "commit", "symlink", "overflow", "case"])
def test_vendor_tree_ambiguity_fails_before_download_or_output(tmp_path, monkeypatch, change):
    entry = {"path": "LICENSE", "type": "blob", "mode": "100644", "size": 1, "sha": "0" * 40}
    tree = {"sha": vendor.COMMIT, "truncated": False, "tree": [entry]}
    if change == "truncated":
        tree["truncated"] = True
    elif change == "commit":
        tree["sha"] = "0" * 40
    elif change == "symlink":
        entry["mode"] = "120000"
    elif change == "overflow":
        entry["size"] = vendor.TOTAL_LIMIT + 1
    elif change == "case":
        tree["tree"] = [dict(entry, path="plugin/source.cpp"), dict(entry, path="plugin/SOURCE.cpp")]
    source = tmp_path / "tree.json"
    source.write_text(json.dumps(tree))
    output = tmp_path / "download"
    monkeypatch.setattr(vendor, "urlopen", lambda *a, **k: pytest.fail("Must reject before HTTP"))
    with pytest.raises(ValueError):
        vendor.fetch(source, output)
    assert not output.exists()


def test_debuggable_and_permission_metadata_comes_from_actual_badging():
    text = "package: name='app.questto3d.client' versionCode='1' versionName='0.1.0-review' compileSdkVersion='36'\n" \
           "sdkVersion:'29'\ntargetSdkVersion:'32'\nuses-permission: name='android.permission.INTERNET'\napplication-debuggable\n"
    result = apk.identity(text)
    assert result["debuggable"] and result["compile_sdk"] == 36
    assert result["permissions"] == ["android.permission.INTERNET"]
    with pytest.raises(ValueError, match="metadata"):
        apk.identity("incomplete metadata")

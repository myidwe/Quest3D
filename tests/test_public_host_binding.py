"""A new host cannot be shipped with a different runtime or historical source."""
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "public_host_bundle", Path(__file__).parents[1] / "scripts/release/build_bundle.py")
bundle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bundle)


def entry(path):
    return {"sha256": bundle.digest(path), "bytes": path.stat().st_size}


def fixture(root):
    runtime = root / "runtime"
    sources = root / "supply"
    runtime.mkdir()
    sources.mkdir()
    for name, data in {
        "sunshine.exe": b"new native executable fixture",
        "zlib1.dll": b"runtime dependency fixture",
        "LICENSE.txt": b"GPLv3 fixture",
        "assets/apps.json": b"{}",
        "assets/web/index.html": b"<html></html>",
    }.items():
        path = runtime / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    binary = bundle.digest(runtime / "sunshine.exe")
    (sources / "modified.tar").write_bytes(b"source archive fixture")
    (sources / "provenance.json").write_text(json.dumps({
        "host_binary_sha256": binary, "binary_source_rebuild_verified": True,
        "source_complete": True, "dependency_notices_verified": True}), "utf-8")
    records = {p.relative_to(runtime).as_posix(): entry(p) for p in runtime.rglob("*") if p.is_file()}
    (runtime / "runtime-preparation.json").write_text(json.dumps({
        "schema": 1, "kind": "host-release-runtime", "host_exe_sha256": binary,
        "files": records}), "utf-8")
    record = {
        "schema": 1, "kind": "host-release-input", "binary_sha256": binary,
        "runtime_path": "runtime", "runtime_manifest_name": "runtime-preparation.json",
        "runtime_manifest_sha256": bundle.digest(runtime / "runtime-preparation.json"),
        "runtime_files": records, "source_supply_path": "supply",
        "files": {p.name: entry(p) for p in sources.iterdir()},
        "source_complete": True, "notices_verified": True, "native_build_verified": True,
        "hardware_validation": False,
    }
    path = root / "HOST_RELEASE.json"
    path.write_text(json.dumps(record), "utf-8")
    return path, record


def save(path, record):
    path.write_text(json.dumps(record), "utf-8")


def test_new_runtime_source_and_notices_are_bound_without_claiming_hardware(tmp_path):
    path, record = fixture(tmp_path)
    assert bundle.validate_host_release(tmp_path, path) == record
    assert not record["hardware_validation"]
    assert bundle.HOST_SHA != record["binary_sha256"]


@pytest.mark.parametrize("flag", ["source_complete", "notices_verified", "native_build_verified"])
@pytest.mark.parametrize("value", [False, "true", 1, None])
def test_unverified_or_untyped_release_gate_is_refused(tmp_path, flag, value):
    path, record = fixture(tmp_path)
    record[flag] = value
    save(path, record)
    with pytest.raises(ValueError, match="must be verified"):
        bundle.validate_host_release(tmp_path, path)


@pytest.mark.parametrize("name", ["runtime/sunshine.exe", "runtime/assets/apps.json", "supply/modified.tar", "runtime/runtime-preparation.json"])
def test_changed_input_is_refused_after_selection(tmp_path, name):
    path, _ = fixture(tmp_path)
    (tmp_path / name).write_bytes(b"changed")
    with pytest.raises(ValueError, match="hash mismatch"):
        bundle.validate_host_release(tmp_path, path)


@pytest.mark.parametrize("base", ["runtime", "supply"])
def test_unreviewed_file_cannot_enter_either_inventory(tmp_path, base):
    path, _ = fixture(tmp_path)
    (tmp_path / base / "extra.txt").write_text("unreviewed")
    with pytest.raises(ValueError, match="Unlisted"):
        bundle.validate_host_release(tmp_path, path)


def test_valid_checksums_cannot_bind_a_source_to_another_executable(tmp_path):
    path, record = fixture(tmp_path)
    source = tmp_path / "supply/provenance.json"
    provenance = json.loads(source.read_text())
    provenance["host_binary_sha256"] = "4" * 64
    source.write_text(json.dumps(provenance))
    record["files"]["provenance.json"] = entry(source)
    save(path, record)
    with pytest.raises(ValueError, match="provenance does not match"):
        bundle.validate_host_release(tmp_path, path)


@pytest.mark.parametrize("field", ["runtime_path", "source_supply_path", "runtime_manifest_name"])
def test_host_input_cannot_escape_workspace(tmp_path, field):
    path, record = fixture(tmp_path)
    record[field] = "../outside"
    save(path, record)
    with pytest.raises(ValueError, match="Unsafe"):
        bundle.validate_host_release(tmp_path, path)


def test_private_source_name_is_rejected_even_if_listed_with_a_hash(tmp_path):
    path, record = fixture(tmp_path)
    private = tmp_path / "supply/credentials.json"
    private.write_text("state fixture")
    record["files"][private.name] = entry(private)
    save(path, record)
    with pytest.raises(ValueError, match="Private/state"):
        bundle.validate_host_release(tmp_path, path)


@pytest.mark.parametrize("change", ["malformed", "binary", "inventory", "kind"])
def test_rehashed_manifest_must_still_describe_the_same_runtime(tmp_path, change):
    path, record = fixture(tmp_path)
    runtime_manifest = tmp_path / "runtime/runtime-preparation.json"
    value = json.loads(runtime_manifest.read_text())
    if change == "malformed":
        runtime_manifest.write_text("not JSON")
    else:
        if change == "binary":
            value["host_exe_sha256"] = "5" * 64
        elif change == "inventory":
            value["files"]["sunshine.exe"]["sha256"] = "0" * 64
        else:
            value["kind"] = "unrelated"
        runtime_manifest.write_text(json.dumps(value))
    record["runtime_manifest_sha256"] = bundle.digest(runtime_manifest)
    save(path, record)
    with pytest.raises(ValueError):
        bundle.validate_host_release(tmp_path, path)

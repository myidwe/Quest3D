"""Validate the real compiled bootstrap, integrity guards, and child lifecycle."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import struct
import subprocess
import sys
import time
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _name in ("exe_payload", "build_exe_installer"):
    _spec = importlib.util.spec_from_file_location(_name, ROOT / "scripts/release" / (_name + ".py"))
    _module = importlib.util.module_from_spec(_spec)
    sys.modules[_name] = _module
    _spec.loader.exec_module(_module)
payload_api = sys.modules["exe_payload"]
builder = sys.modules["build_exe_installer"]


def package(*, target="pc", version="0.1.1-preview", extra=None, child_code=0, child_script=None, raw_manifest=None, names=None):
    scripts = {"scripts/release/installer-launcher.ps1": (child_script or f"param([string]$Target,[switch]$SelfTest,[switch]$NoDialog)\nexit {child_code}\n").encode(),
               "scripts/release/" + ("install-ui.ps1" if target == "pc" else "quest-install-ui.ps1"): b"# test-only entry point\n",
               "scripts/release/" + ("install.ps1" if target == "pc" else "install-quest.ps1"): b"param([string]$Destination,[string]$Python,[switch]$Update,[switch]$NoShortcuts)\nexit 0\n"}
    scripts.update(extra or {})
    files = {name: {"bytes": len(value), "sha256": hashlib.sha256(value).hexdigest()} for name, value in scripts.items()}
    manifest = {"schema": 1, "release": version, "metadata": {"kind": target + "-installer-candidate"}, "files": files}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, value in scripts.items():
            archive.writestr(name, value)
        for name, value in names or []:
            archive.writestr(name, value)
        archive.writestr("distribution-manifest.json", raw_manifest or json.dumps(manifest))
    return buffer.getvalue()


def overlay(payload=None):
    payload = payload or package()
    stub = b"MZ" + payload_api.SETUP_MARKER.encode("utf-16-le")
    return stub + payload + payload_api.pack_footer(len(stub), payload)


def test_overlay_round_trip_and_metadata():
    data = overlay()
    payload, metadata = payload_api.read_payload(data)
    assert payload == package()
    assert metadata["payload_sha256"] == hashlib.sha256(payload).hexdigest()
    assert metadata["stub_bytes"] + metadata["payload_bytes"] + 64 == len(data)
    assert payload_api.has_setup_marker(data)


@pytest.mark.parametrize("encoding", ["ascii", "utf-16-le", "utf-16-be"])
def test_marker_survives_a_damaged_footer(encoding):
    assert payload_api.has_setup_marker(b"MZ" + payload_api.SETUP_MARKER.encode(encoding))


@pytest.mark.parametrize("kind", ["mz", "magic", "hash", "size", "start", "truncated", "extra", "zip"])
def test_overlay_rejects_corruption(kind):
    data = bytearray(overlay())
    if kind == "mz": data[0] = 0
    elif kind == "magic": data[-64] ^= 1
    elif kind == "hash": data[-1] ^= 1
    elif kind == "size": data[-40:-32] = struct.pack("<Q", 0)
    elif kind == "start": data[-48:-40] = struct.pack("<Q", 2**64 - 1)
    elif kind == "truncated": del data[-3:]
    elif kind == "extra": data += b"unexpected tail"
    elif kind == "zip":
        original, metadata = payload_api.read_payload(bytes(data))
        data[metadata["stub_bytes"]] = 0
        data[-32:] = hashlib.sha256(bytes(data[metadata["stub_bytes"]:-64])).digest()
    with pytest.raises(ValueError): payload_api.read_payload(bytes(data))


@pytest.mark.parametrize("size", [True, 0, 1, -1, 2**64, "2"])
def test_footer_rejects_invalid_stub_size(size):
    with pytest.raises(ValueError): payload_api.pack_footer(size, package())


@pytest.mark.parametrize("name", ["../escape", "/absolute", "C:/drive", "folder\\file", "a//b", "a/./b", "a/../b", "trailing.", "trailing ", "NUL", "nested/COM1.txt", "a/aux", "a\x00b", "a\nb", "a?b", "a:b", "credentials.json", "nested/.env", "signing/file", ".venv/file", ".venv-host/file", ".cache/file", "user-data/file", "secret.pem", "logs/private.log"])
def test_zip_refuses_private_or_unsafe_manifest_paths(name):
    with pytest.raises(ValueError): builder.verify_zip(package(extra={name: b"x"}), target="pc", version="0.1.1-preview")


def test_zip_rejects_missing_or_unknown_files():
    with pytest.raises(ValueError): builder.verify_zip(package(names=[("unknown.txt", b"x")]), target="pc", version="0.1.1-preview")
    with pytest.raises(ValueError): builder.verify_zip(package(), target="quest", version="0.1.1-preview")
    with pytest.raises(ValueError): builder.verify_zip(package(), target="pc", version="0.1.2-preview")


def test_zip_rejects_case_and_parent_collisions():
    with pytest.raises(ValueError): builder.verify_zip(package(extra={"README.md": b"x", "readme.md": b"x"}), target="pc", version="0.1.1-preview")
    with pytest.raises(ValueError): builder.verify_zip(package(extra={"folder": b"x", "Folder/file": b"y"}), target="pc", version="0.1.1-preview")


def test_zip_rejects_duplicate_json_and_duplicate_entries():
    with pytest.raises(ValueError): builder.verify_zip(package(raw_manifest='{"schema":1,"schema":1}'), target="pc", version="0.1.1-preview")
    with pytest.warns(UserWarning): data = package(names=[("scripts/release/installer-launcher.ps1", b"extra")])
    with pytest.raises(ValueError): builder.verify_zip(data, target="pc", version="0.1.1-preview")


def test_zip_rejects_symlink_and_directory_attributes():
    for mode in (stat.S_IFLNK | 0o777, stat.S_IFDIR | 0o755):
        source = zipfile.ZipFile(io.BytesIO(package()))
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as output:
            for entry in source.infolist():
                if entry.filename == "scripts/release/install-ui.ps1": entry.external_attr = mode << 16
                output.writestr(entry, source.read(entry))
        with pytest.raises(ValueError): builder.verify_zip(buffer.getvalue(), target="pc", version="0.1.1-preview")


def test_zip_rejects_wrong_individual_hash():
    source = zipfile.ZipFile(io.BytesIO(package()))
    manifest = json.loads(source.read("distribution-manifest.json"))
    manifest["files"]["scripts/release/install-ui.ps1"]["sha256"] = "0" * 64
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as output:
        for entry in source.infolist(): output.writestr(entry.filename, json.dumps(manifest) if entry.filename == "distribution-manifest.json" else source.read(entry))
    with pytest.raises(ValueError): builder.verify_zip(buffer.getvalue(), target="pc", version="0.1.1-preview")


WINDOWS = pytest.mark.skipif(sys.platform != "win32" or not (Path(os.environ.get("SystemRoot", "C:/Windows")) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe").is_file(), reason="Windows .NET Framework compiler required")


def compile_package(directory, data=None, target="pc", version="0.1.1-preview"):
    directory.mkdir(parents=True, exist_ok=True)
    zip_path = directory / "payload.zip"
    zip_path.write_bytes(data or package(target=target, version=version))
    exe = directory / "Setup.exe"
    builder.build(zip_path, target, version, exe)
    return exe


def run(exe, *args, timeout=45):
    result = subprocess.run([str(exe), *map(str, args)], capture_output=True, text=True, timeout=timeout)
    assert result.stderr == ""
    return result, json.loads(result.stdout)


@WINDOWS
def test_actual_compiled_verification_and_korean_space_extraction(tmp_path):
    exe = compile_package(tmp_path / "설치 파일")
    result, report = run(exe, "--verify-only")
    assert result.returncode == 0 and report["passed"] and not report["installed_confirmed"]
    destination = tmp_path / "한글 추출 폴더"
    result, report = run(exe, "--extract-only", destination)
    assert result.returncode == 0 and report["extraction_verified"]
    with zipfile.ZipFile(exe.parent / "payload.zip") as archive:
        assert {p.relative_to(destination).as_posix() for p in destination.rglob("*") if p.is_file()} == set(archive.namelist())
        assert all((destination / entry.filename).read_bytes() == archive.read(entry) for entry in archive.infolist())
    # A retry cannot silently overwrite a previously extracted file.
    result, report = run(exe, "--extract-only", destination)
    assert result.returncode != 0 and report["error"] == "destination"


@WINDOWS
def test_actual_footer_hash_and_compiled_hash_tampering_are_blocked(tmp_path):
    exe = compile_package(tmp_path / "source")
    data = exe.read_bytes()
    original, metadata = payload_api.read_payload(data)
    for label, mutate, fix_footer in [("bad-footer", -64, False), ("bad-hash", metadata["stub_bytes"] + 30, False), ("forged-footer", metadata["stub_bytes"] + 30, True)]:
        changed = bytearray(data); changed[mutate] ^= 1
        if fix_footer: changed[-32:] = hashlib.sha256(changed[metadata["stub_bytes"]:-64]).digest()
        copied = tmp_path / (label + ".exe"); copied.write_bytes(changed)
        result, report = run(copied, "--verify-only")
        assert result.returncode != 0 and report["error"] == "integrity"


@WINDOWS
def test_actual_child_close_is_not_reported_as_installation(tmp_path):
    exe = compile_package(tmp_path / "closed")
    result, report = run(exe, "--ui-self-test")
    assert result.returncode == 0 and report["ui_self_test"] and report["child_exit_code"] == 0
    assert report["installed_confirmed"] is False and report["staging_cleaned"] is True
    assert not list(exe.parent.glob("Quest3D-Setup-*"))


@WINDOWS
def test_actual_child_failure_preserves_private_stage(tmp_path):
    exe = compile_package(tmp_path / "failed", package(child_code=17))
    result, report = run(exe, "--ui-self-test")
    assert result.returncode == 17 and report["child_exit_code"] == 17 and not report["passed"]
    assert report["installed_confirmed"] is False and report["staging_preserved"] is True
    stages = list(exe.parent.glob("Quest3D-Setup-*")); assert len(stages) == 1
    assert (stages[0] / ".quest3d-stage-owner").read_text() == payload_api.SETUP_MARKER
    # Protected ACL must not inherit access from a shared parent directory.
    acl = subprocess.run(["powershell.exe", "-NoProfile", "-Command", "$ErrorActionPreference='Stop'; [IO.Directory]::GetAccessControl($env:Q3D_STAGE).AreAccessRulesProtected"], env={**os.environ, "Q3D_STAGE": str(stages[0])}, capture_output=True, text=True, timeout=10)
    assert acl.returncode == 0 and acl.stdout.strip() == "True"


@WINDOWS
def test_actual_advanced_install_routes_quoted_paths(tmp_path):
    script = b'param([string]$Destination,[string]$Python,[switch]$Update,[switch]$NoShortcuts)\nNew-Item -ItemType Directory -Path $Destination -Force | Out-Null\n[IO.File]::WriteAllText((Join-Path $Destination "args.json"),(@{python=$Python;update=$Update.IsPresent;no_shortcuts=$NoShortcuts.IsPresent} | ConvertTo-Json))\nexit 0\n'
    exe = compile_package(tmp_path / "advanced", package(extra={"scripts/release/install.ps1": script}))
    destination = tmp_path / "설치 결과"
    python = tmp_path / "Python 경로" / "python.exe"
    result, report = run(exe, "--install", destination, "--python", python, "--update", "--no-shortcuts")
    assert result.returncode == 0 and report["installed_confirmed"] is True
    actual = json.loads((destination / "args.json").read_text("utf-8-sig"))
    assert actual == {"python": str(python), "update": True, "no_shortcuts": True}


@WINDOWS
def test_actual_extraction_supports_paths_over_260_characters(tmp_path):
    name = "nested/" + "/".join(["longcomponent" * 5] * 4) + "/검증.txt"
    exe = compile_package(tmp_path / "long-path", package(extra={name: b"long path content"}))
    destination = tmp_path / "긴 경로 검증 폴더"
    assert len(str(destination / name)) > 300
    result, report = run(exe, "--extract-only", destination)
    assert result.returncode == 0 and report["extraction_verified"]
    assert (destination / name).read_bytes() == b"long path content"


def pe_resources(data):
    """Read actual PE resources without adding a runtime/build dependency."""
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    assert data[pe:pe + 4] == b"PE\0\0"
    machine, section_count = struct.unpack_from("<HH", data, pe + 4)
    optional_length = struct.unpack_from("<H", data, pe + 20)[0]
    optional = pe + 24
    assert machine == 0x8664 and struct.unpack_from("<H", data, optional + 68)[0] == 2  # AMD64, Windows GUI
    assert struct.unpack_from("<H", data, optional)[0] == 0x20B  # PE32+
    sections = optional + optional_length

    def raw(rva):
        for index in range(section_count):
            entry = sections + index * 40
            size, start, raw_size, raw_start = struct.unpack_from("<IIII", data, entry + 8)
            if start <= rva < start + max(size, raw_size): return raw_start + rva - start
        raise AssertionError("Resource outside PE sections")

    resource_rva = struct.unpack_from("<I", data, optional + 112 + 16)[0]
    base = raw(resource_rva)
    result = []

    def walk(offset, identifiers=()):
        named, ids = struct.unpack_from("<HH", data, base + offset + 12)
        for index in range(named + ids):
            name, child = struct.unpack_from("<II", data, base + offset + 16 + index * 8)
            path = identifiers + (name,)
            if child & 0x80000000: walk(child & 0x7FFFFFFF, path)
            else:
                rva, length = struct.unpack_from("<II", data, base + child)
                start = raw(rva)
                result.append((path, data[start:start + length]))
    walk(0)
    return result


@WINDOWS
@pytest.mark.parametrize("version,numeric", [("0.1.2-preview", "0.1.2.0"), ("nightly", "1.0.0.0")])
def test_actual_windows_version_icon_manifest_and_gui_metadata(tmp_path, version, numeric):
    exe = compile_package(tmp_path / version, version=version)
    result = subprocess.run(["powershell.exe", "-NoProfile", "-Command", "$ErrorActionPreference='Stop'; $v=[Diagnostics.FileVersionInfo]::GetVersionInfo($env:Q3D_EXE); [Console]::WriteLine($v.FileVersion); [Console]::WriteLine($v.ProductVersion)"], env={**os.environ, "Q3D_EXE": str(exe)}, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0 and result.stderr == ""
    assert result.stdout.splitlines() == [numeric, version]
    proof = json.loads(exe.with_suffix(".exe.build.json").read_text("utf-8"))
    assert proof["version"] == version and proof["windows_file_version"] == numeric
    assert proof["generated_build_info_sha256"] and not proof["administrator_requested"] and not proof["debug_symbols"]
    resources = pe_resources(exe.read_bytes())
    manifests = [value for ids, value in resources if ids[0] == 24]
    assert len(manifests) == 1 and b'level="asInvoker"' in manifests[0]
    assert b'uiAccess="false"' in manifests[0] and b"requireAdministrator" not in manifests[0]
    assert any(ids[0] == 14 for ids, _ in resources)  # Windows icon group
    icons = [hashlib.sha256(value).hexdigest() for ids, value in resources if ids[0] == 3]
    original = (ROOT / "resources/desktop.ico").read_bytes()
    _, kind, count = struct.unpack_from("<HHH", original)
    assert kind == 1
    expected = []
    for index in range(count):
        length, offset = struct.unpack_from("<II", original, 6 + index * 16 + 8)
        expected.append(hashlib.sha256(original[offset:offset + length]).hexdigest())
    assert sorted(icons) == sorted(expected)


@WINDOWS
def test_actual_duplicate_bootstrap_is_blocked(tmp_path):
    exe = compile_package(tmp_path / "duplicate", package(child_script="param([string]$Target,[switch]$SelfTest,[switch]$NoDialog)\n[IO.File]::WriteAllText((Join-Path $PSScriptRoot 'started.flag'),'ready')\nStart-Sleep -Seconds 4\nexit 0\n"))
    first = subprocess.Popen([str(exe), "--ui-self-test"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not list(exe.parent.glob("Quest3D-Setup-*/scripts/release/started.flag")): time.sleep(.05)
        assert list(exe.parent.glob("Quest3D-Setup-*/scripts/release/started.flag"))
        second, report = run(exe, "--verify-only")
        assert second.returncode != 0 and report["error"] == "busy"
        output, errors = first.communicate(timeout=15)
        assert first.returncode == 0 and not errors and json.loads(output)["passed"]
    finally:
        if first.poll() is None: first.kill(); first.communicate()


@WINDOWS
def test_build_refuses_overwriting_and_reparse_ancestors(tmp_path):
    exe = compile_package(tmp_path / "ordinary")
    before = exe.read_bytes()
    with pytest.raises(FileExistsError): builder.build(exe.parent / "payload.zip", "pc", "0.1.1-preview", exe)
    assert exe.read_bytes() == before
    linked = tmp_path / "junction"
    command = subprocess.run(["powershell.exe", "-NoProfile", "-Command", "New-Item -ItemType Junction -Path $env:Q3D_LINK -Target $env:Q3D_REAL | Out-Null"], env={**os.environ, "Q3D_LINK": str(linked), "Q3D_REAL": str(exe.parent)}, capture_output=True, text=True, timeout=10)
    assert command.returncode == 0
    try:
        with pytest.raises(ValueError): builder.build(linked / "payload.zip", "pc", "0.1.1-preview", tmp_path / "must-not-exist.exe")
        result, report = run(exe, "--extract-only", linked / "child")
        assert result.returncode != 0 and report["error"] == "linked_path"
        assert not (exe.parent / "child").exists()
    finally:
        # Remove only the junction itself; never traverse its target.
        os.rmdir(linked)

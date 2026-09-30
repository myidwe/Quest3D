"""Runtime staging preserves inputs and refuses unsafe/incomplete candidates."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess

import pytest

spec = importlib.util.spec_from_file_location("host_runtime_candidate", Path(__file__).parents[1] / "scripts/release/prepare_host_runtime_candidate.py")
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


def fixture(tmp_path):
    candidate = tmp_path / "compile"
    build = candidate / "cmake-build-compat-candidate"
    source = candidate / "source"
    entries = {
        "candidate-provenance.json": json.dumps({"kind": "historical-api-compatibility-candidate", "candidate_version": "candidate"}),
        "candidate-build-summary.json": json.dumps({"native_build_success": True, "tests_passed": True, "exit_code": 0, "host_exe_sha256": runtime.sha(b"exe")}),
        "cmake-build-compat-candidate/sunshine.exe": "exe",
        "source/LICENSE": "license",
        "source/src_assets/common/assets/desktop.png": "image",
        "source/src_assets/common/assets/web/private-source.js": "do not stage raw web",
        "source/src_assets/windows/assets/apps.json": "{}",
        "source/src_assets/windows/assets/shaders/convert.hlsl": "shader",
        "config/credentials.json": "must remain private",
    }
    for page in ("index", "pin", "password", "welcome"):
        entries[f"cmake-build-compat-candidate/assets/web/{page}.html"] = "html"
    for relative, text in entries.items():
        path = candidate / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    zlib = tmp_path / "toolchain/zlib1.dll"
    zlib.parent.mkdir()
    zlib.write_bytes(b"zlib")
    return candidate, zlib, tmp_path / "runtime"


def test_stage_uses_raw_shaders_built_web_and_omits_runtime_state(tmp_path):
    candidate, zlib, output = fixture(tmp_path)
    report = runtime.prepare(candidate, zlib, output)
    assert (output / "assets/shaders/convert.hlsl").read_text() == "shader"
    assert not (output / "config").exists()
    assert not (output / "assets/web/private-source.js").exists()
    assert (candidate / "config/credentials.json").read_text() == "must remain private"
    assert report["web_ui_packaged"] is True
    assert report["release_source_gate"] is False
    assert report["server_started"] is False
    for name, entry in report["files"].items():
        assert runtime.sha((output / name).read_bytes()) == entry["sha256"]


@pytest.mark.parametrize("kind", ["existing", "nested", "parent"])
def test_refuses_existing_or_overlapping_output_without_overwriting(tmp_path, kind):
    candidate, zlib, output = fixture(tmp_path)
    if kind == "existing":
        output.mkdir()
        marker = output / "keep"
        marker.write_text("keep")
    elif kind == "nested":
        output = candidate / "new-runtime"
    else:
        output = tmp_path
    with pytest.raises(FileExistsError):
        runtime.prepare(candidate, zlib, output)
    if kind == "existing":
        assert marker.read_text() == "keep"


def test_changed_executable_refused_before_output_creation(tmp_path):
    candidate, zlib, output = fixture(tmp_path)
    (candidate / "cmake-build-compat-candidate/sunshine.exe").write_bytes(b"different")
    with pytest.raises(ValueError, match="differs"):
        runtime.prepare(candidate, zlib, output)
    assert not output.exists()


@pytest.mark.parametrize("field,value", [("tests_passed", False), ("native_build_success", False), ("exit_code", 1)])
def test_failed_native_candidate_refused(tmp_path, field, value):
    candidate, zlib, output = fixture(tmp_path)
    path = candidate / "candidate-build-summary.json"
    report = json.loads(path.read_text())
    report[field] = value
    path.write_text(json.dumps(report))
    with pytest.raises(ValueError, match="Successful"):
        runtime.prepare(candidate, zlib, output)
    assert not output.exists()


def test_native_only_build_without_web_ui_refused(tmp_path):
    candidate, zlib, output = fixture(tmp_path)
    (candidate / "cmake-build-compat-candidate/assets/web/index.html").unlink()
    with pytest.raises(ValueError, match="not been built"):
        runtime.prepare(candidate, zlib, output)
    assert not output.exists()


def test_duplicate_asset_destination_refused(tmp_path):
    candidate, zlib, output = fixture(tmp_path)
    (candidate / "source/src_assets/common/assets/apps.json").write_text("duplicate")
    with pytest.raises(ValueError, match="Duplicate"):
        runtime.prepare(candidate, zlib, output)
    assert not output.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction boundary")
def test_junction_input_refused_before_output_creation(tmp_path):
    candidate, zlib, output = fixture(tmp_path)
    alias = tmp_path / "redirected-compile"
    subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(alias), str(candidate)],
                   capture_output=True, check=True)
    with pytest.raises(ValueError, match="Redirected"):
        runtime.prepare(alias, zlib, output)
    assert not output.exists()

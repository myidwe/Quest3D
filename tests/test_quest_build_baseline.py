"""Candidate boundaries for the isolated Quest source-build preparation."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile

import pytest

spec = importlib.util.spec_from_file_location("quest_build_baseline", Path(__file__).resolve().parents[1] / "scripts/release/prepare_quest_build_baseline.py")
baseline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(baseline)


PRESET = '''[preset.0]
name="NightfallDev"
export_path="./old.apk"
export_filter="all_resources"
[preset.0.options]
version/code=1
version/name=""
package/unique_name="app.questto3d.client.debug"
package/name="Quest 3D Desktop (Dev)"
package/signed=true
graphics/opengl_debug=true
keystore/debug="PRIVATE KEY PATH"
keystore/debug_password="SECRET"
architectures/arm64-v8a=true
xr_features/xr_mode=1
permissions/internet=true
'''


def digest(data):
    return hashlib.sha256(data).hexdigest()


def archive(name, data):
    result = io.BytesIO()
    with tarfile.open(fileobj=result, mode="w") as output:
        entry = tarfile.TarInfo(name)
        entry.size = len(data)
        output.addfile(entry, io.BytesIO(data))
    return result.getvalue()


def fixture_source(tmp_path, monkeypatch):
    source = tmp_path / "input"
    (source / "project/patches").mkdir(parents=True)
    (source / "upstream").mkdir()
    engine = archive("engine.txt", b"before\n")
    cpp = archive("SConstruct", b"# source\n")
    (source / "upstream/godot.tar").write_bytes(engine)
    (source / "upstream/godot-cpp.tar").write_bytes(cpp)
    (source / "project/export_presets.cfg").write_text(PRESET, "utf-8")
    patch = b"diff --git a/engine.txt b/engine.txt\n--- a/engine.txt\n+++ b/engine.txt\n@@ -1 +1 @@\n-before\n+after\n"
    (source / "project/patches/review.patch").write_bytes(patch)
    (source / "SOURCE_PROVENANCE.json").write_text(json.dumps({"cached_inputs": {"source_archives": {
        "godot": {"sha256": digest(engine), "commit": "engine-pin"},
        "godot-cpp": {"sha256": digest(cpp), "commit": "cpp-pin"},
    }}}), "utf-8")
    files = {p.relative_to(source).as_posix(): digest(p.read_bytes()) for p in source.rglob("*") if p.is_file()}
    (source / "source-manifest.json").write_text(json.dumps({"files": files}), "utf-8")
    # This source fixture intentionally contains just one independently valid patch.
    monkeypatch.setattr(baseline, "PATCHES", ("review.patch",))
    return source, files


def test_unsigned_public_identity_and_retained_xr_network_settings():
    result = baseline.public_preset(PRESET + '\n[preset.1]\nname="Another"\n')
    assert 'package/unique_name="app.questto3d.client"' in result
    assert "package/signed=false" in result
    assert 'name="Quest3DPublicReview"' in result
    assert 'version/name="0.1.0-review"' in result
    assert "graphics/opengl_debug=false" in result
    assert 'keystore/debug=""' in result and 'keystore/debug_password=""' in result
    assert "SECRET" not in result and "PRIVATE KEY PATH" not in result
    for line in ("architectures/arm64-v8a=true", "xr_features/xr_mode=1", "permissions/internet=true"):
        assert line in result
    assert "[preset.1]" not in result


@pytest.mark.parametrize("broken", [PRESET.replace("package/signed=true\n", ""), PRESET + "package/signed=true\n", ""])
def test_ambiguous_or_incomplete_export_identity_is_rejected(broken):
    with pytest.raises(ValueError):
        baseline.public_preset(broken)


def test_real_patch_applied_only_to_new_candidate(tmp_path, monkeypatch):
    source, files = fixture_source(tmp_path, monkeypatch)
    output = tmp_path / "new"
    result = baseline.prepare(source, output)
    assert result["prepared"] and not result["public_release_ready"]
    assert (output / "native-source/godot/engine.txt").read_bytes() == b"after\n"
    record = json.loads((output / "quest-build-preparation.json").read_text())
    assert record["engine_patch_hashes"]["review.patch"] == files["project/patches/review.patch"]
    assert record["public_preset_change"]["signed"] is False
    assert not record["clean_engine_build_verified"] and not record["full_apk_build_verified"]
    assert (source / "project/export_presets.cfg").read_text() == PRESET
    assert {name: digest((source / name).read_bytes()) for name in files} == files
    before = (output / "quest-build-preparation.json").read_bytes()
    with pytest.raises(FileExistsError):
        baseline.prepare(source, output)
    assert (output / "quest-build-preparation.json").read_bytes() == before


def test_archive_tamper_rejected_before_creating_output(tmp_path, monkeypatch):
    source, _ = fixture_source(tmp_path, monkeypatch)
    (source / "upstream/godot.tar").write_bytes(b"tampered")
    output = tmp_path / "new"
    with pytest.raises(ValueError):
        baseline.prepare(source, output)
    assert not output.exists()


def test_git_global_and_inherited_tree_do_not_change_patch_bytes(tmp_path, monkeypatch):
    source, files = fixture_source(tmp_path, monkeypatch)
    config = tmp_path / "global.gitconfig"
    config.write_text("[core]\n    autocrlf = true\n", "utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_WORK_TREE", str(source))
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "absent.git"))
    output = tmp_path / "new"
    baseline.prepare(source, output)
    assert (output / "native-source/godot/engine.txt").read_bytes() == b"after\n"
    assert {name: digest((source / name).read_bytes()) for name in files} == files
    record = json.loads((output / "quest-build-preparation.json").read_text())
    assert record["patched_engine_files"]["engine.txt"] == digest(b"after\n")


@pytest.mark.parametrize("relative", ["input/child", "input"])
def test_output_cannot_modify_input(tmp_path, monkeypatch, relative):
    source, _ = fixture_source(tmp_path, monkeypatch)
    with pytest.raises((ValueError, FileExistsError)):
        baseline.safe_new_output(tmp_path / relative, source)


def test_failed_patch_does_not_mark_source_complete(tmp_path, monkeypatch):
    source, _ = fixture_source(tmp_path, monkeypatch)
    patch = source / "project/patches/review.patch"
    patch.write_bytes(patch.read_bytes().replace(b"-before", b"-unrelated"))
    manifest = source / "source-manifest.json"
    data = json.loads(manifest.read_text())
    data["files"]["project/patches/review.patch"] = digest(patch.read_bytes())
    manifest.write_text(json.dumps(data), "utf-8")
    output = tmp_path / "new"
    with pytest.raises(Exception):
        baseline.prepare(source, output)
    assert not (output / "quest-build-preparation.json").exists()
    assert (source / "upstream/godot.tar").read_bytes() == archive("engine.txt", b"before\n")


@pytest.mark.parametrize("member", ["../outside", "/absolute", "NUL.txt"])
def test_source_archive_escape_rejected(tmp_path, member):
    target = tmp_path / "source"
    with pytest.raises(ValueError):
        baseline.host_tool.extract_archive(archive(member, b"payload"), target)
    assert not target.exists()

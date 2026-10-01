"""Public rebrand boundaries: exact source, UI-only changes and preserved state."""
import importlib.util
import json
from pathlib import Path
import sys

import pytest

RELEASE = Path(__file__).resolve().parents[1] / "scripts/release"
sys.path.insert(0, str(RELEASE))
spec = importlib.util.spec_from_file_location("quest_brand_source", RELEASE / "prepare_quest_brand_source.py")
brand = importlib.util.module_from_spec(spec)
spec.loader.exec_module(brand)
sys.path.remove(str(RELEASE))


def inputs(tmp_path):
    source, overlay = tmp_path / "source", tmp_path / "overlay"
    source.mkdir()
    project = source / "project"
    (project / "src").mkdir(parents=True)
    for name in brand.ALLOWED:
        (project / name).write_text('var product_title = "Quest3D"\n', "utf-8")
    (project / "project.godot").write_text('[application]\nconfig/name="Nightfall"\n', "utf-8")
    (project / "user-state-schema.txt").write_text("preserved settings schema", "utf-8")
    files = {p.relative_to(source).as_posix(): brand.sha(p) for p in source.rglob("*") if p.is_file()}
    (source / "source-manifest.json").write_text(json.dumps({"files": files}), "utf-8")
    records = {}
    for name in brand.ALLOWED:
        target = overlay / "files" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('var product_title = "Sterevi"\n', "utf-8")
        records[name] = {"before_sha256": brand.sha(project / name), "sha256": brand.sha(target)}
    (overlay / "manifest.json").write_text(json.dumps({"base_source_manifest_sha256": brand.sha(source / "source-manifest.json"),
                                                       "display_name": "Sterevi", "files": records}), "utf-8")
    return source, overlay


def test_rebrand_keeps_original_source_and_state_paths(tmp_path):
    source, overlay = inputs(tmp_path)
    original = {p: p.read_bytes() for p in source.rglob("*") if p.is_file()}
    output = tmp_path / "new-brand"
    result = brand.prepare(source, overlay, output)
    assert result["brand_overlay_files"] == 2 and not result["native_changed"]
    assert all(p.read_bytes() == data for p, data in original.items())
    assert (output / "project/project.godot").read_bytes() == (source / "project/project.godot").read_bytes()
    assert (output / "project/user-state-schema.txt").read_bytes() == (source / "project/user-state-schema.txt").read_bytes()
    assert all('"Sterevi"' in (output / "project" / name).read_text("utf-8") for name in brand.ALLOWED)
    assert not json.loads((output / "source-manifest.json").read_text())["clean_build_verified"]
    with pytest.raises(FileExistsError):
        brand.prepare(source, overlay, output)


@pytest.mark.parametrize("change", ["baseline", "hash", "logic", "extra-file"])
def test_unreviewed_source_or_non_ui_delta_rejected_before_output(tmp_path, change):
    source, overlay = inputs(tmp_path)
    manifest_path = overlay / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    name = sorted(brand.ALLOWED)[0]
    target = overlay / "files" / name
    if change == "baseline":
        manifest["base_source_manifest_sha256"] = "0" * 64
    elif change == "hash":
        target.write_text('var product_title = "changed"\n')
    elif change == "logic":
        target.write_text('var product_title = "Sterevi"\nvar protocol = "changed"\n')
        manifest["files"][name]["sha256"] = brand.sha(target)
    else:
        (overlay / "files/native.cpp").write_text("unreviewed native", "utf-8")
    manifest_path.write_text(json.dumps(manifest), "utf-8")
    output = tmp_path / "rejected"
    with pytest.raises(ValueError):
        brand.prepare(source, overlay, output)
    assert not output.exists()

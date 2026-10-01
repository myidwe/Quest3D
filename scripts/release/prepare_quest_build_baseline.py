"""Materialize reviewed Quest inputs in a new, isolated source-build candidate.

No signing, downloads, installation, or release approval. The prior source
candidate and installed app are immutable inputs; rebuilt files belong only
to this new candidate. An existing editor may generate the bindings API in a
later build, but its use is explicitly not a clean editor rebuild.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess


def sibling(name: str):
    spec = importlib.util.spec_from_file_location("quest_baseline_" + name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


source_tool = sibling("prepare_quest_source")
host_tool = sibling("prepare_host_source")
PATCHES = (
    "godot-4.7-ahb.patch", "godot-4.7-projectionless.patch",
    "godot-4.7-projectionless-lifecycle.patch", "godot-4.7-compositor-filter.patch",
    "godot-4.7-submission-observer.patch", "godot-4.7-submission-thread-guard.patch",
    "godot-4.7-gles-render-target-ownership.patch", "godot-4.7-rec709-color-space.patch",
)


def safe_new_output(path: Path, source: Path) -> Path:
    target = Path(os.path.abspath(path))
    for node in (target, *target.parents):
        if node.is_symlink() or (hasattr(node, "is_junction") and node.is_junction()):
            raise ValueError("Build output parent contains a link or junction")
    if target.exists():
        raise FileExistsError("Use a new Quest build candidate directory")
    if target.is_relative_to(source) or source.is_relative_to(target):
        raise ValueError("Build candidate must be separate from its source input")
    return target


def public_preset(raw: str, version_code: int = 1, display_name: str = "Quest3D") -> str:
    """A single unsigned release preset; reject ambiguous or absent identity."""
    if type(version_code) is not int or not 1 <= version_code <= 2100000000:
        raise ValueError("Invalid Android version code")
    if (not isinstance(display_name, str) or display_name != display_name.strip() or
            not re.fullmatch(r"[A-Za-z][A-Za-z0-9 ._-]{0,63}", display_name)):
        raise ValueError("Invalid public application display name")
    parts = re.split(r"(?m)^\[preset\.\d+\]\s*$", raw)
    if len(parts) < 2:
        raise ValueError("Android export preset missing")
    preset = parts[1]
    preset = re.sub(r"(?m)^\[preset\.\d+\.options\]$", "[preset.0.options]", preset)
    replacements = {
        "name": '"Quest3DPublicReview"', "export_path": '"./Quest3D-public-review-unsigned.apk"',
        "version/code": str(version_code), "version/name": '"0.1.0-review"',
        "package/unique_name": '"app.questto3d.client"', "package/name": '"' + display_name + '"',
        "package/signed": "false", "graphics/opengl_debug": "false",
    }
    for key, value in replacements.items():
        preset, count = re.subn(r"(?m)^" + re.escape(key) + r"=.*$", key + "=" + value, preset)
        if count != 1:
            raise ValueError("Ambiguous or missing export field: " + key)
    preset = re.sub(r"(?m)^(keystore/[^=]+)=.*$", r'\1=""', preset)
    return "[preset.0]\n" + preset.lstrip("\n")


def git_apply(directory: Path, patch: Path, *, check: bool) -> None:
    # A developer's global core.autocrlf changed patched bytes in an isolated
    # fixture. External Git configuration and inherited Git state must not
    # define release source inputs.
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    args = ["git", "-c", "core.autocrlf=false", "-c", "core.safecrlf=false",
            "-C", str(directory), "apply"]
    if check:
        args.append("--check")
    subprocess.run([*args, str(patch)], check=True, capture_output=True, env=env)


def prepare(source: Path, output: Path) -> dict:
    source = source.resolve(strict=True)
    verification = source_tool.verify_manifest(source)
    provenance = json.loads((source / "SOURCE_PROVENANCE.json").read_text("utf-8"))
    expected_archives = provenance.get("cached_inputs", {}).get("source_archives", {})
    for component in ("godot", "godot-cpp"):
        archive = source / "upstream" / (component + ".tar")
        source_tool.checked(archive, source)
        if source_tool.sha(archive) != expected_archives.get(component, {}).get("sha256"):
            raise ValueError("Upstream archive hash differs: " + component)
    output = safe_new_output(output, source)
    output.mkdir(parents=True)
    manifest = json.loads((source / "source-manifest.json").read_text("utf-8"))
    files = manifest["files"]
    copied = {}
    for name, record in files.items():
        if not name.startswith("project/"):
            continue
        origin = source_tool.checked(source / name, source)
        expected = record["sha256"] if isinstance(record, dict) else record
        data = origin.read_bytes()
        if hashlib.sha256(data).hexdigest() != expected:
            raise ValueError("Source changed during baseline copy")
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        copied[name] = expected
    engine = output / "native-source/godot"
    cpp = output / "native-source/godot-cpp"
    for component, destination in (("godot", engine), ("godot-cpp", cpp)):
        archive = source / "upstream" / (component + ".tar")
        data = archive.read_bytes()
        if hashlib.sha256(data).hexdigest() != expected_archives[component]["sha256"]:
            raise ValueError("Archive changed during extraction")
        host_tool.extract_archive(data, destination)
    patch_records = {}
    for name in PATCHES:
        path = output / "project/patches" / name
        git_apply(engine, path, check=True)
        git_apply(engine, path, check=False)
        patch_records[name] = source_tool.sha(path)
    changed_engine_files = {}
    for name in PATCHES:
        patch = output / "project/patches" / name
        for relative in re.findall(r"(?m)^\+\+\+ b/(.+)$", patch.read_text("utf-8")):
            safe = host_tool.bundle.relative_name(relative)
            changed_engine_files[safe] = source_tool.sha(engine / safe)
    preset = output / "project/export_presets.cfg"
    before = source_tool.sha(preset)
    preset.write_text(public_preset(preset.read_text("utf-8")), "utf-8", newline="\n")
    record = {
        "schema": 1, "kind": "isolated-quest-source-build-baseline", "published": False,
        "source_manifest_sha256": source_tool.sha(source / "source-manifest.json"),
        "input_verification": verification, "copied_project_files": copied,
        "engine_commit": expected_archives["godot"]["commit"],
        "godot_cpp_commit": expected_archives["godot-cpp"]["commit"],
        "upstream_archives": {key: expected_archives[key] for key in ("godot", "godot-cpp")},
        "expected_static_link_inputs": provenance.get("cached_inputs", {}).get("static_link_inputs", {}),
        "engine_patch_hashes": patch_records,
        "patched_engine_files": changed_engine_files,
        "preparation_helper_sha256": source_tool.sha(Path(__file__)),
        "public_preset_change": {"before_sha256": before, "after_sha256": source_tool.sha(preset),
                                 "package": "app.questto3d.client", "signed": False},
        "source_preserved": source_tool.verify_manifest(source),
        "clean_editor_build_verified": False, "clean_engine_build_verified": False,
        "native_build_verified": False, "full_apk_build_verified": False,
        "dependency_notices_verified": False, "public_release_ready": False,
    }
    source_tool.json_write(output / "quest-build-preparation.json", record)
    return {"prepared": True, "project_files": len(copied), "engine_patches": len(patch_records),
            "public_release_ready": False}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    print(json.dumps(prepare(args.source, args.output), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

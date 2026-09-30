"""Prepare a new unsigned Android source export without altering prior builds.

Use verified r3 project source, freshly rebuilt own native libraries, the
original pinned vendor distribution and Android template. Engine runtime is
injected only after its independent fresh release build passes. This remains
a build investigation, not a release approval or device installation.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import zipfile

from prepare_quest_build_baseline import public_preset, safe_new_output, source_tool, host_tool

VENDOR_SHA = "b68135657f64fea782cac3efe3e5cef7a4de13e4215f1b621ce6a49fe32592eb"
VENDOR_MEMBERS = (
    "addons/godotopenxrvendors/.bin/android/template_release/arm64/libgodotopenxrvendors.so",
    "addons/godotopenxrvendors/.bin/android/release/godotopenxr-meta-release.aar",
    "addons/godotopenxrvendors/.bin/linux/template_release/x86_64/libgodotopenxrvendors.so",
    "addons/godotopenxrvendors/.bin/linux/template_debug/x86_64/libgodotopenxrvendors.so",
)


def extract_template(archive: Path, target: Path) -> dict:
    files = {}
    with zipfile.ZipFile(archive) as value:
        names = set()
        for member in value.infolist():
            name = host_tool.bundle.relative_name(member.filename.rstrip("/"))
            if name.casefold() in names or ((member.external_attr >> 16) & 0o170000) == 0o120000:
                raise ValueError("Template duplicate or symlink member")
            names.add(name.casefold())
        for member in value.infolist():
            path = target / host_tool.bundle.relative_name(member.filename.rstrip("/"))
            if member.is_dir():
                path.mkdir(parents=True, exist_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(value.read(member))
                files[member.filename] = source_tool.sha(path)
    return files


def complete_template_installation(output: Path, vendor: Path) -> dict:
    """Reproduce the stamp written by Godot's Android template installation.

    This is a generated build input, not an APK source restoration. The exact
    pinned Linux debug vendor input lets the headless editor load its export
    extension; the APK still selects the Android release native input.
    """
    if source_tool.sha(vendor) != VENDOR_SHA:
        raise ValueError("Pinned official vendor ZIP changed")
    stamp = output / "project/android/.build_version"
    if stamp.exists() and stamp.read_bytes() != b"4.7.stable\n":
        raise ValueError("Existing Android template stamp differs")
    stamp.parent.mkdir(parents=True, exist_ok=True)
    if not stamp.exists():
        stamp.write_bytes(b"4.7.stable\n")
    name = VENDOR_MEMBERS[-1]
    target = output / "project" / name
    with zipfile.ZipFile(vendor) as archive:
        raw = archive.read("asset/" + name)
    if target.exists() and target.read_bytes() != raw:
        raise ValueError("Existing editor vendor input differs")
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_bytes(raw)
    result = {"schema": 1, "template_version": "4.7.stable",
              "android_build_version_sha256": source_tool.sha(stamp),
              "editor_vendor": {"path": "project/" + name, "sha256": source_tool.sha(target), "bytes": len(raw)},
              "vendor_zip_sha256": VENDOR_SHA, "public_release_ready": False}
    receipt = output / "template-installation-stamp.json"
    if receipt.exists():
        raise FileExistsError("Preserve previous template installation receipt")
    source_tool.json_write(receipt, result)
    return result


def prepare(source: Path, native: Path, vendor: Path, template: Path, output: Path) -> dict:
    source, native = source.resolve(strict=True), native.resolve(strict=True)
    manifest = source_tool.verify_manifest(source)
    build = json.loads((native / "native-build-summary.json").read_text("utf-8"))
    if build.get("native_build_verified") is not True or build.get("exit_code") != 0:
        raise ValueError("Own native build must pass first")
    if source_tool.sha(vendor) != VENDOR_SHA:
        raise ValueError("Pinned official vendor ZIP changed")
    output = safe_new_output(output, source)
    if output.is_relative_to(native) or native.is_relative_to(output):
        raise ValueError("Android export must be separate from native build")
    output.mkdir(parents=True)
    project = output / "project"
    project_files = {}
    for name, expected in manifest["files"].items():
        if not name.startswith("project/"):
            continue
        file = source_tool.checked(source / name, source)
        if source_tool.sha(file) != expected:
            raise ValueError("Project source changed during copy")
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(file, target)
        project_files[name] = expected
    descriptor = project / source_tool.XR_DESCRIPTOR
    if not descriptor.is_file():
        raise ValueError("Native XR source descriptor missing")
    own_native = {}
    for name in (
        "project/addons/nightfall-stream/bin/android/libnightfall-stream.android.template_release.arm64.so",
        "project/extensions/nightfall-xr/bin/android/libnightfall-xr.android.template_release.arm64.so",
    ):
        expected = build["outputs"][name]
        file = native / name
        if source_tool.sha(file) != expected["sha256"]:
            raise ValueError("Rebuilt native changed before export")
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(file, target)
        own_native[name] = expected
    preset = project / "export_presets.cfg"
    preset.write_text(public_preset(preset.read_text("utf-8")), "utf-8", newline="\n")
    vendor_files = {}
    with zipfile.ZipFile(vendor) as archive:
        for name in VENDOR_MEMBERS:
            member = "asset/" + name
            if len([entry for entry in archive.infolist() if entry.filename == member]) != 1:
                raise ValueError("Pinned vendor entry missing or duplicated")
            target = project / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(member))
            vendor_files[name] = {"sha256": source_tool.sha(target), "bytes": target.stat().st_size}
    template_files = extract_template(template, project / "android/build")
    gradle = project / "android/build/build.gradle"
    before_gradle = source_tool.sha(gradle)
    # Match the loader actually used by the preserved, working development
    # APK. The stock 4.7 exporter requests 1.1.53; strict Gradle selection
    # prevents that request from changing this project's audited loader pin.
    with gradle.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write('\n// Quest3D exact Khronos loader pin from the preserved APK.\n'
                     'dependencies {\n'
                     '    implementation("org.khronos.openxr:openxr_loader_for_android:1.1.54") {\n'
                     '        version { strictly "1.1.54" }\n'
                     '    }\n'
                     '}\n')
    for name in ("GodotApp.java", "CodecCapabilityDiagnostics.java"):
        file = project / "android/src/main/java/com/godot/game" / name
        target = project / "android/build/src/main/java/com/godot/game" / name
        if not file.is_file():
            raise ValueError("Current custom Android source missing")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(file, target)
    meta = project / VENDOR_MEMBERS[1]
    target = project / "android/build/libs/release" / meta.name
    shutil.copyfile(meta, target)
    template_installation = complete_template_installation(output, vendor)
    source_tool.json_write(output / "android-export-preparation.json", {
        "schema": 1, "kind": "isolated-unsigned-public-android-export", "published": False,
        "source_manifest_sha256": source_tool.sha(source / "source-manifest.json"),
        "project_files": project_files, "own_rebuilt_native": own_native,
        "vendor_zip_sha256": VENDOR_SHA, "vendor_inputs": vendor_files,
        "vendor_built_from_source": False,
        "android_template_sha256": source_tool.sha(template), "android_template_files": template_files,
        "loader_pin_overlay": {"version": "1.1.54", "file": "project/android/build/build.gradle",
                               "before_sha256": before_gradle, "after_sha256": source_tool.sha(gradle)},
        "template_installation": template_installation,
        "godot_java_aar_rebuilt_from_source": False, "signed": False,
        "package": "app.questto3d.client", "public_release_ready": False,
    })
    return {"prepared": True, "project_files": len(project_files), "own_native": len(own_native),
            "public_release_ready": False}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("source", "native", "vendor", "template", "output"):
        parser.add_argument("--" + key, type=Path, required=True)
    args = parser.parse_args(argv)
    print(json.dumps(prepare(args.source, args.native, args.vendor, args.template, args.output), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Read-only APK identity, permissions and fresh-native correspondence checks.

Creates a separate evidence directory. It never signs, installs, runs ADB,
modifies an APK, or treats a build as a hardware/publication approval.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import zipfile


def sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def identity(badging: str) -> dict:
    package = re.search(r"^package: name='([^']+)' versionCode='([^']+)' versionName='([^']+)'", badging, re.M)
    minimum = re.search(r"^sdkVersion:'([^']+)'", badging, re.M)
    target = re.search(r"^targetSdkVersion:'([^']+)'", badging, re.M)
    compile_sdk = re.search(r"compileSdkVersion='([^']+)'", badging)
    if not package or not minimum or not target:
        raise ValueError("Missing actual APK package/SDK metadata")
    return {
        "package": package[1], "version_code": package[2], "version_name": package[3],
        "min_sdk": int(minimum[1]), "target_sdk": int(target[1]),
        "compile_sdk": int(compile_sdk[1]) if compile_sdk else None,
        "debuggable": bool(re.search(r"^application-debuggable", badging, re.M)),
        "permissions": re.findall(r"^uses-permission: name='([^']+)'", badging, re.M),
    }


def verify(export: Path, rebuilt: Path, vendor: Path, tools: Path, output: Path, attempt: str = "first",
           public_vendor: Path | None = None, version_code: int = 1) -> dict:
    if type(version_code) is not int or not 1 <= version_code <= 2100000000:
        raise ValueError("Invalid Android version code")
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", attempt):
        raise ValueError("Invalid export attempt")
    apk = export / "Quest3D-public-review-unsigned.apk"
    exported = json.loads((export / ("apk-export-" + attempt + "-summary.json")).read_text())
    if sha(vendor) != "b68135657f64fea782cac3efe3e5cef7a4de13e4215f1b621ce6a49fe32592eb":
        raise ValueError("Unpinned official vendor ZIP")
    native = json.loads((rebuilt / "native-build-summary.json").read_text())
    engine = json.loads((rebuilt / "engine-build-summary.json").read_text())
    assert exported["exit_code"] == 0 and exported["full_project_export_verified"] is True
    assert native["exit_code"] == 0 and native["native_build_verified"] is True
    assert engine["exit_code"] == 0 and engine["clean_android_engine_build_verified"] is True
    assert sha(apk) == exported["apk"]["sha256"]
    if output.exists():
        raise FileExistsError("Preserve previous APK evidence")
    if output.resolve().is_relative_to(export.resolve()) or output.resolve().is_relative_to(rebuilt.resolve()):
        raise ValueError("Evidence output must be separate from build inputs")
    output.mkdir(parents=True)
    bt = tools / "android-sdk/build-tools/36.1.0"
    ndkbin = tools / "android-sdk/ndk/29.0.14206865/toolchains/llvm/prebuilt/linux-x86_64/bin"
    environment = os.environ.copy()
    environment["JAVA_HOME"] = str(tools / "linux/jdk-17.0.20.1+1")
    environment["PATH"] = environment["JAVA_HOME"] + "/bin:" + environment.get("PATH", "")
    commands = []

    def run(argv: list[str], label: str, expected: int | None = 0) -> subprocess.CompletedProcess:
        completed = subprocess.run(argv, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=environment)
        (output / (label + ".txt")).write_text(completed.stdout)
        commands.append({"operation": label, "argv": [Path(a).name if a.startswith("/") else a for a in argv], "exit_code": completed.returncode})
        if expected is not None and completed.returncode != expected:
            raise RuntimeError(label + " failed; see evidence log")
        return completed

    badging = run([str(bt / "aapt"), "dump", "badging", str(apk)], "apk-badging").stdout
    metadata = identity(badging)
    preparation = json.loads((export / "android-export-preparation.json").read_text())
    assert metadata["package"] == "app.questto3d.client"
    assert preparation.get("version_code", 1) == version_code
    assert metadata["version_code"] == str(version_code) and metadata["version_name"] == preparation.get("version_name", "0.1.0-review")
    assert not metadata["debuggable"]
    run([str(bt / "aapt"), "dump", "xmltree", str(apk), "AndroidManifest.xml"], "apk-manifest")
    signed = run([str(bt / "apksigner"), "verify", "--verbose", "--print-certs", str(apk)], "apk-signature", None)
    assert signed.returncode != 0 and "DOES NOT VERIFY" in signed.stdout, "Unexpected signed APK"
    aligned = run([str(bt / "zipalign"), "-c", "-P", "16", "-v", "4", str(apk)], "apk-alignment", None)
    binding = {}
    with zipfile.ZipFile(apk) as archive:
        names = archive.namelist()
        assert len(names) == len(set(names))
        unwanted = [n for n in names if n.endswith((".so", ".dll", ".aar", ".dylib")) and not n.startswith("lib/arm64-v8a/")]
        assert not unwanted, "Editor/non-Android binary payload leaked into APK"
        sizes = [{"path": n, "bytes": archive.getinfo(n).file_size, "compressed_bytes": archive.getinfo(n).compress_size, "compression": archive.getinfo(n).compress_type}
                 for n in names if n.startswith("lib/") or n.endswith(".dex")]
        own_inputs = {
            "libgodot_android.so": (rebuilt / "runtime/libgodot_android.so", engine["outputs"]["libgodot_android.so"]["sha256"]),
            "libc++_shared.so": (rebuilt / "runtime/libc++_shared.so", engine["outputs"]["libc++_shared.so"]["sha256"]),
        }
        for name, record in native["outputs"].items():
            if "/build/" not in name:
                own_inputs[Path(name).name] = (rebuilt / name, record["sha256"])
        for name, (raw, expected) in own_inputs.items():
            assert sha(raw) == expected
            member = "lib/arm64-v8a/" + name
            packaged = output / name
            packaged.write_bytes(archive.read(member))
            stripped = output / ("stripped-" + name)
            shutil.copyfile(raw, stripped)
            run([str(ndkbin / "llvm-strip"), "--strip-unneeded", str(stripped)], "strip-" + name)
            source_id = run([str(ndkbin / "llvm-readelf"), "--notes", str(raw)], "source-notes-" + name).stdout
            apk_id = run([str(ndkbin / "llvm-readelf"), "--notes", str(packaged)], "apk-notes-" + name).stdout
            raw_build_id = re.findall(r"Build ID: (\w+)", source_id)
            apk_build_id = re.findall(r"Build ID: (\w+)", apk_id)
            binding[name] = {"raw_sha256": expected, "stripped_sha256": sha(stripped), "apk_sha256": sha(packaged),
                             "raw_matches_apk": expected == sha(packaged), "stripped_matches_apk": sha(stripped) == sha(packaged),
                             "raw_build_ids": raw_build_id, "apk_build_ids": apk_build_id,
                             "build_ids_match": bool(raw_build_id) and raw_build_id == apk_build_id}
            assert binding[name]["stripped_matches_apk"], "APK differs from freshly rebuilt native after exact strip"
        vendor_name = "libgodotopenxrvendors.so"
        with zipfile.ZipFile(vendor) as distribution:
            raw_vendor = output / ("raw-" + vendor_name)
            raw_vendor.write_bytes(distribution.read("asset/addons/godotopenxrvendors/.bin/android/template_release/arm64/" + vendor_name))
        if public_vendor:
            review = json.loads((public_vendor / "vendor-build-summary.json").read_text())
            assert review["exit_code"] == 0 and review["vendor_built_from_source"] is True
            assert review["meta_preview_headers_selected"] is False
            selected = public_vendor / review["native"]["path"]
            assert selected.resolve().is_relative_to(public_vendor.resolve())
            assert sha(selected) == review["native"]["sha256"]
            assert preparation["public_vendor_binding"]["native_sha256"] == sha(selected)
            shutil.copyfile(selected, raw_vendor)
        vendor_stripped = output / ("stripped-" + vendor_name)
        shutil.copyfile(raw_vendor, vendor_stripped)
        run([str(ndkbin / "llvm-strip"), "--strip-unneeded", str(vendor_stripped)], "strip-" + vendor_name)
        packaged_vendor = archive.read("lib/arm64-v8a/" + vendor_name)
        binding[vendor_name] = {"official_zip_sha256": sha(vendor), "raw_sha256": sha(raw_vendor), "stripped_sha256": sha(vendor_stripped),
                               "apk_sha256": hashlib.sha256(packaged_vendor).hexdigest(), "rebuilt_from_source": bool(public_vendor),
                               "meta_preview_headers_selected": False if public_vendor else None}
        assert sha(vendor_stripped) == binding[vendor_name]["apk_sha256"]
        loader_candidates = list((export / "cache/gradle/caches/modules-2/files-2.1/org.khronos.openxr/openxr_loader_for_android/1.1.54").rglob("*.aar"))
        assert len(loader_candidates) == 1
        with zipfile.ZipFile(loader_candidates[0]) as loader:
            loader_raw = loader.read("jni/arm64-v8a/libopenxr_loader.so")
        loader_apk = archive.read("lib/arm64-v8a/libopenxr_loader.so")
        assert loader_raw == loader_apk
        binding["libopenxr_loader.so"] = {"coordinate": "org.khronos.openxr:openxr_loader_for_android:1.1.54", "aar_sha256": sha(loader_candidates[0]),
                                          "apk_sha256": hashlib.sha256(loader_apk).hexdigest(), "exact_aar_native_match": True}
    result = {"schema": 1, "apk_sha256": sha(apk), "apk_bytes": apk.stat().st_size, "metadata": metadata,
              "native_binding_verified": True, "native": binding, "members": sizes,
              "unexpected_editor_binary_members": unwanted, "unsigned_verified": True,
              "zip_alignment_16k_verified": aligned.returncode == 0, "commands": commands,
              "signed": False, "installed": False, "hardware_verified": False, "published": False,
              "tool_sha256": {name: sha(file) for name, file in
                              (("aapt", bt / "aapt"), ("apksigner", bt / "apksigner"), ("zipalign", bt / "zipalign"),
                               ("llvm-strip", ndkbin / "llvm-strip"), ("llvm-readelf", ndkbin / "llvm-readelf"))},
              "dependency_notices_verified": False, "public_release_ready": False}
    result["public_vendor_binding"] = preparation.get("public_vendor_binding")
    (output / "apk-correspondence.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("export", "rebuilt", "vendor", "tools", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--attempt", default="first")
    parser.add_argument("--public-vendor", type=Path)
    parser.add_argument("--version-code", type=int, default=1)
    args = parser.parse_args()
    result = verify(args.export, args.rebuilt, args.vendor, args.tools, args.output, args.attempt, args.public_vendor, args.version_code)
    print(json.dumps({k: result[k] for k in ("apk_sha256", "metadata", "native_binding_verified", "unsigned_verified", "zip_alignment_16k_verified", "public_release_ready")}, indent=2))


if __name__ == "__main__":
    main()

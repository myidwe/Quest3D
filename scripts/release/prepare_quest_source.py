"""Prepare the latest reviewed Quest source without changing any installed APK.

This produces a candid source candidate, never a publication approval. The
legacy project missed the independently built mDNS delta; only the three exact
recorded source hashes may repair that gap in the new copy.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[2]
APK_SHA = "81a9fc0f3008e95bbdb10f17d0421bd364475ee532cb14341e9d3007803f5042"
PACKAGE = "app.questto3d.client.debug"
CERTIFICATE_SHA = "a0c962041dd1a2154f07ffb5dd44867f8789d721a876c4540e2d9b70a9809dee"
NATIVE_SHA = {
    "libc++_shared.so": "c4c2fe5cbcb1fba0003a31fc7ab29a9bb12df6cc187ec45a806462540e83d93b",
    "libgodot_android.so": "759493fde5ac3bce2ab742a037b85b45bfaee19af7bc853262608310c20aa719",
    "libgodotopenxrvendors.so": "b5d9d5fd2e578282396b056493c471a850add5baa3faffb7b9ef99c7400f5611",
    "libnightfall-stream.android.template_release.arm64.so": "d859bc403577edab14b00042f9f3c57ae098089ef8e68cb7e13c22ab2a8cdddf",
    "libnightfall-xr.android.template_debug.arm64.so": "831d8f5350aadc2994fd6e0458f44e734a8780b9c6539d9e74e86210192f91a7",
    "libopenxr_loader.so": "12b10b0d6f3a93c52f9aaf8b0d2452771eeff6e6d0852fe0ecc16412c6b9c311",
}
MDNS_SHA = {
    "src/network/mdns_browser.cpp": "e2711dee3814b37379232841906641c5dda099fd937114ff3c850afb1a3f769d",
    "src/network/mdns_browser.h": "39772190cd657a57744d1691639ff28769e0de3000c7a74244bd44fbe67fdae7",
    "src/network/mdns_parser.h": "dd6c5f1b5e3e1829d56136c902f1b08859e14494db2e1870e82d61950a2f628f",
}
SKIP_DIRS = {".git", ".godot", ".bin", ".cache", ".build-cache", "build", "bin", "signing", "vcpkg_installed", "models", "captures", "screenshots", "__pycache__"}
SECRET_NAMES = {".env", "app_state.cfg", "host_state.cfg", "config.ini", "credentials.json", "desktop.json", "sunshine_state.json"}
SECRET_SUFFIXES = {".key", ".pem", ".jks", ".keystore", ".p12", ".pfx", ".clixml", ".log", ".jsonl"}
SOURCE_SUFFIXES = {".py", ".ps1", ".sh", ".h", ".hpp", ".c", ".cpp", ".cmake", ".json", ".md", ".txt", ".gd", ".gdshader", ".gdshaderinc", ".tscn", ".tres", ".godot", ".gdextension", ".cfg", ".java", ".xml", ".gradle", ".properties", ".in", ".yml", ".yaml", ".bat", ".cmd", ".patch", ".uid", ".svg", ".png", ".ico", ".ttf", ".otf", ".toml", ".lock"}
ARCHIVES = (
    "ffmpeg-ffmpeg-n7.1.2.tar.gz", "curl-curl-curl-8_17_0.tar.gz",
    "openssl-openssl-openssl-3.6.0.tar.gz", "godotengine-godot-cpp-godot-4.4-stable.tar.gz",
    "madler-zlib-v1.3.1.tar.gz", "xiph-opus-v1.5.2.tar.gz",
    "simd-everywhere-simde-v0.8.2.tar.gz",
    "moonlight-stream-moonlight-common-c-7b026e77be62175104640e7e722b758df6d3d0d7.tar.gz",
    "cgutman-enet-dea6fb5414b180908b58c0293c831105b5d124dd.tar.gz",
)
XR_DESCRIPTOR = "extensions/nightfall-xr/bin/nightfall-xr.gdextension"
SOURCE_DIRECTORY_EXCEPTIONS = {XR_DESCRIPTOR, XR_DESCRIPTOR + ".uid"}


def sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def safe_name(name: str) -> str:
    if not name or "\\" in name or ":" in name or "\x00" in name:
        raise ValueError("Unsafe source path")
    path = PurePosixPath(name)
    if path.is_absolute() or any(p in {"", ".", ".."} or p.endswith((" ", ".")) for p in name.split("/")):
        raise ValueError("Unsafe source path")
    if any(re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", part) for part in path.parts):
        raise ValueError("Windows reserved source path")
    if path.name.lower() in SECRET_NAMES or path.suffix.lower() in SECRET_SUFFIXES:
        raise ValueError("Private source payload rejected")
    descriptor_name = name.removeprefix("project/")
    if any(part.lower() in SKIP_DIRS for part in path.parts) and descriptor_name not in SOURCE_DIRECTORY_EXCEPTIONS:
        raise ValueError("Cache or binary source payload rejected")
    return path.as_posix()


def checked(path: Path, root: Path) -> Path:
    absolute = Path(os.path.abspath(path))
    base = root.resolve(strict=True)
    if not absolute.is_relative_to(base):
        raise ValueError("Source path escapes its reviewed root")
    for node in (absolute, *absolute.parents):
        if node.is_symlink() or (hasattr(node, "is_junction") and node.is_junction()):
            raise ValueError("Source link or junction rejected")
        if node == base:
            break
    if not absolute.is_file():
        raise ValueError("Source file missing")
    return absolute


def json_write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def sanitize_export(raw: bytes) -> bytes:
    text = raw.decode("utf-8")
    # Godot export settings are source; credentials and machine-local keystore
    # paths are not. Package/version settings remain those of the audited APK.
    text = re.sub(r'(?m)^(keystore/(?:debug|debug_user|debug_password|release|release_user|release_password))=.*$', r'\1=""', text)
    return text.encode("utf-8")


def copy_project(project: Path, output: Path) -> tuple[int, list[str]]:
    count, sanitized = 0, []
    for parent, dirs, files in os.walk(project, followlinks=False):
        relative_parent = Path(parent).relative_to(project).as_posix()
        dirs[:] = sorted(d for d in dirs if d.lower() not in SKIP_DIRS or
                         (relative_parent == "extensions/nightfall-xr" and d == "bin"))
        if relative_parent == "extensions/nightfall-xr/bin":
            dirs[:] = []
        for name in sorted(files):
            file = Path(parent) / name
            relative = file.relative_to(project).as_posix()
            if relative_parent == "extensions/nightfall-xr/bin" and relative not in SOURCE_DIRECTORY_EXCEPTIONS:
                continue
            if name.lower() in SECRET_NAMES or file.suffix.lower() in SECRET_SUFFIXES:
                continue
            license_name = name.upper().startswith(("LICENSE", "LICENCE", "COPYING", "NOTICE"))
            if file.suffix.lower() not in SOURCE_SUFFIXES and not license_name and name not in {"CMakeLists.txt", ".gitignore", ".env.example"}:
                continue
            safe_name(relative)
            checked(file, project)
            destination = output / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            raw = file.read_bytes()
            if name == "export_presets.cfg":
                replacement = sanitize_export(raw)
                if replacement != raw:
                    sanitized.append(relative)
                raw = replacement
            destination.write_bytes(raw)
            count += 1
    return count, sanitized


def restore_xr_descriptor(apk: Path, source: Path) -> dict:
    """The installed APK keeps this plain-text source asset, not compiled code.

    Historical delta projects omitted the source-controlled bin descriptor.
    Recover its exact bytes from the already pinned APK; never reconstruct a
    UID absent from that APK or admit arbitrary files below bin.
    """
    name = "assets/" + XR_DESCRIPTOR
    with zipfile.ZipFile(apk) as archive:
        matches = [entry for entry in archive.infolist() if entry.filename == name]
        if len(matches) != 1 or matches[0].file_size > 16384:
            raise ValueError("Pinned APK XR source descriptor missing or ambiguous")
        raw = archive.read(matches[0])
    text = raw.decode("utf-8")
    for required in ('entry_symbol = "nightfall_xr_library_init"',
                     'android.arm64.single.debug = "./android/libnightfall-xr.android.template_debug.arm64.so"',
                     'android.arm64.single.release = "./android/libnightfall-xr.android.template_release.arm64.so"'):
        if required not in text:
            raise ValueError("Pinned APK XR source descriptor differs")
    target = source / "project" / XR_DESCRIPTOR
    if target.exists() and target.read_bytes() != raw:
        raise ValueError("Preserved XR source descriptor differs from pinned APK")
    restored = not target.exists()
    target.parent.mkdir(parents=True, exist_ok=True)
    if restored:
        target.write_bytes(raw)
    return {"path": "project/" + XR_DESCRIPTOR, "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw), "restored": restored,
            "authority": "Exact plain-text source asset in pinned development APK",
            "apk_sha256": sha(apk), "uid_reconstructed": False}


def verify_apk(apk: Path) -> dict[str, str]:
    if sha(apk) != APK_SHA:
        raise ValueError("Latest reviewed APK hash mismatch")
    with zipfile.ZipFile(apk) as archive:
        names = archive.namelist()
        if len(set(names)) != len(names):
            raise ValueError("Duplicate APK entries")
        native_names = {name for name in names if name.startswith("lib/") and name.endswith(".so")}
        if native_names != {"lib/arm64-v8a/" + name for name in NATIVE_SHA}:
            raise ValueError("Unexpected native APK paths or ABI")
        actual = {PurePosixPath(name).name: hashlib.sha256(archive.read(name)).hexdigest() for name in native_names}
    if actual != NATIVE_SHA:
        raise ValueError("Unexpected native APK payload")
    return actual


def verify_manifest(source: Path) -> dict:
    manifest = json.loads((source / "source-manifest.json").read_text("utf-8"))
    expected = manifest["files"]
    actual = set()
    for file in source.rglob("*"):
        if file.is_symlink() or (hasattr(file, "is_junction") and file.is_junction()):
            raise ValueError("Source link or junction rejected")
        if file.is_file() and file != source / "source-manifest.json":
            name = file.relative_to(source).as_posix()
            safe_name(name)
            checked(file, source)
            actual.add(name)
            if sha(file) != expected.get(name):
                raise ValueError("Source manifest hash mismatch")
    if actual != set(expected):
        raise ValueError("Source manifest membership mismatch")
    return manifest


def collect_cached_inputs(root: Path, cache: Path, source: Path, include_archives: bool) -> dict:
    records = {"notices": {}, "source_archives": {}, "native_matches": {}, "static_link_inputs": {}, "packages": []}
    share = cache / "source/addons/nightfall-stream/build/android/vcpkg_installed/arm64-android/share"
    for package in ("curl", "ffmpeg", "godot-cpp", "moonlight-common-c", "openssl", "opus", "simde", "zlib"):
        file = share / package / "copyright"
        if file.is_file():
            checked(file, cache)
            target = source / "licenses" / (package + ".txt")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(file, target)
            records["notices"][package] = sha(target)
    ndk = cache / "android-sdk/ndk/29.0.14206865"
    for name in ("NOTICE", "NOTICE.toolchain"):
        file = ndk / name
        if file.is_file():
            target = source / "licenses" / ("Android-NDK-r29-" + name + ".txt")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(checked(file, cache), target)
            records["notices"][name] = sha(target)
    status = share.parent.parent / "vcpkg/status"
    if status.is_file():
        for block in status.read_text().split("\n\n"):
            fields = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
            if "Package" in fields and "Version" in fields and "Feature" not in fields:
                records["packages"].append({"name": fields["Package"], "version": fields["Version"]})
    aar_root = cache / "gradle/caches/modules-2/files-2.1/org.khronos.openxr/openxr_loader_for_android/1.1.54"
    for aar in sorted(aar_root.glob("*/*.aar")):
        with zipfile.ZipFile(aar) as archive:
            name = "jni/arm64-v8a/libopenxr_loader.so"
            if name in archive.namelist():
                value = hashlib.sha256(archive.read(name)).hexdigest()
                records["native_matches"]["libopenxr_loader.so"] = {
                    "source": "org.khronos.openxr:openxr_loader_for_android:1.1.54",
                    "aar_sha256": sha(aar), "sha256": value, "matches_apk": value == NATIVE_SHA["libopenxr_loader.so"],
                    "license": "Apache-2.0", "url": "https://repo.maven.apache.org/maven2/org/khronos/openxr/openxr_loader_for_android/1.1.54/",
                }
    lib_root = share.parent / "lib"
    prior = json.loads((root / "artifacts/quest/quest3-scan-20260914/native/link-inputs.json").read_text())
    for original, value in prior.items():
        if original.endswith(".a"):
            name = PurePosixPath(original).name
            file = lib_root / name
            actual = sha(file) if file.is_file() else None
            records["static_link_inputs"][name] = {"sha256": actual, "recorded_sha256": value["sha256"], "matches_recorded": actual == value["sha256"]}
    if include_archives:
        for name in ARCHIVES:
            file = cache / "vcpkg/downloads" / name
            if file.is_file():
                target = source / "upstream/static-dependencies" / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(checked(file, cache), target)
                records["source_archives"][name] = {"sha256": sha(target), "bytes": target.stat().st_size, "source": "Preserved vcpkg source download used by recorded dependency build; upstream port hashes require final verification"}
        for name, pin in (("godot", "5b4e0cb0fd279832bbdd69fed5354d4e5ad26f88"), ("godot-cpp", "05057de73de4b99f114d36c40d84ca46926c0e25")):
            repository = cache / "native-xr" / name
            target = source / "upstream" / (name + ".tar")
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as stream:
                environment = dict(os.environ, GIT_NO_LAZY_FETCH="1", GIT_TERMINAL_PROMPT="0")
                subprocess.run(["git", "-c", "core.excludesFile=/dev/null", "-C", str(repository), "archive", "--format=tar", pin], stdout=stream, stderr=subprocess.PIPE, env=environment, check=True)
            records["source_archives"][name] = {"commit": pin, "sha256": sha(target), "bytes": target.stat().st_size}
    return records


def attach_native_evidence(root: Path, evidence: Path, source: Path, collected: dict) -> None:
    """Copy measured sidecars only after binding them to this exact APK."""
    vendor = json.loads(checked(evidence / "vendor-input-check/verification.json", root).read_text())
    native = json.loads(checked(evidence / "native-input-check/verification.json", root).read_text())
    if vendor.get("download_sha256") != "b68135657f64fea782cac3efe3e5cef7a4de13e4215f1b621ce6a49fe32592eb" or vendor.get("stripped_sha256") != NATIVE_SHA["libgodotopenxrvendors.so"] or vendor.get("stripped_matches_apk") is not True:
        raise ValueError("Vendor evidence does not match the reviewed APK")
    for name in ("libgodot_android.so", "libc++_shared.so"):
        if native.get(name, {}).get("stripped_sha256") != NATIVE_SHA[name] or native[name].get("matches_apk") is not True:
            raise ValueError("Engine or NDK evidence does not match the reviewed APK")
    license_file = checked(evidence / "vendor-input-check/Godot-OpenXR-Vendors-MIT.txt", root)
    if sha(license_file) != "6133abb9ee2e3752675df201d5a5c0942a0762558b35d2471ebfae72cad82b61":
        raise ValueError("Vendor license evidence mismatch")
    (source / "licenses").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(license_file, source / "licenses/Godot-OpenXR-Vendors-MIT.txt")
    project = root / "artifacts/quest/level-settings-20260930/project"
    record = {
        "schema": 1, "apk_sha256": APK_SHA, "native_libraries": NATIVE_SHA,
        "copied_source_file_license_additions": ["project/addons/godotopenxrvendors/meta/LICENSE-LOADER", "project/addons/godotopenxrvendors/meta/LICENSE-SDK"],
        "vendor": vendor, "native_stripped_inputs": native,
        "openxr_loader": collected.get("native_matches", {}).get("libopenxr_loader.so", {}),
        "static_link_input_count": len(collected.get("static_link_inputs", {})),
        "static_link_all_match": bool(collected.get("static_link_inputs")) and all(value["matches_recorded"] for value in collected["static_link_inputs"].values()),
        "stream_mdns_repaired_source": MDNS_SHA,
        "xr_library_exact_matches_project": sha(project / "extensions/nightfall-xr/bin/android/libnightfall-xr.android.template_debug.arm64.so") == NATIVE_SHA["libnightfall-xr.android.template_debug.arm64.so"],
        "source_clean_rebuild_verified": False, "all_android_dependencies_verified": False, "public_release_ready": False,
    }
    json_write(source / "NATIVE_INPUT_VERIFICATION.json", record)


def prepare(root: Path, output: Path, cache: Path | None = None, include_archives: bool = False, evidence: Path | None = None) -> dict:
    # Explicit reviewed sources only; the working checkout is used solely for
    # the three mDNS files whose historical build hashes are pinned above.
    project = root / "artifacts/quest/level-settings-20260930/project"
    build = project.parent / "build"
    if output.exists():
        raise ValueError("Existing output is preserved; choose a new directory")
    if not output.resolve().is_relative_to((root / "artifacts/publication").resolve()):
        raise ValueError("Output must stay inside workspace artifacts/publication")
    verify_apk(build / "candidate-signed.apk")
    exported = json.loads((build / "export-sources.json").read_text())
    for name, expected in exported.items():
        safe_name(name)
        if sha(checked(project / name, project)) != expected:
            raise ValueError("Reviewed product source changed")
    source = output / "public-source"
    source.mkdir(parents=True)
    count, sanitized = copy_project(project, source / "project")
    descriptor = restore_xr_descriptor(build / "candidate-signed.apk", source)
    baseline = json.loads((root / "artifacts/quest/quest3-scan-20260914/native/source-baseline.json").read_text())
    stream = source / "project/addons/nightfall-stream"
    for name, record in baseline.items():
        if sha(stream / safe_name(name)) != record["published_sha256"]:
            raise ValueError("Unexpected stream baseline source")
    overlay = {}
    for name, expected in MDNS_SHA.items():
        file = checked(root / "third_party/nightfall/addons/nightfall-stream" / name, root)
        if sha(file) != expected:
            raise ValueError("Exact historical mDNS delta unavailable")
        target = stream / name
        target.parent.mkdir(parents=True, exist_ok=True)
        before = sha(target) if target.is_file() else None
        shutil.copyfile(file, target)
        overlay[name] = {"baseline_sha256": before, "candidate_sha256": expected}
    for name in ("versions.lock.json", "downloads.lock.json", "env.sh", "setup_sdk.sh", "prepare.sh", "bootstrap.py", "build_native.sh", "build_xr.sh", "build_stream.sh", "build_apk.sh"):
        target = source / "build-tools" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(checked(root / "scripts/quest" / name, root), target)
    collected = collect_cached_inputs(root, cache, source, include_archives) if cache else {}
    if evidence:
        attach_native_evidence(root, evidence, source, collected)
    apk_metadata = {"package": PACKAGE, "version_code": 1, "version_name": "1.0.0",
                    "certificate_sha256": CERTIFICATE_SHA, "signing_kind": "development"}
    provenance = {
        "schema": 1, "apk_sha256": APK_SHA, "package": PACKAGE, "version_code": 1, "version_name": "1.0.0",
        "certificate_sha256": CERTIFICATE_SHA, "native_components": NATIVE_SHA,
        "apk_metadata": apk_metadata,
        "project_files_copied": count, "sanitized_source_files": sanitized,
        "xr_source_descriptor": descriptor,
        "compiled_recent_scripts_verified": sorted(exported), "mdns_source_overlay": overlay,
        "source_complete": False, "clean_build_verified": False, "dependency_notices_verified": False,
        "public_release_ready": False, "cached_inputs": collected,
        "remaining_gaps": [
            "All compiled APK scripts and DEX need a full clean source build comparison, beyond the eight recorded recent scripts.",
            "Exported project lacked the actual mDNS source delta; this candidate repairs only the three exact verified files.",
            "Preserved upstream/static source archive hashes must be checked against their original fetch/build recipes.",
            "All Android runtime dependencies, vendor source and notices must be audited and included before APK release.",
            "Public package/version/signing identity and first public update preservation are not yet finalized.",
        ],
    }
    json_write(source / "SOURCE_PROVENANCE.json", provenance)
    (source / "SOURCE_BUILD_NOTES.md").write_text(
        "# Latest Quest source candidate\n\n"
        "This manifest binds the reviewed development APK to the complete copied project source and explicit mDNS repair. "
        "It does not certify full corresponding-source completeness or a clean public release build. "
        "SOURCE_PROVENANCE.json records actual evidence and unresolved gates.\n\n"
        "No APK, signing key, user preference, pairing record or model weight is copied. "
        "project/export_presets.cfg keeps the historical debug package/version but clears signing fields. "
        "Use a separately built public package and an external private signing identity for public release; "
        "do not re-sign the installed development APK or silently uninstall it.\n\n"
        "Build-tools are preserved historical inputs whose workspace layout must be adapted for a clean source build. "
        "They are not advertised as a verified one-command clean build.\n", encoding="utf-8")
    files = {file.relative_to(source).as_posix(): sha(file) for file in sorted(source.rglob("*")) if file.is_file()}
    manifest = {"version": 1, "schema": 1, "apk_sha256": APK_SHA, "package": PACKAGE,
                "apk_metadata": apk_metadata,
                "source_complete": False, "clean_build_verified": False, "dependency_notices_verified": False,
                "public_release_ready": False, "files": files}
    json_write(source / "source-manifest.json", manifest)
    verify_manifest(source)
    report = {"pass": True, "apk_unchanged": sha(build / "candidate-signed.apk") == APK_SHA,
              "source_manifest_sha256": sha(source / "source-manifest.json"), "source_file_count": len(files),
              "source_complete": False, "public_release_ready": False, "source": "public-source"}
    json_write(output / "preparation-result.json", report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--build-cache", type=Path)
    parser.add_argument("--include-native-source-archives", action="store_true")
    parser.add_argument("--native-evidence", type=Path, help="Reviewed sidecar directory with vendor and engine/NDK strip comparisons")
    parser.add_argument("--verify", type=Path)
    args = parser.parse_args()
    if args.verify:
        value = verify_manifest(args.verify.resolve())
        print(json.dumps({"pass": True, "files": len(value["files"]), "source_complete": value["source_complete"]}))
    else:
        if not args.output or (args.include_native_source_archives and not args.build_cache):
            parser.error("--output is required; native source archives also require --build-cache")
        print(json.dumps(prepare(args.root.resolve(), args.output.resolve(), args.build_cache.resolve() if args.build_cache else None, args.include_native_source_archives, args.native_evidence.resolve() if args.native_evidence else None)))


if __name__ == "__main__":
    main()

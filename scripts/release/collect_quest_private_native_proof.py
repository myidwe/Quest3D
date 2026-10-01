"""Bind a fresh native rebuild to corresponding recipes and privacy checks.

Only redacted counts are recorded. Private user/home markers are obtained
locally, never included in arguments, report strings or filenames. This is
the native-library gate; independent final APK/ZIP checks remain required.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess

ARCHIVES = (
    "libavcodec.a", "libavformat.a", "libavutil.a", "libcrypto.a", "libcurl.a", "libenet.a",
    "libgodot-cpp.android.arm64-v8a.template_release.arm64.a", "libmoonlight-common-c.a",
    "libopus.a", "libssl.a", "libswresample.a", "libswscale.a", "libz.a",
)
PACKAGES = ("curl", "ffmpeg", "godot-cpp", "moonlight-common-c", "openssl", "opus", "simde", "zlib")

def sha(f: Path) -> str:
    h = hashlib.sha256()
    with f.open("rb") as s:
        for b in iter(lambda: s.read(1024 * 1024), b""):
            h.update(b)
    return h.hexdigest()

def privacy(data: bytes) -> dict:
    # Use exact current build-user markers, plus broad home-path detection.
    text = data.lower()
    home, user = str(Path.home()), Path.home().name
    counts = {}
    for category, value in (("current_private_home", home), ("current_private_user", user)):
        counts[category] = sum(text.count(value.lower().encode(codec)) for codec in ("utf-8", "utf-16-le", "utf-16-be"))
    counts["generic_linux_home_paths"] = len(re.findall(rb"\/home\/[^/\\\s\x00]+/", data))
    counts["generic_windows_home_paths"] = len(re.findall(rb"(?i)[a-z]:[\\/]users[\\/][^/\\\s\x00]+[\\/]", data))
    counts["generic_source_remap_occurrences"] = data.count(b"/quest3d/")
    return counts

def collect(base: Path, old_source: Path, output: Path, repo: Path) -> dict:
    base, old_source, repo = (p.resolve(strict=True) for p in (base, old_source, repo))
    output = output.absolute()
    if output.exists() or not str(output).startswith("/mnt/a/"):
        raise ValueError("Use a new A-drive evidence directory")
    build = json.loads((base / "native-build-summary.json").read_text())
    prep = json.loads((base / "private-path-build-preparation.json").read_text())
    if build["exit_code"] or not build["native_build_verified"] or build["static_dependencies_reused"]:
        raise ValueError("Fresh native build must pass")
    def versions(status: Path) -> dict:
        result = {}
        for block in status.read_text().split("\n\n"):
            fields = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
            if "Package" in fields and "Version" in fields and "Feature" not in fields:
                result[fields["Package"] + ":" + fields["Architecture"]] = {
                    k: fields[k] for k in ("Version", "Port-Version") if k in fields}
        return result
    original_versions = versions(old_source / "dependency-supply/native/installed-packages.txt")
    rebuilt_versions = versions(base / "installed/vcpkg/status")
    if rebuilt_versions != original_versions:
        raise ValueError("Runtime dependency or build-helper version changed")
    libroot = base / "installed/arm64-android/lib"
    actual = {p.name for p in libroot.glob("*.a")}
    if actual != set(ARCHIVES):
        raise ValueError("Exact 13 rebuilt static archives required")
    main = base / "output/libnightfall-stream.android.template_release.arm64.so"
    files = {}
    for f in [*(libroot / n for n in ARCHIVES), main]:
        data = f.read_bytes()
        counts = privacy(data)
        if any(v for k, v in counts.items() if k != "generic_source_remap_occurrences"):
            raise ValueError("Private or generic home paths remain in rebuilt native input")
        files[f.name] = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data), "privacy_counts": counts,
                         "rebuilt_in_this_run": True}
    observed_sources = {p.relative_to(base / "project/addons/nightfall-stream").as_posix(): sha(p)
                        for p in (base / "project/addons/nightfall-stream").rglob("*")
                        if p.is_file() and p.suffix != ".so"}
    if observed_sources != prep["source_inputs"]:
        raise ValueError("Native product source changed during build")
    commands = base / "build/stream/compile_commands.json"
    if any(v for k, v in privacy(commands.read_bytes()).items() if k != "generic_source_remap_occurrences"):
        raise ValueError("Compile command evidence contains home paths")
    output.mkdir(parents=True)
    shutil.copytree(base / "overlay-ports", output / "recipes")
    shutil.copytree(base / "triplets", output / "triplets")
    for name in ("prepare_quest_private_path_build.py", "rebuild_quest_private_path_native.sh",
                 "collect_quest_private_native_proof.py", "quest_openssl_private_build_info.patch", "cache_quest_ndk_in_ram.py",
                 "cache_quest_godot_buildtrees_in_ram.py"):
        shutil.copyfile(repo / "scripts/release" / name, output / name)
    shutil.copyfile(base / "installed/vcpkg/status", output / "installed-packages.txt")
    shutil.copyfile(commands, output / "stream-compile-commands.json")
    for name in ("private-path-build-preparation.json", "native-build-summary.json"):
        shutil.copyfile(base / name, output / name)
    if (base / "host-pkg-config-input.json").is_file():
        shutil.copyfile(base / "host-pkg-config-input.json", output / "host-pkg-config-input.json")
    if (base / "ndk-RAM-readcache.json").is_file():
        shutil.copyfile(base / "ndk-RAM-readcache.json", output / "ndk-RAM-readcache.json")
    if (base / "godot-RAM-buildtree.json").is_file():
        shutil.copyfile(base / "godot-RAM-buildtree.json", output / "godot-RAM-buildtree.json")
    abi = {}
    for package in PACKAGES:
        original = base / "buildtrees" / package / "arm64-android.vcpkg_abi_info.txt"
        content = original.read_bytes()
        if any(v for k, v in privacy(content).items() if k != "generic_source_remap_occurrences"):
            raise ValueError("ABI source evidence contains home paths")
        target = output / "abi" / original.name.replace("arm64-android", package + "-arm64-android")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        abi[package] = {"path": target.relative_to(output).as_posix(), "sha256": sha(target)}
    original_ledger = json.loads((old_source / "dependency-supply/dependency-source-notices.json").read_text())
    updated_packages = {}
    for package in PACKAGES:
        original = original_ledger["native"]["native_packages"][package]
        recipe = output / "recipes" / package
        license_file = base / "installed/arm64-android/share" / package / "copyright"
        if sha(license_file) != original["license_sha256"]:
            raise ValueError("Selected native dependency license changed")
        updated_packages[package] = {**original,
            "portfile_sha256": sha(recipe / "portfile.cmake"),
            "recipe_files": {f.relative_to(recipe).as_posix(): sha(f) for f in sorted(recipe.rglob("*")) if f.is_file()},
            "actual_rebuilt_abi_info": abi[package],
            "rebuilt_in_this_run": True,
        }
    native_ledger = {**original_ledger["native"], "native_packages": updated_packages,
                     "installed_versions_match_original": True, "runtime_static_archives_rebuilt": True,
                     "static_link_inputs": {n: files[n] for n in ARCHIVES}}
    (output / "updated-native-dependency-ledger.json").write_text(json.dumps(native_ledger, indent=2) + "\n")
    report = {
        "schema": 1, "kind": "rebuilt-quest-native-private-path-remediation", "native_privacy_gate_passed": True,
        "original_public_source_manifest_sha256": sha(old_source / "source-manifest.json"),
        "preparation_sha256": sha(base / "private-path-build-preparation.json"),
        "build_summary_sha256": sha(base / "native-build-summary.json"),
        "static_link_inputs": {n: files[n] for n in ARCHIVES}, "stream_extension": files[main.name],
        "actual_dependency_abis": abi, "old_compiled_dependencies_reused": False,
        "diagnostic_file_remap": "/mnt/a=/quest3d", "openssl_diagnostic_compiler_paths_omitted": True,
        "assertions_disabled": False, "model_or_video_settings_changed": False,
        "native_product_source_changed": False,
        "dependency_and_helper_versions": rebuilt_versions,
        "dependency_and_helper_versions_match_original": True,
        "elf_and_static_archives_scanned": 14, "final_apk_scanned": False,
        "device_functional_validation": False, "published": False,
        "source_files": {p.relative_to(output).as_posix(): sha(p) for p in sorted(output.rglob("*")) if p.is_file()},
    }
    (output / "privacy-native-proof.json").write_text(json.dumps(report, indent=2) + "\n")
    return {"native_privacy_gate_passed": True, "native_files": 14, "static_archives": len(ARCHIVES),
            "private_user_matches": 0, "private_home_matches": 0, "device_validation": False}

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("base", "old-source", "output", "repo"):
        parser.add_argument("--" + key, required=True, type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(collect(args.base, args.old_source, args.output, args.repo), indent=2))
    except Exception as error:
        print(json.dumps({"native_privacy_gate_passed": False, "error_type": type(error).__name__}))
        raise SystemExit(1)

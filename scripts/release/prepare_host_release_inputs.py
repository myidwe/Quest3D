"""Supply a newly built host's exact source and notices in fresh directories.

This never replaces the development executable, installs a service or promotes
hardware validation. It separates source completeness from runtime validation.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tarfile


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(data)
    return h.hexdigest()


def checked(path: Path, missing: bool = False) -> Path:
    path = Path(os.path.abspath(path))
    for part in (path, *path.parents):
        try:
            item = part.lstat()
        except FileNotFoundError:
            if missing:
                continue
            raise
        if stat.S_ISLNK(item.st_mode) or getattr(item, "st_file_attributes", 0) & 0x400:
            raise ValueError("Redirected source or destination")
    return path


def relative_name(name: str) -> str:
    if not name or name.startswith(("/", "\\")) or "\\" in name or ":" in name:
        raise ValueError("Unsafe supplied member name")
    if any(p in ("", ".", "..") or p.rstrip(" .") != p or any(ord(c) < 32 for c in p) or
           re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", p)
           for p in name.split("/")):
        raise ValueError("Unsafe supplied member name")
    return name


def put_file(destination: Path, original: Path, expected: str | None = None):
    checked(original)
    checked(destination, missing=True)
    if not original.is_file():
        raise ValueError("Regular source input required")
    identity = sha(original)
    if expected and identity != expected:
        raise ValueError("Source input differs from its exact authority")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("xb") as output, original.open("rb") as source:
        shutil.copyfileobj(source, output)
    if sha(destination) != identity:
        raise ValueError("Supplied file differs after copy")


def put_text(destination: Path, text: str):
    checked(destination, missing=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(text)


def json_file(destination: Path, content: dict):
    put_text(destination, json.dumps(content, ensure_ascii=False, indent=2) + "\n")


def inventory(root: Path) -> dict:
    result = {}
    for parent, dirs, files in os.walk(checked(root), followlinks=False):
        for name in dirs:
            checked(Path(parent) / name)
        for name in sorted(files):
            path = checked(Path(parent) / name)
            relative = relative_name(path.relative_to(root).as_posix())
            if any(relative.casefold() == existing.casefold() for existing in result):
                raise ValueError("Supplied path alias")
            result[relative] = {"sha256": sha(path), "bytes": path.stat().st_size}
    return dict(sorted(result.items()))


def source_archive(source: Path, expected: dict, output: Path) -> dict:
    """Archive only the frozen source inputs, excluding npm/build outputs."""
    seen = set()
    for name, identity in expected.items():
        name = relative_name(name)
        if name.casefold() in seen:
            raise ValueError("Aliased source identity")
        seen.add(name.casefold())
        original = checked(source / name)
        if sha(original) != identity:
            raise ValueError("Actual source differs from archive/overlay/patch authority")
    with tarfile.open(output, "x:gz", format=tarfile.PAX_FORMAT) as archive:
        for name in sorted(expected):
            original = source / name
            entry = tarfile.TarInfo(name)
            entry.size = original.stat().st_size
            entry.mode = 0o755 if name.endswith(".sh") else 0o644
            entry.mtime = 0
            with original.open("rb") as stream:
                archive.addfile(entry, stream)
    # Verify every actual archived byte rather than trusting addfile success.
    with tarfile.open(output, "r:gz") as archive:
        actual = {}
        for entry in archive:
            if not entry.isfile() or entry.name in actual:
                raise ValueError("Unexpected supplied source archive member")
            relative_name(entry.name)
            data = archive.extractfile(entry).read()
            actual[entry.name] = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise ValueError("Supplied source archive differs from frozen build input")
    return {"file_count": len(actual), "sha256": sha(output),
            "every_member_sha_verified": True, "source_files": expected,
            "node_modules_or_build_outputs_included": False}


def collect_notices(root: Path, candidate: Path, source: Path, supplied: Path,
                    dependency_review: Path, statics: Path, web_overrides: Path) -> dict:
    """Preserve actual copyright/license texts alongside dependency identities."""
    notices = supplied / "notices"
    expected = json.loads((supplied / "source-file-inventory.json").read_text(encoding="utf-8"))["source_files"]
    host_licenses = []
    for name in sorted(expected):
        if re.fullmatch(r"(?i)(licen[cs]e|copying|copyright|notice)(?:[._-].*)?", Path(name).name):
            original = source / name
            if original.stat().st_size > 4 * 1024 * 1024:
                raise ValueError("Unexpectedly large host license")
            put_file(notices / "host-and-vendored" / name, original, expected[name])
            host_licenses.append(name)
    # These header-only/source dependencies are outside the Sunshine tree.
    for original in sorted((supplied / "dependencies").iterdir()):
        if not original.is_file() or not any(original.name.endswith(suffix) for suffix in (".tar", ".tar.xz", ".tar.gz")):
            continue
        with tarfile.open(original) as archive:
            for entry in archive:
                if entry.isfile() and re.fullmatch(r"(?i)(licen[cs]e|copying|copyright|notice)(?:[._-].*)?", Path(entry.name).name):
                    relative_name(entry.name)
                    data = archive.extractfile(entry).read()
                    if len(data) > 4 * 1024 * 1024:
                        raise ValueError("Unexpectedly large dependency license")
                    destination = notices / "source-dependencies" / original.name / entry.name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with destination.open("xb") as stream:
                        stream.write(data)
    evidence = json.loads((dependency_review / "dependency-evidence.json").read_text(encoding="utf-8"))
    bsdtar = root / "native/host/tools/msys64/usr/bin/bsdtar.exe"
    msys = []
    for component in evidence["msys_link_inputs"]:
        name = component["package"]["NAME"].removeprefix("mingw-w64-ucrt-x86_64-")
        archive = root / "native/host/tools/msys64/var/cache/pacman/pkg" / component["package_file"]
        if sha(archive) != component["locked_package_sha256"]:
            raise ValueError("Actual runtime package differs from frozen toolchain")
        names = subprocess.run([str(bsdtar), "-tf", str(archive)],
                               capture_output=True, check=True, timeout=60).stdout.decode("utf-8").splitlines()
        texts = [n for n in names if "/share/licenses/" in n and not n.endswith("/")]
        count = 0
        for name_in_archive in texts:
            relative_name(name_in_archive)
            data = subprocess.run([str(bsdtar), "-xOf", str(archive), name_in_archive],
                                  capture_output=True, check=True, timeout=60).stdout
            if len(data) > 4 * 1024 * 1024:
                raise ValueError("Unexpected runtime license size")
            target = notices / "msys" / name / Path(name_in_archive).relative_to("ucrt64/share/licenses")
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as stream:
                stream.write(data)
            count += 1
        if name == "MinHook":
            c = json.loads((statics / "source-collection.json").read_text(encoding="utf-8"))
            record = next(item for item in c["components"] if item["name"] == name)
            outer = statics / record["archive"]["path"]
            raw = subprocess.run([str(bsdtar), "-xOf", str(outer), "mingw-w64-MinHook/MinHook-1.3.4.tar.gz"],
                                 capture_output=True, check=True, timeout=60).stdout
            with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
                entry = next(e for e in tar if e.name.endswith("/LICENSE.txt") and e.isfile())
                data = tar.extractfile(entry).read()
            target = notices / "msys/MinHook/LICENSE.txt"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            count += 1
        if name == "libidn2":
            for item in sorted((dependency_review / "copyleft-sources/attempt-03/libidn2/license-evidence").iterdir()):
                put_file(notices / "msys/libidn2" / item.name, item)
                count += 1
        if name == "gcc":
            # GCC runtime exception is an applicable-file boundary. Copy the
            # actual installed runtime notices, not arbitrary compiler docs.
            for item in sorted((root / "native/host/tools/msys64/ucrt64/share/licenses/gcc-libs").iterdir()):
                put_file(notices / "msys/gcc-runtime" / item.name, item)
                count += 1
        if count == 0:
            raise ValueError("Linked runtime dependency lacks a preserved license: " + name)
        msys.append({"name": name, "version": component["package"]["VERSION"],
                     "license": component["package"]["LICENSE"], "license_files": count,
                     "binary_package_sha256": component["locked_package_sha256"]})
    lock = json.loads((source / "package-lock.json").read_text(encoding="utf-8"))
    web = []
    for module, item in sorted(lock["packages"].items()):
        if not module.startswith("node_modules/") or item.get("dev"):
            continue
        package = checked(source / module)
        metadata = json.loads((package / "package.json").read_text(encoding="utf-8"))
        if metadata["version"] != item["version"]:
            raise ValueError("Actual web dependency differs from package lock")
        license_files = [p for p in package.iterdir() if p.is_file() and re.match(
            r"(?i)^(licen[cs]e|copying|notice|copyright)(?:$|[._-])", p.name)]
        if metadata["name"] == "@vue/devtools-api":
            license_files = [web_overrides / "vue--devtools-api.txt"]
        if not license_files:
            raise ValueError("Runtime web dependency license missing: " + metadata["name"])
        key = metadata["name"].replace("/", "--").replace("@", "")
        for item_file in license_files:
            put_file(notices / "web" / key / item_file.name, item_file)
        web.append({"name": metadata["name"], "version": metadata["version"],
                    "license": item.get("license"), "package_url": item["resolved"],
                    "integrity": item["integrity"], "license_files": len(license_files)})
    report = {"schema": 1, "host_binary_sha256": sha(candidate / "cmake-build-compat-candidate/sunshine.exe"),
              "host_and_vendored_license_files": host_licenses, "msys_runtime_dependencies": msys,
              "web_runtime_dependencies": web, "web_lock_sha256": sha(source / "package-lock.json"),
              "dependency_notices_verified": True,
              "compiler_and_build_tools_bundled": False,
              "system_runtime_boundary": ["Windows SDK/OS DLLs are not redistributed.",
                 "MinGW CRT/Windows POSIX threading are ordinary platform runtime components; notices retained.",
                 "GCC libstdc++/libgcc use their applicable GCC Runtime Library Exception 3.1; notices retained.",
                 "Node, npm, CMake, Ninja and GCC build executables are build tools, not shipped product payload."],
              "source_package_signatures_verified": False}
    report["web_license_external_bindings"] = [{"name": "@vue/devtools-api", "version": "6.6.4",
        "source_commit": "df6ab6bb7791a7a525a97990de73b3ea5e9a1941",
        "license_url": "https://raw.githubusercontent.com/vuejs/vue-devtools/df6ab6bb7791a7a525a97990de73b3ea5e9a1941/LICENSE",
        "sha256": sha(web_overrides / "vue--devtools-api.txt")}]
    json_file(notices / "dependency-notices.json", report)
    text = "# Quest3D host dependency notices\n\n"
    text += "New host 2026.930.1 · GPL-3.0 · corresponding-source archive accompanies this runtime\n\n"
    text += "This host is rebuilt from the supplied source baseline. It is not the historical development executable. "
    text += "See SOURCE.md and provenance.json in the source supply for exact build inputs.\n\n"
    text += "Original license and copyright texts: `host-and-vendored/`, `msys/`, `web/`\n\n"
    text += "| Linked runtime component | Version | Package license metadata |\n|---|---|---|\n"
    for item in msys:
        text += f"| {item['name']} | {item['version']} | {item['license']} |\n"
    text += "\nFFmpeg 38b88335 with GPL-2.0-or-later; x264, x265, SVT-AV1, AMF and NV codec headers: "
    text += "exact source/submodule commits and original notices are included in the Sunshine source archive.\n\n"
    text += "The web UI's production npm dependencies are itemized with exact versions and original license text "
    text += "in `dependency-notices.json` and `web/`. Build-only npm tools are not installed into this runtime.\n"
    put_text(notices / "THIRD_PARTY_NOTICES.md", text)
    return report


def prepare(root: Path, candidate: Path, old_runtime: Path, dependency_review: Path,
            statics: Path, web_overrides: Path, output: Path) -> dict:
    root, candidate, old_runtime, dependency_review, statics, web_overrides = [checked(p) for p in (
        root, candidate, old_runtime, dependency_review, statics, web_overrides)]
    output = checked(output, missing=True)
    if (output.exists() or not output.is_relative_to(root) or any(output.is_relative_to(p)
            for p in (candidate, old_runtime, dependency_review, statics, web_overrides))):
        raise FileExistsError("Use a fresh release-input directory within the workspace")
    provenance = json.loads((candidate / "candidate-provenance.json").read_text(encoding="utf-8"))
    native = json.loads((candidate / "candidate-build-summary.json").read_text(encoding="utf-8"))
    web_build = json.loads((candidate / "web-build-summary.json").read_text(encoding="utf-8"))
    collection = json.loads((statics / "source-collection.json").read_text(encoding="utf-8"))
    input_binding = json.loads((statics / "recipe-input-binding.json").read_text(encoding="utf-8"))
    if native.get("native_build_success") is not True or native.get("tests_passed") is not True or native.get("exit_code") != 0:
        raise ValueError("Successful actual native build and tests required")
    if web_build.get("build_exit") != 0 or web_build.get("dependency_install_exit") != 0:
        raise ValueError("Successful actual web build required")
    if collection.get("source_collection_complete") is not True or input_binding.get("all_verified") is not True:
        raise ValueError("Complete exact dependency source/input binding required")
    exe = candidate / "cmake-build-compat-candidate/sunshine.exe"
    if sha(exe) != native["host_exe_sha256"]:
        raise ValueError("Actual executable differs from successful build report")
    historical = root / "artifacts/publication/host-source-prep-20260930"
    baseline = json.loads((historical / "source-preparation.json").read_text(encoding="utf-8"))
    expected = dict(baseline["source_files"])
    for name, identities in provenance["modified_files"].items():
        if expected[name] != identities["before_sha256"]:
            raise ValueError("Patch before source is not exact authority")
        expected[name] = identities["after_sha256"]
    expected.update(provenance["added_files"])
    # Original upstream AMF bundles unrelated FFmpeg sample binaries/PDBs.
    # Its build-deps target copies only amf/public/include. These binaries and
    # Vulkan test plugins do not occur in the actual Sunshine Ninja deps/link
    # graph. Retain their exact excluded identities rather than republishing
    # unused SDK executables with unrelated redistribution obligations.
    excluded = {}
    for name in list(expected):
        if (name.startswith("third-party/build-deps/third-party/FFmpeg/AMF/Thirdparty/ffmpeg/") or
            name == "third-party/build-deps/third-party/FFmpeg/AMF/Thirdparty/file_to_header/file_to_header.exe" or
            name.startswith("third-party/build-deps/third-party/FFmpeg/Vulkan-Loader/tests/framework/data/")):
            excluded[name] = expected.pop(name)
    output.mkdir(parents=True, exist_ok=False)
    supplied = output / "source-supply"
    runtime = output / "runtime"
    supplied.mkdir()
    source = candidate / "source"
    archive_report = source_archive(source, expected, supplied / "sunshine-modified.tar.gz")
    json_file(supplied / "source-file-inventory.json", archive_report)
    json_file(supplied / "excluded-unused-upstream-inputs.json", {"schema": 1, "files": excluded,
        "reason": "Unlinked SDK sample FFmpeg prebuilts/PDBs/file-to-header executable and Vulkan loader fixture data; not actual host build inputs.",
        "amf_header_build_recipe": "third-party/build-deps/cmake/ffmpeg/amf.cmake",
        "required_nvapi_import_libraries_retained": True})
    for name, identity in provenance["shared_dependency_archives"].items():
        put_file(supplied / "dependencies" / name, historical / "dependencies" / name, identity["sha256"])
    # Preserve complete source packages, including every recipe patch/signature.
    for item in collection["components"]:
        for key in ("archive", "official_recipe"):
            evidence = item[key]
            put_file(supplied / "dependencies/msys" / item["name"] / Path(evidence["path"]).name,
                     statics / evidence["path"], evidence["sha256"])
    gnu = json.loads((dependency_review / "copyleft-sources/source-collection.json").read_text(encoding="utf-8"))
    for item in gnu["components"]:
        # The old bounded collection has exact .BUILDINFO-bound recipes and
        # source tar checksums, independently revalidated on collection.
        name = item.get("component", item.get("name"))
        if item.get("exact_recipe_and_referenced_sources_collected") is not True:
            raise ValueError("Incomplete GNU source/recipe binding")
        for key in ("msys_source_archive", "official_recipe"):
            evidence = item[key]
            original = dependency_review / "copyleft-sources" / evidence["path"]
            put_file(supplied / "dependencies/msys" / name / original.name, original, evidence["sha256"])
    for name in ("toolchain.lock.json", "audio-packet-api-compat.patch"):
        put_file(supplied / name, candidate / name)
    for name in ("candidate-build-summary.json", "web-build-summary.json", "candidate-regression.xml"):
        put_file(supplied / "build-evidence" / name, candidate / name)
    for name in ("collect_host_dependency_sources.py", "prepare_host_release_inputs.py", "unpack_host_release.py", "rebuild_host_release.sh"):
        put_file(supplied / "tools" / name, root / "scripts/release" / name)
    put_file(supplied / "tools/rebuild_host_compat_candidate.sh", root / "scripts/release/rebuild_host_compat_candidate.sh")
    json_file(supplied / "dependencies/msys/source-bindings.json", {
        "schema": 1, "components": collection["components"], "input_binding": input_binding,
        "gnu_copyleft_source_collection_sha256": sha(dependency_review / "copyleft-sources/source-collection.json"),
        "source_package_signatures_verified": False})
    notice_report = collect_notices(root, candidate, source, supplied, dependency_review, statics, web_overrides)
    public_provenance = {"schema": 1, "kind": "new-host-source-supply",
        "host_binary_sha256": native["host_exe_sha256"], "host_version": provenance["candidate_version"],
        "upstream_commit": provenance["upstream_commit"], "api_compat_patch_sha256": provenance["patch_sha256"],
        "source_archive": "sunshine-modified.tar.gz", "source_archive_sha256": archive_report["sha256"],
        "source_file_inventory_sha256": sha(supplied / "source-file-inventory.json"),
        "source_file_count": len(expected), "binary_source_rebuild_verified": True,
        "source_complete": True, "dependency_notices_verified": True,
        "historical_binary_source_correspondence_verified": False,
        "hardware_validation": False, "quest_stream_validation": False,
        "static_dependency_source_packages": 14,
        "frame_bridge_protocol": 2,
        "file_pcm_flush_scope_implemented": False,
        "dependency_archives": provenance["shared_dependency_archives"],
        "excluded_unused_official_files": len(excluded),
        "runtime_notice_files": {"notices/" + name: "notices/" + name
                                for name in inventory(supplied / "notices")},
        "notes": ["New binary built from this exact assembled source and supplied dependency inputs.",
                  "Existing historical binary is preserved and is not described as rebuilt.",
                  "Runtime integration and Quest visual/audio testing are independent preview limitations."]}
    json_file(supplied / "provenance.json", public_provenance)
    put_text(supplied / "SOURCE.md", "# Quest3D host source\n\n"
             "New host version 2026.930.1. The included archive is the exact modified Sunshine source "
             "used for the successful native and web builds, including all initialized submodule sources. "
             "`source-file-inventory.json` binds every archive member to its original input SHA-256.\n\n"
             "Source archive: `sunshine-modified.tar.gz`. Extract into a new `source` directory. "
             "`dependencies/` supplies pinned Boost, JSON, NV codec headers, the original FFmpeg "
             "input and exact MSYS2 source packages with their patches and recipes. The FFmpeg "
             "source and build-deps recipe are in the modified Sunshine archive. `toolchain.lock.json` "
             "pins build tool and binary dependency downloads with their SHA-256.\n\n"
             "Unlinked AMF sample FFmpeg binaries/PDBs, file-to-header executable and Vulkan test fixture "
             "data are excluded with exact identities in excluded-unused-upstream-inputs.json. Actual "
             "AMF headers and MIT-licensed NVIDIA NVAPI import libraries remain included.\n\n"
             "The supplied MSYS2 source recipes are data; review before executing. GCC runtime, "
             "MinGW CRT and threading are unmodified ordinary compiler/platform runtime components; "
             "their original licenses and applicable GCC Runtime Library Exception are under notices. "
             "Windows OS DLLs and NVIDIA drivers are not redistributed.\n\n"
             "Native build settings: Release, no host tray, no WiX/driver/broker install, six jobs, "
             "version 2026.930.1; see the included tools build helper and actual build reports. "
             "Prepare an empty build workspace with `python tools/unpack_host_release.py --supply . "
             "--output ../host-rebuild`. Then, from MSYS2 UCRT64 with the locked tool versions, run "
             "`QUEST3D_NATIVE_NPM=/path/to/node-v24.20.0-win-x64/npm.cmd bash tools/rebuild_host_release.sh "
             "../host-rebuild 6`. The helper builds and tests; it does not install/start a server. "
             "For the web build use Node v24.20.0 and the unchanged package-lock.json: "
             "npm ci --ignore-scripts --no-audit --no-fund, then build the CMake web-ui target. "
             "Do not set a Codecov token or the LizardByte/Sunshine CI repository identity.\n\n"
             "Frame bridge protocol 2 is the desktop product's current enlarged-monitor input. "
             "Later protocol-3/source-selection/file-PCM experiments are not included in this host baseline. "
             "The desktop product excludes inline 3D, PC pointer injection and built-in file playback. "
             "PC audio is the default; Quest/shared audio uses the retained system-loopback Opus path.\n\n"
             "This is the corresponding source for the new binary identified by provenance.json. "
             "The old development binary's missing historical source is not claimed to be recovered. "
             "Hardware/streaming validation is recorded separately from source completeness.\n")
    runtime.mkdir()
    old_manifest = json.loads((old_runtime / "runtime-preparation.json").read_text(encoding="utf-8"))
    for name, item in old_manifest["files"].items():
        put_file(runtime / relative_name(name), old_runtime / name, item["sha256"])
    for name, item in inventory(supplied / "notices").items():
        put_file(runtime / "notices" / name, supplied / "notices" / name, item["sha256"])
    runtime_files = inventory(runtime)
    runtime_manifest = {"schema": 1, "kind": "host-release-runtime", "host_exe_sha256": native["host_exe_sha256"],
        "files": runtime_files, "source_complete": True, "dependency_notices_verified": True,
        "installed": False, "server_started": False, "hardware_validation": False,
        "historical_binary_source_correspondence_verified": False}
    json_file(runtime / "runtime-preparation.json", runtime_manifest)
    result = {"schema": 1, "kind": "host-release-input", "binary_sha256": native["host_exe_sha256"],
        "runtime_path": runtime.relative_to(root).as_posix(), "runtime_manifest_name": "runtime-preparation.json",
        "runtime_manifest_sha256": sha(runtime / "runtime-preparation.json"), "runtime_files": runtime_files,
        "source_supply_path": supplied.relative_to(root).as_posix(), "files": inventory(supplied),
        "source_complete": True, "notices_verified": True, "native_build_verified": True,
        "hardware_validation": False, "historical_binary_source_correspondence_verified": False,
        "published": False}
    json_file(output / "HOST_RELEASE.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("root", "candidate", "runtime", "dependency-review", "statics", "web-overrides", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    report = prepare(args.root, args.candidate, args.runtime, args.dependency_review,
                     args.statics, args.web_overrides, args.output)
    print(json.dumps({"source_files": len(report["files"]), "runtime_files": len(report["runtime_files"]),
                      "source_complete": report["source_complete"], "binary_sha256": report["binary_sha256"]}, indent=2))


if __name__ == "__main__":
    main()

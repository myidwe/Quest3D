"""Verify release ZIP contents and cross-bind PC, Quest and native sources.

Never extracts, uploads, or promotes a review build to a public release. Archive
integrity is distinct from fresh installation and hardware acceptance.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import stat
import zipfile

spec = importlib.util.spec_from_file_location("asset_export", Path(__file__).with_name("export_repository.py"))
publication = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publication)
bundle = publication.bundle
KINDS = {"Desktop": "pc-installer-candidate", "Quest": "quest-installer-candidate", "Source": "source-candidate"}


def small_json(archive: zipfile.ZipFile, name: str, limit=16 * 1024 * 1024) -> dict:
    info = archive.getinfo(name)
    if info.file_size > limit:
        raise ValueError("Oversized JSON record")
    value = json.loads(archive.read(info))
    if not isinstance(value, dict):
        raise ValueError("JSON record must be an object")
    return value


def inspect_zip(path: Path, release: str, kind: str, private_markers=()) -> dict:
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        if len(infos) > 100000 or sum(info.file_size for info in infos) > 4 * 1024**3:
            raise ValueError("Archive exceeds reviewed distribution bounds")
        seen = set()
        for info in infos:
            bundle.public_name(info.filename)
            if info.is_dir() or info.filename.casefold() in seen or stat.S_ISLNK(info.external_attr >> 16):
                raise ValueError("Unexpected directory, duplicate or symlink ZIP entry")
            seen.add(info.filename.casefold())
        manifest = small_json(archive, "distribution-manifest.json")
        if manifest.get("schema") != 1 or manifest.get("release") != release or manifest.get("metadata", {}).get("kind") != kind:
            raise ValueError("Archive release/kind does not match the reviewed set")
        files = manifest.get("files")
        if not isinstance(files, dict) or not files or {i.filename for i in infos} != set(files) | {"distribution-manifest.json"}:
            raise ValueError("Archive manifest omits or adds files")
        for name, entry in files.items():
            info = archive.getinfo(name)
            with archive.open(info) as stream:
                actual = hashlib.file_digest(stream, "sha256").hexdigest()
            if info.file_size != entry.get("bytes") or actual != entry.get("sha256"):
                raise ValueError(f"Archive content checksum mismatch: {name}")
            # Scan first-party text; binaries and upstream tar contents require
            # their separate dependency/privacy audit, not a UTF-8 guess.
            if Path(name).suffix.casefold() in bundle.SOURCE_SUFFIXES and info.file_size < 16 * 1024 * 1024:
                findings = publication.content_findings(name, archive.read(info), private_markers)
                if findings:
                    raise ValueError("Archive first-party text audit failed: " + json.dumps(findings))
        return {"file": path.name, "sha256": bundle.digest(path), "bytes": path.stat().st_size,
                "payload_files": len(files), "metadata": manifest["metadata"]}


def review(directory: Path, release: str, private_markers=()) -> dict:
    rows = {}
    missing = []
    for label, kind in KINDS.items():
        path = directory / f"Sterevi-{label}-{release}.zip"
        if not path.is_file():
            missing.append(label)
            continue
        rows[label] = inspect_zip(path, release, kind, private_markers)
    binding = False
    source_status = {}
    if not missing:
        with zipfile.ZipFile(directory / rows["Desktop"]["file"]) as pc, zipfile.ZipFile(directory / rows["Quest"]["file"]) as quest, zipfile.ZipFile(directory / rows["Source"]["file"]) as source:
            install = small_json(quest, "quest-install.json")
            quest_source = small_json(source, "sources/quest/source-manifest.json")
            host_source = small_json(source, "sources/sunshine/provenance.json")
            with quest.open("Quest3D-Quest.apk") as stream:
                apk_sha = hashlib.file_digest(stream, "sha256").hexdigest()
            if apk_sha != install.get("sha256") or apk_sha != quest_source.get("apk_sha256") or apk_sha != rows["Quest"]["metadata"].get("apk_sha256"):
                raise ValueError("Quest APK/install/source cross-binding mismatch")
            direct_path = directory / f"Sterevi-Quest-{release}.apk"
            if direct_path.is_file() and bundle.digest(direct_path) != apk_sha:
                raise ValueError("Direct APK differs from the signed Quest installer payload")
            expected = bundle.quest_install_metadata(quest_source, apk_sha)
            if install != expected:
                raise ValueError("Quest install metadata differs from the reviewed APK source")
            config = small_json(pc, "config/distribution.json")
            host_runtime = config.get("host_runtime")
            if config.get("schema") != 1 or not isinstance(host_runtime, str):
                raise ValueError("Invalid PC host layout")
            bundle.public_name(host_runtime)
            with pc.open(host_runtime + "/sunshine.exe") as stream:
                host_sha = hashlib.file_digest(stream, "sha256").hexdigest()
            if any(value != host_sha for value in (host_source.get("host_binary_sha256"),
                    rows["Desktop"]["metadata"].get("host_sha256"), config.get("host_sha256"))):
                raise ValueError("PC host and native source cross-binding mismatch")
            source_files = quest_source.get("files")
            if not isinstance(source_files, dict) or not source_files:
                raise ValueError("Quest corresponding source list is empty")
            for name, entry in source_files.items():
                bundle.public_name(name)
                if Path(name).suffix.casefold() in {".apk", ".so", ".dll", ".gdc", ".pck"}:
                    raise ValueError("Compiled Quest payload is not corresponding source")
                with source.open("sources/quest/" + name) as stream:
                    actual = hashlib.file_digest(stream, "sha256").hexdigest()
                if actual != (entry if isinstance(entry, str) else entry["sha256"]):
                    raise ValueError(f"Quest corresponding source mismatch: {name}")
            quest_notices = quest_source.get("binary_notice_files", [])
            if not isinstance(quest_notices, list) or len(quest_notices) != len(set(quest_notices)):
                raise ValueError("Invalid Quest binary notice inventory")
            for name in quest_notices:
                bundle.public_name(name)
                if name not in source_files:
                    raise ValueError("Quest binary notice is not supplied as source")
                try:
                    with quest.open("notices/quest/" + name) as stream:
                        actual = hashlib.file_digest(stream, "sha256").hexdigest()
                except KeyError as exc:
                    raise ValueError("Quest binary notice was not delivered") from exc
                entry = source_files[name]
                if actual != (entry if isinstance(entry, str) else entry["sha256"]):
                    raise ValueError("Quest binary notice differs from its reviewed source")
            host_notices = host_source.get("runtime_notice_files", {})
            if not isinstance(host_notices, dict):
                raise ValueError("Invalid host runtime notice inventory")
            for runtime_name, source_name in host_notices.items():
                bundle.public_name(runtime_name)
                bundle.public_name(source_name)
                try:
                    with pc.open(host_runtime + "/" + runtime_name) as stream:
                        runtime_sha = hashlib.file_digest(stream, "sha256").hexdigest()
                    with source.open("sources/sunshine/" + source_name) as stream:
                        source_sha = hashlib.file_digest(stream, "sha256").hexdigest()
                except KeyError as exc:
                    raise ValueError("Host runtime notice was not delivered with its source") from exc
                if runtime_sha != source_sha:
                    raise ValueError("Host runtime notice differs from its supplied source")
            binding = True
            source_status = {"host_rebuild_verified": host_source.get("binary_source_rebuild_verified") is True,
                             "host_source_complete": host_source.get("source_complete") is True,
                             "host_notices_verified": host_source.get("dependency_notices_verified") is True,
                             "host_notice_delivery_verified": bool(host_notices),
                             "quest_source_complete": quest_source.get("source_complete") is True,
                             "quest_clean_build_verified": quest_source.get("clean_build_verified") is True,
                             "quest_notices_verified": quest_source.get("dependency_notices_verified") is True,
                             "quest_notice_delivery_verified": bool(quest_notices),
                             "public_release_signing": install.get("signing_kind") == "release" and install.get("package") == "app.questto3d.client"}
    native_verified = bool(source_status) and all(source_status.values())
    pending = ["fresh_windows_install", "update_rollback_on_final_package", "quest2_and_quest3_final_package",
               "wearer_audio_av_and_long_run", "upstream_archives_and_binary_metadata_privacy_audit"]
    if not native_verified:
        pending.insert(0, "complete_native_sources_and_notices")
    return {"schema": 1, "release": release, "kind": "release-asset-review", "published": False,
            "available_archive_integrity_verified": True, "missing_assets": missing,
            "binary_source_cross_binding_verified": binding, "source_and_signing": source_status,
            "ready_for_public_release": False,
            "native_source_and_signing_verified": native_verified,
            "pending_acceptance": pending,
            "assets": rows,
            "direct_apk_present": (directory / f"Sterevi-Quest-{release}.apk").is_file(),
            "direct_apk_matches_quest_payload": binding and (directory / f"Sterevi-Quest-{release}.apk").is_file()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--release", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists():
        raise FileExistsError("Use a new review report path")
    result = review(args.directory, args.release, publication.default_private_markers())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", "utf-8")
    print(json.dumps({key: value for key, value in result.items() if key != "assets"}, indent=2))


if __name__ == "__main__":
    main()

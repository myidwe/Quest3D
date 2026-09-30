"""Audit or sign a newly built public APK with an external private key.

No key generation, package rewriting, installation, upload, or debug APK
promotion. Signing passwords are requested interactively and sent through
apksigner stdin, never command arguments, environment variables, or reports.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import zipfile

from prepare_quest_source import sha, verify_manifest

HASH = re.compile(r"^[0-9a-f]{64}$")
PUBLIC_PACKAGE = "app.questto3d.client"


def specification(spec: dict) -> dict:
    metadata = spec.get("apk_metadata", {})
    if metadata.get("package") != PUBLIC_PACKAGE:
        raise ValueError("Public build must use the separately built app.questto3d.client package")
    version = metadata.get("version_code")
    if type(version) is not int or version < 1 or not isinstance(metadata.get("version_name"), str) or not metadata["version_name"].strip():
        raise ValueError("Public package version is required")
    if metadata.get("signing_kind") != "release":
        raise ValueError("A release signing policy is required")
    for name in ("unsigned_apk_sha256", "source_manifest_sha256"):
        if not isinstance(spec.get(name), str) or not HASH.fullmatch(spec[name]):
            raise ValueError("Exact APK and source manifest hashes are required")
    certificate = metadata.get("certificate_sha256")
    if not isinstance(certificate, str) or not HASH.fullmatch(certificate):
        raise ValueError("Expected public release certificate SHA-256 is required")
    if certificate == "a0c962041dd1a2154f07ffb5dd44867f8789d721a876c4540e2d9b70a9809dee":
        raise ValueError("The existing development signing identity cannot become the public release identity")
    return metadata


def apk_badging(raw: str) -> dict:
    match = re.search(r"(?m)^package: name='([^']+)' versionCode='(\d+)' versionName='([^']*)'", raw)
    if not match:
        raise ValueError("Cannot read APK package and version")
    return {"package": match[1], "version_code": int(match[2]), "version_name": match[3],
            "debuggable": bool(re.search(r"(?m)^application-debuggable", raw))}


def tool(argv: list[str]) -> bytes:
    result = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        # Tool stderr can contain a keystore path, alias or exception payload.
        # Keep automated reports free of it; do not echo inputs or passwords.
        raise RuntimeError("Android build tool failed; no publication was performed")
    return result.stdout


def audit(spec_path: Path, apk: Path, source: Path, aapt2: Path) -> dict:
    spec = json.loads(spec_path.read_text("utf-8"))
    metadata = specification(spec)
    if sha(apk) != spec["unsigned_apk_sha256"] or sha(source / "source-manifest.json") != spec["source_manifest_sha256"]:
        raise ValueError("Public build input hash mismatch")
    manifest = verify_manifest(source)
    actual = apk_badging(tool([str(aapt2), "dump", "badging", str(apk)]).decode("utf-8", errors="strict"))
    if actual["debuggable"] or any(actual[name] != metadata[name] for name in ("package", "version_code", "version_name")):
        raise ValueError("APK metadata does not match the non-debug public build specification")
    if manifest.get("apk_sha256") != spec["unsigned_apk_sha256"]:
        raise ValueError("Source manifest must correspond to this newly built unsigned APK")
    gaps = [name for name in ("source_complete", "clean_build_verified", "dependency_notices_verified") if manifest.get(name) is not True]
    return {"schema": 1, "unsigned_apk_sha256": spec["unsigned_apk_sha256"],
            "source_manifest_sha256": spec["source_manifest_sha256"], "apk_metadata": metadata,
            "ready_to_sign": not gaps, "remaining_gates": gaps,
            "signed": False, "installed": False, "published": False}


def apk_payload(apk: Path) -> dict[str, str]:
    """Hash every product ZIP entry, excluding only JAR signature metadata."""
    entries = {}
    with zipfile.ZipFile(apk) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError("Duplicate APK entries")
        for info in archive.infolist():
            if info.is_dir() or re.fullmatch(r"META-INF/(?:MANIFEST\.MF|[^/]+\.(?:SF|RSA|DSA|EC))", info.filename, re.IGNORECASE):
                continue
            digest = hashlib.sha256()
            with archive.open(info) as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            entries[info.filename] = digest.hexdigest()
    return entries


def sign(report: dict, apk: Path, output: Path, java: Path, apksigner_jar: Path,
         zipalign: Path, keystore: Path, alias: str, source: Path | None = None) -> dict:
    if not report["ready_to_sign"]:
        raise ValueError("Corresponding source, clean build and dependency notices must pass before public signing")
    if output.exists():
        raise ValueError("Existing output is preserved; choose a new signing directory")
    if not alias or "\x00" in alias or not keystore.is_file() or keystore.is_symlink():
        raise ValueError("External keystore and explicit alias are required")
    for path in (java, apksigner_jar, zipalign):
        if not path.is_file():
            raise ValueError("Explicit JDK and Android Build Tools files are required")
    if sha(apk) != report["unsigned_apk_sha256"]:
        raise ValueError("Input APK changed after the signing audit")
    # Do not re-sign an already signed APK. A successful verification means
    # the caller selected a signed build instead of a clean unsigned output.
    previous = subprocess.run([str(java), "-jar", str(apksigner_jar), "verify", str(apk)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if previous.returncode == 0:
        raise ValueError("Select the new unsigned public build, not an already signed APK")
    output.mkdir(parents=True)
    snapshot = output / "audited-unsigned.apk"
    aligned = output / "aligned-unsigned.apk"
    unverified = output / "signed-unverified.apk"
    final = output / "Quest3D-Quest.apk"
    shutil.copyfile(apk, snapshot)
    if sha(snapshot) != report["unsigned_apk_sha256"]:
        raise ValueError("Input APK changed while creating the audited snapshot")
    before_payload = apk_payload(snapshot)
    tool([str(zipalign), "-P", "16", "4", str(snapshot), str(aligned)])
    tool([str(zipalign), "-c", "-P", "16", "4", str(aligned)])
    password = getpass.getpass("Keystore password: ")
    key_password = getpass.getpass("Key password (Enter = same): ") or password
    if any(character in password + key_password for character in "\r\n\x00"):
        raise ValueError("Signing passwords cannot contain line breaks or NUL")
    argv = [str(java), "-Dfile.encoding=UTF-8", "-jar", str(apksigner_jar), "sign", "--ks", str(keystore),
            "--ks-key-alias", alias, "--ks-pass", "stdin", "--key-pass", "stdin",
            "--pass-encoding", "utf-8",
            "--out", str(unverified), str(aligned)]
    process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        stdout, stderr = process.communicate((password + "\n" + key_password + "\n").encode("utf-8"))
    finally:
        # Python strings cannot promise memory zeroization, but their lifetime
        # is limited to this isolated signing process and they are never saved.
        password = key_password = ""
    if process.returncode:
        raise RuntimeError("APK signing failed; unverified output remains separate and was not published")
    verification = tool([str(java), "-jar", str(apksigner_jar), "verify", "--print-certs", str(unverified)]).decode("utf-8", errors="strict")
    matches = re.findall(r"Signer #\d+ certificate SHA-256 digest: ([0-9a-fA-F]{64})", verification)
    if matches != [report["apk_metadata"]["certificate_sha256"]]:
        raise ValueError("Signing certificate mismatch; output is unverified and was not published")
    tool([str(zipalign), "-c", "-P", "16", "4", str(unverified)])
    if apk_payload(unverified) != before_payload:
        raise ValueError("Signed APK product payload differs from the audited unsigned snapshot")
    result = dict(report, signed=True, apk_sha256=sha(unverified), apk="Quest3D-Quest.apk",
                  product_payload_unchanged=True, product_payload_entries=len(before_payload))
    if source:
        result["signed_source_binding"] = bind_signed_source(source, output / "corresponding-source", result)
    unverified.replace(final)
    with (output / "signing-result.json").open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")
    return result


def bind_signed_source(source: Path, target: Path, report: dict) -> dict:
    """Preserve unsigned provenance and create a separate signed-APK binding."""
    if not report.get("signed") or sha(source / "source-manifest.json") != report["source_manifest_sha256"]:
        raise ValueError("Source changed after the signing audit")
    manifest = verify_manifest(source)
    if manifest.get("apk_sha256") != report["unsigned_apk_sha256"]:
        raise ValueError("Unsigned source binding mismatch")
    if target.exists():
        raise ValueError("Existing signed source output is preserved")
    shutil.copytree(source, target)
    if sha(target / "source-manifest.json") != report["source_manifest_sha256"]:
        raise ValueError("Source manifest changed while copying the signed source binding")
    verify_manifest(target)
    manifest.update(apk_sha256=report["apk_sha256"], unsigned_apk_sha256=report["unsigned_apk_sha256"],
                    signing_source_manifest_sha256=report["source_manifest_sha256"], apk_metadata=report["apk_metadata"])
    (target / "source-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    verify_manifest(target)
    return {"source": "corresponding-source", "source_manifest_sha256": sha(target / "source-manifest.json"),
            "apk_sha256": report["apk_sha256"], "unsigned_apk_sha256": report["unsigned_apk_sha256"],
            "original_source_manifest_sha256": report["source_manifest_sha256"],
            "apk_metadata": report["apk_metadata"], "original_source_preserved": True}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--unsigned-apk", required=True, type=Path)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--aapt2", required=True, type=Path)
    parser.add_argument("--execute", action="store_true", help="Sign only after all audited source gates pass")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--java", type=Path)
    parser.add_argument("--apksigner-jar", type=Path)
    parser.add_argument("--zipalign", type=Path)
    parser.add_argument("--keystore", type=Path)
    parser.add_argument("--alias")
    args = parser.parse_args()
    report = audit(args.spec.resolve(), args.unsigned_apk.resolve(), args.source.resolve(), args.aapt2.resolve())
    if args.execute:
        if not all((args.output, args.java, args.apksigner_jar, args.zipalign, args.keystore, args.alias)):
            parser.error("Signing requires --output --java --apksigner-jar --zipalign --keystore --alias")
        report = sign(report, args.unsigned_apk.resolve(), args.output.resolve(), args.java.resolve(),
                      args.apksigner_jar.resolve(), args.zipalign.resolve(), args.keystore.resolve(), args.alias, args.source.resolve())
    print(json.dumps(report))


if __name__ == "__main__":
    main()

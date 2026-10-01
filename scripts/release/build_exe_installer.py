"""Compile the auditable .NET bootstrap and append one verified release ZIP."""
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
import sys
import tempfile
import zipfile

from exe_payload import MAGIC, FOOTER_SIZE, pack_footer, read_payload

ROOT = Path(__file__).resolve().parents[2]
FORBIDDEN_NAMES = {"credentials.json", "web-access.clixml", "app_state.cfg", "host_state.cfg", "config.ini", "desktop.json", "receipt.json", "process.json", "launch.json", "sunshine.conf", "sunshine_state.json", "sunshine.log", ".env"}
FORBIDDEN_SUFFIXES = {".pem", ".key", ".keystore", ".jks", ".pfx", ".p12", ".clixml", ".log", ".jsonl", ".pyc", ".mp4", ".mkv", ".jpg", ".jpeg", ".wav", ".mp3", ".flac", ".webm", ".lnk", ".pdb"}


def safe_name(name: str) -> str:
    if not isinstance(name, str) or not name or any(c in name for c in "\\:\0") or name.startswith("/"):
        raise ValueError("Unsafe package path")
    parts = name.split("/")
    if any(not p or p in {".", ".."} or p.endswith((" ", ".")) or any(ord(c) < 32 or c in '<>"|?*' for c in p) or re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", p) for p in parts):
        raise ValueError("Unsafe package path")
    if any(p.casefold() in {".git", "__pycache__", ".godot", ".cache", "user-data", "signing", "captures", "screenshots"} or p.casefold().startswith(".venv") for p in parts) or parts[-1].casefold() in FORBIDDEN_NAMES or Path(parts[-1]).suffix.casefold() in FORBIDDEN_SUFFIXES:
        raise ValueError("Private or state file prohibited")
    return name


def unique_object(pairs):
    result = {}
    seen = set()
    for key, value in pairs:
        folded = key.casefold()
        if folded in seen:
            raise ValueError("Duplicate JSON key")
        seen.add(folded)
        result[key] = value
    return result


def verify_zip(payload: bytes, *, target: str, version: str) -> dict:
    if target not in {"pc", "quest"} or not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z.+-]{0,79}", version):
        raise ValueError("Unsupported target or release version")
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        entries = {}
        folded = set()
        for entry in archive.infolist():
            name = safe_name(entry.filename)
            if entry.is_dir() or name.casefold() in folded or entry.flag_bits & 1:
                raise ValueError("Duplicate, encrypted, or directory ZIP entry")
            mode = entry.external_attr >> 16
            if stat.S_IFMT(mode) not in {0, stat.S_IFREG} or entry.external_attr & (0x400 | 0x10):
                raise ValueError("Linked ZIP entry prohibited")
            folded.add(name.casefold())
            entries[name] = entry
        for name in folded:
            if any("/".join(name.split("/")[:index]) in folded for index in range(1, len(name.split("/")))):
                raise ValueError("A ZIP file is also used as a parent directory")
        if "distribution-manifest.json" not in entries:
            raise ValueError("Missing package manifest")
        if entries["distribution-manifest.json"].file_size > 16 * 1024 * 1024:
            raise ValueError("Manifest too large")
        manifest = json.loads(archive.read("distribution-manifest.json").decode("utf-8-sig"), object_pairs_hook=unique_object)
        files = manifest.get("files")
        if manifest.get("schema") != 1 or manifest.get("release") != version or not isinstance(files, dict) or not files:
            raise ValueError("Unsupported package manifest")
        if set(entries) != set(files) | {"distribution-manifest.json"} or "distribution-manifest.json" in files:
            raise ValueError("Package contains missing or unlisted files")
        seen = set()
        total = 0
        for name, item in files.items():
            safe_name(name)
            if name.casefold() in seen or not isinstance(item, dict):
                raise ValueError("Duplicate or malformed manifest file")
            seen.add(name.casefold())
            size = item.get("bytes")
            sha = item.get("sha256")
            if isinstance(size, bool) or not isinstance(size, int) or size < 0 or not isinstance(sha, str) or not re.fullmatch(r"[a-f0-9]{64}", sha):
                raise ValueError("Malformed manifest integrity value")
            entry = entries[name]
            if entry.file_size != size:
                raise ValueError("Package file size mismatch")
            total += size
            if total > 8 * 1024 * 1024 * 1024:
                raise ValueError("Unpacked package exceeds supported size")
            digest = hashlib.sha256()
            with archive.open(entry) as file:
                for chunk in iter(lambda: file.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != sha:
                raise ValueError("Package file hash mismatch")
        required = {"scripts/release/installer-launcher.ps1", "scripts/release/install-ui.ps1" if target == "pc" else "scripts/release/quest-install-ui.ps1", "scripts/release/install.ps1" if target == "pc" else "scripts/release/install-quest.ps1"}
        if not required <= set(files):
            raise ValueError("Missing selected installer entry points")
        kind = manifest.get("metadata", {}).get("kind", "")
        if not isinstance(kind, str) or not kind.startswith(target + "-"):
            raise ValueError("ZIP target does not match selected installer")
        return {"target": target, "version": version, "files": len(files), "unpacked_bytes": total, "manifest_sha256": hashlib.sha256(archive.read("distribution-manifest.json")).hexdigest()}


def assert_unlinked(path: Path) -> None:
    current = Path(os.path.abspath(path))
    while True:
        if current.exists() or current.is_symlink():
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("Linked build path prohibited")
        if current.parent == current:
            return
        current = current.parent


def build(zip_path: Path, target: str, version: str, output: Path, compiler: Path | None = None) -> dict:
    if sys.platform != "win32":
        raise RuntimeError("The bootstrap compiler requires Windows")
    assert_unlinked(zip_path)
    assert_unlinked(output)
    provenance = output.with_suffix(output.suffix + ".build.json")
    if output.exists() or provenance.exists():
        raise FileExistsError("Refusing to replace an existing installer or provenance")
    payload = zip_path.read_bytes()
    verified = verify_zip(payload, target=target, version=version)
    payload_sha = hashlib.sha256(payload).hexdigest()
    compiler = compiler or Path(os.environ.get("SystemRoot", "C:/Windows")) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    assert_unlinked(compiler)
    if not compiler.is_file():
        raise RuntimeError("The .NET Framework compiler is not available")
    output.parent.mkdir(parents=True, exist_ok=True)
    source = ROOT / "scripts/release/Quest3DSetup.cs"
    manifest = ROOT / "scripts/release/Quest3DSetup.manifest"
    icon = ROOT / "resources/desktop.ico"
    with tempfile.TemporaryDirectory(prefix=".quest3d-setup-build-", dir=output.parent) as temporary:
        directory = Path(temporary)
        generated = directory / "BuildInfo.cs"
        numeric = re.match(r"(\d+)\.(\d+)\.(\d+)", version)
        file_version = ".".join(numeric.groups()) + ".0" if numeric and all(int(item) < 65535 for item in numeric.groups()) else "1.0.0.0"
        generated_source = '[assembly: System.Reflection.AssemblyFileVersion("' + file_version + '")]\n[assembly: System.Reflection.AssemblyInformationalVersion("' + version + '")]\ninternal static class BuildInfo { internal const string Target = "' + target + '"; internal const string Version = "' + version + '"; internal const string PayloadSha256 = "' + payload_sha + '"; internal const long PayloadLength = ' + str(len(payload)) + 'L; }\n'
        generated.write_text(generated_source, "utf-8")
        stub = directory / "Quest3DSetup.exe"
        result = subprocess.run([str(compiler), "/nologo", "/target:winexe", "/platform:x64", "/optimize+", "/debug-", "/utf8output", "/out:" + str(stub), "/win32manifest:" + str(manifest), "/win32icon:" + str(icon), "/reference:System.Windows.Forms.dll", "/reference:System.Drawing.dll", "/reference:System.Web.Extensions.dll", "/reference:System.IO.Compression.dll", "/reference:System.IO.Compression.FileSystem.dll", str(source), str(generated)], capture_output=True, text=True, encoding="utf-8", errors="replace")
        if result.returncode:
            # Compiler errors are useful locally; a successful public proof never embeds paths.
            raise RuntimeError("Bootstrap compilation failed:\n" + result.stdout + result.stderr)
        stub_bytes = stub.read_bytes()
        footer = pack_footer(len(stub_bytes), payload)
        with output.open("xb") as file:
            file.write(stub_bytes)
            file.write(payload)
            file.write(footer)
    _, overlay = read_payload(output.read_bytes())
    record = {"schema": 1, "format": "Quest3D.Setup overlay v1", "target": target, "version": version, "payload": verified, "overlay": overlay, "compiler": {"kind": "Microsoft .NET Framework csc", "sha256": hashlib.sha256(compiler.read_bytes()).hexdigest()}, "source": {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in (source, manifest, icon, Path(__file__), ROOT / "scripts/release/exe_payload.py")}, "generated_build_info_sha256": hashlib.sha256(generated_source.encode("utf-8")).hexdigest(), "windows_file_version": file_version, "stub_sha256": hashlib.sha256(stub_bytes).hexdigest(), "footer_sha256": hashlib.sha256(footer).hexdigest(), "exe_sha256": hashlib.sha256(output.read_bytes()).hexdigest(), "exe_bytes": output.stat().st_size, "administrator_requested": False, "debug_symbols": False, "byte_reproducible_compilation_claimed": False}
    with provenance.open("x", encoding="utf-8", newline="\n") as file:
        json.dump(record, file, indent=2, ensure_ascii=False)
        file.write("\n")
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zip", type=Path, required=True)
    parser.add_argument("--target", choices=("pc", "quest"), required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--compiler", type=Path)
    args = parser.parse_args()
    print(json.dumps(build(args.zip, args.target, args.version, args.output, args.compiler), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

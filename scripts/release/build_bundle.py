"""Build an allowlisted, state-free local distribution candidate. Never publishes."""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import io
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parents[2]
HOST_SHA = "77c950b526ba6b944589b8697cbaaa76b26955e3ae2e412a4cfba7bc93626b63"
CAPTURE_SHA = "0d8bea69406e930dc424694061ed109489cc230b6948721e18ec5838505a4496"
AUDIO_PROBE_SHA = "1511eb13904a5164f8b6b7bce15abbb76115fb42ecd9cdc7a2b1548014657944"
HOST = "artifacts/host/runtime-20260909-233013-03efb525"
SOURCE_SUFFIXES = {".py", ".ps1", ".sh", ".h", ".hpp", ".c", ".cpp", ".cu", ".cuh", ".rs", ".toml", ".json", ".lock", ".patch", ".cmake", ".txt", ".md", ".gd", ".gdshader", ".gdshaderinc", ".tscn", ".tres", ".godot", ".cfg", ".java", ".xml", ".gradle", ".properties", ".in", ".yml", ".yaml", ".bat", ".cmd", ".rc", ".manifest", ".uid", ".qml", ".qrc", ".svg", ".ttf", ".otf", ".png", ".ico"}
SOURCE_SUFFIXES.add(".gdextension")
# Device operations and developer-specific probes are not corresponding source.
LOCAL_ONLY_SCRIPTS = (
    "*-reviewed.*", "snapshot-ui-baseline.py", "desktop-qt-preview.py",
    "desktop-ui-preview.py", "prepare-first-connect-runtime-check.py",
    "prepare-first-connect-ui-check.py", "probe-quest3-mdns.py",
    "probe_quest3_mdns.cpp", "build_render_target_probe.py",
    "compile_stream_resolution_android.py", "verify_audio_startup_resolution_source.py",
    "verify_file_audio_sink_native.py", "quest*-export.sh", "quest*-check-source.sh",
    "quest-compact-verify-runtime.sh", "quest-display-verify-runtime.sh",
    "quest-level-verify-runtime.sh", "quest-pointer-verify-runtime.sh",
    "quest3-verify-runtime.sh", "quest3-sign.sh",
)
LOCAL_ONLY_DIAGNOSTICS = ("select_quest_codec.py", "dev-firewall.ps1", "run_file_audio_interop.py", "HDR_CANDIDATE.md", "WINDOW_CANDIDATE.md")
PUBLIC_RELEASE_DOCS = (
    "DISTRIBUTION.md", "BUILDING.md", "DESKTOP_USER_GUIDE.html", "DESKTOP_USER_GUIDE.md",
    "PRODUCT_SCOPE.md", "OPEN_SOURCE_RELEASE_PLAN_2026-09-30.md",
    "DEPENDENCY_AUDIT_2026-09-30.md", "RELEASE_NOTES_TEMPLATE.md", "GITHUB_PUBLICATION.md",
    "HOST_RELEASE_PREPARATION_2026-09-30.md", "QUEST_RELEASE_PREPARATION_2026-09-30.md",
    "SETUP_PERMISSIONS_2026-09-30.md",
    "QUEST_CLEAN_BUILD_2026-10-01.md",
)
SKIP_DIRS = {".git", ".godot", "__pycache__", ".pytest_cache", "node_modules", "target", "build", "tools", ".cache", "config", "models", "bin", "installed", "vcpkg_installed", "user", "user-data"}
FORBIDDEN_NAMES = {"credentials.json", "web-access.clixml", "app_state.cfg", "host_state.cfg", "config.ini", "desktop.json", "receipt.json", "process.json", "launch.json", "sunshine.conf", "sunshine_state.json", "sunshine.log", ".env"}
FORBIDDEN_SUFFIXES = {".pem", ".key", ".keystore", ".jks", ".pfx", ".p12", ".clixml", ".log", ".jsonl", ".pyc", ".mp4", ".mkv", ".jpg", ".jpeg", ".wav", ".mp3", ".flac", ".webm", ".lnk", ".pdb"}


def digest(path: Path) -> str:
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def relative_name(name: str) -> str:
    if not isinstance(name, str) or not name or "\\" in name or ":" in name or "\x00" in name:
        raise ValueError(f"Unsafe archive path: {name!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or any(p in {"", ".", ".."} or p.endswith((" ", ".")) for p in name.split("/")):
        raise ValueError(f"Unsafe archive path: {name!r}")
    if any(re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", p) for p in path.parts):
        raise ValueError("Windows reserved path")
    return path.as_posix()


def assert_regular(path: Path, base: Path) -> None:
    # Callers may supply a relative reviewed Quest-source directory. Compare
    # absolute lexical ancestors so walking to an absolute base always ends;
    # retain symlinks for lstat checks rather than resolving them away.
    path = Path(os.path.abspath(path))
    base = Path(os.path.abspath(base))
    if not path.resolve().is_relative_to(base.resolve()):
        raise ValueError(f"Source escapes root: {path}")
    current = path
    while True:
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError(f"Reparse point not allowed: {current}")
        if current == base:
            break
        current = current.parent
    if not path.is_file():
        raise ValueError(f"Not a regular file: {path}")


def public_name(name: str) -> None:
    path = PurePosixPath(relative_name(name))
    if path.name.lower() in FORBIDDEN_NAMES or path.suffix.lower() in FORBIDDEN_SUFFIXES:
        raise ValueError(f"Private/state file prohibited: {name}")
    if any(part.casefold() in {".git", "__pycache__", ".godot", "signing", "captures", "screenshots"} for part in path.parts):
        raise ValueError(f"Private/cache directory prohibited: {name}")


def validate_runtime_tree(source: Path) -> None:
    """Unknown application assets require review instead of silently vanishing."""
    for parent, dirs, files in os.walk(source, followlinks=False):
        dirs[:] = [name for name in dirs if name not in {"__pycache__", ".git", ".pytest_cache"}]
        for name in files:
            path = Path(parent) / name
            if path.suffix == ".pyc":
                continue
            relative = path.relative_to(source).as_posix()
            public_name(relative)
            if path.suffix.lower() not in SOURCE_SUFFIXES and name not in {"LICENSE", "COPYING", "NOTICE"}:
                raise ValueError(f"Unclassified runtime asset: {relative}")


class Payload:
    def __init__(self, base: Path, destination: Path):
        self.base = base.resolve()
        self.destination = destination.resolve()
        if self.destination.exists():
            raise FileExistsError(f"Refusing to overwrite: {self.destination}")
        self.destination.mkdir(parents=True)
        self.names: set[str] = set()

    def put(self, name: str, data: bytes) -> None:
        public_name(name)
        if name.casefold() in self.names:
            raise ValueError(f"Case-colliding/duplicate payload: {name}")
        self.names.add(name.casefold())
        target = self.destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def copy(self, source: Path, name: str, expected: str | None = None) -> None:
        assert_regular(source, self.base)
        data = source.read_bytes()
        if expected is not None and hashlib.sha256(data).hexdigest() != expected:
            raise ValueError(f"Pinned hash mismatch: {source}")
        self.put(name, data)

    def tree(self, source: Path, name: str, *, source_only: bool = False, skip_dirs=(), skip_files=()) -> None:
        excluded = set(skip_dirs) | {".git", "__pycache__", ".godot"}
        for parent, dirs, files in os.walk(source, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in excluded and not d.startswith("cmake-build-"))
            for filename in sorted(files):
                if any(fnmatch.fnmatchcase(filename, pattern) for pattern in skip_files):
                    continue
                file = Path(parent) / filename
                if source_only and file.suffix.lower() not in SOURCE_SUFFIXES and filename not in {"LICENSE", "COPYING", "NOTICE", "CMakeLists.txt", "Cargo.lock"}:
                    continue
                if filename.lower() in FORBIDDEN_NAMES or file.suffix.lower() in FORBIDDEN_SUFFIXES:
                    continue
                self.copy(file, f"{name}/{file.relative_to(source).as_posix()}")

    def manifest(self, *, release: str, metadata: dict) -> dict:
        files = {}
        for file in sorted(self.destination.rglob("*")):
            if file.is_file():
                files[file.relative_to(self.destination).as_posix()] = {"sha256": digest(file), "bytes": file.stat().st_size}
        result = {"schema": 1, "release": release, "metadata": metadata, "files": files}
        self.put("distribution-manifest.json", (json.dumps(result, indent=2, ensure_ascii=False) + "\n").encode())
        return result


def verify_directory(root: Path) -> dict:
    manifest = json.loads((root / "distribution-manifest.json").read_text("utf-8"))
    if manifest.get("schema") != 1 or not isinstance(manifest.get("files"), dict):
        raise ValueError("Invalid distribution manifest")
    seen = set()
    for name, entry in manifest["files"].items():
        public_name(name)
        if name.casefold() in seen:
            raise ValueError("Case-colliding manifest")
        seen.add(name.casefold())
        file = root / name
        assert_regular(file, root)
        if file.stat().st_size != entry["bytes"] or digest(file) != entry["sha256"]:
            raise ValueError(f"Payload hash mismatch: {name}")
    actual = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
    if actual != set(manifest["files"]) | {"distribution-manifest.json"}:
        raise ValueError("Unexpected payload files")
    return manifest


def zip_payload(root: Path, output: Path) -> None:
    verify_directory(root)
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for file in sorted(root.rglob("*")):
            if file.is_file():
                archive.write(file, file.relative_to(root).as_posix())


def safe_extract(archive: Path, target: Path) -> None:
    if target.exists():
        raise FileExistsError("Extraction requires a new directory")
    with zipfile.ZipFile(archive) as source:
        names = set()
        for info in source.infolist():
            public_name(info.filename)
            if info.filename.casefold() in names or stat.S_ISLNK(info.external_attr >> 16):
                raise ValueError("Duplicate/symlink ZIP entry")
            names.add(info.filename.casefold())
        target.mkdir(parents=True)
        source.extractall(target)
    verify_directory(target)


def installer_launcher_cmd(target: str) -> bytes:
    if target not in {"pc", "quest"}:
        raise ValueError("Unknown installer target")
    lines = [
        "@echo off", "setlocal", ":choose_log",
        'set "QUEST3D_START_LOG=%TEMP%\\Quest3D-Start-%RANDOM%-%RANDOM%"',
        'if exist "%QUEST3D_START_LOG%.log" goto choose_log',
        'if exist "%QUEST3D_START_LOG%.details.log" goto choose_log',
        'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\\release\\installer-launcher.ps1" '
        f'-Target {target} -LogPath "%QUEST3D_START_LOG%.details.log" > "%QUEST3D_START_LOG%.log" 2>&1',
        "if not errorlevel 1 exit /b 0", "echo.", "echo Quest3D installer did not start.",
        'type "%QUEST3D_START_LOG%.log"', "echo.",
        'echo Startup log: "%QUEST3D_START_LOG%.log"',
        "echo Check the complete trusted ZIP and docs/DISTRIBUTION.md.",
        "echo On a managed PC, contact your Windows administrator.",
        "pause", "exit /b 1",
    ]
    return ("\r\n".join(lines) + "\r\n").encode("ascii")


def build_pc(root: Path, out: Path, release: str) -> Path:
    validate_runtime_tree(root / "src/quest3d")
    check_spec = importlib.util.spec_from_file_location("quest3d_release_ui", Path(__file__).with_name("verify_installed_ui.py"))
    ui_check = importlib.util.module_from_spec(check_spec)
    check_spec.loader.exec_module(ui_check)
    ui_verification = ui_check.verify_resources(root)
    runtime_sources = ui_check.verify_runtime_sources(root)
    payload = Payload(root, out / "pc")
    for name in ("README.md", "README.en.md", "pyproject.toml", "uv.lock", ".python-version", "LICENSE", "THIRD_PARTY_NOTICES.md", "CONTRIBUTING.md", "SECURITY.md", "CHANGELOG.md"):
        payload.copy(root / name, name)
    payload.tree(root / "src/quest3d", "src/quest3d", source_only=True)
    payload.tree(root / "scripts/release", "scripts/release", source_only=True)
    for name in ("install-desktop-shortcut.ps1", "stop-verified-host.py"):
        payload.copy(root / "scripts" / name, "scripts/" + name)
    for extension in ("*.ps1", "*.py"):
        for file in sorted((root / "native/host").glob(extension)):
            if file.name in LOCAL_ONLY_DIAGNOSTICS:
                continue
            payload.copy(file, "native/host/" + file.name)
    payload.copy(root / "artifacts/host/frame_bridge_probe.exe", "artifacts/host/frame_bridge_probe.exe")
    payload.copy(root / "native/audio/dist/audio_probe.exe", "native/audio/dist/audio_probe.exe", AUDIO_PROBE_SHA)
    for name in PUBLIC_RELEASE_DOCS:
        payload.copy(root / "docs" / name, "docs/" + name)
    payload.tree(root / "resources", "resources", source_only=True)
    payload.copy(root / "config/models.json", "config/models.json")
    depth = root / "third_party/depth-anything-v2"
    payload.tree(depth / "depth_anything_v2", "third_party/depth-anything-v2/depth_anything_v2", source_only=True)
    payload.copy(depth / "LICENSE", "third_party/depth-anything-v2/LICENSE")
    spec = json.loads((root / "config/models.json").read_text())["depth_anything_v2_small"]
    manifest = {"source_commit": spec["source_commit"], "files": {p.relative_to(depth).as_posix(): digest(p) for p in sorted((depth / "depth_anything_v2").rglob("*.py"))}}
    payload.put("config/depth-source.json", json.dumps(manifest, indent=2).encode())
    host = root / HOST
    for name in ("sunshine.exe", "zlib1.dll", "LICENSE.txt"):
        payload.copy(host / name, "artifacts/host/runtime-public/" + name, HOST_SHA if name == "sunshine.exe" else None)
    payload.tree(host / "assets", "artifacts/host/runtime-public/assets", skip_dirs={"config"})
    capture_wheel = root / "artifacts/capture/hdr-experimental/wc_cuda-0.1.2+quest2-cp310-abi3-win_amd64.whl"
    if digest(capture_wheel) != CAPTURE_SHA:
        raise ValueError("Capture wheel changed")
    with zipfile.ZipFile(capture_wheel) as archive:
        for info in archive.infolist():
            if not info.is_dir():
                payload.put("runtime/capture/" + relative_name(info.filename), archive.read(info))
    payload.copy(root / "vendor/wheels/wc_cuda-0.1.2+quest1-cp310-abi3-win_amd64.whl", "vendor/wheels/wc_cuda-0.1.2+quest1-cp310-abi3-win_amd64.whl")
    payload.tree(root / ".tools/desktop/powershell", ".tools/desktop/powershell")
    payload.copy(root / ".tools/desktop/uv.exe", ".tools/desktop/uv.exe", "f13b441ca13bf0a1d02d367e395c8a72e11733c5c29efa4a2f71af6a3a30d5ac")
    payload.tree(root / "scripts/release/licenses", "licenses")
    payload.put("config/distribution.json", json.dumps({"schema": 1, "host_runtime": "artifacts/host/runtime-public", "host_sha256": HOST_SHA, "hdr_package": "runtime/capture", "depth_source": "config/depth-source.json"}, indent=2).encode())
    payload.put("Install-Quest3D.cmd", installer_launcher_cmd("pc"))
    payload.put("README-FIRST.txt", ("Quest3D Desktop preview\n\n1. Extract this entire ZIP.\n2. Open Install-Quest3D.cmd. First install downloads Python, GPU dependencies and the model.\n3. Open Quest3D Desktop, start PC, then pair/connect in the Quest app.\n\nRead docs/DISTRIBUTION.md. This candidate has not been published.\n").encode())
    # Validate the copied payload itself: checking only the working tree cannot
    # detect an allowlist that accidentally drops runtime CUDA/QML source files.
    if ui_check.verify_resources(payload.destination) != ui_verification or ui_check.verify_runtime_sources(payload.destination) != runtime_sources:
        raise ValueError("Packaged runtime resources differ from the reviewed source")
    payload.manifest(release=release, metadata={"kind": "pc-installer-candidate", "host_sha256": HOST_SHA, "capture_wheel_sha256": CAPTURE_SHA, "model_included": False, "published": False, "ui": ui_verification, "cuda_sources": runtime_sources})
    zip_payload(payload.destination, out / f"Quest3D-Desktop-{release}.zip")
    return payload.destination


def git_bytes(repository: Path, *args: str) -> bytes:
    return subprocess.run(["git", "-c", "core.excludesFile=NUL", "-C", str(repository), *args], check=True, capture_output=True).stdout


def snapshot_tar(directory: Path) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for file in sorted(directory.rglob("*")):
            if file.is_file():
                assert_regular(file, directory)
                name = relative_name(file.relative_to(directory).as_posix())
                public_name(name)
                info = tarfile.TarInfo(name)
                data = file.read_bytes()
                info.size = len(data)
                info.mode = 0o644
                archive.addfile(info, io.BytesIO(data))
    return output.getvalue()


def build_sources(root: Path, out: Path, release: str, quest_source: Path | None) -> Path:
    """Keep binary-corresponding historical host sources separate from later experiments."""
    payload = Payload(root, out / "source")
    for name in ("README.md", "README.en.md", "pyproject.toml", "uv.lock", ".python-version", "LICENSE", "THIRD_PARTY_NOTICES.md", "CONTRIBUTING.md", "SECURITY.md", "CHANGELOG.md"):
        payload.copy(root / name, name)
    for name in ("src", "scripts", "tests"):
        payload.tree(root / name, name, source_only=True, skip_dirs=SKIP_DIRS,
                     skip_files=LOCAL_ONLY_SCRIPTS if name == "scripts" else ())
    payload.tree(root / "resources", "resources", source_only=True)
    payload.tree(root / "native", "native", source_only=True, skip_dirs=SKIP_DIRS | {"vendor"}, skip_files=LOCAL_ONLY_DIAGNOSTICS)
    payload.copy(root / "config/models.json", "config/models.json")
    for name in PUBLIC_RELEASE_DOCS:
        payload.copy(root / "docs" / name, "docs/" + name)
    payload.tree(root / "scripts/release/licenses", "licenses")
    sunshine_pin = "cb72dffa3233c5815cd5ba88f09f049dd679ba75"
    sunshine = root / "third_party/sunshine"
    payload.put("sources/sunshine/upstream.tar", git_bytes(sunshine, "archive", "--format=tar", sunshine_pin))
    snapshot = root / "artifacts/host/patch-reproduction-f6e4f3c7490a4c9eac9cf2ea37a4342d"
    payload.put("sources/sunshine/quest3d-host-20260909-2253.tar", snapshot_tar(snapshot))
    submodules = []
    def submodule_sources(repository: Path, pin: str, prefix: str = ""):
        rows = git_bytes(repository, "ls-tree", "-r", "-z", pin).split(b"\0")
        for row in rows:
            if not row:
                continue
            metadata, name = row.split(b"\t", 1)
            mode, kind, commit = metadata.decode().split()
            if mode != "160000":
                continue
            relative = name.decode()
            full = prefix + relative
            child = repository / relative
            record = {"path": full, "commit": commit, "source_included": False}
            if (child / ".git").exists():
                archive_name = "sources/sunshine/submodules/" + full.replace("/", "--") + ".tar"
                payload.put(archive_name, git_bytes(child, "archive", "--format=tar", commit))
                record.update(source_included=True, archive=archive_name)
                submodule_sources(child, commit, full + "/")
            submodules.append(record)
    submodule_sources(sunshine, sunshine_pin)
    provenance = {
        "host_binary_sha256": HOST_SHA,
        "upstream_commit": sunshine_pin,
        "modified_source_snapshot": "quest3d-host-20260909-2253.tar",
        "snapshot_files": {p.relative_to(snapshot).as_posix(): digest(p) for p in sorted(snapshot.rglob("*")) if p.is_file()},
        "evidence": "Snapshot exported at 22:53 on 2026-09-09 after the 22:51 regression used by the stable runtime. Binary/source reproducibility must be checked before public release.",
        "binary_source_rebuild_verified": False,
        "historical_snapshot_build_status": "failed",
        "known_source_gap": "Historical audio::packet_t is a struct, while upstream src/stream.cpp:1879 still uses tuple std::get. Missing historical source changes must be restored or a new release host rebuilt and validated.",
        "submodules": submodules,
    }
    payload.put("sources/sunshine/provenance.json", json.dumps(provenance, indent=2).encode())
    capture = root / "third_party/wc_cuda"
    payload.put("sources/capture/upstream.tar", git_bytes(capture, "archive", "--format=tar", "6f6c6eaed91f36f0e937f1da92cf5cfc35a9bfcc"))
    for name in ("wc_cuda-quest1.patch", "wc_cuda-quest2-hdr.patch", "Cargo.hdr.lock"):
        payload.copy(root / "native/capture" / name, "sources/capture/" + name)
    depth = root / "third_party/depth-anything-v2"
    # This repository is a sparse/promisor checkout. Ship exactly the complete
    # inference package used at runtime; do not fetch unrelated model/demo blobs.
    payload.tree(depth / "depth_anything_v2", "sources/depth-anything-v2/depth_anything_v2", source_only=True)
    payload.copy(depth / "LICENSE", "sources/depth-anything-v2/LICENSE")
    if quest_source is not None:
        # The Quest builder supplies an explicit source manifest, not a whole mutable
        # working directory. Compiled caches, user preferences and signing keys are omitted.
        manifest_path = quest_source / "source-manifest.json"
        manifest = json.loads(manifest_path.read_text("utf-8"))
        for name, expected in manifest["files"].items():
            relative_name(name)
            payload.copy(quest_source / name, "sources/quest/" + name, expected if isinstance(expected, str) else expected["sha256"])
        payload.copy(manifest_path, "sources/quest/source-manifest.json")
    payload.manifest(release=release, metadata={"kind": "source-candidate", "published": False, "host_binary_source_rebuild_verified": False, "quest_source_included": quest_source is not None})
    zip_payload(payload.destination, out / f"Quest3D-Source-{release}.zip")
    return payload.destination


def validate_quest_source(source: Path, expected_apk_sha: str) -> dict:
    """Prevent a reviewed APK from being paired with another APK's source list."""
    manifest_path = source / "source-manifest.json"
    assert_regular(manifest_path, source)
    manifest = json.loads(manifest_path.read_text("utf-8"))
    if manifest.get("apk_sha256") != expected_apk_sha:
        raise ValueError("Quest source manifest is not bound to the reviewed APK")
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise ValueError("Quest corresponding source list is empty")
    for name, entry in files.items():
        public_name(name)
        if PurePosixPath(name).suffix.lower() in {".apk", ".so", ".dll", ".gdc", ".pck"}:
            raise ValueError("Compiled Quest payload is not corresponding source")
        file = source / name
        assert_regular(file, source)
        expected = entry if isinstance(entry, str) else entry["sha256"]
        if digest(file) != expected:
            raise ValueError(f"Quest corresponding source hash mismatch: {name}")
    return manifest


def quest_install_metadata(source_manifest: dict, expected_sha: str) -> dict:
    """Installation metadata comes from the exact reviewed APK/source record."""
    if source_manifest.get("apk_sha256") != expected_sha:
        raise ValueError("Quest installation metadata is not bound to the APK")
    meta = source_manifest.get("apk_metadata", {})
    package = meta.get("package")
    if package not in {"app.questto3d.client.debug", "app.questto3d.client"}:
        raise ValueError("Reviewed Quest package metadata is required")
    version = meta.get("version_code")
    cert = meta.get("certificate_sha256", "")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1 or not re.fullmatch(r"[0-9a-f]{64}", cert):
        raise ValueError("Reviewed Quest version and signing certificate are required")
    return {"schema": 1, "apk": "Quest3D-Quest.apk", "sha256": expected_sha,
            "package": package, "version_code": version, "version_name": meta.get("version_name", ""),
            "certificate_sha256": cert, "signing_kind": meta.get("signing_kind", "unverified"), "preserve_data": True}


def build_quest(root: Path, out: Path, release: str, apk: Path, expected_sha: str, source_manifest: dict) -> Path:
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha):
        raise ValueError("The reviewed APK SHA-256 is required")
    install_metadata = quest_install_metadata(source_manifest, expected_sha)
    payload = Payload(root, out / "quest")
    payload.copy(apk, "Quest3D-Quest.apk", expected_sha)
    for name in ("install-quest.ps1", "quest-install-ui.ps1", "installer-launcher.ps1"):
        payload.copy(root / "scripts/release" / name, "scripts/release/" + name)
    for name in ("README.md", "README.en.md", "LICENSE", "THIRD_PARTY_NOTICES.md", "CONTRIBUTING.md", "SECURITY.md", "CHANGELOG.md"):
        payload.copy(root / name, name)
    for name in PUBLIC_RELEASE_DOCS:
        payload.copy(root / "docs" / name, "docs/" + name)
    payload.put("quest-install.json", json.dumps(install_metadata, indent=2).encode())
    # The bundled HTML guide must also work from a Quest-only download.
    payload.copy(root / "resources/ui/brand/quest3d-mark.png", "resources/ui/brand/quest3d-mark.png")
    payload.put("Install-Quest.cmd", installer_launcher_cmd("quest"))
    payload.put("README-FIRST.txt", "Enable Quest developer mode, connect USB and allow debugging in the headset.\nDownload official Google Platform Tools.\nOpen Install-Quest.cmd, select adb.exe, check the device and install.\nRead docs/DISTRIBUTION.md. Existing app data is never automatically deleted.\n".encode())
    payload.manifest(release=release, metadata={"kind": "quest-installer-candidate", "apk_sha256": expected_sha, "published": False, "signing_private_key_included": False,
        "package": install_metadata["package"], "signing_kind": install_metadata["signing_kind"],
        "quest_corresponding_source_complete": source_manifest.get("source_complete") is True,
        "quest_clean_build_verified": source_manifest.get("clean_build_verified") is True,
        "ready_for_public_release": False})
    zip_payload(payload.destination, out / f"Quest3D-Quest-{release}.zip")
    return payload.destination


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--release", default="0.1.0-preview")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--sources", action="store_true")
    parser.add_argument("--quest-source", type=Path)
    parser.add_argument("--quest-apk", type=Path)
    parser.add_argument("--quest-sha256")
    args = parser.parse_args(argv)
    if args.verify:
        result = verify_directory(args.output)
        print(json.dumps({"verified": True, "files": len(result["files"])}))
        return
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", args.release):
        raise ValueError("Invalid release name")
    if args.quest_apk:
        if not args.quest_sha256 or not args.quest_source or not args.sources:
            raise ValueError("APK distribution requires reviewed SHA and corresponding source bundle")
        if not re.fullmatch(r"[0-9a-f]{64}", args.quest_sha256) or digest(args.quest_apk) != args.quest_sha256:
            raise ValueError("Reviewed APK hash mismatch")
        quest_manifest = validate_quest_source(args.quest_source, args.quest_sha256)
        quest_install_metadata(quest_manifest, args.quest_sha256)
    if args.output.exists():
        raise FileExistsError("Use a new release directory")
    args.output.mkdir(parents=True)
    destination = build_pc(args.root.resolve(), args.output.resolve(), args.release)
    if args.sources:
        build_sources(args.root.resolve(), args.output.resolve(), args.release, args.quest_source.resolve() if args.quest_source else None)
    if args.quest_apk:
        build_quest(args.root.resolve(), args.output.resolve(), args.release, args.quest_apk.resolve(), args.quest_sha256, quest_manifest)
    verify_directory(destination)
    print(json.dumps({"pc": str(destination), "published": False}, indent=2))


if __name__ == "__main__":
    main()

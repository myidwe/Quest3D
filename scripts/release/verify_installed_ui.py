"""Validate packaged UI resources and render actual QML without starting PC services."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tomllib

QT_VERSION = "6.8.3"
QT_WHEELS = {
    "pyside6-essentials": "3c0fae5550aff69f2166f46476c36e0ef56ce73d84829eac4559770b0c034b07",
    "shiboken6": "bca3a94513ce9242f7d4bbdca902072a1631888e0aa3a8711a52cc5dbe93588f",
}
REQUIRED_ASSETS = {
    "fonts/Pretendard-Regular.otf", "fonts/Pretendard-Medium.otf",
    "fonts/Pretendard-SemiBold.otf", "fonts/OFL-Pretendard.txt",
    "icons/LICENSE-Lucide.txt",
}
REQUIRED_CUDA_SOURCES = ("tone_map.cu", "forward_warp.cu", "depth_edges.cu", "stereo_pack.cu", "reference_cubic.cu", "colour_fit.cu")


def verify_runtime_sources(root: Path) -> dict:
    return {name: sha256(resource_file(root.resolve(), "src/quest3d/shaders/" + name))
            for name in REQUIRED_CUDA_SOURCES}


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def resource_file(root: Path, name: str) -> Path:
    if (not isinstance(name, str) or not name or "\\" in name or ":" in name or
            "\0" in name or PurePosixPath(name).is_absolute() or
            any(part in {"", ".", ".."} for part in name.split("/"))):
        raise ValueError("Unsafe UI resource path")
    target = root / name
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError("UI resource escapes package")
    current = target
    while True:
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("UI resource cannot be a reparse point")
        if current == root:
            break
        current = current.parent
    if not target.is_file():
        raise ValueError(f"Missing UI resource: {name}")
    return target


def verify_resources(root: Path) -> dict:
    """Fail before bundling when a selected UI resource or exact dependency is missing."""
    root = root.resolve()
    asset_root = root / "resources/ui"
    manifest = json.loads(resource_file(asset_root, "ASSET_MANIFEST.json").read_text("utf-8"))
    entries = manifest.get("files")
    if not isinstance(entries, dict) or not REQUIRED_ASSETS.issubset(entries):
        raise ValueError("UI asset manifest must include all three fonts and their licenses")
    if not any(name.startswith("icons/") and name.endswith(".svg") for name in entries):
        raise ValueError("UI asset manifest has no icons")
    for name, entry in entries.items():
        path = resource_file(asset_root, name)
        expected = entry.get("sha256", "")
        if not re.fullmatch("[0-9a-f]{64}", expected) or sha256(path) != expected or path.stat().st_size != entry.get("bytes"):
            raise ValueError(f"UI resource checksum mismatch: {name}")
    brand_root = asset_root / "brand"
    brand = json.loads(resource_file(brand_root, "provenance.json").read_text("utf-8"))
    if brand.get("kind") != "generated-ui-asset" or brand.get("file") != "quest3d-mark.png":
        raise ValueError("Selected brand asset provenance is missing")
    brand_file = resource_file(brand_root, brand["file"])
    if sha256(brand_file) != brand.get("sha256") or brand_file.stat().st_size != brand.get("bytes"):
        raise ValueError("Brand resource checksum mismatch")
    desktop_icon = brand.get("desktop_icon", {})
    if desktop_icon.get("file") != "resources/desktop.ico" or sha256(resource_file(root, "resources/desktop.ico")) != desktop_icon.get("sha256"):
        raise ValueError("Desktop/tray icon checksum mismatch")
    resource_file(root, "resources/desktop/Main.qml")
    for name in ("Qt-LGPL-3.0-only.txt", "Qt-GPL-3.0-only.txt", "Qt-GPL-2.0-only.txt"):
        resource_file(root, "scripts/release/licenses/" + name)
    lock = tomllib.loads(resource_file(root, "uv.lock").read_text("utf-8"))
    packages = {p["name"].lower(): p for p in lock["package"]}
    for name, wheel_hash in QT_WHEELS.items():
        package = packages.get(name, {})
        if package.get("version") != QT_VERSION or not any(
            w.get("hash") == "sha256:" + wheel_hash and w.get("url", "").endswith("win_amd64.whl")
            for w in package.get("wheels", [])
        ):
            raise ValueError(f"Pinned Qt wheel missing or changed: {name}")
    return {"asset_files": len(entries), "brand_verified": True, "qml_files": len(list((root / "resources/desktop").rglob("*.qml"))), "qt_version": QT_VERSION}


def verify_qml(root: Path) -> dict:
    # These apply only to this checker process. Never inherit the user's Qt plugin
    # path or write to their normal Qt settings/cache while validating an install.
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    os.environ["QT_QUICK_BACKEND"] = "software"
    os.environ["QT_QUICK_CONTROLS_STYLE"] = "Basic"
    os.environ["QT_QPA_FONTDIR"] = str(root.resolve() / "resources/ui/fonts")
    os.environ["QML_DISABLE_DISK_CACHE"] = "1"
    for name in ("QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH", "QML2_IMPORT_PATH", "QML_IMPORT_PATH"):
        os.environ.pop(name, None)
    import PySide6
    import shiboken6
    from PySide6.QtCore import QObject, Property, QTimer, QUrl, Signal, Slot, qInstallMessageHandler, qVersion
    from PySide6.QtGui import QFontDatabase, QGuiApplication
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtQuick import QQuickWindow
    from quest3d.desktop_qt_adapter import DesktopQtAdapter

    versions = {"PySide6-Essentials": importlib.metadata.version("PySide6-Essentials"),
                "shiboken6": shiboken6.__version__, "Qt": qVersion()}
    if any(version != QT_VERSION for version in versions.values()) or PySide6.__version__ != QT_VERSION:
        raise ValueError(f"Unexpected UI runtime versions: {versions}")
    root = root.resolve()
    from quest3d.paths import ROOT
    if ROOT.resolve() != root:
        raise ValueError("UI checker imported another Quest3D installation")

    class ReadOnlyController:
        def get_snapshot(self):
            return dict(phase="checking", busy=False, running=False, host_running=False,
                        ready=False, monitors=[], mode="3d", depth=1.31)

        def command(self, *_args, **_kwargs):
            raise RuntimeError("Install validation must not dispatch application commands")

    class ReadOnlyLifecycle(QObject):
        changed = Signal()
        allow_close = False

        @Property(bool, notify=changed)
        def closePrompt(self):
            return False

        @Property(bool, notify=changed)
        def allowClose(self):
            return self.allow_close

        @Slot()
        def requestClose(self):
            pass

        @Slot()
        def hide(self):
            pass

        @Slot()
        def cancelClose(self):
            pass

        @Slot()
        def stopAndExit(self):
            pass

    messages = []
    previous_handler = qInstallMessageHandler(lambda _kind, _context, message: messages.append(message))
    app = QGuiApplication.instance() or QGuiApplication(["Quest3D UI installation check"])
    app.setOrganizationName("Quest3D-Install-Check")
    app.setApplicationName("Quest3D-Install-Check")
    fonts = []
    for weight in ("Regular", "Medium", "SemiBold"):
        font_id = QFontDatabase.addApplicationFont(str(root / f"resources/ui/fonts/Pretendard-{weight}.otf"))
        if font_id < 0:
            raise ValueError(f"Cannot register packaged font: {weight}")
        fonts.extend(QFontDatabase.applicationFontFamilies(font_id))
    bridge = DesktopQtAdapter(ReadOnlyController(), poll=False)
    lifecycle = ReadOnlyLifecycle()
    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("bridge", bridge)
    engine.rootContext().setContextProperty("lifecycle", lifecycle)
    engine.load(QUrl.fromLocalFile(str(root / "resources/desktop/Main.qml")))
    if not engine.rootObjects():
        raise ValueError("Main.qml did not load: " + " | ".join(messages))
    window = engine.rootObjects()[0]
    if not isinstance(window, QQuickWindow):
        raise ValueError("Main.qml did not create a QQuickWindow")
    window.show()
    def finish_check():
        lifecycle.allow_close = True
        lifecycle.changed.emit()
        app.quit()
    QTimer.singleShot(350, finish_check)
    app.exec()
    frame = window.grabWindow()
    if frame.isNull() or frame.width() < 100 or frame.height() < 100:
        raise ValueError("Actual QML window did not render a frame")
    window.hide()
    engine.deleteLater()
    app.processEvents()
    qInstallMessageHandler(previous_handler)
    if messages:
        raise ValueError("QML emitted warnings: " + " | ".join(messages))
    return {"versions": versions, "font_families": sorted(set(fonts)),
            "qml_loaded": True, "rendered_frame": [frame.width(), frame.height()],
            "application_commands_dispatched": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--resources-only", action="store_true")
    parser.add_argument("--qml-worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    if args.qml_worker:
        print(json.dumps(verify_qml(args.root), ensure_ascii=True))
        return
    result = {"resources": verify_resources(args.root), "cuda_sources": verify_runtime_sources(args.root)}
    if not args.resources_only:
        # A malformed QML handler must not leave the installer waiting forever.
        # Keep the actual GUI in a disposable child so the deadline is enforced
        # even if its JavaScript event loop or closing handler stops responding.
        try:
            checked = subprocess.run([sys.executable, str(Path(__file__).resolve()),
                                      "--root", str(args.root.resolve()), "--qml-worker"],
                                     capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        except subprocess.TimeoutExpired as error:
            raise RuntimeError("Application screen validation exceeded 30 seconds") from error
        if checked.returncode:
            raise RuntimeError("Application screen validation failed: " + checked.stderr[-12000:])
        result["runtime"] = json.loads(checked.stdout)
    result["verified"] = True
    output = json.dumps(result, indent=2, ensure_ascii=True) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(output, "utf-8")
    print(output)


if __name__ == "__main__":
    main()

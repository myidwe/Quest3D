"""Read-only desktop cursor probe and offscreen native DrawIconEx oracle.

No SetCursor, ShowCursor, SendInput, visible window or desktop capture is used.
Only our own GDI objects and private cursor handles are created and destroyed.
"""
from __future__ import annotations

import argparse
import ctypes as C
from ctypes import wintypes as W
from dataclasses import replace
from datetime import datetime
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import time

import numpy as np

from quest3d.cursor import (CursorSample, DesktopCursorOverlay, Win32CursorReader,
                            _BITMAPINFOHEADER, _ICONINFO)
from quest3d.geometry import ScreenRect, SourceGeometry
from quest3d.source_identity import SourceIdentity, SourceKind
from quest3d.stereo import StereoFrame


class Dib:
    def __init__(self, reader, width, height):
        self.r, self.dc, self.bitmap, self.previous = reader, None, None, None
        self.width, self.height = width, height
        try:
            self.dc = reader._check(reader.g.CreateCompatibleDC(None), "CreateCompatibleDC")
            header = _BITMAPINFOHEADER(biSize=C.sizeof(_BITMAPINFOHEADER), biWidth=width,
                biHeight=-height, biPlanes=1, biBitCount=32)
            pointer = C.c_void_p()
            self.bitmap = reader._check(reader.g.CreateDIBSection(self.dc, C.byref(header), 0,
                C.byref(pointer), None, 0), "CreateDIBSection")
            self.previous = reader._check(reader.g.SelectObject(self.dc, self.bitmap), "SelectObject")
            self.pixels = np.ctypeslib.as_array((C.c_ubyte * (width * height * 4)).from_address(
                pointer.value)).reshape(height, width, 4)
        except BaseException:
            self.close()
            raise

    def draw(self, handle, background, width, height, x=0, y=0):
        self.pixels[:] = background
        self.r._check(self.r.u.DrawIconEx(self.dc, x, y, handle, width, height, 0,
                                        None, 3 | 0x10), "DrawIconEx oracle")
        self.r._check(self.r.g.GdiFlush(), "GdiFlush oracle")
        return self.pixels.copy()

    def close(self):
        if self.previous:
            self.r.g.SelectObject(self.dc, self.previous)
            self.previous = None
        if self.bitmap:
            self.r.g.DeleteObject(self.bitmap)
            self.bitmap = None
        if self.dc:
            self.r.g.DeleteDC(self.dc)
            self.dc = None


def bind(r):
    for dll, name, args, restype in (
        (r.u, "LoadCursorW", [W.HINSTANCE, C.c_void_p], W.HANDLE),
        (r.u, "CreateCursor", [W.HINSTANCE, C.c_int, C.c_int, C.c_int, C.c_int,
                               C.c_void_p, C.c_void_p], W.HANDLE),
        (r.u, "CreateIconIndirect", [C.POINTER(_ICONINFO)], W.HANDLE),
        (r.u, "GetGuiResources", [W.HANDLE, W.DWORD], W.DWORD),
        (r.g, "CreateBitmap", [C.c_int, C.c_int, W.UINT, W.UINT, C.c_void_p], W.HBITMAP),
    ):
        fn = getattr(dll, name)
        fn.argtypes, fn.restype = args, restype


def make_alpha_cursor(r):
    dib = Dib(r, 32, 32)
    mask = None
    try:
        rng = np.random.default_rng(63815)
        alpha = rng.integers(0, 256, (32, 32, 1), dtype=np.uint16)
        colors = rng.integers(0, 256, (32, 32, 3), dtype=np.uint16)
        dib.pixels[:, :, :3] = (colors * alpha // 255).astype(np.uint8)
        dib.pixels[:, :, 3] = alpha[:, :, 0].astype(np.uint8)
        # CreateIconIndirect copies both bitmaps; original handles stay ours.
        mask_bits = (C.c_ubyte * 128)()
        mask = r._check(r.g.CreateBitmap(32, 32, 1, 1, mask_bits), "CreateBitmap alpha mask")
        icon = _ICONINFO(False, 4, 7, mask, dib.bitmap)
        return r._check(r.u.CreateIconIndirect(C.byref(icon)), "CreateIconIndirect alpha")
    finally:
        if mask:
            r.g.DeleteObject(mask)
        dib.close()


def make_mono_cursor(r):
    # Four repeating AND/XOR cases, including background inversion.
    and_bits = (C.c_ubyte * 128)(*([0x33] * 128))
    xor_bits = (C.c_ubyte * 128)(*([0x55] * 128))
    return r._check(r.u.CreateCursor(None, 3, 5, 32, 32, and_bits, xor_bits), "CreateCursor mono")


def oracle_case(r, name, handle):
    pixels, mask, hotspot = r.read_shape(handle)
    h, w = pixels.shape[:2]
    rng = np.random.default_rng(917)
    background = rng.integers(0, 256, (h, w, 4), dtype=np.uint8)
    background[:, :, 3] = 255
    output = StereoFrame(np.concatenate((background, background), axis=1), 9, 11,
                         "3d", False, None, (0, 0, w, h))
    bounds = ScreenRect(-500, 200, w, h)
    geometry = SourceGeometry(bounds, 11)
    identity = SourceIdentity(SourceKind.MONITOR, 0, 1, 2, 0, bounds)
    cursor = CursorSample(True, (bounds.left + hotspot[0], bounds.top + hotspot[1]),
                          hotspot, pixels, mask)
    actual, _ = DesktopCursorOverlay._composite(output, geometry, cursor, identity)
    dib = Dib(r, w, h)
    try:
        oracle = dib.draw(handle, background, w, h)
    finally:
        dib.close()
    error = np.abs(actual.bgra[:, :w, :3].astype(np.int16) - oracle[:, :, :3].astype(np.int16))
    result = {"name": name, "size": [w, h], "hotspot": list(hotspot),
              "mode": "alpha" if mask is None else "and_xor", "max_rgb_error": int(error.max()),
              "mean_rgb_error": float(error.mean()), "both_eyes_identical": bool(
                  np.array_equal(actual.bgra[:, :w], actual.bgra[:, w:])),
              "input_unchanged": bool(np.array_equal(output.bgra[:, :w], background)),
              "frame_id_preserved": actual.frame_id == output.frame_id,
              "generation_preserved": actual.generation == output.generation}
    result["passed"] = all((result["max_rgb_error"] <= 1, result["both_eyes_identical"],
                             result["input_unchanged"], result["frame_id_preserved"],
                             result["generation_preserved"]))
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=300)
    args = parser.parse_args()
    if not 100 <= args.samples <= 1000:
        raise ValueError("samples must be 100..1000")
    args.output.mkdir(parents=True, exist_ok=False)
    r = Win32CursorReader()
    bind(r)
    kernel = C.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.argtypes, kernel.GetCurrentProcess.restype = [], W.HANDLE
    process = kernel.GetCurrentProcess()
    created = []
    report = {"started": datetime.now().astimezone().isoformat(), "samples_requested": args.samples,
              "desktop_input_injected": False, "desktop_capture_used": False,
              "visible_windows_created": False, "system_cursor_changed_by_probe": False,
              "quest_verified": False, "cases": []}
    try:
        for name, resource in (("system_arrow", 32512), ("system_ibeam", 32513)):
            handle = r._check(r.u.LoadCursorW(None, resource), "LoadCursorW")
            report["cases"].append(oracle_case(r, name, handle))
        for name, builder in (("private_alpha", make_alpha_cursor), ("private_mono", make_mono_cursor)):
            handle = builder(r)
            created.append(handle)
            report["cases"].append(oracle_case(r, name, handle))
        # Warm up Python/numpy and USER32 before recording resource stability.
        for _ in range(5):
            r.sample()
        r.g.GdiFlush()
        before = {"gdi": int(r.u.GetGuiResources(process, 0)), "user": int(r.u.GetGuiResources(process, 1))}
        timings, visible, modes, shapes = [], 0, set(), set()
        for _ in range(args.samples):
            start = time.perf_counter_ns()
            cursor = r.sample()
            timings.append((time.perf_counter_ns() - start) / 1e6)
            visible += int(cursor.visible)
            if cursor.visible:
                modes.add("alpha" if cursor.and_mask is None else "and_xor")
                shapes.add(hashlib.sha256(cursor.bgra.tobytes() +
                    (cursor.and_mask.tobytes() if cursor.and_mask is not None else b"")).hexdigest())
        r.g.GdiFlush()
        after = {"gdi": int(r.u.GetGuiResources(process, 0)), "user": int(r.u.GetGuiResources(process, 1))}
        report["actual_global_cursor"] = {"samples": len(timings), "visible_samples": visible,
            "shape_modes": sorted(modes), "shape_count": len(shapes),
            "sample_ms_p50": float(np.percentile(timings, 50)),
            "sample_ms_p95": float(np.percentile(timings, 95)), "sample_ms_max": max(timings),
            "resources_before": before, "resources_after": after,
            "resource_counts_stable": before == after}
        report["passed"] = all(c["passed"] for c in report["cases"]) and before == after and visible > 0
    except Exception as exc:
        report["passed"] = False
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        for handle in created:
            r.u.DestroyIcon(handle)
        r.close()
        report["finished"] = datetime.now().astimezone().isoformat()
        report["source_sha256"] = hashlib.sha256(Path("src/quest3d/cursor.py").read_bytes()).hexdigest()
        (args.output / "result.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

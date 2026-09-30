"""Read the Windows cursor separately and composite it flat after stereo.

No input is injected and no capture/depth timestamp is refreshed.  A fresh
cursor sample can therefore move on an otherwise unchanged desktop frame.
"""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
from dataclasses import dataclass, replace
import hashlib
import math
import sys
import time

import numpy as np

from .geometry import SourceGeometry
from .source_identity import SourceIdentity, SourceKind
from .stereo import StereoFrame


@dataclass(frozen=True)
class CursorSample:
    visible: bool
    position: tuple[int, int] = (0, 0)
    hotspot: tuple[int, int] = (0, 0)
    # Alpha cursor pixels are premultiplied BGRA, as drawn by DrawIconEx.
    # Otherwise BGR is the XOR image and and_mask contains 0/255 per channel.
    bgra: np.ndarray | None = None
    and_mask: np.ndarray | None = None


@dataclass(frozen=True)
class _CursorPlan:
    rectangle: tuple[int, int, int, int]
    foreground: np.ndarray
    affected: np.ndarray
    mask: np.ndarray | None
    signature: str


class _CURSORINFO(C.Structure):
    _fields_ = [("cbSize", W.DWORD), ("flags", W.DWORD), ("hCursor", W.HANDLE),
                ("ptScreenPos", W.POINT)]


class _ICONINFO(C.Structure):
    _fields_ = [("fIcon", W.BOOL), ("xHotspot", W.DWORD), ("yHotspot", W.DWORD),
                ("hbmMask", W.HANDLE), ("hbmColor", W.HANDLE)]


class _BITMAP(C.Structure):
    _fields_ = [("bmType", W.LONG), ("bmWidth", W.LONG), ("bmHeight", W.LONG),
                ("bmWidthBytes", W.LONG), ("bmPlanes", W.WORD),
                ("bmBitsPixel", W.WORD), ("bmBits", C.c_void_p)]


class _BITMAPINFOHEADER(C.Structure):
    _fields_ = [("biSize", W.DWORD), ("biWidth", W.LONG), ("biHeight", W.LONG),
                ("biPlanes", W.WORD), ("biBitCount", W.WORD), ("biCompression", W.DWORD),
                ("biSizeImage", W.DWORD), ("biXPelsPerMeter", W.LONG),
                ("biYPelsPerMeter", W.LONG), ("biClrUsed", W.DWORD), ("biClrImportant", W.DWORD)]


class Win32CursorReader:
    """Snapshot actual cursor visibility, physical position, hotspot and pixels.

    Drawing each copied cursor into our private DIB handles monochrome AND/XOR
    and premultiplied alpha without replacing it with a generic arrow. Animated
    cursor resources currently use DrawIconEx step 0; system pointer trails and
    hardware-only accessibility effects are not reproduced.
    """
    def __init__(self):
        if sys.platform != "win32":
            raise RuntimeError("Windows cursor sampling requires Windows")
        self.u = C.WinDLL("user32", use_last_error=True)
        self.g = C.WinDLL("gdi32", use_last_error=True)
        for dll, name, args, restype in (
            (self.u, "GetCursorInfo", [C.POINTER(_CURSORINFO)], W.BOOL),
            (self.u, "GetPhysicalCursorPos", [C.POINTER(W.POINT)], W.BOOL),
            (self.u, "CopyIcon", [W.HANDLE], W.HANDLE),
            (self.u, "DestroyIcon", [W.HANDLE], W.BOOL),
            (self.u, "GetIconInfo", [W.HANDLE, C.POINTER(_ICONINFO)], W.BOOL),
            (self.u, "DrawIconEx", [W.HDC, C.c_int, C.c_int, W.HANDLE, C.c_int,
                                   C.c_int, W.UINT, W.HBRUSH, W.UINT], W.BOOL),
            (self.g, "GetObjectW", [W.HANDLE, C.c_int, C.c_void_p], C.c_int),
            (self.g, "CreateCompatibleDC", [W.HDC], W.HDC),
            (self.g, "CreateDIBSection", [W.HDC, C.c_void_p, W.UINT,
                                         C.POINTER(C.c_void_p), W.HANDLE, W.DWORD], W.HBITMAP),
            (self.g, "SelectObject", [W.HDC, W.HANDLE], W.HANDLE),
            (self.g, "DeleteObject", [W.HANDLE], W.BOOL),
            (self.g, "DeleteDC", [W.HDC], W.BOOL),
            (self.g, "GdiFlush", [], W.BOOL),
        ):
            fn = getattr(dll, name)
            fn.argtypes, fn.restype = args, restype

    @staticmethod
    def _check(value, name):
        if not value:
            raise OSError(C.get_last_error(), f"{name} failed")
        return value

    def sample(self) -> CursorSample:
        info = _CURSORINFO(cbSize=C.sizeof(_CURSORINFO))
        self._check(self.u.GetCursorInfo(C.byref(info)), "GetCursorInfo")
        if not info.flags & 1 or info.flags & 2 or not info.hCursor:
            return CursorSample(False)
        point = W.POINT()
        self._check(self.u.GetPhysicalCursorPos(C.byref(point)), "GetPhysicalCursorPos")
        copied = self._check(self.u.CopyIcon(info.hCursor), "CopyIcon")
        try:
            pixels, mask, hotspot = self.read_shape(copied)
            return CursorSample(True, (point.x, point.y), hotspot, pixels, mask)
        finally:
            self.u.DestroyIcon(copied)

    def read_shape(self, handle) -> tuple[np.ndarray, np.ndarray | None, tuple[int, int]]:
        """Read an existing icon/cursor handle without taking ownership of it."""
        icon = _ICONINFO()
        self._check(self.u.GetIconInfo(handle, C.byref(icon)), "GetIconInfo")
        dc = dib = previous = None
        try:
            bitmap = _BITMAP()
            self._check(self.g.GetObjectW(icon.hbmColor or icon.hbmMask, C.sizeof(bitmap),
                                         C.byref(bitmap)), "GetObjectW")
            width, height = bitmap.bmWidth, bitmap.bmHeight
            if not icon.hbmColor:
                if height % 2:
                    raise ValueError("Monochrome cursor mask has odd double height")
                height //= 2
            if not (0 < width <= 512 and 0 < height <= 512):
                raise ValueError("Unsupported cursor raster dimensions")
            header = _BITMAPINFOHEADER(biSize=C.sizeof(_BITMAPINFOHEADER), biWidth=width,
                biHeight=-height, biPlanes=1, biBitCount=32, biCompression=0)
            dc = self._check(self.g.CreateCompatibleDC(None), "CreateCompatibleDC")
            pointer = C.c_void_p()
            dib = self._check(self.g.CreateDIBSection(dc, C.byref(header), 0,
                C.byref(pointer), None, 0), "CreateDIBSection")
            previous = self._check(self.g.SelectObject(dc, dib), "SelectObject")
            if previous == C.c_void_p(-1).value:
                previous = None
                raise OSError("SelectObject returned HGDI_ERROR")
            raster = np.ctypeslib.as_array((C.c_ubyte * (width * height * 4)).from_address(
                pointer.value)).reshape(height, width, 4)

            def draw(flags):
                raster.fill(0)
                self._check(self.u.DrawIconEx(dc, 0, 0, handle, width, height, 0,
                                             None, flags | 0x10), "DrawIconEx")
                self._check(self.g.GdiFlush(), "GdiFlush")
                return raster.copy()

            pixels = draw(3)
            mask = None
            if not np.any(pixels[:, :, 3]):
                mask = draw(1)[:, :, :3]
                pixels = draw(2)
                if not np.all((mask == 0) | (mask == 255)):
                    raise ValueError("Cursor AND mask is not monochrome")
            return pixels, mask, (int(icon.xHotspot), int(icon.yHotspot))
        finally:
            if previous:
                self.g.SelectObject(dc, previous)
            if dib:
                self.g.DeleteObject(dib)
            if dc:
                self.g.DeleteDC(dc)
            if icon.hbmMask:
                self.g.DeleteObject(icon.hbmMask)
            if icon.hbmColor:
                self.g.DeleteObject(icon.hbmColor)

    def close(self):
        pass  # Every GDI object and copied cursor is released within each sample.


class DesktopCursorOverlay:
    """Single-presentation-thread cursor overlay, with optional one-result reuse.

    ``immutable_base=True`` is an explicit caller lifetime contract: after a
    StereoFrame has been submitted, neither its pixels nor any aliases (including
    Torch tensors) may ever be changed. A frozen dataclass or a NumPy writeable
    flag alone cannot prove this. The default does not assume that contract.
    Cached results own separate storage, are read-only and must also remain
    immutable. No cursor handle/shape is cached: every tick samples Windows.
    """
    def __init__(self, *, enabled=True, supported=True, reader=None, immutable_base=False):
        if type(immutable_base) is not bool:
            raise TypeError("immutable_base must be an explicit boolean")
        self.enabled = bool(enabled)
        self.supported = supported is True
        self.immutable_base = immutable_base
        self.reader = reader
        self._cache = None
        self._signature = "hidden"
        self._state = {"enabled": self.enabled, "supported": self.supported,
                       "status": "waiting", "visible": False, "changed": False,
                       "visual_signature": self._signature, "samples": 0, "composites": 0,
                       "errors": 0, "last_error": None, "sample_ms": 0.0,
                       "immutable_base": immutable_base, "cache_hits": 0,
                       "copied_bytes_total": 0}
        self.last_metrics = {}
        self._closed = False

    @classmethod
    def from_capture(cls, capture, enabled=True, *, immutable_base=False):
        return cls(enabled=enabled,
                   supported=getattr(capture, "cursor_exclusion_supported", False) is True,
                   immutable_base=immutable_base)

    def _finish(self, status, *, signature="hidden", visible=False):
        if not visible:
            self._cache = None
        self._state.update(status=status, visible=visible, changed=signature != self._signature,
                           visual_signature=signature)
        self._signature = signature

    def sample_and_composite(self, output: StereoFrame, source) -> StereoFrame:
        self.last_metrics = {"cursor_read_ms": 0.0, "cursor_plan_ms": 0.0,
                             "cursor_copy_ms": 0.0, "cursor_blend_ms": 0.0,
                             "cursor_cache_hit": False, "cursor_copied_bytes": 0}
        self._state["sample_ms"] = 0.0
        if self._closed or not self.enabled or not self.supported:
            self._finish("closed" if self._closed else "disabled" if not self.enabled
                         else "capture_cursor_exclusion_unconfirmed")
            return output
        identity = getattr(source, "source_identity", None)
        if not isinstance(identity, SourceIdentity) or identity.kind != SourceKind.MONITOR:
            self._finish("source_not_monitor")
            return output
        started = time.perf_counter_ns()
        try:
            geometry = getattr(source, "geometry", None)
            if not isinstance(geometry, SourceGeometry):
                raise ValueError("Cursor overlay requires physical source geometry")
            if (output.frame_id != source.frame_id or output.generation != source.geometry_generation
                    or geometry.generation != source.geometry_generation):
                raise ValueError("Cursor source geometry does not match stereo output")
            identity.validate_region((geometry.bounds.left, geometry.bounds.top,
                                      geometry.bounds.width, geometry.bounds.height))
            if self.reader is None:
                self.reader = Win32CursorReader()
            read_started = time.perf_counter_ns()
            try:
                cursor = self.reader.sample()
            finally:
                self.last_metrics["cursor_read_ms"] = (time.perf_counter_ns() - read_started) / 1e6
            self._state["samples"] += 1
            if not cursor.visible:
                self._finish("cursor_hidden")
                return output
            plan_started = time.perf_counter_ns()
            plan = self._plan(output, geometry, cursor, identity)
            self.last_metrics["cursor_plan_ms"] = (time.perf_counter_ns() - plan_started) / 1e6
            if plan is None:
                result, signature = output, "hidden"
            else:
                signature = plan.signature
                # Retain the actual base object, not id(base), so allocator id
                # reuse cannot alias a previous frame. Scope changes invalidate
                # even if a caller happens to retain the same output object.
                scope = (getattr(source, "source_id", None), source.frame_id,
                         source.geometry_generation, source.captured_ns, geometry, identity,
                         output.mode, output.content_rect, output.bgra.shape, output.bgra.strides)
                cached = self._cache if self.immutable_base else None
                if (cached is not None and cached[0] is output and cached[1] is output.bgra
                        and cached[2] == scope and cached[3] == signature):
                    result = cached[4]
                    self.last_metrics["cursor_cache_hit"] = True
                    self._state["cache_hits"] += 1
                else:
                    self._cache = None
                    result = self._apply(output, plan, self.last_metrics)
                    self._state["composites"] += 1
                    self._state["copied_bytes_total"] += self.last_metrics["cursor_copied_bytes"]
                    if self.immutable_base:
                        result.bgra.setflags(write=False)
                        self._cache = (output, output.bgra, scope, signature, result)
            self._state["last_error"] = None
            self._finish("visible" if signature != "hidden" else "outside_source",
                         signature=signature, visible=signature != "hidden")
            return result
        except (OSError, RuntimeError, ValueError, TypeError, AttributeError) as exc:
            self._state["errors"] += 1
            self._state["last_error"] = f"{type(exc).__name__}: {exc}"
            self._finish("error")
            return output
        finally:
            self._state["sample_ms"] = (time.perf_counter_ns() - started) / 1e6

    @staticmethod
    def _plan(output, geometry, cursor, identity):
        image, pixels = output.bgra, cursor.bgra
        if (not isinstance(image, np.ndarray) or image.dtype != np.uint8 or image.ndim != 3
                or image.shape[2] != 4 or image.shape[1] % 2):
            raise ValueError("Cursor overlay requires even-width BGRA8 stereo output")
        if (not isinstance(pixels, np.ndarray) or pixels.dtype != np.uint8 or pixels.ndim != 3
                or pixels.shape[2] != 4 or not all(0 < n <= 512 for n in pixels.shape[:2])):
            raise ValueError("Invalid cursor BGRA8 raster")
        if cursor.and_mask is not None:
            if (cursor.and_mask.shape != pixels[:, :, :3].shape
                    or cursor.and_mask.dtype != np.uint8
                    or not np.all((cursor.and_mask == 0) | (cursor.and_mask == 255))):
                raise ValueError("Invalid cursor AND mask")
        height, packed_width = image.shape[:2]
        eye_width = packed_width // 2
        cx, cy, cw, ch = output.content_rect
        if (not all(type(v) is int for v in (cx, cy, cw, ch)) or cw <= 0 or ch <= 0
                or cx < 0 or cy < 0 or cx + cw > eye_width or cy + ch > height):
            raise ValueError("Invalid stereo content rectangle")
        bounds = geometry.bounds
        left = cursor.position[0] - cursor.hotspot[0] - bounds.left
        top = cursor.position[1] - cursor.hotspot[1] - bounds.top
        sx, sy = cw / bounds.width, ch / bounds.height
        x0, y0 = max(cx, math.floor(cx + left * sx)), max(cy, math.floor(cy + top * sy))
        x1 = min(cx + cw, math.ceil(cx + (left + pixels.shape[1]) * sx))
        y1 = min(cy + ch, math.ceil(cy + (top + pixels.shape[0]) * sy))
        if x0 >= x1 or y0 >= y1:
            return None
        # Pixel-center inverse mapping keeps hotspot/crop alignment at fractional
        # downscale factors. Nearest sampling preserves exact AND/XOR semantics.
        xs = np.floor((np.arange(x0, x1) + .5 - cx) / sx - left).astype(np.int32)
        ys = np.floor((np.arange(y0, y1) + .5 - cy) / sy - top).astype(np.int32)
        valid = (ys[:, None] >= 0) & (ys[:, None] < pixels.shape[0]) & (
            xs[None, :] >= 0) & (xs[None, :] < pixels.shape[1])
        xs, ys = xs.clip(0, pixels.shape[1] - 1), ys.clip(0, pixels.shape[0] - 1)
        foreground = pixels[ys[:, None], xs[None, :]]
        if cursor.and_mask is None:
            affected = valid & (foreground[:, :, 3] != 0)
            mask = None
        else:
            mask = cursor.and_mask[ys[:, None], xs[None, :]]
            affected = valid & (np.any(mask != 255, axis=2) | np.any(foreground[:, :, :3] != 0, axis=2))
        if not np.any(affected):
            return None
        digest = hashlib.sha256()
        digest.update(repr((x0, y0, x1, y1, output.content_rect, geometry, identity,
                            cursor.position, cursor.hotspot, pixels.shape)).encode())
        # Include the original shape too: DPI/resource changes must invalidate
        # even when nearest sampling happens to produce the same visible pixels.
        digest.update(pixels.tobytes())
        if cursor.and_mask is not None:
            digest.update(cursor.and_mask.tobytes())
        digest.update(foreground.tobytes())
        digest.update(affected.tobytes())
        if mask is not None:
            digest.update(mask.tobytes())
        return _CursorPlan((x0, y0, x1, y1), foreground, affected, mask, digest.hexdigest())

    @staticmethod
    def _apply(output, plan, metrics):
        image = output.bgra
        x0, y0, x1, y1 = plan.rectangle
        foreground, affected, mask = plan.foreground, plan.affected, plan.mask
        eye_width = image.shape[1] // 2
        copy_started = time.perf_counter_ns()
        result = image.copy()
        metrics["cursor_copy_ms"] = (time.perf_counter_ns() - copy_started) / 1e6
        metrics["cursor_copied_bytes"] = image.nbytes
        blend_started = time.perf_counter_ns()
        for offset in (0, eye_width):
            destination = result[y0:y1, offset + x0:offset + x1, :3]
            if mask is None:
                alpha = foreground[:, :, 3:4].astype(np.uint16)
                blended = np.minimum(255, foreground[:, :, :3].astype(np.uint16) +
                    (destination.astype(np.uint16) * (255 - alpha) + 127) // 255).astype(np.uint8)
            else:
                blended = (destination & mask) ^ foreground[:, :, :3]
            destination[affected] = blended[affected]
        metrics["cursor_blend_ms"] = (time.perf_counter_ns() - blend_started) / 1e6
        return replace(output, bgra=result)

    @staticmethod
    def _composite(output, geometry, cursor, identity):
        """Uncached pure compositor retained for independent pixel fixtures."""
        plan = DesktopCursorOverlay._plan(output, geometry, cursor, identity)
        if plan is None:
            return output, "hidden"
        return DesktopCursorOverlay._apply(output, plan, {}), plan.signature

    def snapshot(self):
        return {**self._state, **self.last_metrics}

    def close(self):
        self._cache = None
        if self.reader is not None:
            self.reader.close()
        self._closed = True
        self._finish("closed")

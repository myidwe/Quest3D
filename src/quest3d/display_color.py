"""Read-only Win32 Advanced Color and SDR white metadata by GDI monitor name.

Declarations follow wingdi.h. No DisplayConfigSetDeviceInfo call exists here.
Legacy Advanced Color does not distinguish HDR from managed SDR, so it remains
unknown instead of silently being treated as HDR.
"""
import ctypes as C
from dataclasses import asdict, dataclass
import math
import sys


U32, I32 = C.c_uint32, C.c_int32


class Luid(C.Structure):
    _fields_ = [("low", U32), ("high", I32)]


class Rational(C.Structure):
    _fields_ = [("numerator", U32), ("denominator", U32)]


class PathSource(C.Structure):
    _fields_ = [("adapter", Luid), ("id", U32), ("mode_index", U32), ("flags", U32)]


class PathTarget(C.Structure):
    _fields_ = [("adapter", Luid), ("id", U32), ("mode_index", U32), ("technology", I32),
                ("rotation", U32), ("scaling", U32), ("refresh", Rational),
                ("scanline", U32), ("available", I32), ("flags", U32)]


class DisplayPath(C.Structure):
    _fields_ = [("source", PathSource), ("target", PathTarget), ("flags", U32)]


class ModePayload(C.Union):
    _fields_ = [("opaque", C.c_uint64 * 6)]  # Full 48-byte, 8-byte aligned union.


class DisplayMode(C.Structure):
    _fields_ = [("type", U32), ("id", U32), ("adapter", Luid), ("data", ModePayload)]


class DeviceHeader(C.Structure):
    _fields_ = [("type", U32), ("size", U32), ("adapter", Luid), ("id", U32)]


class SourceName(C.Structure):
    _fields_ = [("header", DeviceHeader), ("name", C.c_wchar * 32)]


class AdvancedColor(C.Structure):
    _fields_ = [("header", DeviceHeader), ("flags", U32), ("encoding", U32), ("bits", U32)]


class AdvancedColor2(C.Structure):
    _fields_ = [("header", DeviceHeader), ("flags", U32), ("encoding", U32),
                ("bits", U32), ("mode", U32)]


class WhiteLevel(C.Structure):
    _fields_ = [("header", DeviceHeader), ("level", U32)]


@dataclass(frozen=True)
class DisplayColor:
    device_name: str
    active_mode: str
    hdr_enabled: bool | None
    bits_per_channel: int | None
    sdr_white_raw: int | None
    sdr_white_nits: float | None
    sc_rgb_sdr_white_scale: float | None
    query_errors: dict


def require_hdr_color(device_name, colors):
    """Reject missing/clone/unknown profiles instead of guessing a white level."""
    matches = [color for color in colors if color.device_name.casefold() == device_name.casefold()]
    if len(matches) != 1:
        raise RuntimeError("HDR capture requires exactly one color profile for the selected monitor")
    color = matches[0]
    scale = color.sc_rgb_sdr_white_scale
    if (color.hdr_enabled is not True or color.active_mode != "hdr"
            or not isinstance(scale, (int, float)) or isinstance(scale, bool)
            or not .001 <= scale <= 100 or not math.isfinite(scale)):
        raise RuntimeError("Experimental HDR capture requires an active HDR output and a measured SDR white")
    return color


def read_display_colors():
    if sys.platform != "win32":
        raise OSError("Display color metadata requires a Windows user session")
    expected = [(DisplayPath, 72), (DisplayMode, 64), (DeviceHeader, 20),
                (SourceName, 84), (AdvancedColor, 32), (AdvancedColor2, 36), (WhiteLevel, 24)]
    if any(C.sizeof(kind) != size for kind, size in expected):
        raise RuntimeError("Win32 display structures have an unexpected ABI")
    api = C.WinDLL("user32", use_last_error=True)
    api.GetDisplayConfigBufferSizes.argtypes = [U32, C.POINTER(U32), C.POINTER(U32)]
    api.GetDisplayConfigBufferSizes.restype = I32
    api.QueryDisplayConfig.argtypes = [U32, C.POINTER(U32), C.POINTER(DisplayPath),
                                      C.POINTER(U32), C.POINTER(DisplayMode), C.c_void_p]
    api.QueryDisplayConfig.restype = I32
    api.DisplayConfigGetDeviceInfo.argtypes = [C.POINTER(DeviceHeader)]
    api.DisplayConfigGetDeviceInfo.restype = I32
    paths = None
    for _ in range(3):
        path_count, mode_count = U32(), U32()
        result = api.GetDisplayConfigBufferSizes(2, C.byref(path_count), C.byref(mode_count))
        if result:
            raise C.WinError(result)
        if path_count.value > 128 or mode_count.value > 1024:
            raise RuntimeError("Unexpectedly large display topology")
        paths = (DisplayPath * path_count.value)()
        modes = (DisplayMode * mode_count.value)()
        result = api.QueryDisplayConfig(2, C.byref(path_count), paths, C.byref(mode_count), modes, None)
        if result == 122:  # Topology changed between size and data query.
            continue
        if result:
            raise C.WinError(result)
        break
    else:
        raise RuntimeError("Display topology kept changing during color query")

    def query(kind, info_type, adapter, ident):
        value = kind()
        value.header = DeviceHeader(info_type, C.sizeof(kind), adapter, ident)
        result = api.DisplayConfigGetDeviceInfo(C.byref(value.header))
        return value, result

    colors = []
    for path in paths[:path_count.value]:
        errors = {}
        name, code = query(SourceName, 1, path.source.adapter, path.source.id)
        if code:
            raise C.WinError(code)  # An unidentified output must not match another monitor.
        advanced, code = query(AdvancedColor2, 15, path.target.adapter, path.target.id)
        if code:
            errors["advanced_color_2"] = code
            legacy, legacy_code = query(AdvancedColor, 9, path.target.adapter, path.target.id)
            if legacy_code:
                errors["advanced_color"] = legacy_code
                mode, hdr, bits = "unknown", None, None
            else:
                mode = "advanced_unknown" if legacy.flags & 2 else "sdr"
                hdr, bits = (None if mode == "advanced_unknown" else False), legacy.bits
        else:
            mode = {0: "sdr", 1: "wcg", 2: "hdr"}.get(advanced.mode, "unknown")
            hdr, bits = (mode == "hdr" if mode != "unknown" else None), advanced.bits
        white, code = query(WhiteLevel, 11, path.target.adapter, path.target.id)
        if code or not white.level:
            errors["sdr_white"] = code or "zero_white_level"
            raw = nits = scale = None
        else:
            raw = white.level
            nits = raw * 80 / 1000
            scale = raw / 1000 if hdr else (1.0 if hdr is False else None)
        colors.append(DisplayColor(name.name, mode, hdr, bits, raw, nits, scale, errors))
    return colors


if __name__ == "__main__":
    import json
    print(json.dumps([asdict(value) for value in read_display_colors()], indent=2))

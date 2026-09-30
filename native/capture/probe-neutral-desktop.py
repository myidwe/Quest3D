"""Measure only owned neutral HWND patches through actual desktop WGC FP16/CUDA.

No screenshot, OS color-setting change, network, model, or shared publisher.
The small no-activate windows are closed even when capture fails.
"""
from pathlib import Path
import argparse
import ctypes
from ctypes import wintypes
from datetime import datetime
import json
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--package", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
args.output.mkdir(parents=True, exist_ok=False)
sys.path.insert(0, str(args.package.resolve(strict=True)))
import torch
import wc_cuda
from quest3d.capture import list_monitors
from quest3d.display_color import read_display_colors, require_hdr_color
from quest3d.tonemap_cuda import CudaToneMapper
from window_fixture import ThreadedOwnedWindowFixture

assert wc_cuda.QUEST3D_PATCH_VERSION == 2
torch.set_num_threads(2)
primary = next(m for m in list_monitors() if m.is_primary)
profile = require_hdr_color(primary.device_name, read_display_colors())
rows = []
native = bridge = mapper = None
report = {"started_at": datetime.now().astimezone().isoformat(), "monitor": primary.device_name,
          "sdr_white_scale": profile.sc_rgb_sdr_white_scale, "source": "own GDI HWNDs in real HDR desktop",
          "capture": "actual WGC rgba16f, native CUDA texture lease", "images_saved": False,
          "pixel_arrays_saved": False, "system_color_settings_changed": False,
          "quest_color_verified": False, "samples": rows}
try:
    mapper = CudaToneMapper()
    with ThreadedOwnedWindowFixture() as fixture:
        report["foreground_before"] = fixture.foreground_before
        windows = []
        for i, value in enumerate((64, 128, 192, 255)):
            hwnd = fixture.create(left=primary.bounds.left + 80 + i * 310, top=primary.bounds.top + 90,
                                  width=280, height=180, color=value * 0x010101)
            origin = wintypes.POINT(0, 0)
            user = ctypes.WinDLL("user32", use_last_error=True)
            user.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
            user.ClientToScreen.restype = wintypes.BOOL
            if not user.ClientToScreen(hwnd, ctypes.byref(origin)):
                raise ctypes.WinError(ctypes.get_last_error())
            windows.append((hwnd, value, origin.x - primary.bounds.left + 65, origin.y - primary.bounds.top + 50))
        user.WindowFromPoint.argtypes = [wintypes.POINT]
        user.WindowFromPoint.restype = wintypes.HWND
        user.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        user.GetAncestor.restype = wintypes.HWND
        native = wc_cuda._NativeWcCapture(luid=wc_cuda.get_luid(), monitor_index=primary.index,
                                        cursor_capture=False, color_format="rgba16f")
        native.start()
        last = 0
        stream = torch.cuda.Stream()
        for index in range(4):
            for hwnd, *_ in windows:
                fixture.paint(hwnd)
            deadline = time.monotonic() + 3
            item = None
            while time.monotonic() < deadline:
                item = native.get_frame(last, timeout=.1)
                if item:
                    break
                if native.get_last_error():
                    raise RuntimeError(native.get_last_error())
            if not item:
                raise TimeoutError("No actual desktop frame")
            frame, last = item
            if bridge is None:
                bridge = wc_cuda._DX11ToPyTorchBridge(frame.texture_ptr, frame.width, frame.height,
                                                    color_format=frame.color_format)
            pixels = bridge.update(stream, 0)
            native.release_frame(last)
            for hwnd, expected, x, y in windows:
                points = ((x,y), (x+95,y), (x,y+47), (x+95,y+47), (x+48,y+24))
                visible = all(int(user.GetAncestor(user.WindowFromPoint(wintypes.POINT(px+primary.bounds.left, py+primary.bounds.top)), 2) or 0) == hwnd for px, py in points)
                if not visible:
                    rows.append({"capture_frame_id": last, "gdi_neutral_code": expected,
                                 "sample_rejected": "owned patch occluded at observation; no pixel statistics collected"})
                    continue
                roi = pixels[y:y+48, x:x+96].contiguous()
                raw = roi[..., :3].float().cpu()
                bgra = mapper(roi, sdr_white_scale=profile.sc_rgb_sdr_white_scale).cpu()
                rgb = bgra[..., [2, 1, 0]].to(torch.float32)
                rows.append({"capture_frame_id": last, "gdi_neutral_code": expected,
                    "raw_rgb_mean": raw.mean((0, 1)).tolist(), "raw_rg_abs_max": float((raw[...,0]-raw[...,1]).abs().max()),
                    "raw_bg_abs_max": float((raw[...,2]-raw[...,1]).abs().max()),
                    "tonemapped_rgb_mean": rgb.mean((0, 1)).tolist(),
                    "tonemapped_rg_abs_max": float((rgb[...,0]-rgb[...,1]).abs().max()),
                    "tonemapped_bg_abs_max": float((rgb[...,2]-rgb[...,1]).abs().max())})
        report["foreground_unchanged_before_cleanup"] = int(fixture.user.GetForegroundWindow() or 0) == fixture.foreground_before
    report["owned_windows_closed"] = not fixture.thread.is_alive()
    report["status"] = "measured; excluded any occluded patches"
finally:
    if native:
        native.stop()
        owner = native.lease_stats()[0]
        if owner is not None:
            native.release_frame(owner)
    if bridge:
        bridge.close()
    if mapper:
        mapper.close()
    report["finished_at"] = datetime.now().astimezone().isoformat()
    (args.output / "result.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
print(json.dumps(report, indent=2))

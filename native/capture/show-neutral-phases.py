"""Temporarily show owned neutral patches for Quest color-stage measurement.

Only this process's no-activate HWND is changed/closed. No screenshot, GPU
worker, desktop setting, foreign window, input injection, or publisher change.
Run alongside the bounded Quest debug color statistics after actual connection.
"""
import argparse
import ctypes
from ctypes import wintypes as w
from datetime import datetime
import json
from pathlib import Path
import time

from quest3d.capture import list_monitors
from window_fixture import OwnedWindowFixture


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seconds-per-phase", type=float, default=8)
    args = parser.parse_args()
    if not 4 <= args.seconds_per_phase <= 10:
        parser.error("Use 4..10 seconds per stable phase")
    args.output.mkdir(parents=True, exist_ok=False)
    monitor = next(m for m in list_monitors() if m.is_primary)
    report = {"started_at": datetime.now().astimezone().isoformat(), "monitor": monitor.device_name,
              "source_size": [monitor.bounds.width, monitor.bounds.height], "phases": [],
              "system_settings_changed": False, "host_or_publisher_changed": False,
              "screenshots_saved": False, "headset_color_verified": False}
    try:
        with OwnedWindowFixture() as fixture:
            user = fixture.user
            hwnd = fixture.create(left=monitor.bounds.left, top=monitor.bounds.top,
                                  width=monitor.bounds.width, height=monitor.bounds.height, color=0x808080)
            fixture.owned(hwnd)
            user.SetWindowLongPtrW.argtypes = [w.HWND, ctypes.c_int, ctypes.c_ssize_t]
            user.SetWindowLongPtrW.restype = ctypes.c_ssize_t
            user.WindowFromPoint.argtypes = [w.POINT]
            user.WindowFromPoint.restype = w.HWND
            user.GetAncestor.argtypes = [w.HWND, w.UINT]
            user.GetAncestor.restype = w.HWND
            ctypes.set_last_error(0)
            old_style = user.SetWindowLongPtrW(hwnd, -16, 0x90000000)  # own popup + visible
            if old_style == 0 and ctypes.get_last_error():
                raise ctypes.WinError(ctypes.get_last_error())
            b = monitor.bounds
            if not user.SetWindowPos(hwnd, ctypes.c_void_p(-1), b.left, b.top, b.width, b.height, 0x30):
                raise ctypes.WinError(ctypes.get_last_error())
            for code in (128, 192, 255):
                fixture.windows[fixture.owned(hwnd)]["color"] = code * 0x010101
                fixture.paint(hwnd)
                phase = {"gdi_neutral_rgb": [code]*3, "started_at": datetime.now().astimezone().isoformat(),
                         "started_qpc_ns": time.perf_counter_ns(), "visible_center_checks": 0}
                report["phases"].append(phase)
                deadline = time.monotonic() + args.seconds_per_phase
                while time.monotonic() < deadline:
                    for fx, fy in ((.25,.25),(.75,.25),(.5,.5),(.25,.75),(.75,.75)):
                        point = w.POINT(b.left + round(b.width*fx), b.top + round(b.height*fy))
                        if int(user.GetAncestor(user.WindowFromPoint(point), 2) or 0) != hwnd:
                            raise RuntimeError("Central calibration area became occluded; stop rather than mislabel pixels")
                    phase["visible_center_checks"] += 1
                    fixture.paint(hwnd)  # changing corner stripe keeps real WGC source fresh
                    fixture.pump(.05)
                phase["finished_at"] = datetime.now().astimezone().isoformat()
                phase["finished_qpc_ns"] = time.perf_counter_ns()
            report["foreground_unchanged"] = int(user.GetForegroundWindow() or 0) == fixture.foreground_before
        report["owned_windows_closed"] = True
        report["status"] = "phases_displayed; compare with actual Quest sample logs separately"
    finally:
        report["finished_at"] = datetime.now().astimezone().isoformat()
        (args.output / "phases.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()

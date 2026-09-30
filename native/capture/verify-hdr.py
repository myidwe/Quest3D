"""Inspect actual WGC/CUDA pixel statistics. Saves no screenshot and changes no HDR setting."""
from pathlib import Path
import argparse
import hashlib
import json
import sys
import time
import zipfile

ROOT = Path(__file__).resolve().parents[2]
parser = argparse.ArgumentParser()
parser.add_argument("--wheel", type=Path, required=True)
args = parser.parse_args()
wheel = args.wheel.resolve(strict=True)
digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
unpacked = ROOT / "artifacts/capture/hdr-experimental" / f"package-{digest[:12]}"
unpacked.mkdir(parents=True, exist_ok=True)
with zipfile.ZipFile(wheel) as archive:
    for member in archive.infolist():
        if not (unpacked / member.filename).resolve().is_relative_to(unpacked.resolve()):
            raise RuntimeError("Unsafe wheel member path")
    archive.extractall(unpacked)
sys.path.insert(0, str(unpacked))

import torch
import wc_cuda
from quest3d.capture import list_monitors

assert wc_cuda.QUEST3D_PATCH_VERSION == 2
results = []
for monitor in list_monitors():
    if monitor.index == 0:
        continue  # WGC captures one physical monitor, not the virtual bounding rectangle.
    for color_format in ("bgra8", "rgba16f"):
        native = wc_cuda._NativeWcCapture(luid=wc_cuda.get_luid(), monitor_index=monitor.index,
                                        cursor_capture=False, color_format=color_format)
        bridge = None
        native.start()
        try:
            deadline = time.monotonic() + 5
            frame = None
            while time.monotonic() < deadline:
                item = native.get_frame(0, timeout=0.1)
                if item:
                    frame, frame_id = item
                    break
                if native.get_last_error():
                    raise RuntimeError(native.get_last_error())
            if frame is None:
                raise TimeoutError("No actual WGC frame")
            assert frame.color_format == color_format
            stream = torch.cuda.Stream()
            bridge = wc_cuda._DX11ToPyTorchBridge(frame.texture_ptr, frame.width, frame.height,
                                                color_format=frame.color_format)
            copy_start = time.perf_counter_ns()
            tensor = bridge.update(stream, 0)
            copy_ms = (time.perf_counter_ns() - copy_start) / 1e6
            native.release_frame(frame_id)
            expected = torch.float16 if color_format == "rgba16f" else torch.uint8
            assert tensor.dtype == expected and tensor.is_cuda
            assert tensor.stride(0) * tensor.element_size() == frame.row_pitch_bytes
            crop = tensor[:frame.original_height, :frame.original_width]
            rgb = crop[..., :3].float()
            alpha = crop[..., 3].float()
            values = dict(monitor_index=monitor.index, primary=monitor.is_primary,
                          device_name=monitor.device_name,
                          color_format=color_format, dtype=str(tensor.dtype),
                          source_size=[frame.original_width, frame.original_height],
                          aligned_size=[frame.width, frame.height], row_pitch_bytes=frame.row_pitch_bytes,
                          copy_ms=copy_ms, finite_rgb=bool(torch.isfinite(rgb).all()),
                          rgb_min=float(rgb.min()), rgb_max=float(rgb.max()),
                          rgb_mean=float(rgb.mean()), rgb_above_one_count=int((rgb > 1).sum()),
                          rgb_negative_count=int((rgb < 0).sum()),
                          alpha_min=float(alpha.min()), alpha_max=float(alpha.max()),
                          source_crop_matches=bool(torch.equal(crop[:65, :129].contiguous(), tensor[:65, :129])),
                          lease_released=native.lease_stats()[0] is None)
            assert values["finite_rgb"] and values["lease_released"] and values["source_crop_matches"]
            results.append(values)
        finally:
            native.stop()
            owner = native.lease_stats()[0]
            if owner is not None:
                native.release_frame(owner)
            if bridge:
                bridge.close()

report = dict(verified_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"), wheel=str(wheel), sha256=digest,
              package_path=str(unpacked), actual_desktop_pixels=True, screenshot_saved=False,
              windows_hdr_changed=False, tone_mapping_verified=False, color_accuracy_verified=False,
              quest_display_verified=False, captures=results)
output = ROOT / "artifacts/capture/hdr-experimental" / f"verification-{time.strftime('%Y%m%d-%H%M%S')}.json"
output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
print(json.dumps({"report": str(output), **report}, indent=2))

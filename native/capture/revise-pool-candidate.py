"""One-time comparison: restore upstream dispatcher pool after fixing shutdown."""
from pathlib import Path

root = Path(__file__).resolve().parents[2]
prepare = root / "native/capture/prepare-window-dependency.py"
text = prepare.read_text(encoding="utf-8")
prefix = "            new_pool = '''"
start = text.index(prefix)
end = text.index("'''", start + len(prefix)) + 3
old_pool = "let frame_pool = Direct3D11CaptureFramePool::Create(&direct3d_device, pixel_format, 1, item.Size()?)?;"
free_pool = text[start + len(prefix):end-3]
vendor = root / "native/capture/vendor/windows-capture-2.0.0-alpha.7/src/graphics_capture_api.rs"
source = vendor.read_text(encoding="utf-8")
assert source.count(free_pool) == 1
vendor.write_text(source.replace(free_pool, old_pool), encoding="utf-8", newline="\n")
prepare.write_text(text[:start] + "            new_pool = old_pool" + text[end:], encoding="utf-8", newline="\n")

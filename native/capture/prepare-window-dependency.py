"""Verify a checksum-pinned crate with explicit content-size and teardown fixes."""
import hashlib
from pathlib import Path
import tarfile

ROOT = Path(__file__).resolve().parents[2]
CRATE_HASH = "6dcd037f0a621bbbb3d6a6b895ddd5b0d691b008b2f9d6980e2d367b16ab5bf6"
crates = list((ROOT / ".tools/capture-build/cargo/registry/cache").glob("*/windows-capture-2.0.0-alpha.7.crate"))
if len(crates) != 1 or hashlib.sha256(crates[0].read_bytes()).hexdigest() != CRATE_HASH:
    raise RuntimeError("Expected pinned windows-capture crate is unavailable")
vendor = ROOT / "native/capture/vendor"
vendor.mkdir(exist_ok=True)
destination = vendor / "windows-capture-2.0.0-alpha.7"
if not destination.exists():
    with tarfile.open(crates[0], "r:gz") as archive:
        for member in archive.getmembers():
            if not (vendor / member.name).resolve().is_relative_to(destination.resolve()):
                raise RuntimeError("Crate extraction target escaped the expected package")
        archive.extractall(vendor, filter="data")
frame = destination / "src/frame.rs"
source = frame.read_text(encoding="utf-8")
anchor = "    /// Gets the timestamp of the frame."
addition = '''    /// Actual WGC content bounds, distinct from old pool texture size during resize.
    /// Quest3D local accessor; no pixels or ownership semantics are changed.
    #[inline]
    pub fn content_size(&self) -> Result<windows::Graphics::SizeInt32, windows::core::Error> {
        self.capture_frame.ContentSize()
    }

'''
frame_drop = '''impl Drop for Frame<'_> {
    fn drop(&mut self) {
        // Return the WGC frame to its pool explicitly on every callback path.
        // Derived COM Release alone is not our frame-return contract.
        let _ = self.capture_frame.Close();
    }
}

'''
if addition not in source:
    if source.count(anchor) != 1:
        raise RuntimeError("Unexpected frame source; existing files preserved")
    source = source.replace(anchor, addition + anchor)
    frame.write_text(source, encoding="utf-8", newline="\n")
drop_anchor = "impl<'a> Frame<'a> {"
if frame_drop not in source:
    if source.count(drop_anchor) != 1:
        raise RuntimeError("Unexpected frame implementation; source preserved")
    source = source.replace(drop_anchor, frame_drop + drop_anchor)
    frame.write_text(source, encoding="utf-8", newline="\n")
# A path dependency has no registry checksum in Cargo.lock. Verify every vendor
# byte against the checksum-pinned archive plus this one explicit source change.
SAFE_DROP = '''impl GraphicsCaptureApi {
    /// Close explicitly so the owner can report teardown errors.
    pub fn close_checked(&mut self) -> Result<(), Error> {
        if self.session.is_none() && self.frame_pool.is_none() { return Ok(()); }
        self.halt.store(true, atomic::Ordering::Relaxed);
        let item = match &self.item_with_details {
            GraphicsCaptureItemType::Window((item, _)) => item,
            GraphicsCaptureItemType::Monitor((item, _)) => item,
            GraphicsCaptureItemType::Unknown((item, _)) => item,
        };
        let debug = std::env::var_os("QUEST3D_CAPTURE_DIAGNOSTICS").is_some();
        let mut error = item.RemoveClosed(self.capture_closed_event_token).err();
        if let Some(ref pool) = self.frame_pool {
            if let Err(value) = pool.RemoveFrameArrived(self.frame_arrived_event_token) {
                error = error.or(Some(value));
            }
        }
        // The caller keeps the dispatcher alive until the real Closed event
        // when the HWND disappears. Close on its original initialized thread.
        if let Some(session) = self.session.as_ref() {
            if debug { eprintln!("Quest3D teardown: session Close"); }
            match session.Close() {
                Ok(()) => { self.session = None; },
                Err(value) => { error = error.or(Some(value)); },
            }
        }
        if let Some(pool) = self.frame_pool.as_ref() {
            if debug { eprintln!("Quest3D teardown: frame pool Close"); }
            match pool.Close() {
                Ok(()) => { self.frame_pool = None; },
                Err(value) => { error = error.or(Some(value)); },
            }
        }
        if debug { eprintln!("Quest3D teardown: done"); }
        error.map_or(Ok(()), |value| Err(Error::WindowsError(value)))
    }
}
impl Drop for GraphicsCaptureApi {
    fn drop(&mut self) {
        let _ = self.close_checked();
    }
}
'''
expected = {}
with tarfile.open(crates[0], "r:gz") as archive:
    for member in archive.getmembers():
        if not member.isfile():
            if not member.isdir():
                raise RuntimeError("Unexpected non-regular crate entry")
            continue
        relative = Path(member.name).relative_to(destination.name)
        data = archive.extractfile(member).read()
        if relative.as_posix() == "src/frame.rs":
            original = data.decode("utf-8").replace("\r\n", "\n")
            if original.count(anchor) != 1:
                raise RuntimeError("Unexpected upstream frame source")
            data = original.replace(anchor, addition + anchor).replace(drop_anchor, frame_drop + drop_anchor).encode("utf-8")
        if relative.as_posix() == "src/graphics_capture_api.rs":
            original = data.decode("utf-8").replace("\r\n", "\n")
            marker = "impl Drop for GraphicsCaptureApi {"
            if original.count(marker) != 1:
                raise RuntimeError("Unexpected upstream capture destructor")
            changed = original[:original.index(marker)] + SAFE_DROP
            old_pool = "let frame_pool = Direct3D11CaptureFramePool::Create(&direct3d_device, pixel_format, 1, item.Size()?)?;"
            new_pool = old_pool
            if changed.count(old_pool) != 1:
                raise RuntimeError("Unexpected upstream frame pool creation")
            prior = changed
            changed = changed.replace(old_pool, new_pool)
            target = destination / relative
            if target.read_text(encoding="utf-8") in (original, prior):
                target.write_text(changed, encoding="utf-8", newline="\n")
            data = changed.encode("utf-8")
        expected[relative.as_posix()] = data
actual = {item.relative_to(destination).as_posix(): item for item in destination.rglob("*") if item.is_file()}
if actual.keys() != expected.keys():
    raise RuntimeError("Vendor file inventory differs from pinned crate; existing files preserved")
for name, data in expected.items():
    if actual[name].is_symlink() or actual[name].read_bytes() != data:
        raise RuntimeError(f"Vendor differs from pinned crate plus content-size accessor: {name}")
print(f"Verified {len(expected)} files against crate {CRATE_HASH} plus explicit content-size/teardown fixes")

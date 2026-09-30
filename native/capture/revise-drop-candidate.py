"""Explicit one-time migration of the current experimental destructor policy."""
import ast
from pathlib import Path

root = Path(__file__).resolve().parents[2]
prepare = root / "native/capture/prepare-window-dependency.py"
text = prepare.read_text(encoding="utf-8")
tree = ast.parse(text)
node = next(node for node in tree.body if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "SAFE_DROP" for target in node.targets))
old = ast.literal_eval(node.value)
new = '''impl GraphicsCaptureApi {
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
vendor = root / "native/capture/vendor/windows-capture-2.0.0-alpha.7/src/graphics_capture_api.rs"
source = vendor.read_text(encoding="utf-8")
assert source.count(old) == 1
vendor.write_text(source.replace(old, new), encoding="utf-8", newline="\n")
lines = text.splitlines(keepends=True)
lines[node.lineno-1:node.end_lineno] = ["SAFE_DROP = '''" + new + "'''\n"]
prepare.write_text("".join(lines), encoding="utf-8", newline="\n")

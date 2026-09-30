"""Exact HWND WGC/CUDA feature probe using owned GDI content only; no user pixels saved."""
import argparse
from dataclasses import asdict
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys
import time
import zipfile

parser = argparse.ArgumentParser()
parser.add_argument("--wheel", type=Path, required=True)
args = parser.parse_args()
root = Path(__file__).resolve().parents[2]
wheel = args.wheel.resolve()
digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
folder = root / "artifacts/capture/window-experimental"
package = folder / ("package-" + digest[:12])
if not package.exists():
    package.mkdir(parents=True)
    with zipfile.ZipFile(wheel) as archive:
        for entry in archive.namelist():
            if not (package / entry).resolve().is_relative_to(package.resolve()):
                raise RuntimeError("Wheel extraction path escaped candidate directory")
        archive.extractall(package)
sys.path.insert(0, str(package))
import torch
import wc_cuda
from window_fixture import OwnedWindowFixture

assert wc_cuda.QUEST3D_PATCH_VERSION == 3
stream = torch.cuda.Stream()


def identity_tuple(identity):
    return (identity.process_id, identity.process_created_filetime, identity.thread_id, identity.class_name)


class Capture:
    def __init__(self, identity, color_format):
        self.identity, self.color_format = identity, color_format
        self.native = wc_cuda._NativeWcCapture(luid=wc_cuda.get_luid(), window_hwnd=identity.hwnd,
            window_identity=identity_tuple(identity), color_format=color_format, cursor_capture=False)
        self.last = 0
        self.bridge = None
        self.native.start()

    def next(self, fixture, timeout=4):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            fixture.pump()
            error = self.native.get_last_error()
            if error:
                raise RuntimeError(error)
            result = self.native.get_frame(self.last, timeout=.03)
            if result:
                frame, self.last = result
                return frame, self.last
        raise TimeoutError("No new HWND WGC frame")

    def copy(self, frame, frame_id):
        try:
            assert frame.source_hwnd == self.identity.hwnd
            assert frame.source_identity == identity_tuple(self.identity)
            if (self.bridge is None or self.bridge.texture_ptr != frame.texture_ptr or
                    self.bridge.width != frame.width or self.bridge.height != frame.height):
                if self.bridge:
                    self.bridge.close()
                self.bridge = wc_cuda._DX11ToPyTorchBridge(frame.texture_ptr, frame.width, frame.height,
                                                          color_format=frame.color_format)
            tensor = self.bridge.update(stream, 0)
            assert tensor.dtype == (torch.uint8 if self.color_format == "bgra8" else torch.float16)
            assert tensor.stride(0) * tensor.element_size() == frame.row_pitch_bytes
            owned = tensor[:frame.original_height, :frame.original_width].contiguous()
            return owned
        finally:
            self.native.release_frame(frame_id)

    def close(self):
        self.native.stop()
        owner = self.native.lease_stats()[0]
        if owner is not None:
            self.native.release_frame(owner)
        if self.bridge:
            self.bridge.close()


def stats(frame, pixels, frame_id=None):
    height, width = pixels.shape[:2]
    center = pixels[height // 3:2 * height // 3, width // 3:2 * width // 3, :3].float()
    now = time.perf_counter_ns()
    return dict(frame_id=frame_id, content_size=[width, height], aligned_size=[frame.width, frame.height],
        row_pitch_bytes=frame.row_pitch_bytes, source_hwnd=frame.source_hwnd,
        system_relative_time_ns=frame.system_relative_time_ns, observed_ns=now,
        raw_wgc_time_to_observation_ms=(now - frame.system_relative_time_ns) / 1e6,
        center_channels=center.mean(dim=(0, 1)).tolist(), finite=bool(torch.isfinite(pixels.float()).all()))


results = []
with OwnedWindowFixture() as fixture:
    source = fixture.create()
    identity = fixture.provider.observe(source).identity
    assert identity
    # Even a correct HWND must not accept a previous process creation time.
    bad = wc_cuda._NativeWcCapture(luid=wc_cuda.get_luid(), window_hwnd=source,
        window_identity=(identity.process_id, identity.process_created_filetime + 1,
                         identity.thread_id, identity.class_name))
    try:
        bad.start()
        deadline = time.monotonic() + 3
        while not bad.get_last_error() and time.monotonic() < deadline:
            fixture.pump(.01)
        assert "does not match" in (bad.get_last_error() or "")
    finally:
        bad.stop()
    for color_format in ("bgra8", "rgba16f"):
        capture = Capture(identity, color_format)
        trial = dict(color_format=color_format, stages=[])
        try:
            frame, frame_id = capture.next(fixture)
            original = capture.copy(frame, frame_id)
            initial = stats(frame, original)
            initial["frame_id"] = frame_id
            trial["stages"].append(dict(event="initial", **initial))
            assert initial["finite"] and min(initial["center_channels"]) > .1
            baseline_clone = original.clone()
            cover = fixture.create(left=2680, top=180, width=460, height=340, color=0)
            # A fresh changing stripe inside the fully occluded source proves
            # this is HWND content capture rather than visible desktop crop.
            fixture.paint(source)
            changed = None
            for attempt in range(12):
                fixture.paint(source)
                fixture.pump(.03)
                frame, frame_id = capture.next(fixture)
                changed = capture.copy(frame, frame_id)
                if changed.shape == original.shape and not torch.equal(changed, original):
                    break
            assert changed is not None and changed.shape == original.shape and not torch.equal(changed, original)
            occluded = stats(frame, changed, frame_id)
            assert min(occluded["center_channels"]) >= min(initial["center_channels"]) * .98
            assert torch.equal(original, baseline_clone), "Earlier owned tensor changed under native reuse"
            trial["stages"].append(dict(event="occluded_fresh_source_pixels", **occluded))
            fixture.close_window(cover)
            # Keep the native lease through resize: no D3D writer may overwrite
            # that shared texture while a CUDA consumer owns it.
            fixture.paint(source)
            held, held_id = capture.next(fixture)
            held_frame_counter = capture.native.lease_stats()[2]
            skips_before = capture.native.lease_stats()[1]
            fixture.move(source, 2780, 260, 638, 414)
            fixture.pump(.15)
            lease = capture.native.lease_stats()
            assert lease[0] == held_id and lease[2] == held_frame_counter and lease[1] > skips_before
            capture.copy(held, held_id)
            resized = None
            for attempt in range(20):
                fixture.paint(source)
                frame, frame_id = capture.next(fixture)
                pixels = capture.copy(frame, frame_id)
                if list(pixels.shape[:2]) != list(original.shape[:2]):
                    resized = stats(frame, pixels, frame_id)
                    break
            assert resized
            trial["stages"].append(dict(event="resized_after_lease", **resized,
                lease_skips=lease[1] - skips_before, bounds=asdict(fixture.provider.observe(source).frame_bounds)))
            # Native minimum-state behavior is observed, not asserted as a
            # guarantee of fresh frames; the adapter must reject minimized use.
            before_minimize = capture.native.lease_stats()[2]
            fixture.show(source, 7)
            fixture.pump(.3)
            minimized = fixture.provider.observe(source).minimized
            assert minimized
            trial["stages"].append(dict(event="minimized", native_alive=capture.native.is_alive(),
                new_frames=capture.native.lease_stats()[2] - before_minimize,
                closed=capture.native.window_status()[0]))
            fixture.show(source, 4)
            fixture.move(source, 200, 200, 420, 300)
            fixture.pump(.2)
            # Reject the observed 146x28 minimize/restore transition, including
            # old-size frames. Require actual restored DWM bounds and 3 new
            # matching samples before interpreting white on this monitor.
            white_samples = []
            discarded_shapes = []
            expected = fixture.provider.observe(source).frame_bounds
            for attempt in range(24):
                fixture.paint(source)
                fixture.pump(.025)
                frame, frame_id = capture.next(fixture)
                pixels = capture.copy(frame, frame_id)
                if list(pixels.shape[:2]) != [expected.height, expected.width]:
                    discarded_shapes.append(list(pixels.shape[:2]))
                    continue
                white_samples.append(stats(frame, pixels, frame_id))
                if len(white_samples) == 3:
                    break
            assert len(white_samples) == 3
            trial["stages"].append(dict(event="primary_monitor_white", samples=white_samples,
                discarded_shapes=discarded_shapes, current_monitor=fixture.provider.observe(source).monitor_device))
            fixture.move(source, 2500, 200, 420, 300)  # Spans the 2560 boundary.
            fixture.pump(.2)
            white_samples = []
            for attempt in range(3):
                fixture.paint(source)
                fixture.pump(.025)
                frame, frame_id = capture.next(fixture)
                spanning = capture.copy(frame, frame_id)
                assert list(spanning.shape[:2]) == [expected.height, expected.width]
                white_samples.append(stats(frame, spanning, frame_id))
            trial["stages"].append(dict(event="spanning_monitors_white", samples=white_samples,
                current_monitor=fixture.provider.observe(source).monitor_device))
            assert torch.equal(original, baseline_clone)
            trial["old_tensor_unchanged_after_move_resize"] = True
            trial["resize_frames_skipped"] = capture.native.window_status()[1]
        finally:
            capture.close()
        results.append(trial)
        fixture.move(source, 2700, 200, 420, 300)
    # Actual item Closed must end capture, and the same object must refuse a restart.
    fixture.move(source, 200, 200, 420, 300)
    fixture.pump(.2)
    capture = Capture(identity, "rgba16f")
    try:
        frame, frame_id = capture.next(fixture)
        owned_after_close = capture.copy(frame, frame_id)
        results.append(dict(color_format="rgba16f", stages=[dict(event="fresh_capture_primary_white",
            **stats(frame, owned_after_close, frame_id))]))
        saved_after_close = owned_after_close.clone()
        fixture.close_window(source)
        deadline = time.monotonic() + 3
        while capture.native.is_alive() and time.monotonic() < deadline:
            fixture.pump(.01)
        assert not capture.native.is_alive()
        assert capture.native.window_status()[0]
        capture.native.stop()
        try:
            capture.native.start()
        except RuntimeError as error:
            assert "closed HWND" in str(error)
        else:
            raise AssertionError("Closed HWND capture was restarted")
        assert torch.equal(owned_after_close, saved_after_close)
    finally:
        capture.close()
    unchanged_foreground = fixture.foreground_before == int(fixture.user.GetForegroundWindow() or 0)

report = dict(verified_at=datetime.now().astimezone().isoformat(), wheel=str(wheel), sha256=digest,
    package_path=str(package), actual_owned_window_capture=True, user_window_pixels_saved=False,
    windows_hdr_changed=False, foreground_unchanged=unchanged_foreground,
    wrong_creation_time_rejected=True, closed_item_restart_rejected=True,
    owned_tensor_valid_after_close=True, color_accuracy_verified=False, tone_mapping_verified=False,
    quest_display_verified=False, captures=results)
output = folder / ("verification-" + time.strftime("%Y%m%d-%H%M%S") + ".json")
output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
print(json.dumps({"report": str(output), **report}, indent=2))

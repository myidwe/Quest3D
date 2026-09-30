"""Real owned HWND worker -> CPU/GPU copy -> V2 Small -> SBS -> private native v3.

Only this probe's owned fixture windows are changed. No image, user input,
operating host replacement, firewall change, or Quest proof is produced.
"""
import argparse
from dataclasses import asdict
from datetime import datetime
import hashlib
import json
from pathlib import Path
import subprocess
import time
import uuid

import numpy as np
import torch

from window_fixture import ThreadedOwnedWindowFixture
from quest3d.bridge import FramePublisher, FULL_SBS, ORIGINAL_2D, CAPTURE_RECEIPT
from quest3d.depth import DepthEngine
from quest3d.paths import ARTIFACT_DIR
from quest3d.source_identity import SourceKind
from quest3d.stereo import StereoSynthesizer
from quest3d.window_capture import WindowCaptureUnavailable
from quest3d.window_process_capture import ProcessWindowCapture


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("output", type=Path)
parser.add_argument("--native-probe", type=Path, default=Path("artifacts/host/frame_bridge_probe.exe"))
parser.add_argument("--frames", type=int, default=30)
args = parser.parse_args()
if not 10 <= args.frames <= 300:
    parser.error("--frames must be 10..300")
args.output.mkdir(parents=True, exist_ok=False)
active = ARTIFACT_DIR / "active-session.json"
active_before = active.read_bytes() if active.exists() else None
prefix = "Local\\Quest3D.Verify.WindowAI." + uuid.uuid4().hex
result = {"status": "running", "checked_at": datetime.now().astimezone().isoformat(),
    "namespace": prefix, "actual_window_ai_verified": False, "native_reader_verified": False,
    "quest_verified": False, "nvenc_verified": False, "audio_verified": False,
    "os_input_events": 0, "images_saved": False, "zero_copy": False}
rows = []
native = capture = None
torch.set_num_threads(4)


def next_frame(fixture, hwnd, source):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        fixture.paint(hwnd)
        try:
            return source.grab(timeout_seconds=.1)
        except (TimeoutError, WindowCaptureUnavailable):
            pass
    raise TimeoutError("Own HWND did not produce a matching fresh worker frame")


try:
    engine = DepthEngine(280)
    synth = StereoSynthesizer(640, 360, disparity_px=8)
    with ThreadedOwnedWindowFixture() as fixture:
        hwnd = fixture.create()
        identity = fixture.provider.observe(hwnd).identity
        assert identity is not None
        with ProcessWindowCapture(identity, experimental_window=True) as capture, FramePublisher(prefix, version=3) as publisher, \
                (args.output / "native.jsonl").open("wb") as log, (args.output / "native.stderr").open("wb") as stderr:
            for index in range(args.frames + 2):
                started = time.perf_counter_ns()
                frame = next_frame(fixture, hwnd, capture)
                source_identity = frame.source_identity
                assert source_identity.kind is SourceKind.WINDOW and source_identity.native_handle == hwnd
                assert frame.bgra.dtype == np.uint8 and frame.bgra.flags.c_contiguous
                torch.cuda.synchronize()
                upload_started = time.perf_counter_ns()
                gpu = torch.from_numpy(frame.bgra).to("cuda")
                torch.cuda.synchronize()
                upload_ms = (time.perf_counter_ns() - upload_started) / 1e6
                depth = None
                if index in (0, args.frames + 1):
                    output = synth.original_2d(gpu, frame_id=frame.frame_id, generation=frame.geometry_generation)
                    np.testing.assert_array_equal(output.bgra[:, :640], output.bgra[:, 640:])
                else:
                    depth = engine.infer(gpu, frame_id=frame.frame_id, generation=frame.geometry_generation)
                    output = synth.synthesize(gpu, depth, frame_id=frame.frame_id, generation=frame.geometry_generation)
                    assert depth.frame_id == output.frame_id == frame.frame_id
                    assert output.mode == "3d"
                bounds = frame.geometry.bounds
                flags = FULL_SBS | CAPTURE_RECEIPT | (ORIGINAL_2D if output.mode == "2d" else 0)
                assert publisher.publish(output.bgra, frame_id=index+1, capture_ns=frame.captured_ns,
                    generation=frame.geometry_generation, flags=flags,
                    source_rect=(bounds.left, bounds.top, bounds.width, bounds.height),
                    content_rect=output.content_rect, source_identity=source_identity)
                rows.append(dict(publication=index+1, source_frame_id=frame.frame_id,
                    capture_ns=frame.captured_ns, generation=frame.geometry_generation, mode=output.mode, flags=flags,
                    source_identity=asdict(source_identity), stream_epoch=publisher.epoch,
                    payload_sha256=hashlib.sha256(memoryview(output.bgra).cast("B")).hexdigest(),
                    cpu_gpu_upload_ms=upload_ms, inference_ms=depth.inference_ms if depth else None,
                    differing_eye_components=int(np.count_nonzero(output.bgra[:, :640] != output.bgra[:, 640:])),
                    pc_probe_ms=(time.perf_counter_ns()-started)/1e6, worker=capture.last_frame_diagnostics))
                if native is None and depth is not None:
                    result["native_sha256"] = hashlib.sha256(args.native_probe.read_bytes()).hexdigest()
                    native = subprocess.Popen([str(args.native_probe.resolve()), "1", "--protocol", "3", "--prefix", prefix, "--checksum"],
                        stdout=log, stderr=stderr)
                # Keep the private namespace alive for the reader's actual 1s observation.
            if native.wait(timeout=5) != 0:
                raise RuntimeError("Native v3 reader failed")
        result["worker_close"] = capture.close_result
        assert capture.close_result["normal_exit"] and not capture.close_result["forced"]
    result["foreground_unchanged"] = int(fixture.user.GetForegroundWindow() or 0) == fixture.foreground_before
    assert result["foreground_unchanged"]
    native_rows = [json.loads(line) for line in (args.output / "native.jsonl").read_text().splitlines()]
    native_frames = [row for row in native_rows if "frame_id" in row and "payload_sha256" in row]
    assert len(native_frames) >= 2, "Native reader did not observe multiple source publications"
    by_id = {row["publication"]: row for row in rows}
    observed_ids = []
    for native_row in native_frames:
        row = by_id[native_row["frame_id"]]
        snapshot = row["source_identity"]
        bounds = snapshot["parent_bounds"]
        assert native_row["bridge_protocol"] == 3 and native_row["stream_epoch"] == row["stream_epoch"]
        assert (native_row["width"], native_row["height"], native_row["payload_bytes"]) == (1280, 360, 1280*360*4)
        assert native_row["flags"] == row["flags"]
        assert native_row["source_kind"] == "window"
        assert native_row["source_selection_id"] == str(snapshot["selection_id"])
        assert native_row["source_native_handle"] == str(snapshot["native_handle"])
        assert native_row["source_process_id"] == snapshot["process_id"]
        assert native_row["source_creation_filetime"] == str(snapshot["creation_filetime"])
        assert native_row["parent_bounds"] == [bounds[k] for k in ("left", "top", "width", "height")]
        assert native_row["payload_sha256"].lower() == row["payload_sha256"]
        observed_ids.append(native_row["frame_id"])
    assert observed_ids == sorted(set(observed_ids))
    assert any(row["mode"] == "3d" and row["differing_eye_components"] for row in rows)
    (args.output / "source-frames.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    result.update(status="passed", actual_window_ai_verified=True, native_reader_verified=True,
        published_frames=len(rows), actual_ai_frames=args.frames, native_frames_checked=len(native_frames),
        cpu_gpu_upload_ms_p50_p95=np.percentile([row["cpu_gpu_upload_ms"] for row in rows], [50, 95]).tolist())
except BaseException as exc:
    result.update(status="failed", error=f"{type(exc).__name__}: {exc}")
    raise
finally:
    if native is not None and native.poll() is None:
        try:
            native.wait(timeout=5)
        except subprocess.TimeoutExpired:
            native.terminate()
            native.wait(timeout=3)
            result.update(status="failed", native_reader_verified=False,
                          cleanup_error="Own native probe required forced termination")
    (args.output / "source-frames.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    result["active_session_preserved"] = active_before == (active.read_bytes() if active.exists() else None)
    if not result["active_session_preserved"]:
        result.update(status="failed", error="Active session routing changed during verification")
    (args.output / "verification.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result), flush=True)
    if not result["active_session_preserved"]:
        raise RuntimeError("Active session routing changed")

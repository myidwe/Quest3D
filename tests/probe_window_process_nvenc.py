"""Actual owned HWND -> V2 Small -> SBS -> Sunshine NVENC -> PC decode.

This opt-in probe owns only its fixture, worker, test executable and default-v3
publisher. It neither starts a server nor changes the active product session.
"""
from __future__ import annotations

import argparse
import csv
import ctypes
from dataclasses import asdict
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

import av
import numpy as np
import psutil
import torch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "native/capture"))
from window_fixture import ThreadedOwnedWindowFixture
from quest3d.bridge import FramePublisher, FULL_SBS, CAPTURE_RECEIPT
from quest3d.depth import DepthEngine
from quest3d.source_identity import SourceKind
from quest3d.stereo import StereoSynthesizer
from quest3d.window_capture import WindowCaptureUnavailable
from quest3d.window_process_capture import ProcessWindowCapture, _WinAPI

TEST_EXE = ROOT / "third_party/sunshine/cmake-build-quest3d/tests/test_sunshine.exe"
EXPECTED_TEST_SHA = "140e4420106243486a93e0cbdbd83db7ed5b80746dd47632edf7446467e72714"
EXPECTED_LIVE_SHA = "77c950b526ba6b944589b8697cbaaa76b26955e3ae2e412a4cfba7bc93626b63"
FILTER = "Quest3DExternalSource.RealPublisherToNvenc"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def process_snapshot():
    rows = []
    for item in psutil.process_iter(["pid", "name", "exe"]):
        if (item.info["name"] or "").lower() == "test_sunshine.exe":
            raise RuntimeError("An existing Sunshine test process is running; do not overlap probes")
        if (item.info["name"] or "").lower() == "sunshine.exe":
            rows.append(dict(pid=item.pid, executable=item.info["exe"], sha256=sha(item.info["exe"])))
    if len(rows) != 1 or rows[0]["sha256"] != EXPECTED_LIVE_SHA:
        raise RuntimeError("Live host is not the unchanged inspected 77C runtime")
    return rows


def empty_v3_preflight():
    win = _WinAPI()
    ctypes.set_last_error(0)
    owner = win.k.OpenMutexW(0x100001, False, "Local\\Quest3D.Frame.Producer.v3")
    if owner:
        win.k.CloseHandle(owner)  # Do not wait: an abandoned signal is not ours.
        raise RuntimeError("Existing default-v3 owner object: refusing this probe")
    if ctypes.get_last_error() != 2:
        raise ctypes.WinError(ctypes.get_last_error())


def next_frame(fixture, hwnd, capture):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        fixture.paint(hwnd)
        try:
            return capture.grab(timeout_seconds=.1)
        except (TimeoutError, WindowCaptureUnavailable):
            pass
    raise TimeoutError("Actual owned window did not supply a fresh frame")


def stats(values):
    values = np.array(values, dtype=np.float64)
    return dict(samples=len(values), p50=float(np.percentile(values, 50)),
                p95=float(np.percentile(values, 95)), maximum=float(values.max()))


def decode_and_check(directory, codec, publications, references):
    stem = "quest3d-h264" if codec == "h264" else "quest3d-hevc"
    path = directory / (stem + "." + codec)
    with (directory / (stem + ".csv")).open(newline="", encoding="utf-8") as file:
        metrics = list(csv.DictReader(file))
    assert len(metrics) == 24
    assert [int(row["index"]) for row in metrics] == list(range(1, 25))
    ids = [int(row["publisher_frame_id"]) for row in metrics]
    assert ids == sorted(set(ids)), "Native snapshot must consume distinct publications"
    decoded = []
    with av.open(str(path), format=codec) as container:
        for index, frame in enumerate(container.decode(video=0)):
            assert index < 24 and (frame.width, frame.height) == (1280, 360)
            metric = metrics[index]
            source = publications[int(metric["publisher_frame_id"])]
            assert int(metric["publisher_epoch"]) == source["stream_epoch"]
            assert int(metric["geometry_generation"]) == source["generation"]
            assert int(metric["flags"]) == source["flags"] == FULL_SBS | CAPTURE_RECEIPT
            assert int(metric["host_geometry_revision"]) > 0
            rgb = frame.to_ndarray(format="rgb24")
            expected = references[source["publisher_frame_id"]]
            reference_mae = float(np.abs(rgb[::4, ::4].astype(np.float32) - expected).mean())
            left, right = np.split(rgb.astype(np.float32), 2, axis=1)
            eye_mae = float(np.abs(left - right).mean())
            # This is a small own-window lossless-reference comparison through
            # lossy video. Fixed tolerance is not a stereo comfort criterion.
            assert reference_mae < 5.0, (codec, index, reference_mae)
            assert eye_mae > 0.05 and source["differing_eye_components"] > 0
            decoded.append(dict(index=index + 1, publisher_frame_id=source["publisher_frame_id"],
                decoded_rgb_sha256=hashlib.sha256(memoryview(rgb).cast("B")).hexdigest(),
                reference_rgb_grid_mae=reference_mae, left_right_rgb_mae=eye_mae,
                source_identity=source["source_identity"], stream_epoch=source["stream_epoch"],
                geometry_generation=source["generation"]))
    assert len(decoded) == 24
    return dict(codec=codec, path=str(path), sha256=sha(path), bytes=path.stat().st_size,
        decoded_frames=len(decoded), frames=decoded,
        reference_rgb_grid_mae=stats([row["reference_rgb_grid_mae"] for row in decoded]),
        left_right_rgb_mae=stats([row["left_right_rgb_mae"] for row in decoded]),
        native_timings_ms={name: stats([float(row[name]) for row in metrics]) for name in
            ("source_age_ms", "snapshot_wait_copy_upload_ms", "convert_ms", "encode_ms")})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    active = ROOT / "artifacts/active-session.json"
    active_before = active.read_bytes() if active.exists() else None
    result = dict(status="running", created=datetime.now().astimezone().isoformat(),
        actual_ai=False, actual_nvenc=False, pc_decode=False, quest_verified=False,
        audio_verified=False, input_enabled=False, network_started=False, zero_copy=False,
        protocol=3, namespace="Local\\Quest3D.Frame", test_filter=FILTER,
        test_executable=str(TEST_EXE), test_sha256=sha(TEST_EXE), images_saved=False)
    rows, references = {}, {}
    native = capture = None
    native_out = native_err = None
    try:
        assert result["test_sha256"] == EXPECTED_TEST_SHA, "Unexpected test binary; re-review before running"
        result["host_before"] = process_snapshot()
        empty_v3_preflight()
        # Owner existence is rechecked atomically by FramePublisher itself.
        with FramePublisher(version=3) as publisher:
            torch.set_num_threads(4)
            engine = DepthEngine(280)
            synth = StereoSynthesizer(640, 360, disparity_px=8)
            with ThreadedOwnedWindowFixture() as fixture:
                hwnd = fixture.create()
                identity = fixture.provider.observe(hwnd).identity
                assert identity is not None
                with ProcessWindowCapture(identity, experimental_window=True) as capture:
                    started = time.monotonic()
                    while native is None or native.poll() is None:
                        assert time.monotonic() - started < 40 and len(rows) < 300, "Bounded native probe did not complete"
                        frame_started = time.perf_counter_ns()
                        frame = next_frame(fixture, hwnd, capture)
                        capture_wait_ms = (time.perf_counter_ns() - frame_started) / 1e6
                        assert frame.source_identity.kind is SourceKind.WINDOW
                        assert (frame.source_identity.native_handle, frame.source_identity.process_id,
                                frame.source_identity.creation_filetime) == (hwnd, identity.process_id, identity.process_created_filetime)
                        torch.cuda.synchronize()
                        stage = time.perf_counter_ns()
                        gpu = torch.from_numpy(frame.bgra).to("cuda")
                        torch.cuda.synchronize()
                        upload_ms = (time.perf_counter_ns() - stage) / 1e6
                        depth = engine.infer(gpu, frame_id=frame.frame_id, generation=frame.geometry_generation)
                        stage = time.perf_counter_ns()
                        stereo = synth.synthesize(gpu, depth, frame_id=frame.frame_id, generation=frame.geometry_generation)
                        stereo_ms = (time.perf_counter_ns() - stage) / 1e6
                        assert depth.frame_id == stereo.frame_id == frame.frame_id
                        assert depth.generation == stereo.generation == frame.geometry_generation
                        assert stereo.mode == "3d" and stereo.bgra.shape == (360, 1280, 4)
                        bounds = frame.geometry.bounds
                        stage = time.perf_counter_ns()
                        success = publisher.publish(stereo.bgra, frame_id=frame.frame_id,
                            capture_ns=frame.captured_ns, generation=frame.geometry_generation,
                            flags=FULL_SBS | CAPTURE_RECEIPT, source_identity=frame.source_identity,
                            source_rect=(bounds.left, bounds.top, bounds.width, bounds.height), content_rect=stereo.content_rect)
                        publish_ms = (time.perf_counter_ns() - stage) / 1e6
                        if success:
                            rows[frame.frame_id] = dict(publisher_frame_id=frame.frame_id,
                                captured_ns=frame.captured_ns, source_identity=asdict(frame.source_identity),
                                stream_epoch=publisher.epoch, generation=frame.geometry_generation,
                                flags=FULL_SBS | CAPTURE_RECEIPT, source_resolution=[bounds.width, bounds.height],
                                ai_resolution=list(depth.input_shape), eye_resolution=[640, 360],
                                capture_wait_and_paint_ms=capture_wait_ms, cpu_gpu_upload_ms=upload_ms,
                                preprocess_ms=depth.preprocess_ms, inference_ms=depth.inference_ms,
                                stereo_and_download_ms=stereo_ms, publish_ms=publish_ms,
                                capture_receipt_to_publication_ms=(time.perf_counter_ns()-frame.captured_ns)/1e6,
                                differing_eye_components=int(np.count_nonzero(stereo.bgra[:, :640, :3] != stereo.bgra[:, 640:, :3])),
                                payload_sha256=hashlib.sha256(memoryview(stereo.bgra).cast("B")).hexdigest(),
                                worker=capture.last_frame_diagnostics)
                            references[frame.frame_id] = stereo.bgra[::4, ::4, :3][:, :, ::-1].copy()
                            if native is None:
                                native_out = (output / "native.stdout.log").open("wb")
                                native_err = (output / "native.stderr.log").open("wb")
                                coverage = output / "coverage"
                                coverage.mkdir()
                                env = dict(os.environ, QUEST3D_PIPELINE_OUTPUT=str(output), QUEST3D_PIPELINE_PROTOCOL="3",
                                           GCOV_PREFIX=str(coverage))
                                native = subprocess.Popen([str(TEST_EXE), "--gtest_filter=" + FILTER,
                                    "--gtest_output=xml:" + str(output / "native.xml")],
                                    cwd=TEST_EXE.parent.parent, env=env, stdout=native_out, stderr=native_err,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
                    assert native.returncode == 0, f"Native test exited {native.returncode}; inspect retained logs"
                result["worker_close"] = capture.close_result
                assert capture.close_result["normal_exit"] and not capture.close_result["forced"]
            result["foreground_unchanged"] = int(fixture.user.GetForegroundWindow() or 0) == fixture.foreground_before
            result["publisher_epoch"] = publisher.epoch
            result["publisher_skipped"] = publisher.skipped
        suite = ET.parse(output / "native.xml").getroot()
        cases = suite.findall(".//testcase")
        assert len(cases) == 1 and cases[0].get("name") == "RealPublisherToNvenc"
        assert not cases[0].findall("failure") and not cases[0].findall("skipped")
        result["codecs"] = [decode_and_check(output, codec, rows, references) for codec in ("h264", "hevc")]
        result["published_frames"] = len(rows)
        result["producer_timings_ms"] = {name: stats([row[name] for row in rows.values()]) for name in
            ("capture_wait_and_paint_ms", "cpu_gpu_upload_ms", "preprocess_ms", "inference_ms",
             "stereo_and_download_ms", "publish_ms", "capture_receipt_to_publication_ms")}
        result["status"] = "passed"
        result.update(actual_ai=True, actual_nvenc=True, pc_decode=True)
        return 0
    except BaseException as exc:
        result.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        return 1
    finally:
        if native is not None:
            if native.poll() is None:
                native.terminate()
                native.wait(timeout=5)
                result.update(status="failed", native_forced_cleanup=True)
            native._handle.Close()
        for file in (native_out, native_err):
            if file:
                file.close()
        if capture:
            result["worker_close"] = capture.close_result
        result["active_session_preserved"] = active_before == (active.read_bytes() if active.exists() else None)
        try:
            result["host_after"] = process_snapshot()
            result["host_preserved"] = result.get("host_before") == result["host_after"]
        except Exception as exc:
            result["host_check_error"] = repr(exc)
            result["host_preserved"] = False
        if not result["active_session_preserved"] or not result["host_preserved"]:
            result.update(status="failed", preservation_error="Active session or inspected live host changed")
        result["completed"] = datetime.now().astimezone().isoformat()
        (output / "source-frames.json").write_text(json.dumps(list(rows.values()), indent=2), encoding="utf-8")
        (output / "verification.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps({key: value for key, value in result.items() if key not in ("codecs",)}), flush=True)
        if result["status"] != "passed":
            raise SystemExit(1)


if __name__ == "__main__":
    raise SystemExit(main())

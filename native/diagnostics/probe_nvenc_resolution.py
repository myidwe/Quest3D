"""Private calibration PNG -> production SourceProvider/D3D11/NVENC -> PC decode.

No capture/model worker, network, input, Windows audio or Quest is started.
This measures supported encoded sizes and diagnostic pattern loss, not 3D quality.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import xml.etree.ElementTree as ET

import av
import numpy as np
from PIL import Image
import psutil

from quest3d.bridge import FramePublisher, FULL_SBS

ROOT = Path(__file__).resolve().parents[2]
TEST_EXE = ROOT / "third_party/sunshine/cmake-build-quest3d/tests/test_sunshine.exe"
EXPECTED_TEST = "140e4420106243486a93e0cbdbd83db7ed5b80746dd47632edf7446467e72714"
EXPECTED_HOST = "77c950b526ba6b944589b8697cbaaa76b26955e3ae2e412a4cfba7bc93626b63"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def stats(values):
    values = np.asarray(values, dtype=np.float64)
    return {"samples": len(values), "p50": float(np.percentile(values, 50)),
            "p95": float(np.percentile(values, 95)), "maximum": float(values.max())}


def host_snapshot():
    rows = []
    for process in psutil.process_iter(["pid", "name", "exe", "create_time"]):
        name = (process.info["name"] or "").lower()
        if name in ("test_sunshine.exe", "quest3d_color_probe.exe"):
            raise RuntimeError("Another native probe is running")
        if name == "sunshine.exe":
            rows.append({"pid": process.pid, "birth": process.info["create_time"],
                         "exe": process.info["exe"], "sha256": sha(process.info["exe"])})
    if len(rows) != 1 or rows[0]["sha256"] != EXPECTED_HOST:
        raise RuntimeError("The inspected operational host must remain unchanged")
    return rows


def gpu_snapshot():
    """Global GPU observation includes the unchanged live producer/host."""
    completed = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.used,utilization.gpu,utilization.encoder",
                                "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=3,
                               creationflags=subprocess.CREATE_NO_WINDOW)
    return {"scope": "global GPU; includes live applications", "returncode": completed.returncode,
            "csv": completed.stdout.strip() if completed.returncode == 0 else None}


def pattern(eye_width, height):
    """Exact grayscale frequency bands, ramp, and asymmetric eye markers."""
    gray = np.full((height, eye_width), 32, np.uint8)
    bands = []
    band_height = height // 10
    for index, period in enumerate((1, 2, 4, 8)):
        y = index * band_height
        gray[y:y + band_height] = np.where((np.arange(eye_width) // period) % 2, 224, 32)
        bands.append({"period_pixels": period, "roi": [32, y + 8, eye_width - 64, band_height - 16]})
    gray[4 * band_height:7 * band_height] = np.linspace(0, 255, eye_width, dtype=np.uint8)
    yy, xx = np.indices((height - 7 * band_height, eye_width))
    gray[7 * band_height:] = np.where(xx > yy * 3 + eye_width // 4, 224, 32)
    left = np.repeat(gray[..., None], 3, axis=2)
    right = np.roll(left, 8, axis=1)
    # Eye markers allow exact eye orientation/packing checks without AI claims.
    left[-64:-16, 16:64] = (224, 32, 32)
    right[-64:-16, 16:64] = (32, 32, 224)
    return np.concatenate((left, right), axis=1), bands


def decode_case(directory, codec, eye_width, height, reference, bands, publications):
    stem = "quest3d-" + codec
    path = directory / (stem + "." + codec)
    with (directory / (stem + ".csv")).open(newline="", encoding="utf-8") as file:
        metrics = list(csv.DictReader(file))
    assert len(metrics) == 4 and [int(row["index"]) for row in metrics] == list(range(1, 5))
    ids = [int(row["publisher_frame_id"]) for row in metrics]
    assert ids == sorted(set(ids))
    frames = []
    start = time.perf_counter_ns()
    with av.open(str(path), format=codec) as container:
        for index, frame in enumerate(container.decode(video=0)):
            assert index < 4 and (frame.width, frame.height) == (eye_width * 2, height)
            metric = metrics[index]
            row = publications[int(metric["publisher_frame_id"])]
            assert int(metric["publisher_epoch"]) == row["epoch"]
            assert int(metric["geometry_generation"]) == row["generation"] == 1
            assert int(metric["flags"]) == FULL_SBS
            assert int(metric["host_geometry_revision"]) > 0
            rgb = frame.to_ndarray(format="rgb24")
            error = rgb.astype(np.float32) - reference.astype(np.float32)
            mse = float(np.mean(error * error))
            neutral = rgb[:, :, :].astype(np.float32).mean(axis=2)
            frequency = []
            for band in bands:
                x, y, width, h = band["roi"]
                expected = reference[y:y+h, x:x+width, 0].astype(np.float32)
                actual = neutral[y:y+h, x:x+width]
                frequency.append({"period_pixels": band["period_pixels"],
                                  "luma_mae": float(np.abs(actual - expected).mean()),
                                  "luma_stddev_ratio": float(actual.std() / expected.std())})
            left_marker = rgb[-56:-24, 24:56].mean(axis=(0, 1))
            right_marker = rgb[-56:-24, eye_width+24:eye_width+56].mean(axis=(0, 1))
            assert left_marker[0] > left_marker[2] + 80 and right_marker[2] > right_marker[0] + 80
            frames.append({"publisher_frame_id": int(metric["publisher_frame_id"]),
                           "decoded_size": [frame.width, frame.height], "eye_size": [eye_width, height],
                           "format": frame.format.name, "colorspace": int(frame.colorspace), "range": int(frame.color_range),
                           "rgb_mae": float(np.abs(error).mean()), "rgb_psnr_db": 10 * np.log10(255*255/mse) if mse else None,
                           "frequency_bands": frequency, "left_marker_rgb": left_marker.tolist(),
                           "right_marker_rgb": right_marker.tolist()})
            if index == 0:
                Image.fromarray(rgb).save(directory / (codec + "-decoded-sbs.png"))
    assert len(frames) == 4
    return {"codec": codec, "bitstream_bytes": path.stat().st_size, "bitstream_sha256": sha(path),
            "pc_decode_and_analysis_ms": (time.perf_counter_ns() - start) / 1e6,
            "frames": frames, "native_timings_ms": {name: stats([float(row[name]) for row in metrics])
                for name in ("source_age_ms", "snapshot_wait_copy_upload_ms", "convert_ms", "encode_ms")}}


def run_size(manifest, directory, eye_width, height):
    directory.mkdir(exist_ok=False)
    result = {"status": "running", "eye_size": [eye_width, height], "packed_size": [eye_width*2, height],
              "source": "owned diagnostic calibration PNG; no actual AI/capture", "bitrate_kbps": 20000,
              "encoder_nominal_fps": 30, "frames_per_codec": 4, "input_enabled": False,
              "quest_verified": False, "network_started": False, "zero_copy": False}
    native, handles = None, []
    rows, publishes, parent_rss, native_rss = {}, [], [], []
    try:
        reference, bands = pattern(eye_width, height)
        png = directory / "source-sbs.png"
        Image.fromarray(reference).save(png)
        with Image.open(png) as source:
            decoded_source = np.array(source.convert("RGB"))
        np.testing.assert_array_equal(decoded_source, reference)
        source = np.empty((*reference.shape[:2], 4), np.uint8)
        source[..., :3] = decoded_source[..., ::-1]
        source[..., 3] = 255
        result["source_png_sha256"], result["payload_bytes"] = sha(png), source.nbytes
        prefix = manifest["private_prefix"]
        start = time.monotonic()
        with FramePublisher(prefix, version=2) as publisher:
            frame_id = 0
            while native is None or native.poll() is None:
                assert time.monotonic() - start < 12, "Size probe exceeded its unchanged bounded deadline"
                frame_id += 1
                submitted = time.perf_counter_ns()
                if publisher.publish(source, frame_id=frame_id, capture_ns=submitted, generation=1, flags=FULL_SBS,
                                     source_rect=(0, 0, eye_width, height), content_rect=(0, 0, eye_width, height)):
                    publishes.append((time.perf_counter_ns() - submitted) / 1e6)
                    rows[frame_id] = {"epoch": publisher.epoch, "generation": 1, "timestamp_meaning": "fixture submit time; not capture"}
                    if native is None:
                        handles = [(directory / "native.stdout.log").open("wb"), (directory / "native.stderr.log").open("wb")]
                        coverage = directory / "coverage"
                        coverage.mkdir()
                        env = dict(os.environ, QUEST3D_PIPELINE_OUTPUT=str(directory), QUEST3D_PIPELINE_PROTOCOL="2", GCOV_PREFIX=str(coverage))
                        native = subprocess.Popen([manifest["executable"], "--gtest_filter=" + manifest["test_filter"],
                            "--gtest_output=xml:" + str(directory / "native.xml")], cwd=directory, env=env,
                            stdout=handles[0], stderr=handles[1], creationflags=subprocess.CREATE_NO_WINDOW)
                parent_rss.append(psutil.Process().memory_info().rss / 1048576)
                if native is not None:
                    try: native_rss.append(psutil.Process(native.pid).memory_info().rss / 1048576)
                    except psutil.NoSuchProcess: pass
                time.sleep(.025)  # Fixture publication cadence, never an ACK or readiness condition.
            assert native.returncode == 0, "Actual native encode failed; retained logs contain the failure"
            result["publisher_skipped"], result["publisher_epoch"] = publisher.skipped, publisher.epoch
        result["native_wall_seconds"] = time.monotonic() - start
        cases = ET.parse(directory / "native.xml").getroot().findall(".//testcase")
        assert len(cases) == 1 and cases[0].get("name") == "NeutralFixtureNvenc"
        assert not cases[0].findall("failure") and not cases[0].findall("skipped")
        result["codecs"] = [decode_case(directory, codec, eye_width, height, reference, bands, rows) for codec in ("h264", "hevc")]
        result["publish_ms"] = stats(publishes)
        result["parent_rss_mib"], result["native_rss_mib"] = stats(parent_rss), stats(native_rss)
        result["published_frames"] = len(rows)
        result["status"] = "measured"
    except BaseException as error:
        result.update(status="failed", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        if native is not None:
            if native.poll() is None:
                native.terminate(); native.wait(5)
                result.update(status="failed", forced_native_cleanup=True)
            native._handle.Close()
        for file in handles: file.close()
        (directory / "source-frames.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
        (directory / "verification.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest_path = args.build.resolve() / "build.json"
    manifest = json.loads(manifest_path.read_text())
    assert manifest["protocol"] == 2 and manifest["frames_per_codec"] == 4
    assert manifest["test_filter"] == "Quest3DColorProbe.NeutralFixtureNvenc"
    assert sha(manifest["executable"]) == manifest["executable_sha256"]
    assert sha(TEST_EXE) == EXPECTED_TEST == manifest["original_test_executable_sha256"]
    prefix = manifest["private_prefix"]
    assert prefix.startswith("Local\\Quest3D.ColorProbe.")
    nonce = prefix.rsplit(".", 1)[1]
    assert len(nonce) == 32 and all(c in "0123456789abcdef" for c in nonce)
    output = args.output.resolve()
    assert output.is_relative_to(ROOT / "artifacts")
    output.mkdir(parents=True, exist_ok=False)
    active = ROOT / "artifacts/active-session.json"
    active_before = sha(active) if active.exists() else None
    result = {"status": "running", "created": datetime.now().astimezone().isoformat(),
              "private_prefix": prefix, "executable_sha256": manifest["executable_sha256"],
              "build_manifest_sha256": sha(manifest_path), "quest_verified": False,
              "actual_ai": False, "network_started": False, "audio_changed": False, "sizes": []}
    try:
        result["host_before"], result["gpu_before"] = host_snapshot(), gpu_snapshot()
        for width, height in ((1600, 900), (1920, 1080)):
            result["sizes"].append(run_size(manifest, output / f"eye-{width}x{height}", width, height))
        result["status"] = "measured"
    except BaseException as error:
        result.update(status="failed", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        result["host_after"], result["gpu_after"] = host_snapshot(), gpu_snapshot()
        result["host_preserved"] = result.get("host_before") == result["host_after"]
        result["active_session_preserved"] = active_before == (sha(active) if active.exists() else None)
        result["native_production_preserved"] = sha(TEST_EXE) == EXPECTED_TEST
        if not all(result[name] for name in ("host_preserved", "active_session_preserved", "native_production_preserved")):
            result["status"] = "preservation_failure"
        result["completed"] = datetime.now().astimezone().isoformat()
        (output / "verification.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps({"output": str(output), "status": result["status"], "sizes": len(result["sizes"])}))
        assert result["status"] == "measured"


if __name__ == "__main__": main()

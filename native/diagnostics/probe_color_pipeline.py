"""Tiny owned FP16 pattern -> actual CUDA tone map -> private Sunshine NVENC -> PC decode."""
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
import torch

from quest3d.bridge import FramePublisher, FULL_SBS, ORIGINAL_2D, CAPTURE_RECEIPT
from quest3d.tonemap import scrgb_to_bgra8
from quest3d.tonemap_cuda import CudaToneMapper

ROOT = Path(__file__).resolve().parents[2]
EXPECTED_LIVE_SHA = "77c950b526ba6b944589b8697cbaaa76b26955e3ae2e412a4cfba7bc93626b63"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def payload_sha(array):
    return hashlib.sha256(memoryview(np.ascontiguousarray(array)).cast("B")).hexdigest()


def host_snapshot():
    rows = []
    for process in psutil.process_iter(["pid", "name", "exe", "create_time"]):
        name = (process.info["name"] or "").lower()
        if name in ("test_sunshine.exe", "quest3d_color_probe.exe"):
            raise RuntimeError("Another native probe is running")
        if name == "sunshine.exe":
            rows.append({"pid": process.pid, "birth": process.info["create_time"],
                         "executable": process.info["exe"], "sha256": sha(process.info["exe"])})
    if len(rows) != 1 or rows[0]["sha256"] != EXPECTED_LIVE_SHA:
        raise RuntimeError("The inspected 77C operational host must remain unchanged")
    return rows


def pattern():
    gray = [0, .0031308, .01, .05, .18, .5, .75, 1, 1.25, 1.5, 2, 3, 4, 8, 16, 100]
    colors = [[1, 0, 0], [0, 1, 0], [0, 0, 1], [1, 1, 0], [0, 1, 1], [1, 0, 1], [.2, .2, .2], [.8, .8, .8]]
    colors += [[.5, .1, .1], [.1, .5, .1], [.1, .1, .5], [.5, .5, .1], [.1, .5, .5], [.5, .1, .5], [0, 0, 0], [1, 1, 1]]
    rgb_values = [[g, g, g] for g in gray] + colors
    source = np.ones((360, 640, 4), dtype=np.float16)
    tiles = []
    for index, value in enumerate(rgb_values):
        x, y = (index % 8) * 80, (index // 8) * 90
        source[y:y+90, x:x+80, :3] = np.asarray(value) * 4.8
        tiles.append({"index": index, "neutral": value[0] == value[1] == value[2],
                      "linear_rgb_normalized": value, "roi": [x+12, y+12, 56, 66]})
    return source, tiles


def measure(rgb, tiles):
    result = []
    for tile in tiles:
        x, y, width, height = tile["roi"]
        values = rgb[y:y+height, x:x+width].astype(np.int16)
        result.append(dict(tile, mean_rgb=values.mean(axis=(0, 1)).tolist(),
            min_rgb=values.min(axis=(0, 1)).tolist(), max_rgb=values.max(axis=(0, 1)).tolist(),
            mean_r_minus_g=float((values[..., 0]-values[..., 1]).mean()),
            mean_b_minus_g=float((values[..., 2]-values[..., 1]).mean()),
            maximum_abs_neutral_channel_delta=int(np.maximum(np.abs(values[..., 0]-values[..., 1]), np.abs(values[..., 2]-values[..., 1])).max()),
            code_zero_fraction=(values == 0).mean(axis=(0, 1)).tolist(),
            code_255_fraction=(values == 255).mean(axis=(0, 1)).tolist()))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    build = args.build.resolve()
    manifest = json.loads((build / "build.json").read_text())
    output = args.output.resolve()
    assert output.is_relative_to(ROOT / "artifacts")
    output.mkdir(parents=True, exist_ok=False)
    executable = Path(manifest["executable"])
    assert sha(executable) == manifest["executable_sha256"]
    prefix = manifest["private_prefix"]
    assert prefix.startswith("Local\\Quest3D.ColorProbe.") and len(prefix.rsplit(".", 1)[1]) == 32
    assert manifest["frames_per_codec"] == 4 and manifest["protocol"] == 2
    active = ROOT / "artifacts/active-session.json"
    active_hash = sha(active) if active.exists() else None
    result = {"status": "running", "created": datetime.now().astimezone().isoformat(),
        "source": "owned_synthetic_FP16_pattern", "source_resolution": [640, 360], "combined_resolution": [1280, 360],
        "synthetic_sdr_white_scale": 4.8, "knee": .75, "network_started": False, "actual_monitor_capture": False,
        "quest_verified": False, "display_calibration": False, "input_enabled": False,
        "private_prefix": prefix, "build_manifest_sha256": sha(build / "build.json"),
        "executable_sha256": sha(executable), "frames_per_codec": 4, "bitrate_kbps": 20000, "fps": 30}
    native = None
    files = []
    rows = {}
    try:
        result["host_before"] = host_snapshot()
        source, tiles = pattern()
        np.save(output / "source-linear-rgba-fp16.npy", source, allow_pickle=False)
        result["fp16_sha256"] = payload_sha(source)
        torch.set_num_threads(2)
        result["torch"] = torch.__version__
        result["gpu"] = torch.cuda.get_device_name(0)
        with CudaToneMapper() as mapper:
            gpu_source = torch.from_numpy(source).to("cuda")
            start = time.perf_counter_ns()
            mapped = mapper(gpu_source, sdr_white_scale=4.8, knee=.75).cpu().numpy()
            result["tone_map_and_readback_ms"] = (time.perf_counter_ns()-start)/1e6
            result["mapper"] = mapper.metadata
        reference = scrgb_to_bgra8(torch.from_numpy(source), sdr_white_scale=4.8, knee=.75).numpy()
        result["cuda_cpu_policy_max_code_error"] = int(np.abs(mapped.astype(np.int16)-reference.astype(np.int16)).max())
        assert result["cuda_cpu_policy_max_code_error"] <= 1
        eye_rgb = mapped[..., 2::-1].copy()
        result["tone_map_tiles"] = measure(eye_rgb, tiles)
        assert all(t["maximum_abs_neutral_channel_delta"] == 0 for t in result["tone_map_tiles"] if t["neutral"])
        sbs = np.concatenate((mapped, mapped), axis=1)
        Image.fromarray(eye_rgb).save(output / "tone-map-eye.png")
        result["bgra_sha256"] = payload_sha(sbs)
        start = time.monotonic()
        with FramePublisher(prefix, version=2) as publisher:
            frame_id = 0
            while native is None or native.poll() is None:
                assert time.monotonic()-start < 12, "Small native fixture exceeded its bounded deadline"
                frame_id += 1
                captured = time.perf_counter_ns()
                if publisher.publish(sbs, frame_id=frame_id, capture_ns=captured, generation=1,
                    flags=FULL_SBS | ORIGINAL_2D | CAPTURE_RECEIPT, source_rect=(0, 0, 640, 360), content_rect=(0, 0, 640, 360)):
                    rows[frame_id] = {"epoch": publisher.epoch, "captured_ns": captured, "bgra_sha256": result["bgra_sha256"]}
                    if native is None:
                        files = [(output / "native.stdout.log").open("wb"), (output / "native.stderr.log").open("wb")]
                        coverage = output / "coverage"
                        coverage.mkdir()
                        env = dict(os.environ, QUEST3D_PIPELINE_OUTPUT=str(output), QUEST3D_PIPELINE_PROTOCOL="2", GCOV_PREFIX=str(coverage))
                        native = subprocess.Popen([str(executable), "--gtest_filter=" + manifest["test_filter"],
                            "--gtest_output=xml:" + str(output / "native.xml")], cwd=output, env=env,
                            stdout=files[0], stderr=files[1], creationflags=subprocess.CREATE_NO_WINDOW)
                time.sleep(.025)
            assert native.returncode == 0, "Native fixture failed; inspect retained output"
        result["native_wall_seconds"] = time.monotonic()-start
        suite = ET.parse(output / "native.xml").getroot()
        cases = suite.findall(".//testcase")
        assert len(cases) == 1 and cases[0].get("name") == "NeutralFixtureNvenc"
        assert not cases[0].findall("failure") and not cases[0].findall("skipped")
        result["codecs"] = []
        for codec in ("h264", "hevc"):
            stem = "quest3d-" + codec
            bitstream = output / (stem + "." + codec)
            with (output / (stem + ".csv")).open(newline="", encoding="utf-8") as file:
                metrics = list(csv.DictReader(file))
            assert len(metrics) == 4
            frames = []
            with av.open(str(bitstream), format=codec) as container:
                for index, frame in enumerate(container.decode(video=0)):
                    assert index < 4 and (frame.width, frame.height) == (1280, 360)
                    metric = metrics[index]
                    source_row = rows[int(metric["publisher_frame_id"])]
                    assert int(metric["publisher_epoch"]) == source_row["epoch"]
                    assert int(metric["flags"]) == FULL_SBS | ORIGINAL_2D | CAPTURE_RECEIPT
                    rgb = frame.to_ndarray(format="rgb24")
                    measured = measure(rgb[:, :640], tiles)
                    neutral = [tile for tile in measured if tile["neutral"]]
                    planes = []
                    for plane in frame.planes:
                        data = np.frombuffer(plane, np.uint8).reshape(plane.height, plane.line_size)[:, :plane.width]
                        planes.append({"width": plane.width, "height": plane.height,
                                       "minimum": int(data.min()), "maximum": int(data.max())})
                    frames.append({"publisher_frame_id": int(metric["publisher_frame_id"]),
                        "decoded_rgb_sha256": payload_sha(rgb), "format": frame.format.name,
                        "color_range": int(frame.color_range), "colorspace": int(frame.colorspace),
                        "plane_ranges": planes, "tiles": measured,
                        "neutral_max_abs_mean_r_minus_g": max(abs(t["mean_r_minus_g"]) for t in neutral),
                        "neutral_max_abs_mean_b_minus_g": max(abs(t["mean_b_minus_g"]) for t in neutral),
                        "eye_rgb_mae": float(np.abs(rgb[:, :640].astype(np.int16)-eye_rgb.astype(np.int16)).mean()),
                        "left_right_rgb_mae": float(np.abs(rgb[:, :640].astype(np.int16)-rgb[:, 640:].astype(np.int16)).mean())})
                    if index == 0:
                        Image.fromarray(rgb[:, :640]).save(output / (codec + "-decoded-eye.png"))
            assert len(frames) == 4
            result["codecs"].append({"codec": codec, "bitstream_sha256": sha(bitstream), "bitstream_bytes": bitstream.stat().st_size, "frames": frames})
        result["published_frames"] = len(rows)
        result["status"] = "measured"
    except BaseException as exc:
        result.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        if native is not None:
            if native.poll() is None:
                native.terminate()
                native.wait(timeout=5)
                result["forced_native_cleanup"] = True
            native._handle.Close()
        for file in files:
            file.close()
        result["host_after"] = host_snapshot()
        result["host_preserved"] = result.get("host_before") == result["host_after"]
        result["active_session_preserved"] = active_hash == (sha(active) if active.exists() else None)
        if not result["host_preserved"] or not result["active_session_preserved"]:
            result["status"] = "preservation_failure"
        result["completed"] = datetime.now().astimezone().isoformat()
        (output / "verification.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        assert result["host_preserved"] and result["active_session_preserved"]
    print(json.dumps({key: value for key, value in result.items() if key not in ("tone_map_tiles", "codecs")}, indent=2))


if __name__ == "__main__":
    main()

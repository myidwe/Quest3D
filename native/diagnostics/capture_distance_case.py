"""Capture ONE current WGC HDR/fused frame, real AI280 and fresh strict/float stereo.

Read-only toward the live producer. Fresh min/max normalization cannot reproduce
its unexposed smoothed depth limits; this is not an exact live-pixel or FPS proof.
"""

import argparse
from dataclasses import asdict
from datetime import datetime
import hashlib
import importlib.metadata
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "artifacts/capture/hdr-experimental/package-0d8bea69406e"
CODE = ("src/quest3d/capture.py", "src/quest3d/depth.py", "src/quest3d/resample.py",
        "src/quest3d/tonemap_cuda.py", "src/quest3d/shaders/tone_map.cu",
        "src/quest3d/stereo.py", "src/quest3d/colour_fit.py", "src/quest3d/session.py",
        "src/quest3d/forward_warp.py", "src/quest3d/forward_warp_cuda.py",
        "src/quest3d/shaders/forward_warp.cu", "config/models.json")
BIND = ("session_id", "stream_epoch", "revision", "requested_mode", "disparity",
        "eye_width", "eye_height", "rgb_resize_filter", "stereo_method",
        "depth_refinement", "colour_precision")


def stamp():
    return datetime.now().astimezone().isoformat()


def sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def option(argv, name, default=None):
    for index, value in enumerate(argv):
        if value == name:
            return argv[index + 1]
        if value.startswith(name + "="):
            return value.split("=", 1)[1]
    return default


def find_producer(session):
    import psutil
    found = []
    for process in psutil.process_iter():
        try:
            if process.name().lower() not in ("python.exe", "pythonw.exe"):
                continue  # Skip the quest3d.exe entry-point launcher, not its Python worker.
            argv = process.cmdline()
            target = option(argv, "--output")
            if "serve" not in argv or target is None:
                continue
            path = Path(target)
            if (path if path.is_absolute() else Path(process.cwd()) / path).resolve() == session:
                found.append(process)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    # Windows venv may keep a Python redirector with the exact same argv alive.
    # Select its matching child worker, never an arbitrary first matching PID.
    parent_ids = {process.ppid() for process in found}
    found = [process for process in found if process.pid not in parent_ids]
    if len(found) != 1:
        raise RuntimeError(f"Expected one actual serve process for session; found {len(found)}")
    return found[0]


def process_snapshot(process):
    if not process.is_running():
        raise RuntimeError("Bound producer exited or its PID was reused")
    return {"pid": process.pid, "create_time": process.create_time(),
            "exe": process.exe(), "argv": process.cmdline(), "cwd": process.cwd()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    session = args.session.resolve(strict=True)
    output = args.output.resolve()
    output.relative_to((ROOT / "artifacts").resolve())
    output.mkdir(parents=True, exist_ok=False)
    sys.path.insert(0, str(PACKAGE.resolve(strict=True)))
    import numpy as np
    import torch
    import torch.nn.functional as F
    from PIL import Image
    from quest3d.assets import model_spec, verified_model
    from quest3d.capture import GPUDesktopCapture
    from quest3d.depth import DepthEngine
    from quest3d.session_control import read_json
    from quest3d.stereo import StereoSynthesizer

    report = {"status": "RUNNING", "started_at": stamp(), "scope": __doc__,
              "session_directory": str(session), "errors": [], "live_control_written": False,
              "normalization_caveat": "Fresh scene min/max, not the live worker's smoothed limits. "
              "D is the same configured pixel parameter, not proof of identical live per-pixel disparity. "
              "Independent capture frame/generation IDs do not identify a live published frame."}
    synth = process = None
    paths = [Path(__file__).resolve(), *(ROOT / name for name in CODE)]
    hashes = lambda: {str(path.relative_to(ROOT)): sha(path) for path in paths}

    def snapshot_status():
        status = read_json(session / "status.json")
        age = (time.perf_counter_ns() - status["updated_monotonic_ns"]) / 1e9
        if not status.get("running") or not 0 <= age <= 10:
            raise RuntimeError(f"Session is stopped/stale (status age {age:.3f}s)")
        return status

    def save_depth(name, tensor):
        values = tensor.detach().cpu().numpy()
        if not np.isfinite(values).all():
            raise RuntimeError(f"Nonfinite {name}")
        np.save(output / f"{name}.npy", values, allow_pickle=False)
        Image.fromarray(np.round(values.clip(0, 1) * 255).astype(np.uint8)).save(output / f"{name}.png")
        return {"shape": list(values.shape), "min": float(values.min()), "max": float(values.max()),
                "npy": "float32 inverse depth; near=1", "png": "8-bit diagnostic preview; [0,1] maps to [0,255]"}

    try:
        before = report["status_before"] = snapshot_status()
        process = find_producer(session)
        identity = report["producer_before"] = process_snapshot(process)
        argv = identity["argv"]
        for name, expected in (("--monitor", "1"), ("--ai-size", "280"), ("--hdr-tonemap", "fused")):
            if option(argv, name) != expected:
                raise RuntimeError(f"Actual producer {name} does not match required {expected}")
        if "--experimental-hdr" not in argv:
            raise RuntimeError("Actual producer does not use experimental HDR capture")
        for key, expected in (("requested_mode", "3d"), ("rgb_resize_filter", "bicubic-aa"),
                              ("stereo_method", "forward-cuda"), ("depth_refinement", "none"),
                              ("colour_precision", "float")):
            if before[key] != expected:
                raise RuntimeError(f"Current {key} differs from required {expected}")
        if not 0 < before["disparity"] <= before["eye_width"] * .04:
            raise RuntimeError("Current disparity is not positive/in range")
        report["code_before"] = hashes()
        free = int(subprocess.check_output(["nvidia-smi", "--id=0", "--query-gpu=memory.free",
                   "--format=csv,noheader,nounits"], text=True, timeout=10).strip())
        report["free_vram_before_mib"] = free
        if free < 2500:
            raise RuntimeError("At least 2500 MiB free VRAM is required for this bounded probe")
        torch.set_num_threads(4)
        report["versions"] = {"torch": torch.__version__, "wc_cuda": importlib.metadata.version("wc_cuda")}
        with GPUDesktopCapture(monitor=1, experimental_hdr=True, hdr_tonemap="fused") as capture:
            frame = capture.grab(timeout_seconds=5)
            pixels = frame.bgra
            report["capture"] = {"frame_id": frame.frame_id, "generation": frame.geometry_generation,
                "captured_ns": frame.captured_ns, "shape": list(pixels.shape), "source_id": frame.source_id,
                "source_identity": asdict(frame.source_identity) if hasattr(frame.source_identity, "__dataclass_fields__") else frame.source_identity,
                "geometry": asdict(frame.geometry), "monitor": 1, "backend": capture.backend,
                "color_profile": capture.color_profile, "color_processing_ms": frame.color_processing_ms}
        original = pixels.cpu().numpy().copy()
        np.save(output / "source-bgra.npy", original, allow_pickle=False)
        Image.fromarray(original[:, :, [2, 1, 0]]).save(output / "source.png")
        engine = DepthEngine(280)
        depth = engine.infer(pixels, frame_id=frame.frame_id, generation=frame.geometry_generation)
        raw = depth.tensor.cpu().numpy().copy()
        np.save(output / "depth-280.npy", raw, allow_pickle=False)
        report["model"] = {**model_spec(), "verified_weights": str(verified_model())}
        report["depth"] = {"frame_id": depth.frame_id, "generation": depth.generation,
            "input_shape": list(depth.input_shape), "shape": list(raw.shape),
            "min": float(raw.min()), "max": float(raw.max()), "preprocess_ms": depth.preprocess_ms,
            "inference_ms": depth.inference_ms, "timing_scope": "One cold call under live contention; not a benchmark"}
        synth = StereoSynthesizer(before["eye_width"], before["eye_height"], before["disparity"],
            resize_filter=before["rgb_resize_filter"], stereo_method=before["stereo_method"],
            depth_refinement=before["depth_refinement"], colour_precision=before["colour_precision"])
        stereo = synth.synthesize(pixels, depth, frame_id=frame.frame_id, generation=frame.geometry_generation)
        report["stereo"] = {key: getattr(stereo, key) for key in ("frame_id", "generation", "mode",
            "scene_reset", "depth_range", "content_rect", "colour_precision", "colour_precision_reason",
            "depth_refinement", "depth_refinement_reason")}
        report["stereo"].update(disparity_px=synth.disparity_px, convergence=synth.convergence)
        low, high = stereo.depth_range
        normalized = ((depth.tensor - low) / max(high - low, 1e-6)).clamp(0, 1)
        _, _, width, height = stereo.content_rect
        eye_depth = F.interpolate(normalized[:, None], size=(height, width), mode="bilinear", align_corners=True)[0, 0]
        report["normalized_ai"] = save_depth("depth-280-normalized", normalized[0])
        report["eye_depth"] = save_depth("depth-eye", eye_depth)
        report["eye_depth"]["scope"] = "Same normalization/upsample formula as actual none-refinement synth; content rectangle only, shared by both eyes"
        for name, values in (("stereo-sbs", stereo.bgra), ("left", stereo.bgra[:, :synth.width]),
                             ("right", stereo.bgra[:, synth.width:])):
            Image.fromarray(values[:, :, [2, 1, 0]]).save(output / f"{name}.png")
        report["same_frame_generation"] = ((stereo.frame_id, stereo.generation) ==
            (depth.frame_id, depth.generation) == (frame.frame_id, frame.geometry_generation))
        report["source_unchanged"] = bool(np.array_equal(original, pixels.cpu().numpy()))
        report["raw_depth_unchanged"] = bool(np.array_equal(raw, depth.tensor.cpu().numpy()))
        if not (report["same_frame_generation"] and report["source_unchanged"] and report["raw_depth_unchanged"]
                and stereo.mode == "3d" and stereo.scene_reset and stereo.colour_precision == "float"
                and stereo.colour_precision_reason is None and stereo.depth_refinement == "none"):
            raise RuntimeError("Actual synthesis/frame/ownership contract failed")
    except BaseException as exc:
        report["errors"].append(f"{type(exc).__name__}: {exc}")
    finally:
        if synth is not None:
            try:
                synth.close()
                report["synth_closed"] = True
            except BaseException as exc:
                report["errors"].append(f"synth.close: {type(exc).__name__}: {exc}")
        try:
            after = report["status_after"] = snapshot_status()
            report["producer_after"] = process_snapshot(process) if process is not None else None
            report["binding_unchanged"] = (all(report["status_before"][key] == after[key] for key in BIND)
                and report.get("producer_before") == report["producer_after"])
            if not report["binding_unchanged"]:
                raise RuntimeError("Producer/session/epoch/settings changed during capture")
            report["code_after"] = hashes()
            if report.get("code_before") != report["code_after"]:
                raise RuntimeError("Diagnostic production code changed during capture")
        except BaseException as exc:
            report["errors"].append(f"final binding: {type(exc).__name__}: {exc}")
        report["files"] = {path.name: sha(path) for path in output.iterdir() if path.is_file()}
        report["status"] = "FAIL" if report["errors"] else "PASS"
        report["finished_at"] = stamp()
        (output / "capture.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"status": report["status"], "output": str(output), "errors": report["errors"]}))
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

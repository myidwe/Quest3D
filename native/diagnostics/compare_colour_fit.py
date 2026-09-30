"""Saved-frame GPU comparison of middle uint8 rounding versus float colour fit.

Timing includes colour fit, completed strict forward projection and one final
eye quantization. It EXCLUDES AI, depth preparation, Full-SBS packing/readback,
capture, bridge, codec and Quest. CUDA events include current-stream dispatch
gaps, not isolated kernel time. Differences are not ground-truth quality scores.
No live source/settings are touched. Run only with the coordinated GPU slot.
"""

import argparse
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import time
import traceback

import numpy as np
from PIL import Image
import torch
import torch.nn.functional as F

from quest3d.colour_fit import fit_bgra_float
from quest3d.forward_warp_cuda import CudaForwardWarper
from quest3d.stereo import StereoSynthesizer


ROOT = Path(__file__).resolve().parents[2]
SCENE = ROOT / "artifacts/diagnostics/target-scene-20260910"
SOURCE, DEPTH = SCENE / "source-gdi.png", SCENE / "replay-280-a/depth.npy"
SOURCE_SHA = "ebbf586ee710ddc09aa658289577e427bbcb5f66acf34ebac2b555f9f2672117"
DEPTH_SHA = "6ffb76144c602369e38c53b03311a0ef26b0e9bf4bff38b073ef170f3954ae1b"
STRICT = {
    "src/quest3d/forward_warp.py": "4982e37f0f7a9dfa5903d5ea760015bf3b76565e00414d90ad1e3eb6fcb45f96",
    "src/quest3d/forward_warp_cuda.py": "17438ab57347e8ff81d2aff76ad4bfbe33a95c41e33c346c5eaee5d0269cdf15",
    "src/quest3d/shaders/forward_warp.cu": "7127fe594a2e08db6593f90385904e13c5221a3724425780fb0c0a2ba014eb3e",
}
ROIS = {"characters": (720, 300, 525, 488), "rail": (970, 510, 200, 150)}
_FAILED_CLOSE_OWNERS = []


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def stamp():
    return datetime.now().astimezone().isoformat()


def tensor_sha(value):
    return hashlib.sha256(value.detach().cpu().contiguous().numpy().tobytes()).hexdigest()


def gpu_state():
    try:
        result = subprocess.run(["C:/Windows/System32/nvidia-smi.exe",
            "--query-gpu=index,uuid,pstate,clocks.current.graphics,clocks.current.memory,temperature.gpu,utilization.gpu,memory.used",
            "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return {"at": stamp(), "exit_code": result.returncode,
                "stdout": result.stdout, "stderr": result.stderr, "scope": "global GPU, not process attribution"}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"at": stamp(), "error": str(exc)}


class EvidenceWarper(CudaForwardWarper):
    """Observe private raw coverage only AFTER the production completion gate.

    The exact wrapper hash above binds this diagnostic to its 15-element owned
    bundle. Normal timed calls do not retain/copy the bundle or read the device.
    This does not replace synchronization or manufacture a completion receipt.
    """
    observe = False
    receipt = None

    def _complete(self, stream):
        bundle = self._inflight if self.observe else None
        super()._complete(stream)
        if bundle is not None:
            if len(bundle) != 15 or tuple(bundle[6].shape) != (2, 1080, 1920):
                raise RuntimeError("Unexpected pinned raw-coverage bundle")
            self.receipt = {name: bundle[index].cpu().numpy().copy()
                            for name, index in (("remaining", 6), ("nearest", 7), ("farthest", 8))}


def difference(old, new):
    delta = np.abs(new.astype(np.int16) - old.astype(np.int16))
    return {"max_abs_lsb": int(delta.max()), "mean_abs_lsb": float(delta.mean()),
            "changed_channel_fraction": float(np.mean(delta != 0)),
            "changed_pixel_fraction": float(np.mean(np.any(delta != 0, axis=-1)))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--device", type=int, default=0)
    parser.add_argument("--repeats", type=int, default=30)
    parser.add_argument("--disparity", type=float, default=13.2)
    args = parser.parse_args()
    if not 1 <= args.repeats <= 200 or not math.isfinite(args.disparity) or not 0 < args.disparity <= 76.8:
        raise ValueError("Require repeats 1..200 and disparity (0,76.8]")
    output = args.output.resolve()
    output.relative_to(ROOT / "artifacts")
    output.mkdir(parents=True, exist_ok=False)
    paths = [Path(__file__).resolve(), SOURCE, DEPTH, DEPTH.parent / "replay.json",
             ROOT / "src/quest3d/colour_fit.py", ROOT / "src/quest3d/stereo.py",
             *(ROOT / name for name in STRICT)]
    hashes = lambda: {str(path.relative_to(ROOT)): sha(path) for path in paths}
    report = {"status": "RUNNING", "started_at": stamp(), "pid": os.getpid(), "scope": __doc__,
              "samples": [], "errors": [], "warmup_each": 5, "measured_each": args.repeats,
              "disparity": args.disparity, "convergence": .5, "rois_xywh": ROIS}
    owner = None
    try:
        report["hash_before"] = hashes()
        if sha(SOURCE) != SOURCE_SHA or sha(DEPTH) != DEPTH_SHA:
            raise ValueError("Saved RGB/depth bytes changed")
        for name, expected in STRICT.items():
            if sha(ROOT / name) != expected:
                raise ValueError(f"Pinned strict source changed: {name}")
        provenance = json.loads((DEPTH.parent / "replay.json").read_text(encoding="utf-8"))
        if provenance["source_sha256"] != SOURCE_SHA or provenance["raw_depth_shape"] != [1, 280, 504]:
            raise ValueError("Saved depth is not bound to this RGB source")
        report["provenance"] = {"source_sha256": SOURCE_SHA, "depth_sha256": DEPTH_SHA,
                                "ai_size": provenance["ai_size"], "fresh_inference": False,
                                "normalization": "one shared min/max then bilinear align_corners=True",
                                "source_size": [2560, 1440], "eye_size": [1920, 1080]}
        rgb = np.array(Image.open(SOURCE).convert("RGB"), copy=True)
        raw = np.load(DEPTH, allow_pickle=False)
        if rgb.shape != (1440, 2560, 3) or raw.shape != (1, 280, 504) or raw.dtype != np.float32 or not np.isfinite(raw).all():
            raise ValueError("Unexpected source/depth layout or nonfinite data")
        bgra = np.concatenate((rgb[:, :, ::-1], np.full((*rgb.shape[:2], 1), 255, np.uint8)), axis=-1)
        torch.set_num_threads(4)
        with torch.cuda.device(args.device), torch.inference_mode():
            source = torch.from_numpy(bgra).to(device=args.device)
            low_depth = torch.from_numpy(raw).to(device=args.device)
            low, high = torch.aminmax(low_depth)
            depth = ((low_depth - low) / (high - low).clamp_min(1e-6)).clamp(0, 1)
            depth = F.interpolate(depth[:, None], (1080, 1920), mode="bilinear", align_corners=True)[0, 0]
            torch.cuda.current_stream().synchronize()
            report["prepared_hash_before"] = {"bgra": tensor_sha(source), "depth": tensor_sha(depth)}
            report["channel_order"] = {"capture_and_projection": "BGR", "saved_PNG": "RGB"}
            synth = StereoSynthesizer(1920, 1080, disparity_px=args.disparity, resize_filter="bicubic-aa")

            def fit(name):
                if name == "baseline":
                    pixels, rect = synth._fit(source)
                    return pixels[:, :, :3].permute(2, 0, 1)[None].float() / 255, rect
                result = fit_bgra_float(source, 1920, 1080)
                return result.image, result.content_rect

            owner = EvidenceWarper(device=args.device)
            report["compiler"] = owner.metadata
            fitted = {name: fit(name) for name in ("baseline", "candidate")}
            if any(rect != (0, 0, 1920, 1080) for _, rect in fitted.values()):
                raise RuntimeError("Letterbox geometry changed")
            # Canonical old middle-round oracle, independent of _fit dispatch.
            canonical = F.interpolate(source.permute(2, 0, 1)[None].float(), (1080, 1920),
                                      mode="bicubic", align_corners=False, antialias=True)
            old_oracle = canonical.round().clamp(0, 255).to(torch.uint8)[:, :3].float() / 255
            float_oracle = canonical[:, :3].clamp(0, 255) / 255
            report["fit_oracles"] = {
                "old_middle_round_exact": bool(torch.equal(old_oracle, fitted["baseline"][0])),
                "candidate_float_max_abs": float((float_oracle - fitted["candidate"][0]).abs().max()),
                "baseline_quantizations": "middle fit round + one final eye round",
                "candidate_quantizations": "one final eye round only"}
            if not report["fit_oracles"]["old_middle_round_exact"]:
                raise RuntimeError("Actual baseline no longer matches its middle-round oracle")
            torch.testing.assert_close(fitted["candidate"][0], float_oracle, rtol=0, atol=1e-7)
            del canonical, old_oracle, float_oracle
            images, geometry = {}, {}
            owner.observe = True
            for name, (image, _) in fitted.items():
                result = owner(image, depth, args.disparity, .5)
                # The SAME final quantizer is called exactly once in each path.
                images[name] = (result.eyes.clamp(0, 1) * 255).round().to(torch.uint8).permute(0, 2, 3, 1).cpu().numpy()
                geometry[name] = {**owner.receipt, **{key: getattr(result, key).cpu().numpy().copy()
                    for key in ("hole_mask", "filled_mask", "reconstructed_mask", "estimated_donor_mask")}}
            owner.observe, owner.receipt = False, None
            report["geometry_exact"] = {key: bool(np.array_equal(geometry["baseline"][key], geometry["candidate"][key]))
                                        for key in geometry["baseline"]}
            if not all(report["geometry_exact"].values()):
                raise RuntimeError("Colour-only fit changed projection coverage or masks")
            report["coverage"] = {name: {"raw_hole_fraction": geometry[name]["hole_mask"].mean(axis=(1, 2)).tolist(),
                "uncovered_area_mean": geometry[name]["remaining"].mean(axis=(1, 2)).tolist()} for name in geometry}
            report["difference"] = {"both": difference(images["baseline"], images["candidate"])}
            for eye, side in enumerate(("left", "right")):
                report["difference"][side] = difference(images["baseline"][eye], images["candidate"][eye])
                for name in images:
                    png = Image.fromarray(images[name][eye][:, :, ::-1])
                    png.save(output / f"{name}-{side}.png")
                    for roi, (x, y, w, h) in ROIS.items():
                        png.crop((x, y, x + w, y + h)).save(output / f"{name}-{side}-{roi}.png")
                for roi, (x, y, w, h) in ROIS.items():
                    report["difference"][f"{side}:{roi}"] = difference(
                        images["baseline"][eye, y:y+h, x:x+w], images["candidate"][eye, y:y+h, x:x+w])
            del fitted, result, image, images, geometry

            def measured_call(name):
                events = [torch.cuda.Event(enable_timing=True) for _ in range(4)]
                started = time.perf_counter_ns()
                events[0].record()
                colour, _ = fit(name)
                events[1].record()
                projected = owner(colour, depth, args.disparity, .5)
                events[2].record()
                quantized = (projected.eyes.clamp(0, 1) * 255).round().to(torch.uint8)
                events[3].record()
                events[3].synchronize()
                completed = time.perf_counter_ns()
                return {"version": name, "start_qpc_ns": started, "completed_qpc_ns": completed,
                    "wall_ms": (completed - started) / 1e6,
                    "cuda_total_ms": events[0].elapsed_time(events[3]),
                    "cuda_fit_ms": events[0].elapsed_time(events[1]),
                    "cuda_projection_ms": events[1].elapsed_time(events[2]),
                    "cuda_final_quant_ms": events[2].elapsed_time(events[3])}

            for repeat in range(5):
                for name in (("baseline", "candidate") if repeat % 2 == 0 else ("candidate", "baseline")):
                    measured_call(name)
            report["gpu_before"] = gpu_state()
            report["measurement_started_at"] = stamp()
            for repeat in range(args.repeats):
                for position, name in enumerate(("baseline", "candidate") if repeat % 2 == 0 else ("candidate", "baseline")):
                    report["samples"].append({"round": repeat, "position": position, **measured_call(name)})
            report["measurement_finished_at"] = stamp()
            report["gpu_after"] = gpu_state()
            keys = ("wall_ms", "cuda_total_ms", "cuda_fit_ms", "cuda_projection_ms", "cuda_final_quant_ms")
            report["timing"] = {name: {key: dict(zip(("p50", "p95"), np.percentile(
                [sample[key] for sample in report["samples"] if sample["version"] == name], [50, 95]).tolist()))
                for key in keys} for name in ("baseline", "candidate")}
            report["prepared_hash_after"] = {"bgra": tensor_sha(source), "depth": tensor_sha(depth)}
            if report["prepared_hash_before"] != report["prepared_hash_after"]:
                raise RuntimeError("Shared original pixels or normalized depth were modified")
        report["status"] = "PASS"
    except BaseException:
        report["status"] = "FAIL"
        report["errors"].append(traceback.format_exc())
    finally:
        if owner is not None:
            try:
                owner.close()
                report["close"] = {"closed": owner.closed, "inflight_released": owner._inflight is None,
                                   "module_unloaded": not owner.module.value}
            except BaseException:
                _FAILED_CLOSE_OWNERS.append(owner)
                report["status"] = "FAIL"
                report["errors"].append(traceback.format_exc())
        try:
            report["hash_after"] = hashes()
            report["all_files_stable"] = report.get("hash_before") == report["hash_after"]
            if not report["all_files_stable"]:
                report["status"] = "FAIL"
                report["errors"].append("Source, diagnostic, or input bytes changed")
        except BaseException:
            report["status"] = "FAIL"
            report["errors"].append(traceback.format_exc())
        report["finished_at"] = stamp()
        (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps({key: value for key, value in report.items() if key != "samples"}, indent=2))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

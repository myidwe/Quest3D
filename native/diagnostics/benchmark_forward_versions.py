"""Interleave frozen pre-strict and strict CUDA owners on one prepared RGB/depth pair.

This compares completed forward calls (validation + projection + fill + sync).
It excludes AI, RGB fitting, depth normalization/upscaling, packing/readback,
capture, encoding and Quest. It does not measure sustainable video FPS. No live
settings, GPU clocks, services, environment packages or global CUDA policy change.
"""

import argparse
import ast
import csv
from datetime import datetime
import hashlib
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import traceback

import numpy as np
from PIL import Image
import torch
import torch.nn.functional as F

from quest3d.forward_warp_cuda import CudaForwardWarper


ROOT = Path(__file__).resolve().parents[2]
BEFORE = ROOT / "artifacts/diagnostics/forward-warp-strict-20260910-a/before"
SCENE = ROOT / "artifacts/diagnostics/target-scene-20260910"
SOURCE = SCENE / "source-gdi.png"
DEPTH = SCENE / "replay-280-a/depth.npy"
PROVENANCE = DEPTH.parent / "replay.json"
EXPECTED_BACKUP = {
    "forward_warp.py": "e21244608c22cc9bb2d69feb988edc6699bfaf87d6534948d6b6c20f3bf614df",
    "forward_warp_cuda.py": "825f91eeaee3711ea958a00d2707c0e6e5b747a7f4e65752e2ac9ec6de813b76",
    "forward_warp.cu": "4404f0cfacbb4f2e7ef6d060f13de2b5d22862dce25d6bffd89769aebb91ed9d",
}
EXPECTED_STRICT = {
    "src/quest3d/forward_warp.py": "4982e37f0f7a9dfa5903d5ea760015bf3b76565e00414d90ad1e3eb6fcb45f96",
    "src/quest3d/forward_warp_cuda.py": "17438ab57347e8ff81d2aff76ad4bfbe33a95c41e33c346c5eaee5d0269cdf15",
    "src/quest3d/shaders/forward_warp.cu": "7127fe594a2e08db6593f90385904e13c5221a3724425780fb0c0a2ba014eb3e",
}
EXPECTED_SOURCE = "ebbf586ee710ddc09aa658289577e427bbcb5f66acf34ebac2b555f9f2672117"
EXPECTED_DEPTH = "6ffb76144c602369e38c53b03311a0ef26b0e9bf4bff38b073ef170f3954ae1b"
_FAILED_CLOSE_OWNERS = []  # Preserve failed in-flight ownership until process exit.


def stamp():
    return datetime.now().astimezone().isoformat()


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def hashes(paths):
    return {str(path.relative_to(ROOT)): sha(path) for path in paths}


def definition(path, name):
    node = next(node for node in ast.parse(path.read_text(encoding="utf-8")).body
                if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name == name)
    return ast.dump(node, include_attributes=False)


def gpu_state():
    fields = ("index", "uuid", "name", "pstate", "clocks.current.graphics", "clocks.current.sm",
              "clocks.current.memory", "temperature.gpu", "utilization.gpu", "utilization.memory",
              "power.draw", "memory.used")
    executable = shutil.which("nvidia-smi") or "C:/Windows/System32/nvidia-smi.exe"
    record = {"at": stamp(), "fields": fields, "scope": "Global GPU state, not process attribution"}
    try:
        result = subprocess.run([executable, "--query-gpu=" + ",".join(fields),
                                 "--format=csv,noheader,nounits"], capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=10,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        record.update(exit_code=result.returncode, stdout=result.stdout, stderr=result.stderr)
        if result.returncode == 0:
            record["gpus"] = [dict(zip(fields, (value.strip() for value in row)))
                              for row in csv.reader(io.StringIO(result.stdout)) if row]
    except (OSError, subprocess.TimeoutExpired) as exc:
        record["error"] = f"{type(exc).__name__}: {exc}"
    return record


def tensor_sha(tensor):
    return hashlib.sha256(tensor.detach().cpu().contiguous().numpy().tobytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New directory beneath project artifacts")
    parser.add_argument("--device", type=int, default=0)
    parser.add_argument("--disparity", type=float, default=13.2)
    args = parser.parse_args()
    if not math.isfinite(args.disparity) or args.disparity <= 0:
        raise ValueError("Disparity must be finite and positive")
    output = args.output.resolve()
    output.relative_to(ROOT / "artifacts")
    output.mkdir(parents=True, exist_ok=False)
    report = {"started_at": stamp(), "pid": os.getpid(), "status": "RUNNING", "scope": __doc__,
              "warmup_per_version": 5, "measured_per_version": 100, "disparity": args.disparity,
              "convergence": .5, "samples": [], "close": {}, "errors": []}
    watched = [Path(__file__).resolve(), SOURCE, DEPTH, PROVENANCE,
               *(BEFORE / name for name in EXPECTED_BACKUP), *(ROOT / name for name in EXPECTED_STRICT)]
    owners, module_name = {}, None
    try:
        report["hash_before"] = hashes(watched)
        for name, expected in EXPECTED_BACKUP.items():
            if sha(BEFORE / name) != expected:
                raise ValueError(f"Frozen baseline hash differs: {name}")
        for name, expected in EXPECTED_STRICT.items():
            if sha(ROOT / name) != expected:
                raise ValueError(f"Strict source hash differs: {name}")
        provenance = json.loads(PROVENANCE.read_text(encoding="utf-8"))
        if sha(SOURCE) != EXPECTED_SOURCE or sha(DEPTH) != EXPECTED_DEPTH:
            raise ValueError("Preserved source/depth bytes differ from the reviewed pair")
        if provenance["source_sha256"] != sha(SOURCE) or provenance["raw_depth_shape"] != [1, 280, 504]:
            raise ValueError("Source/depth provenance mismatch")
        report["provenance"] = {"source_sha256": sha(SOURCE), "depth_sha256": sha(DEPTH),
                                "replay_sha256": sha(PROVENANCE), "ai_size": provenance["ai_size"]}
        # The old wrapper's relative import intentionally resolves to the common
        # quest3d.forward_warp module. Prove both used definitions are unchanged.
        common = ROOT / "src/quest3d/forward_warp.py"
        for name in ("_validate", "ForwardWarpResult"):
            if definition(BEFORE / "forward_warp.py", name) != definition(common, name):
                raise ValueError(f"Shared baseline dependency changed: {name}")
        report["shared_dependency_ast_equal"] = ["_validate", "ForwardWarpResult"]
        baseline = output / "baseline"
        (baseline / "shaders").mkdir(parents=True)
        copied = [(BEFORE / "forward_warp_cuda.py", baseline / "forward_warp_cuda.py"),
                  (BEFORE / "forward_warp.cu", baseline / "shaders/forward_warp.cu")]
        for original, destination in copied:
            shutil.copyfile(original, destination)
            if sha(original) != sha(destination):
                raise ValueError("Baseline copy differs from original backup")
        watched.extend(destination for _, destination in copied)
        report["hash_before"].update(hashes(destination for _, destination in copied))
        # Unique package-qualified name preserves .forward_warp, while __file__
        # makes the owner compile ONLY the copied baseline/shaders source.
        module_name = "quest3d._baseline_forward_benchmark_" + hashlib.sha256(str(output).encode()).hexdigest()[:16]
        if module_name in sys.modules:
            raise RuntimeError("Baseline module name already loaded")
        spec = importlib.util.spec_from_file_location(module_name, baseline / "forward_warp_cuda.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)

        torch.set_num_threads(4)
        pixels = np.array(Image.open(SOURCE).convert("RGB"), copy=True)
        raw = np.load(DEPTH, allow_pickle=False)
        if pixels.shape != (1440, 2560, 3) or raw.shape != (1, 280, 504) or raw.dtype != np.float32:
            raise ValueError("Unexpected preserved RGB/depth layout")
        if not np.isfinite(raw).all():
            raise ValueError("Nonfinite preserved depth")
        with torch.cuda.device(args.device):
            prep_started = time.perf_counter_ns()
            source_tensor = torch.from_numpy(pixels).to(device=args.device).permute(2, 0, 1)[None].float()
            image = F.interpolate(source_tensor, size=(1080, 1920), mode="bicubic",
                                  align_corners=False, antialias=True).round().clamp(0, 255) / 255
            depth = torch.from_numpy(raw.copy()).to(device=args.device)
            low, high = torch.aminmax(depth)
            depth = ((depth - low) / (high - low).clamp_min(1e-6)).clamp(0, 1)
            depth = F.interpolate(depth[:, None], size=(1080, 1920),
                                  mode="bilinear", align_corners=True)[0, 0]
            torch.cuda.current_stream().synchronize()
            report["one_time_preparation_ms"] = (time.perf_counter_ns() - prep_started) / 1e6
            del source_tensor
            prepared_before = {"rgb": tensor_sha(image), "depth": tensor_sha(depth)}
            report["prepared_tensor_hash_before"] = prepared_before
            report["prepared"] = {"rgb_shape": list(image.shape), "depth_shape": list(depth.shape),
                                  "device": str(image.device), "same_tensor_objects_for_both": True}
            owners["baseline"] = module.CudaForwardWarper(device=args.device)
            owners["strict"] = CudaForwardWarper(device=args.device)
            report["compiler"] = {name: owner.metadata for name, owner in owners.items()}
            last_results = {}
            for repeat in range(5):
                for name in (("baseline", "strict") if repeat % 2 == 0 else ("strict", "baseline")):
                    last_results[name] = owners[name](image, depth, args.disparity, .5)
            report["gpu_before"] = gpu_state()
            torch.cuda.reset_peak_memory_stats(args.device)
            report["allocated_before_mib"] = torch.cuda.memory_allocated(args.device) / 2**20
            report["measurement_started_at"] = stamp()
            for repeat in range(100):
                order = ("baseline", "strict") if repeat % 2 == 0 else ("strict", "baseline")
                for position, name in enumerate(order):
                    started = time.perf_counter_ns()
                    result = owners[name](image, depth, args.disparity, .5)
                    completed = time.perf_counter_ns()
                    report["samples"].append({"round": repeat, "position": position, "version": name,
                        "start_qpc_ns": started, "completed_qpc_ns": completed,
                        "wall_ms": (completed - started) / 1e6})
                    last_results[name] = result
                    if owners[name]._inflight is not None:
                        raise RuntimeError("Owner returned without a completion receipt")
            report["measurement_finished_at"] = stamp()
            report["gpu_after"] = gpu_state()
            report["process_peak_allocated_mib"] = torch.cuda.max_memory_allocated(args.device) / 2**20
            report["summary"] = {name: dict(zip(("p50_ms", "p95_ms", "min_ms", "max_ms"),
                np.percentile([sample["wall_ms"] for sample in report["samples"] if sample["version"] == name],
                              [50, 95, 0, 100]).tolist())) for name in owners}
            report["prepared_tensor_hash_after"] = {"rgb": tensor_sha(image), "depth": tensor_sha(depth)}
            if prepared_before != report["prepared_tensor_hash_after"]:
                raise RuntimeError("Prepared shared inputs were mutated")
            report["final_unfilled_fraction"] = {name: (value.hole_mask & ~value.filled_mask).float()
                .mean(dim=(1, 2)).cpu().tolist() for name, value in last_results.items()}
        report["status"] = "PASS"
    except BaseException:
        report["status"] = "FAIL"
        report["errors"].append(traceback.format_exc())
    finally:
        for name, owner in owners.items():
            try:
                owner.close()
                report["close"][name] = {"closed": owner.closed, "module_unloaded": not owner.module.value,
                                          "inflight_released": owner._inflight is None}
            except BaseException:
                _FAILED_CLOSE_OWNERS.append(owner)
                report["status"] = "FAIL"
                report["errors"].append(f"{name} close failed:\n{traceback.format_exc()}")
        if module_name and not _FAILED_CLOSE_OWNERS:
            sys.modules.pop(module_name, None)
        try:
            report["hash_after"] = hashes(watched)
            report["all_files_stable"] = report.get("hash_before") == report["hash_after"]
            if not report["all_files_stable"]:
                report["status"] = "FAIL"
                report["errors"].append("Code, backup, copied baseline, or preserved inputs changed")
        except BaseException:
            report["status"] = "FAIL"
            report["errors"].append(traceback.format_exc())
        report["finished_at"] = stamp()
        (output / "benchmark.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps({key: value for key, value in report.items() if key != "samples"}, indent=2))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

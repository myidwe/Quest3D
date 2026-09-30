"""Bounded CUDA resize/stereo comparison; synthetic RGB/depth, no capture or IPC.

This checks the actual production kernels and packing, not AI quality, encoder,
network, Quest optics, or sustainable FPS. Concurrent desktop load is retained.
"""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import time

import numpy as np
import torch

from quest3d.depth import DepthResult
from quest3d.stereo import StereoSynthesizer


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    output = args.output.resolve()
    output.relative_to((root / "artifacts").resolve())
    output.mkdir(parents=True, exist_ok=False)
    torch.set_num_threads(4)
    report = {"started_at": datetime.now().astimezone().isoformat(), "passed": False,
              "synthetic_rgb_and_depth": True, "actual_ai": False, "capture": False,
              "ipc_published": False, "quest_tested": False, "concurrent_live_load": True,
              "source": [2560, 1440], "eye": [1920, 1080], "samples_per_filter": 8,
              "source_sha256": hashlib.sha256((root / "src/quest3d/stereo.py").read_bytes()).hexdigest(),
              "profiles": {}, "rows": []}
    try:
        y, x = np.indices((1440, 2560))
        source = np.stack(((x * 3 + y) % 256, (x + y * 2) % 256,
                           (x * 7 + y * 5) % 256, np.full_like(x, 255)), axis=-1).astype(np.uint8)
        gpu = torch.from_numpy(source).to("cuda")
        reference = gpu.clone()
        depth = DepthResult(1, 0, torch.linspace(0, 1, 280 * 504, device="cuda").reshape(1, 280, 504),
                            (280, 504), 0.0, 0.0)
        filters = {name: StereoSynthesizer(1920, 1080, disparity_px=27.6, resize_filter=name)
                   for name in ("area", "bicubic-aa")}
        torch.cuda.reset_peak_memory_stats()
        for name, synth in filters.items():
            cpu_fit, cpu_rect = synth._fit(torch.from_numpy(source))
            cuda_fit, rect = synth._fit(gpu)
            difference = np.abs(cuda_fit.cpu().numpy().astype(np.int16) - cpu_fit.numpy().astype(np.int16))
            assert rect == cpu_rect == (0, 0, 1920, 1080)
            assert int(difference.max()) <= 1, "CUDA resize diverges from CPU implementation"
            mono = synth.original_2d(gpu, frame_id=1, generation=0)
            assert np.array_equal(mono.bgra[:, :1920], mono.bgra[:, 1920:])
            assert np.all(mono.bgra[:, :, 3] == 255)
            report["profiles"][name] = {"cpu_cuda_max_component_error": int(difference.max())}
            synth.synthesize(gpu, depth, frame_id=1, generation=0)
        for iteration in range(8):
            order = list(filters) if iteration % 2 == 0 else list(reversed(filters))
            for name in order:
                synth = filters[name]
                torch.cuda.synchronize()
                start = time.perf_counter_ns()
                synth._fit(gpu)
                torch.cuda.synchronize()
                fit_ms = (time.perf_counter_ns() - start) / 1e6
                start = time.perf_counter_ns()
                stereo = synth.synthesize(gpu, depth, frame_id=1, generation=0)
                torch.cuda.synchronize()
                stereo_ms = (time.perf_counter_ns() - start) / 1e6
                assert stereo.bgra.shape == (1080, 3840, 4) and stereo.mode == "3d"
                assert stereo.content_rect == (0, 0, 1920, 1080)
                assert np.all(stereo.bgra[:, :, 3] == 255)
                assert not np.array_equal(stereo.bgra[:, :1920, :3], stereo.bgra[:, 1920:, :3])
                report["rows"].append(dict(iteration=iteration, filter=name, fit_ms=fit_ms,
                                           stereo_and_readback_ms=stereo_ms))
        assert torch.equal(gpu, reference), "Source pixels were modified"
        for name, profile in report["profiles"].items():
            for key in ("fit_ms", "stereo_and_readback_ms"):
                values = [row[key] for row in report["rows"] if row["filter"] == name]
                profile[key] = dict(p50=float(np.percentile(values, 50)),
                                    p95=float(np.percentile(values, 95)), maximum=max(values))
        report["cuda_peak_allocated_bytes"] = torch.cuda.max_memory_allocated()
        report["cuda_peak_reserved_bytes"] = torch.cuda.max_memory_reserved()
        report["gpu"] = torch.cuda.get_device_name()
        report["torch"] = torch.__version__
        report["passed"] = True
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        report["finished_at"] = datetime.now().astimezone().isoformat()
        (output / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps({key: report[key] for key in ("passed", "profiles", "started_at", "finished_at")}, indent=2))


if __name__ == "__main__":
    main()

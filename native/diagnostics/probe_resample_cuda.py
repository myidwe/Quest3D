"""Explicit isolated resize benchmark; requires the coordinator's GPU slot."""
import argparse
import hashlib
import json
from pathlib import Path
import time

import numpy as np
import torch

from quest3d.resample import ReferenceCubicResize
from quest3d.resample_cuda import CudaReferenceCubicResize


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    root = Path(__file__).resolve().parents[2]
    summary = {
        "scope": "actual GPU resize of fixed synthetic pixels only; no capture/model/IPC/Quest",
        "completion_contract": "both implementations followed by current stream synchronize; candidate also synchronizes internally",
        "event_scope": "events bracket complete callable, including host dispatch/synchronization gaps; not isolated kernel time",
        "torch": torch.__version__, "gpu": torch.cuda.get_device_name(),
        "source_sha256": {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
            for name in ("src/quest3d/resample.py", "src/quest3d/resample_cuda.py",
                         "src/quest3d/shaders/reference_cubic.cu",
                         "native/diagnostics/probe_resample_cuda.py")},
        "shapes": [],
    }
    reference = ReferenceCubicResize()
    stream = torch.cuda.current_stream()
    with CudaReferenceCubicResize() as candidate:
        summary["candidate"] = candidate.metadata
        for height, width in ((1440, 2560), (1080, 1920)):
            generator = torch.Generator(device="cuda").manual_seed(7203)
            backing = torch.randint(0, 256, (height, width, 4), dtype=torch.uint8,
                                    device="cuda", generator=generator)
            source = backing[:, :, :3]
            before = source.clone()
            expected = reference(source, 504, 280)
            actual = candidate(source, 504, 280)
            assert torch.equal(expected.view(torch.int32), actual.view(torch.int32))
            shape = {"source_size": [width, height], "source_stride": list(source.stride()),
                     "output_size": [504, 280], "exact_float_bits": True, "runs": []}
            for repetition in range(3):
                for name in (("reference", "candidate") if repetition % 2 == 0
                             else ("candidate", "reference")):
                    resize = candidate if name == "candidate" else reference
                    for _ in range(5):
                        resize(source, 504, 280)
                        stream.synchronize()
                    events = [(torch.cuda.Event(enable_timing=True),
                               torch.cuda.Event(enable_timing=True)) for _ in range(50)]
                    rows = []
                    for begin, end in events:
                        tick = time.perf_counter_ns()
                        begin.record(stream)
                        result = resize(source, 504, 280)
                        end.record(stream)
                        stream.synchronize()
                        rows.append({"wall_ms": (time.perf_counter_ns() - tick) / 1e6,
                                     "cuda_event_ms": begin.elapsed_time(end)})
                    assert torch.equal(result.view(torch.int32), expected.view(torch.int32))
                    shape["runs"].append({"implementation": name, "repetition": repetition,
                        "samples": rows, "timings_ms": {key: dict(zip(("p50", "p95", "max"),
                        map(float, np.percentile([row[key] for row in rows], [50, 95, 100]))))
                        for key in ("wall_ms", "cuda_event_ms")}})
            assert torch.equal(source, before)
            shape["source_unchanged"] = True
            summary["shapes"].append(shape)
            (args.output / "verification.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(args.output), "shapes": [
        {"source_size": shape["source_size"], "runs": [
            {key: value for key, value in run.items() if key != "samples"}
            for run in shape["runs"]]} for shape in summary["shapes"]]}, indent=2))


if __name__ == "__main__":
    main()

"""CPU-only 1080p-per-eye cursor cache comparison; no capture, CUDA or IPC.

The stationary cursor is a declared synthetic raster. This isolates full-frame
CPU copy/composition and does not measure Win32 shape queries or Quest FPS.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import time
from types import SimpleNamespace

import numpy as np

from quest3d.cursor import CursorSample, DesktopCursorOverlay
from quest3d.geometry import ScreenRect, SourceGeometry
from quest3d.source_identity import SourceIdentity, SourceKind
from quest3d.stereo import StereoFrame


class FixtureReader:
    def __init__(self, sample):
        self.current = sample
        self.calls = 0

    def sample(self):
        self.calls += 1
        return self.current

    def close(self):
        pass


def percentiles(values):
    return dict(zip(("p50", "p95", "max"), map(float, np.percentile(values, [50, 95, 100]))))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=100)
    args = parser.parse_args()
    if not 10 <= args.samples <= 1000:
        parser.error("--samples must be in 10..1000")
    args.output.mkdir(parents=True, exist_ok=False)
    bounds = ScreenRect(-2560, 0, 2560, 1440)
    image = np.empty((1080, 3840, 4), dtype=np.uint8)
    image[:, :, 0] = np.arange(3840, dtype=np.uint16)[None, :] % 256
    image[:, :, 1] = np.arange(1080, dtype=np.uint16)[:, None] % 256
    image[:, :, 2] = 51
    image[:, :, 3] = 255
    image.setflags(write=False)  # The fixture promises no mutation of any alias.
    base = StereoFrame(image, 1, 1, "3d", False, (.1, .9), (0, 0, 1920, 1080))
    source = SimpleNamespace(frame_id=1, geometry_generation=1, captured_ns=123456,
        source_id="synthetic-cpu-fixture", geometry=SourceGeometry(bounds, 1),
        source_identity=SourceIdentity(SourceKind.MONITOR, 0, 91, 99, 0, bounds))
    shape = np.zeros((48, 32, 4), dtype=np.uint8)
    for row in range(40):
        width = min(25, row // 2 + 1)
        shape[row, :width] = [255, 255, 255, 255]
        shape[row, 0] = [0, 0, 0, 255]
        shape[row, width - 1] = [0, 0, 0, 255]
    sample = CursorSample(True, (-1800, 500), (0, 0), shape)
    before = hashlib.sha256(image).hexdigest()
    baseline = DesktopCursorOverlay(reader=FixtureReader(sample))
    expected = baseline.sample_and_composite(base, source)
    comparison = []
    started = time.perf_counter_ns()
    for repetition in range(3):
        # Reverse the order in the middle repetition to expose gross warm-order bias.
        for caching in ((False, True) if repetition % 2 == 0 else (True, False)):
            overlay = DesktopCursorOverlay(reader=FixtureReader(sample), immutable_base=caching)
            previous = None
            for _ in range(5):
                previous = overlay.sample_and_composite(base, source)
            metrics = []
            for _ in range(args.samples):
                tick = time.perf_counter_ns()
                result = overlay.sample_and_composite(base, source)
                elapsed = (time.perf_counter_ns() - tick) / 1e6
                metrics.append({"total_ms": elapsed, **overlay.last_metrics})
                if caching:
                    assert result is previous and not result.bgra.flags.writeable
                    assert overlay.last_metrics["cursor_copied_bytes"] == 0
                previous = result
            np.testing.assert_array_equal(result.bgra, expected.bgra)
            comparison.append({"repetition": repetition, "immutable_base": caching,
                "samples": args.samples, "warmup": 5,
                "timings_ms": {key: percentiles([row[key] for row in metrics])
                    for key in ("total_ms", "cursor_read_ms", "cursor_plan_ms",
                                "cursor_copy_ms", "cursor_blend_ms")},
                "measured_copied_bytes": sum(row["cursor_copied_bytes"] for row in metrics),
                "final_status": overlay.snapshot()})
            overlay.close()
    # Change position on the same immutable image: no cursor trail or stale result.
    overlay = DesktopCursorOverlay(reader=FixtureReader(sample), immutable_base=True)
    previous = overlay.sample_and_composite(base, source)
    saved = previous.bgra.copy()
    for x in range(-1790, -1670, 10):
        overlay.reader.current = replace(sample, position=(x, 550))
        baseline.reader.current = overlay.reader.current
        result = overlay.sample_and_composite(base, source)
        np.testing.assert_array_equal(result.bgra, baseline.sample_and_composite(base, source).bgra)
        assert not overlay.last_metrics["cursor_cache_hit"]
        np.testing.assert_array_equal(previous.bgra, saved)
    overlay.close()
    baseline.close()
    assert hashlib.sha256(image).hexdigest() == before
    root = Path(__file__).resolve().parents[2]
    summary = {"scope": "CPU synthetic raster/base only; no Win32 sampling, capture, CUDA, IPC or Quest",
        "packed_size": [3840, 1080], "bytes_per_frame": image.nbytes,
        "immutable_caller_contract": True, "pixel_parity": True,
        "movement_cases": 12, "base_unchanged": True, "gpu_executed": False,
        "duration_seconds": (time.perf_counter_ns() - started) / 1e9,
        "source_sha256": hashlib.sha256((root / "src/quest3d/cursor.py").read_bytes()).hexdigest(),
        "probe_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "runs": comparison}
    (args.output / "verification.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()

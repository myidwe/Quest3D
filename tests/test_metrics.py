import json

import numpy as np

from quest3d.metrics import Metrics


def test_disk_metrics_keep_exact_percentiles_without_retaining_frame_rows(tmp_path):
    metrics = Metrics(tmp_path)
    values = [((i * 71) % 1024) / 10 for i in range(1024)]
    metrics.add({"warmup": True, "completed_ns": 0, "pc_total_ms": 99999})
    for i, value in enumerate(values):
        metrics.add({"completed_ns": (i + 1) * 40_000_000, "pc_total_ms": value})
    assert not hasattr(metrics, "rows")
    summary = metrics.finish({"test": True})
    assert summary["frames_total"] == 1025
    assert summary["frames_measured"] == 1024
    assert summary["unique_processing_fps"] == 25
    np.testing.assert_allclose(list(summary["timings_ms"]["pc_total_ms"].values()),
                               np.percentile(values, [50, 95, 99]), rtol=0, atol=0)
    assert sum(1 for _ in (tmp_path / "frames.jsonl").open()) == 1025
    assert json.loads((tmp_path / "summary.json").read_text())["test"] is True


def test_no_measured_frames_still_produces_failure_diagnostics(tmp_path):
    metrics = Metrics(tmp_path)
    summary = metrics.finish({"error": "model initialization failed"})
    assert summary["frames_measured"] == 0
    assert summary["timings_ms"] == {}
    assert summary["error"] == "model initialization failed"

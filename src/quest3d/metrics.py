import json
from pathlib import Path

import numpy as np


class Metrics:
    TIMINGS = ("capture_ms", "capture_color_processing_ms", "upload_ms", "preprocess_ms", "inference_ms", "stereo_readback_ms", "cursor_overlay_ms",
               "encode_ms", "publish_ms", "pc_total_ms", "source_age_at_completion_ms")

    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.directory = directory
        self.output = (directory / "frames.jsonl").open("w", encoding="utf-8")
        self.timing_path = directory / "timings.f64"
        self.timing_file = self.timing_path.open("wb")
        self.frames_total = self.frames_measured = 0
        self.first_completed = self.last_completed = None

    def add(self, row: dict):
        self.output.write(json.dumps(row, ensure_ascii=False) + "\n")
        self.output.flush()
        self.frames_total += 1
        if not row.get("warmup", False):
            self.timing_file.write(np.asarray([row.get(key, np.nan) for key in self.TIMINGS],
                                             dtype="<f8").tobytes())
            self.frames_measured += 1
            if self.first_completed is None:
                self.first_completed = row["completed_ns"]
            self.last_completed = row["completed_ns"]

    def finish(self, metadata: dict) -> dict:
        self.output.close()
        self.timing_file.close()
        timings = {}
        if self.frames_measured:
            samples = np.memmap(self.timing_path, dtype="<f8", mode="r",
                                shape=(self.frames_measured, len(self.TIMINGS)))
            try:
                for column, key in enumerate(self.TIMINGS):
                    values = samples[:, column]
                    if np.isfinite(values).any():
                        timings[key] = dict(zip(("p50", "p95", "p99"),
                                               map(float, np.nanpercentile(values, [50, 95, 99]))))
            finally:
                del values
                del samples
        result = {**metadata, "frames_total": self.frames_total, "frames_measured": self.frames_measured,
                  "timings_ms": timings, "timing_columns": list(self.TIMINGS), "timing_dtype": "<f8",
                  "quest_display_verified": False, "audio_verified": False}
        if self.frames_measured > 1:
            elapsed = (self.last_completed - self.first_completed) / 1e9
            result["unique_processing_fps"] = (self.frames_measured - 1) / elapsed if elapsed else 0
        (self.directory / "summary.json").write_text(json.dumps(result, indent=2), "utf-8")
        return result

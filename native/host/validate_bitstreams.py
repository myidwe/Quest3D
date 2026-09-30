"""Decode the actual Sunshine bridge/NVENC test outputs using the pinned PyAV runtime."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import av
import numpy as np


def inspect_codec(directory: Path, codec: str, width: int, height: int) -> dict:
    stem = "quest3d-h264" if codec == "h264" else "quest3d-hevc"
    bitstream = directory / f"{stem}.{codec}"
    decoded = 0
    eye_differences = []
    image_deviations = []
    with av.open(str(bitstream), format=codec) as container:
        for frame in container.decode(video=0):
            if (frame.width, frame.height) != (width, height):
                raise AssertionError(f"Unexpected decoded size: {frame.width}x{frame.height}")
            rgb = frame.to_ndarray(format="rgb24")
            image_deviations.append(float(rgb.std()))
            if width % 2 == 0:
                left, right = np.split(rgb.astype(np.float32), 2, axis=1)
                eye_differences.append(float(np.abs(left - right).mean()))
            decoded += 1
    if decoded != 24:
        raise AssertionError(f"{codec}: expected 24 decoded frames, got {decoded}")
    with (directory / f"{stem}.csv").open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    if len(rows) != decoded:
        raise AssertionError(f"{codec}: metrics/decoded frame count mismatch")
    timings = {}
    for name in ("source_age_ms", "snapshot_wait_copy_upload_ms", "convert_ms", "encode_ms"):
        samples = np.array([float(row[name]) for row in rows])
        timings[name] = {"p50": float(np.median(samples)), "p95": float(np.percentile(samples, 95)), "max": float(samples.max())}
    return {
        "codec": codec,
        "size": [width, height],
        "decoded_frames": decoded,
        "bytes": bitstream.stat().st_size,
        "image_std_min": min(image_deviations),
        "left_right_rgb_mae_mean": float(np.mean(eye_differences)),
        "timings_ms": timings,
        "scope": "PC SourceProvider/D3D/NVENC/CPU decode; no network, audio, or Quest display validation",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--width", type=int, required=True)
    parser.add_argument("--height", type=int, required=True)
    args = parser.parse_args()
    report = {
        "results": [inspect_codec(args.directory, codec, args.width, args.height) for codec in ("h264", "hevc")],
        "pixel_metric_note": "Nonzero eye difference describes encoded pixels; it does not prove comfortable stereo or correct Quest eye assignment.",
    }
    text = json.dumps(report, indent=2, ensure_ascii=False)
    (args.directory / "decode-validation.json").write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()

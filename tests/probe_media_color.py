"""Record a CPU-only actual file color verification in a fresh output directory."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

import av
import numpy as np

from quest3d.media import MediaReader

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_media_color import expected_rgb, write_clip


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    rows = []
    for matrix in (1, 5, 6):
        for color_range in (1, 2):
            path = args.output / f"color-{matrix}-{color_range}.mkv"
            yuv = write_clip(path, matrix, color_range)
            reference = expected_rgb(yuv, matrix, color_range)
            with av.open(str(path)) as container:
                baseline = next(container.decode(video=0)).to_ndarray(format="bgra")
            reader = MediaReader(path)
            try:
                current, pts = reader.next()
                policy = reader.color_info
                second, second_pts = reader.next()
                assert second_pts > pts and np.array_equal(current, second)
            finally:
                reader.close()
            old_error = np.abs(baseline[:, :, [2, 1, 0]].astype(float) - reference)
            new_error = np.abs(current[:, :, [2, 1, 0]].astype(float) - reference)
            assert float(new_error.max()) <= 2
            rows.append({"matrix": matrix, "range": color_range,
                "clip_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "pixels_sha256": hashlib.sha256(current.tobytes()).hexdigest(),
                "default_mae": float(old_error.mean()), "fixed_mae": float(new_error.mean()),
                "fixed_max_abs_error": float(new_error.max()), "policy": policy})
    root = Path(__file__).resolve().parents[1]
    result = {"scope": "CPU synthetic raw YUV -> actual lossless FFV1 file -> MediaReader BGRA",
        "gpu_executed": False, "quest_verified": False, "av_version": av.__version__,
        "library_versions": av.library_versions, "rows": rows,
        "media_py_sha256": hashlib.sha256((root / "src/quest3d/media.py").read_bytes()).hexdigest(),
        "tests_sha256": hashlib.sha256((root / "tests/test_media_color.py").read_bytes()).hexdigest()}
    (args.output / "verification.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps([{key: row[key] for key in ("matrix", "range", "default_mae", "fixed_mae", "fixed_max_abs_error")} for row in rows], indent=2))


if __name__ == "__main__":
    main()

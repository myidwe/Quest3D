"""CPU-only reinspection: compare PyAV's default RGB conversion with explicit stream metadata."""
import argparse
import json
from pathlib import Path

import av
from av.video.reformatter import Colorspace, ColorRange
import numpy as np
from PIL import Image

from probe_color_pipeline import measure, pattern, payload_sha, sha


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    args = parser.parse_args()
    source = args.input.resolve()
    output = source / "explicit-rec709-decode.json"
    assert not output.exists(), "Preserve earlier diagnostic evidence"
    reference = np.asarray(Image.open(source / "tone-map-eye.png").convert("RGB"))
    _, tiles = pattern()
    result = {"gpu_executed": False, "new_encoding": False, "quest_verified": False,
              "reference_verification_sha256": sha(source / "verification.json"), "codecs": []}
    for codec in ("h264", "hevc"):
        path = source / ("quest3d-" + codec + "." + codec)
        frames = []
        with av.open(str(path), format=codec) as container:
            for index, frame in enumerate(container.decode(video=0)):
                # Read actual bitstream metadata, rather than assuming the matrix.
                assert int(frame.colorspace) == int(Colorspace.ITU709) == 1
                assert int(frame.color_range) == int(ColorRange.MPEG) == 1
                default = frame.to_ndarray(format="rgb24")
                explicit = frame.reformat(format="rgb24", src_colorspace=Colorspace.ITU709,
                    dst_colorspace=Colorspace.ITU709, src_color_range=ColorRange.MPEG,
                    dst_color_range=ColorRange.JPEG).to_ndarray()
                values = measure(explicit[:, :640], tiles)
                neutral = [v for v in values if v["neutral"]]
                frames.append({"index": index+1, "colorspace": int(frame.colorspace), "color_range": int(frame.color_range),
                    "explicit_rec709_rgb_sha256": payload_sha(explicit),
                    "default_rgb_mae": float(np.abs(default[:, :640].astype(np.int16)-reference.astype(np.int16)).mean()),
                    "explicit_rec709_rgb_mae": float(np.abs(explicit[:, :640].astype(np.int16)-reference.astype(np.int16)).mean()),
                    "max_neutral_abs_mean_r_minus_g": max(abs(v["mean_r_minus_g"]) for v in neutral),
                    "max_neutral_abs_mean_b_minus_g": max(abs(v["mean_b_minus_g"]) for v in neutral),
                    "tiles": values})
                if index == 0:
                    Image.fromarray(explicit[:, :640]).save(source / (codec + "-explicit-rec709-eye.png"))
        assert len(frames) == 4
        result["codecs"].append({"codec": codec, "bitstream_sha256": sha(path), "frames": frames})
    output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    for codec in result["codecs"]:
        frames = codec["frames"]
        print(json.dumps({"codec": codec["codec"], "frames": len(frames),
            "default_mae": max(f["default_rgb_mae"] for f in frames),
            "explicit_rec709_mae": max(f["explicit_rec709_rgb_mae"] for f in frames),
            "neutral_rg": max(f["max_neutral_abs_mean_r_minus_g"] for f in frames),
            "neutral_bg": max(f["max_neutral_abs_mean_b_minus_g"] for f in frames)}))


if __name__ == "__main__":
    main()

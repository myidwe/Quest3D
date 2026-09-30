"""CPU-only audit of preserved offline comparison packets and Annex-B NAL bytes."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
START_CODE = re.compile(b"\x00\x00(?:\x00)?\x01")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def inspect_annex_b(data, codec):
    markers = list(START_CODE.finditer(data))
    if not markers:
        raise ValueError("No Annex-B start codes")
    nal_counts, nal_bytes, filler_payload_valid = {}, {}, True
    filler = 12 if codec == "h264" else 38
    for index, marker in enumerate(markers):
        end = markers[index + 1].start() if index + 1 < len(markers) else len(data)
        body = data[marker.end():end]
        if len(body) < (1 if codec == "h264" else 2):
            raise ValueError("Empty or truncated NAL header")
        kind = (body[0] & 31) if codec == "h264" else ((body[0] >> 1) & 63)
        nal_counts[kind] = nal_counts.get(kind, 0) + 1
        nal_bytes[kind] = nal_bytes.get(kind, 0) + end - marker.start()
        if kind == filler:
            payload = body[1 if codec == "h264" else 2:]
            filler_payload_valid &= bool(payload) and payload[-1] == 0x80 and all(value == 0xFF for value in payload[:-1])
    filler_bytes = nal_bytes.get(filler, 0)
    return {"nal_counts": nal_counts, "nal_bytes_including_start_codes": nal_bytes,
            "unassigned_prefix_bytes": markers[0].start(), "filler_nal_type": filler,
            "filler_bytes_including_start_codes": filler_bytes,
            "filler_fraction": filler_bytes / len(data),
            "filler_rbsp_ff_then_stop_bit_verified": filler_payload_valid if filler_bytes else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--comparison", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists() or not output.is_relative_to(ROOT / "artifacts"):
        raise ValueError("Choose a new output directory under workspace artifacts")
    source = args.comparison.resolve()
    raw_result = (source / "result.json").read_bytes()
    result = json.loads(raw_result)
    if result["status"] != "measured_offline":
        raise ValueError("Only a completed comparison is eligible for this audit")
    reviewed = {"scope": "CPU read of preserved bitstreams/metrics; no re-encode/GPU/network/device",
                "comparison_result": str(source / "result.json"), "comparison_result_sha256": sha(raw_result),
                "comparison_harness_sha256": result["script_sha256"],
                "inspector_sha256": sha(Path(__file__).read_bytes()),
                "fps_nominal": result["fps"], "ordered_input_slots": len(result["sequence"]["frames"]),
                "capture_timestamp_span_seconds": result["sequence"]["capture_timestamp_span_seconds"],
                "common_yuv_sha256": result["preparation"]["sha256"],
                "common_rgb_420_roundtrip_full": result["preparation"]["rgb_to_420_roundtrip"]["full"],
                "cases": []}
    for case in result["cases"]:
        encoded, decoded = case["encoded"], case["decoded"]
        path = source / case["name"] / ("stream." + encoded["codec"])
        data = path.read_bytes()
        if sha(data) != encoded["bitstream_sha256"] or len(data) != encoded["bitstream_bytes"]:
            raise ValueError("Saved bitstream differs from original benchmark attestation")
        nals = inspect_annex_b(data, encoded["codec"])
        if nals["unassigned_prefix_bytes"] + sum(nals["nal_bytes_including_start_codes"].values()) != len(data):
            raise ValueError("NAL accounting does not cover the saved file")
        duration = encoded["nominal_duration_seconds"]
        later = decoded["frames"][1:]
        mse = sum(row["codec_only"]["full"]["rgb_mse"] for row in later) / len(later) if later else None
        reviewed["cases"].append({"name": case["name"], "bitstream": str(path), "sha256": sha(data),
            "bytes": len(data), "nominal_duration_seconds": duration, **nals,
            "full_elementary_mbps": len(data) * 8 / duration / 1e6,
            "non_filler_elementary_mbps": (len(data) - nals["filler_bytes_including_start_codes"]) * 8 / duration / 1e6,
            "non_filler_scope": "Includes VPS/SPS/PPS/SEI and all coded video; not just video payload and not network overhead",
            "codec_only": decoded["codec_only"], "source_rgb": decoded["source_rgb"],
            "first_frame_codec_rgb_psnr_db": decoded["frames"][0]["codec_only"]["full"]["rgb_psnr_db"],
            "frames_after_first_codec_rgb_psnr_db": 10 * math.log10(255 ** 2 / mse) if mse else None,
            "frames_after_first_scope": "Additional descriptive subset, not a new benchmark or steady-state proof",
            "encode_call_ms": encoded["encode_call_ms"], "encoder_open_ms": encoded["open_ms"],
            "decoder": decoded["decoder"]})
    output.mkdir(parents=True, exist_ok=False)
    with (output / "review.json").open("x", encoding="utf-8") as stream:
        json.dump(reviewed, stream, indent=2, allow_nan=False)
        stream.write("\n")
    print(json.dumps([{k: c[k] for k in ("name", "filler_bytes_including_start_codes", "filler_fraction",
                      "full_elementary_mbps", "non_filler_elementary_mbps")} for c in reviewed["cases"]], indent=2))


if __name__ == "__main__":
    main()

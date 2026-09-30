"""Verify actual GPU StereoSynthesizer colour options on one preserved AI pair.

This runs real synthesize(), strict projection, Full-SBS packing and pinned CPU
readback. Timing excludes capture, AI inference, bridge, encoding and Quest and
is not a live/sustainable FPS measurement. All DepthResults are explicitly bound
copies of SAVED REAL AI output for the unchanged saved RGB, never fresh inference.
Only a new artifact directory is written; no live process or settings change.
"""

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import time
import traceback

import numpy as np
from PIL import Image
import torch

from quest3d.depth import DepthResult
from quest3d.stereo import StereoSynthesizer


ROOT = Path(__file__).resolve().parents[2]
SCENE = ROOT / "artifacts/diagnostics/target-scene-20260910"
SOURCE, DEPTH = SCENE / "source-gdi.png", SCENE / "replay-280-a/depth.npy"
SOURCE_SHA = "ebbf586ee710ddc09aa658289577e427bbcb5f66acf34ebac2b555f9f2672117"
DEPTH_SHA = "6ffb76144c602369e38c53b03311a0ef26b0e9bf4bff38b073ef170f3954ae1b"
MASK_PROOF = ROOT / "artifacts/diagnostics/colour-fit-gpu-20260910-a/report.json"
CODE = ("src/quest3d/stereo.py", "src/quest3d/colour_fit.py", "src/quest3d/depth.py",
        "src/quest3d/forward_warp.py", "src/quest3d/forward_warp_cuda.py", "src/quest3d/shaders/forward_warp.cu")
_FAILED_CLOSE_SYNTHS = []


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def stamp():
    return datetime.now().astimezone().isoformat()


def tensor_sha(value):
    return hashlib.sha256(value.detach().cpu().contiguous().numpy().tobytes()).hexdigest()


def meta(frame):
    return {key: getattr(frame, key) for key in ("frame_id", "generation", "mode", "scene_reset",
        "depth_range", "content_rect", "depth_refinement", "depth_refinement_reason",
        "colour_precision", "colour_precision_reason")}


def assert_stereo(frame, precision, *, frame_id=7, generation=3):
    assert frame.bgra.shape == (1080, 3840, 4) and frame.bgra.dtype == np.uint8
    assert frame.bgra.flags.c_contiguous and np.all(frame.bgra[:, :, 3] == 255)
    assert frame.frame_id == frame_id and frame.generation == generation and frame.mode == "3d"
    assert frame.content_rect == (0, 0, 1920, 1080)
    assert frame.colour_precision == precision and frame.colour_precision_reason is None
    assert frame.depth_refinement == "none" and frame.depth_refinement_reason is None
    backing = frame.bgra.base
    assert isinstance(backing, torch.Tensor) and backing.device.type == "cpu" and backing.is_pinned()


def delta_summary(old, new):
    diff = np.abs(new[:, :, :3].astype(np.int16) - old[:, :, :3].astype(np.int16))
    regions = {"both": diff, "left": diff[:, :1920], "right": diff[:, 1920:],
               "left_rail": diff[510:660, 970:1170], "right_rail": diff[510:660, 2890:3090]}
    return {name: {"max_abs_lsb": int(value.max()), "mean_abs_lsb": float(value.mean()),
                   "changed_pixel_fraction": float(np.any(value != 0, axis=-1).mean())}
            for name, value in regions.items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--device", type=int, default=0)
    parser.add_argument("--repeats", type=int, default=10)
    args = parser.parse_args()
    if not 1 <= args.repeats <= 30:
        raise ValueError("repeats must be 1..30")
    output = args.output.resolve()
    output.relative_to(ROOT / "artifacts")
    output.mkdir(parents=True, exist_ok=False)
    paths = [Path(__file__).resolve(), SOURCE, DEPTH, DEPTH.parent / "replay.json", MASK_PROOF,
             *(ROOT / name for name in CODE)]
    hashes = lambda: {str(path.relative_to(ROOT)): sha(path) for path in paths}
    report = {"status": "RUNNING", "started_at": stamp(), "pid": os.getpid(), "scope": __doc__,
              "disparities": [13.2, 22.8], "samples": [], "cases": {}, "close": {}, "errors": [],
              "warmup_each": 3, "measured_each": args.repeats}
    synths = {}
    try:
        report["hash_before"] = hashes()
        assert sha(SOURCE) == SOURCE_SHA and sha(DEPTH) == DEPTH_SHA, "Saved pair changed"
        provenance = json.loads((DEPTH.parent / "replay.json").read_text(encoding="utf-8"))
        assert provenance["source_sha256"] == SOURCE_SHA and provenance["raw_depth_shape"] == [1, 280, 504]
        previous = json.loads(MASK_PROOF.read_text(encoding="utf-8"))
        assert previous["status"] == "PASS" and all(previous["geometry_exact"].values())
        previous_hash = {key.replace("\\", "/"): value for key, value in previous["hash_after"].items()}
        for name in CODE[-3:]:
            assert sha(ROOT / name) == previous_hash[name], "Strict renderer differs from prior mask proof"
        report["prior_mask_proof"] = {"path": str(MASK_PROOF), "sha256": sha(MASK_PROOF),
            "disparity": previous["disparity"], "geometry_exact": previous["geometry_exact"],
            "scope": "Prior direct-renderer raw coverage/masks proof at its recorded disparity; this script does not expose new masks"}
        report["depth_binding"] = {"saved_real_ai": True, "fresh_inference": False,
            "source_sha256": SOURCE_SHA, "depth_sha256": DEPTH_SHA, "ai_size": 280,
            "second_frame": "independent byte-identical RGB/depth copies, new frame ID/generation; no changed RGB with stale depth"}
        rgb = np.array(Image.open(SOURCE).convert("RGB"), copy=True)
        raw = np.load(DEPTH, allow_pickle=False)
        assert rgb.shape == (1440, 2560, 3) and raw.shape == (1, 280, 504) and raw.dtype == np.float32
        assert np.isfinite(raw).all()
        bgra = np.concatenate((rgb[:, :, ::-1], np.full((*rgb.shape[:2], 1), 255, np.uint8)), axis=-1)
        torch.set_num_threads(4)
        with torch.cuda.device(args.device), torch.inference_mode():
            source = torch.from_numpy(bgra).to(device=args.device)
            depth_tensor = torch.from_numpy(raw).to(device=args.device)
            depth = DepthResult(7, 3, depth_tensor, (280, 504), 0., 0.)
            report["input_hash_before"] = {"bgra": tensor_sha(source), "raw_depth": tensor_sha(depth_tensor)}
            names = ("default", "uint8", "float")
            for disparity in (13.2, 22.8):
                key = str(disparity)
                group = {}
                for name in names:
                    kwargs = {} if name == "default" else {"colour_precision": name}
                    group[name] = StereoSynthesizer(1920, 1080, disparity_px=disparity,
                        resize_filter="bicubic-aa", stereo_method="forward-cuda", depth_refinement="none", **kwargs)
                    synths[f"{key}:{name}"] = group[name]
                results = {name: synth.synthesize(source, depth, frame_id=7, generation=3) for name, synth in group.items()}
                for name, frame in results.items():
                    assert_stereo(frame, "float" if name == "float" else "uint8")
                    assert frame.scene_reset, "Each fresh synth must start a fresh scene"
                np.testing.assert_array_equal(results["default"].bgra, results["uint8"].bgra)
                assert results["uint8"].depth_range == results["float"].depth_range
                case = {"metadata": {name: meta(frame) for name, frame in results.items()},
                        "default_uint8_exact": True, "pinned_contiguous_readback": True,
                        "difference": delta_summary(results["uint8"].bgra, results["float"].bgra)}
                report["cases"][key] = case
                for name, frame in results.items():
                    for eye, side in enumerate(("left", "right")):
                        pixels = frame.bgra[:, eye*1920:(eye+1)*1920, :3][:, :, ::-1]
                        Image.fromarray(pixels).save(output / f"D{key}-{name}-{side}.png")
                held = results["float"]
                held_bytes = held.bgra.copy()

                def invoke(name):
                    start_event, end_event = torch.cuda.Event(enable_timing=True), torch.cuda.Event(enable_timing=True)
                    started = time.perf_counter_ns()
                    start_event.record()
                    frame = group[name].synthesize(source, depth, frame_id=7, generation=3)
                    end_event.record()
                    end_event.synchronize()
                    completed = time.perf_counter_ns()
                    # These assertions run outside the measured interval.
                    assert_stereo(frame, "float" if name == "float" else "uint8")
                    return {"disparity": disparity, "version": name, "start_qpc_ns": started,
                            "completed_qpc_ns": completed, "wall_ms": (completed-started)/1e6,
                            "cuda_stream_span_ms": start_event.elapsed_time(end_event)}

                for repeat in range(3):
                    for name in names[repeat:] + names[:repeat]:
                        invoke(name)
                case["timing_started_at"] = stamp()
                for repeat in range(args.repeats):
                    offset = repeat % 3
                    order = names[offset:] + names[:offset]
                    if repeat % 2:
                        order = order[::-1]
                    for position, name in enumerate(order):
                        report["samples"].append({"round": repeat, "position": position, **invoke(name)})
                case["timing_finished_at"] = stamp()
                case["timing"] = {name: {metric: dict(zip(("p50", "p95"), np.percentile(
                    [s[metric] for s in report["samples"] if s["version"] == name and s["disparity"] == disparity],
                    [50, 95]).tolist())) for metric in ("wall_ms", "cuda_stream_span_ms")} for name in names}
                np.testing.assert_array_equal(held.bgra, held_bytes)

                originals = {name: synth.original_2d(source, frame_id=7, generation=3) for name, synth in group.items()}
                for frame in originals.values():
                    np.testing.assert_array_equal(frame.bgra, originals["uint8"].bgra)
                    assert frame.colour_precision == "uint8" and frame.mode == "2d"
                assert originals["float"].colour_precision_reason == "original_2d"
                floating = group["float"]
                floating.disparity_px = 0
                zero = floating.synthesize(source, depth, frame_id=7, generation=3)
                np.testing.assert_array_equal(zero.bgra, originals["uint8"].bgra)
                assert zero.colour_precision == "uint8" and zero.colour_precision_reason == "original_2d"
                rejected = 0
                for test_disparity in (0, disparity):
                    floating.disparity_px = test_disparity
                    for wrong_id, wrong_generation in ((8, 3), (7, 4)):
                        bad = DepthResult(wrong_id, wrong_generation, depth_tensor, (280, 504), 0., 0.)
                        try:
                            floating.synthesize(source, bad, frame_id=7, generation=3)
                        except ValueError as exc:
                            assert "different RGB frame" in str(exc)
                            rejected += 1
                        else:
                            raise AssertionError("Mismatched depth was accepted")
                source2, depth2 = source.clone(), depth_tensor.clone()
                second = floating.synthesize(source2, DepthResult(8, 4, depth2, (280, 504), 0., 0.), frame_id=8, generation=4)
                assert_stereo(second, "float", frame_id=8, generation=4)
                assert second.scene_reset and not np.shares_memory(held.bgra, second.bgra)
                second.bgra[0, 0, 0] ^= 1  # Alter only this owned result, after its GPU completion.
                np.testing.assert_array_equal(held.bgra, held_bytes)
                assert tensor_sha(source2) == report["input_hash_before"]["bgra"]
                assert tensor_sha(depth2) == report["input_hash_before"]["raw_depth"]
                floating.disparity_px = 0
                after_2d = floating.synthesize(source, depth, frame_id=7, generation=3)
                np.testing.assert_array_equal(after_2d.bgra, originals["uint8"].bgra)
                np.testing.assert_array_equal(held.bgra, held_bytes)
                case.update(two_d_exact=True, mismatch_rejections=rejected,
                    held_output_survives_repeats_and_next_frame=True, distinct_pinned_result_storage=True,
                    second_output_mutation_does_not_change_first_or_inputs=True)
            report["input_hash_after"] = {"bgra": tensor_sha(source), "raw_depth": tensor_sha(depth_tensor)}
            assert report["input_hash_before"] == report["input_hash_after"]
        report["status"] = "PASS"
    except BaseException:
        report["status"] = "FAIL"
        report["errors"].append(traceback.format_exc())
    finally:
        for name, synth in synths.items():
            try:
                synth.close()
                report["close"][name] = "completed"
            except BaseException:
                _FAILED_CLOSE_SYNTHS.append(synth)
                report["status"] = "FAIL"
                report["errors"].append(f"{name} cleanup: {traceback.format_exc()}")
        try:
            report["hash_after"] = hashes()
            report["all_files_stable"] = report.get("hash_before") == report["hash_after"]
            if not report["all_files_stable"]:
                raise RuntimeError("Code or preserved evidence changed during verification")
        except BaseException:
            report["status"] = "FAIL"
            report["errors"].append(traceback.format_exc())
        report["finished_at"] = stamp()
        (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps({key: value for key, value in report.items() if key != "samples"}, indent=2))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

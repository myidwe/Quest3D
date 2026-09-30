"""Real saved-AI StereoSynthesizer comfort A/B/A; no capture/model/live changes.

Full synth/pack/pinned readback timing excludes AI, capture, encoder and Quest.
Fresh replay uses saved min/max, not live smoothed bounds. No mask is fabricated.
"""

import argparse
from datetime import datetime
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parents[2]
CASE = ROOT / "artifacts/diagnostics/distance-contours-20260910-a/source-case"
BEFORE = ROOT / "artifacts/diagnostics/comfort-profile-20260910-a/before-core/stereo.py"
PIN = {"capture.json": "bd9c492d4c75e9f9169ae4cbcf8d35fd118321973d60707f41d9bdc7e72fd925",
       "source-bgra.npy": "66b26bc42e4d30e6a52b82623eff0ba9e5b65f067e2121c9ab6b59d3add9ce5c",
       "depth-280.npy": "60bbd113664c96bb5d1afbd0b0399031fd57c27e7c97bc6c36cb6bce57febc15"}
BEFORE_SHA = "48a3004e38c9c82e838b5444a0f2a5c4f6339cf2792ed1f4bead90eb3e39d931"
_FAILED_CLOSE_SYNTHS = []


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.relative_to((ROOT / "artifacts").resolve())
    output.mkdir(parents=True, exist_ok=False)
    import numpy as np
    from PIL import Image
    import torch
    import quest3d.stereo as current
    import quest3d.forward_warp_cuda as projector
    from quest3d.depth import DepthResult

    stamp = lambda: datetime.now().astimezone().isoformat()
    report = {"status": "RUNNING", "started_at": stamp(), "scope": __doc__, "errors": [],
              "samples": [], "warmup_each": 3, "measured_each": 10, "close": {}}
    paths = [Path(__file__).resolve(), BEFORE, *(CASE / name for name in PIN), CASE / "stereo-sbs.png",
             *(ROOT / "src/quest3d" / name for name in ("stereo.py", "colour_fit.py", "depth.py",
                 "disparity_mapping.py", "forward_warp.py", "forward_warp_cuda.py", "shaders/forward_warp.cu"))]
    hashes = lambda: {str(p.relative_to(ROOT)): sha(p) for p in paths}
    synths = {}
    try:
        assert all(sha(CASE / name) == expected for name, expected in PIN.items())
        assert sha(BEFORE) == BEFORE_SHA
        evidence = json.loads((CASE / "capture.json").read_text(encoding="utf-8"))
        assert evidence["status"] == "PASS" and evidence["same_frame_generation"] and evidence["source_unchanged"]
        assert sha(CASE / "stereo-sbs.png") == evidence["files"]["stereo-sbs.png"]
        for name in ("src/quest3d/forward_warp.py", "src/quest3d/forward_warp_cuda.py", "src/quest3d/shaders/forward_warp.cu"):
            original_hashes = {k.replace("\\", "/"): v for k, v in evidence["code_after"].items()}
            assert sha(ROOT / name) == original_hashes[name], "Saved scene projector changed"
        spec = importlib.util.spec_from_file_location("quest3d._comfort_before_stereo", BEFORE)
        old = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = old
        spec.loader.exec_module(old)
        report["hash_before"] = hashes()
        bgra, raw = (np.load(CASE / name, allow_pickle=False) for name in ("source-bgra.npy", "depth-280.npy"))
        assert bgra.shape == (1440, 2560, 4) and bgra.dtype == np.uint8
        assert raw.shape == (1, 280, 504) and raw.dtype == np.float32 and np.isfinite(raw).all()
        state, capture = evidence["status_before"], evidence["capture"]
        fid, gen = capture["frame_id"], capture["generation"]
        width, height, disparity = state["eye_width"], state["eye_height"], state["disparity"]
        report["saved_real_ai_binding"] = {"frame_id": fid, "generation": gen, "source_sha256": PIN["source-bgra.npy"],
            "raw_ai_sha256": PIN["depth-280.npy"], "fresh_inference": False, "disparity_px": disparity,
            "normalization": "Fresh min/max from this saved real AI; not exact live smoothed bounds"}
        torch.set_num_threads(4)
        with torch.inference_mode():
            source, raw_gpu = torch.from_numpy(bgra).cuda(), torch.from_numpy(raw).cuda()
            depth = DepthResult(fid, gen, raw_gpu, tuple(evidence["depth"]["input_shape"]), 0., 0.)
            common = dict(eye_width=width, eye_height=height, disparity_px=disparity,
                convergence=evidence["stereo"]["convergence"], resize_filter="bicubic-aa",
                stereo_method="forward-cuda", depth_refinement="none", colour_precision="float")
            synths["before"] = old.StereoSynthesizer(**common)
            for name, profile in (("linear_a", "linear"), ("comfort", "comfort"), ("linear_b", "linear")):
                synths[name] = current.StereoSynthesizer(**common, disparity_profile=profile)

            def run(name):
                return synths[name].synthesize(source, depth, frame_id=fid, generation=gen)

            def check(frame, profile=None):
                assert frame.bgra.shape == (height, width * 2, 4) and frame.bgra.dtype == np.uint8
                assert frame.bgra.flags.c_contiguous and np.all(frame.bgra[:, :, 3] == 255)
                assert frame.frame_id == fid and frame.generation == gen and frame.mode == "3d"
                assert isinstance(frame.bgra.base, torch.Tensor) and frame.bgra.base.is_pinned()
                assert frame.colour_precision == "float" and frame.depth_refinement == "none"
                if profile is not None:
                    assert frame.disparity_profile == profile and frame.disparity_profile_reason is None
                    assert frame.effective_convergence == (.625 if profile == "comfort" else .5)

            # Observe genuine projector inputs on initial calls, then restore it.
            # The production projector still creates every output; timing is unhooked.
            original_project, projected, observing = projector.synthesize_forward_cuda, {}, None
            def observe(image, inverse_depth, d, convergence):
                result = original_project(image, inverse_depth, d, convergence)
                projected[observing] = (inverse_depth.cpu().numpy().copy(), convergence, d)
                return result
            first = {}
            try:
                projector.synthesize_forward_cuda = observe
                for name in synths:
                    observing = name
                    first[name] = run(name)
            finally:
                projector.synthesize_forward_cuda = original_project
            z, c, d = projected["linear_a"]
            expected_z = np.where(z < .75, .75 + .5 * (z - .75), z)
            np.testing.assert_array_equal(projected["comfort"][0], expected_z)
            assert c == .5 and projected["comfort"][1:] == (.625, d)
            np.testing.assert_array_equal(projected["before"][0], z)
            np.save(output / "observed-linear-eye-depth.npy", z, allow_pickle=False)
            np.save(output / "observed-comfort-eye-depth.npy", projected["comfort"][0], allow_pickle=False)
            analytic_z = np.linspace(0, 1, 1025, dtype=np.float64)
            mapped = np.where(analytic_z < .75, .75 + .5 * (analytic_z - .75), analytic_z)
            assert np.all(np.diff(mapped) >= 0) and mapped[512] == .625
            assert np.array_equal(mapped[analytic_z >= .75], analytic_z[analytic_z >= .75])
            left_shift = (mapped - .625) * d / 2
            right_shift = -left_shift
            assert left_shift[512] == right_shift[512] == 0 and left_shift[-1] > 0 > right_shift[-1]
            assert np.allclose(np.diff(left_shift[768:]), np.diff(analytic_z[768:]) * d / 2, rtol=0, atol=1e-14)
            report["mapping_proof"] = {"actual_projector_inputs_match_independent_formula": True,
                "monotone_order_analytic": True, "near_local_slope_one_analytic": True,
                "both_eye_projection_signs_analytic": True, "midpoint": [.5, .625],
                "no_new_mask_claim": "No coverage masks are exposed by StereoSynthesizer; saved depth arrays are actual projector inputs, not masks",
                "limit": "Near slope/sign checks are analytic, not evidence of quality on another real near-object scene"}
            for name, frame in first.items():
                check(frame, None if name == "before" else ("comfort" if name == "comfort" else "linear"))
                assert frame.scene_reset and frame.depth_range == first["before"].depth_range
            for name in ("linear_a", "linear_b"):
                np.testing.assert_array_equal(first[name].bgra, first["before"].bgra)
            np.testing.assert_array_equal(first["linear_a"].bgra[:, :, [2, 1, 0]], np.asarray(Image.open(CASE / "stereo-sbs.png").convert("RGB")))
            held = {name: frame.bgra.copy() for name, frame in first.items()}
            for name in ("linear_a", "comfort", "linear_b"):
                for eye, label in enumerate(("left", "right")):
                    Image.fromarray(first[name].bgra[:, eye*width:(eye+1)*width, [2, 1, 0]]).save(output / f"{name}-{label}.png")
            diff = np.abs(held["comfort"][:, :, :3].astype(np.int16) - held["linear_a"][:, :, :3].astype(np.int16))
            report["difference_lsb"] = {name: {"max": int(value.max()), "mean": float(value.mean()),
                "changed_pixel_fraction": float(np.any(value != 0, axis=-1).mean())}
                for name, value in (("both", diff), ("left", diff[:, :width]), ("right", diff[:, width:]))}
            report["fresh_linear_before_and_saved_png_exact"] = True

            for round_index in range(13):
                order = ("linear_a", "comfort", "linear_b") if round_index % 2 == 0 else ("linear_b", "comfort", "linear_a")
                for name in order:
                    begin, end = torch.cuda.Event(enable_timing=True), torch.cuda.Event(enable_timing=True)
                    start = time.perf_counter_ns()
                    begin.record()
                    result = run(name)
                    end.record()
                    end.synchronize()
                    done = time.perf_counter_ns()
                    check(result, "comfort" if name == "comfort" else "linear")
                    assert result.depth_range == first[name].depth_range and not result.scene_reset
                    np.testing.assert_array_equal(result.bgra, held[name])
                    if round_index >= 3:
                        report["samples"].append({"round": round_index-3, "name": name, "start_qpc_ns": start,
                            "end_qpc_ns": done, "wall_ms": (done-start)/1e6, "cuda_stream_span_ms": begin.elapsed_time(end)})
            report["timing"] = {name: {metric: dict(zip(("p50", "p95"), np.percentile(
                [row[metric] for row in report["samples"] if row["name"] == name], [50, 95]).tolist()))
                for metric in ("wall_ms", "cuda_stream_span_ms")} for name in ("linear_a", "comfort", "linear_b")}

            owner = synths["linear_a"]
            for profile, expected in (("comfort", held["comfort"]), ("linear", held["linear_a"])):
                owner.disparity_profile = profile
                result = run("linear_a")
                check(result, profile)
                assert result.depth_range == first["linear_a"].depth_range and not result.scene_reset
                np.testing.assert_array_equal(result.bgra, expected)
            report["same_owner_ABA_exact"] = True
            original = synths["before"].original_2d(source, frame_id=fid, generation=gen)
            rejected = 0
            for profile in ("linear", "comfort"):
                owner.disparity_profile = profile
                mono = owner.original_2d(source, frame_id=fid, generation=gen)
                np.testing.assert_array_equal(mono.bgra, original.bgra)
                assert mono.mode == "2d" and mono.disparity_profile == "linear"
                assert mono.disparity_profile_reason == ("original_2d" if profile == "comfort" else None)
                assert mono.effective_convergence is None
                owner.disparity_px = 0
                zero = run("linear_a")
                np.testing.assert_array_equal(zero.bgra, original.bgra)
                assert zero.mode == "2d" and zero.disparity_profile == "linear"
                assert zero.disparity_profile_reason == mono.disparity_profile_reason and zero.effective_convergence is None
                for d in (0, disparity):
                    owner.disparity_px = d
                    for bad_id, bad_gen in ((fid+1, gen), (fid, gen+1)):
                        bad = DepthResult(bad_id, bad_gen, raw_gpu, depth.input_shape, 0., 0.)
                        try:
                            owner.synthesize(source, bad, frame_id=fid, generation=gen)
                        except ValueError as exc:
                            assert "different RGB frame" in str(exc)
                            rejected += 1
                        else:
                            raise AssertionError("Mismatched frame/generation accepted")
            report.update(two_d_and_zero_d_exact=True, frame_generation_rejections=rejected)
            np.testing.assert_array_equal(source.cpu().numpy(), bgra)
            np.testing.assert_array_equal(raw_gpu.cpu().numpy(), raw)
            for name, frame in first.items():
                np.testing.assert_array_equal(frame.bgra, held[name])
            report["input_and_held_output_immutable"] = True
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
                report["errors"].append(f"{name} close: {traceback.format_exc()}")
        report["hash_after"] = hashes()
        if report.get("hash_before") != report["hash_after"]:
            report["status"] = "FAIL"
            report["errors"].append("Source/evidence changed during run")
        report["output_hashes"] = {p.name: sha(p) for p in output.iterdir() if p.is_file()}
        report["finished_at"] = stamp()
        (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"status": report["status"], "output": str(output), "errors": report["errors"]}))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

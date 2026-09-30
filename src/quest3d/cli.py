"""Reproducible PC diagnostics and the first real capture/AI/encoder connection."""

import argparse
from dataclasses import asdict
from datetime import datetime
import json
import math
from pathlib import Path
import sys
import time

from .paths import ARTIFACT_DIR


def print_json(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def doctor() -> dict:
    import av
    import torch
    import torchvision
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is unavailable. This command does not substitute a CPU model.")
    capability = torch.cuda.get_device_capability()
    torch.manual_seed(0)
    source = torch.randn(128, 128, device="cuda")
    result = source @ source.T
    torch.cuda.synchronize()
    if not bool(torch.isfinite(result).all().item()):
        raise RuntimeError("CUDA smoke test produced invalid values")
    return {
        "python": sys.version, "executable": sys.executable,
        "torch": torch.__version__, "torchvision": torchvision.__version__,
        "cuda_runtime": torch.version.cuda, "gpu": torch.cuda.get_device_name(),
        "capability": capability, "wheel_architectures": torch.cuda.get_arch_list(),
        "total_vram_bytes": torch.cuda.get_device_properties(0).total_memory,
        "cuda_tensor_test": "passed", "av": av.__version__,
        "h264_nvenc_codec_registered": "h264_nvenc" in av.codecs_available,
        "model_inference_verified": False, "quest_display_verified": False,
    }


def capture_pipeline(args) -> dict:
    from .input_policy import frame_input_decision, validate_input_options
    validate_input_options(args)
    resize_filter = getattr(args, "resize_filter", "area")
    stereo_method = getattr(args, "stereo_method", "backward")
    depth_refinement = getattr(args, "depth_refinement", "none")
    colour_precision = getattr(args, "colour_precision", "uint8")
    disparity_profile = getattr(args, "disparity_profile", "linear")
    if not isinstance(disparity_profile, str) or disparity_profile not in ("linear", "comfort"):
        raise ValueError("Disparity profile must be linear or comfort")
    if disparity_profile == "comfort" and (getattr(args, "inline_rect", None) or getattr(args, "file_av_clock", False)):
        raise ValueError("Comfort disparity profile requires the enlarged desktop path")
    if colour_precision not in ("uint8", "float"):
        raise ValueError("Unknown colour precision")
    if colour_precision == "float" and (getattr(args, "inline_rect", None) or getattr(args, "file_av_clock", False)):
        raise ValueError("Float colour precision requires the enlarged desktop path")
    if depth_refinement not in ("none", "guided", "edge-aware", "edge-cuda"):
        raise ValueError("Unknown depth refinement")
    if depth_refinement != "none" and (getattr(args, "inline_rect", None) or getattr(args, "file_av_clock", False)):
        raise ValueError("Guided depth refinement requires the enlarged desktop path")
    if stereo_method not in ("backward", "forward", "forward-cuda"):
        raise ValueError("Unknown stereo method")
    if stereo_method != "backward" and getattr(args, "inline_rect", None):
        raise ValueError("Forward stereo requires the enlarged desktop path")
    if resize_filter != "area" and getattr(args, "inline_rect", None):
        raise ValueError("RGB resize comparison requires the enlarged desktop path")
    import cv2
    import psutil
    import torch
    from .bridge import FramePublisher, FULL_SBS, ORIGINAL_2D, CAPTURE_RECEIPT, INPUT_ENABLED
    from .capture import DesktopCapture, GPUDesktopCapture
    from .cursor import DesktopCursorOverlay
    from .depth import DepthEngine
    from .encode import NvencRecorder
    from .geometry import parse_rect
    from .metrics import Metrics
    from .stereo import StereoSynthesizer

    output = Path(args.output) if args.output else ARTIFACT_DIR / datetime.now().strftime("run-%Y%m%d-%H%M%S")
    if (output / "frames.jsonl").exists() or (output / "summary.json").exists():
        raise FileExistsError("Use a new output directory to preserve previous measurements")
    output.mkdir(parents=True, exist_ok=True)
    metrics = Metrics(output)
    torch.set_num_threads(args.cpu_threads)
    engine = publisher = recorder = capture = cursor = stereo = None
    process = psutil.Process()
    error = None
    torch.cuda.reset_peak_memory_stats()
    frame_count, last = 0, None
    try:
        engine = None if args.mode == "2d" else DepthEngine(args.ai_size, not args.fp32)
        inline_rect = parse_rect(args.inline_rect) if getattr(args, "inline_rect", None) else None
        if inline_rect:
            from .inline import InlineGeometry, InlineSynthesizer
            stereo = InlineSynthesizer(args.eye_width, args.eye_height, args.disparity)
        else:
            stereo = StereoSynthesizer(args.eye_width, args.eye_height, args.disparity,
                                       resize_filter=resize_filter, stereo_method=stereo_method,
                                       depth_refinement=depth_refinement, colour_precision=colour_precision,
                                       disparity_profile=disparity_profile)
        bridge_protocol = getattr(args, "bridge_protocol", 2)
        publisher = (FramePublisher() if bridge_protocol == 2 else FramePublisher(version=bridge_protocol)) if args.publish else None
        recorder = NvencRecorder(output / "nvenc-sbs.mp4", args.eye_width * 2,
                                 args.eye_height, args.fps) if args.record else None
        backend = GPUDesktopCapture if args.capture_backend == "wc-cuda" else DesktopCapture
        capture_options = {"experimental_hdr": True, "hdr_tonemap": args.hdr_tonemap or "fused"} if getattr(args, "experimental_hdr", False) else {}
        started = time.perf_counter()
        with backend(monitor=args.monitor, rect=parse_rect(args.rect) if args.rect else None, **capture_options) as capture:
            cursor = DesktopCursorOverlay.from_capture(capture, enabled=not getattr(args, "hide_cursor", False))
            while time.perf_counter() - started < args.seconds or frame_count < args.warmup + 1:
                tick = time.perf_counter_ns()
                frame = capture.grab()
                capture_ms = (time.perf_counter_ns() - tick) / 1e6
                if frame_count == 0:
                    source = frame.bgra.cpu().numpy() if isinstance(frame.bgra, torch.Tensor) else frame.bgra
                    cv2.imwrite(str(output / "source.png"), source)
                geometry = InlineGeometry(frame.geometry, inline_rect) if inline_rect else None
                inference_source = geometry.crop_for_depth(frame.bgra) if geometry else frame.bgra
                depth = engine.infer(inference_source, frame_id=frame.frame_id,
                                     generation=frame.geometry_generation) if engine else None
                stereo_start = time.perf_counter_ns()
                if geometry:
                    if depth:
                        last = stereo.synthesize(frame.bgra, geometry.bind_depth(depth), frame_id=frame.frame_id,
                                                 geometry=geometry)
                    else:
                        last = stereo.original_2d(frame.bgra, frame_id=frame.frame_id, geometry=geometry)
                elif depth:
                    last = stereo.synthesize(frame.bgra, depth, frame_id=frame.frame_id,
                                             generation=frame.geometry_generation)
                else:
                    last = stereo.original_2d(frame.bgra, frame_id=frame.frame_id,
                                              generation=frame.geometry_generation)
                stereo_ms = (time.perf_counter_ns() - stereo_start) / 1e6
                cursor_start = time.perf_counter_ns()
                last = cursor.sample_and_composite(last, frame)
                cursor_ms = (time.perf_counter_ns() - cursor_start) / 1e6
                if frame_count == 0:
                    cv2.imwrite(str(output / "first-sbs.png"), last.bgra)
                encode_start = time.perf_counter_ns()
                if recorder:
                    recorder.write(last.bgra, frame.captured_ns)
                encode_ms = (time.perf_counter_ns() - encode_start) / 1e6
                publish_start = time.perf_counter_ns()
                published = False
                input_decision = frame_input_decision(opt_in=getattr(args, "enable_input", False),
                    protocol=bridge_protocol, requested_mode=args.mode, output=last,
                    source=frame, current_frame=frame, now_ns=time.perf_counter_ns())
                if publisher:
                    bounds = frame.geometry.bounds
                    published = publisher.publish(
                        last.bgra, frame_id=frame.frame_id, capture_ns=frame.captured_ns,
                        generation=frame.geometry_generation,
                        flags=FULL_SBS | CAPTURE_RECEIPT | (ORIGINAL_2D if last.mode == "2d" else 0)
                            | (INPUT_ENABLED if input_decision.enabled else 0),
                        source_rect=(bounds.left, bounds.top, bounds.width, bounds.height),
                        content_rect=last.content_rect,
                        **({"source_identity": frame.source_identity} if bridge_protocol == 3 else {}),
                    )
                completed = time.perf_counter_ns()
                metrics.add({
                    "frame_id": frame.frame_id, "source_id": frame.source_id,
                    "generation": frame.geometry_generation, "mode": last.mode,
                    "view_layout": "inline" if geometry else "enlarged",
                    "inline_rect": getattr(args, "inline_rect", None),
                    "inference_source_size": list(inference_source.shape[1::-1]),
                    "capture_timestamp_kind": getattr(capture, "timestamp_kind", "cpu_receipt"),
                    "capture_backend": args.capture_backend, "capture_ns": frame.captured_ns,
                    "completed_ns": completed, "source_size": list(frame.bgra.shape[1::-1]),
                    "source_rect": asdict(frame.geometry.bounds),
                    "ai_shape_hw": depth.input_shape if depth else None,
                    "eye_size": [args.eye_width, args.eye_height],
                    "rgb_resize_filter": resize_filter,
                    "stereo_method": getattr(stereo, "stereo_method", "backward"),
                    "depth_refinement": getattr(stereo, "depth_refinement", "none"),
                    "effective_depth_refinement": getattr(last, "depth_refinement", "none"),
                    "depth_refinement_reason": getattr(last, "depth_refinement_reason", None),
                    "colour_precision": colour_precision,
                    "effective_colour_precision": getattr(last, "colour_precision", "uint8"),
                    "colour_precision_reason": getattr(last, "colour_precision_reason", None),
                    "disparity_profile": disparity_profile,
                    "effective_disparity_profile": getattr(last, "disparity_profile", "linear"),
                    "disparity_profile_reason": getattr(last, "disparity_profile_reason", None),
                    "effective_convergence": getattr(last, "effective_convergence", None),
                    "stream_size": [args.eye_width * 2, args.eye_height],
                    "bridge_protocol": bridge_protocol,
                    "publisher_epoch": str(publisher.epoch) if publisher else None,
                    "input_opt_in": bool(getattr(args, "enable_input", False)),
                    "input_flag": bool(published and input_decision.enabled),
                    "input_reason": input_decision.reason, "os_input_verified": False,
                    "capture_ms": capture_ms, "preprocess_ms": depth.preprocess_ms if depth else 0,
                    "capture_color_processing_ms": getattr(frame, "color_processing_ms", 0),
                    "inference_ms": depth.inference_ms if depth else 0,
                    "stereo_readback_ms": stereo_ms, "encode_ms": encode_ms,
                    "cursor_overlay_ms": cursor_ms, "cursor": cursor.snapshot(),
                    "publish_ms": (completed - publish_start) / 1e6,
                    "pc_total_ms": (completed - tick) / 1e6,
                    "source_age_at_completion_ms": (completed - frame.captured_ns) / 1e6,
                    "capture_frames_overwritten": getattr(capture, "dropped_frames", 0),
                    "published": published, "warmup": frame_count < args.warmup,
                    "scene_reset": last.scene_reset, "depth_range": last.depth_range,
                    "torch_vram_allocated": torch.cuda.memory_allocated(),
                    "ram_rss": process.memory_info().rss,
                })
                frame_count += 1
                if args.fps > 0:
                    remaining = 1 / args.fps - (time.perf_counter_ns() - tick) / 1e9
                    if remaining > 0:
                        time.sleep(remaining)
    except KeyboardInterrupt:
        error = "user_interrupted"
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        cleanup_errors = []
        try:
            if last is not None:
                cv2.imwrite(str(output / "last-sbs.png"), last.bgra)
        except Exception as exc:
            cleanup_errors.append(f"snapshot: {exc}")
        for name, resource in (("cursor", cursor), ("publisher", publisher), ("recorder", recorder), ("stereo", stereo)):
            if resource is not None and hasattr(resource, "close"):
                try:
                    resource.close()
                except Exception as exc:
                    cleanup_errors.append(f"{name}: {exc}")
        summary = metrics.finish({
            "output": str(output.resolve()), "model": "Depth-Anything-V2-Small" if engine else None,
            "mode": args.mode, "error": error, "requested_output_fps": args.fps,
            "stereo_method": stereo_method,
            "depth_refinement": depth_refinement,
            "colour_precision": colour_precision,
            "effective_colour_precision": getattr(last, "colour_precision", "uint8"),
            "colour_precision_reason": getattr(last, "colour_precision_reason", None),
            "disparity_profile": disparity_profile,
            "effective_disparity_profile": getattr(last, "disparity_profile", "linear"),
            "disparity_profile_reason": getattr(last, "disparity_profile_reason", None),
            "effective_convergence": getattr(last, "effective_convergence", None),
            "timestamp_scope": "PC capture call to encode/publish; excludes network/Quest",
            "fp16": not args.fp32, "torch": torch.__version__, "cuda_runtime": torch.version.cuda,
            "peak_torch_vram_allocated": torch.cuda.max_memory_allocated(),
            "peak_torch_vram_reserved": torch.cuda.max_memory_reserved(),
            "bridge_skipped": publisher.skipped if publisher else 0,
            "nvenc_recording": bool(recorder), "pc_audio_integrated": False,
            "capture_backend": args.capture_backend,
            "view_layout": "inline" if getattr(args, "inline_rect", None) else "enlarged",
            "inline_rect": getattr(args, "inline_rect", None),
            "capture_color_profile": getattr(capture, "color_profile", None),
            "cursor": cursor.snapshot() if cursor else None,
            "capture_frames_overwritten": getattr(capture, "dropped_frames", 0),
            "native_texture_lease_verified": getattr(capture, "native_texture_lease_verified", None),
            "cleanup_errors": cleanup_errors,
        })
        if cleanup_errors and error is None:
            raise RuntimeError("; ".join(cleanup_errors))
    return summary


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="quest3d")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="Validate the actual CUDA wheel and device")
    setup = commands.add_parser("setup-model", help="Explicitly download and verify a pinned local depth checkpoint")
    from .model_choice import DEFAULT_DEPTH_MODEL, DEPTH_MODEL_IDS
    setup.add_argument("--model-id", choices=DEPTH_MODEL_IDS, default=DEFAULT_DEPTH_MODEL)
    commands.add_parser("sources", help="List real Windows monitors and windows")
    serve = commands.add_parser("serve", help="Persistent GPU session with independent original-2D control")
    serve_source = serve.add_mutually_exclusive_group()
    serve_source.add_argument("--monitor", type=int, default=1)
    serve_source.add_argument("--window", type=lambda value: int(value, 0),
                              help="Explicit current HWND (decimal or 0x...); isolated WGC with v3")
    serve.add_argument("--experimental-window", action="store_true",
                       help="Enable the contained HWND capture candidate; requires --window")
    serve.add_argument("--bridge-protocol", type=int, choices=(2, 3), default=2,
                       help="Explicit shared-frame protocol; v3 carries source lifetime and requires a matching host")
    serve.add_argument("--enable-input", action="store_true",
                       help="Opt in to fresh original-2D monitor frame input; requires v3 and separate host/Quest opt-in")
    serve.add_argument("--rect")
    serve.add_argument("--hide-cursor", action="store_true",
                       help="Hide the flat Windows cursor overlay; desktop capture only")
    serve.add_argument("--inline-rect", help="Physical desktop ROI to stereo in place; surrounding desktop stays flat")
    serve.add_argument("--experimental-hdr", action="store_true",
                       help="Explicit FP16 HDR capture candidate; requires the separate +quest2 wheel")
    serve.add_argument("--hdr-tonemap", choices=("torch", "fused"),
                       help="Experimental HDR transform implementation; defaults to the fused sm75 candidate")
    serve_source.add_argument("--file", help="Local video/photo source; audio requires the explicit native PCM candidate")
    serve.add_argument("--file-av-clock", action="store_true",
                       help="Use the common video/audio timeline; native audio requires --file-native-pcm")
    serve.add_argument("--file-native-pcm", action="store_true",
                       help="Explicit file PCM candidate; requires a native consumer, transport pause/seek pending")
    serve.add_argument("--paused", action="store_true", help="Open a local video paused")
    serve.add_argument("--subtitles", help="UTF-8 SRT for --file; flat captions after stereo synthesis")
    serve.add_argument("--subtitle-font", help="Explicit installed TTF/OTF; default Windows Malgun Gothic")
    serve.add_argument("--subtitle-size", type=int, help="Caption pixels per eye, 8..128; default 32")
    serve.add_argument("--file-playout-ms", type=float, default=120,
                       help="Bounded file playout (0..500 ms); common AV clock requires integral 1..500 ms")
    serve.add_argument("--mode", choices=("2d", "3d"), default="2d")
    serve.add_argument("--seconds", type=float, default=0, help="0 runs until stopped")
    serve.add_argument("--ai-size", type=int, default=280)
    serve.add_argument("--depth-model", choices=DEPTH_MODEL_IDS, default=DEFAULT_DEPTH_MODEL)
    serve.add_argument("--enable-dad-comparison", action="store_true",
                       help="Prewarm both installed models before capture for live selection; one inference per frame")
    serve.add_argument("--depth-execution", choices=("eager", "cuda-graph"), default="eager",
                       help="Same model execution; prewarm a fixed-shape CUDA graph before monitor capture")
    serve.add_argument("--reuse-depth-constants", action="store_true",
                       help="Reuse immutable model constants with the pinned FP16 CUDA graph")
    serve.add_argument("--fused-depth-resize", action="store_true",
                       help="Use exact fused cubic GPU input resize with monitor CUDA graph execution")
    serve.add_argument("--fused-stereo-output", action="store_true",
                       help="Combine forward CUDA final fill and owned SBS readback")
    serve.add_argument("--fused-forward-validation", action="store_true",
                       help="Validate the same forward input ranges with one CUDA scan")
    serve.add_argument("--fused-colour-fit", action="store_true",
                       help="Use exact cached Torch AA coefficients for CUDA float colour fitting")
    serve.add_argument("--reuse-immutable-payload", action="store_true",
                       help="Reuse identical owned desktop pixels while updating every bridge header")
    serve.add_argument("--trace-cadence", action="store_true",
                       help="Record capture, worker availability and presentation timing for diagnosis")
    serve.add_argument("--eye-width", type=int, default=1280)
    serve.add_argument("--eye-height", type=int, default=720)
    serve.add_argument("--resize-filter", choices=("area", "bicubic-aa"), default="area",
                       help="RGB resize filter; bicubic-aa is an explicit quality comparison, independent of AI input size")
    serve.add_argument("--stereo-method", choices=("backward", "forward", "forward-cuda"), default="backward",
                       help="Stereo synthesis; forward is an explicit visibility experiment for the enlarged desktop")
    serve.add_argument("--depth-refinement", choices=("none", "guided", "edge-aware", "edge-cuda"), default="none",
                       help="Optional depth interpolation; edge-cuda is bounded contour correction on pinned sm75")
    serve.add_argument("--colour-precision", choices=("uint8", "float"), default="uint8",
                       help="Float preserves fitted colour until eye quantization; requires bicubic-aa, same-device Tensor and no depth refinement")
    serve.add_argument("--disparity", type=float, default=12)
    serve.add_argument("--disparity-profile", choices=("linear", "comfort"), default="linear",
                       help="Optional disparity mapping; linear preserves the existing profile")
    serve.add_argument("--fps", type=int, default=30)
    serve.add_argument("--cpu-threads", type=int, default=4)
    serve.add_argument("--max-frame-age-ms", type=float, default=200)
    serve.add_argument("--output")
    control = commands.add_parser("control", help="Set the current local session mode or stereo strength")
    control.add_argument("--session", help="Session output directory; defaults to the latest session")
    control.add_argument("--mode", choices=("2d", "3d"))
    control.add_argument("--depth-model", choices=DEPTH_MODEL_IDS)
    control.add_argument("--disparity", type=float)
    control.add_argument("--disparity-profile", choices=("linear", "comfort"),
                         help="Change the disparity profile only when explicitly specified; requires a compatible producer")
    control.add_argument("--stop", action="store_true")
    pause_group = control.add_mutually_exclusive_group()
    pause_group.add_argument("--pause", action="store_const", const=True, dest="paused", default=None)
    pause_group.add_argument("--resume", action="store_const", const=False, dest="paused")
    control.add_argument("--seek", type=float, dest="seek_seconds", help="Local video position in seconds")
    run = commands.add_parser("run", help="Real desktop capture, depth inference, stereo, and optional NVENC/IPC")
    run.add_argument("--monitor", type=int, default=1)
    run.add_argument("--bridge-protocol", type=int, choices=(2, 3), default=2,
                     help="Explicit IPC protocol for --publish; default v2, source lifetime v3")
    run.add_argument("--enable-input", action="store_true",
                     help="Opt in to original-2D monitor frame input; requires --publish --bridge-protocol 3")
    run.add_argument("--capture-backend", choices=("mss", "wc-cuda"), default="mss",
                     help="wc-cuda is experimental and requires the gpu-capture extra")
    run.add_argument("--rect", help="LEFT,TOP,WIDTH,HEIGHT in physical desktop pixels")
    run.add_argument("--hide-cursor", action="store_true",
                     help="Exclude the flat cursor overlay from desktop measurements/recording")
    run.add_argument("--inline-rect", help="Physical desktop ROI to stereo in place; surrounding desktop stays flat")
    run.add_argument("--experimental-hdr", action="store_true",
                     help="FP16 HDR candidate with measured monitor white; requires wc-cuda +quest2")
    run.add_argument("--hdr-tonemap", choices=("torch", "fused"),
                     help="Experimental HDR transform implementation; defaults to the fused sm75 candidate")
    run.add_argument("--mode", choices=("2d", "3d"), default="3d")
    run.add_argument("--seconds", type=float, default=20)
    run.add_argument("--warmup", type=int, default=5)
    run.add_argument("--ai-size", type=int, default=280)
    run.add_argument("--eye-width", type=int, default=1280)
    run.add_argument("--eye-height", type=int, default=720)
    run.add_argument("--resize-filter", choices=("area", "bicubic-aa"), default="area",
                     help="RGB resize filter for the enlarged desktop; preserves area unless explicitly selected")
    run.add_argument("--stereo-method", choices=("backward", "forward", "forward-cuda"), default="backward",
                     help="Stereo synthesis; forward is an explicit visibility experiment for the enlarged desktop")
    run.add_argument("--depth-refinement", choices=("none", "guided", "edge-aware", "edge-cuda"), default="none",
                     help="Optional depth interpolation; none retains bilinear, edge-aware is the Torch reference")
    run.add_argument("--colour-precision", choices=("uint8", "float"), default="uint8",
                     help="Float is an explicit colour fit comparison; unsupported inputs report uint8 fallback")
    run.add_argument("--disparity", type=float, default=12)
    run.add_argument("--disparity-profile", choices=("linear", "comfort"), default="linear",
                     help="Optional disparity mapping for the enlarged desktop; linear remains the default")
    run.add_argument("--fps", type=int, default=30)
    run.add_argument("--cpu-threads", type=int, default=4)
    run.add_argument("--fp32", action="store_true")
    run.add_argument("--publish", action="store_true")
    run.add_argument("--record", action="store_true")
    run.add_argument("--output")
    args = parser.parse_args()
    from .input_policy import validate_input_options
    from .subtitle_session import validate_subtitle_options
    from .window_session import validate_window_options
    try:
        validate_input_options(args)
        validate_subtitle_options(args)
        validate_window_options(args)
    except ValueError as exc:
        parser.error(str(exc))
    if getattr(args, "file_av_clock", False) and not getattr(args, "file", None):
        parser.error("--file-av-clock requires a local video --file")
    if getattr(args, "file_native_pcm", False) and not getattr(args, "file_av_clock", False):
        parser.error("--file-native-pcm requires --file-av-clock")
    if getattr(args, "hdr_tonemap", None) and not getattr(args, "experimental_hdr", False):
        parser.error("--hdr-tonemap requires --experimental-hdr")
    if getattr(args, "inline_rect", None) and (getattr(args, "rect", None) or getattr(args, "file", None)):
        parser.error("--inline-rect uses a complete desktop source; do not combine with --rect or --file")
    if getattr(args, "depth_refinement", "none") != "none" and (
            getattr(args, "inline_rect", None) or getattr(args, "file_av_clock", False)):
        parser.error("Guided depth refinement requires the enlarged desktop path")
    if getattr(args, "colour_precision", "uint8") == "float" and (
            getattr(args, "inline_rect", None) or getattr(args, "file_av_clock", False)):
        parser.error("Float colour precision requires the enlarged desktop path")
    if getattr(args, "disparity_profile", "linear") == "comfort" and (
            getattr(args, "inline_rect", None) or getattr(args, "file_av_clock", False)):
        parser.error("Comfort disparity profile requires the enlarged desktop path")
    if getattr(args, "stereo_method", "backward") == "forward" and (
            getattr(args, "inline_rect", None) or getattr(args, "file_av_clock", False)):
        parser.error("Forward stereo requires the enlarged desktop path")
    try:
        if args.command == "doctor":
            result = doctor()
        elif args.command == "setup-model":
            from .assets import setup_model
            result = setup_model(model_id=args.model_id)
        elif args.command == "sources":
            from .capture import list_monitors, list_windows
            result = {"monitors": [asdict(value) for value in list_monitors()],
                      "windows": [asdict(value) for value in list_windows()]}
        elif args.command == "serve":
            from .session import serve as serve_session
            if (not math.isfinite(args.seconds) or args.seconds < 0
                    or not math.isfinite(args.max_frame_age_ms)
                    or min(args.fps, args.cpu_threads, args.max_frame_age_ms) <= 0):
                parser.error("seconds must be nonnegative; fps/threads/frame age must be positive")
            if args.file and args.rect:
                parser.error("--file and desktop --rect cannot be combined")
            if args.file and args.experimental_hdr:
                parser.error("--experimental-hdr is a desktop capture option, not a file color transform")
            if args.paused and not args.file:
                parser.error("--paused requires --file")
            if not 0 <= args.file_playout_ms <= 500:
                parser.error("file playout delay must be finite and between 0 and 500 ms")
            if args.output is None:
                args.output = str(ARTIFACT_DIR / datetime.now().strftime("session-%Y%m%d-%H%M%S"))
            result = serve_session(args)
        elif args.command == "control":
            from .session_control import read_json, send_control
            if args.session:
                session = Path(args.session)
            else:
                session = Path(read_json(ARTIFACT_DIR / "active-session.json")["directory"])
            result = send_control(session, mode=args.mode, disparity=args.disparity, stop=args.stop,
                                  paused=args.paused, seek_seconds=args.seek_seconds,
                                  disparity_profile=args.disparity_profile, depth_model=args.depth_model)
        else:
            if args.experimental_hdr and args.capture_backend != "wc-cuda":
                parser.error("--experimental-hdr requires --capture-backend wc-cuda")
            if args.seconds <= 0 or args.warmup < 0 or args.fps <= 0 or args.cpu_threads <= 0:
                parser.error("seconds/fps/cpu-threads must be positive and warmup nonnegative")
            result = capture_pipeline(args)
        print_json(result)
    except Exception as exc:
        print_json({"error": type(exc).__name__, "message": str(exc)})
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

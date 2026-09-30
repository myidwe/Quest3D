"""Bounded AI worker and independent original-2D presentation loop."""
from dataclasses import dataclass, replace
from collections import deque
from functools import partial
import json
from pathlib import Path
import threading
import time
import uuid
import sys

import torch

from .bridge import CAPTURE_RECEIPT, FULL_SBS, ORIGINAL_2D, INPUT_ENABLED, FramePublisher
from .capture import GPUDesktopCapture
from .cursor import DesktopCursorOverlay
from .depth import DepthEngine
from .geometry import parse_rect
from .metrics import Metrics
from .model_choice import DEFAULT_DEPTH_MODEL, DEPTH_MODEL_IDS, validate_depth_model
from .input_policy import MAX_INPUT_CAPTURE_AGE_NS, frame_input_decision, validate_input_options
from .paths import ARTIFACT_DIR
from .pacing import FramePacer
from .session_control import atomic_json, expected_output_mode, read_json, validate_request
from .stereo import StereoSynthesizer
from .subtitle_session import FileSubtitleOverlay, validate_subtitle_options
from .window_session import selected_window_capture, validate_window_options
from .window_capture import WindowCaptureUnavailable


@dataclass(frozen=True)
class Processed:
    source: object
    stereo: object
    revision: int
    completion_sequence: int = 0
    completed_ns: int = 0
    available_ns: int = 0
    depth_model: str | None = None
    ai_input_shape: tuple[int, int] | None = None


class _PreparationCancelled(Exception):
    """A valid Stop arrived before live capture was started."""


class LatestAIWorker:
    def __init__(self, *, eye_width, eye_height, ai_size, directory,
                 engine_factory=DepthEngine, synth_factory=StereoSynthesizer, buffered=False,
                 inline_rect=None, engine_prepare_size=None, isolate_cuda=False, trace_cadence=False):
        self.condition = threading.Condition()
        self.pending = self.latest = None
        self.error = None
        self.ready = False
        self.closing = False
        self.overwritten = 0
        self.frames = 0
        self.buffered = buffered
        self.completed_queue = deque()
        self.display_candidate = None
        self.completed_queue_overwritten = 0
        self.completed_due_skipped = 0
        self.eye_width, self.eye_height, self.ai_size = eye_width, eye_height, ai_size
        self.directory = Path(directory)
        self.engine_factory, self.synth_factory = engine_factory, synth_factory
        self.inline_rect = inline_rect
        self.engine_prepare_size = engine_prepare_size
        self.execution_status = None
        self.isolate_cuda = isolate_cuda
        self.trace_cadence = trace_cadence
        self.thread = threading.Thread(target=self._run, name="Quest3D AI", daemon=True)
        self.thread.start()

    def submit(self, frame, revision, disparity, disparity_profile="linear", *, depth_model=None):
        if depth_model is not None:
            validate_depth_model(depth_model)
        if disparity_profile not in ("linear", "comfort"):
            raise ValueError("Unknown disparity profile")
        if self.inline_rect is not None and disparity_profile != "linear":
            raise ValueError("Disparity comfort requires the enlarged desktop path")
        with self.condition:
            if self.error or self.closing:
                return
            if self.pending is not None:
                self.overwritten += 1
            # Profile travels with the revision/frame, never via shared mutable
            # synth state while an older inference is still running.
            submitted_ns = time.perf_counter_ns()
            self.pending = (frame, revision, disparity, disparity_profile, submitted_ns, depth_model)
            self.condition.notify()
            return submitted_ns

    def snapshot(self):
        with self.condition:
            return self.latest, self.error, self.ready

    def due_for_presentation(self, now_ns, delay_ns, revision, current_frame, timestamp_for=None):
        """Bounded file playout; due RGB/depth pairs remain immutable together."""
        with self.condition:
            def matches(candidate):
                return (candidate.revision == revision
                        and candidate.source.source_id == current_frame.source_id
                        and candidate.source.geometry_generation == current_frame.geometry_generation)
            if self.display_candidate is not None and not matches(self.display_candidate):
                self.display_candidate = None
            selected_this_tick = False
            while self.completed_queue:
                candidate = self.completed_queue[0]
                if not matches(candidate):
                    self.completed_queue.popleft()
                    continue
                scheduled_ns = timestamp_for(candidate.source) if timestamp_for else candidate.source.captured_ns
                if scheduled_ns is None:
                    self.completed_queue.popleft()
                    continue
                if scheduled_ns + delay_ns > now_ns:
                    break
                self.completed_queue.popleft()
                if selected_this_tick:
                    self.completed_due_skipped += 1
                self.display_candidate = candidate
                selected_this_tick = True
            candidate = self.display_candidate
            if candidate is not None and timestamp_for:
                scheduled_ns = timestamp_for(candidate.source)
                if scheduled_ns is None:
                    return None
                candidate = replace(candidate, source=replace(candidate.source, captured_ns=scheduled_ns))
            return candidate

    def _run(self):
        if not self.isolate_cuda:
            return self._run_impl()
        try:
            # Capture hands off completed, independently owned tensors. Keep AI
            # launches off capture's default stream; synthesis completes before
            # publishing and retains the source for the whole operation.
            stream = torch.cuda.Stream()
            stream.wait_stream(torch.cuda.current_stream())
            with torch.cuda.stream(stream):
                self._run_impl()
        except Exception as exc:
            with self.condition:
                self.error = f"AI stream: {type(exc).__name__}: {exc}"

    def _run_impl(self):
        metrics = None
        error = None
        synth = None
        engine = None
        timing_log = None
        active_depth_model = None
        try:
            metrics = Metrics(self.directory)
            if self.trace_cadence:
                timing_log = (self.directory / "worker-timing.jsonl").open("x", encoding="utf-8", buffering=65536)
            engine = self.engine_factory(self.ai_size)
            if self.engine_prepare_size is not None:
                engine.prepare_execution(*self.engine_prepare_size)
                self.execution_status = engine.execution_status()
            with self.condition:
                if self.closing:
                    return
            if self.inline_rect is not None:
                from .inline import InlineGeometry, InlineSynthesizer
                synth = InlineSynthesizer(self.eye_width, self.eye_height, disparity_px=0)
            else:
                synth = self.synth_factory(self.eye_width, self.eye_height, disparity_px=0)
            with self.condition:
                self.ready = True
            while True:
                with self.condition:
                    self.condition.wait_for(lambda: self.pending is not None or self.closing)
                    if self.closing:
                        break
                    frame, revision, disparity, disparity_profile, submitted_ns, depth_model = self.pending
                    self.pending = None
                if depth_model is not None and depth_model != active_depth_model:
                    engine.select_model(depth_model)
                    # Raw scales are model-specific. A single model transition
                    # must not blend the previous model's min/max into the new.
                    synth.reset_depth_history()
                    active_depth_model = depth_model
                started = time.perf_counter_ns()
                geometry = InlineGeometry(frame.geometry, self.inline_rect) if self.inline_rect is not None else None
                inference_source = geometry.crop_for_depth(frame.bgra) if geometry else frame.bgra
                depth = engine.infer(inference_source, frame_id=frame.frame_id,
                                     generation=frame.geometry_generation)
                if hasattr(engine, "execution_status"):
                    self.execution_status = engine.execution_status()
                synth.disparity_px = disparity
                if geometry is None:
                    synth.disparity_profile = disparity_profile
                stereo_started = time.perf_counter_ns()
                if geometry:
                    stereo = synth.synthesize(frame.bgra, geometry.bind_depth(depth), frame_id=frame.frame_id,
                                              geometry=geometry)
                else:
                    stereo = synth.synthesize(frame.bgra, depth, frame_id=frame.frame_id,
                                              generation=frame.geometry_generation)
                completed = time.perf_counter_ns()
                metrics.add({
                    "frame_id": frame.frame_id, "revision": revision,
                    "depth_model": depth_model or getattr(engine, "model_id", DEFAULT_DEPTH_MODEL),
                    "source_id": frame.source_id, "generation": frame.geometry_generation,
                    "completion_sequence": self.frames + 1,
                    "submitted_ns": submitted_ns, "started_ns": started,
                    "view_layout": "inline" if geometry else "enlarged",
                    "stereo_method": getattr(synth, "stereo_method", "backward"),
                    "output_backend": getattr(stereo, "output_backend", "torch"),
                    "validation_backend": getattr(stereo, "validation_backend", "not-used"),
                    "colour_fit_backend": getattr(stereo, "colour_fit_backend", "legacy"),
                    "colour_fit_reason": getattr(stereo, "colour_fit_reason", None),
                    "depth_refinement": getattr(synth, "depth_refinement", "none"),
                    "effective_depth_refinement": getattr(stereo, "depth_refinement", "none"),
                    "depth_refinement_reason": getattr(stereo, "depth_refinement_reason", None),
                    "colour_precision": getattr(synth, "colour_precision", "uint8"),
                    "effective_colour_precision": getattr(stereo, "colour_precision", "uint8"),
                    "colour_precision_reason": getattr(stereo, "colour_precision_reason", None),
                    "disparity_profile": disparity_profile,
                    "effective_disparity_profile": getattr(stereo, "disparity_profile", "linear"),
                    "disparity_profile_reason": getattr(stereo, "disparity_profile_reason", None),
                    "effective_convergence": getattr(stereo, "effective_convergence", None),
                    "inference_source_size": list(inference_source.shape[1::-1]),
                    "ai_size": self.ai_size,
                    "ai_input_shape": list(depth.input_shape) if getattr(depth, 'input_shape', None) else None,
                    "capture_ns": frame.captured_ns, "completed_ns": completed,
                    "depth_execution": getattr(depth, "execution_mode", "eager"),
                    "depth_execution_reason": getattr(depth, "execution_reason", None),
                    "preprocess_ms": depth.preprocess_ms, "inference_ms": depth.inference_ms,
                    "stereo_readback_ms": (completed - stereo_started) / 1e6,
                    "pc_total_ms": (completed - started) / 1e6,
                    "source_age_at_completion_ms": (completed - frame.captured_ns) / 1e6,
                    "warmup": self.frames < 5,
                })
                with self.condition:
                    self.frames += 1
                    available_ns = time.perf_counter_ns()
                    self.latest = Processed(frame, stereo, revision, self.frames, completed, available_ns,
                                            depth_model or getattr(engine, "model_id", DEFAULT_DEPTH_MODEL),
                                            tuple(depth.input_shape) if getattr(depth, 'input_shape', None) else None)
                    if self.buffered:
                        if len(self.completed_queue) >= 8:
                            self.completed_queue.popleft()
                            self.completed_queue_overwritten += 1
                        self.completed_queue.append(self.latest)
                if timing_log is not None:
                    timing_log.write(json.dumps({"completion_sequence": self.frames,
                        "frame_id": frame.frame_id, "revision": revision,
                        "source_id": frame.source_id, "generation": frame.geometry_generation,
                        "capture_ns": frame.captured_ns, "submitted_ns": submitted_ns,
                        "started_ns": started, "completed_ns": completed, "available_ns": available_ns}) + "\n")
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            with self.condition:
                self.error = error
        finally:
            if timing_log is not None:
                try:
                    timing_log.close()
                except Exception as exc:
                    error = f"{error + '; ' if error else ''}worker timing cleanup: {exc}"
                    with self.condition:
                        self.error = error
            if synth is not None and hasattr(synth, "close"):
                try:
                    synth.close()
                except Exception as exc:
                    error = f"{error + '; ' if error else ''}stereo cleanup: {type(exc).__name__}: {exc}"
                    with self.condition:
                        self.error = error
            if metrics is not None:
                if engine is not None and hasattr(engine, "close"):
                    try:
                        engine.close()
                    except Exception as exc:
                        error = f"{error + '; ' if error else ''}depth cleanup: {type(exc).__name__}: {exc}"
                        with self.condition:
                            self.error = error
                metrics.finish({"error": error, "scope": "AI worker only; excludes presentation/network/Quest",
                                "depth_execution": self.execution_status,
                                "stereo_method": getattr(synth, "stereo_method", "backward") if synth else None,
                                "depth_refinement": getattr(synth, "depth_refinement", "none") if synth else None,
                                "colour_precision": getattr(synth, "colour_precision", "uint8") if synth else None,
                                "disparity_profile": getattr(synth, "disparity_profile", "linear") if synth else None,
                                "pending_frames_overwritten": self.overwritten})

    def close(self):
        with self.condition:
            self.closing = True
            self.pending = None
            self.condition.notify_all()
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            raise RuntimeError("AI worker did not exit within 5 seconds; original 2D was independent during the session")


def select_processed(processed, *, requested_mode, revision, current_frame, now_ns, max_age_ms,
                     ai_error=None):
    """Old RGB/depth stay paired; stale results cause current unwarped 2D."""
    if requested_mode == "2d":
        return None, "requested_2d"
    if ai_error:
        return None, "ai_error"
    if processed is None:
        return None, "waiting_for_ai"
    if (processed.stereo.frame_id != processed.source.frame_id
            or processed.stereo.generation != processed.source.geometry_generation):
        return None, "mismatched_rgb_depth"
    if processed.source.captured_ns > now_ns or current_frame.captured_ns > now_ns:
        return None, "future_capture_timestamp"
    if (processed.revision != revision or processed.source.source_id != current_frame.source_id
            or processed.source.geometry_generation != current_frame.geometry_generation):
        return None, "stale_configuration"
    # A static WGC source has no new frames; the exact same RGB/depth pair is
    # still valid. Age bounds apply when a newer source frame actually exists.
    if (processed.source.frame_id != current_frame.frame_id
            and now_ns - processed.source.captured_ns > max_age_ms * 1e6):
        return None, "stale_depth_frame"
    return processed, None


class _DesktopProcessedSelector:
    """Remember actual same-frame observations, without refreshing capture age.

    A static pair may be old by capture timestamp yet was the exact current
    image one presentation tick ago. Briefly retaining that whole pair avoids
    a 2D flash while its replacement is inferred. Only an observed identical
    source object can establish/renew continuity; newer RGB never renews it.
    File playout deliberately continues to use the stateless selector.
    """

    def __init__(self):
        self._pair = None
        self._last_same_frame_ns = None
        self._scope = None
        self._last_now_ns = None
        self._last_current = None
        self._decision = "starting"
        self._hold_age_ms = None

    @staticmethod
    def _source_scope(frame):
        return (type(frame), frame.source_id, frame.geometry_generation,
                getattr(frame, "geometry", None), getattr(frame, "source_identity", None),
                getattr(frame, "timeline_epoch", None), tuple(frame.bgra.shape))

    def _clear_pair(self):
        self._pair = None
        self._last_same_frame_ns = None

    def snapshot(self):
        return {"decision": self._decision, "hold_age_ms": self._hold_age_ms,
                "last_same_frame_observed_ns": self._last_same_frame_ns}

    def select(self, processed, *, requested_mode, revision, current_frame, now_ns, max_age_ms,
               ai_error=None, source_error=None):
        self._hold_age_ms = None
        scope = (requested_mode, revision, self._source_scope(current_frame))
        if scope != self._scope:
            self._clear_pair()
            self._last_current = None
            self._scope = scope

        # Do not turn a clock reversal into a newly observed static frame.
        # Keep the high watermark until the monotonic timeline catches up.
        if self._last_now_ns is not None and now_ns < self._last_now_ns:
            self._clear_pair()
            self._decision = "non_monotonic_presentation_time"
            return None, self._decision
        self._last_now_ns = now_ns

        current_identity = (current_frame.frame_id, current_frame.captured_ns)
        if (self._last_current is not None and
                (current_identity[0] < self._last_current[0]
                 or current_identity[1] < self._last_current[1]
                 or (current_identity[0] == self._last_current[0]
                     and current_identity[1] != self._last_current[1]))):
            self._clear_pair()
            self._decision = "non_monotonic_source_frame"
            return None, self._decision
        self._last_current = current_identity

        selected, reason = select_processed(processed, requested_mode=requested_mode,
            revision=revision, current_frame=current_frame, now_ns=now_ns,
            max_age_ms=max_age_ms, ai_error=ai_error)
        if processed is not self._pair or source_error:
            self._clear_pair()
        if selected is None and reason != "stale_depth_frame":
            self._clear_pair()
            self._decision = reason
            return None, reason

        # The stateless API intentionally has older compatibility semantics.
        # Continuity additionally rejects geometry/identity/timeline changes,
        # even if a source accidentally reuses its numeric frame/generation IDs.
        if processed is not None and self._source_scope(processed.source) != scope[2]:
            self._clear_pair()
            self._decision = "stale_configuration"
            return None, self._decision
        if (processed is not None and processed.source.frame_id == current_frame.frame_id
                and processed.source is not current_frame):
            self._clear_pair()
            self._decision = "mismatched_rgb_depth"
            return None, self._decision

        if selected is not None:
            self._decision = "fresh_pair"
            if (selected.source is current_frame and selected.stereo.mode == "3d"
                    and not source_error):
                self._pair = selected
                self._last_same_frame_ns = now_ns
                self._decision = "observed_current_pair"
            return selected, reason

        if (reason == "stale_depth_frame" and self._pair is processed
                and self._last_same_frame_ns is not None and not source_error):
            age_ns = now_ns - self._last_same_frame_ns
            if 0 <= age_ns <= max_age_ms * 1e6:
                self._decision = "held_matching_pair"
                self._hold_age_ms = age_ns / 1e6
                return self._pair, None

        self._clear_pair()
        self._decision = reason
        return None, reason


def serve(args):
    validate_input_options(args)
    validate_subtitle_options(args)
    validate_window_options(args)
    reuse_payload = getattr(args, "reuse_immutable_payload", False)
    trace_cadence = getattr(args, "trace_cadence", False)
    if type(reuse_payload) is not bool or type(trace_cadence) is not bool:
        raise ValueError("Payload reuse and cadence tracing require explicit booleans")
    if reuse_payload and any(getattr(args, name, None) for name in
            ("file", "file_av_clock", "window", "inline_rect", "subtitles")):
        raise ValueError("Immutable payload reuse requires the enlarged desktop monitor path")
    resize_filter = getattr(args, "resize_filter", "area")
    stereo_method = getattr(args, "stereo_method", "backward")
    fused_output = getattr(args, "fused_stereo_output", False)
    if type(fused_output) is not bool or (fused_output and stereo_method != "forward-cuda"):
        raise ValueError("Fused stereo output requires forward-cuda")
    fused_validation = getattr(args, "fused_forward_validation", False)
    if type(fused_validation) is not bool or (fused_validation and not fused_output):
        raise ValueError("Fused validation requires fused forward output")
    depth_refinement = getattr(args, "depth_refinement", "none")
    colour_precision = getattr(args, "colour_precision", "uint8")
    fused_colour_fit = getattr(args, "fused_colour_fit", False)
    if type(fused_colour_fit) is not bool or (fused_colour_fit and (
            colour_precision != "float" or resize_filter != "bicubic-aa")):
        raise ValueError("Fused colour fit requires float colour and bicubic-aa")
    disparity_profile = getattr(args, "disparity_profile", "linear")
    depth_model = validate_depth_model(getattr(args, "depth_model", DEFAULT_DEPTH_MODEL))
    compare_depth_models = getattr(args, "enable_dad_comparison", False)
    if type(compare_depth_models) is not bool:
        raise ValueError("Depth comparison requires an explicit boolean")
    available_depth_models = list(DEPTH_MODEL_IDS) if compare_depth_models else [depth_model]
    if (compare_depth_models or depth_model != DEFAULT_DEPTH_MODEL) and (
            getattr(args, "file", None) or getattr(args, "window", None) is not None or getattr(args, "inline_rect", None)):
        raise ValueError("Depth model comparison requires enlarged desktop monitor capture")
    depth_execution = getattr(args, "depth_execution", "eager")
    if depth_execution not in ("eager", "cuda-graph"):
        raise ValueError("Unknown depth execution mode")
    if compare_depth_models and depth_execution != "cuda-graph":
        raise ValueError("Live model comparison requires prewarmed cuda-graph execution")
    if getattr(args, "reuse_depth_constants", False) and depth_execution != "cuda-graph":
        raise ValueError("Depth constant reuse requires cuda-graph execution")
    if getattr(args, "fused_depth_resize", False) and depth_execution != "cuda-graph":
        raise ValueError("Fused depth resize requires cuda-graph monitor execution")
    if depth_execution != "eager" and (getattr(args, "file", None) or getattr(args, "window", None) is not None
                                       or getattr(args, "inline_rect", None)):
        raise ValueError("Prewarmed depth execution currently requires desktop monitor capture")
    if disparity_profile not in ("linear", "comfort"):
        raise ValueError("Unknown disparity profile")
    if disparity_profile != "linear" and (getattr(args, "inline_rect", None) or getattr(args, "file_av_clock", False)):
        raise ValueError("Disparity comfort requires the enlarged desktop path")
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
    if stereo_method != "backward" and (getattr(args, "inline_rect", None) or getattr(args, "file_av_clock", False)):
        raise ValueError("Forward stereo requires the enlarged desktop path")
    if resize_filter not in ("area", "bicubic-aa"):
        raise ValueError("Unknown RGB resize filter")
    if resize_filter != "area" and (getattr(args, "inline_rect", None) or getattr(args, "file_av_clock", False)):
        raise ValueError("RGB resize comparison requires the enlarged desktop path")
    if getattr(args, "file_av_clock", False):
        from .file_session import serve_file
        return serve_file(args)
    subtitles = FileSubtitleOverlay.from_args(args)
    directory = Path(args.output)
    if (directory / "status.json").exists() or (directory / "ai").exists():
        raise FileExistsError("Use a new session output directory to preserve previous evidence")
    directory.mkdir(parents=True, exist_ok=True)
    session_id = uuid.uuid4().hex
    validate_request({"session_id": session_id, "request_id": "initial", "mode": args.mode,
                      "disparity": args.disparity}, session_id, args.eye_width)
    torch.set_num_threads(args.cpu_threads)
    bridge_protocol = getattr(args, "bridge_protocol", 2)
    input_opt_in = bool(getattr(args, "enable_input", False))
    last_input_flag, last_input_ns, last_input_revision = False, 0, -1
    input_reason = "starting"
    publisher = FramePublisher() if bridge_protocol == 2 else FramePublisher(version=bridge_protocol)
    worker = None
    capture = None
    cursor = None
    cursor_overlay_ms = 0.0
    capture_poll_ms = publish_ms = 0.0
    cursor_changed_published = 0
    previous_cursor_signature = "hidden"
    error = None
    capture_unavailable = None
    status_write_error = None
    status_write_failures = 0
    requested_mode, disparity, revision = args.mode, args.disparity, 0
    applied_request = seen_request = None
    rejected_request = None
    pending_media_epoch = None
    file_preroll = bool(getattr(args, "file", None) and expected_output_mode(args.mode, args.disparity) == "3d" and not getattr(args, "paused", False))
    current = original = None
    published = attempts = repeated = 0
    ai_completed_published = 0
    last_ai_published_sequence = 0
    presentation_log = None
    capture_timing_log = None
    previous_identity = None
    effective_mode, fallback_reason = "2d", "starting"
    effective_depth_refinement, depth_refinement_reason = "none", None
    effective_colour_precision = "uint8"
    colour_precision_reason = "not_yet_presented" if colour_precision == "float" else None
    effective_disparity_profile = "linear"
    disparity_profile_reason = "not_yet_presented" if disparity_profile != "linear" else None
    effective_convergence = None
    effective_depth_model = None
    desktop_selector = _DesktopProcessedSelector() if not getattr(args, "file", None) else None
    status_path, request_path = directory / "status.json", directory / "request.json"
    started = time.perf_counter()
    last_status = 0

    def status(running):
        processed, ai_error, ready = worker.snapshot() if worker else (None, None, False)
        input_enabled = bool(running and not error and last_input_flag
            and last_input_revision == revision
            and 0 <= time.perf_counter_ns() - last_input_ns <= MAX_INPUT_CAPTURE_AGE_NS)
        return {
            "session_id": session_id, "running": running, "requested_mode": requested_mode,
            "effective_mode": effective_mode, "disparity": disparity, "revision": revision,
            "depth_model": depth_model, "available_depth_models": available_depth_models,
            "effective_depth_model": effective_depth_model,
            "depth_model_switching": bool(compare_depth_models and seen_request != applied_request),
            "eye_width": args.eye_width, "eye_height": args.eye_height,
            "ai_size": args.ai_size,
            "ai_input_shape": list(processed.ai_input_shape) if processed and getattr(processed, 'ai_input_shape', None) else None,
            "rgb_resize_filter": resize_filter,
            "stereo_method": stereo_method,
            "fused_stereo_output": fused_output,
            "fused_forward_validation": fused_validation,
            "fused_colour_fit": fused_colour_fit,
            "latest_ai_output_backend": getattr(processed.stereo, "output_backend", "torch") if processed else None,
            "latest_ai_validation_backend": getattr(processed.stereo, "validation_backend", "not-used") if processed else None,
            "latest_ai_colour_fit_backend": getattr(processed.stereo, "colour_fit_backend", "legacy") if processed else None,
            "latest_ai_colour_fit_reason": getattr(processed.stereo, "colour_fit_reason", None) if processed else None,
            "depth_refinement": depth_refinement,
            "effective_depth_refinement": effective_depth_refinement,
            "depth_refinement_reason": depth_refinement_reason,
            "colour_precision": colour_precision,
            "effective_colour_precision": effective_colour_precision,
            "colour_precision_reason": colour_precision_reason,
            "disparity_profile": disparity_profile,
            "effective_disparity_profile": effective_disparity_profile,
            "disparity_profile_reason": disparity_profile_reason,
            "effective_convergence": effective_convergence,
            "applied_request": applied_request, "published_frames": published,
            "seen_request": seen_request,
            "rejected_request": rejected_request,
            "publication_attempts": attempts, "bridge_skipped": publisher.skipped,
            "reuse_immutable_payload": reuse_payload, "trace_cadence": trace_cadence,
            "publisher": publisher.snapshot() if hasattr(publisher, "snapshot") else None,
            "stream_epoch": publisher.epoch, "bridge_protocol": bridge_protocol,
            "repeated_frames": repeated, "ai_frames": worker.frames if worker else 0,
            "ai_completed_published": ai_completed_published,
            "ai_completed_unpresented": (worker.frames if worker else 0) - ai_completed_published,
            "ai_pending_overwritten": worker.overwritten if worker else 0,
            "ai_completed_queue_overwritten": worker.completed_queue_overwritten if worker else 0,
            "ai_completed_due_skipped": worker.completed_due_skipped if worker else 0,
            "file_playout_ms": getattr(args, "file_playout_ms", 120) if getattr(args, "file", None) else None,
            "capture_overwritten": getattr(capture, "dropped_frames", 0),
            "native_frames_skipped_during_copy": getattr(capture, "native_frames_skipped_during_copy", 0),
            "capture_color_profile": getattr(capture, "color_profile", None),
            "capture_color_processing_ms": getattr(current, "color_processing_ms", 0),
            "cursor": cursor.snapshot() if cursor else None,
            "cursor_overlay_ms": cursor_overlay_ms,
            "capture_poll_ms": capture_poll_ms, "publish_ms": publish_ms,
            "cursor_changed_published": cursor_changed_published,
            "view_layout": "inline" if getattr(args, "inline_rect", None) else "enlarged",
            "inline_rect": getattr(args, "inline_rect", None),
            "media": capture.playback_status() if hasattr(capture, "playback_status") else None,
            "media_preroll": file_preroll,
            "subtitles": subtitles.snapshot() if subtitles else None,
            "ai_ready": ready, "ai_error": ai_error, "error": error or capture_unavailable,
            "depth_execution": getattr(worker, "execution_status", None),
            "capture_unavailable": capture_unavailable,
            "window_capture": capture.status if getattr(args, "window", None) is not None and capture is not None else None,
            "status_write_error": status_write_error,
            "status_write_failures": status_write_failures,
            "fallback_reason": fallback_reason, "updated_monotonic_ns": time.perf_counter_ns(),
            "stereo_continuity": desktop_selector.snapshot() if desktop_selector else None,
            "seconds_since_new_capture": (time.perf_counter_ns() - current.captured_ns) / 1e9 if current else None,
            "quest_display_verified": False, "audio_integrated": False,
            "input_opt_in": input_opt_in, "input_enabled": input_enabled,
            "input_last_published_flag": last_input_flag,
            "input_reason": input_reason if running else "stopped",
            "os_input_verified": False,
        }

    def write_status(running):
        nonlocal status_write_error, status_write_failures
        try:
            atomic_json(status_path, status(running))
            status_write_error = None
        except OSError as exc:
            status_write_failures += 1
            message = f"{type(exc).__name__}: {exc}"
            if status_write_error != message:
                print(f"Session status write failed: {message}", file=sys.stderr, flush=True)
            status_write_error = message

    def submit_current(frame):
        # Preserve existing injected/legacy worker signatures for the default.
        if compare_depth_models:
            return worker.submit(frame, revision, disparity, disparity_profile=disparity_profile,
                                 depth_model=depth_model)
        if disparity_profile == "linear":
            return worker.submit(frame, revision, disparity)
        else:
            return worker.submit(frame, revision, disparity, disparity_profile=disparity_profile)

    try:
        presentation_log = (directory / "presentation.jsonl").open("x", encoding="utf-8", buffering=65536)
        if trace_cadence:
            capture_timing_log = (directory / "capture-timing.jsonl").open("x", encoding="utf-8", buffering=65536)
        write_status(True)
        atomic_json(ARTIFACT_DIR / "active-session.json", {"directory": str(directory.resolve()),
                                                          "session_id": session_id})
        print(json.dumps({"session_directory": str(directory.resolve()), "session_id": session_id}), flush=True)
        worker_options = {"inline_rect": parse_rect(args.inline_rect)} if getattr(args, "inline_rect", None) else {}
        if trace_cadence:
            worker_options["trace_cadence"] = True
        if depth_execution == "cuda-graph":
            # Capture is process-wide sensitive: prepare the sole graph before
            # starting any WGC/CUDA capture thread. Later size changes use eager.
            from .capture import list_monitors
            bounds = next(m.bounds for m in list_monitors() if m.index == args.monitor)
            if args.rect:
                bounds = parse_rect(args.rect)
            engine_options = {"execution_mode": depth_execution}
            if getattr(args, "reuse_depth_constants", False):
                engine_options["reuse_constants"] = True
            if getattr(args, "fused_depth_resize", False):
                engine_options["fused_resize"] = True
            worker_options["engine_factory"] = partial(DepthEngine, **engine_options)
            worker_options["engine_prepare_size"] = (bounds.width, bounds.height)
            worker_options["isolate_cuda"] = True
        if compare_depth_models:
            if depth_execution != "cuda-graph":
                raise ValueError("Live model comparison requires prewarmed cuda-graph execution")
            from .depth_choice import SelectableDepthEngine
            worker_options["engine_factory"] = partial(SelectableDepthEngine, model_id=depth_model,
                                                       model_ids=tuple(available_depth_models), **engine_options)
        elif depth_model != DEFAULT_DEPTH_MODEL:
            worker_options["engine_factory"] = partial(DepthEngine, model_id=depth_model,
                                                       **(engine_options if depth_execution == "cuda-graph" else {}))
        if resize_filter != "area" or stereo_method != "backward" or depth_refinement != "none" or colour_precision != "uint8":
            output_options = {"fused_output": True} if fused_output else {}
            if fused_validation:
                output_options["fused_validation"] = True
            if fused_colour_fit:
                output_options["fused_colour_fit"] = True
            worker_options["synth_factory"] = partial(StereoSynthesizer, resize_filter=resize_filter,
                                                       stereo_method=stereo_method,
                                                       depth_refinement=depth_refinement, colour_precision=colour_precision,
                                                       **output_options)
        worker = LatestAIWorker(eye_width=args.eye_width, eye_height=args.eye_height,
                                ai_size=args.ai_size, directory=directory / "ai",
                                buffered=bool(getattr(args, "file", None)), **worker_options)
        if depth_execution == "cuda-graph":
            deadline = time.monotonic() + 30
            while True:
                if request_path.exists():
                    try:
                        request = validate_request(read_json(request_path, max_bytes=8192),
                                                   session_id, args.eye_width)
                    except (ValueError, OSError):
                        # Other/invalid controls still go through the ordinary
                        # control loop after preparation. Only a valid Stop can
                        # cancel startup, with the same priority as live Stop.
                        pass
                    else:
                        if request.get("stop"):
                            seen_request = request["request_id"]
                            raise _PreparationCancelled()
                _, prepare_error, prepare_ready = worker.snapshot()
                if prepare_error:
                    raise RuntimeError(f"Depth execution preparation failed: {prepare_error}")
                if prepare_ready:
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError("Depth execution preparation exceeded 30 seconds")
                write_status(True)
                time.sleep(.05)
        mono = StereoSynthesizer(args.eye_width, args.eye_height, disparity_px=0, resize_filter=resize_filter,
                                 stereo_method=stereo_method, depth_refinement=depth_refinement,
                                 colour_precision=colour_precision)
        mono.disparity_profile = disparity_profile
        if getattr(args, "file", None):
            from .media import FileVideoSource
            source_provider = FileVideoSource(args.file, paused=getattr(args, "paused", False) or file_preroll)
        elif getattr(args, "window", None) is not None:
            source_provider = selected_window_capture(args)
        else:
            capture_options = {"experimental_hdr": True, "hdr_tonemap": getattr(args, "hdr_tonemap", None) or "fused"} if getattr(args, "experimental_hdr", False) else {}
            source_provider = GPUDesktopCapture(monitor=args.monitor, rect=parse_rect(args.rect) if args.rect else None,
                                                **capture_options)
        with source_provider as capture:
            # The AI source remains cursor-free. Refresh the actual Windows
            # cursor at presentation cadence, including static captured frames.
            cursor = DesktopCursorOverlay.from_capture(capture,
                enabled=not getattr(args, "hide_cursor", False) and not getattr(args, "file", None),
                # This desktop branch only publishes independently owned mono
                # or completed AI outputs; no later stage mutates their pixels.
                immutable_base=not any(getattr(args, name, None) for name in ("file", "window", "inline_rect")))
            pacer = FramePacer(args.fps)
            capture_attempts = 0
            while args.seconds == 0 or time.perf_counter() - started < args.seconds:
                tick = time.perf_counter_ns()
                if request_path.exists():
                    try:
                        request = validate_request(read_json(request_path, max_bytes=8192),
                                                   session_id, args.eye_width)
                    except (ValueError, OSError) as exc:
                        error = f"control rejected: {exc}"
                    else:
                        if request["request_id"] not in (seen_request, rejected_request):
                            if request.get("stop"):
                                seen_request = request["request_id"]
                                break
                            if request.get("expected_revision", revision) != revision:
                                rejected_request = request["request_id"]
                                error = "control rejected: revision conflict; reload current state"
                                last_status = 0
                                continue
                            next_profile = request.get("disparity_profile", disparity_profile)
                            next_depth_model = request.get("depth_model", depth_model)
                            if next_depth_model not in available_depth_models:
                                rejected_request = request["request_id"]
                                error = "control rejected: depth model was not prepared before capture"
                                last_status = 0
                                continue
                            if next_profile != "linear" and getattr(args, "inline_rect", None):
                                rejected_request = request["request_id"]
                                error = "control rejected: disparity comfort requires the enlarged desktop path"
                                last_status = 0
                                continue
                            if "paused" in request or "seek_seconds" in request:
                                try:
                                    if not hasattr(capture, "control"):
                                        raise ValueError("Playback controls require a file source")
                                    capture.control(paused=request.get("paused"), seek_seconds=request.get("seek_seconds"))
                                    if "seek_seconds" in request:
                                        pending_media_epoch = capture.playback_status()["timeline_epoch"]
                                except (ValueError, RuntimeError) as exc:
                                    error = f"playback control rejected: {exc}"
                                    rejected_request = request["request_id"]
                                    continue
                            requested_mode, disparity = request["mode"], request["disparity"]
                            disparity_profile = next_profile
                            depth_model = next_depth_model
                            mono.disparity_profile = disparity_profile
                            original = None
                            if file_preroll and ("paused" in request or expected_output_mode(requested_mode, disparity) == "2d"):
                                if "paused" not in request:
                                    capture.control(paused=False)
                                file_preroll = False
                            revision += 1
                            seen_request = request["request_id"]
                            last_status = 0
                            error = None
                            if current is not None and expected_output_mode(requested_mode, disparity) == "3d":
                                submit_current(current)
                capture_started = time.perf_counter_ns()
                capture_attempts += 1
                capture_outcome, captured_this_tick, submitted_ns = "error", None, None
                try:
                    first_frame_timeout = .1 if getattr(args, "window", None) is not None else 3
                    poll_capture = getattr(capture, "try_grab", None)
                    if current is not None and callable(poll_capture):
                        # Do not delay presentation of a completed AI pair for
                        # another WGC event. None preserves the last source ID
                        # and timestamp; native errors still reach cleanup.
                        frame = poll_capture()
                        if frame is None:
                            raise TimeoutError("No new completed capture")
                    else:
                        frame = capture.grab(timeout_seconds=.01 if current is not None else first_frame_timeout)
                except WindowCaptureUnavailable as exc:
                    capture_outcome = "unavailable"
                    # Minimize/resize/ambiguous geometry is retryable. Keep
                    # controls alive and visibly flatten any retained old RGB.
                    capture_unavailable = str(exc) or "window_unavailable"
                    fallback_reason = "source_unavailable"
                    effective_mode = "2d"
                    if current is None:
                        if time.perf_counter() - last_status > .25:
                            write_status(True)
                            last_status = time.perf_counter()
                        pacer.wait_next()
                        continue
                except TimeoutError:
                    capture_outcome = "timeout"
                    if current is None:
                        if getattr(args, "window", None) is None:
                            raise
                        capture_unavailable = "awaiting_first_window_frame"
                        fallback_reason = "source_unavailable"
                        effective_mode = "2d"
                        if time.perf_counter() - last_status > .25:
                            write_status(True)
                            last_status = time.perf_counter()
                        pacer.wait_next()
                        continue
                else:
                    capture_outcome, captured_this_tick = "frame", frame
                    capture_unavailable = None
                    current = frame
                    original = None
                    if expected_output_mode(requested_mode, disparity) == "3d":
                        submitted_ns = submit_current(frame)
                finally:
                    capture_completed_ns = time.perf_counter_ns()
                    capture_poll_ms = (capture_completed_ns - capture_started) / 1e6
                    if capture_timing_log is not None:
                        capture_timing_log.write(json.dumps({"attempt": capture_attempts, "tick_started_ns": tick,
                            "capture_started_ns": capture_started, "capture_completed_ns": capture_completed_ns,
                            "capture_poll_ms": capture_poll_ms, "outcome": capture_outcome,
                            "frame_id": captured_this_tick.frame_id if captured_this_tick else None,
                            "capture_ns": captured_this_tick.captured_ns if captured_this_tick else None,
                            "source_id": captured_this_tick.source_id if captured_this_tick else None,
                            "generation": captured_this_tick.geometry_generation if captured_this_tick else None,
                            "revision": revision, "submitted_ns": submitted_ns}) + "\n")
                if current is None:
                    continue
                processed, ai_error, ready = worker.snapshot()
                snapshot_ns = time.perf_counter_ns()
                if file_preroll and ai_error:
                    # Only our startup hold is released. Explicit user pause
                    # already cancels preroll and must never auto-resume.
                    capture.control(paused=False)
                    file_preroll = False
                    last_status = 0
                if getattr(args, "file", None):
                    processed = worker.due_for_presentation(time.perf_counter_ns(),
                        round(getattr(args, "file_playout_ms", 120) * 1e6), revision, current,
                        timestamp_for=capture.host_presentation_ns)
                selector = desktop_selector.select if desktop_selector else select_processed
                selected, fallback_reason = selector(
                    processed, requested_mode=expected_output_mode(requested_mode, disparity), revision=revision, current_frame=current,
                    now_ns=time.perf_counter_ns(), max_age_ms=args.max_frame_age_ms, ai_error=ai_error,
                    **({"source_error": error or capture_unavailable} if desktop_selector else {}))
                if capture_unavailable:
                    selected, fallback_reason = None, "source_unavailable"
                if selected is None:
                    if original is None:
                        original = mono.original_2d(current.bgra, frame_id=current.frame_id,
                                                    generation=current.geometry_generation)
                    output, source = original, current
                    if ai_error:
                        fallback_reason = "ai_error"
                else:
                    output, source = selected.stereo, selected.source
                effective_mode = output.mode
                effective_depth_refinement = getattr(output, "depth_refinement", "none")
                depth_refinement_reason = getattr(output, "depth_refinement_reason", None)
                effective_colour_precision = getattr(output, "colour_precision", "uint8")
                colour_precision_reason = getattr(output, "colour_precision_reason", None)
                effective_disparity_profile = getattr(output, "disparity_profile", "linear")
                disparity_profile_reason = getattr(output, "disparity_profile_reason", None)
                effective_convergence = getattr(output, "effective_convergence", None)
                if getattr(args, "file", None):
                    scheduled_ns = capture.host_presentation_ns(source)
                    if scheduled_ns is not None:
                        source = replace(source, captured_ns=scheduled_ns)
                identity = (source.frame_id, source.geometry_generation, output.mode, revision)
                bounds = source.geometry.bounds
                input_decision = frame_input_decision(opt_in=input_opt_in, protocol=bridge_protocol,
                    requested_mode=requested_mode, output=output, source=source, current_frame=current,
                    now_ns=time.perf_counter_ns(), error=error or capture_unavailable)
                input_reason = input_decision.reason
                if subtitles:
                    media = capture.playback_status()
                    # Decoder exhaustion alone cannot establish the final
                    # frame's duration. Match the common-clock end policy.
                    eof = bool(media["eof"] and media["duration_seconds"] is not None
                               and media["timeline_epoch"] == source.timeline_epoch)
                    output = subtitles.composite(output, source.pts_ns, eof=eof)
                cursor_started = time.perf_counter_ns()
                output = cursor.sample_and_composite(output, source)
                cursor_overlay_ms = (time.perf_counter_ns() - cursor_started) / 1e6
                cursor_state = cursor.snapshot()
                publish_started = time.perf_counter_ns()
                success = publisher.publish(output.bgra, frame_id=attempts + 1, capture_ns=source.captured_ns,
                                  generation=source.geometry_generation,
                                  flags=FULL_SBS | (0 if getattr(args, "file", None) or capture_unavailable else CAPTURE_RECEIPT)
                                  | (ORIGINAL_2D if output.mode == "2d" else 0)
                                  | (INPUT_ENABLED if input_decision.enabled else 0),
                                  source_rect=(bounds.left, bounds.top, bounds.width, bounds.height),
                                  content_rect=output.content_rect,
                                  **({"source_identity": source.source_identity} if bridge_protocol == 3 else {}),
                                  **({"immutable_payload": True} if reuse_payload else {}))
                publish_ms = (time.perf_counter_ns() - publish_started) / 1e6
                attempts += 1
                presentation_log.write(json.dumps({"attempt": attempts, "published": success,
                    "host_ns": time.perf_counter_ns(), "source_frame_id": source.frame_id,
                    "source_id": source.source_id,
                    "source_ns": source.captured_ns, "generation": source.geometry_generation,
                    "revision": revision, "mode": output.mode,
                    "stereo_method": stereo_method,
                    "depth_refinement": depth_refinement,
                    "effective_depth_refinement": effective_depth_refinement,
                    "depth_refinement_reason": depth_refinement_reason,
                    "colour_precision": colour_precision,
                    "effective_colour_precision": effective_colour_precision,
                    "colour_precision_reason": colour_precision_reason,
                    "disparity_profile": disparity_profile,
                    "effective_disparity_profile": effective_disparity_profile,
                    "disparity_profile_reason": disparity_profile_reason,
                    "effective_convergence": effective_convergence,
                    "ai_completion_sequence": selected.completion_sequence if selected else None,
                    "fallback_reason": fallback_reason,
                    "capture_unavailable": capture_unavailable,
                    "current_source_frame_id": current.frame_id, "current_source_ns": current.captured_ns,
                    "stereo_continuity": desktop_selector.snapshot() if desktop_selector else None,
                    "input_flag": input_decision.enabled, "input_reason": input_decision.reason,
                    "subtitles": subtitles.presentation_record() if subtitles else None,
                    "cursor": cursor_state, "cursor_overlay_ms": cursor_overlay_ms,
                    "capture_poll_ms": capture_poll_ms, "publish_ms": publish_ms,
                    "publisher": publisher.snapshot() if hasattr(publisher, "snapshot") else None,
                    **({"tick_started_ns": tick, "capture_started_ns": capture_started,
                        "snapshot_ns": snapshot_ns, "publish_started_ns": publish_started,
                        "selected_completed_ns": getattr(selected, "completed_ns", 0) if selected else None,
                        "selected_available_ns": getattr(selected, "available_ns", 0) if selected else None,
                        "pacing": pacer.snapshot() if hasattr(pacer, "snapshot") else None} if trace_cadence else {}),
                    "pts_ns": getattr(source, "pts_ns", None)}) + "\n")
                if success:
                    # Cursor motion does not turn an old RGB/depth frame into
                    # a new AI result; keep the existing repeat count intact.
                    signature = cursor_state.get("visual_signature")
                    if signature != previous_cursor_signature:
                        cursor_changed_published += 1
                    previous_cursor_signature = signature
                    last_input_flag = input_decision.enabled
                    last_input_ns, last_input_revision = source.captured_ns, revision
                    if selected is not None and selected.completion_sequence != last_ai_published_sequence:
                        ai_completed_published += 1
                        last_ai_published_sequence = selected.completion_sequence
                    if file_preroll and output.mode == "3d":
                        capture.control(paused=False)
                        file_preroll = False
                        last_status = 0
                    if identity == previous_identity:
                        repeated += 1
                    previous_identity = identity
                    published += 1
                    effective_depth_model = getattr(selected, "depth_model", None) if output.mode == "3d" else None
                    if (output.mode == expected_output_mode(requested_mode, disparity) and
                            (pending_media_epoch is None or source.geometry_generation == pending_media_epoch)):
                        if applied_request != seen_request:
                            last_status = 0
                        applied_request = seen_request
                        pending_media_epoch = None
                if time.perf_counter() - last_status > .25:
                    presentation_log.flush()
                    write_status(True)
                    last_status = time.perf_counter()
                pacer.wait_next()
    except _PreparationCancelled:
        # CUDA preparation itself cannot be interrupted safely. close() below
        # must finish the worker; a timeout remains an explicit session error.
        pass
    except KeyboardInterrupt:
        error = "user_interrupted"
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        if cursor:
            try:
                cursor.close()
            except Exception as exc:
                error = f"{error or ''}; cursor cleanup: {exc}"
        publisher.close()
        if worker:
            try:
                worker.close()
            except Exception as exc:
                error = f"{error or ''}; cleanup: {exc}"
        for label, log in (("presentation log", presentation_log),
                           ("capture timing", capture_timing_log)):
            if log is not None:
                try:
                    log.close()
                except Exception as exc:
                    error = f"{error + '; ' if error else ''}{label} cleanup: {type(exc).__name__}: {exc}"
        write_status(False)
    return status(False)

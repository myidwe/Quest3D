"""File video presentation using the common RGB/PCM clock.

The explicit candidate path publishes real RGB/AI frames. PCM remains bounded
and unconsumed until a native sink is connected; elapsed time is never an ack.
"""
from pathlib import Path
import json
import threading
import time
import uuid

import numpy as np
import torch

from .bridge import FULL_SBS, ORIGINAL_2D, FramePublisher
from .depth import DepthEngine
from .file_audio import FileAudioLink
from .media import seconds_ns
from .media_playout import FileAVPlayback
from .metrics import Metrics
from .paths import ARTIFACT_DIR
from .session_control import atomic_json, expected_output_mode, read_json, validate_request
from .stereo import StereoSynthesizer
from .source_identity import SourceIdentity, SourceKind
from .geometry import ScreenRect


class FileAIWorker:
    """Infer bounded future RGB without advancing the presentation clock."""
    def __init__(self, player, *, eye_width, eye_height, ai_size, directory,
                 mode="2d", disparity=12, engine_factory=DepthEngine,
                 synth_factory=StereoSynthesizer, gpu_upload=True):
        self.player = player
        self.condition = threading.Condition()
        self.mode, self.disparity, self.revision = expected_output_mode(mode, disparity), disparity, 0
        self.closing = self.ready = False
        self.error = None
        self.frames = self.accepted = 0
        self.eye_width, self.eye_height, self.ai_size = eye_width, eye_height, ai_size
        self.directory = Path(directory)
        self.engine_factory, self.synth_factory = engine_factory, synth_factory
        self.gpu_upload = gpu_upload
        self.thread = threading.Thread(target=self._run, name="Quest3D file AI", daemon=True)
        self.thread.start()

    def configure(self, *, mode, disparity, revision):
        with self.condition:
            self.player.set_ai_revision(revision)
            self.mode, self.disparity, self.revision = expected_output_mode(mode, disparity), disparity, revision
            self.condition.notify_all()

    def snapshot(self):
        with self.condition:
            return {"ready": self.ready, "error": self.error, "frames": self.frames,
                    "accepted": self.accepted}

    def _run(self):
        metrics = None
        try:
            metrics = Metrics(self.directory)
            engine = self.engine_factory(self.ai_size)
            synth = self.synth_factory(self.eye_width, self.eye_height, disparity_px=0)
            staging = None
            with self.condition:
                self.ready = True
            while True:
                with self.condition:
                    self.condition.wait_for(lambda: self.closing or self.mode == "3d")
                    if self.closing:
                        break
                try:
                    request = self.player.next_ai_frame(timeout_seconds=.1)
                except TimeoutError:
                    continue
                with self.condition:
                    if self.closing:
                        break
                    if self.mode != "3d" or request.ai_revision != self.revision:
                        continue
                    disparity = self.disparity
                frame = request.frame
                started = time.perf_counter_ns()
                if self.gpu_upload:
                    # One owned upload serves both GPU preprocessing and stereo.
                    # The serial worker retains staging/source until both stages
                    # finish on the same Torch stream before reusing staging.
                    if staging is None or tuple(staging.shape) != frame.bgra.shape:
                        staging = torch.empty(frame.bgra.shape, dtype=torch.uint8, pin_memory=True)
                    np.copyto(staging.numpy(), frame.bgra)
                    pixels = staging.to("cuda", non_blocking=True)
                    torch.cuda.current_stream().synchronize()
                else:
                    pixels = frame.bgra
                uploaded = time.perf_counter_ns()
                depth = engine.infer(pixels, frame_id=frame.frame_id, generation=frame.epoch)
                synth.disparity_px = disparity
                stereo_started = time.perf_counter_ns()
                stereo = synth.synthesize(pixels, depth, frame_id=frame.frame_id, generation=frame.epoch)
                completed = time.perf_counter_ns()
                accepted = self.player.submit_ai(request, stereo)
                metrics.add({"frame_id": frame.frame_id, "pts_ns": frame.pts_ns,
                             "epoch": frame.epoch, "revision": request.ai_revision,
                             "accepted_for_presentation": accepted, "completed_ns": completed,
                             "upload_ms": (uploaded - started) / 1e6,
                             "preprocess_ms": depth.preprocess_ms, "inference_ms": depth.inference_ms,
                             "stereo_readback_ms": (completed - stereo_started) / 1e6,
                             "pc_total_ms": (completed - started) / 1e6, "warmup": self.frames < 5})
                with self.condition:
                    self.frames += 1
                    self.accepted += int(accepted)
        except Exception as exc:
            with self.condition:
                self.error = f"{type(exc).__name__}: {exc}"
        finally:
            if metrics is not None:
                metrics.finish({"error": self.error, "scope": "future file AI; not presentation FPS or A/V latency",
                                "accepted_results": self.accepted, "gpu_source_upload": self.gpu_upload})

    def close(self):
        with self.condition:
            self.closing = True
            self.condition.notify_all()
        self.thread.join(5)
        if self.thread.is_alive():
            raise RuntimeError("File AI worker did not stop within five seconds")


def serve_file(args):
    """Common-clock candidate; desktop/photo paths retain the existing session."""
    from .subtitle_session import FileSubtitleOverlay
    from .input_policy import validate_input_options
    validate_input_options(args)
    subtitles = FileSubtitleOverlay.from_args(args)
    directory = Path(args.output)
    if (directory / "status.json").exists() or (directory / "ai").exists():
        raise FileExistsError("Use a new session output directory to preserve previous evidence")
    directory.mkdir(parents=True, exist_ok=True)
    session_id = uuid.uuid4().hex
    validate_request({"session_id": session_id, "request_id": "initial", "mode": args.mode,
                      "disparity": args.disparity}, session_id, args.eye_width)
    lookahead = getattr(args, "file_playout_ms", 120)
    if lookahead != int(lookahead) or not 1 <= lookahead <= 500:
        raise ValueError("The common file clock requires integral lookahead of 1..500 ms")
    torch.set_num_threads(args.cpu_threads)
    bridge_protocol = getattr(args, "bridge_protocol", 2)
    publisher = FramePublisher() if bridge_protocol == 2 else FramePublisher(version=bridge_protocol)
    player = FileAVPlayback(args.file, paused=getattr(args, "paused", False), lookahead_ms=int(lookahead))
    worker = None
    audio = None
    audio_requested = getattr(args, "file_native_pcm", False)
    mode, disparity, revision = args.mode, args.disparity, 0
    seen_request = applied_request = rejected_request = None
    pending_epoch = None
    attempts = published = repeated = unique_ai_published = 0
    previous_identity = last_ai_identity = None
    effective_mode, fallback_reason = "2d", "starting"
    error = status_error = None
    status_failures = 0
    status_path, request_path = directory / "status.json", directory / "request.json"
    log = None
    final_media = None
    started = time.perf_counter()
    last_status = 0

    def status(running):
        ai = worker.snapshot() if worker else {"ready": False, "error": None, "frames": 0, "accepted": 0}
        audio_state = audio.snapshot() if audio else None
        media = dict(final_media if final_media is not None else player.status())
        media["native_output_integrated"] = audio_state["audio_integrated"] if audio_state else False
        return {"session_id": session_id, "running": running, "requested_mode": mode,
                "effective_mode": effective_mode, "disparity": disparity, "revision": revision,
                "eye_width": args.eye_width, "eye_height": args.eye_height,
                "applied_request": applied_request, "seen_request": seen_request,
                "rejected_request": rejected_request, "published_frames": published,
                "publication_attempts": attempts, "bridge_skipped": publisher.skipped,
                "stream_epoch": publisher.epoch, "bridge_protocol": bridge_protocol, "repeated_frames": repeated,
                "ai_frames": ai["frames"], "ai_accepted": ai["accepted"],
                "ai_completed_published": unique_ai_published, "ai_ready": ai["ready"],
                "ai_error": ai["error"], "error": error, "fallback_reason": fallback_reason,
                "media": media, "media_preroll": media["preroll"],
                "subtitles": subtitles.snapshot() if subtitles else None,
                "file_clock": "common-av-candidate", "file_lookahead_ms": lookahead,
                "view_layout": "enlarged", "input_enabled": False,
                "audio_requested": audio_requested,
                "audio_integrated": audio_state["audio_integrated"] if audio_state else False,
                "audio": audio_state,
                "pcm_consumption": "native Opus encode/handoff" if audio else "none; no native sink",
                "quest_display_verified": False, "updated_monotonic_ns": time.perf_counter_ns(),
                "status_write_error": status_error, "status_write_failures": status_failures}

    def write_status(running):
        nonlocal status_error, status_failures
        try:
            atomic_json(status_path, status(running))
            status_error = None
        except OSError as exc:
            status_error = f"{type(exc).__name__}: {exc}"
            status_failures += 1

    try:
        if audio_requested:
            audio = FileAudioLink(player)
            audio_state = audio.snapshot()
            atomic_json(directory / "audio-channel.json", {"version": 1, "session_id": session_id,
                "file_session_id": player.session_id, "channel": audio_state["channel"],
                "producer_pid": audio_state["producer_pid"],
                "producer_creation_filetime": audio_state["producer_creation_filetime"]})
        log = (directory / "presentation.jsonl").open("x", encoding="utf-8", buffering=65536)
        write_status(True)
        atomic_json(ARTIFACT_DIR / "active-session.json", {"directory": str(directory.resolve()), "session_id": session_id})
        print(json.dumps({"session_directory": str(directory.resolve()), "session_id": session_id}), flush=True)
        mono = StereoSynthesizer(args.eye_width, args.eye_height, disparity_px=0)
        with player:
            worker = FileAIWorker(player, eye_width=args.eye_width, eye_height=args.eye_height,
                                  ai_size=args.ai_size, directory=directory / "ai", mode=mode, disparity=disparity)
            original = original_identity = None
            try:
                while args.seconds == 0 or time.perf_counter() - started < args.seconds:
                    tick = time.perf_counter_ns()
                    if request_path.exists():
                        try:
                            request = validate_request(read_json(request_path, max_bytes=8192), session_id, args.eye_width)
                        except (ValueError, OSError) as exc:
                            error = f"control rejected: {exc}"
                        else:
                            if request["request_id"] not in (seen_request, rejected_request):
                                if "disparity_profile" in request:
                                    # This excluded path has no profile implementation. Reject
                                    # the whole request before playback/configuration or stop.
                                    rejected_request = request["request_id"]
                                    error = "control rejected: disparity profiles are unsupported in file AV playback"
                                    last_status = 0
                                elif request.get("stop"):
                                    seen_request = request["request_id"]
                                    break
                                elif request.get("expected_revision", revision) != revision:
                                    rejected_request = request["request_id"]
                                    error = "control rejected: revision conflict; reload current state"
                                    last_status = 0
                                else:
                                    try:
                                        # No PCM is issued on this path. A future native sink must
                                        # replace direct controls with the confirmed flush barrier.
                                        if "seek_seconds" in request:
                                            if audio: audio.require_direct_control_safe()
                                            pending_epoch = player.seek(seconds_ns(request["seek_seconds"]), paused=request.get("paused"))
                                            original = original_identity = None
                                        elif "paused" in request:
                                            if audio: audio.require_direct_control_safe()
                                            player.set_paused(request["paused"])
                                    except (ValueError, RuntimeError) as exc:
                                        rejected_request = request["request_id"]
                                        error = f"playback control rejected: {exc}"
                                    else:
                                        mode, disparity = request["mode"], request["disparity"]
                                        revision += 1
                                        worker.configure(mode=mode, disparity=disparity, revision=revision)
                                        seen_request, error = request["request_id"], None
                                    last_status = 0
                    ai = worker.snapshot()
                    target_mode = expected_output_mode(mode, disparity)
                    require_ai = target_mode == "3d" and not ai["error"]
                    clock = player.clock_snapshot()
                    if audio and player.has_audio and not audio.error:
                        try:
                            if audio.ready() and clock.preroll and player.ready_for_start(require_ai=require_ai):
                                player.start(require_ai=require_ai)
                            clock = player.clock_snapshot()
                            if not clock.preroll and not player.finished and (clock.epoch, clock.generation) != audio.scope:
                                audio.adopt_started_clock()
                            audio.pump()
                        except Exception as exc:
                            audio.fail(exc)
                            if player.status()["pending_transition"] is None:
                                player.begin_transition(paused=True)
                    elif (audio is None or not player.has_audio) and clock.preroll and player.ready_for_start(require_ai=require_ai):
                        player.start(require_ai=require_ai)
                    presentation = player.tick(mode=target_mode if not ai["error"] else "2d")
                    source = presentation.source
                    fallback_reason = "ai_error" if ai["error"] and mode == "3d" else presentation.fallback_reason
                    if source is not None:
                        key = (source.epoch, source.frame_id)
                        if presentation.processed is not None:
                            output = presentation.processed
                        else:
                            if original_identity != key:
                                original = mono.original_2d(source.bgra, frame_id=source.frame_id, generation=source.epoch)
                                original_identity = key
                            output = original
                        effective_mode = output.mode
                        identity = (*key, output.mode, revision)
                        source_ns = presentation.clock.deadline(source.pts_ns)
                        height, width = source.bgra.shape[:2]
                        identity_fields = {}
                        if bridge_protocol == 3:
                            identity_fields["source_identity"] = SourceIdentity(SourceKind.VIDEO, 0,
                                (int(player.session_id, 16) & ((1 << 64) - 1)) or 1, 0, 0, ScreenRect(0, 0, width, height))
                        if subtitles:
                            media = player.status()
                            eof = bool(media["video_presentation_finished"] and media["epoch"] == source.epoch)
                            output = subtitles.composite(output, source.pts_ns, eof=eof)
                        success = publisher.publish(output.bgra, frame_id=attempts + 1, capture_ns=source_ns,
                                                    generation=source.epoch, flags=FULL_SBS | (ORIGINAL_2D if output.mode == "2d" else 0),
                                                    source_rect=(0, 0, width, height), content_rect=output.content_rect,
                                                    **identity_fields)
                        attempts += 1
                        log.write(json.dumps({"attempt": attempts, "published": success, "host_ns": time.perf_counter_ns(),
                            "source_frame_id": source.frame_id, "pts_ns": source.pts_ns, "source_ns": source_ns,
                            "generation": source.epoch, "clock_generation": presentation.clock.generation,
                            "selected_ns": presentation.selected_ns,
                            "media_position_ns": presentation.clock.position(presentation.selected_ns), "revision": revision,
                            "mode": output.mode, "fallback_reason": fallback_reason,
                            "subtitles": subtitles.presentation_record() if subtitles else None,
                            "preroll": presentation.clock.preroll, "paused": presentation.clock.paused,
                            "lateness_ns": presentation.lateness_ns}) + "\n")
                        if success:
                            published += 1
                            repeated += int(identity == previous_identity)
                            previous_identity = identity
                            if output.mode == "3d" and identity != last_ai_identity:
                                unique_ai_published += 1
                                last_ai_identity = identity
                            if output.mode == target_mode and (pending_epoch is None or source.epoch == pending_epoch):
                                if applied_request != seen_request:
                                    last_status = 0
                                applied_request, pending_epoch = seen_request, None
                    player.finish_if_drained()
                    if time.perf_counter() - last_status >= .25:
                        log.flush()
                        write_status(True)
                        last_status = time.perf_counter()
                    remaining = 1 / args.fps - (time.perf_counter_ns() - tick) / 1e9
                    if remaining > 0:
                        time.sleep(remaining)
            finally:
                # Stop inference before freeing its retained source frames.
                worker.close()
                final_media = player.status()
    except KeyboardInterrupt:
        error = "user_interrupted"
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        publisher.close()
        if audio is not None:
            audio.close()
        if log is not None:
            log.close()
        write_status(False)
    return status(False)

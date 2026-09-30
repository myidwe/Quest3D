"""One file presentation clock, bounded future RGB/PCM, and consumed-PCM acks.

No sound output, native transport or GPU inference is performed here. AI callers
receive immutable RGB identities and return results for those exact identities.
"""
from collections import deque
from dataclasses import dataclass, replace
from fractions import Fraction
import math
import threading
import time
import uuid

import numpy as np

from .media import MediaReader, PlaybackTimeline
from .media_audio import AudioPCM, MediaAudioReader, RATE, _integer
from .stereo import StereoFrame


@dataclass(frozen=True, slots=True)
class FileClock:
    session_id: str
    video_origin: Fraction | None
    host_anchor_ns: int
    media_anchor_ns: int
    paused: bool
    epoch: int
    generation: int
    preroll: bool

    def position(self, now_ns):
        return self.media_anchor_ns + (0 if self.paused else max(0, now_ns - self.host_anchor_ns))

    def deadline(self, pts_ns):
        return self.host_anchor_ns + pts_ns - self.media_anchor_ns


@dataclass(frozen=True, slots=True)
class FileRGB:
    bgra: np.ndarray
    pts_ns: int
    epoch: int
    frame_id: int


@dataclass(frozen=True, slots=True)
class FileAIRequest:
    frame: FileRGB
    ai_revision: int


@dataclass(frozen=True, slots=True)
class FileTransition:
    session_id: str
    transition_id: str
    epoch: int
    generation: int
    frozen_position_ns: int
    paused: bool
    seek_ns: int | None


@dataclass(frozen=True, slots=True)
class FilePresentation:
    source: FileRGB | None
    processed: StereoFrame | None
    mode: str
    fallback_reason: str | None
    clock: FileClock
    lateness_ns: int
    selected_ns: int


@dataclass(frozen=True, slots=True)
class ScheduledPCM:
    token: tuple[str, int, int, int]  # (file session, global epoch, generation, block id)
    samples: np.ndarray
    pts_ns: int
    absolute_pts: Fraction
    due_ns: int | None  # None for native epoch offers: no host playout deadline is assigned.
    offset_samples: int
    total_samples: int
    discontinuity: str | None


@dataclass(slots=True)
class _StagedPCM:
    block_id: int
    block: AudioPCM
    consumed: int = 0


class FileAVPlayback:
    """Call tick/audio_window on the presentation thread; decode stays on workers.

    The common clock starts paused for preroll. `start` releases it after the
    first requested RGB/AI and playable audio (or audio EOF) are ready. A seek starts a new
    preroll; pause/resume only remaps unconsumed PCM without seeking either codec.
    """
    def __init__(self, path, *, paused=False, lookahead_ms=120, max_audio_samples=24_000,
                 max_video_frames=16, max_video_bytes=128 * 1024 * 1024, now_ns=time.perf_counter_ns):
        if type(paused) is not bool:
            raise ValueError("paused must be boolean")
        _integer(lookahead_ms, "lookahead_ms", minimum=1, maximum=500)
        _integer(max_audio_samples, "max_audio_samples", minimum=480, maximum=24_000)
        _integer(max_video_frames, "max_video_frames", minimum=2, maximum=64)
        _integer(max_video_bytes, "max_video_bytes", minimum=4096, maximum=512 * 1024 * 1024)
        self.path, self.now_ns = path, now_ns
        self.session_id = uuid.uuid4().hex
        self.lookahead_ns = lookahead_ms * 1_000_000
        self.max_audio_samples, self.max_video_frames, self.max_video_bytes = max_audio_samples, max_video_frames, max_video_bytes
        self.condition = threading.Condition()
        self.timeline = PlaybackTimeline(now_ns(), paused=True)
        self.user_paused, self.preroll, self.generation = paused, True, 0
        self.video_origin = None
        self.metadata_ready = self.audio_ready = False
        self.has_audio = False
        self.video_eof = self.audio_eof = False
        self.closing = False
        self.error = None
        self.duration_ns = None
        self.finished = False
        self._audio_end_ns = None
        self.target_ns = 0
        self._effective_start_ns = 0
        self._video = deque()
        self._audio = deque()
        self._processed = {}
        self._requested = {}
        self.ai_revision = 0
        self._issued_audio = set()
        self._retired_audio = deque(maxlen=128)
        self._video_bytes = self._audio_samples = 0
        self._video_pending_bytes = 0
        self._video_pending_pts = None
        self._last_presented = None
        self._resolved_epoch = None
        self._next_video_id = self._next_audio_id = 0
        self._threads = []
        self._transition = None
        self._committing_transition = False
        self._native_scope = None
        self._native_released = False
        self._native_require_ai = False
        self.metrics = dict(video_decoded=0, video_dropped=0, video_presentations=0,
                            ai_underflow_ticks=0, video_underflow_ticks=0, audio_underflow_ticks=0,
                            stale_ai_results=0, stale_audio_acks=0, audio_consumed_samples=0,
                            audio_trimmed_before_start=0, max_video_lateness_ns=0,
                            max_audio_lateness_ns=0, max_video_buffer_bytes=0, max_audio_buffer_samples=0)
        self.metrics["transport_reset_restarts"] = 0

    def __enter__(self):
        if self._threads:
            raise RuntimeError("FileAVPlayback instances cannot be entered twice")
        self._threads = [threading.Thread(target=self._video_worker, name="Quest3D AV video", daemon=True),
                         threading.Thread(target=self._audio_worker, name="Quest3D AV audio", daemon=True)]
        for thread in self._threads:
            thread.start()
        return self

    def _clock(self):
        return FileClock(self.session_id, self.video_origin, self.timeline.host_anchor_ns, self.timeline.media_anchor_ns,
                         self.timeline.paused, self.timeline.epoch, self.generation, self.preroll)

    def clock_snapshot(self):
        with self.condition:
            return self._clock()

    def _fail(self, error):
        with self.condition:
            self.error = f"{type(error).__name__}: {error}"
            self.condition.notify_all()

    def _video_worker(self):
        reader = None
        pending = None
        worker_epoch = 0
        try:
            reader = MediaReader(self.path)
            if reader.photo:
                raise ValueError("FileAVPlayback currently requires video; photos use the existing still source")
            estimate = reader.width * reader.height * 4
            if estimate > self.max_video_bytes:
                raise ValueError("One video frame exceeds the configured byte bound")
            with self.condition:
                self.video_origin = reader.origin
                self.has_audio = bool(reader.audio_streams)
                self.audio_ready = not self.has_audio
                self.audio_eof = not self.has_audio
                self.duration_ns = reader.duration_ns
                self.metadata_ready = True
                self.condition.notify_all()
            while True:
                with self.condition:
                    if self.closing:
                        return
                    epoch, target = self.timeline.epoch, self.target_ns
                    needs_seek = worker_epoch != epoch
                    if not needs_seek and (self.video_eof or
                            (pending is None and (len(self._video) >= self.max_video_frames - 1
                                                  or self._video_bytes + estimate > self.max_video_bytes))):
                        self.condition.wait(.02)
                        continue
                if needs_seek:
                    reader.seek(target)  # Never hold the shared clock while libav blocks.
                    worker_epoch, pending = epoch, None
                if pending is None:
                    try:
                        pending = reader.next()
                    except StopIteration:
                        with self.condition:
                            if worker_epoch == self.timeline.epoch:
                                self.video_eof = True
                                self.duration_ns = reader.duration_ns
                                self._video_pending_bytes = 0
                                self._video_pending_pts = None
                                self.condition.notify_all()
                        continue
                pixels, pts = pending
                with self.condition:
                    if self.closing:
                        return
                    if worker_epoch != self.timeline.epoch:
                        pending = None
                        self._video_pending_bytes = 0
                        self._video_pending_pts = None
                        continue
                    if self.video_origin is None:
                        self.video_origin = reader.origin
                        self.condition.notify_all()
                    self._video_pending_bytes = pixels.nbytes
                    self._video_pending_pts = pts
                    total_bytes = self._video_bytes + self._video_pending_bytes
                    self.metrics["max_video_buffer_bytes"] = max(self.metrics["max_video_buffer_bytes"], total_bytes)
                    if total_bytes > self.max_video_bytes:
                        raise ValueError("Decoded video exceeds the configured byte bound")
                    if pts < self.target_ns:
                        pending = None
                        self._video_pending_bytes = 0
                        self._video_pending_pts = None
                        self.metrics["video_dropped"] += 1
                        continue
                    if self._resolved_epoch != worker_epoch:
                        # Resolve paused VFR seek to the actual selected frame.
                        self._resolved_epoch = worker_epoch
                        self._effective_start_ns = pts
                        self.timeline.media_anchor_ns = pts
                        self.timeline.host_anchor_ns = self.now_ns()
                        self._trim_audio_before(pts)
                    horizon = self.timeline.position(self.now_ns()) + self.lookahead_ns
                    if pts > horizon or len(self._video) >= self.max_video_frames - 1:
                        self.condition.wait(.02)
                        continue
                    self._next_video_id += 1
                    frame = FileRGB(pixels, pts, worker_epoch, self._next_video_id)
                    pixels.setflags(write=False)
                    self._video.append(frame)
                    self._video_bytes += pixels.nbytes
                    self._video_pending_bytes = 0
                    self._video_pending_pts = None
                    self.metrics["video_decoded"] += 1
                    pending = None
                    self.condition.notify_all()
        except Exception as exc:
            self._fail(exc)
        finally:
            if reader:
                reader.close()

    def _trim_block(self, block, position_ns):
        target = block.video_origin + Fraction(position_ns, 1_000_000_000)
        skip = max(0, min(len(block.samples), math.ceil((target - block.absolute_pts) * RATE)))
        if not skip:
            return block, 0
        if skip == len(block.samples):
            return None, skip
        samples = block.samples[skip:].copy(order="C")
        samples.setflags(write=False)
        return replace(block, samples=samples, absolute_pts=block.absolute_pts + Fraction(skip, RATE),
                       resampler_pts=block.resampler_pts + Fraction(skip, RATE) if block.resampler_pts is not None else None), skip

    def _trim_audio_before(self, position_ns):
        while self._audio:
            staged = self._audio[0]
            block, skipped = self._trim_block(staged.block, position_ns)
            if not skipped:
                break
            self.metrics["audio_trimmed_before_start"] += skipped
            self._audio_samples -= skipped
            if block is None:
                self._audio.popleft()
            else:
                staged.block = block
                break

    def _audio_worker(self):
        reader = None
        pending = None
        worker_epoch = 0
        try:
            with self.condition:
                self.condition.wait_for(lambda: self.closing or self.error or
                                         (self.metadata_ready and (not self.has_audio or self.video_origin is not None)))
                if self.closing or self.error or not self.has_audio:
                    return
                origin = self.video_origin
            reader = MediaAudioReader(self.path, video_origin=origin)
            while True:
                with self.condition:
                    if self.closing:
                        return
                    epoch, target = self.timeline.epoch, self.target_ns
                    needs_seek = worker_epoch != epoch
                    if not needs_seek and (self.audio_eof or self._audio_samples + 480 > self.max_audio_samples):
                        self.condition.wait(.02)
                        continue
                if needs_seek:
                    reader.seek(target, epoch)
                    worker_epoch, pending = epoch, None
                if pending is None:
                    try:
                        pending = reader.next()
                    except StopIteration:
                        with self.condition:
                            if worker_epoch == self.timeline.epoch:
                                self.audio_eof = True
                                self.audio_ready = True
                                self.condition.notify_all()
                        continue
                with self.condition:
                    if self.closing:
                        return
                    if worker_epoch != self.timeline.epoch:
                        pending = None
                        continue
                    # Initial negative audio and resolved VFR seek preroll are
                    # discarded explicitly; pause/resume never enters this path.
                    pending, skipped = self._trim_block(pending, self._effective_start_ns)
                    self.metrics["audio_trimmed_before_start"] += skipped
                    if pending is None:
                        continue
                    self.audio_ready = True
                    self.condition.notify_all()
                    horizon = self.timeline.position(self.now_ns()) + self.lookahead_ns
                    if pending.pts_ns >= horizon or self._audio_samples + len(pending.samples) > self.max_audio_samples:
                        self.condition.wait(.02)
                        continue
                    self._next_audio_id += 1
                    self._audio.append(_StagedPCM(self._next_audio_id, pending))
                    self._audio_end_ns = max(self._audio_end_ns or 0,
                        round((pending.end_absolute_pts - pending.video_origin) * 1_000_000_000))
                    self._audio_samples += len(pending.samples)
                    self.metrics["max_audio_buffer_samples"] = max(self.metrics["max_audio_buffer_samples"], self._audio_samples)
                    pending = None
                    self.condition.notify_all()
        except Exception as exc:
            self._fail(exc)
        finally:
            if reader:
                reader.close()

    def _selected_video(self, position_ns):
        return next((frame for frame in reversed(self._video) if frame.pts_ns <= position_ns), None)

    def ready_for_start(self, *, require_ai=False):
        with self.condition:
            frame = self._selected_video(self.timeline.media_anchor_ns)
            return bool(not self.error and frame and self.audio_ready and
                        (not require_ai or frame.frame_id in self._processed))

    def start(self, *, require_ai=False):
        if type(require_ai) is not bool:
            raise ValueError("require_ai must be boolean")
        with self.condition:
            if self._native_scope is not None:
                raise RuntimeError("A prepared native epoch requires release_native_epoch")
            if self._transition is not None:
                raise RuntimeError("A native flush transition is pending")
            if not self.preroll:
                raise RuntimeError("This file epoch has already started")
            if not self.ready_for_start(require_ai=require_ai):
                raise RuntimeError("Common file preroll is not ready")
            self.preroll = False
            self.timeline.set_paused(self.user_paused, self.now_ns())
            self.generation += 1
            self._issued_audio.clear()
            self._retired_audio.clear()
            self.condition.notify_all()

    def prepare_native_epoch(self, *, require_ai=False):
        """Reserve one PCM generation while the actual common preroll stays held.

        This only checks local decoded RGB/audio and optionally matching AI. It
        supplies neither a native/device READY receipt nor permission to play.
        """
        if type(require_ai) is not bool:
            raise ValueError("require_ai must be boolean")
        with self.condition:
            if self.closing or self.error:
                raise RuntimeError(self.error or "File playback is closing")
            if self._native_scope is not None:
                raise RuntimeError("A native epoch is already prepared")
            self._require_direct_control_safe()
            if not self.preroll or not self.timeline.paused:
                raise RuntimeError("Native preparation requires held file preroll")
            if not self.ready_for_start(require_ai=require_ai):
                raise RuntimeError("Common file preroll is not ready")
            self.generation += 1
            self._native_scope = (self.session_id, self.timeline.epoch, self.generation)
            self._native_released = False
            self._native_require_ai = require_ai
            self._issued_audio.clear()
            self._retired_audio.clear()
            self.condition.notify_all()
            return self._clock()

    def release_native_epoch(self, exact_scope, start_host_ns):
        """Set the common anchor for this one prepared scope without new tokens.

        The caller must independently verify native preparation and the clock
        handshake before calling. A future anchor holds position until it is due;
        neither this method nor native_audio_batch acknowledges PCM consumption.
        """
        _integer(start_host_ns, "start_host_ns")
        with self.condition:
            if (type(exact_scope) is not tuple or len(exact_scope) != 3
                    or type(exact_scope[0]) is not str
                    or any(type(value) is not int for value in exact_scope[1:])
                    or exact_scope != self._native_scope
                    or exact_scope != (self.session_id, self.timeline.epoch, self.generation)):
                raise ValueError("Native file clock scope changed before release")
            if self.closing or self.error:
                raise RuntimeError(self.error or "File playback is closing")
            if self._native_released or not self.preroll or not self.timeline.paused:
                raise RuntimeError("The native file epoch has already been released")
            if self._transition is not None:
                raise RuntimeError("A native flush transition is pending")
            if not self.ready_for_start(require_ai=self._native_require_ai):
                raise RuntimeError("Common file preroll is not ready")
            if start_host_ns < self.now_ns():
                raise ValueError("Native file start must not be in the past")
            self.timeline.host_anchor_ns = start_host_ns
            self.timeline.paused = self.user_paused
            self.preroll = False
            self._native_released = True
            self.condition.notify_all()
            return self._clock()

    def _invalidate_native_epoch(self):
        self._native_scope = None
        self._native_released = False
        self._native_require_ai = False

    def set_paused(self, paused):
        if type(paused) is not bool:
            raise ValueError("paused must be boolean")
        with self.condition:
            self._require_direct_control_safe()
            if self.finished and not paused:
                return self.seek(0, paused=False)
            if paused != self.user_paused:
                self._invalidate_native_epoch()
                self.user_paused = paused
                self.timeline.set_paused(True if self.preroll else paused, self.now_ns())
                self.generation += 1
                self._issued_audio.clear()
                self._retired_audio.clear()
                self.condition.notify_all()

    def seek(self, position_ns, *, paused=None):
        _integer(position_ns, "position_ns")
        if paused is not None and type(paused) is not bool:
            raise ValueError("paused must be boolean")
        with self.condition:
            self._require_direct_control_safe()
            if self.duration_ns is not None and position_ns >= self.duration_ns:
                raise ValueError("Seek is outside the video timeline")
            self._invalidate_native_epoch()
            self.timeline.seek(position_ns, self.now_ns())
            self.timeline.set_paused(True, self.now_ns())
            if paused is not None:
                self.user_paused = paused
            self.target_ns, self.preroll = position_ns, True
            self.finished = False
            self._audio_end_ns = None
            self._effective_start_ns = position_ns
            self.generation += 1
            self._video.clear()
            self._audio.clear()
            self._processed.clear()
            self._requested.clear()
            self._issued_audio.clear()
            self._retired_audio.clear()
            self._video_bytes = self._audio_samples = 0
            self._last_presented = None
            self.video_eof, self.audio_eof = False, not self.has_audio
            self.audio_ready = not self.has_audio
            self.condition.notify_all()
            return self.timeline.epoch

    def _require_direct_control_safe(self):
        if not self._committing_transition and (self._transition is not None or self._issued_audio):
            raise RuntimeError("Issued PCM requires begin_transition and a confirmed native flush barrier")

    def begin_transition(self, *, paused=None, seek_ns=None, expected_scope=None):
        """Freeze presentation; stop new PCM offers but still accept old-scope acks.

        The consumer must stop/flush its queue and return the FINAL consumed
        cursor before commit. Time passing is never confirmation of consumption.
        Optional expected_scope binds a host reset to this exact clock atomically.
        """
        if paused is not None and type(paused) is not bool:
            raise ValueError("paused must be boolean")
        if paused is None and seek_ns is None:
            raise ValueError("A pause state or global seek position is required")
        if seek_ns is not None:
            _integer(seek_ns, "seek_ns")
        with self.condition:
            if expected_scope is not None and (type(expected_scope) is not tuple or
                    expected_scope != (self.session_id, self.timeline.epoch, self.generation) or
                    len(expected_scope) != 3 or type(expected_scope[0]) is not str or
                    any(type(value) is not int for value in expected_scope[1:])):
                raise ValueError("File clock scope changed before transition freeze")
            if self._transition is not None:
                raise RuntimeError("A native flush transition is already pending")
            if self.duration_ns is not None and seek_ns is not None and seek_ns >= self.duration_ns:
                raise ValueError("Seek is outside the video timeline")
            now = self.now_ns()
            position = self.timeline.position(now)
            self.timeline.set_paused(True, now)
            self._transition = FileTransition(self.session_id, uuid.uuid4().hex, self.timeline.epoch,
                                              self.generation, position,
                                              self.user_paused if paused is None else paused, seek_ns)
            self.condition.notify_all()
            return self._transition

    @staticmethod
    def _validate_ack_shape(token, consumed_samples):
        _integer(consumed_samples, "consumed_samples", maximum=480)
        if (type(token) is not tuple or len(token) != 4 or type(token[0]) is not str or len(token[0]) != 32 or
                any(value not in "0123456789abcdef" for value in token[0]) or
                any(type(value) is not int or not 0 <= value <= 9_223_372_036_854_775_807 for value in token[1:])):
            raise ValueError("Invalid PCM acknowledgement token")

    def commit_transition(self, transition, final_acks, *, flush_confirmed):
        """Commit only after authoritative final consumed acks + completed flush.

        final_acks contains (issued token, cumulative consumed samples) pairs.
        The caller, not this coordinator, proves the native queue is stopped.
        """
        if flush_confirmed is not True:
            raise ValueError("An unconfirmed native flush cannot commit a transition")
        if not isinstance(final_acks, (tuple, list)) or len(final_acks) > self.max_audio_samples:
            raise ValueError("Invalid final acknowledgement batch")
        with self.condition:
            if transition is not self._transition or transition is None:
                raise ValueError("Unknown or already committed transition")
            # Validate the entire batch before consuming anything.
            for pair in final_acks:
                if not isinstance(pair, (tuple, list)) or len(pair) != 2:
                    raise ValueError("Invalid final acknowledgement pair")
                token, count = pair
                self._validate_ack_shape(token, count)
                if token[:3] != (self.session_id, self.timeline.epoch, self.generation):
                    raise ValueError("Final acknowledgement belongs to another clock generation")
                if token not in self._issued_audio and token not in self._retired_audio:
                    raise ValueError("Final acknowledgement was never issued")
                staged = next((item for item in self._audio if item.block_id == token[3]), None)
                if staged is not None and count > len(staged.block.samples):
                    raise ValueError("Final acknowledgement exceeds the PCM block")
            for token, count in final_acks:
                self.ack_audio(token, count)
            self._committing_transition = True
            try:
                if transition.seek_ns is not None or (self.finished and not transition.paused):
                    self.seek(transition.seek_ns if transition.seek_ns is not None else 0, paused=transition.paused)
                else:
                    self._invalidate_native_epoch()
                    self.user_paused = transition.paused
                    self.timeline.set_paused(True if self.preroll else transition.paused, self.now_ns())
                    self.generation += 1
                    self._issued_audio.clear()
                    self._retired_audio.clear()
                self._transition = None
            finally:
                self._committing_transition = False
            self.condition.notify_all()
            return self._clock()

    def restart_after_transport_reset(self, transition, *, reset_confirmed, position_ns=None):
        """Unknown consumption requires confirmed transport teardown + global seek.

        Old audio and video are discarded together. This does not claim the old
        samples were consumed; a new epoch is the explicit recovery boundary.
        """
        if reset_confirmed is not True:
            raise ValueError("Transport reset must be confirmed before restarting")
        with self.condition:
            if transition is not self._transition or transition is None:
                raise ValueError("Unknown or already committed transition")
            target = position_ns if position_ns is not None else (
                transition.seek_ns if transition.seek_ns is not None else transition.frozen_position_ns)
            self._committing_transition = True
            try:
                epoch = self.seek(target, paused=transition.paused)
                self._transition = None
                self.metrics["transport_reset_restarts"] += 1
            finally:
                self._committing_transition = False
            return epoch

    def replay(self):
        return self.seek(0, paused=False)

    def finish_if_drained(self):
        """Freeze at a known end only after actual PCM consumption and EOF.

        Unknown last-frame duration is never inferred from nominal FPS. A silent
        or disconnected sink cannot drain an issued/unconsumed audio queue.
        """
        with self.condition:
            if self.finished:
                return True
            if (self.preroll or self._transition is not None or self.duration_ns is None
                    or not self.video_eof or not self.audio_eof or self._audio or self._issued_audio
                    or not self._video or self._last_presented != self._video[-1].frame_id):
                return False
            end_ns = max(self.duration_ns, self._audio_end_ns or 0)
            now = self.now_ns()
            if self.timeline.position(now) < end_ns:
                return False
            self.timeline.media_anchor_ns = end_ns
            self.timeline.host_anchor_ns = now
            self.timeline.paused = self.user_paused = True
            self._invalidate_native_epoch()
            self.generation += 1
            self._retired_audio.clear()
            self.finished = True
            self.condition.notify_all()
            return True

    def next_ai_frame(self, timeout_seconds=3):
        if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
                or not 0 <= timeout_seconds <= 60 or not math.isfinite(timeout_seconds)):
            raise ValueError("Invalid AI wait timeout")
        with self.condition:
            def available():
                return self.closing or self.error or any(frame.frame_id not in self._requested for frame in self._video)
            if not self.condition.wait_for(available, timeout_seconds):
                raise TimeoutError("No future RGB available for AI")
            if self.error:
                raise RuntimeError(self.error)
            if self.closing:
                raise RuntimeError("File playback is closing")
            frame = next(frame for frame in self._video if frame.frame_id not in self._requested)
            request = FileAIRequest(frame, self.ai_revision)
            self._requested[frame.frame_id] = request
            return request

    def set_ai_revision(self, revision):
        _integer(revision, "ai_revision")
        with self.condition:
            if revision < self.ai_revision:
                raise ValueError("AI revision cannot move backwards")
            if revision != self.ai_revision:
                self.ai_revision = revision
                self._processed.clear()
                self._requested.clear()
                self.condition.notify_all()

    def submit_ai(self, request, result):
        if not isinstance(request, FileAIRequest) or not isinstance(result, StereoFrame):
            raise ValueError("AI submission requires FileAIRequest and StereoFrame")
        frame = request.frame
        with self.condition:
            if (frame.epoch != self.timeline.epoch or request.ai_revision != self.ai_revision
                    or self._requested.get(frame.frame_id) is not request
                    or not any(existing is frame for existing in self._video)):
                self.metrics["stale_ai_results"] += 1
                return False
            if result.frame_id != frame.frame_id or result.generation != frame.epoch:
                raise ValueError("StereoFrame belongs to a different RGB frame or seek epoch")
            if result.mode not in ("2d", "3d"):
                raise ValueError("Invalid stereo result mode")
            self._processed[frame.frame_id] = result
            self.condition.notify_all()
            return True

    def tick(self, *, mode="2d", now_ns=None):
        if mode not in ("2d", "3d"):
            raise ValueError("Unknown file presentation mode")
        with self.condition:
            if self.error:
                raise RuntimeError(self.error)
            now = self.now_ns() if now_ns is None else now_ns
            clock = self._clock()
            position = clock.position(now)
            frame = self._selected_video(position)
            if frame is None:
                if not clock.paused:
                    self.metrics["video_underflow_ticks"] += 1
                self.condition.notify_all()
                return FilePresentation(None, None, "2d", "video_underflow", clock, 0, now)
            while self._video[0] is not frame:
                old = self._video.popleft()
                self._video_bytes -= old.bgra.nbytes
                self._processed.pop(old.frame_id, None)
                self._requested.pop(old.frame_id, None)
                if old.frame_id != self._last_presented:
                    self.metrics["video_dropped"] += 1
            first_presentation = self._last_presented != frame.frame_id
            if first_presentation:
                self.metrics["video_presentations"] += 1
                self._last_presented = frame.frame_id
            processed = self._processed.get(frame.frame_id) if mode == "3d" else None
            fallback = "ai_underflow" if mode == "3d" and processed is None else None
            if fallback and not clock.paused:
                self.metrics["ai_underflow_ticks"] += 1
            decode_wait = (not clock.paused and not self.video_eof and self._video_pending_pts is None
                           and self._video[-1] is frame and position - frame.pts_ns > self.lookahead_ns)
            if decode_wait:
                self.metrics["video_underflow_ticks"] += 1
                fallback = fallback or "video_decode_wait"
            lateness = max(0, now - clock.deadline(frame.pts_ns)) if not clock.paused and first_presentation else 0
            self.metrics["max_video_lateness_ns"] = max(self.metrics["max_video_lateness_ns"], lateness)
            self.condition.notify_all()
            return FilePresentation(frame, processed, processed.mode if processed is not None else "2d", fallback, clock, lateness, now)

    def audio_window(self, *, max_packets=50, now_ns=None):
        _integer(max_packets, "max_packets", minimum=1, maximum=50)
        with self.condition:
            if self.error:
                raise RuntimeError(self.error)
            clock = self._clock()
            if clock.paused or clock.preroll:
                return []
            now = self.now_ns() if now_ns is None else now_ns
            result = []
            for staged in self._audio:
                if staged.consumed == len(staged.block.samples):
                    continue
                absolute = staged.block.absolute_pts + Fraction(staged.consumed, RATE)
                pts = round((absolute - staged.block.video_origin) * 1_000_000_000)
                token = (clock.session_id, clock.epoch, clock.generation, staged.block_id)
                result.append(ScheduledPCM(token, staged.block.samples[staged.consumed:], pts, absolute,
                                           clock.deadline(pts), staged.consumed, len(staged.block.samples),
                                           staged.block.discontinuity if staged.consumed == 0 else None))
                self._issued_audio.add(token)
                self.metrics["max_audio_lateness_ns"] = max(self.metrics["max_audio_lateness_ns"],
                                                             max(0, now - clock.deadline(pts)))
                if len(result) == max_packets:
                    break
            if not result and self.has_audio and not self.audio_eof:
                self.metrics["audio_underflow_ticks"] += 1
            self.condition.notify_all()
            return result

    def audio_batch(self, *, max_packets=50, now_ns=None):
        """Atomically snapshot offers and whether they include all final PCM.

        Reading audio_eof separately after audio_window races the decoder's
        final append. EOF can be forwarded only after every offer in this
        snapshot succeeded and no unoffered suffix remained under this lock.
        """
        with self.condition:
            packets = self.audio_window(max_packets=max_packets, now_ns=now_ns)
            included = {packet.token[3] for packet in packets}
            final = self.audio_eof and all(staged.consumed == len(staged.block.samples)
                                           or staged.block_id in included for staged in self._audio)
            return tuple(packets), final

    def native_audio_batch(self, *, max_packets=50):
        """Observe bounded original PCM suffixes without a host due time or ACK.

        Available during native preroll and after its same-scope release. Offers
        repeat until actual consumed acknowledgements retire them; callers must
        deduplicate accepted tokens/offsets and retain backpressured offers. EOF
        is valid only after every returned offer was accepted. Empty future-audio
        preroll is not device READY and does not imply EOF.
        """
        _integer(max_packets, "max_packets", minimum=1, maximum=50)
        with self.condition:
            if self.closing or self.error:
                raise RuntimeError(self.error or "File playback is closing")
            clock = self._clock()
            if self._native_scope != (clock.session_id, clock.epoch, clock.generation):
                raise RuntimeError("No current prepared native file epoch")
            if self._transition is not None:
                raise RuntimeError("A native flush transition is pending")
            packets = []
            for staged in self._audio:
                if staged.consumed == len(staged.block.samples):
                    continue
                absolute = staged.block.absolute_pts + Fraction(staged.consumed, RATE)
                pts = round((absolute - staged.block.video_origin) * 1_000_000_000)
                token = (*self._native_scope, staged.block_id)
                packets.append(ScheduledPCM(token, staged.block.samples[staged.consumed:], pts, absolute,
                                            None, staged.consumed, len(staged.block.samples),
                                            staged.block.discontinuity if staged.consumed == 0 else None))
                self._issued_audio.add(token)
                if len(packets) == max_packets:
                    break
            included = {packet.token[3] for packet in packets}
            final = self.audio_eof and all(staged.consumed == len(staged.block.samples)
                                          or staged.block_id in included for staged in self._audio)
            self.condition.notify_all()
            return tuple(packets), final

    def ack_audio(self, token, consumed_samples):
        self._validate_ack_shape(token, consumed_samples)
        with self.condition:
            scope = (self.session_id, self.timeline.epoch, self.generation)
            if token in self._retired_audio and token[:3] == scope:
                return True
            if token not in self._issued_audio or token[:3] != scope:
                self.metrics["stale_audio_acks"] += 1
                return False
            staged = next((item for item in self._audio if item.block_id == token[3]), None)
            if staged is None:
                return True  # Repeated ack of an already retired issued token.
            if consumed_samples > len(staged.block.samples):
                raise ValueError("Consumed sample count exceeds the original PCM block")
            if consumed_samples > staged.consumed:
                self.metrics["audio_consumed_samples"] += consumed_samples - staged.consumed
                staged.consumed = consumed_samples
            while self._audio and self._audio[0].consumed == len(self._audio[0].block.samples):
                retired = self._audio.popleft()
                self._audio_samples -= len(retired.block.samples)
                retired_token = (*scope, retired.block_id)
                self._issued_audio.discard(retired_token)
                self._retired_audio.append(retired_token)
            self.condition.notify_all()
            return True

    def status(self):
        with self.condition:
            position = self.timeline.position(self.now_ns())
            known_video_done = bool(self.video_eof and self._video and
                                    self._last_presented == self._video[-1].frame_id and
                                    position >= (self.duration_ns if self.duration_ns is not None else self._video[-1].pts_ns))
            return {"session_id": self.session_id, "metadata_ready": self.metadata_ready, "audio_ready": self.audio_ready,
                    "video_origin": str(self.video_origin), "position_ns": position,
                    "epoch": self.timeline.epoch, "generation": self.generation,
                    "ai_revision": self.ai_revision,
                    "paused": self.timeline.paused, "preroll": self.preroll,
                    "native_epoch_prepared": self._native_scope is not None and not self._native_released,
                    "native_epoch_released": self._native_released,
                    "pending_transition": self._transition.transition_id if self._transition is not None else None,
                    "video_frames": len(self._video), "video_bytes": self._video_bytes + self._video_pending_bytes,
                    "audio_samples": self._audio_samples, "video_eof": self.video_eof, "audio_eof": self.audio_eof,
                    "issued_audio_tokens": len(self._issued_audio), "retired_ack_tokens": len(self._retired_audio),
                    "preroll_exhausted": self.preroll and self.video_eof and not self._video,
                    "known_samples_drained": known_video_done and self.audio_eof and not self._audio,
                    "video_duration_ns": self.duration_ns,
                    "video_presentation_finished": bool(known_video_done and self.duration_ns is not None
                        and not self.preroll and self._transition is None),
                    "last_video_duration_known": self.duration_ns is not None,
                    "finished": self.finished, "audio_end_ns": self._audio_end_ns,
                    "error": self.error, "native_output_integrated": False, "av_sync_verified": False,
                    **self.metrics}

    def __exit__(self, *_):
        with self.condition:
            self.closing = True
            self._invalidate_native_epoch()
            self.condition.notify_all()
        deadline = time.monotonic() + 5
        for thread in self._threads:
            thread.join(max(0, deadline - time.monotonic()))
        if any(thread.is_alive() for thread in self._threads):
            raise RuntimeError("File AV worker did not stop within five seconds")
        with self.condition:
            self._video.clear()
            self._audio.clear()
            self._processed.clear()
            self._video_bytes = self._audio_samples = self._video_pending_bytes = 0

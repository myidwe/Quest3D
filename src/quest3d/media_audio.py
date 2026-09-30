"""Bounded local-file audio decoding, without playback, devices or host clock.

PCM timestamps use the same video origin as MediaReader. Audio-leading negative
positions, timestamp gaps and overlaps remain explicit; this layer does not fill
silence, stretch audio, or report audible AV synchronization.
"""
from collections import deque
from dataclasses import dataclass
from fractions import Fraction
import math
from pathlib import Path
import threading

import av
import numpy as np

RATE = 48_000
NANOSECONDS = 1_000_000_000


def _integer(value, name, minimum=0, maximum=9_223_372_036_854_775_807):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer in [{minimum}, {maximum}]")
    return value


@dataclass(frozen=True, slots=True)
class AudioPCM:
    samples: np.ndarray  # Owned interleaved float32 [sample, left/right], <=480 samples.
    absolute_pts: Fraction  # Original segment anchor + exact 48k sample count.
    video_origin: Fraction
    epoch: int
    discontinuity: str | None = None  # start, seek, gap, overlap, format_change
    discontinuity_ns: int = 0
    sample_rate: int = RATE
    resampler_pts: Fraction | None = None  # Unmodified libav output PTS for diagnostics.
    timestamp_adjustment_ns: int = 0  # Only normalization within container PTS precision.

    @property
    def pts_ns(self):
        return round((self.absolute_pts - self.video_origin) * NANOSECONDS)

    @property
    def end_absolute_pts(self):
        return self.absolute_pts + Fraction(len(self.samples), RATE)

    @property
    def end_pts_ns(self):
        return round((self.end_absolute_pts - self.video_origin) * NANOSECONDS)


class MediaAudioReader:
    """Single-thread decoder with <=2s of decoded PCM plus libav internal state.

    Caller owns scheduling. `next` may block on file IO. Seek drops all old
    decoder/resampler output and trims to the first output sample at/after the
    requested video-relative time; it never pads partial blocks at EOF.
    """
    def __init__(self, path, *, video_origin=None, stream_index=0, block_samples=480):
        self.path = Path(path).resolve(strict=True)
        if not self.path.is_file():
            raise ValueError("Audio source must be a local file")
        _integer(stream_index, "stream_index", maximum=255)
        _integer(block_samples, "block_samples", minimum=1, maximum=480)
        if video_origin is not None and not isinstance(video_origin, Fraction):
            raise ValueError("video_origin must be an exact Fraction")
        self.container = None
        self.block_samples = block_samples
        self.epoch = 0
        self.eof = False
        self.decoded_frames = 0
        self.resampler_flush_samples = 0
        self.trimmed_samples = 0
        self.discontinuities = 0
        self.max_timestamp_adjustment_ns = 0
        self.source_anchor = None
        try:
            self.container = av.open(str(self.path), options={"protocol_whitelist": "file"})
            if stream_index >= len(self.container.streams.audio):
                raise ValueError("Requested local audio stream does not exist")
            self.stream = self.container.streams.audio[stream_index]
            self.audio_start = (self.stream.start_time * self.stream.time_base
                                if self.stream.start_time is not None else None)
            self.input_rate = self.stream.codec_context.sample_rate
            self.input_layout = self.stream.codec_context.layout.name
            if video_origin is None:
                videos = self.container.streams.video
                if not videos:
                    raise ValueError("An audio-only file requires an explicit shared video_origin")
                video = videos[0]
                if video.start_time is not None:
                    video_origin = video.start_time * video.time_base
                else:
                    # A separate handle avoids consuming any packet from audio.
                    with av.open(str(self.path), options={"protocol_whitelist": "file"}) as probe:
                        first = next(probe.decode(probe.streams.video[0]))
                        if first.pts is None or first.time_base is None:
                            raise ValueError("Video has no PTS for the common audio/video origin")
                        video_origin = first.pts * first.time_base
            self.video_origin = Fraction(video_origin)
            self.frames = iter(self.container.decode(self.stream))
            self.blocks = self._decode_blocks("start", None)
        except BaseException:
            self.close()
            raise

    def _converted(self, outputs, reason, delta_ns, target, clock):
        first = True
        for output in outputs:
            if output is None:  # PyAV15 passthrough resample(None) returns [None].
                continue
            if output.pts is None or output.time_base is None:
                raise ValueError("Resampled PCM has no presentation timestamp")
            if output.sample_rate != RATE or output.layout.name != "stereo" or output.format.name != "flt":
                raise ValueError("Unexpected resampler output format")
            if not 0 < output.samples <= RATE * 2:
                raise ValueError("Decoded PCM exceeds the two-second frame bound")
            values = output.to_ndarray().reshape(-1, 2)
            if not np.isfinite(values).all():
                raise ValueError("Decoded PCM contains nonfinite samples")
            resampler_pts = Fraction(output.pts) * output.time_base
            if clock["anchor"] is None:
                clock["anchor"] = resampler_pts
            absolute = clock["anchor"] + Fraction(clock["samples"], RATE)
            adjustment = absolute - resampler_pts
            if abs(adjustment) > clock["tolerance"]:
                raise ValueError("Resampled PTS diverged beyond source timestamp precision")
            adjustment_ns = round(adjustment * NANOSECONDS)
            self.max_timestamp_adjustment_ns = max(self.max_timestamp_adjustment_ns, abs(adjustment_ns))
            clock["samples"] += len(values)
            offset = 0
            if target is not None and absolute < target:
                offset = min(len(values), max(0, math.ceil((target - absolute) * RATE)))
                self.trimmed_samples += offset
            while offset < len(values):
                count = min(self.block_samples, len(values) - offset)
                # Own a small allocation; a queued view must not retain a 2s decoder frame.
                samples = values[offset:offset + count].copy(order="C")
                samples.setflags(write=False)
                yield AudioPCM(samples, absolute + Fraction(offset, RATE), self.video_origin,
                               self.epoch, reason if first else None, delta_ns if first else 0,
                               resampler_pts=resampler_pts + Fraction(offset, RATE),
                               timestamp_adjustment_ns=adjustment_ns)
                first = False
                offset += count

    def _decode_blocks(self, initial_reason, target):
        resampler = None
        expected = None
        signature = None
        pending_reason, pending_delta = initial_reason, 0
        segment_origin, segment_samples = None, 0
        clock = None
        for frame in self.frames:
            self.decoded_frames += 1
            if frame.pts is None or frame.time_base is None:
                raise ValueError("Audio frame has no presentation timestamp")
            if not 8_000 <= frame.sample_rate <= 192_000 or not 1 <= len(frame.layout.channels) <= 8:
                raise ValueError("Unsupported audio rate or channel count")
            if not 0 < frame.samples <= frame.sample_rate * 2:
                raise ValueError("Decoded audio exceeds the two-second frame bound")
            absolute = Fraction(frame.pts) * frame.time_base
            current_signature = (frame.sample_rate, frame.layout.name, frame.format.name)
            # Container timestamp quantization can exceed one sample; do not
            # misclassify rounded 1ms Matroska timestamps as real audio gaps.
            tolerance = Fraction(frame.time_base) + Fraction(1, frame.sample_rate)
            delta = absolute - expected if expected is not None else Fraction(0)
            changed = signature is not None and signature != current_signature
            discontinuous = expected is not None and abs(delta) > tolerance
            if changed or discontinuous:
                flushed = resampler.resample(None)
                self.resampler_flush_samples += sum(out.samples for out in flushed if out is not None)
                for block in self._converted(flushed, pending_reason, pending_delta, target, clock):
                    yield block
                    pending_reason, pending_delta = None, 0
                resampler = None
                pending_reason = "format_change" if changed else "gap" if delta > 0 else "overlap"
                pending_delta = round(delta * NANOSECONDS)
                self.discontinuities += 1
            if resampler is None:
                resampler = av.AudioResampler(format="flt", layout="stereo", rate=RATE,
                                              frame_size=self.block_samples)
                segment_origin, segment_samples = absolute, 0
                self.source_anchor = absolute
                signature = current_signature
                clock = {"anchor": None, "samples": 0, "tolerance": tolerance}
            clock["tolerance"] = max(clock["tolerance"], tolerance)
            segment_samples += frame.samples
            expected = segment_origin + Fraction(segment_samples, frame.sample_rate)
            outputs = resampler.resample(frame)
            for block in self._converted(outputs, pending_reason, pending_delta, target, clock):
                yield block
                pending_reason, pending_delta = None, 0
        if resampler is not None:
            outputs = resampler.resample(None)
            self.resampler_flush_samples += sum(out.samples for out in outputs if out is not None)
            for block in self._converted(outputs, pending_reason, pending_delta, target, clock):
                yield block
                pending_reason, pending_delta = None, 0

    def next(self):
        if self.container is None:
            raise RuntimeError("Audio reader is closed")
        try:
            return next(self.blocks)
        except StopIteration:
            self.eof = True
            raise

    def seek(self, position_ns, epoch):
        _integer(position_ns, "position_ns")
        _integer(epoch, "epoch")
        if epoch <= self.epoch:
            raise ValueError("Seek requires a newer timeline epoch")
        if self.container is None:
            raise RuntimeError("Audio reader is closed")
        target = self.video_origin + Fraction(position_ns, NANOSECONDS)
        # One second of decode preroll warms resampling filters where available.
        before = target - 1
        if self.audio_start is not None:
            before = max(self.audio_start, before)
        self.container.seek(math.floor(before / self.stream.time_base), stream=self.stream, backward=True)
        self.frames = iter(self.container.decode(self.stream))
        self.epoch, self.eof = epoch, False
        self.blocks = self._decode_blocks("seek", target)

    def diagnostics(self):
        return {"decoded_frames": self.decoded_frames, "trimmed_samples": self.trimmed_samples,
                "resampler_flush_samples": self.resampler_flush_samples,
                "discontinuities": self.discontinuities,
                "max_timestamp_adjustment_ns": self.max_timestamp_adjustment_ns}

    def close(self):
        blocks = getattr(self, "blocks", None)
        if blocks is not None:
            blocks.close()
            self.blocks = None
        if self.container is not None:
            self.container.close()
            self.container = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class FileAudioSource:
    """Decode worker with <=24000 queued samples (500ms) and one pending block.

    This is a demand-driven PCM source, not a playout clock. Pausing prevents
    reads and new decode; an in-flight block can complete but stays pending.
    A seek requires the new epoch from the shared video timeline and atomically
    drops queued old audio before performing libav IO outside the lock.
    """
    def __init__(self, path, *, video_origin=None, paused=False, max_buffer_samples=24_000):
        if type(paused) is not bool:
            raise ValueError("paused must be boolean")
        _integer(max_buffer_samples, "max_buffer_samples", minimum=480, maximum=24_000)
        self.path, self.video_origin = path, video_origin
        self.paused, self.max_buffer_samples = paused, max_buffer_samples
        self.condition = threading.Condition()
        self.queue = deque()
        self.queued_samples = 0
        self.high_water_samples = 0
        self.epoch = 0
        self.pending_seek = None
        self.closing = self.ready = self.eof = False
        self.error = None
        self.thread = None
        self.metadata = {}

    def __enter__(self):
        if self.thread is not None:
            raise RuntimeError("PCM worker instances cannot be entered twice")
        self.thread = threading.Thread(target=self._run, name="Quest3D local PCM decoder", daemon=True)
        self.thread.start()
        return self

    def _run(self):
        reader = None
        pending = None
        try:
            reader = MediaAudioReader(self.path, video_origin=self.video_origin)
            with self.condition:
                self.ready = True
                self.metadata = {"video_origin": str(reader.video_origin), "audio_start": str(reader.audio_start),
                                 "input_rate": reader.input_rate, "input_layout": reader.input_layout}
                self.condition.notify_all()
            while True:
                seek = None
                with self.condition:
                    if self.closing:
                        return
                    if self.pending_seek is not None:
                        seek, self.pending_seek = self.pending_seek, None
                        pending, self.eof = None, False
                    epoch = self.epoch
                    if seek is None and (self.paused or self.eof or self.queued_samples + 480 > self.max_buffer_samples):
                        self.condition.wait()
                        continue
                if seek is not None:
                    reader.seek(*seek)
                    with self.condition:
                        if self.closing:
                            return
                        if epoch != self.epoch or self.pending_seek is not None:
                            continue
                        if self.paused:
                            continue
                if pending is None:
                    try:
                        pending = reader.next()
                    except StopIteration:
                        with self.condition:
                            if epoch == self.epoch and self.pending_seek is None:
                                self.eof = True
                                self.metadata.update(reader.diagnostics())
                                self.condition.notify_all()
                        continue
                with self.condition:
                    if self.closing:
                        return
                    if epoch != self.epoch or self.pending_seek is not None:
                        pending = None
                        continue
                    if self.paused:
                        continue
                    self.metadata.update(reader.diagnostics())
                    self.queue.append(pending)
                    self.queued_samples += len(pending.samples)
                    self.high_water_samples = max(self.high_water_samples, self.queued_samples)
                    pending = None
                    self.condition.notify_all()
        except Exception as exc:
            with self.condition:
                self.error = f"{type(exc).__name__}: {exc}"
                self.condition.notify_all()
        finally:
            if reader is not None:
                reader.close()

    def read(self, timeout_seconds=3):
        if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
                or not 0 <= timeout_seconds <= 60 or not math.isfinite(timeout_seconds)):
            raise ValueError("Invalid PCM read timeout")
        with self.condition:
            available = self.condition.wait_for(lambda: self.closing or self.error or
                                                (not self.paused and (self.queue or self.eof)), timeout_seconds)
            if self.error:
                raise RuntimeError(self.error)
            if self.closing or not available:
                raise TimeoutError("No local PCM available")
            if not self.queue:
                raise StopIteration
            block = self.queue.popleft()
            self.queued_samples -= len(block.samples)
            self.condition.notify_all()
            return block

    def control(self, *, paused=None, seek_ns=None, epoch=None):
        if paused is not None and type(paused) is not bool:
            raise ValueError("paused must be boolean")
        if (seek_ns is None) != (epoch is None):
            raise ValueError("Seek position and new shared timeline epoch are required together")
        if seek_ns is not None:
            _integer(seek_ns, "seek_ns")
            _integer(epoch, "epoch")
        with self.condition:
            if self.closing:
                raise RuntimeError("PCM source is closing")
            if self.error:
                raise RuntimeError(self.error)
            if seek_ns is not None:
                if epoch <= self.epoch:
                    raise ValueError("Seek requires a newer timeline epoch")
                self.epoch = epoch
                self.pending_seek = (seek_ns, epoch)
                self.queue.clear()
                self.queued_samples = 0
                self.eof = False
            if paused is not None:
                self.paused = paused
            self.condition.notify_all()

    def status(self):
        with self.condition:
            return {"ready": self.ready, "paused": self.paused, "eof": self.eof,
                    "decoder_eof": self.eof, "drained": self.eof and not self.queue,
                    "epoch": self.epoch, "queued_samples": self.queued_samples,
                    "buffer_ms": self.queued_samples / 48, "high_water_samples": self.high_water_samples,
                    "error": self.error, "audio_playback_integrated": False, **self.metadata}

    def __exit__(self, *_):
        with self.condition:
            self.closing = True
            self.queue.clear()
            self.queued_samples = 0
            self.condition.notify_all()
        self.thread.join(5)
        if self.thread.is_alive():
            raise RuntimeError("Local PCM decoder did not stop within five seconds")

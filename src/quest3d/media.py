"""Local file video/photo source with a bounded, PTS-driven presentation queue.

This is the video half of the media player. Audio is detected but not played;
the product must not report synchronized media playback until its PCM path is
connected. Decoding slower than real time drops video rather than growing delay.
"""
from dataclasses import dataclass, asdict
from fractions import Fraction
import math
import logging
from pathlib import Path
import threading
import time
import uuid

import av
import numpy as np
from PIL import Image, ImageOps

from .geometry import ScreenRect, SourceGeometry


class UnsupportedMediaColor(ValueError):
    """The file needs a color transform this SDR decoder does not implement."""


@dataclass(frozen=True, slots=True)
class VideoColorPolicy:
    pixel_format: str
    matrix_metadata: int
    range_metadata: int
    transfer_metadata: int
    primaries_metadata: int
    source_matrix: str
    source_range: str
    assumptions: tuple[str, ...]
    output: str = "BGRA8 full-range; SDR transfer and primaries preserved"
    metadata_scope: str = "frame matrix/range; decoder-context transfer/primaries (PyAV15)"

    def reformat_options(self):
        if self.source_matrix == "RGB":
            return {"format": "bgra"}
        # PyAV15 only calls sws_setColorspaceDetails when matrix or range differ.
        # Even full->full must enter that branch. The destination YUV matrix is
        # unused for packed RGB output; this does NOT convert RGB primaries/TRC.
        return {"format": "bgra", "src_colorspace": self.source_matrix,
                "dst_colorspace": "ITU601" if self.source_matrix == "ITU709" else "ITU709",
                "src_color_range": self.source_range, "dst_color_range": "JPEG"}


def video_color_policy(frame, codec_context=None):
    """Resolve observed SDR matrix/range, never infer BT.709 from resolution."""
    matrix = int(frame.colorspace)
    color_range = int(frame.color_range)
    # VideoFrame 15.0.0 does not expose these two AVFrame fields. Read the live
    # decoder context after decode and report this limitation, not frame proof.
    transfer = int(getattr(codec_context, "color_trc", 2))
    primaries = int(getattr(codec_context, "color_primaries", 2))
    assumptions = []
    if transfer in (16, 18) or matrix in (9, 10, 14) or primaries == 9:
        raise UnsupportedMediaColor("HDR/BT.2020 file input needs a dedicated transfer/gamut/tone-map path")
    if transfer not in (1, 2, 4, 5, 6, 7, 13):
        raise UnsupportedMediaColor(f"Unsupported file transfer characteristic {transfer}; no implicit SDR conversion")
    if primaries not in (1, 2, 4, 5, 6, 7):
        raise UnsupportedMediaColor(f"Unsupported file color primaries {primaries}; gamut conversion is unavailable")
    if transfer == 2:
        if max((part.bits for part in frame.format.components), default=0) > 8:
            raise UnsupportedMediaColor("High-bit-depth file has unspecified transfer; HDR cannot be inferred safely")
        assumptions.append("unspecified transfer: assume SDR; no transfer conversion")
    if primaries == 2:
        assumptions.append("unspecified primaries: preserve decoded RGB; gamut unverified")
    if frame.format.is_bayer:
        raise UnsupportedMediaColor("Raw Bayer file input needs a camera color pipeline")
    if color_range not in (0, 1, 2):
        raise UnsupportedMediaColor(f"Unsupported file color range {color_range}")
    if frame.format.is_rgb or frame.format.has_palette:
        if color_range == 1:
            raise UnsupportedMediaColor("Limited-range RGB file input is not implemented")
        source_matrix, source_range = "RGB", "JPEG"
    else:
        if matrix == 1:
            source_matrix = "ITU709"
        elif matrix in (5, 6):
            source_matrix = "ITU601"
        elif matrix == 2:
            source_matrix = "ITU601"
            assumptions.append("unspecified YUV matrix: assume BT.601 (no resolution heuristic)")
        else:
            raise UnsupportedMediaColor(f"Unsupported file YUV matrix {matrix}")
        if color_range == 2 or (color_range == 0 and frame.format.name.startswith("yuvj")):
            source_range = "JPEG"
        else:
            source_range = "MPEG"
            if color_range == 0:
                assumptions.append("unspecified YUV range: assume limited")
    return VideoColorPolicy(frame.format.name, matrix, color_range, transfer, primaries,
                            source_matrix, source_range, tuple(assumptions))


def seconds_ns(value):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not 0 <= value <= 9_223_372_036 or not math.isfinite(value)):
        raise ValueError("Media position must be a finite, nonnegative number")
    return round(value * 1_000_000_000)


class PlaybackTimeline:
    """A media clock; pause/resume never advances or loses the saved position."""
    def __init__(self, now_ns, *, paused=False):
        self.host_anchor_ns = now_ns
        self.media_anchor_ns = 0
        self.paused = paused
        self.epoch = 0

    def position(self, now_ns):
        return self.media_anchor_ns + (0 if self.paused else max(0, now_ns - self.host_anchor_ns))

    def set_paused(self, paused, now_ns):
        if not isinstance(paused, bool):
            raise ValueError("Paused must be a boolean")
        self.media_anchor_ns = self.position(now_ns)
        self.host_anchor_ns = now_ns
        self.paused = paused

    def seek(self, position_ns, now_ns):
        if position_ns < 0:
            raise ValueError("Negative seek is unsupported")
        self.media_anchor_ns, self.host_anchor_ns = position_ns, now_ns
        self.epoch += 1

    def deadline(self, pts_ns):
        return self.host_anchor_ns + pts_ns - self.media_anchor_ns


@dataclass(frozen=True, slots=True)
class MediaFrame:
    bgra: np.ndarray
    captured_ns: int  # Scheduled presentation time on host clock, not capture receipt.
    source_id: str
    frame_id: int
    geometry_generation: int
    geometry: SourceGeometry
    pts_ns: int
    timeline_epoch: int
    source_identity: object | None = None


class MediaReader:
    """Single-thread owned decoder, preserving rational PTS including VFR."""
    PHOTO_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff"}

    def __init__(self, path):
        self.path = Path(path).resolve(strict=True)
        if not self.path.is_file():
            raise ValueError("Media source must be a local file")
        self.container = None
        self.photo = self.path.suffix.lower() in self.PHOTO_SUFFIXES
        self.audio_streams = 0
        self.duration_ns = None
        self.origin = None
        self.last_pts_ns = None
        self.last_frame_end_ns = None
        self.image = None
        self.color_info = None
        self._last_color_policy = None
        try:
            if self.photo:
                with Image.open(self.path) as image:
                    # This path preserves conventional 8-bit photo RGB values.
                    # It is not a decoder for linear/high-bit-depth HDR photos.
                    if image.mode in ("I", "F") or "16" in image.mode:
                        raise UnsupportedMediaColor("High-bit-depth/linear photo input needs an explicit color pipeline")
                    self.color_info = {"kind": "photo", "output": "BGRA8 full-range",
                        "conversion": "Pillow RGB values, EXIF orientation, alpha over black",
                        "icc_present": bool(image.info.get("icc_profile")), "icc_applied": False,
                        "assumptions": ["unmanaged SDR photo RGB; embedded ICC/gain-map not applied"]}
                    # Preserve orientation; alpha is composited onto black, not discarded.
                    image = ImageOps.exif_transpose(image).convert("RGBA")
                    background = Image.new("RGBA", image.size, (0, 0, 0, 255))
                    rgba = np.asarray(Image.alpha_composite(background, image))
                    self.image = np.ascontiguousarray(rgba[:, :, [2, 1, 0, 3]])
                self.width, self.height = self.image.shape[1], self.image.shape[0]
            else:
                # FFmpeg must not fetch a playlist URL or remote segment implicitly.
                self.container = av.open(str(self.path), options={"protocol_whitelist": "file"})
                videos = self.container.streams.video
                if not videos:
                    raise ValueError("This file has no video stream")
                self.stream = videos[0]
                self.stream.thread_type = "SLICE"
                self.audio_streams = len(self.container.streams.audio)
                self.width, self.height = self.stream.codec_context.width, self.stream.codec_context.height
                if self.stream.start_time is not None:
                    self.origin = self.stream.start_time * self.stream.time_base
                if self.stream.duration is not None:
                    self.duration_ns = round(self.stream.duration * self.stream.time_base * 1_000_000_000)
                self.frames = iter(self.container.decode(self.stream))
        except Exception:
            self.close()
            raise

    def next(self):
        if self.photo:
            return self.image, 0
        try:
            frame = next(self.frames)
        except StopIteration:
            # Containers such as NUT may omit stream duration while decoded
            # frames carry exact durations. Only the actual final frame can
            # establish this end; never substitute nominal/average FPS.
            if self.duration_ns is None and self.last_frame_end_ns is not None:
                self.duration_ns = self.last_frame_end_ns
            raise
        if frame.pts is None or frame.time_base is None:
            raise ValueError("Video frame has no presentation timestamp; implicit FPS timing is unsupported")
        timestamp = frame.pts * frame.time_base
        if self.origin is None:
            self.origin = timestamp
        pts_ns = round((timestamp - self.origin) * 1_000_000_000)
        if self.last_pts_ns is not None and pts_ns < self.last_pts_ns:
            raise ValueError("Video presentation timestamps moved backwards")
        self.last_pts_ns = pts_ns
        duration = frame.duration
        self.last_frame_end_ns = (round((timestamp + duration * frame.time_base - self.origin) * 1_000_000_000)
                                  if duration is not None and duration > 0 else None)
        policy = video_color_policy(frame, self.stream.codec_context)
        if policy != self._last_color_policy:
            self._last_color_policy = policy
            self.color_info = asdict(policy)
            if policy.assumptions:
                logging.getLogger(__name__).warning("File color assumptions: %s", "; ".join(policy.assumptions))
        # Resolve after decode, using this frame's actual matrix/range. Never
        # mutate decoder metadata or tag an assumed conversion as measured 709.
        pixels = frame.to_ndarray(**policy.reformat_options())
        return np.ascontiguousarray(pixels), pts_ns

    def seek(self, position_ns):
        if self.photo:
            if position_ns:
                raise ValueError("A photo has no seekable timeline")
            return
        if self.duration_ns is not None and position_ns >= self.duration_ns:
            raise ValueError("Seek must be before the end of the video")
        timestamp = Fraction(position_ns, 1_000_000_000) + (self.origin or 0)
        self.container.seek(int(timestamp / self.stream.time_base), stream=self.stream, backward=True)
        self.frames = iter(self.container.decode(self.stream))
        self.last_pts_ns = None
        self.last_frame_end_ns = None

    def close(self):
        if self.container is not None:
            self.container.close()
            self.container = None


class FileVideoSource:
    """Decode on one worker; expose only the latest due frame to presentation."""
    timestamp_kind = "file_pts_mapped_to_host"
    native_texture_lease_verified = None

    def __init__(self, path, *, paused=False):
        self.path = Path(path)
        self.condition = threading.Condition()
        self.timeline = PlaybackTimeline(time.perf_counter_ns(), paused=paused)
        self.source_id = "file:" + uuid.uuid4().hex
        self.latest = None
        self.delivered_id = 0
        self.error = None
        self.closing = False
        self.eof = False
        self.ready = False
        self.dropped_frames = 0
        self.decoded_frames = 0
        self.audio_streams = 0
        self.photo = False
        self.duration_ns = None
        self.pending_seek = None
        self.max_lateness_ns = 0
        self.thread = None
        self.color_info = None

    def __enter__(self):
        self.thread = threading.Thread(target=self._run, name="Quest3D file decoder", daemon=True)
        self.thread.start()
        return self

    def _run(self):
        reader = None
        try:
            reader = MediaReader(self.path)
            with self.condition:
                self.photo, self.audio_streams = reader.photo, reader.audio_streams
                self.duration_ns = reader.duration_ns
                # Opening/demux initialization time is not playback elapsed time.
                self.timeline.host_anchor_ns = time.perf_counter_ns()
                if reader.photo:
                    self.timeline.set_paused(True, self.timeline.host_anchor_ns)
                self.ready = True
            pending = None
            seek_target = None
            while True:
                seek_command = None
                with self.condition:
                    if self.closing:
                        break
                    if self.pending_seek is not None:
                        seek_target, self.pending_seek = self.pending_seek, None
                        seek_command = seek_target
                        pending, self.eof = None, False
                    epoch = self.timeline.epoch
                    if (self.eof or reader.photo) and self.latest is not None and seek_target is None:
                        self.condition.wait(.1)
                        continue
                    if self.timeline.paused and self.latest is not None and seek_target is None:
                        self.condition.wait(.1)
                        continue
                if seek_command is not None:
                    # Local/slow storage can block here. Never hold the control
                    # condition while libav seeks; 2D/status/stop stay responsive.
                    reader.seek(seek_command)
                    with self.condition:
                        if epoch != self.timeline.epoch or self.pending_seek is not None:
                            pending = None
                            continue
                if pending is None:
                    try:
                        pending = reader.next()
                        self.decoded_frames += 1
                    except StopIteration:
                        with self.condition:
                            self.duration_ns = reader.duration_ns
                            # Keep the last frame until its declared presentation
                            # interval ends. Decoder EOF is not the playback clock.
                            while (not self.closing and self.pending_seek is None
                                   and self.duration_ns is not None
                                   and self.timeline.position(time.perf_counter_ns()) < self.duration_ns):
                                if self.timeline.paused:
                                    self.condition.wait(.05)
                                else:
                                    remaining = self.duration_ns - self.timeline.position(time.perf_counter_ns())
                                    self.condition.wait(max(0, min(remaining / 1e9, .05)))
                            if self.closing:
                                break
                            if self.pending_seek is not None:
                                continue
                            self.eof = True
                            self.timeline.set_paused(True, time.perf_counter_ns())
                            if self.duration_ns is not None:
                                self.timeline.media_anchor_ns = self.duration_ns
                            self.condition.notify_all()
                        continue
                pixels, pts_ns = pending
                with self.condition:
                    if epoch != self.timeline.epoch or self.pending_seek is not None:
                        pending = None
                        continue
                    if seek_target is not None and pts_ns < seek_target:
                        self.dropped_frames += 1
                        pending = None
                        continue
                    now = time.perf_counter_ns()
                    due = self.timeline.deadline(pts_ns)
                    if not self.timeline.paused and not reader.photo and due > now:
                        self.condition.wait(min((due - now) / 1e9, .05))
                        continue
                    if seek_target is not None and self.timeline.paused:
                        # Report the actual selected VFR PTS, so resuming does
                        # not schedule the already-visible frame into the future.
                        self.timeline.media_anchor_ns = pts_ns
                        self.timeline.host_anchor_ns = now
                    seek_target = None
                    # Seeking while paused shows the selected frame immediately.
                    stamp = now if self.timeline.paused or reader.photo else due
                    self.max_lateness_ns = max(self.max_lateness_ns, now - stamp)
                    if self.latest is not None and self.latest.frame_id != self.delivered_id:
                        self.dropped_frames += 1
                    bounds = ScreenRect(0, 0, pixels.shape[1], pixels.shape[0])
                    number = 1 if self.latest is None else self.latest.frame_id + 1
                    from .source_identity import SourceIdentity, SourceKind
                    identity = SourceIdentity(SourceKind.PHOTO if reader.photo else SourceKind.VIDEO,
                        0, (int(self.source_id.split(":", 1)[1], 16) & ((1 << 64) - 1)) or 1, 0, 0, bounds)
                    self.latest = MediaFrame(pixels, stamp, self.source_id, number, epoch,
                                             SourceGeometry(bounds, epoch), pts_ns, epoch, identity)
                    self.color_info = reader.color_info
                    pending = None
                    self.condition.notify_all()
        except Exception as exc:
            with self.condition:
                self.error = f"{type(exc).__name__}: {exc}"
                self.condition.notify_all()
        finally:
            if reader is not None:
                reader.close()

    def grab(self, timeout_seconds=3):
        with self.condition:
            available = self.condition.wait_for(
                lambda: self.error or self.closing or (self.latest is not None and self.latest.frame_id != self.delivered_id),
                timeout_seconds)
            if self.error:
                raise RuntimeError(self.error)
            if not available or self.closing:
                raise TimeoutError("No new file video frame")
            self.delivered_id = self.latest.frame_id
            return self.latest

    def control(self, *, paused=None, seek_seconds=None):
        with self.condition:
            if seek_seconds is not None:
                position = seconds_ns(seek_seconds)
                if not self.ready:
                    raise RuntimeError("Media metadata is not ready")
                if (self.photo and position) or (self.duration_ns is not None and position >= self.duration_ns):
                    raise ValueError("Seek is outside the media timeline")
                self.timeline.seek(position, time.perf_counter_ns())
                self.pending_seek = position
                self.eof = False
            if paused is not None:
                if self.photo:
                    paused = True
                if not paused and self.eof and seek_seconds is None:
                    self.timeline.seek(0, time.perf_counter_ns())
                    self.pending_seek = 0
                    self.eof = False
                self.timeline.set_paused(paused, time.perf_counter_ns())
            self.condition.notify_all()

    def playback_status(self):
        with self.condition:
            return {"kind": "photo" if self.photo else "video", "ready": self.ready,
                    "paused": self.timeline.paused, "eof": self.eof, "timeline_epoch": self.timeline.epoch,
                    "position_seconds": self.timeline.position(time.perf_counter_ns()) / 1e9,
                    "duration_seconds": self.duration_ns / 1e9 if self.duration_ns is not None else None,
                    "decoded_frames": self.decoded_frames, "dropped_frames": self.dropped_frames,
                    "max_video_lateness_ms": self.max_lateness_ns / 1e6,
                    "audio_streams": self.audio_streams, "audio_playback_integrated": False,
                    "color": self.color_info,
                    "buffer_policy": "one pending decoded frame; latest due presentation only"}

    def host_presentation_ns(self, frame):
        """Map immutable file PTS using the active pause/resume clock anchor."""
        with self.condition:
            if frame.timeline_epoch != self.timeline.epoch:
                return None
            return frame.captured_ns if self.photo or self.timeline.paused else self.timeline.deadline(frame.pts_ns)

    def __exit__(self, *_):
        with self.condition:
            self.closing = True
            self.condition.notify_all()
        self.thread.join(5)
        if self.thread.is_alive():
            raise RuntimeError("File decoder did not stop within 5 seconds")

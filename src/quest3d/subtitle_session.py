"""Optional file captions selected from the exact RGB/depth presentation PTS."""
from pathlib import Path
import time

from .subtitles import FlatSubtitleRenderer, SubtitleTrack


def validate_subtitle_options(args):
    path = getattr(args, "subtitles", None)
    if path and not getattr(args, "file", None):
        raise ValueError("--subtitles requires a local --file source")
    if not path and any(getattr(args, key, None) is not None for key in ("subtitle_font", "subtitle_size")):
        raise ValueError("--subtitle-font/--subtitle-size require --subtitles")
    size = getattr(args, "subtitle_size", None)
    if size is not None and (isinstance(size, bool) or not isinstance(size, int) or not 8 <= size <= 128):
        raise ValueError("--subtitle-size must be an integer in 8..128 pixels per eye")


class FileSubtitleOverlay:
    """A caption layout failure disables captions while preserving video/control."""

    @classmethod
    def from_args(cls, args):
        validate_subtitle_options(args)
        if not getattr(args, "subtitles", None):
            return None
        return cls(args.subtitles, getattr(args, "subtitle_font", None) or "C:/Windows/Fonts/malgun.ttf",
                   getattr(args, "subtitle_size", None) or 32)

    def __init__(self, path, font_path, size):
        self.path = str(Path(path).resolve())
        self.track = SubtitleTrack.load(path)
        self.renderer = FlatSubtitleRenderer(font_path, font_size_px=size)
        self.error = None
        self.composites = 0
        self.last_ms = 0.0

    def composite(self, frame, pts_ns, *, eof=False):
        if self.error:
            return frame
        started = time.perf_counter_ns()
        try:
            result = self.renderer.composite_frame(frame, pts_ns, self.track, eof=eof)
        except (ValueError, OSError) as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            self.renderer.set_visible(False)
            return frame
        finally:
            self.last_ms = (time.perf_counter_ns() - started) / 1e6
        self.composites += 1
        return result

    def snapshot(self):
        return {"enabled": self.error is None and self.renderer.visible, "error": self.error,
                "path": self.path, "font": self.renderer.font_path,
                "font_size_px": self.renderer.font_size_px, "cue_count": len(self.track.cues),
                "composites": self.composites, "last_composite_ms": self.last_ms,
                "placement": "flat post-warp; identical pixels in both eyes",
                "last": dict(self.renderer.last_diagnostics), "quest_verified": False}

    def presentation_record(self):
        """Per-frame evidence omits unchanged paths/configuration and cue text."""
        return {"enabled": self.error is None and self.renderer.visible, "error": self.error,
                "last_composite_ms": self.last_ms, "last": dict(self.renderer.last_diagnostics)}

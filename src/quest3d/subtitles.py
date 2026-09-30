"""Bounded UTF-8 SRT selection on source PTS and flat post-warp SBS captions."""

from bisect import bisect_left, bisect_right
from collections import OrderedDict
from dataclasses import dataclass, replace
from io import BytesIO
from itertools import islice
from pathlib import Path
import re
import struct
import threading
import unicodedata

import numpy as np
from PIL import Image, ImageDraw, ImageFont


MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_CUES = 10000
MAX_CUE_CHARS = 2048
MAX_OVERLAP = 8
MAX_FONT_BYTES = 32 * 1024 * 1024
MAX_RENDER_CHARS = 4096
MAX_LINES = 8
MAX_FRAME_BYTES = 256 * 1024 * 1024
_TIME = re.compile(r"^(\d{2,6}):([0-5]\d):([0-5]\d),(\d{3}) --> (\d{2,6}):([0-5]\d):([0-5]\d),(\d{3})$")


def _int(value, name, low=0, high=(1 << 63) - 1):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{name} must be an integer in [{low}, {high}]")
    return value


@dataclass(frozen=True, slots=True)
class SubtitleCue:
    index: int
    start_ns: int
    end_ns: int
    text: str


@dataclass(frozen=True, slots=True)
class _Intervals:
    center: int
    by_start: tuple
    starts: tuple
    by_end: tuple
    ends: tuple
    left: object
    right: object

    @classmethod
    def build(cls, entries):
        if not entries:
            return None
        center = sorted(c.start_ns for _, c in entries)[len(entries) // 2]
        left, right, middle = [], [], []
        for item in entries:
            cue = item[1]
            (left if cue.end_ns <= center else right if cue.start_ns > center else middle).append(item)
        start = tuple(sorted(middle, key=lambda item: item[1].start_ns))
        end = tuple(sorted(middle, key=lambda item: item[1].end_ns))
        return cls(center, start, tuple(c.start_ns for _, c in start), end,
                   tuple(c.end_ns for _, c in end), cls.build(left), cls.build(right))

    def collect(self, pts_ns, out):
        if pts_ns < self.center:
            out.extend(self.by_start[:bisect_right(self.starts, pts_ns)])
            if self.left:
                self.left.collect(pts_ns, out)
        else:
            out.extend(self.by_end[bisect_right(self.ends, pts_ns):])
            if self.right:
                self.right.collect(pts_ns, out)


class SubtitleTrack:
    """Immutable cues; queries have no advancing cursor and work after any seek."""
    __slots__ = ("_cues", "_tree")

    def __init__(self, cues):
        cues = tuple(islice(iter(cues), MAX_CUES + 1))
        if not 0 < len(cues) <= MAX_CUES:
            raise ValueError(f"SRT requires 1..{MAX_CUES} cues")
        indices, events, total_chars = set(), [], 0
        for cue in cues:
            if not isinstance(cue, SubtitleCue):
                raise TypeError("Expected SubtitleCue")
            _int(cue.index, "cue index", 1, 999999999)
            _int(cue.start_ns, "cue start")
            _int(cue.end_ns, "cue end")
            if cue.index in indices or cue.end_ns <= cue.start_ns:
                raise ValueError(f"Invalid/duplicate index or non-positive interval: cue {cue.index}")
            if not isinstance(cue.text, str) or not cue.text.strip() or len(cue.text) > MAX_CUE_CHARS:
                raise ValueError(f"Empty or oversized text: cue {cue.index}")
            if any(unicodedata.category(c) == "Cc" and c not in "\n\t" for c in cue.text):
                raise ValueError(f"Control character in cue {cue.index}")
            if any(unicodedata.category(c) == "Cs" for c in cue.text):
                raise ValueError(f"Invalid Unicode surrogate in cue {cue.index}")
            indices.add(cue.index)
            total_chars += len(cue.text)
            if total_chars > MAX_FILE_BYTES:
                raise ValueError("Subtitle text exceeds track memory limit")
            events.extend(((cue.start_ns, 1), (cue.end_ns, -1)))
        overlap = 0
        for _, delta in sorted(events): # end-exclusive: ends before starts at the same PTS.
            overlap += delta
            if overlap > MAX_OVERLAP:
                raise ValueError(f"SRT exceeds {MAX_OVERLAP} overlapping cues")
        self._cues, self._tree = cues, _Intervals.build(tuple(enumerate(cues)))

    @property
    def cues(self):
        return self._cues

    @classmethod
    def load(cls, path):
        with Path(path).open("rb") as stream:
            data = stream.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise ValueError(f"SRT exceeds {MAX_FILE_BYTES} bytes")
        try:
            text = data.decode("utf-8-sig", errors="strict")
        except UnicodeDecodeError as error:
            raise ValueError("SRT must be UTF-8, optionally with a BOM") from error
        return cls.from_text(text)

    @classmethod
    def from_text(cls, text):
        if not isinstance(text, str):
            raise TypeError("SRT text must be str")
        try:
            if len(text) > MAX_FILE_BYTES or len(text.encode("utf-8")) > MAX_FILE_BYTES:
                raise ValueError("SRT exceeds byte limit")
        except UnicodeEncodeError as error:
            raise ValueError("SRT contains invalid Unicode") from error
        text = text.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n").strip()
        if not text:
            raise ValueError("SRT contains no cues")
        blocks = re.split(r"\n(?:[ \t]*\n)+", text)
        if len(blocks) > MAX_CUES:
            raise ValueError("SRT exceeds cue limit")
        cues = []
        for block in blocks:
            lines = block.strip().split("\n")
            if len(lines) < 3 or not re.fullmatch(r"[1-9]\d{0,8}", lines[0].strip()):
                raise ValueError(f"Invalid SRT index/text block {len(cues) + 1}")
            match = _TIME.fullmatch(lines[1].strip())
            if match is None:
                raise ValueError(f"Invalid SRT timestamp at cue {lines[0]}")
            values = tuple(map(int, match.groups()))
            def timestamp(group):
                h, m, s, ms = group
                return ((h * 3600 + m * 60 + s) * 1000 + ms) * 1000000
            cues.append(SubtitleCue(int(lines[0]), timestamp(values[:4]), timestamp(values[4:]),
                                    "\n".join(lines[2:]).strip()))
        return cls(cues)

    def active(self, pts_ns, *, eof=False):
        _int(pts_ns, "source pts_ns")
        if not isinstance(eof, bool):
            raise ValueError("eof must be bool")
        if eof:
            return ()
        found = []
        self._tree.collect(pts_ns, found)
        return tuple(cue for _, cue in sorted(found, key=lambda item: item[0]))


class _FontCoverage:
    """Read cmap 4/12 in the exact TTF/OTF bytes, not a substituted OS font."""
    def __init__(self, data):
        self.data, self.tables = data, []
        def u16(at):
            return struct.unpack_from(">H", data, at)[0]
        def u32(at):
            return struct.unpack_from(">I", data, at)[0]
        try:
            base = u32(12) if data[:4] == b"ttcf" else 0
            count = u16(base + 4)
            if not 1 <= count <= 256:
                raise ValueError("Invalid font table count")
            cmap = None
            for i in range(count):
                entry = base + 12 + 16 * i
                offset, length = u32(entry + 8), u32(entry + 12)
                if offset + length > len(data):
                    raise ValueError("Font table outside file")
                if data[entry:entry + 4] == b"cmap":
                    cmap = offset, offset + length
            if cmap is None:
                raise ValueError("Font has no Unicode cmap")
            start, end = cmap
            records = u16(start + 2)
            if records > 256 or start + 4 + 8 * records > end:
                raise ValueError("Invalid font cmap records")
            candidates = []
            for i in range(records):
                entry = start + 4 + i * 8
                platform, encoding = u16(entry), u16(entry + 2)
                if platform != 0 and (platform != 3 or encoding not in (1, 10)):
                    continue
                offset = start + u32(entry + 4)
                if offset + 4 > end:
                    raise ValueError("Invalid cmap offset")
                form = u16(offset)
                if form in (4, 12):
                    candidates.append(((0 if form == 12 else 2) + (0 if platform == 3 else 1), form, offset))
            # One preferred Unicode map; duplicate encoding records cannot
            # multiply the font's in-memory group table. Prefer UCS-4 coverage.
            for _, form, offset in sorted(candidates)[:1]:
                if form == 12:
                    if offset + 16 > end:
                        raise ValueError("Truncated cmap12 header")
                    length, groups = u32(offset + 4), u32(offset + 12)
                    if groups > 65536 or length < 16 + groups * 12 or offset + length > end:
                        raise ValueError("Invalid cmap12")
                    spans = tuple(struct.unpack_from(">III", data, offset + 16 + j * 12) for j in range(groups))
                    previous = -1
                    for first, last, glyph in spans:
                        if first <= previous or first > last or last > 0x10ffff:
                            raise ValueError("Invalid cmap12 ranges")
                        previous = last
                    self.tables.append((12, tuple(first for first, _, _ in spans), spans))
                elif form == 4:
                    if offset + 8 > end:
                        raise ValueError("Truncated cmap4 header")
                    length, segments = u16(offset + 2), u16(offset + 6) // 2
                    if not segments or length < 16 + 8 * segments or offset + length > end:
                        raise ValueError("Invalid cmap4")
                    ends = struct.unpack_from(f">{segments}H", data, offset + 14)
                    if tuple(sorted(ends)) != ends:
                        raise ValueError("Invalid cmap4 ranges")
                    self.tables.append((4, ends, (offset, length, segments)))
            if not self.tables:
                raise ValueError("Font needs a Unicode cmap format 4 or 12")
        except (struct.error, IndexError) as error:
            raise ValueError("Truncated font cmap") from error

    def contains(self, char):
        code = ord(char)
        for form, positions, values in self.tables:
            if form == 12:
                index = bisect_right(positions, code) - 1
                if index >= 0:
                    first, last, glyph = values[index]
                    if first <= code <= last and glyph + code - first:
                        return True
            elif code <= 0xffff:
                index = bisect_left(positions, code)
                if index == len(positions):
                    continue
                offset, length, count = values
                start_at = offset + 16 + count * 2
                first = struct.unpack_from(">H", self.data, start_at + index * 2)[0]
                if code < first:
                    continue
                delta = struct.unpack_from(">h", self.data, start_at + count * 2 + index * 2)[0]
                range_at = start_at + count * 4 + index * 2
                relative = struct.unpack_from(">H", self.data, range_at)[0]
                if relative == 0:
                    glyph = code
                else:
                    at = range_at + relative + 2 * (code - first)
                    if at + 2 > offset + length:
                        raise ValueError("Font glyph reference outside cmap")
                    glyph = struct.unpack_from(">H", self.data, at)[0]
                    if glyph == 0:
                        continue
                if (glyph + delta) & 0xffff:
                    return True
        return False


class FlatSubtitleRenderer:
    """CPU BGRA compositor; opaque tight backing keeps even antialias pixels flat."""
    def __init__(self, font_path, font_size_px=32, *, visible=True, margin_px=24,
                 padding_px=8, cache_bytes=4 * 1024 * 1024):
        self._lock = threading.RLock()
        self.font_path = str(Path(font_path).resolve())
        with Path(font_path).open("rb") as stream:
            self._font_bytes = stream.read(MAX_FONT_BYTES + 1)
        if len(self._font_bytes) > MAX_FONT_BYTES:
            raise ValueError("Font exceeds 32 MiB limit")
        self._coverage = _FontCoverage(self._font_bytes)
        self._fonts, self._cache = OrderedDict(), OrderedDict()
        self._cache_size = 0
        self._cache_limit = _int(cache_bytes, "cache_bytes", 0, 16 * 1024 * 1024)
        self.margin_px = _int(margin_px, "margin_px", 0, 512)
        self.padding_px = _int(padding_px, "padding_px", 0, 64)
        self.font_size_px = _int(font_size_px, "font_size_px", 8, 128)
        self.visible = False
        self.last_diagnostics = {}
        self.set_visible(visible)
        self._font()

    def set_visible(self, visible):
        if not isinstance(visible, bool):
            raise ValueError("visible must be bool")
        with self._lock:
            self.visible = visible

    def set_size(self, font_size_px):
        size = _int(font_size_px, "font_size_px", 8, 128)
        with self._lock:
            self.font_size_px = size
            self._font()

    def _font(self):
        if self.font_size_px not in self._fonts:
            self._fonts[self.font_size_px] = ImageFont.truetype(BytesIO(self._font_bytes), self.font_size_px,
                                                              layout_engine=ImageFont.Layout.BASIC)
            while len(self._fonts) > 2:
                self._fonts.popitem(last=False)
        self._fonts.move_to_end(self.font_size_px)
        return self._fonts[self.font_size_px]

    def _overlay(self, text, eye_width, height):
        key = (text, eye_width, height, self.font_size_px, self.margin_px, self.padding_px)
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key], True
        if len(text) > MAX_RENDER_CHARS:
            raise ValueError("Active subtitles exceed 4096 render characters")
        for char in set(text) - {"\n", "\t"}:
            if not self._coverage.contains(char):
                raise ValueError(f"Selected font lacks glyph U+{ord(char):04X}")
        font = self._font()
        max_width = eye_width - 2 * (self.margin_px + self.padding_px)
        if max_width < 1:
            raise ValueError("Subtitle margins leave no horizontal space")
        measure = ImageDraw.Draw(Image.new("RGB", (1, 1)))
        lines = []
        for raw in text.expandtabs(4).split("\n"):
            remaining = raw.strip()
            while remaining:
                low, high = 1, len(remaining)
                fitting = 0
                while low <= high:
                    mid = (low + high) // 2
                    box = measure.textbbox((0, 0), remaining[:mid], font=font)
                    if box[2] - box[0] <= max_width:
                        fitting, low = mid, mid + 1
                    else:
                        high = mid - 1
                if not fitting:
                    raise ValueError("Subtitle glyph cannot fit the selected eye width")
                if fitting < len(remaining):
                    word = remaining.rfind(" ", 0, fitting + 1)
                    if word > 0:
                        fitting = word
                lines.append(remaining[:fitting].rstrip())
                remaining = remaining[fitting:].lstrip()
                if len(lines) > MAX_LINES:
                    raise ValueError("Subtitle layout exceeds 8 lines; select a smaller font")
        boxes = [measure.textbbox((0, 0), line, font=font) for line in lines]
        if not boxes:
            raise ValueError("Subtitle has no visible text")
        spacing = max(1, self.font_size_px // 5)
        width = max(box[2] - box[0] for box in boxes) + 2 * self.padding_px
        overlay_height = sum(box[3] - box[1] for box in boxes) + spacing * (len(boxes) - 1) + 2 * self.padding_px
        if overlay_height > height // 3 or overlay_height + 2 * self.margin_px > height:
            raise ValueError("Subtitle layout exceeds one third of eye height; select a smaller font")
        image = Image.new("RGBA", (width, overlay_height), (0, 0, 0, 255))
        draw = ImageDraw.Draw(image)
        y = self.padding_px
        for line, box in zip(lines, boxes):
            x = (width - (box[2] - box[0])) // 2
            draw.text((x - box[0], y - box[1]), line, font=font, fill=(255, 255, 255, 255))
            y += box[3] - box[1] + spacing
        overlay = np.asarray(image)[:, :, [2, 1, 0, 3]].copy()
        overlay.setflags(write=False)
        while self._cache and (self._cache_size + overlay.nbytes > self._cache_limit or len(self._cache) >= 16):
            _, previous = self._cache.popitem(last=False)
            self._cache_size -= previous.nbytes
        if overlay.nbytes <= self._cache_limit:
            self._cache[key] = overlay
            self._cache_size += overlay.nbytes
        return overlay, False

    def composite(self, bgra, pts_ns, track, *, eof=False):
        if not isinstance(track, SubtitleTrack):
            raise TypeError("track must be SubtitleTrack")
        cues = track.active(pts_ns, eof=eof)
        if not isinstance(bgra, np.ndarray) or bgra.dtype != np.uint8 or bgra.ndim != 3 or bgra.shape[2] != 4:
            raise ValueError("Expected uint8 HxWx4 Full-SBS BGRA")
        height, width = bgra.shape[:2]
        if height < 16 or width < 32 or width % 2 or bgra.nbytes > MAX_FRAME_BYTES:
            raise ValueError("Invalid or oversized Full-SBS frame")
        with self._lock:
            output = np.array(bgra, copy=True, order="C")
            diagnostics = {"pts_ns": pts_ns, "cue_indices": [cue.index for cue in cues], "visible": self.visible,
                           "eof": eof, "overlay_rect": None, "cache_hit": False, "quest_verified": False}
            self.last_diagnostics = diagnostics
            if self.visible and cues:
                text = unicodedata.normalize("NFC", "\n".join(cue.text for cue in cues))
                try:
                    overlay, hit = self._overlay(text, width // 2, height)
                except (ValueError, OSError) as error:
                    diagnostics["error"] = str(error)
                    raise
                oh, ow = overlay.shape[:2]
                x, y = (width // 2 - ow) // 2, height - self.margin_px - oh
                output[y:y + oh, x:x + ow] = overlay
                output[y:y + oh, width // 2 + x:width // 2 + x + ow] = overlay
                diagnostics.update(overlay_rect=(x, y, ow, oh), cache_hit=hit)
            diagnostics.update(cache_bytes=self._cache_size, cache_entries=len(self._cache), font_cache_entries=len(self._fonts))
            self.last_diagnostics = diagnostics
            return output

    def composite_frame(self, frame, pts_ns, track, *, eof=False):
        from .stereo import StereoFrame
        if not isinstance(frame, StereoFrame):
            raise TypeError("Expected StereoFrame")
        return replace(frame, bgra=self.composite(frame.bgra, pts_ns, track, eof=eof))

"""Optional real NVENC recording used to isolate host/receiver failures."""

from fractions import Fraction
from pathlib import Path

import av
import numpy as np


class NvencRecorder:
    def __init__(self, path: Path, width: int, height: int, fps: int = 30):
        if fps <= 0:
            raise ValueError("Frame rate must be positive")
        self.container = av.open(str(path), "w")
        self.stream = self.container.add_stream("h264_nvenc", rate=fps)
        self.stream.width, self.stream.height = width, height
        self.stream.pix_fmt = "yuv420p"
        self.stream.bit_rate = 12_000_000
        self.stream.codec_context.options = {"preset": "p1", "tune": "ull", "zerolatency": "1", "bf": "0"}
        self.time_base = Fraction(1, 90000)
        self.stream.codec_context.time_base = self.time_base
        self.origin_ns = None
        self.last_pts = -1

    def write(self, image: np.ndarray, capture_ns: int):
        frame = av.VideoFrame.from_ndarray(image, format="bgra")
        if self.origin_ns is None:
            self.origin_ns = capture_ns
        pts = round((capture_ns - self.origin_ns) * 90000 / 1e9)
        if pts <= self.last_pts:
            raise ValueError("Recording timestamps must increase")
        frame.pts, frame.time_base = pts, self.time_base
        for packet in self.stream.encode(frame):
            self.container.mux(packet)
        self.last_pts = pts

    def close(self):
        try:
            for packet in self.stream.encode(None):
                self.container.mux(packet)
        finally:
            self.container.close()

"""Physical-pixel rectangles and flat panel input mapping.

Coordinates describe pixel edges; right/bottom bounds are exclusive. This maps
an unwarped flat image, not a generated stereo image back through its depth warp.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from numbers import Integral


@dataclass(frozen=True, slots=True)
class ScreenRect:
    left: int
    top: int
    width: int
    height: int

    def __post_init__(self) -> None:
        for name in ("left", "top", "width", "height"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, Integral):
                raise TypeError(f"{name} must be an integer physical-pixel value")
            object.__setattr__(self, name, int(value))
        if self.width <= 0 or self.height <= 0:
            raise ValueError("rectangle width and height must be positive")

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height

    def contains(self, other: ScreenRect) -> bool:
        return (
            self.left <= other.left and self.top <= other.top
            and other.right <= self.right and other.bottom <= self.bottom
        )

    def as_mss(self) -> dict[str, int]:
        return {"left": self.left, "top": self.top,
                "width": self.width, "height": self.height}

    def translated(self, dx: int, dy: int) -> ScreenRect:
        return ScreenRect(self.left + dx, self.top + dy, self.width, self.height)


def validate_roi(roi: ScreenRect, desktop: ScreenRect) -> ScreenRect:
    """Reject out-of-bounds input instead of silently moving the selected area."""
    if not desktop.contains(roi):
        raise ValueError(f"ROI {roi} is outside desktop bounds {desktop}")
    return roi


def parse_rect(value: str) -> ScreenRect:
    """Parse LEFT,TOP,WIDTH,HEIGHT, including negative desktop coordinates."""
    parts = value.split(",")
    if len(parts) != 4:
        raise ValueError("rectangle must be LEFT,TOP,WIDTH,HEIGHT")
    try:
        return ScreenRect(*(int(part.strip()) for part in parts))
    except (TypeError, ValueError) as exc:
        raise ValueError("rectangle requires integer LEFT,TOP and positive WIDTH,HEIGHT") from exc


@dataclass(frozen=True, slots=True)
class SourceGeometry:
    bounds: ScreenRect
    generation: int = 0

    def __post_init__(self) -> None:
        if isinstance(self.generation, bool) or not isinstance(self.generation, Integral):
            raise TypeError("generation must be an integer")
        if self.generation < 0:
            raise ValueError("generation must be nonnegative")


@dataclass(frozen=True, slots=True)
class LetterboxTransform:
    """Fit a physical source rectangle into an unwarped display viewport.

    No OS input is injected here. Callers must check the displayed geometry
    generation before using the result, and separately account for SBS UVs.
    """

    source: ScreenRect
    viewport_width: int
    viewport_height: int

    def __post_init__(self) -> None:
        for value in (self.viewport_width, self.viewport_height):
            if isinstance(value, bool) or not isinstance(value, Integral) or value <= 0:
                raise ValueError("viewport dimensions must be positive integers")

    @property
    def scale(self) -> float:
        return min(self.viewport_width / self.source.width,
                   self.viewport_height / self.source.height)

    @property
    def content_size(self) -> tuple[float, float]:
        # Keep the fitted axis exact: multiplying a rounded float scale back
        # by source size can otherwise create a negative sub-pixel black bar.
        if self.viewport_width * self.source.height <= self.viewport_height * self.source.width:
            return (float(self.viewport_width),
                    self.viewport_width * self.source.height / self.source.width)
        return (self.viewport_height * self.source.width / self.source.height,
                float(self.viewport_height))

    @property
    def offset(self) -> tuple[float, float]:
        width, height = self.content_size
        return ((self.viewport_width - width) / 2,
                (self.viewport_height - height) / 2)

    def display_to_source(self, x: float, y: float) -> tuple[float, float] | None:
        """Return source edge coordinates; black bars/outside return None."""
        if not math.isfinite(x) or not math.isfinite(y):
            return None
        ox, oy = self.offset
        width, height = self.content_size
        if not (ox <= x < ox + width and oy <= y < oy + height):
            return None
        local_x = (x - ox) * self.source.width / width
        local_y = (y - oy) * self.source.height / height
        if not (0 <= local_x < self.source.width and 0 <= local_y < self.source.height):
            return None
        return self.source.left + local_x, self.source.top + local_y

    def display_to_pixel(self, x: float, y: float) -> tuple[int, int] | None:
        point = self.display_to_source(x, y)
        if point is None:
            return None
        # floor chooses the pixel containing this point, also at negative origins.
        return math.floor(point[0]), math.floor(point[1])

    def uv_to_pixel(self, u: float, v: float) -> tuple[int, int] | None:
        if not (0 <= u < 1 and 0 <= v < 1):
            return None
        return self.display_to_pixel(u * self.viewport_width, v * self.viewport_height)

    def source_to_display(self, x: float, y: float) -> tuple[float, float]:
        if not math.isfinite(x) or not math.isfinite(y):
            raise ValueError("source coordinates must be finite")
        ox, oy = self.offset
        width, height = self.content_size
        return (ox + (x - self.source.left) * width / self.source.width,
                oy + (y - self.source.top) * height / self.source.height)


def pixel_to_windows_absolute(x: int, y: int, desktop: ScreenRect) -> tuple[int, int]:
    """Normalize physical desktop pixels for ABSOLUTE | VIRTUALDESK.

    Use pixel centers in the 65536-bin input range. This is a pure conversion;
    actual SendInput delivery/rounding and UIPI require a Windows integration test.
    """
    if isinstance(x, bool) or isinstance(y, bool) or not isinstance(x, Integral) or not isinstance(y, Integral):
        raise TypeError("input coordinates must be integer pixels")
    if not (desktop.left <= x < desktop.right and desktop.top <= y < desktop.bottom):
        raise ValueError("input coordinate is outside the virtual desktop")
    return (min(65535, ((2 * (x - desktop.left) + 1) * 65536) // (2 * desktop.width)),
            min(65535, ((2 * (y - desktop.top) + 1) * 65536) // (2 * desktop.height)))


def require_generation(displayed: int, current: int) -> None:
    if displayed != current:
        raise ValueError(f"stale geometry: displayed {displayed}, current {current}")

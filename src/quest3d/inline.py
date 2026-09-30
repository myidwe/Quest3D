"""Stereo inside a physical desktop ROI, preserving the complete 2D desktop.

This module does not select/track windows, inject input, infer depth, or prove
Quest presentation. The caller must capture in physical pixels and bind depth
to the *same* immutable capture frame, geometry and inference crop. A numeric
rectangle alone cannot reveal that a caller accidentally supplied DPI-scaled
logical coordinates.

The whole desktop is fitted exactly once by the existing 2D synthesizer. Image
pixels are never resized independently inside the ROI. Warped samples that
would leave the safe ROI interior, or touch protected UI, retain the original
pixel. This conservative boundary policy leaves a flat rim rather than cloning
neighboring address bars into the video. It is not an occlusion reconstruction
or an inverse mapping for clicks.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from numbers import Integral, Real

import cv2
import numpy as np
import torch
import torch.nn.functional as F

from .depth import DepthResult
from .geometry import ScreenRect, SourceGeometry, validate_roi
from .stereo import StereoFrame, StereoSynthesizer


Image = np.ndarray | torch.Tensor


def _validate_image(image: Image, source: SourceGeometry) -> None:
    if not isinstance(image, (np.ndarray, torch.Tensor)):
        raise TypeError("Desktop must be a numpy array or torch tensor")
    expected_type = torch.uint8 if isinstance(image, torch.Tensor) else np.uint8
    if image.dtype != expected_type or image.ndim != 3 or image.shape[2] != 4:
        raise ValueError("Desktop must be HWC uint8 BGRA")
    if tuple(image.shape[:2]) != (source.bounds.height, source.bounds.width):
        raise ValueError("Desktop dimensions must match physical source bounds; do not pass DPI-scaled bounds")


def _validate_frame_id(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, Integral) or value < 0:
        raise ValueError("Frame ID must be a nonnegative integer")


def _validate_generation(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, Integral) or value < 0:
        raise ValueError("Depth generation must be a nonnegative integer")


@dataclass(frozen=True, slots=True)
class InlineGeometry:
    """All rectangles are absolute physical desktop pixel edges, right-exclusive.

    ``roi`` is the exact inference crop. Optional ``content`` is an explicit
    video/image rectangle inside it; surrounding player letterbox pixels stay
    flat. Nothing is inferred from black colors and no invalid ROI is clamped.
    Changing bounds, ROI or content must accompany the caller's geometry
    generation update. The compositor also compares the full descriptors.
    """

    source: SourceGeometry
    roi: ScreenRect
    content: ScreenRect | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.source, SourceGeometry) or not isinstance(self.roi, ScreenRect):
            raise TypeError("Inline geometry requires physical SourceGeometry and ScreenRect")
        validate_roi(self.roi, self.source.bounds)
        if self.content is not None:
            if not isinstance(self.content, ScreenRect):
                raise TypeError("Content must be a physical ScreenRect")
            if not self.roi.contains(self.content):
                raise ValueError("Content must remain inside the inference ROI")

    @property
    def active_rect(self) -> ScreenRect:
        return self.content if self.content is not None else self.roi

    def crop_for_depth(self, desktop: Image) -> Image:
        """Return the exact inference crop as a view; the caller owns its lifetime.

        Do not recycle/mutate a capture texture until inference and synthesis
        finish. This function intentionally does not copy an entire GPU frame.
        """
        _validate_image(desktop, self.source)
        x = self.roi.left - self.source.bounds.left
        y = self.roi.top - self.source.bounds.top
        return desktop[y:y + self.roi.height, x:x + self.roi.width]

    def bind_depth(self, result: DepthResult) -> RoiDepth:
        """Associate actual inference output with the crop descriptor used for it."""
        return RoiDepth(result, self)


@dataclass(frozen=True, slots=True)
class RoiDepth:
    """DepthResult plus its capture/crop identity, not a confidence estimate.

    DepthResult.input_shape is the AI preprocessed size, not the source ROI
    size. Tensor resolution may differ from either; it describes the full ROI.
    """

    result: DepthResult
    geometry: InlineGeometry

    def __post_init__(self) -> None:
        if not isinstance(self.result, DepthResult) or not isinstance(self.geometry, InlineGeometry):
            raise TypeError("RoiDepth requires DepthResult and InlineGeometry")
        _validate_frame_id(self.result.frame_id)
        _validate_generation(self.result.generation)
        if self.result.generation != self.geometry.source.generation:
            raise ValueError("Depth belongs to a different geometry generation")


class InlineSynthesizer:
    """Produce Full-SBS with unchanged full-desktop eye size and content_rect.

    Strength is in output-eye pixels (full left/right disparity), never in AI
    pixels or ROI pixels. Outside the conservatively projected active rect,
    every BGRA pixel is byte-identical to ``original_2d`` for the same input.
    The first implementation reuses the existing CPU-returning 2D baseline;
    it makes no zero-copy or real-time performance claim.
    """

    def __init__(self, eye_width: int = 1280, eye_height: int = 720,
                 disparity_px: float = 12.0, convergence: float = 0.5):
        for value in (eye_width, eye_height):
            if isinstance(value, bool) or not isinstance(value, Integral):
                raise ValueError("Eye dimensions must be integer pixels")
        self._baseline = StereoSynthesizer(int(eye_width), int(eye_height), 0)
        self.width, self.height = int(eye_width), int(eye_height)
        self.disparity_px = disparity_px
        self.convergence = convergence
        self._validate_settings()
        self._previous_geometry: InlineGeometry | None = None
        self._previous_thumbnail: torch.Tensor | None = None
        self._depth_limits: tuple[float, float] | None = None

    def _validate_settings(self) -> None:
        for name, value, maximum in (("Disparity", self.disparity_px, self.width * 0.04),
                                     ("Convergence", self.convergence, 1.0)):
            if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value) or not 0 <= value <= maximum:
                raise ValueError(f"{name} must be finite and between 0 and {maximum}")

    def _reset(self) -> None:
        self._previous_geometry = None
        self._previous_thumbnail = None
        self._depth_limits = None

    def original_2d(self, desktop: Image, *, frame_id: int,
                    geometry: InlineGeometry) -> StereoFrame:
        """Return immediately without depth; both whole-desktop eyes are identical."""
        if not isinstance(geometry, InlineGeometry):
            raise TypeError("Expected InlineGeometry")
        _validate_image(desktop, geometry.source)
        _validate_frame_id(frame_id)
        self._reset()
        return self._baseline.original_2d(desktop, frame_id=frame_id,
                                          generation=geometry.source.generation)

    @staticmethod
    def _project_inside(rect: ScreenRect, source: ScreenRect,
                        fitted: tuple[int, int, int, int]) -> ScreenRect | None:
        """Keep only destination pixel footprints wholly inside physical rect.

        Integer arithmetic avoids float rounding across odd ROI/DPI edges.
        This rounds only the output support inward; it never changes the ROI.
        """
        ox, oy, fw, fh = fitted
        left = ((rect.left - source.left) * fw + source.width - 1) // source.width
        top = ((rect.top - source.top) * fh + source.height - 1) // source.height
        right = (rect.right - source.left) * fw // source.width
        bottom = (rect.bottom - source.top) * fh // source.height
        if right <= left or bottom <= top:
            return None
        return ScreenRect(ox + left, oy + top, right - left, bottom - top)

    def eye_active_rect(self, geometry: InlineGeometry) -> ScreenRect | None:
        """Describe eligible output pixels, excluding full-desktop/player bars."""
        source = geometry.source.bounds
        scale = min(self.width / source.width, self.height / source.height)
        fw, fh = max(1, round(source.width * scale)), max(1, round(source.height * scale))
        fitted = ((self.width - fw) // 2, (self.height - fh) // 2, fw, fh)
        return self._project_inside(geometry.active_rect, source, fitted)

    @staticmethod
    def _validate_mask(flat_mask: Image | None, desktop: Image) -> None:
        if flat_mask is None:
            return
        if not isinstance(flat_mask, (np.ndarray, torch.Tensor)) or tuple(flat_mask.shape) != tuple(desktop.shape[:2]):
            raise ValueError("Flat mask must match physical desktop dimensions")
        valid_types = (torch.bool, torch.uint8) if isinstance(flat_mask, torch.Tensor) else (np.bool_, np.uint8)
        if flat_mask.dtype not in valid_types:
            raise ValueError("Flat mask must contain boolean or uint8 protection flags")

    @staticmethod
    def _protected_pixels(flat_mask: Image | None, desktop: Image, fitted: tuple[int, int, int, int],
                          active: ScreenRect, device: torch.device) -> torch.Tensor:
        if flat_mask is None:
            return torch.zeros((1, 1, active.height, active.width), dtype=torch.bool, device=device)
        ox, oy, fw, fh = fitted
        if isinstance(desktop, torch.Tensor):
            mask = torch.as_tensor(flat_mask, device=device).ne(0).float()[None, None]
            # Match the tensor baseline's area footprint, not nearest-neighbor.
            protected = F.interpolate(mask, size=(fh, fw), mode="area")[0, 0] > 0
        else:
            if isinstance(flat_mask, torch.Tensor):
                flat_mask = flat_mask.detach().cpu().numpy()
            mask = (flat_mask != 0).astype(np.float32)
            protected = torch.from_numpy(cv2.resize(mask, (fw, fh), interpolation=cv2.INTER_AREA) > 0).to(device)
        x, y = active.left - ox, active.top - oy
        return protected[y:y + active.height, x:x + active.width][None, None]

    @torch.inference_mode()
    def synthesize(self, desktop: Image, depth: RoiDepth, *, frame_id: int,
                   geometry: InlineGeometry, flat_mask: Image | None = None) -> StereoFrame:
        """Warp only this frame's selected content; reject stale/misbound depth.

        Zero strength still validates supplied frame/crop identity. Explicit
        2D return uses original_2d and does not wait for AI or require depth.
        """
        self._validate_settings()
        if not isinstance(geometry, InlineGeometry) or not isinstance(depth, RoiDepth):
            raise TypeError("Expected InlineGeometry and bound RoiDepth")
        _validate_image(desktop, geometry.source)
        _validate_frame_id(frame_id)
        self._validate_mask(flat_mask, desktop)
        if depth.geometry != geometry:
            raise ValueError("Refusing depth from a different physical source, ROI or content geometry")
        result = depth.result
        _validate_frame_id(result.frame_id)
        _validate_generation(result.generation)
        if result.frame_id != frame_id or result.generation != geometry.source.generation:
            raise ValueError("Refusing depth from a different RGB frame or geometry generation")
        if self.disparity_px == 0:
            return self.original_2d(desktop, frame_id=frame_id, geometry=geometry)
        raw = result.tensor
        if not isinstance(raw, torch.Tensor) or raw.ndim != 3 or raw.shape[0] != 1 or min(raw.shape[-2:]) < 1 or not raw.is_floating_point():
            raise ValueError("Depth tensor must be floating point with shape [1, H, W]")
        if not bool(torch.isfinite(raw).all().item()):
            raise ValueError("Depth model produced non-finite values")
        packed_gpu = None
        if isinstance(desktop, torch.Tensor) and desktop.is_cuda and desktop.device == raw.device:
            # Use the identical fit/rounding as original_2d, retaining it on the
            # GPU until both eyes are complete. Avoid full-image readback and
            # the former upload of the ROI before warping.
            fitted_pixels, fitted = self._baseline._fit(desktop)
            fx, fy, fw, fh = fitted
            packed_gpu = torch.zeros((self.height, self.width * 2, 4),
                                      dtype=torch.uint8, device=desktop.device)
            packed_gpu[..., 3] = 255
            packed_gpu[fy:fy + fh, fx:fx + fw] = fitted_pixels
            packed_gpu[fy:fy + fh, self.width + fx:self.width + fx + fw] = fitted_pixels
            baseline = None
        else:
            baseline = self._baseline.original_2d(desktop, frame_id=frame_id,
                                                  generation=geometry.source.generation)
            fitted = baseline.content_rect
        active = self._project_inside(geometry.active_rect, geometry.source.bounds, fitted)
        if active is None:
            raise ValueError("Selected content has no complete output pixel; enlarge the ROI or eye resolution")
        ox, oy, fw, fh = fitted
        ah, aw = active.height, active.width
        device = raw.device
        y, x = torch.meshgrid(torch.arange(ah, device=device, dtype=torch.float32),
                               torch.arange(aw, device=device, dtype=torch.float32), indexing="ij")
        # Sample depth at the globally fitted desktop's pixel centers, avoiding
        # an independent ROI resize with subtly different rounding/alignment.
        source = geometry.source.bounds
        dx = ((x + active.left - ox + 0.5) * source.width / fw
              - (geometry.roi.left - source.left)) * 2 / geometry.roi.width - 1
        dy = ((y + active.top - oy + 0.5) * source.height / fh
              - (geometry.roi.top - source.top)) * 2 / geometry.roi.height - 1
        if packed_gpu is not None:
            original = packed_gpu[active.top:active.bottom, active.left:active.right, :3]
            rgb = original.permute(2, 0, 1)[None].float()
        else:
            original = baseline.bgra[active.top:active.bottom, active.left:active.right, :3]
            rgb = torch.from_numpy(np.ascontiguousarray(original)).to(device).permute(2, 0, 1)[None].float()
        thumbnail = F.interpolate(rgb, size=(18, 32), mode="bilinear", align_corners=False) / 255.0
        reset = self._previous_geometry != geometry or self._previous_thumbnail is None
        if not reset:
            reset = self._previous_thumbnail.device != device or float((thumbnail - self._previous_thumbnail).abs().mean().item()) > 0.22
        # Normalize before resampling. Bilinear float roundoff on a constant
        # depth field must not become a spurious full-range disparity pattern.
        low, high = (float(value.item()) for value in torch.aminmax(raw))
        if not reset and self._depth_limits is not None:
            old_low, old_high = self._depth_limits
            low, high = old_low * 0.9 + low * 0.1, old_high * 0.9 + high * 0.1
        normalized_raw = ((raw.float() - low) / max(high - low, 1e-6)).clamp(0, 1)
        normalized = F.grid_sample(normalized_raw[:, None], torch.stack((dx, dy), -1)[None],
                                    mode="bilinear", padding_mode="border", align_corners=False)[0, 0]
        protected = self._protected_pixels(flat_mask, desktop, fitted, active, device)
        shift = (normalized - self.convergence) * self.disparity_px * 0.5
        shift = shift.masked_fill(protected[0, 0], 0)
        sample_x = torch.stack((x - shift, x + shift))
        gx = (sample_x + 0.5) * 2 / aw - 1
        gy = ((y + 0.5) * 2 / ah - 1).expand(2, -1, -1)
        grids = torch.stack((gx, gy), -1)
        eyes = F.grid_sample(rgb.expand(2, -1, -1, -1), grids,
                             mode="bilinear", padding_mode="border", align_corners=False)
        # Both source and destination UI protection matter. Invalid samples are
        # restored from the identical baseline, never clipped to foreign pixels.
        touches_ui = F.grid_sample(protected.float().expand(2, -1, -1, -1), grids,
                                   mode="bilinear", padding_mode="border", align_corners=False) > 0
        outside = ((sample_x < 0) | (sample_x > aw - 1))[:, None]
        eyes = torch.where(touches_ui | protected | outside, rgb.expand(2, -1, -1, -1), eyes)
        pixels = eyes.round().clamp(0, 255).to(torch.uint8).permute(0, 2, 3, 1)
        if packed_gpu is None:
            pixels = pixels.cpu().numpy()
        packed = packed_gpu if packed_gpu is not None else baseline.bgra
        for eye in range(2):
            left = eye * self.width + active.left
            packed[active.top:active.bottom, left:left + aw, :3] = pixels[eye]
        if packed_gpu is not None:
            owned_cpu = torch.empty(packed_gpu.shape, dtype=torch.uint8, device="cpu", pin_memory=True)
            owned_cpu.copy_(packed_gpu, non_blocking=True)
            torch.cuda.current_stream(raw.device).synchronize()
            packed = owned_cpu.numpy()
        # Alpha is retained byte-for-byte even within the active region.
        self._previous_geometry = geometry
        self._previous_thumbnail = thumbnail
        self._depth_limits = (low, high)
        return StereoFrame(packed, frame_id, geometry.source.generation, "3d", reset,
                           self._depth_limits, fitted)

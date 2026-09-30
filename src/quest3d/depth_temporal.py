"""Conservative, motion-gated two-frame stabilization of normalized depth.

The only history is the previous UNFILTERED normalized depth and matching RGB
guide. Filtered output is never fed back, so no sample survives beyond one next
accepted frame. Guides must already cover the same depth-grid coordinates, on
the same device; this module performs no resize or image device transfer.

The caller owns RGB/depth identity, source/revision and normalization coordinates.
Reset on source/revision, 2D/resubmit, scene or material normalization changes.
timestamp_ns is the source capture timestamp, never completion/presentation time.
This cannot distinguish tiny real depth changes from model noise, or detect all
motion in an aliased guide. It protects visible boundaries and limits any blend
to one preceding input, at most half weight, with capture-age decay.
"""

from dataclasses import dataclass
import math

import torch
import torch.nn.functional as F


STAT_NAMES = ("mean_history_weight", "blended_fraction", "rgb_rejected_fraction",
              "edge_rejected_fraction", "depth_rejected_fraction", "scene_cut")


@dataclass(frozen=True)
class TemporalDepthDiagnostics:
    frame_id: int
    generation: int
    timestamp_ns: int
    previous_frame_id: int | None
    history_age_ns: int | None
    reset_reason: str | None
    history_weight_ceiling: float
    stats: torch.Tensor  # six float32 scalars, on input device; no forced readback
    stat_names: tuple[str, ...] = STAT_NAMES


@dataclass(frozen=True)
class TemporalDepthResult:
    depth: torch.Tensor
    diagnostics: TemporalDepthDiagnostics


@dataclass(frozen=True)
class _History:
    depth: torch.Tensor
    guide: torch.Tensor
    frame_id: int
    generation: int
    timestamp_ns: int


class TemporalDepthStabilizer:
    """One owner/ordered caller; input errors leave the previous history intact.

    RGB/depth thresholds are conservative candidate constants, not measured
    model confidence. Spatial operations are only local range/dilated gates;
    no spatial depth averaging, flow, global model, or filtered-history feedback.
    """

    _RGB_CHANGE = .02
    _DEPTH_CHANGE = .025
    _RGB_EDGE = .04
    _DEPTH_EDGE = .015
    _CUT_RGB_MEAN = .10

    def __init__(self, *, max_history_weight: float = .5, max_gap_ms: float = 200):
        if (isinstance(max_history_weight, bool) or not isinstance(max_history_weight, (int, float))
                or not math.isfinite(max_history_weight) or not 0 <= max_history_weight <= .5):
            raise ValueError("History weight must be finite and in [0, 0.5]")
        if (isinstance(max_gap_ms, bool) or not isinstance(max_gap_ms, (int, float))
                or not math.isfinite(max_gap_ms) or not 0 < max_gap_ms <= 200):
            raise ValueError("History gap must be finite and in (0, 200] milliseconds")
        self.max_history_weight = float(max_history_weight)
        self.max_gap_ns = max(1, round(max_gap_ms * 1_000_000))
        self._history: _History | None = None
        self._pending_reset = "no_history"

    def reset(self) -> None:
        """Drop retained images and acceptance identity for an explicit boundary.

        A control-resubmitted frame may legitimately have the same identity after
        this call. It becomes a fresh unfiltered input, never a second blend.
        """
        self._history = None
        self._pending_reset = "manual_reset"

    @staticmethod
    def _validate(depth, guide, frame_id, generation, timestamp_ns, reset):
        for name, value in (("frame_id", frame_id), ("generation", generation), ("timestamp_ns", timestamp_ns)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        if not isinstance(reset, bool):
            raise ValueError("reset must be boolean")
        if not isinstance(depth, torch.Tensor) or not isinstance(guide, torch.Tensor):
            raise ValueError("Depth and guide must be tensors")
        if depth.ndim != 3 or depth.shape[0] != 1 or min(depth.shape[1:]) < 1:
            raise ValueError("Normalized depth must have nonempty shape [1, h, w]")
        if guide.shape != (1, 3, *depth.shape[1:]):
            raise ValueError("RGB guide must have matching shape [1, 3, h, w]")
        if depth.device != guide.device or depth.device.type not in ("cpu", "cuda"):
            raise ValueError("Depth and guide must share a CPU or CUDA device")
        supported = (torch.float16, torch.bfloat16, torch.float32, torch.float64)
        if depth.dtype not in supported or guide.dtype not in supported:
            raise ValueError("Depth and guide must have floating dtype")
        # Exactly one scalar validation readback, also before explicit resets.
        # Diagnostic reductions remain on device and require no host branch.
        valid = (torch.isfinite(depth).all() & (depth >= 0).all() & (depth <= 1).all()
                 & torch.isfinite(guide).all() & (guide >= 0).all() & (guide <= 1).all())
        if not bool(valid):
            raise ValueError("Depth and guide must be finite normalized values in [0, 1]")

    @staticmethod
    def _dilate(value, radius):
        return F.max_pool2d(value, 2 * radius + 1, stride=1, padding=radius)

    @classmethod
    def _local_range(cls, value):
        return cls._dilate(value, 1) + cls._dilate(-value, 1)

    @torch.no_grad()
    def apply(self, normalized_depth: torch.Tensor, rgb_guide: torch.Tensor, *,
              frame_id: int, generation: int, timestamp_ns: int,
              reset: bool = False) -> TemporalDepthResult:
        self._validate(normalized_depth, rgb_guide, frame_id, generation, timestamp_ns, reset)
        previous = self._history
        reason = "explicit_reset" if reset else None
        age = None
        if not reset:
            if previous is None:
                reason = self._pending_reset
            elif generation != previous.generation:
                reason = "generation"
            else:
                # Shape changes do not provide an escape from identity order.
                if frame_id <= previous.frame_id:
                    raise ValueError("Duplicate or reversed frame_id requires an explicit reset")
                if timestamp_ns <= previous.timestamp_ns:
                    raise ValueError("Duplicate or reversed capture timestamp requires an explicit reset")
                age = timestamp_ns - previous.timestamp_ns
                if normalized_depth.shape != previous.depth.shape or rgb_guide.shape != previous.guide.shape:
                    reason = "geometry"
                elif normalized_depth.device != previous.depth.device:
                    reason = "device"
                elif normalized_depth.dtype != previous.depth.dtype or rgb_guide.dtype != previous.guide.dtype:
                    reason = "dtype"
                elif age >= self.max_gap_ns:
                    reason = "gap"

        ceiling = 0.
        if reason is not None:
            output = normalized_depth.detach().clone()
            stats = torch.zeros(len(STAT_NAMES), device=normalized_depth.device, dtype=torch.float32)
        else:
            current_d = normalized_depth[:, None].float()
            previous_d = previous.depth[:, None].float()
            current_rgb, previous_rgb = rgb_guide.float(), previous.guide.float()
            rgb_delta = (current_rgb-previous_rgb).abs().amax(dim=1, keepdim=True)
            local_rgb_delta = self._dilate(rgb_delta, 2)
            local_depth_delta = self._dilate((current_d-previous_d).abs(), 2)
            rgb_rejected = local_rgb_delta >= self._RGB_CHANGE
            depth_rejected = local_depth_delta >= self._DEPTH_CHANGE
            edges = ((self._local_range(current_d) >= self._DEPTH_EDGE)
                     | (self._local_range(previous_d) >= self._DEPTH_EDGE)
                     | (self._local_range(current_rgb).amax(dim=1, keepdim=True) >= self._RGB_EDGE)
                     | (self._local_range(previous_rgb).amax(dim=1, keepdim=True) >= self._RGB_EDGE))
            edge_rejected = self._dilate(edges.float(), 1) > 0
            scene_cut = rgb_delta.mean() >= self._CUT_RGB_MEAN
            ceiling = self.max_history_weight * (1-age/self.max_gap_ns)
            weight = (ceiling * (1-local_rgb_delta/self._RGB_CHANGE).clamp(0, 1)
                      * (1-local_depth_delta/self._DEPTH_CHANGE).clamp(0, 1))
            weight = torch.where(rgb_rejected | depth_rejected | edge_rejected | scene_cut,
                                 0., weight)[:, 0]
            # Difference form preserves identical samples exactly. Rejected
            # samples bypass even a dtype roundtrip; depth is not spatially blurred.
            mixed = (normalized_depth + (previous.depth-normalized_depth)*weight).to(normalized_depth.dtype)
            output = torch.where(weight > 0, mixed, normalized_depth)
            stats = torch.stack((weight.mean(), (weight > 0).float().mean(),
                                 rgb_rejected.float().mean(), edge_rejected.float().mean(),
                                 depth_rejected.float().mean(), scene_cut.float()))

        # Publish state only after all fallible validation/calculation/copies
        # succeed. Retain unfiltered input in separate storage from caller/result.
        next_history = _History(normalized_depth.detach().clone(), rgb_guide.detach().clone(),
                                frame_id, generation, timestamp_ns)
        diagnostics = TemporalDepthDiagnostics(
            frame_id, generation, timestamp_ns,
            None if previous is None else previous.frame_id, age, reason, ceiling, stats)
        self._history = next_history
        self._pending_reset = "no_history"
        return TemporalDepthResult(output, diagnostics)

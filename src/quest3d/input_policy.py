"""Producer permission for an exact original desktop frame; host still validates it.

This module never injects input. The native authenticated endpoint also requires
its local opt-in, a live source, and a ticket for a frame actually shown by Quest.
"""
from dataclasses import dataclass

from .source_identity import SourceIdentity, SourceKind


MAX_INPUT_CAPTURE_AGE_NS = 500_000_000


def validate_input_options(args):
    if not getattr(args, "enable_input", False):
        return
    if getattr(args, "bridge_protocol", 2) != 3:
        raise ValueError("--enable-input requires --bridge-protocol 3")
    if (getattr(args, "file", None) or getattr(args, "file_av_clock", False)
            or getattr(args, "window", None) is not None
            or getattr(args, "monitor", 0) <= 0):
        raise ValueError("--enable-input requires one desktop monitor; files, windows and virtual desktops are unsupported")
    if getattr(args, "command", None) == "run" and not getattr(args, "publish", False):
        raise ValueError("--enable-input requires --publish for run")


@dataclass(frozen=True)
class InputDecision:
    enabled: bool
    reason: str


def frame_input_decision(*, opt_in, protocol, requested_mode, output, source,
                         current_frame, now_ns, error=None):
    """No permission for a stale frame, AI fallback, or guessed source identity."""
    if not opt_in:
        return InputDecision(False, "producer_opt_out")
    if protocol != 3:
        return InputDecision(False, "source_protocol_required")
    if error:
        return InputDecision(False, "producer_error")
    if requested_mode != "2d" or output.mode != "2d":
        return InputDecision(False, "explicit_original_2d_required")
    identity = getattr(source, "source_identity", None)
    if not isinstance(identity, SourceIdentity) or identity.kind != SourceKind.MONITOR:
        return InputDecision(False, "monitor_identity_required")
    if (identity != getattr(current_frame, "source_identity", None)
            or source.source_id != current_frame.source_id
            or source.frame_id != current_frame.frame_id
            or source.captured_ns != current_frame.captured_ns
            or source.geometry_generation != current_frame.geometry_generation
            or source.geometry != current_frame.geometry
            or output.frame_id != source.frame_id
            or output.generation != source.geometry_generation):
        return InputDecision(False, "source_frame_mismatch")
    if not 0 <= now_ns - source.captured_ns <= MAX_INPUT_CAPTURE_AGE_NS:
        return InputDecision(False, "capture_not_fresh")
    if source.geometry is None or output.content_rect is None:
        return InputDecision(False, "source_geometry_required")
    bounds = source.geometry.bounds
    try:
        identity.validate_region((bounds.left, bounds.top, bounds.width, bounds.height), input_enabled=True)
    except ValueError:
        return InputDecision(False, "source_geometry_mismatch")
    return InputDecision(True, "eligible_monitor_2d")

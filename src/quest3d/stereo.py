"""Small-disparity stereo synthesis from actual inverse depth, with flat UI masks."""

from dataclasses import dataclass

import cv2
import numpy as np
import torch
import torch.nn.functional as F

from .depth import DepthResult
from .disparity_mapping import map_disparity_depth, validate_disparity_profile


@dataclass(frozen=True)
class StereoFrame:
    bgra: np.ndarray
    frame_id: int
    generation: int
    mode: str
    scene_reset: bool
    depth_range: tuple[float, float] | None
    content_rect: tuple[int, int, int, int] = (0, 0, 0, 0)
    depth_refinement: str = "none"
    depth_refinement_reason: str | None = None
    colour_precision: str = "uint8"
    colour_precision_reason: str | None = None
    disparity_profile: str = "linear"
    disparity_profile_reason: str | None = None
    effective_convergence: float | None = None
    output_backend: str = "torch"
    validation_backend: str = "not-used"
    colour_fit_backend: str = "legacy"
    colour_fit_reason: str | None = None


class StereoSynthesizer:
    def __init__(self, eye_width: int = 1280, eye_height: int = 720,
                 disparity_px: float = 12.0, convergence: float = 0.5, *,
                 resize_filter: str = "area", stereo_method: str = "backward",
                 depth_refinement: str = "none", colour_precision: str = "uint8",
                 disparity_profile: str = "linear", fused_output: bool = False,
                 fused_validation: bool = False, fused_colour_fit: bool = False):
        if eye_width < 16 or eye_height < 16 or eye_width % 2 or eye_height % 2:
            raise ValueError("Eye dimensions must be even and at least 16 pixels")
        if not 0 <= disparity_px <= eye_width * 0.04:
            raise ValueError("Disparity must be between 0 and 4% of eye width")
        if not 0 <= convergence <= 1:
            raise ValueError("Convergence must be in [0, 1]")
        if not isinstance(resize_filter, str) or resize_filter not in ("area", "bicubic-aa"):
            raise ValueError("Resize filter must be area or bicubic-aa")
        if not isinstance(stereo_method, str) or stereo_method not in ("backward", "forward", "forward-cuda"):
            raise ValueError("Stereo method must be backward, forward or forward-cuda")
        if not isinstance(depth_refinement, str) or depth_refinement not in ("none", "guided", "edge-aware", "edge-cuda"):
            raise ValueError("Depth refinement must be none, guided, edge-aware or edge-cuda")
        if not isinstance(colour_precision, str) or colour_precision not in ("uint8", "float"):
            raise ValueError("Colour precision must be uint8 or float")
        self.disparity_profile = validate_disparity_profile(disparity_profile)
        if type(fused_output) is not bool or (fused_output and stereo_method != "forward-cuda"):
            raise ValueError("Fused output requires an explicit boolean and forward-cuda stereo")
        self.fused_output = fused_output
        if type(fused_validation) is not bool or (fused_validation and not fused_output):
            raise ValueError("Fused validation requires fused forward output")
        self.fused_validation = fused_validation
        if type(fused_colour_fit) is not bool or (fused_colour_fit and (
                colour_precision != "float" or resize_filter != "bicubic-aa")):
            raise ValueError("Fused colour fit requires float colour and bicubic-aa")
        self.fused_colour_fit = fused_colour_fit
        self.width, self.height = eye_width, eye_height
        self.disparity_px, self.convergence = disparity_px, convergence
        self.resize_filter = resize_filter
        self.stereo_method = stereo_method
        self.depth_refinement = depth_refinement
        self.colour_precision = colour_precision
        self.previous_thumbnail = None
        self.previous_generation = None
        self.depth_limits = None
        self.grid_cache = {}
        self._stereo_packer = None
        self._colour_fitter = None

    def reset_depth_history(self):
        """Model scales differ even on the same RGB; never blend their limits."""
        self.previous_thumbnail = None
        self.previous_generation = None
        self.depth_limits = None

    def close(self):
        failures = []
        if self._colour_fitter is not None:
            try:
                self._colour_fitter.close()
                self._colour_fitter = None
            except Exception as exc:
                failures.append(f"colour fit cleanup: {type(exc).__name__}: {exc}")
        if self.depth_refinement == "edge-cuda":
            try:
                from .depth_edges_cuda import close_cached_depth_edges_cuda
                close_cached_depth_edges_cuda()
            except Exception as exc:
                failures.append(f"depth edge cleanup: {type(exc).__name__}: {exc}")
        if self.stereo_method == "forward-cuda":
            try:
                # Each cache retains a faulted owner until synchronization succeeds.
                from .forward_warp_cuda import close_cached_forward_warp_cuda
                close_cached_forward_warp_cuda()
            except Exception as exc:
                failures.append(f"forward cleanup: {type(exc).__name__}: {exc}")
        if self._stereo_packer is not None:
            try:
                self._stereo_packer.close()
                self._stereo_packer = None
            except Exception as exc:
                failures.append(f"stereo pack cleanup: {type(exc).__name__}: {exc}")
        if failures:
            raise RuntimeError("; ".join(failures))

    def _fit(self, source: np.ndarray | torch.Tensor):
        height, width = source.shape[:2]
        scale = min(self.width / width, self.height / height)
        fitted_w, fitted_h = max(1, round(width * scale)), max(1, round(height * scale))
        x, y = (self.width - fitted_w) // 2, (self.height - fitted_h) // 2
        if self.resize_filter == "bicubic-aa":
            if (fitted_h, fitted_w) == (height, width):
                fitted = source  # Preserve exact source bytes when no resize is needed.
            else:
                tensor = source if isinstance(source, torch.Tensor) else torch.from_numpy(np.ascontiguousarray(source))
                fitted = F.interpolate(tensor.permute(2, 0, 1)[None].float(),
                                       size=(fitted_h, fitted_w), mode="bicubic",
                                       align_corners=False, antialias=True)[0]
                # Cubic interpolation can overshoot strong edges. Quantize once
                # and clamp before uint8 conversion so negative/high values do
                # not wrap; this does not claim to remove all ringing.
                fitted = fitted.round().clamp(0, 255).to(torch.uint8).permute(1, 2, 0)
                if not isinstance(source, torch.Tensor):
                    fitted = fitted.numpy()
        elif isinstance(source, torch.Tensor):
            fitted = F.interpolate(source.permute(2, 0, 1)[None].float(),
                                   size=(fitted_h, fitted_w), mode="area")[0]
            fitted = fitted.round().clamp(0, 255).to(torch.uint8).permute(1, 2, 0)
        else:
            fitted = cv2.resize(source, (fitted_w, fitted_h), interpolation=cv2.INTER_AREA)
        return fitted, (x, y, fitted_w, fitted_h)

    def original_2d(self, bgra: np.ndarray | torch.Tensor, *, frame_id: int, generation: int) -> StereoFrame:
        requested_profile = validate_disparity_profile(self.disparity_profile)
        fitted, (x, y, width, height) = self._fit(bgra)
        if isinstance(fitted, torch.Tensor):
            fitted = fitted.cpu().numpy()
        eye = np.zeros((self.height, self.width, 4), dtype=np.uint8)
        eye[:, :, 3] = 255
        eye[y:y + height, x:x + width] = fitted
        return StereoFrame(np.concatenate((eye, eye), axis=1), frame_id, generation, "2d", False, None,
                           (x, y, width, height), colour_precision="uint8",
                           colour_precision_reason="original_2d" if self.colour_precision == "float" else None,
                           disparity_profile_reason="original_2d" if requested_profile == "comfort" else None)

    def _select_colour_precision(self, bgra, depth_tensor):
        """Metadata-only eligibility; never transfer or reinterpret a source."""
        if self.colour_precision == "uint8":
            return "uint8", None
        if self.resize_filter != "bicubic-aa":
            return "uint8", "unsupported_resize_filter"
        if self.depth_refinement != "none":
            return "uint8", "depth_refinement_active"
        if not isinstance(bgra, torch.Tensor):
            return "uint8", "non_tensor_source"
        if bgra.device != depth_tensor.device:
            return "uint8", "source_device_mismatch"
        if bgra.dtype != torch.uint8:
            return "uint8", "unsupported_source_dtype"
        return "float", None

    @torch.inference_mode()
    def synthesize(self, bgra: np.ndarray | torch.Tensor, depth: DepthResult, *, frame_id: int,
                   generation: int, flat_mask: np.ndarray | None = None) -> StereoFrame:
        if depth.frame_id != frame_id or depth.generation != generation:
            raise ValueError("Refusing depth from a different RGB frame or geometry generation")
        requested_profile = validate_disparity_profile(self.disparity_profile)
        if self.depth_refinement != "none" and flat_mask is not None:
            raise ValueError("Guided depth refinement does not support flat_mask")
        if self.stereo_method != "backward" and flat_mask is not None:
            raise ValueError("Forward stereo does not support flat_mask")
        if self.disparity_px == 0:
            return self.original_2d(bgra, frame_id=frame_id, generation=generation)
        applied_precision, precision_reason = self._select_colour_precision(bgra, depth.tensor)
        float_fit = None
        colour_fit_backend, colour_fit_reason = "legacy", None
        if applied_precision == "float":
            from .colour_fit import fit_bgra_float
            if self.fused_colour_fit and bgra.is_cuda:
                from .colour_fit_cuda import CudaFloatColourFit
                if self._colour_fitter is None:
                    self._colour_fitter = CudaFloatColourFit(device=bgra.device.index)
                float_fit = self._colour_fitter(bgra, self.width, self.height)
                fit_status = self._colour_fitter.status()
                colour_fit_backend, colour_fit_reason = fit_status["effective"], fit_status["reason"]
            else:
                float_fit = fit_bgra_float(bgra, self.width, self.height)
                colour_fit_backend = "torch"
                colour_fit_reason = "non_cuda_source" if self.fused_colour_fit else None
            x, y, width, height = float_fit.content_rect
        else:
            fitted, (x, y, width, height) = self._fit(bgra)
        if isinstance(bgra, torch.Tensor):
            from .scene_thumbnail import tensor_scene_thumbnail
            # Exact midpoint sampling avoids casting the full desktop only for
            # supported CUDA geometries. Other inputs retain the old sampler.
            thumbnail = tensor_scene_thumbnail(bgra, sparse_midpoint=bgra.is_cuda)
        else:
            thumbnail = cv2.resize(bgra[:, :, :3], (32, 18)).astype(np.float32) / 255.0
        reset = self.previous_generation != generation or self.previous_thumbnail is None
        if not reset:
            # Heuristic, not a model confidence score. No old depth is reused.
            if isinstance(thumbnail, torch.Tensor) != isinstance(self.previous_thumbnail, torch.Tensor):
                reset = True
            elif isinstance(thumbnail, torch.Tensor):
                reset = float((thumbnail - self.previous_thumbnail).abs().mean().item()) > 0.22
            else:
                reset = float(np.abs(thumbnail - self.previous_thumbnail).mean()) > 0.22
        self.previous_thumbnail = thumbnail
        self.previous_generation = generation
        raw = depth.tensor
        if not bool(torch.isfinite(raw).all().item()):
            raise ValueError("Depth model produced non-finite values")
        low, high = (float(value.item()) for value in torch.aminmax(raw))
        if reset or self.depth_limits is None:
            self.depth_limits = (low, high)
        else:
            old_low, old_high = self.depth_limits
            self.depth_limits = (old_low * 0.9 + low * 0.1, old_high * 0.9 + high * 0.1)
        low, high = self.depth_limits
        normalized = ((raw - low) / max(high - low, 1e-6)).clamp(0, 1)
        if float_fit is not None:
            # Independently owned same-device BGR, with no middle uint8 round.
            # Original 2D, AI input and normalized depth retain their old path.
            image = float_fit.image
        else:
            if isinstance(fitted, torch.Tensor):
                image = fitted[:, :, :3].to(raw.device)
            else:
                image = torch.from_numpy(np.ascontiguousarray(fitted[:, :, :3])).to(raw.device)
            image = image.permute(2, 0, 1).unsqueeze(0).float() / 255.0
        applied_refinement = self.depth_refinement
        refinement_reason = None
        if self.depth_refinement in ("edge-aware", "edge-cuda") and (
                height < normalized.shape[-2] or width < normalized.shape[-1]):
            # The contour method is an upsampler only. An unusually narrow or
            # small fitted image keeps its existing interpolation and geometry.
            applied_refinement = "none"
            refinement_reason = "downsampled_content"
        if self.depth_refinement == "guided":
            # The synthesis image preserves capture's BGR ordering. Guidance
            # explicitly receives actual RGB covering this same fitted content.
            from .depth_refine import guided_upsample_depth
            inverse_depth = guided_upsample_depth(image.flip(1), normalized, radius=2, epsilon=0.01)
        elif applied_refinement in ("edge-aware", "edge-cuda"):
            # Both eyes use one same-frame correction. This changes only bounded
            # interpolation near supported RGB/depth boundaries, not global D.
            if self.depth_refinement == "edge-cuda":
                from .depth_edges_cuda import edge_aware_upsample_depth_cuda as refine
            else:
                from .depth_edges import edge_aware_upsample_depth as refine
            inverse_depth = refine(image.flip(1), normalized.float())
        else:
            inverse_depth = F.interpolate(normalized[:, None], size=(height, width),
                                          mode="bilinear", align_corners=True)[0, 0]
        inverse_depth, effective_convergence = map_disparity_depth(
            inverse_depth, self.convergence, requested_profile)
        cpu_frame = None
        output_backend = "torch"
        validation_backend = "not-used"
        if self.fused_output:
            from .forward_warp_cuda import synthesize_forward_packed_cuda
            validation_options = {"fused_validation": True} if self.fused_validation else {}
            if requested_profile == "comfort":
                # The shared mapping sends normalized [0, 1] to [.375, 1].
                # The projector validates that claim before using narrower search bounds.
                validation_options["projection_depth_range"] = (0.375, 1.0)
            result = synthesize_forward_packed_cuda(
                image, inverse_depth.float(), self.disparity_px, effective_convergence,
                self.width, self.height, (x, y, width, height), **validation_options)
            cpu_frame = result.cpu_bgra
            output_backend = "forward-fill-pack-cuda"
            validation_backend = "cuda-range" if self.fused_validation else "torch"
        elif self.stereo_method != "backward":
            # Explicit experiment: the public projector owns visibility and
            # bounded hole filling. RGB fitting, depth normalization and final
            # Full-SBS packing remain shared with the existing backward path.
            if self.stereo_method == "forward-cuda":
                from .forward_warp_cuda import synthesize_forward_cuda as project
            else:
                from .forward_warp import synthesize_forward as project
            project_options = {"fused_fill": True} if self.stereo_method == "forward-cuda" and image.is_cuda else {}
            if self.stereo_method == "forward-cuda" and requested_profile == "comfort":
                project_options["projection_depth_range"] = (0.375, 1.0)
            eyes = project(image, inverse_depth.float(), self.disparity_px, effective_convergence,
                           **project_options).eyes
            validation_backend = "torch"
        else:
            key = (width, height, str(raw.device))
            if key not in self.grid_cache:
                if len(self.grid_cache) >= 16:
                    self.grid_cache.clear()
                gy, gx = torch.meshgrid(torch.linspace(-1, 1, height, device=raw.device),
                                        torch.linspace(-1, 1, width, device=raw.device), indexing="ij")
                self.grid_cache[key] = torch.stack((gx, gy), dim=-1)[None]
            base = self.grid_cache[key]
            # Full L/R disparity in eye pixels; each eye gets half the horizontal shift.
            displacement = (inverse_depth - effective_convergence) * self.disparity_px / max(width - 1, 1)
            mask_tensor = None
            if flat_mask is not None:
                if flat_mask.shape != bgra.shape[:2]:
                    raise ValueError("Flat mask must match source dimensions")
                mask = cv2.resize(flat_mask.astype(np.uint8), (width, height), interpolation=cv2.INTER_NEAREST)
                mask_tensor = torch.from_numpy(mask > 0).to(raw.device)[None, None]
                displacement = displacement.masked_fill(mask_tensor[0, 0], 0)
            left_grid, right_grid = base.clone(), base.clone()
            left_grid[0, :, :, 0] -= displacement
            right_grid[0, :, :, 0] += displacement
            grids = torch.cat((left_grid, right_grid), dim=0)
            eyes = F.grid_sample(image.expand(2, -1, -1, -1), grids, mode="bilinear",
                                 padding_mode="border", align_corners=True)
            if mask_tensor is not None:
                # Keeping only destination UI pixels flat still lets neighboring
                # warped pixels sample that UI, duplicating text at its boundary.
                # Reject every bilinear sample that touches a protected source.
                protected_samples = F.grid_sample(
                    mask_tensor.float().expand(2, -1, -1, -1), grids,
                    mode="bilinear", padding_mode="border", align_corners=True
                ) > 0
                eyes = torch.where(protected_samples | mask_tensor,
                                   image.expand(2, -1, -1, -1), eyes)
        if cpu_frame is not None:
            pass  # Packed output has already completed its readback on the calling stream.
        elif self.stereo_method == "forward-cuda" and eyes.is_cuda:
            # This verified backend packs read-only float eyes directly into
            # owned BGRA. Other backends retain their original Torch path.
            from .stereo_pack_cuda import CudaStereoPacker
            if self._stereo_packer is None:
                self._stereo_packer = CudaStereoPacker(device=eyes.device.index)
            cpu_frame = self._stereo_packer(eyes, self.width, self.height, (x, y, width, height))
            output_backend = "separate-pack-cuda"
        else:
            eyes = (eyes.clamp(0, 1) * 255).round().to(torch.uint8).permute(0, 2, 3, 1)
            packed = torch.zeros((self.height, self.width * 2, 4), dtype=torch.uint8, device=raw.device)
            packed[:, :, 3] = 255
            packed[y:y + height, x:x + width, :3] = eyes[0]
            packed[y:y + height, self.width + x:self.width + x + width, :3] = eyes[1]
            if packed.is_cuda:
                cpu_frame = torch.empty(packed.shape, dtype=torch.uint8, device="cpu", pin_memory=True)
                cpu_frame.copy_(packed, non_blocking=True)
                torch.cuda.current_stream(raw.device).synchronize()
            else:
                cpu_frame = packed
        return StereoFrame(cpu_frame.numpy(), frame_id, generation, "3d", reset, self.depth_limits,
                           (x, y, width, height), applied_refinement, refinement_reason,
                           applied_precision, precision_reason, requested_profile, None, effective_convergence,
                           output_backend, validation_backend, colour_fit_backend, colour_fit_reason)

"""Pinned DPT Small inference; explicit model selection, no inference downloads."""

from dataclasses import dataclass
from pathlib import Path
import operator
import sys
import time

import cv2
import numpy as np
import torch

from .assets import DEFAULT_MODEL_ID, model_spec, verified_model, verified_model_source
from .paths import ROOT
from .resample import ReferenceCubicResize
from .depth_runtime import DepthModelRuntime


@dataclass
class DepthResult:
    frame_id: int
    generation: int
    tensor: torch.Tensor
    input_shape: tuple[int, int]
    preprocess_ms: float
    inference_ms: float
    execution_mode: str = "eager"
    execution_reason: str | None = None


def load_depth_weights(model, path=None, *, model_id: str = DEFAULT_MODEL_ID) -> dict:
    """Strict CPU checkpoint loading shared by runtime and offline validation."""
    spec = model_spec(model_id)
    checkpoint = verified_model(path, model_id=model_id)
    weight_format = spec.get("weight_format", "torch")
    if weight_format == "safetensors":
        try:
            from safetensors.torch import load_file
        except ImportError as exc:
            raise RuntimeError("DAD requires safetensors==0.6.2. Restore dependencies with uv sync --locked.") from exc
        weights = load_file(str(checkpoint), device="cpu")
    elif weight_format == "torch":
        weights = torch.load(checkpoint, map_location="cpu", weights_only=True)
    else:
        raise ValueError(f"Unsupported checkpoint format: {weight_format}")
    loaded = model.load_state_dict(weights, strict=True)
    runtime_spec = model_spec(spec.get("runtime_source_model_id", model_id))
    return {"model_id": model_id, "name": spec.get("display_name", "Depth Anything V2 Small"),
            "checkpoint": str(checkpoint), "sha256": spec["sha256"], "weight_format": weight_format,
            "repository": spec["repository"], "revision": spec["revision"],
            "source_repository": spec["source_repository"], "source_commit": spec["source_commit"],
            "runtime_source_commit": runtime_spec["source_commit"],
            "license": spec.get("license"),
            "depth_kind": spec.get("depth_kind", "inverse_depth"),
            "strict_load": {"missing": list(loaded.missing_keys), "unexpected": list(loaded.unexpected_keys)}}


class DepthEngine:
    def __init__(self, input_size: int = 280, fp16: bool = True, *,
                 execution_mode: str = "eager", reuse_constants: bool = False,
                 fused_resize: bool = False, model_id: str = DEFAULT_MODEL_ID):
        if input_size < 140 or input_size > 1036 or input_size % 14:
            raise ValueError("AI input short edge must be a multiple of 14 in [140, 1036]")
        if execution_mode not in ("eager", "cuda-graph"):
            raise ValueError("Depth execution mode must be eager or cuda-graph")
        if type(fused_resize) is not bool:
            raise ValueError("Fused resize must be an explicit boolean")
        if type(reuse_constants) is not bool or (reuse_constants and (execution_mode != "cuda-graph" or not fp16)):
            raise ValueError("Constant reuse requires explicit FP16 CUDA graph inference")
        if reuse_constants and torch.is_inference_mode_enabled():
            raise ValueError("Create the frozen depth engine outside inference_mode")
        model_spec(model_id)  # Reject unknown selections before any CUDA activity.
        if not torch.cuda.is_available():
            raise RuntimeError("CUDA is required for the validated PC path; run quest3d doctor")
        source = verified_model_source(model_id=model_id)
        sys.path.insert(0, str(source))
        from depth_anything_v2.dpt import DepthAnythingV2
        from depth_anything_v2.util.transform import Resize

        loaded_source = Path(sys.modules[DepthAnythingV2.__module__].__file__).resolve()
        if loaded_source != (source / "depth_anything_v2/dpt.py").resolve():
            raise RuntimeError("A different depth model checkout is already imported. Restart the PC app to load the verified source.")

        self.model = DepthAnythingV2(encoder="vits", features=64, out_channels=[48, 96, 192, 384])
        self.model_metadata = load_depth_weights(self.model, model_id=model_id)
        self.model_metadata["parameter_count"] = sum(parameter.numel() for parameter in self.model.parameters())
        self.model_id = model_id
        self.model.eval().to("cuda")
        self.input_size = input_size
        self.fp16 = fp16
        self.resize = Resize(input_size, input_size, resize_target=False,
                             keep_aspect_ratio=True, ensure_multiple_of=14,
                             resize_method="lower_bound", image_interpolation_method=cv2.INTER_CUBIC)
        self.gpu_resize = ReferenceCubicResize()
        self.mean = torch.tensor([0.485, 0.456, 0.406], device="cuda")[:, None, None]
        self.std = torch.tensor([0.229, 0.224, 0.225], device="cuda")[:, None, None]
        self.start = torch.cuda.Event(enable_timing=True)
        self.end = torch.cuda.Event(enable_timing=True)
        self.constants = None
        if reuse_constants:
            from .depth_constants import FrozenDepthConstants
            self.constants = FrozenDepthConstants(self.model)
        self.runtime = DepthModelRuntime(self._forward_model, mode=execution_mode)
        if fused_resize:
            from .resample_cuda import CudaReferenceCubicResize
            try:
                self.gpu_resize = CudaReferenceCubicResize(device=self.mean.device.index)
            except BaseException:
                self.close()
                raise

    def _forward_model(self, image: torch.Tensor) -> torch.Tensor:
        # Exactly the original eager forward/autocast policy, also used during
        # graph warmup/capture. No model, weight, precision or AI size changes.
        with torch.autocast("cuda", dtype=torch.float16, enabled=self.fp16):
            return self.model(image).float()

    @torch.inference_mode()
    def prepare_execution(self, source_width: int, source_height: int):
        """Opt-in graph preparation before starting any CUDA capture thread.

        A synthetic BGRA input uses the real preprocessing layout. Guessing an
        NCHW layout would miss the GPU preprocessing's channels-last strides.
        A new input shape later falls back eagerly; it never triggers capture.
        """
        if not (0 < source_width <= 16384 and 0 < source_height <= 16384):
            raise ValueError("Graph source dimensions must be in [1, 16384]")
        if self.runtime.mode == "eager":
            return self.runtime.status.to_dict()
        if getattr(self, "constants", None) is not None:
            self.constants.validate()
        source = torch.zeros((source_height, source_width, 4), dtype=torch.uint8,
                             device=self.mean.device)
        image = self.prepare(source)
        return self.runtime.prepare(image).to_dict()

    def execution_status(self):
        status = self.runtime.status.to_dict()
        if hasattr(self, "model_metadata"):
            status["model"] = {**self.model_metadata, "strict_load": dict(self.model_metadata["strict_load"])}
        if getattr(self, "constants", None) is not None:
            status["constants"] = self.constants.status()
        metadata = getattr(getattr(self, "gpu_resize", None), "metadata", None)
        if metadata is not None:
            status["gpu_resize"] = {"implementation": "fused-reference-cubic", **metadata}
        return status

    def close(self):
        self.runtime.close()
        if getattr(self, "constants", None) is not None:
            self.constants.close()
        resize = getattr(self, "gpu_resize", None)
        if resize is not None and hasattr(resize, "close"):
            resize.close()

    def prepare(self, bgra: np.ndarray | torch.Tensor) -> torch.Tensor:
        if isinstance(bgra, torch.Tensor):
            if not bgra.is_cuda or bgra.dtype != torch.uint8 or bgra.ndim != 3 or bgra.shape[2] != 4:
                raise ValueError("GPU input must be a CUDA HWC uint8 BGRA tensor")
            # The pinned model Resize returns NumPy integer scalars. Normalize
            # that integer protocol at the native boundary, without truncating
            # a float or changing the model's chosen dimensions.
            width, height = map(operator.index, self.resize.get_size(bgra.shape[1], bgra.shape[0]))
            small = self.gpu_resize(bgra[:, :, :3], width, height)
            rgb = small.flip(-1).permute(2, 0, 1) / 255.0
            return ((rgb - self.mean) / self.std).unsqueeze(0)
        # Preserve the reference's float64 cubic interpolation. Normalization
        # runs on the smaller frame; float32 interpolation failed the numerical
        # parity check at sharp edges, so it is deliberately not used here.
        bgr = np.ascontiguousarray(bgra[:, :, :3])
        width, height = self.resize.get_size(bgr.shape[1], bgr.shape[0])
        resized = cv2.resize(bgr.astype(np.float64), (width, height), interpolation=cv2.INTER_CUBIC)
        rgb = resized[:, :, ::-1] / 255.0
        rgb = (rgb - [0.485, 0.456, 0.406]) / [0.229, 0.224, 0.225]
        return torch.from_numpy(np.ascontiguousarray(rgb.transpose(2, 0, 1), dtype=np.float32)).unsqueeze(0).to("cuda")

    @torch.inference_mode()
    def infer(self, bgra: np.ndarray | torch.Tensor, *, frame_id: int, generation: int) -> DepthResult:
        started = time.perf_counter_ns()
        if getattr(self, "constants", None) is not None:
            self.constants.validate()
        image = self.prepare(bgra)
        # CapturedGPUFrame publication already completes the source copy and
        # tone map. Wait for this worker's preprocessing only, not unrelated
        # capture/NVENC work on other CUDA streams.
        torch.cuda.current_stream(image.device).synchronize()
        preprocess_ms = (time.perf_counter_ns() - started) / 1e6
        self.start.record()
        depth = self.runtime(image)
        self.end.record()
        self.end.synchronize()
        return DepthResult(frame_id, generation, depth, tuple(image.shape[-2:]),
                           preprocess_ms, self.start.elapsed_time(self.end),
                           self.runtime.status.effective, self.runtime.status.reason)

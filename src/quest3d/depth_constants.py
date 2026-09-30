"""Opt-in reuse of constants in the frozen Depth Anything V2 inference model.

No model files or mathematical operations change. The positional tensor is the
original interpolation result; Linear/Conv2d operands use the existing FP16
autocast conversion once. Norms, position parameters and other modules stay FP32.
The owner must close its CUDA graph before closing these constants.
"""
from __future__ import annotations

from types import MethodType

import torch


def _identity(tensor):
    return (id(tensor), tensor._version, tensor.data_ptr(), tuple(tensor.shape),
            tuple(tensor.stride()), tensor.dtype, tensor.device)


class FrozenDepthConstants:
    """One positional layout and immutable model operands, owned until close.

    Calls are restricted to eval/no-grad inference. A different positional
    layout uses the original calculation without evicting the captured layout.
    validate() must run outside graph replay on every inference request.
    """

    def __init__(self, model):
        if torch.is_inference_mode_enabled() or any(parameter.is_inference() for parameter in model.parameters()):
            raise ValueError("Create the frozen depth model and constants outside inference_mode")
        if model.training or any(module.training for module in model.modules()):
            raise ValueError("Constant reuse requires an eval model")
        self.model = model
        self.backbone = model.pretrained
        for name in ("pos_embed", "patch_size", "interpolate_offset", "interpolate_antialias"):
            if not hasattr(self.backbone, name):
                raise ValueError("Unexpected positional encoding implementation")
        self._original_interpolate = self.backbone.interpolate_pos_encoding
        self._had_method = "interpolate_pos_encoding" in self.backbone.__dict__
        self._prior_method = self.backbone.__dict__.get("interpolate_pos_encoding")
        self._original_parameters = []
        self._streams = {}
        self._cache_key = self._cache_value = self._cache_event = None
        self._producer_stream = None
        self._closed = False
        self.cache_hits = self.cache_misses = self.uncached_layouts = 0
        self.converted_modules = 0
        try:
            with torch.no_grad():
                for module in model.modules():
                    if type(module) not in (torch.nn.Linear, torch.nn.Conv2d):
                        continue
                    for name in ("weight", "bias"):
                        original = getattr(module, name)
                        if original is None:
                            continue
                        if original.dtype != torch.float32:
                            raise ValueError("Expected the original FP32 model operands")
                        converted = torch.nn.Parameter(original.detach().to(torch.float16), requires_grad=False)
                        self._original_parameters.append((module, name, original, _identity(original), converted))
                        setattr(module, name, converted)
                    self.converted_modules += 1
            self._parameters = tuple((name, _identity(value)) for name, value in model.named_parameters())
            self._modules = tuple(model.modules())
            self._position_options = self._options()

            def cached(backbone, x, w, h):
                return self._interpolate(x, w, h)
            self._cached_method = MethodType(cached, self.backbone)
            self.backbone.interpolate_pos_encoding = self._cached_method
        except BaseException:
            self.close()
            raise

    def _options(self):
        return (self.backbone.patch_size, self.backbone.interpolate_offset,
                self.backbone.interpolate_antialias)

    def _note_stream(self, device):
        if device.type != "cuda":
            return None
        stream = torch.cuda.current_stream(device)
        self._streams[(device.index, stream.cuda_stream)] = stream
        return stream

    def validate(self):
        """Refuse stale graph constants after any supported model-state change."""
        if self._closed:
            raise RuntimeError("Depth constants are closed")
        if torch.is_grad_enabled() or any(module.training for module in self._modules):
            raise RuntimeError("Depth constants require eval/no-grad inference")
        if tuple((name, _identity(value)) for name, value in self.model.named_parameters()) != self._parameters:
            raise RuntimeError("Frozen depth model parameters changed")
        if any(_identity(original) != identity for _, _, original, identity, _ in self._original_parameters):
            raise RuntimeError("Original depth model operands changed")
        if self._options() != self._position_options:
            raise RuntimeError("Frozen positional encoding options changed")
        if self.backbone.interpolate_pos_encoding is not self._cached_method:
            raise RuntimeError("Frozen positional encoding method changed")
        self._note_stream(self.backbone.pos_embed.device)

    def _interpolate(self, x, w, h):
        if self._closed or torch.is_grad_enabled() or self.backbone.training:
            raise RuntimeError("Position cache requires active eval/no-grad inference")
        key = (tuple(x.shape[1:]), x.dtype, x.device, int(w), int(h),
               _identity(self.backbone.pos_embed), self._options())
        stream = self._note_stream(x.device)
        if self._cache_key == key:
            if stream is not None and stream != self._producer_stream:
                stream.wait_event(self._cache_event)
            self.cache_hits += 1
            return self._cache_value
        result = self._original_interpolate(x, w, h)
        if self._cache_key is None:
            self._cache_key, self._cache_value = key, result
            self._producer_stream = stream
            if stream is not None:
                self._cache_event = torch.cuda.Event()
                self._cache_event.record(stream)
            self.cache_misses += 1
        else:
            # The existing CUDA graph can still reference the first allocation.
            # Never replace it just because a one-off input has another shape.
            self.uncached_layouts += 1
        return result

    def status(self):
        return {"enabled": not self._closed, "converted_modules": self.converted_modules,
                "position_cache_ready": self._cache_value is not None,
                "position_cache_shape": list(self._cache_value.shape) if self._cache_value is not None else None,
                "position_cache_hits": self.cache_hits, "position_cache_misses": self.cache_misses,
                "position_uncached_layouts": self.uncached_layouts}

    def close(self):
        if self._closed:
            return
        # Consumers include graph replay streams registered by validate().
        # Keep storage alive until every known consumer finishes.
        for stream in self._streams.values():
            stream.synchronize()
        for module, name, original, _, converted in reversed(self._original_parameters):
            if getattr(module, name) is converted:
                setattr(module, name, original)
        if getattr(self, "_cached_method", None) is getattr(self.backbone, "interpolate_pos_encoding", None):
            if self._had_method:
                self.backbone.interpolate_pos_encoding = self._prior_method
            else:
                del self.backbone.interpolate_pos_encoding
        self._cache_key = self._cache_value = self._cache_event = None
        self._producer_stream = None
        self._streams.clear()
        self._original_parameters.clear()
        self._parameters = self._modules = ()
        self._original_interpolate = self._prior_method = self._cached_method = None
        self.model = self.backbone = None
        self._closed = True

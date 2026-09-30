"""Bounded Torch-bicubic-AA colour fit with exact Torch coefficient extraction.

This is unrelated to AI preprocessing's OpenCV cubic coefficients. A small
periodic basis is resized by the pinned Torch CUDA AA operator once per axis;
its nonoverlapping impulses expose the original float32 weights without copying
captured pixels to CPU. A uint8->horizontal->vertical kernel pair reuses them.

No CUDA work at import. Calls serialize with close and return fresh owned planar
float32 storage after completing the current stream. Invalid inputs and CUDA
failures are errors; only explicitly bounded-out geometry uses the unchanged
Torch implementation and records its reason. Numerical acceptance requires the
opt-in real GPU tests; mathematical equivalence alone is not sufficient.
"""
from __future__ import annotations

from collections import OrderedDict
import ctypes as C
import hashlib
import math
from pathlib import Path
import sys
import threading

import numpy as np
import torch
import torch.nn.functional as F

from .colour_fit import FloatColourFit, fit_bgra_float


class CudaFloatColourFit:
    MAX_AXIS = 16384
    MAX_TAPS = 65
    MAX_TEMPORARY_BYTES = 128 * 1024 * 1024
    MAX_CACHE_AXES = 8

    def __init__(self, *, device=0):
        self.module = C.c_void_p()
        self.closed = self.faulted = False
        self._lock = threading.RLock()
        self._inflight = None
        self.device = device
        self._cache = OrderedDict()
        self.cache_hits = self.cache_misses = 0
        self.last_execution = {"effective": "not_run", "reason": None}
        if sys.platform != "win32" or torch.__version__ != "2.7.1+cu126":
            raise RuntimeError("Colour fit requires pinned Windows Torch 2.7.1+cu126")
        if type(device) is not int or not 0 <= device < torch.cuda.device_count():
            raise ValueError("Invalid CUDA device")
        if torch.cuda.get_device_capability(device) != (7, 5):
            raise RuntimeError("Colour fit currently targets verified sm75 only")
        nvrtc = C.WinDLL(str(Path(torch.__file__).resolve().parent / "lib/nvrtc64_120_0.dll"))
        self.driver = C.WinDLL("C:/Windows/System32/nvcuda.dll")
        for name, arguments in {
            "nvrtcVersion": [C.POINTER(C.c_int), C.POINTER(C.c_int)],
            "nvrtcCreateProgram": [C.POINTER(C.c_void_p), C.c_char_p, C.c_char_p, C.c_int, C.c_void_p, C.c_void_p],
            "nvrtcCompileProgram": [C.c_void_p, C.c_int, C.POINTER(C.c_char_p)],
            "nvrtcGetProgramLogSize": [C.c_void_p, C.POINTER(C.c_size_t)],
            "nvrtcGetProgramLog": [C.c_void_p, C.c_void_p],
            "nvrtcGetPTXSize": [C.c_void_p, C.POINTER(C.c_size_t)],
            "nvrtcGetPTX": [C.c_void_p, C.c_void_p],
            "nvrtcDestroyProgram": [C.POINTER(C.c_void_p)],
        }.items():
            getattr(nvrtc, name).argtypes = arguments
            getattr(nvrtc, name).restype = C.c_int
        for name, arguments in {
            "cuModuleLoadData": [C.POINTER(C.c_void_p), C.c_void_p],
            "cuModuleGetFunction": [C.POINTER(C.c_void_p), C.c_void_p, C.c_char_p],
            "cuLaunchKernel": [C.c_void_p] + [C.c_uint] * 7 + [C.c_void_p, C.POINTER(C.c_void_p), C.c_void_p],
            "cuModuleUnload": [C.c_void_p],
        }.items():
            getattr(self.driver, name).argtypes = arguments
            getattr(self.driver, name).restype = C.c_int
        major, minor = C.c_int(), C.c_int()
        self._check(nvrtc.nvrtcVersion(C.byref(major), C.byref(minor)), "nvrtcVersion")
        if (major.value, minor.value) != (12, 6):
            raise RuntimeError("Expected pinned CUDA 12.6 NVRTC")
        source = (Path(__file__).parent / "shaders/colour_fit.cu").read_bytes()
        options = (C.c_char_p * 3)(b"--gpu-architecture=compute_75", b"--fmad=false", b"--std=c++11")
        program = C.c_void_p()
        self._check(nvrtc.nvrtcCreateProgram(C.byref(program), source, b"colour_fit.cu", 0, None, None), "nvrtcCreateProgram")
        try:
            code = nvrtc.nvrtcCompileProgram(program, len(options), options)
            if code:
                size = C.c_size_t()
                self._check(nvrtc.nvrtcGetProgramLogSize(program, C.byref(size)), "nvrtcGetProgramLogSize")
                if not 0 < size.value < 1_048_576:
                    raise RuntimeError("Unexpected NVRTC log size")
                log = C.create_string_buffer(size.value)
                self._check(nvrtc.nvrtcGetProgramLog(program, log), "nvrtcGetProgramLog")
                raise RuntimeError(f"NVRTC compile failed ({code}): {log.value.decode('utf-8', 'replace')}")
            size = C.c_size_t()
            self._check(nvrtc.nvrtcGetPTXSize(program, C.byref(size)), "nvrtcGetPTXSize")
            if not 0 < size.value < 16_777_216:
                raise RuntimeError("Unexpected PTX size")
            ptx = C.create_string_buffer(size.value)
            self._check(nvrtc.nvrtcGetPTX(program, ptx), "nvrtcGetPTX")
        finally:
            self._check(nvrtc.nvrtcDestroyProgram(C.byref(program)), "nvrtcDestroyProgram")
        self.metadata = {"nvrtc": [major.value, minor.value], "architecture": "compute_75",
            "source_sha256": hashlib.sha256(source).hexdigest(), "ptx_sha256": hashlib.sha256(ptx.raw).hexdigest(),
            "compiler_fmad": False, "accumulation": "explicit float32 FMA after first RN product, horizontal then vertical",
            "coefficients": "pinned Torch CUDA bicubic antialias periodic-basis oracle",
            "normalization": "clamp to 0..255 then float32 reciprocal(255) multiply",
            "completion": "fresh owned CUDA planar float32, current stream synchronized",
            "max_cached_axes": self.MAX_CACHE_AXES, "max_taps": self.MAX_TAPS,
            "max_temporary_bytes": self.MAX_TEMPORARY_BYTES}
        try:
            with torch.cuda.device(device):
                torch.empty(1, device=device)
                self._check(self.driver.cuModuleLoadData(C.byref(self.module), ptx), "cuModuleLoadData")
                self.functions = {}
                for name in ("same_size", "horizontal", "vertical"):
                    function = C.c_void_p()
                    self._check(self.driver.cuModuleGetFunction(C.byref(function), self.module,
                        f"quest3d_colour_{name}".encode()), "cuModuleGetFunction")
                    self.functions[name] = function
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _check(code, operation):
        if code:
            raise RuntimeError(f"{operation} failed with CUDA/NVRTC code {code}")

    @classmethod
    def _axis_taps(cls, source, target):
        if source == target:
            return 1
        scale = np.float32(source) / np.float32(target)
        support = np.float32(2) * max(scale, np.float32(1))
        # The oracle support span is <=2*ceil(support)+1. Two additional zero
        # taps allow conservative integer bounds without reproducing its span
        # arithmetic. Every actual contributing source index is still unique
        # modulo this padded period.
        return 2 * math.ceil(float(support)) + 3

    @classmethod
    def support_status(cls, source_width, source_height, eye_width, eye_height):
        if any(type(value) is not int or value < 1 for value in (source_width, source_height, eye_width, eye_height)):
            raise ValueError("Source and eye dimensions must be positive integers")
        scale = min(eye_width / source_width, eye_height / source_height)
        width, height = max(1, round(source_width * scale)), max(1, round(source_height * scale))
        rectangle = ((eye_width - width) // 2, (eye_height - height) // 2, width, height)
        tx, ty = cls._axis_taps(source_width, width), cls._axis_taps(source_height, height)
        same_size = (source_width, source_height) == (width, height)
        temporary = 0 if same_size else 3 * source_height * width * 4
        reason = None
        if max(source_width, source_height, width, height) > cls.MAX_AXIS:
            reason = "dimension_limit"
        elif max(tx, ty) > cls.MAX_TAPS:
            reason = "axis_tap_limit"
        elif temporary > cls.MAX_TEMPORARY_BYTES:
            reason = "intermediate_limit"
        return {"supported": reason is None, "reason": reason, "content_rect": rectangle,
                "taps_x": tx, "taps_y": ty, "temporary_bytes": temporary}

    @staticmethod
    def _validate_layout(bgra, eye_width, eye_height):
        if (not isinstance(bgra, torch.Tensor) or bgra.dtype != torch.uint8 or bgra.ndim != 3
                or bgra.shape[2] != 4 or min(bgra.shape[:2]) < 1 or any(s < 0 for s in bgra.stride())):
            raise ValueError("Expected nonempty HWC uint8 BGRA with nonnegative strides")
        if bgra.is_neg():
            raise ValueError("Lazy-negative BGRA views are unsupported; materialize the logical values first")
        if any(type(value) is not int or value < 1 for value in (eye_width, eye_height)):
            raise ValueError("Eye dimensions must be positive integers")

    def status(self):
        return {**self.last_execution, "closed": self.closed, "faulted": self.faulted,
                "coefficient_cache_entries": len(self._cache), "coefficient_cache_hits": self.cache_hits,
                "coefficient_cache_misses": self.cache_misses, "metadata": self.metadata}

    def _coefficients(self, source, target, device):
        key = (source, target)
        if key in self._cache:
            self.cache_hits += 1
            self._cache.move_to_end(key)
            return self._cache[key]
        taps = self._axis_taps(source, target)
        if source == target:
            indices = torch.arange(source, dtype=torch.int32, device=device)[:, None]
            weights = torch.ones((target, 1), dtype=torch.float32, device=device)
        else:
            scale = np.float32(source) / np.float32(target)
            support = np.float32(2) * max(scale, np.float32(1))
            centers = (np.arange(target, dtype=np.float32) + np.float32(.5)) * scale
            starts = np.maximum(np.floor(centers.astype(np.float64) - float(support)).astype(np.int64) - 1, 0)
            raw_indices = starts[:, None] + np.arange(taps, dtype=np.int64)[None]
            valid = torch.from_numpy(raw_indices < source).to(device)
            indices = torch.from_numpy(np.clip(raw_indices, 0, source - 1).astype(np.int32)).to(device)
            # Each source location is a unit impulse in one periodic basis
            # channel. The period is wider than the original filter span, so
            # each contributing tap has a distinct channel and is recovered
            # exactly, including its Torch boundary renormalization.
            basis = torch.zeros((1, taps, 1, source), dtype=torch.float32, device=device)
            locations = torch.arange(source, device=device)
            basis[0, locations.remainder(taps), 0, locations] = 1
            response = F.interpolate(basis, size=(1, target), mode="bicubic", align_corners=False,
                                     antialias=True)[0, :, 0, :].transpose(0, 1)
            weights = response.gather(1, indices.to(torch.int64).remainder(taps))
            weights = torch.where(valid, weights, 0).contiguous()
        if len(self._cache) >= self.MAX_CACHE_AXES:
            # Every previous public call completed its stream. The current
            # call also holds returned coefficients locally across eviction.
            self._cache.popitem(last=False)
        self._cache[key] = (indices, weights)
        self.cache_misses += 1
        return indices, weights

    def _complete(self, stream):
        try:
            stream.synchronize()
        except BaseException:
            self.faulted = True
            raise
        self._inflight = None

    def _launch(self, name, count, values, stream):
        arguments = (C.c_void_p * len(values))(*(C.addressof(value) for value in values))
        self._check(self.driver.cuLaunchKernel(self.functions[name], (count + 255) // 256, 1, 1,
            256, 1, 1, 0, C.c_void_p(stream.cuda_stream), arguments, None), f"quest3d_colour_{name}")

    def _execute(self, bgra, output, temporary, coefficients, stream):
        pointer = lambda tensor: C.c_uint64(tensor.data_ptr())
        width, height = output.shape[-1], output.shape[-2]
        self._inflight = (stream, bgra, output, temporary, *coefficients)
        try:
            if temporary is None:
                self._launch("same_size", output.numel(), [pointer(bgra), pointer(output), C.c_int(width), C.c_int(height),
                    *(C.c_int64(s) for s in bgra.stride())], stream)
            else:
                xi, xw, yi, yw = coefficients
                self._launch("horizontal", temporary.numel(), [pointer(bgra), pointer(temporary),
                    C.c_int(bgra.shape[0]), C.c_int(width), *(C.c_int64(s) for s in bgra.stride()),
                    pointer(xi), pointer(xw), C.c_int(xi.shape[1])], stream)
                self._launch("vertical", output.numel(), [pointer(temporary), pointer(output),
                    C.c_int(bgra.shape[0]), C.c_int(width), C.c_int(height),
                    pointer(yi), pointer(yw), C.c_int(yi.shape[1])], stream)
        except BaseException:
            self.faulted = True
            raise
        finally:
            self._complete(stream)

    @torch.inference_mode()
    def __call__(self, bgra, eye_width, eye_height):
        with self._lock:
            if self.closed or self.faulted:
                raise RuntimeError("CUDA colour fit is closed or faulted")
            self._validate_layout(bgra, eye_width, eye_height)
            if not bgra.is_cuda or bgra.device.index != self.device:
                raise ValueError("Expected BGRA on this owner's CUDA device")
            plan = self.support_status(bgra.shape[1], bgra.shape[0], eye_width, eye_height)
            if not plan["supported"]:
                self.last_execution = {**plan, "effective": "torch"}
                stream = torch.cuda.current_stream(self.device)
                self._inflight = (stream, bgra)
                try:
                    result = fit_bgra_float(bgra, eye_width, eye_height)
                    self._inflight = (stream, bgra, result.image)
                except BaseException:
                    self.faulted = True
                    raise
                finally:
                    self._complete(stream)
                return result
            width, height = plan["content_rect"][2:]
            same_size = (height, width) == bgra.shape[:2]
            with torch.cuda.device(self.device), torch.autocast("cuda", enabled=False):
                output = torch.empty((1, 3, height, width), dtype=torch.float32, device=bgra.device)
                temporary = None if same_size else torch.empty((1, 3, bgra.shape[0], width),
                                                               dtype=torch.float32, device=bgra.device)
                coefficients = ()
                try:
                    if not same_size:
                        coefficients = (*self._coefficients(bgra.shape[1], width, bgra.device),
                                        *self._coefficients(bgra.shape[0], height, bgra.device))
                    self._execute(bgra, output, temporary, coefficients, torch.cuda.current_stream(self.device))
                except BaseException:
                    self.faulted = True
                    raise
            self.last_execution = {**plan, "effective": "cuda-same-size" if same_size else "cuda-2pass"}
            return FloatColourFit(output, plan["content_rect"])

    def close(self):
        with self._lock:
            if self.module.value:
                with torch.cuda.device(self.device):
                    try:
                        torch.cuda.synchronize(self.device)
                        self._inflight = None
                        self._check(self.driver.cuModuleUnload(self.module), "cuModuleUnload")
                    except BaseException:
                        self.faulted = True
                        raise
                self.module = C.c_void_p()
            self._cache.clear()
            self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

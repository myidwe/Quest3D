"""Opt-in fused OpenCV-coefficient cubic resize for the pinned Windows GPU.

No CUDA work occurs at import. Public calls serialize with close, return fresh
owned HWC float32 storage, and complete their current stream before returning.
Only this owner uses its bounded reference coefficient cache. No source pixels
are copied to CPU, and full-resolution float/gather intermediates are avoided.
GPU numerical/performance acceptance is separate from the CPU lifetime tests.
"""
from __future__ import annotations

import ctypes as C
import hashlib
from pathlib import Path
import sys
import threading

import torch

from .resample import ReferenceCubicResize


class CudaReferenceCubicResize:
    def __init__(self, *, device=0):
        self.module = C.c_void_p()
        self.closed = self.faulted = False
        self._inflight = None
        self._lock = threading.RLock()
        self.device = device
        self._reference = ReferenceCubicResize()
        if sys.platform != "win32" or torch.__version__ != "2.7.1+cu126":
            raise RuntimeError("Cubic CUDA resize requires pinned Windows Torch 2.7.1+cu126")
        if type(device) is not int or not 0 <= device < torch.cuda.device_count():
            raise ValueError("Invalid CUDA device")
        if torch.cuda.get_device_capability(device) != (7, 5):
            raise RuntimeError("Cubic CUDA resize currently targets verified sm75 only")
        nvrtc = C.WinDLL(str(Path(torch.__file__).resolve().parent / "lib/nvrtc64_120_0.dll"))
        self.driver = C.WinDLL("C:/Windows/System32/nvcuda.dll")
        for name, arguments in {
            "nvrtcVersion": [C.POINTER(C.c_int), C.POINTER(C.c_int)],
            "nvrtcCreateProgram": [C.POINTER(C.c_void_p), C.c_char_p, C.c_char_p,
                                   C.c_int, C.c_void_p, C.c_void_p],
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
            "cuLaunchKernel": [C.c_void_p] + [C.c_uint] * 7
                + [C.c_void_p, C.POINTER(C.c_void_p), C.c_void_p],
            "cuModuleUnload": [C.c_void_p],
        }.items():
            getattr(self.driver, name).argtypes = arguments
            getattr(self.driver, name).restype = C.c_int
        major, minor = C.c_int(), C.c_int()
        self._check(nvrtc.nvrtcVersion(C.byref(major), C.byref(minor)), "nvrtcVersion")
        if (major.value, minor.value) != (12, 6):
            raise RuntimeError("Expected pinned CUDA 12.6 NVRTC")
        source = (Path(__file__).parent / "shaders/reference_cubic.cu").read_bytes()
        options = (C.c_char_p * 3)(b"--gpu-architecture=compute_75", b"--fmad=false", b"--std=c++11")
        program = C.c_void_p()
        self._check(nvrtc.nvrtcCreateProgram(C.byref(program), source, b"reference_cubic.cu",
                                            0, None, None), "nvrtcCreateProgram")
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
            "source_sha256": hashlib.sha256(source).hexdigest(),
            "ptx_sha256": hashlib.sha256(ptx.raw).hexdigest(), "fmad": False,
            "completion": "owned contiguous CUDA float32 HWC, synchronized current stream",
            "coefficients": "unchanged ReferenceCubicResize._coefficients; maximum 16 cached axes"}
        try:
            with torch.cuda.device(device):
                torch.empty(1, device=device)
                self._check(self.driver.cuModuleLoadData(C.byref(self.module), ptx), "cuModuleLoadData")
                self.function = C.c_void_p()
                self._check(self.driver.cuModuleGetFunction(C.byref(self.function), self.module,
                    b"quest3d_reference_cubic"), "cuModuleGetFunction")
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _check(code, operation):
        if code:
            raise RuntimeError(f"{operation} failed with CUDA/NVRTC code {code}")

    @staticmethod
    def _validate_layout(image, width, height):
        """Metadata-only validation, separately CPU-testable without CUDA init."""
        if (not isinstance(image, torch.Tensor) or image.dtype != torch.uint8
                or image.ndim != 3 or image.shape[2] != 3
                or not all(0 < size <= 16384 for size in image.shape[:2])
                or any(stride < 0 for stride in image.stride())):
            raise ValueError("Expected nonempty HWC uint8 three-channel image up to 16384 pixels")
        if any(type(value) is not int or not 0 < value <= 16384 for value in (width, height)):
            raise ValueError("Resize dimensions must be integers in [1,16384]")

    def _complete(self, stream):
        try:
            stream.synchronize()
        except BaseException:
            self.faulted = True
            raise
        self._inflight = None

    def _execute(self, image, output, coefficients, stream):
        """Retain all kernel arguments until a real completion, including errors."""
        xi, xw, yi, yw = coefficients
        pointer = lambda tensor: C.c_uint64(tensor.data_ptr() if tensor is not None else 0)
        values = [pointer(image), pointer(output), C.c_int64(image.shape[1]), C.c_int64(image.shape[0]),
                  *(C.c_int64(stride) for stride in image.stride()),
                  C.c_int(output.shape[1]), C.c_int(output.shape[0]),
                  *(pointer(tensor) for tensor in coefficients)]
        arguments = (C.c_void_p * len(values))(*(C.addressof(value) for value in values))
        self._inflight = (stream, image, output, xi, xw, yi, yw)
        try:
            self._check(self.driver.cuLaunchKernel(
                self.function, (output.numel() + 255) // 256, 1, 1, 256, 1, 1, 0,
                C.c_void_p(stream.cuda_stream), arguments, None), "quest3d_reference_cubic")
        except BaseException:
            self.faulted = True
            raise
        finally:
            self._complete(stream)

    @torch.inference_mode()
    def __call__(self, image, width, height):
        with self._lock:
            if self.closed or self.faulted:
                raise RuntimeError("Cubic CUDA resize is closed or faulted")
            self._validate_layout(image, width, height)
            if not image.is_cuda or image.device.index != self.device:
                raise ValueError("Expected image on this owner's CUDA device")
            with torch.cuda.device(self.device):
                xi, xw = (self._reference._coefficients(image.shape[1], width, image.device)
                          if width != image.shape[1] else (None, None))
                yi, yw = (self._reference._coefficients(image.shape[0], height, image.device)
                          if height != image.shape[0] else (None, None))
                output = torch.empty((height, width, 3), device=image.device, dtype=torch.float32)
                self._execute(image, output, (xi, xw, yi, yw), torch.cuda.current_stream(self.device))
                return output

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
            self._reference.cache.clear()
            self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

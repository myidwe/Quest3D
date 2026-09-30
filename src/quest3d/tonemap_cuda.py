"""Explicit experimental fused tone mapper using the pinned Torch NVRTC DLL.

No compiler installation, network, cache execution, or new CUDA context. The
module loads in Torch's existing device context and launches on its current
stream. Every result is an owned, completed Torch tensor.
"""
import ctypes as C
import hashlib
import math
from pathlib import Path
import sys
import threading

import torch


class CudaToneMapper:
    def __init__(self, *, device=0):
        self.module = C.c_void_p()
        self.closed = False
        self._lock = threading.RLock()
        self.device = device
        if sys.platform != "win32" or torch.__version__ != "2.7.1+cu126":
            raise RuntimeError("This candidate is validated only with Windows Torch2.7.1+cu126")
        if type(device) is not int or not 0 <= device < torch.cuda.device_count():
            raise ValueError("Invalid CUDA device")
        if torch.cuda.get_device_capability(device) != (7, 5):
            raise RuntimeError("The fused candidate currently targets the verified sm75 device")
        library = Path(torch.__file__).resolve().parent / "lib" / "nvrtc64_120_0.dll"
        nvrtc = C.WinDLL(str(library))
        self.driver = C.WinDLL("C:/Windows/System32/nvcuda.dll")
        declarations = {
            "nvrtcVersion": [C.POINTER(C.c_int), C.POINTER(C.c_int)],
            "nvrtcCreateProgram": [C.POINTER(C.c_void_p), C.c_char_p, C.c_char_p,
                                    C.c_int, C.c_void_p, C.c_void_p],
            "nvrtcCompileProgram": [C.c_void_p, C.c_int, C.POINTER(C.c_char_p)],
            "nvrtcGetProgramLogSize": [C.c_void_p, C.POINTER(C.c_size_t)],
            "nvrtcGetProgramLog": [C.c_void_p, C.c_void_p],
            "nvrtcGetPTXSize": [C.c_void_p, C.POINTER(C.c_size_t)],
            "nvrtcGetPTX": [C.c_void_p, C.c_void_p],
            "nvrtcDestroyProgram": [C.POINTER(C.c_void_p)],
        }
        for name, args in declarations.items():
            getattr(nvrtc, name).argtypes, getattr(nvrtc, name).restype = args, C.c_int
        declarations = {
            "cuModuleLoadData": [C.POINTER(C.c_void_p), C.c_void_p],
            "cuModuleGetFunction": [C.POINTER(C.c_void_p), C.c_void_p, C.c_char_p],
            "cuLaunchKernel": [C.c_void_p] + [C.c_uint] * 7 + [C.c_void_p, C.POINTER(C.c_void_p), C.c_void_p],
            "cuModuleUnload": [C.c_void_p],
        }
        for name, args in declarations.items():
            getattr(self.driver, name).argtypes, getattr(self.driver, name).restype = args, C.c_int
        major, minor = C.c_int(), C.c_int()
        self._check(nvrtc.nvrtcVersion(C.byref(major), C.byref(minor)), "nvrtcVersion")
        if (major.value, minor.value) != (12, 6):
            raise RuntimeError("Expected the locked CUDA12.6 NVRTC")
        source = (Path(__file__).parent / "shaders/tone_map.cu").read_bytes()
        options = (C.c_char_p * 3)(b"--gpu-architecture=compute_75", b"--fmad=false", b"--std=c++11")
        program = C.c_void_p()
        self._check(nvrtc.nvrtcCreateProgram(C.byref(program), source, b"tone_map.cu", 0, None, None), "nvrtcCreateProgram")
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
                         "ptx_sha256": hashlib.sha256(ptx.raw).hexdigest(), "fmad": False}
        try:
            with torch.cuda.device(device):
                torch.empty(1, device=device)  # Initialize Torch's existing primary context.
                self._check(self.driver.cuModuleLoadData(C.byref(self.module), ptx), "cuModuleLoadData")
                self.function = C.c_void_p()
                self._check(self.driver.cuModuleGetFunction(C.byref(self.function), self.module,
                                                           b"quest3d_tone_map"), "cuModuleGetFunction")
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _check(code, operation):
        if code:
            raise RuntimeError(f"{operation} failed with CUDA/NVRTC code {code}")

    @torch.inference_mode()
    def __call__(self, rgba, *, sdr_white_scale, knee=.75):
        with self._lock:
            return self._map(rgba, sdr_white_scale=sdr_white_scale, knee=knee)

    def _map(self, rgba, *, sdr_white_scale, knee):
        if self.closed:
            raise RuntimeError("CUDA tone mapper is closed")
        if (not isinstance(rgba, torch.Tensor) or not rgba.is_cuda or rgba.device.index != self.device
                or rgba.dtype != torch.float16 or rgba.ndim != 3 or rgba.shape[2] != 4
                or not 0 < rgba.shape[0] <= 8192 or not 0 < rgba.shape[1] <= 8192
                or any(stride < 0 for stride in rgba.stride())):
            raise ValueError("Expected valid device-local FP16 HWC RGBA, up to8192 pixels per dimension")
        if (type(sdr_white_scale) not in (int, float) or not .001 <= sdr_white_scale <= 100
                or not math.isfinite(sdr_white_scale) or type(knee) not in (int, float)
                or not 0 < knee < 1 or not math.isfinite(knee)):
            raise ValueError("Invalid tone-map white or knee")
        height, width = rgba.shape[:2]
        with torch.cuda.device(self.device):
            output = torch.empty((height, width, 4), dtype=torch.uint8, device=rgba.device)
            values = [C.c_uint64(rgba.data_ptr()), C.c_uint64(output.data_ptr()),
                      C.c_uint(width), C.c_uint(height), *(C.c_uint64(s) for s in rgba.stride()),
                      C.c_float(sdr_white_scale), C.c_float(knee)]
            arguments = (C.c_void_p * len(values))(*(C.addressof(value) for value in values))
            stream = torch.cuda.current_stream(self.device)
            self._check(self.driver.cuLaunchKernel(self.function, (width * height + 255) // 256, 1, 1,
                        256, 1, 1, 0, C.c_void_p(stream.cuda_stream), arguments, None), "cuLaunchKernel")
            stream.synchronize()  # Retain input/output and module until GPU completion.
        return output

    def close(self):
        with self._lock:
            if self.module.value:
                with torch.cuda.device(self.device):
                    torch.cuda.synchronize(self.device)
                    self._check(self.driver.cuModuleUnload(self.module), "cuModuleUnload")
                self.module = C.c_void_p()
            self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

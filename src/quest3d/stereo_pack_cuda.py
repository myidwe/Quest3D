"""Fused Full-SBS packing for the verified CUDA stereo path.

No CUDA work occurs at import. Launch and close are serialized; failures retain
inflight tensors and the module until successful synchronization. No reusable
frame buffer, global owner cache, changed projector, or asynchronous return.
"""
import ctypes as C
import hashlib
from pathlib import Path
import sys
import threading

import torch


class CudaStereoPacker:
    def __init__(self, *, device=0):
        self.module = C.c_void_p()
        self.closed = False
        self.faulted = False
        self._inflight = None
        self._lock = threading.RLock()
        self.device = device
        if sys.platform != "win32" or torch.__version__ != "2.7.1+cu126":
            raise RuntimeError("Stereo packer requires pinned Windows Torch 2.7.1+cu126")
        if type(device) is not int or not 0 <= device < torch.cuda.device_count():
            raise ValueError("Invalid CUDA device")
        if torch.cuda.get_device_capability(device) != (7, 5):
            raise RuntimeError("Stereo packer currently targets verified sm75 only")
        nvrtc = C.WinDLL(str(Path(torch.__file__).resolve().parent / "lib/nvrtc64_120_0.dll"))
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
        for name, arguments in declarations.items():
            getattr(nvrtc, name).argtypes = arguments
            getattr(nvrtc, name).restype = C.c_int
        declarations = {
            "cuModuleLoadData": [C.POINTER(C.c_void_p), C.c_void_p],
            "cuModuleGetFunction": [C.POINTER(C.c_void_p), C.c_void_p, C.c_char_p],
            "cuLaunchKernel": [C.c_void_p] + [C.c_uint] * 7
                + [C.c_void_p, C.POINTER(C.c_void_p), C.c_void_p],
            "cuModuleUnload": [C.c_void_p],
        }
        for name, arguments in declarations.items():
            getattr(self.driver, name).argtypes = arguments
            getattr(self.driver, name).restype = C.c_int
        major, minor = C.c_int(), C.c_int()
        self._check(nvrtc.nvrtcVersion(C.byref(major), C.byref(minor)), "nvrtcVersion")
        if (major.value, minor.value) != (12, 6):
            raise RuntimeError("Expected pinned CUDA 12.6 NVRTC")
        source = (Path(__file__).parent / "shaders/stereo_pack.cu").read_bytes()
        options = (C.c_char_p * 3)(b"--gpu-architecture=compute_75", b"--fmad=false", b"--std=c++11")
        program = C.c_void_p()
        self._check(nvrtc.nvrtcCreateProgram(C.byref(program), source, b"stereo_pack_cuda.cu",
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
                         "completion": "owned pinned CPU tensor, synchronized current Torch stream"}
        try:
            with torch.cuda.device(device):
                torch.empty(1, device=device)
                self._check(self.driver.cuModuleLoadData(C.byref(self.module), ptx), "cuModuleLoadData")
                self.function = C.c_void_p()
                self._check(self.driver.cuModuleGetFunction(C.byref(self.function), self.module,
                                                           b"quest3d_pack_stereo"), "cuModuleGetFunction")
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _check(code, operation):
        if code:
            raise RuntimeError(f"{operation} failed with CUDA/NVRTC code {code}")

    def _complete(self, stream):
        try:
            stream.synchronize()
        except BaseException:
            self.faulted = True
            raise
        self._inflight = None

    @torch.inference_mode()
    def __call__(self, eyes, eye_width, eye_height, content_rect):
        with self._lock:
            if self.closed or self.faulted:
                raise RuntimeError("CUDA stereo packer is closed or faulted")
            if (not isinstance(eyes, torch.Tensor) or not eyes.is_cuda
                    or eyes.device.index != self.device or eyes.dtype != torch.float32
                    or eyes.ndim != 4 or eyes.shape[:2] != (2, 3) or not eyes.is_contiguous()):
                raise ValueError("Expected owned-device contiguous float32 [2,3,H,W] eyes")
            if (type(eye_width) is not int or type(eye_height) is not int
                    or not 1 <= eye_width <= 8192 or not 1 <= eye_height <= 8192):
                raise ValueError("Eye dimensions must be integers in [1,8192]")
            if not isinstance(content_rect, tuple) or len(content_rect) != 4:
                raise ValueError("Content rectangle must be (x,y,width,height)")
            x, y, width, height = content_rect
            if (any(type(v) is not int for v in content_rect) or min(x, y) < 0
                    or min(width, height) < 1 or x + width > eye_width or y + height > eye_height
                    or (height, width) != eyes.shape[-2:]):
                raise ValueError("Content rectangle must fit the eye and match the input")
            with torch.cuda.device(self.device):
                stream = torch.cuda.current_stream(self.device)
                output = torch.empty((eye_height, eye_width * 2, 4), dtype=torch.uint8, device=eyes.device)
                cpu_output = torch.empty(output.shape, dtype=torch.uint8, device="cpu", pin_memory=True)
                values = [C.c_uint64(eyes.data_ptr()), C.c_uint64(output.data_ptr()),
                          C.c_int(eye_width), C.c_int(eye_height), C.c_int(x), C.c_int(y),
                          C.c_int(width), C.c_int(height)]
                arguments = (C.c_void_p * len(values))(*(C.addressof(value) for value in values))
                self._inflight = (stream, eyes, output, cpu_output)
                try:
                    self._check(self.driver.cuLaunchKernel(
                        self.function, (eye_width * eye_height * 2 + 255) // 256, 1, 1,
                        256, 1, 1, 0, C.c_void_p(stream.cuda_stream), arguments, None), "quest3d_pack_stereo")
                    cpu_output.copy_(output, non_blocking=True)
                except BaseException:
                    self.faulted = True
                    raise
                finally:
                    self._complete(stream)
                return cpu_output

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
            self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

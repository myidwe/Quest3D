"""Explicit pinned CUDA implementation of depth_edges.py, with no fallback.

Compilation/context creation never happens at import. Each owner compiles the
adjacent source in Torch's primary context and serializes launch/close. Calls use
the caller's current Torch stream and return only after actual synchronization.
All input/scratch/output tensors stay retained until verified completion. A
launch/sync failure makes the owner terminal; failed close retains its module and
in-flight tensors. No workers, settings, global CUDA policy or live state change.

Use CudaDepthEdges for session ownership, or explicitly call
close_cached_depth_edges_cuda() on shutdown when using the convenience function.
This is a candidate, not a claim of correct boundaries or real-time performance.
"""

import ctypes as C
import hashlib
import math
from pathlib import Path
import sys
import threading

import torch

from .depth_edges import _validate


class CudaDepthEdges:
    """Owned NVRTC module. Same fallible lifetime contract as CudaForwardWarper."""

    def __init__(self, *, device=0):
        self.module = C.c_void_p()
        self.closed = False
        self.faulted = False
        self._inflight = None
        self._lock = threading.RLock()
        self.device = device
        if sys.platform != "win32" or torch.__version__ != "2.7.1+cu126":
            raise RuntimeError("Depth edges CUDA requires pinned Windows Torch 2.7.1+cu126")
        if type(device) is not int or not 0 <= device < torch.cuda.device_count():
            raise ValueError("Invalid CUDA device")
        if torch.cuda.get_device_capability(device) != (7, 5):
            raise RuntimeError("Depth edges CUDA supports the verified sm75 device only")
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
            raise RuntimeError("Expected locked CUDA 12.6 NVRTC")
        source = (Path(__file__).parent / "shaders/depth_edges.cu").read_bytes()
        options = (C.c_char_p * 3)(b"--gpu-architecture=compute_75", b"--fmad=false", b"--std=c++11")
        program = C.c_void_p()
        self._check(nvrtc.nvrtcCreateProgram(C.byref(program), source, b"depth_edges.cu",
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
        self.metadata = {
            "nvrtc": [major.value, minor.value], "architecture": "compute_75", "fmad": False,
            "source_sha256": hashlib.sha256(source).hexdigest(),
            "ptx_sha256": hashlib.sha256(ptx.raw).hexdigest(),
            "algorithm": "gated four-donor joint-bilateral depth with final smooth bounded correction",
            "completion": "current Torch stream synchronized before return",
        }
        self.functions = {}
        try:
            with torch.cuda.device(device):
                torch.empty(1, device=device)  # Establish/reuse Torch's primary context.
                self._check(self.driver.cuModuleLoadData(C.byref(self.module), ptx), "cuModuleLoadData")
                for name in ("quest3d_depth_low_prepare", "quest3d_depth_rgb_edge",
                             "quest3d_depth_max_horizontal", "quest3d_depth_max_vertical",
                             "quest3d_depth_joint_blend", "quest3d_depth_bilinear"):
                    function = C.c_void_p()
                    self._check(self.driver.cuModuleGetFunction(C.byref(function), self.module,
                                                               name.encode("ascii")), "cuModuleGetFunction")
                    self.functions[name] = function
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _check(code, operation):
        if code:
            raise RuntimeError(f"{operation} failed with CUDA/NVRTC code {code}")

    def _launch(self, name, blocks, values, stream):
        arguments = (C.c_void_p * len(values))(*(C.addressof(value) for value in values))
        self._check(self.driver.cuLaunchKernel(
            self.functions[name], blocks, 1, 1, 256, 1, 1, 0,
            C.c_void_p(stream.cuda_stream), arguments, None), name)

    def _complete(self, stream):
        try:
            stream.synchronize()
        except BaseException:
            self.faulted = True
            raise
        self._inflight = None

    @torch.inference_mode()
    def __call__(self, rgb, low_depth, *, strength=.75, max_correction=.04):
        with self._lock:
            if self.closed or self.faulted:
                raise RuntimeError("CUDA depth edges owner is closed or faulted")
            try:
                _validate(rgb, low_depth, strength, max_correction)
            except RuntimeError:
                self.faulted = True
                raise
            if not rgb.is_cuda or rgb.device.index != self.device:
                raise ValueError("Expected RGB/depth on this owner's CUDA device")
            height, width = rgb.shape[-2:]
            low_height, low_width = low_depth.shape[-2:]
            if width > 2**24 or height > 2**24 or 3 * height * width >= 2**31:
                raise ValueError("Image dimensions exceed CUDA kernel indexing bounds")
            with torch.cuda.device(self.device):
                stream = torch.cuda.current_stream(self.device)
                image, depth = rgb.contiguous(), low_depth.contiguous()
                output = torch.empty((height, width), dtype=torch.float32, device=rgb.device)
                blocks = (height * width + 255) // 256
                pointer = lambda tensor: C.c_uint64(tensor.data_ptr())
                dimensions = [C.c_int(width), C.c_int(height), C.c_int(low_width), C.c_int(low_height)]
                scale = [C.c_float((low_width - 1) / (width - 1) if width > 1 else 0),
                         C.c_float((low_height - 1) / (height - 1) if height > 1 else 0)]
                if strength == 0 or max_correction == 0 or (height, width) == (low_height, low_width):
                    self._inflight = (stream, rgb, low_depth, image, depth, output)
                    try:
                        if (height, width) == (low_height, low_width):
                            output.copy_(depth[0])
                        else:
                            self._launch("quest3d_depth_bilinear", blocks,
                                [pointer(depth), pointer(output), *dimensions, *scale], stream)
                    except BaseException:
                        self.faulted = True
                        raise
                    finally:
                        self._complete(stream)
                    return output
                guide = torch.empty((3, low_height, low_width), dtype=torch.float32, device=rgb.device)
                curve = torch.empty_like(depth[0])
                edge = torch.empty_like(output)
                pooled_x = torch.empty_like(output)
                pooled_y = torch.empty_like(output)
                rx = math.ceil((width - 1) / (low_width - 1)) if low_width > 1 else 0
                ry = math.ceil((height - 1) / (low_height - 1)) if low_height > 1 else 0
                self._inflight = (stream, rgb, low_depth, image, depth, output, guide,
                                  curve, edge, pooled_x, pooled_y)
                try:
                    self._launch("quest3d_depth_low_prepare", (low_height * low_width + 255) // 256,
                        [pointer(image), pointer(depth), pointer(guide), pointer(curve), *dimensions,
                         C.c_float((width - 1) / (low_width - 1) if low_width > 1 else 0),
                         C.c_float((height - 1) / (low_height - 1) if low_height > 1 else 0)], stream)
                    self._launch("quest3d_depth_rgb_edge", blocks,
                        [pointer(image), pointer(edge), *dimensions[:2]], stream)
                    self._launch("quest3d_depth_max_horizontal", blocks,
                        [pointer(edge), pointer(pooled_x), *dimensions[:2], C.c_int(rx)], stream)
                    self._launch("quest3d_depth_max_vertical", blocks,
                        [pointer(pooled_x), pointer(pooled_y), *dimensions[:2], C.c_int(ry)], stream)
                    self._launch("quest3d_depth_joint_blend", blocks,
                        [pointer(image), pointer(depth), pointer(guide), pointer(curve), pointer(pooled_y),
                         pointer(output), *dimensions, *scale, C.c_float(strength), C.c_float(max_correction)], stream)
                except BaseException:
                    self.faulted = True
                    raise
                finally:
                    self._complete(stream)
                return output

    def close(self):
        with self._lock:
            if self.module.value:
                with torch.cuda.device(self.device):
                    try:
                        # A failed sync must keep BOTH module and tensor bundle.
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


_cache_lock = threading.RLock()
_owners: dict[int, CudaDepthEdges] = {}


def edge_aware_upsample_depth_cuda(rgb, low_depth, *, strength=.75, max_correction=.04):
    """Same input/result contract as edge_aware_upsample_depth; CUDA-only.

    Per-device cache ownership ends only on close_cached_depth_edges_cuda().
    Prefer an explicit CudaDepthEdges when a session can own/close it directly.
    """
    if not isinstance(rgb, torch.Tensor) or not rgb.is_cuda:
        raise ValueError("edge_aware_upsample_depth_cuda requires CUDA RGB")
    with _cache_lock:
        device = rgb.device.index
        if device not in _owners:
            _owners[device] = CudaDepthEdges(device=device)
        return _owners[device](rgb, low_depth, strength=strength, max_correction=max_correction)


def close_cached_depth_edges_cuda():
    """Actual sync/unload per cached module; a failed close leaves it owned."""
    with _cache_lock:
        for device in tuple(_owners):
            _owners[device].close()
            del _owners[device]

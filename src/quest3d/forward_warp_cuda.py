"""Explicit pinned sm75 CUDA implementation of the forward-warp reference.

No inference, context creation, or compilation happens at import. An owner loads
the NVRTC program in Torch's existing primary context and serializes launch and
close. Calls use the calling thread's current Torch stream, retain every tensor
until that stream actually completes, and propagate launch/asynchronous errors.
A failed owner is terminal: there is no retry, CPU fallback, or quality fallback.

The convenience function retains one owner per device. Call
close_cached_forward_warp_cuda() at the containing session's shutdown, or use an
explicit CudaForwardWarper context manager instead. Cache removal occurs only
after successful close; failed synchronization does not authorize module unload.
"""

import ctypes as C
from dataclasses import dataclass
import hashlib
import math
from pathlib import Path
import sys
import threading

import torch

from .forward_warp import ForwardWarpResult, _validate, _validate_metadata


def _validate_projection_depth_range(value):
    """Validate scalar search bounds without reading tensors or starting CUDA."""
    if (not isinstance(value, tuple) or len(value) != 2
            or any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in value)
            or not 0 <= value[0] <= value[1] <= 1
            or not all(math.isfinite(item) for item in value)):
        raise ValueError("projection_depth_range must be a finite (low, high) tuple with 0 <= low <= high <= 1")
    return float(value[0]), float(value[1])


def _validate_projection_inputs(image, inverse_depth, disparity_px, convergence, depth_range):
    if depth_range == (0., 1.):
        return _validate(image, inverse_depth, disparity_px, convergence)
    _validate_metadata(image, inverse_depth, disparity_px, convergence)
    low, high = depth_range
    # Match the original single-receipt validation, replacing its depth bounds
    # rather than performing a second full-device scan for the narrower range.
    valid = (torch.isfinite(image).all() & (image >= 0).all() & (image <= 1).all()
             & torch.isfinite(inverse_depth).all()
             & (inverse_depth >= low).all() & (inverse_depth <= high).all())
    if not bool(valid):
        raise ValueError("Image and inverse depth must be finite and in [0, 1]; inverse depth must fit projection_depth_range")


@dataclass(frozen=True)
class PackedForwardWarpResult:
    cpu_bgra: torch.Tensor  # Owned pinned CPU [eye_height,2*eye_width,4] uint8.
    hole_mask: torch.Tensor  # Owned CUDA [2,H,W], before filling.
    filled_mask: torch.Tensor
    reconstructed_mask: torch.Tensor
    estimated_donor_mask: torch.Tensor
    owner_metadata: dict


class CudaForwardWarper:
    """Owned module, no worker threads; public calls return completed tensors."""

    def __init__(self, *, device=0, fused_fill=False):
        self.module = C.c_void_p()
        self.closed = False
        self.faulted = False
        self._inflight = None
        self._lock = threading.RLock()
        self.device = device
        if type(fused_fill) is not bool:
            raise ValueError("fused_fill must be an explicit boolean")
        self.fused_fill = fused_fill
        if sys.platform != "win32" or torch.__version__ != "2.7.1+cu126":
            raise RuntimeError("Forward CUDA requires pinned Windows Torch 2.7.1+cu126")
        if type(device) is not int or not 0 <= device < torch.cuda.device_count():
            raise ValueError("Invalid CUDA device")
        if torch.cuda.get_device_capability(device) != (7, 5):
            raise RuntimeError("Forward CUDA currently supports the verified sm75 device only")
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
        source = (Path(__file__).parent / "shaders/forward_warp.cu").read_bytes()
        options = (C.c_char_p * 3)(b"--gpu-architecture=compute_75", b"--fmad=false", b"--std=c++11")
        program = C.c_void_p()
        self._check(nvrtc.nvrtcCreateProgram(C.byref(program), source, b"forward_warp.cu",
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
            "nvrtc": [major.value, minor.value], "architecture": "compute_75",
            "source_sha256": hashlib.sha256(source).hexdigest(),
            "ptx_sha256": hashlib.sha256(ptx.raw).hexdigest(), "fmad": False,
            "algorithm": "strict forward depth visibility + primary donor + partial reconstruction + estimated original donor",
            "completion": "current Torch stream synchronized before return",
            "fused_fill": fused_fill,
        }
        self.functions = {}
        try:
            with torch.cuda.device(device):
                torch.empty(1, device=device)  # Reuse Torch's primary context.
                self._check(self.driver.cuModuleLoadData(C.byref(self.module), ptx), "cuModuleLoadData")
                names = ["quest3d_forward_project", "quest3d_forward_donor_bounds", "quest3d_forward_fill",
                         "quest3d_forward_estimated_donor_bounds", "quest3d_forward_estimated_donor_fill"]
                if self.fused_fill:
                    names += ["quest3d_forward_all_donor_bounds", "quest3d_forward_all_fill",
                              "quest3d_forward_all_fill_packed", "quest3d_forward_zero_packed",
                              "quest3d_forward_validate_values"]
                for name in names:
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

    def _launch_packed_grid(self, name, grid, values, stream):
        """Packed-only 32x8 tiles: one warp per row, with an explicit eye axis."""
        arguments = (C.c_void_p * len(values))(*(C.addressof(value) for value in values))
        self._check(self.driver.cuLaunchKernel(
            self.functions[name], *grid, 32, 8, 1, 0,
            C.c_void_p(stream.cuda_stream), arguments, None), name)

    def _complete(self, stream):
        try:
            stream.synchronize()
        except BaseException:
            # A sync error is not a completion receipt. Retain the stream and
            # every tensor even when the public call's Python locals unwind.
            self.faulted = True
            raise
        self._inflight = None

    @torch.inference_mode()
    def __call__(self, image, inverse_depth, disparity_px, convergence, *, projection_depth_range=(0., 1.)):
        with self._lock:
            if self.closed or self.faulted:
                raise RuntimeError("CUDA forward owner is closed or faulted")
            depth_range = _validate_projection_depth_range(projection_depth_range)
            try:
                _validate_projection_inputs(image, inverse_depth, disparity_px, convergence, depth_range)
            except RuntimeError:
                self.faulted = True
                raise
            if not image.is_cuda or image.device.index != self.device:
                raise ValueError("Expected image/depth on this owner's CUDA device")
            height, width = inverse_depth.shape
            if width > 2**24 or height > 2**24 or 2 * height * width >= 2**31:
                raise ValueError("Image dimensions exceed CUDA kernel indexing bounds")
            with torch.cuda.device(self.device):
                stream = torch.cuda.current_stream(self.device)
                # These references stay alive through actual stream completion,
                # including a failure after one kernel has already launched.
                # A contiguous view can still carry a lazy negative bit. Native
                # pointer reads require its logical values to be materialized.
                rgb = image.resolve_neg().contiguous()
                depth = inverse_depth.resolve_neg().contiguous()
                output = torch.empty((2, 3, height, width), dtype=torch.float32, device=image.device)
                holes = torch.empty((2, height, width), dtype=torch.bool, device=image.device)
                filled = torch.empty_like(holes)
                reconstructed = torch.empty_like(holes)
                estimated = torch.empty_like(holes)
                if disparity_px == 0:
                    self._inflight = (stream, image, inverse_depth, rgb, depth, output, holes,
                                      filled, reconstructed, estimated)
                    try:
                        output.copy_(rgb.expand(2, -1, -1, -1))
                        holes.zero_()
                        filled.zero_()
                        reconstructed.zero_()
                        estimated.zero_()
                    except BaseException:
                        self.faulted = True
                        raise
                    finally:
                        self._complete(stream)
                    return ForwardWarpResult(output, holes, filled, reconstructed, estimated)

                raw = torch.empty_like(output)
                remaining = torch.empty((2, height, width), dtype=torch.float32, device=image.device)
                nearest = torch.empty_like(remaining)
                farthest = torch.empty_like(remaining)
                bounds = torch.empty((2 * height, 4 if self.fused_fill else 2), dtype=torch.int32, device=image.device)
                # Python's ceil preserves the reference's scalar bound; no
                # tensor scalar leaves the device inside the three kernels.
                import math
                layers = min(width, math.ceil(disparity_px / 2) + 2)
                donor_depth_tolerance = 0.5 / disparity_px
                blocks = (2 * height * width + 255) // 256
                pointer = lambda tensor: C.c_uint64(tensor.data_ptr())
                self._inflight = (stream, image, inverse_depth, rgb, depth, raw, remaining, nearest,
                                  farthest, holes, bounds, output, filled, reconstructed, estimated)
                try:
                    self._launch("quest3d_forward_project", blocks, [
                        pointer(rgb), pointer(depth), pointer(raw), pointer(remaining),
                        pointer(nearest), pointer(farthest), pointer(holes),
                        C.c_int(width), C.c_int(height), C.c_float(disparity_px / 2),
                        C.c_float(convergence), C.c_int(layers),
                        C.c_float(depth_range[0]), C.c_float(depth_range[1]),
                    ], stream)
                    if self.fused_fill:
                        self._launch("quest3d_forward_all_donor_bounds", 2 * height, [
                            pointer(holes), pointer(remaining), pointer(nearest), pointer(farthest), pointer(bounds),
                            C.c_int(width), C.c_int(height), C.c_float(donor_depth_tolerance),
                        ], stream)
                        self._launch("quest3d_forward_all_fill", blocks, [
                            pointer(raw), pointer(remaining), pointer(nearest), pointer(farthest),
                            pointer(holes), pointer(bounds), pointer(output), pointer(filled),
                            pointer(reconstructed), pointer(estimated), C.c_int(width), C.c_int(height),
                            C.c_float(donor_depth_tolerance), C.c_int(layers),
                        ], stream)
                    else:
                        self._launch_reference_fill(stream, blocks, height, width, pointer, raw,
                            remaining, nearest, farthest, holes, bounds, output, filled,
                            reconstructed, estimated, donor_depth_tolerance, layers)
                except BaseException:
                    self.faulted = True
                    raise
                finally:
                    self._complete(stream)
                return ForwardWarpResult(output, holes, filled, reconstructed, estimated)

    def _launch_reference_fill(self, stream, blocks, height, width, pointer, raw,
                               remaining, nearest, farthest, holes, bounds, output, filled,
                               reconstructed, estimated, donor_depth_tolerance, layers):
        self._launch("quest3d_forward_donor_bounds", 2 * height, [
            pointer(holes), pointer(nearest), pointer(farthest), pointer(bounds),
            C.c_int(width), C.c_int(height), C.c_float(donor_depth_tolerance),
        ], stream)
        self._launch("quest3d_forward_fill", blocks, [
            pointer(raw), pointer(remaining), pointer(nearest), pointer(farthest),
            pointer(holes), pointer(bounds), pointer(output), pointer(filled), pointer(reconstructed),
            C.c_int(width), C.c_int(height), C.c_float(donor_depth_tolerance), C.c_int(layers),
        ], stream)
        # Same-stream ordering allows reuse of the small bounds
        # buffer after the primary fill has finished reading it.
        self._launch("quest3d_forward_estimated_donor_bounds", 2 * height, [
            pointer(remaining), pointer(nearest), pointer(farthest), pointer(bounds),
            C.c_int(width), C.c_int(height), C.c_float(donor_depth_tolerance),
        ], stream)
        self._launch("quest3d_forward_estimated_donor_fill", blocks, [
            pointer(raw), pointer(remaining), pointer(nearest), pointer(farthest),
            pointer(holes), pointer(bounds), pointer(output), pointer(filled), pointer(estimated),
            C.c_int(width), C.c_int(height), C.c_float(donor_depth_tolerance), C.c_int(layers),
        ], stream)

    @staticmethod
    def _validate_pack_geometry(eye_width, eye_height, content_rect, source_shape):
        """Metadata-only geometry checks before allocations or CUDA launches."""
        if (type(eye_width) is not int or type(eye_height) is not int
                or not 1 <= eye_width <= 8192 or not 1 <= eye_height <= 8192):
            raise ValueError("Eye dimensions must be integers in [1,8192]")
        if not isinstance(content_rect, tuple) or len(content_rect) != 4:
            raise ValueError("Content rectangle must be (x,y,width,height)")
        x, y, width, height = content_rect
        if (any(type(value) is not int for value in content_rect)
                or min(x, y) < 0 or min(width, height) < 1
                or x + width > eye_width or y + height > eye_height
                or tuple(source_shape) != (height, width)):
            raise ValueError("Content rectangle must fit the eye and match the input")

    def _execute_packed(self, stream, launches, packed, cpu_bgra, keepalive):
        """A failed launch/copy/sync retains everything until successful close."""
        self._inflight = (stream, *keepalive, packed, cpu_bgra)
        try:
            for name, blocks, values in launches:
                if isinstance(blocks, tuple):
                    self._launch_packed_grid(name, blocks, values, stream)
                else:
                    self._launch(name, blocks, values, stream)
            cpu_bgra.copy_(packed, non_blocking=True)
        except BaseException:
            self.faulted = True
            raise
        finally:
            self._complete(stream)

    def _execute_validation(self, image, inverse_depth, rgb, depth, flag, receipt, stream,
                            projection_depth_range=(0., 1.)):
        """A completed scalar receipt distinguishes invalid values from GPU faults."""
        count = depth.numel()
        self._inflight = (stream, image, inverse_depth, rgb, depth, flag, receipt)
        try:
            self._launch("quest3d_forward_validate_values", min((count + 255) // 256, 1024), [
                C.c_uint64(rgb.data_ptr()), C.c_uint64(depth.data_ptr()),
                C.c_uint64(flag.data_ptr()), C.c_int(count),
                C.c_int(rgb.is_neg()), C.c_int(depth.is_neg()),
                C.c_float(projection_depth_range[0]), C.c_float(projection_depth_range[1]),
            ], stream)
            receipt.copy_(flag, non_blocking=True)
        except BaseException:
            self.faulted = True
            raise
        finally:
            self._complete(stream)
        # This CPU read is reached only after actual current-stream completion.
        # A value error is ordinary invalid input, so the owner remains usable.
        if receipt.item() != 0:
            suffix = "; inverse depth must fit projection_depth_range" if projection_depth_range != (0., 1.) else ""
            raise ValueError("Image and inverse depth must be finite and in [0, 1]" + suffix)

    def _validate_values_cuda(self, image, inverse_depth, rgb, depth, stream,
                              projection_depth_range=(0., 1.)):
        # Only the four-byte flag is initialized; every source element is read
        # by one explicit CUDA kernel. These buffers are fresh for each call.
        flag = torch.zeros((), dtype=torch.int32, device=rgb.device)
        receipt = torch.empty((), dtype=torch.int32, device="cpu", pin_memory=True)
        self._execute_validation(image, inverse_depth, rgb, depth, flag, receipt, stream,
                                 projection_depth_range)

    @torch.inference_mode()
    def packed(self, image, inverse_depth, disparity_px, convergence,
               eye_width, eye_height, content_rect, *, fused_validation=False,
               projection_depth_range=(0., 1.)) -> PackedForwardWarpResult:
        """Project/fill directly into owned Full-SBS BGRA, then complete D2H.

        This explicit fused-owner API keeps all visibility, donor and float32
        rounding rules of ``self(...)`` followed by CudaStereoPacker. It omits
        only the final planar float eyes allocation and its separate pack pass.
        Masks describe fitted content, excluding letterbox padding, as before.
        ``fused_validation=True`` checks the same values with one CUDA kernel
        and a completed scalar receipt before projection. The default keeps
        the original Torch validation expression.
        ``projection_depth_range`` narrows only the source search, after checking
        every depth value against these bounds. It does not clamp/remap depth or
        change visibility, layer count, donor distance, or colour arithmetic.
        """
        with self._lock:
            if self.closed or self.faulted:
                raise RuntimeError("CUDA forward owner is closed or faulted")
            if not self.fused_fill:
                raise RuntimeError("Packed forward output requires fused_fill=True")
            if type(fused_validation) is not bool:
                raise ValueError("fused_validation must be an explicit boolean")
            depth_range = _validate_projection_depth_range(projection_depth_range)
            source_shape = image.shape[-2:] if isinstance(image, torch.Tensor) else ()
            self._validate_pack_geometry(eye_width, eye_height, content_rect, source_shape)
            try:
                if fused_validation:
                    _validate_metadata(image, inverse_depth, disparity_px, convergence)
                else:
                    _validate_projection_inputs(image, inverse_depth, disparity_px, convergence, depth_range)
            except RuntimeError:
                self.faulted = True
                raise
            if not image.is_cuda or image.device.index != self.device:
                raise ValueError("Expected image/depth on this owner's CUDA device")
            height, width = inverse_depth.shape
            x, y, _, _ = content_rect
            with torch.cuda.device(self.device):
                stream = torch.cuda.current_stream(self.device)
                rgb = image.resolve_neg().contiguous()
                depth = inverse_depth.resolve_neg().contiguous()
                if fused_validation:
                    self._validate_values_cuda(image, inverse_depth, rgb, depth, stream, depth_range)
                holes = torch.empty((2, height, width), dtype=torch.bool, device=image.device)
                filled, reconstructed, estimated = (torch.empty_like(holes) for _ in range(3))
                packed = torch.empty((eye_height, 2 * eye_width, 4), dtype=torch.uint8, device=image.device)
                cpu_bgra = torch.empty(packed.shape, dtype=torch.uint8, device="cpu", pin_memory=True)
                pointer = lambda tensor: C.c_uint64(tensor.data_ptr())
                packed_grid = ((eye_width + 31) // 32, (eye_height + 7) // 8, 2)
                geometry = [C.c_int(width), C.c_int(height), C.c_int(eye_width),
                            C.c_int(eye_height), C.c_int(x), C.c_int(y)]
                keepalive = [image, inverse_depth, rgb, depth, holes, filled, reconstructed, estimated]
                if disparity_px == 0:
                    launches = [("quest3d_forward_zero_packed", packed_grid, [
                        pointer(rgb), pointer(packed), pointer(holes), pointer(filled),
                        pointer(reconstructed), pointer(estimated), *geometry,
                    ])]
                else:
                    import math
                    raw = torch.empty((2, 3, height, width), dtype=torch.float32, device=image.device)
                    remaining = torch.empty((2, height, width), dtype=torch.float32, device=image.device)
                    nearest, farthest = torch.empty_like(remaining), torch.empty_like(remaining)
                    bounds = torch.empty((2 * height, 4), dtype=torch.int32, device=image.device)
                    keepalive.extend((raw, remaining, nearest, farthest, bounds))
                    layers = min(width, math.ceil(disparity_px / 2) + 2)
                    donor_depth_tolerance = 0.5 / disparity_px
                    launches = [
                        ("quest3d_forward_project", (2 * height * width + 255) // 256, [
                            pointer(rgb), pointer(depth), pointer(raw), pointer(remaining),
                            pointer(nearest), pointer(farthest), pointer(holes),
                            C.c_int(width), C.c_int(height), C.c_float(disparity_px / 2),
                            C.c_float(convergence), C.c_int(layers),
                            C.c_float(depth_range[0]), C.c_float(depth_range[1]),
                        ]),
                        ("quest3d_forward_all_donor_bounds", 2 * height, [
                            pointer(holes), pointer(remaining), pointer(nearest), pointer(farthest),
                            pointer(bounds), C.c_int(width), C.c_int(height), C.c_float(donor_depth_tolerance),
                        ]),
                        ("quest3d_forward_all_fill_packed", packed_grid, [
                            pointer(raw), pointer(remaining), pointer(nearest), pointer(farthest),
                            pointer(holes), pointer(bounds), pointer(packed), pointer(filled),
                            pointer(reconstructed), pointer(estimated), *geometry,
                            C.c_float(donor_depth_tolerance), C.c_int(layers),
                        ]),
                    ]
                self._execute_packed(stream, launches, packed, cpu_bgra, keepalive)
                return PackedForwardWarpResult(cpu_bgra, holes, filled, reconstructed, estimated,
                    {**self.metadata, "output": "owned pinned CPU BGRA; current stream completed",
                     "packed_output": True, "kernels": len(launches),
                     "packed_grid": list(packed_grid), "packed_block": [32, 8, 1],
                     "projection_depth_range": list(depth_range),
                     "validation": "fused-cuda" if fused_validation else "torch"})

    def close(self):
        with self._lock:
            if self.module.value:
                with torch.cuda.device(self.device):
                    # Never unload after a failed synchronization. The original
                    # handle remains owned so the caller can report/retry close.
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


_cache_lock = threading.RLock()
_owners: dict[tuple[int, bool], CudaForwardWarper] = {}


def synthesize_forward_cuda(image, inverse_depth, disparity_px, convergence, *, fused_fill=False,
                            projection_depth_range=(0., 1.)) -> ForwardWarpResult:
    """Same result contract as synthesize_forward; CUDA-only, no fallback.

    Use an explicit CudaForwardWarper for session-local ownership. This helper's
    per-device/implementation module is retained until close_cached_forward_warp_cuda().
    The default retains the original five-kernel path; fused_fill=True selects
    the exact-output three-kernel path after the containing product opts in.
    """
    if type(fused_fill) is not bool:
        raise ValueError("fused_fill must be an explicit boolean")
    depth_range = _validate_projection_depth_range(projection_depth_range)
    if not isinstance(image, torch.Tensor) or not image.is_cuda:
        raise ValueError("synthesize_forward_cuda requires a CUDA image")
    device = image.device.index
    key = (device, fused_fill)
    # Serialize cache creation/removal with the entire completed call. The owner
    # also locks its own launch path, including explicit-owner callers.
    with _cache_lock:
        if key not in _owners:
            _owners[key] = CudaForwardWarper(device=device, fused_fill=fused_fill)
        options = {"projection_depth_range": depth_range} if depth_range != (0., 1.) else {}
        return _owners[key](image, inverse_depth, disparity_px, convergence, **options)


def synthesize_forward_packed_cuda(image, inverse_depth, disparity_px, convergence,
                                   eye_width, eye_height, content_rect, *,
                                   fused_validation=False, projection_depth_range=(0., 1.)) -> PackedForwardWarpResult:
    """Explicit CUDA project/fill/pack path sharing the cached fused owner.

    Output is independently owned pinned CPU BGRA plus completed CUDA masks.
    No fallback or cache eviction occurs after a launch/completion failure.
    """
    if type(fused_validation) is not bool:
        raise ValueError("fused_validation must be an explicit boolean")
    depth_range = _validate_projection_depth_range(projection_depth_range)
    if not isinstance(image, torch.Tensor) or not image.is_cuda:
        raise ValueError("synthesize_forward_packed_cuda requires a CUDA image")
    device = image.device.index
    key = (device, True)
    with _cache_lock:
        if key not in _owners:
            _owners[key] = CudaForwardWarper(device=device, fused_fill=True)
        options = {"fused_validation": True} if fused_validation else {}
        if depth_range != (0., 1.):
            options["projection_depth_range"] = depth_range
        return _owners[key].packed(image, inverse_depth, disparity_px, convergence,
                                  eye_width, eye_height, content_rect, **options)


def close_cached_forward_warp_cuda():
    """Actually synchronize/unload each cached owner; failed closes stay owned."""
    with _cache_lock:
        for key in tuple(_owners):
            _owners[key].close()
            del _owners[key]

"""CPU contract tests and explicitly gated real CUDA exact-parity tests.

Run real GPU tests only when the owner grants a GPU measurement window:
QUEST3D_RUN_GPU_TESTS=1 uv run --locked --offline --extra gpu-capture pytest
tests/test_resample_cuda.py. Normal collection never initializes CUDA.
"""
from contextlib import nullcontext
import ctypes as C
import hashlib
import json
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest
import torch

from quest3d.resample import ReferenceCubicResize, _axis_coefficients
from quest3d.resample_cuda import CudaReferenceCubicResize


def _owner():
    owner = object.__new__(CudaReferenceCubicResize)
    owner.module = C.c_void_p(123)
    owner.function = C.c_void_p(456)
    owner.closed = owner.faulted = False
    owner.device = 0
    owner._lock = threading.RLock()
    owner._inflight = None
    owner._reference = ReferenceCubicResize()
    return owner


def _scalar_order(image, width, height):
    """CPU emulation of the proposed kernel rounding; not GPU evidence."""
    sh, sw = image.shape[:2]
    xi, xw = _axis_coefficients(sw, width) if sw != width else (None, None)
    yi, yw = _axis_coefficients(sh, height) if sh != height else (None, None)

    def horizontal(row, x):
        if xi is None:
            return image[row, x].astype(np.float32)
        value = np.float32(image[row, xi[x, 0]].astype(np.float32) * xw[x, 0])
        for tap in range(1, 4):
            product = np.float32(image[row, xi[x, tap]].astype(np.float32) * xw[x, tap])
            value = np.float32(value + product)
        return value

    result = np.empty((height, width, 3), dtype=np.float32)
    for y in range(height):
        for x in range(width):
            if yi is None:
                result[y, x] = horizontal(y, x)
                continue
            value = np.float32(horizontal(yi[y, 0], x) * yw[y, 0])
            for tap in range(1, 4):
                value = np.float32(value + np.float32(horizontal(yi[y, tap], x) * yw[y, tap]))
            result[y, x] = value
    return result


@pytest.mark.parametrize("shape,target", [
    ((17, 29), (7, 9)), ((7, 9), (19, 13)), ((1, 2), (9, 7)),
    ((3, 1), (1, 13)), ((19, 19), (19, 19)), ((9, 9), (9, 5)),
    ((9, 9), (5, 9)), ((1, 1), (1, 1)), ((1, 1), (9, 7)),
])
def test_proposed_scalar_order_matches_existing_float32_reference_exactly(shape, target):
    pixels = np.random.default_rng(172).integers(0, 256, (*shape, 4), dtype=np.uint8)[:, :, :3]
    height, width = target
    expected = ReferenceCubicResize()(torch.from_numpy(pixels), width, height).numpy()
    actual = _scalar_order(pixels, width, height)
    np.testing.assert_array_equal(actual.view(np.uint32), expected.view(np.uint32))


@pytest.mark.parametrize("width,height", [(0, 1), (1, 0), (True, 2), (2, 1.0), (16385, 1)])
def test_invalid_target_rejected_without_cuda_calls(width, height):
    with pytest.raises(ValueError, match="dimensions"):
        CudaReferenceCubicResize._validate_layout(torch.zeros((3, 5, 3), dtype=torch.uint8), width, height)


@pytest.mark.parametrize("image", [None, torch.zeros(4), torch.zeros((0, 4, 3), dtype=torch.uint8),
    torch.zeros((3, 4, 4), dtype=torch.uint8), torch.zeros((3, 4, 3), dtype=torch.float32)])
def test_invalid_layout_rejected_without_cuda_calls(image):
    with pytest.raises(ValueError, match="three-channel"):
        CudaReferenceCubicResize._validate_layout(image, 3, 5)


def test_cpu_image_is_not_silently_uploaded_by_public_api():
    owner = _owner()
    with pytest.raises(ValueError, match="owner's CUDA device"):
        owner(torch.zeros((3, 4, 3), dtype=torch.uint8), 3, 5)
    assert owner._inflight is None and not owner.faulted


def test_failed_kernel_completion_keeps_every_input_and_blocks_next_call():
    owner = _owner()
    owner.driver = SimpleNamespace(cuLaunchKernel=lambda *args: 0)
    image = torch.zeros((5, 7, 3), dtype=torch.uint8)
    output = torch.empty((3, 4, 3))
    coefficients = tuple(torch.empty((4, 4)) for _ in range(4))
    def fail():
        raise RuntimeError("injected GPU completion failure")
    stream = SimpleNamespace(cuda_stream=11, synchronize=fail)
    with pytest.raises(RuntimeError, match="completion failure"):
        owner._execute(image, output, coefficients, stream)
    assert owner.faulted and owner._inflight[0] is stream
    assert owner._inflight[1] is image and owner._inflight[2] is output
    assert all(a is b for a, b in zip(owner._inflight[3:], coefficients))
    with pytest.raises(RuntimeError, match="closed or faulted"):
        owner(image, 4, 3)


def test_launch_error_is_not_success_even_when_completion_succeeds():
    owner = _owner()
    owner.driver = SimpleNamespace(cuLaunchKernel=lambda *args: 999)
    completed = []
    stream = SimpleNamespace(cuda_stream=11, synchronize=lambda: completed.append(True))
    with pytest.raises(RuntimeError, match="code 999"):
        owner._execute(torch.zeros((2, 3, 3), dtype=torch.uint8), torch.empty((2, 3, 3)),
                       (None, None, None, None), stream)
    assert completed == [True] and owner.faulted and owner._inflight is None


def test_close_failure_preserves_module_and_coefficients_until_real_completion(monkeypatch):
    owner = _owner()
    owner._inflight = ("retained source", "retained result")
    retained = owner._inflight
    owner._reference.cache["owned coefficients"] = object()
    unloaded = []
    owner.driver = SimpleNamespace(cuModuleUnload=lambda module: unloaded.append(module.value) or 0)
    monkeypatch.setattr(torch.cuda, "device", lambda device: nullcontext())
    def fail(device):
        raise RuntimeError("injected close failure")
    monkeypatch.setattr(torch.cuda, "synchronize", fail)
    with pytest.raises(RuntimeError, match="close failure"):
        owner.close()
    assert owner.faulted and not owner.closed and owner._inflight is retained
    assert owner.module.value == 123 and owner._reference.cache and not unloaded
    monkeypatch.setattr(torch.cuda, "synchronize", lambda device: None)
    owner.close()
    assert owner.closed and owner._inflight is None and not owner._reference.cache
    assert owner.module.value is None and unloaded == [123]
    owner.close()
    assert unloaded == [123]


_real_gpu = pytest.mark.skipif(os.environ.get("QUEST3D_RUN_GPU_TESTS") != "1",
                              reason="Opt-in actual CUDA test; no GPU execution by default")


@pytest.mark.gpu
@_real_gpu
def test_real_gpu_exact_parity_edges_strides_axes_and_owned_lifetime():
    reference = ReferenceCubicResize()
    with CudaReferenceCubicResize() as candidate:
        rng = np.random.default_rng(780)
        cases = [((1440, 2560), (280, 504)), ((143, 257), (29, 51)),
                 ((17, 29), (7, 9)), ((7, 9), (19, 13)), ((1, 2), (9, 7)),
                 ((3, 1), (1, 13)), ((19, 19), (19, 19)), ((9, 9), (9, 5)),
                 ((9, 9), (5, 9)), ((1, 1), (1, 1)), ((1, 1), (9, 7))]
        held = None
        saved = None
        for (sh, sw), (height, width) in cases:
            # Nonzero storage offset plus row/column/channel stride, as in a ROI.
            backing = torch.from_numpy(rng.integers(0, 256, (sh + 2, 2 * sw + 3, 6),
                                                     dtype=np.uint8)).cuda()
            source = backing[1:1 + sh, 1:1 + 2 * sw:2, ::2]
            before = source.clone()
            expected = reference(source, width, height)
            actual = candidate(source, width, height)
            assert torch.equal(actual.view(torch.int32), expected.contiguous().view(torch.int32))
            assert actual.dtype == torch.float32 and actual.is_contiguous()
            assert actual.data_ptr() != source.data_ptr() and torch.equal(source, before)
            if held is not None:
                assert torch.equal(held, saved)
            held, saved = actual, actual.clone()
        # Negative ringing must not be silently clamped to byte range.
        edge = torch.zeros((3, 3, 3), device="cuda", dtype=torch.uint8)
        edge[:, 1] = 255
        expected = reference(edge, 31, 9)
        actual = candidate(edge, 31, 9)
        assert bool((expected < 0).any())
        assert torch.equal(actual.view(torch.int32), expected.view(torch.int32))
        # Coefficients are reused across streams only after completed calls.
        stream = torch.cuda.Stream()
        stream.wait_stream(torch.cuda.current_stream())
        with torch.cuda.stream(stream):
            changed_stream = candidate(edge, 31, 9)
        assert torch.equal(changed_stream.view(torch.int32), expected.view(torch.int32))
        assert candidate._inflight is None and len(candidate._reference.cache) <= 16
    assert torch.equal(held, saved)  # Closing never invalidates returned storage.


@pytest.mark.gpu
@_real_gpu
def test_real_gpu_rejects_float_alpha_input_and_oversized_target_without_faulting():
    with CudaReferenceCubicResize() as candidate:
        source = torch.ones((5, 9, 3), dtype=torch.uint8, device="cuda")
        for invalid in (source.cpu(), source.float(), source[:, :, :2], source[0]):
            with pytest.raises(ValueError):
                candidate(invalid, 3, 7)
        with pytest.raises(ValueError, match="dimensions"):
            candidate(source, 16385, 7)
        assert candidate._inflight is None and not candidate.faulted


@pytest.mark.gpu
@_real_gpu
@pytest.mark.skipif(not os.environ.get("QUEST3D_RESAMPLE_BENCHMARK_DIR"),
                    reason="Separate opt-in GPU microbenchmark; set a fresh artifact directory")
def test_real_gpu_component_microbenchmark():
    """Exact pixel input, no model or capture; measures only resize completion."""
    directory = Path(os.environ["QUEST3D_RESAMPLE_BENCHMARK_DIR"])
    directory.mkdir(parents=True, exist_ok=False)
    generator = torch.Generator(device="cuda").manual_seed(7203)
    backing = torch.randint(0, 256, (1440, 2560, 4), dtype=torch.uint8,
                            device="cuda", generator=generator)
    source = backing[:, :, :3]
    before = source.clone()
    reference = ReferenceCubicResize()
    stream = torch.cuda.current_stream()
    records = []
    started = time.perf_counter_ns()
    with CudaReferenceCubicResize() as candidate:
        expected = reference(source, 504, 280)
        actual = candidate(source, 504, 280)
        assert torch.equal(expected.view(torch.int32), actual.view(torch.int32))
        for repetition in range(3):
            for name in (("reference", "candidate") if repetition % 2 == 0 else ("candidate", "reference")):
                resize = candidate if name == "candidate" else reference
                for _ in range(5):
                    resize(source, 504, 280)
                    stream.synchronize()
                rows = []
                for _ in range(50):
                    event_start = torch.cuda.Event(enable_timing=True)
                    event_end = torch.cuda.Event(enable_timing=True)
                    tick = time.perf_counter_ns()
                    event_start.record(stream)
                    result = resize(source, 504, 280)
                    event_end.record(stream)
                    event_end.synchronize()
                    rows.append({"wall_ms": (time.perf_counter_ns() - tick) / 1e6,
                                 "cuda_event_ms": event_start.elapsed_time(event_end)})
                assert torch.equal(result.view(torch.int32), expected.view(torch.int32))
                records.append({"implementation": name, "repetition": repetition, "samples": rows,
                    "timings_ms": {key: dict(zip(("p50", "p95", "max"),
                        map(float, np.percentile([row[key] for row in rows], [50, 95, 100]))))
                        for key in ("wall_ms", "cuda_event_ms")}})
        assert torch.equal(source, before)
        root = Path(__file__).resolve().parents[1]
        summary = {"scope": "actual GPU resize only on fixed synthetic uint8 input; no capture, model, IPC or Quest",
            "source_size": [2560, 1440], "source_stride": list(source.stride()),
            "output_size": [504, 280], "exact_float_bits": True, "source_unchanged": True,
            "candidate": candidate.metadata, "duration_seconds": (time.perf_counter_ns() - started) / 1e9,
            "torch": torch.__version__, "gpu": torch.cuda.get_device_name(),
            "source_sha256": {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                for name in ("src/quest3d/resample.py", "src/quest3d/resample_cuda.py",
                             "src/quest3d/shaders/reference_cubic.cu", "tests/test_resample_cuda.py")},
            "runs": records}
        (directory / "verification.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(json.dumps({"microbenchmark": str(directory), "timings": [
            {"implementation": row["implementation"], "repetition": row["repetition"],
             **row["timings_ms"]} for row in records]}, indent=2))

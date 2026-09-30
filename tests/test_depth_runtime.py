"""CPU lifetime/dispatch/error checks; these do not initialize CUDA.

Actual model parity and timing belong to the separately scheduled GPU probe.
"""

from contextlib import nullcontext

import pytest
import torch

from quest3d.depth import DepthEngine
from quest3d.depth_runtime import (
    DepthModelRuntime, GraphCaptureUnavailable, TensorSignature,
    _TorchDepthGraph, _known_capture_limitation,
)


class CPUReplay:
    def __init__(self, forward):
        self.forward = forward
        self.closed = 0
        self.replays = 0

    def replay(self, image):
        self.replays += 1
        return self.forward(image).clone()

    def close(self):
        self.closed += 1


class Factory:
    def __init__(self):
        self.captured = []

    def __call__(self, forward, image, warmup):
        assert torch.is_inference_mode_enabled()
        graph = CPUReplay(forward)
        self.captured.append((graph, image, warmup))
        return graph


def image(height=4, width=6):
    return torch.arange(3 * height * width, dtype=torch.float32).reshape(1, 3, height, width)


def test_eager_never_prepares_cuda_and_keeps_exact_forward_contract():
    calls = []
    sample = image()

    def forward(value):
        assert value is sample
        assert torch.is_inference_mode_enabled()
        calls.append(value)
        return value + 7

    def forbidden(*_):
        pytest.fail("Eager execution must not create a graph")

    runtime = DepthModelRuntime(forward, capture_factory=forbidden)
    runtime.prepare(sample)
    torch.testing.assert_close(runtime(sample), sample + 7, rtol=0, atol=0)
    assert len(calls) == 1
    assert runtime.status.effective == "eager"
    assert runtime.status.reason is None


def test_graph_is_never_captured_lazily_from_an_incoming_frame():
    factory = Factory()
    runtime = DepthModelRuntime(lambda value: value + 1, mode="cuda-graph", capture_factory=factory)
    for _ in range(3):
        torch.testing.assert_close(runtime(image()), image() + 1)
    assert not factory.captured
    assert runtime.status.reason == "not_prepared"


def test_prepared_graph_uses_each_new_input_and_reports_actual_execution():
    factory = Factory()
    runtime = DepthModelRuntime(lambda value: value.sum(1), mode="cuda-graph", capture_factory=factory)
    runtime.prepare(image())
    result_a = runtime(image())
    result_b = runtime(image() + 11)
    torch.testing.assert_close(result_a, image().sum(1), rtol=0, atol=0)
    torch.testing.assert_close(result_b, (image() + 11).sum(1), rtol=0, atol=0)
    assert factory.captured[0][0].replays == 2
    assert runtime.status.effective == "cuda-graph"
    assert runtime.status.prepared_shape == (1, 3, 4, 6)
    assert runtime.status.reason is None
    assert runtime.status.preparation_ms >= 0


@pytest.mark.parametrize("change", [
    lambda value: value[:, :, :2, :],
    lambda value: value.to(torch.float64),
    lambda value: value.contiguous(memory_format=torch.channels_last),
])
def test_changed_shape_dtype_or_layout_uses_eager_without_recapture(change):
    factory = Factory()
    forwarded = []

    def forward(value):
        forwarded.append(value)
        return value + 1

    runtime = DepthModelRuntime(forward, mode="cuda-graph", capture_factory=factory)
    runtime.prepare(image())
    changed = change(image())
    torch.testing.assert_close(runtime(changed), changed + 1)
    assert forwarded[-1] is changed
    assert runtime.status.reason == "input_signature_changed"
    assert factory.captured[0][0].replays == 0
    assert len(factory.captured) == 1
    runtime(image())
    assert runtime.status.effective == "cuda-graph"
    assert runtime.status.reason is None


def test_device_is_part_of_the_signature_without_initializing_cuda():
    assert TensorSignature.of(image()) != TensorSignature.of(image().to("meta"))


def test_explicit_reprepare_releases_previous_graph_before_allocating_another():
    factory = Factory()
    runtime = DepthModelRuntime(lambda value: value, mode="cuda-graph", capture_factory=factory)
    runtime.prepare(image())
    first = factory.captured[0][0]
    runtime.prepare(image())
    assert len(factory.captured) == 1
    runtime.prepare(image(8, 6))
    assert first.closed == 1
    assert len(factory.captured) == 2
    assert runtime.status.prepared_shape == (1, 3, 8, 6)


def test_known_capture_failure_is_visible_and_not_retried_on_each_frame():
    attempts = []

    def unavailable(*_):
        attempts.append(True)
        raise GraphCaptureUnavailable("unsupported_capture:operation not supported during stream capture")

    runtime = DepthModelRuntime(lambda value: value + 1, mode="cuda-graph", capture_factory=unavailable)
    state = runtime.prepare(image())
    assert state.effective == "eager"
    assert state.reason.startswith("unsupported_capture:")
    assert state.prepared_shape is None
    for _ in range(3):
        torch.testing.assert_close(runtime(image()), image() + 1)
    assert len(attempts) == 1
    assert runtime.status.reason == state.reason


@pytest.mark.parametrize("error", [ValueError("bad model dimensions"),
                                   RuntimeError("CUDA out of memory"),
                                   RuntimeError("CUDA illegal memory access")])
def test_unknown_model_or_driver_failure_is_not_disguised_as_fallback(error):
    def failed(*_):
        raise error

    runtime = DepthModelRuntime(lambda value: value, mode="cuda-graph", capture_factory=failed)
    with pytest.raises(type(error), match=str(error)):
        runtime.prepare(image())
    assert runtime.status.effective == "eager"


def test_eager_model_failure_and_graph_replay_failure_propagate():
    def failed(_):
        raise RuntimeError("model forward failure")

    runtime = DepthModelRuntime(failed, mode="cuda-graph", capture_factory=Factory())
    with pytest.raises(RuntimeError, match="model forward failure"):
        runtime(image())
    runtime.prepare(image())
    with pytest.raises(RuntimeError, match="model forward failure"):
        runtime(image())


def test_close_is_idempotent_and_closed_runtime_rejects_more_work():
    factory = Factory()
    runtime = DepthModelRuntime(lambda value: value, mode="cuda-graph", capture_factory=factory)
    runtime.prepare(image())
    runtime.close()
    runtime.close()
    assert factory.captured[0][0].closed == 1
    assert runtime.status.reason == "closed"
    with pytest.raises(RuntimeError, match="closed"):
        runtime(image())
    with pytest.raises(RuntimeError, match="closed"):
        runtime.prepare(image())


def test_reprepare_or_close_cleanup_failure_is_not_hidden():
    factory = Factory()
    runtime = DepthModelRuntime(lambda value: value, mode="cuda-graph", capture_factory=factory)
    runtime.prepare(image())

    def failed():
        raise RuntimeError("pending CUDA work failed")

    factory.captured[0][0].close = failed
    with pytest.raises(RuntimeError, match="pending CUDA work failed"):
        runtime.prepare(image(8, 6))
    with pytest.raises(RuntimeError, match="pending CUDA work failed"):
        runtime.close()
    assert len(factory.captured) == 1


@pytest.mark.parametrize("message, expected", [
    ("CUDA error: operation not permitted when stream is capturing", True),
    ("operation not supported during stream capture", True),
    ("CUDA out of memory", False),
    ("CUDA error: illegal memory access", False),
    ("mat1 and mat2 shapes cannot be multiplied", False),
    ("custom model RuntimeError", False),
])
def test_only_specific_capture_compatibility_errors_allow_fallback(message, expected):
    assert _known_capture_limitation(RuntimeError(message)) is expected


class Stream:
    def __init__(self, name, events):
        self.name, self.events = name, events

    def wait_stream(self, other):
        self.events.append(("wait", self.name, other.name))

    def synchronize(self):
        self.events.append(("sync", self.name))


def test_real_replay_wrapper_clones_outputs_and_orders_cross_stream_access(monkeypatch):
    events = []
    stream_a, stream_b = Stream("a", events), Stream("b", events)
    selected = [stream_a]
    monkeypatch.setattr(torch.cuda, "current_stream", lambda _: selected[0])
    static_input, static_output = torch.zeros(4), torch.zeros(4)

    class Graph:
        def replay(self):
            static_output.copy_(static_input * 2)
            events.append(("replay",))

        def reset(self):
            events.append(("reset",))

    graph = _TorchDepthGraph(Graph(), static_input, static_output, stream_a)
    previous = graph.replay(torch.ones(4))
    selected[0] = stream_b
    current = graph.replay(torch.full((4,), 3.))
    torch.testing.assert_close(previous, torch.full((4,), 2.), rtol=0, atol=0)
    torch.testing.assert_close(current, torch.full((4,), 6.), rtol=0, atol=0)
    assert previous.data_ptr() != static_output.data_ptr() != current.data_ptr()
    assert events == [("replay",), ("wait", "b", "a"), ("replay",)]
    graph.close()
    graph.close()
    assert events[-2:] == [("sync", "b"), ("reset",)]
    assert graph.static_input is graph.static_output is None


def test_depth_engine_preparation_uses_real_preprocess_layout_without_gpu(monkeypatch):
    engine = object.__new__(DepthEngine)
    engine.mean = torch.zeros((3, 1, 1))  # CPU allocation for this isolated test
    engine.runtime = DepthModelRuntime(lambda value: value, mode="cuda-graph", capture_factory=Factory())
    samples = []

    def prepare(source):
        samples.append(source)
        return image().contiguous(memory_format=torch.channels_last)

    engine.prepare = prepare
    state = engine.prepare_execution(32, 18)
    assert tuple(samples[0].shape) == (18, 32, 4)
    assert samples[0].dtype == torch.uint8
    assert state["effective"] == "cuda-graph"
    assert engine.execution_status() == state
    engine.runtime(image().contiguous(memory_format=torch.channels_last))
    assert engine.execution_status()["reason"] is None
    engine.close()


def test_engine_eager_preparation_does_not_allocate_a_synthetic_frame(monkeypatch):
    engine = object.__new__(DepthEngine)
    engine.runtime = DepthModelRuntime(lambda value: value)
    monkeypatch.setattr(torch, "zeros", lambda *_a, **_kw: pytest.fail("unexpected allocation"))
    assert engine.prepare_execution(32, 18)["effective"] == "eager"


def test_engine_forward_keeps_existing_fp16_autocast_and_float_output(monkeypatch):
    engine = object.__new__(DepthEngine)
    engine.fp16 = True
    calls = []

    def autocast(device, **kwargs):
        calls.append((device, kwargs))
        return nullcontext()

    monkeypatch.setattr(torch, "autocast", autocast)
    engine.model = lambda value: value.to(torch.float16)
    output = engine._forward_model(image())
    assert output.dtype == torch.float32
    torch.testing.assert_close(output, image(), rtol=0, atol=0)
    assert calls == [("cuda", {"dtype": torch.float16, "enabled": True})]


def test_engine_infer_preserves_frame_identity_and_reports_runtime_dispatch(monkeypatch):
    engine = object.__new__(DepthEngine)
    engine.prepare = lambda _: image()
    engine.runtime = DepthModelRuntime(lambda value: value.sum(1), mode="cuda-graph", capture_factory=Factory())
    engine.runtime.prepare(image())

    class Event:
        def record(self): pass
        def synchronize(self): pass
        def elapsed_time(self, _): return 4.5

    engine.start, engine.end = Event(), Event()
    waited_devices = []

    def current_stream(device):
        waited_devices.append(device)
        return Event()

    monkeypatch.setattr(torch.cuda, "current_stream", current_stream)
    monkeypatch.setattr(torch.cuda, "synchronize", lambda: pytest.fail("No global device synchronization"))
    result = engine.infer(None, frame_id=32, generation=4)
    assert (result.frame_id, result.generation, result.input_shape) == (32, 4, (4, 6))
    assert result.inference_ms == 4.5
    assert result.execution_mode == "cuda-graph"
    assert result.execution_reason is None
    assert waited_devices == [torch.device("cpu")]


@pytest.mark.parametrize("kwargs", [{"mode": "unknown"}, {"warmup_iterations": 0}, {"warmup_iterations": 11}])
def test_invalid_options_rejected_before_any_cuda_use(kwargs):
    with pytest.raises(ValueError):
        DepthModelRuntime(lambda value: value, **kwargs)

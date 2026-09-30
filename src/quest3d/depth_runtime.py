"""Optional CUDA graph execution of the unchanged depth forward pass.

Preparation is explicit: call ``prepare`` only while no other thread in this
process submits CUDA work. Never capture lazily from a live frame. The runtime
keeps one shape/layout, falls back eagerly for other inputs, and returns owned
outputs so the following replay cannot overwrite an earlier DepthResult.

API reference pinned to the installed torch 2.7.1 implementation:
https://docs.pytorch.org/docs/2.7/notes/cuda.html#cuda-graphs
https://github.com/pytorch/pytorch/blob/v2.7.1/torch/cuda/graphs.py
"""

from dataclasses import asdict, dataclass
import threading
import time
from typing import Callable, Protocol

import torch


_CAPTURE_LOCK = threading.Lock()


class GraphCaptureUnavailable(RuntimeError):
    """A recoverable graph limitation, not an arbitrary model/driver error."""


@dataclass(frozen=True)
class TensorSignature:
    shape: tuple[int, ...]
    stride: tuple[int, ...]
    dtype: torch.dtype
    device: torch.device

    @classmethod
    def of(cls, image: torch.Tensor):
        return cls(tuple(image.shape), tuple(image.stride()), image.dtype, image.device)


@dataclass(frozen=True)
class DepthExecutionStatus:
    requested: str
    effective: str = "eager"
    reason: str | None = None
    prepared_shape: tuple[int, ...] | None = None
    preparation_ms: float | None = None

    def to_dict(self):
        return asdict(self)


class _Replay(Protocol):
    def replay(self, image: torch.Tensor) -> torch.Tensor: ...
    def close(self) -> None: ...


def _known_capture_limitation(error: RuntimeError) -> bool:
    # Do not turn OOM, illegal accesses, bad model shapes or arbitrary Python
    # RuntimeErrors into an apparently healthy eager fallback.
    message = str(error).lower()
    return any(marker in message for marker in (
        "operation not permitted when stream is capturing",
        "operation not supported during stream capture",
        "cudaerrorstreamcaptureunsupported",
    ))


class _TorchDepthGraph:
    def __init__(self, graph, static_input, static_output, stream):
        self.graph = graph
        self.static_input = static_input
        self.static_output = static_output
        self.last_stream = stream
        self.device = static_input.device

    def replay(self, image):
        current = torch.cuda.current_stream(self.device)
        if current != self.last_stream:
            current.wait_stream(self.last_stream)
        # Both copy and clone are part of the measured execution cost. Never
        # expose static_output: every replay writes that exact storage again.
        self.static_input.copy_(image)
        self.graph.replay()
        output = self.static_output.clone()
        self.last_stream = current
        return output

    def close(self):
        if self.graph is None:
            return
        # The last output clone must complete before its source pool is freed.
        self.last_stream.synchronize()
        self.graph.reset()
        self.graph = None
        self.static_input = self.static_output = self.last_stream = None


def _capture_depth_graph(forward: Callable, example: torch.Tensor,
                         warmup_iterations: int) -> _Replay:
    if not example.is_cuda:
        raise GraphCaptureUnavailable("cuda_input_required")
    if torch.__version__ != "2.7.1+cu126":
        raise GraphCaptureUnavailable(f"unvalidated_torch_version:{torch.__version__}")
    if example.requires_grad:
        raise ValueError("Depth graph input must not require gradients")

    with _CAPTURE_LOCK, torch.cuda.device(example.device):
        static_input = example.clone()
        if static_input.stride() != example.stride():
            raise GraphCaptureUnavailable("unsupported_input_layout")
        stream = torch.cuda.Stream(device=example.device)
        stream.wait_stream(torch.cuda.current_stream(example.device))
        # Warmup/model failures deliberately escape before capture recovery.
        with torch.cuda.stream(stream):
            for _ in range(warmup_iterations):
                reference = forward(static_input)
        stream.synchronize()
        if not isinstance(reference, torch.Tensor):
            raise TypeError("Depth forward must return a tensor")
        if not bool(torch.isfinite(reference).all()):
            raise RuntimeError("Depth model produced non-finite warmup output")

        graph = torch.cuda.CUDAGraph()
        try:
            # Explicit outer stream scope restores the caller stream even if
            # capture_end fails (torch 2.7.1 graph.__exit__ does not use finally).
            with torch.cuda.stream(stream):
                graph.capture_begin(capture_error_mode="global")
                try:
                    static_output = forward(static_input)
                except BaseException as model_error:
                    try:
                        graph.capture_end()
                    except RuntimeError as end_error:
                        model_error.add_note(f"Capture termination: {end_error}")
                    raise
                graph.capture_end()
        except BaseException as error:
            # Recovery is allowed only if capture actually ended and the CUDA
            # context remains healthy. Cleanup errors propagate, too.
            graph.reset()
            stream.synchronize()
            if isinstance(error, RuntimeError) and _known_capture_limitation(error):
                raise GraphCaptureUnavailable(f"unsupported_capture:{error}") from error
            raise

        captured = _TorchDepthGraph(graph, static_input, static_output, stream)
        try:
            observed = captured.replay(example)
            # Zero tolerance for a changed result. This startup sample is only
            # a guard; representative-image GPU parity is a separate test.
            if not torch.equal(reference, observed):
                raise GraphCaptureUnavailable("capture_output_mismatch")
        except BaseException:
            captured.close()
            raise
        return captured


class DepthModelRuntime:
    """Single inference-only callable with explicit startup graph preparation.

    ``forward`` must preserve the model's eager operations/autocast policy and
    must not mutate parameters. A training model is outside this contract.
    ``capture_factory`` is injectable for CPU lifetime/failure tests; production
    uses the pinned CUDA implementation above.
    """

    def __init__(self, forward: Callable, *, mode: str = "eager",
                 warmup_iterations: int = 3, capture_factory=None):
        if mode not in ("eager", "cuda-graph"):
            raise ValueError("Depth execution mode must be eager or cuda-graph")
        if not 1 <= warmup_iterations <= 10:
            raise ValueError("Graph warmup iterations must be in [1, 10]")
        self.forward = forward
        self.mode = mode
        self.warmup_iterations = warmup_iterations
        self._capture_factory = capture_factory or _capture_depth_graph
        self._graph = None
        self._signature = None
        self._closed = False
        self._lock = threading.Lock()
        self._base_reason = "not_prepared" if mode == "cuda-graph" else None
        self._preparation_ms = None
        self._status = DepthExecutionStatus(mode, reason=self._base_reason)

    @property
    def status(self) -> DepthExecutionStatus:
        return self._status

    def _set_status(self, effective="eager", reason=None):
        self._status = DepthExecutionStatus(
            self.mode, effective, reason,
            self._signature.shape if self._signature else None,
            self._preparation_ms,
        )

    @torch.inference_mode()
    def prepare(self, example: torch.Tensor) -> DepthExecutionStatus:
        """Prepare one layout while all other CUDA producers are stopped.

        Re-preparing explicitly releases the old pool first, bounding memory.
        Unsupported capture falls back once; model/driver errors propagate.
        """
        with self._lock:
            if self._closed:
                raise RuntimeError("Depth runtime is closed")
            if self.mode == "eager":
                return self.status
            signature = TensorSignature.of(example)
            if self._graph is not None and signature == self._signature:
                self._set_status("cuda-graph")
                return self.status
            if self._graph is not None:
                self._graph.close()
            self._graph = self._signature = None
            self._base_reason = "not_prepared"
            self._set_status(reason=self._base_reason)
            started = time.perf_counter_ns()
            try:
                self._graph = self._capture_factory(self.forward, example, self.warmup_iterations)
            except GraphCaptureUnavailable as error:
                self._base_reason = str(error)
            finally:
                self._preparation_ms = (time.perf_counter_ns() - started) / 1e6
            if self._graph is not None:
                self._signature = signature
                self._base_reason = None
                self._set_status("cuda-graph")
            else:
                self._set_status(reason=self._base_reason)
            return self.status

    @torch.inference_mode()
    def __call__(self, image: torch.Tensor) -> torch.Tensor:
        with self._lock:
            if self._closed:
                raise RuntimeError("Depth runtime is closed")
            if self._graph is None:
                self._set_status(reason=self._base_reason)
                return self.forward(image)
            if TensorSignature.of(image) != self._signature:
                self._set_status(reason="input_signature_changed")
                return self.forward(image)
            self._set_status("cuda-graph")
            # A replay failure is not recoverable: never silently return eager
            # results after an unknown CUDA execution failure.
            return self._graph.replay(image)

    def close(self):
        with self._lock:
            if self._closed:
                return
            if self._graph is not None:
                self._graph.close()
            self._graph = self._signature = None
            self._closed = True
            self._set_status(reason="closed")

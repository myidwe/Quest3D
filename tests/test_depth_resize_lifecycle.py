"""Fused preprocessing keeps the engine's failure and lifetime contract."""
from types import SimpleNamespace

import pytest
import numpy as np
import torch

from quest3d.depth import DepthEngine


@pytest.mark.parametrize('value', ['true', 1, None])
def test_resize_option_rejected_before_cuda_or_model_loading(monkeypatch, value):
    monkeypatch.setattr(torch.cuda, 'is_available', lambda: pytest.fail('CUDA accessed'))
    with pytest.raises(ValueError, match='explicit boolean'):
        DepthEngine(fused_resize=value)


def test_resize_owner_released_after_graph_and_constants():
    events = []
    engine = object.__new__(DepthEngine)
    engine.runtime = SimpleNamespace(close=lambda: events.append('graph'))
    engine.constants = SimpleNamespace(close=lambda: events.append('constants'))
    engine.gpu_resize = SimpleNamespace(close=lambda: events.append('resize'))
    engine.close()
    assert events == ['graph', 'constants', 'resize']


@pytest.mark.parametrize('failed', ['graph', 'constants', 'resize'])
def test_cleanup_error_preserves_later_owners_for_retry(failed):
    events = []
    def close(name):
        events.append(name)
        if name == failed:
            raise RuntimeError(name + ' still in use')
    engine = object.__new__(DepthEngine)
    engine.runtime = SimpleNamespace(close=lambda: close('graph'))
    engine.constants = SimpleNamespace(close=lambda: close('constants'))
    engine.gpu_resize = SimpleNamespace(close=lambda: close('resize'))
    owner = engine.gpu_resize
    with pytest.raises(RuntimeError, match='still in use'):
        engine.close()
    assert events == ['graph', 'constants', 'resize'][:['graph', 'constants', 'resize'].index(failed) + 1]
    assert engine.gpu_resize is owner
    failed = None
    engine.close()
    assert events[-3:] == ['graph', 'constants', 'resize']


def test_fused_resize_is_reported_with_real_owner_metadata():
    engine = object.__new__(DepthEngine)
    engine.runtime = SimpleNamespace(status=SimpleNamespace(to_dict=lambda: {'effective': 'cuda-graph'}))
    engine.gpu_resize = SimpleNamespace(metadata={'fmad': False, 'source_sha256': 'sample'})
    assert engine.execution_status()['gpu_resize'] == {
        'implementation': 'fused-reference-cubic', 'fmad': False, 'source_sha256': 'sample'}


def test_model_numpy_integer_dimensions_reach_native_resize_as_python_integers(monkeypatch):
    # Only simulate CUDA metadata: all arithmetic stays on CPU in this test.
    monkeypatch.setattr(torch.Tensor, 'is_cuda', property(lambda _: True))
    engine = object.__new__(DepthEngine)
    engine.resize = SimpleNamespace(get_size=lambda width, height: (np.int64(14), np.int64(28)))
    calls = []
    def resize(source, width, height):
        assert type(width) is int and type(height) is int
        calls.append((width, height))
        return torch.zeros((height, width, 3), dtype=torch.float32)
    engine.gpu_resize = resize
    engine.mean = torch.zeros((3, 1, 1))
    engine.std = torch.ones((3, 1, 1))
    result = engine.prepare(torch.zeros((72, 128, 4), dtype=torch.uint8))
    assert calls == [(14, 28)] and result.shape == (1, 3, 28, 14)

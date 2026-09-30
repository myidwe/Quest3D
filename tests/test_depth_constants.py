"""Frozen-model constant ownership/invalidation, with no CUDA initialization."""
from types import SimpleNamespace
import weakref

import pytest
import torch

from quest3d.depth import DepthEngine
from quest3d.depth_constants import FrozenDepthConstants


class Backbone(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.pos_embed = torch.nn.Parameter(torch.arange(20, dtype=torch.float32).reshape(1, 5, 4))
        self.patch_size, self.interpolate_offset, self.interpolate_antialias = 2, .1, False
        self.calls = 0

    def interpolate_pos_encoding(self, image, width, height):
        self.calls += 1
        # A stand-in for the input-independent official interpolation. Tests
        # check that this very result is reused, not a substitute calculation.
        return (self.pos_embed[:, :1].expand(1, image.shape[1], 4) + width + height).to(image.dtype)


class Model(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.pretrained = Backbone()
        self.linear = torch.nn.Linear(4, 4)
        self.conv = torch.nn.Conv2d(3, 4, 1)
        self.norm = torch.nn.LayerNorm(4)
        self.transpose = torch.nn.ConvTranspose2d(3, 4, 1)
        self.dropout = torch.nn.Dropout(0)


def model():
    return Model().eval()


def test_only_original_autocast_eligible_operands_are_converted_and_restored():
    original = model()
    parameters = dict(original.named_parameters())
    constants = FrozenDepthConstants(original)
    converted_refs = [weakref.ref(record[4]) for record in constants._original_parameters]
    assert constants.converted_modules == 2
    for name, parameter in original.named_parameters():
        if name.startswith(('linear.', 'conv.')):
            assert parameter.dtype == torch.float16 and not parameter.requires_grad
            assert torch.equal(parameter, parameters[name].detach().to(torch.float16))
        else:
            assert parameter is parameters[name] and parameter.dtype == torch.float32
    with torch.inference_mode():
        constants.validate()
    constants.close()
    constants.close()
    assert all(value is parameters[name] for name, value in original.named_parameters())
    assert 'interpolate_pos_encoding' not in original.pretrained.__dict__
    assert constants._original_parameters == []
    assert constants._modules == constants._parameters == ()
    # The loop variable above retains its last (unconverted) parameter only.
    assert all(reference() is None for reference in converted_refs)


def test_position_cache_retains_first_layout_without_evicting_graph_storage():
    original = model()
    constants = FrozenDepthConstants(original)
    a = torch.empty(1, 6, 4)
    b = torch.empty(1, 7, 4)
    with torch.inference_mode():
        first = original.pretrained.interpolate_pos_encoding(a, 10, 12)
        snapshot = first.clone()
        same = original.pretrained.interpolate_pos_encoding(a + 5, 10, 12)
        assert same is first
        alternate = original.pretrained.interpolate_pos_encoding(b, 10, 12)
        another = original.pretrained.interpolate_pos_encoding(b, 10, 12)
        assert alternate is not another
        again = original.pretrained.interpolate_pos_encoding(a, 10, 12)
        assert again is first and torch.equal(first, snapshot)
        assert original.pretrained.calls == 3
        assert constants.status()['position_uncached_layouts'] == 2
    constants.close()


def test_position_cache_key_includes_shape_dtype_and_physical_dimensions():
    original = model()
    constants = FrozenDepthConstants(original)
    with torch.inference_mode():
        image = torch.empty(1, 6, 4)
        first = original.pretrained.interpolate_pos_encoding(image, 10, 12)
        changed_dtype = original.pretrained.interpolate_pos_encoding(image.half(), 10, 12)
        changed_width = original.pretrained.interpolate_pos_encoding(image, 11, 12)
        assert changed_dtype.dtype == torch.float16 and changed_dtype is not first
        assert not torch.equal(changed_width, first)
        assert original.pretrained.interpolate_pos_encoding(image, 10, 12) is first
    constants.close()


@pytest.mark.parametrize('change', ['value', 'pointer', 'replacement', 'normalization', 'original_operand', 'training', 'child_training', 'option', 'method'])
def test_mutated_model_state_is_refused_before_replaying_frozen_constants(change):
    original = model()
    old_weight = original.linear.weight
    constants = FrozenDepthConstants(original)
    with torch.no_grad():
        if change == 'value':
            original.pretrained.pos_embed.add_(1)
        elif change == 'pointer':
            original.pretrained.pos_embed.data = original.pretrained.pos_embed.detach().clone()
        elif change == 'replacement':
            original.linear.weight = torch.nn.Parameter(original.linear.weight.detach().clone())
        elif change == 'normalization':
            original.norm.weight.add_(1)
        elif change == 'original_operand':
            old_weight.add_(1)
        elif change == 'training':
            original.train()
        elif change == 'child_training':
            original.dropout.train()
        elif change == 'option':
            original.pretrained.interpolate_offset = .2
        else:
            original.pretrained.interpolate_pos_encoding = lambda *_: None
    with torch.inference_mode(), pytest.raises(RuntimeError):
        constants.validate()
    constants.close()


def test_grad_enabled_or_closed_use_is_rejected():
    original = model()
    constants = FrozenDepthConstants(original)
    with pytest.raises(RuntimeError, match='no-grad'):
        constants.validate()
    with pytest.raises(RuntimeError, match='no-grad'):
        original.pretrained.interpolate_pos_encoding(torch.empty(1, 6, 4), 10, 12)
    constants.close()
    with torch.inference_mode(), pytest.raises(RuntimeError, match='closed'):
        constants.validate()


def test_initialization_failure_restores_already_converted_parameters():
    original = model()
    parameters = dict(original.named_parameters())
    original.conv.weight = torch.nn.Parameter(original.conv.weight.half())
    with pytest.raises(ValueError, match='FP32'):
        FrozenDepthConstants(original)
    assert original.linear.weight is parameters['linear.weight']
    assert original.linear.bias is parameters['linear.bias']
    assert 'interpolate_pos_encoding' not in original.pretrained.__dict__


def test_inference_mode_constructor_is_rejected_before_altering_parameters(monkeypatch):
    original = model()
    weight = original.linear.weight
    with torch.inference_mode(), pytest.raises(ValueError, match='outside inference_mode'):
        FrozenDepthConstants(original)
    assert original.linear.weight is weight
    with torch.inference_mode():
        inference_created = model()
    with pytest.raises(ValueError, match='outside inference_mode'):
        FrozenDepthConstants(inference_created)
    monkeypatch.setattr(torch.cuda, 'is_available', lambda: pytest.fail('CUDA must not be initialized'))
    with torch.inference_mode(), pytest.raises(ValueError, match='outside inference_mode'):
        DepthEngine(execution_mode='cuda-graph', reuse_constants=True)


def test_graph_closes_before_constants_and_graph_failure_retains_storage():
    events = []
    engine = object.__new__(DepthEngine)
    engine.runtime = SimpleNamespace(close=lambda: events.append('graph'))
    engine.constants = SimpleNamespace(close=lambda: events.append('constants'))
    engine.close()
    assert events == ['graph', 'constants']

    def failure():
        raise RuntimeError('graph still in flight')
    engine.runtime.close = failure
    events.clear()
    with pytest.raises(RuntimeError, match='in flight'):
        engine.close()
    assert not events


@pytest.mark.parametrize('kwargs', [dict(execution_mode='eager', reuse_constants=True),
                                  dict(execution_mode='cuda-graph', reuse_constants=True, fp16=False),
                                  dict(execution_mode='cuda-graph', reuse_constants='yes')])
def test_incompatible_constant_mode_is_rejected_before_cuda_check(monkeypatch, kwargs):
    monkeypatch.setattr(torch.cuda, 'is_available', lambda: pytest.fail('CUDA must not be initialized'))
    with pytest.raises(ValueError, match='explicit FP16 CUDA graph'):
        DepthEngine(**kwargs)

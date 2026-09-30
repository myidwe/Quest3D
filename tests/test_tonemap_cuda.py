"""Actual compiled sm75 kernel vs the established float32 Torch policy."""
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest
import torch

from quest3d.tonemap import scrgb_to_bgra8
from quest3d.tonemap_cuda import CudaToneMapper

pytestmark = pytest.mark.gpu


@pytest.fixture
def mapper():
    if not torch.cuda.is_available() or torch.cuda.get_device_capability() != (7, 5):
        pytest.skip("This compiled candidate requires the actual sm75 GPU")
    with CudaToneMapper() as kernel:
        yield kernel


def compare(mapper, values, scale=4.8, knee=.75):
    expected = scrgb_to_bgra8(values, sdr_white_scale=scale, knee=knee)
    actual = mapper(values, sdr_white_scale=scale, knee=knee)
    assert actual.is_contiguous() and actual.dtype == torch.uint8
    assert bool((actual[..., 3] == 255).all())
    assert (actual.int() - expected.int()).abs().max().item() <= 1
    return actual


def test_every_fp16_bit_pattern_including_nan_and_inf_has_reference_output(mapper):
    half = np.arange(65536, dtype=np.uint16).view(np.float16)
    raw = torch.from_numpy(half.copy()).reshape(256, 256, 1).expand(-1, -1, 4).cuda()
    compare(mapper, raw)


@pytest.mark.parametrize("scale,knee", [(4.8, .75), (3, .75), (1, .75), (.001, .75), (100, .75), (4.8, .1)])
def test_noncontiguous_roi_channel_strides_match_reference(mapper, scale, knee):
    generator = torch.Generator().manual_seed(9873)
    source = ((torch.rand((83, 139, 8), generator=generator) - .1) * 14).half().cuda()
    compare(mapper, source[3:80:2, 5:138:2, ::2], scale, knee)


def test_result_is_owned_across_next_call_and_module_unload(mapper):
    data = torch.ones((31, 33, 4), device="cuda", dtype=torch.float16)
    original = mapper(data, sdr_white_scale=4.8)
    preserved = original.clone()
    data.zero_()
    second = mapper(data, sdr_white_scale=4.8)
    assert not torch.equal(original, second)
    mapper.close()
    assert torch.equal(original, preserved)
    with pytest.raises(RuntimeError, match="closed"):
        mapper(data, sdr_white_scale=4.8)


def test_separate_calling_threads_use_their_torch_context_and_stream(mapper):
    def run(value):
        stream = torch.cuda.Stream()
        with torch.cuda.stream(stream):
            data = torch.full((97, 133, 4), value, device="cuda", dtype=torch.float16)
            return compare(mapper, data).cpu()
    with ThreadPoolExecutor(max_workers=2) as pool:
        outputs = list(pool.map(run, [.25, 3.5]))
    assert not torch.equal(outputs[0], outputs[1])


@pytest.mark.parametrize("scale", [True, 0, 1e-100, float("nan"), float("inf"), 10 ** 400])
def test_invalid_scale_is_rejected_before_kernel_submission(mapper, scale):
    with pytest.raises(ValueError):
        mapper(torch.zeros((1, 1, 4), device="cuda", dtype=torch.float16), sdr_white_scale=scale)


def test_wrong_dtype_cpu_and_zero_size_are_rejected(mapper):
    for source in [torch.zeros((3, 3, 4)), torch.zeros((3, 3, 4), device="cuda"),
                   torch.zeros((0, 3, 4), device="cuda", dtype=torch.float16)]:
        with pytest.raises(ValueError):
            mapper(source, sdr_white_scale=4.8)

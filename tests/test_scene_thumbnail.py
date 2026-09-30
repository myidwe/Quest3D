"""Exact scene sampling, ownership and opt-in CUDA stream checks.

GPU checks require QUEST3D_RUN_GPU_TESTS=1; ordinary collection does not touch
CUDA. No capture/model process or custom CUDA module is needed by these tests.
"""
import gc
import os

import pytest
import torch
import torch.nn.functional as F

from quest3d.scene_thumbnail import tensor_scene_thumbnail


def _reference(source):
    return F.interpolate(source[:, :, :3].permute(2, 0, 1)[None].float(),
                         size=(18, 32), mode="bilinear", align_corners=False)[0] / 255.0


@pytest.fixture
def cpu_only(monkeypatch):
    previous = torch.get_num_threads()
    torch.set_num_threads(2)
    monkeypatch.setattr(torch.cuda, "_lazy_init", lambda: pytest.fail("CPU check initialized CUDA"))
    yield
    torch.set_num_threads(previous)


def _assert_exact(source):
    before = source.clone()
    expected = _reference(source)
    actual = tensor_scene_thumbnail(source, sparse_midpoint=True)
    assert torch.equal(actual, expected)
    assert actual.shape == (3, 18, 32) and actual.dtype == torch.float32
    assert actual.device == source.device and actual.is_contiguous()
    assert actual.stride() == expected.stride()
    assert actual.untyped_storage().data_ptr() != source.untyped_storage().data_ptr()
    assert torch.equal(source, before)
    return actual


@pytest.mark.parametrize("height,width", [(36, 64), (72, 128), (720, 1280),
    (1080, 1920), (1440, 2560), (2160, 3840), (108, 448), (36, 256), (36, 8192)])
def test_supported_geometry_matches_exact_old_interpolation(cpu_only, height, width):
    generator = torch.Generator().manual_seed(height + width)
    _assert_exact(torch.randint(256, (height, width, 4), dtype=torch.uint8, generator=generator))


@pytest.mark.parametrize("kind", ["crop", "row_column_stride", "transpose", "channel_stride", "broadcast"])
def test_strided_capture_matches_old_interpolation(cpu_only, kind):
    generator = torch.Generator().manual_seed(731)
    source = torch.randint(256, (146, 258, 8), dtype=torch.uint8, generator=generator)
    if kind == "crop":
        source = source[1:73, 2:130, :4]
    elif kind == "row_column_stride":
        source = source[1:145:2, 1:257:2, :4]
    elif kind == "transpose":
        source = source[:128, :72, :4].transpose(0, 1)
    elif kind == "channel_stride":
        source = source[:72, :128, ::2]
    else:
        source = source[:1, :128, :4].expand(72, -1, -1)
    assert source.shape == (72, 128, 4) and not source.is_contiguous()
    _assert_exact(source)


@pytest.mark.parametrize("height,width", [(1, 1), (18, 32), (18, 64), (36, 32),
    (35, 64), (36, 63), (71, 129), (48, 64), (1439, 2560), (36, 8256)])
def test_unsupported_geometry_retains_original_interpolation(cpu_only, monkeypatch, height, width):
    source = torch.randint(256, (height, width, 4), dtype=torch.uint8)
    original = F.interpolate
    calls = []
    def record(*args, **kwargs):
        calls.append(kwargs)
        return original(*args, **kwargs)
    expected = _reference(source)
    monkeypatch.setattr(F, "interpolate", record)
    actual = tensor_scene_thumbnail(source, sparse_midpoint=True)
    assert torch.equal(actual, expected)
    assert calls == [{"size": (18, 32), "mode": "bilinear", "align_corners": False}]


def test_default_is_original_and_non_uint8_never_uses_midpoint_path(cpu_only, monkeypatch):
    source = torch.randint(256, (72, 128, 4), dtype=torch.uint8)
    original = F.interpolate
    calls = []
    def record(*args, **kwargs):
        calls.append(args[0].shape)
        return original(*args, **kwargs)
    monkeypatch.setattr(F, "interpolate", record)
    assert torch.equal(tensor_scene_thumbnail(source), original(
        source[:, :, :3].permute(2, 0, 1)[None].float(), (18, 32),
        mode="bilinear", align_corners=False)[0] / 255)
    float_source = source.float() / 3.1
    tensor_scene_thumbnail(float_source, sparse_midpoint=True)
    assert calls == [torch.Size([1, 3, 72, 128])] * 2


def test_all_uint8_quartet_sums_match_exact_normalization(cpu_only):
    # Every possible sum 0..1020 appears. Splitting into four uint8 values also
    # exercises endpoints and values around every final quarter-code boundary.
    sums = torch.arange(1021)
    quartet = torch.stack([(sums - i * 255).clamp(0, 255) for i in range(4)], -1).to(torch.uint8)
    tiled = quartet.repeat(2, 1)[:18 * 32 * 3].reshape(18, 32, 3, 2, 2)
    source = torch.zeros((36, 64, 4), dtype=torch.uint8)
    source[:, :, :3] = tiled.permute(0, 3, 1, 4, 2).reshape(36, 64, 3)
    _assert_exact(source)


def test_no_source_or_retained_output_alias_alpha_and_autocast(cpu_only):
    source = torch.randint(256, (72, 128, 4), dtype=torch.uint8)
    before = source.clone()
    with torch.autocast("cpu", dtype=torch.bfloat16):
        actual = tensor_scene_thumbnail(source, sparse_midpoint=True)
    expected = _reference(source)
    assert torch.equal(actual, expected)
    changed_alpha = source.clone()
    changed_alpha[:, :, 3] = 0
    assert torch.equal(actual, tensor_scene_thumbnail(changed_alpha, sparse_midpoint=True))
    later = tensor_scene_thumbnail(torch.zeros_like(source), sparse_midpoint=True)
    with torch.inference_mode():
        later.fill_(1)
    assert torch.equal(actual, expected) and torch.equal(source, before)
    with torch.inference_mode():
        actual.zero_()
    assert torch.equal(source, before)


def _threshold_frames(device="cpu"):
    current = torch.zeros((36, 64, 4), dtype=torch.uint8, device=device)
    # A realizable uint8 quartet changes the mean by 1/(4*255*1728).
    # These adjacent quartet sums straddle 0.22 without fabricated float input.
    for extra in (691, 692):
        previous = torch.full_like(current, 56)
        upper_left = previous[::2, ::2, :3].clone()
        upper_left.reshape(-1)[:extra] += 1
        previous[::2, ::2, :3] = upper_left
        yield current, previous, extra == 692


def _assert_thresholds(device="cpu"):
    outcomes = []
    for current, previous, expected_reset in _threshold_frames(device):
        old_mean = float((_reference(current) - _reference(previous)).abs().mean().item())
        new_mean = float((tensor_scene_thumbnail(current, sparse_midpoint=True)
                          - tensor_scene_thumbnail(previous, sparse_midpoint=True)).abs().mean().item())
        assert old_mean == new_mean and (new_mean > 0.22) == expected_reset
        assert abs(new_mean - 0.22) < 1 / (4 * 255 * 18 * 32 * 3)
        outcomes.append(expected_reset)
    assert outcomes == [False, True]


def test_realizable_uint8_scene_means_immediately_straddle_threshold(cpu_only):
    _assert_thresholds()


@pytest.mark.parametrize("source,flag", [(None, True), (torch.zeros(0, 8, 4), True),
    (torch.zeros(8, 8, 3), True), (torch.zeros(8, 8, 4, device="meta"), True),
    (torch.zeros(8, 8, 4), "true")])
def test_invalid_inputs_rejected_without_cuda_initialization(cpu_only, source, flag):
    with pytest.raises(ValueError):
        tensor_scene_thumbnail(source, sparse_midpoint=flag)


_real_gpu = pytest.mark.skipif(os.environ.get("QUEST3D_RUN_GPU_TESTS") != "1",
                               reason="Opt-in local CUDA test; set QUEST3D_RUN_GPU_TESTS=1")


@pytest.mark.gpu
@_real_gpu
def test_gpu_exact_supported_fallback_strides_ownership_and_current_stream():
    assert torch.__version__ == "2.7.1+cu126"
    assert torch.cuda.get_device_capability() == (7, 5)
    _assert_thresholds("cuda")
    for height, width in ((36, 64), (720, 1280), (1080, 1920), (1440, 2560),
                          (2160, 3840), (36, 65), (71, 129), (1, 1)):
        source = torch.randint(256, (height, width, 4), dtype=torch.uint8, device="cuda")
        _assert_exact(source)
    big = torch.randint(256, (146, 258, 8), dtype=torch.uint8, device="cuda")
    for source in (big[1:145:2, 1:257:2, :4], big[:128, :72, :4].transpose(0, 1),
                   big[:72, :128, ::2], big[:1, :128, :4].expand(72, -1, -1)):
        _assert_exact(source)

    stream = torch.cuda.Stream()
    with torch.cuda.stream(stream):
        # Source is produced and consumed on the same non-default stream. The
        # helper adds no global/default-stream synchronization or cached owner.
        source = torch.randint(256, (1440, 2560, 4), dtype=torch.uint8, device="cuda")
        expected = _reference(source)
        actual = tensor_scene_thumbnail(source, sparse_midpoint=True)
        held = actual.clone()
        later = tensor_scene_thumbnail(torch.zeros_like(source), sparse_midpoint=True)
        with torch.inference_mode():
            later.fill_(1)
        del source, later
        gc.collect()
        # Exercise Torch allocator reuse after intermediate/input references go.
        churn = [torch.empty((1440, 2560, 4), dtype=torch.uint8, device="cuda") for _ in range(3)]
        for item in churn:
            item.zero_()
        done = stream.record_event()
    torch.cuda.current_stream().wait_event(done)
    assert torch.equal(actual, expected) and torch.equal(actual, held)

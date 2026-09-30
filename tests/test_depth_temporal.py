"""CPU tests with known depth/motion, not model or wearer quality evidence."""

import pytest
import torch

from quest3d.depth_temporal import STAT_NAMES, TemporalDepthStabilizer


@pytest.fixture(autouse=True)
def _bounded_cpu_threads():
    previous = torch.get_num_threads()
    torch.set_num_threads(2)
    yield
    torch.set_num_threads(previous)


def _frame(value=.4, height=24, width=40, dtype=torch.float32):
    return torch.full((1, height, width), value, dtype=dtype), torch.full((1, 3, height, width), .4, dtype=dtype)


def _apply(stabilizer, depth, guide, frame_id, timestamp_ns=None, generation=0, **kwargs):
    return stabilizer.apply(depth, guide, frame_id=frame_id, generation=generation,
                            timestamp_ns=frame_id*20_000_000 if timestamp_ns is None else timestamp_ns, **kwargs)


def test_first_input_is_exact_independent_and_diagnostics_stay_on_input_device():
    depth, guide = _frame()
    before_d, before_g = depth.clone(), guide.clone()
    result = _apply(TemporalDepthStabilizer(), depth, guide, 1)
    assert torch.equal(result.depth, depth)
    assert result.depth.data_ptr() != depth.data_ptr()
    assert result.depth.dtype == depth.dtype and result.depth.device == depth.device
    assert result.diagnostics.reset_reason == "no_history"
    assert result.diagnostics.history_age_ns is None
    assert result.diagnostics.stat_names == STAT_NAMES
    assert result.diagnostics.stats.shape == (6,)
    assert result.diagnostics.stats.dtype == torch.float32 and result.diagnostics.stats.device == depth.device
    assert torch.count_nonzero(result.diagnostics.stats) == 0
    assert torch.equal(depth, before_d) and torch.equal(guide, before_g)


def test_stationary_known_plane_reduces_noise_variance_without_mean_shift():
    rng = torch.Generator(device="cpu").manual_seed(7341)
    stabilizer = TemporalDepthStabilizer()
    truth, guide = _frame(.45, 32, 48)
    inputs, outputs = [], []
    for frame_id in range(48):
        depth = truth + (torch.rand(truth.shape, generator=rng)-.5)*.012
        source = depth.clone()
        result = _apply(stabilizer, depth, guide, frame_id)
        assert torch.equal(depth, source)
        if frame_id:
            inputs.append(depth-truth)
            outputs.append(result.depth-truth)
    before, after = torch.stack(inputs), torch.stack(outputs)
    # Ground truth is the supplied stationary plane, not the prior output.
    assert after.square().mean() < before.square().mean()*.8
    assert after.var(dim=0).mean() < before.var(dim=0).mean()*.8
    assert abs(float(after.mean())) < .0002


@pytest.mark.parametrize("shallow", [False, True])
def test_similar_colour_moving_thin_structure_has_no_history_trail(shallow):
    stabilizer = TemporalDepthStabilizer()
    old, guide_old = _frame()
    current, guide_current = _frame()
    depth_difference = .02 if shallow else .35
    old[:, :, 17] += depth_difference
    current[:, :, 18] += depth_difference
    # RGB motion is below the nominal change threshold: the depth boundary
    # protection must still reject old positions and the newly revealed area.
    guide_old[:, :, :, 17] += .001
    guide_current[:, :, :, 18] += .001
    _apply(stabilizer, old, guide_old, 1)
    result = _apply(stabilizer, current, guide_current, 2)
    assert torch.equal(result.depth, current)
    assert result.diagnostics.stats[3] > 0  # current AND previous boundary guard


def test_rgb_motion_rejects_small_depth_changes_without_a_depth_edge():
    stabilizer = TemporalDepthStabilizer()
    old, guide_old = _frame()
    current, guide_current = _frame(.405)
    guide_current[:, :, 6:18, 12:28] = .7
    _apply(stabilizer, old, guide_old, 1)
    result = _apply(stabilizer, current, guide_current, 2)
    assert torch.equal(result.depth[:, 4:20, 10:30], current[:, 4:20, 10:30])
    assert result.depth[0, 0, 0] < current[0, 0, 0]  # unaffected stationary region may blend


def test_static_sharp_boundary_and_new_disocclusion_are_not_spatially_blurred():
    stabilizer = TemporalDepthStabilizer()
    old, guide_old = _frame()
    old[:, :, 20:] = .8
    current, guide_current = _frame(.405)
    current[:, :, 20:] = .8
    current[:, 6:18, 18:22] = .2  # newly revealed patch without helpful RGB
    _apply(stabilizer, old, guide_old, 1)
    result = _apply(stabilizer, current, guide_current, 2)
    assert torch.equal(result.depth[:, :, 18:23], current[:, :, 18:23])
    assert torch.equal(result.depth[:, 4:20, 16:24], current[:, 4:20, 16:24])
    assert result.depth[0, 0, 0] < current[0, 0, 0]


def test_scene_cut_rejects_all_old_depth_and_next_history_is_current_unfiltered():
    stabilizer = TemporalDepthStabilizer()
    old, guide = _frame()
    _apply(stabilizer, old, guide, 1)
    current, cut_guide = _frame(.405)
    cut_guide.fill_(.8)
    cut = _apply(stabilizer, current, cut_guide, 2)
    assert torch.equal(cut.depth, current)
    assert cut.diagnostics.stats[-1] == 1
    assert cut.diagnostics.stats[0] == 0
    after = _apply(stabilizer, current, cut_guide, 3)
    assert torch.equal(after.depth, current)
    assert after.diagnostics.stats[-1] == 0


def test_same_rgb_large_depth_change_is_current_not_frozen():
    stabilizer = TemporalDepthStabilizer()
    old, guide = _frame()
    _apply(stabilizer, old, guide, 1)
    current, _ = _frame(.55)
    result = _apply(stabilizer, current, guide, 2)
    assert torch.equal(result.depth, current)
    assert result.diagnostics.stats[4] == 1


def test_same_rgb_genuine_slow_depth_change_lags_at_most_half_one_frame():
    stabilizer = TemporalDepthStabilizer()
    _, guide = _frame()
    for frame_id in range(80):
        current, _ = _frame(.4+frame_id*.002)
        result = _apply(stabilizer, current, guide, frame_id)
        lag = current-result.depth
        assert lag.min() >= -1e-7 and lag.max() <= .001+1e-7
    assert result.depth.min() > .556
    # No feedback from filtered output: one repeated current input catches up
    # exactly instead of accumulating a long tail from older estimated frames.
    caught_up = _apply(stabilizer, current, guide, 80)
    assert torch.equal(caught_up.depth, current)


def test_limitation_true_small_depth_change_without_rgb_cue_is_delayed():
    stabilizer = TemporalDepthStabilizer()
    old, guide = _frame()
    _apply(stabilizer, old, guide, 1)
    truth, _ = _frame(.405)
    result = _apply(stabilizer, truth, guide, 2)
    # The supplied depth is already correct. An invisible small approach of a
    # flat surface is indistinguishable here from static model noise. Preserve
    # this negative control rather than claiming universal quality improvement.
    assert (result.depth-truth).square().mean() > 0
    assert result.depth.min() > old.max()
    assert result.depth.max() < truth.min()
    assert (truth-result.depth).max() <= .0025+1e-7


def test_capture_age_reduces_history_and_expiry_does_not_renew_an_old_timestamp():
    differences = []
    for age in (20_000_000, 100_000_000, 199_000_000, 200_000_000, 1_000_000_000):
        stabilizer = TemporalDepthStabilizer()
        old, guide = _frame()
        _apply(stabilizer, old, guide, 1, timestamp_ns=10)
        current, _ = _frame(.405)
        result = _apply(stabilizer, current, guide, 99, timestamp_ns=10+age)
        assert result.diagnostics.history_age_ns == age
        differences.append(float((current-result.depth).max()))
        if age >= 200_000_000:
            assert torch.equal(result.depth, current)
            assert result.diagnostics.reset_reason == "gap"
    assert differences[0] > differences[1] > differences[2] > differences[3] == differences[4] == 0


def test_duplicate_reverse_id_or_timestamp_error_keeps_last_good_history():
    for frame_id, timestamp in ((1, 30_000_000), (0, 30_000_000), (2, 20_000_000), (2, 19_999_999)):
        stabilizer, control = TemporalDepthStabilizer(), TemporalDepthStabilizer()
        old, guide = _frame()
        for instance in (stabilizer, control):
            _apply(instance, old, guide, 1)
        with pytest.raises(ValueError, match="Duplicate or reversed"):
            _apply(stabilizer, old, guide, frame_id, timestamp_ns=timestamp)
        current, _ = _frame(.405)
        actual, expected = [_apply(instance, current, guide, 2) for instance in (stabilizer, control)]
        assert torch.equal(actual.depth, expected.depth)
        assert actual.diagnostics.previous_frame_id == 1


def test_explicit_reset_accepts_control_resubmission_without_second_blend():
    for method in ("reset_method", "reset_argument"):
        stabilizer = TemporalDepthStabilizer()
        old, guide = _frame()
        _apply(stabilizer, old, guide, 1)
        current, _ = _frame(.405)
        _apply(stabilizer, current, guide, 2)
        if method == "reset_method":
            stabilizer.reset()
        result = _apply(stabilizer, current, guide, 2, reset=(method == "reset_argument"))
        assert torch.equal(result.depth, current)
        assert result.diagnostics.reset_reason == ("manual_reset" if method == "reset_method" else "explicit_reset")


def test_generation_change_clears_history_even_with_new_timeline_and_ids():
    stabilizer = TemporalDepthStabilizer()
    old, guide = _frame()
    _apply(stabilizer, old, guide, 30, generation=8)
    current, _ = _frame(.405)
    result = _apply(stabilizer, current, guide, 0, timestamp_ns=0, generation=9)
    assert torch.equal(result.depth, current)
    assert result.diagnostics.reset_reason == "generation"


@pytest.mark.parametrize("change", ["shape", "depth_dtype", "guide_dtype"])
def test_geometry_or_dtype_change_resets_without_resizing_or_old_conversion(change):
    stabilizer = TemporalDepthStabilizer()
    old, guide = _frame()
    _apply(stabilizer, old, guide, 1)
    current, guide = _frame(.405, height=25 if change == "shape" else 24)
    if change == "depth_dtype":
        current = current.to(torch.float64)
    if change == "guide_dtype":
        guide = guide.to(torch.float64)
    result = _apply(stabilizer, current, guide, 2)
    assert torch.equal(result.depth, current)
    assert result.depth.dtype == current.dtype
    assert result.diagnostics.reset_reason == ("geometry" if change == "shape" else "dtype")


def test_retained_raw_inputs_are_independent_of_caller_and_result_mutation():
    stabilizer, control = TemporalDepthStabilizer(), TemporalDepthStabilizer()
    old, guide = _frame()
    _apply(control, old, guide, 1)
    first = _apply(stabilizer, old, guide, 1)
    old.fill_(.8)
    guide.fill_(.9)
    first.depth.fill_(1.)
    current, guide = _frame(.405)
    expected = _apply(control, current, guide, 2)
    actual = _apply(stabilizer, current, guide, 2)
    assert torch.equal(actual.depth, expected.depth)
    assert torch.equal(actual.diagnostics.stats, expected.diagnostics.stats)


@pytest.mark.parametrize("bad", ["depth_nan", "depth_inf", "guide_nan", "depth_range", "guide_range", "guide_shape"])
def test_invalid_input_even_with_explicit_reset_does_not_mutate_good_history(bad):
    stabilizer, control = TemporalDepthStabilizer(), TemporalDepthStabilizer()
    depth, guide = _frame()
    for instance in (stabilizer, control):
        _apply(instance, depth, guide, 1)
    wrong_d, wrong_g = depth.clone(), guide.clone()
    if bad == "depth_nan": wrong_d[0, 0, 0] = float("nan")
    elif bad == "depth_inf": wrong_d[0, 0, 0] = float("inf")
    elif bad == "guide_nan": wrong_g[0, 0, 0, 0] = float("nan")
    elif bad == "depth_range": wrong_d[0, 0, 0] = -.01
    elif bad == "guide_range": wrong_g[0, 0, 0, 0] = 1.01
    else: wrong_g = wrong_g[:, :, :, :-1]
    with pytest.raises(ValueError):
        _apply(stabilizer, wrong_d, wrong_g, 2, reset=True)
    current, _ = _frame(.405)
    actual, expected = [_apply(instance, current, guide, 2) for instance in (stabilizer, control)]
    assert torch.equal(actual.depth, expected.depth)
    assert actual.diagnostics.previous_frame_id == 1


def test_zero_weight_is_explicit_identity_not_a_quality_claim():
    stabilizer = TemporalDepthStabilizer(max_history_weight=0)
    old, guide = _frame()
    _apply(stabilizer, old, guide, 1)
    current, _ = _frame(.405)
    result = _apply(stabilizer, current, guide, 2)
    assert torch.equal(result.depth, current)
    assert result.diagnostics.stats[0] == result.diagnostics.stats[1] == 0


@pytest.mark.parametrize("kwargs", [{"max_history_weight": .51}, {"max_history_weight": float("nan")},
                                    {"max_history_weight": True}, {"max_gap_ms": 201},
                                    {"max_gap_ms": 0}, {"max_gap_ms": float("inf")}])
def test_unsafe_or_invalid_configuration_is_refused(kwargs):
    with pytest.raises(ValueError):
        TemporalDepthStabilizer(**kwargs)

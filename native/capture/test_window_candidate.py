"""HWND API validation. Real owned-window GPU evidence is verify-window.py."""
import pytest
import wc_cuda

pytestmark = pytest.mark.skipif(getattr(wc_cuda, "QUEST3D_PATCH_VERSION", 0) != 3,
                               reason="requires the separately unpacked quest3 wheel")

@pytest.mark.parametrize("kwargs", [
    {"window_name": "matching title"}, {"window_hwnd": 123},
    {"window_hwnd": 123, "window_identity": (1, 2, 3, "class"), "monitor_index": 1},
    {"window_hwnd": True, "window_identity": (1, 2, 3, "class")},
    {"window_hwnd": -1, "window_identity": (1, 2, 3, "class")},
    {"window_identity": (1, 2, 3, "class")},
    {"window_hwnd": 123, "window_identity": (1, 2, 3)},
    {"window_hwnd": 123, "window_identity": (1, 2, 3, "")},
    {"window_hwnd": 123, "window_identity": (True, 2, 3, "class")},
    {"window_hwnd": 123, "window_identity": (1, 0, 3, "class")},
])
def test_invalid_selection_rejected_before_cuda_initialization(monkeypatch, kwargs):
    monkeypatch.setattr(wc_cuda, "get_luid", lambda *_: pytest.fail("CUDA must not start for an invalid selection"))
    with pytest.raises(ValueError):
        wc_cuda.WindowsCapture(**kwargs)


@pytest.mark.parametrize("kwargs", [
    {"window_title": "matching title"}, {"window_hwnd": 123},
    {"window_hwnd": 123, "window_identity": (1, 2, 3, "class"), "monitor_index": 1},
    {"window_hwnd": -1, "window_identity": (1, 2, 3, "class")},
    {"window_identity": (1, 2, 3, "class")},
])
def test_native_api_cannot_bypass_exact_selection_contract(kwargs):
    with pytest.raises(ValueError):
        wc_cuda._NativeWcCapture(**kwargs)


def test_monitor_construction_does_not_require_hwnd_and_keeps_lease_contract():
    native = wc_cuda._NativeWcCapture(monitor_index=1, color_format="rgba16f")
    assert native.lease_protocol_version == 1
    assert native.window_status() == (False, 0)
    assert native.lease_stats() == (None, 0, 0)

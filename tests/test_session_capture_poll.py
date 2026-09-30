import pytest

from test_session import _SessionHarness, _wait


@pytest.mark.parametrize('mode', ['2d', '3d'])
def test_static_capture_poll_keeps_presentation_and_frame_identity(monkeypatch, tmp_path, mode):
    harness = _SessionHarness(monkeypatch, tmp_path, initial_mode=mode,
                              second_action='static', poll_capture=True)
    try:
        _wait(lambda: len(harness.published) >= 6)
        if mode == '3d':
            assert harness.saw_3d.wait(3)
        assert harness.capture_waits == 1
        assert harness.capture_polls >= 5
        assert len({meta['capture_ns'] for _, meta in harness.published}) == 1
        assert len({meta['generation'] for _, meta in harness.published}) == 1
        if mode == '3d':
            assert harness.workers[0].frames == 1
    finally:
        harness.close()
    assert not harness.errors
    assert harness.publisher_closed

"""Graph preparation must finish before WGC starts submitting CUDA work."""
from contextlib import nullcontext
import threading
from types import SimpleNamespace

import pytest

from quest3d import capture, session
from quest3d.geometry import ScreenRect


@pytest.mark.parametrize('fail_preparation', [False, True])
@pytest.mark.parametrize('reuse_constants', [False, True])
@pytest.mark.parametrize('fused_resize', [False, True])
@pytest.mark.parametrize('ai_size', [280, 322])
def test_capture_never_starts_during_graph_preparation(monkeypatch, tmp_path, fail_preparation, reuse_constants, fused_resize, ai_size):
    preparing, release, capture_entered = threading.Event(), threading.Event(), threading.Event()
    closed, errors = [], []

    class Engine:
        def __init__(self, size, *, execution_mode, **options):
            assert size == ai_size and execution_mode == 'cuda-graph'
            expected = {'reuse_constants': True} if reuse_constants else {}
            if fused_resize:
                expected['fused_resize'] = True
            assert options == expected

        def prepare_execution(self, width, height):
            assert (width, height) == (2560, 1440)
            preparing.set()
            assert release.wait(3)
            if fail_preparation:
                raise RuntimeError('injected graph preparation failure')

        def execution_status(self):
            return {'effective': 'cuda-graph'}

        def close(self):
            closed.append(True)

    class Capture:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            assert release.is_set()
            capture_entered.set()
            raise RuntimeError('capture entry reached after preparation')

        def __exit__(self, *exc):
            pass

    class Publisher:
        skipped, epoch = 0, 1

        def close(self):
            pass

    class Stream:
        def wait_stream(self, other):
            pass

    monkeypatch.setattr(session, 'DepthEngine', Engine)
    monkeypatch.setattr(session, 'GPUDesktopCapture', Capture)
    monkeypatch.setattr(session, 'FramePublisher', Publisher)
    monkeypatch.setattr(session, 'ARTIFACT_DIR', tmp_path/'routing')
    monkeypatch.setattr(capture, 'list_monitors', lambda: [SimpleNamespace(index=1, bounds=ScreenRect(0,0,2560,1440))])
    monkeypatch.setattr(session.torch.cuda, 'Stream', Stream)
    monkeypatch.setattr(session.torch.cuda, 'current_stream', Stream)
    monkeypatch.setattr(session.torch.cuda, 'stream', lambda _: nullcontext())
    args = SimpleNamespace(output=str(tmp_path/'session'), mode='3d', disparity=4,
                           eye_width=320, eye_height=180, ai_size=ai_size, monitor=1,
                           rect=None, seconds=1, fps=30, cpu_threads=1,
                           max_frame_age_ms=200, depth_execution='cuda-graph',
                           reuse_depth_constants=reuse_constants, fused_depth_resize=fused_resize)

    def run():
        try:
            session.serve(args)
        except Exception as error:
            errors.append(str(error))

    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert preparing.wait(3)
        assert not capture_entered.wait(.1)
    finally:
        release.set()
        thread.join(4)
    assert not thread.is_alive()
    assert closed == [True]
    assert capture_entered.is_set() is not fail_preparation
    assert len(errors) == 1
    assert ('injected graph preparation failure' if fail_preparation else 'capture entry reached') in errors[0]
    status = session.read_json(tmp_path/'session/status.json')
    assert status['running'] is False and status['error']
    assert status['ai_size'] == ai_size and status['ai_input_shape'] is None


@pytest.mark.parametrize('close_timeout', [False, True])
def test_stop_during_preparation_never_starts_capture_and_reports_cleanup_outcome(monkeypatch, tmp_path, close_timeout):
    preparing, release, closing = threading.Event(), threading.Event(), threading.Event()
    entered, engines_closed, workers, errors, results, joins = [], [], [], [], [], []
    original_worker = session.LatestAIWorker

    class Engine:
        def __init__(self, size, *, execution_mode):
            pass

        def prepare_execution(self, width, height):
            preparing.set()
            assert release.wait(3)

        def execution_status(self):
            return {'effective': 'cuda-graph'}

        def close(self):
            engines_closed.append(True)

    class Capture:
        def __init__(self, **kwargs):
            entered.append('constructed')

        def __enter__(self):
            entered.append('entered')
            raise AssertionError('Cancelled startup must not open capture')

    class Publisher:
        skipped, epoch = 0, 1

        def close(self):
            pass

    class Stream:
        def wait_stream(self, other):
            pass

    def worker_factory(**kwargs):
        worker = original_worker(**kwargs)
        workers.append(worker)
        original_close, original_join = worker.close, worker.thread.join
        joins.append(original_join)
        if close_timeout:
            # Simulate the OS returning from the full wait with a still-live
            # worker. The timeout/error policy itself remains production code.
            def expired_join(timeout):
                assert timeout == 5
            worker.thread.join = expired_join

        def close():
            closing.set()
            return original_close()
        worker.close = close
        return worker

    monkeypatch.setattr(session, 'DepthEngine', Engine)
    monkeypatch.setattr(session, 'LatestAIWorker', worker_factory)
    monkeypatch.setattr(session, 'GPUDesktopCapture', Capture)
    monkeypatch.setattr(session, 'FramePublisher', Publisher)
    monkeypatch.setattr(session, 'ARTIFACT_DIR', tmp_path/'routing')
    monkeypatch.setattr(capture, 'list_monitors', lambda: [SimpleNamespace(index=1, bounds=ScreenRect(0,0,2560,1440))])
    monkeypatch.setattr(session.torch.cuda, 'Stream', Stream)
    monkeypatch.setattr(session.torch.cuda, 'current_stream', Stream)
    monkeypatch.setattr(session.torch.cuda, 'stream', lambda _: nullcontext())
    args = SimpleNamespace(output=str(tmp_path/'session'), mode='3d', disparity=4,
                           eye_width=320, eye_height=180, ai_size=280, monitor=1,
                           rect=None, seconds=1, fps=30, cpu_threads=1,
                           max_frame_age_ms=200, depth_execution='cuda-graph')

    def run():
        try:
            results.append(session.serve(args))
        except Exception as error:
            errors.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert preparing.wait(3)
        # A foreign request cannot stop this session while the model prepares.
        session.atomic_json(tmp_path/'session/request.json', dict(session_id='foreign',
            request_id='wrong-stop', mode='3d', disparity=4, stop=True))
        assert not closing.wait(.12)
        assert not entered and thread.is_alive()
        from quest3d.session_control import send_control
        request = send_control(tmp_path/'session', stop=True)
        assert closing.wait(1)
        assert not entered and not engines_closed and not workers[0].ready
        if close_timeout:
            thread.join(2)
            assert not thread.is_alive()
            stopped = session.read_json(tmp_path/'session/status.json')
            assert stopped['running'] is False
            assert 'did not exit within 5 seconds' in stopped['error']
            assert workers[0].thread.is_alive()
        else:
            # Merely requesting Stop does not report finished cleanup.
            assert thread.is_alive()
            assert session.read_json(tmp_path/'session/status.json')['running'] is True
        release.set()
        joins[0](3)
        thread.join(3)
        assert not thread.is_alive() and not workers[0].thread.is_alive()
        assert engines_closed == [True] and not entered and not workers[0].ready
        assert not errors and len(results) == 1
        stopped = session.read_json(tmp_path/'session/status.json')
        assert stopped['seen_request'] == request['request_id']
        assert stopped['running'] is False and stopped['ai_ready'] is False
        assert bool(stopped['error']) is close_timeout
    finally:
        release.set()
        for join in joins:
            join(3)
        thread.join(3)

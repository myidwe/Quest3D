"""Carry actual engine tensor size, not screen size, through worker publication."""
import json

import pytest

from quest3d.session import LatestAIWorker
from test_session import _frame, _synthetic_depth, _wait


@pytest.mark.parametrize('metadata', [True, False])
def test_actual_inference_dimensions_and_requested_size_are_distinct(tmp_path, metadata):
    sizes = []
    class Engine:
        def __init__(self, size):
            sizes.append(size)
        def infer(self, bgra, *, frame_id, generation):
            depth = _synthetic_depth(bgra, frame_id, generation)
            if metadata:
                depth.input_shape = (322,574)
            else:
                del depth.input_shape
            return depth
    worker = LatestAIWorker(eye_width=64,eye_height=36,ai_size=322,directory=tmp_path,
                            engine_factory=Engine)
    try:
        worker.submit(_frame(),0,1)
        _wait(lambda: worker.snapshot()[0] is not None or worker.snapshot()[1] is not None)
        processed,error,ready = worker.snapshot()
        assert not error and ready
        assert sizes == [322]
        assert processed.ai_input_shape == ((322,574) if metadata else None)
    finally:
        worker.close()
    row = json.loads((tmp_path/'frames.jsonl').read_text().splitlines()[0])
    assert row['ai_size'] == 322
    assert row['ai_input_shape'] == ([322,574] if metadata else None)
    assert row['inference_source_size'] == [32,18]

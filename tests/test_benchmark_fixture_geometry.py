"""A visible benchmark on another display must never count as captured motion."""
import importlib.util
from pathlib import Path
import pytest

spec=importlib.util.spec_from_file_location('q3d_fixture_geometry_check',Path(__file__).resolve().parents[1]/'scripts/benchmark-live-performance.py')
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.mark.parametrize('bounds', [dict(left=0,top=0,width=2560,height=1440),
    dict(left=-1920,top=-120,width=1920,height=1080)])
def test_exact_physical_monitor_and_visibility_required(bounds):
    expected=[bounds['left'],bounds['top'],bounds['left']+bounds['width'],bounds['top']+bounds['height']]
    correct={'bounds':expected,'minimized':False,'center_uncovered':True}
    assert module.fixture_matches_monitor(correct,{'bounds':bounds})
    for changes in ({'bounds':[2560,0,4720,1440]},{'bounds':[expected[0]+1,*expected[1:]]},
                    {'minimized':True},{'center_uncovered':False},{'center_uncovered':None}):
        assert not module.fixture_matches_monitor({**correct,**changes},{'bounds':bounds})
    assert not module.fixture_matches_monitor({}, {'bounds':bounds})

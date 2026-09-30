"""CPU dispatch/lifetime checks; actual CUDA numerical tests are separate."""
import numpy as np
import pytest
import torch

from quest3d.depth import DepthResult
from quest3d.forward_warp import ForwardWarpResult
from quest3d.stereo import StereoSynthesizer


def test_cuda_selection_is_lazy_and_original_2d_stays_exact(monkeypatch):
    from quest3d import forward_warp_cuda
    def forbidden(*args, **kwargs):
        raise AssertionError('2D must not compile or run the CUDA projector')
    monkeypatch.setattr(forward_warp_cuda, 'synthesize_forward_cuda', forbidden)
    source=np.full((18,32,4),127,np.uint8)
    source[:,:,3]=255
    synth=StereoSynthesizer(32,18,0,stereo_method='forward-cuda')
    depth=DepthResult(7,3,torch.ones((1,5,9)),(5,9),0,0)
    result=synth.synthesize(source,depth,frame_id=7,generation=3)
    np.testing.assert_array_equal(result.bgra[:,:32],source)
    np.testing.assert_array_equal(result.bgra[:,32:],source)
    assert result.mode=='2d'


def test_cuda_dispatch_packs_actual_return_and_closes_selected_resource(monkeypatch):
    from quest3d import forward_warp_cuda
    calls=[]
    def projector(image,depth,disparity,convergence):
        calls.append(('project',tuple(image.shape),depth.dtype,disparity,convergence))
        eyes=image.expand(2,-1,-1,-1).clone()
        eyes[0,0]=1
        eyes[1,2]=1
        empty=torch.zeros((2,18,32),dtype=torch.bool)
        return ForwardWarpResult(eyes,empty,empty)
    monkeypatch.setattr(forward_warp_cuda,'synthesize_forward_cuda',projector)
    monkeypatch.setattr(forward_warp_cuda,'close_cached_forward_warp_cuda',lambda:calls.append(('close',)))
    source=np.zeros((18,32,4),np.uint8);source[:,:,3]=255
    synth=StereoSynthesizer(32,18,1,stereo_method='forward-cuda')
    depth=DepthResult(7,3,torch.arange(45,dtype=torch.float32).reshape(1,5,9),(5,9),0,0)
    result=synth.synthesize(source,depth,frame_id=7,generation=3)
    assert np.all(result.bgra[:,:32,0]==255)
    assert np.all(result.bgra[:,32:,2]==255)
    assert np.all(result.bgra[:,:,3]==255)
    synth.close()
    assert calls==[('project',(1,3,18,32),torch.float32,1,.5),('close',)]


def test_cuda_choice_still_refuses_depth_from_a_different_frame():
    synth=StereoSynthesizer(32,18,1,stereo_method='forward-cuda')
    source=np.zeros((18,32,4),np.uint8)
    depth=DepthResult(8,3,torch.ones((1,5,9)),(5,9),0,0)
    with pytest.raises(ValueError,match='different RGB frame'):
        synth.synthesize(source,depth,frame_id=7,generation=3)

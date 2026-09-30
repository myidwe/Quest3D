"""Candidate ABI/policy/owned-buffer checks; no real desktop or OS input."""
import ctypes as C
from dataclasses import replace
import threading
from types import SimpleNamespace

import numpy as np
import pytest
import torch

from quest3d import dxgi_capture as d
from quest3d.display_color import DisplayColor
from quest3d.capture import MonitorInfo
from quest3d.geometry import ScreenRect, SourceGeometry
from quest3d.source_identity import SourceIdentity, SourceKind


def color(hdr=True):
    return DisplayColor("DISPLAYX", "hdr" if hdr else "sdr", hdr, 8, 3000 if hdr else 1000,
                        240.0 if hdr else 80.0, 3.0 if hdr else 1.0, {})


def configured():
    capture = d.DXGISnapshotCapture(experimental_dxgi=True)
    capture._owner = threading.current_thread()
    capture._handle.value = 1
    capture._source.version, capture._source.size = 1, 112
    capture._source.hmonitor, capture._source.adapter_luid = 42, 7
    capture._source.width, capture._source.height, capture._source.rotation = 16, 12, 1
    for i, char in enumerate("DISPLAYX"):
        capture._source.device_name[i] = ord(char)
    capture._source_bytes = bytes(capture._source)
    capture._capacity, capture._frequency = 16*12*8, 1_000_000_000
    capture._geometry = SourceGeometry(ScreenRect(0, 0, 16, 12), 0)
    capture._identity = SourceIdentity(SourceKind.MONITOR, 0, 123, 42, 0, capture._geometry.bounds)
    capture._color = color()
    capture.color_profile = {"native_format": None}
    return capture


def native_frame(capture, now=1_000_000_000):
    frame = d.NativeFrame()
    frame.version, frame.size, frame.source = 1, 224, capture._source
    frame.format, frame.bytes_per_pixel = 10, 8
    frame.row_bytes, frame.rows, frame.copied_bytes = 128, 12, 1536
    frame.frame_id, frame.qpc_frequency = 1, capture._frequency
    frame.last_present_qpc = frame.acquire_started_qpc = now
    frame.acquire_returned_qpc = frame.map_completed_qpc = frame.copy_completed_qpc = now
    return frame


@pytest.mark.parametrize("kwargs", [dict(monitor=0), dict(monitor=True), dict(monitor=-1), dict(device=True),
                                    dict(device=-1), dict(rect=(0,0,1,1)), dict(experimental_dxgi=1)])
def test_options_reject_ambiguous_values(kwargs):
    with pytest.raises((TypeError, ValueError)):
        d.DXGISnapshotCapture(**kwargs)


def test_explicit_opt_in_before_loading(monkeypatch):
    monkeypatch.setattr(d, "_load_native", lambda: pytest.fail("must not load"))
    with pytest.raises(RuntimeError, match="explicit"):
        d.DXGISnapshotCapture().__enter__()


def test_abi_layout_and_integer_clock_conversion():
    assert (C.sizeof(d.NativeSource), C.sizeof(d.NativeFrame), C.sizeof(d.NativeError)) == (112,224,32)
    assert d.NativeFrame.last_present_qpc.offset == 152
    assert d.NativeFrame.qpc_frequency.offset == 192
    assert d._ticks_to_ns(123456789123, 10_000_000) == 12_345_678_912_300


def test_dll_hash_mismatch_never_loads(monkeypatch, tmp_path):
    path = tmp_path / "wrong.dll"
    path.write_bytes(b"not the verified candidate")
    monkeypatch.setattr(d, "DLL_PATH", path)
    monkeypatch.setattr(d.C, "CDLL", lambda *_: pytest.fail("unverified code must not load"))
    with pytest.raises(RuntimeError, match="SHA256"):
        d._load_native()


def test_qpc_envelope_rejects_wrong_epoch(monkeypatch):
    class API:
        def QueryPerformanceFrequency(self, value): value._obj.value=1_000_000_000;return 1
        def QueryPerformanceCounter(self, value): value._obj.value=5;return 1
    monkeypatch.setattr(d,"_qpc_api",API)
    monkeypatch.setattr(d,"perf_counter_ns",lambda: 1000)
    with pytest.raises(RuntimeError,match="clock envelope"):
        d._verify_qpc_domain()


@pytest.mark.parametrize("changes", [dict(active_mode="unknown"),dict(active_mode="wcg"),
    dict(hdr_enabled=None),dict(sc_rgb_sdr_white_scale=None),dict(query_errors={"sdr_white":1})])
def test_unknown_hdr_metadata_refused(monkeypatch,changes):
    monkeypatch.setattr(d,"read_display_colors",lambda:[replace(color(),**changes)])
    with pytest.raises(RuntimeError):d._color_for("DISPLAYX")


def test_duplicate_profile_refused_and_sdr_policy_explicit(monkeypatch):
    monkeypatch.setattr(d,"read_display_colors",lambda:[color(),color()])
    with pytest.raises(RuntimeError,match="unambiguous"):d._color_for("DISPLAYX")
    monkeypatch.setattr(d,"read_display_colors",lambda:[color(False)])
    assert d._color_for("DISPLAYX").hdr_enabled is False


@pytest.mark.parametrize("field,value", [("version",2),("size",128),("reserved",1),("format",87),
    ("format",999),("row_bytes",127),("rows",11),("copied_bytes",1537),("bytes_per_pixel",4),
    ("qpc_frequency",10_000_000),("frame_id",2),("last_present_qpc",0),
    ("acquire_started_qpc",999_999_999),("copy_completed_qpc",1_000_000_001),
    ("protected_content_masked",1)])
def test_immutable_frame_bounds_format_and_clock_rejection(field,value):
    capture=configured(); frame=native_frame(capture)
    setattr(frame,field,value)
    with pytest.raises(RuntimeError):capture._validate_frame(frame,1_000_000_000,1_000_000_000)


def test_same_current_geometry_cannot_relabel_old_source():
    capture=configured();frame=native_frame(capture);frame.source.hmonitor=43
    with pytest.raises(RuntimeError,match="immutable"):capture._validate_frame(frame,1_000_000_000,1_000_000_000)


def test_verified_sdr_bgra_layout_is_accepted_without_hdr_assumption():
    capture=configured();capture._color=color(False);frame=native_frame(capture)
    frame.format,frame.bytes_per_pixel,frame.row_bytes,frame.copied_bytes=87,4,64,768
    assert capture._validate_frame(frame,1_000_000_000,1_000_000_000)[0]==1_000_000_000


def test_scope_checks_topology_and_color_before_reusing_buffers(monkeypatch):
    capture=configured()
    monitors=[MonitorInfo(0,ScreenRect(0,0,16,12),"virtual-desktop",False),
              MonitorInfo(1,ScreenRect(0,0,16,12),"DISPLAYX",True,42)]
    capture._monitor_info=monitors[1]
    capture._topology=d.DesktopCapture._topology_key(monitors)
    monkeypatch.setattr(d,"_require_physical_pixels",lambda:None)
    monkeypatch.setattr(d,"list_monitors",lambda:monitors)
    monkeypatch.setattr(d,"_color_for",lambda _:color())
    capture._validate_scope()
    monkeypatch.setattr(d,"_color_for",lambda _:replace(color(),sc_rgb_sdr_white_scale=4.8))
    with pytest.raises(RuntimeError,match="color/SDR white changed"):capture._validate_scope()
    monkeypatch.setattr(d,"list_monitors",lambda:monitors[:1])
    with pytest.raises(RuntimeError,match="topology changed"):capture._validate_scope()


class FakeNative:
    def __init__(self,capture):self.capture=capture;self.frame_id=0;self.status=0;self.close_status=0;self.format=10
    def q3d_dxgi_snapshot(self,handle,pointer,capacity,timeout,max_age,frame,_size,error):
        if self.status:return self.status
        self.frame_id+=1
        now=d.perf_counter_ns()
        value=native_frame(self.capture,now);value.frame_id=self.frame_id
        if self.format==10:
            data=np.full((12,16,4),.3*self.frame_id,dtype=np.float16)
        else:
            value.format,value.bytes_per_pixel,value.row_bytes,value.copied_bytes=self.format,4,64,768
            data=np.broadcast_to(np.array([11,22,33,44],dtype=np.uint8),(12,16,4)).copy()
        C.memmove(pointer,data.ctypes.data,data.nbytes)
        C.memmove(frame,C.byref(value),C.sizeof(value))
        return 0
    def q3d_dxgi_close(self,handle,error):return self.close_status


@pytest.fixture
def gpu_candidate(monkeypatch):
    if not torch.cuda.is_available() or torch.cuda.get_device_capability()!=(7,5):
        pytest.skip("Actual pinned sm75 conversion required")
    from quest3d.tonemap_cuda import CudaToneMapper
    capture=configured()
    capture._host_buffer=torch.empty(capture._capacity,dtype=torch.uint8,pin_memory=True)
    capture._upload_start=torch.cuda.Event(enable_timing=True);capture._upload_end=torch.cuda.Event(enable_timing=True)
    capture._tone_mapper=CudaToneMapper()
    capture._dll=FakeNative(capture)
    monkeypatch.setattr(capture,"_validate_scope",lambda:None)
    yield capture
    capture._dll.close_status=0
    capture.close()


@pytest.mark.gpu
def test_real_gpu_output_owned_across_cpu_reuse_and_close(gpu_candidate):
    capture=gpu_candidate
    first=capture.grab(); saved=first.bgra.clone()
    second=capture.grab()
    capture._host_buffer.zero_()
    assert first.frame_id==1 and second.frame_id==2
    assert not torch.equal(first.bgra,second.bgra)
    assert torch.equal(first.bgra,saved)
    assert first.timestamp_kind==d.TIMESTAMP_KIND and first.native_metadata.frame_id==1
    assert first.bgra.is_cuda and first.bgra.is_contiguous()
    assert bool((first.bgra[...,3]==255).all())
    capture.close()
    assert torch.equal(first.bgra,saved)
    with pytest.raises(RuntimeError,match="not open"):capture.grab()


@pytest.mark.gpu
@pytest.mark.parametrize("native_format,expected",[(87,[11,22,33,255]),(88,[11,22,33,255]),(28,[33,22,11,255])])
def test_sdr_channel_alpha_and_non_aligned_crop(gpu_candidate,native_format,expected):
    capture=gpu_candidate;capture._color=color(False);capture._dll.format=native_format
    capture._rect=ScreenRect(1,2,13,7);capture._geometry=SourceGeometry(capture._rect,0)
    result=capture.grab()
    assert result.bgra.shape==(7,13,4) and result.bgra.is_contiguous()
    assert result.bgra[0,0].tolist()==expected
    assert result.source_identity.parent_bounds==ScreenRect(0,0,16,12)


@pytest.mark.gpu
def test_native_timeout_can_retry_without_reusing_pixels(gpu_candidate):
    capture=gpu_candidate;capture._dll.status=5
    with pytest.raises(TimeoutError):capture.grab()
    assert capture._frame_id==0 and capture._failure is None
    capture._dll.status=0
    assert capture.grab().frame_id==1


@pytest.mark.gpu
def test_upload_expiration_consumes_native_sequence_without_returning_old_frame(gpu_candidate,monkeypatch):
    capture=gpu_candidate
    clock=[1_000_000_000]
    monkeypatch.setattr(d,"perf_counter_ns",lambda:clock[0])
    original=capture._upload_convert
    def slow(frame):
        result=original(frame);clock[0]+=500_000_001;return result
    monkeypatch.setattr(capture,"_upload_convert",slow)
    with pytest.raises(TimeoutError,match="expired"):capture.grab()
    assert capture._frame_id==1 and capture._failure is None
    monkeypatch.setattr(capture,"_upload_convert",original)
    assert capture.grab().frame_id==2


@pytest.mark.gpu
def test_source_failure_is_sticky_and_close_error_retains_cleanup_handle(gpu_candidate,monkeypatch):
    capture=gpu_candidate
    def changed():raise RuntimeError("topology changed")
    monkeypatch.setattr(capture,"_validate_scope",changed)
    with pytest.raises(RuntimeError,match="topology changed"):capture.grab()
    monkeypatch.setattr(capture,"_validate_scope",lambda:None)
    with pytest.raises(RuntimeError,match="scope failed"):capture.grab()
    capture._dll.close_status=3
    with pytest.raises(RuntimeError,match="retained"):capture.close()
    assert capture._handle.value==1 and capture._host_buffer is not None


@pytest.mark.parametrize("timeout",[True,0,-1,1.1,float("nan"),float("inf")])
def test_timeout_arguments_do_not_call_native(timeout):
    capture=configured()
    with pytest.raises(ValueError):capture.grab(timeout_seconds=timeout)


def test_no_reopen_after_single_use():
    capture=d.DXGISnapshotCapture(experimental_dxgi=True);capture._used=True
    with pytest.raises(RuntimeError,match="single-use"):capture.__enter__()

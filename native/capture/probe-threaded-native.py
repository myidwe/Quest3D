"""Temporary native-only stop isolation; own UI windows, no adapter/tracker/AI."""
import threading
import time
import ctypes
from ctypes import wintypes as w
import torch
import wc_cuda
from window_fixture import ThreadedOwnedWindowFixture

with ThreadedOwnedWindowFixture() as fixture:
    source = fixture.create()
    identity = fixture.provider.observe(source).identity
    native = wc_cuda._NativeWcCapture(window_hwnd=source, luid=wc_cuda.get_luid(), color_format="rgba16f",
        window_identity=(identity.process_id, identity.process_created_filetime, identity.thread_id, identity.class_name))
    native.start()
    frame = None
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        result = native.get_frame(0, timeout=.05)
        if result:
            frame, number = result
            break
    assert frame is not None
    bridge = wc_cuda._DX11ToPyTorchBridge(frame.texture_ptr, frame.width, frame.height, color_format="rgba16f")
    tensor = bridge.update(torch.cuda.Stream(), 0)
    native.release_frame(number)
    bridge.close()
    print("Native-only threaded-UI source captured", flush=True)
    fixture.close_window(source)
    print("Source destroyed", flush=True)
    deadline = time.monotonic() + 2
    while not native.window_status()[0] and time.monotonic() < deadline:
        fixture.pump(.005)
    print(f"WGC closed observed={native.window_status()[0]}", flush=True)
    done = threading.Event()
    def stop():
        native.stop()
        done.set()
    stopper = threading.Thread(target=stop, daemon=True)
    stopper.start()
    user = fixture.provider.user
    user.PeekMessageW.argtypes, user.PeekMessageW.restype = [ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT, w.UINT], w.BOOL
    user.TranslateMessage.argtypes = [ctypes.POINTER(w.MSG)]
    user.DispatchMessageW.argtypes, user.DispatchMessageW.restype = [ctypes.POINTER(w.MSG)], ctypes.c_ssize_t
    message = w.MSG()
    deadline = time.monotonic() + 5
    while not done.is_set() and time.monotonic() < deadline:
        while user.PeekMessageW(ctypes.byref(message), None, 0, 0, 1):
            user.TranslateMessage(ctypes.byref(message))
            user.DispatchMessageW(ctypes.byref(message))
        done.wait(.002)
    assert done.is_set(), "native stop exceeded 5 seconds with caller message pumping"
    stopper.join()
    print("Native-only threaded-UI stop completed", flush=True)

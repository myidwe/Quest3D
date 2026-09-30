"""Bounded actual WGC shutdown regression; manipulates only its own HWNDs."""
import argparse
from collections import Counter
import faulthandler
import gc
from datetime import datetime
import hashlib
import json
from pathlib import Path
import threading
import time

import torch
import wc_cuda
import psutil
from own_handle_inventory import inventory
from window_fixture import ThreadedOwnedWindowFixture

parser = argparse.ArgumentParser()
parser.add_argument("--repeat", type=int, default=5)
parser.add_argument("--no-cuda", action="store_true")
parser.add_argument("--release-frame-before-stop", action="store_true")
parser.add_argument("--collect-between", action="store_true")
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
args.output.parent.mkdir(parents=True, exist_ok=True)
fault_log = args.output.with_suffix(".fault.txt").open("x", encoding="utf-8")
faulthandler.enable(fault_log)
assert 1 <= args.repeat <= 100
records = []
initial_handles = inventory()
report = dict(actual_owned_window_capture=True, artificial_closed_wait=False,
              close_timeout_seconds=5, cuda_copy=not args.no_cuda, quest_verified=False,
              resource_stability_verified=False, records=records)
try:
    with ThreadedOwnedWindowFixture() as fixture:
        for iteration in range(args.repeat):
            for mode in ("destroy", "resize_destroy", "minimize_destroy", "live_stop", "destroy_during_stop", "lease_destroy"):
                source = fixture.create()
                identity = fixture.provider.observe(source).identity
                native = wc_cuda._NativeWcCapture(window_hwnd=source, luid=wc_cuda.get_luid(),
                    color_format="rgba16f", window_identity=(identity.process_id,
                    identity.process_created_filetime, identity.thread_id, identity.class_name))
                native.start()
                result = None
                deadline = time.monotonic() + 4
                while result is None and time.monotonic() < deadline:
                    result = native.get_frame(0, timeout=.05)
                assert result is not None, "No real source frame"
                frame, number = result
                if not args.no_cuda:
                    bridge = wc_cuda._DX11ToPyTorchBridge(frame.texture_ptr, frame.width, frame.height,
                                                           color_format="rgba16f")
                    tensor = bridge.update(torch.cuda.Stream(), 0)
                    bridge.close()
                    assert tensor.is_cuda and tensor.dtype == torch.float16
                if mode != "lease_destroy":
                    native.release_frame(number)
                if args.release_frame_before_stop:
                    del frame, result
                if mode == "resize_destroy":
                    fixture.move(source, 2780, 240, 600, 400)
                elif mode == "minimize_destroy":
                    fixture.show(source, 6)
                if mode not in ("live_stop", "destroy_during_stop"):
                    fixture.close_window(source)
                # No polling Closed and no grace sleep between destruction and
                # stop: this is the previously failing ordering.
                closed_before_stop = native.window_status()[0]
                started = time.perf_counter()
                errors = []
                def stop():
                    try:
                        native.stop()
                    except BaseException as error:
                        errors.append(repr(error))
                stopper = threading.Thread(target=stop, daemon=True)
                stopper.start()
                if mode == "destroy_during_stop":
                    fixture.close_window(source)
                stopper.join(5)
                elapsed = time.perf_counter() - started
                if args.collect_between:
                    gc.collect()
                record = dict(iteration=iteration, mode=mode, closed_before_stop=closed_before_stop,
                    close_seconds=elapsed, worker_alive=stopper.is_alive(),
                    native_alive=native.is_alive(), closed_after_stop=native.window_status()[0], errors=errors,
                    process_threads=psutil.Process().num_threads(), process_handles=psutil.Process().num_handles(),
                    rss_mib=psutil.Process().memory_info().rss / 1048576,
                    handle_type_counts=dict(Counter(inventory().values())))
                records.append(record)
                args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
                print(json.dumps({key: value for key, value in record.items() if key != "handle_type_counts"}), flush=True)
                assert not stopper.is_alive(), "Native stop exceeded 5 seconds"
                assert not errors and not native.is_alive()
                if mode == "lease_destroy":
                    assert native.lease_stats()[0] == number
                    try:
                        native.start()
                        raise AssertionError("Outstanding lease must prevent restart")
                    except RuntimeError as error:
                        assert "outstanding CUDA frame lease" in str(error)
                    native.release_frame(number)
                assert native.lease_stats()[0] is None
                if mode not in ("live_stop", "destroy_during_stop"):
                    assert native.window_status()[0], "Real Closed event must finish source shutdown"
                    try:
                        native.start()
                        raise AssertionError("Destroyed source must never be reselected")
                    except RuntimeError as error:
                        assert "closed HWND" in str(error)
                elif mode == "live_stop":
                    # Same identity can restart after a normal explicit stop.
                    native.start()
                    result = None
                    deadline = time.monotonic() + 4
                    while result is None and time.monotonic() < deadline:
                        result = native.get_frame(number, timeout=.05)
                    assert result is not None
                    native.release_frame(result[1])
                    fixture.close_window(source)
                    stopper = threading.Thread(target=stop, daemon=True)
                    stopper.start()
                    stopper.join(5)
                    assert not stopper.is_alive() and not errors and not native.is_alive()
        assert fixture.foreground_before == int(fixture.user.GetForegroundWindow() or 0)
    report["passed"] = True
finally:
    final_handles = inventory()
    report["added_handle_types"] = {str(handle): kind for handle, kind in final_handles.items()
                                    if handle not in initial_handles}
    report["verified_at"] = datetime.now().astimezone().isoformat()
    report["native_sha256"] = hashlib.sha256(Path(wc_cuda._wc_cuda.__file__).read_bytes()).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")

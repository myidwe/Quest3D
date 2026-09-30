"""Gracefully stop only explicitly identified project Sunshine processes.

The hidden session-monitor window handles WM_ENDSESSION through normal SIGINT
shutdown. No broadcast or force kill is used. Retained original process handles
bind exact executable/Windows creation FILETIME until HWND posting and actual
exit wait finish; an optional --birth also binds the caller's earlier identity.
"""
import argparse
from contextlib import ExitStack
import ctypes as C
from ctypes import wintypes as W
import json
from pathlib import Path
import sys


STAGED_ROOT = Path(__file__).resolve().parents[1] / "artifacts" / "host"
WINDOW_CLASS = "SunshineSessionMonitorClass"


class Windows:
    def __init__(self):
        if sys.platform != "win32":
            raise RuntimeError("Verified host shutdown requires Windows")
        self.k = C.WinDLL("kernel32", use_last_error=True)
        self.u = C.WinDLL("user32", use_last_error=True)
        self.callback_type = C.WINFUNCTYPE(W.BOOL, W.HWND, C.c_ssize_t)
        declarations = {
            self.k: {"OpenProcess": ([W.DWORD, W.BOOL, W.DWORD], W.HANDLE),
                "CloseHandle": ([W.HANDLE], W.BOOL),
                "QueryFullProcessImageNameW": ([W.HANDLE, W.DWORD, W.LPWSTR, C.POINTER(W.DWORD)], W.BOOL),
                "GetProcessTimes": ([W.HANDLE, C.POINTER(W.FILETIME), C.POINTER(W.FILETIME),
                    C.POINTER(W.FILETIME), C.POINTER(W.FILETIME)], W.BOOL),
                "WaitForSingleObject": ([W.HANDLE, W.DWORD], W.DWORD),
                "GetExitCodeProcess": ([W.HANDLE, C.POINTER(W.DWORD)], W.BOOL)},
            self.u: {"EnumWindows": ([self.callback_type, C.c_ssize_t], W.BOOL),
                "GetWindowThreadProcessId": ([W.HWND, C.POINTER(W.DWORD)], W.DWORD),
                "GetClassNameW": ([W.HWND, W.LPWSTR, C.c_int], C.c_int),
                "PostMessageW": ([W.HWND, W.UINT, C.c_size_t, C.c_ssize_t], W.BOOL)},
        }
        for library, functions in declarations.items():
            for name, (arguments, result) in functions.items():
                function = getattr(library, name)
                function.argtypes, function.restype = arguments, result

    def open_process(self, process_id):
        handle = self.k.OpenProcess(0x1000 | 0x100000, False, process_id)
        if not handle:  # QUERY_LIMITED_INFORMATION | SYNCHRONIZE, without TERMINATE.
            raise C.WinError(C.get_last_error())
        return handle

    def close_handle(self, handle):
        if not self.k.CloseHandle(handle):
            raise C.WinError(C.get_last_error())

    def identity(self, handle):
        size = W.DWORD(32768)
        name = C.create_unicode_buffer(size.value)
        if not self.k.QueryFullProcessImageNameW(handle, 0, name, C.byref(size)):
            raise C.WinError(C.get_last_error())
        created, exited, kernel, user = W.FILETIME(), W.FILETIME(), W.FILETIME(), W.FILETIME()
        if not self.k.GetProcessTimes(handle, C.byref(created), C.byref(exited), C.byref(kernel), C.byref(user)):
            raise C.WinError(C.get_last_error())
        birth = (created.dwHighDateTime << 32) | created.dwLowDateTime
        return Path(name.value).resolve(), birth

    def alive(self, handle):
        result = self.k.WaitForSingleObject(handle, 0)
        if result == 258:
            return True
        if result == 0:
            return False
        raise C.WinError(C.get_last_error())

    def window_identity(self, window):
        process_id = W.DWORD()
        if not self.u.GetWindowThreadProcessId(window, C.byref(process_id)):
            return None
        name = C.create_unicode_buffer(256)
        if not self.u.GetClassNameW(window, name, len(name)):
            return None
        return process_id.value, name.value

    def shutdown_windows(self, process_ids):
        windows = {process_id: [] for process_id in process_ids}
        errors = []
        @self.callback_type
        def inspect(window, _):
            try:
                identity = self.window_identity(window)
                if identity and identity[0] in windows and identity[1] == WINDOW_CLASS:
                    windows[identity[0]].append(window)
                return True
            except BaseException as exc:
                errors.append(exc)
                return False
        ok = self.u.EnumWindows(inspect, 0)
        if errors:
            raise errors[0]
        if not ok:
            raise C.WinError(C.get_last_error())
        if any(len(matches) != 1 for matches in windows.values()):
            raise RuntimeError("A unique verified host shutdown window is missing; no messages were sent")
        return {process_id: matches[0] for process_id, matches in windows.items()}

    def post_shutdown(self, window):
        if not self.u.PostMessageW(window, 0x0016, 1, 0):  # WM_ENDSESSION, no force/broadcast.
            raise C.WinError(C.get_last_error())

    def wait_exit(self, handle, timeout_ms):
        result = self.k.WaitForSingleObject(handle, timeout_ms)
        if result == 258:
            raise TimeoutError("Host did not exit; forced termination was not used")
        if result != 0:
            raise C.WinError(C.get_last_error())
        code = W.DWORD()
        if not self.k.GetExitCodeProcess(handle, C.byref(code)):
            raise C.WinError(C.get_last_error())
        return code.value


def stop_verified_hosts(executable, process_ids, birth=None, *, api=None):
    process_ids = list(process_ids)
    if not process_ids or any(type(pid) is not int or not 0 < pid <= 0xFFFFFFFF for pid in process_ids):
        raise ValueError("Positive Windows process IDs are required")
    if birth is not None and (len(process_ids) != 1 or type(birth) is not int or not 0 < birth < 1 << 64):
        raise ValueError("--birth requires exactly one --pid and a positive uint64 Windows FILETIME")
    expected = Path(executable).resolve(strict=True)
    if not expected.is_relative_to(STAGED_ROOT.resolve()) or expected.name.lower() != "sunshine.exe":
        raise ValueError("Expected a staged project host executable")
    api = api or Windows()
    processes = {}
    with ExitStack() as cleanup:
        for process_id in dict.fromkeys(process_ids):  # Preserve legacy duplicate-PID de-duplication.
            handle = api.open_process(process_id)
            cleanup.callback(api.close_handle, handle)
            actual_executable, actual_birth = api.identity(handle)
            if actual_executable != expected or (birth is not None and actual_birth != birth):
                raise ValueError(f"Process {process_id} executable/creation FILETIME differs from the expected identity")
            if not api.alive(handle):
                raise RuntimeError(f"Original process {process_id} has already exited; no messages were sent")
            processes[process_id] = (handle, actual_birth)
        windows = api.shutdown_windows(processes)
        # Preflight every window, then recheck each immediately before posting.
        def check_target(process_id, handle, captured_birth):
            if not api.alive(handle) or api.identity(handle) != (expected, captured_birth):
                raise RuntimeError(f"Original process {process_id} is no longer the validated live identity")
            if api.window_identity(windows[process_id]) != (process_id, WINDOW_CLASS):
                raise RuntimeError(f"Host {process_id} shutdown HWND changed; no message sent to that HWND")
        for process_id, (handle, captured_birth) in processes.items():
            check_target(process_id, handle, captured_birth)
        for process_id, (handle, captured_birth) in processes.items():
            check_target(process_id, handle, captured_birth)
            api.post_shutdown(windows[process_id])
        stopped = []
        for process_id, (handle, captured_birth) in processes.items():
            code = api.wait_exit(handle, 8000)
            stopped.append({"process_id": process_id, "exit_code": code, "creation_filetime": str(captured_birth)})
    return {"gracefully_stopped": stopped}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--pid", type=int, action="append", required=True)
    parser.add_argument("--birth", type=int, help="Expected creation FILETIME, decimal uint64; single --pid only")
    args = parser.parse_args(argv)
    if args.birth is not None and (len(args.pid) != 1 or not 0 < args.birth < 1 << 64):
        parser.error("--birth requires exactly one --pid and a positive uint64 FILETIME")
    return args


def main():
    args = parse_args()
    print(json.dumps(stop_verified_hosts(args.exe, args.pid, args.birth)))


if __name__ == "__main__":
    main()

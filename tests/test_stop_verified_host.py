"""No process/GUI actions: verified-stop orchestration against retained-handle fakes."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def stop_module(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[1] / "scripts/stop-verified-host.py"
    spec = importlib.util.spec_from_file_location("verified_host_stop_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stage = tmp_path / "artifacts/host"
    stage.mkdir(parents=True)
    monkeypatch.setattr(module, "STAGED_ROOT", stage)
    executable = stage / "sunshine.exe"
    executable.write_bytes(b"not executable: identity test only")
    return module, executable.resolve()


class FakeWindows:
    def __init__(self, executable, ids=(17,)):
        self.current = {pid: (1 << 40) + pid for pid in ids}
        self.states = {handle: {"pid": pid, "exe": executable, "birth": 134335237795705347 + pid, "alive": True}
                       for pid, handle in self.current.items()}
        self.closed, self.events, self.posted = [], [], []
        self.on_windows = lambda: None
        self.bad_window = False
        self.timeout = False
        self.open_failure = None

    def open_process(self, pid):
        if self.open_failure == pid:
            raise PermissionError("access denied")
        handle = self.current[pid]
        self.events.append(("open", handle))
        return handle

    def close_handle(self, handle):
        assert handle not in self.closed
        self.closed.append(handle)
        self.events.append(("close", handle))

    def identity(self, handle):
        assert handle not in self.closed
        state = self.states[handle]
        return state["exe"], state["birth"]

    def alive(self, handle):
        assert handle not in self.closed
        return self.states[handle]["alive"]

    def shutdown_windows(self, ids):
        self.events.append(("windows", tuple(ids)))
        self.on_windows()
        return {pid: pid + 1000 for pid in ids}

    def window_identity(self, window):
        return (999 if self.bad_window else window - 1000), "SunshineSessionMonitorClass"

    def post_shutdown(self, window):
        assert not self.closed
        self.posted.append(window)
        self.events.append(("post", window))

    def wait_exit(self, handle, timeout_ms):
        assert timeout_ms == 8000 and not self.closed
        self.events.append(("wait", handle))
        if self.timeout:
            raise TimeoutError("No forced termination")
        return 0


def test_exact_birth_holds_original_handle_through_post_and_exit(stop_module):
    module, exe = stop_module
    api = FakeWindows(exe)
    handle = api.current[17]
    birth = api.states[handle]["birth"]
    result = module.stop_verified_hosts(exe, [17], birth, api=api)
    assert result == {"gracefully_stopped": [{"process_id": 17, "exit_code": 0, "creation_filetime": str(birth)}]}
    assert api.events[-2:] == [("wait", handle), ("close", handle)]
    assert api.posted == [1017]


def test_caller_stale_birth_rejects_same_path_before_window_enumeration(stop_module):
    module, exe = stop_module
    api = FakeWindows(exe)
    handle = api.current[17]
    with pytest.raises(ValueError, match="FILETIME"):
        module.stop_verified_hosts(exe, [17], api.states[handle]["birth"] - 1, api=api)
    assert not api.posted and api.closed == [handle]
    assert [kind for kind, _ in api.events] == ["open", "close"]


def test_wrong_executable_cannot_receive_shutdown(stop_module):
    module, exe = stop_module
    api = FakeWindows(exe)
    api.states[api.current[17]]["exe"] = exe.with_name("another.exe")
    with pytest.raises(ValueError, match="executable"):
        module.stop_verified_hosts(exe, [17], api=api)
    assert not api.posted and len(api.closed) == 1


def test_pid_replacement_cannot_replace_retained_dead_handle(stop_module):
    module, exe = stop_module
    api = FakeWindows(exe)
    original = api.current[17]
    def replace_pid():
        api.states[original]["alive"] = False
        api.current[17] = original + 500
        api.states[api.current[17]] = {**api.states[original], "alive": True, "birth": 999}
    api.on_windows = replace_pid
    with pytest.raises(RuntimeError, match="validated live identity"):
        module.stop_verified_hosts(exe, [17], api=api)
    assert not api.posted and api.closed == [original]
    assert [entry for entry in api.events if entry[0] == "open"] == [("open", original)]


def test_hwnd_owner_changed_no_message(stop_module):
    module, exe = stop_module
    api = FakeWindows(exe)
    api.bad_window = True
    with pytest.raises(RuntimeError, match="HWND changed"):
        module.stop_verified_hosts(exe, [17], api=api)
    assert not api.posted and len(api.closed) == 1


def test_legacy_multi_pid_and_duplicate_pid_supported(stop_module):
    module, exe = stop_module
    api = FakeWindows(exe, (17, 18))
    result = module.stop_verified_hosts(exe, [17, 18, 17], api=api)
    assert [row["process_id"] for row in result["gracefully_stopped"]] == [17, 18]
    assert api.posted == [1017, 1018]
    assert [kind for kind, _ in api.events][-4:] == ["wait", "wait", "close", "close"]


def test_second_process_open_failure_releases_first_without_post(stop_module):
    module, exe = stop_module
    api = FakeWindows(exe, (17, 18))
    api.open_failure = 18
    with pytest.raises(PermissionError):
        module.stop_verified_hosts(exe, [17, 18], api=api)
    assert api.closed == [api.current[17]] and not api.posted


def test_timeout_releases_handles_without_force_or_success(stop_module):
    module, exe = stop_module
    api = FakeWindows(exe)
    api.timeout = True
    with pytest.raises(TimeoutError):
        module.stop_verified_hosts(exe, [17], api=api)
    assert api.posted == [1017] and api.closed == [api.current[17]]


@pytest.mark.parametrize("extra", [["--pid", "18", "--birth", "1"], ["--birth", "0"],
    ["--birth", "-1"], ["--birth", str(1 << 64)]])
def test_cli_birth_requires_single_pid_and_uint64(stop_module, extra):
    module, exe = stop_module
    with pytest.raises(SystemExit) as failure:
        module.parse_args(["--exe", str(exe), "--pid", "17", *extra])
    assert failure.value.code == 2


def test_cli_legacy_and_exact_large_filetime(stop_module):
    module, exe = stop_module
    legacy = module.parse_args(["--exe", str(exe), "--pid", "17", "--pid", "18"])
    assert legacy.pid == [17, 18] and legacy.birth is None
    exact = module.parse_args(["--exe", str(exe), "--pid", "17", "--birth", "134335237795705347"])
    assert exact.birth == 134335237795705347


def test_native_boundary_uses_full_filetime_and_only_endsession(stop_module):
    module, exe = stop_module
    native = module.Windows.__new__(module.Windows)  # No WinDLLs or real process/windows.
    expected = 134335237795705347
    calls = []
    def image_name(handle, flags, buffer, size):
        assert handle == 1 << 40 and flags == 0
        buffer.value = str(exe)
        size._obj.value = len(str(exe))
        return True
    def times(handle, created, exited, kernel, user):
        created._obj.dwHighDateTime = expected >> 32
        created._obj.dwLowDateTime = expected & 0xFFFFFFFF
        return True
    native.k = SimpleNamespace(QueryFullProcessImageNameW=image_name, GetProcessTimes=times)
    native.u = SimpleNamespace(PostMessageW=lambda *args: calls.append(args) or True)
    assert native.identity(1 << 40) == (exe, expected)
    native.post_shutdown((1 << 40) + 1)
    assert calls == [((1 << 40) + 1, 0x0016, 1, 0)]


@pytest.mark.parametrize("windows", [[], [1017, 2017]])
def test_native_enumerator_requires_one_monitor_window(stop_module, windows):
    module, _ = stop_module
    native = module.Windows.__new__(module.Windows)
    native.callback_type = lambda function: function
    native.window_identity = lambda hwnd: (17, module.WINDOW_CLASS)
    native.u = SimpleNamespace(EnumWindows=lambda cb, lp: all(cb(hwnd, lp) for hwnd in windows))
    with pytest.raises(RuntimeError, match="unique"):
        native.shutdown_windows([17])


def test_native_open_rights_and_wait_use_original_handle(stop_module):
    module, _ = stop_module
    native = module.Windows.__new__(module.Windows)
    calls = []
    handle = (1 << 40) + 3
    def code(actual_handle, pointer):
        assert actual_handle == handle
        pointer._obj.value = 7
        return True
    native.k = SimpleNamespace(OpenProcess=lambda *args: calls.append(("open", *args)) or handle,
        WaitForSingleObject=lambda *args: calls.append(("wait", *args)) or 0, GetExitCodeProcess=code)
    assert native.open_process(17) == handle
    assert native.wait_exit(handle, 8000) == 7
    assert calls == [("open", 0x101000, False, 17), ("wait", handle, 8000)]


def test_native_wait_timeout_is_not_reported_as_exit(stop_module):
    module, _ = stop_module
    native = module.Windows.__new__(module.Windows)
    native.k = SimpleNamespace(WaitForSingleObject=lambda handle, timeout: 258)
    with pytest.raises(TimeoutError, match="forced termination was not used"):
        native.wait_exit(1 << 40, 8000)

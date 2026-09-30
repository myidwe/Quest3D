"""Icon ownership in the real shell loop, with all window/tray calls mocked."""
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from quest3d.desktop_shell import ShellIntegration, _WinApi

pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Win32 ctypes layouts')


def shell_loop(tmp_path, *, present=True, load=True, register=True, create=True):
    events = []
    if present:
        directory = tmp_path/'resources'
        directory.mkdir()
        (directory/'desktop.ico').write_bytes(b'not read by the mocked LoadImageW')
    shell = ShellIntegration(tmp_path, lambda: events.append('show'), lambda: None, lambda: None)
    real = _WinApi()  # Only declarations/structures; no real window or tray call.
    def record(name, result):
        def callback(*args):
            events.append(name)
            return result
        return callback
    user = SimpleNamespace(
        LoadImageW=record('load-file', 101 if load else None),
        LoadIconW=record('load-shared', 201),
        RegisterClassExW=record('register-class', 1 if register else 0),
        CreateWindowExW=record('create-window', 301 if create else None),
        PostMessageW=record('post-close', 1),
        GetMessageW=record('get-message', 0),
        IsWindow=lambda handle: True,
        DestroyWindow=record('destroy-window', 1),
        UnregisterClassW=record('unregister-class', 1),
        DestroyIcon=record('destroy-icon', 1),
    )
    shell._api = SimpleNamespace(k=SimpleNamespace(GetModuleHandleW=lambda value: 1), u=user,
        WNDPROC=real.WNDPROC, WindowClass=real.WindowClass, Message=real.Message)
    shell._add_icon = lambda: events.append('add-tray')
    shell._remove_icon = lambda: events.append('remove-tray')
    shell._closing.set()
    shell._message_loop()
    return shell, events


def test_owned_icon_destroyed_after_window_and_class(tmp_path):
    shell, events = shell_loop(tmp_path)
    assert 'load-shared' not in events
    assert events.index('destroy-window') < events.index('unregister-class') < events.index('destroy-icon')
    assert events.count('destroy-icon') == 1
    assert shell._icon is None and not shell._owns_icon


def test_registration_failure_still_releases_owned_icon(tmp_path):
    shell, events = shell_loop(tmp_path, register=False)
    assert 'create-window' not in events and 'unregister-class' not in events
    assert events.count('destroy-icon') == 1
    assert shell._startup_error


def test_window_creation_failure_unregistrers_then_releases_icon(tmp_path):
    shell, events = shell_loop(tmp_path, create=False)
    assert 'destroy-window' not in events
    assert events.index('unregister-class') < events.index('destroy-icon')
    assert shell._startup_error


@pytest.mark.parametrize('present', [False, True])
def test_missing_or_invalid_file_uses_shared_fallback_without_destroy(tmp_path, present):
    shell, events = shell_loop(tmp_path, present=present, load=False)
    assert 'load-shared' in events and 'destroy-icon' not in events
    assert not shell._owns_icon


def test_actual_windows_icon_load_and_release():
    """Loads only a local ICO handle, with no visible window or tray changes."""
    api = _WinApi()
    path = Path(__file__).resolve().parents[1]/'resources/desktop.ico'
    handle = api.u.LoadImageW(None, str(path), 1, 0, 0, 0x10 | 0x40)
    assert handle
    assert api.u.DestroyIcon(handle)

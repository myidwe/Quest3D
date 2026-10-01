"""Windows single-instance/tray shell; no capture, process killing or Tk calls.

ShellIntegration(root, on_show, on_stop, on_exit).start() returns False only for
an existing workspace owner (notify_existing reports whether show was posted).
True means this instance owns the name and hidden HWND; check tray_available
before hiding any visible UI. Tray registration failure keeps single-instance
ownership, records last_error and requests on_show. Window/startup failures raise
ShellError. Callbacks execute on the owned message thread: supply queue.put
adapters, never direct Tk calls or blocking lifecycle work. close() must be called
outside that thread and returns only after its actual join; failure retains the
mutex handle and owner so callers can retry, never infer retirement from timeout.

The mutex is an exclusive *named-object existence lease* (initial owner FALSE),
not a thread-affine mutex wait. Keeping its original handle prevents a second
instance across pythonw entry-point/redirector processes without PID guessing.

Win32 layouts/protocol: Microsoft NOTIFYICONDATAW, Shell_NotifyIconW and
TrackPopupMenu documentation. A hidden top-level window, not HWND_MESSAGE,
receives Explorer's TaskbarCreated broadcast.
"""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
import hashlib
import logging
import os
from pathlib import Path
import sys
import threading
from .brand import DESKTOP_NAME
import time


class ShellError(RuntimeError):
    pass


class _WinApi:
    def __init__(self):
        if sys.platform != "win32":
            raise ShellError("Windows desktop shell is available only on Windows")
        self.k = C.WinDLL("kernel32", use_last_error=True)
        self.u = C.WinDLL("user32", use_last_error=True)
        self.s = C.WinDLL("shell32", use_last_error=True)
        self.WNDPROC = C.WINFUNCTYPE(C.c_ssize_t, W.HWND, W.UINT, C.c_size_t, C.c_ssize_t)
        callback = self.WNDPROC

        class WindowClass(C.Structure):
            _fields_ = [("cbSize", W.UINT), ("style", W.UINT), ("lpfnWndProc", callback),
                ("cbClsExtra", C.c_int), ("cbWndExtra", C.c_int), ("hInstance", W.HINSTANCE),
                ("hIcon", W.HICON), ("hCursor", W.HANDLE), ("hbrBackground", W.HBRUSH),
                ("lpszMenuName", W.LPCWSTR), ("lpszClassName", W.LPCWSTR), ("hIconSm", W.HICON)]

        class Guid(C.Structure):
            _fields_ = [("data1", W.DWORD), ("data2", W.WORD), ("data3", W.WORD), ("data4", C.c_ubyte * 8)]

        class NotifyIcon(C.Structure):
            _fields_ = [("cbSize", W.DWORD), ("hWnd", W.HWND), ("uID", W.UINT),
                ("uFlags", W.UINT), ("uCallbackMessage", W.UINT), ("hIcon", W.HICON),
                ("szTip", W.WCHAR * 128), ("dwState", W.DWORD), ("dwStateMask", W.DWORD),
                ("szInfo", W.WCHAR * 256), ("uVersion", W.UINT), ("szInfoTitle", W.WCHAR * 64),
                ("dwInfoFlags", W.DWORD), ("guidItem", Guid), ("hBalloonIcon", W.HICON)]

        class Message(C.Structure):
            _fields_ = [("hwnd", W.HWND), ("message", W.UINT), ("wParam", C.c_size_t),
                ("lParam", C.c_ssize_t), ("time", W.DWORD), ("pt", W.POINT), ("lPrivate", W.DWORD)]

        self.WindowClass, self.NotifyIcon, self.Message = WindowClass, NotifyIcon, Message
        # LLP64: handles/return values/LPARAM remain pointer-sized on Python x64.
        if C.sizeof(C.c_void_p) == 8:
            if C.sizeof(NotifyIcon) != 976 or C.sizeof(WindowClass) != 80 or C.sizeof(Message) != 48:
                raise ShellError("Unexpected Win64 shell structure layout")
        declarations = {
            self.k: {"CreateMutexW": ([C.c_void_p, W.BOOL, W.LPCWSTR], W.HANDLE),
                "CloseHandle": ([W.HANDLE], W.BOOL), "GetModuleHandleW": ([W.LPCWSTR], W.HMODULE)},
            self.u: {"RegisterWindowMessageW": ([W.LPCWSTR], W.UINT),
                "FindWindowW": ([W.LPCWSTR, W.LPCWSTR], W.HWND),
                "GetWindowThreadProcessId": ([W.HWND, C.POINTER(W.DWORD)], W.DWORD),
                "AllowSetForegroundWindow": ([W.DWORD], W.BOOL),
                "PostMessageW": ([W.HWND, W.UINT, C.c_size_t, C.c_ssize_t], W.BOOL),
                "RegisterClassExW": ([C.POINTER(WindowClass)], W.ATOM),
                "UnregisterClassW": ([W.LPCWSTR, W.HINSTANCE], W.BOOL),
                "CreateWindowExW": ([W.DWORD, W.LPCWSTR, W.LPCWSTR, W.DWORD,
                    C.c_int, C.c_int, C.c_int, C.c_int, W.HWND, W.HMENU, W.HINSTANCE, C.c_void_p], W.HWND),
                "DestroyWindow": ([W.HWND], W.BOOL), "IsWindow": ([W.HWND], W.BOOL),
                "DefWindowProcW": ([W.HWND, W.UINT, C.c_size_t, C.c_ssize_t], C.c_ssize_t),
                "GetMessageW": ([C.POINTER(Message), W.HWND, W.UINT, W.UINT], W.BOOL),
                "TranslateMessage": ([C.POINTER(Message)], W.BOOL),
                "DispatchMessageW": ([C.POINTER(Message)], C.c_ssize_t),
                "PostQuitMessage": ([C.c_int], None), "LoadIconW": ([W.HINSTANCE, W.LPCWSTR], W.HICON),
                "LoadImageW": ([W.HINSTANCE, W.LPCWSTR, W.UINT, C.c_int, C.c_int, W.UINT], W.HANDLE),
                "DestroyIcon": ([W.HICON], W.BOOL),
                "CreatePopupMenu": ([], W.HMENU), "AppendMenuW": ([W.HMENU, W.UINT, C.c_size_t, W.LPCWSTR], W.BOOL),
                "TrackPopupMenu": ([W.HMENU, W.UINT, C.c_int, C.c_int, C.c_int, W.HWND, C.c_void_p], W.UINT),
                "DestroyMenu": ([W.HMENU], W.BOOL), "GetCursorPos": ([C.POINTER(W.POINT)], W.BOOL),
                "SetForegroundWindow": ([W.HWND], W.BOOL)},
            self.s: {"Shell_NotifyIconW": ([W.DWORD, C.POINTER(NotifyIcon)], W.BOOL)},
        }
        for library, functions in declarations.items():
            for name, (arguments, result) in functions.items():
                function = getattr(library, name)
                function.argtypes, function.restype = arguments, result


class ShellIntegration:
    WM_CLOSE, WM_DESTROY, WM_TRAY = 0x10, 2, 0x8001

    def __init__(self, root: Path, on_show, on_stop, on_exit):
        self.root = Path(root).resolve(strict=True)
        if not self.root.is_dir() or not all(callable(cb) for cb in (on_show, on_stop, on_exit)):
            raise ValueError("Existing root directory and three callable queue adapters are required")
        key = hashlib.sha256(os.path.normcase(str(self.root)).encode("utf-8")).hexdigest()[:32]
        self._class_name = "Quest3DDesktopTray_" + key
        self._mutex_name = "Local\\Quest3DDesktop_" + key
        self._message_name = "Quest3DDesktopShow_" + key
        self._callbacks = {1: on_show, 2: on_stop, 3: on_exit}
        self._api = None
        self._mutex = self._hwnd = None
        self._thread = self._wndproc = None
        self._lock = threading.RLock()
        self._ready, self._closing = threading.Event(), threading.Event()
        self._started = False
        self._tray_available = False
        self._last_error = None
        self._startup_error = None
        self._notified_existing = False
        self._icon_added = False
        self._icon = None
        self._owns_icon = False

    @property
    def owns_mutex(self):
        with self._lock:
            return self._mutex is not None

    @property
    def tray_available(self):
        with self._lock:
            return self._tray_available

    @property
    def last_error(self):
        with self._lock:
            return self._last_error

    @property
    def notified_existing(self):
        return self._notified_existing

    def _error(self, message, *, show=False):
        with self._lock:
            self._last_error = message
        logging.getLogger(__name__).warning(message)
        if show:
            self._invoke(1)

    def _invoke(self, choice):
        try:
            self._callbacks[choice]()
        except BaseException as exc:
            self._error(f"Desktop shell callback failed: {type(exc).__name__}: {exc}")

    def start(self) -> bool:
        with self._lock:
            if self._started:
                raise ShellError("ShellIntegration.start is one-shot")
            self._started = True
            try:
                self._api = api = _WinApi()
            except Exception as exc:
                raise ShellError(f"Windows shell API initialization failed: {exc}") from exc
            self._show_message = api.u.RegisterWindowMessageW(self._message_name)
            self._taskbar_message = api.u.RegisterWindowMessageW("TaskbarCreated")
            if not self._show_message or not self._taskbar_message:
                raise ShellError(f"RegisterWindowMessageW failed: {C.get_last_error()}")
            C.set_last_error(0)
            handle = api.k.CreateMutexW(None, False, self._mutex_name)
            error = C.get_last_error()
            if not handle:
                raise ShellError(f"CreateMutexW failed: {error}")
            if error == 183:  # ERROR_ALREADY_EXISTS; never wait/acquire another owner's lease.
                try:
                    deadline = time.monotonic() + 2
                    while time.monotonic() < deadline:
                        window = api.u.FindWindowW(self._class_name, None)
                        if window:
                            pid = W.DWORD()
                            api.u.GetWindowThreadProcessId(window, C.byref(pid))
                            if pid.value:
                                api.u.AllowSetForegroundWindow(pid.value)
                            if api.u.PostMessageW(window, self._show_message, 0, 0):
                                self._notified_existing = True
                                break
                        time.sleep(.025)
                    if not self._notified_existing:
                        self._error("Existing desktop owner found, but its show message could not be posted")
                finally:
                    api.k.CloseHandle(handle)
                return False
            self._mutex = handle
            self._thread = threading.Thread(target=self._message_loop, name="Quest3D desktop tray", daemon=False)
            try:
                self._thread.start()
            except Exception as exc:
                self._thread = None
                self._release_mutex()
                raise ShellError(f"Desktop shell thread creation failed: {exc}") from exc
        if not self._ready.wait(5):
            self.close()
            raise ShellError("Desktop shell startup timed out")
        if self._startup_error:
            error = self._startup_error
            self.close()
            raise ShellError(error)
        return True

    def _notify_data(self):
        data = self._api.NotifyIcon()
        data.cbSize, data.hWnd, data.uID = C.sizeof(data), self._hwnd, 1
        data.uFlags, data.uCallbackMessage = 1 | 2 | 4 | 0x80, self.WM_TRAY
        data.hIcon, data.szTip, data.uVersion = self._icon, DESKTOP_NAME, 4
        return data

    def _add_icon(self):
        data = self._notify_data()
        self._icon_added = bool(self._api.s.Shell_NotifyIconW(0, C.byref(data)))
        available = self._icon_added and bool(self._api.s.Shell_NotifyIconW(4, C.byref(data)))
        if self._icon_added and not available:
            self._api.s.Shell_NotifyIconW(2, C.byref(data))
            self._icon_added = False
        with self._lock:
            self._tray_available = available
        if not available:
            self._error("Tray icon unavailable; keep or restore the desktop window", show=True)

    def _remove_icon(self):
        if self._icon_added:
            data = self._notify_data()
            self._api.s.Shell_NotifyIconW(2, C.byref(data))
            self._icon_added = False
        with self._lock:
            self._tray_available = False

    def _menu(self):
        api = self._api.u
        menu = api.CreatePopupMenu()
        if not menu:
            self._error("Could not create tray menu", show=True)
            return
        try:
            for choice, label in ((1, "창 열기"), (2, "PC 중지"), (3, "종료")):
                if not api.AppendMenuW(menu, 0, choice, label):
                    raise ShellError("AppendMenuW failed")
            point = W.POINT()
            if not api.GetCursorPos(C.byref(point)):
                raise ShellError("GetCursorPos failed")
            api.SetForegroundWindow(self._hwnd)
            choice = api.TrackPopupMenu(menu, 0x100 | 0x2, point.x, point.y, 0, self._hwnd, None)
            api.PostMessageW(self._hwnd, 0, 0, 0)  # Required focus dismissal pattern after TrackPopupMenu.
            if choice in self._callbacks:
                self._invoke(choice)
        finally:
            api.DestroyMenu(menu)

    def _window_proc(self, window, message, wp, lp):
        try:
            if message == self._show_message:
                self._invoke(1)
                return 0
            if message == self._taskbar_message:
                self._icon_added = False  # Explorer discarded the old registration.
                if not self._closing.is_set():
                    self._add_icon()
                return 0
            if message == self.WM_TRAY:
                event = lp & 0xFFFF  # NOTIFYICON_VERSION_4 packs event/id in LPARAM.
                if event in (0x203, 0x400, 0x401):  # Double-click / NIN_SELECT / keyboard activation.
                    self._invoke(1)
                elif event == 0x7B:  # Version4 WM_CONTEXTMENU, including keyboard context menu.
                    self._menu()
                return 0
            if message == self.WM_CLOSE:
                self._remove_icon()
                if not self._api.u.DestroyWindow(window):
                    self._error("DestroyWindow failed; shell remains owned", show=True)
                return 0
            if message == self.WM_DESTROY:
                self._api.u.PostQuitMessage(0)
                return 0
        except BaseException as exc:
            self._error(f"Tray message failed: {type(exc).__name__}: {exc}", show=True)
            return 0
        return self._api.u.DefWindowProcW(window, message, wp, lp)

    def _message_loop(self):
        api, registered, instance = self._api, False, None
        try:
            instance = api.k.GetModuleHandleW(None)
            if not instance:
                raise ShellError("GetModuleHandleW failed")
            self._wndproc = api.WNDPROC(self._window_proc)  # Retained until HWND destruction and thread join.
            icon_path = self.root/'resources/desktop.ico'
            if icon_path.is_file():
                self._icon = api.u.LoadImageW(None, str(icon_path), 1, 0, 0, 0x10 | 0x40)
                self._owns_icon = bool(self._icon)
            if not self._icon:
                self._icon = api.u.LoadIconW(None, C.cast(C.c_void_p(32512), W.LPCWSTR))
            if not self._icon:
                raise ShellError("LoadIconW(IDI_APPLICATION) failed")
            wc = api.WindowClass()
            wc.cbSize, wc.lpfnWndProc, wc.hInstance = C.sizeof(wc), self._wndproc, instance
            wc.lpszClassName, wc.hIcon = self._class_name, self._icon
            if not api.u.RegisterClassExW(C.byref(wc)):
                raise ShellError(f"RegisterClassExW failed: {C.get_last_error()}")
            registered = True
            window = api.u.CreateWindowExW(0x80, self._class_name, "", 0, 0, 0, 0, 0,
                                         None, None, instance, None)
            if not window:
                raise ShellError(f"CreateWindowExW failed: {C.get_last_error()}")
            with self._lock:
                self._hwnd = window
            self._add_icon()
            self._ready.set()
            if self._closing.is_set():
                api.u.PostMessageW(window, self.WM_CLOSE, 0, 0)
            message = api.Message()
            while True:
                result = api.u.GetMessageW(C.byref(message), None, 0, 0)
                if result == -1:
                    raise ShellError(f"GetMessageW failed: {C.get_last_error()}")
                if result == 0:
                    break
                api.u.TranslateMessage(C.byref(message))
                api.u.DispatchMessageW(C.byref(message))
        except BaseException as exc:
            self._startup_error = f"{type(exc).__name__}: {exc}"
            self._error(self._startup_error, show=True)
        finally:
            self._remove_icon()
            if self._hwnd and api.u.IsWindow(self._hwnd):
                api.u.DestroyWindow(self._hwnd)
            if registered:
                api.u.UnregisterClassW(self._class_name, instance)
            if self._owns_icon:
                api.u.DestroyIcon(self._icon)
                self._icon = None
                self._owns_icon = False
            with self._lock:
                self._hwnd = None
            self._ready.set()
            if not self._closing.is_set():
                self._error("Desktop shell stopped; keep the main window visible", show=True)

    def _release_mutex(self):
        if self._mutex is not None:
            if not self._api.k.CloseHandle(self._mutex):
                raise ShellError(f"CloseHandle(single-instance lease) failed: {C.get_last_error()}")
            self._mutex = None

    def close(self):
        if threading.current_thread() is self._thread:
            raise ShellError("Queue close to the UI owner; the tray thread cannot join itself")
        self._closing.set()
        with self._lock:
            thread, window = self._thread, self._hwnd
        if thread is not None:
            if window and thread.is_alive() and not self._api.u.PostMessageW(window, self.WM_CLOSE, 0, 0):
                self._error("Posting shell close failed; waiting for thread completion")
            thread.join(timeout=5)
            if thread.is_alive():
                raise ShellError("Shell thread has not stopped; ownership retained, retry close")
        with self._lock:
            self._release_mutex()
            self._thread = self._wndproc = None

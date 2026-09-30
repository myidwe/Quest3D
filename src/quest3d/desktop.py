"""Stable Windows shortcut entry point for the Qt Quick desktop.

DesktopApp is retained as the legacy lifecycle reference for regression tests.
The public entry point always uses the selected Qt interface.
"""
from __future__ import annotations

import argparse
import ctypes
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import queue
import sys
try:
    import tkinter as tk
    from tkinter import messagebox, ttk
except ImportError:
    # Tcl/Tk belongs to the retained legacy view, not the Qt launcher runtime.
    tk = messagebox = ttk = None

from .paths import ROOT


class DesktopApp:
    def __init__(self, directory=ROOT):
        if tk is None:
            raise RuntimeError('이전 화면 실행 불가 · Tcl/Tk 설치 필요')
        from .capture import enable_per_monitor_dpi_awareness
        from .desktop_backend import DesktopController
        from .desktop_shell import ShellIntegration
        from .desktop_ui import DesktopWindow

        enable_per_monitor_dpi_awareness()
        self.events = queue.Queue()
        self.root = None
        self.window = self.controller = None
        self.closing = False
        self.finished = False
        self.dialog = None
        self.shell = ShellIntegration(directory, lambda: self.events.put('show'),
                                      lambda: self.events.put('stop'), lambda: self.events.put('exit'))
        self.primary = self.shell.start()
        if not self.primary:
            if not self.shell.notified_existing:
                raise RuntimeError(self.shell.last_error or '이미 열린 앱을 불러오지 못했습니다.')
            return
        try:
            self.root = tk.Tk()
            self.root.report_callback_exception = self._tk_error
            self.controller = DesktopController(directory)
            self.window = DesktopWindow(self.root, self.controller, on_hide=self.hide, on_exit=self.request_exit)
            icon = Path(directory)/'resources/desktop.ico'
            if icon.exists():
                self.root.iconbitmap(str(icon))
            self.root.after(150, self._events)
        except BaseException:
            if self.controller:
                self.controller.close()
            self.shell.close()
            raise

    def _tk_error(self, kind, error, trace):
        self.closing = False
        logging.error('Desktop callback failed', exc_info=(kind, error, trace))
        self.show()
        messagebox.showerror('Quest3D', '화면 작업을 마치지 못했습니다.\n'+str(error), parent=self.root)

    def _events(self):
        while not self.events.empty():
            event = self.events.get_nowait()
            if event == 'show':
                self.show()
            elif event == 'stop':
                self.show()
                self.controller.command('stop')
            elif event == 'exit':
                self.request_exit()
            if self.finished:
                return
        self.root.after(150, self._events)

    def show(self):
        if self.root:
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()

    def hide(self):
        if self.closing:
            return
        if self.shell.tray_available:
            self.root.withdraw()
        else:
            self.show()
            messagebox.showinfo('Quest3D', '트레이 아이콘을 만들지 못해 창을 유지합니다.\n'
                                '창을 열어 둔 채 사용할 수 있습니다.', parent=self.root)

    def request_exit(self):
        if self.closing:
            return
        self.show()
        state = self.controller.get_snapshot()
        if state['busy'] or state['phase'] == 'checking':
            messagebox.showinfo('Quest3D', '현재 작업을 마친 뒤 종료할 수 있습니다.', parent=self.root)
            return
        if not (state['running'] or state['host_running']):
            self.finish()
            return
        if self.dialog and self.dialog.winfo_exists():
            self.dialog.lift()
            return
        dialog = self.dialog = tk.Toplevel(self.root)
        dialog.title('Quest3D 창 닫기')
        dialog.resizable(False, False)
        dialog.transient(self.root)
        panel = ttk.Frame(dialog, padding=22, style='Q.Card.TFrame')
        panel.pack(fill='both', expand=True)
        ttk.Label(panel, text='PC 화면을 계속 송출할까요?', style='Q.Heading.TLabel').pack(anchor='w')
        ttk.Label(panel, text='트레이로 보내면 Quest에서 계속 볼 수 있습니다.\n'
                             '중지하고 종료하면 Quest 연결도 종료됩니다.', style='Q.TLabel').pack(anchor='w', pady=(12,20))
        buttons = ttk.Frame(panel, style='Q.Card.TFrame')
        buttons.pack(fill='x')

        def choose(action):
            dialog.grab_release()
            dialog.destroy()
            self.dialog = None
            if action == 'hide':
                self.hide()
            elif action == 'exit':
                self.stop_and_exit()

        ttk.Button(buttons, text='트레이로 보내기', style='Q.Primary.TButton', command=lambda: choose('hide'),
                   state='normal' if self.shell.tray_available else 'disabled').pack(side='left', padx=(0,8))
        ttk.Button(buttons, text='중지하고 종료', style='Q.TButton', command=lambda: choose('exit')).pack(side='left', padx=(0,8))
        ttk.Button(buttons, text='취소', style='Q.TButton', command=lambda: choose('cancel')).pack(side='left')
        dialog.protocol('WM_DELETE_WINDOW', lambda: choose('cancel'))
        dialog.update_idletasks()
        dialog.geometry(f'+{self.root.winfo_rootx()+65}+{self.root.winfo_rooty()+140}')
        dialog.grab_set()

    def stop_and_exit(self):
        if self.controller.command('stop'):
            self.closing = True
            self.root.after(250, self._wait_stop)

    def _wait_stop(self):
        state = self.controller.get_snapshot()
        if state['busy']:
            self.root.after(250, self._wait_stop)
        elif state['running'] or state['host_running'] or state['error']:
            self.closing = False
            self.show()
            messagebox.showerror('Quest3D', '정상 종료를 확인하지 못해 앱을 열어 둡니다.\n'
                                +(state['error'] or '중지 상태를 확인해 주세요.'), parent=self.root)
        else:
            self.finish()

    def finish(self):
        # This method closes UI ownership only; stop_and_exit verifies workers.
        if self.finished:
            return
        try:
            self.shell.close()
        except Exception:
            self.closing = False
            raise
        self.finished = True
        self.window.close()
        self.controller.close()
        self.root.destroy()

    def run(self):
        if self.primary:
            self.root.mainloop()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Quest3D Windows desktop app')
    parser.add_argument('--root', type=Path, default=ROOT)
    args = parser.parse_args(argv)
    directory = args.root.resolve()
    try:
        if os.name != 'nt' or directory != ROOT.resolve():
            raise RuntimeError('설치된 Windows 프로젝트 경로의 바로가기를 사용해 주세요.')
        os.chdir(directory)
        logs = directory/'artifacts/desktop'
        logs.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(logs/'app.log', maxBytes=1024*1024, backupCount=2, encoding='utf-8')
        logging.basicConfig(level=logging.INFO, handlers=[handler], format='%(asctime)s %(levelname)s %(message)s')
        logging.info('Qt desktop opened pid=%s', os.getpid())
        from .desktop_qt import main as qt_main
        result = qt_main(['--root', str(directory)])
        logging.info('Desktop closed pid=%s', os.getpid())
        return result
    except Exception as exc:
        logging.exception('Desktop failed')
        ctypes.windll.user32.MessageBoxW(None, 'Quest3D 실행 실패\n'+str(exc)
            +'\n\n복구 안내: docs/DESKTOP_USER_GUIDE.html', 'Quest3D', 0x10)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())

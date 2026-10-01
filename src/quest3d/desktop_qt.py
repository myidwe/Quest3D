"""Qt Quick desktop shell. The validated controller still owns video processes."""
from __future__ import annotations

import argparse
import ctypes
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import queue
import sys

from PySide6.QtCore import QObject, Property, QTimer, Qt, QtMsgType, QUrl, Signal, Slot, qInstallMessageHandler
from PySide6.QtGui import QFont, QFontDatabase, QGuiApplication, QIcon
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle

from .paths import ROOT
from .brand import DISPLAY_NAME


def _qt_message(kind, context, message):
    level = logging.ERROR if kind in (QtMsgType.QtCriticalMsg, QtMsgType.QtFatalMsg) else (
        logging.WARNING if kind == QtMsgType.QtWarningMsg else logging.INFO)
    logging.getLogger('quest3d.qt').log(level, '%s', message)


class QtDesktopLifecycle(QObject):
    closePromptChanged = Signal()
    allowCloseChanged = Signal()

    def __init__(self, application, controller, adapter, shell, parent=None):
        super().__init__(parent)
        self.application, self.controller, self.adapter, self.shell = application, controller, adapter, shell
        self.window = None
        self._close_prompt = False
        self._allow_close = False
        self._closing = False
        self._finished = False
        self._stop_timer = QTimer(self)
        self._stop_timer.setInterval(200)
        self._stop_timer.timeout.connect(self._wait_stop)
        self.adapter.uiEvent.connect(self.shellEvent)

    @Property(bool, notify=closePromptChanged)
    def closePrompt(self):
        return self._close_prompt

    @Property(bool, notify=allowCloseChanged)
    def allowClose(self):
        return self._allow_close

    def _prompt(self, value):
        if value != self._close_prompt:
            self._close_prompt = value
            self.closePromptChanged.emit()

    def _notice(self, text):
        self.adapter.setNotice(text, 'error')

    def _state(self):
        try:
            state = self.controller.get_snapshot()
            if (not isinstance(state, dict)
                    or any(type(state.get(key)) is not bool for key in ('busy', 'running', 'host_running'))
                    or state.get('phase') not in ('checking', 'starting', 'stopping', 'running', 'idle', 'error', 'conflict')):
                raise ValueError('Incomplete lifecycle state')
            return state
        except Exception:
            self.show()
            self._notice('PC 상태 확인 실패 · 잠시 후 재시도')
            return None

    @Slot()
    def show(self):
        if not self._finished and self.window:
            if self.window.windowState() == Qt.WindowState.WindowMinimized:
                self.window.showNormal()
            else:
                self.window.show()
            self.window.raise_()
            self.window.requestActivate()

    @Slot()
    def hide(self):
        if self._finished or self._closing:
            return
        if self.shell.tray_available and self.window:
            self._prompt(False)
            self.window.hide()
        else:
            self.show()
            self._notice('트레이 사용 불가 · 창 유지')

    @Slot()
    def requestClose(self):
        if self._finished or self._closing:
            return
        self.show()
        state = self._state()
        if state is None:
            return
        if state.get('busy') or state.get('phase') in ('checking', 'starting', 'stopping'):
            self._notice('작업 진행 중 · 완료 후 종료 가능')
        elif state.get('running') or state.get('host_running'):
            self._prompt(True)
        else:
            self.finish()

    @Slot()
    def cancelClose(self):
        self._prompt(False)

    @Slot()
    def stopAndExit(self):
        if self._finished or self._closing:
            return
        state = self._state()
        if state is None:
            return
        if state['busy'] or state.get('phase') in ('checking', 'starting', 'stopping'):
            self._notice('작업 진행 중 · 완료 후 종료 가능')
            return
        if not state['running'] and not state['host_running']:
            self._prompt(False)
            self.finish()
            return
        try:
            accepted = self.controller.command('stop')
        except Exception:
            accepted = False
        if accepted:
            self._prompt(False)
            self._closing = True
            self._stop_timer.start()
        else:
            self.show()
            self._notice('송출 중지 요청 실패 · 재시도 필요')

    @Slot()
    def _wait_stop(self):
        if self._finished or not self._closing:
            self._stop_timer.stop()
            return
        state = self._state()
        if state is None:
            self._stop_timer.stop()
            self._closing = False
            return
        if state.get('busy'):
            return
        self._stop_timer.stop()
        if (state.get('running') or state.get('host_running') or state.get('error')
                or state.get('phase') in ('checking', 'starting', 'stopping')):
            self._closing = False
            self.show()
            self._notice('송출 종료 미확인 · PC 상태 확인 필요')
        else:
            self.finish()

    @Slot(str)
    def shellEvent(self, event):
        if self._finished:
            return
        if event == 'show':
            self.show()
        elif event == 'hide':
            self.hide()
        elif event == 'stop':
            self.show()
            if not self._closing:
                self.adapter.poll()
                self.adapter.stop()
        elif event == 'exit':
            self.requestClose()

    def finish(self):
        if self._finished:
            return
        try:
            self.shell.close()
        except Exception:
            self._closing = False
            self.show()
            self._notice('트레이 종료 미확인 · 종료 재시도 필요')
            return
        self._finished = True
        self._stop_timer.stop()
        self.adapter.close()
        self.controller.close()
        self._allow_close = True
        self.allowCloseChanged.emit()
        if self.window: self.window.close()
        self.application.quit()


def main(argv=None):
    from .desktop_backend import DesktopController
    from .desktop_qt_adapter import DesktopQtAdapter
    from .desktop_shell import ShellIntegration
    parser = argparse.ArgumentParser(description=DISPLAY_NAME + ' Windows desktop')
    parser.add_argument('--root', type=Path, default=ROOT)
    args = parser.parse_args(argv)
    directory = args.root.resolve()
    if os.name != 'nt' or directory != ROOT.resolve():
        raise RuntimeError('설치된 앱 바로가기 필요')
    os.chdir(directory)
    events = queue.Queue()
    shell = ShellIntegration(directory, lambda: events.put('show'), lambda: events.put('stop'), lambda: events.put('exit'))
    if not shell.start():
        if not shell.notified_existing: raise RuntimeError(shell.last_error or '기존 창 열기 실패')
        return 0
    controller = adapter = lifecycle = event_timer = None
    previous_message_handler = qInstallMessageHandler(_qt_message)
    try:
        QQuickStyle.setStyle('Basic')
        application = QGuiApplication(sys.argv[:1])
        application.setApplicationName('Quest3D')
        application.setApplicationDisplayName(DISPLAY_NAME)
        application.setQuitOnLastWindowClosed(False)
        application.setWindowIcon(QIcon(str(directory/'resources/desktop.ico')))
        for font in (directory/'resources/ui/fonts').glob('*.otf'):
            QFontDatabase.addApplicationFont(str(font))
        for font in (directory/'resources/ui/fonts').glob('*.ttf'):
            QFontDatabase.addApplicationFont(str(font))
        application.setFont(QFont('Pretendard', 10))
        controller = DesktopController(directory)
        adapter = DesktopQtAdapter(controller)
        lifecycle = QtDesktopLifecycle(application, controller, adapter, shell)
        engine = QQmlApplicationEngine()
        engine.rootContext().setContextProperty('bridge', adapter)
        engine.rootContext().setContextProperty('lifecycle', lifecycle)
        engine.load(QUrl.fromLocalFile(str(directory/'resources/desktop/Main.qml')))
        if not engine.rootObjects(): raise RuntimeError('앱 화면 로드 실패')
        lifecycle.window = engine.rootObjects()[0]
        logging.info('Qt QML ready pid=%s', os.getpid())
        event_timer = QTimer()
        event_timer.setInterval(100)
        def drain():
            while not events.empty() and not lifecycle._finished:
                lifecycle.shellEvent(events.get_nowait())
            if lifecycle._finished:
                event_timer.stop()
        event_timer.timeout.connect(drain)
        event_timer.start()
        return application.exec()
    finally:
        try:
            if event_timer:
                event_timer.stop()
            if not lifecycle or not lifecycle._finished:
                if adapter:
                    adapter.close()
                if controller:
                    controller.close()
                shell.close()
        finally:
            qInstallMessageHandler(previous_message_handler)


if __name__ == '__main__':
    try:
        logs = ROOT/'artifacts/desktop'
        logs.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(level=logging.INFO, handlers=[RotatingFileHandler(logs/'qt-app.log', maxBytes=1024*1024, backupCount=2, encoding='utf-8')])
        sys.exit(main())
    except Exception as error:
        logging.exception('Qt desktop failed')
        ctypes.windll.user32.MessageBoxW(None, DISPLAY_NAME + ' 실행 실패\n'+str(error), DISPLAY_NAME, 0x10)
        sys.exit(1)

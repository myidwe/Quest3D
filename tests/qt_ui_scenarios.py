"""Subprocess-only QGuiApplication tests of production QML event paths."""
from __future__ import annotations
import importlib.util
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
os.environ['QT_QUICK_BACKEND'] = 'software'
os.environ['CUDA_VISIBLE_DEVICES'] = ''
os.environ['QT_QUICK_CONTROLS_STYLE'] = 'Basic'
os.environ['QT_SCALE_FACTOR'] = '1'
os.environ['QT_QPA_FONTDIR'] = str(Path(__file__).resolve().parents[1]/'resources/ui/fonts')
from PySide6.QtCore import QObject, QPoint, QPointF, Qt, QUrl, qInstallMessageHandler
from PySide6.QtGui import QGuiApplication, QFontDatabase
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuick import QQuickWindow
from PySide6.QtTest import QTest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
from quest3d.desktop_qt_adapter import DesktopQtAdapter
from quest3d.desktop_qt import QtDesktopLifecycle

spec = importlib.util.spec_from_file_location('qt_preview', ROOT/'scripts/desktop-qt-preview.py')
preview = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preview)


class UiController(preview.PreviewController):
    """Only in-memory snapshots; no runtime model loading or process access."""
    def __init__(self):
        super().__init__()
        self.state.update(depth_model='depth_anything_v2_small', depth_models=[
            dict(id='depth_anything_v2_small', label='DAv2 Small (기본)', available=True),
            dict(id='distill_any_depth_small', label='DAD Small (비교)', available=True)])
        self.state.update(ai_quality='standard', active_ai_quality='standard', ai_qualities=[
            dict(id='standard', label='Standard'), dict(id='quality', label='Quality · Preview')],
            ai_input_text='실제 AI 입력 504 × 280 · 요청 280 px')
        self.reject_quality = False

    def command(self, action, **values):
        if action == 'select_ai_quality' and self.reject_quality:
            return False
        accepted = super().command(action, **values)
        if accepted and action == 'select_depth_model':
            self.state['depth_model'] = values['depth_model']
        if accepted and action == 'select_ai_quality':
            self.state['ai_quality'] = values['ai_quality']
        return accepted


def main():
    messages = []
    qInstallMessageHandler(lambda kind, context, message: messages.append(message))
    app = QGuiApplication([])
    for font in (ROOT/'resources/ui/fonts').glob('*.otf'):
        QFontDatabase.addApplicationFont(str(font))
    controller = UiController()
    controller.state['depth_percent'] = 1.3125
    bridge = DesktopQtAdapter(controller, poll=False)
    lifecycle = QtDesktopLifecycle(app, controller, bridge, SimpleNamespace(tray_available=True, close=lambda: None))
    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty('bridge', bridge)
    engine.rootContext().setContextProperty('lifecycle', lifecycle)
    engine.load(QUrl.fromLocalFile(str(ROOT/'resources/desktop/Main.qml')))
    assert engine.rootObjects(), messages
    window = lifecycle.window = engine.rootObjects()[0]
    QTest.qWait(30)
    tested = []
    def item(name):
        value = window.findChild(QObject, name)
        if value is None:
            pending = [window.contentItem()]
            while pending:
                candidate = pending.pop()
                if candidate.objectName() == name:
                    value = candidate
                    break
                pending.extend(candidate.childItems())
        assert value is not None, name
        return value
    def click(name):
        value = item(name)
        pos = value.mapToScene(QPointF(value.width()/2, value.height()/2)).toPoint()
        QTest.mouseClick(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, pos)
        app.processEvents()
        QTest.qWait(20)  # Let Qt Quick's next polish pass apply page/layout changes.
    def poll():
        bridge.poll()
        app.processEvents()
        QTest.qWait(20)
    def enter(name, text):
        click(name)
        QTest.keyClick(window, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
        for letter in text:
            QTest.keyClick(window, ord(letter.upper()))

    assert item('primaryButton').property('text') == 'PC 중지'
    assert item('mode3D').property('selected')
    assert item('eyeResolution').property('text') == '1920 × 1080'
    assert not item('monitorCombo').property('enabled')
    click('mode2D')
    assert controller.calls[-1] == ('control', {'mode': '2d'})
    assert bridge.state['mode'] == '3d', 'Mode must await authoritative snapshot'
    assert not item('mode2D').property('enabled')
    poll()
    assert item('mode2D').property('selected')
    tested.append('Mode command and authoritative state, stream source gate')

    click('depthPlus')
    assert controller.calls[-1] == ('control', {'depth_delta': .05})
    poll()
    assert abs(bridge.state['depth_percent'] - 1.3625) < 1e-8
    enter('depthInput', '1.3125')
    QTest.keyClick(window, Qt.Key.Key_Return)
    assert controller.calls[-1] == ('control', {'depth_percent': 1.3125})
    poll()
    assert item('depthInput').property('text') == '1.31%'
    before = len(controller.calls)
    enter('depthInput', '9')
    QTest.keyClick(window, Qt.Key.Key_Return)
    click('nav0')
    assert len(controller.calls) == before, 'Out-of-range input must not queue'
    tested.append('Fine depth step, exact numeric apply, out-of-range rejection')

    slider = item('depthSlider')
    start = slider.mapToScene(QPointF(slider.width()*.3, slider.height()/2)).toPoint()
    end = slider.mapToScene(QPointF(slider.width()*.5, slider.height()/2)).toPoint()
    before = len(controller.calls)
    QTest.mousePress(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start)
    QTest.mouseMove(window, end)
    assert len(controller.calls) == before, 'Dragging must not flood controller'
    QTest.mouseRelease(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, end)
    assert len(controller.calls) == before + 1, controller.calls[before:]
    assert controller.calls[-1][0] == 'control' and 'depth_percent' in controller.calls[-1][1]
    poll()
    assert abs(slider.property('value') - bridge.state['depth_percent']) < 1e-8
    tested.append('Slider release submits once and restores authoritative binding')

    click('comfortSwitch')
    assert controller.calls[-1] == ('control', {'profile': 'linear'})
    assert item('comfortSwitch').property('checked'), 'Switch must await snapshot'
    poll()
    assert not item('comfortSwitch').property('checked')
    tested.append('Comfort switch authoritative state')

    click('nav1')
    assert window.property('page') == 1
    assert item('pairPin').property('enabled')
    enter('pairPin', '0123')
    assert item('pairButton').property('enabled')
    click('pairButton')
    assert controller.calls[-1][0] == 'pair'
    assert item('pairPin').property('text') == ''
    assert '0123' not in bridge.notice and '0123' not in repr(bridge.state)
    poll()
    assert item('pairingMessage').property('visible')
    tested.append('Connection navigation, PIN submission, immediate field clearing')

    click('nav2')
    assert not item('headsetCombo').property('enabled')
    assert not item('audioOutput_quest').property('enabled')
    assert not item('aiQualityCombo').property('enabled')
    assert item('aiInputSize').property('text') == '실제 AI 입력 504 × 280 · 요청 280 px'
    assert item('headsetCombo').height() >= 40, 'Headset must retain a complete click target inside RowLayout'
    assert item('depthModelCombo').property('enabled'), 'Ready stream permits model comparison'
    assert item('depthModelCombo').height() >= 40
    item('depthModelCombo').forceActiveFocus()
    app.processEvents()
    QTest.qWait(20)
    click('depthModelCombo')
    QTest.keyClick(window, Qt.Key.Key_End)
    QTest.keyClick(window, Qt.Key.Key_Return)
    app.processEvents()
    assert controller.calls[-1] == ('select_depth_model', {'depth_model': 'distill_any_depth_small'}), dict(
        calls=controller.calls[-3:], position=str(item('depthModelCombo').mapToScene(QPointF())),
        index=item('depthModelCombo').property('currentIndex'),
        focus=window.activeFocusItem().objectName() if window.activeFocusItem() else None, messages=messages)
    assert bridge.state['depth_model'] == 'depth_anything_v2_small'
    assert item('depthModelCombo').property('currentIndex') == 0
    assert item('depthModelLabel').property('text') == 'DAv2 Small (기본)'
    assert not item('depthModelCombo').property('enabled')
    poll()
    assert item('depthModelCombo').property('currentIndex') == 1
    assert item('depthModelLabel').property('text') == 'DAD Small (비교)'
    assert '입체감 감소' in item('depthModelHint').property('text')
    tested.append('Live depth-model selection and authoritative label/index')
    controller.state['depth_model_switching'] = True
    poll()
    assert not item('depthModelCombo').property('enabled')
    assert item('depthModelHint').property('text') == '모델 전환 중'
    controller.state.update(depth_model_switching=False, depth_model='depth_anything_v2_small')
    controller.state['depth_models'][1]['available'] = False
    poll()
    click('depthModelCombo')
    assert not item('depthModelOption1').property('enabled')
    assert '미설치' in item('depthModelOption1').property('contentItem').property('text')
    before = len(controller.calls)
    QTest.keyClick(window, Qt.Key.Key_End)
    QTest.keyClick(window, Qt.Key.Key_Return)
    app.processEvents()
    assert len(controller.calls) == before, 'Unavailable option must never queue through keyboard'
    assert item('depthModelCombo').property('currentIndex') == 0
    assert item('depthModelLabel').property('text') == 'DAv2 Small (기본)'
    controller.state['depth_model'] = 'distill_any_depth_small'
    poll()
    assert item('depthModelLabel').property('text') == 'DAD Small (비교) · 미설치'
    controller.state.update(depth_model='depth_anything_v2_small', depth_models=None)
    poll()
    assert not item('depthModelCombo').property('enabled')
    assert item('depthModelLabel').property('text') == '모델 확인 전'
    controller.state['depth_models'] = UiController().state['depth_models']
    controller.state['depth_models'][1]['available'] = True
    poll()
    tested.append('Switch-in-progress and unavailable depth-model option gates')
    for name, action in [('exportButton', 'export_diagnostics'), ('logsButton', 'open_logs'),
                         ('diagnosticsButton', 'open_diagnostics'), ('refreshButton', 'refresh')]:
        item(name).forceActiveFocus()
        app.processEvents()
        QTest.qWait(20)
        click(name)
        assert controller.calls[-1][0] == action, (name, controller.calls[-1])
        poll()
    tested.append('Diagnostics, logs, refresh commands')
    # The added Headset row makes App actions scrollable. Keyboard focus must
    # reveal the actual control before a mouse click, as in the compact layout.
    item('hideButton').forceActiveFocus()
    app.processEvents()
    QTest.qWait(20)
    click('hideButton')
    assert not window.isVisible(), dict(last=controller.calls[-1],
        hide_position=str(item('hideButton').mapToScene(QPointF())),
        focus=window.activeFocusItem().objectName() if window.activeFocusItem() else None,
        messages=messages)
    lifecycle.show()
    click('exitButton')
    assert lifecycle.closePrompt
    click('closeCancelButton')
    assert not lifecycle.closePrompt
    tested.append('Tray hide and recoverable close dialog')

    click('navHelp')
    assert window.property('page') == 3
    item('guideButton').forceActiveFocus()
    app.processEvents()
    click('guideButton')
    assert controller.calls[-1][0] == 'open_guide', dict(last=controller.calls[-1], guideY=item('guideButton').mapToScene(QPointF()).y(), guideEnabled=item('guideButton').property('enabled'), scroll=item('pageScroll').property('contentItem').property('contentY'), focus=window.activeFocusItem().objectName() if window.activeFocusItem() else None, messages=messages)
    poll()
    tested.append('Help navigation and guide')

    controller.state.update(busy=True)
    poll()
    assert not item('depthModelCombo').property('enabled')
    click('nav0')
    before = len(controller.calls)
    click('mode3D'); click('depthPlus'); click('primaryButton')
    assert len(controller.calls) == before
    assert window.property('page') == 0, 'Busy state must retain navigation'
    tested.append('Busy gating without blocking navigation')

    controller.state.update(phase='idle', busy=False, running=False, ready=False,
        host_running=False, pairing_available=False, connected=False)
    poll()
    assert item('primaryButton').property('text') == 'PC 시작'
    assert item('monitorCombo').property('enabled')
    click('monitorCombo')
    QTest.keyClick(window, Qt.Key.Key_Home)
    QTest.keyClick(window, Qt.Key.Key_Return)
    assert controller.calls[-1] == ('select_monitor', {'device_name': 'DISPLAY1'})
    poll()
    assert item('monitorCombo').property('currentIndex') == 0
    tested.append('Stopped monitor selection via keyboard popup')

    click('nav2')
    assert item('depthModelCombo').property('enabled')
    assert item('audioOutput_pc').property('selected')
    click('audioOutput_quest')
    assert controller.calls[-1] == ('select_audio_output', {'audio_output':'quest'})
    assert item('audioOutput_pc').property('selected'), 'Audio output awaits authoritative snapshot'
    assert not item('audioOutput_quest').property('enabled')
    poll()
    assert item('audioOutput_quest').property('selected')
    assert 'PC 출력 복원' in item('audioOutputHint').property('text')
    click('audioOutput_both')
    poll()
    assert item('audioOutput_both').property('selected')
    click('audioOutput_pc')
    poll()
    assert item('audioOutput_pc').property('selected')
    tested.append('Sound selection, active-stream gate, authoritative state and PC default return')
    assert item('aiQualityCombo').property('enabled')
    item('aiQualityCombo').forceActiveFocus()
    app.processEvents()
    QTest.qWait(20)
    click('aiQualityCombo')
    QTest.keyClick(window, Qt.Key.Key_End)
    QTest.keyClick(window, Qt.Key.Key_Return)
    assert controller.calls[-1] == ('select_ai_quality', {'ai_quality': 'quality'})
    assert item('aiQualityCombo').property('currentIndex') == 0
    assert item('aiQualityLabel').property('text') == 'Standard'
    assert not item('aiQualityCombo').property('enabled')
    poll()
    assert item('aiQualityCombo').property('currentIndex') == 1
    assert item('aiQualityLabel').property('text') == 'Quality · Preview'
    assert '장면에 따라 입체감 변화' in item('aiQualityHint').property('text')
    controller.reject_quality = True
    click('aiQualityCombo')
    QTest.keyClick(window, Qt.Key.Key_Home)
    QTest.keyClick(window, Qt.Key.Key_Return)
    app.processEvents()
    assert item('aiQualityCombo').property('currentIndex') == 1
    assert item('aiQualityLabel').property('text') == 'Quality · Preview'
    controller.reject_quality = False
    tested.append('AI Quality selection, authoritative acknowledgement and rejected choice restoration')
    item('depthModelCombo').forceActiveFocus()
    app.processEvents()
    QTest.qWait(20)
    click('depthModelCombo')
    QTest.keyClick(window, Qt.Key.Key_End)
    QTest.keyClick(window, Qt.Key.Key_Return)
    assert controller.calls[-1] == ('select_depth_model', {'depth_model': 'distill_any_depth_small'})
    assert item('depthModelLabel').property('text') == 'DAv2 Small (기본)'
    poll()
    assert item('depthModelCombo').property('currentIndex') == 1
    tested.append('Stopped depth-model selection')
    item('headsetCombo').forceActiveFocus()
    app.processEvents()
    QTest.qWait(20)
    assert item('headsetCombo').property('enabled')
    click('headsetCombo')
    QTest.keyClick(window, Qt.Key.Key_End)
    QTest.keyClick(window, Qt.Key.Key_Return)
    assert controller.calls[-1] == ('select_output_profile', {'output_profile': 'quest3'})
    assert bridge.state['output_profile'] == 'quest2', 'Headset awaits controller snapshot'
    poll()
    assert item('headsetCombo').property('currentIndex') == 1
    assert item('eyeResolution').property('text') == '2048 × 1152'
    click('headsetCombo')
    QTest.keyClick(window, Qt.Key.Key_Home)
    QTest.keyClick(window, Qt.Key.Key_Return)
    assert controller.calls[-1] == ('select_output_profile', {'output_profile': 'quest2'})
    poll()
    assert item('eyeResolution').property('text') == '1920 × 1080'
    tested.append('Stopped Quest 3 selection, authoritative resolution and Quest 2 return')

    controller.state.update(phase='error', error='검증용 오류 원인')
    poll()
    assert item('errorPanel').property('visible')
    click('errorDetailButton')
    assert item('errorDetail').property('visible')
    assert item('errorDetail').property('text') == '검증용 오류 원인'
    window.setWidth(740); window.setHeight(590)
    QTest.qWait(20)
    click('navHelp')
    item('guideButton').forceActiveFocus()
    app.processEvents()
    guide = item('guideButton')
    position = guide.mapToScene(QPointF(0, guide.height())).y()
    assert position <= window.height() - 70, 'Focused guide remains clipped under footer'
    tested.append('Persistent error detail, compact-window focus scroll')

    window.setProperty('page', 0)
    app.processEvents()
    item('questViewGuide').forceActiveFocus()
    app.processEvents()
    calls_before_guide = list(controller.calls)
    click('questViewGuide')
    assert window.property('page') == 3
    assert item('questViewHelp').property('visible')
    assert controller.calls == calls_before_guide, 'Quest view guidance must not mutate stream or geometry'
    tested.append('Compact-window Quest view guide navigation without stream commands')

    controller.state.update(phase='idle', error=None)
    poll()
    click('primaryButton')
    assert controller.calls[-1] == ('start', {})
    poll()
    assert item('primaryButton').property('text') == 'PC 중지'
    click('primaryButton')
    assert controller.calls[-1] == ('stop', {})
    poll()
    assert item('primaryButton').property('text') == 'PC 시작'
    tested.append('Primary start and stop complete through authoritative state')

    meaningful = [m for m in messages if not m.startswith('This plugin does not support')]
    assert not meaningful, meaningful
    bridge.close()
    lifecycle._allow_close = True
    lifecycle.allowCloseChanged.emit()
    window.close()
    print(json.dumps(dict(passed=tested, qml_warnings=meaningful, capture=False, processes=False), ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())

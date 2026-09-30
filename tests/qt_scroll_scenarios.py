"""Real offscreen/software QML scrolling; fixture controller, no PC capture."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import time

import qt_ui_scenarios as ui
from PySide6.QtGui import QWheelEvent


def main():
    messages = []
    ui.qInstallMessageHandler(lambda kind, context, message: messages.append(message))
    app = ui.QGuiApplication([])
    for font in (ui.ROOT/'resources/ui/fonts').glob('*.otf'):
        ui.QFontDatabase.addApplicationFont(str(font))
    controller = ui.UiController()
    controller.state['last_diagnostics'] = 'fixture-diagnostics'
    bridge = ui.DesktopQtAdapter(controller, poll=False)
    lifecycle = ui.QtDesktopLifecycle(app, controller, bridge,
        ui.SimpleNamespace(tray_available=True, close=lambda: None))
    engine = ui.QQmlApplicationEngine()
    engine.rootContext().setContextProperty('bridge', bridge)
    engine.rootContext().setContextProperty('lifecycle', lifecycle)
    engine.load(ui.QUrl.fromLocalFile(str(ui.ROOT/'resources/desktop/Main.qml')))
    assert engine.rootObjects(), messages
    window = lifecycle.window = engine.rootObjects()[0]

    def settle():
        app.processEvents()
        ui.QTest.qWait(35)

    def item(name):
        result = window.findChild(ui.QObject, name)
        if result is None:
            pending = [window.contentItem()]
            while pending:
                candidate = pending.pop()
                if candidate.objectName() == name:
                    result = candidate
                    break
                pending.extend(candidate.childItems())
        assert result is not None, name
        return result

    def bounds(target):
        p = target.mapToScene(ui.QPointF())
        return [p.x(), p.y(), p.x()+target.width(), p.y()+target.height()]

    def inside(target, view):
        rect, clip = bounds(target), bounds(view)
        assert rect[0] >= clip[0]-.1 and rect[1] >= clip[1]-.1, (target.objectName(), rect, clip)
        assert rect[2] <= clip[2]+.1 and rect[3] <= clip[3]+.1, (target.objectName(), rect, clip)
        return rect

    def click(target):
        p = target.mapToScene(ui.QPointF(target.width()/2, target.height()/2)).toPoint()
        ui.QTest.mouseClick(window, ui.Qt.MouseButton.LeftButton,
            ui.Qt.KeyboardModifier.NoModifier, p)
        settle()

    evidence = []
    output = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    if output:
        output.mkdir(parents=True, exist_ok=True)
    sizes = [tuple(map(int,sys.argv[2].split('x')))] if len(sys.argv) > 2 else [(870, 692), (740, 590)]
    for width, height in sizes:
        controller.state.update(phase='running',running=True,host_running=True,ready=True)
        bridge.poll()
        window.setWidth(width)
        window.setHeight(height)
        click(item('nav2'))
        view = item('pageScroll')
        scroll = view.property('contentItem')
        initial = float(scroll.property('contentY'))
        assert initial == 0
        assert float(view.property('contentHeight')) > view.height()
        scrollbar = item('pageScrollBar')
        assert scrollbar.property('visible')
        assert abs(scrollbar.height()-view.height()) < .1, 'Scroll affordance must span the viewport'
        inside(scrollbar, view)
        bars = [child for child in window.findChildren(ui.QObject)
                if 'ScrollBar' in child.metaObject().className()]
        initial_bars = [dict(type=bar.metaObject().className(), visible=bar.property('visible'),
            width=bar.property('width'), height=bar.property('height'),
            size=bar.property('size'), position=bar.property('position')) for bar in bars]
        pos = view.mapToScene(ui.QPointF(20, 20))
        ui.QTest.mouseMove(window, pos.toPoint())
        settle()
        wheel = QWheelEvent(pos, ui.QPointF(window.mapToGlobal(pos.toPoint())),
            ui.QPoint(), ui.QPoint(0, -2400), ui.Qt.MouseButton.NoButton,
            ui.Qt.KeyboardModifier.NoModifier, ui.Qt.ScrollPhase.NoScrollPhase, False)
        wheel.setTimestamp(int(time.monotonic()*1000))
        app.sendEvent(window, wheel)
        settle()
        for _ in range(100):
            if not scroll.property('moving'):
                break
            ui.QTest.qWait(20)
        assert not scroll.property('moving'), 'Wheel scroll must settle before clicking'
        after_wheel = float(scroll.property('contentY'))
        assert after_wheel > initial, dict(size=[width,height], before=initial,
            after=after_wheel, contentHeight=view.property('contentHeight'), bars=initial_bars,
            wheel_accepted=wheel.isAccepted(), focus=window.activeFocusItem().objectName(),
            enabled=view.property('enabled'), interactive=scroll.property('interactive'),
            wheel_enabled=view.property('wheelEnabled'), popup=lifecycle.closePrompt,
            page=window.property('page'), position=str(pos),
            flickable_content_height=scroll.property('contentHeight'),
            flickable_height=scroll.height(), origin_y=scroll.property('originY'))
        rects = {name: inside(item(name), view) for name in
                 ('exportButton', 'logsButton', 'diagnosticsButton', 'hideButton', 'exitButton')}
        if output:
            window.grabWindow().save(str(output/f'wheel-{width}x{height}.png'))
        for name, action in (('exportButton', 'export_diagnostics'), ('logsButton', 'open_logs'),
                             ('diagnosticsButton', 'open_diagnostics')):
            click(item(name))
            assert controller.calls and controller.calls[-1][0] == action, dict(name=name,
                rect=rects[name], after=bounds(item(name)), scroll=scroll.property('contentY'),
                calls=controller.calls, messages=messages, bars=initial_bars,
                enabled=item(name).property('enabled'), visible=item(name).property('visible'),
                moving=scroll.property('moving'), flicking=scroll.property('flicking'),
                focus=window.activeFocusItem().objectName() if window.activeFocusItem() else '')
            bridge.poll()
            settle()
        click(item('exitButton'))
        assert lifecycle.closePrompt
        click(item('closeCancelButton'))
        assert not lifecycle.closePrompt
        if output:
            assert window.grabWindow().save(str(output/f'settings-bottom-{width}x{height}.png'))

        # Only normal Tab events after entering the page; no forceActiveFocus on
        # offscreen targets. Every settings target must be fully revealed.
        controller.state.update(phase='idle',running=False,host_running=False,ready=False)
        bridge.poll()
        click(item('nav2'))
        focused = []
        for _ in range(45):
            ui.QTest.keyClick(window, ui.Qt.Key.Key_Tab)
            settle()
            active = window.activeFocusItem()
            name = active.objectName() if active else ''
            assert active is not None and active.isVisible(), 'Tab must not focus a hidden page'
            assert name not in {'monitorCombo', 'mode2D', 'mode3D', 'depthInput', 'depthPlus',
                'depthSlider', 'comfortSwitch', 'pairPin', 'pairButton', 'guideButton'}, name
            if name in ('aiQualityCombo', 'depthModelCombo', 'refreshButton', 'exportButton', 'logsButton',
                        'diagnosticsButton', 'hideButton', 'exitButton'):
                inside(active, view)
                focused.append(name)
            if name == 'exitButton':
                break
        expected = {'aiQualityCombo', 'depthModelCombo', 'refreshButton', 'exportButton', 'logsButton',
                    'diagnosticsButton', 'hideButton', 'exitButton'}
        assert set(focused) == expected, focused
        evidence.append(dict(size=[width,height], initial_y=initial, wheel_y=after_wheel,
            content_height=view.property('contentHeight'), viewport_height=view.height(),
            bottom_bounds=rects, keyboard_targets=focused, initial_scrollbars=initial_bars))

    meaningful = [message for message in messages if not message.startswith('This plugin does not support')]
    assert not meaningful, meaningful
    bridge.close()
    lifecycle._allow_close = True
    lifecycle.allowCloseChanged.emit()
    window.close()
    result = dict(passed=True, cases=evidence, qml_warnings=meaningful, gpu=False,
        fixture=True, live_stream=False)
    if output:
        (output/'scroll-checks.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())

"""The installed shortcut must open the chosen UI, with visible failure recovery."""
import sys
import importlib.util
import builtins
from types import SimpleNamespace

from quest3d import desktop


def setup_entrypoint(monkeypatch, tmp_path):
    calls, errors = [], []
    monkeypatch.setattr(desktop, 'ROOT', tmp_path)
    monkeypatch.setattr(desktop.os, 'chdir', lambda directory: calls.append(('cwd', directory)))
    monkeypatch.setattr(desktop, 'DesktopApp', lambda *args: (_ for _ in ()).throw(
        AssertionError('The rejected Tk screen must not open')))
    monkeypatch.setattr(desktop.ctypes, 'windll', SimpleNamespace(user32=SimpleNamespace(
        MessageBoxW=lambda *args: errors.append(args))))
    def configure(**kwargs):
        for handler in kwargs['handlers']:
            handler.close()
    monkeypatch.setattr(desktop.logging, 'basicConfig', configure)
    return calls, errors


def test_existing_shortcut_module_runs_qt_without_changing_install_root(monkeypatch, tmp_path):
    calls, errors = setup_entrypoint(monkeypatch, tmp_path)
    def launch(args):
        calls.append(('qt', args))
        return 0
    monkeypatch.setitem(sys.modules, 'quest3d.desktop_qt', SimpleNamespace(main=launch))
    assert desktop.main(['--root', str(tmp_path)]) == 0
    assert calls == [('cwd', tmp_path), ('qt', ['--root', str(tmp_path)])]
    assert not errors


def test_qt_failure_reports_recovery_without_silent_legacy_fallback(monkeypatch, tmp_path):
    calls, errors = setup_entrypoint(monkeypatch, tmp_path)
    def launch(args):
        raise RuntimeError('앱 화면 로드 실패')
    monkeypatch.setitem(sys.modules, 'quest3d.desktop_qt', SimpleNamespace(main=launch))
    assert desktop.main(['--root', str(tmp_path)]) == 1
    assert len(errors) == 1
    assert '앱 화면 로드 실패' in errors[0][1]
    assert 'docs/DESKTOP_USER_GUIDE.html' in errors[0][1]


def test_wrong_install_root_cannot_launch_a_controller(monkeypatch, tmp_path):
    calls, errors = setup_entrypoint(monkeypatch, tmp_path)
    monkeypatch.setitem(sys.modules, 'quest3d.desktop_qt', SimpleNamespace(
        main=lambda *args: (_ for _ in ()).throw(AssertionError('Unexpected Qt startup'))))
    assert desktop.main(['--root', str(tmp_path/'other')]) == 1
    assert calls == []
    assert len(errors) == 1


def test_qt_entrypoint_does_not_require_tcl_tk(monkeypatch, tmp_path):
    original_import = builtins.__import__
    def no_tk(name, *args, **kwargs):
        if name == 'tkinter' or name.startswith('tkinter.'):
            raise ModuleNotFoundError('No module named tkinter')
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', no_tk)
    spec = importlib.util.spec_from_file_location('quest3d._desktop_without_tk', desktop.__file__)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.tk is None and module.ttk is None
    calls = []
    monkeypatch.setattr(module, 'ROOT', tmp_path)
    monkeypatch.setattr(module.os, 'chdir', lambda directory: None)
    monkeypatch.setattr(module.logging, 'basicConfig', lambda **kwargs: [h.close() for h in kwargs['handlers']])
    monkeypatch.setitem(sys.modules, 'quest3d.desktop_qt', SimpleNamespace(
        main=lambda args: calls.append(args) or 0))
    assert module.main(['--root', str(tmp_path)]) == 0
    assert calls == [['--root', str(tmp_path)]]

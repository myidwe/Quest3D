"""Use a fresh process so Qt GUI tests cannot reuse a QCoreApplication."""
import json
from pathlib import Path
import subprocess
import sys


def test_real_qml_user_flows():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, str(root/'tests/qt_ui_scenarios.py')],
        cwd=root, capture_output=True, text=True, encoding='utf-8', timeout=40)
    assert result.returncode == 0, result.stdout + result.stderr
    evidence = json.loads(result.stdout)
    assert len(evidence['passed']) >= 12
    assert evidence['qml_warnings'] == []
    assert not evidence['capture'] and not evidence['processes']


def test_real_qml_settings_scroll_and_keyboard_reachability(tmp_path):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, str(root/'tests/qt_scroll_scenarios.py'), str(tmp_path)],
        cwd=root, capture_output=True, text=True, encoding='utf-8', timeout=40)
    assert result.returncode == 0, result.stdout + result.stderr
    evidence = json.loads(result.stdout)
    assert evidence['passed'] and len(evidence['cases']) == 2
    assert [case['size'] for case in evidence['cases']] == [[870, 692], [740, 590]]
    assert evidence['qml_warnings'] == []
    assert not evidence['gpu'] and not evidence['live_stream']

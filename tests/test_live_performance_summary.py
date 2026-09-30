"""Host benchmark accounting and isolated namespace; no capture/GPU processes."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("live_perf_script", ROOT / "scripts/benchmark-live-performance.py")
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)


def write_rows(path, values):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(value) + "\n" for value in values), encoding="utf-8")


def fixture(directory, *, mismatch=False):
    def ai(number, seconds):
        return dict(frame_id=number, revision=2, completed_ns=round(seconds * 1e9),
                    preprocess_ms=1, inference_ms=8, stereo_readback_ms=2,
                    pc_total_ms=11, source_age_at_completion_ms=20)
    def shown(number, seconds, *, success=True):
        return dict(published=success, mode="3d", ai_completion_sequence=number,
                    host_ns=round(seconds * 1e9), source_ns=round((seconds - .2) * 1e9),
                    source_frame_id=number + (10 if mismatch else 0), revision=2, cursor_overlay_ms=.1)
    write_rows(directory / "session/ai/frames.jsonl", [ai(1, 109), ai(2, 110.1), ai(3, 111.1), ai(4, 112.1)])
    write_rows(directory / "session/presentation.jsonl", [shown(1, 109.9), shown(2, 110.2),
        shown(2, 110.3), shown(3, 111.2), shown(4, 112.2, success=False), shown(4, 113.2)])
    (directory / "session/status.json").write_text(json.dumps({"running": False, "ai_error": None}), encoding="utf-8")
    (directory / "run-settings.json").write_text(json.dumps({"cursor_workload": "system"}), encoding="utf-8")
    (directory / "gpu-samples.json").write_text(json.dumps([
        {"host_ns": 109_000_000_000, "returncode": 0, "csv": "date, 99, 9000, 99, 200"},
        {"host_ns": 112_000_000_000, "returncode": 0, "csv": "date, 75, 4000, 65, 100"},
        {"host_ns": 114_000_000_000, "returncode": 1, "csv": ""},
    ]), encoding="utf-8")


def test_warmup_repeats_and_failed_publications_do_not_inflate_new_3d_fps(tmp_path):
    fixture(tmp_path)
    result = benchmark.summarize(tmp_path, 100_000_000_000, 115_000_000_000)
    assert result["measurement_seconds"] == 5
    assert result["ai_complete_fps"] == .6
    assert result["published_fps"] == .8
    assert result["unique_3d_published_fps"] == .6
    assert result["repeated_or_2d_publications"] == 1
    assert result["bridge_skipped"] == 1
    assert result["completion_to_first_publish_ms"]["max"] == 1100
    assert result["gpu_telemetry"]["vram_used_mib"]["max"] == 4000
    assert result["gpu_telemetry_failed_samples"] == 1


def test_corrupted_source_completion_pair_cannot_be_reported_as_success(tmp_path):
    fixture(tmp_path, mismatch=True)
    with pytest.raises(RuntimeError, match="does not match"):
        benchmark.summarize(tmp_path, 100_000_000_000, 115_000_000_000)
    assert not (tmp_path / "result.json").exists()


def test_first_repeat_after_cutoff_is_not_counted_as_new_completion(tmp_path):
    fixture(tmp_path)
    path = tmp_path / "session/presentation.jsonl"
    rows = [json.loads(line) for line in path.read_text("utf-8").splitlines()]
    old_repeat = dict(rows[0], host_ns=110_010_000_000)
    later_repeat = dict(rows[0], host_ns=114_000_000_000)
    rows.insert(1, old_repeat)
    rows.append(later_repeat)
    write_rows(path, rows)
    result = benchmark.summarize(tmp_path, 100_000_000_000, 115_000_000_000)
    assert result["unique_3d_published_fps"] == .6
    assert result["published_fps"] == 1.2
    assert result["repeated_or_2d_publications"] == 3
    assert result["unique_3d_accounting"] == "global_first_successful_completion_publication"
    assert result["measurement_window"] == {"start_ns": 110_000_000_000, "stop_ns": 115_000_000_000}
    assert result["completion_to_first_publish_ms"]["max"] == 1100


def test_repeat_still_checks_source_identity(tmp_path):
    fixture(tmp_path)
    path = tmp_path / "session/presentation.jsonl"
    rows = [json.loads(line) for line in path.read_text("utf-8").splitlines()]
    rows[2]["source_frame_id"] = 999
    write_rows(path, rows)
    with pytest.raises(RuntimeError, match="does not match"):
        benchmark.summarize(tmp_path, 100_000_000_000, 115_000_000_000)


def test_noncontiguous_explicit_completion_sequences_use_identity_not_row_number(tmp_path):
    fixture(tmp_path)
    ai_path = tmp_path / "session/ai/frames.jsonl"
    ai_rows = [json.loads(line) for line in ai_path.read_text("utf-8").splitlines()]
    for index, row in enumerate(ai_rows, 1):
        row["completion_sequence"] = index * 10
    write_rows(ai_path, ai_rows)
    present_path = tmp_path / "session/presentation.jsonl"
    present_rows = [json.loads(line) for line in present_path.read_text("utf-8").splitlines()]
    for row in present_rows:
        row["ai_completion_sequence"] *= 10
    write_rows(present_path, present_rows)
    result = benchmark.summarize(tmp_path, 100_000_000_000, 115_000_000_000)
    assert result["unique_3d_published_fps"] == .6
    assert result["completion_to_first_publish_ms"]["max"] == 1100


def test_snapshot_namespace_uses_actual_assets_and_private_session_routing(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "__init__.py").write_text("", encoding="utf-8")
    (source / "session.py").write_text("from .paths import ROOT, MODEL_DIR, ARTIFACT_DIR\n", encoding="utf-8")
    routing = tmp_path / "private"
    module = benchmark.load_package(source, routing)
    assert module.ROOT == ROOT
    assert module.MODEL_DIR == ROOT / "models"
    assert module.ARTIFACT_DIR == routing


def screen(name, rectangle, ratio=1):
    return SimpleNamespace(name=lambda: name, devicePixelRatio=lambda: ratio,
        geometry=lambda: SimpleNamespace(x=lambda: rectangle[0], y=lambda: rectangle[1],
            width=lambda: rectangle[2], height=lambda: rectangle[3]))


def test_friendly_qt_screen_name_uses_unique_physical_geometry():
    selected = screen("Odyssey G5", (0, 0, 2560, 1440))
    other = screen("DISPLAY1", (2560, 0, 2160, 1440))
    monitor = {"device_name": "DISPLAY2", "bounds": {"left": 0, "top": 0, "width": 2560, "height": 1440}}
    result, reason = benchmark.select_qt_screen([other, selected], monitor)
    assert result is selected and reason == "unique_physical_geometry"
    with pytest.raises(RuntimeError, match="unique physical geometry"):
        benchmark.select_qt_screen([selected, selected], monitor)


def test_dpi_scaled_geometry_must_match_native_extent():
    selected = screen("Friendly", (0, 0, 1280, 720), 2)
    monitor = {"device_name": "DISPLAY2", "bounds": {"left": 0, "top": 0, "width": 2560, "height": 1440}}
    assert benchmark.select_qt_screen([selected], monitor)[0] is selected
    with pytest.raises(RuntimeError, match="unique physical geometry"):
        benchmark.select_qt_screen([screen("Wrong", (1, 0, 1280, 720), 2)], monitor)

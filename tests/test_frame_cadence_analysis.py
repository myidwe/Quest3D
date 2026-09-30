"""Offline clock joins: no GPU, capture, desktop process, or real sleep."""
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("frame_cadence_analysis", ROOT / "scripts/analyze-frame-cadence.py")
cadence = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cadence)


def ns(milliseconds):
    return round(milliseconds * 1_000_000)


def ai(sequence, *, captured, submitted, started, completed, available):
    return {"completion_sequence": sequence, "frame_id": sequence * 3, "revision": 2,
            "generation": 1, "capture_ns": ns(captured), "submitted_ns": ns(submitted),
            "started_ns": ns(started), "completed_ns": ns(completed), "available_ns": ns(available),
            "pc_total_ms": completed - started}


def shown(sequence, milliseconds, *, success=True, mode="3d", **fields):
    return {"published": success, "mode": mode, "ai_completion_sequence": sequence,
            "source_frame_id": sequence * 3 if sequence else 0, "revision": 2,
            "generation": 1, "host_ns": ns(milliseconds), **fields}


def simple_rows():
    return [ai(1, captured=1, submitted=2, started=3, completed=23, available=24),
            ai(2, captured=30, submitted=31, started=32, completed=52, available=55)]


def test_repeated_and_failed_publications_are_not_new_3d():
    result = cadence.analyze_rows(simple_rows(), [shown(1, 25), shown(1, 40),
        shown(2, 57, success=False), shown(2, 60), shown(1, 75), shown(None, 90, mode="2d")],
        start_ns=0, stop_ns=ns(100))
    assert result["counts"]["unique_3d_publications"] == 2
    assert result["counts"]["successful_publications"] == 5
    assert result["rates"]["unique_3d_publication_fps"] == 20
    assert result["metrics"]["completion_to_first_publish_ms"]["max"] == 8
    assert result["metrics"]["available_to_first_publish_ms"]["max"] == 5
    cohorts = result["metric_cohorts"]
    assert cohorts["metrics"]["available_to_first_publish_ms"] == "ai_completion_window"
    assert cohorts["metrics"]["source_age_at_first_publish_ms"] == "ai_completion_window"
    assert cohorts["metrics"]["unique_3d_publication_interval_ms"] == "unique_first_publication_window"
    assert set(cohorts["metrics"]) == set(result["metrics"])
    assert "outside the window" in cohorts["definitions"]["ai_completion_window"]


def test_repeat_after_cutoff_does_not_reintroduce_prior_completion():
    result = cadence.analyze_rows(simple_rows(), [shown(1, 25), shown(1, 45), shown(2, 60)],
                                 start_ns=ns(40), stop_ns=ns(100))
    assert result["counts"]["unique_3d_publications"] == 1
    assert result["counts"]["repeated_or_2d_publications"] == 1


@pytest.mark.parametrize("field,value", [("source_frame_id", 9), ("revision", 99),
                                         ("generation", 3), ("ai_completion_sequence", 4)])
def test_wrong_identity_is_rejected_before_reporting_fps(field, value):
    row = shown(1, 25)
    row[field] = value
    with pytest.raises(cadence.CadenceDataError):
        cadence.analyze_rows(simple_rows(), [row])


def test_legacy_missing_timestamps_remain_unavailable_not_zero():
    rows = simple_rows()
    for row in rows:
        for key in ("completion_sequence", "submitted_ns", "started_ns", "available_ns"):
            del row[key]
    result = cadence.analyze_rows(rows, [shown(1, 25), shown(2, 60)])
    assert result["identity"]["legacy_ai_row_sequences"] == 2
    assert result["metrics"]["submit_to_start_ms"] == {"status": "unavailable", "count": 0}
    assert result["metrics"]["completion_to_available_ms"]["status"] == "unavailable"
    assert result["metrics"]["pacing_lateness_ms"]["status"] == "unavailable"
    assert result["legacy_estimates"]["worker_after_previous_completion_ms"]["status"] == "estimated_from_logged_duration"
    assert result["legacy_estimates"]["worker_after_previous_completion_ms"]["max"] == 9


def test_long_gap_separates_submission_dispatch_availability_and_publication():
    rows = [ai(1, captured=1, submitted=2, started=3, completed=23, available=24),
            ai(2, captured=61, submitted=62, started=64, completed=84, available=86)]
    present = [shown(1, 25), shown(1, 42), shown(1, 59), shown(1, 76),
               shown(2, 93, snapshot_ns=ns(90), tick_started_ns=ns(89), capture_poll_ms=.3,
                     pacing={"entered_ns": ns(77), "wake_ns": ns(89), "requested_wait_ns": ns(11),
                             "lateness_ns": ns(1), "clock_resets": 0})]
    result = cadence.analyze_rows(rows, present, long_gap_ms=50)
    gap = result["long_gap_examples"][0]
    assert gap["gap_ms"] == 68
    assert gap["ai"]["started_ns"] == ns(64)
    assert gap["previous_publication"]["host_ns"] == ns(25)
    assert gap["latencies_ms"]["waiting_for_submission_after_previous_available_ms"] == 38
    assert gap["latencies_ms"]["dispatch_after_both_submission_and_previous_available_ms"] == 2
    assert gap["latencies_ms"]["completion_to_available_ms"] == 2
    assert gap["latencies_ms"]["available_to_first_publish_ms"] == 7
    assert gap["intervening_publication_count"] == 4
    assert result["metrics"]["pacing_lateness_ms"]["max"] == 1
    assert result["metrics"]["pacing_elapsed_wait_ms"]["max"] == 12


def test_queued_job_separates_pending_busy_time_from_dispatch():
    rows = simple_rows()
    rows[1].update(capture_ns=ns(9), submitted_ns=ns(10), started_ns=ns(26))
    result = cadence.analyze_rows(rows, [shown(1, 25), shown(2, 60)])
    assert result["metrics"]["submit_to_start_ms"]["max"] == 16
    assert result["metrics"]["waiting_for_submission_after_previous_available_ms"]["max"] == 0
    assert result["metrics"]["dispatch_after_both_submission_and_previous_available_ms"]["max"] == 2


def test_capture_returns_deduplicate_source_receipts_without_claiming_native_callbacks():
    present = [shown(1, 25, current_source_frame_id=3, current_source_ns=ns(1)),
               shown(1, 40, current_source_frame_id=3, current_source_ns=ns(1)),
               shown(2, 60, current_source_frame_id=6, current_source_ns=ns(30))]
    result = cadence.analyze_rows(simple_rows(), present, start_ns=0, stop_ns=ns(100))
    assert result["counts"]["returned_sources"] == 2
    assert result["metrics"]["capture_returned_source_interval_ms"]["max"] == 29
    assert result["coverage"]["native_capture_arrived_timestamps"] == "unavailable"


def test_worker_timing_merge_checks_identity_and_preserves_ai_input():
    row = simple_rows()[0]
    timing = dict(row)
    del row["available_ns"]
    merged = cadence.merge_worker_timing([row], [timing])
    assert merged[0]["available_ns"] == ns(24)
    assert "available_ns" not in row
    timing["frame_id"] = 99
    with pytest.raises(cadence.CadenceDataError, match="frame_id"):
        cadence.merge_worker_timing([row], [timing])


def test_worker_file_and_capture_file_are_loaded(tmp_path):
    rows = simple_rows()
    worker = [dict(row) for row in rows]
    for row in rows:
        del row["available_ns"]
    files = {"ai/frames.jsonl": rows, "ai/worker-timing.jsonl": worker,
             "presentation.jsonl": [shown(1, 25), shown(2, 60)],
             "capture-timing.jsonl": [{"outcome": "frame", "frame_id": 3, "capture_ns": ns(1)},
                                       {"outcome": "timeout", "capture_ns": None},
                                       {"outcome": "frame", "frame_id": 6, "capture_ns": ns(30)}]}
    for name, values in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(row) + "\n" for row in values), encoding="utf-8")
    result = cadence.analyze_session(tmp_path, start_ns=0, stop_ns=ns(100))
    assert result["coverage"]["worker_timing_rows"] == 2
    assert result["coverage"]["capture_timing_rows"] == 3
    assert result["coverage"]["availability_origins"] == ["worker-timing.jsonl"]
    assert result["metrics"]["completion_to_available_ms"]["max"] == 3


def test_selected_availability_is_usable_only_for_verified_published_result():
    rows = simple_rows()
    for row in rows:
        del row["available_ns"]
    present = [shown(1, 25, selected_available_ns=ns(24), selected_completed_ns=ns(23)), shown(2, 60)]
    result = cadence.analyze_rows(rows, present)
    assert result["metrics"]["completion_to_available_ms"]["count"] == 1
    assert result["coverage"]["availability_origins"] == ["presentation selected result only"]
    present[0]["selected_available_ns"] = ns(26)
    with pytest.raises(cadence.CadenceDataError, match="availability"):
        cadence.analyze_rows(rows, present)


@pytest.mark.parametrize("case", ["duplicate_sequence", "backwards_ai", "backwards_publish", "publish_before_complete"])
def test_ambiguous_or_impossible_time_join_is_rejected(case):
    rows = simple_rows()
    present = [shown(1, 25), shown(2, 60)]
    if case == "duplicate_sequence":
        rows[1]["completion_sequence"] = 1
    elif case == "backwards_ai":
        rows.reverse()
    elif case == "backwards_publish":
        present.reverse()
    else:
        present[0]["host_ns"] = ns(22)
    with pytest.raises(cadence.CadenceDataError):
        cadence.analyze_rows(rows, present)


def test_empty_logs_and_pacing_clock_reset_do_not_invent_measurements():
    result = cadence.analyze_rows([], [], start_ns=0, stop_ns=0)
    assert result["rates"]["unique_3d_publication_fps"] is None
    assert result["metrics"]["publication_interval_ms"]["status"] == "unavailable"
    result = cadence.analyze_rows(simple_rows(), [shown(1, 25,
        pacing={"entered_ns": 1000, "wake_ns": 100, "clock_resets": 1, "lateness_ns": None})])
    assert result["metrics"]["pacing_elapsed_wait_ms"]["status"] == "unavailable"
    assert result["metrics"]["pacing_lateness_ms"]["status"] == "unavailable"

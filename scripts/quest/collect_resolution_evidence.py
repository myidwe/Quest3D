"""Read-only Quest resolution evidence; requested size never proves decoded crop.

Use the runtime-metrics collector's bounded logcat pattern, retaining codec/latch
records it filters out. This script never changes log buffers, app or settings.
An existing full log can also be inspected without invoking ADB.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import statistics
import subprocess


ROOT = Path(__file__).resolve().parents[2]
PACKAGE = "app.questto3d.client.debug"
PATTERNS = {
    "pinned_profile": r"\[PC PROFILE\] Pinned host Full-SBS (\d+)x(\d+)",
    "stream_request": r"\[STREAM\] start_stream called \((\d+)x(\d+)@(\d+) ([\d.]+)Mbps",
    "codec_initialized": r"Initialized async codec: (\d+)x(\d+)@(\d+) mime=(\S+) name=(\S+)",
    "codec_format_callback": r"Output format changed: (\d+)x(\d+)",
    "external_frame": r"External SurfaceTexture frame #(\d+): (\d+)x(\d+) pts=(-?\d+)",
    "latch": r"SurfaceTexture latch: valid=(\d+) decoder=(\d+) frame=(\d+) pts=(-?\d+) timestamp_ns=(-?\d+) lease=(\d+)",
    "submission": r"Quest3DSubmission cycle=(\d+) frame=(\d+) decoder=(\d+) lease=(\d+) success=1",
    "runtime_stats": r"app=([\d.]+)fps video_update=([\d.]+)fps",
}


def inspect_log(text, expected):
    records = {key: [] for key in PATTERNS}
    failures = []
    for number, line in enumerate(text.splitlines(), 1):
        for key, pattern in PATTERNS.items():
            found = re.search(pattern, line)
            if found:
                records[key].append({"line": number, "values": list(found.groups()), "text": line})
        if re.search(r"SCRIPT ERROR|Fatal signal|Async codec error=|AMediaCodec_.*failed|swapchain.*failed", line):
            failures.append({"line": number, "text": line})
    requested = records["stream_request"][-1] if records["stream_request"] else None
    boundary = requested["line"] if requested else None
    # A same-PID log segment is useful evidence but lacks the typed crop and
    # codec/transport bindings exposed only by the installed Stream UI getter.
    segment = lambda key: [r for r in records[key] if boundary is not None and r["line"] >= boundary]
    outputs = segment("codec_format_callback")
    latest_output = outputs[-1] if outputs else None
    latches = segment("latch")
    submissions = segment("submission")
    valid_latches = {(r["values"][1], r["values"][2], r["values"][5])
                     for r in latches if r["values"][0] == "1"}
    matching = [r for r in submissions if (r["values"][2], r["values"][1], r["values"][3]) in valid_latches]
    stats = segment("runtime_stats")
    def distribution(index):
        values = [float(r["values"][index]) for r in stats]
        return {"min": min(values), "median": statistics.median(values), "max": max(values)} if values else None
    def matches(record):
        return bool(record and [int(v) for v in record["values"][:2]] == expected)
    return {
        "expected_packed": expected,
        "last_pinned_profile": records["pinned_profile"][-1] if records["pinned_profile"] else None,
        "last_stream_request": requested,
        "latest_output_callback_after_last_request": latest_output,
        "requested_size_matches": matches(requested),
        "logged_callback_dimensions_match": matches(latest_output),
        "current_request_segment_latches": len(latches),
        "current_request_segment_successful_submissions": len(submissions),
        "matching_latch_submission_tokens": len(matching),
        "app_fps": distribution(0), "video_update_fps": distribution(1),
        "errors_in_log": failures,
        "records": records,
        "crop_and_codec_lifetime_verified": False,
        "transport_bound_eye_rect_verified": False,
        "actual_visible_eye_size_verified": False,
        "physical_sharpness_verified": False,
        "limits": [
            "Log dimensions are not the valid crop/lifetime snapshot; newer malformed callbacks may retain logged width/height.",
            "Log order and decoder/frame/lease numbers do not prove a full transport binding or current eye geometry.",
            "Missing startup records or first-three lifetime frame logs stay unknown; no fallback to requested dimensions.",
            "App/video_update counters are not unique source/AI FPS, photons, delay or sustained decode throughput.",
            "Verify Stream Received/Eye/Native and its matching-submission tooltip; compare physical sharpness separately.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--adb", type=Path)
    source.add_argument("--input", type=Path, help="Existing unfiltered same-process logcat/debug log")
    parser.add_argument("--serial")
    parser.add_argument("--seconds", type=int, choices=range(5, 61), default=45)
    parser.add_argument("--eye-width", type=int, default=1600)
    parser.add_argument("--eye-height", type=int, default=900)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.eye_width <= 4096 or not 1 <= args.eye_height <= 4096:
        parser.error("Invalid bounded eye dimensions")
    output = args.output.resolve()
    output.relative_to((ROOT / "artifacts").resolve())
    output.mkdir(parents=True, exist_ok=False)
    record = {"started_at": dt.datetime.now().astimezone().isoformat(), "read_only": True,
              "package": PACKAGE, "source": "existing_log" if args.input else "bounded_logcat",
              "collection_completed": False}
    raw, errors = b"", b""
    try:
        if args.input:
            raw = args.input.resolve(strict=True).read_bytes()
            record["input"] = str(args.input.resolve())
        else:
            adb = str(args.adb.resolve(strict=True))
            prefix = [adb, "-s", args.serial] if args.serial else [adb]
            def pid():
                value = subprocess.check_output(prefix + ["shell", "pidof", PACKAGE], timeout=10).decode().strip()
                if not re.fullmatch(r"[1-9]\d*", value):
                    raise RuntimeError("Exactly one running Quest3D process is required")
                return value
            before = pid()
            record.update(pid_before=before, serial=args.serial, requested_seconds=args.seconds)
            child = subprocess.Popen(prefix + ["logcat", "-T", "1", "--pid=" + before, "-v", "threadtime"],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            timed_out = False
            try:
                raw, errors = child.communicate(timeout=args.seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                child.terminate()  # Our read-only logcat client only; no app/server termination.
                try:
                    raw, errors = child.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    raw, errors = child.communicate(timeout=5)
                    raise RuntimeError("Read-only logcat client needed forced cleanup")
            after = pid()
            record.update(pid_after=after, bounded_window_finished=timed_out,
                          process_unchanged=record["pid_before"] == after)
            if not timed_out:
                raise RuntimeError("Logcat ended before the requested observation window")
        record.update(inspect_log(raw.decode("utf-8", "replace"), [args.eye_width * 2, args.eye_height]))
        record["collection_completed"] = True
    except BaseException as error:
        record["collection_error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        record.update(ended_at=dt.datetime.now().astimezone().isoformat(),
                      log_sha256=hashlib.sha256(raw).hexdigest(),
                      stderr=errors.decode("utf-8", "replace")[:2048])
        (output / "logcat.txt").write_bytes(raw)
        (output / "evidence.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in record.items() if key != "records"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

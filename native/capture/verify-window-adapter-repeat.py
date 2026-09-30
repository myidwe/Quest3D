"""Run owned-HWND adapter lifecycle repeatedly in the same interpreter."""
import argparse
import contextlib
import io
import gc
import json
from pathlib import Path
import runpy
import sys
import time

import psutil

parser = argparse.ArgumentParser()
parser.add_argument("--repeat", type=int, default=5)
parser.add_argument("--output-directory", type=Path, required=True)
args = parser.parse_args()
assert 1 <= args.repeat <= 100
args.output_directory.mkdir(parents=True, exist_ok=False)
records = []
for iteration in range(args.repeat):
    output = args.output_directory / f"iteration-{iteration:03}.json"
    sys.argv = ["verify-window-adapter.py", "--output", str(output)]
    began = time.perf_counter()
    log = io.StringIO()
    original_hook = sys.excepthook
    try:
        with contextlib.redirect_stdout(log):
            runpy.run_path(str(Path(__file__).with_name("verify-window-adapter.py")), run_name="__main__")
    finally:
        sys.excepthook = original_hook
        output.with_suffix(".stdout.txt").write_text(log.getvalue(), encoding="utf-8")
    gc.collect()  # Release the runpy test namespace; never collect a live worker.
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["worker_mapper_tracker_cleaned_up"] and result["foreground_unchanged"]
    row = dict(iteration=iteration, elapsed_seconds=time.perf_counter() - began,
        process_threads=psutil.Process().num_threads(), process_handles=psutil.Process().num_handles(),
        rss_mib=psutil.Process().memory_info().rss / 1048576)
    records.append(row)
    print(json.dumps(row), flush=True)
    (args.output_directory / "summary.json").write_text(json.dumps(dict(records=records,
        passed=len(records) == args.repeat, actual_capture=True, ai_inference=False, quest_verified=False,
        resource_stability_verified=False),
        indent=2), encoding="utf-8")

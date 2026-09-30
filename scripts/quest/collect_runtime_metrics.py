"""Read bounded metrics from the running Quest3D process; no device mutation."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import statistics
import subprocess


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--adb', type=Path, required=True)
    p.add_argument('--seconds', type=int, default=30, choices=range(5, 61))
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    if args.output.exists():
        raise SystemExit('Preserve the existing evidence directory; choose a new output')
    adb = str(args.adb.resolve(strict=True))
    package = 'app.questto3d.client.debug'
    def query(*command):
        return subprocess.check_output([adb, *command], text=True, encoding='utf-8', errors='replace', timeout=10).strip()
    pid = query('shell', 'pidof', package)
    if not re.fullmatch(r'\d+', pid):
        raise SystemExit('Exactly one running Quest3D process is required')
    started = dt.datetime.now().astimezone().isoformat()
    child = subprocess.Popen([adb, 'logcat', '-T', '1', '--pid='+pid, '-v', 'threadtime'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    timed_out = False
    try:
        raw, errors = child.communicate(timeout=args.seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        child.terminate()  # Only our read-only logcat subprocess, never adb server/app.
        raw, errors = child.communicate(timeout=5)
    wanted = re.compile(r'\[STATS\]|\[REFRESH\]|\[STREAM\] start_stream called|Quest3DSubmission|Quest3DCodecCaps|Quest3DColor|Video transfer changed:|warp.*ms|SCRIPT ERROR|Fatal signal')
    lines = [line for line in raw.decode('utf-8', 'replace').splitlines() if wanted.search(line)]
    stats = []
    for line in lines:
        m = re.search(r'app=([\d.]+)fps video_update=([\d.]+)fps', line)
        if m:
            stats.append({'app_fps':float(m[1]), 'video_update_fps':float(m[2])})
    def summary(key):
        values = [s[key] for s in stats]
        return {'minimum':min(values),'median':statistics.median(values),'maximum':max(values)} if values else None
    ended = dt.datetime.now().astimezone().isoformat()
    try:
        after = query('shell', 'pidof', package)
    except (subprocess.SubprocessError, OSError):
        after = None
    text = '\n'.join(lines)+'\n'
    record = {
        'started_at':started,'ended_at':ended,'requested_seconds':args.seconds,
        'package':package,'pid_before':int(pid),'pid_after':after,
        'bounded_collection_finished':timed_out, 'sample_count':len(stats),
        'app_fps':summary('app_fps'),'video_update_fps':summary('video_update_fps'),
        'script_errors_or_fatal_signals':[line for line in lines if re.search(r'SCRIPT ERROR|Fatal signal',line)],
        'stderr':errors.decode('utf-8','replace')[:1024],
        'log_sha256':hashlib.sha256(text.encode('utf-8')).hexdigest(),
        'scope':'Godot process/video consume counters; not photons, unique AI frames, complete decode rate, latency or audio quality',
    }
    args.output.mkdir(parents=True)
    (args.output/'metrics.log').write_text(text, encoding='utf-8')
    (args.output/'summary.json').write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(record,ensure_ascii=False,indent=2))


if __name__ == '__main__':
    main()

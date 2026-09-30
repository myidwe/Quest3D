"""Run only a named self-owned diagnostic executable, never the installed app."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import resource
import re
import signal
import subprocess
import sys
import time

p = argparse.ArgumentParser()
p.add_argument('variant', choices=('baseline', 'fixed'))
p.add_argument('--binary-dir', type=Path, default=Path('/mnt/a/ai/quest_to_3d/artifacts/quest/render-target-ownership'))
p.add_argument('--evidence-suffix', default='')
args = p.parse_args()
base = Path('/mnt/a/ai/quest_to_3d/artifacts/quest/render-target-ownership')
env = os.environ.copy()
env.update(LIBGL_ALWAYS_SOFTWARE='true', GALLIUM_DRIVER='llvmpipe',
    DISPLAY='', WAYLAND_DISPLAY='',
    XDG_DATA_HOME=str(base / 'xdg-data'), XDG_CONFIG_HOME=str(base / 'xdg-config'))
start = time.monotonic()
log = base / (args.variant + args.evidence_suffix + '-run.log')
assert not log.exists(), 'Preserve prior failure/success evidence; choose a new evidence path for repetition.'
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
with log.open('wb') as stream:
    child = subprocess.Popen([str(args.binary_dir / args.variant), str(args.binary_dir / 'empty-project')], env=env,
        stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
    try:
        code, timeout = child.wait(timeout=40), False
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGKILL)
        child.wait()
        code, timeout = None, True
summary = {'variant': args.variant, 'returncode': code, 'timeout': timeout, 'seconds': time.monotonic()-start,
           'backend': 'Mesa llvmpipe software EGL, no Quest/OpenXR or NVIDIA', 'log': str(log)}
content = log.read_text(errors='replace')
summary.update(binary_sha256=hashlib.sha256((args.binary_dir / args.variant).read_bytes()).hexdigest(),
    owned_rid_preserved_cycles=len(re.findall(r'cache_hit_owned_preserved=1', content)),
    cached_names_deleted_cycles=len(re.findall(r'cached_gl_names_deleted=2', content)),
    external_retired_cycles=len(re.findall(r'external_retired=1 old_owned_live=1', content)),
    utilities_tracking_warning_count=content.count('leaked 16384 bytes.'))
if args.variant == 'fixed':
    summary['passed'] = not timeout and code == 0 and summary['owned_rid_preserved_cycles'] == 40 and summary['cached_names_deleted_cycles'] == 40 and summary['external_retired_cycles'] == 40
else:
    summary['reproduced_expected_crash'] = not timeout and code in (-6, -11) and 'cache_hit_owned_preserved=0' in content and '_clear_render_target' in content and 'render_target_set_size' in content
(base / (args.variant + args.evidence_suffix + '-result.json')).write_text(json.dumps(summary, indent=2)+'\n')
print(json.dumps(summary))
print(content[-5000:])
sys.exit(0 if summary.get('passed', summary.get('reproduced_expected_crash', False)) else 1)

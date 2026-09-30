"""Apply the tracked patch to the pinned original file set, without editing either checkout."""
import hashlib
import json
import os
from pathlib import Path
import subprocess

scripts = Path(__file__).resolve().parent
project = scripts.parents[1]
source = project / 'third_party/nightfall'
commit = json.loads((scripts / 'versions.lock.json').read_text())['nightfall']['commit']
patch = scripts / 'nightfall-pc-sbs.patch'
patch_hash = hashlib.sha256(patch.read_bytes()).hexdigest()
cache = Path(os.environ['QUEST_CACHE']).resolve()
destination = cache / ('patch-validation-' + patch_hash[:12])
if destination.exists():
    raise SystemExit(f'Validation directory already exists; preserve it and inspect {destination}')
destination.mkdir(parents=True)
files = [line[6:].split(' b/', 1)[1] for line in patch.read_text().splitlines() if line.startswith('diff --git ')]
for name in files:
    original = subprocess.run(['git', '-C', str(source), 'show', f'{commit}:{name}'], capture_output=True)
    if original.returncode == 0:
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(original.stdout)
        mode = subprocess.check_output(['git', '-C', str(source), 'ls-tree', commit, '--', name], text=True).split()[0]
        if mode == '100755': path.chmod(0o755)
subprocess.run(['git', 'apply', '--check', str(patch)], cwd=destination, check=True)
subprocess.run(['git', 'apply', str(patch)], cwd=destination, check=True)
for name in files:
    assert (destination / name).read_text(encoding='utf-8') == (source / name).read_text(encoding='utf-8'), name
print(f'Patch {patch_hash}: {len(files)} files reproduce current reviewed source at {commit}')

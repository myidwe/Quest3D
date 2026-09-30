"""Prepare pinned, project-specific WSL dependencies without apt installation."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import tarfile
import zipfile

SCRIPTS = Path(__file__).resolve().parent
PROJECT = SCRIPTS.parents[1]
CACHE = Path(os.environ['QUEST_CACHE']).expanduser().resolve()
if CACHE in (Path('/'), Path.home().resolve()) or not CACHE.is_relative_to(Path.home().resolve()):
    raise RuntimeError('QUEST_BUILD_CACHE must be a dedicated directory inside the WSL user home')
CACHE.mkdir(parents=True, exist_ok=True)
if CACHE.stat().st_uid != os.getuid():
    raise RuntimeError('Build cache must be owned by the current WSL user')
DOWNLOADS = CACHE / 'downloads'
DOWNLOADS.mkdir(exist_ok=True)
LOCK = json.loads((SCRIPTS / 'versions.lock.json').read_text())


def run(*args, cwd=None):
    subprocess.run([str(a) for a in args], cwd=cwd, check=True)


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def fetch(asset):
    target = DOWNLOADS / asset['name']
    # Optional reuse of first-build archives; not required on a fresh checkout.
    if not target.exists():
        for folder in ('downloads', 'debs'):
            old = PROJECT / '.tools/quest' / folder / asset['name']
            if old.is_file() and digest(old) == asset['sha256']:
                shutil.copyfile(old, target)
                break
    if not target.exists():
        partial = target.with_name(target.name + '.partial')
        run('curl', '-fL', '--retry', '3', '--silent', '--show-error', asset['url'], '-o', partial)
        if digest(partial) != asset['sha256']:
            raise RuntimeError(f'Checksum mismatch: {partial}; preserved for investigation')
        partial.replace(target)
    if target.stat().st_size != asset['bytes'] or digest(target) != asset['sha256']:
        raise RuntimeError(f'Checksum/size mismatch: {target}; refusing to overwrite')
    print('Verified', target.name, flush=True)
    return target


def inside(destination, name):
    path = destination / name
    if not path.resolve().is_relative_to(destination.resolve()):
        raise RuntimeError(f'Unsafe archive path: {name}')
    return path


def extract_zip(archive, destination, prefix='', select=None):
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as source:
        for info in source.infolist():
            if not info.filename.startswith(prefix) or (select and info.filename not in select):
                continue
            name = info.filename[len(prefix):]
            if not name:
                continue
            target = inside(destination, name)
            mode = info.external_attr >> 16
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if stat.S_ISLNK(mode):
                link = source.read(info).decode()
                inside(destination, str(target.parent.relative_to(destination) / link))
                if not target.is_symlink():
                    target.symlink_to(link)
            else:
                with source.open(info) as stream, target.open('wb') as out:
                    shutil.copyfileobj(stream, out)
                if mode:
                    target.chmod(mode & 0o777)


def extract_tar(archive, destination):
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as source:
        members = source.getmembers()
        for entry in members:
            inside(destination, entry.name)
            if entry.isdev() or entry.isfifo():
                raise RuntimeError('Archive contains device or FIFO')
            if entry.issym():
                inside(destination, str(PurePosixPath(entry.name).parent / entry.linkname))
            elif entry.islnk():
                inside(destination, entry.linkname)
        source.extractall(destination, members=members)


def checkout(name, destination):
    pin = LOCK[name]
    if not (destination / '.git').exists():
        if destination.exists():
            raise RuntimeError(f'Refusing to replace non-Git source directory: {destination}')
        run('git', 'clone', '--filter=blob:none', '--no-checkout', pin['url'], destination)
        run('git', '-C', destination, 'config', 'core.autocrlf', 'false')
        run('git', '-C', destination, 'fetch', '--depth=1', 'origin', pin['commit'])
        run('git', '-C', destination, 'checkout', '--detach', pin['commit'])
    actual = subprocess.check_output(['git', '-C', str(destination), 'rev-parse', 'HEAD'], text=True).strip()
    if actual != pin['commit']:
        raise RuntimeError(f'{name} source is at {actual}, expected {pin["commit"]}; preserving it')


checkout('nightfall', CACHE / 'source')
patch = SCRIPTS / 'nightfall-pc-sbs.patch'
apply = ['git', '-C', str(CACHE / 'source'), 'apply']
if subprocess.run(apply + ['--check', str(patch)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
    run(*apply, patch)
elif subprocess.run(apply + ['--reverse', '--check', str(patch)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0:
    raise RuntimeError('Nightfall patch conflicts with current source. Preserve edits and resolve explicitly.')

for asset in json.loads((SCRIPTS / 'downloads.lock.json').read_text())['assets']:
    archive = fetch(asset)
    name = archive.name
    marker = CACHE / ('.prepared-' + name + '.sha256')
    if marker.exists() and marker.read_text().strip() == asset['sha256']:
        continue
    if name.startswith('OpenJDK'):
        if not (CACHE / 'linux/jdk-17.0.20.1+1/release').is_file():
            extract_tar(archive, CACHE / 'linux')
    elif name.startswith('uv-'):
        if not (CACHE / 'linux/uv-x86_64-unknown-linux-gnu/uv').is_file():
            extract_tar(archive, CACHE / 'linux')
    elif name.endswith('.tpz'):
        extract_zip(archive, CACHE / 'godot-templates', select={'templates/android_source.zip', 'templates/version.txt'})
    elif name.startswith('Godot_'):
        if not (CACHE / 'linux/Godot_v4.7-stable_linux.x86_64').is_file():
            extract_zip(archive, CACHE / 'linux')
    elif name.startswith('godotopenxr'):
        extract_zip(archive, CACHE / 'source', prefix='asset/')
    elif name.startswith('commandline'):
        if not (CACHE / 'android-sdk/cmdline-tools/19.0/bin/sdkmanager').is_file():
            extract_zip(archive, CACHE / 'android-sdk/cmdline-tools/19.0', prefix='cmdline-tools/')
    elif name.startswith('android-ndk'):
        if not (CACHE / 'android-sdk/ndk/29.0.14206865/source.properties').is_file():
            extract_zip(archive, CACHE / 'android-sdk/ndk/29.0.14206865', prefix='android-ndk-r29/')
    elif name.endswith('.deb'):
        run('dpkg-deb', '-x', archive, CACHE / 'sysroot')
    else:
        raise RuntimeError(f'Unrecognized archive: {name}')
    marker.write_text(asset['sha256'] + '\n')

uv = CACHE / 'linux/uv-x86_64-unknown-linux-gnu/uv'
if not (CACHE / 'venv/bin/python').exists():
    run(uv, 'venv', '--no-project', '--python', '/usr/bin/python3', CACHE / 'venv')
run(uv, 'pip', 'install', '--python', CACHE / 'venv/bin/python',
    'cmake==' + LOCK['cmake'], 'ninja==' + LOCK['ninja'], 'scons==' + LOCK['scons'])
checkout('vcpkg', CACHE / 'vcpkg')
for directory in ('vcpkg-registries', 'vcpkg-binaries', 'xdg-data', 'xdg-config', 'gradle'):
    (CACHE / directory).mkdir(exist_ok=True)
(PROJECT / '.tools/quest').mkdir(parents=True, exist_ok=True)
(PROJECT / '.tools/quest/ext4-cache.path').write_text(str(CACHE) + '\n')
print('Pinned tools and patched sources ready:', CACHE)

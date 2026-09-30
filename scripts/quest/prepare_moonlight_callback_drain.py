"""Apply/verify the local callback patch in a private test tree, never vcpkg cache."""
from pathlib import Path
import difflib
import hashlib
import shutil
import subprocess
import sys
import tarfile

root = Path(__file__).resolve().parents[2]
downloads = Path.home() / ".cache/quest_to_3d-quest-build/vcpkg/downloads"
overlay = root / "third_party/nightfall/addons/nightfall-stream/vcpkg-overlay/moonlight-common-c"
out = root / "artifacts/quest/moonlight-callback-drain/source"
if '--output' in sys.argv:
    out = (root / sys.argv[sys.argv.index('--output') + 1]).resolve()
    if not out.is_relative_to(root / 'artifacts/quest'):
        raise ValueError('Private fixture output must stay inside artifacts/quest')
patch = overlay / "0003-quest3d-connection-callback-drain.patch"

archives = [
    ('moonlight-stream-moonlight-common-c-7b026e77be62175104640e7e722b758df6d3d0d7.tar.gz',
     '116340530ed2f431af345bad93f4be0168a04f1846ed5e17d352a46121be6bcc2c8e24d19e74eb40aa3bf5066e694ea6a28f4d63adfa5e74104c25bd2297d4d5'),
    ('cgutman-enet-dea6fb5414b180908b58c0293c831105b5d124dd.tar.gz',
     'c072acff252e495032ddf2f2a414a3644d7d4ca3abf6b01204021a61dfc19cf9a621ab1dface47ebb337f0454cacb8fee877a564b782b49fb868b5b95e7f36ae'),
]
for name, digest in archives:
    if hashlib.sha512((downloads / name).read_bytes()).hexdigest() != digest:
        raise ValueError(f'Pinned archive SHA512 mismatch: {name}')

def archive_file(relative):
    with tarfile.open(downloads / archives[0][0]) as archive:
        members = [m for m in archive.getmembers() if m.name.endswith('/' + relative)]
        assert len(members) == 1 and members[0].isfile()
        return archive.extractfile(members[0]).read().decode('utf-8')

def extract_pinned(name, destination):
    destination = destination.resolve()
    with tarfile.open(downloads / name) as archive:
        members = archive.getmembers()
        assert len({Path(m.name).parts[0] for m in members}) == 1
        for member in members:
            relative = Path(*Path(member.name).parts[1:])
            target = (destination / relative).resolve()
            if not target.is_relative_to(destination):
                raise ValueError('Archive path escapes fixture')
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.extractfile(member).read())
                target.chmod(member.mode & 0o777)
            else:
                raise ValueError(f'Unexpected nonregular archive entry: {member.name}')

def replace_once(text, old, new):
    assert text.count(old) == 1, (old[:70], text.count(old))
    return text.replace(old, new, 1)

def apply_checked(candidate, reverse=False):
    # The checkout uses Windows line endings; vcpkg's Linux port copy does not.
    # Feed canonical patch text without mutating either source copy.
    data = candidate.read_text(encoding='utf-8').encode('utf-8')
    flags = ['--reverse'] if reverse else []
    subprocess.run(['git', 'apply', *flags, '--check', '-'], cwd=out, input=data, check=True)
    if not reverse:
        subprocess.run(['git', 'apply', '-'], cwd=out, input=data, check=True)

if "--generate" in sys.argv:
    base = archive_file('src/Connection.c')
    updated = replace_once(base,
        'static ConnListenerConnectionTerminated originalTerminationCallback;\nstatic bool alreadyTerminated;\nstatic PLT_THREAD terminationCallbackThread;\nstatic int terminationCallbackErrorCode;',
        '#include "ConnectionCallbackDrain.h"')
    updated = replace_once(updated, '    ConnectionInterrupted = true;\n}',
        '    ConnectionInterrupted = true;\n    ClDrainDisable();\n}')
    updated = replace_once(updated, '    alreadyTerminated = true;\n\n    // Set the interrupted flag',
        '    ClDrainDisable();\n\n    // Set the interrupted flag')
    start = updated.index('static void terminationCallbackThreadFunc(')
    end = updated.index('static bool parseRtspPortNumberFromUrl', start)
    updated = updated[:start] + '''// Call only from the serialized lifecycle owner after LiStartConnection and
// LiStopConnection have returned. The task may itself call LiStopConnection.
int LiWaitForConnectionCallbacks(void)
{
    if (stage != STAGE_NONE) {
        return CL_DRAIN_BUSY;
    }
    return ClDrainWait();
}

static void ClInternalConnectionTerminated(int errorCode)
{
    int err = ClDrainIssue(errorCode);
    if (err != 0) {
        Limelog("Failed to create termination thread: %d\\n", err);
    }
}

''' + updated[end:]
    updated = replace_once(updated, '    int err;\n\n    if (drCallbacks != NULL',
        '''    int err;

    // Do not install callbacks for a replacement while an old task can still
    // enter client code. This rejection must not clean up an existing owner.
    err = ClDrainCanBegin();
    if (err != 0 || stage != STAGE_NONE) {
        return err != 0 ? err : CL_DRAIN_BUSY;
    }

    if (drCallbacks != NULL''')
    updated = replace_once(updated,
        '    originalTerminationCallback = clCallbacks->connectionTerminated;',
        '    ClDrainEnable(clCallbacks->connectionTerminated);')
    updated = replace_once(updated, '    alreadyTerminated = false;\n', '')
    header_base = archive_file('src/Limelight.h')
    header_updated = replace_once(header_base, 'void LiStopConnection(void);', '''void LiStopConnection(void);

// Quest3D pinned extension. A serialized lifecycle owner must call this after
// LiStartConnection and LiStopConnection return, before replacing/freeing callback
// state. Zero means the owned termination task is absent or actually joined.
// Never call from a connection callback. Nonzero refuses a new connection; a
// thread create/join failure remains sticky. This is not a GPU/audio drain.
#define LI_HAS_CONNECTION_CALLBACK_DRAIN 1
int LiWaitForConnectionCallbacks(void);''')
    chunks = []
    for name, old, new in [("Connection.c", base, updated), ("Limelight.h", header_base, header_updated)]:
        chunks.extend(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
            fromfile=f"a/src/{name}", tofile=f"b/src/{name}"))
    patch.write_text("".join(chunks), encoding="utf-8", newline="\n")

if out.exists():
    # Never overwrite an existing test tree with an unknown patched source.
    apply_checked(patch, reverse=True)
else:
    # vcpkg may already have patched its mutable source after an app build.
    # Always reconstruct the fixture from verified immutable source archives.
    extract_pinned(archives[0][0], out)
    extract_pinned(archives[1][0], out / 'enet')
    for patch_name in ['0001-add-install-rules.patch', '0002-fix-clang-multiversioning-headers.patch', patch.name]:
        candidate = overlay / patch_name
        apply_checked(candidate)
shutil.copy2(overlay / "quest3d/ConnectionCallbackDrain.h", out / "src/ConnectionCallbackDrain.h")
print(out)

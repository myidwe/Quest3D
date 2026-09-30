"""Export owned host changes and verify both patches against clean pinned file contents."""
from pathlib import Path
import difflib
import re
import subprocess
import uuid

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "third_party/sunshine"
PIN = "cb72dffa3233c5815cd5ba88f09f049dd679ba75"
PATCH = ROOT / "patches/sunshine/0001-quest3d-external-frame-source.patch"


def git(*args, cwd=SOURCE, check=True):
    return subprocess.run(["git", "-c", "core.excludesFile=" + str(ROOT / "artifacts/host/empty-git-ignore"),
                           *args], cwd=cwd, check=check, capture_output=True)


def paths(patch):
    return re.findall(r"^diff --git a/(\S+) b/\S+$", patch.read_text(encoding="utf-8"), re.MULTILINE)


def main():
    assert git("rev-parse", "HEAD").stdout.decode().strip() == PIN
    owned = sorted(set(paths(PATCH)) | {"src/platform/common.h", "src/video.h", "src/rtsp.h",
        "src/quest3d_frame_ledger.h", "src/quest3d_frame_metadata.h", "tests/unit/test_quest3d_frame_ledger.cpp", "src/thread_safe.h", "src/stream.h",
        "src/platform/windows/quest3d_pointer.h", "src/platform/windows/quest3d_pointer.cpp",
        "tests/unit/platform/windows/test_quest3d_pointer.cpp", "tests/unit/platform/windows/test_quest3d_pointer_sink.cpp", "src/quest3d_source_identity.h", "tests/unit/platform/windows/quest3d_pointer_fixture.h", "tests/unit/platform/windows/test_quest3d_pointer_runtime.cpp"})
    text = ""
    for relative in owned:
        if git("ls-files", "--error-unmatch", relative, check=False).returncode == 0:
            text += git("diff", "--no-ext-diff", "--binary", "HEAD", "--", relative).stdout.decode("utf-8")
        else:
            contents = (SOURCE / relative).read_text(encoding="utf-8").splitlines(keepends=True)
            text += f"diff --git a/{relative} b/{relative}\nnew file mode 100644\n"
            text += "".join(difflib.unified_diff([], contents, "/dev/null", "b/" + relative))
    PATCH.write_text(text, encoding="utf-8", newline="\n")
    audio = ROOT / "patches/sunshine/0002-quest3d-audio.patch"
    for patch in (PATCH, audio): git("apply", "--reverse", "--check", str(patch))
    directory = ROOT / "artifacts/host" / ("patch-reproduction-" + uuid.uuid4().hex)
    directory.mkdir()
    all_paths = sorted(set(paths(PATCH) + paths(audio)))
    for relative in all_paths:
        original = git("show", PIN + ":" + relative, check=False)
        if original.returncode == 0:
            destination = directory / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(original.stdout)
    for patch in (PATCH, audio):
        git("apply", "--check", str(patch), cwd=directory)
        git("apply", str(patch), cwd=directory)
    for relative in all_paths:
        assert (directory / relative).read_bytes().replace(b"\r\n", b"\n") == (SOURCE / relative).read_bytes().replace(b"\r\n", b"\n"), relative
    print(f"Host files={len(owned)}; both patches reverse/forward checked; {len(all_paths)} file contents matched: {directory}")


if __name__ == "__main__": main()

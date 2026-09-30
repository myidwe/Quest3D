"""Export only the Quest3D audio-owned changes without staging or modifying sources."""

from pathlib import Path
import difflib
import subprocess

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "third_party/sunshine"
PIN = "cb72dffa3233c5815cd5ba88f09f049dd679ba75"
TRACKED = [
    "src/audio.cpp",
    "src/audio.h",
    "src/platform/windows/audio.cpp",
    "tests/unit/test_audio.cpp",
]
NEW = [
    "src/audio_delay.h",
    "src/audio_route.h",
    "src/platform/windows/audio_route_journal.h",
    "src/platform/windows/audio_watchdog.h",
    "src/quest3d_audio_ring.h",
    "src/quest3d_audio_packet.h",
    "src/quest3d_file_transport.h",
    "src/platform/windows/quest3d_audio_reader.h",
    "tests/unit/test_quest3d_audio_ring.cpp",
    "tests/integration/test_quest3d_audio_pipeline.cpp",
    "tests/integration/test_quest3d_file_transport.cpp",
    "tests/unit/platform/windows/quest3d_pcm_fixture.h",
    "tests/unit/platform/windows/test_quest3d_reader_retirement.cpp",
    "tests/unit/platform/windows/test_quest3d_file_transport_identity.cpp",
]


def git(*args: str) -> str:
    return subprocess.check_output(
        ["git", "-c", "core.excludesFile=" + str(ROOT / "artifacts/host/empty-git-ignore"), "-C", str(SOURCE), *args], encoding="utf-8"
    )


if __name__ == "__main__":
    if git("rev-parse", "HEAD").strip() != PIN:
        raise SystemExit("Refusing to export from an unexpected Sunshine revision")
    patch = git("diff", "--no-ext-diff", "--binary", "HEAD", "--", *TRACKED)
    for relative in NEW:
        contents = (SOURCE / relative).read_text(encoding="utf-8").splitlines(keepends=True)
        patch += f"diff --git a/{relative} b/{relative}\nnew file mode 100644\n"
        patch += "".join(difflib.unified_diff([], contents, "/dev/null", f"b/{relative}"))
    output = ROOT / "patches/sunshine/0002-quest3d-audio.patch"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(patch, encoding="utf-8", newline="\n")
    git("apply", "--reverse", "--check", str(output))
    print(f"Exported and reverse-checked {output}")

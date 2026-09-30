"""Build a separate tiny color probe from pinned production objects, without modifying them."""
from __future__ import annotations
import argparse
import ctypes as C
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import uuid

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "third_party/sunshine"
BUILD = SOURCE / "cmake-build-quest3d"
EXPECTED_TEST = "140e4420106243486a93e0cbdbd83db7ed5b80746dd47632edf7446467e72714"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def split_windows(command):
    shell = C.WinDLL("shell32")
    shell.CommandLineToArgvW.argtypes = [C.c_wchar_p, C.POINTER(C.c_int)]
    shell.CommandLineToArgvW.restype = C.POINTER(C.c_wchar_p)
    kernel = C.WinDLL("kernel32")
    kernel.LocalFree.argtypes = [C.c_void_p]
    count = C.c_int()
    values = shell.CommandLineToArgvW(command, C.byref(count))
    if not values:
        raise C.WinError()
    try:
        return [values[i] for i in range(count.value)]
    finally:
        kernel.LocalFree(values)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    assert output.is_relative_to(ROOT / "artifacts"), "Keep all probe outputs inside project artifacts"
    output.mkdir(parents=True, exist_ok=False)
    assert sha(BUILD / "tests/test_sunshine.exe") == EXPECTED_TEST, "Re-review a changed native build before linking"
    private_prefix = "Local\\Quest3D.ColorProbe." + uuid.uuid4().hex
    source_reader = SOURCE / "src/platform/windows/quest3d_frame.cpp"
    source_test = SOURCE / "tests/integration/test_quest3d_pipeline.cpp"
    reader = source_reader.read_text(encoding="utf-8")
    needle = "object_names protocol_names(std::uint32_t protocol, const std::wstring &prefix) {"
    assert reader.count(needle) == 1
    # Harness-only namespace substitution. The actual reader validation, copying,
    # lifetime checks, production SourceProvider, D3D conversion and NVENC remain.
    reader = reader.replace(needle, needle.replace("&prefix", "&requested_prefix") + "\n"
        + '    const std::wstring prefix = L"' + private_prefix.replace("\\", "\\\\") + '";\n'
        + '    if (requested_prefix != L"Local\\\\Quest3D.Frame" && requested_prefix != prefix) {\n'
        + '      throw std::invalid_argument("Color probe rejects any other namespace");\n    }')
    harness = source_test.read_text(encoding="utf-8")
    replacements = {
        "TEST(Quest3DExternalSource, RealPublisherToNvenc)": "TEST(Quest3DColorProbe, NeutralFixtureNvenc)",
        "index <= 24": "index <= 4",
        "ASSERT_EQ(index, 25U)": "ASSERT_EQ(index, 5U)",
        "24 distinct valid frames within 15 seconds": "4 distinct valid frames within 5 seconds",
        "std::chrono::seconds(15)": "std::chrono::seconds(5)",
    }
    for old, new in replacements.items():
        assert harness.count(old) == 1, old
        harness = harness.replace(old, new)
    copied_reader = output / "private_quest3d_frame.cpp"
    copied_test = output / "color_pipeline_test.cpp"
    copied_reader.write_text(reader, encoding="utf-8")
    copied_test.write_text(harness, encoding="utf-8")
    compilation = json.loads((BUILD / "compile_commands.json").read_text())
    compiled = {}
    commands = []
    compiler = None
    env = dict(os.environ)
    env["PATH"] = str(ROOT / "native/host/tools/msys64/ucrt64/bin") + os.pathsep + env["PATH"]
    for original, copied in ((source_reader, copied_reader), (source_test, copied_test)):
        entries = [e for e in compilation if Path(e["file"]).resolve() == original.resolve()
                   and "test_sunshine.dir" in e["output"]]
        assert len(entries) == 1
        entry = entries[0]
        command = split_windows(entry["command"])
        compiler = command[0]
        obj = output / (copied.stem + ".obj")
        old_obj = command.index("-o") + 1
        command[old_obj] = str(obj)
        command[-1] = str(copied)
        command[1:1] = ["-iquote", str(original.parent)]
        command = [a for a in command if a not in ("-fprofile-arcs", "-ftest-coverage")]
        commands.append(command)
        with (output / (copied.stem + ".build.log")).open("w", encoding="utf-8") as log:
            subprocess.run(command, cwd=BUILD, env=env, stdout=log, stderr=subprocess.STDOUT, check=True)
        compiled[Path(entry["output"]).resolve()] = obj
    ninja = (BUILD / "build.ninja").read_text(encoding="utf-8")
    block = re.search(r"(?m)^build tests/test_sunshine\.exe: ([^\n]+)\n((?:  [^\n]*\n)+)", ninja)
    assert block
    objects = block[1].split(" | ")[0].split()[1:]
    inputs = []
    preserved = []
    for item in objects:
        path = (BUILD / item.replace("$:", ":")).resolve()
        assert path.is_file(), path
        replacement = compiled.get(path)
        inputs.append(str(replacement or path))
        if replacement is None:
            preserved.append({"path": str(path), "sha256": sha(path)})
    assert len(set(compiled.values()).intersection(map(Path, inputs))) == 2
    variables = dict(re.findall(r"(?m)^  ([A-Z_]+) = (.*)$", block[2]))
    libraries = split_windows("dummy " + variables["LINK_LIBRARIES"])[1:]
    # The original MSYS /ucrt64 path is made absolute for the isolated compiler.
    link_args = ["-fprofile-arcs", "-ftest-coverage", "-static", "-L" + str(ROOT / "native/host/tools/msys64/ucrt64/lib")]
    link_args += inputs + libraries
    executable = output / "quest3d_color_probe.exe"
    link_args += ["-o", str(executable)]
    response = output / "link.rsp"
    response.write_text("\n".join('"' + arg.replace("\\", "/").replace('"', '\\"') + '"' for arg in link_args), encoding="utf-8")
    with (output / "link.log").open("w", encoding="utf-8") as log:
        subprocess.run([compiler, "@" + str(response)], cwd=BUILD, env=env, stdout=log, stderr=subprocess.STDOUT, check=True)
    assert all(sha(row["path"]) == row["sha256"] for row in preserved), "Production objects changed during the link"
    manifest = {"private_prefix": private_prefix, "protocol": 2, "frames_per_codec": 4,
        "test_filter": "Quest3DColorProbe.NeutralFixtureNvenc", "executable": str(executable),
        "executable_sha256": sha(executable), "original_test_executable_sha256": EXPECTED_TEST,
        "original_sources": [{"path": str(p), "sha256": sha(p)} for p in (source_reader, source_test)],
        "harness_sources": [{"path": str(p), "sha256": sha(p)} for p in (copied_reader, copied_test)],
        "preserved_production_objects": preserved, "compile_commands": commands,
        "link_response_sha256": sha(response), "gpu_executed": False}
    (output / "build.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps({"executable": str(executable), "sha256": sha(executable), "private_prefix": private_prefix}))


if __name__ == "__main__":
    main()

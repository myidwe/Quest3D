"""Build the reviewed mDNS fix using the pinned Android objects, without updating the cache.

Run under Ubuntu-20.04 with the existing build-cache Python. This intentionally
does not run CMake configure, Ninja build, bootstrap, or the post-build copy.
Three exact source hashes and --execute are required for compilation.
"""

import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys


LIBRARY = "libnightfall-stream.android.template_release.arm64.so"
RAW_SHA = "ca186ad07df9b3dd2cd8b6e03d516d92d6e3bfd790f9e8772ab31eb63b9b61c4"
STRIPPED_SHA = "7600413a1bb3ed8b12a5a6f3f611626420ae7caf1fef0c6dc8d5a50df54e2a43"
MDNS_OBJECT = "CMakeFiles/nightfall-stream.dir/src/network/mdns_browser.cpp.o"
ALLOWED_SOURCE = (
    "src/network/mdns_browser.cpp",
    "src/network/mdns_browser.h",
    "src/network/mdns_parser.h",
)
BASELINE_MDNS_SHA = {
    ALLOWED_SOURCE[0]: "5378cae26dc55609fa843bb116f9bdf90c71f691e5544375afff536b44b99b53",
    ALLOWED_SOURCE[1]: "bfb6be28c9ba8830d96e3a57774d938689a20e55eb031aa55ee989deb2d2ce70",
}


def sha(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def normalized(raw):
    return raw.replace(b"\r\n", b"\n")


def inside(path, root):
    path.resolve().relative_to(root.resolve())
    return path


def write_json(path, data):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(data, stream, indent=2)
        stream.write("\n")


def replace_option(argv, option, replacement):
    require(argv.count(option) == 1, "Expected exactly one option: " + option)
    result = list(argv)
    index = result.index(option)
    require(index + 1 < len(result), "Missing option value: " + option)
    result[index + 1] = str(replacement)
    return result


def compiler_command(text):
    lines = [line for line in text.splitlines() if line.strip()]
    require(len(lines) == 1, "Expected one native compile command")
    argv = shlex.split(lines[0])
    require(argv and Path(argv[0]).name == "clang++", "Unexpected native compiler")
    require(not set(argv) & {"&&", ";", "||", "|", ">", "<"}, "Shell operator in compiler argv")
    return argv


def linker_command(text):
    lines = [line for line in text.splitlines() if line.strip()]
    require(bool(lines), "Missing native linker command")
    tokens = shlex.split(lines[-1])
    require(tokens[:2] == [":", "&&"], "Unexpected Ninja linker prefix")
    end = tokens.index("&&", 2)
    argv = tokens[2:end]
    tail = tokens[end + 1:]
    require(argv and Path(argv[0]).name == "clang++", "Unexpected native linker")
    require(tail[:1] == ["cd"] and "copy_if_different" in tail,
            "Expected the known post-build copy; do not execute an unknown command")
    require(not set(argv) & {"&&", ";", "||", "|", ">", "<"}, "Shell operator in linker argv")
    require(argv.count(MDNS_OBJECT) == 1, "Link must contain the mDNS object exactly once")
    return argv


def isolated_compile(argv, source, output):
    result = replace_option(argv, "-c", source)
    for option, path in (("-o", output), ("-MT", output), ("-MF", output.with_suffix(".o.d"))):
        result = replace_option(result, option, path)
    return result


def isolated_link(argv, output, replacement_object=None):
    result = replace_option(argv, "-o", output)
    dependency = [arg for arg in result if arg.startswith("--dependency-file=")]
    require(len(dependency) == 1, "Expected one linker dependency output")
    result[result.index(dependency[0])] = "--dependency-file=" + str(output.with_suffix(".so.d"))
    if replacement_object is not None:
        require(result.count(MDNS_OBJECT) == 1, "Expected exactly one old mDNS object")
        result[result.index(MDNS_OBJECT)] = str(replacement_object)
    return result


def guard_header_layout(old, new):
    old = normalized(old)
    new = normalized(new)
    removable = (
        b"    String _read_dns_name(const uint8_t *data, int len, int offset, int &out_end);\n",
        b"    Array _parse_dns_response(const uint8_t *data, int len);\n",
    )
    require(all(old.count(line) == 1 for line in removable), "Unknown baseline MdnsBrowser header")
    expected = old
    for line in removable:
        if line not in new:
            expected = expected.replace(line, b"")
    require(new == expected,
            "MdnsBrowser header has additional changes; compiling one object is not approved")


def record_files(paths):
    return {str(path): {"sha256": sha(path), "bytes": path.stat().st_size}
            for path in sorted(set(paths))}


def source_files(addon):
    paths = []
    for directory, dirs, files in os.walk(addon):
        dirs[:] = [name for name in dirs if name not in {"build", "bin", ".git"}]
        paths.extend(Path(directory) / name for name in files)
    return paths


def difference(before, after):
    return [path for path in sorted(set(before) | set(after)) if before.get(path) != after.get(path)]


def query(ninja, build, target):
    result = subprocess.run([str(ninja), "-C", str(build), "-t", "commands", target],
                            check=True, text=True, capture_output=True)
    return result.stdout


def run_step(argv, cwd, output, name, commands):
    # argv is always a list. In particular, the Ninja post-build shell chain is absent.
    commands.append({"name": name, "cwd": str(cwd), "argv": [str(item) for item in argv]})
    with (output / (name + ".log")).open("xb") as log:
        subprocess.run(argv, cwd=cwd, stdout=log, stderr=subprocess.STDOUT, check=True,
                       env={**os.environ, "TMPDIR": str(output / "tmp")})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--cache", type=Path,
                        default=Path.home() / ".cache/quest_to_3d-quest-build")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--source-sha256", action="append", default=[], metavar="PATH=SHA256")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    require(args.execute, "Compilation requires --execute and the three reviewed source hashes")
    require(sys.platform == "linux", "Run this builder inside the pinned Ubuntu-20.04 WSL distro")
    project, cache = args.project.resolve(strict=True), args.cache.resolve(strict=True)
    output = (args.output or project / "artifacts/quest/quest3-scan-20260914/native").resolve()
    inside(output, project / "artifacts/quest")
    require(not output.exists(), "Output already exists; preserve it and choose a new artifact directory")
    approved = {}
    for item in args.source_sha256:
        path, digest = item.split("=", 1)
        require(path in ALLOWED_SOURCE and path not in approved, "Unexpected or duplicate approved source")
        require(len(digest) == 64 and set(digest) <= set("0123456789abcdef"), "Invalid SHA-256")
        approved[path] = digest
    require(set(approved) == set(ALLOWED_SOURCE), "All three reviewed source hashes are required")
    addon = cache / "source/addons/nightfall-stream"
    build = addon / "build/android"
    frozen = project / "artifacts/quest/precision-clean-ui-20260911/public-source/addons/nightfall-stream"
    source = project / "third_party/nightfall/addons/nightfall-stream"
    ninja = cache / "venv/bin/ninja"
    ndk = cache / "android-sdk/ndk/29.0.14206865/toolchains/llvm/prebuilt/linux-x86_64/bin"
    original_so = addon / "bin/android" / LIBRARY
    linked_so = build / "bin/android" / LIBRARY
    require(sha(original_so) == RAW_SHA and sha(linked_so) == RAW_SHA, "Pinned native library changed")
    baseline = sorted(path for path in frozen.rglob("*") if path.is_file())
    require(len(baseline) == 85, "The pinned source baseline must contain 85 files")
    for path in baseline:
        cached = addon / path.relative_to(frozen)
        require(normalized(path.read_bytes()) == normalized(cached.read_bytes()),
                "Cached source differs from installed native source: " + str(cached))
    for relative, expected in BASELINE_MDNS_SHA.items():
        require(sha(addon / relative) == expected, "Pinned mDNS baseline changed: " + relative)
    for relative, expected in approved.items():
        require(sha(source / relative) == expected, "Reviewed source changed: " + relative)
    guard_header_layout((addon / ALLOWED_SOURCE[1]).read_bytes(), (source / ALLOWED_SOURCE[1]).read_bytes())

    compile_text = query(ninja, build, MDNS_OBJECT)
    link_text = query(ninja, build, "bin/android/" + LIBRARY)
    compile_argv, link_argv = compiler_command(compile_text), linker_command(link_text)
    for argv in (compile_argv, link_argv):
        require(Path(argv[0]).resolve() == (ndk / "clang++").resolve(), "Unexpected pinned NDK compiler")
        require("--target=aarch64-none-linux-android28" in argv, "Unexpected Android target")
    require(compile_argv[compile_argv.index("-c") + 1] == str(addon / ALLOWED_SOURCE[0]),
            "Unexpected baseline compilation source")
    require(compile_argv[compile_argv.index("-o") + 1] == MDNS_OBJECT, "Unexpected baseline object output")

    # Track all source/config files, including cache-only shader source, plus every
    # object/library consumed by the exact linked target. Never hash only the changed object.
    cache_sources = source_files(addon)
    link_inputs = [Path(arg) if Path(arg).is_absolute() else build / arg
                   for arg in link_argv if not arg.startswith("-") and arg.endswith((".o", ".a", ".so"))]
    link_inputs = [path for path in link_inputs if path != linked_so]
    config_paths = [build / "build.ninja", build / "CMakeCache.txt", build / "CMakeFiles/rules.ninja",
                    build / "CMakeFiles/VerifyGlobs.cmake", build / ".ninja_deps", build / ".ninja_log"]
    protected_paths = cache_sources + link_inputs + [original_so, linked_so, ninja, ndk / "clang++", ndk / "llvm-strip"]
    protected_paths += [path for path in config_paths if path.is_file()]
    protected = record_files(protected_paths)
    output.mkdir(parents=True, exist_ok=False)
    commands, report = [], {"status": "running", "hardware_verified": False,
                            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                            "source_sha256": approved, "baseline_raw_sha256": RAW_SHA,
                            "baseline_stripped_sha256": STRIPPED_SHA,
                            "baseline_mdns_sha256": BASELINE_MDNS_SHA,
                            "baseline_source_count": len(baseline), "cache_source_count": len(cache_sources),
                            "link_input_count": len(set(link_inputs)), "replaced_object": MDNS_OBJECT,
                            "post_build_executed": False, "shared_cache_build_executed": False,
                            "header_layout_guard": "only removal of two private nonvirtual helper declarations allowed",
                            "invocation": [sys.executable, *sys.argv]}
    write_json(output / "protected-before.json", protected)
    write_json(output / "link-inputs.json", record_files(link_inputs))
    write_json(output / "source-baseline.json", {
        path.relative_to(frozen).as_posix(): {
            "cached_sha256": sha(addon / path.relative_to(frozen)), "published_sha256": sha(path),
            "content_matches_after_crlf_normalization": True,
        } for path in baseline
    })
    (output / "original-compile-command.txt").write_text(compile_text, encoding="utf-8")
    (output / "original-target-commands.txt").write_text(link_text, encoding="utf-8")
    (output / "tmp").mkdir()
    failure = None
    try:
        for path in baseline:
            target = output / "baseline-source" / path.relative_to(frozen)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((addon / path.relative_to(frozen)).read_bytes())
        for relative, expected in approved.items():
            target = output / "source" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((source / relative).read_bytes())
            require(sha(target) == expected, "Source changed during freeze: " + relative)
        # Re-link existing objects first: verifies these cached objects really
        # reproduce the installed baseline, beyond merely preserving their hashes.
        baseline_link = output / "baseline-relinked" / LIBRARY
        baseline_link.parent.mkdir()
        run_step(isolated_link(link_argv, baseline_link), build, output, "baseline-link", commands)
        report["baseline_relinked_sha256"] = sha(baseline_link)
        require(report["baseline_relinked_sha256"] == RAW_SHA, "Existing objects did not reproduce pinned library")
        baseline_strip = output / "baseline-stripped" / LIBRARY
        baseline_strip.parent.mkdir()
        run_step([str(ndk / "llvm-strip"), "--strip-unneeded", "-o", str(baseline_strip), str(original_so)],
                 build, output, "baseline-strip", commands)
        require(sha(baseline_strip) == STRIPPED_SHA, "Pinned library strip does not reproduce installed bytes")
        report["baseline_strip_reproduction_exact"] = True
        new_object = output / "objects/mdns_browser.cpp.o"
        new_object.parent.mkdir()
        run_step(isolated_compile(compile_argv, output / "source" / ALLOWED_SOURCE[0], new_object),
                 build, output, "compile-mdns", commands)
        raw_output = output / "raw" / LIBRARY
        raw_output.parent.mkdir()
        run_step(isolated_link(link_argv, raw_output, new_object), build, output, "link-mdns", commands)
        stripped_output = output / "stripped" / LIBRARY
        stripped_output.parent.mkdir()
        run_step([str(ndk / "llvm-strip"), "--strip-unneeded", "-o", str(stripped_output), str(raw_output)],
                 build, output, "strip-mdns", commands)
        report.update(raw_library=str(raw_output), raw_sha256=sha(raw_output),
                      stripped_library=str(stripped_output), stripped_sha256=sha(stripped_output),
                      stripped_bytes=stripped_output.stat().st_size, new_object_sha256=sha(new_object))
        require(report["stripped_sha256"] != STRIPPED_SHA, "The candidate library is unchanged")
    except Exception as error:
        failure = error
        report["error"] = str(error)
    finally:
        after = {}
        # Include additions to the cache source tree in the final comparison too.
        for path in set(protected_paths + source_files(addon)):
            after[str(path)] = ({"sha256": sha(path), "bytes": path.stat().st_size}
                                if path.is_file() else {"missing": True})
        changed = difference(protected, after)
        write_json(output / "protected-after.json", after)
        report.update(protected_files=len(protected), protected_changed=changed,
                      protected_unchanged=not changed, status="pass" if failure is None and not changed else "fail")
        write_json(output / "commands.json", commands)
        write_json(output / "verification.json", report)
        print(json.dumps(report, indent=2))
    require(not changed, "Protected native cache changed: " + ", ".join(changed))
    if failure is not None:
        raise failure


if __name__ == "__main__":
    main()

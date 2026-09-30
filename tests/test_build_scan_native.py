"""Safety and provenance of the isolated mDNS native rebuild command path."""

import importlib.util
from pathlib import Path

import pytest


SPEC = importlib.util.spec_from_file_location(
    "build_scan_native", Path(__file__).resolve().parents[1] / "scripts/quest/build_scan_native.py"
)
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


def test_compile_redirects_all_outputs_and_source_to_artifact(tmp_path):
    argv = builder.compiler_command(
        '/pinned/clang++ -I/cache/src -MD -MT old.o -MF old.o.d -o old.o -c /cache/mdns.cpp'
    )
    new = builder.isolated_compile(argv, tmp_path / "mdns.cpp", tmp_path / "new.o")
    assert argv[-1] == "/cache/mdns.cpp"
    for option in ("-o", "-MT", "-MF", "-c"):
        assert str(tmp_path) in new[new.index(option) + 1]
    assert "-I/cache/src" in new
    assert "old.o" not in new and "old.o.d" not in new


def test_link_discards_post_build_and_preserves_all_other_object_inputs(tmp_path):
    old = builder.MDNS_OBJECT
    text = (f': && /pinned/clang++ -shared -Xlinker --dependency-file=CMakeFiles/link.d '
            f'-o bin/native.so first.o {old} third.o libunchanged.a && cd /cache && '
            f'/cmake -E copy_if_different bin/native.so /cache/source/bin/native.so')
    argv = builder.linker_command(text)
    new = builder.isolated_link(argv, tmp_path / "new.so", tmp_path / "new.o")
    assert "&&" not in new and "copy_if_different" not in new and "cd" not in new
    assert "first.o" in new and "third.o" in new and "libunchanged.a" in new
    assert new[new.index("-o") + 1] == str(tmp_path / "new.so")
    assert "--dependency-file=" + str(tmp_path / "new.so.d") in new
    assert old not in new and str(tmp_path / "new.o") in new
    assert old in argv


@pytest.mark.parametrize("text", [
    '/clang++ -c x.cpp -o old.o && touch /cache/bad',
    '/clang++ -c x.cpp -o old.o\n/clang++ -c other.cpp -o other.o',
])
def test_unexpected_compile_command_is_rejected(text):
    with pytest.raises(RuntimeError):
        builder.compiler_command(text)


@pytest.mark.parametrize("tail", ["&& touch /cache/bad", "&& cd /cache && /cmake -E make_directory /cache/bin"])
def test_unknown_linker_post_build_shape_is_rejected(tail):
    with pytest.raises(RuntimeError):
        builder.linker_command(f': && /clang++ {builder.MDNS_OBJECT} -o old.so ' + tail)


def test_one_object_abi_guard_rejects_added_instance_member():
    old = (b'class MdnsBrowser {\nprivate:\n'
           b'    String _read_dns_name(const uint8_t *data, int len, int offset, int &out_end);\n'
           b'    Array _parse_dns_response(const uint8_t *data, int len);\n'
           b'public:\n    Array browse(float timeout = 3.0);\n};\n')
    new = old.replace(old.splitlines(keepends=True)[2], b'')
    builder.guard_header_layout(old.replace(b'\n', b'\r\n'), new)
    builder.guard_header_layout(old, new.replace(old.splitlines(keepends=True)[3], b''))
    with pytest.raises(RuntimeError, match="additional changes"):
        builder.guard_header_layout(old, new.replace(b'private:\n', b'private:\n    int records;\n'))


def test_modified_or_missing_protected_input_is_detected():
    before = {"a.o": {"sha256": "old", "bytes": 4}, "b.a": {"sha256": "stable", "bytes": 8}}
    after = {"a.o": {"sha256": "new", "bytes": 4}, "b.a": {"missing": True}}
    assert builder.difference(before, after) == ["a.o", "b.a"]

"""Source collection refuses path aliases and executable recipe expressions."""
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("host_dep_sources", Path(__file__).parents[1] /
                                            "scripts/release/collect_host_dependency_sources.py")
sources = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sources)


@pytest.mark.parametrize("name", ["../PKGBUILD", "/root/file", "x\\y", "a:stream",
                                   "CON", "a/NUL.txt", "foo. ", "a//b", "a/./b"])
def test_unsafe_member_refused(name):
    with pytest.raises(ValueError):
        sources.safe_path(name)


def test_recipe_alias_version_and_signature_expansion():
    text = """_realname=library
source=("${_realname}-${pkgver}.tar.gz::https://example.test/v${pkgver//./_}.tar.gz"{,.asc} fix.patch)
sha256sums=('aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' SKIP
'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb')
"""
    assert sources.recipe_inputs(text, "1.2.3") == [
        ("library-1.2.3.tar.gz", "a" * 64), ("library-1.2.3.tar.gz.asc", "SKIP"),
        ("fix.patch", "b" * 64)]


@pytest.mark.parametrize("expression", ["$(whoami)", "`whoami`", "${unknown}", "../escape"])
def test_executable_unresolved_or_escaping_recipe_refused(expression):
    text = "_realname=x\nsource=('" + expression + "')\nsha256sums=('SKIP')\n"
    with pytest.raises(ValueError):
        sources.recipe_inputs(text, "1")


def test_mismatched_checksums_refused():
    with pytest.raises(ValueError):
        sources.recipe_inputs("_realname=x\nsource=(a b)\nsha256sums=('SKIP')", "1")

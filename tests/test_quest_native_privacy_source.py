"""Reject stale, tampered or incomplete native privacy source ledgers."""
import copy
import hashlib
import importlib
import json
from pathlib import Path
import sys

import pytest

RELEASE = Path(__file__).resolve().parents[1] / "scripts/release"
sys.path.insert(0, str(RELEASE))
source = importlib.import_module("complete_quest_source_supply")
sys.path.remove(str(RELEASE))


def prepared(tmp_path):
    zero = {"current_private_user": 0, "current_private_home": 0,
            "generic_linux_home_paths": 0, "generic_windows_home_paths": 0,
            "generic_source_remap_occurrences": 3}
    record = {"sha256": "a" * 64, "bytes": 123, "rebuilt_in_this_run": True, "privacy_counts": zero}
    archives = {name: copy.deepcopy(record) for name in source.PRIVATE_PATH_ARCHIVES}
    report = {"native_privacy_gate_passed": True, "old_compiled_dependencies_reused": False,
              "dependency_and_helper_versions_match_original": True, "assertions_disabled": False,
              "static_link_inputs": archives, "stream_extension": copy.deepcopy(record),
              "elf_and_static_archives_scanned": 14}
    ledger = {"runtime_static_archives_rebuilt": True, "native_packages": {str(i): {} for i in range(8)},
              "static_link_inputs": archives}
    (tmp_path / "updated-native-dependency-ledger.json").write_text(json.dumps(ledger))
    (tmp_path / "recipe.sh").write_text("# exact reviewed recipe fixture\n")
    report["source_files"] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in tmp_path.iterdir()}
    (tmp_path / "privacy-native-proof.json").write_text(json.dumps(report))
    apk = {"native": {"libnightfall-stream.android.template_release.arm64.so": {
        "raw_sha256": "a" * 64, "stripped_matches_apk": True, "build_ids_match": True}}}
    return report, apk


def test_fresh_exact_native_privacy_ledger_is_bound(tmp_path):
    report, apk = prepared(tmp_path)
    assert source.verify_privacy_native_proof(tmp_path, apk) == report


@pytest.mark.parametrize("mutation", ["tampered-recipe", "extra-source", "stale-raw-native", "strip-mismatch", "build-id-mismatch"])
def test_actual_source_or_apk_mismatch_is_rejected(tmp_path, mutation):
    _, apk = prepared(tmp_path)
    native = apk["native"]["libnightfall-stream.android.template_release.arm64.so"]
    if mutation == "tampered-recipe":
        (tmp_path / "recipe.sh").write_text("changed")
    elif mutation == "extra-source":
        (tmp_path / "unreviewed.json").write_text("{}")
    elif mutation == "stale-raw-native":
        native["raw_sha256"] = "b" * 64
    elif mutation == "strip-mismatch":
        native["stripped_matches_apk"] = False
    else:
        native["build_ids_match"] = False
    with pytest.raises(ValueError):
        source.verify_privacy_native_proof(tmp_path, apk)


@pytest.mark.parametrize("mutation", ["old-static-reuse", "private-user", "missing-archive", "different-version", "disabled-assert"])
def test_privacy_and_fresh_build_failures_are_rejected(tmp_path, mutation):
    report, apk = prepared(tmp_path)
    if mutation == "old-static-reuse":
        report["old_compiled_dependencies_reused"] = True
    elif mutation == "private-user":
        report["stream_extension"]["privacy_counts"]["current_private_user"] = 1
    elif mutation == "missing-archive":
        report["static_link_inputs"].pop(next(iter(report["static_link_inputs"])))
    elif mutation == "different-version":
        report["dependency_and_helper_versions_match_original"] = False
    else:
        report["assertions_disabled"] = True
    (tmp_path / "privacy-native-proof.json").write_text(json.dumps(report))
    with pytest.raises(ValueError):
        source.verify_privacy_native_proof(tmp_path, apk)

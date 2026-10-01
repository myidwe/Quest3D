import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('quest_public_ui_build', ROOT / 'scripts/prepare-quest-public-ui-build.py')
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fixture(tmp_path):
    source, overlay = tmp_path / 'source', tmp_path / 'overlay'
    old = source / 'project/src/product_theme.gd'
    old.parent.mkdir(parents=True)
    old.write_text('old theme\n')
    (source / 'source-manifest.json').write_text(json.dumps({'files': {'project/src/product_theme.gd': digest(old)}}))
    new = overlay / 'files/src/product_theme.gd'
    new.parent.mkdir(parents=True)
    new.write_text('new theme\n')
    delta = {'schema': 1, 'base_source_manifest_sha256': digest(source / 'source-manifest.json'),
             'files': {'src/product_theme.gd': {'before_sha256': digest(old), 'sha256': digest(new)}}}
    (overlay / 'manifest.json').write_text(json.dumps(delta))
    return source, overlay, delta


def test_delta_preserves_baseline_and_build_state(tmp_path):
    source, overlay, delta = fixture(tmp_path)
    old_manifest = (source / 'source-manifest.json').read_bytes()
    result = tool.prepare(source, tmp_path / 'new', overlay)
    assert result['prepared'] and result['native_rebuilt'] is False
    assert (source / 'source-manifest.json').read_bytes() == old_manifest
    assert (source / 'project/src/product_theme.gd').read_text() == 'old theme\n'
    assert (tmp_path / 'new/project/src/product_theme.gd').read_text() == 'new theme\n'
    manifest = tool.verify_manifest(tmp_path / 'new')
    assert not manifest['source_complete'] and not manifest['clean_build_verified']
    assert manifest['ui_refinement']['files'] == delta['files']


@pytest.mark.parametrize('fault', ['overlay', 'source', 'native', 'extra', 'baseline'])
def test_bad_inputs_fail_before_creating_output(tmp_path, fault):
    source, overlay, delta = fixture(tmp_path)
    if fault == 'overlay': (overlay / 'files/src/product_theme.gd').write_text('damaged')
    elif fault == 'source': (source / 'project/src/product_theme.gd').write_text('damaged')
    elif fault == 'native': delta['files']['addons/native/code.cpp'] = delta['files'].pop('src/product_theme.gd')
    elif fault == 'extra': (overlay / 'files/src/extra.gd').write_text('extra')
    elif fault == 'baseline': delta['base_source_manifest_sha256'] = '0' * 64
    (overlay / 'manifest.json').write_text(json.dumps(delta))
    with pytest.raises(ValueError): tool.prepare(source, tmp_path / 'new', overlay)
    assert not (tmp_path / 'new').exists()


def test_existing_output_and_preserved_paths_are_rejected(tmp_path):
    source, overlay, _ = fixture(tmp_path)
    output = tmp_path / 'existing'
    output.mkdir()
    (output / 'keep.txt').write_text('preserve')
    with pytest.raises(FileExistsError): tool.prepare(source, output, overlay)
    assert (output / 'keep.txt').read_text() == 'preserve'
    with pytest.raises(ValueError): tool.prepare(source, source / 'nested', overlay)

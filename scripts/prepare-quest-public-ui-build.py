"""Apply the reviewed public Quest UI delta to its exact source baseline.

Preserve historical source/ APK inputs. This changes GDScript UI and view
geometry only; Android native libraries and streaming settings are reused.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
RELEASE = ROOT / 'scripts/release'
if not RELEASE.is_dir(): RELEASE = Path(__file__).resolve().parent
sys.path.insert(0, str(RELEASE))
from prepare_quest_source import checked, safe_name, sha, verify_manifest

ALLOWED = {
    'src/product_theme.gd', 'src/precision_ui.gd', 'src/precision_depth_slider.gd',
    'src/welcome_screen.gd', 'src/screen_manager.gd', 'src/vr_screen.gd',
    'test/render_product_ui.gd', 'test/test_display_view_settings.gd',
    'test/test_distance_settings.gd', 'test/test_distance_settings.gd.uid',
    'test/test_product_icons.gd', 'test/test_product_icons.gd.uid',
    'test/validate_product_icons_export.sh',
}


def prepare(source: Path, output: Path, overlay: Path) -> dict:
    source = source.resolve(strict=True)
    overlay = overlay.resolve(strict=True)
    output = output.resolve()
    if output.exists():
        raise FileExistsError('Preserve existing output')
    for preserved in (source, overlay):
        if output.is_relative_to(preserved) or preserved.is_relative_to(output):
            raise ValueError('New output must be separate from preserved inputs')
    original = verify_manifest(source)
    delta = json.loads((overlay / 'manifest.json').read_text('utf-8'))
    if delta.get('schema') != 1 or delta.get('base_source_manifest_sha256') != sha(source / 'source-manifest.json'):
        raise ValueError('Exact UI source baseline differs')
    files = delta['files']
    if not files or not set(files) <= ALLOWED:
        raise ValueError('UI overlay cannot change native, preference, or transport sources')
    actual = {f.relative_to(overlay / 'files').as_posix() for f in (overlay / 'files').rglob('*') if f.is_file()}
    if actual != set(files):
        raise ValueError('UI overlay inventory differs')
    for name, record in files.items():
        safe_name(name)
        file = checked(overlay / 'files' / name, overlay)
        if sha(file) != record['sha256']:
            raise ValueError('UI overlay hash differs')
        prior = original['files'].get('project/' + name)
        if prior != record.get('before_sha256'):
            raise ValueError('UI source base file differs')
    shutil.copytree(source, output)
    for name in files:
        target = output / 'project' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(overlay / 'files' / name, target)
    receipt = {
        'schema': 1, 'kind': 'public-quest-ui-refinement',
        'base_source_manifest_sha256': sha(source / 'source-manifest.json'),
        'overlay_manifest_sha256': sha(overlay / 'manifest.json'),
        'files': files, 'native_rebuilt': False,
        'native_sources_changed': False, 'preference_version_changed': False,
        'exported_icon_resource_regression_required': True,
    }
    (output / 'UI_REFINEMENT_PROVENANCE.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n', 'utf-8')
    manifest = dict(original)
    manifest['files'] = {f.relative_to(output).as_posix(): sha(f) for f in sorted(output.rglob('*'))
                         if f.is_file() and f != output / 'source-manifest.json'}
    manifest['ui_refinement'] = receipt
    manifest['source_complete'] = False
    manifest['clean_build_verified'] = False
    manifest['dependency_notices_verified'] = False
    manifest['public_release_ready'] = False
    (output / 'source-manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', 'utf-8')
    verify_manifest(output)
    return {'prepared': True, 'files': len(manifest['files']), 'overlay_files': len(files),
            'source_manifest_sha256': sha(output / 'source-manifest.json'), 'native_rebuilt': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    default_overlay = ROOT / 'patches/quest-public-ui-20261001'
    if not default_overlay.is_dir(): default_overlay = ROOT / 'ui-refinement'
    parser.add_argument('--overlay', type=Path, default=default_overlay)
    args = parser.parse_args()
    print(json.dumps(prepare(args.source, args.output, args.overlay), indent=2))


if __name__ == '__main__':
    main()

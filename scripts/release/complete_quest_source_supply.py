"""Create a new manifest for an actually rebuilt unsigned public Quest APK.

Copies the preserved source candidate, exact dependency supply and build
recipes into a new directory. Does not modify historical manifests. Signing
eligibility records source/build/notice evidence; hardware remains unverified.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import shutil
import tarfile
import re

from prepare_quest_source import sha,verify_manifest,safe_name
from collect_quest_release_dependencies import download

def copy(input:Path,target:Path):
    target.parent.mkdir(parents=True,exist_ok=True)
    if target.exists():raise FileExistsError('Preserve source inputs')
    shutil.copyfile(input,target)

def public_header_audit(source:Path):
    """Inventory exact supplied archives and reject retired SDK/header paths.

    This supplements the pinned-source/native hashes, rather than claiming a
    path regex can establish arbitrary third-party licensing on its own.
    Legacy SDK license URL references remain readable historical text.
    """
    forbidden=re.compile(r'(?i)(?:meta_headers|meta[-_]?openxr[-_]?mobile[-_]?sdk|(?:^|/)openxr_preview(?:\.h|/)|meta_openxr_preview|oculus[-_]?mobile[-_]?sdk)')
    archives=[];matches=[]
    for file in sorted(source.rglob('*')):
        if not file.is_file():continue
        name=file.relative_to(source).as_posix()
        if forbidden.search(name):matches.append(name)
        if file.name.endswith(('.tar','.tar.gz')):
            with tarfile.open(file) as archive:members=archive.getnames()
            matches.extend(name+'::'+member for member in members if forbidden.search(member))
            archives.append({'path':name,'sha256':sha(file),'members':len(members)})
    if matches:raise ValueError('Retired proprietary preview SDK/header inputs are not part of public source supply')
    legacy=source/'project/addons/godotopenxrvendors/meta/LICENSE-SDK'
    report={'schema':1,'review':'Exact pinned upstream input inventory plus retired SDK/header path check',
            'meta_preview_headers_selected':False,'proprietary_sdk_path_matches':[],
            'archive_inventory':archives,'legacy_sdk_license_reference':
            {'path':legacy.relative_to(source).as_posix(),'sha256':sha(legacy),'kind':'Historical license URL text, not SDK code'} if legacy.is_file() else None}
    (source/'PUBLIC_HEADER_INPUT_AUDIT.json').write_text(json.dumps(report,indent=2)+'\n','utf-8')
    return report

def complete(source:Path,dependencies:Path,vendor:Path,export:Path,verification:Path,rebuilt:Path,output:Path,repo:Path,tools:Path):
    for preserved in (source,dependencies,vendor,export,verification,rebuilt,tools):
        if output.resolve().is_relative_to(preserved.resolve()) or preserved.resolve().is_relative_to(output.resolve()):
            raise ValueError('Public source output must be separate from preserved inputs')
    original=verify_manifest(source)
    if output.exists():raise FileExistsError('Preserve historical source candidates')
    proof=json.loads((verification/'apk-correspondence.json').read_text())
    apk=export/'Quest3D-public-review-unsigned.apk'
    if sha(apk)!=proof['apk_sha256'] or not proof['native_binding_verified'] or not proof['unsigned_verified']:
        raise ValueError('Fresh unsigned APK verification must pass')
    vendor_build=json.loads((vendor/'vendor-build-summary.json').read_text())
    if not vendor_build['vendor_built_from_source'] or vendor_build['meta_preview_headers_selected']:
        raise ValueError('Public vendor must not select proprietary preview inputs')
    supply=json.loads((dependencies/'dependency-source-notices.json').read_text())
    expected=supply.get('files',{})
    actual={f.relative_to(dependencies).as_posix():sha(f) for f in dependencies.rglob('*') if f.is_file() and f.name!='dependency-source-notices.json'}
    if not expected or actual!=expected:
        raise ValueError('Dependency source/NOTICE inventory differs from collection receipt')
    actual_runtime=json.loads((export/'project/android/build/quest3d-resolved-runtime-standard-release.json').read_text())
    expected_runtime=json.loads((dependencies/'maven/resolved-runtime.json').read_text())
    if actual_runtime!=expected_runtime:
        raise ValueError('New APK selected runtime dependencies differ from supplied source/notices')
    if supply['maven']['source_unavailable'] or len(supply['native']['native_packages'])!=8 or not supply['native']['ffmpeg_gpl_enabled']:
        raise ValueError('Exact runtime dependency supply incomplete')
    for name,item in supply['native']['source_archives'].items():
        archive=source/'upstream/static-dependencies'/name
        if not archive.is_file() or sha(archive)!=item['sha256']:
            raise ValueError('Actual native dependency source archive not supplied: '+name)
    for package,item in supply['native']['native_packages'].items():
        for value in item['source_sha512_declared']:
            if not any(record['sha512']==value and package in record['packages'] for record in supply['native']['source_archives'].values()):
                raise ValueError('Declared native dependency source fetch hash missing: '+package)
    shutil.copytree(source,output)
    # The source package corresponds to this new generated public preset;
    # first-party GDScript/Java/native sources themselves remain unchanged.
    (output/'project/export_presets.cfg').write_bytes((export/'project/export_presets.cfg').read_bytes())
    dest=output/'dependency-supply'
    shutil.copytree(dependencies,dest)
    vendor_inputs=json.loads((vendor/'vendor-source-inputs.json').read_text())
    # The verified prebuild inventory contains both selected vendor files
    # and exact submodule files. Do not depend on a private review path.
    selected_vendor={name:digest for name,digest in vendor_inputs['source_files'].items()
                     if not name.startswith(('thirdparty/godot-cpp/','thirdparty/openxr-source/'))}
    if len(selected_vendor)!=vendor_inputs['copied_vendor_source_files']:
        raise ValueError('Vendor prebuild inventory differs from selected tracked source')
    for name,digest in selected_vendor.items():
        input=vendor/'source'/name
        if sha(input)!=digest:raise ValueError('Vendor input changed during build')
        copy(input,dest/'openxr-vendor/source'/name)
    for name,record in vendor_inputs['dependencies'].items():
        input=vendor/'downloads'/(name+'.tar.gz')
        if sha(input)!=record['sha256']:raise ValueError('Vendor submodule archive changed')
        copy(input,dest/'openxr-vendor'/input.name)
    copy(vendor/'vendor-source-inputs.json',dest/'openxr-vendor/vendor-source-inputs.json')
    copy(vendor/'vendor-build-summary.json',dest/'openxr-vendor/vendor-build-summary.json')
    # The source JAR for this Maven module contains the Java wrapper, not
    # loader C++. Supply the immutable native source commit as well.
    loader=dest/'khronos-loader/OpenXR-SDK-Source-1.1.54.tar.gz'
    loader_record=download('https://codeload.github.com/KhronosGroup/OpenXR-SDK-Source/tar.gz/58e026e7efa5a4f6a612c554560b53aa482f04d4',loader)
    if loader_record['sha256']!='8d5a8acd5661ce884ef467394c6f58b7d77f7df5db352e04956cf8dc892151ca':
        raise ValueError('Pinned Khronos native loader source archive changed')
    loader_record.update(tag='release-1.1.54',annotated_tag_object='8fd9c550cc7051bf2f1c179675e57a1f4c4a036c',
                         commit='58e026e7efa5a4f6a612c554560b53aa482f04d4')
    (dest/'khronos-loader/source-provenance.json').write_text(json.dumps(loader_record,indent=2)+'\n')
    # Selected exact recipes include ABI hashes and custom nested headers.
    custom=source/'project/addons/nightfall-stream/vcpkg-overlay/moonlight-common-c/quest3d'
    for f in custom.iterdir():
        if f.is_file():
            target=dest/'native/recipes/moonlight-common-c/quest3d'/f.name
            if target.exists():
                if sha(target)!=sha(f):raise ValueError('Selected custom native header differs')
            else:copy(f,target)
    for package in ('vcpkg-cmake','vcpkg-cmake-config','vcpkg-cmake-get-vars','vcpkg-pkgconfig-get-modules','vcpkg-tool-meson'):
        origin=tools/'source/addons/nightfall-stream/build/android/vcpkg_installed/x64-linux/share'/package
        if not origin.is_dir():raise ValueError('Actual build helper source missing: '+package)
        shutil.copytree(origin,dest/'native/build-helpers'/package)
    # Ship runtime license/copyright files in one visible directory so binary
    # bundles can include them without relying on archive-only attributions.
    licenses=output/'licenses'
    copy(source/'project/LICENSE',licenses/'Quest3D-Nightfall-GPL-3.0.txt')
    for name in ('LICENSE.txt','COPYRIGHT.txt','AUTHORS.md'):
        f=rebuilt/'native-source/godot'/name
        if f.is_file():copy(f,licenses/('Godot-'+name))
    copy(vendor/'source/LICENSE',licenses/'OpenXR-Vendor-MIT.txt')
    copy(vendor/'source/CONTRIBUTORS.md',licenses/'OpenXR-Vendor-Contributors.md')
    for component in ('openxr','khronos_openxr_sdk','godot-cpp','openxr-source'):
        for name in ('LICENSE','LICENSE.txt','LICENSE.TXT','LICENSE.md','COPYING'):
            f=vendor/'source/thirdparty'/component/name
            if f.is_file():copy(f,licenses/('OpenXR-'+component+'-'+name.replace('.','-')+'.txt'))
    for archive_path in (source/'upstream/static-dependencies').glob('*.tar.gz'):
        with tarfile.open(archive_path) as archive:
            for item in archive.getmembers():
                if item.isfile() and re.search(r'(?i)(?:^|/)(?:LICENSE|LICENCE|COPYING|NOTICE)[^/]*$',item.name):
                    if item.size>2*1024*1024:raise ValueError('Native component license entry too large')
                    raw=archive.extractfile(item).read();raw.decode('utf-8')
                    target=licenses/'native-sources'/archive_path.name/(item.name.split('/',1)[-1].replace('/','__'))
                    target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(raw)
    for f in (dest/'maven/notices').rglob('*'):
        if f.is_file():
            copy(f,licenses/'maven'/f.relative_to(dest/'maven/notices'))
    copy(dest/'maven/LICENSE-APACHE-2.0.txt',licenses/'Maven-Apache-2.0.txt')
    copy(source/'project/src/assets/precision/fonts/OFL-Pretendard.txt',licenses/'Pretendard-OFL.txt')
    miniaudio=(source/'project/addons/nightfall-stream/include/miniaudio.h').read_text('utf-8')
    license_start=miniaudio.rfind('ALTERNATIVE 2 - MIT No Attribution')
    if license_start<0:raise ValueError('Embedded miniaudio license missing')
    (licenses/'miniaudio-MIT-No-Attribution.txt').write_text(miniaudio[license_start:].rstrip('*/\n \t')+'\n','utf-8')
    with tarfile.open(loader) as archive:
        for item in archive.getmembers():
            if item.isfile() and re.search(r'(?i)(?:^|/)(?:LICENSE|LICENCE|COPYING|NOTICE)[^/]*$',item.name):
                if item.size>2*1024*1024:raise ValueError('Loader license entry too large')
                raw=archive.extractfile(item).read();raw.decode('utf-8')
                target=licenses/'khronos-loader'/item.name.split('/',1)[-1].replace('/','__')
                target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(raw)
    (output/'NOTICES.md').write_text('''# Quest3D Quest notices

Quest3D / Nightfall: GNU GPL version 3 or later. Full first-party source,
Godot changes, native source, build recipes and exact dependency source are
provided in this corresponding-source package. Godot and godot-cpp: MIT;
Godot COPYRIGHT/AUTHORS and bundled third-party notices are in licenses/.
Pretendard fonts: SIL Open Font License, preserved in licenses/Pretendard-OFL.txt.
miniaudio: MIT No Attribution; the selected license text is retained in licenses/.

nightfall-stream links the GPL-enabled FFmpeg 7.1.2 build, Moonlight common C,
ENet, curl, OpenSSL, Opus, SIMDe, zlib and godot-cpp. Their exact source
archives, selected port/patch inputs and license texts accompany this source.
The combined app is supplied under GPL-3.0-or-later; the component notices
and source licenses remain in force.

OpenXR Vendors 5.0.0 is built from its pinned MIT source using public Khronos
headers. Meta preview SDK headers were not selected for this APK. Existing
preview-header license references in historical project assets do not mean
the new vendor runtime was built from those proprietary inputs.
Khronos loader 1.1.54: Apache-2.0; exact native hash and source release pinned.

Android runtime Maven dependencies: 35 modules, Apache-2.0 declarations in
the pinned POMs. Every module's published source JAR is included, retaining
source copyrights and embedded LICENSE/NOTICE. All discovered runtime/source
NOTICE/LICENSE files and Apache-2.0 text are also copied to licenses/maven.
See dependency-supply/maven/resolved-runtime.json for exact coordinates.

NDK libc++: bundled Android NDK r29 runtime. NDK NOTICE and NOTICE.toolchain
texts, including LLVM exception/license notices, are retained in licenses/.
The standard compiler runtime is reused from its exact pinned NDK toolchain;
the NDK toolchain itself is not claimed to have been rebuilt by this project.

Model weights and signing private keys are not included in this APK/source.
''','utf-8')
    build=output/'release-build'
    build.mkdir()
    for name in ('collect_quest_vendor_baseline.py','rebuild_quest_public_vendor.sh','collect_quest_release_dependencies.py',
                 'prepare_quest_android_export.py','export_quest_unsigned_baseline.sh','audit_quest_gradle_dependencies.sh',
                 'verify_quest_unsigned_apk.py','prepare_quest_build_baseline.py','rebuild_quest_baseline.sh','rebuild_quest_engine.sh',
                 'prepare_quest_source.py','prepare_host_source.py','build_bundle.py','complete_quest_source_supply.py'):
        copy(repo/'scripts/release'/name,build/name)
    proof_public={key:value for key,value in proof.items() if key not in ('commands','tool_sha256')}
    (output/'PUBLIC_APK_BUILD.json').write_text(json.dumps(proof_public,indent=2)+'\n')
    (output/'SOURCE_BUILD_NOTES.md').write_text('''# Quest3D public build source

This source binds the newly rebuilt unsigned public app.questto3d.client APK,
not the preserved historical debug APK. Build native stream/XR and modified
Godot from the bundled pinned upstream source plus project patches; use the
exact native dependency archives and ABI-bound port recipes. The public
OpenXR vendor is rebuilt without meta_headers. Android Java/DEX and GDScript
were newly exported; official compatible editor/template/Maven/NDK inputs
are pinned instead of claiming all untouched tools were recompiled.

release-build contains the executed recipes. Follow the repository
docs/QUEST_CLEAN_BUILD_2026-10-01.md and the dependency-source ledger.
Use WSL, an explicit new output path and the pinned build cache. Private key
generation and device installation are separate from corresponding source.
Hardware results are not implied by source/build/notice completion.
''','utf-8')
    # Historical evidence stays readable under an explicit historical name.
    (output/'SOURCE_PROVENANCE.json').rename(output/'HISTORICAL_SOURCE_PROVENANCE.json')
    (output/'NATIVE_INPUT_VERIFICATION.json').rename(output/'HISTORICAL_NATIVE_INPUT_VERIFICATION.json')
    provenance={'schema':1,'apk_sha256':sha(apk),'metadata':proof['metadata'],
                'preserved_source_manifest_sha256':sha(source/'source-manifest.json'),
                'actual_build':proof_public,'dependency_source_ledger':supply,
                'vendor_source_inputs':vendor_inputs,'khronos_loader_source':loader_record,
                'source_complete':True,'clean_build_verified':True,'dependency_notices_verified':True,
                'hardware_verified':False,'installed':False,'published':False,'public_release_ready':False}
    (output/'SOURCE_PROVENANCE.json').write_text(json.dumps(provenance,indent=2)+'\n')
    public_header_audit(output)
    files={f.relative_to(output).as_posix():sha(f) for f in sorted(output.rglob('*')) if f.is_file() and f.name!='source-manifest.json'}
    for name in files:safe_name(name)
    result=dict(original,apk_sha256=sha(apk),package='app.questto3d.client',
                apk_metadata={'package':'app.questto3d.client','version_code':1,'version_name':proof['metadata']['version_name'],'signing_kind':'unsigned'},
                source_complete=True,clean_build_verified=True,dependency_notices_verified=True,public_release_ready=False,
                hardware_verified=False,files=files,source_supply_reason='Actual rebuilt APK + exact source/patch/dependency supply; hardware readiness separate',
                historical_development_apk_sha256=original['apk_sha256'],
                binary_notice_files=['NOTICES.md',*[name for name in files if name.startswith('licenses/')]])
    (output/'source-manifest.json').write_text(json.dumps(result,indent=2)+'\n','utf-8')
    verify_manifest(output)
    return {'files':len(files),'source_manifest_sha256':sha(output/'source-manifest.json'),'apk_sha256':sha(apk),
            'source_complete':True,'clean_build_verified':True,'dependency_notices_verified':True,'hardware_verified':False,'public_release_ready':False}

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('source','dependencies','vendor','export','verification','rebuilt','output','tools'):p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--repo',type=Path,default=Path(__file__).resolve().parents[2]);a=p.parse_args()
    print(json.dumps(complete(a.source,a.dependencies,a.vendor,a.export,a.verification,a.rebuilt,a.output,a.repo,a.tools),indent=2))

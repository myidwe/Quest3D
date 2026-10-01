"""Prepare an immutable public-header OpenXR vendor build from exact sources.

Never changes the official ZIP, prior APKs, devices, or signing material.
Meta preview headers are deliberately not selected; ordinary Khronos/OpenXR
extensions remain available. Device regression is a separate requirement.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import tarfile
from urllib.request import Request, urlopen

COMMIT = "6a04c8632140f7dc14670e5564fd473464047a15"
CPP_COMMIT = "58d1de720b8ffe9f8ffcdfe3a85148582cfd2e74"
XR_COMMIT = "ba4aec9686cb94c99a55f7ceba9768e9e35525c2"

def sha(file: Path) -> str:
    with file.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()

def fetch(url: str, output: Path, limit=96*1024*1024) -> dict:
    total = 0
    with urlopen(Request(url, headers={'User-Agent':'Quest3D-source-preparation/1'}), timeout=60) as response, output.open('xb') as stream:
        if not response.url.startswith('https://codeload.github.com/'):
            raise ValueError('Unexpected source host')
        while block := response.read(1024*1024):
            total += len(block)
            if total > limit:
                raise ValueError('Source download exceeds bounded limit')
            stream.write(block)
    return {'url':url, 'sha256':sha(output), 'bytes':total}

def extract(archive: Path, prefix: str, output: Path) -> None:
    output.mkdir(parents=True)
    with tarfile.open(archive) as value:
        members = value.getmembers()
        names = set()
        for item in members:
            if not (item.name == prefix or item.name.startswith(prefix+'/')) or item.name in names:
                raise ValueError('Unexpected or duplicate source member')
            names.add(item.name)
            name = item.name[len(prefix):].lstrip('/')
            if not name:
                continue
            target = output/name
            if not target.resolve().is_relative_to(output.resolve()) or item.issym() or item.islnk():
                raise ValueError('Unsafe source member')
            if item.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif item.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(value.extractfile(item).read())
            else:
                raise ValueError('Non-file source member')

def prepare(review: Path, output: Path) -> dict:
    if output.exists():
        raise FileExistsError('Preserve existing vendor builds')
    receipt = json.loads((review/'vendor-build-inputs.json').read_text('utf-8'))
    if receipt['source_commit'] != COMMIT or not receipt['exact_git_blobs_verified']:
        raise ValueError('Vendor official source identity is not verified')
    output.mkdir(parents=True)
    source = output/'source'
    for name, item in receipt['files'].items():
        original = review/'source'/name
        if sha(original) != item['sha256']:
            raise ValueError('Official vendor source changed')
        target = source/name
        if not target.resolve().is_relative_to(source.resolve()):
            raise ValueError('Unsafe vendor source path')
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original, target)
    downloads = output/'downloads'
    downloads.mkdir()
    dependencies = {}
    for name, repo, commit in [('godot-cpp','godotengine/godot-cpp',CPP_COMMIT), ('openxr-source','KhronosGroup/OpenXR-SDK-Source',XR_COMMIT)]:
        file = downloads/(name+'.tar.gz')
        dependencies[name] = fetch('https://codeload.github.com/'+repo+'/tar.gz/'+commit,file)
        dependencies[name]['commit'] = commit
        prefix = repo.split('/')[-1]+'-'+commit
        extract(file, prefix, source/'thirdparty'/name)
    result = {'schema':1,'vendor_commit':COMMIT,'copied_vendor_source_files':len(receipt['files']),
              'dependencies':dependencies,'meta_preview_headers_selected':False,
              'source_files':{f.relative_to(source).as_posix():sha(f) for f in sorted(source.rglob('*')) if f.is_file()},
              'built':False,'installed':False,'published':False}
    (output/'vendor-source-inputs.json').write_text(json.dumps(result,indent=2)+'\n','utf-8')
    return result

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--review',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args()
    result=prepare(args.review.resolve(),args.output.resolve())
    print(json.dumps({key:value for key,value in result.items() if key!='source_files'},indent=2))

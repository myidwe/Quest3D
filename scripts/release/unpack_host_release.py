"""Verify and unpack a supplied host baseline into a new build workspace."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tarfile


def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):h.update(block)
    return h.hexdigest()


def checked(path, missing=False):
    path=Path(os.path.abspath(path))
    for p in (path,*path.parents):
        try:item=p.lstat()
        except FileNotFoundError:
            if missing:continue
            raise
        if stat.S_ISLNK(item.st_mode) or getattr(item,'st_file_attributes',0)&0x400:
            raise ValueError('Redirected supplied source path')
    return path


def safe(name):
    name=name.rstrip('/')
    if not name or name.startswith('/') or ':' in name or '\\' in name or any(
        p in ('','.','..') or p.rstrip(' .')!=p or any(ord(c)<32 for c in p) or
        re.fullmatch(r'(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?',p)
        for p in name.split('/')):raise ValueError('Unsafe supplied archive member')
    return name


def extract(path,destination,expected=None):
    with tarfile.open(path) as archive:
        seen=set();actual={}
        for entry in archive:
            name=safe(entry.name)
            if name.casefold() in seen or not(entry.isfile() or entry.isdir()):
                raise ValueError('Aliased or nonregular supplied archive member')
            seen.add(name.casefold())
            if entry.isfile():
                data=archive.extractfile(entry).read()
                actual[name]=hashlib.sha256(data).hexdigest()
        if expected is not None and actual!=expected:
            raise ValueError('Source members differ from exact build input inventory')
    destination.mkdir(parents=True,exist_ok=True)
    with tarfile.open(path) as archive:archive.extractall(destination,filter='data')


def unpack(supply,output):
    supply=checked(supply);output=checked(output,missing=True)
    if output.exists() or output.is_relative_to(supply) or supply.is_relative_to(output):
        raise FileExistsError('Use a new separate build workspace')
    p=json.loads((supply/'provenance.json').read_text(encoding='utf-8'))
    inventory=supply/'source-file-inventory.json'
    if sha(inventory)!=p['source_file_inventory_sha256']:raise ValueError('Changed source inventory')
    expected=json.loads(inventory.read_text(encoding='utf-8'))['source_files']
    archive=supply/p['source_archive']
    if sha(archive)!=p['source_archive_sha256']:raise ValueError('Changed source archive')
    for name,item in p['dependency_archives'].items():
        if sha(supply/'dependencies'/safe(name))!=item['sha256']:raise ValueError('Changed pinned dependency')
    output.mkdir(parents=True,exist_ok=False)
    extract(archive,output/'source',expected)
    for name in p['dependency_archives']:
        destination=output/'dependencies'/name.removesuffix('.tar') if name.startswith('nv-codec-headers-') else output/'dependencies/unpacked'
        extract(supply/'dependencies'/name,destination)
    (output/'unpack-verification.json').write_text(json.dumps({'schema':1,'source_members_verified':len(expected),
        'host_binary_sha256':p['host_binary_sha256'],'source_archive_sha256':p['source_archive_sha256'],
        'build_or_install_executed':False},indent=2)+'\n',encoding='utf-8')
    return len(expected)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--supply',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();print(json.dumps({'verified_members':unpack(args.supply,args.output)}))

"""Collect exact installed native recipes and resolved Maven source/notices.

Run in the pinned WSL build environment. No APK promotion, signing or device
operation occurs. Every cached runtime artifact and native recipe is hashed;
downloaded sources use the primary publishers' HTTPS repositories.
"""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import zipfile

PACKAGES = ('curl','ffmpeg','godot-cpp','moonlight-common-c','openssl','opus','simde','zlib')

def sha(file: Path, algorithm='sha256'):
    value=hashlib.new(algorithm)
    with file.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):value.update(block)
    return value.hexdigest()

def download(url: str, output: Path, limit=96*1024*1024):
    output.parent.mkdir(parents=True,exist_ok=True)
    size=0
    with urlopen(Request(url,headers={'User-Agent':'Quest3D-source-preparation/1'}),timeout=45) as response,output.open('xb') as stream:
        if not response.url.startswith(('https://dl.google.com/','https://repo.maven.apache.org/','https://codeload.github.com/','https://www.apache.org/')):
            raise ValueError('Unexpected dependency source host')
        while block:=response.read(1024*1024):
            size+=len(block)
            if size>limit:raise ValueError('Bounded source download limit exceeded')
            stream.write(block)
    return {'url':url,'sha256':sha(output),'bytes':size}

def notices(raw: bytes, prefix=''):
    result={}
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        for item in archive.infolist():
            name=item.filename
            if item.is_dir():continue
            if re.search(r'(?i)(?:^|/)(?:LICENSE|LICENCE|COPYING|NOTICE)(?:[^/]*)$',name):
                if item.file_size>2*1024*1024:raise ValueError('Unbounded license entry')
                data=archive.read(item)
                data.decode('utf-8',errors='strict')
                result[prefix+name]=data
            elif name=='classes.jar':
                result.update(notices(archive.read(item),prefix+'classes.jar/'))
    return result

def collect_native(tools:Path,output:Path):
    dest=output/'native';dest.mkdir()
    installed=tools/'source/addons/nightfall-stream/build/android/vcpkg_installed'
    status=installed/'vcpkg/status'
    shutil.copyfile(status,dest/'installed-packages.txt')
    ports={}
    trees=Path.home()/'.cache/vcpkg/registries/git-trees'
    candidates=list(trees.glob('*/portfile.cmake'))+list((tools/'vcpkg/ports').glob('*/portfile.cmake'))
    hashes={sha(file):file for file in candidates}
    for package in PACKAGES:
        abi=tools/'vcpkg/buildtrees'/package/'arm64-android.vcpkg_abi_info.txt'
        raw=abi.read_text()
        expected=dict(line.split(' ',1) for line in raw.splitlines() if ' ' in line)['portfile.cmake']
        port=hashes.get(expected)
        if package=='moonlight-common-c':
            port=tools/'source/addons/nightfall-stream/vcpkg-overlay'/package/'portfile.cmake'
        if not port or sha(port)!=expected:raise ValueError('Actual native recipe unavailable: '+package)
        target=dest/'recipes'/package
        target.mkdir(parents=True)
        copied={}
        entries=dict(line.split(' ',1) for line in raw.splitlines() if ' ' in line)
        for file in port.parent.rglob('*'):
            if not file.is_file():continue
            relative=file.relative_to(port.parent).as_posix()
            value=sha(file)
            if relative in entries and entries[relative]!=value:
                raise ValueError('Actual native port input changed: '+package+'/'+relative)
            copied_target=target/relative
            copied_target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(file,copied_target);copied[relative]=value
        shutil.copyfile(abi,target/'build-input-hashes.txt')
        notice=installed/'arm64-android/share'/package/'copyright'
        shutil.copyfile(notice,target/'COPYRIGHT.txt')
        ports[package]={'portfile_sha256':expected,'recipe_files':copied,'license_sha256':sha(notice),
                        'source_sha512_declared':re.findall(r'\bSHA512\s+"?([0-9a-f]{128})',port.read_text())}
    pin=subprocess.check_output(['git','-C',str(tools/'vcpkg'),'rev-parse','HEAD']).decode().strip()
    if pin!='f781d9387e4684783e69e136e2e124ff4660bffc':raise ValueError('vcpkg source pin changed')
    archive=dest/'vcpkg-source.tar.gz'
    with archive.open('xb') as stream:
        subprocess.run(['git','-C',str(tools/'vcpkg'),'archive','--format=tar.gz',pin],stdout=stream,check=True)
    # Source archives used by exact native recipes are already in the r3
    # source bundle. Check their recorded fetch hashes instead of redownloading.
    sources={}
    for file in (tools/'vcpkg/downloads').glob('*.tar.gz'):
        value=sha(file,'sha512')
        matches=[name for name,record in ports.items() if value in record['source_sha512_declared']]
        if matches:
            sources[file.name]={'packages':matches,'sha256':sha(file),'sha512':value,'bytes':file.stat().st_size}
    for package,record in ports.items():
        for expected in record['source_sha512_declared']:
            if not any(item['sha512']==expected and package in item['packages'] for item in sources.values()):
                raise ValueError('Native source fetch SHA512 does not match preserved download: '+package)
    config=tools/'vcpkg/buildtrees/ffmpeg/arm64-android-rel/ffbuild/config.mak'
    text=config.read_text()
    selected=next(line for line in text.splitlines() if line.startswith('FFMPEG_CONFIGURATION='))
    # Machine-specific build/output prefixes are not needed to reproduce flags.
    selected=selected.replace(str(tools),'${QUEST_BUILD_CACHE}')
    (dest/'ffmpeg-configure-flags.txt').write_text(selected+'\n')
    return {'vcpkg_commit':pin,'vcpkg_source_sha256':sha(archive),'native_packages':ports,'source_archives':sources,'ffmpeg_gpl_enabled':'--enable-gpl' in selected}

def collect_maven(export:Path,output:Path):
    records=json.loads((export/'project/android/build/quest3d-resolved-runtime-standard-release.json').read_text())
    cache=export/'cache/gradle/caches/modules-2/files-2.1'
    dest=output/'maven';dest.mkdir()
    modules={}
    for row in records:
        key=(row['group'],row['name'],row['version'])
        folder=cache/row['group']/row['name']/row['version']
        files=list(folder.glob('*/'+row['filename']))
        if len(files)!=1 or sha(files[0])!=row['sha256']:raise ValueError('Resolved Maven artifact changed')
        item=modules.setdefault(key,{'coordinate':':'.join(key),'artifacts':[],'notices':{}})
        item['artifacts'].append({k:v for k,v in row.items() if k not in ('group','name','version')})
        for name,data in notices(files[0].read_bytes()).items():
            target=dest/'notices'/row['group']/row['name']/row['version']/name.replace('/','__')
            target.parent.mkdir(parents=True,exist_ok=True)
            if target.exists() and target.read_bytes()!=data:raise ValueError('Notice collision')
            target.write_bytes(data);item['notices'][name]=sha(target)
        for pom in folder.glob('*/*.pom'):
            target=dest/'poms'/row['group']/row['name']/row['version']/pom.name
            target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(pom,target)
    def source(key):
        group,name,version=key
        host='https://dl.google.com/dl/android/maven2/' if group.startswith('androidx.') else 'https://repo.maven.apache.org/maven2/'
        url=host+group.replace('.','/')+'/'+name+'/'+version+'/'+name+'-'+version+'-sources.jar'
        target=dest/'sources'/group/name/version/(name+'-'+version+'-sources.jar')
        try: result=download(url,target)
        except HTTPError as error:
            if error.code!=404:raise
            return key,{'url':url,'not_published':True}
        with zipfile.ZipFile(target) as z:
            if not z.testzip() is None:raise ValueError('Maven source archive invalid')
        for name,data in notices(target.read_bytes()).items():
            notice=dest/'notices'/group/key[1]/version/('source__'+name.replace('/','__'))
            notice.parent.mkdir(parents=True,exist_ok=True);notice.write_bytes(data)
        return key,result
    with ThreadPoolExecutor(max_workers=8) as executor:
        for key,result in executor.map(source,modules):modules[key]['source']=result
    license=dest/'LICENSE-APACHE-2.0.txt'
    download('https://www.apache.org/licenses/LICENSE-2.0.txt',license,1024*1024)
    shutil.copyfile(export/'project/android/build/quest3d-resolved-runtime-standard-release.json',dest/'resolved-runtime.json')
    return {'runtime_artifacts':len(records),'modules':list(modules.values()),'source_unavailable':[x['coordinate'] for x in modules.values() if x['source'].get('not_published')]}

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tools',required=True,type=Path)
    parser.add_argument('--export',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path)
    a=parser.parse_args()
    if a.output.exists():raise FileExistsError('Keep previous dependency receipts')
    a.output.mkdir(parents=True)
    result={'schema':1,'native':collect_native(a.tools,a.output),'maven':collect_maven(a.export,a.output),'published':False}
    result['files']={f.relative_to(a.output).as_posix():sha(f) for f in sorted(a.output.rglob('*')) if f.is_file()}
    (a.output/'dependency-source-notices.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({'native_packages':len(result['native']['native_packages']),'maven_modules':len(result['maven']['modules']),'missing_maven_sources':result['maven']['source_unavailable']},indent=2))

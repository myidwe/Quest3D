"""Keep an Android release identity outside the checkout with user DPAPI.

Windows-only private maintenance tool. Passwords are passed through pipes,
never arguments/environment/reports. --show-password is an explicit local
recovery action; never use its output in a CI or publication log.
"""
from __future__ import annotations
import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess

class Blob(ctypes.Structure):
    _fields_=[('length',wintypes.DWORD),('data',ctypes.POINTER(ctypes.c_byte))]

def dpapi(value:bytes, decrypt=False):
    if os.name!='nt':raise RuntimeError('Windows current-user DPAPI is required')
    buffer=ctypes.create_string_buffer(value)
    source=Blob(len(value),ctypes.cast(buffer,ctypes.POINTER(ctypes.c_byte)))
    target=Blob()
    crypt=ctypes.WinDLL('crypt32',use_last_error=True)
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    operation=crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    operation.argtypes=[ctypes.POINTER(Blob),ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(Blob)]
    operation.restype=wintypes.BOOL
    kernel.LocalFree.argtypes=[ctypes.c_void_p]
    kernel.LocalFree.restype=ctypes.c_void_p
    if not operation(ctypes.byref(source),None,None,None,None,1,ctypes.byref(target)):
        raise RuntimeError('Current-user DPAPI operation failed')
    try:return ctypes.string_at(target.data,target.length)
    finally:kernel.LocalFree(target.data)

def protect_directory(directory:Path):
    # Resolve the current SID without emitting identity/account details.
    sid=subprocess.check_output(['powershell.exe','-NoProfile','-Command',
        '[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value'],text=True).strip()
    if not sid.startswith('S-1-5-'):raise RuntimeError('Current Windows SID unavailable')
    process=subprocess.run(['icacls.exe',str(directory),'/inheritance:r','/grant:r',
                           '*'+sid+':(OI)(CI)F','*S-1-5-18:(OI)(CI)F'],capture_output=True)
    if process.returncode:raise RuntimeError('Private signing directory ACL could not be restricted')

def wsl_path(file:Path):
    file=file.resolve()
    if not file.drive or len(file.drive)!=2:raise ValueError('Use a local Windows drive')
    return '/mnt/'+file.drive[0].lower()+'/'+file.as_posix()[3:]

def create(directory:Path,linux_keytool:str,workspace:Path):
    directory=directory.resolve()
    if directory.is_relative_to(workspace.resolve()) or workspace.resolve().is_relative_to(directory):
        raise ValueError('Keep private signing files outside the source workspace')
    if directory.exists():raise FileExistsError('Preserve existing release identities')
    directory.mkdir(parents=True)
    protect_directory(directory)
    password=secrets.token_urlsafe(48)
    protected=dpapi(password.encode('ascii'))
    if dpapi(protected,True)!=password.encode('ascii'):raise RuntimeError('DPAPI recovery self-check failed')
    (directory/'password.dpapi').write_bytes(protected)
    keystore=directory/'Quest3D-release.p12'
    argv=['wsl.exe','-e',linux_keytool,'-genkeypair','-storetype','PKCS12','-keystore',wsl_path(keystore),
          '-alias','quest3d-release','-keyalg','RSA','-keysize','3072','-validity','10000',
          '-dname','CN=Quest3D Release, O=Quest3D']
    process=subprocess.run(argv,input=(password+'\n'+password+'\n').encode('ascii'),stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    if process.returncode:raise RuntimeError('Private release identity generation failed; preserve its directory')
    cert=subprocess.run(['wsl.exe','-e',linux_keytool,'-exportcert','-keystore',wsl_path(keystore),
                         '-alias','quest3d-release'],input=(password+'\n').encode('ascii'),stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    password=''
    if cert.returncode or not cert.stdout:raise RuntimeError('Public certificate verification failed')
    (directory/'release-certificate.der').write_bytes(cert.stdout)
    result={'schema':1,'kind':'durable-public-android-release-identity','alias':'quest3d-release',
            'certificate_sha256':hashlib.sha256(cert.stdout).hexdigest(),
            'private_key_exported':False,'password_storage':'Windows current-user DPAPI',
            'key_algorithm':'RSA-3072','validity_days':10000,'backup_verified':False}
    (directory/'public-identity.json').write_text(json.dumps(result,indent=2)+'\n','utf-8')
    (directory/'README.private.md').write_text('''# Quest3D release identity — private

Keep this directory outside every Git repository and Release bundle.
Quest3D-release.p12 and its password are required for future updates of
app.questto3d.client. Losing them prevents compatible signed updates.

password.dpapi is encrypted for the Windows account and device that created
it. Copying it to another PC is not a password backup. Preserve this Windows
profile until you have separately backed up the PKCS12 file and its password
in your own secure password manager/encrypted backup. No off-device backup
has been performed by the build tool.

To explicitly recover the password locally, run the workspace maintenance
tool windows_release_identity.py --show-password --private-dir THIS_DIRECTORY.
Never run that action through a published CI log or copy its output to Git.
The default tool does not print the password.
''','utf-8')
    return result

def sign_public(directory:Path,workspace:Path,tools:str,spec:Path,unsigned:Path,source:Path,output:Path):
    """Send recovered passwords to the audited signer through private stdin.

    This process never displays the password or raw signing-tool diagnostics.
    Source/build/notice gates and exact APK/certificate verification remain
    the responsibility of sign_quest_release.py; this wrapper cannot waive them.
    """
    identity=json.loads((directory/'public-identity.json').read_text('utf-8'))
    definition=json.loads(spec.read_text('utf-8'))
    if definition.get('apk_metadata',{}).get('certificate_sha256')!=identity['certificate_sha256']:
        raise ValueError('Signing specification must select the existing public certificate')
    prefix=tools.rstrip('/')
    argv=['wsl.exe','--cd',wsl_path(workspace),'-e',prefix+'/venv/bin/python',
          'scripts/release/sign_quest_release.py','--spec',wsl_path(spec),
          '--unsigned-apk',wsl_path(unsigned),'--source',wsl_path(source),
          '--aapt2',prefix+'/android-sdk/build-tools/36.1.0/aapt2','--execute',
          '--output',wsl_path(output),'--java',prefix+'/linux/jdk-17.0.20.1+1/bin/java',
          '--apksigner-jar',prefix+'/android-sdk/build-tools/36.1.0/lib/apksigner.jar',
          '--zipalign',prefix+'/android-sdk/build-tools/36.1.0/zipalign',
          '--keystore',wsl_path(directory/'Quest3D-release.p12'),'--alias',identity['alias'],
          '--passwords-from-stdin']
    recovered=dpapi((directory/'password.dpapi').read_bytes(),True)
    if any(value in recovered for value in (b'\r',b'\n',b'\x00')):
        raise ValueError('Recovered signing password is not a valid line')
    try:
        process=subprocess.run(argv,input=recovered+b'\n'+recovered+b'\n',
                               stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    finally:recovered=b''
    if process.returncode:
        raise RuntimeError('Audited public signing failed; private diagnostics were not published')
    result=json.loads(process.stdout.decode('utf-8'))
    if not result.get('signed') or result['apk_metadata']['certificate_sha256']!=identity['certificate_sha256']:
        raise RuntimeError('Public signing result failed identity verification')
    return result

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--private-dir',type=Path,required=True)
    parser.add_argument('--workspace',type=Path,default=Path(__file__).resolve().parents[2])
    parser.add_argument('--linux-keytool')
    parser.add_argument('--create',action='store_true')
    parser.add_argument('--show-password',action='store_true')
    parser.add_argument('--sign-spec',type=Path)
    parser.add_argument('--unsigned-apk',type=Path)
    parser.add_argument('--source',type=Path)
    parser.add_argument('--output',type=Path)
    parser.add_argument('--linux-tools',help='Exact pinned WSL tool cache for audited signing')
    a=parser.parse_args()
    if sum((a.create,a.show_password,bool(a.sign_spec)))>1:parser.error('Choose creation, signing, or explicit private recovery')
    if a.create:
        if not a.linux_keytool:parser.error('--linux-keytool is required')
        print(json.dumps(create(a.private_dir,a.linux_keytool,a.workspace),indent=2))
    elif a.show_password:
        print(dpapi((a.private_dir/'password.dpapi').read_bytes(),True).decode('ascii'))
    elif a.sign_spec:
        if not all((a.unsigned_apk,a.source,a.output,a.linux_tools)):
            parser.error('Audited signing requires --unsigned-apk --source --output --linux-tools')
        print(json.dumps(sign_public(a.private_dir,a.workspace,a.linux_tools,a.sign_spec,
                                     a.unsigned_apk,a.source,a.output),indent=2))
    else:
        print((a.private_dir/'public-identity.json').read_text('utf-8'))

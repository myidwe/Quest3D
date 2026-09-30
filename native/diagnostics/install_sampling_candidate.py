"""Install only the signed, byte-compared native video sampling candidate; keep app data."""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re

from select_quest_codec import adb, ROOT, PACKAGE, BASELINE_APK

BUILD = ROOT / 'artifacts/quest/layer-sampling-20260910/build-20260910-224719'
APK = BUILD / 'candidate-signed.apk'
SHA = 'd139c9a6b312a9b6956ada2119dfca6384f90d0187e7c61e5ec61289771874ad'
STATE = ('files/app_state.cfg','files/host_state.cfg','files/addons/nightfall-stream/config.ini')


def installed_hash():
    location = adb('shell','pm','path',PACKAGE).decode().strip()
    if not re.fullmatch(r'package:/data/app/\S+/base\.apk', location):
        raise RuntimeError('Unexpected installed APK location')
    return adb('shell','sha256sum',location[8:]).decode().split()[0]


def state_bytes():
    output = {}
    for path in STATE:
        data = adb('exec-out','run-as',PACKAGE,'cat',path)
        actual = adb('shell','run-as',PACKAGE,'sha256sum',path).decode().split()[0]
        if hashlib.sha256(data).hexdigest() != actual:
            raise RuntimeError('Raw device transfer differs from file hash')
        output[path] = data
    return output


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--check-only',action='store_true')
    args = p.parse_args()
    out = args.output.resolve()
    out.relative_to(ROOT / 'artifacts')
    if out.exists(): raise FileExistsError('Choose a new installation journal')
    if hashlib.sha256(APK.read_bytes()).hexdigest() != SHA:
        raise RuntimeError('Reviewed signed APK changed')
    compared = json.loads((BUILD/'apk-comparison-signed.json').read_text())
    if compared['status'] != 'PASS' or compared['apks']['candidate']['sha256'] != SHA:
        raise RuntimeError('Exact payload comparison missing')
    if installed_hash() != BASELINE_APK:
        raise RuntimeError('Installed app is no longer the reviewed baseline')
    before = state_bytes()
    out.mkdir(parents=True,exist_ok=False)
    for path,data in before.items():
        (out/Path(path).name).write_bytes(data)
    record = {'started_at':datetime.now().astimezone().isoformat(),
              'candidate_sha256':SHA,'baseline_sha256':BASELINE_APK,
              'check_only':args.check_only,'status':'preflight_pass',
              'wearer_verified':False,'sampling_on_device_verified':False,
              'before_state_sha256':{p:hashlib.sha256(v).hexdigest() for p,v in before.items()}}
    try:
        if not args.check_only:
            (out/'stop.log').write_bytes(adb('shell','am','force-stop',PACKAGE))
            if state_bytes() != before:
                raise RuntimeError('Settings changed during stop; preserve and review before installing')
            installed = adb('install','-r',str(APK))
            (out/'install.log').write_bytes(installed)
            if 'Success' not in installed.decode().splitlines():
                raise RuntimeError('Install did not report success')
            if installed_hash() != SHA or state_bytes() != before:
                raise RuntimeError('Installed APK or state preservation check failed')
            launch = adb('shell','am','start','-W','-n',PACKAGE+'/com.godot.game.GodotAppLauncher')
            (out/'launch.log').write_bytes(launch)
            pid = adb('shell','pidof',PACKAGE).decode().strip()
            activity = rb'(?m)^Activity: ' + re.escape(PACKAGE.encode()) + rb'/com\.godot\.game\.GodotApp(?:Launcher)?\r?$'
            if not re.search(rb'(?m)^Status: ok\r?$',launch) or not re.search(activity,launch) or not re.fullmatch(r'\d+',pid):
                raise RuntimeError('Application launch not verified')
            record.update(status='installed_and_launched',pid=int(pid),
                          state_bytes_preserved_after_install_before_launch=True)
    except BaseException as error:
        record.update(status='failed',error=f'{type(error).__name__}: {error}')
        raise
    finally:
        (out/'result.json').write_text(json.dumps(record,indent=2),encoding='utf-8')
    print(json.dumps(record,indent=2))


if __name__ == '__main__':
    main()

"""One read-only IPC snapshot of the active v2 stereo output, saved in the requested new artifact directory."""
import ctypes as C
from ctypes import wintypes as W
from dataclasses import asdict
from datetime import datetime
import hashlib
import json
from pathlib import Path
import tempfile
import time
import sys
import argparse

import numpy as np
from PIL import Image
from quest3d.bridge import CAPACITY, FrameHeader, HEADER_SIZE

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--session',type=Path,required=True)
parser.add_argument('--output',type=Path,required=True)
parser.add_argument('--require-3d',action='store_true')
args=parser.parse_args()
root=Path(__file__).resolve().parents[2]
session=args.session.resolve(strict=True)
output=args.output.resolve()
output.relative_to(root/'artifacts')
if output.exists():raise FileExistsError('Preserve prior snapshots; choose a new directory')
status = json.loads((session / 'status.json').read_text(encoding='utf-8'))
k = C.WinDLL('kernel32', use_last_error=True)
for name, args, result in [
    ('OpenMutexW', [W.DWORD,W.BOOL,W.LPCWSTR], W.HANDLE),
    ('OpenFileMappingW', [W.DWORD,W.BOOL,W.LPCWSTR], W.HANDLE),
    ('MapViewOfFile', [W.HANDLE,W.DWORD,W.DWORD,W.DWORD,C.c_size_t], C.c_void_p),
    ('UnmapViewOfFile', [C.c_void_p], W.BOOL),
    ('WaitForSingleObject', [W.HANDLE,W.DWORD], W.DWORD),
    ('ReleaseMutex', [W.HANDLE], W.BOOL), ('CloseHandle', [W.HANDLE], W.BOOL)]:
    fn = getattr(k, name)
    fn.argtypes, fn.restype = args, result
mutex = mapping = pointer = None
try:
    mutex = k.OpenMutexW(0x100001, False, 'Local\\Quest3D.Frame.Mutex.v2')
    mapping = k.OpenFileMappingW(4, False, 'Local\\Quest3D.Frame.v2')
    if not mutex or not mapping:
        raise C.WinError(C.get_last_error())
    pointer = k.MapViewOfFile(mapping, 4, 0, 0, CAPACITY)
    if not pointer:
        raise C.WinError(C.get_last_error())
    result = k.WaitForSingleObject(mutex, 100)
    if result == 0x80:
        k.ReleaseMutex(mutex)
        raise RuntimeError('Abandoned producer; no valid snapshot')
    if result != 0:
        raise RuntimeError(f'Mutex wait failed: {result}')
    started = time.perf_counter_ns()
    try:
        header = FrameHeader.unpack(C.string_at(pointer, HEADER_SIZE))
        if header.stream_epoch != status['stream_epoch']:
            raise RuntimeError('Observed epoch changed')
        if '--require-3d' in sys.argv and header.flags & 2:
            raise RuntimeError('The actual published frame is 2D; no 3D snapshot saved')
        payload_bytes=header.width*header.height*4
        if payload_bytes<=0 or payload_bytes>CAPACITY-HEADER_SIZE:
            raise RuntimeError('Invalid shared payload dimensions')
        if (header.width,header.height)!=(status['eye_width']*2,status['eye_height']):
            raise RuntimeError('Published shape differs from bound session')
        pixels = C.string_at(pointer + HEADER_SIZE, payload_bytes)
    finally:
        k.ReleaseMutex(mutex)
    held_ms = (time.perf_counter_ns()-started)/1e6
finally:
    if pointer: k.UnmapViewOfFile(pointer)
    if mapping: k.CloseHandle(mapping)
    if mutex: k.CloseHandle(mutex)

out=output
out.mkdir(parents=True,exist_ok=False)
bgra = np.frombuffer(pixels, dtype=np.uint8).reshape(header.height, header.width, 4)
rgb = bgra[:, :, [2,1,0]]
Image.fromarray(rgb).save(out / 'sbs.png')
Image.fromarray(rgb[:, :header.width//2]).save(out / 'left.png')
Image.fromarray(rgb[:, header.width//2:]).save(out / 'right.png')
record = {'recorded_at':datetime.now().astimezone().isoformat(), 'header':asdict(header),
          'payload_sha256':hashlib.sha256(pixels).hexdigest(), 'read_lock_ms':held_ms,
          'eye_pixels_identical':bool(np.array_equal(bgra[:,:header.width//2],bgra[:,header.width//2:])),
          'session_id':status['session_id'], 'mode_in_status_before_read':status['effective_mode'],
          'disparity_before_read':status['disparity'], 'path':str(out),
          'scope':'Actual pre-encode shared SBS from current producer, one atomic read; no source-frame synchronization with OS screenshot and no headset optical claim'}
(out / 'snapshot.json').write_text(json.dumps(record,indent=2),encoding='utf-8')
print(json.dumps(record,indent=2))

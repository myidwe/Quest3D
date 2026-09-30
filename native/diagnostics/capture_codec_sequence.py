"""Read a bounded sequence of actual pre-encode SBS publications, without changing the producer."""
import argparse
import ctypes as C
from ctypes import wintypes as W
from dataclasses import asdict
from datetime import datetime
import hashlib
import json
from pathlib import Path
import time

import numpy as np
from PIL import Image
from quest3d.bridge import CAPACITY, FrameHeader, HEADER_SIZE


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--session', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--frames', type=int, default=60)
    args = parser.parse_args()
    if not 2 <= args.frames <= 90:
        parser.error('frames must be 2..90 (bounded memory)')
    root = Path(__file__).resolve().parents[2]
    output = args.output.resolve()
    output.relative_to(root / 'artifacts')
    if output.exists():
        raise FileExistsError('Choose a new output directory')
    session = args.session.resolve(strict=True)
    initial = json.loads((session / 'status.json').read_text(encoding='utf-8-sig'))
    if not initial['running'] or initial['effective_mode'] != '3d':
        raise RuntimeError('An existing running 3D session is required')
    k = C.WinDLL('kernel32', use_last_error=True)
    for name, argtypes, restype in [
        ('OpenMutexW', [W.DWORD,W.BOOL,W.LPCWSTR], W.HANDLE),
        ('OpenFileMappingW', [W.DWORD,W.BOOL,W.LPCWSTR], W.HANDLE),
        ('MapViewOfFile', [W.HANDLE,W.DWORD,W.DWORD,W.DWORD,C.c_size_t], C.c_void_p),
        ('UnmapViewOfFile', [C.c_void_p], W.BOOL),
        ('WaitForSingleObject', [W.HANDLE,W.DWORD], W.DWORD),
        ('ReleaseMutex', [W.HANDLE], W.BOOL), ('CloseHandle', [W.HANDLE], W.BOOL)]:
        fn = getattr(k, name)
        fn.argtypes, fn.restype = argtypes, restype
    mutex = mapping = pointer = None
    saved, seen, samples = [], set(), []
    before = time.perf_counter_ns()
    try:
        mutex = k.OpenMutexW(0x100001, False, 'Local\\Quest3D.Frame.Mutex.v2')
        mapping = k.OpenFileMappingW(4, False, 'Local\\Quest3D.Frame.v2')
        if not mutex or not mapping:
            raise C.WinError(C.get_last_error())
        pointer = k.MapViewOfFile(mapping, 4, 0, 0, CAPACITY)
        if not pointer:
            raise C.WinError(C.get_last_error())
        while len(saved) < args.frames and time.perf_counter_ns() - before < 12_000_000_000:
            tick = time.perf_counter_ns()
            result = k.WaitForSingleObject(mutex, 30)
            if result == 0x102:
                samples.append({'timeout': True})
                continue
            if result == 0x80:
                k.ReleaseMutex(mutex)
                raise RuntimeError('Abandoned producer mutex')
            if result != 0:
                raise RuntimeError(f'Wait failed: {result}')
            held = time.perf_counter_ns()
            try:
                header = FrameHeader.unpack(C.string_at(pointer, HEADER_SIZE))
                if header.stream_epoch != initial['stream_epoch'] or header.flags & 2:
                    raise RuntimeError('Bound source changed or returned to 2D')
                if (header.width, header.height) != (initial['eye_width'] * 2, initial['eye_height']):
                    raise RuntimeError('Bound geometry changed')
                identity = (header.generation, header.capture_ns)
                if identity not in seen:
                    # Each frame owns independent memory. No hash, compression or disk IO under the mutex.
                    array = np.empty((header.height, header.width, 4), dtype=np.uint8)
                    C.memmove(array.ctypes.data, pointer + HEADER_SIZE, array.nbytes)
                    saved.append((header, array))
                    seen.add(identity)
            finally:
                k.ReleaseMutex(mutex)
            samples.append({'wait_ms': (held-tick)/1e6, 'lock_ms': (time.perf_counter_ns()-held)/1e6})
            time.sleep(0.012)
    finally:
        if pointer: k.UnmapViewOfFile(pointer)
        if mapping: k.CloseHandle(mapping)
        if mutex: k.CloseHandle(mutex)
    if len(saved) < 2:
        raise RuntimeError('Insufficient unique capture timestamps')
    final = json.loads((session / 'status.json').read_text(encoding='utf-8-sig'))
    if any(final[key] != initial[key] for key in ('session_id','stream_epoch','revision','disparity')):
        raise RuntimeError('Session settings changed while collecting')
    output.mkdir(parents=True, exist_ok=False)
    rows, hashes = [], set()
    deltas = []
    for index, (header, frame) in enumerate(saved):
        path = output / f'frame-{index:04d}.npy'
        np.save(path, frame, allow_pickle=False)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        payload_digest = hashlib.sha256(memoryview(frame).cast('B')).hexdigest()
        hashes.add(payload_digest)
        rows.append({'path':path.name, 'sha256':digest, 'pixel_format':'bgra',
                     'frame_id':header.frame_id, 'captured_ns':header.capture_ns,
                     'header':asdict(header), 'payload_sha256':payload_digest})
        if index:
            deltas.append(float(np.abs(frame[::8,::8,:3].astype(np.int16) - saved[index-1][1][::8,::8,:3]).mean()))
    Image.fromarray(saved[0][1][:,:,[2,1,0]]).save(output / 'first-sbs.png')
    Image.fromarray(saved[-1][1][:,:,[2,1,0]]).save(output / 'last-sbs.png')
    manifest = {'schema':1, 'pixel_format':'bgra', 'frames':rows, 'rois':[],
                'recorded_at':datetime.now().astimezone().isoformat(),
                'session_id':initial['session_id'], 'stream_epoch':initial['stream_epoch'],
                'disparity':initial['disparity'], 'revision':initial['revision'],
                'unique_payloads':len(hashes), 'sampled_frame_delta_mae':deltas,
                'capture_elapsed_s':(saved[-1][0].capture_ns-saved[0][0].capture_ns)/1e9,
                'lock_samples':samples,
                'scope':'Actual same-sequence pre-encode 3D SBS. Unique capture timestamps, not v3 source identity. Original cadence is retained in metadata; benchmark CFR is a separate controlled encode rate. Read-only sampling adds measured mutex/copy overhead. No Quest optical validation.'}
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    print(json.dumps({key:manifest[key] for key in ('session_id','unique_payloads','capture_elapsed_s','scope')},indent=2))
    print(f'Saved {len(rows)} frames: {output / "manifest.json"}')


if __name__ == '__main__':
    main()

"""Test-owned child only: raw line transport, real process/job descendants, and controlled failures."""
import ctypes as C
from ctypes import wintypes as W
import json
import os
import subprocess
import sys
import time

kernel=C.WinDLL("kernel32",use_last_error=True)
kernel.GetCurrentProcess.restype=W.HANDLE
kernel.GetProcessTimes.argtypes=[W.HANDLE,C.POINTER(W.FILETIME),C.POINTER(W.FILETIME),C.POINTER(W.FILETIME),C.POINTER(W.FILETIME)]


def birth(handle):
    c,e,k,u=(W.FILETIME() for _ in range(4))
    if not kernel.GetProcessTimes(handle,C.byref(c),C.byref(e),C.byref(k),C.byref(u)):
        raise C.WinError(C.get_last_error())
    return (c.dwHighDateTime<<32)|c.dwLowDateTime


def send(value):
    sys.stdout.buffer.write(json.dumps(value,separators=(",", ":")).encode()+b"\n")
    sys.stdout.buffer.flush()


send({"pid":os.getpid(),"creation":birth(kernel.GetCurrentProcess())})
sys.stdin.buffer.readline()  # Parent validates real live worker identity before releasing this fixture.
mode=sys.argv[1]
sys.stderr.write("fixture stderr only\n"); sys.stderr.flush()
if mode=="echo":
    for line in sys.stdin.buffer:
        sys.stdout.buffer.write(line); sys.stdout.buffer.flush()
elif mode=="argv":
    send(sys.argv[2:])
elif mode=="partial":
    sys.stdout.buffer.write(b'{"partial":'); sys.stdout.buffer.flush()
    sys.stdin.buffer.readline()
    sys.stdout.buffer.write(b'true}\n{"coalesced":1}\n'); sys.stdout.buffer.flush()
elif mode=="oversize":
    sys.stdout.buffer.write(b"x"*8193+b"\n"); sys.stdout.buffer.flush()
elif mode=="cr":
    sys.stdout.buffer.write(b'{"bad":true}\r\n'); sys.stdout.buffer.flush()
elif mode=="truncated":
    sys.stdout.buffer.write(b'{"truncated":'); sys.stdout.buffer.flush()
elif mode=="sleep":
    time.sleep(60)
elif mode=="tree":
    child=subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"],
                           stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    send({"pid":child.pid,"creation":birth(W.HANDLE(child._handle))})
else:
    raise ValueError("Unknown fixture mode")

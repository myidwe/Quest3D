"""Diagnostic type-only inventory of this process's handles; no object names."""
import ctypes as C
from ctypes import wintypes as W

class Entry(C.Structure):
    _fields_ = [("handle", W.HANDLE), ("handle_count", C.c_size_t), ("pointer_count", C.c_size_t),
                ("access", W.ULONG), ("type_index", W.ULONG), ("attributes", W.ULONG), ("reserved", W.ULONG)]

class String(C.Structure):
    _fields_ = [("length", W.USHORT), ("capacity", W.USHORT), ("buffer", C.c_void_p)]

def inventory():
    native = C.WinDLL("ntdll")
    native.NtQueryInformationProcess.argtypes = [W.HANDLE, W.ULONG, C.c_void_p, W.ULONG, C.POINTER(W.ULONG)]
    native.NtQueryInformationProcess.restype = C.c_long
    native.NtQueryObject.argtypes = [W.HANDLE, W.ULONG, C.c_void_p, W.ULONG, C.POINTER(W.ULONG)]
    native.NtQueryObject.restype = C.c_long
    size = 65536
    needed = W.ULONG()
    while True:
        data = C.create_string_buffer(size)
        code = native.NtQueryInformationProcess(W.HANDLE(-1), 51, data, size, C.byref(needed))
        if code == 0:
            break
        if code not in (-1073741820, -1073741789) or size >= 16777216:
            raise OSError(f"Own process handle query failed: {code}")
        size = max(size * 2, needed.value)
    count = C.c_size_t.from_buffer(data).value
    assert count <= (size - C.sizeof(C.c_size_t) * 2) // C.sizeof(Entry)
    result = {}
    for index in range(count):
        entry = Entry.from_buffer(data, C.sizeof(C.c_size_t) * 2 + index * C.sizeof(Entry))
        info = C.create_string_buffer(2048)
        if native.NtQueryObject(entry.handle, 2, info, len(info), C.byref(needed)) == 0:
            name = String.from_buffer(info)
            text = C.wstring_at(name.buffer, name.length // 2)
            result[int(entry.handle)] = text
    return result

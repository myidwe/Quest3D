"""Immutable source lifetime carried with pixels, never reconstructed from UI status."""
from dataclasses import dataclass
from enum import IntEnum
import struct

from .geometry import ScreenRect


class SourceKind(IntEnum):
    MONITOR = 1
    WINDOW = 2
    VIDEO = 3
    PHOTO = 4


SOURCE_EXTENSION = struct.Struct("<IIQQQ2i2I16x")
assert SOURCE_EXTENSION.size == 64


@dataclass(frozen=True, slots=True)
class SourceIdentity:
    kind: SourceKind
    process_id: int
    selection_id: int
    native_handle: int
    creation_filetime: int
    parent_bounds: ScreenRect

    def __post_init__(self):
        if type(self.kind) is not SourceKind:
            raise ValueError("Source kind must be an explicit SourceKind")
        for name, maximum, minimum in (("process_id", (1 << 32) - 1, 0),
                ("selection_id", (1 << 64) - 1, 1), ("native_handle", (1 << 64) - 1, 0),
                ("creation_filetime", (1 << 64) - 1, 0)):
            value = getattr(self, name)
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError(f"Invalid source {name}")
        if not isinstance(self.parent_bounds, ScreenRect):
            raise ValueError("Source parent bounds are required")
        bounds = self.parent_bounds
        if (not all(-(1 << 31) <= value < (1 << 31) for value in (bounds.left, bounds.top))
                or not all(0 < value < (1 << 32) for value in (bounds.width, bounds.height))):
            raise ValueError("Source bounds exceed the wire range")
        if self.kind == SourceKind.WINDOW:
            if not all((self.process_id, self.native_handle, self.creation_filetime)):
                raise ValueError("Window source requires HWND and process lifetime")
        elif self.kind == SourceKind.MONITOR:
            if not self.native_handle or self.process_id or self.creation_filetime:
                raise ValueError("Monitor source requires HMONITOR, without a process identity")
        elif (self.native_handle or self.process_id or self.creation_filetime
                or bounds.left or bounds.top):
            raise ValueError("File source cannot claim Windows input identity")

    def validate_region(self, source_rect, *, input_enabled=False):
        if not self.parent_bounds.contains(ScreenRect(*source_rect)):
            raise ValueError("Source crop lies outside its immutable parent bounds")
        if input_enabled and self.kind not in (SourceKind.MONITOR, SourceKind.WINDOW):
            raise ValueError("File source cannot enable OS input")

    def pack(self):
        b = self.parent_bounds
        return SOURCE_EXTENSION.pack(int(self.kind), self.process_id, self.selection_id,
            self.native_handle, self.creation_filetime, b.left, b.top, b.width, b.height)

    @classmethod
    def unpack(cls, data):
        fields = SOURCE_EXTENSION.unpack(data)
        if any(data[48:64]):
            raise ValueError("Reserved source identity bytes must be zero")
        return cls(SourceKind(fields[0]), *fields[1:5], ScreenRect(*fields[5:9]))

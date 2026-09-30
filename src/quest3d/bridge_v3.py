"""Explicit v3 frames; v2 reserved bytes retain their original meaning."""
from dataclasses import asdict, dataclass
import struct

from .bridge import FrameHeader, FULL_SBS, ORIGINAL_2D, INPUT_ENABLED
from .source_identity import SourceIdentity

MAGIC_V3 = b"Q3DFRM3\0"
HEADER_SIZE_V3 = 192


@dataclass(frozen=True)
class SourceFrameHeader(FrameHeader):
    source_identity: SourceIdentity | None = None

    def pack(self):
        prefix = bytearray(super().pack())
        if not isinstance(self.source_identity, SourceIdentity):
            raise ValueError("Protocol v3 requires an immutable source identity")
        self.source_identity.validate_region((self.source_left, self.source_top,
            self.source_width, self.source_height), input_enabled=bool(self.flags & INPUT_ENABLED))
        if self.flags & INPUT_ENABLED and self.flags & (FULL_SBS | ORIGINAL_2D) != (FULL_SBS | ORIGINAL_2D):
            raise ValueError("OS input requires unwarped original 2D")
        prefix[:8] = MAGIC_V3
        struct.pack_into("<II", prefix, 8, 3, HEADER_SIZE_V3)
        return bytes(prefix) + self.source_identity.pack()

    @classmethod
    def unpack(cls, data):
        if len(data) != HEADER_SIZE_V3 or data[:8] != MAGIC_V3 or struct.unpack_from("<II", data, 8) != (3, HEADER_SIZE_V3):
            raise ValueError("Unsupported v3 bridge header")
        prefix = bytearray(data[:128])
        prefix[:8] = b"Q3DFRM2\0"
        struct.pack_into("<II", prefix, 8, 2, 128)
        original = FrameHeader.unpack(prefix)
        result = cls(**asdict(original), source_identity=SourceIdentity.unpack(data[128:]))
        result.pack()
        return result

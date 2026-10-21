"""Stand-ins for a game client, so memory objects run without one.

FakeProcess replaces pymem.Pymem with a sparse address space the test lays out by
hand. Reads and writes outside what it mapped fail the way pymem's do, which is
what the error paths in MemoryReader are written against.
"""

import struct

import pymem.exception


# Far above anything a test maps by hand, and 64 KB aligned as VirtualAllocEx's are.
_ALLOCATION_BASE = 0x7F00_0000_0000
_ALLOCATION_GRANULARITY = 0x1_0000


class FakeProcess:
    """A pymem.Pymem whose memory is whatever the test maps."""

    def __init__(self):
        self.process_handle = 1
        self._regions: dict[int, bytearray] = {}
        self._next_allocation = _ALLOCATION_BASE
        self.freed: list[int] = []
        self.threads: list[int] = []
        self.closed = False

    # --- laying memory out -------------------------------------------------

    def map(self, address: int, data: bytes = b"", size: int = None) -> int:
        """Map `size` bytes at `address` (at least `len(data)`), starting with `data`."""
        size = max(size or 0, len(data))
        region = bytearray(size)
        region[: len(data)] = data
        self._regions[address] = region
        return address

    def put(self, address: int, fmt: str, *values) -> int:
        """Write `values` packed little-endian as `fmt`, mapping the bytes if need be."""
        data = struct.pack("<" + fmt, *values)
        try:
            self._locate(address, len(data))
        except pymem.exception.MemoryReadError:
            self.map(address, data)
        else:
            self.write_bytes(address, data, len(data))
        return address

    def get(self, address: int, fmt: str):
        values = struct.unpack("<" + fmt, self.read_bytes(address, struct.calcsize("<" + fmt)))
        return values[0] if len(values) == 1 else values

    def _locate(self, address: int, size: int) -> tuple[bytearray, int]:
        for base, region in self._regions.items():
            if base <= address and address + size <= base + len(region):
                return region, address - base
        raise pymem.exception.MemoryReadError(address, size)

    # --- what pymem.Pymem offers -------------------------------------------

    def read_bytes(self, address: int, size: int) -> bytes:
        if self.closed:
            raise pymem.exception.ProcessError("handle closed")
        region, offset = self._locate(address, size)
        return bytes(region[offset : offset + size])

    def write_bytes(self, address: int, value: bytes, size: int):
        if self.closed:
            raise pymem.exception.ProcessError("handle closed")
        try:
            region, offset = self._locate(address, size)
        except pymem.exception.MemoryReadError:
            raise pymem.exception.MemoryWriteError(address, value)
        region[offset : offset + size] = value[:size]

    def allocate(self, size: int) -> int:
        address = self._next_allocation
        self._next_allocation += -(-size // _ALLOCATION_GRANULARITY) * _ALLOCATION_GRANULARITY
        return self.map(address, size=size)

    def free(self, address: int):
        self.freed.append(address)

    def start_thread(self, address: int):
        self.threads.append(address)

    def close_process(self):
        self.closed = True
        self.process_handle = None


def pack_string(text: str, *, wide: bool = False, heap: int = None, process: FakeProcess = None) -> bytes:
    """The 32 bytes of an MSVC std::basic_string holding `text`.

    Inline while it fits the 16-byte buffer, unless `heap` is given: then the
    characters go at `heap` in `process` and the union holds the pointer, with a
    capacity that says so.
    """
    encoded = text.encode("utf-16-le" if wide else "utf-8")
    char_size = 2 if wide else 1
    buf_chars = 8 if wide else 16
    length = len(encoded) // char_size

    if heap is None:
        if length >= buf_chars:
            raise ValueError("too long to be inline; pass heap")
        union = encoded.ljust(16, b"\x00")
        capacity = buf_chars - 1
    else:
        process.map(heap, encoded + b"\x00" * char_size)
        union = struct.pack("<Q", heap) + b"\x00" * 8
        capacity = max(length, buf_chars)

    return union + struct.pack("<QQ", length, capacity)

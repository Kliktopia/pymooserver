"""Checked little-endian primitives for MooAPI packets."""

from __future__ import annotations

from dataclasses import dataclass
import struct
from typing import Generic, TypeVar


P = TypeVar("P")


@dataclass(frozen=True, slots=True)
class CodecLimits:
    max_name_bytes: int = 64
    max_message_bytes: int = 65_536
    max_motd_bytes: int = 1_024
    max_ip_bytes: int = 64
    max_recv_buffer: int = 256 * 1024


@dataclass(frozen=True, slots=True)
class Decoded(Generic[P]):
    packet: P
    consumed: int


@dataclass(frozen=True, slots=True)
class NeedMore:
    """A structurally plausible packet is incomplete.

    ``required_total`` is the minimum total byte count currently known to be needed.
    """

    required_total: int


@dataclass(frozen=True, slots=True)
class Error:
    """Malformed or policy-invalid wire data."""

    message: str


class _NeedMore(Exception):
    def __init__(self, required_total: int):
        self.required_total = required_total
        super().__init__(f"need at least {required_total} bytes")


class _Malformed(Exception):
    pass


class Reader:
    """Small bounded reader over a single candidate packet."""

    __slots__ = ("_data", "offset")

    def __init__(self, data: bytes | bytearray | memoryview, offset: int = 0):
        self._data = memoryview(data)
        self.offset = offset

    @property
    def total(self) -> int:
        return len(self._data)

    def _need(self, count: int) -> None:
        required = self.offset + count
        if self.total < required:
            raise _NeedMore(required)

    def u8(self) -> int:
        self._need(1)
        value = self._data[self.offset]
        self.offset += 1
        return int(value)

    def i16(self) -> int:
        self._need(2)
        value = struct.unpack_from("<h", self._data, self.offset)[0]
        self.offset += 2
        return value

    def u32(self) -> int:
        self._need(4)
        value = struct.unpack_from("<I", self._data, self.offset)[0]
        self.offset += 4
        return value

    def raw(self, count: int) -> bytes:
        self._need(count)
        start = self.offset
        self.offset += count
        return bytes(self._data[start:self.offset])

    def blob_u32(
        self,
        *,
        label: str,
        maximum: int,
        minimum: int = 0,
        reject_nul: bool = False,
    ) -> bytes:
        # check the unsigned length against the configured cap
        # before waiting for the body.
        length = self.u32()
        if length > maximum:
            raise _Malformed(f"{label} length {length} exceeds cap {maximum}")
        if length < minimum:
            raise _Malformed(f"{label} length {length} is below minimum {minimum}")
        value = self.raw(length)
        if reject_nul and b"\x00" in value:
            raise _Malformed(f"{label} contains NUL")
        return value


def ensure_i16(value: int, label: str = "i16") -> int:
    if not -32768 <= value <= 32767:
        raise ValueError(f"{label} out of range: {value}")
    return value


def ensure_u32(value: int, label: str = "u32", *, allow_zero: bool = True) -> int:
    if not 0 <= value <= 0xFFFFFFFF:
        raise ValueError(f"{label} out of range: {value}")
    if not allow_zero and value == 0:
        raise ValueError(f"{label} must not be zero")
    return value


def pack_i16(value: int) -> bytes:
    return struct.pack("<h", ensure_i16(value))


def pack_u32(value: int, *, allow_zero: bool = True, label: str = "u32") -> bytes:
    return struct.pack("<I", ensure_u32(value, label, allow_zero=allow_zero))


def pack_blob_u32(
    value: bytes,
    *,
    label: str,
    maximum: int,
    minimum: int = 0,
    reject_nul: bool = False,
) -> bytes:
    if not isinstance(value, bytes):
        raise TypeError(f"{label} must be bytes")
    length = len(value)
    if length > maximum:
        raise ValueError(f"{label} length {length} exceeds cap {maximum}")
    if length < minimum:
        raise ValueError(f"{label} length {length} is below minimum {minimum}")
    if reject_nul and b"\x00" in value:
        raise ValueError(f"{label} contains NUL")
    return struct.pack("<I", length) + value

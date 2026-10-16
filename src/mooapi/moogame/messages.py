"""Optional MooGame typed-payload helpers."""

from __future__ import annotations

from dataclasses import dataclass
import struct


@dataclass(frozen=True, slots=True)
class StringMessage:
    data: bytes


@dataclass(frozen=True, slots=True)
class NumberMessage:
    value: int


@dataclass(frozen=True, slots=True)
class BinaryMessage:
    data: bytes


@dataclass(frozen=True, slots=True)
class ObjectMessage:
    """MooGame type 0x0A: serialized object-information message.

    Vendor documentation names the corresponding event **On Object Message** and says
    the payload is consumed by **Load Object**.  The server still treats the bytes as
    opaque; this class is only an opt-in application helper.
    """

    data: bytes


# Backward-compatible alias. Prefer ObjectMessage in new code.
Type0AMessage = ObjectMessage


@dataclass(frozen=True, slots=True)
class TrackingMessage:
    type_id: int
    data: bytes


@dataclass(frozen=True, slots=True)
class IniReply:
    type_id: int
    value: int | bytes


def decode_message(data: bytes):
    if not data:
        raise ValueError("empty MooGame payload")
    kind = data[0]
    body = data[1:]
    if kind == 0x07:
        return StringMessage(body)
    if kind == 0x08:
        if len(body) != 4:
            raise ValueError("number payload must contain one i32")
        return NumberMessage(struct.unpack("<i", body)[0])
    if kind == 0x09:
        return BinaryMessage(body)
    if kind == 0x0A:
        return ObjectMessage(body)
    if kind == 0x0F:
        if len(body) != 4:
            raise ValueError("INI value reply must contain one i32")
        return IniReply(kind, struct.unpack("<i", body)[0])
    if kind == 0x10:
        return IniReply(kind, body)
    if 0x14 <= kind <= 0x17:
        return TrackingMessage(kind, body)
    return None


def encode_message(message) -> bytes:
    if isinstance(message, StringMessage):
        return b"\x07" + message.data
    if isinstance(message, NumberMessage):
        if not -(2**31) <= message.value <= 2**31 - 1:
            raise ValueError("number outside i32")
        return b"\x08" + struct.pack("<i", message.value)
    if isinstance(message, BinaryMessage):
        return b"\x09" + message.data
    if isinstance(message, ObjectMessage):
        return b"\x0A" + message.data
    if isinstance(message, TrackingMessage):
        if not 0x14 <= message.type_id <= 0x17:
            raise ValueError("tracking type must be 0x14..0x17")
        return bytes((message.type_id,)) + message.data
    if isinstance(message, IniReply):
        if message.type_id == 0x0F and isinstance(message.value, int):
            return b"\x0F" + struct.pack("<i", message.value)
        if message.type_id == 0x10 and isinstance(message.value, bytes):
            return b"\x10" + message.value
    raise TypeError(f"unsupported MooGame message: {message!r}")

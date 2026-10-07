"""Dialect-B MooAPI UDP datagram codec."""

from __future__ import annotations

from .packets import UdpChannelBlast, UdpPacket, UdpPrivateBlast, UdpToServer
from .primitives import Error, Reader, _Malformed, _NeedMore, pack_i16, pack_u32


def decode_udp_b(data: bytes | bytearray | memoryview, *, maximum: int = 4096):
    if len(data) > maximum:
        return Error(f"UDP datagram length {len(data)} exceeds cap {maximum}")
    try:
        r = Reader(data)
        packet_id = r.u8()
        if packet_id == 0x01:
            packet = UdpChannelBlast(
                r.i16(), r.u32(), r.u32(), 0, r.raw(r.total - r.offset)
            )
        elif packet_id == 0x02:
            packet = UdpToServer(r.i16(), r.u32(), r.raw(r.total - r.offset))
        elif packet_id == 0x03:
            packet = UdpPrivateBlast(
                r.i16(), r.u32(), r.u32(), r.u32(), r.raw(r.total - r.offset)
            )
        else:
            raise _Malformed(f"unknown dialect-B UDP packet id 0x{packet_id:02X}")
        return packet
    except _NeedMore as exc:
        return Error(f"truncated UDP datagram; need at least {exc.required_total} bytes")
    except _Malformed as exc:
        return Error(str(exc))


def encode_udp_b(packet: UdpPacket, *, maximum: int = 4096) -> bytes:
    if isinstance(packet, UdpChannelBlast):
        payload = (
            b"\x01"
            + pack_i16(packet.subchannel)
            + pack_u32(packet.channel_id, label="channel_id")
            + pack_u32(packet.sender_id, label="sender_id")
            + packet.data
        )
    elif isinstance(packet, UdpToServer):
        payload = (
            b"\x02"
            + pack_i16(packet.subchannel)
            + pack_u32(packet.sender_id, label="sender_id")
            + packet.data
        )
    elif isinstance(packet, UdpPrivateBlast):
        payload = (
            b"\x03"
            + pack_i16(packet.subchannel)
            + pack_u32(packet.channel_id, label="channel_id")
            + pack_u32(packet.sender_id, label="sender_id")
            + pack_u32(packet.target_id, label="target_id")
            + packet.data
        )
    else:
        raise TypeError(f"unsupported dialect-B UDP packet: {type(packet).__name__}")
    if len(payload) > maximum:
        raise ValueError(f"UDP datagram length {len(payload)} exceeds cap {maximum}")
    return payload

"""Dialect-A MooAPI TCP codec."""

from __future__ import annotations

from .packets import (
    Alias,
    AssignedId,
    ChannelMessage,
    ClientPacket,
    Exists,
    FromChannel,
    Hello,
    Join,
    Joined,
    Leave,
    Left,
    Motd,
    PrivateMessage,
    Rename,
    ServerPacket,
    ToServer,
    Welcome,
)
from .primitives import (
    CodecLimits,
    Decoded,
    Error,
    NeedMore,
    Reader,
    _Malformed,
    _NeedMore,
    pack_blob_u32,
    pack_i16,
    pack_u32,
)


DEFAULT_LIMITS = CodecLimits()


def _name(reader: Reader, limits: CodecLimits, label: str = "name") -> bytes:
    return reader.blob_u32(
        label=label,
        maximum=limits.max_name_bytes,
        minimum=1,
        reject_nul=True,
    )


def _message(reader: Reader, limits: CodecLimits, label: str = "message") -> bytes:
    return reader.blob_u32(
        label=label,
        maximum=limits.max_message_bytes,
        minimum=1,
    )


def _ip(reader: Reader, limits: CodecLimits) -> bytes:
    return reader.blob_u32(
        label="ip",
        maximum=limits.max_ip_bytes,
        minimum=1,
        reject_nul=True,
    )


def _as_result(parse):
    try:
        return parse()
    except _NeedMore as exc:
        return NeedMore(exc.required_total)
    except _Malformed as exc:
        return Error(str(exc))


# ---------------------------------------------------------------------------
# Client -> server
# ---------------------------------------------------------------------------

def decode_client_packet(
    data: bytes | bytearray | memoryview,
    limits: CodecLimits = DEFAULT_LIMITS,
):
    """Decode exactly one client->server TCP packet from the beginning of *data*.

    Trailing bytes are allowed and reported via ``Decoded.consumed`` so a stream
    decoder can drain multiple packets from one TCP read.
    """

    def parse():
        r = Reader(data)
        packet_id = r.u8()

        if packet_id == 0x01:
            packet = ChannelMessage(r.i16(), r.u32(), _message(r, limits))
        elif packet_id == 0x02:
            packet = ToServer(r.i16(), _message(r, limits))
        elif packet_id == 0x03:
            packet = PrivateMessage(r.i16(), r.u32(), r.u32(), _message(r, limits))
        elif packet_id == 0x04:
            packet = Join(_name(r, limits, "session name"))
        elif packet_id == 0x05:
            packet = Leave(r.u32())
        elif packet_id == 0x0B:
            packet = Rename(_name(r, limits, "alias"))
        elif packet_id == 0x0C:
            packet = Hello(r.i16(), _name(r, limits, "hello name"))
        else:
            # treat unknown packet IDs as invalid rather than waiting
            # them as protocol errors so the transport can close the connection.
            raise _Malformed(f"unknown client packet id 0x{packet_id:02X}")

        return Decoded(packet, r.offset)

    return _as_result(parse)


def encode_client_packet(packet: ClientPacket, limits: CodecLimits = DEFAULT_LIMITS) -> bytes:
    """Encode a dialect-A client->server packet.

    This is mainly useful for fixtures and a future test client.
    """

    if isinstance(packet, ChannelMessage):
        return (
            b"\x01"
            + pack_i16(packet.subchannel)
            + pack_u32(packet.channel_id, label="channel_id")
            + pack_blob_u32(packet.data, label="message", maximum=limits.max_message_bytes, minimum=1)
        )
    if isinstance(packet, ToServer):
        return (
            b"\x02"
            + pack_i16(packet.subchannel)
            + pack_blob_u32(packet.data, label="message", maximum=limits.max_message_bytes, minimum=1)
        )
    if isinstance(packet, PrivateMessage):
        return (
            b"\x03"
            + pack_i16(packet.subchannel)
            + pack_u32(packet.channel_id, label="channel_id")
            + pack_u32(packet.target_id, label="target_id")
            + pack_blob_u32(packet.data, label="message", maximum=limits.max_message_bytes, minimum=1)
        )
    if isinstance(packet, Join):
        return b"\x04" + _pack_name(packet.name, limits, "session name")
    if isinstance(packet, Leave):
        return b"\x05" + pack_u32(packet.channel_id, label="channel_id")
    if isinstance(packet, Rename):
        return b"\x0B" + _pack_name(packet.name, limits, "alias")
    if isinstance(packet, Hello):
        return b"\x0C" + pack_i16(packet.version) + _pack_name(packet.name, limits, "hello name")
    raise TypeError(f"unsupported client packet type: {type(packet).__name__}")


# ---------------------------------------------------------------------------
# Server -> client
# ---------------------------------------------------------------------------

def decode_server_packet(
    data: bytes | bytearray | memoryview,
    limits: CodecLimits = DEFAULT_LIMITS,
):
    """Decode exactly one dialect-A server->client TCP packet."""

    def parse():
        r = Reader(data)
        packet_id = r.u8()

        if packet_id == 0x01:
            packet = FromChannel(r.i16(), r.u32(), r.u32(), _message(r, limits))
        elif packet_id == 0x05:
            player_id = r.u32()
            channel_id = r.u32()
            master_id = r.u32()
            if channel_id == 0:
                raise _Malformed("Left channel_id must not be zero")
            packet = Left(player_id, channel_id, master_id)
        elif packet_id in (0x06, 0x07, 0x08):
            player_id = r.u32()
            channel_id = r.u32()
            master_id = r.u32()
            if player_id == 0 or channel_id == 0:
                raise _Malformed("member and channel IDs must not be zero")
            name = _name(r, limits, "member name")
            ip = _ip(r, limits)
            if packet_id == 0x06:
                packet = Joined(player_id, channel_id, master_id, name, ip)
            elif packet_id == 0x07:
                packet = Exists(player_id, channel_id, master_id, name, ip)
            else:
                session_name = _name(r, limits, "session name")
                packet = Welcome(player_id, channel_id, master_id, name, ip, session_name)
        elif packet_id == 0x0A:
            text = r.blob_u32(
                label="MOTD", maximum=limits.max_motd_bytes, minimum=1
            )
            packet = Motd(text)
        elif packet_id == 0x0B:
            # DIALECT A: one (player:u32, name:str) tuple; name begins at byte 9.
            # PyLacewing's duplicated tuple is intentionally not reproduced.
            player_id = r.u32()
            if player_id == 0:
                raise _Malformed("alias player_id must not be zero")
            packet = Alias(player_id, _name(r, limits, "alias"))
        elif packet_id == 0x0C:
            version = r.i16()
            connection_id = r.u32()
            if connection_id == 0:
                raise _Malformed("assigned connection ID must not be zero")
            packet = AssignedId(version, connection_id)
        else:
            raise _Malformed(f"unknown server packet id 0x{packet_id:02X}")

        return Decoded(packet, r.offset)

    return _as_result(parse)


def encode_server_packet(packet: ServerPacket, limits: CodecLimits = DEFAULT_LIMITS) -> bytes:
    """Encode one dialect-A server->client TCP packet."""

    if isinstance(packet, FromChannel):
        return (
            b"\x01"
            + pack_i16(packet.subchannel)
            + pack_u32(packet.channel_id, label="channel_id")
            + pack_u32(packet.sender_id, label="sender_id")
            + pack_blob_u32(packet.data, label="message", maximum=limits.max_message_bytes, minimum=1)
        )
    if isinstance(packet, Left):
        if packet.channel_id == 0:
            raise ValueError("Left channel_id must not be zero")
        return (
            b"\x05"
            + pack_u32(packet.player_id, label="player_id")
            + pack_u32(packet.channel_id, label="channel_id", allow_zero=False)
            + pack_u32(packet.master_id, label="master_id")
        )
    if isinstance(packet, (Joined, Exists, Welcome)):
        packet_id = b"\x06" if isinstance(packet, Joined) else b"\x07" if isinstance(packet, Exists) else b"\x08"
        prefix = (
            packet_id
            + pack_u32(packet.player_id, label="player_id", allow_zero=False)
            + pack_u32(packet.channel_id, label="channel_id", allow_zero=False)
            + pack_u32(packet.master_id, label="master_id")
            + _pack_name(packet.name, limits, "member name")
            + pack_blob_u32(packet.ip, label="ip", maximum=limits.max_ip_bytes, minimum=1, reject_nul=True)
        )
        if isinstance(packet, Welcome):
            prefix += _pack_name(packet.session_name, limits, "session name")
        return prefix
    if isinstance(packet, Motd):
        return b"\x0A" + pack_blob_u32(
            packet.text, label="MOTD", maximum=limits.max_motd_bytes, minimum=1
        )
    if isinstance(packet, Alias):
        return (
            b"\x0B"
            + pack_u32(packet.player_id, label="player_id", allow_zero=False)
            + _pack_name(packet.name, limits, "alias")
        )
    if isinstance(packet, AssignedId):
        return (
            b"\x0C"
            + pack_i16(packet.version)
            + pack_u32(packet.connection_id, label="connection_id", allow_zero=False)
        )
    raise TypeError(f"unsupported server packet type: {type(packet).__name__}")


def _pack_name(value: bytes, limits: CodecLimits, label: str) -> bytes:
    return pack_blob_u32(
        value,
        label=label,
        maximum=limits.max_name_bytes,
        minimum=1,
        reject_nul=True,
    )

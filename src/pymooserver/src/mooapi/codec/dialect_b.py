"""Dialect-B MooAPI codec differences."""

from __future__ import annotations

from .dialect_a import decode_client_packet, encode_client_packet, encode_server_packet
from .packets import Alias, ServerPacket
from .primitives import CodecLimits, pack_u32


DEFAULT_LIMITS = CodecLimits()


def encode_server_packet_b(
    packet: ServerPacket,
    limits: CodecLimits = DEFAULT_LIMITS,
    *,
    alias_policy: str = "padded-best-effort",
) -> bytes | None:
    """Encode a server packet for a dialect-B client.

    Alias policy is explicit because no form is yet proven fully reliable:

    ``padded-best-effort``
        Insert six zero bytes between the length and name. Fragmentation can still
        leave the older parser with an empty or truncated rename.
    ``original-A``
        Reproduce the original 1.01 server's A-form packet.
    ``suppress``
        Emit no alias packet at all.
    """

    if not isinstance(packet, Alias):
        return encode_server_packet(packet, limits)
    if alias_policy == "suppress":
        return None
    if alias_policy == "original-A":
        return encode_server_packet(packet, limits)
    if alias_policy != "padded-best-effort":
        raise ValueError(f"unknown dialect-B alias policy: {alias_policy}")
    name = packet.name
    if not (1 <= len(name) <= limits.max_name_bytes):
        raise ValueError("alias length outside configured limits")
    if b"\x00" in name:
        raise ValueError("alias contains NUL")
    return (
        b"\x0B"
        + pack_u32(packet.player_id, label="player_id", allow_zero=False)
        + pack_u32(len(name), label="alias length")
        + (b"\x00" * 6)
        + name
    )

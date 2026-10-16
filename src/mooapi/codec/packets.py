"""Typed packet models for MooAPI TCP and UDP traffic."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias


# Client -> server -----------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ChannelMessage:
    subchannel: int
    channel_id: int
    data: bytes


@dataclass(frozen=True, slots=True)
class ToServer:
    subchannel: int
    data: bytes


@dataclass(frozen=True, slots=True)
class PrivateMessage:
    subchannel: int
    channel_id: int
    target_id: int
    data: bytes


@dataclass(frozen=True, slots=True)
class Join:
    name: bytes


@dataclass(frozen=True, slots=True)
class Leave:
    channel_id: int


@dataclass(frozen=True, slots=True)
class Rename:
    name: bytes


@dataclass(frozen=True, slots=True)
class Hello:
    version: int
    name: bytes


ClientPacket: TypeAlias = (
    ChannelMessage | ToServer | PrivateMessage | Join | Leave | Rename | Hello
)


# Server -> client -----------------------------------------------------------

@dataclass(frozen=True, slots=True)
class FromChannel:
    subchannel: int
    channel_id: int
    sender_id: int
    data: bytes


@dataclass(frozen=True, slots=True)
class Left:
    player_id: int
    channel_id: int
    master_id: int


@dataclass(frozen=True, slots=True)
class Joined:
    player_id: int
    channel_id: int
    master_id: int
    name: bytes
    ip: bytes


@dataclass(frozen=True, slots=True)
class Exists:
    player_id: int
    channel_id: int
    master_id: int
    name: bytes
    ip: bytes


@dataclass(frozen=True, slots=True)
class Welcome:
    player_id: int
    channel_id: int
    master_id: int
    name: bytes
    ip: bytes
    session_name: bytes


@dataclass(frozen=True, slots=True)
class Motd:
    text: bytes


@dataclass(frozen=True, slots=True)
class Alias:
    player_id: int
    name: bytes


@dataclass(frozen=True, slots=True)
class AssignedId:
    version: int
    connection_id: int


ServerPacket: TypeAlias = (
    FromChannel | Left | Joined | Exists | Welcome | Motd | Alias | AssignedId
)

# UDP dialect A -------------------------------------------------------------

# packet id 01 is a generic routed blast in dialect A.  ``target_id``
# is zero for a client -> server channel blast, the recipient connection id for a
# server -> client channel relay, and a non-zero peer id for direct User:Blast
# datagrams that bypass a standalone MOS. The class name is kept for API
# compatibility even though its wire use is broader than channel relay.

@dataclass(frozen=True, slots=True)
class UdpChannelBlast:
    subchannel: int
    channel_id: int
    sender_id: int
    target_id: int
    data: bytes


@dataclass(frozen=True, slots=True)
class UdpPrivateBlast:
    subchannel: int
    channel_id: int
    sender_id: int
    target_id: int
    data: bytes


@dataclass(frozen=True, slots=True)
class UdpToServer:
    subchannel: int
    sender_id: int
    data: bytes


UdpAPacket: TypeAlias = UdpChannelBlast | UdpToServer
UdpPacket: TypeAlias = UdpChannelBlast | UdpPrivateBlast | UdpToServer

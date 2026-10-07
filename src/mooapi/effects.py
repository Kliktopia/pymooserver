"""Side effects emitted by the deterministic MooAPI hub."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

from .codec.packets import ServerPacket, UdpPacket


@dataclass(frozen=True, slots=True)
class SendTcp:
    connection_id: int
    packet: ServerPacket


@dataclass(frozen=True, slots=True)
class SendUdp:
    connection_id: int
    packet: UdpPacket


@dataclass(frozen=True, slots=True)
class CloseConnection:
    connection_id: int
    reason: str


@dataclass(frozen=True, slots=True)
class ApplicationEvent:
    """Protocol-derived event delivered to the application layer.

    ``name`` snapshots the relevant connection/session name when useful so callbacks
    remain meaningful even after a disconnect removed the live hub state.
    """

    kind: str
    connection_id: int
    session_id: int | None = None
    subchannel: int | None = None
    data: bytes | None = None
    target_id: int | None = None
    name: bytes | None = None


Effect: TypeAlias = SendTcp | SendUdp | CloseConnection | ApplicationEvent

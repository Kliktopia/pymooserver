"""Mutable MooAPI state owned by the server hub."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class Connection:
    id: int
    peer_ip: bytes
    name: bytes = b" "
    version: int | None = None
    hello_complete: bool = False
    dialect: str = "A"
    session_ids: list[int] = field(default_factory=list)


@dataclass(slots=True)
class Session:
    id: int
    name: bytes
    member_ids: list[int] = field(default_factory=list)

    @property
    def master_id(self) -> int:
        return self.member_ids[0] if self.member_ids else 0

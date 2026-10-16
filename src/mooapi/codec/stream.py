"""Incremental MooAPI TCP stream decoder."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from .dialect_a import decode_client_packet, decode_server_packet
from .primitives import CodecLimits, Decoded, Error, NeedMore


class Direction(str, Enum):
    CLIENT_TO_SERVER = "client_to_server"
    SERVER_TO_CLIENT = "server_to_client"


@dataclass(frozen=True, slots=True)
class StreamFeedResult:
    packets: tuple[Any, ...]
    error: Error | None = None
    need_more: NeedMore | None = None


class StreamDecoder:
    """Stateful framing buffer around the pure packet decoders.

    A malformed packet leaves the decoder in a failed state because the strict
    transport is expected to close the connection; there is no safe resynchronizing
    delimiter in Moo TCP.
    """

    def __init__(
        self,
        direction: Direction,
        *,
        limits: CodecLimits | None = None,
    ) -> None:
        self.direction = direction
        self.limits = limits or CodecLimits()
        self._buffer = bytearray()
        self._failed: Error | None = None

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    @property
    def failed(self) -> bool:
        return self._failed is not None

    def feed(self, data: bytes) -> StreamFeedResult:
        if self._failed is not None:
            return StreamFeedResult((), error=self._failed)
        if not isinstance(data, bytes):
            raise TypeError("stream data must be bytes")
        if len(self._buffer) + len(data) > self.limits.max_recv_buffer:
            self._failed = Error(
                f"receive buffer would exceed cap {self.limits.max_recv_buffer}"
            )
            return StreamFeedResult((), error=self._failed)

        self._buffer.extend(data)
        packets: list[Any] = []
        decode = (
            decode_client_packet
            if self.direction is Direction.CLIENT_TO_SERVER
            else decode_server_packet
        )

        while self._buffer:
            result = decode(self._buffer, self.limits)
            if isinstance(result, NeedMore):
                return StreamFeedResult(tuple(packets), need_more=result)
            if isinstance(result, Error):
                self._failed = result
                return StreamFeedResult(tuple(packets), error=result)
            assert isinstance(result, Decoded)
            packets.append(result.packet)
            del self._buffer[: result.consumed]

        return StreamFeedResult(tuple(packets))

"""Asyncio UDP adapter for MooAPI blasts."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from time import monotonic
from typing import TYPE_CHECKING

from ..codec.primitives import Error
from ..codec.packets import UdpChannelBlast, UdpPrivateBlast
from ..codec.udp_a import decode_udp_a, encode_udp_a
from ..codec.udp_b import decode_udp_b, encode_udp_b
from ..limits import TokenBucket

if TYPE_CHECKING:
    from ..codec.packets import UdpAPacket
    from ..server import MooServer


@dataclass(slots=True)
class _Endpoint:
    host: str
    port: int
    seen: float


class _Protocol(asyncio.DatagramProtocol):
    def __init__(self, adapter: "UdpServerAdapter") -> None:
        self.adapter = adapter

    def datagram_received(self, data: bytes, addr) -> None:
        self.adapter.dispatch_datagram(data, addr)

    def error_received(self, exc: Exception) -> None:
        # UDP is intentionally best-effort. Aggregate repeated transport errors
        # rather than allowing ICMP/noise conditions to flood the persistent log.
        self.adapter._note_drop("transport-error")


class UdpServerAdapter:
    # Bound transient asyncio work as well as persistent per-client state. Public
    # UDP ports can receive bursts of arbitrary datagrams faster than callbacks
    # can finish; without a cap that can create an unbounded task backlog.
    _MAX_PENDING_TASKS = 512

    def __init__(self, runtime: "MooServer", host: str, port: int) -> None:
        self.runtime = runtime
        self.config = runtime.config
        self.host = host
        self.port = port
        self.transport: asyncio.DatagramTransport | None = None
        self.protocol: _Protocol | None = None
        self.endpoints: dict[int, _Endpoint] = {}
        self.rate_buckets: dict[int, TokenBucket] = {}
        self.drop_counts: dict[str, int] = {}
        self.accepted_datagrams = 0
        self._pending_tasks: set[asyncio.Task] = set()
        self._log = logging.getLogger("mooapi.transport")

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        transport, protocol = await loop.create_datagram_endpoint(
            lambda: _Protocol(self), local_addr=(self.host, self.port)
        )
        self.transport = transport
        self.protocol = protocol
        sockname = transport.get_extra_info("sockname")
        if isinstance(sockname, tuple):
            self.port = int(sockname[1])

    async def close(self) -> None:
        if self.transport is not None:
            self.transport.close()
            self.transport = None
        pending = list(self._pending_tasks)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._pending_tasks.clear()
        self.endpoints.clear()
        self.rate_buckets.clear()
        await asyncio.sleep(0)

    def dispatch_datagram(self, data: bytes, addr) -> None:
        # Reject impossible datagrams synchronously so junk larger than the
        # configured Moo cap does not consume an asyncio task slot at all.
        if len(data) > self.config.max_udp_datagram:
            self._note_drop("oversize")
            return
        if len(self._pending_tasks) >= self._MAX_PENDING_TASKS:
            self._note_drop("task-backlog")
            return
        task = asyncio.create_task(self.handle_datagram(data, addr))
        self._pending_tasks.add(task)
        task.add_done_callback(self._datagram_done)

    def _datagram_done(self, task: asyncio.Task) -> None:
        self._pending_tasks.discard(task)
        if task.cancelled():
            return
        try:
            exc = task.exception()
        except (asyncio.CancelledError, RuntimeError):
            return
        if exc is not None:
            self._log.error(
                "UDP %d datagram task failed: %r",
                self.port, exc,
                exc_info=(type(exc), exc, exc.__traceback__),
            )

    def _note_drop(self, reason: str) -> None:
        count = self.drop_counts.get(reason, 0) + 1
        self.drop_counts[reason] = count
        # Public UDP ports see ordinary noise. Log aggregate milestones rather
        # than one line per datagram, which would itself become a denial of service.
        if count in (100, 1000) or (count > 1000 and count % 10000 == 0):
            self._log.warning(
                "UDP %d drops reason=%s count=%d active_tcp=%d endpoints=%d rate_buckets=%d",
                self.port, reason, count, len(self.runtime.hub.connections),
                len(self.endpoints), len(self.rate_buckets),
            )

    def forget_connection(self, connection_id: int) -> None:
        """Discard transient UDP metadata as soon as a Moo connection ends.

        The observed UDP endpoint is a NAT-compatibility aid, not part of the
        application-facing Moo user model and is never persisted or advertised.
        """
        self.endpoints.pop(connection_id, None)
        self.rate_buckets.pop(connection_id, None)

    def _decode_datagram(self, data: bytes):
        """Decode without mutating connection state before sender authentication.

        Returns ``(packet_or_error, detected_dialect)``.  In auto mode a B-looking
        packet may select the B decoder, but the caller commits that dialect only
        after the claimed sender ID has been authenticated against the TCP peer IP.
        """
        if not data:
            return Error("empty UDP datagram"), None

        packet_id = data[0]
        sender_id = None
        if packet_id in (0x01, 0x03) and len(data) >= 11:
            sender_id = int.from_bytes(data[7:11], "little")
        elif packet_id == 0x02 and len(data) >= 7:
            sender_id = int.from_bytes(data[3:7], "little")

        connection = self.runtime.hub.connections.get(sender_id) if sender_id else None
        dialect = connection.dialect if connection is not None else (
            "A" if self.config.dialect == "auto" else self.config.dialect
        )
        detected_dialect = None

        if self.config.dialect == "auto" and connection is not None:
            # mutate ``connection.dialect`` here: sender IP has not been checked yet.
            if packet_id == 0x03:
                dialect = "B"
                detected_dialect = "B"
            elif packet_id == 0x01 and len(data) >= 15 and data[11:15] != b"\x00\x00\x00\x00":
                dialect = "B"
                detected_dialect = "B"

        decode = decode_udp_b if dialect == "B" else decode_udp_a
        return decode(data, maximum=self.config.max_udp_datagram), detected_dialect

    async def handle_datagram(self, data: bytes, addr) -> None:
        if not isinstance(addr, tuple) or len(addr) < 2:
            self._note_drop("invalid-address")
            return
        source_ip, source_port = str(addr[0]), int(addr[1])
        packet, detected_dialect = self._decode_datagram(data)
        if isinstance(packet, Error):
            self._note_drop("decode-error")
            return

        sender_id = packet.sender_id

        # Do not allocate per-sender state for unauthenticated/random UDP IDs.
        # Public UDP noise can otherwise grow rate_buckets without bound.
        connection = self.runtime.hub.connections.get(sender_id)
        if connection is None:
            self._note_drop("unknown-sender")
            return
        try:
            source_ip_bytes = source_ip.encode("ascii")
        except UnicodeEncodeError:
            self._note_drop("non-ascii-source-ip")
            return
        if connection.peer_ip != source_ip_bytes:
            self._note_drop("source-ip-mismatch")
            return

        if detected_dialect is not None and self.config.dialect == "auto":
            connection.dialect = detected_dialect

        bucket = self.rate_buckets.get(sender_id)
        if bucket is None:
            bucket = TokenBucket(self.config.udp_rate, self.config.udp_burst)
            self.rate_buckets[sender_id] = bucket
        if not bucket.consume(1):
            self._note_drop("rate-limit")
            return

        result = self.runtime.hub.receive_udp(packet, source_ip=source_ip)
        if not result.accepted:
            self._note_drop("hub-rejected")
            return

        # learn only from an accepted datagram associated with this client.
        was_known = sender_id in self.endpoints
        self.endpoints[sender_id] = _Endpoint(source_ip, source_port, monotonic())
        self.accepted_datagrams += 1
        if not was_known:
            self._log.debug(
                "UDP %d learned endpoint id=%d port=%d endpoints=%d",
                self.port, sender_id, source_port, len(self.endpoints),
            )
        await self.runtime._handle_effects(result.effects)

    def send_packet(self, connection_id: int, packet: "UdpAPacket") -> None:
        if self.transport is None:
            return
        connection = self.runtime.hub.connections.get(connection_id)
        if connection is None:
            return

        endpoint = self.endpoints.get(connection_id)
        now = monotonic()
        if endpoint is not None and now - endpoint.seen <= self.config.udp_endpoint_ttl:
            target = (endpoint.host, endpoint.port)
        else:
            target = (connection.peer_ip.decode("ascii", "strict"), self.runtime.base_udp_port)

        dialect = connection.dialect
        if dialect == "B":
            payload = encode_udp_b(packet, maximum=self.config.max_udp_datagram)
        else:
            if isinstance(packet, UdpPrivateBlast):
                # COMPAT conversion: dialect A has no UDP 03 receive path.
                packet = UdpChannelBlast(
                    packet.subchannel,
                    packet.channel_id,
                    packet.sender_id,
                    connection_id,
                    packet.data,
                )
            payload = encode_udp_a(packet, maximum=self.config.max_udp_datagram)
        self.transport.sendto(payload, target)

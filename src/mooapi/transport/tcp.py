"""Asyncio TCP adapter for MooAPI."""

from __future__ import annotations

import asyncio
import logging
import socket
from time import monotonic
from typing import TYPE_CHECKING

from ..codec.dialect_a import encode_server_packet
from ..codec.dialect_b import encode_server_packet_b
from ..codec.packets import Join, Leave, Rename
from ..codec.stream import Direction, StreamDecoder
from ..limits import TokenBucket

if TYPE_CHECKING:
    from ..config import ServerConfig
    from ..server import MooServer


_MOO_CLIENT_PACKET_IDS = frozenset({0x01, 0x02, 0x03, 0x04, 0x05, 0x0B, 0x0C})
_HTTP_PREFIXES = (
    b"GET ", b"POST ", b"HEAD ", b"OPTIONS ", b"CONNECT ",
    b"PUT ", b"DELETE ", b"PATCH ", b"TRACE ", b"PRI * HTTP/2.0",
)



async def _safe_close_writer(writer: asyncio.StreamWriter) -> None:
    """Dispose of an asyncio writer without leaking Windows Proactor close errors.

    This is intentionally used only for shutdown/cleanup.  Cancellation during
    ordinary reads and writes should still propagate normally.
    """
    try:
        writer.close()
    except (ConnectionError, OSError, RuntimeError):
        return

    try:
        await asyncio.wait_for(writer.wait_closed(), timeout=2.0)
    except (asyncio.TimeoutError, ConnectionError, OSError, RuntimeError, asyncio.CancelledError):
        pass

def _classify_initial_bytes(data: bytes) -> str | None:
    """Classify a new inbound stream before feeding it to the Moo decoder.

    Returns ``"moo"`` for an opcode that can begin a client Moo packet, a short
    label for an obvious foreign protocol, ``"unknown"`` when the prefix cannot
    become one of the recognized foreign protocols, or ``None`` while a known
    foreign-protocol signature is still incomplete.  This layer is deliberately
    small: it is transport hygiene for public ports, not an alternate Moo parser.
    """
    if not data:
        return None
    if data[0] in _MOO_CLIENT_PACKET_IDS:
        return "moo"

    # TLS records normally begin 16 03 xx.  Wait for the second byte if a single
    # 0x16 arrived by itself so fragmented ClientHello probes classify cleanly.
    if data[:1] == b"\x16":
        if len(data) < 2:
            return None
        if data[1] == 0x03:
            return "tls"
        return "unknown"

    if b"SSH-".startswith(data):
        return None if len(data) < 4 else "ssh"
    if data.startswith(b"SSH-"):
        return "ssh"

    possible_http = False
    for prefix in _HTTP_PREFIXES:
        if prefix.startswith(data):
            possible_http = True
            continue
        if data.startswith(prefix):
            return "http"
    if possible_http:
        return None
    return "unknown"


class TcpPeer:
    def __init__(
        self,
        adapter: "TcpServerAdapter",
        connection_id: int,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        self.adapter = adapter
        self.connection_id = connection_id
        self.reader = reader
        self.writer = writer
        self.decoder = StreamDecoder(
            Direction.CLIENT_TO_SERVER, limits=adapter.config.codec
        )
        self.started = monotonic()
        self.last_valid_packet = self.started
        self.partial_since: float | None = None
        self.identified = not adapter.config.non_moo_guard
        self.identification_buffer = bytearray()
        self.closed = False
        self.close_reason = "connection ended"
        self.pending_bytes = 0
        self.send_queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.writer_task = asyncio.create_task(self._writer_loop())
        self.packet_bucket = TokenBucket(
            adapter.config.packet_rate, adapter.config.packet_burst
        )
        self.byte_bucket = TokenBucket(adapter.config.byte_rate, adapter.config.byte_burst)
        self.join_rename_bucket = TokenBucket(
            adapter.config.join_rename_rate, adapter.config.join_rename_burst
        )
        self._enable_keepalive()

    def _enable_keepalive(self) -> None:
        """Enable best-effort TCP keepalive with short dead-peer detection."""
        sock = self.writer.get_extra_info("socket")
        if sock is None:
            return
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            if hasattr(socket, "TCP_KEEPIDLE"):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 60)
            if hasattr(socket, "TCP_KEEPINTVL"):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 20)
            if hasattr(socket, "TCP_KEEPCNT"):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
            if hasattr(socket, "SIO_KEEPALIVE_VALS") and hasattr(sock, "ioctl"):
                try:
                    sock.ioctl(socket.SIO_KEEPALIVE_VALS, (1, 60_000, 20_000))
                except (OSError, AttributeError):
                    pass
        except OSError:
            # Some platforms expose the constants but reject tuning them.
            pass

    async def send_packet(self, packet) -> bool:
        if self.closed:
            return False
        connection = self.adapter.runtime.hub.connections.get(self.connection_id)
        if connection is not None and connection.dialect == "B":
            payload = encode_server_packet_b(
                packet,
                self.adapter.config.codec,
                alias_policy=self.adapter.config.dialect_b_alias_policy,
            )
            if payload is None:
                return True
        else:
            payload = encode_server_packet(packet, self.adapter.config.codec)
        if self.pending_bytes + len(payload) > self.adapter.config.max_send_queue:
            self.close_reason = "send queue exceeded configured cap"
            self.adapter._log.warning(
                "TCP %d id=%d send queue exceeded pending=%d add=%d cap=%d",
                self.adapter.port, self.connection_id, self.pending_bytes, len(payload),
                self.adapter.config.max_send_queue,
            )
            await self.adapter.close_connection(
                self.connection_id, self.close_reason
            )
            return False
        self.pending_bytes += len(payload)
        self.send_queue.put_nowait(payload)
        return True

    async def _writer_loop(self) -> None:
        try:
            while True:
                payload = await self.send_queue.get()
                if payload is None:
                    return
                self.writer.write(payload)
                try:
                    await asyncio.wait_for(
                        self.writer.drain(), timeout=self.adapter.config.write_timeout
                    )
                finally:
                    self.pending_bytes -= len(payload)
        except asyncio.TimeoutError:
            self.close_reason = "outbound write timeout"
            self.adapter._log.warning(
                "TCP %d id=%d closing reason=%s pending=%d timeout=%.1fs",
                self.adapter.port, self.connection_id, self.close_reason,
                self.pending_bytes, self.adapter.config.write_timeout,
            )
            if self.connection_id in self.adapter.runtime.hub.connections:
                await self.adapter.runtime._handle_effects(
                    self.adapter.runtime.hub.protocol_error(
                        self.connection_id, self.close_reason
                    )
                )
        except (ConnectionError, OSError, RuntimeError) as exc:
            self.close_reason = "%s: %s" % (type(exc).__name__, exc)
            self.adapter._log.debug(
                "TCP %d id=%d writer ended error=%r",
                self.adapter.port, self.connection_id, exc,
            )
            if self.connection_id in self.adapter.runtime.hub.connections:
                await self.adapter.runtime._handle_effects(
                    self.adapter.runtime.hub.protocol_error(
                        self.connection_id, self.close_reason
                    )
                )

    def _next_timeout(self) -> float | None:
        now = monotonic()
        deadlines: list[float] = []
        cfg = self.adapter.config
        connection = self.adapter.runtime.hub.connections.get(self.connection_id)
        if not self.identified and cfg.identification_timeout:
            deadlines.append(self.started + cfg.identification_timeout)
        if connection is not None and not connection.hello_complete and cfg.hello_timeout:
            deadlines.append(self.started + cfg.hello_timeout)
        if self.partial_since is not None and cfg.partial_packet_timeout:
            deadlines.append(self.partial_since + cfg.partial_packet_timeout)
        if cfg.idle_timeout:
            deadlines.append(self.last_valid_packet + cfg.idle_timeout)
        if not deadlines:
            return None
        return max(0.001, min(deadlines) - now)

    async def run(self) -> None:
        try:
            while not self.closed:
                timeout = self._next_timeout()
                try:
                    if timeout is None:
                        chunk = await self.reader.read(self.adapter.config.tcp_read_size)
                    else:
                        chunk = await asyncio.wait_for(
                            self.reader.read(self.adapter.config.tcp_read_size), timeout
                        )
                except asyncio.TimeoutError:
                    self.close_reason = self._timeout_reason()
                    self.adapter._log.info(
                        "TCP %d id=%d closing reason=%s buffered=%d pending=%d",
                        self.adapter.port, self.connection_id, self.close_reason,
                        self.decoder.buffered_bytes, self.pending_bytes,
                    )
                    await self.adapter.runtime._handle_effects(
                        self.adapter.runtime.hub.protocol_error(
                            self.connection_id, self.close_reason
                        )
                    )
                    return

                if not chunk:
                    self.close_reason = "peer EOF"
                    effects = self.adapter.runtime.hub.disconnect(self.connection_id)
                    await self.adapter.runtime._handle_effects(effects)
                    return

                if not self.byte_bucket.consume(len(chunk)):
                    self.close_reason = "inbound byte rate exceeded"
                    self.adapter._log.warning(
                        "TCP %d id=%d closing reason=%s",
                        self.adapter.port, self.connection_id, self.close_reason,
                    )
                    await self.adapter.runtime._handle_effects(
                        self.adapter.runtime.hub.protocol_error(
                            self.connection_id, "inbound byte rate exceeded"
                        )
                    )
                    return

                if not self.identified:
                    self.identification_buffer.extend(chunk)
                    if len(self.identification_buffer) > self.adapter.config.preidentify_buffer:
                        self.close_reason = "pre-identification buffer exceeded"
                        self.adapter._log.debug(
                            "TCP %d id=%d closing reason=%s bytes=%d",
                            self.adapter.port, self.connection_id, self.close_reason,
                            len(self.identification_buffer),
                        )
                        effects = self.adapter.runtime.hub.disconnect(self.connection_id)
                        await self.adapter.runtime._handle_effects(effects)
                        return
                    classification = _classify_initial_bytes(bytes(self.identification_buffer))
                    if classification is None:
                        continue
                    if classification != "moo":
                        self.close_reason = "non-Moo preface: %s" % classification
                        # Obvious HTTP/TLS/SSH and other non-Moo traffic is ordinary
                        # public-port noise.  Close quietly rather than passing it to
                        # the Moo decoder or manufacturing a Moo protocol error.
                        # classification logging is off by default; when
                        # enabled it records only the protocol label unless the
                        # operator explicitly opts into peer-address logging.
                        self.adapter.log_classification(classification, self.writer)
                        effects = self.adapter.runtime.hub.disconnect(self.connection_id)
                        await self.adapter.runtime._handle_effects(effects)
                        return
                    self.identified = True
                    chunk = bytes(self.identification_buffer)
                    self.identification_buffer.clear()

                before = self.decoder.buffered_bytes
                result = self.decoder.feed(chunk)
                if result.error is not None:
                    self.close_reason = "protocol error: %s" % result.error.message
                    self.adapter._log.warning(
                        "TCP %d id=%d closing reason=%s buffered=%d",
                        self.adapter.port, self.connection_id, self.close_reason,
                        self.decoder.buffered_bytes,
                    )
                    await self.adapter.runtime._handle_effects(
                        self.adapter.runtime.hub.protocol_error(
                            self.connection_id, result.error.message
                        )
                    )
                    return

                if result.packets:
                    self.last_valid_packet = monotonic()
                    self.partial_since = None
                elif self.decoder.buffered_bytes and before == 0:
                    self.partial_since = monotonic()

                for packet in result.packets:
                    if not self.packet_bucket.consume(1):
                        self.close_reason = "packet rate exceeded"
                        self.adapter._log.warning(
                            "TCP %d id=%d closing reason=%s",
                            self.adapter.port, self.connection_id, self.close_reason,
                        )
                        await self.adapter.runtime._handle_effects(
                            self.adapter.runtime.hub.protocol_error(
                                self.connection_id, "packet rate exceeded"
                            )
                        )
                        return
                    if isinstance(packet, (Join, Leave, Rename)) and not self.join_rename_bucket.consume(1):
                        self.close_reason = "join/leave/rename rate exceeded"
                        self.adapter._log.warning(
                            "TCP %d id=%d closing reason=%s",
                            self.adapter.port, self.connection_id, self.close_reason,
                        )
                        await self.adapter.runtime._handle_effects(
                            self.adapter.runtime.hub.protocol_error(
                                self.connection_id, "join/leave/rename rate exceeded"
                            )
                        )
                        return
                    effects = self.adapter.runtime.hub.receive(self.connection_id, packet)
                    await self.adapter.runtime._handle_effects(effects)
                    if self.connection_id not in self.adapter.runtime.hub.connections:
                        return

                if result.need_more is not None and self.partial_since is None:
                    self.partial_since = monotonic()
        except (ConnectionError, OSError) as exc:
            self.close_reason = "%s: %s" % (type(exc).__name__, exc)
            self.adapter._log.debug(
                "TCP %d id=%d socket ended error=%r",
                self.adapter.port, self.connection_id, exc,
            )
            if self.connection_id in self.adapter.runtime.hub.connections:
                effects = self.adapter.runtime.hub.disconnect(self.connection_id)
                await self.adapter.runtime._handle_effects(effects)
        finally:
            # Any transport-level closure must also remove protocol state. Most
            # normal paths already do this before closing the socket, but queue
            # overflow or a future direct transport close can otherwise leave a
            # ghost Hub connection after the peer task exits.
            if self.connection_id in self.adapter.runtime.hub.connections:
                effects = self.adapter.runtime.hub.disconnect(self.connection_id)
                await self.adapter.runtime._handle_effects(effects)
            await self.close_writer()

    def _timeout_reason(self) -> str:
        cfg = self.adapter.config
        now = monotonic()
        connection = self.adapter.runtime.hub.connections.get(self.connection_id)
        if (
            not self.identified
            and cfg.identification_timeout
            and now >= self.started + cfg.identification_timeout
        ):
            return "identification timeout"
        if (
            connection is not None
            and not connection.hello_complete
            and cfg.hello_timeout
            and now >= self.started + cfg.hello_timeout
        ):
            return "hello timeout"
        if (
            self.partial_since is not None
            and cfg.partial_packet_timeout
            and now >= self.partial_since + cfg.partial_packet_timeout
        ):
            return "partial packet timeout"
        return "idle timeout"

    async def close_writer(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.send_queue.put_nowait(None)
        except asyncio.QueueFull:
            pass
        await _safe_close_writer(self.writer)
        if self.writer_task is not asyncio.current_task() and not self.writer_task.done():
            self.writer_task.cancel()
            try:
                await self.writer_task
            except (asyncio.CancelledError, ConnectionError, OSError, RuntimeError):
                pass


class TcpServerAdapter:
    def __init__(self, runtime: "MooServer", host: str, port: int) -> None:
        self.runtime = runtime
        self.config: ServerConfig = runtime.config
        self._log = logging.getLogger("mooapi.transport")
        self.host = host
        self.port = port
        self.server: asyncio.AbstractServer | None = None
        self.peers: dict[int, TcpPeer] = {}
        self._per_ip: dict[str, int] = {}
        self._connect_buckets: dict[str, TokenBucket] = {}

    def log_classification(self, classification: str, writer: asyncio.StreamWriter) -> None:
        if not self.config.log_connection_classification:
            return
        if self.config.log_peer_addresses:
            peername = writer.get_extra_info("peername")
            peer_ip = str(peername[0]) if isinstance(peername, tuple) and peername else "unknown"
            self._log.info("non-Moo connection: %s peer=%s", classification, peer_ip)
        else:
            self._log.info("non-Moo connection: %s", classification)
        # Intentionally no payload logging here.  ``log_foreign_payloads`` remains
        # false by default and is reserved for an explicit future diagnostics path.

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._accept, self.host, self.port)

    async def serve_forever(self) -> None:
        if self.server is None:
            await self.start()
        assert self.server is not None
        async with self.server:
            await self.server.serve_forever()

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        await asyncio.gather(
            *(peer.close_writer() for peer in list(self.peers.values())),
            return_exceptions=True,
        )
        self.peers.clear()
        self._per_ip.clear()
        self._connect_buckets.clear()

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peername = writer.get_extra_info("peername")
        peer_ip = str(peername[0]) if isinstance(peername, tuple) and peername else "0.0.0.0"
        if self._per_ip.get(peer_ip, 0) >= self.config.max_connections_per_ip:
            self._log.warning(
                "TCP %d connection rejected reason=per-IP-limit active=%d per_ip=%d limit=%d",
                self.port, len(self.peers), self._per_ip.get(peer_ip, 0),
                self.config.max_connections_per_ip,
            )
            await _safe_close_writer(writer)
            return

        # Keep the scanner-IP rate-limit cache under a true hard cap. Age-only
        # pruning still permits an attacker with many source IPs to grow it without
        # bound during the retention window. Never evict an IP with a live peer.
        bucket = self._connect_buckets.get(peer_ip)
        if bucket is None and len(self._connect_buckets) >= 4096:
            cutoff = monotonic() - 600.0
            stale = [
                ip for ip, old_bucket in self._connect_buckets.items()
                if ip not in self._per_ip and old_bucket.updated < cutoff
            ]
            for ip in stale:
                self._connect_buckets.pop(ip, None)
            while len(self._connect_buckets) >= 4096:
                # Dicts retain insertion order. Evict the first non-live entry;
                # at most max_connections live IPs can precede it, avoiding an
                # O(4096) min/sort-style scan for every scanner connection.
                evicted = False
                for old_ip in self._connect_buckets:
                    if old_ip not in self._per_ip:
                        self._connect_buckets.pop(old_ip, None)
                        evicted = True
                        break
                if not evicted:
                    break

        bucket = self._connect_buckets.get(peer_ip)
        if bucket is None:
            bucket = TokenBucket(
                self.config.connect_rate_per_ip_per_minute / 60.0,
                self.config.connect_burst_per_ip,
            )
            self._connect_buckets[peer_ip] = bucket
        if not bucket.consume(1):
            self._log.warning(
                "TCP %d connection rejected reason=per-IP-rate-limit active=%d bucket_cache=%d",
                self.port, len(self.peers), len(self._connect_buckets),
            )
            await _safe_close_writer(writer)
            return

        result = self.runtime.hub.connect(peer_ip)
        if result.connection_id is None:
            self._log.warning(
                "TCP %d connection rejected reason=server-limit active=%d max=%d",
                self.port, len(self.peers), self.config.max_connections,
            )
            await _safe_close_writer(writer)
            return

        cid = result.connection_id
        peer = TcpPeer(self, cid, reader, writer)
        self.peers[cid] = peer
        self._per_ip[peer_ip] = self._per_ip.get(peer_ip, 0) + 1
        self.runtime._remember_client(cid)
        shown_peer = peer_ip if self.config.log_peer_addresses else "hidden"
        self._log.info(
            "TCP %d connect id=%d peer=%s active=%d",
            self.port, cid, shown_peer, len(self.peers),
        )
        try:
            await self.runtime._handle_effects(result.effects)
            await peer.run()
        finally:
            self.peers.pop(cid, None)
            current = self._per_ip.get(peer_ip, 0)
            if current <= 1:
                self._per_ip.pop(peer_ip, None)
            else:
                self._per_ip[peer_ip] = current - 1
            await peer.close_writer()
            self._log.info(
                "TCP %d disconnect id=%d age=%.1fs reason=%s active=%d pending=%d",
                self.port, cid, max(0.0, monotonic() - peer.started),
                peer.close_reason, len(self.peers), peer.pending_bytes,
            )

    async def send_packet(self, connection_id: int, packet) -> None:
        peer = self.peers.get(connection_id)
        if peer is not None:
            await peer.send_packet(packet)

    async def close_connection(self, connection_id: int, reason: str = "closed") -> None:
        peer = self.peers.get(connection_id)
        if peer is not None:
            peer.close_reason = reason
            self._log.info(
                "TCP %d close requested id=%d reason=%s pending=%d",
                self.port, connection_id, reason, peer.pending_bytes,
            )
            await peer.close_writer()

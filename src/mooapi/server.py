"""High-level programmable MooAPI server interface."""

from __future__ import annotations

import asyncio
import inspect
import logging
from pathlib import Path
from typing import Any

from .app import MooApplication
from .config import ServerConfig
from .effects import ApplicationEvent, CloseConnection, Effect, SendTcp, SendUdp
from .hub import Hub
from .limits import TokenBucket
from .moogame.ini import FilesystemIniStore, IniService
from .transport.tcp import TcpServerAdapter
from .transport.udp import UdpServerAdapter


class Client:
    """Application facade for one Moo connection."""

    def __init__(self, server: "MooServer", connection_id: int) -> None:
        self._server = server
        self.id = connection_id
        state = server.hub.connections.get(connection_id)
        self._name = state.name if state else b" "
        self._peer_ip = state.peer_ip if state else b"0.0.0.0"
        self._version = state.version if state else None

    def _refresh(self) -> None:
        state = self._server.hub.connections.get(self.id)
        if state is not None:
            self._name = state.name
            self._peer_ip = state.peer_ip
            self._version = state.version

    @property
    def connected(self) -> bool:
        return self.id in self._server.hub.connections

    @property
    def name(self) -> bytes:
        self._refresh()
        return self._name

    @property
    def peer_ip(self) -> bytes:
        self._refresh()
        return self._peer_ip

    @property
    def version(self) -> int | None:
        self._refresh()
        return self._version

    @property
    def dialect(self) -> str:
        state = self._server.hub.connections.get(self.id)
        return state.dialect if state is not None else "A"

    @property
    def sessions(self) -> tuple["Session", ...]:
        state = self._server.hub.connections.get(self.id)
        if state is None:
            return ()
        return tuple(self._server._session(sid) for sid in state.session_ids)

    async def send(self, data: bytes, *, subchannel: int = 0) -> None:
        await self._server._handle_effects(
            self._server.hub.send_server_message(self.id, data, subchannel=subchannel)
        )

    async def blast(self, data: bytes, *, subchannel: int = 0) -> None:
        """Send an unreliable server-originated UDP message to this client."""
        await self._server._handle_effects(
            self._server.hub.blast_server_message(self.id, data, subchannel=subchannel)
        )

    async def rename(self, name: bytes) -> None:
        await self._server._handle_effects(self._server.hub.rename_connection(self.id, name))

    async def disconnect(self, reason: str = "closed by application") -> None:
        await self._server._handle_effects(self._server.hub.close_connection(self.id, reason))

    def __repr__(self) -> str:
        return f"Client(id={self.id}, name={self.name!r}, connected={self.connected})"


class Session:
    """Application facade for one Moo session/channel."""

    def __init__(self, server: "MooServer", session_id: int, *, name: bytes | None = None) -> None:
        self._server = server
        self.id = session_id
        state = server.hub.sessions.get(session_id)
        self._name = state.name if state is not None else (name or b"")

    def _refresh(self) -> None:
        state = self._server.hub.sessions.get(self.id)
        if state is not None:
            self._name = state.name

    @property
    def exists(self) -> bool:
        return self.id in self._server.hub.sessions

    @property
    def name(self) -> bytes:
        self._refresh()
        return self._name

    @property
    def master(self) -> Client | None:
        state = self._server.hub.sessions.get(self.id)
        if state is None or not state.master_id:
            return None
        return self._server._client(state.master_id)

    @property
    def clients(self) -> tuple[Client, ...]:
        state = self._server.hub.sessions.get(self.id)
        if state is None:
            return ()
        return tuple(self._server._client(cid) for cid in state.member_ids)

    async def sign_off(self, client: Client) -> None:
        """Sign one client off this session while leaving its connection open."""
        await self._server._handle_effects(
            self._server.hub.sign_off_session_member(self.id, client.id)
        )

    async def destroy(self) -> None:
        """Destroy this session and sign off all of its current clients."""
        await self._server._handle_effects(self._server.hub.destroy_session(self.id))

    async def broadcast(self, data: bytes, *, subchannel: int = 0) -> None:
        await self._server._handle_effects(
            self._server.hub.broadcast_server_message(
                self.id, data, subchannel=subchannel
            )
        )

    async def blast(self, data: bytes, *, subchannel: int = 0) -> None:
        """Blast an unreliable server-originated UDP message to this session."""
        await self._server._handle_effects(
            self._server.hub.blast_session_message(
                self.id, data, subchannel=subchannel
            )
        )

    def __repr__(self) -> str:
        return f"Session(id={self.id}, name={self.name!r}, exists={self.exists})"


class MooServer:
    """A simple or programmable Moo server with dialect-A/B compatibility.

    Plain server::

        MooServer().run()

    Programmable server::

        MooServer(app=MyApplication()).run()
    """

    def __init__(
        self,
        *,
        app: MooApplication | None = None,
        config: ServerConfig | None = None,
        host: str = "0.0.0.0",
        port: int = 1203,
        udp_port: int | None = None,
        enable_udp: bool = True,
    ) -> None:
        self.config = config or ServerConfig()
        self.hub = Hub(self.config)
        self.app = app or MooApplication()
        self.host = host
        self.port = port
        self._configured_udp_port = udp_port
        self.enable_udp = enable_udp
        self.base_udp_port = (port + 1) if port else 1204
        self.tcp = TcpServerAdapter(self, host, port)
        self.udp: UdpServerAdapter | None = None
        self._clients: dict[int, Client] = {}
        self._sessions: dict[int, Session] = {}
        self._log = logging.getLogger("mooapi.application")
        self.ini: IniService | None = None
        self._ini_write_buckets: dict[int, TokenBucket] = {}
        if self.config.ini_enabled:
            self._configure_ini_store(port)

    def _ini_directory_for_port(self, port: int) -> str:
        if self.config.ini_directory:
            return self.config.ini_directory
        root = Path(self.config.ini_data_root)
        if self.config.ini_shared:
            return str(root)
        return str(root / str(port))

    def _configure_ini_store(self, port: int) -> None:
        """Configure server-side INI/IMI persistence.

        Filesystem mode mirrors the MOO1/2 compatibility server's realm layout:
        ``<ini-root>/<tcp-port>`` by default.  The binary MooGame INI service
        remains unchanged; only its backing store/path convention is shared.
        """
        if self.ini is not None:
            self.ini.store.close()
        store = FilesystemIniStore(
            self._ini_directory_for_port(port),
            max_files=self.config.ini_max_files,
            max_keys_per_file=self.config.ini_max_keys_per_file,
            max_bytes=self.config.ini_max_bytes,
        )
        self.ini = IniService(
            store,
            namespace=self.config.ini_namespace,
            append_imi=self.config.ini_append_imi,
        )

    @property
    def clients(self) -> tuple[Client, ...]:
        return tuple(self._client(cid) for cid in self.hub.connections)

    @property
    def sessions(self) -> tuple[Session, ...]:
        return tuple(self._session(sid) for sid in self.hub.sessions)

    def set_motd(self, motd: bytes) -> None:
        """Set the Message of the Day for future client connections.

        Clients already connected
        keep the MOTD they received at connect time; the updated value is sent
        when a player reconnects or a new player connects.
        """
        self.hub.set_motd(motd)

    async def broadcast(self, data: bytes, *, subchannel: int = 0) -> None:
        """Send a reliable server-wide message to every connected client."""
        await self._handle_effects(
            self.hub.broadcast_all_server_message(data, subchannel=subchannel)
        )

    async def blast(self, data: bytes, *, subchannel: int = 0) -> None:
        """Blast an unreliable server-wide UDP message to every connected client."""
        await self._handle_effects(
            self.hub.blast_all_server_message(data, subchannel=subchannel)
        )

    async def start(self) -> "MooServer":
        await self.tcp.start()
        if self.tcp.server is not None and self.tcp.server.sockets:
            actual_tcp_port = int(self.tcp.server.sockets[0].getsockname()[1])
            self.base_udp_port = actual_tcp_port + 1
            if (
                self.config.ini_enabled
                and self.port == 0
                and not self.config.ini_directory
                and not self.config.ini_shared
            ):
                self._configure_ini_store(actual_tcp_port)
        if self.enable_udp:
            bind_udp_port = (
                self.base_udp_port
                if self._configured_udp_port is None
                else self._configured_udp_port
            )
            self.udp = UdpServerAdapter(self, self.host, bind_udp_port)
            try:
                await self.udp.start()
            except Exception:
                self.udp = None
                await self.tcp.close()
                raise
        return self

    async def serve_forever(self) -> None:
        await self.tcp.serve_forever()

    async def close(self) -> None:
        # Close through the Hub first so session leave semantics and application
        # disconnect callbacks remain consistent during graceful shutdown.
        for connection_id in list(self.hub.connections):
            await self._handle_effects(
                self.hub.close_connection(connection_id, "server shutting down")
            )
        if self.udp is not None:
            await self.udp.close()
            self.udp = None
        await self.tcp.close()
        if self.ini is not None:
            self.ini.store.close()
            self.ini = None

    def run(self) -> None:
        async def runner() -> None:
            await self.start()
            try:
                await self.serve_forever()
            finally:
                await self.close()

        try:
            asyncio.run(runner())
        except KeyboardInterrupt:
            pass

    def _remember_client(self, connection_id: int) -> Client:
        return self._client(connection_id)

    def _client(self, connection_id: int) -> Client:
        client = self._clients.get(connection_id)
        if client is None:
            client = Client(self, connection_id)
            self._clients[connection_id] = client
        client._refresh()
        return client

    def _session(self, session_id: int, *, name: bytes | None = None) -> Session:
        session = self._sessions.get(session_id)
        if session is None:
            session = Session(self, session_id, name=name)
            self._sessions[session_id] = session
        elif name and not session._name:
            session._name = name
        session._refresh()
        return session

    async def _handle_effects(self, effects: tuple[Effect, ...] | list[Effect]) -> None:
        # Effect order is semantically significant.  In particular, join/leave
        # notifications must be serialized before the corresponding app callback.
        for effect in effects:
            if isinstance(effect, SendTcp):
                await self.tcp.send_packet(effect.connection_id, effect.packet)
            elif isinstance(effect, SendUdp):
                if self.udp is not None:
                    self.udp.send_packet(effect.connection_id, effect.packet)
            elif isinstance(effect, CloseConnection):
                await self.tcp.close_connection(effect.connection_id, effect.reason)
            elif isinstance(effect, ApplicationEvent):
                await self._dispatch_application_event(effect)
            else:  # pragma: no cover - defensive for future Effect extensions
                raise TypeError(f"unknown effect: {effect!r}")

    async def _call_hook(self, name: str, *args: Any) -> None:
        hook = getattr(self.app, name)
        try:
            result = hook(*args)
            if inspect.isawaitable(result):
                await result
        except Exception:
            # Application code must not tear down the protocol accept loop.  The
            # exception is logged with its traceback; protocol state remains valid.
            self._log.exception("application hook %s failed", name)

    async def _dispatch_application_event(self, event: ApplicationEvent) -> None:
        client = self._client(event.connection_id)
        if event.name is not None and event.kind in {"connect", "hello", "rename", "disconnect"}:
            client._name = event.name

        if event.kind == "connect":
            await self._call_hook("on_connect", client)
        elif event.kind == "hello":
            await self._call_hook("on_hello", client)
        elif event.kind == "join":
            assert event.session_id is not None
            session = self._session(event.session_id, name=event.name)
            await self._call_hook("on_join", client, session)
        elif event.kind == "leave":
            assert event.session_id is not None
            session = self._session(event.session_id, name=event.name)
            await self._call_hook("on_leave", client, session)
            # The application facade is only a convenience cache. Once the Hub
            # has destroyed a session there is no reason to retain that wrapper
            # for the lifetime of the process.
            if event.session_id not in self.hub.sessions:
                self._sessions.pop(event.session_id, None)
        elif event.kind == "rename":
            await self._call_hook("on_rename", client)
        elif event.kind == "channel_message":
            assert event.session_id is not None and event.subchannel is not None and event.data is not None
            session = self._session(event.session_id, name=event.name)
            await self._call_hook(
                "on_channel_message", client, session, event.subchannel, event.data
            )
        elif event.kind == "private_message":
            assert (
                event.session_id is not None
                and event.target_id is not None
                and event.subchannel is not None
                and event.data is not None
            )
            session = self._session(event.session_id, name=event.name)
            target = self._client(event.target_id)
            await self._call_hook(
                "on_private_message",
                client,
                session,
                target,
                event.subchannel,
                event.data,
            )
        elif event.kind == "server_message":
            assert event.subchannel is not None and event.data is not None
            if self.ini is not None:
                if event.data and event.data[0] in (0x0B, 0x0C):
                    bucket = self._ini_write_buckets.get(client.id)
                    if bucket is None:
                        bucket = TokenBucket(
                            self.config.ini_write_rate, self.config.ini_write_burst
                        )
                        self._ini_write_buckets[client.id] = bucket
                    if not bucket.consume(1):
                        # over-budget INI writes are dropped without a
                        # response, matching the service's normal Set semantics.
                        return
                consumed, reply = self.ini.handle(event.data)
                if consumed:
                    if reply is not None:
                        await client.send(reply, subchannel=0)
                    return
            await self._call_hook("on_server_message", client, event.subchannel, event.data)
        elif event.kind == "udp_channel_message":
            assert event.session_id is not None and event.subchannel is not None and event.data is not None
            session = self._session(event.session_id, name=event.name)
            await self._call_hook(
                "on_udp_channel_message", client, session, event.subchannel, event.data
            )
        elif event.kind == "udp_server_message":
            assert event.subchannel is not None and event.data is not None
            await self._call_hook("on_udp_server_message", client, event.subchannel, event.data)
        elif event.kind == "udp_private_message":
            assert (
                event.session_id is not None
                and event.target_id is not None
                and event.subchannel is not None
                and event.data is not None
            )
            session = self._session(event.session_id, name=event.name)
            target = self._client(event.target_id)
            await self._call_hook(
                "on_udp_private_message",
                client,
                session,
                target,
                event.subchannel,
                event.data,
            )
        elif event.kind == "disconnect":
            self._ini_write_buckets.pop(client.id, None)
            if self.udp is not None and not self.config.persist_network_metadata:
                # observed UDP endpoint/rate state is connection-scoped
                # compatibility metadata.  By default it is discarded immediately
                # at disconnect.  No disk-persistence path is implemented.
                self.udp.forget_connection(client.id)
            await self._call_hook("on_disconnect", client)
            # Do not retain one facade object per historical connection forever.
            self._clients.pop(client.id, None)

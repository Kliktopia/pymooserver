"""Deterministic MooAPI server state machine."""

from __future__ import annotations

from dataclasses import dataclass

from .codec.packets import (
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
    ToServer,
    Welcome,
    UdpChannelBlast,
    UdpPrivateBlast,
    UdpToServer,
)
from .config import ServerConfig
from .effects import ApplicationEvent, CloseConnection, Effect, SendTcp, SendUdp
from .ids import IdAllocator, IdExhausted
from .model import Connection, Session


@dataclass(frozen=True, slots=True)
class ConnectResult:
    connection_id: int | None
    effects: tuple[Effect, ...]


@dataclass(frozen=True, slots=True)
class UdpReceiveResult:
    accepted: bool
    sender_id: int | None
    effects: tuple[Effect, ...]


class Hub:
    def __init__(self, config: ServerConfig | None = None) -> None:
        self.config = config or ServerConfig()
        # The original MooClick specification allows the server MOTD to be
        # changed while hosting. The new value is sent only to future connections.
        self.motd = self.config.motd
        self.ids = IdAllocator()
        self.connections: dict[int, Connection] = {}
        self.sessions: dict[int, Session] = {}
        self._sessions_by_name: dict[bytes, int] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def connect(self, peer_ip: str | bytes = "0.0.0.0") -> ConnectResult:
        if len(self.connections) >= self.config.max_connections:
            return ConnectResult(None, ())
        try:
            connection_id = self.ids.allocate()
        except IdExhausted:
            return ConnectResult(None, ())

        peer_ip_bytes = peer_ip.encode("ascii") if isinstance(peer_ip, str) else peer_ip
        initial_dialect = "A" if self.config.dialect == "auto" else self.config.dialect
        connection = Connection(connection_id, peer_ip_bytes, dialect=initial_dialect)
        self.connections[connection_id] = connection

        # MOTD and ID are sent immediately, in this order, before
        effects: tuple[Effect, ...] = (
            SendTcp(connection_id, Motd(self.motd)),
            SendTcp(connection_id, AssignedId(3, connection_id)),
            ApplicationEvent("connect", connection_id, name=connection.name),
        )
        return ConnectResult(connection_id, effects)

    def set_motd(self, motd: bytes) -> None:
        """Set the MOTD used for future connections.

        Changing the MOTD does not update already connected players; they see the
        new value after reconnecting.
        """
        if not motd:
            raise ValueError("MOTD must be non-empty")
        if len(motd) > self.config.codec.max_motd_bytes:
            raise ValueError("MOTD exceeds codec cap")
        self.motd = bytes(motd)

    def disconnect(self, connection_id: int) -> tuple[Effect, ...]:
        connection = self.connections.get(connection_id)
        if connection is None:
            return ()

        effects: list[Effect] = []
        # disconnect leaves sessions in the order they were joined.
        for session_id in list(connection.session_ids):
            effects.extend(self._leave(connection, session_id, notify_leaver=False))
        self.connections.pop(connection_id, None)
        effects.append(ApplicationEvent("disconnect", connection_id, name=connection.name))
        return tuple(effects)

    def protocol_error(self, connection_id: int, reason: str) -> tuple[Effect, ...]:
        effects = list(self.disconnect(connection_id))
        effects.append(CloseConnection(connection_id, reason))
        return tuple(effects)

    # ------------------------------------------------------------------
    # Typed packet input
    # ------------------------------------------------------------------
    def receive(self, connection_id: int, packet: ClientPacket) -> tuple[Effect, ...]:
        connection = self.connections.get(connection_id)
        if connection is None:
            return ()

        if self.config.require_hello and not connection.hello_complete and not isinstance(packet, Hello):
            # Require the normal hello before stateful packets so the connection
            # has a stable identity before joining or messaging.
            return self.protocol_error(connection_id, "stateful packet before hello")

        if isinstance(packet, Hello):
            return self._hello(connection, packet)
        if isinstance(packet, Join):
            return self._join(connection, packet.name)
        if isinstance(packet, Leave):
            return tuple(self._leave(connection, packet.channel_id, notify_leaver=True))
        if isinstance(packet, Rename):
            return self._rename(connection, packet.name)
        if isinstance(packet, ChannelMessage):
            return self._channel_message(connection, packet)
        if isinstance(packet, PrivateMessage):
            return self._private_message(connection, packet)
        if isinstance(packet, ToServer):
            # The runtime may consume configured INI requests from this event.
            # Other ToServer payloads are application-visible but never relayed by
            # a plain MOS.
            return (
                ApplicationEvent(
                    "server_message",
                    connection.id,
                    subchannel=packet.subchannel,
                    data=packet.data,
                    name=connection.name,
                ),
            )
        raise TypeError(f"unsupported client packet: {type(packet).__name__}")

    # ------------------------------------------------------------------
    # Packet handlers
    # ------------------------------------------------------------------
    def _hello(self, connection: Connection, packet: Hello) -> tuple[Effect, ...]:
        if connection.hello_complete and self.config.require_hello:
            return self.protocol_error(connection.id, "repeated hello")
        connection.version = packet.version
        connection.name = packet.name
        connection.hello_complete = True
        return (ApplicationEvent("hello", connection.id, name=connection.name),)

    def _join(self, connection: Connection, name: bytes) -> tuple[Effect, ...]:
        existing_id = self._sessions_by_name.get(name)
        session = self.sessions.get(existing_id) if existing_id is not None else None

        already_member = session is not None and connection.id in session.member_ids
        if already_member and self.config.duplicate_join == "ignore":
            # ignore duplicate joins by default.  Clients do not
            # de-duplicate 06/07, so the default is to ignore repeats.
            return ()

        if len(connection.session_ids) >= self.config.max_sessions_per_connection:
            return ()

        if session is None:
            if len(self.sessions) >= self.config.max_sessions:
                return ()
            try:
                session_id = self.ids.allocate()
            except IdExhausted:
                return ()
            session = Session(session_id, name)
            self.sessions[session.id] = session
            self._sessions_by_name[name] = session.id
        elif len(session.member_ids) >= self.config.max_members_per_session:
            return ()

        # In older-client comparison mode, a repeated join appends the connection again
        # and resends Welcome/Joined/Exists, which can create duplicate client users.
        # For the resend, "other members" excludes every existing occurrence of
        # the joiner itself.  Default compatibility mode never reaches this path twice.
        others = [mid for mid in session.member_ids if mid != connection.id]
        session.member_ids.append(connection.id)
        connection.session_ids.append(session.id)
        master_id = session.master_id
        ip = self._wire_ip(connection)

        effects: list[Effect] = [
            SendTcp(
                connection.id,
                Welcome(
                    connection.id,
                    session.id,
                    master_id,
                    connection.name,
                    ip,
                    session.name,
                ),
            )
        ]

        # Send all 06 notifications to existing members first...
        for other_id in others:
            effects.append(
                SendTcp(
                    other_id,
                    Joined(
                        connection.id,
                        session.id,
                        master_id,
                        connection.name,
                        ip,
                    ),
                )
            )

        # ...then all 07 existing-member descriptions back to the joiner.
        for other_id in others:
            other = self.connections[other_id]
            effects.append(
                SendTcp(
                    connection.id,
                    Exists(
                        other.id,
                        session.id,
                        master_id,
                        other.name,
                        self._wire_ip(other),
                    ),
                )
            )

        effects.append(
            ApplicationEvent("join", connection.id, session.id, name=session.name)
        )
        return tuple(effects)

    def _leave(
        self,
        connection: Connection,
        session_id: int,
        *,
        notify_leaver: bool,
    ) -> list[Effect]:
        session = self.sessions.get(session_id)
        if session is None or connection.id not in session.member_ids:
            return []

        old_members = list(session.member_ids)
        old_master = session.master_id
        effects: list[Effect] = []

        if self.config.master_announce == "original":
            # form/broadcast 05 before removal, so a departing master
            # confirms this exact old-master field.
            for member_id in old_members:
                if member_id == connection.id and not notify_leaver:
                    continue
                effects.append(SendTcp(member_id, Left(connection.id, session.id, old_master)))
            session.member_ids.remove(connection.id)
        else:
            # remove first and announce the oldest remaining member.
            # This avoids the real client's dangling master pointer.
            session.member_ids.remove(connection.id)
            new_master = session.master_id
            for member_id in session.member_ids:
                effects.append(SendTcp(member_id, Left(connection.id, session.id, new_master)))
            if notify_leaver:
                effects.append(SendTcp(connection.id, Left(connection.id, session.id, new_master)))

        if session.id in connection.session_ids:
            connection.session_ids.remove(session.id)

        effects.append(
            ApplicationEvent("leave", connection.id, session.id, name=session.name)
        )

        if not session.member_ids:
            self.sessions.pop(session.id, None)
            self._sessions_by_name.pop(session.name, None)

        return effects

    def _rename(self, connection: Connection, name: bytes) -> tuple[Effect, ...]:
        connection.name = name

        if self.config.rename_broadcast == "per-session":
            # Emit once per session membership, so peers sharing multiple sessions
            # can receive multiple identical Alias packets.
            effects = []
            for session_id in connection.session_ids:
                session = self.sessions.get(session_id)
                if session is None:
                    continue
                for member_id in session.member_ids:
                    effects.append(SendTcp(member_id, Alias(connection.id, name)))
        else:
            # preserve first-encounter ordering while de-duplicating
            # recipients across all shared sessions.
            recipients: list[int] = []
            seen: set[int] = set()
            for session_id in connection.session_ids:
                session = self.sessions.get(session_id)
                if session is None:
                    continue
                for member_id in session.member_ids:
                    if member_id not in seen:
                        seen.add(member_id)
                        recipients.append(member_id)
            if connection.id not in seen:
                recipients.append(connection.id)
            effects = [
                SendTcp(member_id, Alias(connection.id, name)) for member_id in recipients
            ]
        effects.append(ApplicationEvent("rename", connection.id, name=connection.name))
        return tuple(effects)

    def _channel_message(
        self, connection: Connection, packet: ChannelMessage
    ) -> tuple[Effect, ...]:
        session = self.sessions.get(packet.channel_id)
        if session is None or connection.id not in session.member_ids:
            return ()
        effects: list[Effect] = []
        for member_id in session.member_ids:
            if member_id == connection.id:
                continue
            effects.append(
                SendTcp(
                    member_id,
                    FromChannel(
                        packet.subchannel,
                        session.id,
                        connection.id,
                        packet.data,
                    ),
                )
            )
        effects.append(
            ApplicationEvent(
                "channel_message",
                connection.id,
                session.id,
                packet.subchannel,
                packet.data,
                name=session.name,
            )
        )
        return tuple(effects)

    def _private_message(
        self, connection: Connection, packet: PrivateMessage
    ) -> tuple[Effect, ...]:
        session = self.sessions.get(packet.channel_id)
        if session is None:
            return ()
        if connection.id not in session.member_ids or packet.target_id not in session.member_ids:
            return ()
        return (
            SendTcp(
                packet.target_id,
                FromChannel(
                    packet.subchannel,
                    session.id,
                    connection.id,
                    packet.data,
                ),
            ),
            ApplicationEvent(
                "private_message",
                connection.id,
                session.id,
                packet.subchannel,
                packet.data,
                target_id=packet.target_id,
                name=session.name,
            ),
        )

    # ------------------------------------------------------------------
    # Dialect-A UDP input
    # ------------------------------------------------------------------
    def receive_udp(
        self, packet: UdpChannelBlast | UdpPrivateBlast | UdpToServer, *, source_ip: str | bytes
    ) -> UdpReceiveResult:
        """Validate and relay one decoded dialect-A UDP datagram.

        A datagram is associated with its claimed ``from`` connection ID and the
        matching TCP peer IP. Endpoint learning is performed by the transport only
        when ``accepted`` is true.
        """
        source_ip_bytes = source_ip.encode("ascii") if isinstance(source_ip, str) else source_ip
        sender_id = packet.sender_id
        connection = self.connections.get(sender_id)
        if connection is None or connection.peer_ip != source_ip_bytes:
            return UdpReceiveResult(False, sender_id, ())

        if isinstance(packet, UdpToServer):
            return UdpReceiveResult(
                True,
                sender_id,
                (
                    ApplicationEvent(
                        "udp_server_message",
                        sender_id,
                        subchannel=packet.subchannel,
                        data=packet.data,
                        name=connection.name,
                    ),
                ),
            )

        if isinstance(packet, UdpPrivateBlast):
            session = self.sessions.get(packet.channel_id)
            if (
                session is None
                or sender_id not in session.member_ids
                or packet.target_id not in session.member_ids
            ):
                return UdpReceiveResult(False, sender_id, ())
            effects: list[Effect] = []
            if self.config.relay_private_blast:
                effects.append(SendUdp(packet.target_id, packet))
            effects.append(
                ApplicationEvent(
                    "udp_private_message",
                    sender_id,
                    session.id,
                    packet.subchannel,
                    packet.data,
                    target_id=packet.target_id,
                    name=session.name,
                )
            )
            return UdpReceiveResult(True, sender_id, tuple(effects))

        if packet.target_id != 0:
            # dialect-A User:Blast uses the same type-01 packet with a
            # non-zero target, but sends it directly to the peer IP rather than to a
            # standalone MOS.  Therefore a non-zero target arriving at this server is
            # not a valid client->server channel blast.  A combined host+client Moo
            # instance may internally route such a packet to its local CMooPlayer;
            # outbound for server-relayed channel blasts.
            return UdpReceiveResult(False, sender_id, ())
        session = self.sessions.get(packet.channel_id)
        if session is None or sender_id not in session.member_ids:
            return UdpReceiveResult(False, sender_id, ())

        effects: list[Effect] = []
        for member_id in session.member_ids:
            if member_id == sender_id and not self.config.udp_echo_to_sender:
                continue
            effects.append(
                SendUdp(
                    member_id,
                    UdpChannelBlast(
                        packet.subchannel,
                        session.id,
                        sender_id,
                        member_id,
                        packet.data,
                    ),
                )
            )
        effects.append(
            ApplicationEvent(
                "udp_channel_message",
                sender_id,
                session.id,
                packet.subchannel,
                packet.data,
                name=session.name,
            )
        )
        return UdpReceiveResult(True, sender_id, tuple(effects))

    # ------------------------------------------------------------------
    # Server/application actions
    # ------------------------------------------------------------------
    def send_server_message(
        self, connection_id: int, data: bytes, *, subchannel: int = 0
    ) -> tuple[Effect, ...]:
        """Send a server-originated ``01`` packet to one connected client.

        ``from=0`` denotes the server and clients accept any channel ID
        in that case.  We use ``chan=0`` for direct server messages.
        """
        if connection_id not in self.connections:
            return ()
        return (SendTcp(connection_id, FromChannel(subchannel, 0, 0, data)),)

    def blast_server_message(
        self, connection_id: int, data: bytes, *, subchannel: int = 0
    ) -> tuple[Effect, ...]:
        """Blast one server-originated unreliable message to a client.

        MooClick ``Connection: Blast`` sends packet type ``01`` with
        ``channel=0`` and ``from=0`` and addresses it to the selected connection.
        Dialect A carries that recipient id in the wire ``to`` field.  Dialect B's
        type-01 layout has no ``to`` field; its client receiver has a distinct
        ``from == 0`` server-message path, so the same typed packet converts safely.
        """
        if connection_id not in self.connections:
            return ()
        return (
            SendUdp(
                connection_id,
                UdpChannelBlast(subchannel, 0, 0, connection_id, data),
            ),
        )

    def broadcast_server_message(
        self, session_id: int, data: bytes, *, subchannel: int = 0
    ) -> tuple[Effect, ...]:
        """Send a server-originated message to every member of a session."""
        session = self.sessions.get(session_id)
        if session is None:
            return ()
        return tuple(
            SendTcp(member_id, FromChannel(subchannel, session.id, 0, data))
            for member_id in session.member_ids
        )

    def blast_session_message(
        self, session_id: int, data: bytes, *, subchannel: int = 0
    ) -> tuple[Effect, ...]:
        """Blast a server-originated unreliable message to a session.

        ``Session: Blast`` is delivered to every current member, marked
        as coming from the server (``from=0``).  Dialect-A recipients get their own
        id in ``to``; dialect-B encoding omits ``to`` as required by its 11-byte
        type-01 header.
        """
        session = self.sessions.get(session_id)
        if session is None:
            return ()
        return tuple(
            SendUdp(
                member_id,
                UdpChannelBlast(
                    subchannel,
                    session.id,
                    0,
                    member_id,
                    data,
                ),
            )
            for member_id in session.member_ids
        )

    def broadcast_all_server_message(
        self, data: bytes, *, subchannel: int = 0
    ) -> tuple[Effect, ...]:
        """Send one reliable server-wide message to every connection."""
        return tuple(
            SendTcp(connection_id, FromChannel(subchannel, 0, 0, data))
            for connection_id in self.connections
        )

    def blast_all_server_message(
        self, data: bytes, *, subchannel: int = 0
    ) -> tuple[Effect, ...]:
        """Blast one unreliable server-wide message to every connection."""
        return tuple(
            SendUdp(
                connection_id,
                UdpChannelBlast(subchannel, 0, 0, connection_id, data),
            )
            for connection_id in self.connections
        )

    def sign_off_session_member(self, session_id: int, connection_id: int) -> tuple[Effect, ...]:
        """Sign one connection off a session without disconnecting its socket.

        The member is removed, the oldest remaining member becomes master when
        needed, and the normal ``Left`` notification is emitted.
        """
        connection = self.connections.get(connection_id)
        session = self.sessions.get(session_id)
        if connection is None or session is None or connection_id not in session.member_ids:
            return ()
        return tuple(self._leave(connection, session_id, notify_leaver=True))

    def destroy_session(self, session_id: int) -> tuple[Effect, ...]:
        """Destroy a session by signing off members in membership order.

        Later members observe earlier members leaving before receiving their own
        ``Left`` packet. The packet ``master_id`` follows the configured leave policy.
        """
        if session_id not in self.sessions:
            return ()

        effects: list[Effect] = []
        while True:
            session = self.sessions.get(session_id)
            if session is None or not session.member_ids:
                break
            connection = self.connections.get(session.member_ids[0])
            if connection is None:
                # Keep state coherent even if a stale member id is encountered.
                session.member_ids.pop(0)
                continue
            effects.extend(self._leave(connection, session_id, notify_leaver=True))
        return tuple(effects)

    def rename_connection(self, connection_id: int, name: bytes) -> tuple[Effect, ...]:
        if not isinstance(name, bytes):
            raise TypeError("name must be bytes")
        if not (1 <= len(name) <= self.config.codec.max_name_bytes):
            raise ValueError("name length is outside configured limits")
        if b"\x00" in name:
            raise ValueError("name must not contain NUL")
        connection = self.connections.get(connection_id)
        if connection is None:
            return ()
        return self._rename(connection, name)

    def close_connection(self, connection_id: int, reason: str = "closed by application") -> tuple[Effect, ...]:
        if connection_id not in self.connections:
            return ()
        effects = list(self.disconnect(connection_id))
        effects.append(CloseConnection(connection_id, reason))
        return tuple(effects)

    def _wire_ip(self, connection: Connection) -> bytes:
        return connection.peer_ip if self.config.expose_client_ip else b"0.0.0.0"

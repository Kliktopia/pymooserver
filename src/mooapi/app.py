"""Application callbacks for the MooAPI server."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .server import Client, Session


class MooApplication:
    """Base application with optional sync or async hooks.

    Subclasses may implement any hook as either ``def`` or ``async def``.  Hook
    arguments are lightweight action facades; application payloads remain raw bytes.
    """

    def on_connect(self, client: "Client") -> None:
        pass

    def on_hello(self, client: "Client") -> None:
        pass

    def on_join(self, client: "Client", session: "Session") -> None:
        pass

    def on_leave(self, client: "Client", session: "Session") -> None:
        pass

    def on_rename(self, client: "Client") -> None:
        pass

    def on_channel_message(
        self,
        client: "Client",
        session: "Session",
        subchannel: int,
        data: bytes,
    ) -> None:
        pass

    def on_private_message(
        self,
        client: "Client",
        session: "Session",
        target: "Client",
        subchannel: int,
        data: bytes,
    ) -> None:
        pass

    def on_server_message(self, client: "Client", subchannel: int, data: bytes) -> None:
        pass

    def on_udp_channel_message(
        self,
        client: "Client",
        session: "Session",
        subchannel: int,
        data: bytes,
    ) -> None:
        pass

    def on_udp_server_message(self, client: "Client", subchannel: int, data: bytes) -> None:
        """Handle an unreliable client-to-server Blast.

        Native hosted Moo routes UDP type ``02`` through the same high-level
        message-from-client event as reliable TCP type ``02``.  The default
        therefore delegates to :meth:`on_server_message`.  Override this hook
        only when application code explicitly needs to distinguish transport.
        """
        return self.on_server_message(client, subchannel, data)

    def on_udp_private_message(
        self,
        client: "Client",
        session: "Session",
        target: "Client",
        subchannel: int,
        data: bytes,
    ) -> None:
        pass

    def on_disconnect(self, client: "Client") -> None:
        pass

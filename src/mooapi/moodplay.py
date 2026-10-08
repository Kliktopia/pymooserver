"""mooDPlay application layer for PyMooServer.

This module intentionally builds on the ordinary PyMooServer/MooAPI state machine
rather than replacing it.  mooDPlay-specific control traffic is carried as reliable
TCP message-to-server traffic on a reserved subchannel.  The mooDPlay client never
requires UDP/Blast.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

from .app import MooApplication

CONTROL_SUBCHANNEL = 32000
MAGIC = b"DPSC"
VERSION = 1

OP_HELLO = 1
OP_CAPS = 2
OP_CLAIM = 3
OP_KICK = 4
OP_HOST_CHANGED = 5
OP_JOIN_AUTH = 6
OP_AUTH_RESULT = 7
OP_LIST = 8
OP_LIST_REPLY = 9

CAP_AUTHORITATIVE_HOST = 1 << 0
CAP_KICK = 1 << 1
CAP_MAX_PLAYERS = 1 << 2
CAP_PASSWORD = 1 << 3
CAP_HOST_MIGRATION = 1 << 4
CAPS = (
    CAP_AUTHORITATIVE_HOST
    | CAP_KICK
    | CAP_MAX_PLAYERS
    | CAP_PASSWORD
    | CAP_HOST_MIGRATION
)

SESSION_PREFIX = b"__mooDPlay_v1__"

def wire_session_name(guid: bytes, visible_name: bytes) -> bytes:
    """Return the private Moo session name used by the mooDPlay logical realm."""
    if len(guid) != 16:
        raise ValueError("mooDPlay application GUID must be 16 bytes")
    return SESSION_PREFIX + guid.hex().encode("ascii") + b"__" + bytes(visible_name)

def split_wire_session_name(name: bytes):
    """Return (guid, visible_name) for a mooDPlay-internal session name, else None."""
    raw = bytes(name)
    if not raw.startswith(SESSION_PREFIX):
        return None
    tail = raw[len(SESSION_PREFIX):]
    if len(tail) < 34 or tail[32:34] != b"__":
        return None
    try:
        guid = bytes.fromhex(tail[:32].decode("ascii"))
    except (ValueError, UnicodeDecodeError):
        return None
    if len(guid) != 16:
        return None
    return guid, tail[34:]


def frame(op: int, payload: bytes = b"") -> bytes:
    return MAGIC + bytes((VERSION, op)) + payload


def parse(data: bytes):
    if len(data) < 6 or data[:4] != MAGIC or data[4] != VERSION:
        return None
    return data[5], data[6:]


def list_reply(records) -> bytes:
    out = bytearray(frame(OP_LIST_REPLY))
    out += struct.pack("<H", len(records))
    for sid, current, max_users, flags, name in records:
        name = bytes(name)
        out += struct.pack("<IIIBH", sid, current, max_users, flags, len(name))
        out += name
    return bytes(out)


@dataclass
class CompatSession:
    host_id: int
    host_type: int = 1
    max_users: int = 0
    password: bytes = b""
    guid: bytes = b"\0" * 16
    visible_name: bytes = b""
    closing: bool = False


class MooDPlayApplication(MooApplication):
    """Host-authority/session-directory layer used by mooDPlay clients.

    Ordinary MooAPI traffic is still handled by PyMooServer's normal Hub.  Only
    reserved-subchannel DPSC control messages are interpreted here.
    """

    def __init__(self) -> None:
        self.runtime = None
        self.sessions: dict[int, CompatSession] = {}
        self.authorized_joins: set[tuple[int, int]] = set()
        self.moodplay_clients: set[int] = set()

    def bind(self, runtime):
        self.runtime = runtime
        return self

    def on_udp_server_message(self, client, subchannel, data):
        # mooDPlay control traffic is deliberately TCP-only.  Native MooAPI UDP/Blast
        # remains available to ordinary clients on the same listener.
        return None

    async def on_server_message(self, client, subchannel, data):
        if subchannel != CONTROL_SUBCHANNEL:
            return
        parsed = parse(data)
        if parsed is None or self.runtime is None:
            return
        op, body = parsed

        if op == OP_HELLO:
            if len(body) not in (0, 4):
                return
            required = struct.unpack("<I", body)[0] if body else 0
            if required & ~CAPS:
                return
            self.moodplay_clients.add(client.id)
            await client.send(
                frame(OP_CAPS, struct.pack("<I", CAPS)),
                subchannel=CONTROL_SUBCHANNEL,
            )
            return

        # Every mooDPlay control other than HELLO requires successful explicit
        # identification on this connection.  Native MooAPI clients sharing the
        # listener can therefore never exercise mooDPlay authority controls.
        if client.id not in self.moodplay_clients:
            return

        if op == OP_LIST:
            if len(body) != 16:
                return
            guid = bytes(body)
            records = []
            for sid, state in self.sessions.items():
                if state.guid != guid or state.closing:
                    continue
                sess = self.runtime._session(sid)
                current = len(sess.clients)
                joinable = not state.max_users or current < state.max_users
                flags = (1 if state.password else 0) | (2 if joinable else 0)
                records.append((sid, current, state.max_users, flags, state.visible_name))
            await client.send(list_reply(records), subchannel=CONTROL_SUBCHANNEL)
            return

        if op == OP_JOIN_AUTH:
            if len(body) < 6:
                return
            sid = struct.unpack_from("<I", body, 0)[0]
            plen = struct.unpack_from("<H", body, 4)[0]
            if len(body) != 6 + plen:
                return
            supplied = bytes(body[6:])
            state = self.sessions.get(sid)
            accepted = False
            reason = b"Session is no longer available."
            if state is not None and not state.closing:
                sess = self.runtime._session(sid)
                current = len(sess.clients)
                if state.max_users and current >= state.max_users:
                    reason = b"Session is full."
                elif supplied != state.password:
                    reason = b"Incorrect password."
                else:
                    accepted = True
                    reason = b""
                    self.authorized_joins.add((client.id, sid))
            payload = struct.pack("<IBH", sid, 1 if accepted else 0, len(reason)) + reason
            await client.send(frame(OP_AUTH_RESULT, payload), subchannel=CONTROL_SUBCHANNEL)
            return

        if op == OP_CLAIM:
            if len(body) < 25:
                return
            sid, host_type, max_users = struct.unpack_from("<IBI", body, 0)
            guid = body[9:25]
            password = body[25:]
            sess = next((s for s in client.sessions if s.id == sid), None)
            if sess is None:
                return
            parsed_name = split_wire_session_name(sess.name)
            if parsed_name is None or parsed_name[0] != guid:
                return
            visible_name = parsed_name[1]
            state = self.sessions.get(sid)
            if state is None:
                self.sessions[sid] = CompatSession(
                    client.id, host_type, max_users, password, guid, visible_name
                )
            elif state.host_id != client.id:
                return
            state = self.sessions[sid]
            await sess.broadcast(
                frame(OP_HOST_CHANGED, struct.pack("<II", sid, state.host_id)),
                subchannel=CONTROL_SUBCHANNEL,
            )
            return

        if op == OP_KICK:
            if len(body) != 8:
                return
            sid, target = struct.unpack("<II", body)
            state = self.sessions.get(sid)
            if state is None or state.host_id != client.id:
                return
            sess = next((s for s in client.sessions if s.id == sid), None)
            if sess is None:
                return
            if target == 0:
                for peer in list(sess.clients):
                    if peer.id != client.id:
                        await sess.sign_off(peer)
            else:
                peer = next((p for p in sess.clients if p.id == target), None)
                if peer is not None:
                    await sess.sign_off(peer)

    async def on_join(self, client, session):
        internal = split_wire_session_name(session.name)
        identified = client.id in self.moodplay_clients

        # Native MooAPI and mooDPlay share a listener but never a logical session
        # namespace.  A mooDPlay client may only join namespaced mooDPlay sessions;
        # a native client that somehow guesses one is immediately removed.
        if bool(internal) != identified:
            await session.sign_off(client)
            return

        if not identified:
            return

        state = self.sessions.get(session.id)
        if state is None:
            # The host joins before it can send CLAIM.  Joining a correctly
            # namespaced room is therefore allowed temporarily; CLAIM turns it
            # into an advertised compatibility session.
            return
        auth_key = (client.id, session.id)
        if state.password and auth_key not in self.authorized_joins:
            await session.sign_off(client)
            return
        self.authorized_joins.discard(auth_key)
        if state.max_users and len(session.clients) > state.max_users:
            await session.sign_off(client)

    async def on_leave(self, client, session):
        state = self.sessions.get(session.id)
        if state is None or state.closing or state.host_id != client.id:
            return
        remaining = [p for p in session.clients if p.id != client.id]
        if state.host_type == 1 and remaining:
            state.host_id = min(p.id for p in remaining)
            await session.broadcast(
                frame(OP_HOST_CHANGED, struct.pack("<II", session.id, state.host_id)),
                subchannel=CONTROL_SUBCHANNEL,
            )
        else:
            state.closing = True
            await session.destroy()
            self.sessions.pop(session.id, None)

    def on_disconnect(self, client):
        self.moodplay_clients.discard(client.id)
        self.authorized_joins = {key for key in self.authorized_joins if key[0] != client.id}

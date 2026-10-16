"""MooAPI 1.22 compatibility regressions.

Compatibility regressions. Packets are built and parsed here by hand so the reproductions do not depend on the codec under test.
"""

import asyncio
import contextlib
import struct
import time

import pytest

from mooapi.codec.packets import Hello, Join, Leave, Left
from mooapi.config import ServerConfig
from mooapi.effects import CloseConnection, SendTcp
from mooapi.hub import Hub
from mooapi.server import MooServer
from pymooserver import cli


def _blob(data: bytes) -> bytes:
    return struct.pack("<I", len(data)) + data


def _hello(name: bytes) -> bytes:
    return b"\x0c\x03\x00" + _blob(name)


def _join(name: bytes) -> bytes:
    return b"\x04" + _blob(name)


def _to_channel(subchannel: int, channel_id: int, text: bytes) -> bytes:
    return b"\x01" + struct.pack("<HI", subchannel, channel_id) + _blob(text)


def _to_server(subchannel: int, payload: bytes) -> bytes:
    return b"\x02" + struct.pack("<H", subchannel) + _blob(payload)


def _ini_request(opcode: int, filename: bytes, group: bytes, item: bytes) -> bytes:
    request = bytes([opcode])
    for part in (filename, group, item):
        request += struct.pack("<h", len(part)) + part
    return request


def _parse_server_packet(data: bytes):
    """Return (packet tuple, consumed bytes) or None when more data is needed."""

    def string_at(offset):
        if len(data) < offset + 4:
            return None
        length = struct.unpack_from("<I", data, offset)[0]
        if len(data) < offset + 4 + length:
            return None
        return data[offset + 4:offset + 4 + length], offset + 4 + length

    if not data:
        return None
    kind = data[0]
    if kind == 0x0A:
        text = string_at(1)
        return text and (("motd", text[0]), text[1])
    if kind == 0x0C:
        if len(data) < 7:
            return None
        return ("id", data[1], data[2], struct.unpack_from("<I", data, 3)[0]), 7
    if kind == 0x01:
        if len(data) < 11:
            return None
        subchannel, channel_id, sender_id = struct.unpack_from("<HII", data, 1)
        text = string_at(11)
        return text and (("msg", subchannel, channel_id, sender_id, text[0]), text[1])
    if kind == 0x05:
        if len(data) < 13:
            return None
        return ("left",) + struct.unpack_from("<III", data, 1), 13
    if kind in (0x06, 0x07, 0x08):
        if len(data) < 13:
            return None
        player_id, channel_id, master_id = struct.unpack_from("<III", data, 1)
        name = string_at(13)
        ip = name and string_at(name[1])
        if not ip:
            return None
        if kind == 0x08:
            room = string_at(ip[1])
            return room and (
                ("welcome", player_id, channel_id, master_id, name[0], ip[0], room[0]),
                room[1],
            )
        label = "joined" if kind == 0x06 else "exists"
        return (label, player_id, channel_id, master_id, name[0], ip[0]), ip[1]
    if kind == 0x0B:
        if len(data) < 9:
            return None
        name = string_at(5)
        return name and (("alias", struct.unpack_from("<I", data, 1)[0], name[0]), name[1])
    raise AssertionError("unparseable server packet %r" % data[:16])


class _MooClient:
    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer
        self.buffer = b""
        self.closed = False
        self.connection_id = None

    @classmethod
    async def connect(cls, port: int) -> "_MooClient":
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        client = cls(reader, writer)
        greeting = await client.packets(2)
        assert [packet[0] for packet in greeting] == ["motd", "id"]
        client.connection_id = greeting[1][3]
        return client

    async def send(self, data: bytes) -> None:
        self.writer.write(data)
        await self.writer.drain()

    async def packets(self, count: int = 99, timeout: float = 1.0) -> list:
        received = []
        deadline = time.monotonic() + timeout
        while len(received) < count:
            parsed = _parse_server_packet(self.buffer)
            if parsed:
                received.append(parsed[0])
                self.buffer = self.buffer[parsed[1]:]
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0 or self.closed:
                break
            try:
                chunk = await asyncio.wait_for(self.reader.read(65536), remaining)
            except asyncio.TimeoutError:
                break
            except OSError:
                chunk = b""
            if not chunk:
                self.closed = True
                break
            self.buffer += chunk
        return received

    async def close(self) -> None:
        self.writer.close()
        with contextlib.suppress(OSError):
            await self.writer.wait_closed()


@contextlib.asynccontextmanager
async def _running_server(**overrides):
    config = ServerConfig(**{"ini_enabled": False, **overrides})
    server = MooServer(config=config, host="127.0.0.1", port=0, enable_udp=False)
    await server.start()
    clients = []

    async def connect() -> _MooClient:
        client = await _MooClient.connect(server.tcp.server.sockets[0].getsockname()[1])
        clients.append(client)
        return client

    try:
        yield server, connect
    finally:
        for client in clients:
            await client.close()
        await server.close()


async def _two_players_in_lobby(connect):
    alice = await connect()
    bob = await connect()
    await alice.send(_hello(b"Alice") + _join(b"lobby"))
    channel_id = (await alice.packets(1))[0][2]
    await bob.send(_hello(b"Bob") + _join(b"lobby"))
    await bob.packets(2)
    await alice.packets(1)
    return alice, bob, channel_id


def test_empty_channel_message_does_not_disconnect_sender():
    async def scenario():
        async with _running_server() as (server, connect):
            alice, bob, channel_id = await _two_players_in_lobby(connect)
            await alice.send(_to_channel(1, channel_id, b""))
            await asyncio.sleep(0.2)
            assert alice.connection_id in server.hub.connections
            await alice.send(_to_channel(1, channel_id, b"after"))
            received = await bob.packets(2, timeout=0.5)
            texts = [packet[4] for packet in received if packet[0] == "msg"]
            assert texts and texts[-1] == b"after"

    asyncio.run(scenario())


def test_second_hello_keeps_connection_open():
    hub = Hub(ServerConfig(ini_enabled=False))
    connection_id = hub.connect("127.0.0.1").connection_id
    hub.receive(connection_id, Hello(3, b"Tom"))
    effects = hub.receive(connection_id, Hello(3, b"Tom"))
    assert not any(isinstance(effect, CloseConnection) for effect in effects)
    assert connection_id in hub.connections


def test_burst_of_seven_joins_all_welcomed():
    async def scenario():
        async with _running_server() as (_server, connect):
            joiner = await connect()
            room_names = [b"room%d" % index for index in range(7)]
            await joiner.send(_hello(b"J") + b"".join(_join(name) for name in room_names))
            received = await joiner.packets(7, timeout=1.0)
            welcomed_rooms = [packet[6] for packet in received if packet[0] == "welcome"]
            assert welcomed_rooms == room_names

    asyncio.run(scenario())


def test_long_session_name_joins():
    async def scenario():
        async with _running_server() as (_server, connect):
            joiner = await connect()
            session_name = b"S" * 100
            await joiner.send(_hello(b"J") + _join(session_name))
            received = await joiner.packets(1, timeout=1.0)
            assert received and received[0][0] == "welcome"
            assert received[0][6] == session_name

    asyncio.run(scenario())


def _master_leave_packets(master_announce="successor"):
    hub = Hub(ServerConfig(ini_enabled=False, master_announce=master_announce))
    alice_id = hub.connect("127.0.0.1").connection_id
    bob_id = hub.connect("127.0.0.1").connection_id
    for connection_id, name in ((alice_id, b"Alice"), (bob_id, b"Bob")):
        hub.receive(connection_id, Hello(3, name))
        hub.receive(connection_id, Join(b"lobby"))
    channel_id = next(iter(hub.sessions))
    effects = hub.receive(alice_id, Leave(channel_id))
    packets = {
        effect.connection_id: effect.packet
        for effect in effects
        if isinstance(effect, SendTcp) and isinstance(effect.packet, Left)
    }
    return alice_id, bob_id, channel_id, packets


def test_master_leave_announces_successor_by_default():
    alice_id, bob_id, channel_id, packets = _master_leave_packets()
    expected = Left(alice_id, channel_id, bob_id)
    assert packets[bob_id] == expected
    assert packets[alice_id] == expected


def test_master_leave_can_announce_previous_master():
    alice_id, bob_id, channel_id, packets = _master_leave_packets("original")
    expected = Left(alice_id, channel_id, alice_id)
    assert packets[bob_id] == expected
    assert packets[alice_id] == expected


def test_ini_write_burst_stores_every_value(tmp_path):
    async def scenario():
        async with _running_server(ini_enabled=True, ini_directory=str(tmp_path)) as (_server, connect):
            player = await connect()
            key_count = 30
            set_requests = b"".join(
                _to_server(
                    0,
                    _ini_request(0x0B, b"burst", b"group", b"key%d" % index)
                    + struct.pack("<i", 1000 + index),
                )
                for index in range(key_count)
            )
            get_requests = b"".join(
                _to_server(0, _ini_request(0x0D, b"burst", b"group", b"key%d" % index))
                for index in range(key_count)
            )
            await player.send(_hello(b"P") + set_requests)
            await player.send(get_requests)
            replies = await player.packets(key_count, timeout=2.0)
            values = [struct.unpack("<i", packet[4][1:])[0] for packet in replies]
            assert values == [1000 + index for index in range(key_count)]

    asyncio.run(scenario())


def test_listening_player_is_not_idle_kicked():
    async def scenario():
        async with _running_server(idle_timeout=0.5) as (server, connect):
            talker, listener, channel_id = await _two_players_in_lobby(connect)
            for _ in range(6):
                await talker.send(_to_channel(0, channel_id, b"chat"))
                await listener.packets(1, timeout=0.2)
                await asyncio.sleep(0.2)
            assert listener.connection_id in server.hub.connections
            assert not listener.closed

    asyncio.run(scenario())


def test_default_idle_timeout_is_disabled():
    assert ServerConfig().idle_timeout == 0


def test_cli_identification_timeout_reaches_mooapi_config(tmp_path, monkeypatch):
    built_configs = []

    class _ConfigBuilt(Exception):
        pass

    def recording_config(**kwargs):
        built_configs.append(ServerConfig(**kwargs))
        raise _ConfigBuilt

    monkeypatch.setattr(cli, "ServerConfig", recording_config)
    monkeypatch.setattr(cli, "DEFAULT_MOO12_PORTS", [])
    args = cli.build_unified_parser().parse_args([
        "--host", "127.0.0.1",
        "--mooapi-port", "2",
        "--ini-root", str(tmp_path / "ini"),
        "--backup-root", str(tmp_path / "backup"),
        "--no-backups",
        "--no-status",
        "--no-mooapi-udp",
        "--identification-timeout", "42",
    ])

    with pytest.raises(_ConfigBuilt):
        asyncio.run(cli.run_unified(args))
    assert built_configs[0].identification_timeout == 42.0

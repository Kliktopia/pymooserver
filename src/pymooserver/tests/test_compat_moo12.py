"""MOO1/MOO2 (MOS) compatibility regressions.

Regression cases derived from compatibility review. Tests are normal assertions once behavior is intentionally supported.
"""

import asyncio
import contextlib
import os
import socket
import stat
import time

import pytest

from pymooserver.moo12 import IniStore, Moo12Server


async def _read_until(reader: asyncio.StreamReader, marker: bytes, timeout: float = 1.0) -> bytes:
    """Collect bytes until *marker* appears, EOF, or *timeout*."""
    received = b""
    deadline = time.monotonic() + timeout
    while marker not in received:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            chunk = await asyncio.wait_for(reader.read(65536), remaining)
        except asyncio.TimeoutError:
            break
        except OSError:
            break
        if not chunk:
            break
        received += chunk
    return received


@contextlib.asynccontextmanager
async def _running_realm(tmp_path, listen_socket=None, **options):
    realm = Moo12Server(ini_dir=str(tmp_path / "ini"), server_text="test", **options)
    if listen_socket is None:
        listener = await asyncio.start_server(realm.handle_client, "127.0.0.1", 0)
    else:
        listener = await asyncio.start_server(realm.handle_client, sock=listen_socket)
    writers = []

    async def connect():
        port = listener.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writers.append(writer)
        assert b"motd|" in await _read_until(reader, b"motd|test|\n")
        return reader, writer

    try:
        yield realm, listener, connect
    finally:
        for writer in writers:
            writer.close()
        listener.close()
        if hasattr(listener, "abort_clients"):
            listener.abort_clients()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(listener.wait_closed(), 2.0)


def test_long_first_signon_record_is_accepted(tmp_path):
    async def scenario():
        async with _running_realm(tmp_path) as (_realm, _listener, connect):
            reader, writer = await connect()
            alias, channel = b"C" * 40, b"R" * 40
            signon = b"signon|" + alias + b"|" + channel + b"|\n"
            assert len(signon) > 64
            writer.write(signon)
            await writer.drain()
            reply = await _read_until(reader, b"welcome|")
            assert b"welcome|1|" + alias + b"|1\n" in reply

    asyncio.run(scenario())


def test_leading_blank_line_before_signon_is_ignored(tmp_path):
    async def scenario():
        async with _running_realm(tmp_path) as (_realm, _listener, connect):
            reader, writer = await connect()
            writer.write(b"\nsignon|Erin|Lobby|\n")
            await writer.drain()
            reply = await _read_until(reader, b"welcome|")
            assert b"welcome|1|Erin|1\n" in reply

    asyncio.run(scenario())


def test_stalled_reader_is_removed_from_channel(tmp_path):
    async def scenario():
        loop = asyncio.get_running_loop()
        listen_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # Accepted sockets inherit the small send buffer, so the stall takes kilobytes, not megabytes.
        listen_socket.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        listen_socket.bind(("127.0.0.1", 0))
        async with _running_realm(tmp_path, listen_socket, write_timeout=0.1) as (realm, listener, connect):
            watcher_reader, watcher_writer = await connect()
            watcher_writer.write(b"signon|Watcher|Lobby|\n")
            await watcher_writer.drain()
            await _read_until(watcher_reader, b"welcome|")

            stalled_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            stalled_socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
            stalled_socket.setblocking(False)
            try:
                await loop.sock_connect(stalled_socket, listener.sockets[0].getsockname())
                await loop.sock_sendall(stalled_socket, b"signon|Stall|Lobby|\n")
                deadline = time.monotonic() + 1.0
                stalled_client = None
                while stalled_client is None and time.monotonic() < deadline:
                    await asyncio.sleep(0.02)
                    stalled_client = next(
                        (client for client in realm.clients if client.alias == "Stall"), None
                    )
                assert stalled_client is not None, "stalled client never signed on"
                stalled_id = stalled_client.client_id
                await _read_until(watcher_reader, b"signon|%d|" % stalled_id)

                # The watcher floods private messages at the client that never reads.
                flood_line = b"p|%d|%s|\n" % (stalled_id, b"y" * 900)
                for _ in range(400):
                    if stalled_client.send_failed:
                        break
                    watcher_writer.write(flood_line * 10)
                    await watcher_writer.drain()
                    await asyncio.sleep(0.01)
                if not stalled_client.send_failed:
                    pytest.skip("could not fill the socket buffers to stall the server writer")

                signoff_seen = await _read_until(
                    watcher_reader, b"signoff|%d|" % stalled_id, timeout=1.0
                )
                newcomer_reader, newcomer_writer = await connect()
                newcomer_writer.write(b"signon|Newcomer|Lobby|\n")
                await newcomer_writer.drain()
                newcomer_view = await _read_until(newcomer_reader, b"signed|%d|" % stalled_id, timeout=0.5)

                assert b"signed|%d|" % stalled_id not in newcomer_view
                assert b"signoff|%d|" % stalled_id in signoff_seen
            finally:
                stalled_socket.close()

    asyncio.run(scenario())


def test_inistring_value_cannot_forge_profile_keys(tmp_path):
    async def scenario():
        async with _running_realm(tmp_path) as (_realm, _listener, connect):
            reader, writer = await connect()
            writer.write(b"signon|Mallory|Lobby|\n")
            await writer.drain()
            await _read_until(reader, b"welcome|")
            writer.write(b"inistring|Scores.INI |Top |Mallory |1\r[Top2]\rBob=99999 |\n")
            writer.write(b"getini|Scores.INI |Top2 |Bob |\n")
            await writer.drain()
            reply = await _read_until(reader, b"inistring|")
            assert b"inistring|" in reply
            assert b"99999" not in reply

    asyncio.run(scenario())


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_profile_write_preserves_file_mode(tmp_path):
    profile_path = tmp_path / "scores.imi"
    profile_path.write_bytes(b"[Top]\r\nAlice=1\r\n")
    profile_path.chmod(0o644)
    store = IniStore(str(tmp_path))
    assert store.set("scores.ini", "Top", "Alice", "2")
    assert store.get("scores.ini", "Top", "Alice", "") == "2"
    assert stat.S_IMODE(profile_path.stat().st_mode) == 0o644

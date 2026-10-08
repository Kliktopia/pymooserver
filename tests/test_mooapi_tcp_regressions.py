import asyncio

from mooapi.codec.dialect_a import encode_client_packet
from mooapi.codec.packets import Hello, Join, ToServer
from mooapi.config import ServerConfig
from mooapi.server import MooServer
from mooapi.transport.tcp import TcpPeer


class _OneChunkReader:
    def __init__(self, chunk: bytes):
        self._chunk = chunk
        self._sent = False

    async def read(self, _size: int) -> bytes:
        if not self._sent:
            self._sent = True
            return self._chunk
        return b""


class _FakeWriter:
    def __init__(self):
        self.closed = False
        self.buffer = bytearray()

    def get_extra_info(self, _name):
        return None

    def write(self, data: bytes) -> None:
        self.buffer.extend(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


async def _run_large_first_read_case() -> None:
    config = ServerConfig(
        ini_enabled=False,
        preidentify_buffer=64,
        idle_timeout=0,
        identification_timeout=1.0,
    )
    runtime = MooServer(config=config, enable_udp=False)
    result = runtime.hub.connect("127.0.0.1")
    assert result.connection_id is not None
    cid = result.connection_id

    payload = (
        encode_client_packet(Hello(3, b"tester"), config.codec)
        + encode_client_packet(ToServer(0, b"x" * 5000), config.codec)
    )
    assert len(payload) > config.preidentify_buffer

    writer = _FakeWriter()
    peer = TcpPeer(runtime.tcp, cid, _OneChunkReader(payload), writer)
    await peer.run()

    # EOF after the one deterministic read is the only reason state disappears.
    # The oversized first read must have been recognized as Moo, not rejected by
    # the pre-identification cap.
    assert peer.close_reason == "peer EOF"


async def _run_optional_hello_timeout_case() -> None:
    config = ServerConfig(
        ini_enabled=False,
        require_hello=False,
        hello_timeout=0.05,
        identification_timeout=1.0,
        idle_timeout=0,
    )
    runtime = MooServer(config=config, host="127.0.0.1", port=0, enable_udp=False)
    await runtime.start()
    try:
        assert runtime.tcp.server is not None
        port = runtime.tcp.server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            # Join is a valid first Moo packet when Hello is optional.
            writer.write(encode_client_packet(Join(b"room"), config.codec))
            await writer.drain()
            await asyncio.sleep(0.12)
            assert len(runtime.hub.connections) == 1
        finally:
            writer.close()
            await writer.wait_closed()
            await asyncio.sleep(0)
    finally:
        await runtime.close()


def test_large_first_tcp_read_is_classified_before_preidentify_cap():
    asyncio.run(_run_large_first_read_case())


def test_optional_hello_does_not_keep_hello_timeout():
    asyncio.run(_run_optional_hello_timeout_case())

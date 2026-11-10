import asyncio

from mooapi.codec.packets import Hello, Join, UdpChannelBlast, UdpPrivateBlast
from mooapi.codec.udp_a import encode_udp_a
from mooapi.codec.udp_b import encode_udp_b
from mooapi.config import ServerConfig
from mooapi.server import MooServer
from mooapi.transport.udp import UdpServerAdapter


def _joined_runtime(dialect="auto"):
    config = ServerConfig(ini_enabled=False, dialect=dialect, require_hello=True)
    runtime = MooServer(config=config, enable_udp=False)
    ids = []
    for name in (b"Alice", b"Bobby", b"Carol"):
        result = runtime.hub.connect("127.0.0.1")
        assert result.connection_id is not None
        cid = result.connection_id
        ids.append(cid)
        runtime.hub.receive(cid, Hello(3, name))
    alice, bob, carol = ids
    join_effects = runtime.hub.receive(alice, Join(b"room"))
    session_id = next(effect.session_id for effect in join_effects if getattr(effect, "kind", None) == "join")
    runtime.hub.receive(bob, Join(b"room"))
    runtime.hub.receive(carol, Join(b"room"))
    adapter = UdpServerAdapter(runtime, "127.0.0.1", 1204)
    runtime.udp = adapter
    return runtime, adapter, alice, bob, carol, session_id


async def _auto_a_target_case():
    runtime, adapter, alice, bob, _carol, session_id = _joined_runtime("auto")
    packet = UdpChannelBlast(1, session_id, alice, bob, b"private")
    await adapter.handle_datagram(encode_udp_a(packet), ("127.0.0.1", 40001))
    assert runtime.hub.connections[alice].dialect == "A"
    assert adapter.accepted_datagrams == 0
    assert adapter.drop_counts.get("hub-rejected") == 1


async def _auto_switch_only_after_accepted_b_only_packet():
    runtime, adapter, alice, bob, _carol, session_id = _joined_runtime("auto")

    invalid = UdpPrivateBlast(1, 999999, alice, bob, b"no-room")
    await adapter.handle_datagram(encode_udp_b(invalid), ("127.0.0.1", 40001))
    assert runtime.hub.connections[alice].dialect == "A"
    assert adapter.accepted_datagrams == 0

    valid = UdpPrivateBlast(1, session_id, alice, bob, b"private")
    await adapter.handle_datagram(encode_udp_b(valid), ("127.0.0.1", 40001))
    assert runtime.hub.connections[alice].dialect == "B"
    assert adapter.accepted_datagrams == 1

    short_channel = UdpChannelBlast(2, session_id, alice, 0, b"hi")
    wire = encode_udp_b(short_channel)
    assert len(wire) == 13
    await adapter.handle_datagram(wire, ("127.0.0.1", 40001))
    assert adapter.accepted_datagrams == 2


async def _explicit_b_short_case():
    _runtime, adapter, alice, _bob, _carol, session_id = _joined_runtime("B")
    short_channel = UdpChannelBlast(2, session_id, alice, 0, b"hi")
    wire = encode_udp_b(short_channel)
    assert len(wire) == 13
    await adapter.handle_datagram(wire, ("127.0.0.1", 40001))
    assert adapter.accepted_datagrams == 1


def test_auto_does_not_reinterpret_a_private_target_as_broadcast():
    asyncio.run(_auto_a_target_case())


def test_auto_switches_to_b_only_after_accepted_b_only_packet():
    asyncio.run(_auto_switch_only_after_accepted_b_only_packet())


def test_explicit_b_accepts_short_b_channel_blast():
    asyncio.run(_explicit_b_short_case())

async def _rejected_b_detection_does_not_change_dialect():
    runtime, adapter, alice, bob, _carol, session_id = _joined_runtime("auto")
    packet = UdpPrivateBlast(1, session_id, alice, bob, b"private")
    wire = encode_udp_b(packet)

    await adapter.handle_datagram(wire, ("127.0.0.2", 40001))
    assert runtime.hub.connections[alice].dialect == "A"
    assert adapter.accepted_datagrams == 0
    assert adapter.drop_counts.get("source-ip-mismatch") == 1


def test_rejected_b_detection_never_changes_connection_dialect():
    asyncio.run(_rejected_b_detection_does_not_change_dialect())



# --- relay of dialect-A User:Blast (ServerConfig.relay_direct_blast) ---------

def _relay_runtime():
    config = ServerConfig(ini_enabled=False, dialect="auto", require_hello=True, relay_direct_blast=True)
    runtime = MooServer(config=config, enable_udp=False)
    ids = []
    for name in (b"Alice", b"Bobby", b"Carol"):
        cid = runtime.hub.connect("127.0.0.1").connection_id
        ids.append(cid)
        runtime.hub.receive(cid, Hello(3, name))
    alice, bob, carol = ids
    effects = runtime.hub.receive(alice, Join(b"room"))
    session_id = next(e.session_id for e in effects if getattr(e, "kind", None) == "join")
    runtime.hub.receive(bob, Join(b"room"))
    runtime.hub.receive(carol, Join(b"other"))
    return runtime, alice, bob, carol, session_id


def test_relay_direct_blast_delivers_only_to_the_target():
    from mooapi.effects import SendUdp

    runtime, alice, bob, _carol, session_id = _relay_runtime()
    result = runtime.hub.receive_udp(
        UdpChannelBlast(5, session_id, alice, bob, b"move"), source_ip="127.0.0.1"
    )
    assert result.accepted
    sends = [e for e in result.effects if isinstance(e, SendUdp)]
    assert len(sends) == 1
    assert sends[0].connection_id == bob
    assert sends[0].packet == UdpChannelBlast(5, session_id, alice, bob, b"move")


def test_relay_direct_blast_rejects_targets_outside_the_session_and_self():
    runtime, alice, bob, carol, session_id = _relay_runtime()
    for target in (carol, alice, 999999):
        result = runtime.hub.receive_udp(
            UdpChannelBlast(5, session_id, alice, target, b"x"), source_ip="127.0.0.1"
        )
        assert not result.accepted
        assert result.effects == ()
    wrong_ip = runtime.hub.receive_udp(
        UdpChannelBlast(5, session_id, alice, bob, b"x"), source_ip="10.0.0.9"
    )
    assert not wrong_ip.accepted


def test_relay_direct_blast_is_sent_to_the_targets_learned_endpoint():
    sent = []
    runtime, alice, bob, _carol, session_id = _relay_runtime()
    adapter = UdpServerAdapter(runtime, "127.0.0.1", 1204)
    runtime.udp = adapter

    class _Transport:
        def sendto(self, data, addr):
            sent.append((data, addr))

    adapter.transport = _Transport()

    async def go():
        # Bob is behind NAT: his datagrams come from port 51000, not 1204.
        await adapter.handle_datagram(
            encode_udp_a(UdpChannelBlast(5, session_id, bob, alice, b"hi")), ("127.0.0.1", 51000)
        )
        sent.clear()
        await adapter.handle_datagram(
            encode_udp_a(UdpChannelBlast(5, session_id, alice, bob, b"move")), ("127.0.0.1", 52000)
        )

    asyncio.run(go())
    assert sent == [(encode_udp_a(UdpChannelBlast(5, session_id, alice, bob, b"move")), ("127.0.0.1", 51000))]

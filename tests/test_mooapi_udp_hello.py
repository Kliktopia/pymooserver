from mooapi.codec.packets import Hello, UdpToServer
from mooapi.config import ServerConfig
from mooapi.hub import Hub


def test_udp_obeys_required_hello():
    hub = Hub(ServerConfig(ini_enabled=False, require_hello=True))
    connected = hub.connect("127.0.0.1")
    assert connected.connection_id is not None
    cid = connected.connection_id

    packet = UdpToServer(0, cid, b"before-hello")
    before = hub.receive_udp(packet, source_ip="127.0.0.1")
    assert before.accepted is False
    assert before.effects == ()

    hub.receive(cid, Hello(3, b"tester"))
    after = hub.receive_udp(packet, source_ip="127.0.0.1")
    assert after.accepted is True
    assert len(after.effects) == 1
    assert after.effects[0].kind == "udp_server_message"


def test_udp_still_works_without_hello_when_hello_is_optional():
    hub = Hub(ServerConfig(ini_enabled=False, require_hello=False))
    connected = hub.connect("127.0.0.1")
    assert connected.connection_id is not None
    cid = connected.connection_id

    result = hub.receive_udp(UdpToServer(0, cid, b"allowed"), source_ip="127.0.0.1")
    assert result.accepted is True

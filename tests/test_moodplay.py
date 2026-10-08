import struct

from mooapi.moodplay import (
    CAPS,
    MAGIC,
    OP_CAPS,
    OP_HELLO,
    OP_LIST_REPLY,
    VERSION,
    frame,
    list_reply,
    parse,
    split_wire_session_name,
    wire_session_name,
)


def test_control_frame_round_trip():
    payload = b"abc"
    wire = frame(OP_HELLO, payload)
    assert wire[:4] == MAGIC
    assert wire[4] == VERSION
    assert parse(wire) == (OP_HELLO, payload)


def test_caps_payload_is_stable_u32():
    wire = frame(OP_CAPS, struct.pack("<I", CAPS))
    op, payload = parse(wire)
    assert op == OP_CAPS
    assert struct.unpack("<I", payload)[0] == CAPS


def test_list_reply_encodes_session_record():
    wire = list_reply([(12, 2, 4, 3, b"Room")])
    op, payload = parse(wire)
    assert op == OP_LIST_REPLY
    assert struct.unpack_from("<H", payload, 0)[0] == 1
    sid, current, maximum, flags, name_len = struct.unpack_from("<IIIBH", payload, 2)
    assert (sid, current, maximum, flags, name_len) == (12, 2, 4, 3, 4)
    assert payload[17:21] == b"Room"


def test_moodplay_wire_namespace_is_separate_from_native_session_names():
    guid = bytes(range(16))
    wire = wire_session_name(guid, b"Room")
    assert wire != b"Room"
    assert split_wire_session_name(wire) == (guid, b"Room")
    assert split_wire_session_name(b"Room") is None


def test_client_identification_is_explicit_and_connection_scoped():
    import asyncio
    from mooapi.moodplay import CONTROL_SUBCHANNEL, MooDPlayApplication

    class FakeClient:
        def __init__(self, cid):
            self.id = cid
            self.sent = []
        async def send(self, data, *, subchannel=0):
            self.sent.append((subchannel, data))

    app = MooDPlayApplication()
    app.runtime = object()  # HELLO only needs the application to be bound.
    client = FakeClient(42)
    asyncio.run(app.on_server_message(client, CONTROL_SUBCHANNEL, frame(OP_HELLO)))
    assert 42 in app.moodplay_clients
    assert client.sent and parse(client.sent[-1][1])[0] == OP_CAPS
    app.on_disconnect(client)
    assert 42 not in app.moodplay_clients


def test_non_hello_controls_require_moodplay_identification():
    import asyncio
    from mooapi.moodplay import CONTROL_SUBCHANNEL, MooDPlayApplication, OP_LIST

    class FakeClient:
        def __init__(self, cid):
            self.id = cid
            self.sent = []
        async def send(self, data, *, subchannel=0):
            self.sent.append((subchannel, data))

    app = MooDPlayApplication()
    app.runtime = object()
    client = FakeClient(7)
    asyncio.run(app.on_server_message(client, CONTROL_SUBCHANNEL, frame(OP_LIST, bytes(16))))
    assert client.sent == []
    assert client.id not in app.moodplay_clients


def test_hello_required_capabilities_are_accepted():
    import asyncio
    from mooapi.moodplay import CONTROL_SUBCHANNEL, MooDPlayApplication

    class FakeClient:
        def __init__(self, cid):
            self.id = cid
            self.sent = []
        async def send(self, data, *, subchannel=0):
            self.sent.append((subchannel, data))

    app = MooDPlayApplication()
    app.runtime = object()
    client = FakeClient(8)
    asyncio.run(app.on_server_message(client, CONTROL_SUBCHANNEL, frame(OP_HELLO, struct.pack("<I", CAPS))))
    assert client.id in app.moodplay_clients
    assert parse(client.sent[-1][1])[0] == OP_CAPS


def test_moodplay_control_channel_is_tcp_only():
    from mooapi.moodplay import CONTROL_SUBCHANNEL, MooDPlayApplication

    class FakeClient:
        def __init__(self, cid):
            self.id = cid

    app = MooDPlayApplication()
    app.runtime = object()
    client = FakeClient(9)
    result = app.on_udp_server_message(
        client, CONTROL_SUBCHANNEL, frame(OP_HELLO, struct.pack("<I", CAPS))
    )
    assert result is None
    assert client.id not in app.moodplay_clients

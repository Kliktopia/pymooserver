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


def test_moodplay_claim_list_auth_kick_and_host_migration_integration():
    import asyncio

    from mooapi.codec.packets import FromChannel, Hello, Join, Leave, ToServer
    from mooapi.config import ServerConfig
    from mooapi.moodplay import (
        CONTROL_SUBCHANNEL,
        MooDPlayApplication,
        OP_AUTH_RESULT,
        OP_CLAIM,
        OP_JOIN_AUTH,
        OP_KICK,
        OP_LIST,
        OP_LIST_REPLY,
    )
    from mooapi.server import MooServer

    async def scenario():
        app = MooDPlayApplication()
        runtime = MooServer(app=app, config=ServerConfig(ini_enabled=False), enable_udp=False)
        assert app.runtime is runtime
        sent = []

        async def capture(connection_id, packet):
            sent.append((connection_id, packet))
            return True

        runtime.tcp.send_packet = capture

        async def connect(name):
            result = runtime.hub.connect("127.0.0.1")
            cid = result.connection_id
            assert cid is not None
            await runtime._handle_effects(result.effects)
            await runtime._handle_effects(runtime.hub.receive(cid, Hello(3, name)))
            await runtime._handle_effects(
                runtime.hub.receive(cid, ToServer(CONTROL_SUBCHANNEL, frame(OP_HELLO)))
            )
            assert cid in app.moodplay_clients
            return cid

        async def control(cid, op, payload=b""):
            await runtime._handle_effects(
                runtime.hub.receive(cid, ToServer(CONTROL_SUBCHANNEL, frame(op, payload)))
            )

        guid = bytes(range(16))
        other_guid = bytes(reversed(range(16)))
        room = wire_session_name(guid, b"Arena")

        host = await connect(b"Host")
        await runtime._handle_effects(runtime.hub.receive(host, Join(room)))
        sid = runtime.hub.connections[host].session_ids[0]
        claim = struct.pack("<IBI", sid, 1, 2) + guid + b"secret"
        await control(host, OP_CLAIM, claim)
        assert app.sessions[sid].host_id == host
        assert app.sessions[sid].visible_name == b"Arena"

        guest = await connect(b"Guest")
        sent.clear()
        await control(guest, OP_LIST, guid)
        replies = [
            packet for cid, packet in sent
            if cid == guest and isinstance(packet, FromChannel) and packet.subchannel == CONTROL_SUBCHANNEL
        ]
        assert replies
        op, payload = parse(replies[-1].data)
        assert op == OP_LIST_REPLY
        assert struct.unpack_from("<H", payload, 0)[0] == 1
        listed_sid = struct.unpack_from("<I", payload, 2)[0]
        assert listed_sid == sid
        assert payload.endswith(b"Arena")

        sent.clear()
        await control(guest, OP_LIST, other_guid)
        reply = [packet for cid, packet in sent if cid == guest and isinstance(packet, FromChannel)][-1]
        op, payload = parse(reply.data)
        assert op == OP_LIST_REPLY
        assert struct.unpack_from("<H", payload, 0)[0] == 0

        # A wrong password never authorizes the join.
        sent.clear()
        await control(guest, OP_JOIN_AUTH, struct.pack("<IH", sid, 5) + b"wrong")
        reply = [packet for cid, packet in sent if cid == guest and isinstance(packet, FromChannel)][-1]
        op, payload = parse(reply.data)
        assert op == OP_AUTH_RESULT
        assert struct.unpack_from("<IB", payload, 0) == (sid, 0)
        before_join_effects = len(sent)
        await runtime._handle_effects(runtime.hub.receive(guest, Join(room)))
        assert sid not in runtime.hub.connections[guest].session_ids
        # Admission is rejected before Welcome/Joined/Exists can be emitted.
        assert len(sent) == before_join_effects

        # Correct authentication permits the join and is consumed by it.
        await control(guest, OP_JOIN_AUTH, struct.pack("<IH", sid, 6) + b"secret")
        await runtime._handle_effects(runtime.hub.receive(guest, Join(room)))
        assert sid in runtime.hub.connections[guest].session_ids
        assert (guest, sid) not in app.authorized_joins

        # Host-authorized kick removes only the session membership.
        await control(host, OP_KICK, struct.pack("<II", sid, guest))
        assert guest in runtime.hub.connections
        assert sid not in runtime.hub.connections[guest].session_ids

        # Rejoin, then host departure migrates authority to the remaining member.
        await control(guest, OP_JOIN_AUTH, struct.pack("<IH", sid, 6) + b"secret")
        await runtime._handle_effects(runtime.hub.receive(guest, Join(room)))
        await runtime._handle_effects(runtime.hub.receive(host, Leave(sid)))
        assert app.sessions[sid].host_id == guest
        assert runtime.hub.sessions[sid].master_id == guest

    asyncio.run(scenario())


def test_moodplay_native_namespace_isolation_and_nonmigrating_host_destroys_session():
    import asyncio

    from mooapi.codec.packets import Hello, Join, Leave, ToServer
    from mooapi.config import ServerConfig
    from mooapi.moodplay import CONTROL_SUBCHANNEL, MooDPlayApplication, OP_CLAIM, OP_JOIN_AUTH
    from mooapi.server import MooServer

    async def scenario():
        app = MooDPlayApplication()
        runtime = MooServer(app=app, config=ServerConfig(ini_enabled=False), enable_udp=False)

        async def discard(_connection_id, _packet):
            return True
        runtime.tcp.send_packet = discard

        async def new_client(name, moodplay):
            result = runtime.hub.connect("127.0.0.1")
            cid = result.connection_id
            await runtime._handle_effects(result.effects)
            await runtime._handle_effects(runtime.hub.receive(cid, Hello(3, name)))
            if moodplay:
                await runtime._handle_effects(
                    runtime.hub.receive(cid, ToServer(CONTROL_SUBCHANNEL, frame(OP_HELLO)))
                )
            return cid

        guid = b"G" * 16
        room = wire_session_name(guid, b"Private")

        # A native MooAPI client that guesses the private namespace is signed off.
        native = await new_client(b"Native", False)
        await runtime._handle_effects(runtime.hub.receive(native, Join(room)))
        assert runtime.hub.connections[native].session_ids == []

        host = await new_client(b"Host", True)
        await runtime._handle_effects(runtime.hub.receive(host, Join(room)))
        sid = runtime.hub.connections[host].session_ids[0]
        await runtime._handle_effects(
            runtime.hub.receive(
                host,
                ToServer(CONTROL_SUBCHANNEL, frame(OP_CLAIM, struct.pack("<IBI", sid, 0, 0) + guid)),
            )
        )

        guest = await new_client(b"Guest", True)
        await runtime._handle_effects(
            runtime.hub.receive(
                guest,
                ToServer(CONTROL_SUBCHANNEL, frame(OP_JOIN_AUTH, struct.pack("<IH", sid, 0))),
            )
        )
        await runtime._handle_effects(runtime.hub.receive(guest, Join(room)))
        assert sid in runtime.hub.connections[guest].session_ids

        # host_type=0 means no host migration: leaving host destroys the session.
        await runtime._handle_effects(runtime.hub.receive(host, Leave(sid)))
        assert sid not in runtime.hub.sessions
        assert sid not in app.sessions
        assert sid not in runtime.hub.connections[guest].session_ids

    asyncio.run(scenario())


def test_moodplay_unclaimed_room_cannot_be_stolen_and_host_migration_follows_join_order():
    import asyncio

    from mooapi.codec.packets import Hello, Join, Leave, ToServer
    from mooapi.config import ServerConfig
    from mooapi.moodplay import CONTROL_SUBCHANNEL, MooDPlayApplication, OP_CLAIM
    from mooapi.server import MooServer

    async def scenario():
        app = MooDPlayApplication()
        runtime = MooServer(app=app, config=ServerConfig(ini_enabled=False), enable_udp=False)

        async def discard(_connection_id, _packet):
            return True
        runtime.tcp.send_packet = discard

        async def connect(name):
            result = runtime.hub.connect("127.0.0.1")
            cid = result.connection_id
            assert cid is not None
            await runtime._handle_effects(result.effects)
            await runtime._handle_effects(runtime.hub.receive(cid, Hello(3, name)))
            await runtime._handle_effects(
                runtime.hub.receive(cid, ToServer(CONTROL_SUBCHANNEL, frame(OP_HELLO)))
            )
            return cid

        guid = b"H" * 16
        room = wire_session_name(guid, b"Ordered")

        # Connect one client early but deliberately have it join last. This makes
        # numeric connection order differ from session membership order.
        late_low_id = await connect(b"LateLowId")
        host = await connect(b"Host")
        first_guest = await connect(b"FirstGuest")

        await runtime._handle_effects(runtime.hub.receive(host, Join(room)))
        sid = runtime.hub.connections[host].session_ids[0]

        # A second identified mooDPlay client may not remain in the unclaimed room
        # and therefore cannot steal CLAIM from the actual underlying master.
        await runtime._handle_effects(runtime.hub.receive(first_guest, Join(room)))
        assert sid not in runtime.hub.connections[first_guest].session_ids
        steal = struct.pack("<IBI", sid, 1, 0) + guid
        await runtime._handle_effects(
            runtime.hub.receive(
                first_guest,
                ToServer(CONTROL_SUBCHANNEL, frame(OP_CLAIM, steal)),
            )
        )
        assert sid not in app.sessions

        claim = struct.pack("<IBI", sid, 1, 0) + guid
        await runtime._handle_effects(
            runtime.hub.receive(host, ToServer(CONTROL_SUBCHANNEL, frame(OP_CLAIM, claim)))
        )
        assert app.sessions[sid].host_id == host

        # Join first_guest first, then the numerically smaller late_low_id.
        await runtime._handle_effects(runtime.hub.receive(first_guest, Join(room)))
        await runtime._handle_effects(runtime.hub.receive(late_low_id, Join(room)))
        assert runtime.hub.sessions[sid].member_ids == [host, first_guest, late_low_id]

        await runtime._handle_effects(runtime.hub.receive(host, Leave(sid)))
        assert runtime.hub.sessions[sid].master_id == first_guest
        assert app.sessions[sid].host_id == first_guest

    asyncio.run(scenario())


def test_moodplay_list_reply_respects_mooapi_message_cap():
    import asyncio

    from mooapi.codec.packets import FromChannel, Hello, Join, ToServer
    from mooapi.codec.primitives import CodecLimits
    from mooapi.config import ServerConfig
    from mooapi.moodplay import CONTROL_SUBCHANNEL, MooDPlayApplication, OP_CLAIM, OP_LIST
    from mooapi.server import MooServer

    async def scenario():
        limits = CodecLimits(max_message_bytes=80)
        app = MooDPlayApplication()
        runtime = MooServer(
            app=app,
            config=ServerConfig(codec=limits, ini_enabled=False),
            enable_udp=False,
        )
        sent = []

        async def capture(connection_id, packet):
            sent.append((connection_id, packet))
            return True
        runtime.tcp.send_packet = capture

        async def connect(name):
            result = runtime.hub.connect("127.0.0.1")
            cid = result.connection_id
            assert cid is not None
            await runtime._handle_effects(result.effects)
            await runtime._handle_effects(runtime.hub.receive(cid, Hello(3, name)))
            await runtime._handle_effects(
                runtime.hub.receive(cid, ToServer(CONTROL_SUBCHANNEL, frame(OP_HELLO)))
            )
            return cid

        guid = b"L" * 16
        # Create several separately claimed sessions with visible names large
        # enough that all of them cannot fit in an 80-byte control reply.
        for index in range(4):
            host = await connect(("H%d" % index).encode())
            room = wire_session_name(guid, ("Room-%d-XXXXXXXXXX" % index).encode())
            await runtime._handle_effects(runtime.hub.receive(host, Join(room)))
            sid = runtime.hub.connections[host].session_ids[0]
            claim = struct.pack("<IBI", sid, 1, 0) + guid
            await runtime._handle_effects(
                runtime.hub.receive(host, ToServer(CONTROL_SUBCHANNEL, frame(OP_CLAIM, claim)))
            )

        browser = await connect(b"Browser")
        sent.clear()
        await runtime._handle_effects(
            runtime.hub.receive(browser, ToServer(CONTROL_SUBCHANNEL, frame(OP_LIST, guid)))
        )
        reply = [
            packet for cid, packet in sent
            if cid == browser and isinstance(packet, FromChannel)
        ][-1]
        assert len(reply.data) <= limits.max_message_bytes
        op, payload = parse(reply.data)
        assert op == OP_LIST_REPLY
        count = struct.unpack_from("<H", payload, 0)[0]
        assert 0 < count < 4

    asyncio.run(scenario())


def test_moodplay_only_suppresses_udp_on_reserved_control_subchannel():
    from mooapi.moodplay import CONTROL_SUBCHANNEL, MooDPlayApplication

    class RecordingApp(MooDPlayApplication):
        def __init__(self):
            super().__init__()
            self.received = []

        def on_server_message(self, client, subchannel, data):
            self.received.append((client.id, subchannel, data))

    class FakeClient:
        id = 11

    app = RecordingApp()
    client = FakeClient()
    app.on_udp_server_message(client, 7, b"ordinary")
    app.on_udp_server_message(client, CONTROL_SUBCHANNEL, frame(OP_HELLO))
    assert app.received == [(11, 7, b"ordinary")]


def test_moodplay_rejects_unknown_host_type_and_respects_server_member_cap():
    import asyncio

    from mooapi.codec.packets import FromChannel, Hello, Join, ToServer
    from mooapi.config import ServerConfig
    from mooapi.moodplay import (
        CONTROL_SUBCHANNEL,
        MooDPlayApplication,
        OP_AUTH_RESULT,
        OP_CLAIM,
        OP_JOIN_AUTH,
        OP_LIST,
        OP_LIST_REPLY,
        parse,
    )
    from mooapi.server import MooServer

    async def scenario():
        app = MooDPlayApplication()
        runtime = MooServer(
            app=app,
            config=ServerConfig(ini_enabled=False, max_members_per_session=2),
            enable_udp=False,
        )
        sent = []

        async def capture(connection_id, packet):
            sent.append((connection_id, packet))
            return True

        runtime.tcp.send_packet = capture

        async def connect(name):
            result = runtime.hub.connect("127.0.0.1")
            cid = result.connection_id
            assert cid is not None
            await runtime._handle_effects(result.effects)
            await runtime._handle_effects(runtime.hub.receive(cid, Hello(3, name)))
            await runtime._handle_effects(
                runtime.hub.receive(cid, ToServer(CONTROL_SUBCHANNEL, frame(OP_HELLO)))
            )
            return cid

        guid = b"C" * 16
        room = wire_session_name(guid, b"Cap")
        host = await connect(b"Host")
        await runtime._handle_effects(runtime.hub.receive(host, Join(room)))
        sid = runtime.hub.connections[host].session_ids[0]

        # Unknown host-policy values are not silently reinterpreted as no-migration.
        invalid_claim = struct.pack("<IBI", sid, 2, 0) + guid
        await runtime._handle_effects(
            runtime.hub.receive(host, ToServer(CONTROL_SUBCHANNEL, frame(OP_CLAIM, invalid_claim)))
        )
        assert sid not in app.sessions

        valid_claim = struct.pack("<IBI", sid, 1, 0) + guid
        await runtime._handle_effects(
            runtime.hub.receive(host, ToServer(CONTROL_SUBCHANNEL, frame(OP_CLAIM, valid_claim)))
        )
        assert sid in app.sessions

        guest = await connect(b"Guest")
        await runtime._handle_effects(runtime.hub.receive(guest, Join(room)))
        assert runtime.hub.sessions[sid].member_ids == [host, guest]

        third = await connect(b"Third")
        sent.clear()
        await runtime._handle_effects(
            runtime.hub.receive(
                third,
                ToServer(CONTROL_SUBCHANNEL, frame(OP_JOIN_AUTH, struct.pack("<IH", sid, 0))),
            )
        )
        auth = [
            packet for cid, packet in sent
            if cid == third and isinstance(packet, FromChannel)
        ][-1]
        op, payload = parse(auth.data)
        assert op == OP_AUTH_RESULT
        assert payload[4] == 0

        sent.clear()
        await runtime._handle_effects(
            runtime.hub.receive(third, ToServer(CONTROL_SUBCHANNEL, frame(OP_LIST, guid)))
        )
        listing = [
            packet for cid, packet in sent
            if cid == third and isinstance(packet, FromChannel)
        ][-1]
        op, payload = parse(listing.data)
        assert op == OP_LIST_REPLY
        assert struct.unpack_from("<H", payload, 0)[0] == 1
        # count(2) + sid/current/max/flags/name-len -> flags is byte offset 14.
        flags = payload[14]
        assert not (flags & 0x02), "directory must not advertise a Hub-full room as joinable"

        await runtime._handle_effects(runtime.hub.receive(third, Join(room)))
        assert sid not in runtime.hub.connections[third].session_ids

    asyncio.run(scenario())

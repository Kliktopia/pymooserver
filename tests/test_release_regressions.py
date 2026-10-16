import asyncio
import signal
import struct

import pytest

import mooapi
import pymooserver
from mooapi.codec.dialect_a import decode_client_packet, decode_server_packet, encode_client_packet, encode_server_packet
from mooapi.codec.packets import Alias, Hello, Join
from mooapi.codec.primitives import CodecLimits, Decoded, Error
from mooapi.config import ServerConfig
from mooapi.effects import CloseConnection
from mooapi.hub import Hub
from mooapi.moogame.ini import FilesystemIniStore, normalize_filename
from pymooserver import cli
from pymooserver.moo12 import IniStore
from pymooserver.paths import default_backup_root, default_data_root, default_ini_root, default_log_file


def test_release_versions_are_consistent():
    assert pymooserver.__version__ == "0.1.3"
    assert mooapi.__version__ == "0.1.3"


def test_platform_default_paths():
    assert default_data_root(system="Windows") == r"C:\Moo\data"
    assert default_ini_root(system="Windows") == r"C:\Moo\data\ini"
    assert default_backup_root(system="Windows") == r"C:\Moo\data\backups"
    assert default_log_file(system="Windows") == r"C:\Moo\data\logs\moo-server.log"

    assert default_data_root(system="Linux", environ={}, home="/home/test") == "/home/test/.local/share/pymooserver"
    assert default_ini_root(system="Linux", environ={}, home="/home/test") == "/home/test/.local/share/pymooserver/ini"
    assert default_backup_root(system="Linux", environ={}, home="/home/test") == "/home/test/.local/share/pymooserver/backups"
    assert default_log_file(system="Linux", environ={}, home="/home/test") == "/home/test/.local/share/pymooserver/logs/moo-server.log"
    assert default_data_root(system="Linux", environ={"XDG_DATA_HOME": "/srv/data"}, home="/home/test") == "/srv/data/pymooserver"
    assert default_data_root(system="Linux", environ={"XDG_DATA_HOME": "~/data"}, home="/home/test") == "/home/test/data/pymooserver"
    assert default_data_root(system="Darwin", environ={}, home="/Users/test") == "/Users/test/Library/Application Support/PyMooServer"


def test_server_config_uses_platform_native_ini_default():
    assert ServerConfig(ini_enabled=False).ini_data_root == default_ini_root()


def test_profile_filename_normalization_blocks_windows_special_forms(tmp_path):
    assert normalize_filename(b"D:evil") == b"D_evil.imi"
    assert normalize_filename(b"x:stream") == b"x_stream.imi"
    assert normalize_filename(b"CON") == b"_CON.imi"
    assert normalize_filename(b"aux.txt") == b"_aux.imi"
    assert normalize_filename(b"CON.foo.ini") == b"_CON.foo.imi"

    legacy = IniStore(str(tmp_path / "legacy"))
    assert legacy.set("CON.ini", "g", "k", "v")
    assert (tmp_path / "legacy" / "_CON.imi").is_file()
    assert legacy.get("CON.ini", "g", "k", "") == "v"
    assert legacy.set("CON.foo.ini", "g", "k", "v2")
    assert (tmp_path / "legacy" / "_CON.foo.imi").is_file()

    later = FilesystemIniStore(str(tmp_path / "later"))
    assert later.set("default", b"x:stream", b"g", b"k", b"v")
    assert (tmp_path / "later" / "x_stream.imi").is_file()
    assert later.get("default", b"x:stream", b"g", b"k") == b"v"


def test_profile_structural_injection_is_rejected_in_both_stores(tmp_path):
    legacy = IniStore(str(tmp_path / "legacy"))
    assert not legacy.set("scores", "Top]", "Alice", "1")
    assert not legacy.set("scores", "Top", "Ali=ce", "1")
    assert not legacy.set("scores", "Top", "Alice", "1\rBob=999")

    later = FilesystemIniStore(str(tmp_path / "later"))
    assert not later.set("default", b"scores", b"Top]", b"Alice", b"1")
    assert not later.set("default", b"scores", b"Top", b"Ali=ce", b"1")
    assert not later.set("default", b"scores", b"Top", b"Alice", b"1\nBob=999")


def test_session_names_have_a_separate_larger_limit():
    limits = CodecLimits(max_name_bytes=64, max_session_name_bytes=255)
    encoded = encode_client_packet(Join(b"S" * 100), limits)
    result = decode_client_packet(encoded, limits)
    assert isinstance(result, Decoded)
    assert result.packet == Join(b"S" * 100)

    # Player/Hello names remain on the conservative ordinary-name limit.
    too_long_hello = b"\x0c" + struct.pack("<hI", 3, 100) + (b"N" * 100)
    assert isinstance(decode_client_packet(too_long_hello, limits), Error)


def test_dialect_a_alias_encoder_matches_decoder_and_documented_layout():
    packet = Alias(0x11223344, b"Bob")
    wire = encode_server_packet(packet)
    assert wire == b"\x0b" + struct.pack("<II", 0x11223344, 3) + b"Bob"
    result = decode_server_packet(wire)
    assert isinstance(result, Decoded)
    assert result.packet == packet
    assert result.consumed == len(wire)


def test_identical_repeated_hello_is_idempotent_but_conflicting_hello_closes():
    hub = Hub(ServerConfig(ini_enabled=False))
    cid = hub.connect("127.0.0.1").connection_id
    hub.receive(cid, Hello(3, b"Alice"))
    assert hub.receive(cid, Hello(3, b"Alice")) == ()
    effects = hub.receive(cid, Hello(3, b"Bob"))
    assert any(isinstance(effect, CloseConnection) for effect in effects)
    assert cid not in hub.connections


def test_sigterm_handler_requests_task_cancellation():
    class FakeTask:
        def __init__(self):
            self.cancelled = False
        def cancel(self):
            self.cancelled = True

    class FakeLoop:
        def __init__(self):
            self.callback = None
            self.removed = None
        def add_signal_handler(self, sig, callback):
            assert sig == signal.SIGTERM
            self.callback = callback
        def call_soon_threadsafe(self, callback):
            callback()
        def remove_signal_handler(self, sig):
            self.removed = sig
            return True

    loop = FakeLoop()
    task = FakeTask()
    restore, state = cli._install_sigterm_handler(loop, task)
    loop.callback()
    assert state["value"] is True
    assert task.cancelled is True
    restore()
    assert loop.removed == signal.SIGTERM


def test_destroy_session_cleans_defensive_stale_member_state():
    from mooapi.config import ServerConfig
    from mooapi.hub import Hub
    from mooapi.model import Session

    hub = Hub(ServerConfig(ini_enabled=False, require_hello=False))
    session = Session(123, b"stale")
    session.member_ids.append(999999)
    hub.sessions[session.id] = session
    hub._sessions_by_name[session.name] = session.id

    assert hub.destroy_session(session.id) == ()
    assert session.id not in hub.sessions
    assert session.name not in hub._sessions_by_name


def test_profile_store_rejects_key_names_that_serialize_as_non_keys(tmp_path):
    legacy = IniStore(str(tmp_path / "legacy"))
    assert not legacy.set("scores", "Top", ";comment", "1")
    assert not legacy.set("scores", "Top", "[sectionlike", "1")
    assert legacy.get("scores", "Top", ";comment", "missing") == "missing"

    later = FilesystemIniStore(str(tmp_path / "later"))
    assert not later.set("default", b"scores", b"Top", b";comment", b"1")
    assert not later.set("default", b"scores", b"Top", b"[sectionlike", b"1")
    assert later.get("default", b"scores", b"Top", b";comment") is None


def test_fixed_port_mooapi_server_recreates_ini_service_after_restart(tmp_path):
    import asyncio
    import socket

    from mooapi.config import ServerConfig
    from mooapi.server import MooServer

    async def scenario():
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()

        server = MooServer(
            config=ServerConfig(ini_data_root=str(tmp_path)),
            host="127.0.0.1",
            port=port,
            enable_udp=False,
        )
        await server.start()
        assert server.ini is not None
        await server.close()
        assert server.ini is None

        await server.start()
        try:
            assert server.ini is not None
            assert server.tcp.server is not None
        finally:
            await server.close()

    asyncio.run(scenario())


def test_run_unified_cancels_owned_tasks_when_one_background_task_fails(tmp_path, monkeypatch):
    import socket

    async def scenario():
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()

        watchdog_started = asyncio.Event()
        watchdog_stopped = asyncio.Event()

        async def fake_watchdog(*_args, **_kwargs):
            watchdog_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                watchdog_stopped.set()

        async def failing_backup(*_args, **_kwargs):
            await watchdog_started.wait()
            raise RuntimeError("synthetic backup failure")

        monkeypatch.setattr(cli, "DEFAULT_MOO12_PORTS", [])
        monkeypatch.setattr(cli, "_event_loop_watchdog", fake_watchdog)
        monkeypatch.setattr(cli, "_backup_monitor", failing_backup)
        args = cli.build_unified_parser().parse_args([
            "--host", "127.0.0.1",
            "--mooapi-port", str(port),
            "--no-mooapi-udp",
            "--no-status",
            "--no-file-log",
            "--ini-root", str(tmp_path / "ini"),
            "--backup-root", str(tmp_path / "backups"),
        ])

        with pytest.raises(RuntimeError, match="synthetic backup failure"):
            await cli.run_unified(args)
        assert watchdog_stopped.is_set()

    asyncio.run(scenario())


def test_backup_cycle_tracks_content_and_recovers_from_corrupt_latest(tmp_path):
    import json
    import zipfile

    ini_dir = tmp_path / "ini"
    backup_root = tmp_path / "backups"
    ini_dir.mkdir()
    (ini_dir / "alpha.imi").write_bytes(b"[g]\r\nk=1\r\n")
    # Backups intentionally include legacy .ini leftovers as well as canonical .imi.
    (ini_dir / "legacy.ini").write_bytes(b"[old]\r\nx=y\r\n")

    path, reason = cli._maybe_backup_realm(1200, ini_dir, backup_root, minimum_days=7)
    assert reason == "created"
    assert path is not None and path.is_file()

    digest, files = cli._ini_tree_digest(ini_dir)
    assert digest is not None
    assert {p.name for p in files} == {"alpha.imi", "legacy.ini"}
    assert cli._backup_manifest_digest(path) == digest
    with zipfile.ZipFile(path, "r") as archive:
        manifest = json.loads(archive.read(cli.BACKUP_MANIFEST).decode("utf-8"))
        assert manifest["files"] == ["alpha.imi", "legacy.ini"]
        assert archive.read("alpha.imi") == b"[g]\r\nk=1\r\n"
        assert archive.read("legacy.ini") == b"[old]\r\nx=y\r\n"

    unchanged, reason = cli._maybe_backup_realm(1200, ini_dir, backup_root, minimum_days=7)
    assert unchanged is None
    assert reason == "unchanged"

    (ini_dir / "alpha.imi").write_bytes(b"[g]\r\nk=2\r\n")
    deferred, reason = cli._maybe_backup_realm(1200, ini_dir, backup_root, minimum_days=7)
    assert deferred is None
    assert reason == "changed but not yet due"

    # A corrupt/missing-manifest latest backup must not suppress recovery merely
    # because its date is recent. The replacement should describe current bytes.
    path.write_bytes(b"not a zip")
    replacement, reason = cli._maybe_backup_realm(1200, ini_dir, backup_root, minimum_days=7)
    assert reason == "created"
    assert replacement == path
    current_digest, _ = cli._ini_tree_digest(ini_dir)
    assert cli._backup_manifest_digest(replacement) == current_digest
    with zipfile.ZipFile(replacement, "r") as archive:
        assert archive.read("alpha.imi") == b"[g]\r\nk=2\r\n"

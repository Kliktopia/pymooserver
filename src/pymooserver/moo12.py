"""MOO1/MOO2 server implementation used by PyMooServer.

The implementation preserves the wire behavior required by classic MOO clients while
adding bounded resource handling suitable for a public asyncio server.
"""

import argparse
import asyncio
import hashlib
import json
import logging
import logging.handlers
import os
import platform
import re
import socket
import sys
import tempfile
import time
import zipfile
from datetime import date, datetime, timedelta
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional, Tuple


LOG = logging.getLogger("moo12")
DEFAULT_PORT = 1200
DEFAULT_DATA_ROOT = r"C:\Moo\data\ini"
DEFAULT_INI_DIR = r"C:\Moo\data\ini\1200"
DEFAULT_SERVER_TEXT = "MOO compatibility server"
HISTORICAL_MOTD_FALLBACK = "MOO homepage: http://www.3ee.com/"
HISTORICAL_SERVER_FALLBACK = (
    "Welcome to the world of MOO, be sure to upgrade to the full version "
    "of Moo2 at http://www.3ee.com/"
)
MAX_ALIAS_BYTES = 40
MAX_CHANNEL_BYTES = 40

# MOS reads at most 0x400 bytes at a time and clears its work buffer when the
# accumulated chunk exceeds 0x3c0.  We impose the same practical record ceiling
# while using a safer stream parser.
LEGACY_RECORD_LIMIT = 0x3C0
READ_LIMIT = 64 * 1024

# Public-port transport hygiene. These signatures cannot be valid original MOS
# commands, so they can be rejected before the MOO1/MOO2 line parser sees them.
# The guard is deliberately conservative and does not attempt to replace MOS's
# own fixed-prefix command dispatcher.
_HTTP_PREFIXES = (
    b"GET ", b"POST ", b"HEAD ", b"OPTIONS ", b"CONNECT ",
    b"PUT ", b"DELETE ", b"PATCH ", b"TRACE ", b"PRI * HTTP/2.0",
)
_MOS_INITIAL_BYTES = frozenset(b"sgimcbpv")
DEFAULT_IDENTIFICATION_TIMEOUT = 10.0
DEFAULT_PREIDENTIFY_BUFFER = 64
TCP_READ_CHUNK = 4096
DEFAULT_LEGACY_UNSIGNED_TIMEOUT = 120.0
DEFAULT_LEGACY_WRITE_TIMEOUT = 5.0
DEFAULT_LEGACY_MAX_CONNECTIONS = 256
DEFAULT_LEGACY_MAX_CONNECTIONS_PER_IP = 32
DEFAULT_LEGACY_INI_MAX_FILES = 256
DEFAULT_LEGACY_INI_MAX_KEYS_PER_FILE = 4096
DEFAULT_LEGACY_INI_MAX_BYTES = 4 * 1024 * 1024
DEFAULT_CLOSE_TIMEOUT = 2.0
DEFAULT_LOG_FILE = r"C:\Moo\data\logs\moo-server.log"
DEFAULT_LOG_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_LOG_BACKUPS = 5
DEFAULT_WATCHDOG_INTERVAL = 5.0
DEFAULT_WATCHDOG_WARN_AFTER = 2.0


def _wire(text: str) -> bytes:
    return text.encode("latin-1", errors="replace")


def _line(text: str) -> bytes:
    return _wire(text + "\n")


async def _safe_close_writer(writer: asyncio.StreamWriter) -> None:
    """Close a writer without allowing Windows teardown to hang indefinitely."""
    try:
        writer.close()
    except (ConnectionError, OSError, RuntimeError):
        return

    try:
        await asyncio.wait_for(writer.wait_closed(), timeout=DEFAULT_CLOSE_TIMEOUT)
    except (asyncio.TimeoutError, ConnectionError, OSError, RuntimeError, asyncio.CancelledError):
        pass


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Atomically replace a profile file with *data* in the same directory.

    Writing directly to the live .imi path can truncate/corrupt it if the process,
    disk, or antivirus layer fails mid-write.  A flushed same-directory temporary
    file followed by os.replace() leaves either the old complete file or the new
    complete file visible.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fp:
            fd = -1
            fp.write(data)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp_path, path)
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass


def _enable_tcp_keepalive(writer: asyncio.StreamWriter) -> None:
    """Best-effort keepalive for long-lived MOO1/MOO2 connections, including Windows."""
    sock = writer.get_extra_info("socket")
    if sock is None:
        return
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        if hasattr(socket, "TCP_KEEPIDLE"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 60)
        if hasattr(socket, "TCP_KEEPINTVL"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 20)
        if hasattr(socket, "TCP_KEEPCNT"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
        if hasattr(socket, "SIO_KEEPALIVE_VALS") and hasattr(sock, "ioctl"):
            try:
                sock.ioctl(socket.SIO_KEEPALIVE_VALS, (1, 60_000, 20_000))
            except (OSError, AttributeError):
                pass
    except OSError:
        pass


def _classify_initial_bytes(data: bytes) -> Optional[str]:
    """Classify the beginning of a newly accepted connection.

    ``"mos"`` means the bytes can begin an original MOS command.  HTTP, TLS
    and SSH labels identify obvious foreign traffic.  ``"unknown"`` means the
    bytes cannot become one of those recognised prefaces or a MOS command.
    ``None`` means a known foreign signature is still incomplete, so the caller
    should wait for a few more bytes (subject to the identification timeout).

    This is transport hardening only. Once a stream is identified as MOS, the
    historical fixed-prefix dispatcher remains authoritative.
    """
    if not data:
        return None

    # Every legitimate command matching MOS starts with one of these
    # lower-case bytes: s/g/i/m/c/b/p/v. Identifying on the first byte lets old
    # clients continue to fragment commands arbitrarily after that point.
    if data[0] in _MOS_INITIAL_BYTES:
        return "mos"

    # TLS ClientHello / record prefix. A single 0x16 is ambiguous until the
    # record-version major byte arrives.
    if data[:1] == b"\x16":
        if len(data) < 2:
            return None
        return "tls" if data[1] == 0x03 else "unknown"

    # SSH banners are ASCII and may arrive fragmented.
    if b"SSH-".startswith(data):
        return None if len(data) < 4 else "ssh"
    if data.startswith(b"SSH-"):
        return "ssh"

    # HTTP/1 methods and the HTTP/2 clear-text connection preface.
    possible_http = False
    for prefix in _HTTP_PREFIXES:
        if prefix.startswith(data):
            possible_http = True
            continue
        if data.startswith(prefix):
            return "http"
    if possible_http:
        return None

    return "unknown"


def mos_work_buffer(raw: bytes) -> bytes:
    """Return the byte buffer MOS dispatches after seeing a wire LF.

    The receive scanner consumes the wire LF and the dispatcher appends its own
    LF before fixed-prefix recognition and strtok("|") tokenisation.  Keeping
    that synthetic LF is important: without a conventional trailing pipe, it
    becomes part of the final argument token.
    """
    if raw.endswith(b"\n"):
        raw = raw[:-1]
    return raw + b"\n"


def legacy_tokens(value):
    """Equivalent result of C strtok(value, "|") for one complete command.

    Consecutive/leading delimiters collapse, while CR/LF and spaces remain
    ordinary token bytes.  Both bytes and str are accepted for small tests and
    diagnostics; protocol handling uses bytes and decodes tokens with Latin-1.
    """
    delim = b"|" if isinstance(value, (bytes, bytearray)) else "|"
    empty = b"" if isinstance(value, (bytes, bytearray)) else ""
    return [part for part in value.split(delim) if part != empty]


def legacy_atol(value: str) -> int:
    """C-like decimal atol used by MOS: whitespace/sign, then decimal prefix."""
    match = re.match(r"^[\t\n\v\f\r ]*([+-]?\d+)", value)
    if not match:
        return 0
    try:
        return int(match.group(1), 10)
    except ValueError:
        return 0


def _bounded(value: str, limit: int) -> str:
    """Bound a MOO1/MOO2 text field to its protocol-compatible storage limit."""
    return value[:limit]


def _ssini_unframe(value: str) -> str:
    """MOS removes the final character of every SSINI token unconditionally."""
    return value[:-1] if value else ""


def _ssini_reply(value: str) -> bytes:
    # Exact MOO1/MOO2 framing includes one space before the final pipe.
    return _line("inistring|%s |" % value)


class JsonlTrace:
    def __init__(self, path: Optional[str]):
        self.path = Path(path) if path else None
        self._fp = None
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fp = self.path.open("a", encoding="utf-8")

    def write(self, direction: str, peer: str, data: bytes, note: str = "") -> None:
        if not self._fp:
            return
        item = {
            "ts": time.time(),
            "direction": direction,
            "peer": peer,
            "hex": data.hex(),
            "latin1": data.decode("latin-1", errors="replace"),
        }
        if note:
            item["note"] = note
        try:
            self._fp.write(json.dumps(item, ensure_ascii=True) + "\n")
            self._fp.flush()
        except OSError as exc:
            # Tracing is optional diagnostics. A full/unavailable trace disk must
            # not take the live MOO listener down with it. Disable trace after the
            # first write failure and leave an ordinary server-log diagnostic.
            LOG.error("disabling JSONL trace after write failure path=%s error=%r", self.path, exc)
            try:
                self._fp.close()
            except OSError:
                pass
            self._fp = None

    def close(self) -> None:
        if self._fp:
            try:
                self._fp.close()
            except OSError:
                pass
            self._fp = None


@dataclass(eq=False)
class Client:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    connection_number: int
    client_id: int = 0
    alias: str = ""
    channel: str = ""
    anchor_id: int = 0
    observed_version: Optional[int] = None  # diagnostics only; MOS does not route on it
    connected_at: float = field(default_factory=time.time)
    send_failed: bool = False

    # Compatibility alias for older versions of this reimplementation.  The
    # executable's field behaves as a channel/session anchor, not a clean
    # leader/master abstraction, so new code should use anchor_id.
    @property
    def master_id(self) -> int:
        return self.anchor_id

    @master_id.setter
    def master_id(self, value: int) -> None:
        self.anchor_id = value

    @property
    def signed_on(self) -> bool:
        return self.client_id > 0

    @property
    def peer_ip(self) -> str:
        peer = self.writer.get_extra_info("peername")
        return str(peer[0]) if peer else "0.0.0.0"

    @property
    def peer_label(self) -> str:
        peer = self.writer.get_extra_info("peername")
        return "%s:%s" % peer if peer else "unknown"

    async def send(self, payload: bytes) -> None:
        self.writer.write(payload)
        await self.writer.drain()


class IniStore:
    """Backing store for MOO2 server-side IMI operations.

    MOS passes the supplied filename to the Windows private-profile APIs. The
    replacement keeps that logical filename (sandboxed to this directory) and
    implements the profile behaviours that matter to original MOO2 data:

    * case-insensitive filename, section, and key lookup;
    * ANSI/8-bit file contents without UTF-8 conversion;
    * Windows-profile whitespace/quote behaviour on reads;
    * first matching section/key wins;
    * malformed/unknown lines remain untouched;
    * writes change only the targeted entry when possible.

    This deliberately does not use ``configparser``. Real MOS .IMI archives
    can contain constructs that configparser rejects (for example an empty key
    or ``[]`` section), and rewriting an old file through configparser would
    unnecessarily normalize its spelling, order, comments, and line endings.
    """

    _WS = b" \t\r\n\v\f"
    _PROFILE_VALUE_MAX = 1023  # MOS supplies a 1024-byte output buffer.

    def __init__(
        self,
        root: str,
        *,
        max_files: int = DEFAULT_LEGACY_INI_MAX_FILES,
        max_keys_per_file: int = DEFAULT_LEGACY_INI_MAX_KEYS_PER_FILE,
        max_bytes: int = DEFAULT_LEGACY_INI_MAX_BYTES,
    ):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_files = max(0, int(max_files))
        self.max_keys_per_file = max(0, int(max_keys_per_file))
        self.max_bytes = max(0, int(max_bytes))

    def _file_count(self) -> int:
        try:
            return sum(
                1 for path in self.root.iterdir()
                if path.is_file() and path.suffix.casefold() == ".imi"
            )
        except OSError:
            # With a configured cap, fail closed rather than allowing unbounded
            # file creation merely because the directory could not be enumerated.
            return self.max_files if self.max_files else 0

    def _key_count(self, lines: List[bytes]) -> int:
        return sum(1 for line in lines if self._line_key_value(line) is not None)

    def _path(self, filename: str) -> Path:
        raw_name = (filename or "moo").replace("\\", "/")
        name = raw_name.rsplit("/", 1)[-1] or "moo"

        # Server-side profile storage always uses a physical .imi file.
        # Whatever final extension the client supplies is replaced, rather
        # than preserved or appended (e.g. .ini/.exe/.dat -> .imi).
        dot = name.rfind(".")
        stem = name[:dot] if dot > 0 else name
        stem = stem[:106] or "moo"
        wanted = stem + ".imi"

        wanted_fold = wanted.casefold()
        try:
            for existing in self.root.iterdir():
                if existing.is_file() and existing.name.casefold() == wanted_fold:
                    return existing
        except OSError:
            pass
        return self.root / wanted

    @staticmethod
    def _to_bytes(value: str) -> bytes:
        return value.encode("latin-1", errors="replace")

    @staticmethod
    def _to_text(value: bytes) -> str:
        return value.decode("latin-1", errors="replace")

    @classmethod
    def _trim(cls, value: bytes) -> bytes:
        return value.strip(cls._WS)

    @staticmethod
    def _split_physical_lines(data: bytes) -> List[bytes]:
        """Split CR, LF and CRLF profile lines while preserving exact bytes."""
        out: List[bytes] = []
        pos = 0
        size = len(data)
        while pos < size:
            idx = pos
            while idx < size and data[idx] not in (0x0D, 0x0A):
                idx += 1
            if idx >= size:
                out.append(data[pos:])
                break
            end = idx + 1
            if data[idx] == 0x0D and end < size and data[end] == 0x0A:
                end += 1
            out.append(data[pos:end])
            pos = end
        return out

    @staticmethod
    def _content_eol(line: bytes) -> Tuple[bytes, bytes]:
        if line.endswith(b"\r\n"):
            return line[:-2], b"\r\n"
        if line.endswith(b"\n"):
            return line[:-1], b"\n"
        if line.endswith(b"\r"):
            return line[:-1], b"\r"
        return line, b""

    @classmethod
    def _line_section(cls, line: bytes) -> Optional[bytes]:
        content, _ = cls._content_eol(line)
        work = cls._trim(content)
        if not work.startswith(b"["):
            return None
        end = work.rfind(b"]")
        if end < 0:
            return None
        return work[1:end]

    @classmethod
    def _line_key_value(cls, line: bytes) -> Optional[Tuple[bytes, bytes]]:
        content, _ = cls._content_eol(line)
        work = cls._trim(content)
        if not work or work.startswith(b";") or work.startswith(b"["):
            return None
        eq = work.find(b"=")
        if eq < 0:
            return None
        key = work[:eq].rstrip(cls._WS)
        value = work[eq + 1:].lstrip(cls._WS)
        return key, value

    @classmethod
    def _query_name(cls, value: str) -> bytes:
        return cls._trim(cls._to_bytes(value))

    @staticmethod
    def _fold(value: bytes) -> bytes:
        return value.lower()

    @classmethod
    def _read_value(cls, raw_value: bytes) -> bytes:
        value = raw_value
        if len(value) >= 2 and value[:1] in (b"'", b'"') and value[-1:] == value[:1]:
            value = value[1:-1]
        return value[:cls._PROFILE_VALUE_MAX]

    def get(self, filename: str, section: str, key: str, default: str = "") -> str:
        path = self._path(filename)
        if not path.exists():
            return default

        wanted_section = self._query_name(section)
        wanted_key = self._query_name(key)

        wanted_section_fold = self._fold(wanted_section)
        wanted_key_fold = self._fold(wanted_key)
        current_section: Optional[bytes] = None
        in_first_match = False
        first_match_seen = False

        try:
            if self.max_bytes and path.stat().st_size > self.max_bytes:
                return default
            lines = self._split_physical_lines(path.read_bytes())
        except OSError:
            return default

        for line in lines:
            section_name = self._line_section(line)
            if section_name is not None:
                if in_first_match:
                    # Windows profile lookup stops at the first matching
                    # section; it does not continue into a later duplicate.
                    return default
                current_section = section_name
                if (not first_match_seen and
                        self._fold(section_name) == wanted_section_fold):
                    first_match_seen = True
                    in_first_match = True
                else:
                    in_first_match = False
                continue

            if current_section is None or not in_first_match:
                continue

            pair = self._line_key_value(line)
            if pair is None:
                continue
            found_key, found_value = pair
            if self._fold(found_key) == wanted_key_fold:
                return self._to_text(self._read_value(found_value))

        return default

    @classmethod
    def _preferred_eol(cls, data: bytes) -> bytes:
        first_lf = data.find(b"\n")
        if first_lf >= 0:
            if first_lf > 0 and data[first_lf - 1:first_lf] == b"\r":
                return b"\r\n"
            return b"\n"
        if b"\r" in data:
            return b"\r"
        return b"\r\n"

    @classmethod
    def _replace_value_line(cls, line: bytes, value: bytes) -> bytes:
        content, eol = cls._content_eol(line)
        eq = content.find(b"=")
        if eq < 0:
            return line

        after = eq + 1
        while after < len(content) and content[after:after + 1] in (b" ", b"\t"):
            after += 1
        prefix = content[:after]
        return prefix + value + eol

    @classmethod
    def _ensure_terminated(cls, line: bytes, eol: bytes) -> bytes:
        _, existing = cls._content_eol(line)
        return line if existing else line + eol

    def set(self, filename: str, section: str, key: str, value: str) -> bool:
        path = self._path(filename)
        existed = path.exists()
        if not existed and self.max_files and self._file_count() >= self.max_files:
            return False
        try:
            if existed and self.max_bytes and path.stat().st_size > self.max_bytes:
                return False
            data = path.read_bytes() if existed else b""
        except OSError:
            # Never turn a transient read failure on an existing profile into a
            # destructive rewrite of an apparently empty file.
            return False

        lines = self._split_physical_lines(data)
        eol = self._preferred_eol(data)

        sec_b = self._query_name(section)
        key_b = self._query_name(key)
        value_b = self._to_bytes(value).lstrip(self._WS)

        sec_fold = self._fold(sec_b)
        key_fold = self._fold(key_b)

        current_section: Optional[bytes] = None
        matching_section_index: Optional[int] = None
        next_section_index: Optional[int] = None
        key_index: Optional[int] = None

        for idx, line in enumerate(lines):
            section_name = self._line_section(line)
            if section_name is not None:
                if matching_section_index is not None and next_section_index is None:
                    next_section_index = idx
                    break
                current_section = section_name
                if matching_section_index is None and self._fold(section_name) == sec_fold:
                    matching_section_index = idx
                continue

            if matching_section_index is None or current_section is None:
                continue
            if self._fold(current_section) != sec_fold:
                continue

            pair = self._line_key_value(line)
            if pair is not None and self._fold(pair[0]) == key_fold:
                key_index = idx
                break

        if key_index is not None:
            lines[key_index] = self._replace_value_line(lines[key_index], value_b)
        elif self.max_keys_per_file and self._key_count(lines) >= self.max_keys_per_file:
            return False
        elif matching_section_index is not None:
            insert_at = next_section_index if next_section_index is not None else len(lines)
            if insert_at > 0:
                lines[insert_at - 1] = self._ensure_terminated(lines[insert_at - 1], eol)
            lines.insert(insert_at, key_b + b"=" + value_b + eol)
        else:
            if lines:
                lines[-1] = self._ensure_terminated(lines[-1], eol)
            lines.append(b"[" + sec_b + b"]" + eol)
            lines.append(key_b + b"=" + value_b + eol)

        output = b"".join(lines)
        if self.max_bytes and len(output) > self.max_bytes:
            return False
        if self.max_keys_per_file and self._key_count(self._split_physical_lines(output)) > self.max_keys_per_file:
            return False
        try:
            _atomic_write_bytes(path, output)
        except OSError:
            return False
        return True


class Moo12Server:
    """MOO1/MOO2 protocol server."""

    def __init__(
        self,
        server_text: Optional[str] = None,
        ini_dir: str = DEFAULT_INI_DIR,
        trace: Optional[JsonlTrace] = None,
        strict: bool = False,
        realm_port: int = DEFAULT_PORT,
        historical_fallbacks: bool = False,
        preface_guard: bool = True,
        identification_timeout: float = DEFAULT_IDENTIFICATION_TIMEOUT,
        preidentify_buffer: int = DEFAULT_PREIDENTIFY_BUFFER,
        unsigned_timeout: float = DEFAULT_LEGACY_UNSIGNED_TIMEOUT,
        write_timeout: float = DEFAULT_LEGACY_WRITE_TIMEOUT,
        max_connections: int = DEFAULT_LEGACY_MAX_CONNECTIONS,
        max_connections_per_ip: int = DEFAULT_LEGACY_MAX_CONNECTIONS_PER_IP,
        ini_max_files: int = DEFAULT_LEGACY_INI_MAX_FILES,
        ini_max_keys_per_file: int = DEFAULT_LEGACY_INI_MAX_KEYS_PER_FILE,
        ini_max_bytes: int = DEFAULT_LEGACY_INI_MAX_BYTES,
        state_changed=None,
        # Deprecated compatibility aliases from earlier revisions.  The MOO1/MOO2
        # protocol uses one configurable server-text value; if older callers supply
        # separate values, motd wins and a warning is logged when they differ.
        motd: Optional[str] = None,
        nag: Optional[str] = None,
    ):
        if server_text is None:
            if motd is not None:
                server_text = motd
                if nag is not None and nag != motd:
                    LOG.warning("separate motd/nag values are obsolete; using motd as shared server_text")
            elif nag is not None:
                server_text = nag
            else:
                server_text = DEFAULT_SERVER_TEXT
        self.server_text = server_text
        self.historical_fallbacks = historical_fallbacks
        self.realm_port = realm_port
        self.ini = IniStore(
            ini_dir,
            max_files=ini_max_files,
            max_keys_per_file=ini_max_keys_per_file,
            max_bytes=ini_max_bytes,
        )
        self.trace = trace or JsonlTrace(None)
        self.strict = strict  # non-MOS diagnostic option; false by default
        self.preface_guard = bool(preface_guard)
        self.identification_timeout = max(0.1, float(identification_timeout))
        self.preidentify_buffer = max(4, int(preidentify_buffer))
        self.unsigned_timeout = max(0.0, float(unsigned_timeout))
        self.write_timeout = max(0.1, float(write_timeout))
        self.max_connections = max(0, int(max_connections))
        self.max_connections_per_ip = max(0, int(max_connections_per_ip))
        self.state_changed = state_changed

        # Original MOS keeps one append-ordered linked list of physical
        # connections.  Order affects channel-master selection.
        self.clients: List[Client] = []
        self.next_connection = 1
        self._id_counter = 0
        self._state_lock = asyncio.Lock()

    def _notify_state_changed(self) -> None:
        callback = self.state_changed
        if callback is None:
            return
        try:
            callback()
        except Exception:
            LOG.exception("realm %d status callback failed", self.realm_port)

    # ------------------------------------------------------------------ wire

    @staticmethod
    def _legacy_ip_number(ip: str) -> int:
        """Signed decimal WinSock IPv4 s_addr value used in MOS presence lines."""
        try:
            raw = int.from_bytes(socket.inet_aton(ip), "little", signed=False)
        except OSError:
            return 0
        return raw - (1 << 32) if raw >= (1 << 31) else raw

    def _trace_peer(self, client: Client) -> str:
        return "tcp/%d %s" % (self.realm_port, client.peer_label)

    async def _send(self, client: Client, payload: bytes, note: str = "") -> None:
        # A disconnected client can remain in a broadcast snapshot briefly while
        # its own reader task processes EOF. Do not repeatedly write to a transport
        # that is already known dead/closing; asyncio otherwise emits noisy
        # ``socket.send() raised exception`` warnings after several such writes.
        if client.send_failed or client.writer.is_closing() or client.reader.at_eof():
            client.send_failed = True
            return
        self.trace.write("S>C", self._trace_peer(client), payload, note)
        try:
            client.writer.write(payload)
            await asyncio.wait_for(client.writer.drain(), timeout=self.write_timeout)
        except asyncio.TimeoutError:
            client.send_failed = True
            LOG.warning(
                "realm %d closing stalled writer %s after %.1fs",
                self.realm_port, client.peer_label, self.write_timeout,
            )
            await _safe_close_writer(client.writer)
        except (ConnectionError, OSError, RuntimeError) as exc:
            client.send_failed = True
            LOG.debug(
                "realm %d send failed peer=%s note=%s error=%s",
                self.realm_port, client.peer_label, note or "-", repr(exc),
            )
            await _safe_close_writer(client.writer)

    async def _broadcast(self, clients: Iterable[Client], payload: bytes, note: str = "") -> None:
        targets = list(clients)
        if targets:
            await asyncio.gather(
                *(self._send(target, payload, note) for target in targets),
                return_exceptions=True,
            )

    def _same_channel(self, channel: str) -> List[Client]:
        # MOS uses strcmp: channel identity is case-sensitive and unsigned
        # physical connections can participate in odd edge cases.
        return [client for client in self.clients if client.channel == channel]

    async def _broadcast_channel(self, channel: str, payload: bytes, note: str = "") -> None:
        await self._broadcast(self._same_channel(channel), payload, note)

    async def _broadcast_count(self) -> None:
        # This is physical TCP connection count, not sign-on count.
        payload = _line("count|%d|" % len(self.clients))
        await self._broadcast(self.clients, payload, "physical connection count")

    def _allocate_id(self) -> int:
        # MOS increments a 32-bit global, assigns it, then resets the global to
        # zero after an assigned value exceeds 100000. Thus 100001 is assigned,
        # followed by 1 on the next sign-on.
        self._id_counter += 1
        value = self._id_counter
        if value > 100000:
            self._id_counter = 0
        return value

    def _find_anchor(self, channel: str) -> int:
        # MOS scans the append-ordered physical connection list and returns the
        # primary ID of the first exact-channel record whose STORED anchor is
        # nonzero.  It does not require that record's primary ID to be nonzero.
        for client in self.clients:
            if client.channel == channel and client.anchor_id != 0:
                return client.client_id
        return 0

    def _repair_anchor(self, leaving_id: int, channel: str) -> int:
        # Pick the first exact-channel record whose NUMERIC primary ID differs
        # from leaving_id, even if that replacement ID is zero.  Propagate the
        # replacement to every exact-channel record.  If no such record exists,
        # MOS performs no write, so existing anchor fields remain unchanged.
        replacement = None
        for client in self.clients:
            if client.channel == channel and client.client_id != leaving_id:
                replacement = client.client_id
                break
        if replacement is None:
            return 0
        for client in self.clients:
            if client.channel == channel:
                client.anchor_id = replacement
        return replacement

    # Backwards-compatible internal aliases for older tests/integrations.
    def _find_master(self, channel: str) -> int:
        return self._find_anchor(channel)

    def _reassign_master(self, leaving_id: int, channel: str) -> int:
        return self._repair_anchor(leaving_id, channel)

    def _welcome(self, client: Client) -> bytes:
        return _line("welcome|%d|%s|%d" % (client.client_id, client.alias, client.anchor_id))

    def _signon_record(self, client: Client) -> bytes:
        return _line(
            "signon|%d|%s|%d|%d"
            % (
                client.client_id,
                client.alias,
                self._legacy_ip_number(client.peer_ip),
                client.anchor_id,
            )
        )

    def _signed_record(self, peer: Client, anchor_id: int) -> bytes:
        return _line(
            "signed|%d|%s|%d|%d"
            % (peer.client_id, peer.alias, self._legacy_ip_number(peer.peer_ip), anchor_id)
        )

    @staticmethod
    def _signoff_record(client_id: int, alias: str, anchor_id: int, trailing_pipe: bool = False) -> bytes:
        text = "signoff|%d|%s|%d" % (client_id, alias, anchor_id)
        if trailing_pipe:
            text += "|"
        return _line(text)

    async def _join_current_channel(self, client: Client, send_welcome: bool = True) -> None:
        anchor = self._find_anchor(client.channel)
        if anchor == 0:
            anchor = client.client_id
        client.anchor_id = anchor

        # Normal signon sends welcome -> signon broadcast -> signed snapshots.
        # setchannel/gotoempty construct a welcome internally in the original
        # executable but overwrite it before any send, so callers suppress it.
        if send_welcome:
            await self._send(client, self._welcome(client), "welcome")
        await self._broadcast_channel(client.channel, self._signon_record(client), "newcomer signon")

        for peer in list(self.clients):
            if peer.channel == client.channel and peer.client_id != client.client_id:
                await self._send(client, self._signed_record(peer, client.anchor_id), "existing peer")

    def _motd_text(self) -> str:
        if self.server_text != "":
            return self.server_text
        if self.historical_fallbacks:
            return HISTORICAL_MOTD_FALLBACK
        return ""

    def _server_query_text(self) -> str:
        if self.server_text != "":
            return self.server_text
        if self.historical_fallbacks:
            return HISTORICAL_SERVER_FALLBACK
        return ""

    @staticmethod
    def _gotoempty_suffix(index: int) -> int:
        """Signed 16-bit decimal suffix used by the original gotoempty loop."""
        raw = index & 0xFFFF
        return raw if raw < 0x8000 else raw - 0x10000

    # ----------------------------------------------------------- connection IO

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        _enable_tcp_keepalive(writer)
        peer = writer.get_extra_info("peername")
        peer_ip = str(peer[0]) if isinstance(peer, tuple) and peer else "0.0.0.0"
        peer_label = (f"{peer[0]}:{peer[1]}" if isinstance(peer, tuple) and len(peer) >= 2 else peer_ip)

        # Public-listener connection limits apply to physical TCP connections,
        # matching the realm's count semantics. They are checked atomically before
        # the connection enters the protocol-visible client list.
        reject_reason = None
        async with self._state_lock:
            total = len(self.clients)
            per_ip = sum(1 for existing in self.clients if existing.peer_ip == peer_ip)
            if self.max_connections and total >= self.max_connections:
                reject_reason = (
                    "total connection limit reached (%d/%d)"
                    % (total, self.max_connections)
                )
            elif self.max_connections_per_ip and per_ip >= self.max_connections_per_ip:
                reject_reason = (
                    "per-IP connection limit reached for %s (%d/%d)"
                    % (peer_ip, per_ip, self.max_connections_per_ip)
                )
            else:
                client = Client(reader, writer, self.next_connection)
                self.next_connection += 1
                self.clients.append(client)

        if reject_reason is not None:
            LOG.warning("realm %d rejecting %s: %s", self.realm_port, peer_label, reject_reason)
            await _safe_close_writer(writer)
            return

        self._notify_state_changed()
        LOG.info(
            "realm %d connect peer=%s conn=%d active=%d per_ip=%d",
            self.realm_port, client.peer_label, client.connection_number,
            len(self.clients), per_ip + 1,
        )
        disconnect_reason = "peer closed"

        async def process_record(raw: bytes) -> None:
            self.trace.write("C>S", self._trace_peer(client), raw)

            # MOO1/MOO2 records have a practical 0x3c0-byte work-buffer ceiling.
            # Overlong logical records are discarded deterministically.
            logical_len = len(raw) - (1 if raw.endswith(b"\n") else 0)
            if logical_len > LEGACY_RECORD_LIMIT:
                LOG.warning("discarding overlong MOS record from %s", client.peer_label)
                return
            await self.dispatch(client, raw)

        try:
            # Accept path matching MOS: count first (to everyone), then
            # exact ver probe and MOTD to the newly accepted socket. The modern
            # preface guard intentionally runs only after this historical greeting.
            await self._broadcast_count()
            await self._send(client, _line("ver|1|"), "MOS version probe")
            await self._send(client, _line("motd|%s|" % self._motd_text()), "message of the day")

            buffer = bytearray()
            identified = not self.preface_guard
            # A newly connected client may remain idle before signon. The
            # identification timeout starts only after the peer sends an ambiguous
            # foreign-protocol prefix (for example a fragmented HTTP method or a
            # lone TLS record byte).
            identify_deadline = None
            unsigned_deadline = (
                time.monotonic() + self.unsigned_timeout
                if self.unsigned_timeout > 0 else None
            )
            was_signed_on = client.signed_on

            while True:
                try:
                    now = time.monotonic()
                    signed_now = client.signed_on
                    if signed_now:
                        # A signed-on player is allowed to remain idle indefinitely.
                        unsigned_deadline = None
                    elif was_signed_on and self.unsigned_timeout > 0:
                        # Explicit signoff can be followed by a later signon on the
                        # same TCP connection. Give that new unsigned period a fresh
                        # grace window rather than reusing the original connect timer.
                        unsigned_deadline = now + self.unsigned_timeout
                    elif unsigned_deadline is None and self.unsigned_timeout > 0:
                        unsigned_deadline = now + self.unsigned_timeout
                    was_signed_on = signed_now

                    deadlines = []
                    if not signed_now and unsigned_deadline is not None:
                        deadlines.append(unsigned_deadline)
                    if not identified and identify_deadline is not None:
                        deadlines.append(identify_deadline)
                    if deadlines:
                        remaining = min(deadlines) - now
                        if remaining <= 0:
                            raise asyncio.TimeoutError
                        chunk = await asyncio.wait_for(reader.read(TCP_READ_CHUNK), remaining)
                    else:
                        chunk = await reader.read(TCP_READ_CHUNK)
                except asyncio.TimeoutError:
                    now = time.monotonic()
                    if (
                        not client.signed_on
                        and unsigned_deadline is not None
                        and now >= unsigned_deadline
                    ):
                        disconnect_reason = "unsigned idle timeout"
                        LOG.info(
                            "realm %d closing unsigned idle connection %s after %.0fs",
                            self.realm_port, client.peer_label, self.unsigned_timeout,
                        )
                        self.trace.write(
                            "NOTE", self._trace_peer(client), b"",
                            "unsigned connection timeout",
                        )
                    else:
                        disconnect_reason = "identification timeout"
                        self.trace.write(
                            "NOTE", self._trace_peer(client), b"",
                            "pre-MOS identification timeout",
                        )
                    break

                if not chunk:
                    disconnect_reason = "peer EOF"
                    # StreamReader.readline() (used by earlier revisions) would
                    # return a final unterminated record at EOF. Preserve that
                    # behavior for an already-identified MOS stream.
                    if identified and buffer:
                        await process_record(bytes(buffer))
                        buffer.clear()
                    break

                buffer.extend(chunk)

                if not identified:
                    if len(buffer) > self.preidentify_buffer:
                        classification = "unknown"
                    else:
                        classification = _classify_initial_bytes(bytes(buffer))

                    if classification is None:
                        if identify_deadline is None:
                            identify_deadline = time.monotonic() + self.identification_timeout
                        continue
                    if classification != "mos":
                        disconnect_reason = "non-MOS preface: %s" % classification
                        # Public Internet HTTP/TLS/SSH probes and other obvious
                        # non-MOS prefaces are ordinary port noise. Close quietly
                        # instead of feeding them into the MOO1/MOO2 dispatcher.
                        self.trace.write(
                            "NOTE", self._trace_peer(client), b"",
                            "non-MOS preface: %s" % classification,
                        )
                        LOG.debug(
                            "realm %d closing non-MOS %s connection from %s",
                            self.realm_port, classification, client.peer_label,
                        )
                        break
                    identified = True

                # Robust stream framing: retain incomplete records across TCP
                # reads, but otherwise preserve MOS's LF-delimited command model.
                while True:
                    lf = buffer.find(b"\n")
                    if lf < 0:
                        break
                    raw = bytes(buffer[:lf + 1])
                    del buffer[:lf + 1]
                    await process_record(raw)

                # Prevent a client that never sends LF from growing memory without
                # bound after it has been identified as MOS.
                if len(buffer) > READ_LIMIT:
                    disconnect_reason = "oversized unterminated input"
                    LOG.warning("closing MOS client with oversized unterminated input: %s", client.peer_label)
                    break

        except (ConnectionError, OSError, asyncio.IncompleteReadError) as exc:
            # Normal peer reset/abort/close path. Windows may report this as
            # ConnectionResetError or a plain OSError depending on timing.
            disconnect_reason = "%s: %s" % (type(exc).__name__, exc)
            LOG.debug(
                "realm %d socket ended peer=%s error=%s",
                self.realm_port, client.peer_label, repr(exc),
            )
        except Exception as exc:
            disconnect_reason = "unexpected %s: %s" % (type(exc).__name__, exc)
            LOG.exception(
                "realm %d unexpected client-loop failure peer=%s",
                self.realm_port, client.peer_label,
            )
        finally:
            age = max(0.0, time.time() - client.connected_at)
            final_id = client.client_id
            final_alias = client.alias
            final_channel = client.channel
            await self._physical_disconnect(client)
            await _safe_close_writer(writer)
            LOG.info(
                "realm %d disconnect peer=%s conn=%d id=%d alias=%r channel=%r "
                "age=%.1fs reason=%s active=%d",
                self.realm_port, client.peer_label, client.connection_number, final_id,
                final_alias, final_channel, age, disconnect_reason, len(self.clients),
            )

    async def _physical_disconnect(self, client: Client) -> None:
        if client not in self.clients:
            return

        old_id = client.client_id
        old_channel = client.channel

        # MOS runs anchor lookup/repair while the departing record is still in
        # the list, even when old_id is zero.  Only close-time signoff emission
        # is conditional on a nonzero primary ID.
        found_anchor = self._find_anchor(old_channel)
        if found_anchor == old_id:
            self._repair_anchor(old_id, old_channel)

        # Snapshot protocol-visible departure state after anchor repair, then
        # remove the connection from the active client list.
        departed_id = client.client_id
        departed_alias = client.alias
        departed_channel = client.channel
        departed_anchor = client.anchor_id
        self.clients.remove(client)

        if departed_id != 0:
            payload = self._signoff_record(
                departed_id, departed_alias, departed_anchor, trailing_pipe=True
            )
            await self._broadcast_channel(departed_channel, payload, "physical disconnect signoff")

        await self._broadcast_count()
        self._notify_state_changed()

    # --------------------------------------------------------------- dispatch

    async def dispatch(self, client: Client, raw: bytes) -> None:
        # The network reader already preserves fragmented TCP lines safely.  At
        # the parser boundary, however, reproduce MOS's command semantics: the
        # wire LF is consumed and one synthetic LF is appended before dispatch
        # and strtok("|") tokenisation.
        work = mos_work_buffer(raw)
        if work == b"\n":
            return
        if b"\x00" in work:
            # Historical C-string processing truncated at NUL.  Rejecting NUL is
            # deterministic and safe while retaining normal original-client
            # compatibility.
            self.trace.write("NOTE", self._trace_peer(client), raw, "NUL-bearing command ignored")
            return

        dispatch_table = (
            (b"signon", self._cmd_signon),
            (b"signoff", self._cmd_signoff),
            (b"getini", self._cmd_getini),
            (b"inistring", self._cmd_inistring),
            (b"setalias", self._cmd_setalias),
            (b"setchannel", self._cmd_setchannel),
            (b"m", self._cmd_m),
            (b"c", self._cmd_c),
            (b"b", self._cmd_b),
            (b"p", self._cmd_p),
            (b"ver", self._cmd_ver),
            (b"gotoempty", self._cmd_gotoempty),
        )
        for prefix, handler in dispatch_table:
            if work[: len(prefix)] == prefix:
                byte_tokens = legacy_tokens(work)
                tokens = [part.decode("latin-1") for part in byte_tokens]
                try:
                    await handler(client, tokens)
                except Exception:
                    # Malformed records with missing fields are logged and ignored;
                    # they do not alter connection state through partial parsing.
                    LOG.exception("malformed %s record from %s: %r", prefix.decode("ascii"), client.peer_label, work)
                    if self.strict:
                        await self._send(client, _line("server|Protocol error"), "diagnostic extension")
                return

        # Unknown commands are silently ignored by MOS.
        self.trace.write("NOTE", self._trace_peer(client), raw, "unknown command ignored")
        if self.strict:
            await self._send(client, _line("server|Unknown command"), "diagnostic extension")

    # ----------------------------------------------------------- session state

    async def _cmd_signon(self, client: Client, tokens: List[str]) -> None:
        # Signon is processed only while the primary ID is zero. The ID is
        # allocated first; omitted arguments leave the current alias/channel state
        # unchanged.
        if client.client_id != 0:
            return

        client.client_id = self._allocate_id()
        if len(tokens) >= 2:
            client.alias = _bounded(tokens[1], MAX_ALIAS_BYTES)
        if len(tokens) >= 3:
            client.channel = _bounded(tokens[2], MAX_CHANNEL_BYTES)
        await self._join_current_channel(client, send_welcome=True)
        LOG.info(
            "realm %d signon conn=%d id=%d alias=%r channel=%r anchor=%d",
            self.realm_port, client.connection_number, client.client_id,
            client.alias, client.channel, client.anchor_id,
        )
        self._notify_state_changed()

    async def _cmd_signoff(self, client: Client, tokens: List[str]) -> None:
        if client.client_id <= 0:
            return

        old_id = client.client_id
        found_anchor = self._find_anchor(client.channel)
        if found_anchor == old_id:
            self._repair_anchor(old_id, client.channel)

        # Build after anchor repair but before zeroing ID. Explicit signoff
        # retains alias/channel/anchor and broadcasts to the exact channel,
        # including the initiating socket.
        payload = self._signoff_record(old_id, client.alias, client.anchor_id)
        client.client_id = 0
        await self._broadcast_channel(client.channel, payload, "explicit signoff")
        LOG.info(
            "realm %d signoff conn=%d old_id=%d alias=%r channel=%r",
            self.realm_port, client.connection_number, old_id, client.alias, client.channel,
        )
        self._notify_state_changed()

    async def _cmd_setalias(self, client: Client, tokens: List[str]) -> None:
        # There is no registration guard. If the alias token is absent, the
        # current alias is retained but the alias event is still emitted.
        old_alias = client.alias
        if len(tokens) >= 2:
            client.alias = _bounded(tokens[1], MAX_ALIAS_BYTES)
        payload = _line("alias|%d|%s" % (client.client_id, client.alias))
        await self._broadcast_channel(client.channel, payload, "alias changed")
        LOG.info(
            "realm %d alias id=%d old=%r new=%r channel=%r",
            self.realm_port, client.client_id, old_alias, client.alias, client.channel,
        )
        self._notify_state_changed()

    async def _cmd_setchannel(self, client: Client, tokens: List[str]) -> None:
        # No registration-state gate.  This is always a leave/rejoin operation,
        # even when no new channel token exists and the old channel is kept.
        old_channel = client.channel
        found_anchor = self._find_anchor(old_channel)
        if found_anchor == client.client_id:
            self._repair_anchor(client.client_id, old_channel)

        # The old-channel signoff is suppressed for primary ID zero.
        if client.client_id > 0:
            payload = self._signoff_record(client.client_id, client.alias, client.anchor_id)
            await self._broadcast_channel(old_channel, payload, "setchannel leave")

        if len(tokens) >= 2:
            client.channel = _bounded(tokens[1], MAX_CHANNEL_BYTES)

        # MOS builds a welcome here but overwrites it before sending.
        await self._join_current_channel(client, send_welcome=False)
        LOG.info(
            "realm %d channel id=%d old=%r new=%r anchor=%d",
            self.realm_port, client.client_id, old_channel, client.channel, client.anchor_id,
        )
        self._notify_state_changed()

    async def _cmd_gotoempty(self, client: Client, tokens: List[str]) -> None:
        if client.client_id <= 0 or len(tokens) < 2:
            # A missing base channel does not provide enough information to choose
            # a destination, so the command is ignored.
            return

        root = _bounded(tokens[1], MAX_CHANNEL_BYTES)
        suffix_chars = MAX_CHANNEL_BYTES - len(root)
        if suffix_chars <= 0:
            LOG.debug(
                "realm %d gotoempty rejected id=%d root_len=%d reason=no representable suffixed channel",
                self.realm_port, client.client_id, len(root),
            )
            return

        # Preserve signed-16-bit candidate order while skipping suffix values whose
        # decimal spelling cannot fit. A 39-byte root, for example, can only use
        # one-digit suffixes.
        positive_max = min(32767, (10 ** suffix_chars) - 1)
        negative_abs_max = (
            min(32768, (10 ** (suffix_chars - 1)) - 1)
            if suffix_chars >= 2 else 0
        )
        occupied_channels = {
            peer.channel for peer in self.clients
            if peer.client_id != client.client_id
        }
        candidate = None
        for suffix in range(0, positive_max + 1):
            test = "%s%d" % (root, suffix)
            if test not in occupied_channels:
                candidate = test
                break
        if candidate is None and negative_abs_max:
            for suffix in range(-negative_abs_max, 0):
                test = "%s%d" % (root, suffix)
                if test not in occupied_channels:
                    candidate = test
                    break
        if candidate is None:
            # Original code can loop forever after a complete signed-16-bit
            # suffix cycle; the replacement fails safely instead.
            return

        old_channel = client.channel
        found_anchor = self._find_anchor(old_channel)
        if found_anchor == client.client_id:
            self._repair_anchor(client.client_id, old_channel)

        await self._broadcast_channel(
            old_channel,
            self._signoff_record(client.client_id, client.alias, client.anchor_id),
            "gotoempty leave",
        )

        # Wire compatibility detail: the session record has no LF, so the following
        # signon record may immediately abut it in the TCP byte stream.
        await self._send(client, _wire("session|%s|" % candidate), "gotoempty session (no LF)")
        client.channel = candidate

        # As with setchannel, the internally built welcome is never transmitted.
        await self._join_current_channel(client, send_welcome=False)
        LOG.info(
            "realm %d gotoempty id=%d old=%r new=%r anchor=%d",
            self.realm_port, client.client_id, old_channel, client.channel, client.anchor_id,
        )
        self._notify_state_changed()

    # --------------------------------------------------------------- messages

    def _message(self, prefix: str, client: Client, fields: List[str]) -> bytes:
        return _line(
            "%s|%s|%d|%s|%s"
            % (prefix, fields[0], client.client_id, client.alias, "|".join(fields[1:]))
        )

    async def _cmd_m(self, client: Client, tokens: List[str]) -> None:
        # No sign-on guard. strtok means text after another '|' is not part of
        # the message; only the first payload token is used.
        if len(tokens) < 3:
            return
        subchannel = tokens[1]
        message = tokens[2]
        payload = _line("m|%s|%d|%s|%s" % (subchannel, client.client_id, client.alias, message))
        await self._broadcast_channel(client.channel, payload, "channel message")

    async def _cmd_c(self, client: Client, tokens: List[str]) -> None:
        # Manual coordinate packet, distinct from MOO2's extension-side UDP
        # autotracking mechanism.
        if len(tokens) < 4:
            return
        payload = _line(
            "c|%s|%d|%s|%s|%s"
            % (tokens[1], client.client_id, client.alias, tokens[2], tokens[3])
        )
        await self._broadcast_channel(client.channel, payload, "coordinate message")

    async def _cmd_b(self, client: Client, tokens: List[str]) -> None:
        if len(tokens) < 3:
            return
        root = tokens[1]
        message = tokens[2]
        payload = _line("m|0|%d|%s|%s" % (client.client_id, client.alias, message))
        # MOS uses case-sensitive strncmp(channel, root, strlen(root)).
        targets = [peer for peer in self.clients if peer.channel.startswith(root)]
        await self._broadcast(targets, payload, "root-prefix broadcast")

    async def _cmd_p(self, client: Client, tokens: List[str]) -> None:
        if len(tokens) < 3:
            return
        target_id = legacy_atol(tokens[1])
        message = tokens[2]
        payload = _line("m|0|%d|%s|%s" % (client.client_id, client.alias, message))
        # No channel restriction; iteration continues through the whole list.
        targets = [peer for peer in self.clients if peer.client_id == target_id]
        await self._broadcast(targets, payload, "private message")

    # ------------------------------------------------------- server-side INI

    async def _cmd_getini(self, client: Client, tokens: List[str]) -> None:
        if client.client_id <= 0 or len(tokens) < 4:
            return
        filename = _ssini_unframe(tokens[1])
        group = _ssini_unframe(tokens[2])
        item = _ssini_unframe(tokens[3])
        value = self.ini.get(filename, group, item, "")
        LOG.debug(
            "realm %d SSINI read id=%d logical=%r physical=%s section=%r key=%r reply_len=%d",
            self.realm_port, client.client_id, filename, self.ini._path(filename),
            group, item, len(value),
        )
        await self._send(client, _ssini_reply(value), "SSINI read")

    async def _cmd_inistring(self, client: Client, tokens: List[str]) -> None:
        if client.client_id <= 0 or len(tokens) < 5:
            return
        filename = _ssini_unframe(tokens[1])
        group = _ssini_unframe(tokens[2])
        item = _ssini_unframe(tokens[3])
        value = _ssini_unframe(tokens[4])
        stored = self.ini.set(filename, group, item, value)
        if not stored:
            LOG.warning(
                "realm %d SSINI write rejected id=%d logical=%r physical=%s section=%r key=%r value_len=%d",
                self.realm_port, client.client_id, filename, self.ini._path(filename),
                group, item, len(value),
            )
            return
        LOG.info(
            "realm %d SSINI write id=%d logical=%r physical=%s section=%r key=%r value_len=%d",
            self.realm_port, client.client_id, filename, self.ini._path(filename),
            group, item, len(value),
        )
        # The MOO1/MOO2 protocol sends no acknowledgement for a successful write.

    # ------------------------------------------------------------- versioning

    async def _cmd_ver(self, client: Client, tokens: List[str]) -> None:
        selector = legacy_atol(tokens[1]) if len(tokens) >= 2 else 0
        client.observed_version = selector  # diagnostics only; not routing state
        if selector == 1:
            # The server-text response contains exactly one LF on the wire.
            await self._send(
                client,
                _line("server|%s" % self._server_query_text()),
                "server text query",
            )

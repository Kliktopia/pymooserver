"""MooGame-compatible server-side INI service."""

from __future__ import annotations

from dataclasses import dataclass
import os
import re
import struct
import tempfile
from pathlib import Path
from typing import Protocol


class IniError(ValueError):
    pass


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
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


@dataclass(frozen=True, slots=True)
class SetValue:
    filename: bytes
    group: bytes
    item: bytes
    value: int


@dataclass(frozen=True, slots=True)
class SetString:
    filename: bytes
    group: bytes
    item: bytes
    value: bytes


@dataclass(frozen=True, slots=True)
class GetValue:
    filename: bytes
    group: bytes
    item: bytes


@dataclass(frozen=True, slots=True)
class GetString:
    filename: bytes
    group: bytes
    item: bytes


IniRequest = SetValue | SetString | GetValue | GetString


def _read_i16_blob(
    data: bytes, offset: int, label: str, *, allow_empty: bool = False
) -> tuple[bytes, int]:
    if offset + 2 > len(data):
        raise IniError(f"truncated {label} length")
    length = struct.unpack_from("<h", data, offset)[0]
    offset += 2
    if length < 0:
        raise IniError(f"negative {label} length")
    minimum = 0 if allow_empty else 1
    if not minimum <= length <= 128:
        raise IniError(f"{label} length outside {minimum}..128")
    end = offset + length
    if end > len(data):
        raise IniError(f"truncated {label}")
    value = data[offset:end]
    if b"\x00" in value:
        raise IniError(f"{label} contains NUL")
    return value, end


def parse_request(data: bytes) -> IniRequest:
    if not data or data[0] not in range(0x0B, 0x0F):
        raise IniError("not an INI request")
    op = data[0]
    offset = 1
    filename, offset = _read_i16_blob(data, offset, "filename")
    # Compatibility tolerance: Windows profile files used by MOO1/2 can contain
    # an empty section name (``[]``) and an empty key (``=value``).  Accept zero-
    # length group/item names so the later binary MooGame service can address the
    # same physical profile constructs.  The filename remains non-empty.
    group, offset = _read_i16_blob(data, offset, "group", allow_empty=True)
    item, offset = _read_i16_blob(data, offset, "item", allow_empty=True)

    if op == 0x0B:
        if offset + 4 != len(data):
            raise IniError("SetValue requires exactly one i32 value")
        value = struct.unpack_from("<i", data, offset)[0]
        return SetValue(filename, group, item, value)
    if op == 0x0C:
        if offset + 2 > len(data):
            raise IniError("truncated SetString length")
        declared = struct.unpack_from("<h", data, offset)[0]
        offset += 2
        if declared < 0:
            raise IniError("negative SetString length")
        remaining = len(data) - offset
        if declared > remaining:
            raise IniError("SetString declared length exceeds remaining payload")
        # the host ignores the declared string length and stores all
        # remaining bytes.  We validate the field but preserve that behavior.
        value = data[offset:]
        if len(value) > 4096:
            raise IniError("SetString value exceeds 4096-byte cap")
        return SetString(filename, group, item, value)
    if offset != len(data):
        raise IniError("Get request has trailing bytes")
    if op == 0x0D:
        return GetValue(filename, group, item)
    return GetString(filename, group, item)


def encode_get_value_reply(value: int) -> bytes:
    if not -(2**31) <= value <= 2**31 - 1:
        raise ValueError("INI reply value is outside i32")
    return b"\x0F" + struct.pack("<i", value)


def encode_get_string_reply(value: bytes) -> bytes:
    return b"\x10" + value


def _ascii_fold(value: bytes) -> bytes:
    return bytes((c + 32 if 65 <= c <= 90 else c) for c in value)


def normalize_filename(value: bytes, *, append_imi: bool = True) -> bytes:
    # supported 1.20/1.23-era hosts use a 0x60 (96-byte) filename bound.
    # Directory components are not part of the compatibility namespace, so strip
    # them before applying the bound; otherwise a long client path can consume the
    # whole limit and collapse an ordinary basename to just ``.imi``.
    raw = value.replace(b"\\", b"/")
    name = raw.rsplit(b"/", 1)[-1] or b"moo"
    if not append_imi:
        return name[:96]
    dot = name.rfind(b".")
    stem = name[:dot] if dot > 0 else name
    stem = stem or b"moo"
    return stem[:92] + b".imi"


class IniStore(Protocol):
    def get(self, namespace: str, filename: bytes, group: bytes, item: bytes) -> bytes | None: ...
    def set(self, namespace: str, filename: bytes, group: bytes, item: bytes, value: bytes) -> bool: ...
    def close(self) -> None: ...


class FilesystemIniStore:
    """Preservation-oriented .IMI/.INI store shared with the MOO1/2 layout.

    The MOO1/MOO2 server keeps server-side profile files under a
    per-listening-port ``<ini-root>/<port>`` directory by default.  This
    store provides the same physical-file behavior for the later MooGame INI
    service while leaving its binary request/reply protocol unchanged.

    Profile lookup is case-insensitive for filename, section and key.  Existing
    spelling, comments, ordering and line endings are preserved where possible.
    Paths supplied by clients are reduced to a basename so profile access cannot
    escape the configured compatibility directory.
    """

    _WS = b" \t\r\n\v\f"

    def __init__(self, root: str, **limits) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_files = max(0, int(limits.get("max_files", 0)))
        self.max_keys_per_file = max(0, int(limits.get("max_keys_per_file", 0)))
        self.max_bytes = max(0, int(limits.get("max_bytes", 0)))

    def _file_count(self) -> int:
        try:
            return sum(
                1 for path in self.root.iterdir()
                if path.is_file() and path.suffix.casefold() == ".imi"
            )
        except OSError:
            return self.max_files if self.max_files else 0

    def _key_count(self, lines: list[bytes]) -> int:
        return sum(1 for line in lines if self._key_value(line) is not None)

    @staticmethod
    def _split_lines(data: bytes) -> list[bytes]:
        return data.splitlines(keepends=True)

    @staticmethod
    def _content_eol(line: bytes) -> tuple[bytes, bytes]:
        if line.endswith(b"\r\n"):
            return line[:-2], b"\r\n"
        if line.endswith(b"\n"):
            return line[:-1], b"\n"
        if line.endswith(b"\r"):
            return line[:-1], b"\r"
        return line, b""

    @classmethod
    def _trim(cls, value: bytes) -> bytes:
        return value.strip(cls._WS)

    @classmethod
    def _section(cls, line: bytes) -> bytes | None:
        content, _ = cls._content_eol(line)
        work = cls._trim(content)
        if not work.startswith(b"["):
            return None
        end = work.rfind(b"]")
        return None if end < 0 else work[1:end]

    @classmethod
    def _key_value(cls, line: bytes) -> tuple[bytes, bytes] | None:
        content, _ = cls._content_eol(line)
        work = cls._trim(content)
        if not work or work.startswith((b";", b"[")):
            return None
        eq = work.find(b"=")
        if eq < 0:
            return None
        return work[:eq].rstrip(cls._WS), work[eq + 1:].lstrip(cls._WS)

    @staticmethod
    def _preferred_eol(data: bytes) -> bytes:
        lf = data.find(b"\n")
        if lf >= 0:
            return b"\r\n" if lf > 0 and data[lf - 1:lf] == b"\r" else b"\n"
        return b"\r" if b"\r" in data else b"\r\n"

    @classmethod
    def _ensure_eol(cls, line: bytes, eol: bytes) -> bytes:
        _, existing = cls._content_eol(line)
        return line if existing else line + eol

    def _path(self, filename: bytes) -> Path:
        # Moo profile names are 8-bit strings.  Ignore client directories and
        # force the physical backing filename to .imi even if a caller bypasses
        # normalize_filename() and supplies another extension.
        raw = filename.decode("latin-1", errors="replace").replace("\\", "/")
        name = raw.rsplit("/", 1)[-1] or "moo"
        dot = name.rfind(".")
        stem = name[:dot] if dot > 0 else name
        stem = stem[:92] or "moo"
        wanted = stem + ".imi"
        folded = wanted.casefold()
        try:
            for existing in self.root.iterdir():
                if existing.is_file() and existing.name.casefold() == folded:
                    return existing
        except OSError:
            pass
        return self.root / wanted

    def get(self, namespace, filename, group, item):
        del namespace  # directory isolation replaces the old logical namespace
        path = self._path(filename)
        if not path.exists():
            return None
        try:
            if self.max_bytes and path.stat().st_size > self.max_bytes:
                return None
            lines = self._split_lines(path.read_bytes())
        except OSError:
            return None
        wanted_group = _ascii_fold(self._trim(group))
        wanted_item = _ascii_fold(self._trim(item))
        in_group = False
        seen_group = False
        for line in lines:
            section = self._section(line)
            if section is not None:
                if in_group:
                    return None
                in_group = (not seen_group and _ascii_fold(section) == wanted_group)
                if in_group:
                    seen_group = True
                continue
            if not in_group:
                continue
            pair = self._key_value(line)
            if pair is not None and _ascii_fold(pair[0]) == wanted_item:
                value = pair[1]
                if len(value) >= 2 and value[:1] in (b"'", b'"') and value[-1:] == value[:1]:
                    value = value[1:-1]
                return value[:4096]
        return None

    def set(self, namespace, filename, group, item, value):
        del namespace
        path = self._path(filename)
        existed = path.exists()
        if not existed and self.max_files and self._file_count() >= self.max_files:
            return False
        try:
            if existed and self.max_bytes and path.stat().st_size > self.max_bytes:
                return False
            data = path.read_bytes() if existed else b""
        except OSError:
            return False
        lines = self._split_lines(data)
        eol = self._preferred_eol(data)
        wanted_group = self._trim(group)
        wanted_item = self._trim(item)
        group_fold = _ascii_fold(wanted_group)
        item_fold = _ascii_fold(wanted_item)
        section_idx = None
        next_section_idx = None
        key_idx = None
        current = None
        for idx, line in enumerate(lines):
            section = self._section(line)
            if section is not None:
                if section_idx is not None and next_section_idx is None:
                    next_section_idx = idx
                    break
                current = section
                if section_idx is None and _ascii_fold(section) == group_fold:
                    section_idx = idx
                continue
            if section_idx is None or current is None or _ascii_fold(current) != group_fold:
                continue
            pair = self._key_value(line)
            if pair is not None and _ascii_fold(pair[0]) == item_fold:
                key_idx = idx
                break
        if key_idx is not None:
            content, line_eol = self._content_eol(lines[key_idx])
            eq = content.find(b"=")
            pos = eq + 1
            while pos < len(content) and content[pos:pos + 1] in (b" ", b"\t"):
                pos += 1
            lines[key_idx] = content[:pos] + value + line_eol
        elif self.max_keys_per_file and self._key_count(lines) >= self.max_keys_per_file:
            return False
        elif section_idx is not None:
            insert_at = next_section_idx if next_section_idx is not None else len(lines)
            if insert_at > 0:
                lines[insert_at - 1] = self._ensure_eol(lines[insert_at - 1], eol)
            lines.insert(insert_at, wanted_item + b"=" + value + eol)
        else:
            if lines:
                lines[-1] = self._ensure_eol(lines[-1], eol)
            lines.extend((b"[" + wanted_group + b"]" + eol, wanted_item + b"=" + value + eol))
        output = b"".join(lines)
        if self.max_bytes and len(output) > self.max_bytes:
            return False
        if self.max_keys_per_file and self._key_count(self._split_lines(output)) > self.max_keys_per_file:
            return False
        try:
            _atomic_write_bytes(path, output)
        except OSError:
            return False
        return True

    def close(self) -> None:
        pass


_ATOI = re.compile(rb"^[\t\n\v\f\r ]*([+-]?\d+)")


def _atoi(value: bytes) -> int:
    match = _ATOI.match(value)
    if not match:
        return 0
    parsed = int(match.group(1))
    # keep the result representable in the i32 reply.
    parsed &= 0xFFFFFFFF
    return parsed - 0x100000000 if parsed & 0x80000000 else parsed


class IniService:
    def __init__(
        self,
        store: IniStore,
        *,
        namespace: str = "default",
        append_imi: bool = True,
    ) -> None:
        self.store = store
        self.namespace = namespace
        self.append_imi = append_imi

    def handle(self, data: bytes) -> tuple[bool, bytes | None]:
        """Return ``(consumed, reply_payload)``.

        A malformed 0x0B..0x0E request is consumed and dropped; non-INI data is not
        consumed and remains available to application callbacks.
        """
        if not data or data[0] not in range(0x0B, 0x0F):
            return False, None
        try:
            request = parse_request(data)
        except IniError:
            return True, None
        filename = normalize_filename(request.filename, append_imi=self.append_imi)
        if isinstance(request, SetValue):
            self.store.set(
                self.namespace, filename, request.group, request.item, str(request.value).encode("ascii")
            )
            return True, None
        if isinstance(request, SetString):
            self.store.set(self.namespace, filename, request.group, request.item, request.value)
            return True, None
        value = self.store.get(self.namespace, filename, request.group, request.item)
        if isinstance(request, GetValue):
            return True, encode_get_value_reply(_atoi(value or b""))
        return True, encode_get_string_reply(value or b"")

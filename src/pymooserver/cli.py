"""Unified PyMooServer launcher for MOO1/MOO2 and MooAPI realms."""

import argparse
import asyncio
import hashlib
import json
import logging
import logging.handlers
import os
import platform
import re
import sys
import tempfile
import time
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path

from .moo12 import (
    DEFAULT_IDENTIFICATION_TIMEOUT,
    DEFAULT_LEGACY_INI_MAX_BYTES,
    DEFAULT_LEGACY_INI_MAX_FILES,
    DEFAULT_LEGACY_INI_MAX_KEYS_PER_FILE,
    DEFAULT_LEGACY_MAX_CONNECTIONS,
    DEFAULT_LEGACY_MAX_CONNECTIONS_PER_IP,
    DEFAULT_LEGACY_UNSIGNED_TIMEOUT,
    DEFAULT_LEGACY_WRITE_TIMEOUT,
    DEFAULT_LOG_BACKUPS,
    DEFAULT_LOG_FILE,
    DEFAULT_LOG_MAX_BYTES,
    DEFAULT_PREIDENTIFY_BUFFER,
    DEFAULT_SERVER_TEXT,
    DEFAULT_WATCHDOG_INTERVAL,
    DEFAULT_WATCHDOG_WARN_AFTER,
    JsonlTrace,
    LOG,
    Moo12Server,
    READ_LIMIT,
)
from mooapi import MooServer, ServerConfig
from mooapi.app import MooApplication
from mooapi.moodplay import MooDPlayApplication

DEFAULT_MOO12_PORTS = (1200,)
DEFAULT_MOOAPI_PORTS = (1203, 3205)
DEFAULT_MOOAPI_UDP_OFFSET = 1
DEFAULT_UNIFIED_INI_ROOT = r'C:\Moo\data\ini'
DEFAULT_MOOAPI_MOTD = 'MooAPI Version 1.22'
DEFAULT_BACKUP_ROOT = r'C:\Moo\data\backups'
DEFAULT_BACKUP_DAYS = 7
DEFAULT_BACKUP_CHECK_INTERVAL = 60 * 60
STATUS_DEBOUNCE_SECONDS = 0.05
BACKUP_MANIFEST = '__moo_backup_manifest__.json'


def _unique_ports(values, defaults):
    chosen = list(values) if values else list(defaults)
    out = []
    seen = set()
    for value in chosen:
        if not (1 <= value <= 65535):
            raise ValueError('invalid TCP port: %s' % value)
        if value not in seen:
            out.append(value)
            seen.add(value)
    return out


def _realm_ini_dir(root, port, shared=False):
    root_path = Path(root)
    return root_path if shared else root_path / str(port)


def _display_text(value, limit=160) -> str:
    if isinstance(value, bytes):
        text = value.decode('latin-1', errors='replace')
    else:
        text = str(value or '')
    # Prevent client-controlled CR/LF/control bytes from forging log lines while
    # leaving ordinary printable Latin-1 names readable enough for diagnostics.
    safe = text.encode('unicode_escape', errors='backslashreplace').decode('ascii')
    return safe if len(safe) <= limit else safe[:limit] + '...'


def _display_bytes(value) -> str:
    return _display_text(value)


def _legacy_realm_status(port, impl):
    users = []
    for client in list(impl.clients):
        if client.client_id > 0:
            alias = _display_text(client.alias) or '<unnamed>'
            channel = _display_text(client.channel) or '<no-channel>'
            users.append('%s[id=%d, channel=%s]' % (alias, client.client_id, channel))
        else:
            users.append('<unsigned conn=%d>' % client.connection_number)
    return port, len(impl.clients), users


def _mooapi_realm_status(port, runtime):
    users = []
    hub = runtime.hub
    for connection_id in sorted(hub.connections):
        connection = hub.connections[connection_id]
        name = _display_bytes(connection.name) or '<hello-pending>'
        session_names = []
        for session_id in sorted(connection.session_ids)[:8]:
            session = hub.sessions.get(session_id)
            if session is not None:
                session_names.append(_display_bytes(session.name) or str(session_id))
        extra_sessions = max(0, len(connection.session_ids) - len(session_names))
        if extra_sessions:
            session_names.append('+%d more' % extra_sessions)
        suffix = ', sessions=%s' % ','.join(session_names) if session_names else ''
        users.append('%s[id=%d%s]' % (name, connection.id, suffix))
    return port, len(hub.connections), users


def _status_users_text(users, limit=50):
    shown = users[:limit]
    text = '' if not shown else ' | ' + '; '.join(shown)
    if len(users) > limit:
        text += '; ... +%d more' % (len(users) - limit)
    return text


def _log_status_snapshot(legacy_realms, mooapi_realms) -> None:
    LOG.info('----- current realm status -----')
    for port, impl in legacy_realms:
        _, count, users = _legacy_realm_status(port, impl)
        signed = sum(1 for client in impl.clients if client.client_id > 0)
        unsigned = count - signed
        LOG.info(
            'MOO1/2 TCP %d: %d connection(s) signed=%d unsigned=%d limit=%s per_ip_limit=%s%s',
            port, count, signed, unsigned,
            impl.max_connections or 'off', impl.max_connections_per_ip or 'off',
            _status_users_text(users),
        )
    for port, runtime in mooapi_realms:
        _, count, users = _mooapi_realm_status(port, runtime)
        udp = runtime.udp
        udp_endpoints = len(udp.endpoints) if udp is not None else 0
        udp_buckets = len(udp.rate_buckets) if udp is not None else 0
        udp_drops = sum(getattr(udp, 'drop_counts', {}).values()) if udp is not None else 0
        udp_tasks = len(getattr(udp, '_pending_tasks', ())) if udp is not None else 0
        queued = sum(peer.pending_bytes for peer in runtime.tcp.peers.values())
        LOG.info(
            'MooAPI TCP %d: %d connection(s) sessions=%d queued_bytes=%d '
            'udp_endpoints=%d udp_rate_buckets=%d udp_pending_tasks=%d udp_drops=%d%s',
            port, count, len(runtime.hub.sessions), queued, udp_endpoints, udp_buckets, udp_tasks, udp_drops,
            _status_users_text(users),
        )


class _StateStatusReporter:
    """Log realm state once at startup and thereafter only when it changes."""

    def __init__(self, legacy_realms, mooapi_realms, enabled=True):
        self.legacy_realms = legacy_realms
        self.mooapi_realms = mooapi_realms
        self.enabled = bool(enabled)
        self._last_signature = None
        self._pending = None

    def _signature(self):
        legacy = tuple(
            (port, count, tuple(users))
            for port, impl in self.legacy_realms
            for _, count, users in [_legacy_realm_status(port, impl)]
        )
        mooapi = tuple(
            (port, count, tuple(users))
            for port, runtime in self.mooapi_realms
            for _, count, users in [_mooapi_realm_status(port, runtime)]
        )
        return legacy, mooapi

    def log_initial(self) -> None:
        if not self.enabled:
            return
        self._last_signature = self._signature()
        _log_status_snapshot(self.legacy_realms, self.mooapi_realms)

    def changed(self) -> None:
        if not self.enabled:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._pending is not None:
            self._pending.cancel()
        self._pending = loop.call_later(STATUS_DEBOUNCE_SECONDS, self._flush)

    def _flush(self) -> None:
        self._pending = None
        signature = self._signature()
        if signature == self._last_signature:
            return
        self._last_signature = signature
        _log_status_snapshot(self.legacy_realms, self.mooapi_realms)

    def close(self) -> None:
        self.enabled = False
        if self._pending is not None:
            self._pending.cancel()
            self._pending = None


class _StatusApplication(MooApplication):
    """MooAPI application hook that reports state-changing events."""

    def __init__(self, reporter):
        self.reporter = reporter

    def _changed(self):
        self.reporter.changed()

    def on_connect(self, client):
        self._changed()

    def on_hello(self, client):
        self._changed()

    def on_join(self, client, session):
        self._changed()

    def on_leave(self, client, session):
        self._changed()

    def on_rename(self, client):
        self._changed()

    def on_disconnect(self, client):
        self._changed()


class _MooDPlayStatusApplication(MooDPlayApplication):
    """mooDPlay policy layered onto a normal MooAPI realm plus status reporting."""

    def __init__(self, reporter):
        super().__init__()
        self.reporter = reporter

    def _changed(self):
        self.reporter.changed()

    def on_connect(self, client):
        self._changed()

    def on_hello(self, client):
        self._changed()

    async def on_join(self, client, session):
        await super().on_join(client, session)
        self._changed()

    async def on_leave(self, client, session):
        await super().on_leave(client, session)
        self._changed()

    def on_rename(self, client):
        self._changed()

    def on_disconnect(self, client):
        super().on_disconnect(client)
        self._changed()


def _ini_files(directory: Path):
    if not directory.exists():
        return []
    return sorted(
        (
            path for path in directory.rglob('*')
            if path.is_file() and path.suffix.lower() in ('.ini', '.imi')
        ),
        key=lambda path: str(path.relative_to(directory)).casefold(),
    )


def _ini_tree_digest(directory: Path):
    files = _ini_files(directory)
    if not files:
        return None, []
    digest = hashlib.sha256()
    for path in files:
        relative = path.relative_to(directory).as_posix()
        digest.update(relative.encode('utf-8', errors='surrogatepass'))
        digest.update(b'\0')
        with path.open('rb') as fp:
            while True:
                chunk = fp.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        digest.update(b'\0')
    return digest.hexdigest(), files


def _backup_name_date(path: Path, port: int):
    match = re.fullmatch(r'%d-(\d{4}-\d{2}-\d{2})\.zip' % port, path.name)
    if not match:
        return None
    try:
        return date.fromisoformat(match.group(1))
    except ValueError:
        return None


def _latest_realm_backup(backup_root: Path, port: int):
    if not backup_root.exists():
        return None, None
    candidates = []
    for path in backup_root.glob('%d-????-??-??.zip' % port):
        backup_date = _backup_name_date(path, port)
        if backup_date is not None:
            candidates.append((backup_date, path))
    if not candidates:
        return None, None
    return max(candidates, key=lambda item: item[0])


def _backup_manifest_digest(path: Path):
    try:
        with zipfile.ZipFile(path, 'r') as archive:
            raw = archive.read(BACKUP_MANIFEST)
        data = json.loads(raw.decode('utf-8'))
        value = data.get('sha256')
        return value if isinstance(value, str) else None
    except (OSError, KeyError, ValueError, zipfile.BadZipFile, json.JSONDecodeError):
        return None


def _create_realm_backup(port: int, ini_dir: Path, backup_root: Path, files) -> Path:
    backup_root.mkdir(parents=True, exist_ok=True)
    today = date.today()
    destination = backup_root / ('%d-%s.zip' % (port, today.isoformat()))
    temporary = destination.with_suffix('.zip.tmp')
    archive_digest = hashlib.sha256()
    archived_files = []
    try:
        with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for path in files:
                relative = path.relative_to(ini_dir).as_posix()
                archive_digest.update(relative.encode('utf-8', errors='surrogatepass'))
                archive_digest.update(b'\0')
                with path.open('rb') as source, archive.open(relative, 'w') as target:
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        archive_digest.update(chunk)
                        target.write(chunk)
                archive_digest.update(b'\0')
                archived_files.append(relative)
            manifest = {
                'port': port,
                'created': datetime.now().astimezone().isoformat(timespec='seconds'),
                'source': str(ini_dir),
                'sha256': archive_digest.hexdigest(),
                'files': archived_files,
            }
            archive.writestr(BACKUP_MANIFEST, json.dumps(manifest, indent=2, sort_keys=True))
        temporary.replace(destination)
    finally:
        try:
            if temporary.exists():
                temporary.unlink()
        except OSError:
            pass
    return destination


def _maybe_backup_realm(port: int, ini_dir: Path, backup_root: Path, minimum_days: int):
    digest, files = _ini_tree_digest(ini_dir)
    if digest is None:
        return None, 'no INI/IMI files'

    latest_date, latest_path = _latest_realm_backup(backup_root, port)
    if latest_path is not None:
        previous_digest = _backup_manifest_digest(latest_path)
        if previous_digest == digest:
            return None, 'unchanged'
        # A corrupt ZIP or missing/invalid manifest is not a valid recent backup;
        # replace it immediately rather than suppressing recovery for minimum_days.
        if (
            previous_digest is not None
            and latest_date is not None
            and date.today() < latest_date + timedelta(days=minimum_days)
        ):
            return None, 'changed but not yet due'

    path = _create_realm_backup(port, ini_dir, backup_root, files)
    return path, 'created'


async def _backup_monitor(realms, backup_root, minimum_days: int, check_interval: float) -> None:
    backup_root = Path(backup_root)
    while True:
        for port, ini_dir in realms:
            try:
                path, reason = await asyncio.to_thread(
                    _maybe_backup_realm,
                    port,
                    Path(ini_dir),
                    backup_root,
                    minimum_days,
                )
                if path is not None:
                    LOG.info('INI/IMI backup created for TCP %d: %s', port, path)
                else:
                    LOG.debug('INI/IMI backup TCP %d: %s', port, reason)
            except Exception:
                LOG.exception('INI/IMI backup check failed for TCP %d', port)
        await asyncio.sleep(check_interval)


async def _event_loop_watchdog(legacy_listeners, legacy_realms, mooapi_realms, interval: float, warn_after: float) -> None:
    """Report event-loop stalls or listeners that unexpectedly stop serving.

    Normal healthy operation is silent. The warning is intended to make
    process-wide stalls and listener failures visible in persistent logs.
    """
    expected = time.monotonic() + interval
    reported_down = set()
    while True:
        await asyncio.sleep(interval)
        now = time.monotonic()
        lag = max(0.0, now - expected)
        expected = now + interval
        if lag >= warn_after:
            legacy_count = sum(len(impl.clients) for _, impl in legacy_realms)
            mooapi_count = sum(len(runtime.hub.connections) for _, runtime in mooapi_realms)
            LOG.warning(
                'event-loop delay %.3fs (threshold %.3fs); active legacy=%d mooapi=%d',
                lag, warn_after, legacy_count, mooapi_count,
            )

        for listener in legacy_listeners:
            key = ('legacy', id(listener))
            if not listener.is_serving():
                if key not in reported_down:
                    reported_down.add(key)
                    LOG.critical('legacy TCP listener is no longer serving')
            else:
                reported_down.discard(key)
        for port, runtime in mooapi_realms:
            server = runtime.tcp.server
            key = ('mooapi', port)
            if server is None or not server.is_serving():
                if key not in reported_down:
                    reported_down.add(key)
                    LOG.critical('MooAPI TCP %d listener is no longer serving', port)
            else:
                reported_down.discard(key)
            if runtime.enable_udp:
                udp_key = ('mooapi-udp', port)
                udp = runtime.udp
                transport = udp.transport if udp is not None else None
                if transport is None or transport.is_closing():
                    if udp_key not in reported_down:
                        reported_down.add(udp_key)
                        LOG.critical('MooAPI UDP %d listener is no longer serving', port + 1)
                else:
                    reported_down.discard(udp_key)


def _configure_logging(args: argparse.Namespace) -> None:
    level = logging.DEBUG if args.verbose else logging.INFO
    formatter = logging.Formatter('%(asctime)s %(levelname)s %(name)s %(message)s')
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)

    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(formatter)
    root.addHandler(console)

    if not args.no_file_log and args.log_file:
        try:
            path = Path(args.log_file)
            path.parent.mkdir(parents=True, exist_ok=True)
            handler = logging.handlers.RotatingFileHandler(
                path,
                maxBytes=args.log_max_bytes,
                backupCount=args.log_backups,
                encoding='utf-8',
            )
            handler.setLevel(level)
            handler.setFormatter(formatter)
            root.addHandler(handler)
            LOG.info(
                'persistent log enabled file=%s max_bytes=%d backups=%d',
                path, args.log_max_bytes, args.log_backups,
            )
        except OSError as exc:
            LOG.error('cannot open persistent log file %s: %s', args.log_file, exc)


async def run_unified(args: argparse.Namespace) -> None:
    loop = asyncio.get_running_loop()
    previous_exception_handler = loop.get_exception_handler()

    def _loop_exception_handler(_loop, context):
        message = context.get('message', 'unhandled asyncio exception')
        exc = context.get('exception')
        if exc is not None:
            LOG.error(
                'asyncio runtime exception: %s',
                message,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
        else:
            LOG.error('asyncio runtime exception: %s', message)

    loop.set_exception_handler(_loop_exception_handler)

    moo12_ports = _unique_ports(args.moo12_ports, DEFAULT_MOO12_PORTS)
    mooapi_ports = _unique_ports(args.mooapi_ports, DEFAULT_MOOAPI_PORTS)

    LOG.info(
        'server configuration host=%s legacy_tcp=%s mooapi_tcp=%s mooapi_udp=%s mooapi_dialect=%s moodplay=%s '
        'ini_root=%s shared_ini=%s status=%s backups=%s legacy_limits=%s/%s '
        'legacy_timeouts=unsigned:%ss write:%ss legacy_ini_limits=files:%s keys:%s bytes:%s watchdog=%ss/%ss',
        args.host, moo12_ports, mooapi_ports,
        'disabled' if args.no_mooapi_udp else [p + 1 for p in mooapi_ports],
        args.mooapi_dialect, 'disabled' if args.no_moodplay else 'enabled', args.ini_root, args.shared_ini, not args.no_status, not args.no_backups,
        args.legacy_max_connections or 'off', args.legacy_max_connections_per_ip or 'off',
        args.legacy_unsigned_timeout, args.legacy_write_timeout,
        args.legacy_ini_max_files or 'off', args.legacy_ini_max_keys_per_file or 'off',
        args.legacy_ini_max_bytes or 'off',
        args.watchdog_interval, args.watchdog_warn_after,
    )

    overlap = sorted(set(moo12_ports) & set(mooapi_ports))
    if overlap:
        raise ValueError(
            'TCP port(s) assigned to both MOO1/2 and MooAPI: %s'
            % ', '.join(str(p) for p in overlap)
        )

    if not args.no_mooapi_udp:
        for port in mooapi_ports:
            udp_port = port + DEFAULT_MOOAPI_UDP_OFFSET
            if udp_port > 65535:
                raise ValueError('MooAPI TCP port %d has no valid P+1 UDP port' % port)

    trace = JsonlTrace(args.trace)
    legacy_listeners = []
    legacy_realms = []
    mooapi_servers = []
    mooapi_realms = []
    backup_realms = []
    status_reporter = _StateStatusReporter(legacy_realms, mooapi_realms, enabled=not args.no_status)

    try:
        for port in moo12_ports:
            ini_dir = _realm_ini_dir(args.ini_root, port, args.shared_ini)
            impl = Moo12Server(
                server_text=args.server_text,
                ini_dir=str(ini_dir),
                trace=trace,
                strict=args.strict,
                realm_port=port,
                historical_fallbacks=args.historical_fallbacks,
                preface_guard=not args.no_preface_guard,
                identification_timeout=args.identification_timeout,
                preidentify_buffer=args.preidentify_buffer,
                unsigned_timeout=args.legacy_unsigned_timeout,
                write_timeout=args.legacy_write_timeout,
                max_connections=args.legacy_max_connections,
                max_connections_per_ip=args.legacy_max_connections_per_ip,
                ini_max_files=args.legacy_ini_max_files,
                ini_max_keys_per_file=args.legacy_ini_max_keys_per_file,
                ini_max_bytes=args.legacy_ini_max_bytes,
                state_changed=status_reporter.changed,
            )
            listener = await asyncio.start_server(
                impl.handle_client,
                args.host,
                port,
                limit=READ_LIMIT,
            )
            legacy_listeners.append(listener)
            legacy_realms.append((port, impl))
            backup_realms.append((port, ini_dir))
            sockets = ', '.join(str(sock.getsockname()) for sock in listener.sockets or [])
            LOG.info('MOO1/2 realm %d listening on %s; INI/IMI=%s', port, sockets, ini_dir)

        for port in mooapi_ports:
            config = ServerConfig(
                motd=args.mooapi_motd.encode('latin-1', errors='replace'),
                ini_data_root=args.ini_root,
                ini_shared=args.shared_ini,
                dialect=args.mooapi_dialect,
            )
            app = (
                _StatusApplication(status_reporter)
                if args.no_moodplay
                else _MooDPlayStatusApplication(status_reporter)
            )
            runtime = MooServer(
                app=app,
                config=config,
                host=args.host,
                port=port,
                udp_port=(port + 1) if not args.no_mooapi_udp else None,
                enable_udp=not args.no_mooapi_udp,
            )
            if isinstance(app, MooDPlayApplication):
                app.bind(runtime)
            await runtime.start()
            mooapi_servers.append(runtime)
            mooapi_realms.append((port, runtime))
            ini_dir = _realm_ini_dir(args.ini_root, port, args.shared_ini)
            backup_realms.append((port, ini_dir))
            udp_text = 'disabled' if args.no_mooapi_udp else str(runtime.udp.port if runtime.udp else port + 1)
            LOG.info(
                'MooAPI realm TCP %d / UDP %s listening on %s; mooDPlay=%s; INI/IMI=%s',
                port,
                udp_text,
                args.host,
                'disabled' if args.no_moodplay else 'enabled',
                ini_dir,
            )

        status_reporter.log_initial()

        tasks = [
            asyncio.create_task(s.serve_forever(), name='legacy-listener-%d' % port)
            for (port, _impl), s in zip(legacy_realms, legacy_listeners)
        ]
        tasks.extend(
            asyncio.create_task(s.serve_forever(), name='mooapi-listener-%d' % port)
            for port, s in mooapi_realms
        )
        if args.watchdog_interval > 0:
            tasks.append(asyncio.create_task(
                _event_loop_watchdog(
                    legacy_listeners, legacy_realms, mooapi_realms,
                    args.watchdog_interval, args.watchdog_warn_after,
                ),
                name='runtime-watchdog',
            ))
        if not args.no_backups:
            tasks.append(asyncio.create_task(
                _backup_monitor(
                    backup_realms,
                    args.backup_root,
                    args.backup_days,
                    args.backup_check_interval,
                )
            ))
        if not tasks:
            raise RuntimeError('no listeners configured')
        try:
            await asyncio.gather(*tasks)
        except Exception:
            for task in tasks:
                if task.done() and not task.cancelled():
                    try:
                        exc = task.exception()
                    except Exception:
                        exc = None
                    if exc is not None:
                        LOG.critical(
                            'background task failed name=%s error=%r',
                            task.get_name(), exc, exc_info=(type(exc), exc, exc.__traceback__),
                        )
            raise
    finally:
        LOG.info('server shutdown starting')
        status_reporter.close()
        for listener in legacy_listeners:
            listener.close()
        if legacy_listeners:
            await asyncio.gather(
                *(listener.wait_closed() for listener in legacy_listeners),
                return_exceptions=True,
            )
        if mooapi_servers:
            await asyncio.gather(
                *(server.close() for server in mooapi_servers),
                return_exceptions=True,
            )
        trace.close()
        loop.set_exception_handler(previous_exception_handler)
        LOG.info('server shutdown complete')


def build_unified_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            'Unified 3EE MOO server: MOO1/MOO2 MOS compatibility plus '
            'MooAPI/MooClick/MooGame compatibility'
        )
    )
    parser.add_argument('--host', default='0.0.0.0', help='listen address (default: 0.0.0.0)')
    parser.add_argument(
        '--moo12-port',
        dest='moo12_ports',
        action='append',
        type=int,
        help='MOO1/MOO2 TCP port; repeat for multiple realms (default: 1200)',
    )
    parser.add_argument(
        '--mooapi-port',
        dest='mooapi_ports',
        action='append',
        type=int,
        help='MooAPI TCP port; repeat for multiple realms (defaults: 1203 and 3205, UDP is TCP+1)',
    )
    parser.add_argument(
        '--no-moodplay',
        action='store_true',
        help='disable mooDPlay enhancement on normal MooAPI listeners',
    )
    parser.add_argument(
        '--no-mooapi-udp',
        action='store_true',
        help='disable MooAPI UDP Blast listener(s)',
    )
    parser.add_argument(
        '--mooapi-dialect',
        choices=('auto', 'A', 'B'),
        default='auto',
        help=(
            'MooAPI dialect for all MooAPI realms: auto, A, or B. '
            'Auto starts in A and switches a connection to B only after an accepted '
            'B-only UDP packet (default: auto)'
        ),
    )
    parser.add_argument(
        '--ini-root',
        default=DEFAULT_UNIFIED_INI_ROOT,
        help=r'INI/IMI root (default: C:\Moo\data\ini; per-port folders are created)',
    )
    parser.add_argument(
        '--shared-ini',
        action='store_true',
        help='share one INI/IMI directory across all ports instead of per-port folders',
    )
    parser.add_argument(
        '--no-status',
        action='store_true',
        help='disable startup and state-change realm status messages',
    )
    parser.add_argument(
        '--backup-root',
        default=DEFAULT_BACKUP_ROOT,
        help=r'ZIP backup directory (default: C:\Moo\data\backups)',
    )
    parser.add_argument(
        '--backup-days',
        type=int,
        default=DEFAULT_BACKUP_DAYS,
        help='minimum days between changed backups for a port (default: 7)',
    )
    parser.add_argument(
        '--backup-check-interval',
        type=float,
        default=DEFAULT_BACKUP_CHECK_INTERVAL,
        help='seconds between backup checks (default: 3600)',
    )
    parser.add_argument(
        '--no-backups',
        action='store_true',
        help='disable automatic INI/IMI ZIP backups',
    )
    parser.add_argument(
        '--server-text',
        default=DEFAULT_SERVER_TEXT,
        help='MOO1/MOO2 server text / MOTD',
    )
    parser.add_argument(
        '--mooapi-motd',
        default=DEFAULT_MOOAPI_MOTD,
        help='MooAPI MOTD (default: MooAPI Version 1.22)',
    )
    parser.add_argument('--trace', help='write MOO1/MOO2 JSONL packet trace')
    parser.add_argument('--strict', action='store_true', help='legacy MOO1/2 protocol diagnostics')
    parser.add_argument('--historical-fallbacks', action='store_true')
    parser.add_argument('--no-preface-guard', action='store_true')
    parser.add_argument(
        '--identification-timeout',
        type=float,
        default=DEFAULT_IDENTIFICATION_TIMEOUT,
    )
    parser.add_argument(
        '--preidentify-buffer',
        type=int,
        default=DEFAULT_PREIDENTIFY_BUFFER,
    )
    parser.add_argument(
        '--legacy-unsigned-timeout',
        type=float,
        default=DEFAULT_LEGACY_UNSIGNED_TIMEOUT,
        help='seconds an unsigned MOO1/2 connection may remain open; 0 disables (default: 120)',
    )
    parser.add_argument(
        '--legacy-write-timeout',
        type=float,
        default=DEFAULT_LEGACY_WRITE_TIMEOUT,
        help='seconds allowed for a MOO1/2 client write/drain before closing it (default: 5)',
    )
    parser.add_argument(
        '--legacy-max-connections',
        type=int,
        default=DEFAULT_LEGACY_MAX_CONNECTIONS,
        help='maximum simultaneous MOO1/2 TCP connections across a realm; 0 disables (default: 256)',
    )
    parser.add_argument(
        '--legacy-max-connections-per-ip',
        type=int,
        default=DEFAULT_LEGACY_MAX_CONNECTIONS_PER_IP,
        help='maximum simultaneous MOO1/2 TCP connections from one IP; 0 disables (default: 32)',
    )
    parser.add_argument(
        '--legacy-ini-max-files',
        type=int,
        default=DEFAULT_LEGACY_INI_MAX_FILES,
        help='maximum physical .imi files in a legacy realm; 0 disables (default: 256)',
    )
    parser.add_argument(
        '--legacy-ini-max-keys-per-file',
        type=int,
        default=DEFAULT_LEGACY_INI_MAX_KEYS_PER_FILE,
        help='maximum key/value entries per legacy .imi file; 0 disables (default: 4096)',
    )
    parser.add_argument(
        '--legacy-ini-max-bytes',
        type=int,
        default=DEFAULT_LEGACY_INI_MAX_BYTES,
        help='maximum bytes in a legacy .imi file after a write; 0 disables (default: 4194304)',
    )
    parser.add_argument(
        '--log-file',
        default=DEFAULT_LOG_FILE,
        help=r'persistent rotating log file (default: C:\Moo\data\logs\moo-server.log)',
    )
    parser.add_argument(
        '--no-file-log',
        action='store_true',
        help='disable persistent rotating file logging',
    )
    parser.add_argument(
        '--log-max-bytes',
        type=int,
        default=DEFAULT_LOG_MAX_BYTES,
        help='rotate persistent log after this many bytes (default: 5242880)',
    )
    parser.add_argument(
        '--log-backups',
        type=int,
        default=DEFAULT_LOG_BACKUPS,
        help='number of rotated log files to retain (default: 5)',
    )
    parser.add_argument(
        '--watchdog-interval',
        type=float,
        default=DEFAULT_WATCHDOG_INTERVAL,
        help='seconds between silent runtime health checks; 0 disables (default: 5)',
    )
    parser.add_argument(
        '--watchdog-warn-after',
        type=float,
        default=DEFAULT_WATCHDOG_WARN_AFTER,
        help='warn when event-loop scheduling is delayed by at least this many seconds (default: 2)',
    )
    parser.add_argument('--verbose', action='store_true')
    return parser


def main() -> int:
    parser = build_unified_parser()
    args = parser.parse_args()
    if args.backup_days < 1:
        parser.error('--backup-days must be at least 1')
    if args.backup_check_interval <= 0:
        parser.error('--backup-check-interval must be greater than 0')
    if args.legacy_unsigned_timeout < 0:
        parser.error('--legacy-unsigned-timeout must be 0 or greater')
    if args.legacy_write_timeout <= 0:
        parser.error('--legacy-write-timeout must be greater than 0')
    if args.legacy_max_connections < 0:
        parser.error('--legacy-max-connections must be 0 or greater')
    if args.legacy_max_connections_per_ip < 0:
        parser.error('--legacy-max-connections-per-ip must be 0 or greater')
    if args.legacy_ini_max_files < 0:
        parser.error('--legacy-ini-max-files must be 0 or greater')
    if args.legacy_ini_max_keys_per_file < 0:
        parser.error('--legacy-ini-max-keys-per-file must be 0 or greater')
    if args.legacy_ini_max_bytes < 0:
        parser.error('--legacy-ini-max-bytes must be 0 or greater')
    if args.log_max_bytes <= 0:
        parser.error('--log-max-bytes must be greater than 0')
    if args.log_backups < 0:
        parser.error('--log-backups must be 0 or greater')
    if args.watchdog_interval < 0:
        parser.error('--watchdog-interval must be 0 or greater')
    if args.watchdog_warn_after <= 0:
        parser.error('--watchdog-warn-after must be greater than 0')
    _configure_logging(args)
    LOG.info(
        'server process start pid=%d python=%s platform=%s',
        os.getpid(), platform.python_version(), platform.platform(),
    )
    try:
        asyncio.run(run_unified(args))
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError) as exc:
        LOG.error('%s', exc)
        return 2
    return 0



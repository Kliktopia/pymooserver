# PyMooServer

PyMooServer is a Python 3.11+ server for classic MOO networking. It combines a
MOO1/MOO2 text-protocol server with MooAPI TCP/UDP support used by later
MooClick, MooGame and Jamagic-era clients.

The repository is the maintainable source layout for the hardened standalone
server: the former embedded MooAPI modules are normal Python modules here, while
the network and protocol behavior remains unchanged.

## Default listeners

| Protocol | Transport | Default port |
| --- | --- | ---: |
| MOO1/MOO2 | TCP | 1200 |
| MooAPI | TCP | 1203 |
| MooAPI Blast | UDP | 1204 |
| MooAPI | TCP | 3205 |
| MooAPI Blast | UDP | 3206 |

## Requirements

- Python 3.11 or newer
- No third-party runtime dependencies
- Windows, Linux, and macOS are supported. Windows retains the historical
  `C:\Moo\data` default layout; other platforms use native per-user data paths.

## Install

For development:

```bash
python -m venv .venv
.venv\Scripts\activate
python -m pip install -e .
```

On POSIX shells use `source .venv/bin/activate` instead.

## Run

After installation:

```bash
pymooserver
```

The module form is also available after the package has been installed (including
an editable development install):

```bash
python -m pymooserver
```

Run `pymooserver --help` for all listener, logging, backup, timeout and storage
options.

## Data layout

With the default settings, server-side profile files are stored per TCP realm.
On Windows the layout remains:

```text
C:\Moo\data\ini\1200\
C:\Moo\data\ini\1203\
C:\Moo\data\ini\3205\
```

Linux uses `$XDG_DATA_HOME/pymooserver` when `XDG_DATA_HOME` is set, otherwise
`~/.local/share/pymooserver`. macOS uses
`~/Library/Application Support/PyMooServer`. The same `ini/<port>`, `backups`, and
`logs` sub-layout is used beneath those platform roots.

Client-supplied profile extensions are normalized to `.imi` for physical storage.
For example, `version`, `version.ini` and `version.exe` all resolve to
`version.imi`.

Changed IMI data is backed up periodically beneath the platform data root, for
example on Windows:

```text
C:\Moo\data\backups\PORT-YYYY-MM-DD.zip
```

## Logging

Console logging is enabled by default. A rotating persistent log is also written
beneath the platform data root, for example on Windows:

```text
C:\Moo\data\logs\moo-server.log
```

The server logs listener health, connection lifecycle and failure reasons without
logging normal message payloads or INI values.

## Protocol documentation

- [`docs/MOO12_PROTOCOL.md`](docs/MOO12_PROTOCOL.md) describes the MOO1/MOO2 text protocol, connection/channel lifecycle, messaging, and server-side profile operations.
- [`docs/MOO12_IMPLEMENTATION.md`](docs/MOO12_IMPLEMENTATION.md) explains how PyMooServer implements that protocol and its less obvious compatibility rules.
- [`docs/MOOAPI_PROTOCOL.md`](docs/MOOAPI_PROTOCOL.md) describes MooAPI TCP, UDP/Blast, Dialects A/B, and MooGame application-level payloads.
- [`docs/MOOAPI_IMPLEMENTATION.md`](docs/MOOAPI_IMPLEMENTATION.md) maps the MooAPI protocol onto the codec, state machine, transports, and profile service.
- [`docs/MOODPLAY_PROTOCOL.md`](docs/MOODPLAY_PROTOCOL.md) specifies the optional DPSC mooDPlay control layer carried over a normal MooAPI connection.

Each protocol guide stands on its own. The corresponding implementation guide builds on that protocol guide and may also point to shared launcher behavior.

## Source layout

```text
src/
  pymooserver/
    cli.py          Unified multi-realm launcher
    moo12.py        MOO1/MOO2 text-protocol server
  mooapi/
    codec/           TCP/UDP codecs
    moogame/         MooGame payload and IMI support
    transport/       asyncio TCP/UDP adapters
    hub.py           protocol state machine
    server.py        application-facing server API
    moodplay.py      optional DPSC mooDPlay application layer
```

`mooapi` remains a top-level package so the implementation keeps the same imports
and public API it used before being split out of the standalone file.

## Tests

```bash
python -m pytest
```

The included tests cover imports, profile filename normalization and injection
protection, MOO1/MOO2 transport regressions, command-line/runtime defaults, MooAPI
stream/Hello/master/rate-limit behavior, UDP dialect handling, and mooDPlay
claim/list/auth/kick/migration flows. CI runs the suite on Windows and Ubuntu.


For MooAPI deployments that need a fixed UDP generation, select it explicitly:

```bash
pymooserver --mooapi-dialect A
pymooserver --mooapi-dialect B
```

The default `auto` mode starts connections in Dialect A and changes a connection to
Dialect B only after an authenticated, accepted B-only UDP packet. Ambiguous type-01
UDP payload bytes are not used as a dialect guess.

## Notes for deployment

The default configuration is designed for a public long-running server, including
connection limits, stale-client handling, bounded UDP work, persistent diagnostics
and periodic changed-data backups. Before exposing it publicly, review the defaults
shown by `--help` and ensure the chosen TCP/UDP ports are permitted by the host
firewall. SIGTERM triggers the same orderly listener/client cleanup path used by
normal shutdown; Ctrl+C remains available for interactive use.

## License

PyMooServer is licensed under the Apache License 2.0. See [`LICENSE`](LICENSE).


## mooDPlay compatibility

Normal MooAPI listeners are mooDPlay-capable by default. A mooDPlay client first
completes the normal MooAPI handshake, then explicitly identifies itself using the
reserved reliable DPSC control channel. Native MooAPI clients remain in the normal
realm; identified mooDPlay clients use a separate application-GUID-scoped logical
session namespace with authoritative host, kick, password, max-player and migration
handling. Admission policy is checked before the underlying MooAPI join is committed,
so rejected password/full/namespace joins do not briefly expose session membership.
mooDPlay controls are TCP-only; native UDP/Blast behavior remains available on other
subchannels. Use `--no-moodplay` to disable the enhancement. See
[`docs/MOODPLAY_PROTOCOL.md`](docs/MOODPLAY_PROTOCOL.md) for the DPSC framing and opcodes.

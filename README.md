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
- Windows is the primary deployment target; the asyncio implementation is also
  written to remain portable where the required socket behavior is available.

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

Or directly from the source tree:

```bash
python -m pymooserver
```

Run `pymooserver --help` for all listener, logging, backup, timeout and storage
options.

## Data layout

With the default settings, server-side profile files are stored per TCP realm:

```text
C:\Moo\data\ini\1200\
C:\Moo\data\ini\1203\
C:\Moo\data\ini\3205\
```

Client-supplied profile extensions are normalized to `.imi` for physical storage.
For example, `version`, `version.ini` and `version.exe` all resolve to
`version.imi`.

Changed IMI data is backed up periodically to:

```text
C:\Moo\data\backups\PORT-YYYY-MM-DD.zip
```

## Logging

Console logging is enabled by default. A rotating persistent log is also written
to:

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
```

`mooapi` remains a top-level package so the implementation keeps the same imports
and public API it used before being split out of the standalone file.

## Tests

```bash
python -m pytest
```

The included tests cover imports, profile filename normalization, basic MOO1/MOO2 IMI
round-tripping, and construction of the default command-line configuration.

## Notes for deployment

The default configuration is designed for a public long-running server, including
connection limits, stale-client handling, bounded UDP work, persistent diagnostics
and periodic changed-data backups. Before exposing it publicly, review the defaults
shown by `--help` and ensure the chosen TCP/UDP ports are permitted by the host
firewall.

## License

PyMooServer is licensed under the Apache License 2.0. See [`LICENSE`](LICENSE).

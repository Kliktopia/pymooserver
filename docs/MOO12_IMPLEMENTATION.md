# PyMooServer MOO1/MOO2 Implementation Notes

## Purpose

This document explains how PyMooServer implements the MOO1/MOO2 protocol described in [`MOO12_PROTOCOL.md`](MOO12_PROTOCOL.md). It focuses on code structure, counter-intuitive compatibility behavior, and operational policy that is useful to maintainers and automated coding agents.

A useful reading order is:

1. repository [`README.md`](../README.md) for installation and operation;
2. [`MOO12_PROTOCOL.md`](MOO12_PROTOCOL.md) for the client-visible protocol model;
3. this document for implementation structure and policy;
4. tests and source for executable behavior.

The MooAPI implementation is documented separately in [`MOOAPI_IMPLEMENTATION.md`](MOOAPI_IMPLEMENTATION.md).

If a future change intentionally alters MOO1/MOO2 wire behavior, update the protocol guide, this implementation guide, and the relevant tests together.

## Relevant source layout

```text
src/pymooserver/
  __main__.py   python -m pymooserver entry point
  cli.py        unified launcher, logging, status, backups, watchdog
  moo12.py      MOO1/MOO2 text protocol and profile store

src/mooapi/     separate MooAPI implementation used by the same launcher
```

`pymooserver.moo12` contains the MOO1/MOO2 protocol implementation. `pymooserver.cli` is shared process infrastructure and launches both protocol families.

---

## Design boundary: protocol behavior versus operational policy

A recurring maintenance rule is:

> Preserve protocol-visible behavior where clients can observe it. Keep connection limits, timeouts, validation, storage bounds, logging, and process-health behavior as explicit PyMooServer operational policy rather than treating them as wire-protocol semantics.

Examples of operational controls include:

- connection caps;
- per-IP connection caps;
- write timeouts;
- unsigned-client timeouts;
- receive-buffer bounds;
- rate limits;
- UDP task bounds;
- safe socket teardown;
- path sandboxing;
- atomic profile writes;
- profile-size/key/file limits;
- rotating logs;
- public-port preface filtering;
- event-loop/listener watchdogs.

These controls are not meant to create new application-visible protocol messages. Where possible, traffic that does not satisfy the protocol or configured policy is dropped or the relevant connection is closed.

---

## Unified launcher: `pymooserver/cli.py`

`cli.py` owns process-level behavior rather than packet semantics.

Its main responsibilities are:

- choose listener ports;
- start one or more MOO1/MOO2 realms;
- start one or more MooAPI realms;
- pair MooAPI TCP realms with UDP listeners;
- select per-port profile directories;
- configure console and rotating-file logging;
- emit state-change status snapshots;
- create changed-data ZIP backups;
- monitor event-loop/listener health;
- coordinate graceful shutdown.

Default listeners are:

```text
MOO1/MOO2  TCP 1200
MooAPI     TCP 1203 / UDP 1204
MooAPI     TCP 3205 / UDP 3206
```

The launcher checks that a TCP port is not assigned to both protocol families.

---

## Realm-local profile directories

By default every TCP realm gets an independent profile directory:

```text
C:\Moo\data\ini\1200\
C:\Moo\data\ini\1203\
C:\Moo\data\ini\3205\
```

This prevents unrelated games/realms from modifying the same logical profile unless the operator explicitly enables shared storage.

The port used for the directory is the TCP realm port, including for MooAPI servers whose profile service is reached through binary packets.

---

## MOO1/MOO2 implementation

The MOO1/MOO2 implementation lives primarily in `pymooserver/moo12.py`.

### One object per physical connection

Each accepted TCP connection receives a `Client` object containing both physical and logical state:

- reader/writer;
- monotonic connection number;
- player ID, initially zero;
- alias;
- channel;
- anchor ID;
- observed version selector for diagnostics.

A signed-off client remains in the physical list until its socket closes. This is intentional because the protocol's connection count and several historical edge cases operate on physical records, not merely signed-on players.

### Physical connection order matters

`Moo12Server.clients` is append ordered.

Do not casually replace it with an unordered set or dictionary iteration if changing anchor selection. The first matching physical record affects MOO1/MOO2 channel-anchor behavior.

### Connection count means sockets

`_broadcast_count()` uses `len(self.clients)`.

Therefore:

- unsigned connections count;
- signed-off-but-still-connected sockets count;
- a player changing channel does not change the count.

This is intentionally different from “online player count”.

---

## MOO1/MOO2 connection greeting and public-port guard

The server sends the compatibility greeting before applying the modern non-MOO preface guard:

1. broadcast `count`;
2. send `ver|1|`;
3. send `motd|...|`;
4. then classify the first client bytes if preface filtering is enabled.

That ordering is deliberate. Moving the guard ahead of the greeting would make the public listener cleaner, but it would also change observable connection behavior for real MOO1/MOO2 clients.

The guard recognizes obvious HTTP, TLS, SSH, and impossible initial bytes. It is not a second protocol parser.

---

## MOO1/MOO2 unsigned timeout

The default unsigned timeout is 120 seconds.

It applies only while `client_id == 0`.

A signed-on player may remain idle indefinitely at the protocol layer. If the player explicitly signs off but keeps the socket open, the implementation creates a **fresh** unsigned grace period. This matters because re-sign-on over the same connection is valid MOO1/MOO2 behavior.

Do not change the timeout to be measured only from initial TCP accept; doing so would cause long-lived players to be disconnected immediately after a later explicit signoff.

---

## MOO1/MOO2 write handling

All normal sends use a bounded `writer.drain()`.

Why this matters: every new MOO1/MOO2 connection causes a `count` broadcast. Without a write timeout, one stale/non-reading socket can eventually block a broadcast and make new clients appear unable to connect even though the listening socket is still alive.

The implementation therefore:

- marks a client after a failed/stalled send;
- stops repeatedly writing to a known-dead/closing writer;
- closes that transport safely;
- broadcasts concurrently to a snapshot of recipients.

This is transport hardening; it does not add a wire-level failure packet.

---

## MOO1/MOO2 parser details that look wrong but are intentional

### Synthetic final LF

`mos_work_buffer()` removes the wire LF and appends one LF to the parser buffer.

This preserves the historical consequence that the final argument contains a newline when the client omits the usual trailing pipe.

Do not “clean up” all trailing whitespace globally; SSINI and malformed-input compatibility depend on more specific behavior.

### Collapsed empty tokens

`legacy_tokens()` discards empty delimiter fields. This intentionally resembles classic `strtok(..., "|")` behavior.

### Fixed-prefix command dispatch

Dispatch checks known byte prefixes rather than requiring token 0 to equal the command name exactly.

This is most visible for the one-letter commands. Tightening it to exact equality would be a behavior change.

### Latin-1 decoding

Tokens are decoded one byte to one code point. The goal is byte preservation, not modern Unicode semantics.

---

## MOO1/MOO2 IDs and channel anchors

### ID wrap

The MOO1/MOO2 ID allocator intentionally produces `100001` and resets the stored counter afterward, so the next assigned ID is `1`.

Do not replace this with the MooAPI allocator; the two families use different ID rules.

### `anchor_id` is not modeled as a strict master pointer

The property name `master_id` exists only as a backward-compatible alias. New code should reason about `anchor_id`.

`_find_anchor()` scans physical connection order for the first same-channel record with non-zero stored anchor, then returns that record's **current player ID**.

That distinction creates odd cases when a physical record has been signed off but retains other channel state.

`_repair_anchor()` also works on physical records and may select player ID zero.

This behavior is intentionally kept separate from MooAPI's cleaner session master model.

---

## MOO1/MOO2 sign-on ordering

`_join_current_channel()` performs:

1. determine anchor;
2. optionally send `welcome` to the joining client;
3. broadcast the joiner's `signon` to the channel;
4. send `signed` records for existing same-channel peers back to the joiner.

`signon` uses the normal `welcome`.

`setchannel` and `gotoempty` deliberately suppress `welcome` while still doing the rest of the join behavior.

This is easy to “simplify” incorrectly by routing all three operations through an always-welcome join helper.

---

## MOO1/MOO2 explicit signoff versus physical disconnect

They are deliberately different.

### Explicit `signoff`

- repairs channel anchor if needed;
- builds the signoff event;
- sets `client_id = 0`;
- leaves the TCP connection and other stored fields in place;
- broadcasts without the physical-disconnect trailing-pipe variant.

### Socket disconnect

- performs anchor repair while the departing record is still present;
- removes the physical record;
- if it had a non-zero player ID, broadcasts the physical-disconnect signoff form;
- updates physical connection count.

A refactor that makes these paths identical would lose compatibility details.

---

## `setchannel` and unsigned clients

`setchannel` intentionally has no sign-on gate.

For a normal client this behaves as a leave/rejoin operation. Unusual unsigned traffic can produce ID-zero state/events. PyMooServer preserves that protocol-visible behavior while applying its normal connection and timeout policies.

---

## `gotoempty` search optimization

The protocol's candidate sequence is based on the signed-16-bit suffix order described in `MOO12_PROTOCOL.md`.

A naive implementation can scan all 65,536 suffix values even when most suffix strings can never fit the 40-character channel bound. That can stall the single asyncio loop.

PyMooServer skips suffixes whose rendered candidate cannot possibly fit, while preserving the order of every candidate that **can** fit. This is an optimization, not a wire-behavior change.

The `session|...|` reply intentionally remains unterminated by LF before the subsequent join traffic.

---

## MOO1/MOO2 messaging

The `m`, `c`, `b`, and `p` handlers intentionally do not impose a modern sign-on requirement that is absent from the compatible parser behavior.

Routing rules are kept separate:

- `m`: exact current channel, sender included;
- `c`: exact current channel, sender included;
- `b`: case-sensitive channel prefix;
- `p`: target player ID anywhere in the realm.

`b` and `p` convert their delivery to normal `m|0|...` server records rather than exposing separate server event types.

---

## MOO1/MOO2 SSINI framing

This is one of the most important counter-intuitive parts of the code.

`_ssini_unframe()` removes the last character of every SSINI token unconditionally.

Therefore this is correct:

```text
getini|version.ini |version |version |
```

and this is **not** equivalent:

```text
getini|version.ini|version|version|
```

The spaces are compatibility padding, not cosmetic formatting.

The reply helper also intentionally emits one space before the final pipe:

```text
inistring|VALUE |
```

Changing either side to a more conventional trim/strip operation will break this behavior.

---

## MOO1/MOO2 profile store

The MOO1/MOO2 `IniStore` is a preservation-oriented parser rather than `configparser`.

Reasons include:

- unusual or malformed historical files should remain readable where possible;
- duplicate sections and keys have first-match semantics;
- comments/order/spelling should survive writes;
- empty section/key constructs can exist;
- mixed CR, LF, and CRLF line endings must be handled;
- quote removal on reads should resemble profile APIs;
- rewriting one key should not normalize the entire file.

### Physical filename normalization

All server-side profile files are physically `.imi`.

The final client extension is replaced:

```text
foo       -> foo.imi
foo.ini   -> foo.imi
foo.exe   -> foo.imi
foo.a.dat -> foo.a.imi
```

Client directory components are discarded/sandboxed.

Existing filenames are located case-insensitively.

### Atomic writes

Profile writes use a temporary file in the same directory, flush and `fsync` it, then `os.replace()` the destination.

This protects an existing profile against truncation if the process, disk, or antivirus layer fails during a write.

If an existing profile cannot be read, a write fails rather than pretending the file is empty and overwriting it.

### Limits

MOO1/MOO2 profile limits are runtime safety policy, not new wire semantics:

- maximum number of files;
- maximum keys per file;
- maximum bytes per file.

A rejected write receives no synthetic acknowledgement because the MOO1/MOO2 wire protocol has no write ACK.

---

## Backups

Backups are launcher functionality, not protocol behavior.

The backup monitor examines `.ini` and `.imi` files under each realm directory, allowing old leftover `.ini` files to remain protected even though new profile writes normalize to `.imi`.

Default behavior:

- check periodically;
- make a first backup when data exists;
- make no backup if data is unchanged;
- if changed, require the configured minimum day interval;
- name archives `PORT-YYYY-MM-DD.zip`;
- include a JSON manifest with file list and SHA-256 digest.

The archive digest is calculated from the same bytes that are written into the ZIP, not from a separate earlier read. This avoids a race where a changing profile could make the manifest describe bytes different from the archive contents.

A corrupt ZIP or invalid/missing manifest is not accepted as a valid recent backup and therefore does not suppress creation of a replacement.

---

## Logging

The launcher configures both console and rotating file logging by default.

Default persistent location:

```text
C:\Moo\data\logs\moo-server.log
```

Normal logs intentionally include operational state such as:

- startup configuration;
- listener availability;
- connection/disconnect reasons;
- connection/session counts;
- queued bytes;
- aggregate UDP drops;
- backup failures;
- event-loop stalls.

They intentionally avoid logging normal game-message bodies and profile values.

Client-controlled names are escaped/bounded for status output so embedded control characters cannot forge log lines.

---

## Status reporting

Status is event-driven rather than periodic spam.

The launcher logs:

- one snapshot at startup;
- a debounced snapshot after connection/session identity state changes.

MOO1/MOO2 state changes call a callback directly from the server object.

MooAPI state changes are observed through a small `MooApplication` subclass attached by the unified launcher.

The reporter compares a signature before writing a new snapshot, so a callback that produces no visible state difference does not duplicate the log.

During shutdown the reporter is disabled before pending callbacks can reschedule new snapshots.

---

## Watchdog

The runtime watchdog checks two broad classes of problems:

- the event loop returning much later than expected;
- TCP/UDP listeners that unexpectedly stop serving.

A watchdog running on the same asyncio loop cannot interrupt a blocking synchronous filesystem call while that call is in progress. It can, however, report the delay after the loop resumes.

That limitation is important when interpreting stall warnings.

---

## Synchronous filesystem operations

Normal profile reads/writes and logging still perform local filesystem work from the process that owns the asyncio event loop.

For ordinary local `C:\Moo\data` storage this is normally short-lived. A severely stalled disk, network-mounted data directory, antivirus filter, or filesystem problem can delay all realms because they share one event loop.

Moving profile I/O wholesale to worker threads is not a free improvement: shared profile files would then require explicit serialization/locking to preserve read-modify-write ordering.

Any future asynchronous-storage refactor should therefore be accompanied by concurrency tests, especially with shared profile storage enabled.

Backup creation is already moved through `asyncio.to_thread()` because it can involve much larger I/O.

---

## Regression priorities

High-value MOO1/MOO2 invariants include:

- new connection gets count/version/MOTD in order;
- signon ordering stays welcome -> signon -> signed snapshots;
- explicit signoff leaves the socket reusable;
- physical disconnect decrements the physical connection count;
- SSINI padding/unframing behavior is preserved;
- arbitrary profile extensions resolve to `.imi`;
- mixed line endings parse correctly;
- a failed profile write does not destroy the existing file;
- `gotoempty` preserves candidate order without excessive search work;
- backups hash the actual bytes archived.

---

## Guidance for automated coding agents

```text
1. Is the change visible on the MOO1/MOO2 wire?
   YES -> consult MOO12_PROTOCOL.md first.

2. Does it touch TCP parsing?
   Preserve LF framing, fixed-prefix recognition, collapsed pipe tokens,
   and the documented final-token behavior.

3. Does it touch signoff/disconnect/channel movement?
   Verify physical connection state, player ID state, anchor repair,
   connection count, and status callbacks separately.

4. Does it touch profile writes?
   Preserve case-insensitive lookup, first-match semantics, .imi naming,
   atomic replacement, and configured caps.

5. Is it only an operational control?
   Keep limits, timeouts, logging, and listener health separate from
   client-visible protocol messages.
```

---

## Documentation relationship

| File | Role |
| --- | --- |
| `README.md` | installation, operation, defaults, repository overview |
| `docs/MOO12_PROTOCOL.md` | implementation-independent MOO1/MOO2 wire/state model |
| `docs/MOO12_IMPLEMENTATION.md` | this implementation guide |
| `docs/MOOAPI_PROTOCOL.md` | separate MooAPI wire/state model |
| `docs/MOOAPI_IMPLEMENTATION.md` | separate MooAPI implementation guide |
| `tests/` | executable regression claims |
| `src/pymooserver/moo12.py` | current MOO1/MOO2 implementation |

Operational-only changes may require only implementation notes/tests. Changes to bytes, ordering, IDs, channel state, or profile behavior should be reflected in both the protocol and implementation documentation.

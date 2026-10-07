# PyMooServer MooAPI Implementation Notes

## Purpose

This document explains how PyMooServer implements the MooAPI protocol described in [`MOOAPI_PROTOCOL.md`](MOOAPI_PROTOCOL.md). It focuses on module responsibilities, state-machine behavior, TCP/UDP transport policy, MooGame application payload handling, and compatibility choices that are intentionally explicit.

A useful reading order is:

1. repository [`README.md`](../README.md) for installation and operation;
2. [`MOOAPI_PROTOCOL.md`](MOOAPI_PROTOCOL.md) for packet/state behavior;
3. this document for implementation structure and policy;
4. tests and source for executable behavior.

The MOO1/MOO2 implementation is documented separately in [`MOO12_IMPLEMENTATION.md`](MOO12_IMPLEMENTATION.md).

If a future change intentionally alters MooAPI wire behavior, update the protocol guide, this implementation guide, and the relevant tests together.

## Relevant source layout

```text
src/mooapi/
  app.py           optional application callbacks
  config.py        compatibility and resource-policy configuration
  effects.py       side-effect objects emitted by the state machine
  hub.py           deterministic connection/session state machine
  ids.py           shared MooAPI ID allocator
  limits.py        token-bucket rate limiter
  model.py         mutable connection/session state
  server.py        high-level server facade and application dispatch

  codec/
    packets.py     typed packet dataclasses
    primitives.py  checked integer/blob primitives
    stream.py      incremental TCP stream decoder
    dialect_a.py   primary TCP codec
    dialect_b.py   Dialect B Alias compatibility handling
    udp_a.py       Dialect A UDP codec
    udp_b.py       Dialect B UDP codec

  moogame/
    messages.py    optional MooGame typed payload helpers
    ini.py         binary profile service and filesystem store

  transport/
    tcp.py         asyncio TCP transport
    udp.py         asyncio UDP transport

src/pymooserver/cli.py
                  shared launcher, logging, status, backups, watchdog
```

The `mooapi` package intentionally remains top-level so it can be used independently of the unified launcher.

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

## MooAPI layering

The MooAPI implementation deliberately separates four concerns:

```text
codec -> hub -> effects -> transport/application
```

### Codec

Pure byte parsing/encoding. No sockets, session mutation, or clocks.

### Hub

Owns deterministic connection/session state. Accepts typed packets and returns ordered effects.

### Effects

Describe what should happen next, such as TCP send, UDP send, close connection, or application callback.

### Transport/server facade

Performs real I/O, rate limiting, timeouts, endpoint learning, and application hook dispatch.

This separation is important for testing. Protocol state transitions can be fuzzed without opening sockets.

---

## MooAPI shared ID namespace

`mooapi.ids.IdAllocator` is used for both connections and sessions.

It starts at `1`, monotonically increments, and stops at `0x7FFFFFFF` rather than wrapping.

This is intentionally different from the MOO1/MOO2 ID behavior.

---

## MooAPI default compatibility policy

`ServerConfig` contains several behavior choices where a single historical rule is either undesirable for modern interoperability or not universal across client generations.

The normal defaults are:

```text
require_hello       = True
master_announce     = "successor"
duplicate_join      = "ignore"
rename_broadcast    = "deduplicated"
dialect             = "auto"
relay_private_blast = False
udp_echo_to_sender  = False
```

These defaults should be understood as **compatibility policy**, not as proof that every original host used those exact semantics.

`original_tcp_comparison()` and `original_b_comparison()` provide alternate presets for comparison behavior for older client generations without weakening the normal transport safety limits.

---

## MooAPI connection establishment

The hub allocates a connection and immediately emits:

1. MOTD;
2. assigned-ID packet;
3. application `connect` event.

The transport sends the first two before it has classified the client's first inbound bytes. This intentionally preserves immediate server greeting behavior.

With the default configuration, the client must then send a valid Hello before stateful packets. A repeated Hello is a protocol error.

Older comparison profiles can disable the Hello requirement.

---

## MooAPI TCP stream decoder

`codec/stream.py` exists because TCP does not preserve packet boundaries.

It retains partial bytes between reads and repeatedly invokes the packet decoder until:

- a complete packet is produced;
- more bytes are required; or
- a malformed packet makes the stream unrecoverable.

There is intentionally no resynchronization scan after a malformed binary packet. Without an outer delimiter, guessing a new packet boundary would be unsafe and likely incorrect.

---

## MooAPI join ordering

`Hub._join()` preserves a specific effect order:

1. `Welcome` to the joiner;
2. all `Joined` notifications to existing members;
3. all `Exists` descriptions back to the joiner;
4. application `join` callback.

Do not interleave `Joined` and `Exists` per peer unless intentionally changing observable packet ordering.

Session name lookup uses exact byte values.

`Session.master_id` is simply the first member ID, or zero when empty.

---

## Duplicate join policy

Default mode ignores a request to join a session the connection already belongs to.

Older-client comparison mode can append the connection again and resend normal join traffic. The state structures are lists rather than sets partly because that duplicate-membership behavior must remain representable.

If changing these collections to sets, the comparison mode and packet ordering would change.

---

## Leave/master policy

The protocol guide describes the ambiguity in the server `Left` packet's `master_id`.

PyMooServer makes the choice explicit:

### `master_announce="successor"` (default)

1. remove the leaving member;
2. compute the new first member;
3. announce that successor in `Left`.

This avoids leaving clients with a master ID that refers to the departing player.

### `master_announce="original"`

1. capture the old master;
2. emit `Left` while the old membership is still conceptually in force;
3. remove the member.

The two policies should not be merged accidentally during refactors.

---

## Rename broadcast policy

The connection name is global.

Two modes exist:

### `deduplicated` (default)

Build the recipient list in first-encounter session order and send at most one Alias packet to each recipient.

### `per-session`

Emit Alias once for each shared session membership. A peer sharing multiple sessions can receive duplicate identical packets.

Again, the use of ordered lists/sets in this code is deliberate.

---

## MooAPI outbound queue and write timeout

Each TCP peer has an asynchronous send queue and a byte count of queued payloads.

Two limits protect the whole process from a client that stops reading:

- `max_send_queue`: closes a peer before queued bytes can grow without bound;
- `write_timeout`: bounds `writer.drain()`.

A critical invariant is that transport closure must also remove the connection from the Hub. Earlier designs that only closed the socket could leave ghost users and sessions indefinitely.

When modifying TCP shutdown paths, verify both:

```text
socket/peer removed
AND
Hub connection/session state removed
```

---

## MooAPI TCP connection/rate controls

The TCP adapter applies:

- global connection cap;
- per-IP simultaneous connection cap;
- per-IP connection-rate token bucket;
- packet-rate token bucket;
- byte-rate token bucket;
- join/leave/rename-rate token bucket;
- Hello timeout;
- partial-packet timeout;
- idle timeout;
- bounded pre-identification buffer;
- receive-buffer cap in the stream decoder.

These are runtime protections. They should not be encoded into packet classes or the deterministic Hub unless they become protocol state.

The per-IP connection-rate cache is bounded/pruned so random source addresses cannot create permanent memory growth.

---

## TCP keepalive and Windows teardown

Both protocol families enable best-effort TCP keepalive. Where available, the server requests roughly:

```text
idle before probes: 60 s
probe interval:      20 s
probe count:          3
```

Windows also receives `SIO_KEEPALIVE_VALS` when the socket wrapper exposes it.

Socket close waits are bounded because Windows asyncio/Proactor transports can retain/reset errors or delay `wait_closed()` during peer aborts.

These exceptions are treated as cleanup conditions, not protocol failures that require a wire response.

---

## Dialect selection

A connection starts in Dialect A when configuration is `auto`.

TCP client packet layouts do not provide a strong discriminator between A and B, so TCP alone generally leaves the connection in A.

UDP can provide a B discriminator:

- packet `0x03` is B-only;
- a B-looking type `0x01` header can also indicate B.

A crucial security rule is that **dialect state is not changed until the UDP sender has been authenticated** against the live TCP connection and source IP.

Otherwise a random datagram could guess a small connection ID and flip another player's TCP encoder to Dialect B.

---

## Dialect-B Alias handling

`codec/dialect_b.py` special-cases only the server Alias packet.

`dialect_b_alias_policy` supports:

- `padded-best-effort` (default);
- `original-A`;
- `suppress`.

`padded-best-effort` inserts six zero bytes before the alias data to accommodate the older parser's apparent offset expectations.

This remains explicitly a best-effort mode. Do not remove the policy switch unless a single form is conclusively shown to work across all supported B clients.

---

## UDP input safety and state mutation

`transport/udp.py` intentionally performs work in this order:

1. reject oversized datagrams synchronously;
2. bound the number of pending datagram tasks;
3. decode a candidate packet **without committing dialect changes**;
4. locate the claimed sender ID in the live Hub;
5. compare UDP source IP with the TCP peer IP;
6. only then commit a detected dialect;
7. allocate/use the sender rate bucket;
8. ask the Hub to validate packet semantics and membership;
9. only after acceptance, learn the UDP source endpoint;
10. execute resulting effects.

The order matters for both security and memory safety.

In particular, do not create a persistent rate bucket for every claimed sender ID before confirming that the ID belongs to a live connection.

---

## UDP pending-task limit

Datagram callbacks are lightweight but handling is asynchronous. A public UDP flood can arrive faster than the event loop completes handlers.

PyMooServer caps pending UDP tasks at 512 per UDP adapter.

Excess datagrams are intentionally dropped and counted. UDP already permits loss, making controlled dropping preferable to allowing unbounded task growth that can affect every TCP realm in the process.

Oversized datagrams are rejected **before** task creation so they cannot consume the task budget.

---

## UDP drop logging

Normal Internet-facing UDP ports receive arbitrary noise.

Logging every rejected datagram would create its own denial-of-service path, so drop reasons are aggregated and warnings are emitted only at milestones.

Examples include:

- oversize;
- task backlog;
- decode error;
- unknown sender;
- source-IP mismatch;
- rate limit;
- Hub rejection;
- transport error.

The counters are visible in status snapshots.

---

## UDP endpoint learning

An accepted UDP datagram teaches the adapter the sender's current `(IP, source_port)`.

The learned endpoint is transient and connection-scoped.

Outbound UDP uses the learned endpoint while it is within `udp_endpoint_ttl`; otherwise it falls back to the TCP peer IP and the server's base UDP port.

By default this metadata is forgotten at disconnect and is never written to disk.

---

## Direct/private Blast policy

The deterministic Hub understands both Dialect A and B UDP packet models, but the default server does **not** relay private user Blast traffic as if the server were a router.

This preserves the later peer-to-peer model.

`relay_private_blast` exists for controlled compatibility experiments.

When sending a server-originated private-style packet to a Dialect A client, the UDP adapter converts it to the Dialect A type-`0x01` form because Dialect A does not have the B `0x03` receive layout.

---

## MooGame typed message layer

`moogame/messages.py` is intentionally optional.

The core MooAPI relay treats application payloads as opaque bytes. Typed decoding is only for applications that knowingly use MooGame's inner message types.

This prevents the transport from rejecting future or game-specific payloads merely because PyMooServer does not understand them.

Object messages and tracking messages remain opaque bodies.

---

## MooGame INI interception

MooGame profile requests arrive as ordinary TCP message-to-server events.

`server.py` gives the configured `IniService` the first chance to consume payloads beginning `0x0B` through `0x0E`.

If consumed:

- Set operations write the profile and return no application callback;
- Get operations generate a typed `0x0F` or `0x10` reply and send it as a server-originated reliable MooAPI message;
- malformed INI-shaped requests are consumed and dropped rather than passed to unrelated application code.

Non-INI payloads continue to `on_server_message`.

This routing boundary is important if adding other application-level services.

---

## MooGame profile filename normalization

Normalization happens at both service and filesystem boundaries so direct store calls cannot bypass it.

Rules:

1. treat the value as an 8-bit name;
2. strip client directory components before applying the filename limit;
3. replace the final extension with `.imi`;
4. cap the stem so the final filename stays within the expected bound;
5. perform case-insensitive physical lookup.

Stripping directories **before** truncation is significant. If a long client path were truncated first, it could consume the whole filename allowance and collapse an ordinary basename into the wrong physical name.

---

## MooGame profile limits and atomic writes

The filesystem store enforces its configured:

- max profile count;
- max keys per profile;
- max profile bytes.

Limits are checked against the final rendered profile where necessary. This matters because an application-supplied binary string can contain line breaks that would otherwise create additional INI-looking key lines after the initial logical operation.

Writes use the same atomic replace pattern as the MOO1/MOO2 store.

If an existing file cannot be read, the store refuses the write rather than risking data loss.

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

## Cleanup of application facade caches

`MooServer` caches lightweight `Client` and `Session` facade objects for application callbacks.

Those caches must be cleaned when the underlying Hub connection/session disappears. Otherwise a long-running public server can leak one Python object for every historical connection or room even though protocol state is correct.

When adding new destruction paths, verify both protocol state and facade-cache cleanup.

---

## Application callback isolation

Hooks in `MooApplication` may be synchronous or asynchronous.

`MooServer` catches/logs application exceptions so a faulty game callback does not tear down the transport accept loop or corrupt protocol sequencing.

Effect order is maintained while hooks are dispatched. In particular, wire notifications for join/leave are processed before the corresponding application callback.

---

## Regression priorities

High-value MooAPI invariants include:

### TCP

- MOTD precedes assigned ID;
- the stream decoder handles fragmentation and coalescing;
- Hello gating follows configuration;
- join packet ordering is stable;
- leave/master policy follows configuration;
- duplicate-join and rename-broadcast policy follow configuration;
- send-queue overflow removes protocol state as well as closing the socket;
- client/session facade caches return to zero after churn.

### UDP

- unknown IDs do not allocate persistent per-sender state;
- source-IP mismatch cannot mutate connection dialect;
- accepted datagrams learn endpoints;
- disconnect forgets endpoints and rate buckets;
- pending task count is bounded;
- oversized datagrams are rejected before task creation;
- Dialect A and Dialect B layouts remain distinct.

### MooGame profile service

- `.ini`, `.exe`, and extensionless names resolve to `.imi`;
- profile lookup is case-insensitive;
- key/file/byte caps are enforced;
- existing data survives failed writes;
- backups hash the bytes actually written to the archive.

---

## Guidance for automated coding agents

```text
1. Is the behavior visible on the MooAPI wire?
   YES -> consult MOOAPI_PROTOCOL.md first.

2. Does it touch TCP framing?
   Preserve incremental parsing across arbitrary recv boundaries.

3. Does it touch join/leave/rename state?
   Verify Hub state, ordered effects, transport state, application facades,
   and configured compatibility policy together.

4. Does it touch UDP sender identification?
   Do not mutate persistent connection state before authenticating the
   claimed sender against the corresponding TCP peer identity.

5. Does it touch profile writes?
   Preserve .imi normalization, case-insensitive lookup, atomic replacement,
   and configured caps.

6. Is the difference generation-specific?
   Prefer an explicit dialect/configuration policy instead of silently
   making one behavior universal.
```

---

## Documentation relationship

| File | Role |
| --- | --- |
| `README.md` | installation, operation, defaults, repository overview |
| `docs/MOOAPI_PROTOCOL.md` | implementation-independent MooAPI wire/state model |
| `docs/MOOAPI_IMPLEMENTATION.md` | this implementation guide |
| `docs/MOO12_PROTOCOL.md` | separate MOO1/MOO2 wire/state model |
| `docs/MOO12_IMPLEMENTATION.md` | separate MOO1/MOO2 implementation guide |
| `tests/` | executable regression claims |
| `src/mooapi/` | current MooAPI implementation |

Operational-only changes may require only implementation notes/tests. Changes to packet bytes, ordering, IDs, session membership, UDP routing, dialect behavior, or client-visible profile operations should be reflected in both the protocol and implementation documentation.

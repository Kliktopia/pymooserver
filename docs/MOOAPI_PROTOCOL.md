# MooAPI Protocol Guide

## Purpose

This document describes the current best understanding of the MooAPI binary protocol family used by MooClick, MooGame, Jamagic-era software, and related clients. It is intended as a practical interoperability specification rather than a historical narrative.

MooAPI is distinct from the MOO1/MOO2 text protocol. It uses typed binary TCP packets, a companion UDP/Blast protocol, connection and session IDs, and optional application-level payload conventions used by MooGame.

Where behavior differs by client generation, the guide uses **Dialect A** for the later/common form and **Dialect B** for an older MooGame-compatible form.

The guide is implementation-independent. It describes packet layouts, state transitions, ordering, and compatibility ambiguities without depending on a particular server codebase.

## Conventions

Unless otherwise stated:

- names and application payloads are opaque byte strings rather than Unicode strings;
- all integers are little-endian;
- `u8` means unsigned 8-bit integer;
- `i16` means signed 16-bit integer;
- `u32` means unsigned 32-bit integer;
- `i32` means signed 32-bit integer;
- `blob32` means `u32 length` followed by exactly that many bytes.

Confidence labels are used where useful:

- **High** — behavior is consistent and should be treated as part of the protocol.
- **Medium** — behavior is strongly indicated but has known variation or incomplete coverage.
- **Low / ambiguous** — multiple compatible behaviors exist or the exact rule is unclear.

Typical port pairs are TCP `1203` / UDP `1204` and TCP `3205` / UDP `3206`. The UDP port is conventionally the associated TCP port plus one.

---

## MooAPI binary TCP protocol

### Stream framing

**Confidence: High**

MooAPI TCP is a byte stream with no outer packet-length header.

A receiver must:

1. inspect the packet ID;
2. read the packet's fixed fields;
3. read any embedded length field;
4. wait until the complete packet has arrived;
5. consume exactly that packet;
6. continue parsing any remaining bytes.

TCP fragmentation and packet coalescing are normal. One `recv()` does not correspond to one MooAPI packet.

---

### Primitive encodings

#### `i16`

Two-byte signed little-endian integer.

#### `u32`

Four-byte unsigned little-endian integer.

#### `blob32`

```text
u32 length
byte[length] value
```

Names, IP strings, MOTD strings, and normal TCP message bodies use this general form.

Names should be treated as opaque 8-bit data. NUL bytes are not expected in normal names.

---

## MooAPI TCP: client to server

### Packet `0x01` — channel message

```text
u8      0x01
i16     subchannel
u32     channel_id
blob32  data
```

The sender must be a member of `channel_id` for normal routing.

---

### Packet `0x02` — message to server

```text
u8      0x02
i16     subchannel
blob32  data
```

This is application/server-directed data rather than a channel relay.

MooGame server-side INI operations are commonly transported inside this packet's `data` field.

---

### Packet `0x03` — private message

```text
u8      0x03
i16     subchannel
u32     channel_id
u32     target_player_id
blob32  data
```

The normal model requires both sender and target to be members of `channel_id`.

There is no separate server-to-client “private” packet ID; the recipient receives a normal server `0x01` message identifying the original sender.

---

### Packet `0x04` — join session

```text
u8      0x04
blob32  session_name
```

Session-name equality is best treated as exact byte equality.

If a session of the same name already exists, the player joins that session; otherwise the server creates it and allocates a session ID.

---

### Packet `0x05` — leave session

```text
u8   0x05
u32  channel_id
```

The player requests removal from the named session.

---

### Packet `0x0B` — rename

```text
u8      0x0B
blob32  new_name
```

The new name applies to the connection/player rather than to a single session.

---

### Packet `0x0C` — hello

```text
u8      0x0C
i16     client_version
blob32  player_name
```

Later clients normally send this early in the connection lifecycle. Some older compatibility modes appear able to operate without requiring it.

---

## MooAPI TCP: server to client

### Packet `0x01` — message from channel/server

```text
u8      0x01
i16     subchannel
u32     channel_id
u32     sender_player_id
blob32  data
```

Typical meanings:

- `channel_id != 0`, `sender_player_id != 0`: message from another player in a session;
- `channel_id` may be `0` for a direct server-originated message;
- `sender_player_id == 0` denotes the server rather than a normal player.

---

### Packet `0x05` — player left

```text
u8   0x05
u32  player_id
u32  channel_id
u32  master_id
```

`channel_id` is non-zero in a normal leave notification.

#### `master_id` ambiguity

**Confidence: Medium / ambiguous**

Two useful interpretations exist in compatible software:

1. **pre-leave master** — the field reflects the master immediately before the player is removed;
2. **successor master** — the field reflects the oldest remaining member after removal.

A robust implementation should make this policy explicit rather than assuming every client generation interprets the field identically.

---

### Packet `0x06` — joined member

```text
u8      0x06
u32     player_id
u32     channel_id
u32     master_id
blob32  player_name
blob32  ip_text
```

Sent to existing session members to announce a newcomer.

`ip_text` is normally an ASCII dotted IP address carried as a length-prefixed byte string.

---

### Packet `0x07` — existing member

```text
u8      0x07
u32     player_id
u32     channel_id
u32     master_id
blob32  player_name
blob32  ip_text
```

Sent to a newly joined player to describe an already-present session member.

---

### Packet `0x08` — welcome to session

```text
u8      0x08
u32     player_id
u32     channel_id
u32     master_id
blob32  player_name
blob32  ip_text
blob32  session_name
```

This describes the joiner's own membership and the session selected by the join.

---

### Packet `0x0A` — MOTD

```text
u8      0x0A
blob32  text
```

The server sends the message of the day when the TCP connection is established.

---

### Packet `0x0B` — alias/name changed

Dialect A form:

```text
u8      0x0B
u32     player_id
blob32  new_name
```

See the dialect-B section for an older-client compatibility problem affecting this packet.

---

### Packet `0x0C` — connection ID assigned

```text
u8   0x0C
i16  protocol_version
u32  connection_id
```

A commonly compatible server value for `protocol_version` is `3`.

The `connection_id` is non-zero.

---

## MooAPI connection lifecycle

### Connection establishment

**Confidence: High for later dialect-A behavior**

A normal server-side sequence is:

1. accept TCP connection;
2. allocate a connection ID;
3. send `0x0A` MOTD;
4. send `0x0C` assigned-ID packet;
5. receive client `0x0C` hello;
6. process join/message requests.

The MOTD precedes the assigned-ID packet.

The server-assigned ID namespace is best modeled as shared between connection IDs and session IDs rather than as two independent counters.

A practical allocator begins at `1`, increments monotonically, does not intentionally reuse IDs, and stops before values exceed signed 32-bit positive range.

---

### Join sequence

Suppose player `P` joins session `S` containing existing players `A`, `B`, ...

The useful ordering is:

1. send `Welcome (0x08)` to `P`;
2. send `Joined (0x06)` describing `P` to each existing member;
3. send `Exists (0x07)` describing each existing member back to `P`.

The session master is normally the oldest/current first member of the membership list.

For a newly created session, the joining player is therefore also the master.

---

### Duplicate joins

**Confidence: Medium / generation-dependent**

Two behaviors are known to be useful:

- **ignore duplicate join** — if the connection is already a member, do nothing;
- **repeat original join mechanics** — append another membership occurrence and resend the normal welcome/join/existing packets.

The latter can produce duplicate user representations in clients. Modern compatibility servers generally prefer the first behavior unless reproducing an older host exactly.

---

### Leave and disconnect

A requested leave normally causes `0x05` notifications and removes the player's membership.

On whole TCP disconnect, the server logically leaves every joined session, normally in the order those sessions were joined, then removes the connection itself.

When a session becomes empty, it ceases to exist.

There is no need for a distinct wire-level “destroy session” packet: server-side destruction can be expressed as a sequence of ordinary member removals.

---

### Rename propagation

A rename changes the connection's name globally.

Two useful broadcast models exist:

- emit one alias packet per shared session, which can produce duplicates to the same recipient;
- de-duplicate recipients and send at most one alias packet to each connected peer.

The exact older-host behavior may be session-oriented; de-duplication is usually friendlier to clients.

---

## MooAPI Dialect B TCP difference

### General rule

Client-to-server TCP packet layouts are effectively the same as Dialect A for the packet set described above.

Most server-to-client packets can also use the Dialect A format.

The main compatibility distinction is server packet `0x0B` (alias/name changed) in an older client generation.

### Alias parser incompatibility

**Confidence: Low / ambiguous**

The older client-side alias parser appears internally inconsistent about where the alias bytes begin and how many bytes should be consumed.

Three server strategies are therefore defensible:

#### A-form

Use the normal Dialect A packet:

```text
0B
u32 player_id
u32 name_length
byte[name_length] name
```

#### Padded compatibility form

Insert six zero bytes between the length and name:

```text
0B
u32 player_id
u32 name_length
00 00 00 00 00 00
byte[name_length] name
```

This can better align with the older parser in some buffering conditions, but cannot be considered universally reliable.

#### Suppress aliases

For an application where renames are non-essential, not sending the alias event can be safer than feeding an unstable parser.

No single alias policy should be described as universally correct for all Dialect B clients.

---

## MooAPI UDP / Blast protocol

### General UDP rules

Each UDP datagram is one complete packet. There is no message-length field for the final payload; the payload is simply the rest of the datagram.

A UDP sender identifies itself with its TCP connection/player ID. A secure receiver should associate that claimed ID with the IP address of the corresponding TCP connection before accepting the datagram.

UDP is unreliable by design. Loss, duplication, and reordering must be tolerated by applications.

---

## Dialect A UDP

### Packet `0x01` — routed Blast

```text
u8    0x01
i16   subchannel
u32   channel_id
u32   sender_id
u32   target_id
bytes data_to_end_of_datagram
```

The meaning of `target_id` depends on direction.

#### Client to server: channel Blast

```text
target_id = 0
```

The server validates that the sender belongs to `channel_id`, then fans the packet out to session recipients.

For each server-to-client copy, `target_id` is rewritten to the receiving player's ID.

#### Direct user Blast

A non-zero `target_id` represents direct player-to-player unreliable delivery.

Later clients are expected to send this directly to the peer rather than through a standalone server. Therefore a server receiving a client `0x01` with non-zero target should not automatically treat it as a request for server relay.

#### Server-originated Blast

The server may use the same packet with:

- `sender_id = 0` to denote the server;
- a session ID or zero channel ID depending on scope;
- `target_id` set to the particular receiving connection.

---

### Packet `0x02` — Blast to server

```text
u8    0x02
i16   subchannel
u32   sender_id
bytes data_to_end_of_datagram
```

This is the unreliable counterpart of a TCP message-to-server operation.

---

## Dialect B UDP

Dialect B has a different channel-Blast header and adds a distinct private-Blast packet.

### Packet `0x01` — channel Blast

```text
u8    0x01
i16   subchannel
u32   channel_id
u32   sender_id
bytes data_to_end_of_datagram
```

There is **no `target_id` field**.

---

### Packet `0x02` — Blast to server

Same basic layout as Dialect A:

```text
u8    0x02
i16   subchannel
u32   sender_id
bytes data_to_end_of_datagram
```

---

### Packet `0x03` — private Blast

```text
u8    0x03
i16   subchannel
u32   channel_id
u32   sender_id
u32   target_id
bytes data_to_end_of_datagram
```

This form is specific to the older dialect.

Whether a standalone server should relay it is a compatibility-policy question. Native later-client behavior favors direct peer-to-peer unreliable delivery rather than server relay.

---

## UDP endpoint discovery and NAT behavior

**Confidence: Medium**

The TCP connection reliably provides the peer IP but not necessarily the UDP source port after NAT.

A practical model is:

1. use the TCP peer IP as the identity check;
2. accept a UDP packet only if its claimed sender ID names a live TCP connection from the same IP;
3. after accepting it, remember the actual `(IP, UDP source port)` as that player's current UDP endpoint;
4. use that learned endpoint for subsequent server-originated UDP while it remains fresh;
5. otherwise fall back to the peer IP and the conventional UDP port associated with the server.

This endpoint-learning mechanism is transport behavior, not persistent player identity.

---

## MooGame application payload layer

MooGame places an additional message type byte inside the ordinary MooAPI message payload.

The following types are useful to recognize.

| Type | Meaning | Body |
| ---: | --- | --- |
| `0x07` | String message | raw bytes |
| `0x08` | Number message | one little-endian `i32` |
| `0x09` | Binary message | raw bytes |
| `0x0A` | Object message | opaque serialized object bytes |
| `0x0B` | INI SetValue request | structured fields |
| `0x0C` | INI SetString request | structured fields |
| `0x0D` | INI GetValue request | structured fields |
| `0x0E` | INI GetString request | structured fields |
| `0x0F` | INI numeric reply | one little-endian `i32` |
| `0x10` | INI string reply | raw bytes |
| `0x14`–`0x17` | Tracking/state messages | opaque bytes unless application-specific knowledge is available |

Unknown application payload types should generally remain opaque rather than being rejected by the MooAPI transport.

---

## MooGame binary INI requests

### Common request prefix

The payload begins with one operation byte, followed by three signed-16-bit-length strings:

```text
u8      operation

i16     filename_length
byte[]  filename

i16     group_length
byte[]  group

i16     item_length
byte[]  item
```

Lengths are little-endian signed 16-bit values and must not be negative.

The filename is expected to be non-empty. Empty group or item names can exist in profile-style files and may need to be accepted.

NUL bytes are not expected in these names.

---

### `0x0B` — SetValue

After the common prefix:

```text
i32 value
```

No reply is required for a successful write.

---

### `0x0C` — SetString

After the common prefix:

```text
i16    declared_string_length
bytes  value_to_end_of_payload
```

A counter-intuitive compatibility behavior is that the declared length can be validated for plausibility while the stored value is still taken from **all remaining bytes** in the payload.

Software aiming for exact compatibility should not assume the declared length is necessarily the final amount consumed.

No reply is required for a successful write.

---

### `0x0D` — GetValue

The request ends immediately after filename/group/item.

Reply payload:

```text
u8   0x0F
i32  value
```

Stored profile text is conventionally converted using decimal-prefix / `atoi`-style behavior. Non-numeric or missing values therefore become zero.

---

### `0x0E` — GetString

The request ends immediately after filename/group/item.

Reply payload:

```text
u8     0x10
bytes  value
```

There is no additional length inside the typed payload; the surrounding MooAPI TCP message already provides its data length.

---

## Server-side profile naming

Profile requests contain a client-supplied filename, but server-side implementations should treat it as a logical profile name rather than an unrestricted filesystem path.

Useful compatibility behavior is:

- ignore client directory components;
- compare physical filenames case-insensitively;
- treat `.imi` as the canonical physical profile extension;
- replace a supplied final extension rather than appending to it.

Examples:

```text
version       -> version.imi
version.ini   -> version.imi
version.exe   -> version.imi
data.old.dat  -> data.old.imi
```

This normalization is storage behavior. The wire protocol itself still transports the filename supplied by the client.

---

## Names, case, and byte handling

The safest interoperability assumptions are:

| Field | Comparison / interpretation |
| --- | --- |
| Session name | exact byte equality |
| Player name | opaque 8-bit bytes |
| Profile filename | case-insensitive at storage layer |
| Profile section | case-insensitive |
| Profile key | case-insensitive |
| Application message body | opaque bytes |

Do not apply Unicode normalization to protocol names unless the application explicitly requires it.

---

## Compatibility cautions

### TCP packets are independent of socket-read boundaries

A single MooAPI packet may arrive across several reads, and several packets may arrive in one read. Parsing must be incremental.

### UDP identity is more than the claimed sender ID

A claimed sender ID should be associated with the corresponding TCP peer identity before state-changing UDP information is trusted.

### Direct Blast is not necessarily server relay

Later Dialect A `User: Blast` traffic is primarily peer-to-peer. A standalone server should not treat every non-zero UDP target as a request for server relay.

### Dialect A/B type-01 ambiguity

Dialect A and Dialect B both use UDP packet type `0x01`, but the header lengths differ. Bytes that are a non-zero `target_id` in Dialect A occupy the start of the payload in Dialect B. Therefore packet contents alone cannot reliably identify the dialect for every type-`0x01` datagram.

A compatible server should use an explicit dialect setting or some independent, unambiguous client-generation signal when it needs to distinguish these forms. A non-zero Dialect-A target must not be reinterpreted as a Dialect-B channel broadcast merely because the same bytes are non-zero.

### Client generations can differ internally

Dialect B Alias handling is the clearest example. Compatibility policy should be explicit where client behavior is ambiguous rather than assuming one packet form works universally.

---

## Recommended interoperability tests

### Handshake

1. Connect TCP.
2. Decode server `0x0A` MOTD.
3. Decode server `0x0C` assigned ID.
4. Send client `0x0C` hello with a name.

### Two-player join

1. Player A completes hello and joins `Room`.
2. A receives `0x08` welcome.
3. Player B completes hello and joins `Room`.
4. B receives `0x08` welcome.
5. A receives `0x06` describing B.
6. B receives `0x07` describing A.

### Reliable channel message

1. A and B join the same session.
2. A sends client `0x01` with that channel ID.
3. B receives server `0x01` containing A's player ID and the same payload.

### Dialect A channel Blast

1. Establish the sender's TCP connection and session membership.
2. Send UDP `0x01` with `target_id = 0`.
3. Validate sender identity and channel membership.
4. Recipients receive UDP `0x01` with their own connection ID in `target_id`.

---

## Known uncertainty summary

| Area | Confidence | Practical guidance |
| --- | --- | --- |
| Dialect A TCP packet layouts | High | Suitable as the primary implementation target |
| Join ordering | High | Welcome, then Joined to peers, then Exists to joiner |
| Leave `master_id` interpretation | Medium / ambiguous | Keep policy explicit/configurable |
| Duplicate joins | Medium / generation-dependent | Prefer a defined policy rather than implicit behavior |
| Rename duplicate broadcasts | Medium | Keep de-duplication/per-session behavior explicit |
| Dialect A UDP | High for layout, Medium for deployment details | Authenticate claimed sender against TCP identity |
| Dialect B UDP layouts | Medium-High | Keep separate from Dialect A |
| Dialect B Alias packet | Low / ambiguous | Keep Alias policy explicit |
| MooGame payload types `0x07`–`0x10` | High for framing | Keep unknown bodies opaque |
| Tracking types `0x14`–`0x17` | Medium for type recognition, Low for internal meaning | Do not invent structure without application evidence |

---

## Compact machine-readable mental model

```text
MooAPI TCP:
  TCP byte stream
  -> packet ID
  -> fixed fields + embedded u32-length blobs
  -> connection state
  -> session membership state
  -> ordered server effects

MooAPI UDP:
  one datagram = one packet
  -> dialect-specific header
  -> claimed sender ID
  -> authenticate against TCP peer identity
  -> validate session membership
  -> learn transient UDP endpoint
  -> relay only forms appropriate to that dialect

MooGame profile service:
  MooAPI message-to-server payload
  -> typed payload 0x0B..0x0E
  -> profile operation
```

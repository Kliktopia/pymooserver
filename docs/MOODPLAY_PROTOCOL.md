# mooDPlay compatibility control protocol

This document describes the mooDPlay compatibility extension carried by a normal
PyMooServer MooAPI connection. It is an application-layer control protocol: the
underlying MooAPI TCP framing, connection ID, session membership, and normal game
messages remain unchanged.

## Transport and identification

A mooDPlay client first completes the ordinary MooAPI connection/Hello exchange.
It then sends control traffic as reliable MooAPI **ToServer** messages on reserved
subchannel `32000`.

Control payloads begin with:

```text
bytes[4]  "DPSC"
u8        control version = 1
u8        opcode
bytes     opcode-specific payload
```

Control messages are TCP-only. A UDP ToServer message on subchannel `32000` is not
interpreted as mooDPlay control traffic. Ordinary UDP application traffic on other
subchannels keeps its normal MooAPI behavior.

The first valid control message must be `HELLO`. Until that succeeds, the
connection remains an ordinary/native MooAPI client and cannot use mooDPlay control
operations or the private mooDPlay session namespace.

## Capabilities

Capability bits are:

| Bit | Value | Meaning |
| ---: | ---: | --- |
| 0 | `0x00000001` | authoritative host |
| 1 | `0x00000002` | kick |
| 2 | `0x00000004` | maximum-player policy |
| 3 | `0x00000008` | password admission |
| 4 | `0x00000010` | host migration |

The current server advertises all five bits.

## Opcodes

### `0x01` HELLO — client to server

Payload is either empty or:

```text
u32  required_capabilities
```

If any required bit is unsupported, the server does not identify the connection as
mooDPlay and does not send `CAPS`. Otherwise the connection becomes a mooDPlay
client and the server replies with `CAPS`.

### `0x02` CAPS — server to client

```text
u32  supported_capabilities
```

### `0x03` CLAIM — client to server

CLAIM turns a newly-created private Moo session into a listed mooDPlay session.
Only the current underlying MooAPI session master may claim it.

```text
u32       session_id
u8        host_type
u32       max_users
bytes[16] application_guid
bytes     password
```

`host_type` values are:

| Value | Meaning |
| ---: | --- |
| `0` | no migration; host departure destroys the session |
| `1` | migrate; host authority moves to the first remaining MooAPI session member |

Other host-type values are invalid and are ignored.

`max_users == 0` means no additional mooDPlay application-level limit. The normal
server member cap still applies.

The GUID must match the private session name described below. A successful claim
causes `HOST_CHANGED` to be broadcast to that session.

### `0x04` KICK — client to server

```text
u32  session_id
u32  target_connection_id
```

Only the current mooDPlay host may use KICK. A non-zero target signs that member off
the specified session without closing its TCP connection. Target `0` signs off all
other current session members and leaves the host in place.

### `0x05` HOST_CHANGED — server to session

```text
u32  session_id
u32  host_connection_id
```

Sent after a successful CLAIM and whenever migrating host authority changes.

### `0x06` JOIN_AUTH — client to server

```text
u32  session_id
u16  password_length
bytes[password_length] password
```

The server checks that the claimed mooDPlay session still exists, is not closing,
has available capacity, and that the supplied password matches. Successful
authorization is scoped to that connection and session.

### `0x07` AUTH_RESULT — server to client

```text
u32  session_id
u8   accepted       # 0 or 1
u16  reason_length
bytes[reason_length] reason
```

An accepted result authorizes a subsequent ordinary MooAPI Join for that private
session. The authorization is consumed when the join succeeds. A passwordless
session does not require JOIN_AUTH before Join.

### `0x08` LIST — client to server

```text
bytes[16] application_guid
```

Returns claimed, non-closing sessions for that application GUID.

### `0x09` LIST_REPLY — server to client

```text
u16  record_count

repeat record_count times:
    u32  session_id
    u32  current_users
    u32  max_users
    u8   flags
    u16  visible_name_length
    bytes[visible_name_length] visible_name
```

Record flag bits are:

| Bit | Meaning |
| ---: | --- |
| 0 | session has a password |
| 1 | session is currently joinable |

Joinability reflects both the mooDPlay `max_users` policy and the server's normal
per-session member cap.

A directory can contain more sessions than fit in one normal MooAPI message. In
that case the reply is truncated at a complete-record boundary so the DPSC payload
remains within the configured MooAPI message-size limit; `record_count` describes
only the records actually included.

## Private session namespace

mooDPlay sessions use a private ordinary MooAPI session name:

```text
__mooDPlay_v1__<32 lowercase hexadecimal GUID characters>__<visible name>
```

For example, the 16-byte application GUID is represented by 32 hexadecimal ASCII
characters. The visible session name follows the second `__` separator.

This namespace prevents native MooAPI sessions from colliding with mooDPlay
sessions and separates session discovery by application GUID.

An identified mooDPlay connection may join only valid private mooDPlay names. An
ordinary/native MooAPI connection may not join those private names. The first member
creates an unclaimed private session and, as its underlying MooAPI master, must send
CLAIM before other mooDPlay clients may join it.

## Admission ordering

Password, capacity, namespace, and unclaimed-room authority checks occur **before**
the underlying MooAPI Join is committed. A rejected join therefore does not receive
a transient Welcome/Exists view and does not cause Joined/Left churn for existing
members.

For a password-protected room the normal sequence is:

```text
MooAPI Hello
DPSC HELLO -> CAPS
LIST -> LIST_REPLY            (optional discovery)
JOIN_AUTH -> AUTH_RESULT(1)
ordinary MooAPI Join
```

## Host departure

For `host_type = 1`, host authority follows the underlying MooAPI session's member
order: the first remaining member becomes both the MooAPI master and the mooDPlay
host, and `HOST_CHANGED` is broadcast.

For `host_type = 0`, host departure destroys the logical session by signing off all
remaining members from that MooAPI session.

## Scope

DPSC is a PyMooServer/mooDPlay compatibility layer carried over MooAPI. It does not
change the ordinary MooAPI packet IDs or UDP wire formats, and clients that never
send DPSC HELLO continue to behave as ordinary MooAPI clients on the same listener.

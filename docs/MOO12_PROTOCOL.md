# MOO1/MOO2 Protocol Guide

## Purpose

This document describes the current best understanding of the MOO1/MOO2 text-protocol family. It is intended as a practical interoperability specification for compatible clients and servers, not as a historical narrative.

MOO1 and MOO2 are documented together because they use the same line-oriented protocol family and connection model. MOO2 adds or makes greater use of features such as server-side profile/IMI operations rather than introducing a separate transport protocol.

Where behavior is uncertain, generation-dependent, or best treated as a compatibility convention, that is stated explicitly.

The guide is implementation-independent. It describes wire framing, state transitions, ordering, and client-visible semantics without depending on a particular server codebase.

## Conventions

The words **must**, **should**, and **may** are used in the ordinary engineering sense:

- **must**: required for known-compatible behavior;
- **should**: strongly recommended because clients appear to depend on it;
- **may**: optional or generation-dependent behavior.

Confidence labels are used where useful:

- **High** — behavior is consistent and should be treated as part of the protocol.
- **Medium** — behavior is strongly indicated but has known variation or incomplete coverage.
- **Low / ambiguous** — multiple behaviors exist or the exact rule is unclear.

Unless otherwise stated, text values are 8-bit strings interpreted in a Windows/Latin-1-compatible way. Channel names are case-sensitive. Profile filenames, sections, and keys are compared case-insensitively at the storage layer.

---

## MOO1 and MOO2 compatibility model

For interoperability, treat the family as one TCP text protocol with optional later features rather than as two unrelated protocols. A server should not require a client to announce whether it is “MOO1” or “MOO2” before accepting the common sign-on, channel, messaging, and connection-management commands.

Server-side profile operations are the clearest later extension in this guide. Clients that do not use them can otherwise participate in the same connection and channel model.

---

## MOO1/MOO2 text protocol

### Transport and framing

**Confidence: High**

The MOO1/MOO2 protocol uses TCP. Logical records are terminated by LF (`0x0A`). CR is not a required part of framing and should not be assumed to be stripped automatically.

A conventional command therefore looks like:

```text
command|arg1|arg2|\n
```

A trailing `|` is important for many commands. Without it, the final argument may effectively include the record-ending newline when processed by a historically compatible parser.

#### Tokenization quirk

The parser behaves like delimiter tokenization that collapses empty fields. In practical terms:

```text
a||b|
```

does not reliably represent an explicit empty token between `a` and `b`.

Do not design new MOO1/MOO2 commands around significant empty fields.

#### Prefix dispatch quirk

Command recognition is best modeled as fixed-prefix matching rather than strict comparison of the first token. This is especially noticeable for one-letter commands such as `m`, `c`, `b`, and `p`.

For compatibility, a server should identify the known command prefix first, then tokenize the record.

#### Practical record size

A practical MOO1/MOO2 record ceiling is approximately `0x3C0` bytes (960 bytes). Larger records should not be relied on for interoperability.

---

### Physical connections versus signed-on players

**Confidence: High**

A TCP connection exists before a player signs on.

Each connection therefore has at least two distinct identities:

- a **physical connection**: the socket itself;
- a **player ID**: non-zero only while signed on.

A connection may remain open with player ID `0`. A client may explicitly sign off and later sign on again over the same TCP connection.

This distinction affects:

- the `count` record;
- channel bookkeeping;
- some unusual anchor/master edge cases;
- signoff behavior.

---

### Initial server greeting

**Confidence: High**

When a new TCP connection is accepted, the expected sequence is:

1. update/broadcast the physical connection count;
2. send a version probe to the new connection;
3. send the message of the day to the new connection.

Records:

```text
count|N|
ver|1|
motd|TEXT|
```

`count|N|` represents the number of physical TCP connections, not the number of signed-on users.

Because the count is broadcast, existing clients may receive another `count|...|` whenever someone connects or disconnects.

---

### Player ID allocation

**Confidence: Medium-High**

The MOO1/MOO2 player ID counter behaves unusually:

1. increment the counter;
2. assign that value;
3. if the assigned value is greater than `100000`, reset the stored counter to zero.

Therefore the sequence includes:

```text
...
99999
100000
100001
1
2
...
```

IDs should not be assumed to be globally unique for the entire process lifetime.

---

### Channel identity

**Confidence: High**

MOO1/MOO2 channel names are case-sensitive byte/text strings.

`Lobby` and `lobby` are distinct channels.

Alias and channel fields should be kept within roughly 40 characters for compatibility with historical fixed-size storage.

---

### The MOO1/MOO2 channel anchor field

**Confidence: Medium-High**

Several server records contain a fourth numeric field often interpreted as a channel master or leader. A cleaner mental model for the MOO1/MOO2 text protocol is **channel anchor**.

It behaves like a stored coordination ID, but it does not maintain all the invariants expected from a modern “master player” abstraction.

Important consequences:

- the first suitable connection in physical connection order can influence the value;
- unsigned physical connections can participate in edge cases;
- anchor repair after a departure may select a connection whose current player ID is zero;
- if no replacement is found, existing stored anchor values may remain unchanged.

Clients should treat this number as MOO1/MOO2 channel state rather than assuming it always names a currently signed-on leader.

---

## Client-to-server commands

The examples below show the recommended trailing delimiter.

### `signon`

```text
signon|ALIAS|CHANNEL|
```

Valid only when the connection's current player ID is zero.

Normal response ordering:

1. `welcome` to the signing-on client;
2. `signon` broadcast to clients in the same channel, including the newcomer;
3. one `signed` snapshot back to the newcomer for each other matching channel record.

Server records:

```text
welcome|PLAYER_ID|ALIAS|ANCHOR
signon|PLAYER_ID|ALIAS|IP_NUMBER|ANCHOR
signed|PLAYER_ID|ALIAS|IP_NUMBER|ANCHOR
```

The `welcome`, `signon`, and `signed` records normally end in LF. A trailing pipe is not required in these server records.

#### `IP_NUMBER`

The IP field is a signed decimal representation of the 32-bit IPv4 address value in the byte order convention historically exposed by WinSock.

It is **not** dotted-decimal text.

Conceptually:

```text
IPv4 bytes -> interpret 4 bytes as little-endian u32 -> render as signed 32-bit decimal
```

A value with bit 31 set is shown as a negative decimal number.

---

### `signoff`

```text
signoff|
```

Valid while signed on.

The server clears the connection's player ID but leaves the TCP connection alive.

The channel receives approximately:

```text
signoff|PLAYER_ID|ALIAS|ANCHOR
```

A later `signon` on the same socket is possible.

#### Physical-disconnect variant

When a signed-on socket disappears rather than issuing an explicit signoff, the notification commonly has a trailing pipe:

```text
signoff|PLAYER_ID|ALIAS|ANCHOR|
```

This trailing-pipe difference is a compatibility quirk and should not be normalized away when reproducing exact behavior.

---

### `setalias`

```text
setalias|NEW_ALIAS|
```

The server updates the stored alias and sends an alias event to the current channel:

```text
alias|PLAYER_ID|NEW_ALIAS
```

A strict sign-on check is not part of the historical behavior; an unsigned connection can therefore produce unusual ID-zero alias events.

---

### `setchannel`

```text
setchannel|NEW_CHANNEL|
```

This is effectively a leave-and-rejoin operation.

Typical sequence:

1. repair the old channel anchor if necessary;
2. if the connection has a non-zero player ID, broadcast its old-channel `signoff`;
3. change the channel;
4. join the new channel;
5. broadcast the player's `signon` in the new channel;
6. send `signed` snapshots of existing members to the moving client.

Counter-intuitively, the normal `welcome` record is not sent during this channel change.

A sign-on guard should not be assumed for this command.

---

### `gotoempty`

```text
gotoempty|ROOT|
```

Valid for a signed-on player.

The server searches for an unoccupied channel by appending a signed 16-bit decimal suffix to `ROOT`.

The candidate order is equivalent to:

```text
ROOT0
ROOT1
...
ROOT32767
ROOT-32768
ROOT-32767
...
ROOT-1
```

Candidates that cannot fit the MOO1/MOO2 channel-size bound are unusable.

Once a channel is selected, the server sends:

```text
session|CHANNEL|
```

A significant wire quirk is that this `session` record may be sent **without an LF terminator**. The next server record can therefore immediately follow it in the TCP byte stream.

The player then leaves the previous channel and joins the selected channel, again without the normal `welcome` record.

---

### Channel message: `m`

```text
m|SUBCHANNEL|MESSAGE|
```

Server broadcast:

```text
m|SUBCHANNEL|PLAYER_ID|ALIAS|MESSAGE
```

Recipients are physical connections whose stored channel exactly matches the sender's channel. The sender is included.

Only the first token after the subchannel is the message body. The tokenizer does not provide an escaping mechanism for literal `|` characters inside the message.

There is no reliable historical sign-on guard; player ID zero is therefore possible in malformed or unusual flows.

---

### Coordinate-style message: `c`

```text
c|SUBCHANNEL|VALUE1|VALUE2|
```

Server broadcast:

```text
c|SUBCHANNEL|PLAYER_ID|ALIAS|VALUE1|VALUE2
```

The two final fields are application-defined coordinate/state values.

---

### Root-prefix broadcast: `b`

```text
b|CHANNEL_ROOT|MESSAGE|
```

The server sends a normal message-shaped event to every connection whose channel starts with `CHANNEL_ROOT` using case-sensitive prefix matching:

```text
m|0|PLAYER_ID|ALIAS|MESSAGE
```

This is broader than a single exact channel.

---

### Private message: `p`

```text
p|TARGET_PLAYER_ID|MESSAGE|
```

The target receives:

```text
m|0|SENDER_PLAYER_ID|SENDER_ALIAS|MESSAGE
```

No same-channel requirement should be assumed.

---

### Version/server-text query: `ver`

```text
ver|1|
```

Selector `1` produces:

```text
server|TEXT
```

with a single LF terminator.

Other selectors appear to be ignored silently.

---

## Server-side INI/IMI operations

### Overview

**Confidence: High for wire framing; Medium for filesystem naming policy**

The MOO1/MOO2 protocol exposes Windows-profile-style reads and writes through `getini` and `inistring`.

These commands are processed only after sign-on.

The most important compatibility quirk is that the server removes the **last character of each argument token unconditionally**.

Clients therefore commonly include one disposable character, usually a space, immediately before the pipe delimiter.

Correctly framed example:

```text
getini|version.ini |version |version |
```

The three logical values received by the profile layer are then:

```text
filename = version.ini
section  = version
key      = version
```

This apparently redundant space is intentional.

Without it:

```text
getini|version.ini|version|version|
```

would effectively become something like:

```text
filename = version.in
section  = versio
key      = versio
```

---

### Read

Client:

```text
getini|FILENAME<pad>|SECTION<pad>|KEY<pad>|
```

Server:

```text
inistring|VALUE |
```

There is exactly one compatibility space before the final pipe in the reply.

Missing files, sections, or keys conventionally return an empty value:

```text
inistring| |
```

---

### Write

Client:

```text
inistring|FILENAME<pad>|SECTION<pad>|KEY<pad>|VALUE<pad>|
```

The server removes one final character from each of the four tokens before writing.

No write acknowledgement is expected.

---

### Profile semantics

A compatible profile store should generally behave like a Windows INI/profile API rather than like a strict modern configuration parser.

Useful compatibility rules include:

- filename matching is case-insensitive;
- section matching is case-insensitive;
- key matching is case-insensitive;
- the first matching section wins;
- the first matching key in that section wins;
- comments and unknown/malformed lines should not make the whole file unreadable;
- surrounding profile whitespace is treated conservatively;
- matching single or double quotes surrounding a stored value may be removed on read;
- line endings may be CRLF, LF, CR, or mixed.

A common server-side convention is to store profiles physically as `.imi` files regardless of the extension supplied by the client. That is a storage convention rather than an additional wire field.

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
| Channel | case-sensitive 8-bit text |
| Alias | opaque 8-bit text |
| Profile filename | case-insensitive at storage layer |
| Profile section | case-insensitive |
| Profile key | case-insensitive |
| Message body | opaque 8-bit text/bytes within the line protocol |

Do not apply Unicode normalization to protocol names.

---

## Compatibility cautions

### Channel anchor is not a universal “master” abstraction

The fourth numeric field used by several MOO1/MOO2 records is best understood as stored channel coordination state. Its edge behavior should not be replaced with a stricter modern leader invariant unless compatibility testing specifically supports that change.

### Physical connection state is distinct from signed-on player state

The TCP socket can exist while the player ID is zero. Explicit signoff does not necessarily mean the socket closes, and a client can sign on again over the same connection.

---

## Recommended interoperability tests

### Basic sign-on

1. Connect TCP.
2. Expect `count`, `ver`, then `motd` for the new connection.
3. Send:

   ```text
   signon|Alice|Lobby|
   ```

4. Expect `welcome`.
5. Expect a `signon` event for Alice.

### Profile read

Given a profile containing logically:

```ini
[version]
version=4
```

send:

```text
getini|version.ini |version |version |
```

expect:

```text
inistring|4 |
```

The spaces in the request are significant compatibility padding.

---

## Known uncertainty summary

| Area | Confidence | Practical guidance |
| --- | --- | --- |
| Line framing and normal commands | High | Preserve LF framing and trailing-delimiter quirks |
| Server-side INI/IMI token trimming | High | Preserve one-character unframing; clients may depend on padding |
| Channel-anchor semantics | Medium-High | Treat as stored coordination state, not a strict leader invariant |
| Exact behavior of malformed/missing fields | Medium | Keep normal compatible traffic deterministic; avoid inventing new wire replies |

---

## Compact machine-readable mental model

For an automated agent implementing or reviewing MOO1/MOO2 behavior:

```text
TCP byte stream
-> LF-delimited records
-> fixed-prefix command recognition
-> pipe tokenization with collapsed empty tokens
-> physical connection state
-> optional signed-on player state
-> exact case-sensitive channel routing
-> optional server-side profile operations
```

Important invariants:

```text
physical connection != signed-on player
count = physical TCP connection count
channel name comparison = case-sensitive
profile lookup = case-insensitive filename/section/key
explicit signoff can leave socket open for a later signon
```

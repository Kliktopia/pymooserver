# Changelog

## 0.1.3

- Fixed MOO1/MOO2 initial protocol identification so valid first records larger than the ambiguity buffer are accepted, and leading blank records no longer cause misclassification.
- Force-abort stalled MOO1/MOO2 writers after bounded graceful-close attempts so dead/non-reading peers cannot remain as ghost channel members or block shutdown.
- Added the same bounded graceful-close/transport-abort fallback to MooAPI TCP writers.
- Reject profile section/key/value record injection and key names that cannot be represented as profile entries, and preserve existing POSIX file modes across atomic profile replacement.
- Normalize Windows-reserved and alternate-stream-style profile basenames portably, while continuing to use `.imi` backing files.
- Added platform-native default data roots: `C:\Moo\data` on Windows, XDG/`~/.local/share/pymooserver` on Linux, and `~/Library/Application Support/PyMooServer` on macOS.
- Applied the same platform-native profile-root selection to programmatic `ServerConfig()` use, not only the unified CLI, and restored the filesystem INI service when a fixed-port `MooServer` is restarted after `close()`.
- Added graceful SIGTERM handling and made the unified launcher explicitly cancel/await all listener, watchdog, and backup tasks it owns when any background task fails.
- MooAPI now permits empty message payloads, treats an exact duplicate Hello as idempotent, and still rejects conflicting repeated Hello packets.
- Split player/alias and session-name limits: ordinary names remain capped at 64 bytes while session names default to 255 bytes for namespaced applications such as mooDPlay.
- Increased the default join/leave/rename burst allowance to match the configured 32-session-per-connection scale while retaining sustained rate limiting.
- Disabled MooAPI application idle timeout and INI write throttling by default; both remain configurable. Explicit idle timeouts count successful outbound traffic as activity.
- Propagated `--identification-timeout` to MooAPI realms as well as MOO1/MOO2.
- Added regression coverage for both supported master-announcement policies without changing the existing `successor` default.
- Hardened mooDPlay admission so namespace/password/capacity checks occur before MooAPI join effects, only the underlying session master can claim an unclaimed room, and migrated host authority follows session membership order.
- Bounded mooDPlay directory replies to the normal MooAPI message cap, included the Hub member cap in joinability/auth decisions, rejected unknown host-policy values, and preserved ordinary UDP application callbacks outside the reserved DPSC control subchannel.
- Added explicit public DPSC/mooDPlay protocol documentation and expanded mooDPlay integration tests for claim/list/authentication, kick, host migration, no-migration destruction, GUID isolation, native MooAPI namespace isolation, capacity, and malformed host policy.
- Expanded CI to run the test suite on Windows and Ubuntu.
- Aligned package metadata and public version reporting on `0.1.3`, and moved license metadata to the current SPDX form used by modern setuptools.

## 0.1.2

- Added mooDPlay compatibility on normal MooAPI listeners, including GUID-scoped session discovery, host authority, passwords, max-player limits, kick, and host migration controls.

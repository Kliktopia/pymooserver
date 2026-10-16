"""Server-wide MooAPI ID allocator."""

from __future__ import annotations


class IdExhausted(RuntimeError):
    pass


class IdAllocator:
    """Allocate one monotonically increasing ID namespace for users and sessions.

    One server-wide counter supplies connection and session IDs alike, starts at 1,
    and IDs are never reused. Client code compares them as signed 32-bit values, so
    allocation stops at 2^31-1.
    """

    __slots__ = ("_next",)

    def __init__(self) -> None:
        self._next = 1

    def allocate(self) -> int:
        if self._next > 0x7FFFFFFF:
            raise IdExhausted("Moo ID space exhausted")
        value = self._next
        self._next += 1
        return value

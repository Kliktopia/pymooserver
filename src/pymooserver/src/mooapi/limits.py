"""Rate-limit primitives used by transports and services."""

from __future__ import annotations

from dataclasses import dataclass, field
from time import monotonic


@dataclass(slots=True)
class TokenBucket:
    rate: float
    capacity: float
    tokens: float = field(init=False)
    updated: float = field(default_factory=monotonic)

    def __post_init__(self) -> None:
        self.tokens = self.capacity

    def consume(self, amount: float) -> bool:
        if self.rate <= 0:
            return True
        now = monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
        self.updated = now
        if amount > self.tokens:
            return False
        self.tokens -= amount
        return True

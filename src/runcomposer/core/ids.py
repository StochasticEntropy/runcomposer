"""ULID minting for run and dispatch ids (DESIGN.md §3.1, §4).

Stdlib-only implementation of the ULID spec: 48-bit millisecond timestamp +
80 bits of randomness, Crockford base32, 26 characters, lexically sortable.
"""

from __future__ import annotations

import os
import time

_ENCODING = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_ulid(timestamp_ms: int | None = None, randomness: bytes | None = None) -> str:
    """Mint a ULID. Arguments exist for deterministic tests only."""
    ts = int(time.time() * 1000) if timestamp_ms is None else timestamp_ms
    if not 0 <= ts < (1 << 48):
        raise ValueError(f"timestamp out of ULID range: {ts}")
    rand = os.urandom(10) if randomness is None else randomness
    if len(rand) != 10:
        raise ValueError("randomness must be exactly 10 bytes")
    value = (ts << 80) | int.from_bytes(rand, "big")
    return "".join(_ENCODING[(value >> shift) & 0x1F] for shift in range(125, -1, -5))

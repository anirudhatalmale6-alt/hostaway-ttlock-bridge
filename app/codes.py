"""Guest code generation for the ``custom`` strategy.

Rules, in order of importance:
  * never collide with another code currently live on the same lock;
  * no trivial sequences (123456, 111111, 000000) -- guests photograph door
    keypads and wear patterns give away repeated digits;
  * cryptographically seeded, because a predictable code is a free key.
"""

from __future__ import annotations

import secrets

_BANNED_EXACT = {"000000", "111111", "123456", "654321", "112233", "123123"}


def _is_weak(code: str) -> bool:
    if code in _BANNED_EXACT:
        return True
    if len(set(code)) <= 2:
        return True
    digits = [int(c) for c in code]
    deltas = {b - a for a, b in zip(digits, digits[1:])}
    if deltas <= {1} or deltas <= {-1}:  # strictly ascending / descending run
        return True
    if code.startswith("0"):  # some keypads swallow a leading zero
        return True
    return False


def generate_code(length: int = 6, *, exclude: set[str] | None = None) -> str:
    """Return a numeric code of ``length`` digits not in ``exclude``."""
    if not 4 <= length <= 9:
        raise ValueError("TTLock passcodes are 4-9 digits")
    exclude = exclude or set()
    for _ in range(500):
        code = "".join(secrets.choice("0123456789") for _ in range(length))
        if _is_weak(code) or code in exclude:
            continue
        return code
    raise RuntimeError(
        f"could not find a free {length}-digit code after 500 tries; "
        "the lock may be saturated -- increase code_length"
    )

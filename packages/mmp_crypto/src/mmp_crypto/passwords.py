"""Password hashing.

argon2id, at parameters chosen for a login endpoint rather than for a
benchmark. This is the one place in the system where slowness is the feature —
it runs once per login, behind a rate limiter, and every millisecond it costs is
a millisecond an offline cracker pays per guess.

Contrast with `mmp_crypto.keys`, where the same reasoning inverts: an API key is
verified on every ingest request, so a deliberately slow hash there would be a
denial of service we built ourselves.
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from argon2.low_level import Type

# OWASP's current argon2id floor: 19 MiB, 2 iterations, 1 degree of parallelism.
# Memory cost is the parameter that actually hurts GPU attackers; raising
# time_cost alone mostly hurts us.
_HASHER = PasswordHasher(
    time_cost=2,
    memory_cost=19 * 1024,
    parallelism=1,
    hash_len=32,
    salt_len=16,
    type=Type.ID,
)

MIN_PASSWORD_LENGTH = 12
# Argon2 reads the whole input; an unbounded password is an unbounded amount of
# work for an unauthenticated caller to hand us.
MAX_PASSWORD_LENGTH = 1024


class PasswordPolicyError(ValueError):
    """Raised when a password cannot be accepted at all."""


def validate_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise PasswordPolicyError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise PasswordPolicyError("password is too long")


def hash_password(password: str) -> str:
    validate_password(password)
    return _HASHER.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    """Constant-time as far as the algorithm allows; never raises on mismatch."""
    if len(password) > MAX_PASSWORD_LENGTH:
        return False
    try:
        return _HASHER.verify(stored_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    """True when the stored hash used weaker parameters than we now require.

    Called after a successful login so that raising the cost parameters
    migrates existing users transparently, instead of leaving the oldest — and
    most valuable — accounts on the weakest settings forever.
    """
    try:
        return _HASHER.check_needs_rehash(stored_hash)
    except InvalidHashError:
        return True


# A fixed hash used to burn the same CPU time when the account does not exist.
# Without it, "no such user" returns in microseconds while a real user costs
# ~50ms, and the difference enumerates your customer list.
_DUMMY_HASH = _HASHER.hash("dummy-password-for-timing-equalisation")


def verify_password_constant_work(stored_hash: str | None, password: str) -> bool:
    if stored_hash is None:
        verify_password(_DUMMY_HASH, password)
        return False
    return verify_password(stored_hash, password)

"""
Password hashing (stdlib scrypt — no extra dependency) and the password policy for new passwords.

Stored format (self-describing, so cost parameters can be raised later without invalidating existing hashes):
    scrypt$<n>$<r>$<p>$<salt, base64>$<derived key, base64>

A user without a password (NULL hash) has the *empty default password*: signing in with an empty password is
accepted only to set a real one (see `loan_agent.auth.providers`).
"""
import base64
import hashlib
import hmac
import os
from typing import Optional

SCRYPT_N = 2 ** 14  # CPU/memory cost (tests lower this module attribute for speed; stored hashes keep their own)
SCRYPT_R = 8
SCRYPT_P = 1
_DKLEN = 32
_SALT_BYTES = 16
_SCHEME = "scrypt"

MAX_PASSWORD_LENGTH = 256  # bounds hashing work per request


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _derive(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=_DKLEN, maxmem=256 * n * r + 1024 * 1024
    )


def hash_password(password: str) -> str:
    salt = os.urandom(_SALT_BYTES)
    key = _derive(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    return f"{_SCHEME}${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(key)}"


def verify_password(password: str, stored_hash: Optional[str]) -> bool:
    """Constant-time check of `password` against a stored hash. A missing / malformed hash never matches."""
    if not stored_hash or password is None or len(password) > MAX_PASSWORD_LENGTH:
        return False
    try:
        scheme, n, r, p, salt_b64, key_b64 = stored_hash.split("$")
        if scheme != _SCHEME:
            return False
        expected = base64.b64decode(key_b64)
        actual = _derive(password, base64.b64decode(salt_b64), int(n), int(r), int(p))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


_DUMMY_HASH: Optional[str] = None


def burn_verification_time(password: str) -> None:
    """Performs a throwaway verification so unknown user IDs take as long to reject as wrong passwords."""
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password("not-a-real-password")
    verify_password(password or "", _DUMMY_HASH)


class PasswordPolicyError(ValueError):
    """The proposed new password does not meet the password policy."""


def validate_new_password(password: str, user_id: str, min_length: int) -> None:
    if password is None or not password.strip():
        raise PasswordPolicyError("The new password cannot be empty.")
    if len(password) < min_length:
        raise PasswordPolicyError(f"The new password must be at least {min_length} characters long.")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise PasswordPolicyError(f"The new password must be at most {MAX_PASSWORD_LENGTH} characters long.")
    if password.strip().lower() == (user_id or "").strip().lower():
        raise PasswordPolicyError("The new password cannot be the same as the user ID.")

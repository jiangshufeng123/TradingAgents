"""Password hashing with PBKDF2-SHA256 from the standard library (no extra dependency)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

ITERATIONS = 310_000


def hash_password(password: str) -> str:
    if len(password) < 8:
        raise ValueError("a password needs at least 8 characters")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iterations, salt, digest = stored.split("$")
        if scheme != "pbkdf2_sha256":
            return False
        check = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt), int(iterations))
        return hmac.compare_digest(check, base64.b64decode(digest))
    except (ValueError, TypeError):
        return False

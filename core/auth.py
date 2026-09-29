"""
Minimal auth: password hashing + JWT issuance/verification, stdlib only.

No PyJWT / passlib / bcrypt dependency -- this sandbox's pip is blocked by
policy (PyPI returns 403 here), and there's no reason to add a dependency
for something this small: a JWT is just base64url(header).base64url(payload)
signed with HMAC-SHA256, and pbkdf2_hmac (stdlib) is a perfectly reasonable
password hash for a single-tenant/small-team deployment.

    from core.auth import hash_password, verify_password, create_token, decode_token

    pw_hash = hash_password("secret")
    verify_password("secret", pw_hash)          # -> True

    token = create_token(user_id, email, role)
    claims = decode_token(token)                # raises AuthError if invalid/expired
    claims["sub"], claims["role"]

Secret key: AGENTICSTUDIO_JWT_SECRET env var. Falls back to a fixed dev
value with a loud warning -- fine for local development, NOT fine for any
real deployment (set the env var before exposing this past localhost).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import warnings
from typing import Optional

_PBKDF2_ITERATIONS = 260_000
_PBKDF2_ALGO = "sha256"
_TOKEN_TTL_SECONDS = 12 * 60 * 60  # 12h

_DEV_SECRET = "agenticstudio-dev-secret-CHANGE-ME"


class AuthError(RuntimeError):
    """Bad credentials, or an invalid/expired/tampered token."""


def _secret() -> bytes:
    secret = os.environ.get("AGENTICSTUDIO_JWT_SECRET")
    if not secret:
        warnings.warn(
            "AGENTICSTUDIO_JWT_SECRET is not set -- using a fixed development "
            "secret. Set this env var before exposing the API past localhost.",
            stacklevel=2,
        )
        secret = _DEV_SECRET
    return secret.encode("utf-8")


# ------------------------------------------------------------------ passwords

def hash_password(password: str) -> str:
    """Returns 'pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>'."""
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac(_PBKDF2_ALGO, password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        scheme, iterations_s, salt_hex, hash_hex = stored_hash.split("$")
        if scheme != "pbkdf2_sha256":
            return False
        iterations = int(iterations_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, AttributeError):
        return False
    dk = hashlib.pbkdf2_hmac(_PBKDF2_ALGO, password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(dk, expected)


# ------------------------------------------------------------------------ JWT

def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def create_token(user_id: str, email: str, role: str, ttl_seconds: int = _TOKEN_TTL_SECONDS) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    now = int(time.time())
    payload = {"sub": user_id, "email": email, "role": role, "iat": now, "exp": now + ttl_seconds}

    header_b64 = _b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    signature = hmac.new(_secret(), signing_input, hashlib.sha256).digest()
    sig_b64 = _b64url_encode(signature)
    return f"{header_b64}.{payload_b64}.{sig_b64}"


def decode_token(token: str) -> dict:
    try:
        header_b64, payload_b64, sig_b64 = token.split(".")
    except ValueError:
        raise AuthError("malformed token")

    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    expected_sig = hmac.new(_secret(), signing_input, hashlib.sha256).digest()
    try:
        actual_sig = _b64url_decode(sig_b64)
    except Exception:
        raise AuthError("malformed token signature")
    if not hmac.compare_digest(expected_sig, actual_sig):
        raise AuthError("invalid token signature")

    try:
        payload = json.loads(_b64url_decode(payload_b64))
    except Exception:
        raise AuthError("malformed token payload")

    if payload.get("exp") is not None and time.time() > payload["exp"]:
        raise AuthError("token expired")

    return payload


def register_user(email: str, password: str, role: str = "end_user") -> dict:
    """Create a user row (core.job_store) with a hashed password. Raises
    AuthError if the email is already taken or the role isn't recognized.

    The very first user ever registered is promoted to admin regardless of
    the requested role -- otherwise a fresh install has no way to reach the
    admin-only endpoints without direct DB access. Every registration after
    that gets exactly the role it asked for (default end_user)."""
    from core.job_store import create_user, get_user_by_email, list_users

    if role not in ("end_user", "admin"):
        raise AuthError(f"unknown role {role!r} -- must be 'end_user' or 'admin'")
    if get_user_by_email(email):
        raise AuthError(f"email {email!r} is already registered")

    if not list_users():
        role = "admin"

    user_id = create_user(email, hash_password(password), role=role)
    return {"id": user_id, "email": email, "role": role}


def authenticate(email: str, password: str) -> dict:
    """Verify credentials and return the user row, or raise AuthError."""
    from core.job_store import get_user_by_email

    row = get_user_by_email(email)
    if not row or not verify_password(password, row["password_hash"]):
        raise AuthError("invalid email or password")
    return row

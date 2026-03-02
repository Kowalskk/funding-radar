"""
app/services/auth_service.py — JWT + API key + password hashing utilities.

All functions are stateless pure helpers so they can be used from any
endpoint or background task without side effects.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
from jose import ExpiredSignatureError, JWTError, jwt

from app.config import get_settings


# ── Password ──────────────────────────────────────────────────────────────────

def hash_password(plain: str) -> str:
    """Hash a password using bcrypt. Raises ValueError if >72 bytes (bcrypt limit)."""
    encoded = plain.encode("utf-8")
    if len(encoded) > 72:
        raise ValueError("Password is too long. Must be at most 72 bytes in UTF-8.")
    return bcrypt.hashpw(encoded, bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """Verify a plain password against a bcrypt hash."""
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False



# ── API key ───────────────────────────────────────────────────────────────────

def generate_api_key() -> str:
    """Return a 64-char cryptographically random hex string."""
    return secrets.token_hex(32)


# ── JWT ───────────────────────────────────────────────────────────────────────

def _settings():
    return get_settings()


def create_access_token(
    subject: int | str,
    tier: str,
    expires_delta: timedelta | None = None,
    extra: dict[str, Any] | None = None,
) -> str:
    s = _settings()
    now = datetime.now(tz=timezone.utc)
    expire = now + (
        expires_delta or timedelta(minutes=s.jwt_access_token_expire_minutes)
    )
    payload: dict[str, Any] = {
        "sub": str(subject),
        "tier": tier,
        "iat": now,
        "exp": expire,
        "type": "access",
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, s.jwt_secret_key.get_secret_value(), algorithm=s.jwt_algorithm)


def create_refresh_token(subject: int | str) -> str:
    s = _settings()
    now = datetime.now(tz=timezone.utc)
    expire = now + timedelta(days=s.jwt_refresh_token_expire_days)
    payload = {
        "sub": str(subject),
        "iat": now,
        "exp": expire,
        "type": "refresh",
    }
    return jwt.encode(payload, s.jwt_secret_key.get_secret_value(), algorithm=s.jwt_algorithm)


def decode_token(token: str) -> dict[str, Any]:
    """Decode and validate a JWT. Raises JWTError or ExpiredSignatureError."""
    s = _settings()
    return jwt.decode(token, s.jwt_secret_key.get_secret_value(), algorithms=[s.jwt_algorithm])


def safe_decode_token(token: str) -> dict[str, Any] | None:
    """Decode without raising — returns None on any error."""
    try:
        return decode_token(token)
    except (JWTError, ExpiredSignatureError):
        return None

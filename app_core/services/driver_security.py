"""Primitivas de autenticação do aplicativo do motorista.

As senhas e os tokens nunca são persistidos em texto puro. Senhas usam PBKDF2
com salt individual; sessões persistem somente SHA-256 do token bearer.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone


PASSWORD_SCHEME = "pbkdf2_sha256"
PASSWORD_ITERATIONS = 260_000
DEFAULT_DRIVER_PASSWORD = "123"
DEFAULT_SESSION_HOURS = 24


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(value: datetime | None = None) -> str:
    current = value or utc_now()
    return current.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def hash_driver_password(password: str) -> str:
    raw = str(password or "")
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", raw.encode("utf-8"), salt.encode("utf-8"), PASSWORD_ITERATIONS
    ).hex()
    return f"{PASSWORD_SCHEME}${PASSWORD_ITERATIONS}${salt}${digest}"


def verify_driver_password(password: str, stored_hash: str) -> bool:
    stored = str(stored_hash or "")
    try:
        scheme, rounds, salt, expected = stored.split("$", 3)
        if scheme != PASSWORD_SCHEME:
            return False
        calculated = hashlib.pbkdf2_hmac(
            "sha256",
            str(password or "").encode("utf-8"),
            salt.encode("utf-8"),
            int(rounds),
        ).hex()
        return hmac.compare_digest(calculated, expected)
    except (TypeError, ValueError):
        return False


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def hash_session_token(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def session_expiry(hours: int = DEFAULT_SESSION_HOURS) -> str:
    return utc_iso(utc_now() + timedelta(hours=max(1, int(hours))))


def is_expired(expires_at: str) -> bool:
    try:
        parsed = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
        return parsed <= utc_now()
    except (TypeError, ValueError):
        return True

"""Local owner credentials and opaque, revocable database sessions."""

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy.orm import Session, sessionmaker

from futures_bot.persistence.models import AuditEvent
from futures_bot.web.models import WebOwner


@dataclass(frozen=True)
class WebAuthSettings:
    allowed_origins: tuple[str, ...]
    cookie_secure: bool = True
    local_mode: bool = False
    session_ttl_seconds: int = 28800
    frontend_dist: Path | None = None

    def __post_init__(self) -> None:
        if not self.allowed_origins:
            raise ValueError("at least one exact allowed origin is required")
        if not 60 <= self.session_ttl_seconds <= 86400:
            raise ValueError("session lifetime must be between 60 and 86400 seconds")
        for origin in self.allowed_origins:
            parsed = urlsplit(origin)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.path
                or parsed.query
                or parsed.fragment
                or origin != f"{parsed.scheme}://{parsed.netloc}"
            ):
                raise ValueError("origins must be exact http(s) scheme://host[:port] values")
            if parsed.scheme != "https" or not self.cookie_secure:
                if not self.local_mode or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
                    raise ValueError("insecure HTTP/cookies require explicit loopback local mode")


def hash_password(password: str) -> str:
    if not isinstance(password, str) or not 14 <= len(password) <= 1024:
        raise ValueError("password must contain between 14 and 1024 characters")
    salt = secrets.token_hex(16)
    digest = hashlib.scrypt(
        password.encode(), salt=bytes.fromhex(salt), n=32768, r=8, p=1, maxmem=64 * 1024 * 1024
    )
    return f"scrypt$32768$8$1${salt}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt, digest = encoded.split("$")
        if (algorithm, n, r, p) != ("scrypt", "32768", "8", "1"):
            return False
        actual = hashlib.scrypt(
            password.encode(),
            salt=bytes.fromhex(salt),
            n=32768,
            r=8,
            p=1,
            maxmem=64 * 1024 * 1024,
        )
        return hmac.compare_digest(actual.hex(), digest)
    except (ValueError, TypeError):
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def bootstrap_owner(session_factory: sessionmaker[Session], username: str, password: str) -> None:
    """Create the sole owner once; deliberately cannot reset an existing credential."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", username):
        raise ValueError(
            "username must contain 1–64 ASCII letters, digits, dots, dashes or underscores"
        )
    encoded = hash_password(password)
    with session_factory.begin() as session:
        if session.get(WebOwner, 1) is not None:
            raise ValueError("owner already bootstrapped")
        session.add(WebOwner(id=1, username=username, password_hash=encoded))
        session.add(
            AuditEvent(
                actor=username,
                action="web.owner_bootstrapped",
                resource_type="web_owner",
                resource_id="1",
                details={},
            )
        )

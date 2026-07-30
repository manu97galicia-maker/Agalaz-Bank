"""Single-user authentication for a panel that is exposed to the internet.

Design notes, because this guards a wallet:

* The password is compared against a PBKDF2-SHA256 hash, never a plaintext
  env var (a plaintext ``PANEL_PASSWORD`` is accepted for first-run convenience
  but is hashed at import and the panel nags you to replace it).
* The session cookie is an HMAC-signed ``expiry.nonce`` token. It is not derived
  from the password, so rotating the password does not silently keep old
  sessions alive only if you also rotate the secret -- ``revoke_all()`` does
  that explicitly.
* Failed logins are rate-limited per IP with a growing lockout, because a panel
  on a public IP will get credential-stuffed within hours.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from dataclasses import dataclass, field

from . import config

COOKIE = "sniper_session"
_PBKDF2_ROUNDS = 240_000

# Lockout schedule: after N consecutive failures, block for this many seconds.
_LOCKOUT_STEPS = ((5, 60), (8, 300), (12, 1800), (20, 21600))


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    """Return a ``pbkdf2_sha256$rounds$salt$hash`` string."""
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${_PBKDF2_ROUNDS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time check of a password against an encoded hash."""
    try:
        algo, rounds, salt_hex, digest_hex = encoded.split("$")
    except ValueError:
        return False
    if algo != "pbkdf2_sha256":
        return False
    try:
        candidate = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), int(rounds)
        )
    except ValueError:
        return False
    return hmac.compare_digest(candidate.hex(), digest_hex)


@dataclass
class _Attempts:
    failures: int = 0
    blocked_until: float = 0.0


@dataclass
class Auth:
    """Holds the credential material and the per-IP failure counters."""

    user: str
    password_hash: str
    secret: bytes
    pin_hash: str = ""
    warnings: list[str] = field(default_factory=list)
    _attempts: dict[str, _Attempts] = field(default_factory=dict)

    @classmethod
    def from_config(cls) -> Auth:
        warnings: list[str] = []
        password_hash = config.PANEL_PASSWORD_HASH.strip()
        if not password_hash and config.PANEL_PASSWORD:
            password_hash = hash_password(config.PANEL_PASSWORD)
            warnings.append(
                "PANEL_PASSWORD esta en texto plano en .env. Genera un hash con "
                "`python -m server.hashpw` y usa PANEL_PASSWORD_HASH."
            )
        pin_hash = config.PANEL_PIN_HASH.strip()
        if not pin_hash and config.PANEL_PIN:
            pin_hash = hash_password(config.PANEL_PIN)
            warnings.append(
                "PANEL_PIN esta en texto plano en .env. Genera un hash con "
                "`python -m server.hashpw --pin` y usa PANEL_PIN_HASH."
            )
        secret_raw = config.SESSION_SECRET.strip()
        if not secret_raw:
            # Ephemeral secret: works, but every restart logs you out.
            secret_raw = secrets.token_hex(32)
            warnings.append(
                "PANEL_SESSION_SECRET no esta definido: se usa uno temporal y "
                "cada reinicio cierra la sesion."
            )
        if not config.PANEL_USER or not password_hash:
            warnings.append(
                "Sin PANEL_USER / PANEL_PASSWORD_HASH el panel queda cerrado a cal "
                "y canto (ningun login sera valido)."
            )
        return cls(
            user=config.PANEL_USER,
            password_hash=password_hash,
            secret=secret_raw.encode(),
            pin_hash=pin_hash,
            warnings=warnings,
        )

    # -- credentials -------------------------------------------------------- #

    @property
    def configured(self) -> bool:
        return bool((self.user and self.password_hash) or self.pin_hash)

    def _lockout_remaining(self, ip: str) -> int:
        entry = self._attempts.get(ip)
        if entry is None:
            return 0
        return max(0, int(entry.blocked_until - time.time()))

    def check(self, ip: str, user: str, password: str) -> tuple[bool, str]:
        """Validate credentials. Returns ``(ok, message)``."""
        remaining = self._lockout_remaining(ip)
        if remaining:
            return False, f"Demasiados intentos. Espera {remaining}s."
        if not self.configured:
            return False, "El panel no tiene credenciales configuradas."

        ok_user = hmac.compare_digest(user or "", self.user)
        ok_password = self.password_hash and verify_password(password or "", self.password_hash)
        if ok_user and ok_password:
            self._attempts.pop(ip, None)
            return True, ""
        return self._fail(ip, "Usuario o contrasena incorrectos.")

    def check_pin(self, ip: str, pin: str) -> tuple[bool, str]:
        """Validate a short PIN login. Same lockout schedule as the password."""
        remaining = self._lockout_remaining(ip)
        if remaining:
            return False, f"Demasiados intentos. Espera {remaining}s."
        if not self.pin_hash:
            return False, "No hay PIN configurado."
        if verify_password(pin or "", self.pin_hash):
            self._attempts.pop(ip, None)
            return True, ""
        return self._fail(ip, "PIN incorrecto.")

    def _fail(self, ip: str, message: str) -> tuple[bool, str]:
        """Record a failed attempt and apply the progressive lockout."""
        entry = self._attempts.setdefault(ip, _Attempts())
        entry.failures += 1
        for threshold, seconds in reversed(_LOCKOUT_STEPS):
            if entry.failures >= threshold:
                entry.blocked_until = time.time() + seconds
                break
        return False, message

    # -- sessions ----------------------------------------------------------- #

    def issue_token(self) -> str:
        """Mint a signed session token valid for the configured TTL."""
        expiry = int(time.time()) + config.SESSION_TTL_SECONDS
        payload = f"{expiry}.{secrets.token_hex(12)}"
        signature = hmac.new(self.secret, payload.encode(), hashlib.sha256).hexdigest()
        return f"{payload}.{signature}"

    def token_valid(self, token: str | None) -> bool:
        """True if the token signature matches and it has not expired."""
        if not token:
            return False
        parts = token.split(".")
        if len(parts) != 3:
            return False
        expiry_raw, nonce, signature = parts
        payload = f"{expiry_raw}.{nonce}"
        expected = hmac.new(self.secret, payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return False
        try:
            return int(expiry_raw) > time.time()
        except ValueError:
            return False

    def revoke_all(self) -> None:
        """Invalidate every outstanding session by rotating the signing key."""
        self.secret = os.urandom(32)

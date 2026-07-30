"""Autenticación multiusuario para un panel expuesto a internet.

Guarda un wallet, así que las decisiones importan:

* Las contraseñas se comparan contra un hash PBKDF2-SHA256, nunca contra texto
  plano. Los usuarios (Manu, Ricardo…) y sus cargos viven en ``state/users.json``
  (ver :mod:`server.users`), fuera del repo.
* La cookie de sesión es un token ``expiry.usuario.nonce`` firmado con HMAC. Al
  llevar el usuario dentro, el panel sabe quién eres (y qué cargo mostrar) sin
  guardar estado de sesión en disco.
* Los fallos de login se limitan por IP con bloqueo creciente.
* "Recuérdame" solo cambia cuánto dura el token y si la cookie es persistente;
  no relaja ninguna comprobación.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from dataclasses import dataclass, field

from . import config

COOKIE = "sniper_session"
_PBKDF2_ROUNDS = 240_000

# Bloqueo: tras N fallos seguidos, bloquea estos segundos.
_LOCKOUT_STEPS = ((5, 60), (8, 300), (12, 1800), (20, 21600))


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    """Devuelve ``pbkdf2_sha256$rounds$salt$hash``."""
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${_PBKDF2_ROUNDS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    """Comprobación en tiempo constante de una contraseña contra su hash."""
    try:
        algo, rounds, salt_hex, digest_hex = encoded.split("$")
    except (ValueError, AttributeError):
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
    """Material de firma de sesión + contadores de fallo por IP."""

    secret: bytes
    pin_hash: str = ""
    warnings: list[str] = field(default_factory=list)
    _attempts: dict[str, _Attempts] = field(default_factory=dict)

    @classmethod
    def from_config(cls) -> Auth:
        from . import users  # import tardío: users importa auth

        warnings: list[str] = []

        pin_hash = config.PANEL_PIN_HASH.strip()
        if not pin_hash and config.PANEL_PIN:
            pin_hash = hash_password(config.PANEL_PIN)
            warnings.append(
                "PANEL_PIN esta en texto plano en .env. Genera un hash con "
                "`python -m server.hashpw --pin` y usa PANEL_PIN_HASH."
            )

        secret_raw = config.SESSION_SECRET.strip()
        if not secret_raw:
            secret_raw = secrets.token_hex(32)
            warnings.append(
                "PANEL_SESSION_SECRET no esta definido: se usa uno temporal y "
                "cada reinicio cierra la sesion."
            )

        users.seed_if_empty()
        if not users.list_users():
            warnings.append(
                "No hay usuarios configurados. Crea uno con "
                '`python -m server.users add Manu "Chief Trololo Officer"` o '
                "pon PANEL_SEED_PASSWORD en el .env."
            )
        return cls(secret=secret_raw.encode(), pin_hash=pin_hash, warnings=warnings)

    # -- credenciales ------------------------------------------------------- #

    def _lockout_remaining(self, ip: str) -> int:
        entry = self._attempts.get(ip)
        if entry is None:
            return 0
        return max(0, int(entry.blocked_until - time.time()))

    def check(self, ip: str, user: str, password: str) -> tuple[bool, str, str]:
        """Valida usuario+contraseña. Devuelve ``(ok, mensaje, nombre_usuario)``."""
        from . import users  # import tardío

        remaining = self._lockout_remaining(ip)
        if remaining:
            return False, f"Demasiados intentos. Espera {remaining}s.", ""
        record = users.check_password(user, password)
        if record:
            self._attempts.pop(ip, None)
            return True, "", record["user"]
        ok, msg = self._fail(ip, "Usuario o contraseña incorrectos.")
        return ok, msg, ""

    def check_pin(self, ip: str, pin: str) -> tuple[bool, str, str]:
        """Login por PIN corto. Entra como el primer usuario (el dueño)."""
        from . import users  # import tardío

        remaining = self._lockout_remaining(ip)
        if remaining:
            return False, f"Demasiados intentos. Espera {remaining}s.", ""
        if not self.pin_hash:
            return False, "No hay PIN configurado.", ""
        if verify_password(pin or "", self.pin_hash):
            self._attempts.pop(ip, None)
            owners = users.list_users()
            return True, "", (owners[0]["user"] if owners else "Manu")
        ok, msg = self._fail(ip, "PIN incorrecto.")
        return ok, msg, ""

    def note_failure(self, ip: str) -> None:
        self._fail(ip, "")

    def _fail(self, ip: str, message: str) -> tuple[bool, str]:
        entry = self._attempts.setdefault(ip, _Attempts())
        entry.failures += 1
        for threshold, seconds in reversed(_LOCKOUT_STEPS):
            if entry.failures >= threshold:
                entry.blocked_until = time.time() + seconds
                break
        return False, message

    # -- sesiones ----------------------------------------------------------- #

    def issue_token(self, user: str, ttl_seconds: int) -> str:
        """Firma un token de sesión que lleva el usuario dentro."""
        expiry = int(time.time()) + ttl_seconds
        user_b64 = base64.urlsafe_b64encode(user.encode()).decode().rstrip("=")
        payload = f"{expiry}.{user_b64}.{secrets.token_hex(9)}"
        signature = hmac.new(self.secret, payload.encode(), hashlib.sha256).hexdigest()
        return f"{payload}.{signature}"

    def token_valid(self, token: str | None) -> bool:
        return self.token_user(token) is not None

    def token_user(self, token: str | None) -> str | None:
        """Devuelve el usuario si el token es válido y no ha caducado, si no None."""
        if not token:
            return None
        parts = token.split(".")
        if len(parts) != 4:
            return None
        expiry_raw, user_b64, nonce, signature = parts
        payload = f"{expiry_raw}.{user_b64}.{nonce}"
        expected = hmac.new(self.secret, payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        try:
            if int(expiry_raw) <= time.time():
                return None
        except ValueError:
            return None
        try:
            padding = "=" * (-len(user_b64) % 4)
            return base64.urlsafe_b64decode(user_b64 + padding).decode()
        except (ValueError, UnicodeDecodeError):
            return None

    def revoke_all(self) -> None:
        self.secret = os.urandom(32)

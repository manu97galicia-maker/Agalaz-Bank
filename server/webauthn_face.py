"""Login por cara/huella (Face ID, Windows Hello, huella Android) con WebAuthn.

Es **additivo y fail-closed**: es una vía extra para entrar, nunca un atajo que
salte la contraseña. Cualquier fallo de verificación deja fuera (se cae a la
contraseña @Otito123, que siempre funciona). Requiere HTTPS y un dominio real
(localhost vale para probar; una IP pelada NO — WebAuthn no lo permite).

Passkeys guardadas en ``state/passkeys.json`` por usuario. Los "challenges" (el
reto que firma el dispositivo) viven en memoria un par de minutos.

Todo el trabajo criptográfico lo hace la librería ``webauthn`` (py_webauthn),
que está probada; aquí solo orquestamos y persistimos. Import perezoso: si no
está instalada, el panel sigue y el botón de cara dice "no disponible".
"""

from __future__ import annotations

import base64
import json
import secrets
import time
from typing import Any

from . import config

_CHALLENGE_TTL = 180
#: handle -> {"challenge": bytes, "user": str, "kind": str, "exp": float}
_CHALLENGES: dict[str, dict[str, Any]] = {}


class FaceError(RuntimeError):
    """Face ID no disponible o verificación fallida (se cae a contraseña)."""


def available() -> bool:
    try:
        import webauthn  # noqa: F401, PLC0415

        return True
    except ImportError:
        return False


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64u(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# --------------------------------------------------------------------------- #
# Persistencia de passkeys                                                     #
# --------------------------------------------------------------------------- #


def _load() -> dict[str, list[dict[str, Any]]]:
    if not config.PASSKEYS_FILE.is_file():
        return {}
    try:
        return json.loads(config.PASSKEYS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save(data: dict[str, list[dict[str, Any]]]) -> None:
    config.PASSKEYS_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.PASSKEYS_FILE.write_text(json.dumps(data, indent=1), encoding="utf-8")


def has_passkey(user: str) -> bool:
    return bool(_load().get(user.lower()))


def _rp_id(host: str) -> str:
    return (config.RP_ID or host.split(":")[0]).strip()


def _origin(host: str, secure: bool) -> str:
    scheme = "https" if secure else "http"
    return f"{scheme}://{host}"


def _expected_origins(host: str, secure: bool) -> list[str]:
    """Orígenes aceptables al verificar una passkey.

    Detrás del proxy de Vercel el droplet ve su propio ``Host`` (la IP), no el
    dominio del navegador, así que derivar el origen del ``Host`` da un valor
    que nunca coincide con el ``clientDataJSON.origin`` real. Por eso se aceptan,
    en orden: el ``PANEL_RP_ORIGIN`` explícito, el derivado de ``PANEL_RP_ID``
    (``https://<rp_id>``) y, como último recurso, el del ``Host`` de la petición
    (que basta en acceso directo por dominio, sin proxy).
    """
    candidates = [
        config.RP_ORIGIN.rstrip("/"),
        f"https://{config.RP_ID}" if config.RP_ID else "",
        _origin(host, secure),
    ]
    out: list[str] = []
    for origin in candidates:
        if origin and origin not in out:
            out.append(origin)
    return out


def _new_challenge(user: str, kind: str, challenge: bytes) -> str:
    _expire()
    handle = secrets.token_urlsafe(12)
    _CHALLENGES[handle] = {
        "challenge": challenge,
        "user": user,
        "kind": kind,
        "exp": time.time() + _CHALLENGE_TTL,
    }
    return handle


def _pop_challenge(handle: str, kind: str) -> dict[str, Any]:
    _expire()
    entry = _CHALLENGES.pop(handle, None)
    if not entry or entry["kind"] != kind:
        raise FaceError("El reto caducó o no existe. Reintenta.")
    return entry


def _expire() -> None:
    now = time.time()
    for k in [k for k, v in _CHALLENGES.items() if v["exp"] < now]:
        _CHALLENGES.pop(k, None)


# --------------------------------------------------------------------------- #
# Registro (dar de alta la cara/huella; requiere estar ya logueado)            #
# --------------------------------------------------------------------------- #


def register_options(user: str, host: str, secure: bool) -> dict[str, Any]:
    try:
        from webauthn import generate_registration_options, options_to_json  # noqa: PLC0415
        from webauthn.helpers.structs import (  # noqa: PLC0415
            AuthenticatorSelectionCriteria,
            ResidentKeyRequirement,
            UserVerificationRequirement,
        )
    except ImportError as exc:
        raise FaceError("La librería 'webauthn' no está instalada en el servidor.") from exc

    options = generate_registration_options(
        rp_id=_rp_id(host),
        rp_name=config.RP_NAME,
        user_name=user,
        user_id=user.encode(),
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
    )
    handle = _new_challenge(user, "register", options.challenge)
    return {"handle": handle, "options": json.loads(options_to_json(options))}


def register_verify(handle: str, credential: dict[str, Any], host: str, secure: bool) -> dict[str, Any]:
    try:
        from webauthn import verify_registration_response  # noqa: PLC0415
    except ImportError as exc:
        raise FaceError("La librería 'webauthn' no está instalada.") from exc

    entry = _pop_challenge(handle, "register")
    try:
        verification = verify_registration_response(
            credential=credential,
            expected_challenge=entry["challenge"],
            expected_rp_id=_rp_id(host),
            expected_origin=_expected_origins(host, secure),
        )
    except Exception as exc:  # noqa: BLE001 -- fail-closed: cualquier error = no alta
        raise FaceError(f"No pude verificar el registro: {exc}") from exc

    user = entry["user"]
    data = _load()
    creds = data.setdefault(user.lower(), [])
    creds.append(
        {
            "credential_id": _b64u(verification.credential_id),
            "public_key": _b64u(verification.credential_public_key),
            "sign_count": verification.sign_count,
        }
    )
    _save(data)
    return {"registered": True, "user": user}


# --------------------------------------------------------------------------- #
# Login por cara/huella                                                        #
# --------------------------------------------------------------------------- #


def login_options(user: str, host: str, secure: bool) -> dict[str, Any]:
    try:
        from webauthn import generate_authentication_options, options_to_json  # noqa: PLC0415
        from webauthn.helpers.structs import (  # noqa: PLC0415
            PublicKeyCredentialDescriptor,
            UserVerificationRequirement,
        )
    except ImportError as exc:
        raise FaceError("La librería 'webauthn' no está instalada.") from exc

    creds = _load().get(user.lower(), [])
    if not creds:
        raise FaceError("Ese usuario no tiene cara/huella dada de alta. Entra con contraseña y actívala.")
    options = generate_authentication_options(
        rp_id=_rp_id(host),
        allow_credentials=[
            PublicKeyCredentialDescriptor(id=_unb64u(c["credential_id"])) for c in creds
        ],
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    handle = _new_challenge(user, "login", options.challenge)
    return {"handle": handle, "options": json.loads(options_to_json(options))}


def login_verify(handle: str, credential: dict[str, Any], host: str, secure: bool) -> str:
    """Verifica el login facial y devuelve el nombre de usuario (o lanza)."""
    try:
        from webauthn import verify_authentication_response  # noqa: PLC0415
    except ImportError as exc:
        raise FaceError("La librería 'webauthn' no está instalada.") from exc

    entry = _pop_challenge(handle, "login")
    user = entry["user"]
    data = _load()
    creds = data.get(user.lower(), [])
    raw_id = credential.get("rawId") or credential.get("id")
    stored = next((c for c in creds if c["credential_id"] == raw_id), None)
    if stored is None:
        raise FaceError("Esa passkey no está registrada para este usuario.")

    try:
        verification = verify_authentication_response(
            credential=credential,
            expected_challenge=entry["challenge"],
            expected_rp_id=_rp_id(host),
            expected_origin=_expected_origins(host, secure),
            credential_public_key=_unb64u(stored["public_key"]),
            credential_current_sign_count=stored["sign_count"],
            require_user_verification=True,
        )
    except Exception as exc:  # noqa: BLE001 -- fail-closed
        raise FaceError(f"No pude verificar la cara/huella: {exc}") from exc

    stored["sign_count"] = verification.new_sign_count
    _save(data)
    return user

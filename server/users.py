"""Usuarios administradores del panel (multiusuario).

Cada usuario tiene nombre, un **cargo** (que se ve al lado del nombre dentro de
la app) y un hash de contraseña. Viven en ``state/users.json`` (gitignored), así
que ninguna contraseña acaba nunca en el repo.

Se puede sembrar en el primer arranque con ``PANEL_SEED_PASSWORD`` (Manu y
Ricardo con esa contraseña) o gestionarlos a mano:

    python -m server.users list
    python -m server.users add Ricardo "Chief Technological Trololo Officer"
"""

from __future__ import annotations

import json
import sys
from typing import Any

from . import config
from .auth import hash_password, verify_password


def _load_raw() -> list[dict[str, Any]]:
    path = config.USERS_FILE
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def _save(users: list[dict[str, Any]]) -> None:
    config.USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.USERS_FILE.write_text(json.dumps(users, indent=1, ensure_ascii=False), encoding="utf-8")


def seed_if_empty() -> None:
    """Primer arranque: si no hay usuarios y hay ``PANEL_SEED_PASSWORD``, crea a
    Manu y Ricardo con sus cargos por defecto. La contraseña nunca está en el
    código: sale de la variable de entorno."""
    if _load_raw() or not config.SEED_PASSWORD:
        return
    pw_hash = hash_password(config.SEED_PASSWORD)
    users = [
        {"user": name.capitalize(), "role": role, "password_hash": pw_hash}
        for name, role in config.DEFAULT_ROLES.items()
    ]
    _save(users)


def list_users() -> list[dict[str, Any]]:
    """Usuarios sin el hash (para pintar en la UI)."""
    return [{"user": u["user"], "role": u.get("role", "")} for u in _load_raw()]


def find(name: str) -> dict[str, Any] | None:
    """Busca un usuario por nombre, sin distinguir mayúsculas."""
    name = (name or "").strip().lower()
    for u in _load_raw():
        if u.get("user", "").lower() == name:
            return u
    return None


def role_of(name: str) -> str:
    user = find(name)
    return user.get("role", "") if user else ""


def check_password(name: str, password: str) -> dict[str, Any] | None:
    """Devuelve el usuario si la contraseña es correcta, si no None."""
    user = find(name)
    if user and verify_password(password or "", user.get("password_hash", "")):
        return user
    return None


def upsert(name: str, role: str, password: str) -> None:
    """Crea o actualiza un usuario."""
    users = _load_raw()
    for u in users:
        if u["user"].lower() == name.strip().lower():
            u["role"] = role
            if password:
                u["password_hash"] = hash_password(password)
            _save(users)
            return
    users.append({"user": name.strip(), "role": role, "password_hash": hash_password(password)})
    _save(users)


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #


def _cli() -> int:
    import getpass

    args = sys.argv[1:]
    if not args or args[0] == "list":
        for u in list_users():
            print(f"  {u['user']:<12} — {u['role']}")
        if not list_users():
            print("  (sin usuarios)")
        return 0
    if args[0] == "add" and len(args) >= 3:
        name, role = args[1], args[2]
        pw = getpass.getpass(f"Contraseña para {name}: ")
        if len(pw) < 6:
            print("Contraseña demasiado corta.", file=sys.stderr)
            return 1
        upsert(name, role, pw)
        print(f"Guardado {name} — {role}")
        return 0
    print('Uso: python -m server.users [list | add <nombre> "<cargo>"]', file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(_cli())

"""Listas de wallets que el bot vigila: developers a copiar, traders buenos,
blacklist. Son los mismos ``top_devs_*.json`` / ``blacklist_devs.json`` del repo
del bot, así que editarlos aquí cambia a quién compra el bot de verdad.

Dos formas de fichero conviven en el repo y respetamos ambas al escribir:

* ``{"wallets": [{"wallet": "...", ...notas}, ...]}``  (listas de devs)
* ``{"wallets": ["addr", "addr", ...]}``               (blacklist)

Nunca reescribimos las notas existentes de una wallet: sólo añadimos o quitamos
entradas, y conservamos ``_README`` y demás metadatos tal cual.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from . import config

_ADDR_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
#: Ficheros de lista que el panel puede tocar dentro del repo del bot.
_LIST_GLOBS = ("top_devs*.json", "blacklist_devs.json")
_SAFE_FILE = re.compile(r"^[A-Za-z0-9._-]{1,64}\.json$")


class ListError(RuntimeError):
    """Una lista o dirección que no cuadra."""


def _repo() -> Path:
    if not config.BOT_REPO.is_dir():
        raise ListError(f"No encuentro el repo del bot en {config.BOT_REPO}")
    return config.BOT_REPO


def _valid_addr(address: str) -> str:
    address = (address or "").strip()
    if not _ADDR_RE.match(address):
        raise ListError(f"Dirección de wallet inválida: {address!r}")
    return address


def _path(filename: str) -> Path:
    filename = (filename or "").strip()
    if not filename.endswith(".json"):
        filename += ".json"
    if not _SAFE_FILE.match(filename):
        raise ListError(f"Nombre de lista inválido: {filename!r}")
    return _repo() / filename


def _addr_of(entry: Any) -> str:
    """Saca la dirección tanto si la entrada es string como objeto {'wallet':…}."""
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        return str(entry.get("wallet") or "")
    return ""


# --------------------------------------------------------------------------- #
# Lectura                                                                      #
# --------------------------------------------------------------------------- #


def list_files() -> list[dict[str, Any]]:
    """Todas las listas de wallets del repo, con su recuento y descripción."""
    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    for pattern in _LIST_GLOBS:
        for path in sorted(_repo().glob(pattern)):
            if path.name in seen:
                continue
            seen.add(path.name)
            data = _read(path)
            wallets = data.get("wallets") or []
            rows.append(
                {
                    "file": path.name,
                    "count": sum(1 for w in wallets if _addr_of(w)),
                    "readme": (data.get("_README") or "")[:160],
                    "is_blacklist": "blacklist" in path.name,
                }
            )
    return rows


def _read(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ListError(f"No pude leer {path.name}: {exc}") from exc
    if not isinstance(data, dict):
        raise ListError(f"{path.name} no tiene el formato esperado.")
    return data


def read_list(filename: str) -> dict[str, Any]:
    """Direcciones (con notas si las hay) de una lista concreta."""
    path = _path(filename)
    if not path.is_file():
        raise ListError(f"No existe la lista {path.name}")
    data = _read(path)
    entries = []
    for entry in data.get("wallets") or []:
        addr = _addr_of(entry)
        if not addr:
            continue
        notes = {k: v for k, v in entry.items() if k != "wallet"} if isinstance(entry, dict) else {}
        entries.append({"wallet": addr, "notes": notes})
    return {"file": path.name, "readme": data.get("_README"), "wallets": entries}


# --------------------------------------------------------------------------- #
# Escritura                                                                    #
# --------------------------------------------------------------------------- #


def _save(path: Path, data: dict[str, Any]) -> None:
    wallets = data.get("wallets") or []
    data["_count"] = sum(1 for w in wallets if _addr_of(w))
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")


def add_wallet(filename: str, address: str, note: str = "") -> dict[str, Any]:
    """Añade una wallet a una lista (o la crea si no existe). Idempotente."""
    address = _valid_addr(address)
    path = _path(filename)
    data = _read(path) if path.is_file() else {"_README": "Creada desde el panel.", "wallets": []}
    wallets = data.setdefault("wallets", [])

    if any(_addr_of(w) == address for w in wallets):
        return {"file": path.name, "address": address, "added": False, "reason": "ya estaba"}

    # Respetamos la forma del fichero: strings si es blacklist, objetos si no.
    string_shaped = all(isinstance(w, str) for w in wallets) and wallets
    if string_shaped or "blacklist" in path.name:
        wallets.append(address)
    else:
        entry: dict[str, Any] = {"wallet": address}
        if note:
            entry["nota"] = note[:120]
        wallets.append(entry)
    _save(path, data)
    return {"file": path.name, "address": address, "added": True, "count": data["_count"]}


def remove_wallet(filename: str, address: str) -> dict[str, Any]:
    """Quita una wallet de una lista."""
    address = _valid_addr(address)
    path = _path(filename)
    if not path.is_file():
        raise ListError(f"No existe la lista {path.name}")
    data = _read(path)
    before = len(data.get("wallets") or [])
    data["wallets"] = [w for w in data.get("wallets") or [] if _addr_of(w) != address]
    removed = before - len(data["wallets"])
    _save(path, data)
    return {"file": path.name, "address": address, "removed": bool(removed), "count": data["_count"]}


def create_list(filename: str, readme: str = "") -> dict[str, Any]:
    """Crea una lista nueva vacía."""
    path = _path(filename)
    if path.is_file():
        raise ListError(f"La lista {path.name} ya existe.")
    if not path.name.startswith(("top_devs", "blacklist")):
        raise ListError("El nombre debe empezar por 'top_devs' o 'blacklist'.")
    _save(path, {"_README": readme or "Creada desde el panel.", "wallets": []})
    return {"file": path.name, "created": True}

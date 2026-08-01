"""Buzón de notificaciones del panel.

Los avisos viven **dentro de AGALAZ BANK**: se guardan en ``state/inbox.json`` y
se leen en la pestaña Notificaciones, con su contador de no leídas. No hace
falta ningún servicio de fuera para enterarse de lo que pasa.

Telegram queda como extra **apagado por defecto**: el bot ya avisa de lo suyo
por ahí (``src/monitoring/notifier.py``) y duplicarlo satura el móvil. Si algún
día lo quieres, se enciende desde la misma pestaña y reutiliza el
``TELEGRAM_BOT_TOKEN`` / ``TELEGRAM_CHAT_ID`` del ``.env`` del bot.

Qué se apunta (cada tipo se puede apagar):

    buy / sell / send   lo que firmas desde el panel
    fill                cada compra o venta que cierra el bot
    bot_down            el bot lleva demasiado rato sin escribir nada
    profits             reparto automático de ganancias

Regla de oro, como en el bot: **un fallo notificando no puede romper nada**.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import aiohttp

from . import config

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"
#: Minutos de silencio del bot antes de dar la voz de alarma.
SILENCE_MINUTES = 20
#: Cada cuánto mira el vigilante si hay novedades.
WATCH_INTERVAL = 60
#: Cuántos avisos se guardan. Es un buzón, no un archivo histórico: los fills
#: de verdad ya están en trades.log, aquí sólo interesa lo reciente.
MAX_ENTRIES = 300

EVENTS = ("buy", "sell", "send", "fill", "bot_down", "profits")


# --------------------------------------------------------------------------- #
# Ficheros de estado                                                           #
# --------------------------------------------------------------------------- #


def _file(name: str) -> Path:
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    return config.STATE_DIR / name


def _read_json(path: Path, fallback: Any) -> Any:
    if not path.exists():
        return fallback
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return fallback


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Preferencias                                                                 #
# --------------------------------------------------------------------------- #


def _defaults() -> dict[str, Any]:
    return {
        "enabled": True,
        "telegram": False,  # apagado a propósito: el buzón es el sitio
        "events": dict.fromkeys(EVENTS, True),
        "seen_fills": 0,
    }


def load() -> dict[str, Any]:
    data = _defaults()
    stored = _read_json(_file("notify.json"), None)
    if isinstance(stored, dict):
        data["enabled"] = bool(stored.get("enabled", True))
        data["telegram"] = bool(stored.get("telegram", False))
        data["seen_fills"] = int(stored.get("seen_fills", 0) or 0)
        for event_name in EVENTS:
            data["events"][event_name] = bool(
                stored.get("events", {}).get(event_name, True)
            )
    return data


def save(data: dict[str, Any]) -> None:
    _write_json(_file("notify.json"), data)


def set_prefs(enabled: bool, events: dict[str, Any], telegram: bool = False) -> dict[str, Any]:
    data = load()
    data["enabled"] = bool(enabled)
    data["telegram"] = bool(telegram)
    for event_name in EVENTS:
        if event_name in events:
            data["events"][event_name] = bool(events[event_name])
    save(data)
    return data


# --------------------------------------------------------------------------- #
# El buzón                                                                     #
# --------------------------------------------------------------------------- #


def entries() -> list[dict[str, Any]]:
    stored = _read_json(_file("inbox.json"), [])
    return stored if isinstance(stored, list) else []


def _next_id(current: list[dict[str, Any]]) -> int:
    return max((int(e.get("id", 0)) for e in current), default=0) + 1


def add(kind: str, title: str, text: str = "", url: str = "") -> dict[str, Any]:
    """Apunta un aviso en el buzón y lo devuelve."""
    current = entries()
    entry = {
        "id": _next_id(current),
        "ts": time.time(),
        "kind": kind,
        "title": title,
        "text": text,
        "url": url,
        "read": False,
    }
    current.append(entry)
    _write_json(_file("inbox.json"), current[-MAX_ENTRIES:])
    return entry


def mark_read(ids: list[int] | None = None) -> int:
    """Marca como leídos los ids dados, o todos si no se da ninguno."""
    current = entries()
    wanted = set(ids or [])
    changed = 0
    for entry in current:
        if (not wanted or int(entry.get("id", 0)) in wanted) and not entry.get("read"):
            entry["read"] = True
            changed += 1
    if changed:
        _write_json(_file("inbox.json"), current)
    return changed


def clear() -> None:
    _write_json(_file("inbox.json"), [])


def unread() -> int:
    return sum(1 for e in entries() if not e.get("read"))


def inbox(limit: int = 60) -> dict[str, Any]:
    """Lo que pinta la pestaña: los más nuevos primero."""
    current = sorted(entries(), key=lambda e: e.get("ts", 0), reverse=True)
    return {
        "entries": current[: max(1, min(limit, MAX_ENTRIES))],
        "unread": sum(1 for e in current if not e.get("read")),
    }


# --------------------------------------------------------------------------- #
# Telegram (opcional, apagado por defecto)                                     #
# --------------------------------------------------------------------------- #


def credentials() -> tuple[str, str]:
    token = os.getenv("PANEL_TELEGRAM_BOT_TOKEN", "").strip() or config.bot_env(
        "TELEGRAM_BOT_TOKEN"
    ).strip()
    chat = os.getenv("PANEL_TELEGRAM_CHAT_ID", "").strip() or config.bot_env(
        "TELEGRAM_CHAT_ID"
    ).strip()
    return token, chat


def telegram_ready() -> bool:
    token, chat = credentials()
    return bool(token and chat)


async def send_telegram(text: str) -> dict[str, Any]:
    """Manda un mensaje. Devuelve el porqué si no ha podido, sin lanzar."""
    token, chat = credentials()
    if not token or not chat:
        return {"sent": False, "reason": "Sin TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID."}
    try:
        async with aiohttp.ClientSession() as session, session.post(
            f"{TELEGRAM_API}/bot{token}/sendMessage",
            json={
                "chat_id": chat,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=aiohttp.ClientTimeout(total=15),
        ) as response:
            body = await response.json(content_type=None)
            if not body.get("ok"):
                reason = body.get("description", f"HTTP {response.status}")
                logger.warning("Telegram rechazó el aviso: %s", reason)
                return {"sent": False, "reason": reason}
            return {"sent": True}
    except Exception as exc:  # noqa: BLE001 - avisar nunca puede tumbar el panel
        logger.debug("No pude avisar por Telegram: %s", exc)
        return {"sent": False, "reason": str(exc)}


# --------------------------------------------------------------------------- #
# Entrada única                                                                #
# --------------------------------------------------------------------------- #


async def event(kind: str, title: str, text: str = "", url: str = "") -> None:
    """Apunta el aviso en el buzón (y en Telegram si está encendido)."""
    prefs = load()
    if not prefs["enabled"] or not prefs["events"].get(kind, True):
        return
    try:
        add(kind, title, text, url)
    except OSError:
        logger.exception("No pude escribir en el buzón de avisos")
    if prefs.get("telegram"):
        body = f"<b>{title}</b>" + (f"\n{text}" if text else "") + (f"\n{url}" if url else "")
        await send_telegram(body)


# --------------------------------------------------------------------------- #
# Mensajes                                                                     #
# --------------------------------------------------------------------------- #


def _solscan(signature: str) -> str:
    return f"https://solscan.io/tx/{signature}" if signature else ""


async def signed_buy(mint: str, sol: float, result: dict[str, Any]) -> None:
    await event("buy", f"Compra firmada · {sol:g} SOL", mint, _solscan(result.get("signature", "")))


async def signed_sell(mint: str, percent: float, result: dict[str, Any]) -> None:
    await event("sell", f"Venta firmada · {percent:g}%", mint, _solscan(result.get("signature", "")))


async def signed_send(to: str, sol: float, result: dict[str, Any]) -> None:
    await event("send", f"Envío de {sol:g} SOL", f"a {to}", _solscan(result.get("signature", "")))


async def profits_sent(distributed_sol: float) -> None:
    await event("profits", "Reparto automático", f"{distributed_sol:g} SOL enviados")


# --------------------------------------------------------------------------- #
# Vigilante: fills nuevos del bot y silencio sospechoso                        #
# --------------------------------------------------------------------------- #


def _trades_path() -> Path:
    return config.BOT_REPO / "trades" / "trades.log"


def fill_lines() -> list[str]:
    path = _trades_path()
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return [line for line in text.splitlines() if line.strip()]


def _describe_fill(line: str) -> tuple[str, str, str] | None:
    try:
        fill = json.loads(line)
    except json.JSONDecodeError:
        return None  # el bot puede estar escribiendo: línea partida, no es un error
    if not isinstance(fill, dict) or not fill.get("action"):
        return None
    action = str(fill["action"]).lower()
    symbol = fill.get("symbol") or (str(fill.get("token_address", ""))[:8] + "…")
    title = f"El bot ha {'comprado' if action == 'buy' else 'vendido'} {symbol}"
    price, amount = fill.get("price"), fill.get("amount")
    text = ""
    if isinstance(amount, (int, float)) and isinstance(price, (int, float)):
        text = f"{amount:,.0f} @ {price:.10f}"
    return title, text, _solscan(str(fill.get("tx_hash", "")))


def minutes_since_bot_wrote() -> float | None:
    """Cuánto lleva el bot sin tocar nada. None si no hay ni por dónde mirar.

    Se mira el fichero más reciente entre logs, posiciones abiertas y fills:
    cualquiera de los tres se toca constantemente con el bot vivo, y funciona
    igual en Windows que en el droplet (systemd no está en todas partes).
    """
    newest: float | None = None
    candidates = list((config.BOT_REPO / "logs").glob("*.log"))
    candidates += list(config.BOT_REPO.glob("open_position_*.json"))
    candidates.append(_trades_path())
    for path in candidates:
        try:
            if path.exists():
                newest = max(newest or 0, path.stat().st_mtime)
        except OSError:
            continue
    return None if newest is None else (time.time() - newest) / 60


async def watch_tick(silent_state: dict[str, bool]) -> None:
    """Una pasada del vigilante. ``silent_state`` recuerda si ya avisamos."""
    prefs = load()
    if not prefs["enabled"]:
        return

    lines = fill_lines()
    seen = int(prefs.get("seen_fills", 0))
    if len(lines) < seen:
        seen = 0  # el log se ha rotado o vaciado: se vuelve a empezar
    if len(lines) > seen:
        for line in lines[seen:]:
            described = _describe_fill(line)
            if described:
                await event("fill", *described)
        prefs["seen_fills"] = len(lines)
        save(prefs)

    quiet = minutes_since_bot_wrote()
    if quiet is None:
        return
    if quiet >= SILENCE_MINUTES and not silent_state.get("warned"):
        silent_state["warned"] = True
        await event(
            "bot_down",
            f"El bot lleva {quiet:.0f} min callado",
            "Ni logs, ni posiciones, ni fills. Míralo.",
        )
    elif quiet < SILENCE_MINUTES and silent_state.get("warned"):
        silent_state["warned"] = False
        await event("bot_down", "El bot vuelve a dar señales de vida")


def status() -> dict[str, Any]:
    """Lo que la web necesita para pintar la pestaña."""
    prefs = load()
    quiet = minutes_since_bot_wrote()
    return {
        "enabled": prefs["enabled"],
        "telegram": prefs["telegram"],
        "events": prefs["events"],
        "telegram_ready": telegram_ready(),
        "silence_minutes": SILENCE_MINUTES,
        "bot_quiet_minutes": round(quiet, 1) if quiet is not None else None,
        "unread": unread(),
    }

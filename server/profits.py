"""Reparto de ganancias: enviar un % de lo ganado a varias wallets.

Modelo que pidió Manu: del total de ganancias, un porcentaje va a cada wallet,
en modo **manual** (tú pulsas "repartir") o **automático** (según se van
realizando beneficios, el panel manda la parte que toca).

El estado vive en un JSON del panel (``state/profits.json``), no en el repo del
bot: es config del panel, no del bot.

Seguridad: repartir MUEVE SOL real, así que pasa por :mod:`wallet` (que exige
``PANEL_HOT_WALLET=true``) y el modo automático arranca sólo si lo enciendes tú.
"""

from __future__ import annotations

import json
import time
from typing import Any

from . import botlink, config, wallet


class ProfitError(RuntimeError):
    """Config de reparto inválida."""


def _state_file():
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    return config.STATE_DIR / "profits.json"


_DEFAULT: dict[str, Any] = {
    "mode": "manual",          # "manual" | "auto"
    "recipients": [],           # [{"label": str, "address": str, "pct": float}]
    "min_auto_sol": 0.2,        # en auto, no reparte hasta acumular esto de beneficio nuevo
    "high_water_sol": None,     # marca del beneficio realizado ya repartido (auto)
    "history": [],              # últimos repartos
}


def get_config() -> dict[str, Any]:
    path = _state_file()
    if not path.is_file():
        return dict(_DEFAULT)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(_DEFAULT)
    return {**_DEFAULT, **data}


def _save(data: dict[str, Any]) -> None:
    _state_file().write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")


def set_config(mode: str, recipients: list[dict[str, Any]], min_auto_sol: float) -> dict[str, Any]:
    """Guarda destinatarios y modo. Valida direcciones y que los % no pasen de 100."""
    if mode not in ("manual", "auto"):
        raise ProfitError("El modo debe ser 'manual' o 'auto'.")
    clean: list[dict[str, Any]] = []
    total_pct = 0.0
    for r in recipients:
        address = str(r.get("address", "")).strip()
        pct = float(r.get("pct", 0))
        if not address:
            continue
        if not 0 < pct <= 100:
            raise ProfitError(f"El % de {address[:8]}… debe estar entre 0 y 100.")
        total_pct += pct
        clean.append({"label": str(r.get("label", "")).strip()[:40], "address": address, "pct": pct})
    if total_pct > 100:
        raise ProfitError(f"Los porcentajes suman {total_pct:g}% (máximo 100%).")

    data = get_config()
    data.update({"mode": mode, "recipients": clean, "min_auto_sol": max(0.0, min_auto_sol)})
    # Al activar auto por primera vez, fijamos la marca en el beneficio actual
    # para no repartir de golpe todo el histórico pasado.
    if mode == "auto" and data.get("high_water_sol") is None:
        data["high_water_sol"] = _realized_pnl()
    _save(data)
    return data


def _realized_pnl() -> float:
    try:
        return float(botlink.performance(None).get("realized_pnl_sol", 0.0))
    except Exception:  # noqa: BLE001 -- si el log no está, tratamos como 0
        return 0.0


def _record(entry: dict[str, Any]) -> None:
    data = get_config()
    data.setdefault("history", []).insert(0, entry)
    data["history"] = data["history"][:50]
    _save(data)


async def distribute(total_sol: float) -> dict[str, Any]:
    """Reparte ``total_sol`` entre los destinatarios según su %. Firma y envía.

    Cada wallet recibe ``total_sol * pct/100``. Lo que no cubran los % se queda
    en la wallet. Devuelve el detalle de cada envío (con enlaces a Solscan).
    """
    if total_sol <= 0:
        raise ProfitError("La cantidad a repartir debe ser mayor que 0.")
    recipients = get_config()["recipients"]
    if not recipients:
        raise ProfitError("No hay destinatarios configurados.")

    sent: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for r in recipients:
        amount = round(total_sol * r["pct"] / 100, 6)
        if amount <= 0:
            continue
        try:
            result = await wallet.send_sol(r["address"], amount)
            sent.append({"label": r["label"], "address": r["address"], "sol": amount, **result})
        except wallet.WalletError as exc:
            errors.append({"address": r["address"], "sol": amount, "error": str(exc)})

    entry = {
        "ts": time.time(),
        "total_sol": total_sol,
        "sent": [{"address": s["address"], "sol": s["sol"], "signature": s.get("signature")} for s in sent],
        "errors": errors,
    }
    _record(entry)
    return {"distributed_sol": sum(s["sol"] for s in sent), "sent": sent, "errors": errors}


async def auto_tick() -> dict[str, Any] | None:
    """Un latido del reparto automático: si hay beneficio nuevo por encima del
    umbral, reparte esa parte. Lo llama un bucle de fondo cada pocos minutos.

    Devuelve el reparto si hizo algo, o ``None`` si no tocaba."""
    data = get_config()
    if data["mode"] != "auto" or not config.HOT_WALLET or not data["recipients"]:
        return None
    current = _realized_pnl()
    mark = data.get("high_water_sol")
    if mark is None:
        data["high_water_sol"] = current
        _save(data)
        return None
    new_profit = current - mark
    if new_profit < data["min_auto_sol"]:
        return None
    result = await distribute(new_profit)
    # Avanzamos la marca sólo por lo que de verdad salió, para no perder el resto.
    data = get_config()
    data["high_water_sol"] = mark + result["distributed_sol"]
    _save(data)
    return result

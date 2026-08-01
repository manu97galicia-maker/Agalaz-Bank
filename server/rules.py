"""Órdenes condicionales: "compra X si pasa Y".

El chat del panel propone acciones que ejecutas al momento. Esto es lo otro:
una orden que **se queda esperando**. La dictas en lenguaje natural, el agente
la convierte en condiciones concretas, tú la confirmas, y a partir de ahí un
bucle la vigila hasta que se cumple.

    "compra 0.05 SOL de <mint> si el market cap baja de 30k"
    "vende el 50% si sube un 40% en 1h"
    "avísame si la liquidez de <mint> cae por debajo de 5.000$"

Se guardan en ``state/rules.json`` para que sobrevivan a un reinicio: una orden
que desaparece porque reiniciaste el panel es peor que no tenerla.

Qué se puede mirar (lo que da DexScreener, vía ``market.token_info``):

    price_usd  market_cap_usd  liquidity_usd
    volume_5m_usd  volume_24h_usd  price_change_5m  price_change_1h

Seguridad, y esto es lo importante:

* Una orden **dispara una sola vez**. Nada de bucles comprando.
* Comprar/vender exige la wallet caliente encendida. Apagada, la orden no se
  ejecuta: se queda bloqueada y te avisa. No se firma nada a tus espaldas.
* Toda compra respeta el tope duro ``PANEL_MAX_BUY_SOL``.
* Cada disparo (y cada fallo) deja su aviso en el buzón.
"""

from __future__ import annotations

import logging
import secrets
import time
from typing import Any

from . import config, market, notify, wallet

logger = logging.getLogger(__name__)

#: Cada cuánto se comprueban las órdenes armadas.
CHECK_INTERVAL = 60
#: Métricas que se pueden vigilar, tal y como las devuelve market.token_info.
METRICS = (
    "price_usd",
    "market_cap_usd",
    "liquidity_usd",
    "volume_5m_usd",
    "volume_24h_usd",
    "price_change_5m",
    "price_change_1h",
)
OPS = ("<", "<=", ">", ">=")
ACTIONS = ("buy", "sell", "alert")
#: Tope de órdenes vivas. Es un panel, no un motor de estrategias.
MAX_ARMED = 40


class RuleError(Exception):
    """Orden mal formada."""


# --------------------------------------------------------------------------- #
# Persistencia                                                                 #
# --------------------------------------------------------------------------- #


def _path():
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    return config.STATE_DIR / "rules.json"


def all_rules() -> list[dict[str, Any]]:
    import json  # noqa: PLC0415

    path = _path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return data if isinstance(data, list) else []


def _save(rules: list[dict[str, Any]]) -> None:
    import json  # noqa: PLC0415

    _path().write_text(json.dumps(rules, indent=1, ensure_ascii=False), encoding="utf-8")


def armed() -> list[dict[str, Any]]:
    return [r for r in all_rules() if r.get("status") == "armed"]


# --------------------------------------------------------------------------- #
# Alta                                                                         #
# --------------------------------------------------------------------------- #


def _clean_conditions(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not raw:
        raise RuleError("Una orden sin condiciones no es una orden: dime cuándo dispara.")
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise RuleError(f"Condición ilegible: {item!r}")
        metric = str(item.get("metric", "")).strip()
        op = str(item.get("op", "")).strip()
        if metric not in METRICS:
            raise RuleError(f"No sé mirar '{metric}'. Puedo con: {', '.join(METRICS)}.")
        if op not in OPS:
            raise RuleError(f"Comparación '{op}' no válida. Usa: {', '.join(OPS)}.")
        try:
            value = float(item["value"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuleError("Cada condición necesita un número en 'value'.") from exc
        out.append({"metric": metric, "op": op, "value": value})
    return out


def create(
    mint: str,
    action: str,
    conditions: Any,
    *,
    text: str = "",
    sol: float | None = None,
    percent: float | None = None,
    expires_minutes: int | None = None,
) -> dict[str, Any]:
    """Arma una orden nueva. Valida a conciencia: esto acaba firmando."""
    action = str(action).strip().lower()
    if action not in ACTIONS:
        raise RuleError(f"Acción '{action}' desconocida. Usa: {', '.join(ACTIONS)}.")
    mint = market._valid_mint(str(mint).strip())  # noqa: SLF001 - misma validación que el resto

    if len(armed()) >= MAX_ARMED:
        raise RuleError(f"Ya hay {MAX_ARMED} órdenes esperando. Cancela alguna antes.")

    if action == "buy":
        if sol is None:
            raise RuleError("Para comprar dime cuántos SOL.")
        sol = float(sol)
        if sol <= 0:
            raise RuleError("El importe tiene que ser mayor que cero.")
        if sol > config.MAX_BUY_SOL:
            raise RuleError(
                f"{sol:g} SOL pasa del tope por compra ({config.MAX_BUY_SOL:g} SOL, "
                "PANEL_MAX_BUY_SOL)."
            )
    if action == "sell":
        if percent is None:
            raise RuleError("Para vender dime qué porcentaje.")
        percent = float(percent)
        if not 0 < percent <= 100:
            raise RuleError("El porcentaje tiene que estar entre 0 y 100.")

    rule = {
        "id": secrets.token_urlsafe(6),
        "created_at": time.time(),
        "text": text.strip(),
        "mint": mint,
        "action": action,
        "sol": sol,
        "percent": percent,
        "conditions": _clean_conditions(conditions),
        "expires_at": (time.time() + expires_minutes * 60) if expires_minutes else None,
        "status": "armed",
        "checked_at": None,
        "last_seen": {},
    }
    rules = all_rules()
    rules.append(rule)
    _save(rules)
    logger.info("Orden condicional armada: %s", describe(rule))
    return rule


def cancel(rule_id: str) -> bool:
    rules = all_rules()
    for rule in rules:
        if rule.get("id") == rule_id and rule.get("status") == "armed":
            rule["status"] = "cancelled"
            _save(rules)
            return True
    return False


def _update(rule_id: str, **changes: Any) -> None:
    rules = all_rules()
    for rule in rules:
        if rule.get("id") == rule_id:
            rule.update(changes)
            break
    _save(rules)


# --------------------------------------------------------------------------- #
# Lectura humana                                                               #
# --------------------------------------------------------------------------- #

_METRIC_NAMES = {
    "price_usd": "el precio",
    "market_cap_usd": "el market cap",
    "liquidity_usd": "la liquidez",
    "volume_5m_usd": "el volumen 5m",
    "volume_24h_usd": "el volumen 24h",
    "price_change_5m": "el cambio 5m (%)",
    "price_change_1h": "el cambio 1h (%)",
}
_OP_NAMES = {"<": "baja de", "<=": "baja de o iguala", ">": "pasa de", ">=": "llega a"}


def describe(rule: dict[str, Any]) -> str:
    if rule["action"] == "buy":
        what = f"comprar {rule['sol']:g} SOL"
    elif rule["action"] == "sell":
        what = f"vender el {rule['percent']:g}%"
    else:
        what = "avisarme"
    when = " y ".join(
        f"{_METRIC_NAMES.get(c['metric'], c['metric'])} {_OP_NAMES.get(c['op'], c['op'])} {c['value']:,.6g}"
        for c in rule["conditions"]
    )
    return f"{what} de {rule['mint'][:8]}… cuando {when}"


# --------------------------------------------------------------------------- #
# Evaluación                                                                   #
# --------------------------------------------------------------------------- #


def _passes(condition: dict[str, Any], snapshot: dict[str, Any]) -> bool | None:
    """True/False, o None si el dato no viene (no se dispara a ciegas)."""
    current = snapshot.get(condition["metric"])
    if not isinstance(current, (int, float)):
        return None
    value = condition["value"]
    op = condition["op"]
    if op == "<":
        return current < value
    if op == "<=":
        return current <= value
    if op == ">":
        return current > value
    return current >= value


async def _fire(rule: dict[str, Any], snapshot: dict[str, Any]) -> None:
    """Ejecuta la acción de una orden que ya se ha cumplido."""
    symbol = snapshot.get("symbol") or rule["mint"][:8] + "…"
    detail = describe(rule)

    if rule["action"] == "alert":
        _update(rule["id"], status="fired", fired_at=time.time())
        await notify.event("fill", f"Se cumplió tu condición · {symbol}", detail,
                           f"https://dexscreener.com/solana/{rule['mint']}")
        return

    if not config.HOT_WALLET:
        # Bloqueada, no fallida: el día que enciendas la wallet sigue teniendo
        # sentido. Pero se desarma para no avisarte cada minuto.
        _update(rule["id"], status="blocked", fired_at=time.time())
        await notify.event(
            "bot_down",
            f"Orden NO ejecutada · {symbol}",
            f"Se cumplió ({detail}) pero la wallet caliente está apagada.",
        )
        return

    try:
        if rule["action"] == "buy":
            result = await wallet.buy(rule["mint"], float(rule["sol"]))
        else:
            result = await wallet.sell(rule["mint"], float(rule["percent"]))
    except Exception as exc:  # noqa: BLE001 - un fallo de red no puede matar el bucle
        logger.exception("Orden condicional %s falló", rule["id"])
        _update(rule["id"], status="failed", fired_at=time.time(), error=str(exc))
        await notify.event(rule["action"], f"Orden FALLÓ · {symbol}", f"{detail}\n{exc}")
        return

    signature = result.get("signature", "")
    _update(rule["id"], status="fired", fired_at=time.time(), result={"signature": signature})
    await notify.event(
        rule["action"],
        f"Orden ejecutada · {symbol}",
        detail,
        f"https://solscan.io/tx/{signature}" if signature else "",
    )


async def check_tick() -> int:
    """Una pasada por todas las órdenes armadas. Devuelve cuántas dispararon."""
    pending = armed()
    if not pending:
        return 0

    now = time.time()
    fired = 0
    # Un token puede tener varias órdenes: se consulta una vez y se reutiliza.
    snapshots: dict[str, dict[str, Any]] = {}

    for rule in pending:
        if rule.get("expires_at") and now > rule["expires_at"]:
            _update(rule["id"], status="expired")
            await notify.event("bot_down", "Orden caducada sin cumplirse", describe(rule))
            continue

        mint = rule["mint"]
        if mint not in snapshots:
            try:
                snapshots[mint] = await market.token_info(mint)
            except Exception as exc:  # noqa: BLE001
                logger.debug("No pude mirar el mercado de %s: %s", mint, exc)
                snapshots[mint] = {}
        snapshot = snapshots[mint]
        if not snapshot.get("found"):
            continue  # sin datos no se decide nada

        verdicts = [_passes(c, snapshot) for c in rule["conditions"]]
        _update(
            rule["id"],
            checked_at=now,
            last_seen={m: snapshot.get(m) for m in {c["metric"] for c in rule["conditions"]}},
        )
        if any(v is None for v in verdicts) or not all(verdicts):
            continue

        await _fire(rule, snapshot)
        fired += 1

    return fired


def summary() -> dict[str, Any]:
    """Lo que pinta la pestaña Servidor."""
    rules = all_rules()
    return {
        "armed": [
            {
                "id": r["id"],
                "text": r.get("text") or describe(r),
                "human": describe(r),
                "mint": r["mint"],
                "action": r["action"],
                "checked_at": r.get("checked_at"),
                "last_seen": r.get("last_seen") or {},
                "expires_at": r.get("expires_at"),
            }
            for r in rules
            if r.get("status") == "armed"
        ],
        "recent": [
            {
                "id": r["id"],
                "human": describe(r),
                "status": r.get("status"),
                "fired_at": r.get("fired_at"),
                "error": r.get("error"),
            }
            for r in sorted(rules, key=lambda x: x.get("fired_at") or 0, reverse=True)
            if r.get("status") != "armed"
        ][:8],
        "hot_wallet": config.HOT_WALLET,
    }

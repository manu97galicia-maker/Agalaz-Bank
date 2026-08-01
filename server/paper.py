"""Simulador de estrategias en papel, con las comisiones puestas.

Dictas una estrategia hablando, el agente la traduce a números, y aquí se
opera **con dinero de mentira y comisiones de verdad**. Nada de esto firma.

Por qué existe: una estrategia puede ganar en bruto y perder en neto. Con la
config actual del bot (``fixed_amount: 60_000_000`` microlamports por unidad de
cómputo y un límite de 85.000 CU) cada transacción paga

    60.000.000 × 85.000 / 1.000.000 = 5.100.000 lamports = 0,0051 SOL

y una operación son DOS transacciones: comprar y vender. Son 0,0102 SOL fijos,
da igual que muevas 0,15 SOL o 1 SOL. Sobre los 0,15 SOL que compra el bot hoy,
eso es un 6,8% que ya has perdido antes de que el precio se mueva; sumando el
~1% de la plataforma por lado, la operación necesita cerca de un **+9% sólo
para quedar en tablas**. Un simulador que no cuente eso miente.

Qué se modela, y de dónde sale cada número:

    fee de prioridad   del YAML del bot (fixed_amount × CU / 1e6), por tx
    fee de red         5.000 lamports por firma, fijo de Solana
    fee de plataforma  SUPUESTO del 1% por lado (pump.fun); ajustable
    slippage           el configurado en el bot, aplicado como coste de entrada
                       y salida en el peor caso razonable
    renta de la cuenta 0,00203928 SOL al abrir la ATA; se recupera al cerrarla,
                       y por eso se cuenta aparte y no como pérdida

Lo que NO se modela, y conviene saberlo: impacto de precio real por el tamaño
de tu orden, transacciones fallidas (que pagan fee igual), MEV, y que en un
token sin liquidez la venta simplemente no entre. Todo eso juega EN TU CONTRA,
así que el resultado de aquí es el **mejor caso**, no el esperado.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from pathlib import Path
from typing import Any

from . import config, market

logger = logging.getLogger(__name__)

LAMPORTS = 1_000_000_000
#: Firma de Solana. No es negociable ni depende de la congestión.
BASE_FEE_LAMPORTS = 5_000
#: Límite de cómputo que fija el bot en client.py cuando manda una tx.
COMPUTE_UNITS = 85_000
#: Alquiler de la cuenta de token (ATA). Se recupera al cerrarla.
ATA_RENT_SOL = 0.00203928
#: Comisión de la plataforma por lado. SUPUESTO, no leído de la cadena.
DEFAULT_PLATFORM_FEE_PCT = 1.0
#: Cada cuánto se revisan las posiciones de papel.
CHECK_INTERVAL = 60
MAX_STRATEGIES = 12


class PaperError(Exception):
    """Estrategia mal definida."""


# --------------------------------------------------------------------------- #
# Costes                                                                       #
# --------------------------------------------------------------------------- #


def _bot_priority_fee_sol() -> tuple[float, str]:
    """Fee de prioridad por transacción, leído del YAML del bot activo.

    Devuelve (sol, de_dónde_sale) para poder enseñar la procedencia: un número
    de coste sin fuente no se lo cree nadie, y con razón.
    """
    import contextlib  # noqa: PLC0415

    bots_dir = config.BOT_REPO / "bots"
    if not bots_dir.is_dir():
        return 0.0, "sin repo del bot: fee de prioridad a 0"

    worst: tuple[float, str] | None = None
    for path in sorted(bots_dir.glob("*.yaml")):
        with contextlib.suppress(OSError, UnicodeDecodeError):
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()

            # `enabled: true` SIN indentar. Buscarlo en cualquier parte del
            # fichero cogía el de una subsección (cleanup, filtros...) y daba
            # por activo un bot parado: el coste salía 60 veces menor.
            enabled = any(
                line.startswith("enabled:") and line.split(":", 1)[1].strip().lower() == "true"
                for line in lines
            )
            if not enabled:
                continue

            for line in lines:
                if "fixed_amount:" in line:
                    raw = line.split(":", 1)[1].strip().replace("_", "").split("#")[0].strip()
                    try:
                        micro = float(raw)
                    except ValueError:
                        continue
                    sol = micro * COMPUTE_UNITS / 1e6 / LAMPORTS
                    source = f"{path.name} (fixed_amount={raw} µlamports × {COMPUTE_UNITS} CU)"
                    # Con varios bots activos se coge el más caro: para decidir
                    # si una estrategia aguanta, el peor caso es el honesto.
                    if worst is None or sol > worst[0]:
                        worst = (sol, source)
                    break

    return worst if worst else (0.0, "sin bot activo: fee de prioridad a 0")


def cost_model(size_sol: float, platform_fee_pct: float | None = None,
               slippage_pct: float | None = None) -> dict[str, Any]:
    """Desglose de lo que cuesta una operación completa (comprar y vender)."""
    priority_each, source = _bot_priority_fee_sol()
    platform_pct = DEFAULT_PLATFORM_FEE_PCT if platform_fee_pct is None else float(platform_fee_pct)
    slip_pct = 0.0 if slippage_pct is None else float(slippage_pct)

    priority = priority_each * 2
    network = BASE_FEE_LAMPORTS * 2 / LAMPORTS
    platform = size_sol * (platform_pct / 100) * 2
    slippage = size_sol * (slip_pct / 100) * 2
    total = priority + network + platform + slippage

    return {
        "size_sol": size_sol,
        "priority_fee_sol": priority,
        "priority_fee_each_sol": priority_each,
        "priority_fee_source": source,
        "network_fee_sol": network,
        "platform_fee_sol": platform,
        "platform_fee_pct": platform_pct,
        "slippage_sol": slippage,
        "slippage_pct": slip_pct,
        "ata_rent_sol": ATA_RENT_SOL,
        "total_sol": total,
        # Lo único que de verdad hay que mirar: cuánto tiene que subir para
        # que no pierdas. Si esto es 9%, cada trade parte con un 9% en contra.
        "breakeven_pct": (total / size_sol * 100) if size_sol > 0 else 0.0,
    }


# --------------------------------------------------------------------------- #
# Persistencia                                                                 #
# --------------------------------------------------------------------------- #


def _path() -> Path:
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    return config.STATE_DIR / "paper.json"


def _load() -> list[dict[str, Any]]:
    path = _path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return data if isinstance(data, list) else []


def _save(data: list[dict[str, Any]]) -> None:
    _path().write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")


def strategies() -> list[dict[str, Any]]:
    return _load()


def _find(strategy_id: str) -> dict[str, Any] | None:
    return next((s for s in _load() if s["id"] == strategy_id), None)


def _replace(strategy: dict[str, Any]) -> None:
    data = _load()
    for i, s in enumerate(data):
        if s["id"] == strategy["id"]:
            data[i] = strategy
            break
    _save(data)


# --------------------------------------------------------------------------- #
# Alta                                                                         #
# --------------------------------------------------------------------------- #


def create(
    name: str,
    mints: list[str],
    entry: Any,
    *,
    text: str = "",
    size_sol: float = 0.15,
    take_profit_pct: float = 40.0,
    stop_loss_pct: float = 20.0,
    max_hold_minutes: int = 60,
    platform_fee_pct: float | None = None,
    slippage_pct: float | None = None,
) -> dict[str, Any]:
    """Da de alta una estrategia de papel. Valida como si fuera dinero real."""
    from . import rules  # noqa: PLC0415 - reutiliza el validador de condiciones

    if len(_load()) >= MAX_STRATEGIES:
        raise PaperError(f"Ya hay {MAX_STRATEGIES} estrategias. Borra alguna antes.")
    if not mints:
        raise PaperError("Dime al menos un token que vigilar.")
    clean_mints = [market._valid_mint(str(m).strip()) for m in mints]  # noqa: SLF001
    if float(size_sol) <= 0:
        raise PaperError("El tamaño de la posición tiene que ser mayor que cero.")
    if float(take_profit_pct) <= 0 or float(stop_loss_pct) <= 0:
        raise PaperError("Take profit y stop loss tienen que ser porcentajes positivos.")

    strategy = {
        "id": secrets.token_urlsafe(6),
        "created_at": time.time(),
        "name": name.strip() or "sin nombre",
        "text": text.strip(),
        "mints": clean_mints,
        "entry": rules._clean_conditions(entry),  # noqa: SLF001
        "size_sol": float(size_sol),
        "take_profit_pct": float(take_profit_pct),
        "stop_loss_pct": float(stop_loss_pct),
        "max_hold_minutes": int(max_hold_minutes),
        "platform_fee_pct": platform_fee_pct,
        "slippage_pct": slippage_pct,
        "running": True,
        "open": {},      # mint -> posición abierta
        "closed": [],    # operaciones cerradas
    }
    data = _load()
    data.append(strategy)
    _save(data)
    logger.info("Estrategia de papel creada: %s", strategy["name"])
    return strategy


def set_running(strategy_id: str, running: bool) -> bool:
    strategy = _find(strategy_id)
    if not strategy:
        return False
    strategy["running"] = bool(running)
    _replace(strategy)
    return True


def delete(strategy_id: str) -> bool:
    data = _load()
    remaining = [s for s in data if s["id"] != strategy_id]
    if len(remaining) == len(data):
        return False
    _save(remaining)
    return True


# --------------------------------------------------------------------------- #
# Simulación                                                                   #
# --------------------------------------------------------------------------- #


def _costs_for(strategy: dict[str, Any]) -> dict[str, Any]:
    return cost_model(
        strategy["size_sol"], strategy.get("platform_fee_pct"), strategy.get("slippage_pct")
    )


def _close(strategy: dict[str, Any], mint: str, price: float, reason: str) -> dict[str, Any]:
    """Cierra una posición de papel y hace las cuentas, comisiones incluidas."""
    position = strategy["open"].pop(mint)
    costs = _costs_for(strategy)
    size = strategy["size_sol"]

    gross_pct = (price / position["entry_price"] - 1) * 100 if position["entry_price"] else 0.0
    gross_sol = size * gross_pct / 100
    net_sol = gross_sol - costs["total_sol"]

    trade = {
        "mint": mint,
        "symbol": position.get("symbol"),
        "opened_at": position["opened_at"],
        "closed_at": time.time(),
        "entry_price": position["entry_price"],
        "exit_price": price,
        "reason": reason,
        "gross_pct": gross_pct,
        "gross_sol": gross_sol,
        "fees_sol": costs["total_sol"],
        "net_sol": net_sol,
    }
    strategy["closed"].append(trade)
    return trade


async def tick() -> int:
    """Una pasada por todas las estrategias vivas. Devuelve cierres+aperturas."""
    from . import rules  # noqa: PLC0415

    data = _load()
    active = [s for s in data if s.get("running")]
    if not active:
        return 0

    snapshots: dict[str, dict[str, Any]] = {}
    moves = 0
    now = time.time()

    for strategy in active:
        for mint in strategy["mints"]:
            if mint not in snapshots:
                try:
                    snapshots[mint] = await market.token_info(mint)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("Sin mercado para %s: %s", mint, exc)
                    snapshots[mint] = {}
            info = snapshots[mint]
            if not info.get("found"):
                continue
            price = info.get("price_usd")
            if not isinstance(price, (int, float)) or price <= 0:
                continue

            position = strategy["open"].get(mint)

            if position:
                change = (price / position["entry_price"] - 1) * 100
                held_min = (now - position["opened_at"]) / 60
                reason = None
                if change >= strategy["take_profit_pct"]:
                    reason = "take profit"
                elif change <= -strategy["stop_loss_pct"]:
                    reason = "stop loss"
                elif held_min >= strategy["max_hold_minutes"]:
                    reason = "tiempo agotado"
                if reason:
                    _close(strategy, mint, price, reason)
                    moves += 1
                continue

            verdicts = [rules._passes(c, info) for c in strategy["entry"]]  # noqa: SLF001
            if any(v is None for v in verdicts) or not all(verdicts):
                continue
            strategy["open"][mint] = {
                "opened_at": now,
                "entry_price": price,
                "symbol": info.get("symbol"),
            }
            moves += 1

    _save(data)
    return moves


# --------------------------------------------------------------------------- #
# Resultados                                                                   #
# --------------------------------------------------------------------------- #


def report(strategy: dict[str, Any]) -> dict[str, Any]:
    """Las cuentas de una estrategia: en bruto, en neto, y cuánto se fue en fees."""
    closed = strategy.get("closed", [])
    costs = _costs_for(strategy)
    gross = sum(t["gross_sol"] for t in closed)
    fees = sum(t["fees_sol"] for t in closed)
    net = sum(t["net_sol"] for t in closed)
    wins = sum(1 for t in closed if t["net_sol"] > 0)

    return {
        "trades": len(closed),
        "wins": wins,
        "win_rate_pct": (wins / len(closed) * 100) if closed else 0.0,
        "gross_sol": gross,
        "fees_sol": fees,
        "net_sol": net,
        # Lo que hace daño de verdad: qué porcentaje de lo ganado en bruto se
        # comieron las comisiones. Por encima de 100 significa que una
        # estrategia ganadora acaba en pérdidas.
        "fees_vs_gross_pct": (fees / gross * 100) if gross > 0 else None,
        "breakeven_pct": costs["breakeven_pct"],
        "costs": costs,
    }


def summary() -> dict[str, Any]:
    out = []
    for strategy in _load():
        out.append(
            {
                "id": strategy["id"],
                "name": strategy["name"],
                "text": strategy.get("text", ""),
                "running": strategy.get("running", False),
                "mints": strategy["mints"],
                "size_sol": strategy["size_sol"],
                "take_profit_pct": strategy["take_profit_pct"],
                "stop_loss_pct": strategy["stop_loss_pct"],
                "max_hold_minutes": strategy["max_hold_minutes"],
                "open": [
                    {"mint": m, **p} for m, p in (strategy.get("open") or {}).items()
                ],
                "report": report(strategy),
                "recent": strategy.get("closed", [])[-6:][::-1],
            }
        )
    return {"strategies": out, "reference_costs": cost_model(0.15)}

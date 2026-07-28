"""Bridge to the trading bot's on-disk state.

The bot is never imported and never spoken to over a socket. It already
communicates through files in its repo root, and this module speaks that same
protocol:

============================  =========================================
``open_position_<bot>.json``  live position written by the bot every ~2s
``sell_cmd_<bot>.json``       sell order the bot picks up and deletes
``trades/trades.log``         JSON-lines fill log (buys and sells)
``bots/<bot>.yaml``           per-bot strategy + filters
``logs/<bot>_<ts>.log``       rotating run logs
============================  =========================================

Writing ``sell_cmd`` is the same channel ``tg_panel.py`` already uses in
production, so a sell from this panel takes exactly the path that is known to
work rather than building a second, unproven one.
"""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from . import config

#: Bot names are used to build file paths, so they are restricted to the shape
#: the repo actually produces. This is the only defence against a model or a
#: crafted request walking out of the repo with "../".
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class BotLinkError(RuntimeError):
    """A request that names something the bot repo does not have."""


def _repo() -> Path:
    repo = config.BOT_REPO
    if not repo.is_dir():
        raise BotLinkError(f"No encuentro el repo del bot en {repo}")
    return repo


def _safe_name(name: str) -> str:
    name = (name or "").strip()
    if name.endswith(".yaml"):
        name = name[: -len(".yaml")]
    if not _SAFE_NAME.match(name):
        raise BotLinkError(f"Nombre de bot invalido: {name!r}")
    return name


def _read_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


# --------------------------------------------------------------------------- #
# Live positions                                                              #
# --------------------------------------------------------------------------- #


def live_positions() -> list[dict[str, Any]]:
    """Positions currently open, as the bots themselves report them.

    Each bot holds at most one position (``position_semaphore=1``), so the file
    suffix identifies the position uniquely. Stale marker files without live
    P&L fields are skipped.
    """
    rows: list[dict[str, Any]] = []
    for path in sorted(_repo().glob("open_position*.json")):
        record = _read_json(path)
        if not isinstance(record, dict) or "pnl_pct" not in record:
            continue
        suffix = str(record.get("suffix") or "")
        rows.append(
            {
                "bot": suffix.lstrip("_") or path.stem,
                "suffix": suffix,
                "mint": record.get("mint"),
                "symbol": record.get("symbol") or "?",
                "pnl_pct": _num(record.get("pnl_pct")),
                "value_sol": _num(record.get("value_sol")),
                "entry_price": _num(record.get("entry_price")),
                "price": _num(record.get("price")),
                "age_seconds": round(max(0.0, time.time() - path.stat().st_mtime), 1),
            }
        )
    rows.sort(key=lambda r: r["pnl_pct"], reverse=True)
    return rows


def request_sell(suffix: str, fraction: float) -> dict[str, Any]:
    """Ask the owning bot to sell ``fraction`` of what is left of its position.

    Writes the same ``sell_cmd`` file the Telegram panel writes. The bot polls
    for it, executes, and deletes it -- so this returns "queued", not "sold".
    """
    if not 0 < fraction <= 1:
        raise BotLinkError("La fraccion debe estar entre 0 y 1")
    position = next((p for p in live_positions() if p["suffix"] == suffix), None)
    if position is None:
        raise BotLinkError(f"No hay posicion abierta para {suffix!r}")

    safe_suffix = _safe_name(suffix.lstrip("_"))
    path = _repo() / f"sell_cmd_{safe_suffix}.json"
    payload = {"mint": position["mint"], "frac": fraction, "ts": time.time()}
    path.write_text(json.dumps(payload), encoding="utf-8")
    return {
        "queued": True,
        "bot": position["bot"],
        "symbol": position["symbol"],
        "mint": position["mint"],
        "fraction": fraction,
    }


# --------------------------------------------------------------------------- #
# Trade history / realised P&L                                                #
# --------------------------------------------------------------------------- #


def _load_trades() -> list[dict[str, Any]]:
    path = _repo() / "trades" / "trades.log"
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue  # the bot may be mid-write; a torn line is not an error
        if isinstance(event, dict) and event.get("action"):
            events.append(event)
    return events


def _positions_from_trades() -> dict[str, list[dict[str, Any]]]:
    """Collapse the fill log into per-token positions.

    ``price`` is SOL per token and ``amount`` is the token quantity, so their
    product is the SOL value of the leg. A token counts as closed once it has
    any sell -- the bot always exits in full or via tiered exits that end at
    zero.
    """
    by_mint: dict[str, dict[str, Any]] = {}
    for event in _load_trades():
        mint = event.get("token_address")
        if not mint:
            continue
        agg = by_mint.setdefault(
            mint,
            {
                "mint": mint,
                "symbol": event.get("symbol") or "?",
                "invested_sol": 0.0,
                "returned_sol": 0.0,
                "buys": 0,
                "sells": 0,
                "first_ts": event.get("timestamp"),
                "last_ts": event.get("timestamp"),
            },
        )
        if event.get("symbol"):
            agg["symbol"] = event["symbol"]
        agg["last_ts"] = event.get("timestamp") or agg["last_ts"]
        value = _num(event.get("price")) * _num(event.get("amount"))
        if event.get("action") == "buy":
            agg["invested_sol"] += value
            agg["buys"] += 1
        elif event.get("action") == "sell":
            agg["returned_sol"] += value
            agg["sells"] += 1

    open_rows: list[dict[str, Any]] = []
    closed_rows: list[dict[str, Any]] = []
    for agg in by_mint.values():
        realized = agg["returned_sol"] - agg["invested_sol"]
        row = {
            **agg,
            "invested_sol": round(agg["invested_sol"], 9),
            "returned_sol": round(agg["returned_sol"], 9),
            "pnl_sol": round(realized, 9),
            "pnl_pct": round(
                realized / agg["invested_sol"] * 100 if agg["invested_sol"] else 0.0, 2
            ),
        }
        (closed_rows if agg["sells"] else open_rows).append(row)

    closed_rows.sort(key=lambda r: r["last_ts"] or "", reverse=True)
    open_rows.sort(key=lambda r: r["first_ts"] or "", reverse=True)
    return {"open": open_rows, "closed": closed_rows}


def _within(row: dict[str, Any], since_iso: str | None) -> bool:
    if not since_iso:
        return True
    return bool(row.get("last_ts")) and str(row["last_ts"]) >= since_iso


def performance(since_hours: float | None = None) -> dict[str, Any]:
    """Realised results over the whole log, or over the last ``since_hours``."""
    since_iso = None
    if since_hours:
        # The bot logs naive UTC (``datetime.utcnow().isoformat()``), so the
        # cutoff has to be naive too for the string comparison to line up.
        cutoff = datetime.fromtimestamp(time.time() - since_hours * 3600, UTC)
        since_iso = cutoff.replace(tzinfo=None).isoformat()

    positions = _positions_from_trades()
    closed = [r for r in positions["closed"] if _within(r, since_iso)]
    wins = [r for r in closed if r["pnl_sol"] > 0]
    total = sum(r["pnl_sol"] for r in closed)
    invested = sum(r["invested_sol"] for r in closed)

    return {
        "window_hours": since_hours,
        "closed_count": len(closed),
        "wins": len(wins),
        "losses": len(closed) - len(wins),
        "win_rate_pct": round(len(wins) / len(closed) * 100, 2) if closed else 0.0,
        "realized_pnl_sol": round(total, 6),
        "invested_sol": round(invested, 6),
        "roi_pct": round(total / invested * 100, 2) if invested else 0.0,
        "avg_pnl_per_trade_sol": round(total / len(closed), 6) if closed else 0.0,
        "best": max(closed, key=lambda r: r["pnl_sol"], default=None),
        "worst": min(closed, key=lambda r: r["pnl_sol"], default=None),
        "still_open_from_log": len(positions["open"]),
    }


def recent_trades(limit: int = 25) -> list[dict[str, Any]]:
    """Most recently closed tokens, newest first."""
    return _positions_from_trades()["closed"][: max(1, min(limit, 200))]


# --------------------------------------------------------------------------- #
# Bot configs                                                                 #
# --------------------------------------------------------------------------- #


def _config_path(name: str) -> Path:
    path = _repo() / "bots" / f"{_safe_name(name)}.yaml"
    if not path.is_file():
        raise BotLinkError(f"No existe el config {path.name}")
    return path


def list_bots() -> list[dict[str, Any]]:
    """Every bot config with the fields that actually decide behaviour."""
    rows: list[dict[str, Any]] = []
    open_by_bot = {p["bot"]: p for p in live_positions()}
    for path in sorted((_repo() / "bots").glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            rows.append({"file": path.stem, "error": str(exc)[:200]})
            continue
        trade = data.get("trade", {}) or {}
        filters = data.get("filters", {}) or {}
        rows.append(
            {
                "file": path.stem,
                "name": data.get("name") or path.stem,
                "enabled": bool(data.get("enabled", True)),
                "platform": data.get("platform", "pump_fun"),
                "listener": filters.get("listener_type"),
                "buy_amount_sol": trade.get("buy_amount"),
                "exit_strategy": trade.get("exit_strategy"),
                "take_profit_pct": trade.get("take_profit_percentage"),
                "stop_loss_pct": trade.get("stop_loss_percentage"),
                "has_open_position": path.stem in open_by_bot,
            }
        )
    return rows


def read_config(name: str) -> dict[str, Any]:
    """Full YAML of one bot config, as a plain dict."""
    path = _config_path(name)
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def set_enabled(name: str, enabled: bool) -> dict[str, Any]:
    """Flip ``enabled`` in a bot's YAML.

    Takes effect on the next bot_runner start -- a running process keeps its
    loaded config, so pair this with a restart when you mean "stop now".
    """
    path = _config_path(name)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    data["enabled"] = bool(enabled)
    path.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    return {"file": path.stem, "enabled": bool(enabled), "needs_restart": True}


#: Only these may be written by the panel. Anything structural (listener type,
#: platform, wallet) stays a manual edit on purpose.
EDITABLE_TRADE_FIELDS = {
    "buy_amount": float,
    "take_profit_percentage": float,
    "stop_loss_percentage": float,
    "take_profit_sell_percentage": float,
    "buy_slippage": float,
    "sell_slippage": float,
    "max_token_age": float,
    "wait_time_after_creation": float,
}


def set_trade_field(name: str, field: str, value: Any) -> dict[str, Any]:
    """Update one whitelisted numeric field under ``trade:`` in a bot config."""
    caster = EDITABLE_TRADE_FIELDS.get(field)
    if caster is None:
        raise BotLinkError(
            f"Campo no editable desde el panel: {field}. "
            f"Permitidos: {', '.join(sorted(EDITABLE_TRADE_FIELDS))}"
        )
    try:
        typed = caster(value)
    except (TypeError, ValueError) as exc:
        raise BotLinkError(f"Valor invalido para {field}: {value!r}") from exc

    path = _config_path(name)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    trade = data.setdefault("trade", {})
    previous = trade.get(field)
    trade[field] = typed
    path.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    return {
        "file": path.stem,
        "field": field,
        "from": previous,
        "to": typed,
        "needs_restart": True,
    }


# --------------------------------------------------------------------------- #
# Logs                                                                        #
# --------------------------------------------------------------------------- #


def tail_log(name: str | None = None, lines: int = 40) -> dict[str, Any]:
    """Tail the newest log file, optionally for one bot."""
    log_dir = _repo() / "logs"
    if not log_dir.is_dir():
        raise BotLinkError("El bot no ha escrito todavia ningun log")
    pattern = f"{_safe_name(name)}_*.log" if name else "*.log"
    candidates = sorted(
        log_dir.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True
    )
    if not candidates:
        raise BotLinkError(f"Sin logs que encajen con {pattern}")

    newest = candidates[0]
    lines = max(1, min(lines, 400))
    tail = newest.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
    return {
        "file": newest.name,
        "modified_seconds_ago": round(time.time() - newest.stat().st_mtime, 1),
        "lines": tail,
    }

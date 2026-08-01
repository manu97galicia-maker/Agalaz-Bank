"""Natural-language control of the bot and the droplet, backed by Claude.

The safety model is the whole point of this file, so it is worth stating plainly:

* **Read tools run automatically.** Asking about P&L, positions, configs, logs
  or server health has no downside, so the agent just does it.
* **Anything that moves money, edits a config, restarts a service or runs a
  shell command does not execute.** Those tools only *queue a proposal*. The
  proposal is returned to the browser, you read exactly what it will do, and it
  runs only when you press Confirm.

That split is why the model is never handed a root shell in the usual sense: it
can compose an action, but it cannot take one. An LLM sitting one prompt
injection away from a wallet's private key is not a risk worth taking for the
convenience of skipping a button press.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from typing import Any

from . import botlink, chain, config, lists, market, ops, paper, profits, rules, wallet

logger = logging.getLogger(__name__)

#: Proposals expire so a queued sell cannot be confirmed an hour later against
#: a position that has completely changed.
PENDING_TTL_SECONDS = 600
MAX_HISTORY_MESSAGES = 24

SYSTEM_PROMPT = """\
Eres el copiloto de Manu para su bot de sniping de Solana (pump.fun / letsbonk),
que corre en su propio servidor. Hablas español, en corto y al grano.

Contexto del sistema:
- Varios bots corren en paralelo, cada uno con su YAML en `bots/`. Cada bot
  mantiene como mucho UNA posición abierta a la vez.
- Las posiciones abiertas las escribe el propio bot cada ~2s; el P&L realizado
  sale del log de fills (`trades/trades.log`) y es una estimación, no una
  auditoría on-chain. La foto real del wallet viene de la herramienta de cadena.
- Cambiar un YAML NO afecta a un bot que ya está corriendo: hay que reiniciarlo.
  Dilo siempre que propongas un cambio de config.
- La wallet es CALIENTE: puedes proponer comprar, vender y enviar SOL de verdad
  (firma real, vía Jupiter). Antes de proponer una compra, mira el token con
  get_token_info y di en una frase si tiene sentido (liquidez, rug, market cap).
- Las listas top_devs_*.json y blacklist_devs.json deciden a quién copia el bot;
  añadir o quitar una wallet cambia lo que compra. Trátalo con cuidado.
- ÓRDENES CONDICIONALES: cuando Manu diga "compra si...", "vende cuando...",
  "snipea esta si cumple...", "avísame si...", NO es una compra para ahora: es
  una orden que se queda esperando (`propose_rule`). Traduce lo que diga a
  números concretos sobre las métricas disponibles, y confirma en una frase
  cómo la has interpretado ("la armo para comprar 0,05 SOL si el mcap baja de
  30.000$"). Si falta el importe, el token o el umbral, PREGUNTA: una orden mal
  entendida gasta dinero solo. Cuando dispare, firmará sin volver a preguntar,
  así que dilo claro al proponerla. Las órdenes de comprar/vender necesitan la
  wallet caliente encendida; si está apagada, avisa de que quedará bloqueada.

- SIMULADOR EN PAPEL: "simula", "en papel", "a ver si esto funcionaría" NO es
  operar. Es `propose_paper_strategy`: posiciones de mentira con comisiones de
  verdad. Nunca mueve dinero, díselo claro.
- LAS COMISIONES SON EL TEMA. Cada operación son dos transacciones y el fee de
  prioridad es FIJO en SOL, no un porcentaje: cuanto más pequeña la posición,
  más se la come. Antes de opinar sobre si una estrategia sale a cuenta, llama
  a `cost_breakdown` y di cuánto tiene que subir sólo para empatar. Si el take
  profit que te pide está por debajo de ese punto, dilo sin rodeos: esa
  estrategia pierde por diseño aunque acierte la dirección.

Cómo trabajas:
- Usa las herramientas de lectura libremente antes de responder. No inventes
  cifras: si no has mirado, mira.
- Las herramientas que empiezan por `propose_` NO ejecutan nada; dejan una
  propuesta pendiente que Manu confirma a mano. Cuando uses una, di en una
  frase qué has dejado preparado y qué efecto tendrá. No digas que ya está hecho.
- Si te pide algo destructivo o ambiguo sobre dinero, propón la acción más
  conservadora que cumpla lo que pidió y dilo.

Estilo:
- Respuestas breves y centradas. Primero el resultado, después el detalle.
- Cifras con unidades (SOL, %, USD). Nada de tablas gigantes ni relleno.
- No te disculpes ni narres tu proceso. Si algo falla, di qué falló y sigue.
- Entrega lo que te piden al alcance que te lo piden: no amplíes la tarea por
  tu cuenta ni añadas pasos que nadie pidió.
"""


class AgentError(RuntimeError):
    """The agent could not be reached or is not configured."""


# --------------------------------------------------------------------------- #
# Pending actions                                                             #
# --------------------------------------------------------------------------- #

_PENDING: dict[str, dict[str, Any]] = {}


def _expire_pending() -> None:
    now = time.time()
    for key in [k for k, v in _PENDING.items() if v["expires_at"] < now]:
        _PENDING.pop(key, None)


def _queue(kind: str, summary: str, danger: str, run: Callable[[], Awaitable[Any]]) -> dict[str, Any]:
    """Register a proposal and return the record shown in the UI."""
    _expire_pending()
    action_id = secrets.token_urlsafe(9)
    record = {
        "id": action_id,
        "kind": kind,
        "summary": summary,
        "danger": danger,  # "low" | "medium" | "high"
        "created_at": time.time(),
        "expires_at": time.time() + PENDING_TTL_SECONDS,
        "_run": run,
    }
    _PENDING[action_id] = record
    return record


def pending_actions() -> list[dict[str, Any]]:
    """Proposals still awaiting confirmation, newest last."""
    _expire_pending()
    return [
        {k: v for k, v in record.items() if not k.startswith("_")}
        for record in sorted(_PENDING.values(), key=lambda r: r["created_at"])
    ]


async def confirm_action(action_id: str) -> dict[str, Any]:
    """Execute a queued proposal. Single-use: it is dropped either way."""
    _expire_pending()
    record = _PENDING.pop(action_id, None)
    if record is None:
        raise AgentError("Esa acción ya no existe (caducó o ya se ejecutó).")
    result = await record["_run"]()
    return {"executed": True, "kind": record["kind"], "summary": record["summary"], "result": result}


def cancel_action(action_id: str) -> bool:
    """Discard a queued proposal without running it."""
    return _PENDING.pop(action_id, None) is not None


# --------------------------------------------------------------------------- #
# Tool schemas                                                                #
# --------------------------------------------------------------------------- #

READ_TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_open_positions",
        "description": (
            "Posiciones abiertas ahora mismo con su P&L en vivo, según lo que "
            "escriben los bots. Úsala para '¿cómo voy?' o '¿qué tengo abierto?'."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_performance",
        "description": (
            "P&L realizado, win rate y ROI a partir del log de fills. Pasa "
            "since_hours para acotar la ventana (24 = hoy, 168 = semana)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "since_hours": {
                    "type": "number",
                    "description": "Ventana en horas. Omítelo para el histórico completo.",
                }
            },
        },
    },
    {
        "name": "get_recent_trades",
        "description": "Últimos tokens cerrados con su P&L, del más reciente al más antiguo.",
        "input_schema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "description": "1-200, por defecto 25"}},
        },
    },
    {
        "name": "get_wallet",
        "description": (
            "Foto real del wallet on-chain: saldo en SOL, tokens que aún tiene y "
            "su valor en USD vía Jupiter. Úsala cuando pregunte por el saldo, "
            "cuánto vale la cuenta, o si sospecha que un token se quedó colgado."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_bots",
        "description": "Todos los bots configurados: si están activos, su estrategia y si tienen posición.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "read_bot_config",
        "description": "YAML completo de un bot concreto (filtros, TP/SL, tamaño de compra).",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Nombre del fichero sin .yaml"}},
            "required": ["name"],
        },
    },
    {
        "name": "tail_logs",
        "description": "Últimas líneas del log más reciente, opcionalmente de un bot concreto.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Bot concreto; omítelo para el log más nuevo"},
                "lines": {"type": "integer", "description": "1-400, por defecto 40"},
            },
        },
    },
    {
        "name": "server_health",
        "description": (
            "Salud del servidor: carga, disco, memoria, procesos del bot vivos y "
            "estado de los servicios systemd permitidos."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "service_journal",
        "description": "Log de systemd (journalctl) de un servicio permitido.",
        "input_schema": {
            "type": "object",
            "properties": {
                "unit": {
                    "type": "string",
                    "description": "Nombre exacto de la unidad, p.ej. sniper-bot.service",
                },
                "lines": {"type": "integer"},
            },
            "required": ["unit"],
        },
    },
    {
        "name": "get_token_info",
        "description": (
            "Datos de mercado de un token por su mint: precio, market cap, "
            "liquidez, volumen (24h y 5m), cambios de precio y veredicto de rug "
            "(RugCheck), más enlaces a DexScreener/GMGN/Solscan. Úsala cuando "
            "pregunte por una moneda concreta o antes de proponer comprarla."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"mint": {"type": "string", "description": "Dirección del token"}},
            "required": ["mint"],
        },
    },
    {
        "name": "list_wallet_lists",
        "description": "Todas las listas de wallets del bot (devs, traders, blacklist) con su recuento.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "read_wallet_list",
        "description": "Direcciones de una lista concreta (p.ej. top_devs_final.json).",
        "input_schema": {
            "type": "object",
            "properties": {"file": {"type": "string"}},
            "required": ["file"],
        },
    },
    {
        "name": "get_profits_config",
        "description": "Config actual del reparto de ganancias: modo, destinatarios y %.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_rules",
        "description": "Las órdenes condicionales que están esperando ahora mismo.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_paper_strategies",
        "description": (
            "Estrategias del simulador en papel, con sus resultados en bruto, "
            "en neto y cuánto se han comido las comisiones."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "cost_breakdown",
        "description": (
            "Desglose de lo que cuesta una operación completa (comprar y vender) "
            "con la config real del bot: fee de prioridad, red, plataforma y "
            "slippage, y cuánto tiene que subir el token sólo para no perder. "
            "Úsalo siempre que se hable de si una estrategia sale a cuenta."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "size_sol": {"type": "number", "description": "Tamaño de la posición en SOL"},
                "slippage_pct": {"type": "number", "description": "Slippage esperado por lado, opcional"},
            },
            "required": ["size_sol"],
        },
    },
]

WRITE_TOOLS: list[dict[str, Any]] = [
    {
        "name": "propose_sell",
        "description": (
            "Deja preparada una venta de parte de una posición abierta. NO vende: "
            "Manu tiene que confirmarla. Usa el 'suffix' que devuelve "
            "get_open_positions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "suffix": {
                    "type": "string",
                    "description": "Identificador de la posición, p.ej. _bot-sniper-gemas",
                },
                "percent": {"type": "number", "description": "Porcentaje del RESTANTE a vender (1-100)"},
            },
            "required": ["suffix", "percent"],
        },
    },
    {
        "name": "propose_toggle_bot",
        "description": (
            "Prepara activar o desactivar un bot en su YAML. Requiere reinicio "
            "para que un proceso ya arrancado lo note. No ejecuta nada por sí sola."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "enabled": {"type": "boolean"},
            },
            "required": ["name", "enabled"],
        },
    },
    {
        "name": "propose_config_change",
        "description": (
            "Prepara el cambio de un parámetro numérico de trading de un bot. "
            "Campos válidos: buy_amount, take_profit_percentage, "
            "stop_loss_percentage, take_profit_sell_percentage, buy_slippage, "
            "sell_slippage, max_token_age, wait_time_after_creation. "
            "Los porcentajes van en fracción (0.4 = +40%). No ejecuta nada."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "field": {"type": "string"},
                "value": {"type": "number"},
            },
            "required": ["name", "field", "value"],
        },
    },
    {
        "name": "propose_buy",
        "description": (
            "Prepara una COMPRA real de un token con la wallet caliente: gasta "
            "SOL vía Jupiter. NO compra: deja la propuesta para que Manu confirme "
            "viendo el mint y el importe. Usa get_token_info antes para no comprar "
            "a ciegas. Hay un tope de seguridad de SOL por compra."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "mint": {"type": "string"},
                "sol": {"type": "number", "description": "SOL a gastar"},
            },
            "required": ["mint", "sol"],
        },
    },
    {
        "name": "propose_sell_token",
        "description": (
            "Prepara la VENTA real por SOL de un % de lo que la wallet tenga de un "
            "token (por mint), vía Jupiter. Distinto de propose_sell, que le pide "
            "al bot vender su posición; este vende directo desde la wallet. No "
            "ejecuta nada."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "mint": {"type": "string"},
                "percent": {"type": "number", "description": "1-100 del saldo de ese token"},
            },
            "required": ["mint", "percent"],
        },
    },
    {
        "name": "propose_send_sol",
        "description": (
            "Prepara un ENVÍO de SOL a otra wallet. No ejecuta nada; Manu confirma "
            "viendo destino e importe."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Dirección de destino"},
                "sol": {"type": "number"},
            },
            "required": ["to", "sol"],
        },
    },
    {
        "name": "propose_add_wallet",
        "description": (
            "Prepara añadir una wallet a una lista del bot (top_devs_*.json o "
            "blacklist_devs.json). Cambia a quién compra el bot. No ejecuta nada."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "Fichero de lista"},
                "address": {"type": "string"},
                "note": {"type": "string"},
            },
            "required": ["file", "address"],
        },
    },
    {
        "name": "propose_remove_wallet",
        "description": "Prepara quitar una wallet de una lista del bot. No ejecuta nada.",
        "input_schema": {
            "type": "object",
            "properties": {"file": {"type": "string"}, "address": {"type": "string"}},
            "required": ["file", "address"],
        },
    },
    {
        "name": "propose_restart_service",
        "description": "Prepara el reinicio de un servicio systemd permitido. No ejecuta nada.",
        "input_schema": {
            "type": "object",
            "properties": {"unit": {"type": "string"}},
            "required": ["unit"],
        },
    },
    {
        "name": "propose_stop_service",
        "description": "Prepara la parada de un servicio systemd permitido. No ejecuta nada.",
        "input_schema": {
            "type": "object",
            "properties": {"unit": {"type": "string"}},
            "required": ["unit"],
        },
    },
    {
        "name": "propose_rule",
        "description": (
            "Prepara una ORDEN CONDICIONAL: algo que se ejecutará solo, más "
            "adelante, cuando el mercado cumpla unas condiciones. Es lo que hay "
            "que usar cuando Manu dice 'compra si...', 'vende cuando...', "
            "'avísame si...'. No compra ni vende ahora: deja la orden esperando. "
            "Traduce lo que te diga a condiciones numéricas concretas; si te "
            "falta el importe, el mint o el umbral, PREGUNTA antes de proponer."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "mint": {"type": "string", "description": "Dirección del token a vigilar"},
                "action": {
                    "type": "string",
                    "enum": ["buy", "sell", "alert"],
                    "description": "buy y sell firman de verdad; alert sólo notifica",
                },
                "sol": {"type": "number", "description": "SOL a gastar (sólo con action=buy)"},
                "percent": {
                    "type": "number",
                    "description": "Porcentaje del saldo a vender (sólo con action=sell)",
                },
                "conditions": {
                    "type": "array",
                    "description": "Todas se tienen que cumplir a la vez (Y, no O)",
                    "items": {
                        "type": "object",
                        "properties": {
                            "metric": {
                                "type": "string",
                                "enum": list(rules.METRICS),
                            },
                            "op": {"type": "string", "enum": list(rules.OPS)},
                            "value": {"type": "number"},
                        },
                        "required": ["metric", "op", "value"],
                    },
                },
                "expires_minutes": {
                    "type": "integer",
                    "description": "Caduca sola si no se cumple en este rato. Opcional.",
                },
                "text": {
                    "type": "string",
                    "description": "La orden tal y como la dijo Manu, para que la reconozca",
                },
            },
            "required": ["mint", "action", "conditions"],
        },
    },
    {
        "name": "propose_paper_strategy",
        "description": (
            "Prepara una estrategia para el SIMULADOR EN PAPEL. No mueve dinero "
            "jamás: abre y cierra posiciones de mentira con comisiones de verdad, "
            "para ver si la idea aguanta los costes. Es lo que hay que usar "
            "cuando Manu dice 'simula', 'en papel', 'a ver si esto funcionaría'. "
            "Antes de proponerla, llama a cost_breakdown con el tamaño de "
            "posición y dile en una frase cuánto tiene que subir sólo para "
            "empatar: si el take profit que pide está por debajo de eso, avísale "
            "de que la estrategia pierde por diseño."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Nombre corto para reconocerla"},
                "mints": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Tokens que vigila la estrategia",
                },
                "entry": {
                    "type": "array",
                    "description": "Condiciones de entrada; todas a la vez (Y, no O)",
                    "items": {
                        "type": "object",
                        "properties": {
                            "metric": {"type": "string", "enum": list(rules.METRICS)},
                            "op": {"type": "string", "enum": list(rules.OPS)},
                            "value": {"type": "number"},
                        },
                        "required": ["metric", "op", "value"],
                    },
                },
                "size_sol": {"type": "number", "description": "SOL por posición"},
                "take_profit_pct": {"type": "number"},
                "stop_loss_pct": {"type": "number"},
                "max_hold_minutes": {"type": "integer"},
                "slippage_pct": {"type": "number", "description": "Slippage esperado por lado"},
                "text": {"type": "string", "description": "La estrategia tal y como la dijo Manu"},
            },
            "required": ["name", "mints", "entry"],
        },
    },
    {
        "name": "propose_cancel_rule",
        "description": "Prepara la cancelación de una orden condicional por su id.",
        "input_schema": {
            "type": "object",
            "properties": {"rule_id": {"type": "string"}},
            "required": ["rule_id"],
        },
    },
]

SHELL_TOOL: dict[str, Any] = {
    "name": "propose_shell",
    "description": (
        "Último recurso: prepara un comando de shell en el directorio del bot "
        "para algo que ninguna otra herramienta cubre. No ejecuta nada; Manu ve "
        "el comando exacto y decide. Explica siempre por qué hace falta."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "why": {"type": "string", "description": "Qué pretendes conseguir"},
        },
        "required": ["command", "why"],
    },
}


def _tools() -> list[dict[str, Any]]:
    tools = READ_TOOLS + WRITE_TOOLS
    if config.AGENT_ALLOW_SHELL:
        tools.append(SHELL_TOOL)
    return tools


# --------------------------------------------------------------------------- #
# Tool execution                                                              #
# --------------------------------------------------------------------------- #


async def _execute_tool(name: str, args: dict[str, Any]) -> Any:
    """Run a read tool, or queue a write tool and describe what was queued."""
    if name == "get_open_positions":
        return botlink.live_positions()
    if name == "get_performance":
        return botlink.performance(args.get("since_hours"))
    if name == "get_recent_trades":
        return botlink.recent_trades(int(args.get("limit", 25)))
    if name == "get_wallet":
        return await chain.wallet_snapshot()
    if name == "list_bots":
        return botlink.list_bots()
    if name == "read_bot_config":
        return botlink.read_config(args["name"])
    if name == "tail_logs":
        return botlink.tail_log(args.get("name"), int(args.get("lines", 40)))
    if name == "server_health":
        return await ops.health()
    if name == "service_journal":
        return await ops.journal(args["unit"], int(args.get("lines", 60)))
    if name == "get_token_info":
        return await market.token_info(str(args["mint"]))
    if name == "list_wallet_lists":
        return lists.list_files()
    if name == "read_wallet_list":
        return lists.read_list(str(args["file"]))
    if name == "get_profits_config":
        return profits.get_config()

    if name == "propose_sell":
        suffix = str(args["suffix"])
        percent = float(args["percent"])
        if not 0 < percent <= 100:
            raise ValueError("El porcentaje debe estar entre 0 y 100")
        position = next((p for p in botlink.live_positions() if p["suffix"] == suffix), None)
        if position is None:
            raise botlink.BotLinkError(f"No hay posicion abierta con suffix {suffix!r}")
        fraction = percent / 100
        record = _queue(
            "sell",
            f"Vender {percent:g}% de {position['symbol']} ({position['bot']}), "
            f"P&L actual {position['pnl_pct']:+.1f}%, ~{position['value_sol']:.3f} SOL en juego",
            "high",
            lambda: _as_async(botlink.request_sell, suffix, fraction),
        )
        return {
            "queued": True,
            "action_id": record["id"],
            "note": "Pendiente de confirmacion por Manu. No se ha vendido nada todavia.",
        }

    if name == "propose_toggle_bot":
        bot_name = str(args["name"])
        enabled = bool(args["enabled"])
        verb = "Activar" if enabled else "Desactivar"
        record = _queue(
            "toggle_bot",
            f"{verb} el bot {bot_name} (requiere reiniciar el proceso para aplicarse)",
            "medium",
            lambda: _as_async(botlink.set_enabled, bot_name, enabled),
        )
        return {"queued": True, "action_id": record["id"], "note": "Pendiente de confirmacion."}

    if name == "propose_config_change":
        bot_name = str(args["name"])
        field = str(args["field"])
        value = float(args["value"])
        if field not in botlink.EDITABLE_TRADE_FIELDS:
            raise botlink.BotLinkError(
                f"Campo no editable: {field}. Permitidos: "
                f"{', '.join(sorted(botlink.EDITABLE_TRADE_FIELDS))}"
            )
        current = (botlink.read_config(bot_name).get("trade") or {}).get(field)
        record = _queue(
            "config",
            f"{bot_name}: {field} {current!r} -> {value!r} (requiere reinicio)",
            "medium",
            lambda: _as_async(botlink.set_trade_field, bot_name, field, value),
        )
        return {"queued": True, "action_id": record["id"], "note": "Pendiente de confirmacion."}

    if name == "propose_buy":
        mint = str(args["mint"]).strip()
        sol = float(args["sol"])
        record = _queue(
            "buy",
            f"COMPRAR {sol:g} SOL de {mint[:8]}… (firma real, vía Jupiter)",
            "high",
            lambda: wallet.buy(mint, sol),
        )
        return {"queued": True, "action_id": record["id"], "note": "Pendiente. No se ha comprado nada."}

    if name == "propose_sell_token":
        mint = str(args["mint"]).strip()
        percent = float(args["percent"])
        record = _queue(
            "sell_token",
            f"VENDER {percent:g}% del saldo de {mint[:8]}… por SOL (firma real)",
            "high",
            lambda: wallet.sell(mint, percent),
        )
        return {"queued": True, "action_id": record["id"], "note": "Pendiente. No se ha vendido nada."}

    if name == "propose_send_sol":
        to = str(args["to"]).strip()
        sol = float(args["sol"])
        record = _queue(
            "send_sol",
            f"ENVIAR {sol:g} SOL a {to[:8]}… (firma real, irreversible)",
            "high",
            lambda: wallet.send_sol(to, sol),
        )
        return {"queued": True, "action_id": record["id"], "note": "Pendiente. No se ha enviado nada."}

    if name == "propose_add_wallet":
        file = str(args["file"]).strip()
        address = str(args["address"]).strip()
        note = str(args.get("note", ""))
        record = _queue(
            "list_add",
            f"Añadir {address[:8]}… a {file} (cambia a quién compra el bot)",
            "medium",
            lambda: _as_async(lists.add_wallet, file, address, note),
        )
        return {"queued": True, "action_id": record["id"], "note": "Pendiente de confirmacion."}

    if name == "propose_remove_wallet":
        file = str(args["file"]).strip()
        address = str(args["address"]).strip()
        record = _queue(
            "list_remove",
            f"Quitar {address[:8]}… de {file}",
            "medium",
            lambda: _as_async(lists.remove_wallet, file, address),
        )
        return {"queued": True, "action_id": record["id"], "note": "Pendiente de confirmacion."}

    if name == "propose_restart_service":
        unit = str(args["unit"])
        record = _queue(
            "restart", f"Reiniciar el servicio {unit}", "high",
            lambda: ops.restart_unit(unit),
        )
        return {"queued": True, "action_id": record["id"], "note": "Pendiente de confirmacion."}

    if name == "propose_stop_service":
        unit = str(args["unit"])
        record = _queue(
            "stop", f"PARAR el servicio {unit} (deja de operar)", "high",
            lambda: ops.stop_unit(unit),
        )
        return {"queued": True, "action_id": record["id"], "note": "Pendiente de confirmacion."}

    if name == "list_rules":
        return rules.summary()

    if name == "list_paper_strategies":
        return paper.summary()

    if name == "cost_breakdown":
        return paper.cost_model(float(args["size_sol"]), slippage_pct=args.get("slippage_pct"))

    if name == "propose_paper_strategy":
        draft = {
            "name": str(args["name"]),
            "mints": list(args["mints"]),
            "entry": args["entry"],
            "text": str(args.get("text") or ""),
        }
        for key in ("size_sol", "take_profit_pct", "stop_loss_pct", "max_hold_minutes", "slippage_pct"):
            if args.get(key) is not None:
                draft[key] = args[key]
        size = float(draft.get("size_sol", 0.15))
        costs = paper.cost_model(size, slippage_pct=draft.get("slippage_pct"))
        record = _queue(
            "paper",
            f"SIMULAR en papel «{draft['name']}» con {size:g} SOL por posición "
            f"(no mueve dinero; empata en +{costs['breakeven_pct']:.1f}%)",
            "low",
            lambda: _as_async(paper.create, **draft),
        )
        return {
            "queued": True,
            "action_id": record["id"],
            "breakeven_pct": costs["breakeven_pct"],
            "note": "Pendiente. Es simulación: no se compra nada de verdad.",
        }

    if name == "propose_rule":
        # Se valida AQUÍ, al proponer, no al confirmar: si el umbral o el
        # importe no cuadran, Manu tiene que verlo antes de darle a aceptar,
        # no descubrirlo con un error después.
        action = str(args["action"]).strip().lower()
        draft = {
            "mint": str(args["mint"]).strip(),
            "action": action,
            "conditions": args.get("conditions"),
            "sol": args.get("sol"),
            "percent": args.get("percent"),
            "text": str(args.get("text") or ""),
            "expires_minutes": args.get("expires_minutes"),
        }
        try:
            preview = rules.describe({**draft, "conditions": rules._clean_conditions(draft["conditions"])})  # noqa: SLF001
        except rules.RuleError as exc:
            return {"error": str(exc), "queued": False}

        danger = "low" if action == "alert" else "high"
        firma = "" if action == "alert" else " — firmará de verdad, sin preguntar otra vez"
        record = _queue(
            "rule",
            f"ORDEN CONDICIONAL: {preview}{firma}",
            danger,
            lambda: _as_async(rules.create, **draft),
        )
        return {
            "queued": True,
            "action_id": record["id"],
            "note": "Pendiente. La orden aún no está armada.",
        }

    if name == "propose_cancel_rule":
        rule_id = str(args["rule_id"]).strip()
        record = _queue(
            "rule_cancel", f"CANCELAR la orden condicional {rule_id}", "low",
            lambda: _as_async(rules.cancel, rule_id),
        )
        return {"queued": True, "action_id": record["id"], "note": "Pendiente de confirmacion."}

    if name == "propose_shell":
        command = str(args["command"])
        record = _queue(
            "shell", f"Ejecutar en {config.BOT_REPO}:  {command}", "high",
            lambda: ops.run_shell(command),
        )
        return {
            "queued": True,
            "action_id": record["id"],
            "note": "Pendiente de confirmacion. Manu vera el comando exacto antes de nada.",
        }

    raise AgentError(f"Herramienta desconocida: {name}")


async def _as_async(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Adapt the synchronous botlink helpers to the async confirmation queue."""
    return func(*args, **kwargs)


# --------------------------------------------------------------------------- #
# Conversation                                                                #
# --------------------------------------------------------------------------- #

_client: Any = None
_anthropic_module: Any = None
_fallbacks_supported = True


def _anthropic() -> Any:
    """Import perezoso del SDK, como se hace con solders y webauthn.

    Importarlo arriba era una bomba: si la rueda no estaba instalada, el
    ``import`` reventaba al cargar el módulo y **no arrancaba el panel entero**
    -- ni el wallet, ni las posiciones, ni el login -- por no tener el chat.
    Ahora la falta del paquete sólo rompe el chat, y lo dice.
    """
    global _anthropic_module
    if _anthropic_module is None:
        try:
            import anthropic  # noqa: PLC0415
        except ImportError as exc:
            raise AgentError(
                "Falta el paquete 'anthropic': el chat no funciona (el resto del "
                "panel sí). Instálalo con:  pip install anthropic"
            ) from exc
        _anthropic_module = anthropic
    return _anthropic_module


def _get_client() -> Any:
    global _client
    if not config.ANTHROPIC_API_KEY:
        raise AgentError(
            "Falta ANTHROPIC_API_KEY en el .env del panel: sin ella el chat en "
            "lenguaje natural no funciona (el resto del panel sí)."
        )
    if _client is None:
        _client = _anthropic().AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


async def _create_message(**kwargs: Any) -> Any:
    """Call the API, opting into server-side fallbacks when the org allows it.

    Claude Opus 5's safety classifiers can decline a request outright; the
    ``fallbacks`` parameter re-runs it on another model server-side. If the beta
    is not enabled for this key the first call 400s once, and we remember not to
    ask again.
    """
    global _fallbacks_supported
    client = _get_client()
    if _fallbacks_supported:
        try:
            return await client.beta.messages.create(
                betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs
            )
        except _anthropic().BadRequestError:
            logger.info("Fallbacks del servidor no disponibles; sigo sin ellos.")
            _fallbacks_supported = False
    return await client.messages.create(**kwargs)


def _snapshot() -> str:
    """A cheap situational summary so the agent starts oriented, not blind."""
    parts: list[str] = []
    try:
        positions = botlink.live_positions()
        if positions:
            parts.append(
                "Posiciones abiertas: "
                + "; ".join(
                    f"{p['symbol']} ({p['bot']}, suffix {p['suffix']}) "
                    f"{p['pnl_pct']:+.1f}% ~{p['value_sol']:.3f} SOL"
                    for p in positions
                )
            )
        else:
            parts.append("Posiciones abiertas: ninguna.")
        bots = botlink.list_bots()
        active = [b["file"] for b in bots if b.get("enabled")]
        parts.append(f"Bots activos ({len(active)} de {len(bots)}): {', '.join(active) or 'ninguno'}")
    except botlink.BotLinkError as exc:
        parts.append(f"No pude leer el estado del bot: {exc}")
    return "\n".join(parts)


def _text_of(response: Any) -> str:
    return "\n".join(b.text for b in response.content if b.type == "text").strip()


def _starts_a_turn(message: dict[str, Any]) -> bool:
    """True if the conversation can legally begin at this message.

    That means a real user turn -- not an assistant reply, and not the synthetic
    user message that carries ``tool_result`` blocks back to the model.
    """
    if message.get("role") != "user":
        return False
    content = message.get("content")
    if isinstance(content, str):
        return True
    return not any(
        isinstance(block, dict) and block.get("type") == "tool_result"
        for block in content or []
    )


def _trim(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Bound the history without splitting a tool_use from its tool_result.

    A naive tail slice can start the conversation on an assistant turn, or on
    the tool_result half of a pair whose tool_use was just dropped -- both are a
    400 from the API. So after slicing, walk forward to the next clean start.
    """
    trimmed = list(history)[-MAX_HISTORY_MESSAGES:]
    while trimmed and not _starts_a_turn(trimmed[0]):
        trimmed.pop(0)
    return trimmed


async def chat(history: list[dict[str, Any]], message: str) -> dict[str, Any]:
    """Run one user turn to completion, executing tools as the model asks.

    ``history`` is the raw ``messages`` list from previous turns; the caller owns
    it (it lives in the session). Returns the reply, the updated history and any
    proposals that are now waiting for confirmation.
    """
    tools = _tools()
    messages: list[dict[str, Any]] = _trim(history)
    # The snapshot goes in the user turn, after the cached system prompt, so it
    # never invalidates the cached prefix.
    messages.append(
        {
            "role": "user",
            "content": [
                {"type": "text", "text": f"<estado_actual>\n{_snapshot()}\n</estado_actual>"},
                {"type": "text", "text": message},
            ],
        }
    )

    queued_before = {a["id"] for a in pending_actions()}
    reply = ""

    for _ in range(config.AGENT_MAX_ROUNDS):
        try:
            response = await _create_message(
                model=config.AGENT_MODEL,
                max_tokens=config.AGENT_MAX_TOKENS,
                system=[
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                output_config={"effort": config.AGENT_EFFORT},
                tools=tools,
                messages=messages,
            )
        except _anthropic().APIStatusError as exc:
            raise AgentError(f"La API de Claude devolvio {exc.status_code}: {exc.message}") from exc
        except _anthropic().APIConnectionError as exc:
            raise AgentError(f"No pude hablar con la API de Claude: {exc}") from exc

        if response.stop_reason == "refusal":
            raise AgentError("Claude ha rechazado esta peticion por politica de seguridad.")

        messages.append({"role": "assistant", "content": response.content})
        reply = _text_of(response) or reply

        tool_uses = [block for block in response.content if block.type == "tool_use"]
        if not tool_uses:
            break

        results: list[dict[str, Any]] = []
        for block in tool_uses:
            try:
                output = await _execute_tool(block.name, dict(block.input or {}))
                content = json.dumps(output, ensure_ascii=False, default=str)[:24000]
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": content})
            except (
                botlink.BotLinkError,
                chain.ChainError,
                ops.OpsError,
                wallet.WalletError,
                market.MarketError,
                lists.ListError,
                profits.ProfitError,
                ValueError,
                KeyError,
            ) as exc:
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": f"Error: {exc}",
                        "is_error": True,
                    }
                )
            except Exception as exc:  # noqa: BLE001 -- never kill the turn on one bad tool
                logger.exception("Fallo la herramienta %s", block.name)
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": f"Error inesperado: {exc}",
                        "is_error": True,
                    }
                )
        messages.append({"role": "user", "content": results})
    else:
        reply = (reply + "\n\n_(Corté aquí: demasiadas vueltas de herramientas.)_").strip()

    new_pending = [a for a in pending_actions() if a["id"] not in queued_before]
    return {
        "reply": reply or "(sin respuesta)",
        "history": messages,
        "pending": new_pending,
    }

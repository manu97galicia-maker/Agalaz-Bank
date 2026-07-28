"""HTTP layer: auth middleware, JSON API and the single-page UI.

Every route except the login page requires a valid session cookie. There is no
"localhost is trusted" shortcut -- this panel is meant to be reached from a
phone over the internet, so the same rules apply to every caller.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from aiohttp import web

from . import agent, auth, botlink, chain, config, ops

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
PUBLIC_ROUTES = frozenset({("GET", "/"), ("POST", "/api/login"), ("GET", "/api/ping")})

#: Per-session agent conversations. In memory on purpose: the history holds a
#: live picture of the wallet and has no business being written to disk.
_SESSIONS: dict[str, dict[str, Any]] = {}
_SESSION_TTL = 60 * 60 * 12


def _session(request: web.Request) -> dict[str, Any]:
    token = request.cookies.get(auth.COOKIE, "")
    now = time.time()
    for key in [k for k, v in _SESSIONS.items() if now - v["seen"] > _SESSION_TTL]:
        _SESSIONS.pop(key, None)
    entry = _SESSIONS.setdefault(token, {"history": [], "seen": now})
    entry["seen"] = now
    return entry


def _client_ip(request: web.Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote or "?"


# --------------------------------------------------------------------------- #
# Middleware                                                                  #
# --------------------------------------------------------------------------- #


@web.middleware
async def auth_middleware(
    request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
) -> web.StreamResponse:
    """Reject anything without a valid session, except the login surface."""
    if (request.method, request.path) in PUBLIC_ROUTES or request.path.startswith("/favicon"):
        return await handler(request)
    if request.app["auth"].token_valid(request.cookies.get(auth.COOKIE)):
        return await handler(request)
    return web.json_response({"ok": False, "error": "login requerido"}, status=401)


@web.middleware
async def error_middleware(
    request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
) -> web.StreamResponse:
    """Turn the domain errors into clean 4xx JSON instead of a 500 + traceback."""
    try:
        return await handler(request)
    except (botlink.BotLinkError, chain.ChainError, ops.OpsError, agent.AgentError) as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=400)
    except web.HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 -- a panel that 500s silently is useless
        logger.exception("Fallo no controlado en %s", request.path)
        return web.json_response({"ok": False, "error": f"Error interno: {exc}"}, status=500)


# --------------------------------------------------------------------------- #
# Auth routes                                                                 #
# --------------------------------------------------------------------------- #


async def handle_index(request: web.Request) -> web.Response:
    return web.Response(
        text=(STATIC_DIR / "index.html").read_text(encoding="utf-8"),
        content_type="text/html",
    )


async def handle_ping(request: web.Request) -> web.Response:
    """Unauthenticated liveness probe. Leaks nothing."""
    return web.json_response({"ok": True})


async def handle_login(request: web.Request) -> web.Response:
    body = await request.json()
    ok, message = request.app["auth"].check(
        _client_ip(request), str(body.get("user", "")), str(body.get("password", ""))
    )
    if not ok:
        logger.warning("Login fallido desde %s", _client_ip(request))
        return web.json_response({"ok": False, "error": message}, status=401)

    token = request.app["auth"].issue_token()
    _SESSIONS[token] = {"history": [], "seen": time.time()}
    response = web.json_response({"ok": True})
    response.set_cookie(
        auth.COOKIE,
        token,
        httponly=True,
        secure=config.COOKIE_SECURE,
        samesite="Lax",
        max_age=config.SESSION_TTL_SECONDS,
        path="/",
    )
    return response


async def handle_logout(request: web.Request) -> web.Response:
    _SESSIONS.pop(request.cookies.get(auth.COOKIE, ""), None)
    response = web.json_response({"ok": True})
    response.del_cookie(auth.COOKIE, path="/")
    return response


# --------------------------------------------------------------------------- #
# Read routes                                                                 #
# --------------------------------------------------------------------------- #


async def handle_state(request: web.Request) -> web.Response:
    """Everything the dashboard needs in one round trip."""

    def _safe(func: Callable[[], Any]) -> Any:
        try:
            return func()
        except (botlink.BotLinkError, OSError) as exc:
            return {"error": str(exc)}

    return web.json_response(
        {
            "ok": True,
            "config": config.public_config(),
            "positions": _safe(botlink.live_positions),
            "today": _safe(lambda: botlink.performance(24)),
            "all_time": _safe(lambda: botlink.performance(None)),
            "bots": _safe(botlink.list_bots),
            "pending": agent.pending_actions(),
            "warnings": request.app["auth"].warnings,
        }
    )


async def handle_wallet(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "wallet": await chain.wallet_snapshot()})


async def handle_trades(request: web.Request) -> web.Response:
    limit = int(request.query.get("limit", "25"))
    return web.json_response({"ok": True, "trades": botlink.recent_trades(limit)})


async def handle_bot_config(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    return web.json_response({"ok": True, "name": name, "config": botlink.read_config(name)})


async def handle_logs(request: web.Request) -> web.Response:
    return web.json_response(
        {
            "ok": True,
            **botlink.tail_log(
                request.query.get("name") or None, int(request.query.get("lines", "60"))
            ),
        }
    )


async def handle_server(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "health": await ops.health()})


async def handle_journal(request: web.Request) -> web.Response:
    unit = request.query.get("unit", "")
    return web.json_response(
        {"ok": True, **await ops.journal(unit, int(request.query.get("lines", "60")))}
    )


# --------------------------------------------------------------------------- #
# Write routes                                                                #
# --------------------------------------------------------------------------- #


async def handle_sell(request: web.Request) -> web.Response:
    """Sell part of an open position.

    This one executes immediately and on purpose: it is a button you pressed
    with the position and its P&L on screen, which is the same confirmation the
    agent's proposals exist to obtain.
    """
    body = await request.json()
    percent = float(body["percent"])
    if not 0 < percent <= 100:
        raise botlink.BotLinkError("El porcentaje debe estar entre 0 y 100")
    result = botlink.request_sell(str(body["suffix"]), percent / 100)
    logger.info("Venta encolada desde el panel: %s", result)
    return web.json_response({"ok": True, **result})


async def handle_toggle_bot(request: web.Request) -> web.Response:
    body = await request.json()
    return web.json_response(
        {"ok": True, **botlink.set_enabled(str(body["name"]), bool(body["enabled"]))}
    )


# --------------------------------------------------------------------------- #
# Agent routes                                                                #
# --------------------------------------------------------------------------- #


async def handle_chat(request: web.Request) -> web.Response:
    body = await request.json()
    message = str(body.get("message", "")).strip()
    if not message:
        return web.json_response({"ok": False, "error": "Mensaje vacio"}, status=400)

    session = _session(request)
    lock: asyncio.Lock = session.setdefault("lock", asyncio.Lock())
    if lock.locked():
        return web.json_response(
            {"ok": False, "error": "Ya hay una peticion en curso; espera a que termine."},
            status=429,
        )
    async with lock:
        result = await agent.chat(session["history"], message)
    session["history"] = result["history"]
    return web.json_response(
        {"ok": True, "reply": result["reply"], "pending": result["pending"]}
    )


async def handle_pending(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "pending": agent.pending_actions()})


async def handle_confirm(request: web.Request) -> web.Response:
    body = await request.json()
    action_id = str(body["id"])
    result = await agent.confirm_action(action_id)
    logger.warning("Accion confirmada: %s", result["summary"])
    return web.json_response({"ok": True, **result})


async def handle_cancel(request: web.Request) -> web.Response:
    body = await request.json()
    return web.json_response({"ok": True, "cancelled": agent.cancel_action(str(body["id"]))})


async def handle_reset_chat(request: web.Request) -> web.Response:
    _session(request)["history"] = []
    return web.json_response({"ok": True})


# --------------------------------------------------------------------------- #
# App                                                                         #
# --------------------------------------------------------------------------- #


def create_app() -> web.Application:
    app = web.Application(middlewares=[error_middleware, auth_middleware])
    app["auth"] = auth.Auth.from_config()

    app.router.add_get("/", handle_index)
    app.router.add_get("/api/ping", handle_ping)
    app.router.add_post("/api/login", handle_login)
    app.router.add_post("/api/logout", handle_logout)

    app.router.add_get("/api/state", handle_state)
    app.router.add_get("/api/wallet", handle_wallet)
    app.router.add_get("/api/trades", handle_trades)
    app.router.add_get("/api/bots/{name}", handle_bot_config)
    app.router.add_get("/api/logs", handle_logs)
    app.router.add_get("/api/server", handle_server)
    app.router.add_get("/api/journal", handle_journal)

    app.router.add_post("/api/sell", handle_sell)
    app.router.add_post("/api/bots/toggle", handle_toggle_bot)

    app.router.add_post("/api/agent/chat", handle_chat)
    app.router.add_get("/api/agent/pending", handle_pending)
    app.router.add_post("/api/agent/confirm", handle_confirm)
    app.router.add_post("/api/agent/cancel", handle_cancel)
    app.router.add_post("/api/agent/reset", handle_reset_chat)
    return app


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    app = create_app()
    for warning in app["auth"].warnings:
        logger.warning(warning)
    logger.info("Panel en http://%s:%s (repo del bot: %s)", config.HOST, config.PORT, config.BOT_REPO)
    web.run_app(app, host=config.HOST, port=config.PORT, print=None)


if __name__ == "__main__":
    main()

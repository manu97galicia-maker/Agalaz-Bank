"""HTTP layer: auth middleware, JSON API and the single-page UI.

Every route except the login page requires a valid session, carried either in a
cookie or in an ``Authorization: Bearer`` header (see :func:`_request_token`).
There is no "localhost is trusted" shortcut -- this panel is meant to be reached
from a phone over the internet, so the same rules apply to every caller.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from aiohttp import web

from . import (
    agent,
    auth,
    botlink,
    chain,
    config,
    lists,
    market,
    notify,
    ops,
    profits,
    rules,
    users,
    wallet,
    webauthn_face,
)

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
PUBLIC_ROUTES = frozenset(
    {
        ("GET", "/"),
        ("POST", "/api/login"),
        ("GET", "/api/ping"),
        ("GET", "/api/bootstrap"),
        ("POST", "/api/face/login/options"),
        ("POST", "/api/face/login/verify"),
    }
)

#: Per-session agent conversations. In memory on purpose: the history holds a
#: live picture of the wallet and has no business being written to disk.
_SESSIONS: dict[str, dict[str, Any]] = {}
_SESSION_TTL = 60 * 60 * 12


def _session(request: web.Request) -> dict[str, Any]:
    token = _request_token(request)
    now = time.time()
    for key in [k for k, v in _SESSIONS.items() if now - v["seen"] > _SESSION_TTL]:
        _SESSIONS.pop(key, None)
    entry = _SESSIONS.setdefault(token, {"history": [], "seen": now})
    entry["seen"] = now
    return entry


def _client_ip(request: web.Request) -> str:
    """Identidad de origen para el bloqueo por intentos fallidos.

    Se coge el ULTIMO valor de ``X-Forwarded-For``, no el primero. El primero lo
    pone el cliente y por tanto se puede falsificar: bastaria con mandar una IP
    distinta en cada intento para saltarse el bloqueo por completo. El ultimo lo
    añade nuestro propio Caddy con la direccion real del que llama, y eso no se
    puede tocar desde fuera.

    Detras del proxy de Vercel esto identifica a Vercel, no al navegador, asi que
    el bloqueo pasa a ser global en vez de por visitante. Es el precio de tener
    una web publica delante, y sigue frenando la fuerza bruta.
    """
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[-1].strip()
    return request.remote or "?"


def _request_token(request: web.Request) -> str:
    """Token de sesión de la petición.

    Se lee primero de la cabecera ``Authorization: Bearer`` y, si no, de la
    cookie. La cabecera es imprescindible cuando el panel se sirve detrás del
    proxy de Vercel: sus *rewrites* hacia un backend externo (el droplet) se
    comen la cabecera ``Set-Cookie`` de la respuesta, así que la cookie nunca
    llega al navegador. Devolviendo el token en el cuerpo del login y
    reenviándolo aquí como Bearer, la sesión sobrevive al proxy. La cookie se
    mantiene para el acceso directo por dominio (Caddy) sin proxy de por medio.
    """
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        token = header[7:].strip()
        if token:
            return token
    return request.cookies.get(auth.COOKIE, "")


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
    if request.app["auth"].token_valid(_request_token(request)):
        return await handler(request)
    return web.json_response({"ok": False, "error": "login requerido"}, status=401)


@web.middleware
async def error_middleware(
    request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
) -> web.StreamResponse:
    """Turn the domain errors into clean 4xx JSON instead of a 500 + traceback."""
    try:
        return await handler(request)
    except (
        botlink.BotLinkError,
        chain.ChainError,
        ops.OpsError,
        agent.AgentError,
        wallet.WalletError,
        market.MarketError,
        lists.ListError,
        profits.ProfitError,
    ) as exc:
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


def _host(request: web.Request) -> str:
    return request.headers.get("X-Forwarded-Host") or request.headers.get("Host") or "localhost"


def _login_response(request: web.Request, user: str, remember: bool) -> web.Response:
    """Emite la cookie de sesión para ``user`` y devuelve quién es + su cargo."""
    ttl = config.REMEMBER_TTL_SECONDS if remember else config.SHORT_TTL_SECONDS
    token = request.app["auth"].issue_token(user, ttl)
    _SESSIONS[token] = {"history": [], "seen": time.time()}
    # El token viaja también en el cuerpo: es la única vía que sobrevive al
    # proxy de Vercel (que elimina Set-Cookie). El cliente lo guarda y lo
    # reenvía como ``Authorization: Bearer``.
    response = web.json_response(
        {"ok": True, "user": user, "role": users.role_of(user), "token": token}
    )
    response.set_cookie(
        auth.COOKIE,
        token,
        httponly=True,
        secure=config.COOKIE_SECURE,
        samesite="Lax",
        # "Recuérdame" => cookie persistente; si no, se borra al cerrar el navegador.
        max_age=ttl if remember else None,
        path="/",
    )
    return response


async def handle_login(request: web.Request) -> web.Response:
    body = await request.json()
    auth_obj = request.app["auth"]
    ip = _client_ip(request)
    remember = bool(body.get("remember", False))
    # Tres puertas: usuario+contraseña, PIN corto, o Face ID (por otra ruta).
    pin = str(body.get("pin", "")).strip()
    if pin:
        ok, message, user = auth_obj.check_pin(ip, pin)
    else:
        ok, message, user = auth_obj.check(
            ip, str(body.get("user", "")), str(body.get("password", ""))
        )
    if not ok:
        logger.warning("Login fallido desde %s", ip)
        return web.json_response({"ok": False, "error": message}, status=401)
    return _login_response(request, user, remember)


async def handle_logout(request: web.Request) -> web.Response:
    _SESSIONS.pop(_request_token(request), None)
    response = web.json_response({"ok": True})
    response.del_cookie(auth.COOKIE, path="/")
    return response


async def handle_bootstrap(request: web.Request) -> web.Response:
    """Datos públicos para pintar la pantalla de login (usuarios y si hay cara)."""
    return web.json_response(
        {
            "ok": True,
            "brand": config.RP_NAME,
            "users": users.list_users(),
            "face_available": webauthn_face.available(),
        }
    )


async def handle_me(request: web.Request) -> web.Response:
    user = request.app["auth"].token_user(_request_token(request))
    return web.json_response({"ok": True, "user": user, "role": users.role_of(user or "")})


# --------------------------------------------------------------------------- #
# Face ID / huella (WebAuthn)                                                  #
# --------------------------------------------------------------------------- #


async def handle_face_register_options(request: web.Request) -> web.Response:
    user = request.app["auth"].token_user(_request_token(request))
    if not user:
        return web.json_response({"ok": False, "error": "login requerido"}, status=401)
    try:
        out = webauthn_face.register_options(user, _host(request), config.COOKIE_SECURE)
    except webauthn_face.FaceError as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=400)
    return web.json_response({"ok": True, **out})


async def handle_face_register_verify(request: web.Request) -> web.Response:
    user = request.app["auth"].token_user(_request_token(request))
    if not user:
        return web.json_response({"ok": False, "error": "login requerido"}, status=401)
    body = await request.json()
    try:
        out = webauthn_face.register_verify(
            str(body["handle"]), body["credential"], _host(request), config.COOKIE_SECURE
        )
    except webauthn_face.FaceError as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=400)
    logger.warning("Face ID dado de alta para %s", user)
    return web.json_response({"ok": True, **out})


async def handle_face_login_options(request: web.Request) -> web.Response:
    body = await request.json()
    try:
        out = webauthn_face.login_options(
            str(body.get("user", "")), _host(request), config.COOKIE_SECURE
        )
    except webauthn_face.FaceError as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=400)
    return web.json_response({"ok": True, **out})


async def handle_face_login_verify(request: web.Request) -> web.Response:
    body = await request.json()
    ip = _client_ip(request)
    try:
        user = webauthn_face.login_verify(
            str(body["handle"]), body["credential"], _host(request), config.COOKIE_SECURE
        )
    except webauthn_face.FaceError as exc:
        request.app["auth"].note_failure(ip)
        logger.warning("Face ID fallido desde %s: %s", ip, exc)
        return web.json_response({"ok": False, "error": str(exc)}, status=401)
    return _login_response(request, user, bool(body.get("remember", False)))


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

    me = request.app["auth"].token_user(_request_token(request))
    return web.json_response(
        {
            "ok": True,
            "config": config.public_config(),
            "me": {
                "user": me,
                "role": users.role_of(me or ""),
                "has_face": webauthn_face.has_passkey(me or ""),
                "face_available": webauthn_face.available(),
            },
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
# Mercado (lectura)                                                            #
# --------------------------------------------------------------------------- #


async def handle_token(request: web.Request) -> web.Response:
    mint = request.query.get("mint", "")
    return web.json_response({"ok": True, "token": await market.token_info(mint)})


async def handle_chart(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, **botlink.pnl_timeline()})


# --------------------------------------------------------------------------- #
# Wallet caliente (FIRMA — botones directos, con la posición en pantalla)      #
# --------------------------------------------------------------------------- #


async def handle_buy(request: web.Request) -> web.Response:
    body = await request.json()
    result = await wallet.buy(
        str(body["mint"]).strip(),
        float(body["sol"]),
        int(body["slippage_bps"]) if body.get("slippage_bps") else None,
    )
    logger.warning("COMPRA firmada desde el panel: %s", result.get("signature"))
    await notify.signed_buy(str(body["mint"]).strip(), float(body["sol"]), result)
    return web.json_response({"ok": True, **result})


async def handle_sell_signed(request: web.Request) -> web.Response:
    body = await request.json()
    result = await wallet.sell(
        str(body["mint"]).strip(),
        float(body["percent"]),
        int(body["slippage_bps"]) if body.get("slippage_bps") else None,
    )
    logger.warning("VENTA firmada desde el panel: %s", result.get("signature"))
    await notify.signed_sell(str(body["mint"]).strip(), float(body["percent"]), result)
    return web.json_response({"ok": True, **result})


async def handle_send_sol(request: web.Request) -> web.Response:
    body = await request.json()
    result = await wallet.send_sol(str(body["to"]).strip(), float(body["sol"]))
    logger.warning("ENVIO de SOL firmado desde el panel: %s", result.get("signature"))
    await notify.signed_send(str(body["to"]).strip(), float(body["sol"]), result)
    return web.json_response({"ok": True, **result})


# --------------------------------------------------------------------------- #
# Notificaciones (Telegram)                                                    #
# --------------------------------------------------------------------------- #


async def handle_notify_status(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, **notify.status()})


async def handle_notify_save(request: web.Request) -> web.Response:
    body = await request.json()
    prefs = notify.set_prefs(
        bool(body.get("enabled", True)),
        body.get("events") or {},
        bool(body.get("telegram", False)),
    )
    logger.info("Avisos: enabled=%s telegram=%s %s", prefs["enabled"], prefs["telegram"], prefs["events"])
    return web.json_response({"ok": True, **notify.status()})


async def handle_rules(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, **rules.summary()})


async def handle_rule_cancel(request: web.Request) -> web.Response:
    body = await request.json()
    cancelled = rules.cancel(str(body.get("id", "")).strip())
    if not cancelled:
        return web.json_response({"ok": False, "error": "Esa orden ya no estaba esperando."}, status=404)
    logger.warning("Orden condicional cancelada a mano: %s", body.get("id"))
    return web.json_response({"ok": True, **rules.summary()})


async def handle_inbox(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, **notify.inbox()})


async def handle_inbox_read(request: web.Request) -> web.Response:
    body = await request.json() if request.can_read_body else {}
    ids = body.get("ids") if isinstance(body, dict) else None
    marked = notify.mark_read([int(i) for i in ids] if ids else None)
    return web.json_response({"ok": True, "marked": marked, "unread": notify.unread()})


async def handle_inbox_clear(request: web.Request) -> web.Response:
    notify.clear()
    return web.json_response({"ok": True, "unread": 0})


async def handle_notify_test(request: web.Request) -> web.Response:
    """Deja un aviso de prueba en el buzón. Salta las preferencias a propósito:
    si estás probando, quieres ver si APARECE, no si además está encendido."""
    user = request.app["auth"].token_user(_request_token(request)) or "alguien"
    entry = notify.add("test", "Prueba de notificaciones", f"Lanzada por {user}. Si lees esto, funciona.")
    result = {"telegram": None}
    if notify.load().get("telegram"):
        result["telegram"] = await notify.send_telegram(
            f"🔔 <b>AGALAZ BANK</b>\nPrueba lanzada por {user}."
        )
    return web.json_response({"ok": True, "entry": entry, **result})


# --------------------------------------------------------------------------- #
# Listas de wallets (devs / traders / blacklist)                               #
# --------------------------------------------------------------------------- #


async def handle_lists(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "lists": lists.list_files()})


async def handle_list_read(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, **lists.read_list(request.match_info["file"])})


async def handle_list_add(request: web.Request) -> web.Response:
    body = await request.json()
    return web.json_response(
        {"ok": True, **lists.add_wallet(str(body["file"]), str(body["address"]), str(body.get("note", "")))}
    )


async def handle_list_remove(request: web.Request) -> web.Response:
    body = await request.json()
    return web.json_response(
        {"ok": True, **lists.remove_wallet(str(body["file"]), str(body["address"]))}
    )


async def handle_list_create(request: web.Request) -> web.Response:
    body = await request.json()
    return web.json_response(
        {"ok": True, **lists.create_list(str(body["file"]), str(body.get("readme", "")))}
    )


# --------------------------------------------------------------------------- #
# Reparto de ganancias                                                        #
# --------------------------------------------------------------------------- #


async def handle_profits_get(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "config": profits.get_config()})


async def handle_profits_set(request: web.Request) -> web.Response:
    body = await request.json()
    return web.json_response(
        {
            "ok": True,
            "config": profits.set_config(
                str(body.get("mode", "manual")),
                list(body.get("recipients", [])),
                float(body.get("min_auto_sol", 0.2)),
            ),
        }
    )


async def handle_profits_distribute(request: web.Request) -> web.Response:
    body = await request.json()
    result = await profits.distribute(float(body["total_sol"]))
    logger.warning("Reparto de ganancias ejecutado: %s SOL", result.get("distributed_sol"))
    return web.json_response({"ok": True, **result})


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
    app.router.add_get("/api/bootstrap", handle_bootstrap)
    app.router.add_post("/api/login", handle_login)
    app.router.add_post("/api/logout", handle_logout)
    app.router.add_get("/api/me", handle_me)

    app.router.add_post("/api/face/register/options", handle_face_register_options)
    app.router.add_post("/api/face/register/verify", handle_face_register_verify)
    app.router.add_post("/api/face/login/options", handle_face_login_options)
    app.router.add_post("/api/face/login/verify", handle_face_login_verify)

    app.router.add_get("/api/state", handle_state)
    app.router.add_get("/api/wallet", handle_wallet)
    app.router.add_get("/api/trades", handle_trades)
    app.router.add_get("/api/bots/{name}", handle_bot_config)
    app.router.add_get("/api/logs", handle_logs)
    app.router.add_get("/api/server", handle_server)
    app.router.add_get("/api/journal", handle_journal)

    app.router.add_post("/api/sell", handle_sell)
    app.router.add_post("/api/bots/toggle", handle_toggle_bot)

    # Mercado + gráficos (lectura)
    app.router.add_get("/api/token", handle_token)
    app.router.add_get("/api/chart", handle_chart)

    # Wallet caliente (firma)
    app.router.add_post("/api/wallet/buy", handle_buy)
    app.router.add_post("/api/wallet/sell", handle_sell_signed)
    app.router.add_post("/api/wallet/send", handle_send_sol)

    # Listas de wallets
    app.router.add_get("/api/lists", handle_lists)
    app.router.add_get("/api/lists/{file}", handle_list_read)
    app.router.add_post("/api/lists/add", handle_list_add)
    app.router.add_post("/api/lists/remove", handle_list_remove)
    app.router.add_post("/api/lists/create", handle_list_create)

    # Reparto de ganancias
    app.router.add_get("/api/profits", handle_profits_get)
    app.router.add_post("/api/profits/set", handle_profits_set)
    app.router.add_post("/api/profits/distribute", handle_profits_distribute)

    app.router.add_post("/api/agent/chat", handle_chat)
    app.router.add_get("/api/agent/pending", handle_pending)
    app.router.add_post("/api/agent/confirm", handle_confirm)
    app.router.add_post("/api/agent/cancel", handle_cancel)
    app.router.add_post("/api/agent/reset", handle_reset_chat)

    app.router.add_get("/api/notify", handle_notify_status)
    app.router.add_post("/api/notify", handle_notify_save)
    app.router.add_post("/api/notify/test", handle_notify_test)
    app.router.add_get("/api/rules", handle_rules)
    app.router.add_post("/api/rules/cancel", handle_rule_cancel)
    app.router.add_get("/api/inbox", handle_inbox)
    app.router.add_post("/api/inbox/read", handle_inbox_read)
    app.router.add_post("/api/inbox/clear", handle_inbox_clear)

    app.on_startup.append(_start_background)
    app.on_cleanup.append(_stop_background)
    return app


# --------------------------------------------------------------------------- #
# Reparto automático de ganancias (bucle de fondo)                            #
# --------------------------------------------------------------------------- #

_AUTO_INTERVAL = 300  # cada 5 minutos


async def _auto_profit_loop() -> None:
    """Si el reparto está en modo 'auto', manda periódicamente el beneficio nuevo.

    Sólo actúa con la wallet caliente encendida y destinatarios configurados;
    en cualquier otro caso es un no-op silencioso.
    """
    while True:
        try:
            await asyncio.sleep(_AUTO_INTERVAL)
            result = await profits.auto_tick()
            if result:
                logger.warning("Reparto AUTO: %s SOL", result.get("distributed_sol"))
                await notify.profits_sent(float(result.get("distributed_sol") or 0))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- un fallo no debe matar el bucle
            logger.exception("Fallo en el bucle de reparto automático")


async def _notify_watch_loop() -> None:
    """Vigila los fills nuevos del bot y su silencio, y avisa por Telegram.

    Arranca marcando los fills que ya había como vistos: al reiniciar el panel
    no tiene ninguna gracia recibir de golpe el histórico entero.
    """
    prefs = notify.load()
    prefs["seen_fills"] = len(notify.fill_lines())
    notify.save(prefs)

    silent_state: dict[str, bool] = {}
    while True:
        try:
            await asyncio.sleep(notify.WATCH_INTERVAL)
            await notify.watch_tick(silent_state)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- un fallo no debe matar el bucle
            logger.exception("Fallo en el vigilante de avisos")


async def _rules_loop() -> None:
    """Vigila las órdenes condicionales y las dispara cuando se cumplen."""
    while True:
        try:
            await asyncio.sleep(rules.CHECK_INTERVAL)
            fired = await rules.check_tick()
            if fired:
                logger.warning("Órdenes condicionales disparadas: %s", fired)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- un fallo no debe matar el bucle
            logger.exception("Fallo revisando las órdenes condicionales")


async def _start_background(app: web.Application) -> None:
    app["auto_profit_task"] = asyncio.create_task(_auto_profit_loop())
    app["notify_task"] = asyncio.create_task(_notify_watch_loop())
    app["rules_task"] = asyncio.create_task(_rules_loop())


async def _stop_background(app: web.Application) -> None:
    for key in ("auto_profit_task", "notify_task", "rules_task"):
        task = app.get(key)
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


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

"""Datos de mercado de un token: precio, liquidez, market cap, volumen y señales
de rug. Sólo lectura, sin firma, sin estado.

Fuentes públicas y gratuitas:
* **DexScreener** — precio, liquidez, MC, volumen y edad del par.
* **RugCheck** — veredicto rápido de riesgo (el mismo que ya usa el bot).

GMGN y Solscan no tienen API pública estable/gratis para esto, así que damos el
enlace directo para abrirlos a mano desde la UI en vez de scrapearlos.
"""

from __future__ import annotations

import re
from typing import Any

import aiohttp

_MINT_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


class MarketError(RuntimeError):
    """No se pudo leer el mercado de ese token."""


def _valid_mint(mint: str) -> str:
    mint = (mint or "").strip()
    if not _MINT_RE.match(mint):
        raise MarketError(f"Mint inválido: {mint!r}")
    return mint


async def _get_json(session: aiohttp.ClientSession, url: str, timeout: int = 12) -> Any:
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as response:
            if response.status != 200:
                return None
            return await response.json()
    except (aiohttp.ClientError, ValueError):
        return None


async def token_info(mint: str) -> dict[str, Any]:
    """Foto de mercado de un token, lista para pintar antes de comprar."""
    mint = _valid_mint(mint)
    async with aiohttp.ClientSession() as session:
        dex = await _get_json(
            session, f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
        )
        rug = await _get_json(session, f"https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary")

    pairs = (dex or {}).get("pairs") or []
    # Nos quedamos con el par de mayor liquidez (el "de verdad").
    best = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd", 0), default=None)

    info: dict[str, Any] = {
        "mint": mint,
        "found": bool(best),
        "links": {
            "dexscreener": f"https://dexscreener.com/solana/{mint}",
            "gmgn": f"https://gmgn.ai/sol/token/{mint}",
            "solscan": f"https://solscan.io/token/{mint}",
            "pumpfun": f"https://pump.fun/{mint}",
        },
    }
    if best:
        info.update(
            {
                "symbol": (best.get("baseToken") or {}).get("symbol"),
                "name": (best.get("baseToken") or {}).get("name"),
                "price_usd": _f(best.get("priceUsd")),
                "market_cap_usd": _f(best.get("marketCap") or best.get("fdv")),
                "liquidity_usd": _f((best.get("liquidity") or {}).get("usd")),
                "volume_24h_usd": _f((best.get("volume") or {}).get("h24")),
                "volume_5m_usd": _f((best.get("volume") or {}).get("m5")),
                "price_change_5m": _f((best.get("priceChange") or {}).get("m5")),
                "price_change_1h": _f((best.get("priceChange") or {}).get("h1")),
                "pair_created_at": best.get("pairCreatedAt"),
                "dex": best.get("dexId"),
            }
        )
    if isinstance(rug, dict):
        info["rug"] = {
            "score": rug.get("score"),
            "risk_level": rug.get("riskLevel") or rug.get("risk"),
            "rugged": rug.get("rugged"),
            "risks": [r.get("name") for r in (rug.get("risks") or [])][:6],
        }
    return info


def _f(value: Any) -> float | None:
    try:
        return round(float(value), 10)
    except (TypeError, ValueError):
        return None

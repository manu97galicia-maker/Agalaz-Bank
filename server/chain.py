"""Read-only Solana access: what the wallet actually holds, right now.

The bot's own P&L is derived from its fill log, which is an estimate: it misses
dust, tokens bought outside the bot, and anything a failed sell left behind.
This module asks the chain instead, and prices the result through Jupiter.

There is deliberately no signing path here. The private key is read once, and
only to derive the public key -- so the worst a compromise of this panel can do
is *read* the wallet, never move it.
"""

from __future__ import annotations

import contextlib
import json
from typing import Any

import aiohttp

from . import config

LAMPORTS_PER_SOL = 1_000_000_000
SOL_MINT = "So11111111111111111111111111111111111111112"
TOKEN_PROGRAMS = (
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",  # SPL Token
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",  # Token-2022
)

_BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


class ChainError(RuntimeError):
    """The chain could not be read (bad RPC, no wallet, network down)."""


# --------------------------------------------------------------------------- #
# base58 (small enough not to justify a dependency)                            #
# --------------------------------------------------------------------------- #


def b58encode(data: bytes) -> str:
    number = int.from_bytes(data, "big")
    out = ""
    while number:
        number, remainder = divmod(number, 58)
        out = _BASE58_ALPHABET[remainder] + out
    # Every leading zero byte is one leading '1'.
    return "1" * (len(data) - len(data.lstrip(b"\0"))) + out


def b58decode(text: str) -> bytes:
    number = 0
    for char in text:
        index = _BASE58_ALPHABET.find(char)
        if index < 0:
            raise ValueError(f"caracter base58 invalido: {char!r}")
        number = number * 58 + index
    body = number.to_bytes((number.bit_length() + 7) // 8, "big")
    return b"\0" * (len(text) - len(text.lstrip("1"))) + body


def wallet_pubkey() -> str:
    """The wallet address the bot trades with.

    Prefers an explicitly configured ``PANEL_WALLET_PUBKEY``. Otherwise derives
    it from the bot's key: an ed25519 keypair is ``seed || pubkey``, so the
    address is simply the trailing 32 bytes -- no signing library needed and no
    reason to keep the secret half in memory.
    """
    if config.WALLET_PUBKEY:
        return config.WALLET_PUBKEY

    raw = config.bot_env("SOLANA_PRIVATE_KEY").strip()
    if not raw:
        raise ChainError(
            "No hay wallet: define PANEL_WALLET_PUBKEY o SOLANA_PRIVATE_KEY "
            "en el .env del bot."
        )
    try:
        keypair = bytes(json.loads(raw)) if raw.startswith("[") else b58decode(raw)
    except (ValueError, TypeError) as exc:
        raise ChainError(f"No pude leer la clave: {exc}") from exc
    if len(keypair) != 64:
        raise ChainError(f"Keypair de {len(keypair)} bytes; esperaba 64.")
    return b58encode(keypair[32:])


# --------------------------------------------------------------------------- #
# RPC                                                                         #
# --------------------------------------------------------------------------- #


async def _rpc(session: aiohttp.ClientSession, method: str, params: list[Any]) -> Any:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    try:
        async with session.post(
            config.RPC_ENDPOINT, json=payload, timeout=aiohttp.ClientTimeout(total=20)
        ) as response:
            body = await response.json()
    except aiohttp.ClientError as exc:
        raise ChainError(f"RPC no responde: {exc}") from exc
    if "error" in body:
        raise ChainError(f"RPC error: {body['error'].get('message', body['error'])}")
    return body.get("result")


async def _prices(session: aiohttp.ClientSession, mints: list[str]) -> dict[str, float]:
    """USD price per mint from Jupiter. Best effort: unknown mints are absent."""
    if not mints:
        return {}
    out: dict[str, float] = {}
    for start in range(0, len(mints), 50):  # the API caps ids per call
        chunk = ",".join(mints[start : start + 50])
        try:
            async with session.get(
                f"{config.JUPITER_PRICE_API}?ids={chunk}",
                timeout=aiohttp.ClientTimeout(total=15),
            ) as response:
                if response.status != 200:
                    continue
                data = await response.json()
        except (aiohttp.ClientError, json.JSONDecodeError):
            continue
        # v3 returns {mint: {usdPrice: ...}}; older shapes nest under "data".
        rows = data.get("data", data) if isinstance(data, dict) else {}
        for mint, entry in (rows or {}).items():
            if isinstance(entry, dict):
                price = entry.get("usdPrice", entry.get("price"))
                if price is not None:
                    with contextlib.suppress(TypeError, ValueError):
                        out[mint] = float(price)
    return out


async def wallet_snapshot(min_usd: float = 0.5) -> dict[str, Any]:
    """SOL balance plus every priced token the wallet still holds.

    ``min_usd`` filters the long tail of worthless rug remnants that every
    sniping wallet accumulates; they are counted but not listed.
    """
    address = wallet_pubkey()
    async with aiohttp.ClientSession() as session:
        balance = await _rpc(session, "getBalance", [address])
        sol = (balance or {}).get("value", 0) / LAMPORTS_PER_SOL

        holdings: list[dict[str, Any]] = []
        for program in TOKEN_PROGRAMS:
            result = await _rpc(
                session,
                "getTokenAccountsByOwner",
                [address, {"programId": program}, {"encoding": "jsonParsed"}],
            )
            for account in (result or {}).get("value", []):
                info = (
                    account.get("account", {})
                    .get("data", {})
                    .get("parsed", {})
                    .get("info", {})
                )
                amount = info.get("tokenAmount", {}).get("uiAmount") or 0
                if amount > 0:
                    holdings.append({"mint": info.get("mint"), "amount": float(amount)})

        prices = await _prices(session, [SOL_MINT] + [h["mint"] for h in holdings])

    sol_price = prices.get(SOL_MINT, 0.0)
    priced: list[dict[str, Any]] = []
    dust_count = 0
    tokens_usd = 0.0
    for holding in holdings:
        price = prices.get(holding["mint"], 0.0)
        value_usd = holding["amount"] * price
        tokens_usd += value_usd
        if value_usd < min_usd:
            dust_count += 1
            continue
        priced.append(
            {
                **holding,
                "price_usd": price,
                "value_usd": round(value_usd, 2),
                "value_sol": round(value_usd / sol_price, 6) if sol_price else None,
            }
        )
    priced.sort(key=lambda h: h["value_usd"], reverse=True)

    return {
        "address": address,
        "sol_balance": round(sol, 6),
        "sol_price_usd": round(sol_price, 2) or None,
        "sol_value_usd": round(sol * sol_price, 2) if sol_price else None,
        "tokens_value_usd": round(tokens_usd, 2),
        "total_value_usd": round(sol * sol_price + tokens_usd, 2) if sol_price else None,
        "holdings": priced,
        "dust_positions_hidden": dust_count,
        "rpc": "configurado" if config.RPC_ENDPOINT != config.FALLBACK_RPC else "publico",
    }

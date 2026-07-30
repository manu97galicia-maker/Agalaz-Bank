"""Wallet caliente: firma transacciones reales de Solana.

Esto es lo contrario del resto del panel. ``chain.py`` sólo LEE la cadena y
nunca toca la mitad secreta de la clave. Este módulo SÍ firma: compra y vende
tokens vía Jupiter y envía SOL a otras wallets. Manu lo pidió a conciencia
("todo con wallet caliente ya"), así que las defensas van en capas:

* **Apagado por defecto.** Sin ``PANEL_HOT_WALLET=true`` cada función aquí
  lanza ``WalletError`` antes de tocar nada.
* **Tope duro de compra.** Ninguna compra puede gastar más de ``MAX_BUY_SOL``,
  pase lo que pase la UI.
* **Un solo secreto.** Reutiliza la clave del bot; no hay una segunda copia.

La clave privada se carga en memoria SÓLO cuando se va a firmar, nunca se
loguea, nunca se devuelve al navegador.
"""

from __future__ import annotations

import base64
import contextlib
from typing import Any

import aiohttp

from . import config

LAMPORTS_PER_SOL = 1_000_000_000
SOL_MINT = "So11111111111111111111111111111111111111112"


class WalletError(RuntimeError):
    """La operación con firma no se pudo (apagada, sin clave, RPC caído…)."""


def _require_hot() -> None:
    if not config.HOT_WALLET:
        raise WalletError(
            "La wallet caliente está APAGADA. Pon PANEL_HOT_WALLET=true en el "
            ".env del panel para poder comprar, vender y enviar desde la web."
        )


# --------------------------------------------------------------------------- #
# Keypair                                                                      #
# --------------------------------------------------------------------------- #


def _keypair() -> Any:
    """Carga el keypair que firma. Import perezoso de solders: el resto del
    panel funciona aunque solders no esté instalado; sólo la firma lo exige."""
    try:
        from solders.keypair import Keypair  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover
        raise WalletError(
            "Falta 'solders' para firmar. Instálalo: pip install solders"
        ) from exc

    raw = config.wallet_secret().strip()
    if not raw:
        raise WalletError(
            "No hay clave para firmar: define SOLANA_PRIVATE_KEY (bot) o "
            "PANEL_WALLET_SECRET (panel)."
        )
    try:
        if raw.startswith("["):
            import json  # noqa: PLC0415

            return Keypair.from_bytes(bytes(json.loads(raw)))
        return Keypair.from_base58_string(raw)
    except (ValueError, TypeError) as exc:
        raise WalletError(f"No pude leer la clave privada: {exc}") from exc


# --------------------------------------------------------------------------- #
# RPC helpers                                                                  #
# --------------------------------------------------------------------------- #


async def _rpc(session: aiohttp.ClientSession, method: str, params: list[Any]) -> Any:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    async with session.post(
        config.RPC_ENDPOINT, json=payload, timeout=aiohttp.ClientTimeout(total=30)
    ) as response:
        body = await response.json()
    if "error" in body:
        raise WalletError(f"RPC error: {body['error'].get('message', body['error'])}")
    return body.get("result")


async def _send_signed(session: aiohttp.ClientSession, tx_b64: str) -> str:
    """Manda una transacción ya firmada (base64) y devuelve la firma."""
    result = await _rpc(
        session,
        "sendTransaction",
        [tx_b64, {"encoding": "base64", "skipPreflight": True, "maxRetries": 3}],
    )
    if not result:
        raise WalletError("El RPC no devolvió firma al enviar la transacción.")
    return str(result)


def _sign_serialized(tx_b64: str, keypair: Any) -> str:
    """Firma una VersionedTransaction que viene serializada en base64 (Jupiter)."""
    from solders.transaction import VersionedTransaction  # noqa: PLC0415

    unsigned = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
    signed = VersionedTransaction(unsigned.message, [keypair])
    return base64.b64encode(bytes(signed)).decode()


# --------------------------------------------------------------------------- #
# Datos de un token (decimales + balance) para poder vender fracciones          #
# --------------------------------------------------------------------------- #


async def _token_balance(session: aiohttp.ClientSession, owner: str, mint: str) -> dict[str, Any]:
    """Cuánto de ``mint`` tiene ``owner``, en unidades base y decimales."""
    result = await _rpc(
        session,
        "getTokenAccountsByOwner",
        [owner, {"mint": mint}, {"encoding": "jsonParsed"}],
    )
    raw_total = 0
    decimals = 0
    for account in (result or {}).get("value", []):
        info = account["account"]["data"]["parsed"]["info"]["tokenAmount"]
        raw_total += int(info["amount"])
        decimals = int(info["decimals"])
    return {"raw": raw_total, "decimals": decimals}


# --------------------------------------------------------------------------- #
# Jupiter swap (comprar / vender cualquier token)                               #
# --------------------------------------------------------------------------- #


async def _jup_quote(
    session: aiohttp.ClientSession, input_mint: str, output_mint: str, amount: int, slippage_bps: int
) -> dict[str, Any]:
    url = (
        f"{config.JUPITER_API}/swap/v1/quote"
        f"?inputMint={input_mint}&outputMint={output_mint}&amount={amount}"
        f"&slippageBps={slippage_bps}&swapMode=ExactIn"
    )
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as response:
        data = await response.json()
    if not isinstance(data, dict) or "outAmount" not in data:
        raise WalletError(
            "Jupiter no encontró ruta para ese token "
            "(¿aún en la curva de pump.fun sin migrar, o mint mal escrito?)."
        )
    return data


async def _jup_swap_tx(
    session: aiohttp.ClientSession, quote: dict[str, Any], user_pubkey: str
) -> str:
    body = {
        "quoteResponse": quote,
        "userPublicKey": user_pubkey,
        "wrapAndUnwrapSol": True,
        "dynamicComputeUnitLimit": True,
        "prioritizationFeeLamports": config.PRIORITY_FEE_LAMPORTS,
    }
    async with session.post(
        f"{config.JUPITER_API}/swap/v1/swap",
        json=body,
        timeout=aiohttp.ClientTimeout(total=20),
    ) as response:
        data = await response.json()
    tx = data.get("swapTransaction") if isinstance(data, dict) else None
    if not tx:
        raise WalletError(f"Jupiter no devolvió transacción: {data}")
    return tx


async def buy(mint: str, sol_amount: float, slippage_bps: int | None = None) -> dict[str, Any]:
    """Compra ``mint`` gastando ``sol_amount`` SOL. Firma y envía de verdad."""
    _require_hot()
    if sol_amount <= 0:
        raise WalletError("La cantidad de SOL debe ser mayor que 0.")
    if sol_amount > config.MAX_BUY_SOL:
        raise WalletError(
            f"Tope de seguridad: no se puede comprar por más de "
            f"{config.MAX_BUY_SOL} SOL (pediste {sol_amount}). Sube "
            f"PANEL_MAX_BUY_SOL si de verdad quieres más."
        )
    slippage = slippage_bps or config.DEFAULT_BUY_SLIPPAGE_BPS
    keypair = _keypair()
    pubkey = str(keypair.pubkey())
    lamports = int(round(sol_amount * LAMPORTS_PER_SOL))

    async with aiohttp.ClientSession() as session:
        quote = await _jup_quote(session, SOL_MINT, mint, lamports, slippage)
        tx_b64 = await _jup_swap_tx(session, quote, pubkey)
        signature = await _send_signed(session, _sign_serialized(tx_b64, keypair))

    return {
        "action": "buy",
        "mint": mint,
        "spent_sol": sol_amount,
        "expected_tokens": _ui_amount(quote.get("outAmount"), quote),
        "signature": signature,
        "solscan": f"https://solscan.io/tx/{signature}",
    }


async def sell(mint: str, percent: float, slippage_bps: int | None = None) -> dict[str, Any]:
    """Vende ``percent`` % (1-100) de lo que la wallet tenga de ``mint`` por SOL."""
    _require_hot()
    if not 0 < percent <= 100:
        raise WalletError("El porcentaje debe estar entre 0 y 100.")
    slippage = slippage_bps or config.DEFAULT_SELL_SLIPPAGE_BPS
    keypair = _keypair()
    pubkey = str(keypair.pubkey())

    async with aiohttp.ClientSession() as session:
        balance = await _token_balance(session, pubkey, mint)
        if balance["raw"] <= 0:
            raise WalletError("La wallet no tiene ese token (o ya lo vendiste).")
        amount = int(balance["raw"] * percent / 100)
        if amount <= 0:
            raise WalletError("La fracción a vender redondea a 0.")
        quote = await _jup_quote(session, mint, SOL_MINT, amount, slippage)
        tx_b64 = await _jup_swap_tx(session, quote, pubkey)
        signature = await _send_signed(session, _sign_serialized(tx_b64, keypair))

    return {
        "action": "sell",
        "mint": mint,
        "percent": percent,
        "expected_sol": int(quote.get("outAmount", 0)) / LAMPORTS_PER_SOL,
        "signature": signature,
        "solscan": f"https://solscan.io/tx/{signature}",
    }


# --------------------------------------------------------------------------- #
# Enviar SOL nativo a otra wallet (reparto de ganancias)                        #
# --------------------------------------------------------------------------- #


async def send_sol(destination: str, sol_amount: float) -> dict[str, Any]:
    """Transfiere ``sol_amount`` SOL nativo a ``destination``. Firma y envía."""
    _require_hot()
    if sol_amount <= 0:
        raise WalletError("La cantidad debe ser mayor que 0.")
    from solders.hash import Hash  # noqa: PLC0415
    from solders.message import MessageV0  # noqa: PLC0415
    from solders.pubkey import Pubkey  # noqa: PLC0415
    from solders.system_program import TransferParams, transfer  # noqa: PLC0415
    from solders.transaction import VersionedTransaction  # noqa: PLC0415

    keypair = _keypair()
    try:
        dest = Pubkey.from_string(destination.strip())
    except (ValueError, TypeError) as exc:
        raise WalletError(f"Dirección de destino inválida: {destination!r}") from exc
    lamports = int(round(sol_amount * LAMPORTS_PER_SOL))

    async with aiohttp.ClientSession() as session:
        # Dejamos un colchón para el fee de red.
        bal = await _rpc(session, "getBalance", [str(keypair.pubkey())])
        have = (bal or {}).get("value", 0)
        if have < lamports + 5000:
            raise WalletError(
                f"Saldo insuficiente: tienes {have / LAMPORTS_PER_SOL:.4f} SOL, "
                f"intentas enviar {sol_amount} + fee."
            )
        blockhash = (await _rpc(session, "getLatestBlockhash", [{"commitment": "finalized"}]))[
            "value"
        ]["blockhash"]
        ix = transfer(
            TransferParams(
                from_pubkey=keypair.pubkey(), to_pubkey=dest, lamports=lamports
            )
        )
        msg = MessageV0.try_compile(
            keypair.pubkey(), [ix], [], Hash.from_string(blockhash)
        )
        tx = VersionedTransaction(msg, [keypair])
        signature = await _send_signed(session, base64.b64encode(bytes(tx)).decode())

    return {
        "action": "send_sol",
        "to": destination,
        "sol": sol_amount,
        "signature": signature,
        "solscan": f"https://solscan.io/tx/{signature}",
    }


# --------------------------------------------------------------------------- #
# util                                                                         #
# --------------------------------------------------------------------------- #


def _ui_amount(raw: Any, quote: dict[str, Any]) -> float | None:
    """Convierte una cantidad base a unidades legibles si conocemos decimales."""
    decimals = None
    with contextlib.suppress(Exception):
        decimals = quote.get("outputDecimals")
    try:
        raw_int = int(raw)
    except (TypeError, ValueError):
        return None
    if decimals is None:
        return raw_int  # sin decimales conocidos, devolvemos crudo
    return raw_int / (10**int(decimals))

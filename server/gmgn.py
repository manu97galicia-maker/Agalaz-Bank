"""GMGN: datos de mercado y ejecución de órdenes con salida automática.

Por qué se añade
----------------
El panel ya sabe comprar y vender por Jupiter (``wallet.py``), pero la venta la
vigila el propio bot en un bucle local. El 1-ago-2026 ese bucle se comió 0,64
SOL: la venta fallaba con ``6023 NotEnoughTokensToSell``, reintentaba cada
segundo y cada intento pagaba comisión de prioridad igual. 178 ventas fallidas.

GMGN acepta las condiciones de salida **en el propio servidor**
(``--condition-orders``): mandas la compra con el take-profit y el stop-loss ya
puestos y los ejecuta su infraestructura. No hay bucle local que se pueda
atascar. Además trae ``--anti-mev``, que Jupiter a pelo no da.

Qué NO hace este módulo
-----------------------
Ejecutar nada por su cuenta. Igual que el resto del panel, cualquier acción que
mueva dinero se propone y la confirma Manu. Aquí sólo se construyen los
comandos y se leen datos.

Requisitos
----------
``gmgn-cli`` instalado y configurado (``gmgn-cli config --apply <api_key>``),
con la wallet vinculada a la API Key. Las lecturas necesitan sólo la API Key;
el swap necesita además la clave privada del keypair local.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any

#: Comisión de plataforma que cobra GMGN, leída de un presupuesto real
#: (``platformFee: 1`` = 0,01%). Se documenta porque la duda "¿me cobran un 1%?"
#: cambia por completo si merece la pena frente a firmar en local.
COMISION_PLATAFORMA_PCT = 0.01

SOL_MINT = "So11111111111111111111111111111111111111112"


def _bin() -> str | None:
    """En Windows el binario de npm es un .cmd y CreateProcess no lo resuelve."""
    return shutil.which("gmgn-cli") or shutil.which("gmgn-cli.cmd")


def disponible() -> bool:
    return _bin() is not None


def _run(args: list[str], timeout: int = 60) -> dict[str, Any]:
    """Ejecuta gmgn-cli y devuelve el JSON. Nunca lanza: devuelve {"error": …}.

    encoding explícito: los nombres de las memecoins llevan emojis y kanji, y
    en Windows subprocess decodifica en cp1252 y revienta.
    """
    binario = _bin()
    if not binario:
        return {"error": "gmgn-cli no está instalado en el servidor"}
    try:
        r = subprocess.run(  # noqa: S603
            [binario, *args, "--raw"],
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}
    if r.returncode != 0:
        return {"error": (r.stderr or r.stdout or "fallo sin mensaje")[:300]}
    try:
        return json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        return {"error": f"respuesta no-JSON: {r.stdout[:200]}"}


# --------------------------------------------------------------------------- #
# Lectura                                                                     #
# --------------------------------------------------------------------------- #


def presupuesto(mint: str, sol: float, desde: str, slippage: float = 10.0) -> dict[str, Any]:
    """Cuánto recibirías por `sol` SOL de `mint`. No firma ni envía nada."""
    d = _run([
        "order", "quote", "--chain", "sol", "--from", desde,
        "--input-token", SOL_MINT, "--output-token", mint,
        "--amount", str(int(sol * 1e9)), "--slippage", str(slippage),
    ])
    if "error" in d:
        return d
    salida = int(d.get("output_amount") or 0)
    minimo = int(d.get("min_output_amount") or 0)
    ruta = (((d.get("tx") or {}).get("quote") or {}).get("routePlan") or [])
    return {
        "tokens": salida / 1e6,
        "tokens_minimo": minimo / 1e6,
        "impacto_pct": float(((d.get("tx") or {}).get("quote") or {}).get("priceImpactPct") or 0),
        "ruta": [x.get("swapInfo", {}).get("label") for x in ruta],
        "slippage": slippage,
    }


def descubrir(max_mc: float = 0, min_vol: float = 0, max_edad: str = "10m",
              limite: int = 40) -> dict[str, Any]:
    """La pestaña Discover de GMGN: qué se está lanzando AHORA y con qué pinta.

    Trae lo que la API de pump.fun no da: cuántas monedas ha creado el dev en
    total (una granja hace miles), ratio de bots de bundle, rug ratio, holders
    y cuánta smart money ha entrado.
    """
    args = ["market", "trenches", "--chain", "sol", "--type", "new_creation",
            "--limit", str(limite), "--max-created", max_edad]
    if max_mc:
        args += ["--max-marketcap", str(max_mc)]
    if min_vol:
        args += ["--min-volume-24h", str(min_vol)]
    d = _run(args)
    if "error" in d:
        return d
    filas = d.get("new_creation") or []
    return {"n": len(filas), "monedas": [{
        "symbol": t.get("symbol"),
        "mint": t.get("address"),
        "mc": float(t.get("market_cap") or 0),
        "vol_24h": float(t.get("volume_24h") or 0),
        "holders": int(t.get("holder_count") or 0),
        "rug_ratio": float(t.get("rug_ratio") or 0),
        "bundle_pct": round(float(t.get("bundler_mhr") or 0) * 100),
        "smart_money": int(t.get("smart_degen_count") or 0),
        "monedas_del_dev": int(t.get("creator_created_count") or 0),
    } for t in filas]}


def velas(mint: str, desde: int, hasta: int, resolucion: str = "30s") -> dict[str, Any]:
    """OHLC + volumen. Es el ÚNICO sitio del que sale el recorrido del precio:
    con sólo el máximo no se puede saber si un stop-loss saltó antes."""
    d = _run(["market", "kline", "--chain", "sol", "--address", mint,
              "--resolution", resolucion, "--from", str(desde), "--to", str(hasta)])
    if "error" in d:
        return d
    return {"velas": d.get("list") or []}


# --------------------------------------------------------------------------- #
# Órdenes con salida automática                                               #
# --------------------------------------------------------------------------- #


def condiciones_escalera(tramos: list[tuple[float, float]],
                         stop_pct: float | None = None,
                         trailing_pct: float | None = None) -> list[dict[str, str]]:
    """Traduce una escalera a las condition-orders de GMGN.

    `tramos` son (ganancia, % a vender). Por ejemplo, la escalera que pidió
    Manu -- +40% vende el 60%, +120% vende el 20%, stop -35% -- es::

        condiciones_escalera([(0.40, 60), (1.20, 20)], stop_pct=35)

    `price_scale` es el precio objetivo en % SOBRE el de entrada: +40% es 140.
    `trailing_pct` usa `profit_stop_trace`, que vende si cae ese % desde el
    máximo alcanzado (el anti pump&dump que el bot hacía a mano).
    """
    out: list[dict[str, str]] = []
    for ganancia, vender in tramos:
        out.append({
            "order_type": "profit_stop",
            "side": "sell",
            "price_scale": f"{(1 + ganancia) * 100:.0f}",
            "sell_ratio": f"{vender:.0f}",
        })
    if trailing_pct:
        out.append({
            "order_type": "profit_stop_trace",
            "side": "sell",
            "price_scale": "100",
            "sell_ratio": "100",
            "drawdown_rate": f"{trailing_pct:.0f}",
        })
    if stop_pct:
        out.append({
            "order_type": "loss_stop",
            "side": "sell",
            "price_scale": f"{(1 - stop_pct / 100) * 100:.0f}",
            "sell_ratio": "100",
        })
    return out


def comando_compra(mint: str, sol: float, desde: str, slippage: float = 10.0,
                   condiciones: list[dict[str, str]] | None = None,
                   anti_mev: bool = True) -> list[str]:
    """Construye el comando del swap. NO lo ejecuta: lo ejecuta `ejecutar`."""
    args = [
        "swap", "--chain", "sol", "--from", desde,
        "--input-token", SOL_MINT, "--output-token", mint,
        "--amount", str(int(sol * 1e9)), "--slippage", str(slippage),
    ]
    if anti_mev:
        args.append("--anti-mev")
    if condiciones:
        args += ["--condition-orders", json.dumps(condiciones)]
    return args


def comando_venta(mint: str, porcentaje: float, desde: str,
                  slippage: float = 25.0, anti_mev: bool = True) -> list[str]:
    args = [
        "swap", "--chain", "sol", "--from", desde,
        "--input-token", mint, "--output-token", SOL_MINT,
        "--percent", str(int(porcentaje)), "--slippage", str(slippage),
    ]
    if anti_mev:
        args.append("--anti-mev")
    return args


def ejecutar(args: list[str]) -> dict[str, Any]:
    """FIRMA Y ENVÍA. Sólo debe llamarse desde una propuesta ya confirmada."""
    return _run(args, timeout=120)


def estado_orden(order_id: str) -> dict[str, Any]:
    return _run(["order", "get", "--chain", "sol", "--order-id", order_id])

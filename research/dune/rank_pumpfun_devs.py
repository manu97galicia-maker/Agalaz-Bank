#!/usr/bin/env python3
"""Rankea los mejores devs de pump.fun combinando Dune + DexScreener.

Flujo:
  1. Ejecuta la query de Dune (dune_pumpfun_devs.sql) con tu API key y trae las
     filas ya filtradas (frecuencia, mediana de MC, migración, vida, orgánico,
     no-rug).
  2. Para cada dev, consulta la API de DexScreener por sus `sample_mints` y mira
     cuántos tienen "Enhanced Token Info" PAGADO (orden con status=approved).
     Eso resuelve el criterio "paga siempre DexScreener".
  3. Calcula un score compuesto y escribe pumpfun_devs_ranked.csv.

La API key NUNCA se escribe en disco: sale de la variable de entorno DUNE_API_KEY.

Uso típico:
  export DUNE_API_KEY=xxxxxxxx
  # (a) si ya guardaste el SQL como query en dune.com y tienes su id:
  python rank_pumpfun_devs.py --query-id 1234567
  # (b) si tu plan permite crear queries por API (crea, ejecuta y borra):
  python rank_pumpfun_devs.py --create --sql dune_pumpfun_devs.sql

Opciones útiles:
  --min-dex-paid-rate 0.5   exige que al menos el 50% de los tokens muestreados
                            del dev tengan DexScreener pagado (default 0.5)
  --max-tokens-per-dev 5    limita llamadas a DexScreener (rate limit 60/min)
  --out pumpfun_devs_ranked.csv
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any

DUNE_BASE = "https://api.dune.com/api/v1"
DEX_ORDERS = "https://api.dexscreener.com/orders/v1/solana/{mint}"
SOL = "So11111111111111111111111111111111111111112"


# --------------------------------------------------------------------------- #
# HTTP helpers                                                                 #
# --------------------------------------------------------------------------- #
def _req(url: str, *, method: str = "GET", headers: dict | None = None,
         body: bytes | None = None, timeout: int = 60) -> Any:
    import json
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return json.loads(raw) if raw else {}


def _dune(path: str, key: str, *, method: str = "GET", payload: dict | None = None) -> Any:
    import json
    headers = {"X-Dune-API-Key": key, "Content-Type": "application/json"}
    body = json.dumps(payload).encode() if payload is not None else None
    try:
        return _req(f"{DUNE_BASE}{path}", method=method, headers=headers, body=body)
    except urllib.error.HTTPError as e:
        sys.exit(f"[Dune] HTTP {e.code} en {path}: {e.read().decode(errors='replace')[:300]}")
    except urllib.error.URLError as e:
        sys.exit(f"[Dune] Sin conexión a api.dune.com: {e.reason}. "
                 "¿Estás detrás de un proxy que lo bloquea? Ejecútalo desde tu PC o el droplet.")


# --------------------------------------------------------------------------- #
# Dune: crear / ejecutar / esperar / resultados                               #
# --------------------------------------------------------------------------- #
def create_query(key: str, sql_path: str) -> int:
    sql = open(sql_path, encoding="utf-8").read()
    out = _dune("/query", key, method="POST", payload={
        "name": "pumpfun-mejores-devs (auto)", "query_sql": sql, "is_private": True})
    qid = out.get("query_id")
    if not qid:
        sys.exit(f"[Dune] No pude crear la query: {out}")
    print(f"[Dune] Query creada: id={qid}")
    return qid


def run_and_fetch(key: str, query_id: int, poll: int = 5, max_wait: int = 1800) -> list[dict]:
    ex = _dune(f"/query/{query_id}/execute", key, method="POST", payload={})
    exec_id = ex.get("execution_id")
    if not exec_id:
        sys.exit(f"[Dune] No arrancó la ejecución: {ex}")
    print(f"[Dune] Ejecutando… execution_id={exec_id}")
    waited = 0
    while waited < max_wait:
        st = _dune(f"/execution/{exec_id}/status", key).get("state")
        if st == "QUERY_STATE_COMPLETED":
            break
        if st in ("QUERY_STATE_FAILED", "QUERY_STATE_CANCELLED", "QUERY_STATE_EXPIRED"):
            sys.exit(f"[Dune] Ejecución terminó en estado {st}")
        time.sleep(poll)
        waited += poll
        print(f"[Dune] … {st} ({waited}s)")
    else:
        sys.exit("[Dune] Timeout esperando resultados.")
    res = _dune(f"/execution/{exec_id}/results", key)
    rows = res.get("result", {}).get("rows", [])
    print(f"[Dune] {len(rows)} devs devueltos.")
    return rows


# --------------------------------------------------------------------------- #
# DexScreener: ¿el token tiene Enhanced Token Info PAGADO?                     #
# --------------------------------------------------------------------------- #
def token_is_dex_paid(mint: str) -> bool:
    """True si el mint tiene alguna orden pagada aprobada en DexScreener."""
    try:
        data = _req(DEX_ORDERS.format(mint=mint), timeout=20)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            time.sleep(2)  # rate limit: espera y reintenta una vez
            try:
                data = _req(DEX_ORDERS.format(mint=mint), timeout=20)
            except Exception:
                return False
        else:
            return False
    except Exception:
        return False
    if not isinstance(data, list):
        return False
    return any(o.get("status") == "approved" for o in data)


def dex_paid_rate(mints: list[str], max_tokens: int, sleep_s: float) -> tuple[float, int]:
    """Fracción de mints (hasta max_tokens) con DexScreener pagado."""
    sample = [m for m in mints if m][:max_tokens]
    if not sample:
        return 0.0, 0
    paid = 0
    for m in sample:
        if token_is_dex_paid(m):
            paid += 1
        time.sleep(sleep_s)  # respeta 60 req/min de DexScreener
    return paid / len(sample), len(sample)


# --------------------------------------------------------------------------- #
# Score compuesto                                                             #
# --------------------------------------------------------------------------- #
def score(row: dict) -> float:
    """Score 0-100 transparente: frecuencia, volumen, limpieza (no pump&dump)."""
    def f(k, d=0.0):
        try:
            return float(row.get(k) or d)
        except (TypeError, ValueError):
            return d
    mcap = min(f("median_peak_mcap_usd") / 100000.0, 1.0)          # 0..1 (tope 100k)
    freq = min(f("launches_per_day") / 3.0, 1.0)                    # más frecuente, mejor
    life = min(f("median_lifespan_h") / 12.0, 1.0)                  # 0..1 (tope 12h)
    vol  = min(f("median_volume_usd") / 50000.0, 1.0)              # 0..1 (tope 50k)
    no_pd = 1.0 - f("pumpdump_1h_rate")                            # sin pump&dump 1ª hora
    org  = f("organic_rate")
    norug = 1.0 - f("rug_rate")
    dexp = f("dex_paid_rate")
    return round(100 * (0.12*mcap + 0.12*freq + 0.09*life + 0.15*vol +
                        0.18*no_pd + 0.16*org + 0.08*norug + 0.10*dexp), 1)


# --------------------------------------------------------------------------- #
# main                                                                        #
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--query-id", type=int, help="id de una query ya guardada en Dune")
    g.add_argument("--create", action="store_true", help="crear la query desde --sql")
    ap.add_argument("--sql", default=os.path.join(os.path.dirname(__file__), "dune_pumpfun_devs.sql"))
    ap.add_argument("--min-dex-paid-rate", type=float, default=0.5,
                    help="fracción mínima de tokens con DexScreener pagado (default 0.5)")
    ap.add_argument("--max-tokens-per-dev", type=int, default=5,
                    help="tokens a consultar por dev en DexScreener (rate limit 60/min)")
    ap.add_argument("--dex-sleep", type=float, default=1.1, help="segundos entre llamadas a DexScreener")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "pumpfun_devs_ranked.csv"))
    args = ap.parse_args()

    key = os.getenv("DUNE_API_KEY", "").strip()
    if not key:
        sys.exit("Falta DUNE_API_KEY en el entorno. `export DUNE_API_KEY=xxxx` y reintenta.")

    query_id = create_query(key, args.sql) if args.create else args.query_id
    rows = run_and_fetch(key, query_id)
    if not rows:
        print("Sin devs que cumplan el filtro. Afloja parámetros en el SQL (mcap, frecuencia, etc.).")
        return 0

    print(f"[DexScreener] Comprobando pago en hasta {args.max_tokens_per_dev} tokens por dev…")
    for i, r in enumerate(rows, 1):
        mints = r.get("sample_mints") or []
        rate, n = dex_paid_rate(mints, args.max_tokens_per_dev, args.dex_sleep)
        r["dex_paid_rate"] = round(rate, 3)
        r["dex_checked"] = n
        r["score"] = score(r)
        print(f"  [{i}/{len(rows)}] {str(r.get('creator'))[:8]}… dex_paid={rate:.0%} score={r['score']}")

    # Filtro final por DexScreener + orden por score.
    kept = [r for r in rows if r.get("dex_paid_rate", 0) >= args.min_dex_paid_rate]
    kept.sort(key=lambda r: r.get("score", 0), reverse=True)

    cols = ["score", "creator", "launches", "launches_per_day", "median_peak_mcap_usd",
            "median_volume_usd", "median_vol_1h_usd", "median_lifespan_h",
            "pumpdump_1h_rate", "organic_rate", "rug_rate", "migration_rate",
            "dex_paid_rate", "dex_checked", "median_uniq_traders", "last_launch_at"]
    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(kept)

    print(f"\n{len(kept)}/{len(rows)} devs pasan el filtro de DexScreener. CSV -> {args.out}")
    print("\nTOP 20:")
    print(f"{'score':>5}  {'creator':<44} {'/día':>5} {'MC med':>9} {'vol med':>9} "
          f"{'p&d1h':>5} {'org':>4} {'rug':>4} {'mig':>4} {'dex':>4}")
    for r in kept[:20]:
        print(f"{r['score']:>5}  {str(r.get('creator','')):<44} "
              f"{float(r.get('launches_per_day',0)):>5.2f} "
              f"{float(r.get('median_peak_mcap_usd',0)):>9,.0f} "
              f"{float(r.get('median_volume_usd',0)):>9,.0f} "
              f"{float(r.get('pumpdump_1h_rate',0)):>5.0%} "
              f"{float(r.get('organic_rate',0)):>4.0%} "
              f"{float(r.get('rug_rate',0)):>4.0%} "
              f"{float(r.get('migration_rate',0)):>4.0%} "
              f"{float(r.get('dex_paid_rate',0)):>4.0%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

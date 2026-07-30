# pump.fun · mejores devs (Dune + DexScreener)

Encuentra wallets de desarrolladores de pump.fun con buen historial para
seguir/snipear, filtrando por consistencia, tamaño y limpieza.

## Criterio (lo que pediste)

| Criterio | Regla por defecto | Dónde |
|---|---|---|
| Frecuencia | ~1 lanzamiento/día (0.5–3), consistente ≥14 días, ≥10 tokens | SQL |
| Tamaño | mediana del **market-cap máximo** ≥ 40.000 USD | SQL |
| Migra a DEX | ≥50% de sus tokens migran a Raydium/PumpSwap | SQL |
| Vida | mediana de vida del token > 3h | SQL |
| Sin dump/rug | ≤20% de tokens "rug" (muere <30 min, no arranca, o dev vende fuerte) | SQL |
| Orgánico | ≥70% sin bundle/snipe del dev en el slot de creación | SQL |
| Paga DexScreener | ≥50% de tokens con Enhanced Token Info **pagado** (status=approved) | script |

Todos los umbrales son ajustables (parámetros `<<<` en el `.sql` y flags del script).

## Cómo se mide cada cosa (resumen honesto)

- **Creación + dev**: `pumpdotfun_solana.pump_call_create` (mint + creador). Hay
  un *fallback crudo* verificado al final del `.sql` por si cambia el casing.
- **Market cap máximo**: pump.fun tiene supply fijo 1e9, así que `MC = precio_USD_pico × 1e9`
  usando `dex_solana.trades.amount_usd / cantidad_token`.
- **Migración**: primera aparición del mint bajo `project='pumpswap'` o `'raydium'`.
- **Vida**: de la creación al último trade (cualquier DEX).
- **Orgánico / bundle**: compras en el **mismo slot** que la creación (los bundles
  Jito aterrizan create+compras en un slot). Dev comprando su propio token = red flag.
- **Rug/dump**: token que muere en <30 min, no supera ~5k MC, o el dev vende >3k USD.
- **Paga DexScreener**: `GET https://api.dexscreener.com/orders/v1/solana/{mint}`;
  si hay una orden con `status=approved` → pagó Enhanced Token Info.

## Requisitos

- Python 3.10+ (solo librería estándar, sin dependencias).
- API key de Dune en `DUNE_API_KEY`. Para datos de Solana normalmente hace falta
  **plan de pago** de Dune (el free tier agota créditos con estas tablas).
- Ejecútalo desde una máquina **con salida a `api.dune.com`** (tu PC o el droplet).
  No corre desde el entorno de Claude: su proxy bloquea Dune por política.

## Uso

```bash
export DUNE_API_KEY=xxxxxxxxxxxxxxxx

# Opción A — pega el SQL en dune.com como query nueva, guarda, copia su id:
python rank_pumpfun_devs.py --query-id 1234567

# Opción B — si tu plan permite crear queries por API (crea y ejecuta solo):
python rank_pumpfun_devs.py --create --sql dune_pumpfun_devs.sql

# Ajustes frecuentes:
python rank_pumpfun_devs.py --query-id 1234567 \
    --min-dex-paid-rate 0.5 --max-tokens-per-dev 5
```

Salida: `pumpfun_devs_ranked.csv` + un TOP 20 por consola, ordenado por un
**score 0-100** (tamaño 22% · frecuencia 12% · vida 14% · migración 16% ·
orgánico 16% · no-rug 10% · DexScreener 10%).

## Objetivo: ~10 operaciones/día

Con ~10 trades/día de media, sigue a **8–12 devs** del top que lancen ~1/día:
así tienes flujo suficiente sin depender de un solo dev. El CSV te da los
candidatos; la columna `launches_per_day` te dice cuántas señales aporta cada uno.

## Avisos

- Los nombres de columnas decodificadas de Dune pueden variar; si la query falla,
  usa el fallback crudo del `.sql` y revisa el explorador de tablas.
- DexScreener limita a ~60 req/min: por eso el script muestrea pocos tokens por dev.
- Esto es análisis on-chain para tu decisión; no es consejo financiero.

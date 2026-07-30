-- ============================================================================
--  pump.fun · mejores DEVS por relación tamaño / consistencia / calidad
-- ----------------------------------------------------------------------------
--  Motor: DuneSQL (Trino).  Blockchain: Solana.
--
--  Devuelve una fila por DEV (creador de tokens en pump.fun) con sus métricas
--  agregadas, ya filtrada a tu criterio:
--    · frecuencia ~1 lanzamiento/día, consistente
--    · mediana del market-cap MÁXIMO  >= 40k USD
--    · migra a Raydium/PumpSwap (completa la curva)   -> "paga el DEX"
--    · vida mediana del token > 3h
--    · SIN dump/rug del dev
--    · lanzamientos ORGÁNICOS (sin bundle/snipe del propio dev)
--
--  El filtro "paga DexScreener (Enhanced Token Info)" NO se puede resolver en
--  Dune: se confirma después con la API de DexScreener en rank_pumpfun_devs.py,
--  usando la lista `sample_mints` que devuelve esta query.
--
--  AJUSTA los parámetros marcados con  <<<  a tu gusto.
--  NOTA de coste: escanea dex_solana.trades de Solana; empieza con LOOKBACK
--  corto (30 días) para no fundir créditos, y súbelo si hace falta.
--
--  CAVEATS de esquema (verificar en el explorador de tablas de Dune):
--   · Casing exacto de columnas decodificadas en pumpdotfun_solana.pump_call_create
--     (account_mint / account_user). Si fallan, usa el fallback crudo del final.
--   · dex_solana.trades: project = 'pumpdotfun' | 'pumpswap' | 'raydium'.
-- ============================================================================

WITH params AS (
    SELECT
        DATE_ADD('day', -30, NOW())  AS since_ts,     -- <<< ventana de análisis
        40000.0   AS min_median_mcap_usd,             -- <<< mediana MC máx mínima
        3.0       AS min_median_lifespan_h,           -- <<< vida mediana mínima (h)
        0.50      AS min_migration_rate,              -- <<< % tokens que migran
        0.70      AS min_organic_rate,                -- <<< % lanzamientos orgánicos
        0.20      AS max_rug_rate,                    -- <<< % rug/dump máximo
        10        AS min_launches,                    -- <<< nº mínimo de lanzamientos
        14        AS min_span_days,                   -- <<< días activo mínimos
        0.5       AS min_launches_per_day,            -- <<< frecuencia mínima
        3.0       AS max_launches_per_day             -- <<< frecuencia máxima
),

-- 1) Creaciones de token en pump.fun (mint + dev) --------------------------- #
creations AS (
    SELECT
        account_mint       AS mint,
        account_user       AS creator,
        call_block_time    AS created_at,
        call_block_slot    AS created_slot
    FROM pumpdotfun_solana.pump_call_create
    WHERE call_block_time >= (SELECT since_ts FROM params)
),

-- 2) Todos los swaps SOL<->token del mint (cualquier DEX) -------------------- #
--    Un solo barrido de dex_solana.trades, normalizado a (mint, side, ...).
all_trades AS (
    SELECT
        t.block_time,
        t.block_slot,
        t.trader_id AS trader,
        t.project,
        t.amount_usd,
        CASE WHEN t.token_bought_mint_address = 'So11111111111111111111111111111111111111112'
             THEN t.token_sold_mint_address ELSE t.token_bought_mint_address END AS mint,
        CASE WHEN t.token_bought_mint_address = 'So11111111111111111111111111111111111111112'
             THEN t.token_sold_amount ELSE t.token_bought_amount END AS token_amount,
        CASE WHEN t.token_bought_mint_address = 'So11111111111111111111111111111111111111112'
             THEN 'sell' ELSE 'buy' END AS side
    FROM dex_solana.trades t
    WHERE t.block_time >= (SELECT since_ts FROM params)
      AND t.project IN ('pumpdotfun', 'pumpswap', 'raydium')
      AND (t.token_bought_mint_address = 'So11111111111111111111111111111111111111112'
           OR t.token_sold_mint_address = 'So11111111111111111111111111111111111111112')
),

-- 3) Precio pico + vida + traders únicos, por token ------------------------- #
token_price AS (
    SELECT
        mint,
        MAX(amount_usd / NULLIF(token_amount, 0)) AS peak_price_usd,   -- USD por token
        MIN(block_time) AS first_trade,
        MAX(block_time) AS last_trade,
        COUNT(DISTINCT trader) AS uniq_traders,
        COUNT(*) AS n_trades
    FROM all_trades
    WHERE token_amount > 0 AND amount_usd > 0
    GROUP BY mint
),

-- 4) ¿Migró? (aparece bajo pumpswap o raydium) ------------------------------ #
migrated AS (
    SELECT DISTINCT mint
    FROM all_trades
    WHERE project IN ('pumpswap', 'raydium')
),

-- 5) Bundle / snipe en el MISMO slot de la creación ------------------------- #
launch_quality AS (
    SELECT
        c.mint,
        COUNT(DISTINCT CASE WHEN b.side = 'buy' AND b.block_slot = c.created_slot
                             AND b.trader <> c.creator THEN b.trader END) AS sameslot_buyers,
        COUNT(CASE WHEN b.side = 'buy' AND b.block_slot = c.created_slot
                    AND b.trader = c.creator THEN 1 END) AS dev_snipe_buys
    FROM creations c
    LEFT JOIN all_trades b ON b.mint = c.mint AND b.project = 'pumpdotfun'
    GROUP BY c.mint
),

-- 6) Dump del dev (ventas del creador en USD) ------------------------------- #
dev_dump AS (
    SELECT
        c.mint,
        COALESCE(SUM(CASE WHEN s.side = 'sell' AND s.trader = c.creator
                          THEN s.amount_usd END), 0) AS dev_sell_usd
    FROM creations c
    LEFT JOIN all_trades s ON s.mint = c.mint
    GROUP BY c.mint
),

-- 7) Métricas + clasificación por token ------------------------------------- #
token_level AS (
    SELECT
        c.creator,
        c.mint,
        c.created_at,
        p.peak_price_usd * 1e9 AS peak_mcap_usd,               -- supply fijo 1e9
        DATE_DIFF('second', c.created_at, COALESCE(p.last_trade, c.created_at)) / 3600.0 AS lifespan_h,
        COALESCE(p.uniq_traders, 0) AS uniq_traders,
        CASE WHEN m.mint IS NOT NULL THEN 1 ELSE 0 END AS migrated,
        COALESCE(lq.sameslot_buyers, 0) AS sameslot_buyers,
        COALESCE(lq.dev_snipe_buys, 0)  AS dev_snipe_buys,
        COALESCE(dd.dev_sell_usd, 0)    AS dev_sell_usd
    FROM creations c
    LEFT JOIN token_price   p  ON p.mint  = c.mint
    LEFT JOIN migrated      m  ON m.mint  = c.mint
    LEFT JOIN launch_quality lq ON lq.mint = c.mint
    LEFT JOIN dev_dump      dd ON dd.mint = c.mint
),

classified AS (
    SELECT
        *,
        -- Orgánico: sin bundle grande en el slot de creación y sin snipe del dev.
        CASE WHEN sameslot_buyers >= 4 OR dev_snipe_buys >= 1 THEN 0 ELSE 1 END AS is_organic,
        -- Rug/dump: muere en <30 min, o no arranca, o el dev vende fuerte.
        CASE WHEN lifespan_h < 0.5 OR peak_mcap_usd < 5000 OR dev_sell_usd > 3000 THEN 1 ELSE 0 END AS is_rug
    FROM token_level
),

-- 8) Agregado por DEV ------------------------------------------------------- #
dev_agg AS (
    SELECT
        creator,
        COUNT(*) AS launches,
        DATE_DIFF('day', MIN(created_at), MAX(created_at)) + 1 AS span_days,
        COUNT(*) * 1.0 / (DATE_DIFF('day', MIN(created_at), MAX(created_at)) + 1) AS launches_per_day,
        APPROX_PERCENTILE(peak_mcap_usd, 0.5) AS median_peak_mcap_usd,
        APPROX_PERCENTILE(lifespan_h, 0.5)    AS median_lifespan_h,
        AVG(CAST(migrated   AS double)) AS migration_rate,
        AVG(CAST(is_organic AS double)) AS organic_rate,
        AVG(CAST(is_rug     AS double)) AS rug_rate,
        APPROX_PERCENTILE(uniq_traders, 0.5) AS median_uniq_traders,
        MAX(created_at) AS last_launch_at,
        -- 10 mints más recientes: los usa el script para consultar DexScreener.
        SLICE(ARRAY_AGG(mint ORDER BY created_at DESC), 1, 10) AS sample_mints
    FROM classified
    GROUP BY creator
)

SELECT
    creator,
    launches,
    span_days,
    ROUND(launches_per_day, 2)        AS launches_per_day,
    ROUND(median_peak_mcap_usd, 0)    AS median_peak_mcap_usd,
    ROUND(median_lifespan_h, 2)       AS median_lifespan_h,
    ROUND(migration_rate, 3)          AS migration_rate,
    ROUND(organic_rate, 3)            AS organic_rate,
    ROUND(rug_rate, 3)                AS rug_rate,
    median_uniq_traders,
    last_launch_at,
    sample_mints
FROM dev_agg, params
WHERE launches            >= min_launches
  AND span_days           >= min_span_days
  AND launches_per_day    BETWEEN min_launches_per_day AND max_launches_per_day
  AND median_peak_mcap_usd >= min_median_mcap_usd
  AND median_lifespan_h   >= min_median_lifespan_h
  AND migration_rate      >= min_migration_rate
  AND organic_rate        >= min_organic_rate
  AND rug_rate            <= max_rug_rate
ORDER BY median_peak_mcap_usd DESC, migration_rate DESC, organic_rate DESC
LIMIT 200;

-- ============================================================================
--  FALLBACK si pumpdotfun_solana.pump_call_create falla (columnas/casing):
--  sustituye la CTE `creations` por esta versión cruda (100% verificada):
--
--  creations AS (
--      SELECT
--          account_arguments[1] AS mint,
--          tx_signer            AS creator,
--          block_time           AS created_at,
--          block_slot           AS created_slot
--      FROM solana.instruction_calls
--      WHERE executing_account = '6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P'  -- pump.fun
--        AND bytearray_substring(data, 1, 8) = 0x181ec828051c0777              -- Create
--        AND tx_success = true
--        AND block_time >= (SELECT since_ts FROM params)
--  ),
-- ============================================================================

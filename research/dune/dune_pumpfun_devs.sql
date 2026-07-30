-- ============================================================================
--  pump.fun · 100 DEVS frecuentes, sanos y orgánicos (sin pump&dump 1ª hora)
-- ----------------------------------------------------------------------------
--  Motor: DuneSQL (Trino).  Blockchain: Solana.
--
--  Una fila por DEV (creador en pump.fun) con sus métricas, ya filtrada a:
--    · FRECUENTE: >=0.5 lanzamientos/día, consistente (>=15 tokens, >=14 días)
--    · SIN PUMP&DUMP en la 1ª hora: casi ningún token pumpea y se desploma <60min
--    · ORGÁNICO: sin bundle/snipe del propio dev en el slot de creación
--    · CON VOLUMEN: mediana de volumen por token por encima del umbral
--    · SANO: no muere al instante, no rug/dump del dev
--    · MIGRACIÓN: informativa (migration_rate), NO obligatoria
--
--  Ordenado por frecuencia (los más frecuentes primero). LIMIT 100.
--  El "paga DexScreener" se confirma luego en rank_pumpfun_devs.py.
--
--  AJUSTA los parámetros marcados con  <<<.
--  Coste: escanea dex_solana.trades de Solana; usa LOOKBACK corto al principio.
-- ============================================================================

WITH params AS (
    SELECT
        DATE_ADD('day', -30, NOW())  AS since_ts,     -- <<< ventana de análisis
        25000.0   AS min_median_mcap_usd,             -- <<< mediana MC máx mínima
        15000.0   AS min_median_volume_usd,           -- <<< mediana de volumen/token
        3000.0    AS min_median_vol_1h_usd,           -- <<< volumen mediano en 1ª hora
        0.10      AS max_pumpdump_1h_rate,            -- <<< % pump&dump 1ª hora (bajo)
        0.70      AS min_organic_rate,                -- <<< % lanzamientos orgánicos
        0.20      AS max_rug_rate,                    -- <<< % rug/dump máximo
        15        AS min_launches,                    -- <<< nº mínimo de lanzamientos
        14        AS min_span_days,                   -- <<< días activo mínimos
        0.5       AS min_launches_per_day             -- <<< frecuencia mínima
),

-- 1) Creaciones de token en pump.fun (mint + dev) --------------------------- #
creations AS (
    SELECT
        account_mint    AS mint,
        account_user    AS creator,
        call_block_time AS created_at,
        call_block_slot AS created_slot
    FROM pumpdotfun_solana.pump_call_create
    WHERE call_block_time >= (SELECT since_ts FROM params)
),

-- 2) Swaps SOL<->token normalizados (con precio USD por token) --------------- #
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
             THEN 'sell' ELSE 'buy' END AS side,
        t.amount_usd / NULLIF(
            CASE WHEN t.token_bought_mint_address = 'So11111111111111111111111111111111111111112'
                 THEN t.token_sold_amount ELSE t.token_bought_amount END, 0) AS price_usd
    FROM dex_solana.trades t
    WHERE t.block_time >= (SELECT since_ts FROM params)
      AND t.project IN ('pumpdotfun', 'pumpswap', 'raydium')
      AND (t.token_bought_mint_address = 'So11111111111111111111111111111111111111112'
           OR t.token_sold_mint_address = 'So11111111111111111111111111111111111111112')
),

-- 3) Precio pico, vida, volumen total, traders, por token ------------------- #
token_price AS (
    SELECT
        mint,
        MAX(price_usd)         AS peak_price_usd,
        SUM(amount_usd)        AS volume_usd,
        MIN(block_time)        AS first_trade,
        MAX(block_time)        AS last_trade,
        COUNT(DISTINCT trader) AS uniq_traders,
        COUNT(*)               AS n_trades
    FROM all_trades
    WHERE token_amount > 0 AND amount_usd > 0
    GROUP BY mint
),

-- 4) Comportamiento en la PRIMERA HORA (para detectar pump&dump) ------------ #
first_hour AS (
    SELECT
        c.mint,
        MIN_BY(t.price_usd, t.block_time) AS first_price,      -- primer precio
        MAX(t.price_usd)                  AS peak_1h,          -- pico en 1ª hora
        MAX_BY(t.price_usd, t.block_time) AS end_1h_price,     -- último precio de la hora
        SUM(t.amount_usd)                 AS vol_1h,
        COUNT(*)                          AS trades_1h,
        COUNT(DISTINCT t.trader)          AS traders_1h
    FROM creations c
    JOIN all_trades t
      ON t.mint = c.mint
     AND t.block_time BETWEEN c.created_at AND c.created_at + interval '60' minute
    WHERE t.amount_usd > 0 AND t.token_amount > 0
    GROUP BY c.mint
),

-- 5) ¿Migró? (aparece bajo pumpswap o raydium) ------------------------------ #
migrated AS (
    SELECT DISTINCT mint FROM all_trades WHERE project IN ('pumpswap', 'raydium')
),

-- 6) Bundle / snipe en el MISMO slot de la creación ------------------------- #
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

-- 7) Dump del dev ----------------------------------------------------------- #
dev_dump AS (
    SELECT
        c.mint,
        COALESCE(SUM(CASE WHEN s.side = 'sell' AND s.trader = c.creator
                          THEN s.amount_usd END), 0) AS dev_sell_usd
    FROM creations c
    LEFT JOIN all_trades s ON s.mint = c.mint
    GROUP BY c.mint
),

-- 8) Métricas + clasificación por token ------------------------------------- #
token_level AS (
    SELECT
        c.creator,
        c.mint,
        c.created_at,
        p.peak_price_usd * 1e9 AS peak_mcap_usd,        -- supply fijo 1e9
        COALESCE(p.volume_usd, 0) AS volume_usd,
        DATE_DIFF('second', c.created_at, COALESCE(p.last_trade, c.created_at)) / 3600.0 AS lifespan_h,
        COALESCE(p.uniq_traders, 0) AS uniq_traders,
        CASE WHEN m.mint IS NOT NULL THEN 1 ELSE 0 END AS migrated,
        COALESCE(fh.vol_1h, 0)      AS vol_1h,
        COALESCE(fh.trades_1h, 0)   AS trades_1h,
        COALESCE(lq.sameslot_buyers, 0) AS sameslot_buyers,
        COALESCE(lq.dev_snipe_buys, 0)  AS dev_snipe_buys,
        COALESCE(dd.dev_sell_usd, 0)    AS dev_sell_usd,
        -- PUMP&DUMP 1ª HORA: pumpea (pico>=2x el 1er precio) y se desploma
        -- (acaba la hora por debajo del 30% del pico).
        CASE WHEN fh.first_price > 0
              AND fh.peak_1h >= 2 * fh.first_price
              AND fh.end_1h_price <= 0.30 * fh.peak_1h
             THEN 1 ELSE 0 END AS is_pumpdump_1h
    FROM creations c
    LEFT JOIN token_price    p  ON p.mint  = c.mint
    LEFT JOIN first_hour     fh ON fh.mint = c.mint
    LEFT JOIN migrated       m  ON m.mint  = c.mint
    LEFT JOIN launch_quality lq ON lq.mint = c.mint
    LEFT JOIN dev_dump       dd ON dd.mint = c.mint
),

classified AS (
    SELECT
        *,
        CASE WHEN sameslot_buyers >= 4 OR dev_snipe_buys >= 1 THEN 0 ELSE 1 END AS is_organic,
        CASE WHEN lifespan_h < 0.5 OR peak_mcap_usd < 5000 OR dev_sell_usd > 3000 THEN 1 ELSE 0 END AS is_rug
    FROM token_level
),

-- 9) Agregado por DEV ------------------------------------------------------- #
dev_agg AS (
    SELECT
        creator,
        COUNT(*) AS launches,
        DATE_DIFF('day', MIN(created_at), MAX(created_at)) + 1 AS span_days,
        COUNT(*) * 1.0 / (DATE_DIFF('day', MIN(created_at), MAX(created_at)) + 1) AS launches_per_day,
        APPROX_PERCENTILE(peak_mcap_usd, 0.5) AS median_peak_mcap_usd,
        APPROX_PERCENTILE(volume_usd, 0.5)    AS median_volume_usd,
        APPROX_PERCENTILE(vol_1h, 0.5)        AS median_vol_1h_usd,
        APPROX_PERCENTILE(lifespan_h, 0.5)    AS median_lifespan_h,
        AVG(CAST(is_pumpdump_1h AS double))   AS pumpdump_1h_rate,
        AVG(CAST(migrated   AS double))       AS migration_rate,
        AVG(CAST(is_organic AS double))       AS organic_rate,
        AVG(CAST(is_rug     AS double))       AS rug_rate,
        APPROX_PERCENTILE(uniq_traders, 0.5)  AS median_uniq_traders,
        MAX(created_at) AS last_launch_at,
        SLICE(ARRAY_AGG(mint ORDER BY created_at DESC), 1, 10) AS sample_mints
    FROM classified
    GROUP BY creator
)

SELECT
    creator,
    launches,
    span_days,
    ROUND(launches_per_day, 2)     AS launches_per_day,
    ROUND(median_peak_mcap_usd, 0) AS median_peak_mcap_usd,
    ROUND(median_volume_usd, 0)    AS median_volume_usd,
    ROUND(median_vol_1h_usd, 0)    AS median_vol_1h_usd,
    ROUND(median_lifespan_h, 2)    AS median_lifespan_h,
    ROUND(pumpdump_1h_rate, 3)     AS pumpdump_1h_rate,
    ROUND(migration_rate, 3)       AS migration_rate,   -- informativo (sí/no en %)
    ROUND(organic_rate, 3)         AS organic_rate,
    ROUND(rug_rate, 3)             AS rug_rate,
    median_uniq_traders,
    last_launch_at,
    sample_mints
FROM dev_agg, params
WHERE launches             >= min_launches
  AND span_days            >= min_span_days
  AND launches_per_day     >= min_launches_per_day
  AND median_peak_mcap_usd >= min_median_mcap_usd
  AND median_volume_usd    >= min_median_volume_usd
  AND median_vol_1h_usd    >= min_median_vol_1h_usd
  AND pumpdump_1h_rate     <= max_pumpdump_1h_rate
  AND organic_rate         >= min_organic_rate
  AND rug_rate             <= max_rug_rate
ORDER BY launches_per_day DESC, median_volume_usd DESC
LIMIT 100;

-- ============================================================================
--  FALLBACK si pumpdotfun_solana.pump_call_create falla (columnas/casing):
--  reemplaza la CTE `creations` por (verificado):
--
--  creations AS (
--      SELECT account_arguments[1] AS mint, tx_signer AS creator,
--             block_time AS created_at, block_slot AS created_slot
--      FROM solana.instruction_calls
--      WHERE executing_account = '6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P'
--        AND bytearray_substring(data, 1, 8) = 0x181ec828051c0777
--        AND tx_success = true
--        AND block_time >= (SELECT since_ts FROM params)
--  ),
-- ============================================================================

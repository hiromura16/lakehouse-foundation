-- 役割: Silverへ採用しなかった「最新候補」を、理由と元の文字列付きで保存する。
-- 入力はBronzeと更新済みSilverの親表。出力は6つのQuarantine表の置き換え。
-- 01_silver.sqlの後に実行し、同じ typed → ranked → latest → checkedを使う。
-- Silverとの差は最終条件がvalidation_reason IS NOT NULLである点。
-- 過去版や重複排除された行の全件台帳ではなく、最新候補の不正だけが対象。
-- raw_record_jsonに変換前文字列、_source_*に原本の出所が残るため原因を追える。
-- CASEの上から最初に該当した1理由だけを保存し、全違反を列挙するわけではない。
-- 親表の不採用によるunknown_*も今回のSilverに対して判定する。
-- 各表は独立commit。同時参照で全表が同じ世代に揃う保証はない。

-- erp_products: 製品マスター。業務キーproduct_idごとに1つの候補を判定する。
-- 型変換結果がNULLでも、原因となった文字列はraw_record_jsonで確認する。
CREATE OR REPLACE TABLE iceberg.quarantine.erp_products AS
-- TRYで変換できない値をNULLにし、型の不正として後段へ渡す。
WITH typed AS (
    SELECT
        product_id,
        product_name,
        product_family,
        unit,
        TRY_CAST(standard_cycle_seconds AS DECIMAL(18, 3)) AS standard_cycle_seconds,
        TRY(FROM_ISO8601_TIMESTAMP(updated_at)) AS updated_at,
        _source_file,
        _source_hash,
        _ingested_at,
        JSON_FORMAT(CAST(MAP(
            ARRAY[
                'product_id',
                'product_name',
                'product_family',
                'unit',
                'standard_cycle_seconds',
                'updated_at'
            ],
            ARRAY[
                product_id,
                product_name,
                product_family,
                unit,
                standard_cycle_seconds,
                updated_at
            ]
        ) AS JSON)) AS raw_record_json
    FROM iceberg.bronze.erp_products
),

-- 更新時刻の同値を、原本hash→取込時刻→元値JSONの順で比較する。
ranked AS (
    SELECT
        *,
        ROW_NUMBER() OVER (
            PARTITION BY product_id
            ORDER BY
                updated_at DESC NULLS LAST, _source_hash DESC,
                _ingested_at DESC, raw_record_json DESC
        ) AS version_rank
    FROM typed
),

-- 品質の良否を見る前に各キーの1位を確定し、古い版への復活を防ぐ。
latest AS (
    SELECT * FROM ranked
    WHERE version_rank = 1
),

-- 標準サイクルは正数。名称欠落もmissing_keyとして扱う。
-- 複数の問題がある場合も、CASEで先に現れた理由を代表値にする。
checked AS (
    SELECT
        *,
        CASE
            WHEN
                COALESCE(TRIM(product_id), '') = ''
                OR COALESCE(TRIM(product_name), '') = ''
                THEN 'missing_key'
            WHEN updated_at IS NULL THEN 'invalid_timestamp'
            WHEN standard_cycle_seconds IS NULL THEN 'invalid_type'
            WHEN standard_cycle_seconds <= 0 THEN 'nonpositive_cycle'
            ELSE CAST(NULL AS VARCHAR)
        END AS validation_reason
    FROM latest
)

-- 不正な最新候補だけを保存し、理由と元値を修正・再投入の照合に使う。
SELECT
    product_id,
    updated_at,
    raw_record_json,
    _source_file,
    _source_hash,
    _ingested_at,
    validation_reason
FROM checked
WHERE validation_reason IS NOT NULL;

-- erp_work_orders: 製造指図。業務キーwork_order_idごとに1つの候補を判定する。
-- 型変換結果がNULLでも、原因となった文字列はraw_record_jsonで確認する。
CREATE OR REPLACE TABLE iceberg.quarantine.erp_work_orders AS
-- TRYで変換できない値をNULLにし、型の不正として後段へ渡す。
WITH typed AS (
    SELECT
        work_order_id,
        factory_id,
        product_id,
        TRY_CAST(planned_date AS DATE) AS planned_date,
        TRY_CAST(planned_qty AS BIGINT) AS planned_qty,
        TRY(FROM_ISO8601_TIMESTAMP(updated_at)) AS updated_at,
        _source_file,
        _source_hash,
        _ingested_at,
        JSON_FORMAT(CAST(MAP(
            ARRAY[
                'work_order_id',
                'factory_id',
                'product_id',
                'planned_date',
                'planned_qty',
                'updated_at'
            ],
            ARRAY[
                work_order_id,
                factory_id,
                product_id,
                planned_date,
                planned_qty,
                updated_at
            ]
        ) AS JSON)) AS raw_record_json
    FROM iceberg.bronze.erp_work_orders
),

-- 更新時刻の同値を、原本hash→取込時刻→元値JSONの順で比較する。
ranked AS (
    SELECT
        *,
        ROW_NUMBER() OVER (
            PARTITION BY work_order_id
            ORDER BY
                updated_at DESC NULLS LAST, _source_hash DESC,
                _ingested_at DESC, raw_record_json DESC
        ) AS version_rank
    FROM typed
),

-- 品質の良否を見る前に各キーの1位を確定し、古い版への復活を防ぐ。
latest AS (
    SELECT * FROM ranked
    WHERE version_rank = 1
),

-- 予定数0は許容し、製品が今回のSilverマスターに存在するかを確認する。
-- 複数の問題がある場合も、CASEで先に現れた理由を代表値にする。
checked AS (
    SELECT
        *,
        CASE
            WHEN
                COALESCE(TRIM(work_order_id), '') = ''
                OR COALESCE(TRIM(factory_id), '') = ''
                THEN 'missing_key'
            WHEN updated_at IS NULL THEN 'invalid_timestamp'
            WHEN planned_date IS NULL THEN 'invalid_date'
            WHEN planned_qty IS NULL THEN 'invalid_type'
            WHEN planned_qty < 0 THEN 'negative_quantity'
            WHEN NOT EXISTS (
                SELECT 1
                FROM iceberg.silver.erp_products AS p
                WHERE p.product_id = latest.product_id
            ) THEN 'unknown_product'
            ELSE CAST(NULL AS VARCHAR)
        END AS validation_reason
    FROM latest
)

-- 不正な最新候補だけを保存し、理由と元値を修正・再投入の照合に使う。
SELECT
    work_order_id,
    updated_at,
    raw_record_json,
    _source_file,
    _source_hash,
    _ingested_at,
    validation_reason
FROM checked
WHERE validation_reason IS NOT NULL;

-- erp_sales_orders: 受注。業務キーsales_order_idごとに1つの候補を判定する。
-- 型変換結果がNULLでも、原因となった文字列はraw_record_jsonで確認する。
CREATE OR REPLACE TABLE iceberg.quarantine.erp_sales_orders AS
-- TRYで変換できない値をNULLにし、型の不正として後段へ渡す。
WITH typed AS (
    SELECT
        sales_order_id,
        factory_id,
        product_id,
        TRY_CAST(order_date AS DATE) AS order_date,
        TRY_CAST(promised_date AS DATE) AS promised_date,
        TRY_CAST(ordered_qty AS BIGINT) AS ordered_qty,
        TRY_CAST(unit_price AS DECIMAL(18, 2)) AS unit_price,
        TRY(FROM_ISO8601_TIMESTAMP(updated_at)) AS updated_at,
        _source_file,
        _source_hash,
        _ingested_at,
        JSON_FORMAT(CAST(MAP(
            ARRAY[
                'sales_order_id',
                'factory_id',
                'product_id',
                'order_date',
                'promised_date',
                'ordered_qty',
                'unit_price',
                'updated_at'
            ],
            ARRAY[
                sales_order_id,
                factory_id,
                product_id,
                order_date,
                promised_date,
                ordered_qty,
                unit_price,
                updated_at
            ]
        ) AS JSON)) AS raw_record_json
    FROM iceberg.bronze.erp_sales_orders
),

-- 更新時刻の同値を、原本hash→取込時刻→元値JSONの順で比較する。
ranked AS (
    SELECT
        *,
        ROW_NUMBER() OVER (
            PARTITION BY sales_order_id
            ORDER BY
                updated_at DESC NULLS LAST, _source_hash DESC,
                _ingested_at DESC, raw_record_json DESC
        ) AS version_rank
    FROM typed
),

-- 品質の良否を見る前に各キーの1位を確定し、古い版への復活を防ぐ。
latest AS (
    SELECT * FROM ranked
    WHERE version_rank = 1
),

-- 受注数は正数・単価は0以上。約束日が受注日より前の版は採用しない。
-- 複数の問題がある場合も、CASEで先に現れた理由を代表値にする。
checked AS (
    SELECT
        *,
        CASE
            WHEN
                COALESCE(TRIM(sales_order_id), '') = ''
                OR COALESCE(TRIM(factory_id), '') = ''
                THEN 'missing_key'
            WHEN updated_at IS NULL THEN 'invalid_timestamp'
            WHEN
                order_date IS NULL
                OR promised_date IS NULL
                THEN 'invalid_date'
            WHEN promised_date < order_date THEN 'invalid_date_range'
            WHEN ordered_qty IS NULL OR unit_price IS NULL THEN 'invalid_type'
            WHEN ordered_qty <= 0 OR unit_price < 0 THEN 'invalid_quantity'
            WHEN NOT EXISTS (
                SELECT 1
                FROM iceberg.silver.erp_products AS p
                WHERE p.product_id = latest.product_id
            ) THEN 'unknown_product'
            ELSE CAST(NULL AS VARCHAR)
        END AS validation_reason
    FROM latest
)

-- 不正な最新候補だけを保存し、理由と元値を修正・再投入の照合に使う。
SELECT
    sales_order_id,
    updated_at,
    raw_record_json,
    _source_file,
    _source_hash,
    _ingested_at,
    validation_reason
FROM checked
WHERE validation_reason IS NOT NULL;

-- erp_shipments: 出荷明細。業務キーshipment_idごとに1つの候補を判定する。
-- 型変換結果がNULLでも、原因となった文字列はraw_record_jsonで確認する。
CREATE OR REPLACE TABLE iceberg.quarantine.erp_shipments AS
-- TRYで変換できない値をNULLにし、型の不正として後段へ渡す。
WITH typed AS (
    SELECT
        shipment_id,
        sales_order_id,
        TRY(FROM_ISO8601_TIMESTAMP(shipped_at)) AS shipped_at,
        TRY_CAST(shipped_qty AS BIGINT) AS shipped_qty,
        TRY(FROM_ISO8601_TIMESTAMP(updated_at)) AS updated_at,
        _source_file,
        _source_hash,
        _ingested_at,
        JSON_FORMAT(CAST(MAP(
            ARRAY[
                'shipment_id',
                'sales_order_id',
                'shipped_at',
                'shipped_qty',
                'updated_at'
            ],
            ARRAY[
                shipment_id,
                sales_order_id,
                shipped_at,
                shipped_qty,
                updated_at
            ]
        ) AS JSON)) AS raw_record_json
    FROM iceberg.bronze.erp_shipments
),

-- 更新時刻の同値を、原本hash→取込時刻→元値JSONの順で比較する。
ranked AS (
    SELECT
        *,
        ROW_NUMBER() OVER (
            PARTITION BY shipment_id
            ORDER BY
                updated_at DESC NULLS LAST, _source_hash DESC,
                _ingested_at DESC, raw_record_json DESC
        ) AS version_rank
    FROM typed
),

-- 品質の良否を見る前に各キーの1位を確定し、古い版への復活を防ぐ。
latest AS (
    SELECT * FROM ranked
    WHERE version_rank = 1
),

-- 出荷数は正数とし、採用済み受注が存在する明細だけを通す。
-- 複数の問題がある場合も、CASEで先に現れた理由を代表値にする。
checked AS (
    SELECT
        *,
        CASE
            WHEN COALESCE(TRIM(shipment_id), '') = '' THEN 'missing_key'
            WHEN
                updated_at IS NULL
                OR shipped_at IS NULL
                THEN 'invalid_timestamp'
            WHEN shipped_qty IS NULL THEN 'invalid_type'
            WHEN shipped_qty <= 0 THEN 'invalid_quantity'
            WHEN NOT EXISTS (
                SELECT 1
                FROM iceberg.silver.erp_sales_orders AS s
                WHERE s.sales_order_id = latest.sales_order_id
            ) THEN 'unknown_sales_order'
            ELSE CAST(NULL AS VARCHAR)
        END AS validation_reason
    FROM latest
)

-- 不正な最新候補だけを保存し、理由と元値を修正・再投入の照合に使う。
SELECT
    shipment_id,
    updated_at,
    raw_record_json,
    _source_file,
    _source_hash,
    _ingested_at,
    validation_reason
FROM checked
WHERE validation_reason IS NOT NULL;

-- mes_events: 生産実績イベント。業務キーevent_idごとに1つの候補を判定する。
-- 型変換結果がNULLでも、原因となった文字列はraw_record_jsonで確認する。
CREATE OR REPLACE TABLE iceberg.quarantine.mes_events AS
-- TRYで変換できない値をNULLにし、型の不正として後段へ渡す。
WITH typed AS (
    SELECT
        event_id,
        work_order_id,
        TRY(FROM_ISO8601_TIMESTAMP(event_time)) AS event_time,
        TRY_CAST(good_qty AS BIGINT) AS good_qty,
        TRY_CAST(reject_qty AS BIGINT) AS reject_qty,
        TRY(FROM_ISO8601_TIMESTAMP(updated_at)) AS updated_at,
        _source_file,
        _source_hash,
        _ingested_at,
        JSON_FORMAT(CAST(MAP(
            ARRAY[
                'event_id',
                'work_order_id',
                'event_time',
                'good_qty',
                'reject_qty',
                'updated_at'
            ],
            ARRAY[
                event_id,
                work_order_id,
                event_time,
                good_qty,
                reject_qty,
                updated_at
            ]
        ) AS JSON)) AS raw_record_json
    FROM iceberg.bronze.mes_events
),

-- 更新時刻の同値を、原本hash→取込時刻→元値JSONの順で比較する。
ranked AS (
    SELECT
        *,
        ROW_NUMBER() OVER (
            PARTITION BY event_id
            ORDER BY
                updated_at DESC NULLS LAST, _source_hash DESC,
                _ingested_at DESC, raw_record_json DESC
        ) AS version_rank
    FROM typed
),

-- 品質の良否を見る前に各キーの1位を確定し、古い版への復活を防ぐ。
latest AS (
    SELECT * FROM ranked
    WHERE version_rank = 1
),

-- 良品・不良数は0以上。工場は採用済み製造指図との関連から後段で求める。
-- 複数の問題がある場合も、CASEで先に現れた理由を代表値にする。
checked AS (
    SELECT
        *,
        CASE
            WHEN COALESCE(TRIM(event_id), '') = '' THEN 'missing_key'
            WHEN
                updated_at IS NULL
                OR event_time IS NULL
                THEN 'invalid_timestamp'
            WHEN good_qty IS NULL OR reject_qty IS NULL THEN 'invalid_type'
            WHEN good_qty < 0 OR reject_qty < 0 THEN 'negative_quantity'
            WHEN NOT EXISTS (
                SELECT 1
                FROM iceberg.silver.erp_work_orders AS w
                WHERE w.work_order_id = latest.work_order_id
            ) THEN 'unknown_work_order'
            ELSE CAST(NULL AS VARCHAR)
        END AS validation_reason
    FROM latest
)

-- 不正な最新候補だけを保存し、理由と元値を修正・再投入の照合に使う。
SELECT
    event_id,
    updated_at,
    raw_record_json,
    _source_file,
    _source_hash,
    _ingested_at,
    validation_reason
FROM checked
WHERE validation_reason IS NOT NULL;

-- mes_downtime: 設備停止区間。業務キーdowntime_idごとに1つの候補を判定する。
-- 型変換結果がNULLでも、原因となった文字列はraw_record_jsonで確認する。
CREATE OR REPLACE TABLE iceberg.quarantine.mes_downtime AS
-- TRYで変換できない値をNULLにし、型の不正として後段へ渡す。
WITH typed AS (
    SELECT
        downtime_id,
        work_order_id,
        TRY(FROM_ISO8601_TIMESTAMP(start_time)) AS start_time,
        TRY(FROM_ISO8601_TIMESTAMP(end_time)) AS end_time,
        reason_code,
        TRY(FROM_ISO8601_TIMESTAMP(updated_at)) AS updated_at,
        _source_file,
        _source_hash,
        _ingested_at,
        JSON_FORMAT(CAST(MAP(
            ARRAY[
                'downtime_id',
                'work_order_id',
                'start_time',
                'end_time',
                'reason_code',
                'updated_at'
            ],
            ARRAY[
                downtime_id,
                work_order_id,
                start_time,
                end_time,
                reason_code,
                updated_at
            ]
        ) AS JSON)) AS raw_record_json
    FROM iceberg.bronze.mes_downtime
),

-- 更新時刻の同値を、原本hash→取込時刻→元値JSONの順で比較する。
ranked AS (
    SELECT
        *,
        ROW_NUMBER() OVER (
            PARTITION BY downtime_id
            ORDER BY
                updated_at DESC NULLS LAST, _source_hash DESC,
                _ingested_at DESC, raw_record_json DESC
        ) AS version_rank
    FROM typed
),

-- 品質の良否を見る前に各キーの1位を確定し、古い版への復活を防ぐ。
latest AS (
    SELECT * FROM ranked
    WHERE version_rank = 1
),

-- 終了が開始より前の区間を拒否する。同時刻の開始・終了は許容する。
-- 複数の問題がある場合も、CASEで先に現れた理由を代表値にする。
checked AS (
    SELECT
        *,
        CASE
            WHEN COALESCE(TRIM(downtime_id), '') = '' THEN 'missing_key'
            WHEN
                updated_at IS NULL
                OR start_time IS NULL
                OR end_time IS NULL
                THEN 'invalid_timestamp'
            WHEN end_time < start_time THEN 'invalid_time_range'
            WHEN NOT EXISTS (
                SELECT 1
                FROM iceberg.silver.erp_work_orders AS w
                WHERE w.work_order_id = latest.work_order_id
            ) THEN 'unknown_work_order'
            ELSE CAST(NULL AS VARCHAR)
        END AS validation_reason
    FROM latest
)

-- 不正な最新候補だけを保存し、理由と元値を修正・再投入の照合に使う。
SELECT
    downtime_id,
    updated_at,
    raw_record_json,
    _source_file,
    _source_hash,
    _ingested_at,
    validation_reason
FROM checked
WHERE validation_reason IS NOT NULL;

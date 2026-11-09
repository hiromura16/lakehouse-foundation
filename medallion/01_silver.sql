-- MES/ERP medallion transformation for Trino 484 and Iceberg.
-- Each CREATE OR REPLACE is atomic for that table. The whole file is not one
-- transaction.
-- Run tables in this order so child records see the new parent master tables.
-- All Bronze business columns are strings. Metadata retains file/hash/ingest
-- provenance.
-- A primary key keeps its newest parseable updated_at, then source hash and
-- ingest time.
-- Exact duplicates collapse to one row. A rejected latest version does not
-- revive an older row.

-- erp_products: cast defensively, deduplicate by business key, validate
-- references.
CREATE OR REPLACE TABLE iceberg.silver.erp_products AS
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

latest AS (
    SELECT * FROM ranked
    WHERE version_rank = 1
),

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

SELECT
    product_id,
    product_name,
    product_family,
    unit,
    standard_cycle_seconds,
    updated_at,
    _source_file,
    _source_hash,
    _ingested_at
FROM checked
WHERE validation_reason IS NULL;

-- erp_work_orders: cast defensively, deduplicate by business key, validate
-- references.
CREATE OR REPLACE TABLE iceberg.silver.erp_work_orders AS
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

latest AS (
    SELECT * FROM ranked
    WHERE version_rank = 1
),

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

SELECT
    work_order_id,
    factory_id,
    product_id,
    planned_date,
    planned_qty,
    updated_at,
    _source_file,
    _source_hash,
    _ingested_at
FROM checked
WHERE validation_reason IS NULL;

-- erp_sales_orders: cast defensively, deduplicate by business key, validate
-- references.
CREATE OR REPLACE TABLE iceberg.silver.erp_sales_orders AS
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

latest AS (
    SELECT * FROM ranked
    WHERE version_rank = 1
),

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

SELECT
    sales_order_id,
    factory_id,
    product_id,
    order_date,
    promised_date,
    ordered_qty,
    unit_price,
    updated_at,
    _source_file,
    _source_hash,
    _ingested_at
FROM checked
WHERE validation_reason IS NULL;

-- erp_shipments: cast defensively, deduplicate by business key, validate
-- references.
CREATE OR REPLACE TABLE iceberg.silver.erp_shipments AS
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

latest AS (
    SELECT * FROM ranked
    WHERE version_rank = 1
),

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

SELECT
    shipment_id,
    sales_order_id,
    shipped_at,
    shipped_qty,
    updated_at,
    _source_file,
    _source_hash,
    _ingested_at
FROM checked
WHERE validation_reason IS NULL;

-- mes_events: cast defensively, deduplicate by business key, validate
-- references.
CREATE OR REPLACE TABLE iceberg.silver.mes_events AS
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

latest AS (
    SELECT * FROM ranked
    WHERE version_rank = 1
),

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

SELECT
    event_id,
    work_order_id,
    event_time,
    good_qty,
    reject_qty,
    updated_at,
    _source_file,
    _source_hash,
    _ingested_at
FROM checked
WHERE validation_reason IS NULL;

-- mes_downtime: cast defensively, deduplicate by business key, validate
-- references.
CREATE OR REPLACE TABLE iceberg.silver.mes_downtime AS
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

latest AS (
    SELECT * FROM ranked
    WHERE version_rank = 1
),

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

SELECT
    downtime_id,
    work_order_id,
    start_time,
    end_time,
    reason_code,
    updated_at,
    _source_file,
    _source_hash,
    _ingested_at
FROM checked
WHERE validation_reason IS NULL;

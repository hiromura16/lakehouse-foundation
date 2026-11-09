-- Daily factory quantities combine independent one-row-per-factory/day
-- aggregates.
-- Events and downtime join the unique Silver work order master before
-- aggregation.
-- Do not join raw events to shipments or downtime: that multiplies production
-- rows.
-- Event dates are UTC. Downtime minutes are assigned to the UTC start date.
-- No shift calendar or speed baseline is assumed, so this is not an OEE
-- calculation.
CREATE OR REPLACE TABLE iceberg.gold.factory_daily_production AS
WITH plans AS (
    SELECT
        factory_id,
        planned_date AS production_date,
        sum(planned_qty) AS planned_qty
    FROM iceberg.silver.erp_work_orders
    GROUP BY factory_id, planned_date
),

production AS (
    SELECT
        w.factory_id,
        cast(e.event_time AT TIME ZONE 'UTC' AS DATE) AS production_date,
        sum(e.good_qty) AS good_qty,
        sum(e.reject_qty) AS reject_qty
    FROM iceberg.silver.mes_events AS e
    INNER JOIN iceberg.silver.erp_work_orders AS w ON e.work_order_id = w.work_order_id
    GROUP BY w.factory_id, cast(e.event_time AT TIME ZONE 'UTC' AS DATE)
),

downtime AS (
    SELECT
        w.factory_id,
        cast(d.start_time AT TIME ZONE 'UTC' AS DATE) AS production_date,
        sum(date_diff('second', d.start_time, d.end_time)) / 60e0 AS downtime_minutes
    FROM iceberg.silver.mes_downtime AS d
    INNER JOIN iceberg.silver.erp_work_orders AS w ON d.work_order_id = w.work_order_id
    GROUP BY w.factory_id, cast(d.start_time AT TIME ZONE 'UTC' AS DATE)
),

factory_dates AS (
    SELECT
        factory_id,
        production_date
    FROM plans
    UNION DISTINCT
    SELECT
        factory_id,
        production_date
    FROM production
    UNION DISTINCT
    SELECT
        factory_id,
        production_date
    FROM downtime
),

combined AS (
    SELECT
        k.factory_id,
        k.production_date,
        coalesce(p.planned_qty, 0) AS planned_qty,
        coalesce(e.good_qty, 0) AS good_qty,
        coalesce(e.reject_qty, 0) AS reject_qty,
        cast(coalesce(d.downtime_minutes, 0) AS DOUBLE) AS downtime_minutes
    FROM factory_dates AS k
    LEFT JOIN plans AS p
        ON k.factory_id = p.factory_id AND k.production_date = p.production_date
    LEFT JOIN production AS e
        ON k.factory_id = e.factory_id AND k.production_date = e.production_date
    LEFT JOIN downtime AS d
        ON k.factory_id = d.factory_id AND k.production_date = d.production_date
)

SELECT
    *,
    good_qty + reject_qty AS total_qty,
    cast(round(100e0 * good_qty / nullif(good_qty + reject_qty, 0), 2) AS DOUBLE)
        AS yield_pct,
    cast(round(100e0 * good_qty / nullif(planned_qty, 0), 2) AS DOUBLE)
        AS plan_adherence_pct
FROM combined;

-- Sales is one row per unique order. Shipments aggregate before joining the
-- order.
-- A delivery is late when its UTC shipping date is after the promised date.
-- Open quantity is floored at zero. Over-fulfillment can exceed 100 percent.
-- The positive-quantity Silver contract prevents zero denominators, and NULLIF
-- also makes the calculation safe if that contract is later relaxed.
CREATE OR REPLACE TABLE iceberg.gold.sales_fulfillment AS
WITH shipment_totals AS (
    SELECT
        s.sales_order_id,
        sum(s.shipped_qty) AS shipped_qty,
        sum(CASE
            WHEN cast(s.shipped_at AT TIME ZONE 'UTC' AS DATE) > o.promised_date
                THEN s.shipped_qty
            ELSE 0
        END) AS late_delivered_qty,
        count_if(cast(s.shipped_at AT TIME ZONE 'UTC' AS DATE) > o.promised_date)
            AS late_shipment_count,
        max(s.shipped_at) AS last_shipped_at
    FROM iceberg.silver.erp_shipments AS s
    INNER JOIN iceberg.silver.erp_sales_orders AS o
        ON s.sales_order_id = o.sales_order_id
    GROUP BY s.sales_order_id
)

SELECT
    o.sales_order_id,
    o.factory_id,
    o.product_id,
    o.order_date,
    o.promised_date,
    o.ordered_qty,
    o.unit_price,
    coalesce(s.shipped_qty, 0) AS shipped_qty,
    greatest(o.ordered_qty - coalesce(s.shipped_qty, 0), 0) AS open_qty,
    cast(
        round(
            100e0 * coalesce(s.shipped_qty, 0) / nullif(o.ordered_qty, 0), 2
        ) AS DOUBLE
    ) AS fulfillment_pct,
    coalesce(s.late_delivered_qty, 0) AS late_delivered_qty,
    coalesce(s.late_shipment_count, 0) AS late_shipment_count,
    coalesce(s.shipped_qty, 0) >= o.ordered_qty AS is_fully_shipped,
    s.last_shipped_at
FROM iceberg.silver.erp_sales_orders AS o
LEFT JOIN shipment_totals AS s ON o.sales_order_id = s.sales_order_id;

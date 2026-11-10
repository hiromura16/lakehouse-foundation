-- 役割: 採用済みSilverから、工場×日と受注ごとの業務指標を作る。
-- 入力はSilverの指図・受注・出荷・実績・停止、出力はGold2表。
-- 01_silver.sqlの後に読む/実行する。
-- 工場日別では計画・生産・停止を別々に同じ粒度へ集計してから結合する。
-- 実績明細と停止明細を直接JOINすると行が掛け算になり、数量が水増しされる。
-- イベントの日付はUTC。停止時間はUTCの開始日に全量を割り当て、日跨ぎ分割しない。
-- シフト暦や速度基準を持たないため、歩留まり/計画達成をOEEと読み替えない。
-- 各CREATE OR REPLACEは表単位commitで、2表同時のsnapshotではない。

CREATE OR REPLACE TABLE iceberg.gold.factory_daily_production AS
-- 計画日がDATEで定義された指図を、まず工場×予定日へまとめる。
WITH plans AS (
    SELECT
        factory_id,
        planned_date AS production_date,
        sum(planned_qty) AS planned_qty
    FROM iceberg.silver.erp_work_orders
    GROUP BY factory_id, planned_date
),

-- 一意な指図から工場を引き、生産イベントのUTC日ごとに良品/不良を集計する。
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

-- 秒差を分へ換算する。重なった停止区間の統合や、日付境界での分割はしない。
downtime AS (
    SELECT
        w.factory_id,
        cast(d.start_time AT TIME ZONE 'UTC' AS DATE) AS production_date,
        sum(date_diff('second', d.start_time, d.end_time)) / 60e0 AS downtime_minutes
    FROM iceberg.silver.mes_downtime AS d
    INNER JOIN iceberg.silver.erp_work_orders AS w ON d.work_order_id = w.work_order_id
    GROUP BY w.factory_id, cast(d.start_time AT TIME ZONE 'UTC' AS DATE)
),

-- 計画だけ/実績だけ/停止だけの日も残すため、各集計のキーを和集合にする。
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

-- 既に1行/工場日になった集計同士を結び、該当しない数量は0として揃える。
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

-- 歩留まりは良品/(良品+不良)、計画達成率は良品/計画数。行率の単純平均ではない。
-- 分母0はNULL(未定義)とし、100e0で整数除算を避け、表示用に小数2桁へ丸める。
SELECT
    *,
    good_qty + reject_qty AS total_qty,
    cast(round(100e0 * good_qty / nullif(good_qty + reject_qty, 0), 2) AS DOUBLE)
        AS yield_pct,
    cast(round(100e0 * good_qty / nullif(planned_qty, 0), 2) AS DOUBLE)
        AS plan_adherence_pct
FROM combined;

-- 受注別の出荷充足: 出荷明細を先に受注1行へ集計してから受注明細へ結ぶ。
-- 遅延は「UTC出荷日 > 約束日」の出荷分。未出荷の期限超過を数える指標ではない。
-- 未出荷数は0を下限とする一方、過剰出荷の充足率は100%を超えたまま示す。
-- Silverの受注数は正数だが、将来の契約変更に備えて分母もNULLIFで保護する。
CREATE OR REPLACE TABLE iceberg.gold.sales_fulfillment AS
-- 同一受注に複数出荷があっても、受注数・単価を重複計上しない粒度へ畳む。
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

-- LEFT JOINで出荷のない受注も残す。数量は0、最終出荷時刻はNULLのままになる。
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

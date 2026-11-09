-- factory daily measures avoid join multiplication.
-- Fixture: seed 42, orders 20, days 7, scenario edge.
SELECT
    factory_id,
    CAST(production_date AS VARCHAR) AS production_day,
    planned_qty,
    good_qty,
    reject_qty,
    total_qty,
    downtime_minutes,
    yield_pct,
    plan_adherence_pct
FROM iceberg.gold.factory_daily_production
ORDER BY factory_id, production_date;

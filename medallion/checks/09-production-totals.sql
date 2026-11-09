-- production totals.
-- Fixture: seed 42, orders 20, days 7, scenario edge.
SELECT
    sum(planned_qty) AS planned_qty_total,
    sum(good_qty) AS good_qty_total,
    sum(reject_qty) AS reject_qty_total,
    sum(total_qty) AS total_qty_total,
    sum(downtime_minutes) AS downtime_minutes_total
FROM iceberg.gold.factory_daily_production;

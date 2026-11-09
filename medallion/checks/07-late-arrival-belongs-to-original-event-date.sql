-- late arrival belongs to original event date.
-- Fixture: seed 42, orders 20, days 7, scenario edge.
SELECT
    CAST(event_time AT TIME ZONE 'UTC' AS DATE) AS event_day,
    good_qty,
    reject_qty
FROM iceberg.silver.mes_events
WHERE event_id = 'EV-LATE';

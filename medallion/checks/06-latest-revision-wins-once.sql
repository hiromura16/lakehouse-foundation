-- latest revision wins once.
-- Fixture: seed 42, orders 20, days 7, scenario edge.
SELECT
    good_qty,
    reject_qty
FROM iceberg.silver.mes_events
WHERE event_id = 'EV00001-1';

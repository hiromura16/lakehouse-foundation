-- invalid original value remains inspectable.
-- Fixture: seed 42, orders 20, days 7, scenario edge.
SELECT
    validation_reason,
    json_extract_scalar(
        raw_record_json,
        '$.good_qty'
    ) AS raw_good_qty
FROM iceberg.quarantine.mes_events
WHERE event_id = 'EV-BAD-TYPE';

-- sales totals.
-- Fixture: seed 42, orders 20, days 7, scenario edge.
-- Assertion column order follows the serialized expected fixture.
-- noqa: disable=ST06
SELECT
    count(*) AS order_count,
    sum(ordered_qty) AS ordered_qty_total,
    sum(shipped_qty) AS shipped_qty_total,
    sum(open_qty) AS open_qty_total,
    sum(late_delivered_qty) AS late_delivered_qty_total,
    sum(late_shipment_count) AS late_shipment_count_total,
    count_if(is_fully_shipped) AS fully_shipped_orders,
    cast(round(
        sum(unit_price * ordered_qty),
        2
    ) AS DOUBLE) AS ordered_value
FROM iceberg.gold.sales_fulfillment;

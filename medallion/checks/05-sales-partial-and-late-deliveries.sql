-- sales partial and late deliveries.
-- Fixture: seed 42, orders 20, days 7, scenario edge.
SELECT
    sales_order_id,
    ordered_qty,
    shipped_qty,
    open_qty,
    fulfillment_pct,
    late_delivered_qty,
    late_shipment_count,
    is_fully_shipped
FROM iceberg.gold.sales_fulfillment
ORDER BY sales_order_id;

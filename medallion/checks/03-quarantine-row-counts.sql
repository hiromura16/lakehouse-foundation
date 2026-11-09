-- quarantine row counts.
-- Fixture: seed 42, orders 20, days 7, scenario edge.
SELECT
    'erp_products' AS source_table,
    count(*) AS row_count
FROM iceberg.quarantine.erp_products
UNION ALL
SELECT
    'erp_sales_orders' AS source_table,
    count(*) AS row_count
FROM iceberg.quarantine.erp_sales_orders
UNION ALL
SELECT
    'erp_shipments' AS source_table,
    count(*) AS row_count
FROM iceberg.quarantine.erp_shipments
UNION ALL
SELECT
    'erp_work_orders' AS source_table,
    count(*) AS row_count
FROM iceberg.quarantine.erp_work_orders
UNION ALL
SELECT
    'mes_downtime' AS source_table,
    count(*) AS row_count
FROM iceberg.quarantine.mes_downtime
UNION ALL
SELECT
    'mes_events' AS source_table,
    count(*) AS row_count
FROM iceberg.quarantine.mes_events
ORDER BY source_table;

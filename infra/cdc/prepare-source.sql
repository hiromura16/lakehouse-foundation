-- SQL Server / T-SQL. A database administrator must review and run this file.
-- The consumer and connector never run this preparation SQL automatically.
-- Replace the database, login, schema, table and column names for your site.
-- Review the impact of CDC capture jobs, retention, SQL Agent and log growth.
-- This template must be executed in the explicitly approved application DB.

DECLARE @ApprovedDatabase sysname = N'YOUR_APPROVED_DATABASE';
DECLARE @DbaApproved bit = 0;

IF @DbaApproved <> 1
    THROW 51000, 'A DBA must approve and customize this CDC template.', 1;

IF DB_NAME() <> @ApprovedDatabase
    OR DB_NAME() IN (N'master', N'model', N'msdb', N'tempdb')
    THROW 51001, 'Unexpected database. No CDC setting was changed.', 1;

-- Existing company tables must have a single-column business primary key.
-- The six canonical names below are examples. Customize them with table-map.
-- This operation does not add, alter or remove application columns or rows.
IF EXISTS (
    SELECT 1
    FROM (VALUES
        (N'erp_products'), (N'erp_work_orders'), (N'erp_sales_orders'),
        (N'erp_shipments'), (N'mes_events'), (N'mes_downtime')
    ) AS expected(table_name)
    WHERE NOT EXISTS (
        SELECT 1
        FROM sys.tables AS application_table
        INNER JOIN sys.schemas AS application_schema
            ON application_schema.schema_id = application_table.schema_id
        INNER JOIN sys.indexes AS primary_index
            ON primary_index.object_id = application_table.object_id
            AND primary_index.is_primary_key = 1
        WHERE application_schema.name = N'dbo'
            AND application_table.name = expected.table_name
            AND (
                SELECT COUNT(*)
                FROM sys.index_columns AS key_column
                WHERE key_column.object_id = primary_index.object_id
                    AND key_column.index_id = primary_index.index_id
                    AND key_column.key_ordinal > 0
            ) = 1
    )
)
    THROW 51002, 'Review table mapping and single-column primary keys.', 1;

-- Database-level and table-level CDC preparation requires DBA privileges.
IF NOT EXISTS (SELECT 1 FROM sys.databases WHERE name = DB_NAME() AND is_cdc_enabled = 1)
    EXEC sys.sp_cdc_enable_db;

IF DATABASE_PRINCIPAL_ID(N'lh_cdc_reader') IS NULL
    CREATE ROLE [lh_cdc_reader];

-- A DBA creates a dedicated capture login/user separately, using the site's
-- authentication policy. Never reuse SOURCE_SQLSERVER or application writers.
-- Replace YOUR_CAPTURE_USER after creating its login and database user.
ALTER ROLE [lh_cdc_reader] ADD MEMBER [YOUR_CAPTURE_USER];
-- Debezium 3.7 reads these metadata objects explicitly. Granting SELECT on
-- the whole cdc schema would also expose unrelated capture data, so avoid it.
GRANT SELECT ON [cdc].[captured_columns] TO [lh_cdc_reader];
GRANT SELECT ON [cdc].[change_tables] TO [lh_cdc_reader];
GRANT SELECT ON [cdc].[lsn_time_mapping] TO [lh_cdc_reader];
GRANT VIEW DATABASE STATE TO [lh_cdc_reader];
-- On SQL Server 2022+, review the following permission for CDC status DMVs.
-- GRANT VIEW DATABASE PERFORMANCE STATE TO [lh_cdc_reader];

DECLARE @Tables TABLE (table_name sysname, captured_columns nvarchar(max));
INSERT INTO @Tables VALUES
    (N'erp_products', N'product_id,product_name,product_family,unit,standard_cycle_seconds,updated_at'),
    (N'erp_work_orders', N'work_order_id,factory_id,product_id,planned_date,planned_qty,updated_at'),
    (N'erp_sales_orders', N'sales_order_id,factory_id,product_id,order_date,promised_date,ordered_qty,unit_price,updated_at'),
    (N'erp_shipments', N'shipment_id,sales_order_id,shipped_at,shipped_qty,updated_at'),
    (N'mes_events', N'event_id,work_order_id,event_time,good_qty,reject_qty,updated_at'),
    (N'mes_downtime', N'downtime_id,work_order_id,start_time,end_time,reason_code,updated_at');

DECLARE @Table sysname;
DECLARE @Columns nvarchar(max);
DECLARE capture_tables CURSOR LOCAL FAST_FORWARD FOR
    SELECT table_name, captured_columns FROM @Tables;
OPEN capture_tables;
FETCH NEXT FROM capture_tables INTO @Table, @Columns;
WHILE @@FETCH_STATUS = 0
BEGIN
    -- Existing capture instances and their roles/columns require manual review.
    -- Do not disable or replace an instance belonging to another application.
    IF EXISTS (
        SELECT 1 FROM cdc.change_tables
        WHERE source_object_id = OBJECT_ID(N'dbo.' + QUOTENAME(@Table))
    )
        THROW 51003, 'Capture instance already exists. Review it manually.', 1;

    EXEC sys.sp_cdc_enable_table
        @source_schema = N'dbo',
        @source_name = @Table,
        @role_name = N'lh_cdc_reader',
        @captured_column_list = @Columns,
        @supports_net_changes = 0;

    DECLARE @Grant nvarchar(max) =
        N'GRANT SELECT ON [dbo].' + QUOTENAME(@Table) + N' TO [lh_cdc_reader];';
    EXEC sys.sp_executesql @Grant;

    -- Debezium 3.7 reads the selected change table directly for streaming.
    -- Limit access to this capture instance, not the entire cdc schema.
    SET @Grant = N'GRANT SELECT ON [cdc].'
        + QUOTENAME(N'dbo_' + @Table + N'_CT')
        + N' TO [lh_cdc_reader];';
    EXEC sys.sp_executesql @Grant;
    FETCH NEXT FROM capture_tables INTO @Table, @Columns;
END;
CLOSE capture_tables;
DEALLOCATE capture_tables;

-- This template grants no server role, sysadmin, db_owner or application DML
-- to the capture reader. The Agent status check may additionally need a DBA
-- signed read-only scalar function. Configure its approved name through
-- CDC_SQLSERVER_AGENT_STATUS_QUERY rather than granting instance visibility.
-- Verify capture jobs and retention separately with the DBA's tooling.

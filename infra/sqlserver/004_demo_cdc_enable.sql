-- Fixed six-table CDC lifecycle; DBA manual execution only, never an API.
-- Public procedure references (the sql-server-ver16 version targets SQL 2022):
-- https://learn.microsoft.com/en-us/sql/relational-databases/system-stored-procedures/sys-sp-cdc-enable-table-transact-sql?view=sql-server-ver16
-- https://learn.microsoft.com/en-us/sql/relational-databases/system-stored-procedures/sys-sp-cdc-disable-table-transact-sql?view=sql-server-ver16
-- Fixed synthetic source only. Run manually as the DBA with sqlcmd -b.
-- Select the database using the client connection, then provide these vars:
-- TargetDatabase, InstallationId (32 lower hex), OwnershipHash (SHA-256 hex).
-- Render variables with scripts/prepare-sqlserver.py in a private file.
-- SQLCMD uses textual substitution: never supply untrusted variable strings.
-- Values are supplied privately. No runtime app receives administrator auth.
-- No existing dbo/legacy object, login, password, Swarm or deployment is changed.
-- Refuse a caller-owned transaction before changing session options.
-- Only the nested refusal disables XACT_ABORT; caller options are restored.
IF @@TRANCOUNT <> 0
BEGIN
    -- SET is local to this nested batch; caller options restore on return.
    EXEC sys.sp_executesql N'SET XACT_ABORT OFF;
        RAISERROR(''Use a dedicated DBA session without an outer transaction.'',
                  16,1);';
    RETURN;
END;
SET NOCOUNT ON;
SET XACT_ABORT ON;
DECLARE @ExpectedDatabase nvarchar(4000) = N'$(TargetDatabase)';
DECLARE @InstallationId nvarchar(4000) = N'$(InstallationId)';
DECLARE @OwnershipHash nvarchar(4000) = N'$(OwnershipHash)';
IF LEN(@ExpectedDatabase) NOT BETWEEN 1 AND 128
    OR @ExpectedDatabase COLLATE Latin1_General_100_BIN2 LIKE N'%[^A-Za-z0-9_]%'
    OR LEFT(@ExpectedDatabase, 1) COLLATE Latin1_General_100_BIN2 LIKE N'[0-9]'
    OR LOWER(@ExpectedDatabase) IN (N'master', N'model', N'msdb', N'tempdb')
    OR DB_NAME() COLLATE Latin1_General_100_BIN2
       <> @ExpectedDatabase COLLATE Latin1_General_100_BIN2
    THROW 51000, 'Select the explicitly approved non-system target database.', 1;
IF LEN(@InstallationId) <> 32
    OR @InstallationId COLLATE Latin1_General_100_BIN2 LIKE N'%[^0-9a-f]%'
    OR LEN(@OwnershipHash) <> 64
    OR @OwnershipHash COLLATE Latin1_General_100_BIN2 LIKE N'%[^0-9a-f]%'
    THROW 51001, 'Supply the private installation identity and ownership hash.', 1;
SET IMPLICIT_TRANSACTIONS OFF;
DECLARE @HasCdcLock bit=0, @LockResult int;
DECLARE @CaptureSnapshotReady bit=0, @CdcMutationAttempted bit=0;
DECLARE @PreflightCapturePresence TABLE (
    capture_instance sysname PRIMARY KEY, was_present bit NOT NULL
);
BEGIN TRY
BEGIN TRANSACTION;
-- Read the marker only after checking its columns. Never claim old tables.
IF OBJECT_ID(N'lh_demo_ctl.installation', N'U') IS NOT NULL
BEGIN
    IF COL_LENGTH(N'lh_demo_ctl.installation', N'installation_id') IS NULL
        OR COL_LENGTH(N'lh_demo_ctl.installation', N'ownership_hash') IS NULL
        OR COL_LENGTH(N'lh_demo_ctl.installation', N'schema_version') IS NULL
        OR COL_LENGTH(N'lh_demo_ctl.installation', N'singleton_id') IS NULL
        THROW 51002, 'An unrelated installation table exists; preserve it.', 1;
    DECLARE @ActualId char(32), @ActualHash char(64), @ActualVersion int;
    EXEC sys.sp_executesql
        N'SELECT @Id=installation_id, @Hash=ownership_hash,
                 @Version=schema_version FROM lh_demo_ctl.installation
          WHERE singleton_id=1',
        N'@Id char(32) OUTPUT, @Hash char(64) OUTPUT, @Version int OUTPUT',
        @Id=@ActualId OUTPUT, @Hash=@ActualHash OUTPUT,
        @Version=@ActualVersion OUTPUT;
    IF @ActualId IS NULL OR @ActualHash IS NULL
        OR @ActualVersion IS NULL OR @ActualVersion <> 1
        OR @ActualId COLLATE Latin1_General_100_BIN2
           <> @InstallationId COLLATE Latin1_General_100_BIN2
        OR @ActualHash COLLATE Latin1_General_100_BIN2
           <> @OwnershipHash COLLATE Latin1_General_100_BIN2
        THROW 51003, 'Installation ownership/version mismatch; no changes.', 1;
END
ELSE IF EXISTS (
    SELECT 1 FROM sys.tables t JOIN sys.schemas s ON t.schema_id=s.schema_id
    WHERE s.name IN (N'lh_demo_erp',N'lh_demo_mes',N'lh_demo_ctl')
)
    THROW 51004, 'Unmarked existing tables cannot be claimed automatically.', 1;
-- Any unexpected object in the fixed schemas requires separate DBA review.
IF EXISTS (
    SELECT 1 FROM sys.objects o JOIN sys.schemas s ON o.schema_id=s.schema_id
    WHERE o.is_ms_shipped=0 AND o.parent_object_id=0
      AND s.name IN (N'lh_demo_erp',N'lh_demo_mes',N'lh_demo_ctl')
      AND s.name+N'.'+o.name NOT IN (
        N'lh_demo_erp.products',N'lh_demo_erp.work_orders',
        N'lh_demo_erp.sales_orders',N'lh_demo_erp.shipments',
        N'lh_demo_mes.events',N'lh_demo_mes.downtime',
        N'lh_demo_ctl.installation',N'lh_demo_ctl.demo_runs',
        N'lh_demo_ctl.batch_watermarks',
        N'lh_demo_ctl.check_write_safety_v1'
      )
)
    THROW 51005, 'Unexpected fixed-schema objects exist; preserve them.', 1;
-- Refuse indirect writes into legacy assets through DML triggers or FKs.
-- The DBA sees complete metadata. No trigger/FK is disabled by these files.
IF EXISTS (
    SELECT 1 FROM sys.triggers tr JOIN sys.objects o ON tr.parent_id=o.object_id
    JOIN sys.schemas s ON o.schema_id=s.schema_id
    WHERE tr.is_disabled=0
      AND s.name IN (N'lh_demo_erp',N'lh_demo_mes',N'lh_demo_ctl')
)
    THROW 51016, 'Active fixed-schema triggers require separate review.', 1;
IF EXISTS (
    SELECT 1 FROM sys.foreign_keys f
    JOIN sys.objects p ON f.parent_object_id=p.object_id
    JOIN sys.schemas ps ON p.schema_id=ps.schema_id
    JOIN sys.objects r ON f.referenced_object_id=r.object_id
    JOIN sys.schemas rs ON r.schema_id=rs.schema_id
    WHERE ps.name IN (N'lh_demo_erp',N'lh_demo_mes',N'lh_demo_ctl')
       OR rs.name IN (N'lh_demo_erp',N'lh_demo_mes',N'lh_demo_ctl')
)
    THROW 51017, 'Fixed-table foreign keys require separate review.', 1;
IF OBJECT_ID(N'lh_demo_ctl.installation',N'U') IS NULL
    THROW 51101, 'Apply and validate the fixed source installation first.', 1;
IF NOT EXISTS (SELECT 1 FROM sys.databases
    WHERE database_id=DB_ID() AND is_cdc_enabled=1)
    THROW 51102, 'Database CDC must already be prepared by the DBA.', 1;
IF OBJECT_ID(N'cdc.change_tables',N'U') IS NULL
    OR OBJECT_ID(N'cdc.captured_columns',N'U') IS NULL
    OR OBJECT_ID(N'cdc.lsn_time_mapping',N'U') IS NULL
    THROW 51103, 'Required existing CDC metadata is unavailable.', 1;
EXEC @LockResult=sys.sp_getapplock
    @Resource=N'lakehouse.fixed-demo.cdc', @LockMode=N'Exclusive',
    @LockOwner=N'Session', @LockTimeout=10000;
IF @LockResult < 0
    THROW 51104, 'Another fixed CDC setup operation is active.', 1;
SET @HasCdcLock=1;
-- This lock serializes these scripts only. The DBA must reserve a window
-- without other administrators changing CDC, source DDL or shared jobs.
DECLARE @Reader nvarchar(4000)=N'$(SourceReaderUser)';
IF LEN(@Reader) NOT BETWEEN 1 AND 128
    OR DATABASE_PRINCIPAL_ID(@Reader) IS NULL
    OR DATABASE_PRINCIPAL_ID(@Reader)=DATABASE_PRINCIPAL_ID(N'dbo')
    OR NOT EXISTS (SELECT 1 FROM sys.database_principals
        WHERE principal_id=DATABASE_PRINCIPAL_ID(@Reader)
          AND type IN ('S','U','G','E','X'))
    THROW 51105, 'Choose the approved existing source-reader database user.', 1;
DECLARE @Targets TABLE (
    ordinal int PRIMARY KEY, source_schema sysname, source_table sysname,
    capture_instance sysname UNIQUE, primary_column sysname,
    source_object_id int NULL, primary_index sysname NULL,
    captured_column_list nvarchar(max)
);
INSERT INTO @Targets
    (ordinal,source_schema,source_table,capture_instance,primary_column,
     captured_column_list)
VALUES
    (1,N'lh_demo_erp',N'products',
     N'lh_demo_erp_products_v1',N'product_id',
     N'[product_id],[product_name],[product_family],[unit],[standard_cycle_seconds],[updated_at],[created_at],[run_id],[original_values]'),
    (2,N'lh_demo_erp',N'work_orders',
     N'lh_demo_erp_work_orders_v1',N'work_order_id',
     N'[work_order_id],[factory_id],[product_id],[planned_date],[planned_qty],[updated_at],[created_at],[run_id],[original_values]'),
    (3,N'lh_demo_erp',N'sales_orders',
     N'lh_demo_erp_sales_orders_v1',N'sales_order_id',
     N'[sales_order_id],[factory_id],[product_id],[order_date],[promised_date],[ordered_qty],[unit_price],[updated_at],[created_at],[run_id],[original_values]'),
    (4,N'lh_demo_erp',N'shipments',
     N'lh_demo_erp_shipments_v1',N'shipment_id',
     N'[shipment_id],[sales_order_id],[shipped_at],[shipped_qty],[updated_at],[created_at],[run_id],[original_values]'),
    (5,N'lh_demo_mes',N'events',
     N'lh_demo_mes_events_v1',N'event_id',
     N'[event_id],[work_order_id],[event_time],[good_qty],[reject_qty],[updated_at],[created_at],[run_id],[original_values]'),
    (6,N'lh_demo_mes',N'downtime',
     N'lh_demo_mes_downtime_v1',N'downtime_id',
     N'[downtime_id],[work_order_id],[start_time],[end_time],[reason_code],[updated_at],[created_at],[run_id],[original_values]');
DECLARE @ExpectedColumns TABLE (
    capture_instance sysname, column_ordinal int, column_name sysname,
    PRIMARY KEY (capture_instance,column_ordinal)
);
INSERT INTO @ExpectedColumns VALUES
    (N'lh_demo_erp_products_v1',1,N'product_id'),
    (N'lh_demo_erp_products_v1',2,N'product_name'),
    (N'lh_demo_erp_products_v1',3,N'product_family'),
    (N'lh_demo_erp_products_v1',4,N'unit'),
    (N'lh_demo_erp_products_v1',5,N'standard_cycle_seconds'),
    (N'lh_demo_erp_products_v1',6,N'updated_at'),
    (N'lh_demo_erp_products_v1',7,N'created_at'),
    (N'lh_demo_erp_products_v1',8,N'run_id'),
    (N'lh_demo_erp_products_v1',9,N'original_values'),
    (N'lh_demo_erp_work_orders_v1',1,N'work_order_id'),
    (N'lh_demo_erp_work_orders_v1',2,N'factory_id'),
    (N'lh_demo_erp_work_orders_v1',3,N'product_id'),
    (N'lh_demo_erp_work_orders_v1',4,N'planned_date'),
    (N'lh_demo_erp_work_orders_v1',5,N'planned_qty'),
    (N'lh_demo_erp_work_orders_v1',6,N'updated_at'),
    (N'lh_demo_erp_work_orders_v1',7,N'created_at'),
    (N'lh_demo_erp_work_orders_v1',8,N'run_id'),
    (N'lh_demo_erp_work_orders_v1',9,N'original_values'),
    (N'lh_demo_erp_sales_orders_v1',1,N'sales_order_id'),
    (N'lh_demo_erp_sales_orders_v1',2,N'factory_id'),
    (N'lh_demo_erp_sales_orders_v1',3,N'product_id'),
    (N'lh_demo_erp_sales_orders_v1',4,N'order_date'),
    (N'lh_demo_erp_sales_orders_v1',5,N'promised_date'),
    (N'lh_demo_erp_sales_orders_v1',6,N'ordered_qty'),
    (N'lh_demo_erp_sales_orders_v1',7,N'unit_price'),
    (N'lh_demo_erp_sales_orders_v1',8,N'updated_at'),
    (N'lh_demo_erp_sales_orders_v1',9,N'created_at'),
    (N'lh_demo_erp_sales_orders_v1',10,N'run_id'),
    (N'lh_demo_erp_sales_orders_v1',11,N'original_values'),
    (N'lh_demo_erp_shipments_v1',1,N'shipment_id'),
    (N'lh_demo_erp_shipments_v1',2,N'sales_order_id'),
    (N'lh_demo_erp_shipments_v1',3,N'shipped_at'),
    (N'lh_demo_erp_shipments_v1',4,N'shipped_qty'),
    (N'lh_demo_erp_shipments_v1',5,N'updated_at'),
    (N'lh_demo_erp_shipments_v1',6,N'created_at'),
    (N'lh_demo_erp_shipments_v1',7,N'run_id'),
    (N'lh_demo_erp_shipments_v1',8,N'original_values'),
    (N'lh_demo_mes_events_v1',1,N'event_id'),
    (N'lh_demo_mes_events_v1',2,N'work_order_id'),
    (N'lh_demo_mes_events_v1',3,N'event_time'),
    (N'lh_demo_mes_events_v1',4,N'good_qty'),
    (N'lh_demo_mes_events_v1',5,N'reject_qty'),
    (N'lh_demo_mes_events_v1',6,N'updated_at'),
    (N'lh_demo_mes_events_v1',7,N'created_at'),
    (N'lh_demo_mes_events_v1',8,N'run_id'),
    (N'lh_demo_mes_events_v1',9,N'original_values'),
    (N'lh_demo_mes_downtime_v1',1,N'downtime_id'),
    (N'lh_demo_mes_downtime_v1',2,N'work_order_id'),
    (N'lh_demo_mes_downtime_v1',3,N'start_time'),
    (N'lh_demo_mes_downtime_v1',4,N'end_time'),
    (N'lh_demo_mes_downtime_v1',5,N'reason_code'),
    (N'lh_demo_mes_downtime_v1',6,N'updated_at'),
    (N'lh_demo_mes_downtime_v1',7,N'created_at'),
    (N'lh_demo_mes_downtime_v1',8,N'run_id'),
    (N'lh_demo_mes_downtime_v1',9,N'original_values');
UPDATE t SET source_object_id=OBJECT_ID(
    QUOTENAME(source_schema)+N'.'+QUOTENAME(source_table),N'U')
FROM @Targets t;
IF EXISTS (SELECT 1 FROM @Targets WHERE source_object_id IS NULL)
    THROW 51106, 'All six fixed source tables must already exist.', 1;
IF EXISTS (
    SELECT 1 FROM @Targets t WHERE
        OBJECT_SCHEMA_NAME(t.source_object_id) COLLATE Latin1_General_100_BIN2
           <> t.source_schema COLLATE Latin1_General_100_BIN2
        OR OBJECT_NAME(t.source_object_id) COLLATE Latin1_General_100_BIN2
           <> t.source_table COLLATE Latin1_General_100_BIN2
)
    THROW 51107, 'Fixed source identifiers must match their exact case.', 1;
-- All cross-source string comparisons and EXCEPT projections below use
-- the same explicit BIN2 collation on both operands. Catalog/database
-- collation differences must not change identifier or permission meaning.
-- Only the declared source columns may be captured. Extra/renamed/computed
-- columns require a new reviewed schema/capture version, not automatic DDL.
IF EXISTS (
    SELECT 1 FROM @Targets t JOIN @ExpectedColumns e
      ON e.capture_instance COLLATE Latin1_General_100_BIN2
         = t.capture_instance COLLATE Latin1_General_100_BIN2
    LEFT JOIN sys.columns c ON c.object_id=t.source_object_id
      AND c.name COLLATE Latin1_General_100_BIN2
          =e.column_name COLLATE Latin1_General_100_BIN2
    WHERE c.column_id IS NULL OR c.is_computed=1
)
    OR EXISTS (
        SELECT 1 FROM @Targets t JOIN sys.columns c
          ON c.object_id=t.source_object_id
        WHERE NOT EXISTS (SELECT 1 FROM @ExpectedColumns e
          WHERE e.capture_instance COLLATE Latin1_General_100_BIN2
             = t.capture_instance COLLATE Latin1_General_100_BIN2
            AND e.column_name COLLATE Latin1_General_100_BIN2
                =c.name COLLATE Latin1_General_100_BIN2)
    )
    THROW 51108, 'Fixed source column contract mismatch.', 1;

DECLARE @ExpectedSourceTypes TABLE (
    source_schema sysname, source_table sysname, column_name sysname,
    type_name sysname, max_length smallint, precision_value tinyint,
    scale_value tinyint, nullable_value bit, primary_key_value bit
);
INSERT INTO @ExpectedSourceTypes VALUES
    (N'lh_demo_erp',N'products',N'product_id',N'nvarchar',320,0,0,0,1),
    (N'lh_demo_erp',N'products',N'product_name',N'nvarchar',512,0,0,1,0),
    (N'lh_demo_erp',N'products',N'product_family',N'nvarchar',256,0,0,1,0),
    (N'lh_demo_erp',N'products',N'unit',N'nvarchar',64,0,0,1,0),
    (N'lh_demo_erp',N'products',N'standard_cycle_seconds',N'decimal',9,18,3,1,0),
    (N'lh_demo_erp',N'products',N'updated_at',N'datetime2',8,27,7,0,0),
    (N'lh_demo_erp',N'products',N'created_at',N'datetime2',8,27,7,0,0),
    (N'lh_demo_erp',N'products',N'run_id',N'char',12,0,0,0,0),
    (N'lh_demo_erp',N'products',N'original_values',N'nvarchar',-1,0,0,0,0),
    (N'lh_demo_erp',N'work_orders',N'work_order_id',N'nvarchar',320,0,0,0,1),
    (N'lh_demo_erp',N'work_orders',N'factory_id',N'nvarchar',128,0,0,1,0),
    (N'lh_demo_erp',N'work_orders',N'product_id',N'nvarchar',320,0,0,1,0),
    (N'lh_demo_erp',N'work_orders',N'planned_date',N'date',3,10,0,1,0),
    (N'lh_demo_erp',N'work_orders',N'planned_qty',N'bigint',8,19,0,1,0),
    (N'lh_demo_erp',N'work_orders',N'updated_at',N'datetime2',8,27,7,0,0),
    (N'lh_demo_erp',N'work_orders',N'created_at',N'datetime2',8,27,7,0,0),
    (N'lh_demo_erp',N'work_orders',N'run_id',N'char',12,0,0,0,0),
    (N'lh_demo_erp',N'work_orders',N'original_values',N'nvarchar',-1,0,0,0,0),
    (N'lh_demo_erp',N'sales_orders',N'sales_order_id',N'nvarchar',320,0,0,0,1),
    (N'lh_demo_erp',N'sales_orders',N'factory_id',N'nvarchar',128,0,0,1,0),
    (N'lh_demo_erp',N'sales_orders',N'product_id',N'nvarchar',320,0,0,1,0),
    (N'lh_demo_erp',N'sales_orders',N'order_date',N'date',3,10,0,1,0),
    (N'lh_demo_erp',N'sales_orders',N'promised_date',N'date',3,10,0,1,0),
    (N'lh_demo_erp',N'sales_orders',N'ordered_qty',N'bigint',8,19,0,1,0),
    (N'lh_demo_erp',N'sales_orders',N'unit_price',N'decimal',9,18,2,1,0),
    (N'lh_demo_erp',N'sales_orders',N'updated_at',N'datetime2',8,27,7,0,0),
    (N'lh_demo_erp',N'sales_orders',N'created_at',N'datetime2',8,27,7,0,0),
    (N'lh_demo_erp',N'sales_orders',N'run_id',N'char',12,0,0,0,0),
    (N'lh_demo_erp',N'sales_orders',N'original_values',N'nvarchar',-1,0,0,0,0),
    (N'lh_demo_erp',N'shipments',N'shipment_id',N'nvarchar',320,0,0,0,1),
    (N'lh_demo_erp',N'shipments',N'sales_order_id',N'nvarchar',320,0,0,1,0),
    (N'lh_demo_erp',N'shipments',N'shipped_at',N'datetime2',8,27,7,1,0),
    (N'lh_demo_erp',N'shipments',N'shipped_qty',N'bigint',8,19,0,1,0),
    (N'lh_demo_erp',N'shipments',N'updated_at',N'datetime2',8,27,7,0,0),
    (N'lh_demo_erp',N'shipments',N'created_at',N'datetime2',8,27,7,0,0),
    (N'lh_demo_erp',N'shipments',N'run_id',N'char',12,0,0,0,0),
    (N'lh_demo_erp',N'shipments',N'original_values',N'nvarchar',-1,0,0,0,0),
    (N'lh_demo_mes',N'events',N'event_id',N'nvarchar',320,0,0,0,1),
    (N'lh_demo_mes',N'events',N'work_order_id',N'nvarchar',320,0,0,1,0),
    (N'lh_demo_mes',N'events',N'event_time',N'datetime2',8,27,7,1,0),
    (N'lh_demo_mes',N'events',N'good_qty',N'bigint',8,19,0,1,0),
    (N'lh_demo_mes',N'events',N'reject_qty',N'bigint',8,19,0,1,0),
    (N'lh_demo_mes',N'events',N'updated_at',N'datetime2',8,27,7,0,0),
    (N'lh_demo_mes',N'events',N'created_at',N'datetime2',8,27,7,0,0),
    (N'lh_demo_mes',N'events',N'run_id',N'char',12,0,0,0,0),
    (N'lh_demo_mes',N'events',N'original_values',N'nvarchar',-1,0,0,0,0),
    (N'lh_demo_mes',N'downtime',N'downtime_id',N'nvarchar',320,0,0,0,1),
    (N'lh_demo_mes',N'downtime',N'work_order_id',N'nvarchar',320,0,0,1,0),
    (N'lh_demo_mes',N'downtime',N'start_time',N'datetime2',8,27,7,1,0),
    (N'lh_demo_mes',N'downtime',N'end_time',N'datetime2',8,27,7,1,0),
    (N'lh_demo_mes',N'downtime',N'reason_code',N'nvarchar',128,0,0,1,0),
    (N'lh_demo_mes',N'downtime',N'updated_at',N'datetime2',8,27,7,0,0),
    (N'lh_demo_mes',N'downtime',N'created_at',N'datetime2',8,27,7,0,0),
    (N'lh_demo_mes',N'downtime',N'run_id',N'char',12,0,0,0,0),
    (N'lh_demo_mes',N'downtime',N'original_values',N'nvarchar',-1,0,0,0,0);
-- Refuse type/nullability drift before creating any capture instance. This
-- published source type contract is the same as 002_demo_tables.sql.
IF EXISTS (
    SELECT 1 FROM @ExpectedSourceTypes e JOIN @Targets t
      ON t.source_schema COLLATE Latin1_General_100_BIN2
         = e.source_schema COLLATE Latin1_General_100_BIN2
      AND t.source_table COLLATE Latin1_General_100_BIN2
         = e.source_table COLLATE Latin1_General_100_BIN2
    LEFT JOIN sys.columns c ON c.object_id=t.source_object_id
      AND c.name COLLATE Latin1_General_100_BIN2
          =e.column_name COLLATE Latin1_General_100_BIN2
    WHERE c.column_id IS NULL OR c.max_length<>e.max_length
       OR c.[precision]<>e.precision_value OR c.[scale]<>e.scale_value
       OR c.is_nullable<>e.nullable_value
       OR TYPE_NAME(c.user_type_id) COLLATE Latin1_General_100_BIN2
          <>e.type_name COLLATE Latin1_General_100_BIN2
)
    THROW 51128, 'The published fixed source scalar type contract differs.', 1;
UPDATE t SET primary_index=i.name FROM @Targets t JOIN sys.indexes i
    ON i.object_id=t.source_object_id AND i.is_primary_key=1
    AND i.is_disabled=0;
IF EXISTS (
    SELECT 1 FROM @Targets t WHERE t.primary_index IS NULL
      OR (SELECT COUNT(*) FROM sys.index_columns ic JOIN sys.indexes i
          ON ic.object_id=i.object_id AND ic.index_id=i.index_id
          WHERE ic.object_id=t.source_object_id AND i.is_primary_key=1
            AND ic.key_ordinal>0)<>1
      OR NOT EXISTS (
          SELECT 1 FROM sys.index_columns ic JOIN sys.indexes i
            ON ic.object_id=i.object_id AND ic.index_id=i.index_id
          JOIN sys.columns c ON c.object_id=ic.object_id
            AND c.column_id=ic.column_id
          WHERE ic.object_id=t.source_object_id AND i.is_primary_key=1
            AND ic.key_ordinal=1
            AND c.name COLLATE Latin1_General_100_BIN2
                =t.primary_column COLLATE Latin1_General_100_BIN2
      )
)
    THROW 51109, 'Each fixed source needs its declared single-column primary key.', 1;
-- The supplied source DDL is unpartitioned. Do not silently adopt partition
-- switch semantics, which CDC does not turn into row-level DELETE events.
IF EXISTS (
    SELECT 1 FROM @Targets t JOIN sys.indexes i ON i.object_id=t.source_object_id
    JOIN sys.data_spaces d ON i.data_space_id=d.data_space_id
    WHERE i.index_id IN (0,1) AND d.type='PS'
)
    THROW 51110, 'Partitioned fixed sources require a separate reviewed design.', 1;
-- Never add a second capture version or adopt a case-only alias.
-- OBJECT_ID deliberately uses catalog identifier resolution to discover
-- aliases on unrelated sources. Additional case-fold discovery rejects
-- ASCII fixed-name aliases even when object/CDC collations differ. This
-- is not general Unicode identifier equivalence. Only the original exact
-- BIN2 name may be reused or mutated by the lifecycle queries below.
IF EXISTS (
    SELECT 1 FROM cdc.change_tables ct JOIN @Targets t
      ON ct.source_object_id=t.source_object_id
      OR ct.object_id=OBJECT_ID(
          N'cdc.'+QUOTENAME(t.capture_instance+N'_CT'),N'U')
      OR UPPER(
          ct.capture_instance COLLATE Latin1_General_100_BIN2
      ) COLLATE Latin1_General_100_BIN2
         = UPPER(
             t.capture_instance COLLATE Latin1_General_100_BIN2
         ) COLLATE Latin1_General_100_BIN2
    WHERE ct.capture_instance COLLATE Latin1_General_100_BIN2
       <> t.capture_instance COLLATE Latin1_General_100_BIN2
)
    THROW 51111, 'A conflicting fixed capture identity exists; preserve it.', 1;
-- SQL2022 normally stores NULL here. Health requires a real, tracked source;
-- NULL alone is not evidence that orphan or pending-drop metadata is healthy.
IF EXISTS (
    SELECT 1 FROM cdc.change_tables ct
    LEFT JOIN sys.tables src ON src.object_id=ct.source_object_id
    LEFT JOIN sys.tables changed ON changed.object_id=ct.object_id
    WHERE src.object_id IS NULL OR src.is_tracked_by_cdc<>1
       OR (ct.has_drop_pending IS NOT NULL AND ct.has_drop_pending<>0)
       OR changed.object_id IS NULL
)
    THROW 51130, 'Pending or orphan CDC metadata requires DBA review.', 1;
-- Validate every existing fixed capture before changing any of the six.
IF EXISTS (
    SELECT 1 FROM cdc.change_tables ct JOIN @Targets t
      ON ct.capture_instance COLLATE Latin1_General_100_BIN2
         = t.capture_instance COLLATE Latin1_General_100_BIN2
    WHERE ct.capture_instance COLLATE Latin1_General_100_BIN2
       <> t.capture_instance COLLATE Latin1_General_100_BIN2
       OR ct.source_object_id<>t.source_object_id
       OR ct.role_name IS NULL
       OR ct.role_name COLLATE Latin1_General_100_BIN2
          <> N'lh_demo_cdc_reader' COLLATE Latin1_General_100_BIN2
       OR COALESCE(ct.supports_net_changes,1)<>0
       OR (ct.has_drop_pending IS NOT NULL AND ct.has_drop_pending<>0)
       OR ct.index_name IS NULL
       OR ct.index_name COLLATE Latin1_General_100_BIN2
          <> t.primary_index COLLATE Latin1_General_100_BIN2
       OR OBJECT_SCHEMA_NAME(ct.object_id) COLLATE Latin1_General_100_BIN2
          <> N'cdc' COLLATE Latin1_General_100_BIN2
       OR OBJECT_NAME(ct.object_id) COLLATE Latin1_General_100_BIN2
          <> (t.capture_instance+N'_CT') COLLATE Latin1_General_100_BIN2
)
    THROW 51112, 'Existing fixed capture identity, role or options differ.', 1;
-- The application version is installation schema=1 plus exact *_v1 names.
-- cdc.change_tables.version is an undocumented internal format and is not
-- treated as a published SQL Server version contract.
DECLARE @ActualColumns TABLE (
    capture_instance sysname, column_ordinal int, column_name sysname
);
INSERT INTO @ActualColumns
SELECT ct.capture_instance,cc.column_ordinal,cc.column_name
FROM cdc.change_tables ct JOIN @Targets t
    ON ct.capture_instance COLLATE Latin1_General_100_BIN2
       = t.capture_instance COLLATE Latin1_General_100_BIN2
JOIN cdc.captured_columns cc ON cc.object_id=ct.object_id;
IF EXISTS (
    SELECT e.capture_instance COLLATE Latin1_General_100_BIN2,
        e.column_ordinal,e.column_name COLLATE Latin1_General_100_BIN2
    FROM @ExpectedColumns e JOIN cdc.change_tables ct
      ON ct.capture_instance COLLATE Latin1_General_100_BIN2
         = e.capture_instance COLLATE Latin1_General_100_BIN2
    EXCEPT SELECT capture_instance COLLATE Latin1_General_100_BIN2,column_ordinal,
        column_name COLLATE Latin1_General_100_BIN2 FROM @ActualColumns
) OR EXISTS (
    SELECT capture_instance COLLATE Latin1_General_100_BIN2,column_ordinal,
        column_name COLLATE Latin1_General_100_BIN2 FROM @ActualColumns
    EXCEPT SELECT capture_instance COLLATE Latin1_General_100_BIN2,column_ordinal,
        column_name COLLATE Latin1_General_100_BIN2 FROM @ExpectedColumns
)
    THROW 51113, 'Existing captured-column set or order differs.', 1;
-- Compare source and change-table scalar storage types without assuming that
-- CT nullability equals source nullability (CDC permits nullable before data).
IF EXISTS (
    SELECT 1 FROM cdc.change_tables ct JOIN @Targets t
      ON ct.capture_instance COLLATE Latin1_General_100_BIN2
         = t.capture_instance COLLATE Latin1_General_100_BIN2
    JOIN cdc.captured_columns cc ON cc.object_id=ct.object_id
    LEFT JOIN sys.columns src ON src.object_id=t.source_object_id
      AND src.column_id=cc.column_id
    LEFT JOIN sys.columns changed ON changed.object_id=ct.object_id
      AND changed.name COLLATE Latin1_General_100_BIN2
         = cc.column_name COLLATE Latin1_General_100_BIN2
    WHERE src.column_id IS NULL OR changed.column_id IS NULL
      OR COALESCE(cc.is_computed,1)<>0
      OR changed.name COLLATE Latin1_General_100_BIN2
         <>cc.column_name COLLATE Latin1_General_100_BIN2
      OR src.name COLLATE Latin1_General_100_BIN2
         <>cc.column_name COLLATE Latin1_General_100_BIN2
      OR src.system_type_id<>changed.system_type_id
      OR src.max_length<>changed.max_length
      OR src.[precision]<>changed.[precision] OR src.[scale]<>changed.[scale]
)
    THROW 51114, 'Existing capture scalar types differ from the fixed source.', 1;
IF EXISTS (
    SELECT 1 FROM cdc.change_tables ct
    -- Native identity also finds saved case aliases in a CI catalog. This
    -- broadens rejection only; authorization still requires exact BIN2 names.
    WHERE (ct.role_name COLLATE Latin1_General_100_BIN2
       = N'lh_demo_cdc_reader' COLLATE Latin1_General_100_BIN2
       OR DATABASE_PRINCIPAL_ID(ct.role_name)
          = DATABASE_PRINCIPAL_ID(N'lh_demo_cdc_reader'))
      AND NOT EXISTS (
        SELECT 1 FROM @Targets t
        WHERE t.capture_instance COLLATE Latin1_General_100_BIN2
           = ct.capture_instance COLLATE Latin1_General_100_BIN2
    )
)
    THROW 51115, 'The fixed gating role is already used by another capture.', 1;
DECLARE @AllowedReadObjects TABLE (object_name nvarchar(257) PRIMARY KEY);
INSERT INTO @AllowedReadObjects VALUES
    (N'cdc.change_tables'),(N'cdc.captured_columns'),(N'cdc.lsn_time_mapping');
INSERT INTO @AllowedReadObjects
SELECT N'cdc.'+capture_instance+N'_CT' FROM @Targets;
INSERT INTO @AllowedReadObjects
SELECT N'cdc.fn_cdc_get_all_changes_'+capture_instance FROM @Targets;
IF DATABASE_PRINCIPAL_ID(N'lh_demo_cdc_reader') IS NOT NULL
BEGIN
    IF NOT EXISTS (SELECT 1 FROM sys.database_principals r
        WHERE r.name COLLATE Latin1_General_100_BIN2
           = N'lh_demo_cdc_reader' COLLATE Latin1_General_100_BIN2
          AND r.type='R'
          AND r.owning_principal_id=DATABASE_PRINCIPAL_ID(N'dbo'))
        THROW 51116, 'The fixed CDC role has an unrelated kind or owner.', 1;
    IF EXISTS (
        SELECT 1 FROM sys.database_role_members m
        WHERE m.member_principal_id=DATABASE_PRINCIPAL_ID(N'lh_demo_cdc_reader')
          OR (m.role_principal_id=DATABASE_PRINCIPAL_ID(N'lh_demo_cdc_reader')
              AND m.member_principal_id<>DATABASE_PRINCIPAL_ID(@Reader))
    )
        THROW 51117, 'The fixed CDC role has unexpected memberships.', 1;
    IF EXISTS (
        SELECT 1 FROM sys.database_permissions p
        WHERE p.grantee_principal_id=DATABASE_PRINCIPAL_ID(N'lh_demo_cdc_reader')
          AND (p.class<>1 OR p.minor_id<>0 OR p.state<>'G'
              OR p.permission_name COLLATE Latin1_General_100_BIN2
                 <> N'SELECT' COLLATE Latin1_General_100_BIN2 OR NOT EXISTS (
                  SELECT 1 FROM @AllowedReadObjects a
                  WHERE OBJECT_ID(a.object_name)=p.major_id
              ))
    ) OR EXISTS (
        SELECT 1 FROM sys.database_principals
        WHERE owning_principal_id=DATABASE_PRINCIPAL_ID(N'lh_demo_cdc_reader')
    ) OR EXISTS (
        SELECT 1 FROM sys.schemas
        WHERE principal_id=DATABASE_PRINCIPAL_ID(N'lh_demo_cdc_reader')
    ) OR EXISTS (
        SELECT 1 FROM sys.objects
        WHERE principal_id=DATABASE_PRINCIPAL_ID(N'lh_demo_cdc_reader')
    )
        THROW 51118, 'The existing CDC role exceeds fixed read-only permissions.', 1;
END
-- Job health is independent of capture health. Empty databases may have
-- zero jobs or the existing pair. Existing captures require both shared jobs.
-- Transactional replication uses another log-reader topology; review it first.
IF EXISTS (SELECT 1 FROM sys.databases WHERE database_id=DB_ID() AND is_published=1)
    THROW 51131, 'Review the replication job topology separately.', 1;
DECLARE @CdcJobsBefore TABLE (
    job_id uniqueidentifier, job_type nvarchar(20), job_name sysname,
    maxtrans int, maxscans int, continuous bit, pollinginterval bigint,
    retention bigint, threshold bigint
);
DECLARE @CdcJobsAfter TABLE (
    job_id uniqueidentifier, job_type nvarchar(20), job_name sysname,
    maxtrans int, maxscans int, continuous bit, pollinginterval bigint,
    retention bigint, threshold bigint
);
IF OBJECT_ID(N'msdb.dbo.cdc_jobs',N'U') IS NOT NULL
    INSERT INTO @CdcJobsBefore EXEC sys.sp_cdc_help_jobs;
IF (SELECT COUNT(*) FROM @CdcJobsBefore) NOT IN (0,2)
    OR (EXISTS (SELECT 1 FROM cdc.change_tables)
        AND (SELECT COUNT(*) FROM @CdcJobsBefore)<>2)
    OR EXISTS (SELECT 1 FROM @CdcJobsBefore j
        LEFT JOIN msdb.dbo.sysjobs sj ON sj.job_id=j.job_id
        WHERE sj.job_id IS NULL)
    OR ((SELECT COUNT(*) FROM @CdcJobsBefore)=2 AND (
        (SELECT COUNT(*) FROM @CdcJobsBefore
         WHERE job_type COLLATE Latin1_General_100_BIN2=N'capture')<>1
        OR (SELECT COUNT(*) FROM @CdcJobsBefore
            WHERE job_type COLLATE Latin1_General_100_BIN2=N'cleanup')<>1))
    THROW 51132, 'Shared CDC jobs are incomplete; preserve state and review.', 1;
SELECT N'fixed_demo_cdc_preflight' AS check_name,
    COUNT_BIG(*) AS fixed_capture_count
FROM cdc.change_tables ct JOIN @Targets t
  ON ct.capture_instance COLLATE Latin1_General_100_BIN2
     = t.capture_instance COLLATE Latin1_General_100_BIN2;
IF DATABASE_PRINCIPAL_ID(N'lh_demo_cdc_reader') IS NULL
    CREATE ROLE [lh_demo_cdc_reader] AUTHORIZATION [dbo];
IF NOT EXISTS (SELECT 1 FROM sys.database_role_members
    WHERE role_principal_id=DATABASE_PRINCIPAL_ID(N'lh_demo_cdc_reader')
      AND member_principal_id=DATABASE_PRINCIPAL_ID(@Reader))
BEGIN
    DECLARE @MemberSql nvarchar(max)=
        N'ALTER ROLE [lh_demo_cdc_reader] ADD MEMBER '+QUOTENAME(@Reader);
    EXEC sys.sp_executesql @MemberSql;
END;
-- Read-only CDC metadata needed by Debezium 3.7.1.Final. No schema-wide
-- SELECT, legacy capture data, server-state or writer role is granted here.
GRANT SELECT ON OBJECT::[cdc].[change_tables] TO [lh_demo_cdc_reader];
GRANT SELECT ON OBJECT::[cdc].[captured_columns] TO [lh_demo_cdc_reader];
GRANT SELECT ON OBJECT::[cdc].[lsn_time_mapping] TO [lh_demo_cdc_reader];
-- Record observed fixed capture presence before the autocommit lifecycle.
-- A later difference is an observation, not proof that this caller caused it.
INSERT INTO @PreflightCapturePresence (capture_instance,was_present)
SELECT t.capture_instance,
    CONVERT(bit,CASE WHEN ct.object_id IS NULL THEN 0 ELSE 1 END)
FROM @Targets t LEFT JOIN cdc.change_tables ct
  ON ct.capture_instance COLLATE Latin1_General_100_BIN2
     = t.capture_instance COLLATE Latin1_General_100_BIN2;
SET @CaptureSnapshotReady=1;
COMMIT TRANSACTION;
-- Validation and role changes use a transaction; CDC procedure calls below
-- deliberately run in autocommit after preflight COMMIT. Microsoft documents
-- the procedures and job side effects but does not promise six-table atomic
-- rollback under an enclosing user transaction. Partial completion can remain.
-- Stop on errors, inspect privately, then rerun: exact existing captures are
-- preserved and only missing/remaining fixed instances are processed.
-- Runtime evidence is recorded separately from source/AST validation.
DECLARE @Schema sysname,@Table sysname,@Capture sysname,
    @PrimaryIndex sysname,@CapturedColumns nvarchar(max),@GrantSql nvarchar(max),@Result int;
DECLARE FixedCdcTargets CURSOR LOCAL FAST_FORWARD FOR
    SELECT source_schema,source_table,capture_instance,primary_index,captured_column_list
    FROM @Targets ORDER BY ordinal;
OPEN FixedCdcTargets;
FETCH NEXT FROM FixedCdcTargets INTO @Schema,@Table,@Capture,@PrimaryIndex,@CapturedColumns;
WHILE @@FETCH_STATUS=0
BEGIN
    IF NOT EXISTS (SELECT 1 FROM cdc.change_tables
        WHERE capture_instance COLLATE Latin1_General_100_BIN2
           = @Capture COLLATE Latin1_General_100_BIN2)
    BEGIN
        SET @CdcMutationAttempted=1;
        EXEC @Result=sys.sp_cdc_enable_table
            @source_schema=@Schema,@source_name=@Table,
            @capture_instance=@Capture,@role_name=N'lh_demo_cdc_reader',
            @supports_net_changes=0,@index_name=@PrimaryIndex,
            @captured_column_list=@CapturedColumns;
        -- Nonpartitioned tables always store partition_switch=1. Omit the
        -- ignored switch=0 to avoid warning 22857. Preflight rejects PS sources.
        -- https://learn.microsoft.com/en-us/sql/relational-databases/system-stored-procedures/sys-sp-cdc-enable-table-transact-sql?view=sql-server-ver16
        IF @Result<>0 OR NOT EXISTS (SELECT 1 FROM cdc.change_tables
            WHERE capture_instance COLLATE Latin1_General_100_BIN2
               = @Capture COLLATE Latin1_General_100_BIN2
              AND source_object_id=OBJECT_ID(QUOTENAME(@Schema)+N'.'+QUOTENAME(@Table)))
            THROW 51121, 'Fixed capture enablement failed; inspect private state.', 1;
    END;
    SET @GrantSql=N'GRANT SELECT ON OBJECT::[cdc].'
        +QUOTENAME(@Capture+N'_CT')+N' TO [lh_demo_cdc_reader]; '
        +N'GRANT SELECT ON OBJECT::[cdc].'
        +QUOTENAME(N'fn_cdc_get_all_changes_'+@Capture)
        +N' TO [lh_demo_cdc_reader];';
    EXEC sys.sp_executesql @GrantSql;
    FETCH NEXT FROM FixedCdcTargets INTO @Schema,@Table,@Capture,@PrimaryIndex,@CapturedColumns;
END;
IF (SELECT COUNT_BIG(*) FROM cdc.change_tables ct JOIN @Targets t
    ON ct.capture_instance COLLATE Latin1_General_100_BIN2
       = t.capture_instance COLLATE Latin1_General_100_BIN2)<>6
    THROW 51122, 'Not all fixed captures are present; retain partial state.', 1;
-- Revalidate the full published metadata after all new captures exist.
-- Validate every existing fixed capture before changing any of the six.
IF EXISTS (
    SELECT 1 FROM cdc.change_tables ct JOIN @Targets t
      ON ct.capture_instance COLLATE Latin1_General_100_BIN2
         = t.capture_instance COLLATE Latin1_General_100_BIN2
    WHERE ct.capture_instance COLLATE Latin1_General_100_BIN2
       <> t.capture_instance COLLATE Latin1_General_100_BIN2
       OR ct.source_object_id<>t.source_object_id
       OR ct.role_name IS NULL
       OR ct.role_name COLLATE Latin1_General_100_BIN2
          <> N'lh_demo_cdc_reader' COLLATE Latin1_General_100_BIN2
       OR COALESCE(ct.supports_net_changes,1)<>0
       OR (ct.has_drop_pending IS NOT NULL AND ct.has_drop_pending<>0)
       OR ct.index_name IS NULL
       OR ct.index_name COLLATE Latin1_General_100_BIN2
          <> t.primary_index COLLATE Latin1_General_100_BIN2
       OR OBJECT_SCHEMA_NAME(ct.object_id) COLLATE Latin1_General_100_BIN2
          <> N'cdc' COLLATE Latin1_General_100_BIN2
       OR OBJECT_NAME(ct.object_id) COLLATE Latin1_General_100_BIN2
          <> (t.capture_instance+N'_CT') COLLATE Latin1_General_100_BIN2
)
    THROW 51112, 'Existing fixed capture identity, role or options differ.', 1;
-- The application version is installation schema=1 plus exact *_v1 names.
-- cdc.change_tables.version is an undocumented internal format and is not
-- treated as a published SQL Server version contract.
DELETE FROM @ActualColumns;
INSERT INTO @ActualColumns
SELECT ct.capture_instance,cc.column_ordinal,cc.column_name
FROM cdc.change_tables ct JOIN @Targets t
    ON ct.capture_instance COLLATE Latin1_General_100_BIN2
       = t.capture_instance COLLATE Latin1_General_100_BIN2
JOIN cdc.captured_columns cc ON cc.object_id=ct.object_id;
IF EXISTS (
    SELECT e.capture_instance COLLATE Latin1_General_100_BIN2,
        e.column_ordinal,e.column_name COLLATE Latin1_General_100_BIN2
    FROM @ExpectedColumns e JOIN cdc.change_tables ct
      ON ct.capture_instance COLLATE Latin1_General_100_BIN2
         = e.capture_instance COLLATE Latin1_General_100_BIN2
    EXCEPT SELECT capture_instance COLLATE Latin1_General_100_BIN2,column_ordinal,
        column_name COLLATE Latin1_General_100_BIN2 FROM @ActualColumns
) OR EXISTS (
    SELECT capture_instance COLLATE Latin1_General_100_BIN2,column_ordinal,
        column_name COLLATE Latin1_General_100_BIN2 FROM @ActualColumns
    EXCEPT SELECT capture_instance COLLATE Latin1_General_100_BIN2,column_ordinal,
        column_name COLLATE Latin1_General_100_BIN2 FROM @ExpectedColumns
)
    THROW 51113, 'Existing captured-column set or order differs.', 1;
-- Compare source and change-table scalar storage types without assuming that
-- CT nullability equals source nullability (CDC permits nullable before data).
IF EXISTS (
    SELECT 1 FROM cdc.change_tables ct JOIN @Targets t
      ON ct.capture_instance COLLATE Latin1_General_100_BIN2
         = t.capture_instance COLLATE Latin1_General_100_BIN2
    JOIN cdc.captured_columns cc ON cc.object_id=ct.object_id
    LEFT JOIN sys.columns src ON src.object_id=t.source_object_id
      AND src.column_id=cc.column_id
    LEFT JOIN sys.columns changed ON changed.object_id=ct.object_id
      AND changed.name COLLATE Latin1_General_100_BIN2
         = cc.column_name COLLATE Latin1_General_100_BIN2
    WHERE src.column_id IS NULL OR changed.column_id IS NULL
      OR COALESCE(cc.is_computed,1)<>0
      OR changed.name COLLATE Latin1_General_100_BIN2
         <>cc.column_name COLLATE Latin1_General_100_BIN2
      OR src.name COLLATE Latin1_General_100_BIN2
         <>cc.column_name COLLATE Latin1_General_100_BIN2
      OR src.system_type_id<>changed.system_type_id
      OR src.max_length<>changed.max_length
      OR src.[precision]<>changed.[precision] OR src.[scale]<>changed.[scale]
)
    THROW 51114, 'Existing capture scalar types differ from the fixed source.', 1;

-- The first table creates the normal pair. Existing IDs and settings must
-- survive every rerun, including a database with zero captures but two jobs.
IF OBJECT_ID(N'msdb.dbo.cdc_jobs',N'U') IS NOT NULL
    INSERT INTO @CdcJobsAfter EXEC sys.sp_cdc_help_jobs;
IF (SELECT COUNT(*) FROM @CdcJobsAfter)<>2
    OR (SELECT COUNT(*) FROM @CdcJobsAfter
        WHERE job_type COLLATE Latin1_General_100_BIN2=N'capture')<>1
    OR (SELECT COUNT(*) FROM @CdcJobsAfter
        WHERE job_type COLLATE Latin1_General_100_BIN2=N'cleanup')<>1
    OR EXISTS (SELECT 1 FROM @CdcJobsAfter j
        LEFT JOIN msdb.dbo.sysjobs sj ON sj.job_id=j.job_id
        WHERE sj.job_id IS NULL)
    OR EXISTS (
        SELECT job_id,job_type COLLATE Latin1_General_100_BIN2,
        job_name COLLATE Latin1_General_100_BIN2,maxtrans,maxscans,continuous,
        pollinginterval,retention,threshold FROM @CdcJobsBefore
        EXCEPT SELECT job_id,job_type COLLATE Latin1_General_100_BIN2,
        job_name COLLATE Latin1_General_100_BIN2,maxtrans,maxscans,continuous,
        pollinginterval,retention,threshold FROM @CdcJobsAfter
    )
    THROW 51133, 'Shared CDC jobs changed or are missing; inspect private state.', 1;
SELECT N'fixed_demo_cdc_enabled' AS check_name,COUNT_BIG(*) AS fixed_capture_count
FROM cdc.change_tables ct JOIN @Targets t
  ON ct.capture_instance COLLATE Latin1_General_100_BIN2
     = t.capture_instance COLLATE Latin1_General_100_BIN2;
CLOSE FixedCdcTargets;
DEALLOCATE FixedCdcTargets;
IF @HasCdcLock=1
BEGIN
    EXEC sys.sp_releaseapplock @Resource=N'lakehouse.fixed-demo.cdc',
        @LockOwner=N'Session';
    SET @HasCdcLock=0;
END
END TRY
BEGIN CATCH
    IF XACT_STATE()<>0 ROLLBACK TRANSACTION;
    -- Keep the original failure even when metadata inspection itself fails.
    -- Output only six public fixed names and state flags, never private input
    -- or ERROR_MESSAGE(). A partial change must not trigger automatic cleanup.
    DECLARE @FailureNumber int=ERROR_NUMBER();
    BEGIN TRY
        DECLARE @CurrentPresenceReadable bit=0;
        DECLARE @CurrentCapturePresence TABLE (
            capture_instance sysname PRIMARY KEY
        );
        IF @HasCdcLock=1 AND OBJECT_ID(N'cdc.change_tables',N'U') IS NOT NULL
        BEGIN
            -- Dynamic SELECT defers binding missing CDC metadata until here.
            INSERT INTO @CurrentCapturePresence (capture_instance)
            EXEC sys.sp_executesql N'
                SELECT fixed.capture_instance
                FROM (VALUES
                    (N''lh_demo_erp_products_v1''),
                    (N''lh_demo_erp_work_orders_v1''),
                    (N''lh_demo_erp_sales_orders_v1''),
                    (N''lh_demo_erp_shipments_v1''),
                    (N''lh_demo_mes_events_v1''),
                    (N''lh_demo_mes_downtime_v1'')
                ) fixed(capture_instance)
                JOIN cdc.change_tables ct
                  ON ct.capture_instance COLLATE Latin1_General_100_BIN2
                     = fixed.capture_instance COLLATE Latin1_General_100_BIN2';
            SET @CurrentPresenceReadable=1;
        END;
        SELECT N'fixed_demo_cdc_enable_failure_state' AS check_name,
            fixed.capture_instance, @FailureNumber AS original_error_number,
            @CdcMutationAttempted AS cdc_mutation_attempted,
            CASE WHEN @CaptureSnapshotReady=0 OR before_state.was_present IS NULL
                    THEN N'unknown'
                 WHEN before_state.was_present=1 THEN N'present'
                 ELSE N'absent' END AS preflight_presence,
            CASE WHEN @CurrentPresenceReadable=0 THEN N'unknown'
                 WHEN now_state.capture_instance IS NULL THEN N'absent'
                 ELSE N'present' END AS current_presence,
            CASE WHEN @CaptureSnapshotReady=0 OR @CurrentPresenceReadable=0
                      OR before_state.was_present IS NULL
                    THEN N'unknown'
                 WHEN before_state.was_present=0
                      AND now_state.capture_instance IS NOT NULL
                    THEN N'became_present'
                 WHEN before_state.was_present=1
                      AND now_state.capture_instance IS NULL
                    THEN N'became_absent'
                 ELSE N'unchanged' END AS observed_change,
            N'preserve_state_and_review_before_rerun' AS next_action
        FROM (VALUES
            (N'lh_demo_erp_products_v1'),(N'lh_demo_erp_work_orders_v1'),
            (N'lh_demo_erp_sales_orders_v1'),(N'lh_demo_erp_shipments_v1'),
            (N'lh_demo_mes_events_v1'),(N'lh_demo_mes_downtime_v1')
        ) fixed(capture_instance)
        LEFT JOIN @PreflightCapturePresence before_state
          ON before_state.capture_instance COLLATE Latin1_General_100_BIN2
             = fixed.capture_instance COLLATE Latin1_General_100_BIN2
        LEFT JOIN @CurrentCapturePresence now_state
          ON now_state.capture_instance COLLATE Latin1_General_100_BIN2
             = fixed.capture_instance COLLATE Latin1_General_100_BIN2
        ORDER BY fixed.capture_instance COLLATE Latin1_General_100_BIN2;
    END TRY
    BEGIN CATCH
        SELECT N'fixed_demo_cdc_failure_diagnostic_unavailable' AS check_name,
            @FailureNumber AS original_error_number,
            N'manual_metadata_review_required' AS next_action;
    END CATCH;

    IF CURSOR_STATUS('local','FixedCdcTargets')>=0 CLOSE FixedCdcTargets;
    IF CURSOR_STATUS('local','FixedCdcTargets')>=-1 DEALLOCATE FixedCdcTargets;
    IF @HasCdcLock=1
        EXEC sys.sp_releaseapplock @Resource=N'lakehouse.fixed-demo.cdc',
            @LockOwner=N'Session';
    THROW;
END CATCH;

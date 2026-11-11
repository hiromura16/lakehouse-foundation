-- Fixed synthetic source only. Run manually as the DBA with sqlcmd -b.
-- Select the database using the client connection, then provide these vars:
-- TargetDatabase, InstallationId (32 lower hex), OwnershipHash (SHA-256 hex).
-- Values are supplied privately. No runtime app receives administrator auth.
-- No existing dbo/legacy object, login, password, Swarm or deployment is changed.
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
    IF @ActualId IS NULL OR @ActualHash IS NULL OR @ActualVersion <> 1
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
    WHERE o.type IN ('U','V','P','FN','IF','TF')
      AND s.name IN (N'lh_demo_erp',N'lh_demo_mes',N'lh_demo_ctl')
      AND s.name+N'.'+o.name NOT IN (
        N'lh_demo_erp.products',N'lh_demo_erp.work_orders',
        N'lh_demo_erp.sales_orders',N'lh_demo_erp.shipments',
        N'lh_demo_mes.events',N'lh_demo_mes.downtime',
        N'lh_demo_ctl.installation',N'lh_demo_ctl.demo_runs',
        N'lh_demo_ctl.batch_watermarks'
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
-- All schema creation is a separate DBA step; never manufacture a DB.
IF SCHEMA_ID(N'lh_demo_erp') IS NULL OR SCHEMA_ID(N'lh_demo_mes') IS NULL
    OR SCHEMA_ID(N'lh_demo_ctl') IS NULL
    THROW 51006, 'Apply 001_demo_schemas.sql first.', 1;
DECLARE @RepositoryVersion nvarchar(4000)=N'$(RepositoryVersion)';
IF LEN(@RepositoryVersion) NOT BETWEEN 1 AND 128
    OR @RepositoryVersion COLLATE Latin1_General_100_BIN2
       LIKE N'%[^A-Za-z0-9._/-]%'
    THROW 51007, 'Supply the reviewed public repository version.', 1;
IF OBJECT_ID(N'lh_demo_ctl.installation', N'U') IS NULL
CREATE TABLE [lh_demo_ctl].[installation] (
    [singleton_id] tinyint NOT NULL DEFAULT 1,
    [installation_id] char(32) NOT NULL,
    [ownership_hash] char(64) NOT NULL,
    [schema_version] int NOT NULL DEFAULT 1,
    [installed_at] datetime2(7) NOT NULL DEFAULT SYSUTCDATETIME(),
    [installed_by] nvarchar(128) NOT NULL DEFAULT ORIGINAL_LOGIN(),
    [repository_version] nvarchar(128) NOT NULL,
    CONSTRAINT [PK_lh_demo_ctl_installation] PRIMARY KEY ([singleton_id]),
    CONSTRAINT [CK_lh_demo_ctl_installation_singleton] CHECK (singleton_id=1),
    CONSTRAINT [UQ_lh_demo_ctl_installation_identity] UNIQUE (installation_id)
);

IF OBJECT_ID(N'lh_demo_ctl.demo_runs', N'U') IS NULL
CREATE TABLE [lh_demo_ctl].[demo_runs] (
    [run_id] char(12) NOT NULL,
    [seed] int NOT NULL,
    [quality_mode] nvarchar(16) NOT NULL,
    [record_count] int NOT NULL,
    [period_days] int NOT NULL,
    [target_mode] nvarchar(32) NOT NULL,
    [started_at] datetime2(7) NOT NULL DEFAULT SYSUTCDATETIME(),
    [completed_at] datetime2(7) NULL DEFAULT SYSUTCDATETIME(),
    [status] nvarchar(16) NOT NULL,
    [error_summary] nvarchar(512) NULL,
    CONSTRAINT [PK_lh_demo_ctl_demo_runs] PRIMARY KEY ([run_id])
);

IF OBJECT_ID(N'lh_demo_ctl.batch_watermarks', N'U') IS NULL
CREATE TABLE [lh_demo_ctl].[batch_watermarks] (
    [source_table] nvarchar(64) NOT NULL,
    [last_watermark] datetime2(7) NULL,
    [last_primary_key] nvarchar(160) NULL,
    [snapshot_id] varchar(64) NULL,
    [updated_at] datetime2(7) NOT NULL DEFAULT SYSUTCDATETIME(),
    CONSTRAINT [PK_lh_demo_ctl_batch_watermarks] PRIMARY KEY ([source_table])
);

IF OBJECT_ID(N'lh_demo_erp.products', N'U') IS NULL
CREATE TABLE [lh_demo_erp].[products] (
    [product_id] nvarchar(160) NOT NULL,
    [product_name] nvarchar(256) NULL,
    [product_family] nvarchar(128) NULL,
    [unit] nvarchar(32) NULL,
    [standard_cycle_seconds] decimal(18,3) NULL,
    [updated_at] datetime2(7) NOT NULL DEFAULT SYSUTCDATETIME(),
    [created_at] datetime2(7) NOT NULL DEFAULT SYSUTCDATETIME(),
    [run_id] char(12) NOT NULL,
    [original_values] nvarchar(max) NOT NULL,
    CONSTRAINT [PK_lh_demo_erp_products] PRIMARY KEY ([product_id]),
    CONSTRAINT [CK_lh_demo_erp_products_original_json] CHECK (ISJSON(original_values)=1)
);

IF OBJECT_ID(N'lh_demo_erp.work_orders', N'U') IS NULL
CREATE TABLE [lh_demo_erp].[work_orders] (
    [work_order_id] nvarchar(160) NOT NULL,
    [factory_id] nvarchar(64) NULL,
    [product_id] nvarchar(160) NULL,
    [planned_date] date NULL,
    [planned_qty] bigint NULL,
    [updated_at] datetime2(7) NOT NULL DEFAULT SYSUTCDATETIME(),
    [created_at] datetime2(7) NOT NULL DEFAULT SYSUTCDATETIME(),
    [run_id] char(12) NOT NULL,
    [original_values] nvarchar(max) NOT NULL,
    CONSTRAINT [PK_lh_demo_erp_work_orders] PRIMARY KEY ([work_order_id]),
    CONSTRAINT [CK_lh_demo_erp_work_orders_original_json] CHECK (ISJSON(original_values)=1)
);

IF OBJECT_ID(N'lh_demo_erp.sales_orders', N'U') IS NULL
CREATE TABLE [lh_demo_erp].[sales_orders] (
    [sales_order_id] nvarchar(160) NOT NULL,
    [factory_id] nvarchar(64) NULL,
    [product_id] nvarchar(160) NULL,
    [order_date] date NULL,
    [promised_date] date NULL,
    [ordered_qty] bigint NULL,
    [unit_price] decimal(18,2) NULL,
    [updated_at] datetime2(7) NOT NULL DEFAULT SYSUTCDATETIME(),
    [created_at] datetime2(7) NOT NULL DEFAULT SYSUTCDATETIME(),
    [run_id] char(12) NOT NULL,
    [original_values] nvarchar(max) NOT NULL,
    CONSTRAINT [PK_lh_demo_erp_sales_orders] PRIMARY KEY ([sales_order_id]),
    CONSTRAINT [CK_lh_demo_erp_sales_orders_original_json] CHECK (ISJSON(original_values)=1)
);

IF OBJECT_ID(N'lh_demo_erp.shipments', N'U') IS NULL
CREATE TABLE [lh_demo_erp].[shipments] (
    [shipment_id] nvarchar(160) NOT NULL,
    [sales_order_id] nvarchar(160) NULL,
    [shipped_at] datetime2(7) NULL,
    [shipped_qty] bigint NULL,
    [updated_at] datetime2(7) NOT NULL DEFAULT SYSUTCDATETIME(),
    [created_at] datetime2(7) NOT NULL DEFAULT SYSUTCDATETIME(),
    [run_id] char(12) NOT NULL,
    [original_values] nvarchar(max) NOT NULL,
    CONSTRAINT [PK_lh_demo_erp_shipments] PRIMARY KEY ([shipment_id]),
    CONSTRAINT [CK_lh_demo_erp_shipments_original_json] CHECK (ISJSON(original_values)=1)
);

IF OBJECT_ID(N'lh_demo_mes.events', N'U') IS NULL
CREATE TABLE [lh_demo_mes].[events] (
    [event_id] nvarchar(160) NOT NULL,
    [work_order_id] nvarchar(160) NULL,
    [event_time] datetime2(7) NULL,
    [good_qty] bigint NULL,
    [reject_qty] bigint NULL,
    [updated_at] datetime2(7) NOT NULL DEFAULT SYSUTCDATETIME(),
    [created_at] datetime2(7) NOT NULL DEFAULT SYSUTCDATETIME(),
    [run_id] char(12) NOT NULL,
    [original_values] nvarchar(max) NOT NULL,
    CONSTRAINT [PK_lh_demo_mes_events] PRIMARY KEY ([event_id]),
    CONSTRAINT [CK_lh_demo_mes_events_original_json] CHECK (ISJSON(original_values)=1)
);

IF OBJECT_ID(N'lh_demo_mes.downtime', N'U') IS NULL
CREATE TABLE [lh_demo_mes].[downtime] (
    [downtime_id] nvarchar(160) NOT NULL,
    [work_order_id] nvarchar(160) NULL,
    [start_time] datetime2(7) NULL,
    [end_time] datetime2(7) NULL,
    [reason_code] nvarchar(64) NULL,
    [updated_at] datetime2(7) NOT NULL DEFAULT SYSUTCDATETIME(),
    [created_at] datetime2(7) NOT NULL DEFAULT SYSUTCDATETIME(),
    [run_id] char(12) NOT NULL,
    [original_values] nvarchar(max) NOT NULL,
    CONSTRAINT [PK_lh_demo_mes_downtime] PRIMARY KEY ([downtime_id]),
    CONSTRAINT [CK_lh_demo_mes_downtime_original_json] CHECK (ISJSON(original_values)=1)
);

-- Validate every existing column before publishing the ownership marker.
-- A same-named but differently typed table is rejected, not silently adopted.
DECLARE @Expected TABLE (
    schema_name sysname, table_name sysname, column_name sysname,
    type_name sysname, max_length smallint, precision_value tinyint,
    scale_value tinyint, nullable_value bit, primary_key_value bit
);
INSERT INTO @Expected VALUES
    (N'lh_demo_ctl',N'installation',N'singleton_id',N'tinyint',1,3,0,0,1),
    (N'lh_demo_ctl',N'installation',N'installation_id',N'char',32,0,0,0,0),
    (N'lh_demo_ctl',N'installation',N'ownership_hash',N'char',64,0,0,0,0),
    (N'lh_demo_ctl',N'installation',N'schema_version',N'int',4,10,0,0,0),
    (N'lh_demo_ctl',N'installation',N'installed_at',N'datetime2',8,27,7,0,0),
    (N'lh_demo_ctl',N'installation',N'installed_by',N'nvarchar',256,0,0,0,0),
    (N'lh_demo_ctl',N'installation',N'repository_version',N'nvarchar',256,0,0,0,0),
    (N'lh_demo_ctl',N'demo_runs',N'run_id',N'char',12,0,0,0,1),
    (N'lh_demo_ctl',N'demo_runs',N'seed',N'int',4,10,0,0,0),
    (N'lh_demo_ctl',N'demo_runs',N'quality_mode',N'nvarchar',32,0,0,0,0),
    (N'lh_demo_ctl',N'demo_runs',N'record_count',N'int',4,10,0,0,0),
    (N'lh_demo_ctl',N'demo_runs',N'period_days',N'int',4,10,0,0,0),
    (N'lh_demo_ctl',N'demo_runs',N'target_mode',N'nvarchar',64,0,0,0,0),
    (N'lh_demo_ctl',N'demo_runs',N'started_at',N'datetime2',8,27,7,0,0),
    (N'lh_demo_ctl',N'demo_runs',N'completed_at',N'datetime2',8,27,7,1,0),
    (N'lh_demo_ctl',N'demo_runs',N'status',N'nvarchar',32,0,0,0,0),
    (N'lh_demo_ctl',N'demo_runs',N'error_summary',N'nvarchar',1024,0,0,1,0),
    (N'lh_demo_ctl',N'batch_watermarks',N'source_table',N'nvarchar',128,0,0,0,1),
    (N'lh_demo_ctl',N'batch_watermarks',N'last_watermark',N'datetime2',8,27,7,1,0),
    (N'lh_demo_ctl',N'batch_watermarks',N'last_primary_key',N'nvarchar',320,0,0,1,0),
    (N'lh_demo_ctl',N'batch_watermarks',N'snapshot_id',N'varchar',64,0,0,1,0),
    (N'lh_demo_ctl',N'batch_watermarks',N'updated_at',N'datetime2',8,27,7,0,0),
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
-- Materialize actual metadata separately so the comparison is inspectable.
DECLARE @Actual TABLE (
    schema_name sysname, table_name sysname, column_name sysname,
    type_name sysname, max_length smallint, precision_value tinyint,
    scale_value tinyint, nullable_value bit, primary_key_value bit
);
INSERT INTO @Actual
SELECT s.name, t.name, c.name, ty.name, c.max_length, c.[precision], c.[scale],
    c.is_nullable, CONVERT(bit, CASE WHEN pk.column_id IS NULL THEN 0 ELSE 1 END)
FROM sys.tables t JOIN sys.schemas s ON t.schema_id=s.schema_id
JOIN sys.columns c ON t.object_id=c.object_id
JOIN sys.types ty ON c.user_type_id=ty.user_type_id
LEFT JOIN (
    SELECT ic.object_id, ic.column_id
    FROM sys.index_columns ic JOIN sys.indexes i
      ON ic.object_id=i.object_id AND ic.index_id=i.index_id
    WHERE i.is_primary_key=1
) pk ON pk.object_id=t.object_id AND pk.column_id=c.column_id
WHERE s.name IN (N'lh_demo_erp',N'lh_demo_mes',N'lh_demo_ctl');
-- EXCEPT in both directions detects absent, additional and changed columns,
-- including type/size/precision/scale/nullability/primary-key membership.
IF EXISTS (SELECT * FROM @Expected EXCEPT SELECT * FROM @Actual)
    OR EXISTS (SELECT * FROM @Actual EXCEPT SELECT * FROM @Expected)
    THROW 51008, 'Existing fixed-table contract mismatch; transaction refused.', 1;
IF NOT EXISTS (SELECT 1 FROM [lh_demo_ctl].[installation] WHERE singleton_id=1)
    INSERT INTO [lh_demo_ctl].[installation]
        (singleton_id,installation_id,ownership_hash,schema_version,repository_version)
    VALUES (1,@InstallationId,@OwnershipHash,1,@RepositoryVersion);
SELECT N'fixed_demo_tables' AS check_name, COUNT_BIG(*) AS business_table_count
FROM sys.tables t JOIN sys.schemas s ON t.schema_id=s.schema_id
WHERE s.name IN (N'lh_demo_erp',N'lh_demo_mes');
COMMIT TRANSACTION;

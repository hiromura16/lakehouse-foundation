-- DBA read-only CDC inventory. Keep the result private; never commit it.
-- Fixed synthetic source only. Run manually as the DBA with sqlcmd -b.
-- Select the database using the client connection, then provide these vars:
-- TargetDatabase, InstallationId (32 lower hex), OwnershipHash (SHA-256 hex).
-- Values are supplied privately. No runtime app receives administrator auth.
-- No existing dbo/legacy object, login, password, Swarm or deployment is changed.
-- Reject an inherited transaction before changing options or starting our own.
IF @@TRANCOUNT<>0
BEGIN
    RAISERROR('Use a dedicated DBA session without an outer transaction.',16,1);
    RETURN;
END;
SET IMPLICIT_TRANSACTIONS OFF;
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
    THROW 51011, 'The fixed demo installation has not been prepared.', 1;
IF OBJECT_ID(N'cdc.change_tables',N'U') IS NOT NULL
    EXEC sys.sp_executesql N'
        SELECT ct.capture_instance,ct.source_object_id,src.is_tracked_by_cdc,
            ct.has_drop_pending,ct.start_lsn,ct.role_name,ct.index_name,
            ct.supports_net_changes,
            CASE WHEN src.object_id IS NOT NULL AND src.is_tracked_by_cdc=1
                AND (ct.has_drop_pending IS NULL OR ct.has_drop_pending=0)
                AND changed.object_id IS NOT NULL
                THEN N''healthy'' ELSE N''review_required'' END AS capture_health
        FROM cdc.change_tables ct
        LEFT JOIN sys.tables src ON src.object_id=ct.source_object_id
        LEFT JOIN sys.tables changed ON changed.object_id=ct.object_id;';
IF EXISTS (SELECT 1 FROM sys.databases
    WHERE database_id=DB_ID() AND is_cdc_enabled=1)
    AND OBJECT_ID(N'msdb.dbo.cdc_jobs',N'U') IS NOT NULL
    EXEC sys.sp_cdc_help_jobs;
COMMIT TRANSACTION;

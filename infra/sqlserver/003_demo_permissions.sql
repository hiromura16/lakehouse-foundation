-- Fixed synthetic source only. Run manually as the DBA with sqlcmd -b.
-- Select the database using the client connection, then provide these vars:
-- TargetDatabase, InstallationId (32 lower hex), OwnershipHash (SHA-256 hex).
-- Values are supplied privately. No runtime app receives administrator auth.
-- No existing dbo/legacy object, login, password, Swarm or deployment is changed.
-- Refuse a caller-owned transaction before any setting or THROW. RAISERROR
-- deliberately does not honor XACT_ABORT, including an inherited ON setting.
IF @@TRANCOUNT<>0
BEGIN
    RAISERROR('Use a dedicated DBA session without an outer transaction.',16,1);
    RETURN;
END;
SET IMPLICIT_TRANSACTIONS OFF;
SET ANSI_NULLS ON;
SET QUOTED_IDENTIFIER ON;
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
-- A dedicated session avoids rolling back a caller's unrelated transaction.
-- Runtime errors caught below are rolled back, then observed again. Compile
-- errors, client cancellation and disconnects still require separate preflight.
DECLARE @CheckerCreatedThisAttempt bit=0;
DECLARE @CheckerExistedBefore bit=CASE
    WHEN OBJECT_ID(N'lh_demo_ctl.check_write_safety_v1') IS NULL THEN 0 ELSE 1 END;
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
-- Existing database users only. No CREATE LOGIN/USER or password changes.
-- Extra vars: DemoWriterUser, SourceReaderUser, ControlWriterUser.
-- A distinct control principal keeps checkpoint writes out of SOURCE auth.
IF OBJECT_ID(N'lh_demo_ctl.installation',N'U') IS NULL
    THROW 51009, 'Apply the schema/table installation first.', 1;
-- This fixed no-argument checker reads metadata as dbo, never business data.
-- Do not adopt/overwrite any existing module, even a same-version mismatch.
DECLARE @SafetyDefinition nvarchar(max) = N'CREATE PROCEDURE [lh_demo_ctl].[check_write_safety_v1]
WITH EXECUTE AS ''dbo''
AS
BEGIN
    SET NOCOUNT ON;
    SELECT CAST(CASE WHEN USER_ID()=DATABASE_PRINCIPAL_ID(N''dbo'')
        AND (SELECT COUNT_BIG(*) FROM sys.tables t
             JOIN sys.schemas s ON t.schema_id=s.schema_id
             WHERE s.name+N''.''+t.name IN (
                 N''lh_demo_erp.products'',N''lh_demo_erp.work_orders'',
                 N''lh_demo_erp.sales_orders'',N''lh_demo_erp.shipments'',
                 N''lh_demo_mes.events'',N''lh_demo_mes.downtime'',
                 N''lh_demo_ctl.installation'',N''lh_demo_ctl.demo_runs'',
                 N''lh_demo_ctl.batch_watermarks''))=9
        AND NOT EXISTS (
            SELECT 1 FROM sys.triggers tr
            JOIN sys.objects o ON tr.parent_id=o.object_id
            JOIN sys.schemas s ON o.schema_id=s.schema_id
            WHERE tr.is_disabled=0
              AND s.name IN (N''lh_demo_erp'',N''lh_demo_mes'',N''lh_demo_ctl''))
        AND NOT EXISTS (
            SELECT 1 FROM sys.foreign_keys fk
            JOIN sys.objects p ON fk.parent_object_id=p.object_id
            JOIN sys.schemas ps ON p.schema_id=ps.schema_id
            JOIN sys.objects r ON fk.referenced_object_id=r.object_id
            JOIN sys.schemas rs ON r.schema_id=rs.schema_id
            WHERE ps.name IN (N''lh_demo_erp'',N''lh_demo_mes'',N''lh_demo_ctl'')
               OR rs.name IN (N''lh_demo_erp'',N''lh_demo_mes'',N''lh_demo_ctl''))
        THEN 1 ELSE 0 END AS bit) AS safe;
END';
IF SCHEMA_ID(N'lh_demo_ctl') IS NULL OR NOT EXISTS (
    SELECT 1 FROM sys.schemas WHERE name=N'lh_demo_ctl'
      AND principal_id=DATABASE_PRINCIPAL_ID(N'dbo')
)
    THROW 51018, 'Safety checker schema must be owned by dbo.', 1;
IF OBJECT_ID(N'lh_demo_ctl.check_write_safety_v1') IS NULL
BEGIN
    EXEC sys.sp_executesql @SafetyDefinition;
    SET @CheckerCreatedThisAttempt=1;
END;
IF NOT EXISTS (
    SELECT 1 FROM sys.sql_modules m JOIN sys.objects o
    ON m.object_id=o.object_id JOIN sys.schemas s ON o.schema_id=s.schema_id
    WHERE o.object_id=OBJECT_ID(N'lh_demo_ctl.check_write_safety_v1',N'P')
      AND o.is_ms_shipped=0
      AND COALESCE(o.principal_id,s.principal_id)=DATABASE_PRINCIPAL_ID(N'dbo')
      AND m.execute_as_principal_id=DATABASE_PRINCIPAL_ID(N'dbo')
      AND m.uses_ansi_nulls=1 AND m.uses_quoted_identifier=1
      AND DATALENGTH(m.[definition])=DATALENGTH(@SafetyDefinition)
      AND m.[definition] COLLATE Latin1_General_100_BIN2
          = @SafetyDefinition COLLATE Latin1_General_100_BIN2
)
    THROW 51019, 'Safety checker identity/definition mismatch; preserve it.', 1;
DECLARE @Writer nvarchar(4000)=N'$(DemoWriterUser)';
DECLARE @Reader nvarchar(4000)=N'$(SourceReaderUser)';
DECLARE @Control nvarchar(4000)=N'$(ControlWriterUser)';
IF LEN(@Writer) NOT BETWEEN 1 AND 128
    OR LEN(@Reader) NOT BETWEEN 1 AND 128
    OR LEN(@Control) NOT BETWEEN 1 AND 128
    THROW 51018, 'Existing principal names must fit SQL Server identifiers.', 1;
IF DATABASE_PRINCIPAL_ID(@Writer) IS NULL
    OR DATABASE_PRINCIPAL_ID(@Reader) IS NULL
    OR DATABASE_PRINCIPAL_ID(@Control) IS NULL
    OR DATABASE_PRINCIPAL_ID(@Writer)=DATABASE_PRINCIPAL_ID(@Reader)
    OR DATABASE_PRINCIPAL_ID(@Writer)=DATABASE_PRINCIPAL_ID(@Control)
    OR DATABASE_PRINCIPAL_ID(@Reader)=DATABASE_PRINCIPAL_ID(@Control)
    THROW 51010, 'Provide three distinct existing database users.', 1;
-- Resolve catalog IDs under their native rules, then reject case aliases.
-- Do not silently attach grants to a differently spelled existing identity.
IF EXISTS (
    SELECT 1 FROM (VALUES (@Writer),(@Reader),(@Control)) requested(user_name)
    JOIN sys.database_principals actual
      ON actual.principal_id=DATABASE_PRINCIPAL_ID(requested.user_name)
    WHERE actual.name COLLATE Latin1_General_100_BIN2
          <> requested.user_name COLLATE Latin1_General_100_BIN2
)
    THROW 51021, 'Use the exact existing database principal spelling.', 1;
IF EXISTS (
    SELECT 1 FROM sys.database_principals r
    WHERE r.principal_id IN (
        DATABASE_PRINCIPAL_ID(N'lh_demo_writer'),
        DATABASE_PRINCIPAL_ID(N'lh_demo_reader'),
        DATABASE_PRINCIPAL_ID(N'lh_demo_control_writer'))
      AND r.name COLLATE Latin1_General_100_BIN2 NOT IN (
        N'lh_demo_writer',N'lh_demo_reader',N'lh_demo_control_writer')
)
    THROW 51022, 'A fixed role resolves to a case alias; preserve it.', 1;
-- These roles are fixed and owned by dbo. Existing grants/members are not
-- removed. Runtime diagnosis rejects legacy broad roles or external grants.
IF DATABASE_PRINCIPAL_ID(N'lh_demo_writer') IS NULL
    CREATE ROLE [lh_demo_writer] AUTHORIZATION [dbo];
IF DATABASE_PRINCIPAL_ID(N'lh_demo_reader') IS NULL
    CREATE ROLE [lh_demo_reader] AUTHORIZATION [dbo];
IF DATABASE_PRINCIPAL_ID(N'lh_demo_control_writer') IS NULL
    CREATE ROLE [lh_demo_control_writer] AUTHORIZATION [dbo];
-- Refuse to extend a same-named role with unrelated grants or members.
-- No old permission/role is revoked to force the application checks to pass.
DECLARE @AllowedGrants TABLE (role_name sysname, object_name nvarchar(257), permission_name sysname);
INSERT INTO @AllowedGrants VALUES
    (N'lh_demo_writer',N'lh_demo_ctl.check_write_safety_v1',N'EXECUTE'),
    (N'lh_demo_writer',N'lh_demo_ctl.check_write_safety_v1',N'VIEW DEFINITION'),
    (N'lh_demo_control_writer',N'lh_demo_ctl.check_write_safety_v1',N'EXECUTE'),
    (N'lh_demo_control_writer',N'lh_demo_ctl.check_write_safety_v1',N'VIEW DEFINITION'),
    (N'lh_demo_writer',N'lh_demo_erp.products',N'VIEW DEFINITION'),
    (N'lh_demo_writer',N'lh_demo_erp.work_orders',N'VIEW DEFINITION'),
    (N'lh_demo_writer',N'lh_demo_erp.sales_orders',N'VIEW DEFINITION'),
    (N'lh_demo_writer',N'lh_demo_erp.shipments',N'VIEW DEFINITION'),
    (N'lh_demo_writer',N'lh_demo_mes.events',N'VIEW DEFINITION'),
    (N'lh_demo_writer',N'lh_demo_mes.downtime',N'VIEW DEFINITION'),
    (N'lh_demo_writer',N'lh_demo_ctl.demo_runs',N'VIEW DEFINITION'),
    (N'lh_demo_control_writer',N'lh_demo_ctl.batch_watermarks',N'VIEW DEFINITION'),
    (N'lh_demo_writer',N'lh_demo_erp.products',N'SELECT'),
    (N'lh_demo_writer',N'lh_demo_erp.products',N'INSERT'),
    (N'lh_demo_writer',N'lh_demo_erp.products',N'UPDATE'),
    (N'lh_demo_writer',N'lh_demo_erp.products',N'DELETE'),
    (N'lh_demo_reader',N'lh_demo_erp.products',N'SELECT'),
    (N'lh_demo_writer',N'lh_demo_erp.work_orders',N'SELECT'),
    (N'lh_demo_writer',N'lh_demo_erp.work_orders',N'INSERT'),
    (N'lh_demo_writer',N'lh_demo_erp.work_orders',N'UPDATE'),
    (N'lh_demo_writer',N'lh_demo_erp.work_orders',N'DELETE'),
    (N'lh_demo_reader',N'lh_demo_erp.work_orders',N'SELECT'),
    (N'lh_demo_writer',N'lh_demo_erp.sales_orders',N'SELECT'),
    (N'lh_demo_writer',N'lh_demo_erp.sales_orders',N'INSERT'),
    (N'lh_demo_writer',N'lh_demo_erp.sales_orders',N'UPDATE'),
    (N'lh_demo_writer',N'lh_demo_erp.sales_orders',N'DELETE'),
    (N'lh_demo_reader',N'lh_demo_erp.sales_orders',N'SELECT'),
    (N'lh_demo_writer',N'lh_demo_erp.shipments',N'SELECT'),
    (N'lh_demo_writer',N'lh_demo_erp.shipments',N'INSERT'),
    (N'lh_demo_writer',N'lh_demo_erp.shipments',N'UPDATE'),
    (N'lh_demo_writer',N'lh_demo_erp.shipments',N'DELETE'),
    (N'lh_demo_reader',N'lh_demo_erp.shipments',N'SELECT'),
    (N'lh_demo_writer',N'lh_demo_mes.events',N'SELECT'),
    (N'lh_demo_writer',N'lh_demo_mes.events',N'INSERT'),
    (N'lh_demo_writer',N'lh_demo_mes.events',N'UPDATE'),
    (N'lh_demo_writer',N'lh_demo_mes.events',N'DELETE'),
    (N'lh_demo_reader',N'lh_demo_mes.events',N'SELECT'),
    (N'lh_demo_writer',N'lh_demo_mes.downtime',N'SELECT'),
    (N'lh_demo_writer',N'lh_demo_mes.downtime',N'INSERT'),
    (N'lh_demo_writer',N'lh_demo_mes.downtime',N'UPDATE'),
    (N'lh_demo_writer',N'lh_demo_mes.downtime',N'DELETE'),
    (N'lh_demo_reader',N'lh_demo_mes.downtime',N'SELECT'),
    (N'lh_demo_writer',N'lh_demo_ctl.installation',N'SELECT'),
    (N'lh_demo_reader',N'lh_demo_ctl.installation',N'SELECT'),
    (N'lh_demo_control_writer',N'lh_demo_ctl.installation',N'SELECT'),
    (N'lh_demo_writer',N'lh_demo_ctl.demo_runs',N'SELECT'),
    (N'lh_demo_writer',N'lh_demo_ctl.demo_runs',N'INSERT'),
    (N'lh_demo_writer',N'lh_demo_ctl.demo_runs',N'UPDATE'),
    (N'lh_demo_control_writer',N'lh_demo_ctl.batch_watermarks',N'SELECT'),
    (N'lh_demo_control_writer',N'lh_demo_ctl.batch_watermarks',N'INSERT'),
    (N'lh_demo_control_writer',N'lh_demo_ctl.batch_watermarks',N'UPDATE');
IF EXISTS (
    SELECT 1 FROM sys.database_principals r
    WHERE r.name IN (N'lh_demo_writer',N'lh_demo_reader',N'lh_demo_control_writer')
      AND (r.type<>'R' OR r.owning_principal_id IS NULL
           OR r.owning_principal_id<>DATABASE_PRINCIPAL_ID(N'dbo'))
)
    THROW 51012, 'A same-named role has an unrelated owner or kind.', 1;
IF EXISTS (
    SELECT 1 FROM sys.database_permissions p
    JOIN sys.database_principals r ON p.grantee_principal_id=r.principal_id
    WHERE r.name IN (N'lh_demo_writer',N'lh_demo_reader',N'lh_demo_control_writer')
      AND (p.class<>1 OR p.minor_id<>0 OR p.state<>'G' OR NOT EXISTS (
        SELECT 1 FROM @AllowedGrants a
        -- Both columns are implicit-collation expressions from different
        -- sources. Fix the comparison, never server/database collation.
        WHERE a.role_name COLLATE Latin1_General_100_BIN2
              = r.name COLLATE Latin1_General_100_BIN2
          AND a.permission_name COLLATE Latin1_General_100_BIN2
              = p.permission_name COLLATE Latin1_General_100_BIN2
          AND OBJECT_ID(a.object_name)=p.major_id
      ))
)
    THROW 51013, 'An existing role has unexpected permissions; preserve it.', 1;
IF EXISTS (
    SELECT 1 FROM sys.database_role_members m
    JOIN sys.database_principals r ON m.member_principal_id=r.principal_id
    WHERE r.name IN (N'lh_demo_writer',N'lh_demo_reader',N'lh_demo_control_writer')
)
    THROW 51014, 'A fixed role inherits another role; manual review required.', 1;
IF EXISTS (
    SELECT 1 FROM sys.database_role_members m
    JOIN sys.database_principals r ON m.role_principal_id=r.principal_id
    WHERE (r.name=N'lh_demo_writer' AND m.member_principal_id<>DATABASE_PRINCIPAL_ID(@Writer))
       OR (r.name=N'lh_demo_reader' AND m.member_principal_id<>DATABASE_PRINCIPAL_ID(@Reader))
       OR (r.name=N'lh_demo_control_writer' AND m.member_principal_id<>DATABASE_PRINCIPAL_ID(@Control))
)
    THROW 51015, 'A fixed role has other members; manual review required.', 1;
GRANT SELECT, INSERT, UPDATE, DELETE ON OBJECT::[lh_demo_erp].[products] TO [lh_demo_writer];
GRANT SELECT ON OBJECT::[lh_demo_erp].[products] TO [lh_demo_reader];
GRANT SELECT, INSERT, UPDATE, DELETE ON OBJECT::[lh_demo_erp].[work_orders] TO [lh_demo_writer];
GRANT SELECT ON OBJECT::[lh_demo_erp].[work_orders] TO [lh_demo_reader];
GRANT SELECT, INSERT, UPDATE, DELETE ON OBJECT::[lh_demo_erp].[sales_orders] TO [lh_demo_writer];
GRANT SELECT ON OBJECT::[lh_demo_erp].[sales_orders] TO [lh_demo_reader];
GRANT SELECT, INSERT, UPDATE, DELETE ON OBJECT::[lh_demo_erp].[shipments] TO [lh_demo_writer];
GRANT SELECT ON OBJECT::[lh_demo_erp].[shipments] TO [lh_demo_reader];
GRANT SELECT, INSERT, UPDATE, DELETE ON OBJECT::[lh_demo_mes].[events] TO [lh_demo_writer];
GRANT SELECT ON OBJECT::[lh_demo_mes].[events] TO [lh_demo_reader];
GRANT SELECT, INSERT, UPDATE, DELETE ON OBJECT::[lh_demo_mes].[downtime] TO [lh_demo_writer];
GRANT SELECT ON OBJECT::[lh_demo_mes].[downtime] TO [lh_demo_reader];
GRANT SELECT ON OBJECT::[lh_demo_ctl].[installation] TO [lh_demo_writer];
GRANT SELECT ON OBJECT::[lh_demo_ctl].[installation] TO [lh_demo_reader];
GRANT SELECT ON OBJECT::[lh_demo_ctl].[installation] TO [lh_demo_control_writer];
GRANT SELECT, INSERT, UPDATE ON OBJECT::[lh_demo_ctl].[demo_runs] TO [lh_demo_writer];
GRANT SELECT, INSERT, UPDATE ON OBJECT::[lh_demo_ctl].[batch_watermarks] TO [lh_demo_control_writer];
-- Only this exact read-only dbo checker may be executed by runtime writers.
GRANT EXECUTE, VIEW DEFINITION ON OBJECT::[lh_demo_ctl].[check_write_safety_v1] TO [lh_demo_writer];
GRANT EXECUTE, VIEW DEFINITION ON OBJECT::[lh_demo_ctl].[check_write_safety_v1] TO [lh_demo_control_writer];
-- Local metadata visibility is retained for diagnostics, not completeness.
GRANT VIEW DEFINITION ON OBJECT::[lh_demo_erp].[products] TO [lh_demo_writer];
GRANT VIEW DEFINITION ON OBJECT::[lh_demo_erp].[work_orders] TO [lh_demo_writer];
GRANT VIEW DEFINITION ON OBJECT::[lh_demo_erp].[sales_orders] TO [lh_demo_writer];
GRANT VIEW DEFINITION ON OBJECT::[lh_demo_erp].[shipments] TO [lh_demo_writer];
GRANT VIEW DEFINITION ON OBJECT::[lh_demo_mes].[events] TO [lh_demo_writer];
GRANT VIEW DEFINITION ON OBJECT::[lh_demo_mes].[downtime] TO [lh_demo_writer];
GRANT VIEW DEFINITION ON OBJECT::[lh_demo_ctl].[demo_runs] TO [lh_demo_writer];
GRANT VIEW DEFINITION ON OBJECT::[lh_demo_ctl].[batch_watermarks] TO [lh_demo_control_writer];
-- Identifiers from trusted DBA input are always quoted before dynamic DDL.
DECLARE @RoleCommand nvarchar(max);
IF NOT EXISTS (SELECT 1 FROM sys.database_role_members
    WHERE role_principal_id=DATABASE_PRINCIPAL_ID(N'lh_demo_writer')
      AND member_principal_id=DATABASE_PRINCIPAL_ID(@Writer))
BEGIN
    SET @RoleCommand=N'ALTER ROLE [lh_demo_writer] ADD MEMBER '+QUOTENAME(@Writer);
    EXEC sys.sp_executesql @RoleCommand;
END;
IF NOT EXISTS (SELECT 1 FROM sys.database_role_members
    WHERE role_principal_id=DATABASE_PRINCIPAL_ID(N'lh_demo_reader')
      AND member_principal_id=DATABASE_PRINCIPAL_ID(@Reader))
BEGIN
    SET @RoleCommand=N'ALTER ROLE [lh_demo_reader] ADD MEMBER '+QUOTENAME(@Reader);
    EXEC sys.sp_executesql @RoleCommand;
END;
IF NOT EXISTS (SELECT 1 FROM sys.database_role_members
    WHERE role_principal_id=DATABASE_PRINCIPAL_ID(N'lh_demo_control_writer')
      AND member_principal_id=DATABASE_PRINCIPAL_ID(@Control))
BEGIN
    SET @RoleCommand=N'ALTER ROLE [lh_demo_control_writer] ADD MEMBER '+QUOTENAME(@Control);
    EXEC sys.sp_executesql @RoleCommand;
END;
SELECT N'fixed_demo_roles' AS check_name, COUNT_BIG(*) AS role_count
FROM sys.database_principals WHERE name IN
    (N'lh_demo_writer',N'lh_demo_reader',N'lh_demo_control_writer');
COMMIT TRANSACTION;
END TRY
BEGIN CATCH
    -- This is best-effort observation, not proof that every error rolled back.
    -- Never drop/update an existing checker or echo definition/private values.
    IF XACT_STATE()<>0 ROLLBACK TRANSACTION;
    BEGIN TRY
        SELECT N'fixed_demo_permissions_failed' AS check_name,
            ERROR_NUMBER() AS error_number,
            @CheckerExistedBefore AS checker_existed_before,
            @CheckerCreatedThisAttempt AS checker_created_this_attempt,
            CONVERT(bit,CASE WHEN OBJECT_ID(
                N'lh_demo_ctl.check_write_safety_v1') IS NULL
                THEN 0 ELSE 1 END) AS checker_present_after_failure,
            (SELECT COUNT_BIG(*) FROM sys.database_principals
             WHERE name IN (N'lh_demo_writer',N'lh_demo_reader',
                            N'lh_demo_control_writer')) AS fixed_role_count,
            (SELECT COUNT_BIG(*) FROM sys.database_permissions p
             WHERE p.grantee_principal_id IN (
                DATABASE_PRINCIPAL_ID(N'lh_demo_writer'),
                DATABASE_PRINCIPAL_ID(N'lh_demo_reader'),
                DATABASE_PRINCIPAL_ID(N'lh_demo_control_writer')))
                AS fixed_role_permission_rows,
            N'Run the read-only permissions preflight; preserve mismatches.'
                AS next_action;
    END TRY
    BEGIN CATCH
        SELECT N'fixed_demo_permissions_diagnostic_unavailable' AS check_name;
    END CATCH;
    -- Preserve the original error even if diagnostic metadata was unavailable.
    THROW;
END CATCH;

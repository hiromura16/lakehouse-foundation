-- Read-only DBA diagnostic after an interrupted/failed 003. Never an API.
-- Same private SQLCMD variables as 003; no DBA password belongs in that file.
-- No CREATE/ALTER/DROP/GRANT/REVOKE or business-table write is performed.
-- Output is fixed object names, booleans and counts, never a module body,
-- installation identity/hash, actual principal names, or connection details.
-- Refuse a caller-owned transaction before any setting or THROW. RAISERROR
-- deliberately does not honor XACT_ABORT, including an inherited ON setting.
IF @@TRANCOUNT<>0
BEGIN
    RAISERROR('Use a dedicated DBA session without an outer transaction.',16,1);
    RETURN;
END;
SET IMPLICIT_TRANSACTIONS OFF;
SET NOCOUNT ON;
DECLARE @ExpectedDatabase nvarchar(4000)=N'$(TargetDatabase)';
DECLARE @InstallationId nvarchar(4000)=N'$(InstallationId)';
DECLARE @OwnershipHash nvarchar(4000)=N'$(OwnershipHash)';
DECLARE @Writer nvarchar(4000)=N'$(DemoWriterUser)';
DECLARE @Reader nvarchar(4000)=N'$(SourceReaderUser)';
DECLARE @Control nvarchar(4000)=N'$(ControlWriterUser)';
IF LEN(@ExpectedDatabase) NOT BETWEEN 1 AND 128
    OR @ExpectedDatabase COLLATE Latin1_General_100_BIN2 LIKE N'%[^A-Za-z0-9_]%'
    OR LEFT(@ExpectedDatabase,1) COLLATE Latin1_General_100_BIN2 LIKE N'[0-9]'
    OR LOWER(@ExpectedDatabase) IN (N'master',N'model',N'msdb',N'tempdb')
    OR DB_NAME() COLLATE Latin1_General_100_BIN2
       <> @ExpectedDatabase COLLATE Latin1_General_100_BIN2
    THROW 51200, 'Select the explicitly approved non-system database.', 1;
IF LEN(@InstallationId)<>32
    OR @InstallationId COLLATE Latin1_General_100_BIN2 LIKE N'%[^0-9a-f]%'
    OR LEN(@OwnershipHash)<>64
    OR @OwnershipHash COLLATE Latin1_General_100_BIN2 LIKE N'%[^0-9a-f]%'
    THROW 51201, 'Supply the private installation identity and hash.', 1;
IF COALESCE(IS_MEMBER(N'db_owner'),0)<>1
    AND COALESCE(IS_SRVROLEMEMBER(N'sysadmin'),0)<>1
    AND USER_ID()<>DATABASE_PRINCIPAL_ID(N'dbo')
    THROW 51202, 'Use a separately authorized DBA metadata session.', 1;
IF OBJECT_ID(N'lh_demo_ctl.installation',N'U') IS NULL
    OR COL_LENGTH(N'lh_demo_ctl.installation',N'installation_id') IS NULL
    OR COL_LENGTH(N'lh_demo_ctl.installation',N'ownership_hash') IS NULL
    OR COL_LENGTH(N'lh_demo_ctl.installation',N'schema_version') IS NULL
    OR COL_LENGTH(N'lh_demo_ctl.installation',N'singleton_id') IS NULL
    THROW 51204, 'Installation marker shape is unavailable; preserve state.', 1;
DECLARE @ActualId char(32),@ActualHash char(64),@Version int;
EXEC sys.sp_executesql
    N'SELECT @Id=installation_id,@Hash=ownership_hash,@Version=schema_version
      FROM lh_demo_ctl.installation WHERE singleton_id=1',
    N'@Id char(32) OUTPUT,@Hash char(64) OUTPUT,@Version int OUTPUT',
    @Id=@ActualId OUTPUT,@Hash=@ActualHash OUTPUT,@Version=@Version OUTPUT;
IF @ActualId IS NULL OR @ActualHash IS NULL OR COALESCE(@Version,0)<>1
    OR @ActualId COLLATE Latin1_General_100_BIN2
       <> @InstallationId COLLATE Latin1_General_100_BIN2
    OR @ActualHash COLLATE Latin1_General_100_BIN2
       <> @OwnershipHash COLLATE Latin1_General_100_BIN2
    THROW 51205, 'Installation ownership/version mismatch; preserve state.', 1;
-- Pinned verbatim to scripts/sqlserver_safety.sql and 003; no normalization.
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
-- LEFT JOIN returns a mismatch row even for a wrong-kind same-name object.
SELECT N'fixed_demo_checker_preflight' AS check_name,
    CONVERT(bit,CASE WHEN o.object_id IS NULL THEN 0 ELSE 1 END) AS checker_present,
    CONVERT(bit,CASE WHEN o.type=N'P' AND o.is_ms_shipped=0
        THEN 1 ELSE 0 END) AS expected_kind,
    CONVERT(bit,CASE WHEN s.principal_id=DATABASE_PRINCIPAL_ID(N'dbo')
        THEN 1 ELSE 0 END) AS schema_owner_dbo,
    CONVERT(bit,CASE WHEN COALESCE(o.principal_id,s.principal_id)
        =DATABASE_PRINCIPAL_ID(N'dbo') THEN 1 ELSE 0 END) AS effective_owner_dbo,
    CONVERT(bit,CASE WHEN m.execute_as_principal_id=DATABASE_PRINCIPAL_ID(N'dbo')
        THEN 1 ELSE 0 END) AS execute_as_dbo,
    CONVERT(bit,COALESCE(m.uses_ansi_nulls,0)) AS ansi_nulls_on,
    CONVERT(bit,COALESCE(m.uses_quoted_identifier,0)) AS quoted_identifier_on,
    CONVERT(bit,CASE WHEN DATALENGTH(m.[definition])=DATALENGTH(@SafetyDefinition)
        AND m.[definition] COLLATE Latin1_General_100_BIN2
            = @SafetyDefinition COLLATE Latin1_General_100_BIN2
        THEN 1 ELSE 0 END) AS exact_definition_match,
    N'v1' AS expected_checker_version,
    N'Installation marker verified; no per-attempt persisted origin proof.'
        AS ownership_evidence,
    N'Observation only; preserve mismatches for separate DBA review.'
        AS next_action
FROM (VALUES(1)) singleton(n)
LEFT JOIN sys.objects o
    ON o.object_id=OBJECT_ID(N'lh_demo_ctl.check_write_safety_v1')
LEFT JOIN sys.schemas s ON s.schema_id=SCHEMA_ID(N'lh_demo_ctl')
LEFT JOIN sys.sql_modules m ON m.object_id=o.object_id;
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
-- Query by resolved ID, then expose exact spelling as a boolean. Never
-- hide case aliases by filtering them out with an exact-name JOIN.
SELECT N'fixed_demo_role_preflight' AS check_name, expected.role_name,
    CONVERT(bit,CASE WHEN r.principal_id IS NULL THEN 0 ELSE 1 END) AS role_present,
    CONVERT(bit,CASE WHEN r.name COLLATE Latin1_General_100_BIN2
        = expected.role_name COLLATE Latin1_General_100_BIN2
        THEN 1 ELSE 0 END) AS exact_role_name,
    CONVERT(bit,CASE WHEN requested_user.principal_id IS NULL
        THEN 0 ELSE 1 END) AS user_present,
    CONVERT(bit,CASE WHEN requested_user.name COLLATE Latin1_General_100_BIN2
        = expected.user_name COLLATE Latin1_General_100_BIN2
        THEN 1 ELSE 0 END) AS exact_user_name,
    CONVERT(bit,CASE WHEN r.type=N'R'
        AND r.owning_principal_id=DATABASE_PRINCIPAL_ID(N'dbo')
        THEN 1 ELSE 0 END) AS expected_kind_owner,
    (SELECT COUNT_BIG(*) FROM sys.database_role_members members
     WHERE members.role_principal_id=r.principal_id
       AND members.member_principal_id=DATABASE_PRINCIPAL_ID(expected.user_name))
        AS expected_memberships,
    (SELECT COUNT_BIG(*) FROM sys.database_role_members members
     WHERE members.role_principal_id=r.principal_id
       AND (DATABASE_PRINCIPAL_ID(expected.user_name) IS NULL
            OR members.member_principal_id<>DATABASE_PRINCIPAL_ID(expected.user_name)))
        AS other_memberships,
    (SELECT COUNT_BIG(*) FROM sys.database_role_members inherited
     WHERE inherited.member_principal_id=r.principal_id)
        AS inherited_role_memberships,
    (SELECT COUNT_BIG(*) FROM sys.database_permissions p
     WHERE p.grantee_principal_id=r.principal_id) AS permission_rows,
    (SELECT COUNT_BIG(*) FROM sys.database_permissions p
     WHERE p.grantee_principal_id=r.principal_id
       AND (p.class<>1 OR p.minor_id<>0 OR p.state<>N'G' OR NOT EXISTS (
         SELECT 1 FROM @AllowedGrants a
         WHERE a.role_name COLLATE Latin1_General_100_BIN2
               = r.name COLLATE Latin1_General_100_BIN2
           AND a.permission_name COLLATE Latin1_General_100_BIN2
               = p.permission_name COLLATE Latin1_General_100_BIN2
           AND OBJECT_ID(a.object_name)=p.major_id))) AS unexpected_permissions
FROM (VALUES
    (N'lh_demo_writer',@Writer),
    (N'lh_demo_reader',@Reader),
    (N'lh_demo_control_writer',@Control)
) expected(role_name,user_name)
LEFT JOIN sys.database_principals r
    ON r.principal_id=DATABASE_PRINCIPAL_ID(expected.role_name)
LEFT JOIN sys.database_principals requested_user
    ON requested_user.principal_id=DATABASE_PRINCIPAL_ID(expected.user_name);
-- These counts describe observation now, not changes made by a previous run.
SELECT N'fixed_demo_permissions_preflight_only' AS check_name,
    CONVERT(bit,1) AS installation_matches,
    CONVERT(bit,0) AS mutations_performed;

-- Optional DBA-only provisioning. Never execute against an unreviewed database.
-- This is not part of 001–006, and does not grant rights to legacy data.
-- Review and set the three private SQLCMD values using the existing DBA process.
SET NOCOUNT ON;
SET ANSI_NULLS ON;
SET QUOTED_IDENTIFIER ON;
SET XACT_ABORT ON;
IF @@TRANCOUNT<>0 OR XACT_STATE()<>0
    THROW 51801, 'Start in a new DBA session with no external transaction.', 1;
IF USER_ID()<>DATABASE_PRINCIPAL_ID(N'dbo')
    THROW 51802, 'The database owner must provision the optional policy.', 1;
IF DB_NAME() COLLATE Latin1_General_100_BIN2<>
    N'$(TargetDatabase)' COLLATE Latin1_General_100_BIN2
    THROW 51803, 'The selected database does not match.', 1;
IF NOT EXISTS (
    SELECT 1 FROM lh_demo_ctl.installation
    WHERE singleton_id=1 AND schema_version=1
      AND installation_id COLLATE Latin1_General_100_BIN2=N'$(InstallationId)'
      AND ownership_hash COLLATE Latin1_General_100_BIN2=N'$(OwnershipHash)'
)
    THROW 51804, 'The fixed installation marker does not match.', 1;
IF NOT EXISTS (
    SELECT 1 FROM sys.schemas WHERE name COLLATE Latin1_General_100_BIN2=
        N'lh_demo_ctl' AND principal_id=DATABASE_PRINCIPAL_ID(N'dbo')
)
    THROW 51805, 'The control schema is not owned by dbo.', 1;
IF NOT EXISTS (
    SELECT 1 FROM sys.database_principals
    WHERE name COLLATE Latin1_General_100_BIN2=N'lh_demo_writer'
      AND type='R' AND owning_principal_id=DATABASE_PRINCIPAL_ID(N'dbo')
)
    THROW 51806, 'The canonical writer role is missing or untrusted.', 1;
IF OBJECT_ID(N'lh_demo_ctl.writer_legacy_policy_v2') IS NOT NULL
    OR OBJECT_ID(N'lh_demo_ctl.check_writer_legacy_delegation_v2') IS NOT NULL
    THROW 51807, 'An optional policy object already exists; never adopt it.', 1;

BEGIN TRY
    BEGIN TRANSACTION;
    CREATE TABLE [lh_demo_ctl].[writer_legacy_policy_v2] (
        installation_id char(32) COLLATE Latin1_General_100_BIN2 NOT NULL,
        writer_sid varbinary(85) NOT NULL,
        writer_principal_id int NOT NULL,
        object_id int NOT NULL,
        schema_id int NOT NULL,
        schema_name sysname COLLATE Latin1_General_100_BIN2 NOT NULL,
        table_name sysname COLLATE Latin1_General_100_BIN2 NOT NULL,
        object_created_at datetime NOT NULL,
        operation varchar(6) COLLATE Latin1_General_100_BIN2 NOT NULL,
        PRIMARY KEY (installation_id,writer_sid,object_id,operation),
        CHECK (operation IN ('SELECT','INSERT','UPDATE','DELETE')),
        CHECK (writer_principal_id>4 AND object_id>0 AND schema_id>0)
    );
    DECLARE @Definition nvarchar(max)=N'CREATE PROCEDURE [lh_demo_ctl].[check_writer_legacy_delegation_v2]
WITH EXECUTE AS ''dbo''
AS
BEGIN
    SET NOCOUNT ON;
    DECLARE @policy int=OBJECT_ID(N''lh_demo_ctl.writer_legacy_policy_v2'',N''U'');
    DECLARE @control int=SCHEMA_ID(N''lh_demo_ctl'');
    DECLARE @actor int;
    SELECT @actor=principal_id FROM sys.database_principals
        WHERE type=''S'' AND authentication_type=1 AND principal_id>4
          AND sid=SUSER_SID(ORIGINAL_LOGIN());
    ;WITH membership AS (
        SELECT principal_id FROM sys.database_principals
            WHERE principal_id IN (@actor,DATABASE_PRINCIPAL_ID(N''public''))
        UNION ALL
        SELECT r.role_principal_id
        FROM sys.database_role_members r
        JOIN membership m ON r.member_principal_id=m.principal_id
    )
    SELECT CAST(CASE WHEN
        USER_ID()=DATABASE_PRINCIPAL_ID(N''dbo'')
        AND @actor IS NOT NULL AND @policy IS NOT NULL
        AND NOT EXISTS (
            SELECT 1 FROM sys.security_predicates
            WHERE target_object_id=@policy
        )
        AND NOT EXISTS (
            SELECT 1 FROM sys.database_role_members r
            JOIN sys.database_principals rdef
                 ON rdef.principal_id=r.role_principal_id
            WHERE rdef.name IN (
                N''db_datawriter'',N''db_ddladmin'',N''db_securityadmin'',
                N''db_accessadmin''
            ) AND r.member_principal_id<>DATABASE_PRINCIPAL_ID(N''dbo'')
        )
        AND NOT EXISTS (
            SELECT 1 FROM sys.database_permissions p
            WHERE p.state IN (N''G'',N''W'')
              AND p.grantee_principal_id<>DATABASE_PRINCIPAL_ID(N''dbo'')
              AND (
                (
                    (p.class=1 AND p.major_id=@policy)
                    OR (p.class=3 AND p.major_id=@control)
                    OR p.class=0
                ) AND p.permission_name IN (
                    N''INSERT'',N''UPDATE'',N''DELETE'',N''CONTROL'',N''ALTER'',
                    N''TAKE OWNERSHIP'',N''ALTER ANY SCHEMA'',
                    N''ALTER ANY SECURITY POLICY'',N''ALTER ANY USER'',
                    N''ALTER ANY ROLE'',N''IMPERSONATE ANY USER''
                )
                OR (
                    p.class=4 AND p.major_id=DATABASE_PRINCIPAL_ID(N''dbo'')
                    AND p.permission_name IN (N''IMPERSONATE'',N''CONTROL'')
                )
              )
        )
        AND NOT EXISTS (
            SELECT 1 FROM sys.database_permissions p
            JOIN membership m ON p.grantee_principal_id=m.principal_id
            WHERE p.state=N''W''
        )
        AND NOT EXISTS (
            SELECT 1
            FROM sys.database_permissions p
            JOIN membership m ON p.grantee_principal_id=m.principal_id
            JOIN [lh_demo_ctl].[writer_legacy_policy_v2] lp
                 ON p.major_id=lp.object_id
                 AND p.permission_name COLLATE Latin1_General_100_BIN2
                     =lp.operation
            WHERE p.class=1 AND p.minor_id>0 AND p.state IN (N''G'',N''W'')
              AND lp.writer_sid=SUSER_SID(ORIGINAL_LOGIN())
              AND lp.writer_principal_id=@actor
        )
        THEN 1 ELSE 0 END AS bit) AS safe
    OPTION (MAXRECURSION 16);
END';
    EXEC sys.sp_executesql @Definition;
    GRANT SELECT ON OBJECT::[lh_demo_ctl].[writer_legacy_policy_v2]
        TO [lh_demo_writer];
    GRANT EXECUTE, VIEW DEFINITION
        ON OBJECT::[lh_demo_ctl].[check_writer_legacy_delegation_v2]
        TO [lh_demo_writer];
    COMMIT;
END TRY
BEGIN CATCH
    IF XACT_STATE()<>0 ROLLBACK;
    THROW;
END CATCH;

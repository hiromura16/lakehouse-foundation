CREATE PROCEDURE [lh_demo_ctl].[check_writer_legacy_delegation_v2]
WITH EXECUTE AS 'dbo'
AS
BEGIN
    SET NOCOUNT ON;
    DECLARE @policy int=OBJECT_ID(N'lh_demo_ctl.writer_legacy_policy_v2',N'U');
    DECLARE @control int=SCHEMA_ID(N'lh_demo_ctl');
    DECLARE @actor int;
    SELECT @actor=principal_id FROM sys.database_principals
        WHERE type='S' AND authentication_type=1 AND principal_id>4
          AND sid=SUSER_SID(ORIGINAL_LOGIN());
    ;WITH membership AS (
        SELECT principal_id FROM sys.database_principals
            WHERE principal_id IN (@actor,DATABASE_PRINCIPAL_ID(N'public'))
        UNION ALL
        SELECT r.role_principal_id
        FROM sys.database_role_members r
        JOIN membership m ON r.member_principal_id=m.principal_id
    )
    SELECT CAST(CASE WHEN
        USER_ID()=DATABASE_PRINCIPAL_ID(N'dbo')
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
                N'db_datawriter',N'db_ddladmin',N'db_securityadmin',
                N'db_accessadmin'
            ) AND r.member_principal_id<>DATABASE_PRINCIPAL_ID(N'dbo')
        )
        AND NOT EXISTS (
            SELECT 1 FROM sys.database_permissions p
            WHERE p.state IN (N'G',N'W')
              AND p.grantee_principal_id<>DATABASE_PRINCIPAL_ID(N'dbo')
              AND (
                (
                    (p.class=1 AND p.major_id=@policy)
                    OR (p.class=3 AND p.major_id=@control)
                    OR p.class=0
                ) AND p.permission_name IN (
                    N'INSERT',N'UPDATE',N'DELETE',N'CONTROL',N'ALTER',
                    N'TAKE OWNERSHIP',N'ALTER ANY SCHEMA',
                    N'ALTER ANY SECURITY POLICY',N'ALTER ANY USER',
                    N'ALTER ANY ROLE',N'IMPERSONATE ANY USER'
                )
                OR (
                    p.class=4 AND p.major_id=DATABASE_PRINCIPAL_ID(N'dbo')
                    AND p.permission_name IN (N'IMPERSONATE',N'CONTROL')
                )
              )
        )
        AND NOT EXISTS (
            SELECT 1 FROM sys.database_permissions p
            JOIN membership m ON p.grantee_principal_id=m.principal_id
            WHERE p.state=N'W'
        )
        AND NOT EXISTS (
            SELECT 1
            FROM sys.database_permissions p
            JOIN membership m ON p.grantee_principal_id=m.principal_id
            JOIN [lh_demo_ctl].[writer_legacy_policy_v2] lp
                 ON p.major_id=lp.object_id
                 AND p.permission_name COLLATE Latin1_General_100_BIN2
                     =lp.operation
            WHERE p.class=1 AND p.minor_id>0 AND p.state IN (N'G',N'W')
              AND lp.writer_sid=SUSER_SID(ORIGINAL_LOGIN())
              AND lp.writer_principal_id=@actor
        )
        THEN 1 ELSE 0 END AS bit) AS safe
    OPTION (MAXRECURSION 16);
END

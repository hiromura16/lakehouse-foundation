-- Synthetic approximation: force two different implicit column collations.
-- This does not assert that the selected server's real catalog uses either.
SET NOCOUNT ON;
DECLARE @Observed468 bit=0;
BEGIN TRY
    EXEC sys.sp_executesql N'
        DECLARE @Catalog TABLE (
            role_name nvarchar(128) COLLATE Latin1_General_CI_AS_KS_WS);
        DECLARE @Allowed TABLE (
            role_name nvarchar(128) COLLATE Latin1_General_CI_AS);
        INSERT @Catalog VALUES (N''fixture_role'');
        INSERT @Allowed VALUES (N''fixture_role'');
        SELECT COUNT_BIG(*) FROM @Catalog c JOIN @Allowed a
            ON c.role_name=a.role_name;';
END TRY
BEGIN CATCH
    IF ERROR_NUMBER()<>468 THROW;
    SET @Observed468=1;
END CATCH;
IF @Observed468<>1
    THROW 51980, 'The deliberately conflicting comparison did not fail.', 1;
DECLARE @Catalog TABLE (
    role_name nvarchar(128) COLLATE Latin1_General_CI_AS_KS_WS);
DECLARE @Allowed TABLE (
    role_name nvarchar(128) COLLATE Latin1_General_CI_AS);
INSERT @Catalog VALUES (N'fixture_role');
INSERT @Allowed VALUES (N'fixture_role');
IF (SELECT COUNT_BIG(*) FROM @Catalog c JOIN @Allowed a
    ON c.role_name COLLATE Latin1_General_100_BIN2
       =a.role_name COLLATE Latin1_General_100_BIN2)<>1
    THROW 51981, 'The bilateral binary comparison did not match.', 1;
UPDATE @Allowed SET role_name=N'Fixture_Role';
IF EXISTS (SELECT 1 FROM @Catalog c JOIN @Allowed a
    ON c.role_name COLLATE Latin1_General_100_BIN2
       =a.role_name COLLATE Latin1_General_100_BIN2)
    THROW 51982, 'The binary comparison accepted a case mismatch.', 1;
-- Case folding is used ONLY to detect/reject a conflicting ASCII name.
-- It never authorizes a folded identity as an exact capture/role match.
IF NOT EXISTS (SELECT 1 FROM @Catalog c JOIN @Allowed a
    ON UPPER(c.role_name COLLATE Latin1_General_100_BIN2)
           COLLATE Latin1_General_100_BIN2
       =UPPER(a.role_name COLLATE Latin1_General_100_BIN2)
           COLLATE Latin1_General_100_BIN2
    WHERE c.role_name COLLATE Latin1_General_100_BIN2
        <>a.role_name COLLATE Latin1_General_100_BIN2)
    THROW 51983, 'The reject-only case-alias check did not detect conflict.', 1;
PRINT N'LAKEHOUSE_SQL_FIXTURE={"stage":"mixed_collation","status":"passed","observed_468":true,"forced_column_collations":true,"case_alias_detected":true}';

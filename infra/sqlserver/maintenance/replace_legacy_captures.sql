-- DBA-only, explicitly approved replacement. Never included by normal 004.
-- Render with prepare-sqlserver.py --legacy-allowlist; do not run this template
-- directly. All four exact schema/table/capture triples are private inputs.
-- Reserve a window without other CDC/DDL/job administrators. Use sqlcmd -b.
IF @@TRANCOUNT <> 0
BEGIN
    -- Restore caller options when this nested refusal returns.
    EXEC sys.sp_executesql N'SET XACT_ABORT OFF;
        RAISERROR(''Use a dedicated DBA session without an outer transaction.'',
                  16,1);';
    RETURN;
END;
SET NOCOUNT ON;
SET XACT_ABORT ON;
SET IMPLICIT_TRANSACTIONS OFF;
DECLARE @LegacyLock bit=0,@LegacyLockResult int,@LegacyAttempted bit=0;
DECLARE @LegacyInventoryReady bit=0;
DECLARE @LegacyPhase nvarchar(40)=N'preflight';
DECLARE @LegacyEnableSql nvarchar(max)=N'$(FixedEnableSql)';
DECLARE @LegacyApproved TABLE (
    ordinal int PRIMARY KEY, source_schema sysname, source_table sysname,
    capture_instance sysname COLLATE Latin1_General_100_BIN2 UNIQUE,
    was_present bit NOT NULL DEFAULT 0, source_object_id int NULL
);
INSERT INTO @LegacyApproved (ordinal,source_schema,source_table,capture_instance)
VALUES
    (1,N'$(LegacySchema1)',N'$(LegacyTable1)',N'$(LegacyCapture1)'),
    (2,N'$(LegacySchema2)',N'$(LegacyTable2)',N'$(LegacyCapture2)'),
    (3,N'$(LegacySchema3)',N'$(LegacyTable3)',N'$(LegacyCapture3)'),
    (4,N'$(LegacySchema4)',N'$(LegacyTable4)',N'$(LegacyCapture4)');
DECLARE @LegacyFixed TABLE (
    capture_instance sysname COLLATE Latin1_General_100_BIN2 PRIMARY KEY
);
INSERT INTO @LegacyFixed VALUES
    (N'lh_demo_erp_products_v1'),(N'lh_demo_erp_work_orders_v1'),
    (N'lh_demo_erp_sales_orders_v1'),(N'lh_demo_erp_shipments_v1'),
    (N'lh_demo_mes_events_v1'),(N'lh_demo_mes_downtime_v1');
BEGIN TRY
    EXEC @LegacyLockResult=sys.sp_getapplock
        @Resource=N'lakehouse.fixed-demo.cdc',@LockMode=N'Exclusive',
        @LockOwner=N'Session',@LockTimeout=10000;
    IF @LegacyLockResult<0
        THROW 51200, 'Another fixed CDC operation is active.', 1;
    SET @LegacyLock=1;
    IF DB_NAME() COLLATE Latin1_General_100_BIN2
        <> N'$(TargetDatabase)' COLLATE Latin1_General_100_BIN2
        OR NOT EXISTS (SELECT 1 FROM sys.databases
            WHERE database_id=DB_ID() AND is_cdc_enabled=1)
        OR OBJECT_ID(N'cdc.change_tables',N'U') IS NULL
        THROW 51201, 'Select the approved CDC-enabled database.', 1;
    IF (SELECT COUNT(*) FROM @LegacyApproved)<>4
        OR EXISTS (SELECT 1 FROM @LegacyApproved
            WHERE UPPER(capture_instance COLLATE Latin1_General_100_BIN2)
                =N'ALL' COLLATE Latin1_General_100_BIN2)
        OR EXISTS (SELECT 1 FROM @LegacyApproved a JOIN @LegacyFixed f
            ON a.capture_instance COLLATE Latin1_General_100_BIN2
               =f.capture_instance COLLATE Latin1_General_100_BIN2)
        THROW 51202, 'Configure exactly four distinct approved legacy captures.', 1;
    UPDATE a SET source_object_id=OBJECT_ID(
        QUOTENAME(source_schema)+N'.'+QUOTENAME(source_table),N'U')
    FROM @LegacyApproved a;
    IF EXISTS (SELECT 1 FROM @LegacyApproved a
        WHERE a.source_object_id IS NULL
           OR OBJECT_SCHEMA_NAME(a.source_object_id) COLLATE Latin1_General_100_BIN2
              <>a.source_schema COLLATE Latin1_General_100_BIN2
           OR OBJECT_NAME(a.source_object_id) COLLATE Latin1_General_100_BIN2
              <>a.source_table COLLATE Latin1_General_100_BIN2)
        THROW 51203, 'Approved legacy source identity differs; no changes.', 1;
    -- No wildcard/prefix discovery. Unknown captures block before role/grant
    -- or capture mutations. Missing approved entries permit autocommit retry.
    IF EXISTS (SELECT 1 FROM cdc.change_tables ct
        WHERE NOT EXISTS (SELECT 1 FROM @LegacyFixed f
            WHERE f.capture_instance COLLATE Latin1_General_100_BIN2
                =ct.capture_instance COLLATE Latin1_General_100_BIN2)
          AND NOT EXISTS (SELECT 1 FROM @LegacyApproved a
            WHERE a.capture_instance COLLATE Latin1_General_100_BIN2
                =ct.capture_instance COLLATE Latin1_General_100_BIN2))
        THROW 51204, 'Unknown capture exists; preserve all state for review.', 1;
    IF EXISTS (SELECT 1 FROM cdc.change_tables ct JOIN @LegacyApproved a
        ON ct.capture_instance COLLATE Latin1_General_100_BIN2
            =a.capture_instance COLLATE Latin1_General_100_BIN2
        WHERE ct.source_object_id<>a.source_object_id)
        THROW 51205, 'Approved legacy capture/source mapping differs.', 1;
    UPDATE a SET was_present=1 FROM @LegacyApproved a JOIN cdc.change_tables ct
        ON ct.capture_instance COLLATE Latin1_General_100_BIN2
            =a.capture_instance COLLATE Latin1_General_100_BIN2;
    SET @LegacyInventoryReady=1;
    SELECT N'legacy_replacement_inventory' AS check_name,a.ordinal,
        a.was_present AS approved_legacy_present,
        ct.has_drop_pending,src.is_tracked_by_cdc
    FROM @LegacyApproved a LEFT JOIN cdc.change_tables ct
        ON ct.capture_instance COLLATE Latin1_General_100_BIN2
            =a.capture_instance COLLATE Latin1_General_100_BIN2
    LEFT JOIN sys.tables src ON src.object_id=ct.source_object_id;
    -- The reviewed 004 validates installation ownership, reader, every existing
    -- fixed definition, all capture health and shared jobs before any mutation.
    -- Its session lock is reentrant; this outer lock spans the entire operation.
    SET @LegacyPhase=N'fixed_enable_and_verify';
    EXEC sys.sp_executesql @LegacyEnableSql;
    IF (SELECT COUNT(*) FROM cdc.change_tables ct JOIN @LegacyFixed f
        ON ct.capture_instance COLLATE Latin1_General_100_BIN2
            =f.capture_instance COLLATE Latin1_General_100_BIN2)<>6
        THROW 51206, 'All six fixed captures must exist before legacy removal.', 1;
    DECLARE @LegacyJobsBefore varbinary(32)=HASHBYTES('SHA2_256',
        CONVERT(nvarchar(max),(SELECT job_id,job_type,maxtrans,maxscans,
            continuous,pollinginterval,retention,threshold
            FROM msdb.dbo.cdc_jobs WHERE database_id=DB_ID()
            ORDER BY job_type FOR JSON PATH,INCLUDE_NULL_VALUES)));
    SET @LegacyPhase=N'approved_legacy_disable';
    DECLARE @LegacySchema sysname,@LegacyTable sysname,@LegacyCapture sysname,
        @LegacyResult int;
    DECLARE ApprovedLegacyTargets CURSOR LOCAL FAST_FORWARD FOR
        SELECT source_schema,source_table,capture_instance FROM @LegacyApproved
        ORDER BY ordinal;
    OPEN ApprovedLegacyTargets;
    FETCH NEXT FROM ApprovedLegacyTargets
        INTO @LegacySchema,@LegacyTable,@LegacyCapture;
    WHILE @@FETCH_STATUS=0
    BEGIN
        IF EXISTS (SELECT 1 FROM cdc.change_tables
            WHERE capture_instance COLLATE Latin1_General_100_BIN2
                =@LegacyCapture COLLATE Latin1_General_100_BIN2)
        BEGIN
            SET @LegacyAttempted=1;
            EXEC @LegacyResult=sys.sp_cdc_disable_table
                @source_schema=@LegacySchema,@source_name=@LegacyTable,
                @capture_instance=@LegacyCapture;
            IF @LegacyResult<>0 OR EXISTS (SELECT 1 FROM cdc.change_tables
                WHERE capture_instance COLLATE Latin1_General_100_BIN2
                    =@LegacyCapture COLLATE Latin1_General_100_BIN2)
                THROW 51207, 'Approved legacy removal incomplete; inspect state.', 1;
        END;
        FETCH NEXT FROM ApprovedLegacyTargets
            INTO @LegacySchema,@LegacyTable,@LegacyCapture;
    END;
    CLOSE ApprovedLegacyTargets;
    DEALLOCATE ApprovedLegacyTargets;
    SET @LegacyPhase=N'final_verify';
    EXEC sys.sp_executesql @LegacyEnableSql;
    IF @LegacyJobsBefore<>HASHBYTES('SHA2_256',CONVERT(nvarchar(max),(
        SELECT job_id,job_type,maxtrans,maxscans,continuous,pollinginterval,
            retention,threshold FROM msdb.dbo.cdc_jobs WHERE database_id=DB_ID()
        ORDER BY job_type FOR JSON PATH,INCLUDE_NULL_VALUES)))
        THROW 51209, 'Shared CDC jobs changed during replacement; review.', 1;
    IF EXISTS (SELECT 1 FROM cdc.change_tables ct JOIN @LegacyApproved a
        ON ct.capture_instance COLLATE Latin1_General_100_BIN2
            =a.capture_instance COLLATE Latin1_General_100_BIN2)
        THROW 51208, 'Approved legacy captures remain; inspect private state.', 1;
    IF EXISTS (SELECT 1 FROM cdc.change_tables ct
        WHERE NOT EXISTS (SELECT 1 FROM @LegacyFixed f
            WHERE f.capture_instance COLLATE Latin1_General_100_BIN2
                =ct.capture_instance COLLATE Latin1_General_100_BIN2))
        THROW 51210, 'Unexpected capture appeared; preserve state and review.', 1;
    SELECT N'legacy_replacement_complete' AS check_name,
        6 AS fixed_capture_count,0 AS approved_legacy_remaining;
    EXEC sys.sp_releaseapplock @Resource=N'lakehouse.fixed-demo.cdc',
        @LockOwner=N'Session';
    SET @LegacyLock=0;
END TRY
BEGIN CATCH
    IF XACT_STATE()<>0 ROLLBACK TRANSACTION;
    DECLARE @LegacyError int=ERROR_NUMBER();
    BEGIN TRY
        -- Ordinals only: private approved names and server errors stay private.
        SELECT N'legacy_replacement_failure_state' AS check_name,
            @LegacyError AS original_error_number,@LegacyPhase AS phase,
            @LegacyAttempted AS legacy_mutation_attempted,a.ordinal,
            CASE WHEN @LegacyInventoryReady=0 THEN NULL
                 ELSE a.was_present END AS preflight_presence,
            CONVERT(bit,CASE WHEN ct.object_id IS NULL THEN 0 ELSE 1 END)
                AS current_presence,
            N'preserve_state_review_then_rerun_same_allowlist' AS next_action
        FROM @LegacyApproved a LEFT JOIN cdc.change_tables ct
            ON ct.capture_instance COLLATE Latin1_General_100_BIN2
                =a.capture_instance COLLATE Latin1_General_100_BIN2;
    END TRY
    BEGIN CATCH
        SELECT N'legacy_replacement_diagnostic_unavailable' AS check_name,
            @LegacyError AS original_error_number;
    END CATCH;
    IF CURSOR_STATUS('local','ApprovedLegacyTargets')>=0 CLOSE ApprovedLegacyTargets;
    IF CURSOR_STATUS('local','ApprovedLegacyTargets')>=-1 DEALLOCATE ApprovedLegacyTargets;
    IF @LegacyLock=1
        EXEC sys.sp_releaseapplock @Resource=N'lakehouse.fixed-demo.cdc',
            @LockOwner=N'Session';
    THROW;
END CATCH;

CREATE PROCEDURE [lh_demo_ctl].[check_write_safety_v1]
WITH EXECUTE AS 'dbo'
AS
BEGIN
    SET NOCOUNT ON;
    SELECT CAST(CASE WHEN USER_ID()=DATABASE_PRINCIPAL_ID(N'dbo')
        AND (SELECT COUNT_BIG(*) FROM sys.tables t
             JOIN sys.schemas s ON t.schema_id=s.schema_id
             WHERE s.name+N'.'+t.name IN (
                 N'lh_demo_erp.products',N'lh_demo_erp.work_orders',
                 N'lh_demo_erp.sales_orders',N'lh_demo_erp.shipments',
                 N'lh_demo_mes.events',N'lh_demo_mes.downtime',
                 N'lh_demo_ctl.installation',N'lh_demo_ctl.demo_runs',
                 N'lh_demo_ctl.batch_watermarks'))=9
        AND NOT EXISTS (
            SELECT 1 FROM sys.triggers tr
            JOIN sys.objects o ON tr.parent_id=o.object_id
            JOIN sys.schemas s ON o.schema_id=s.schema_id
            WHERE tr.is_disabled=0
              AND s.name IN (N'lh_demo_erp',N'lh_demo_mes',N'lh_demo_ctl'))
        AND NOT EXISTS (
            SELECT 1 FROM sys.foreign_keys fk
            JOIN sys.objects p ON fk.parent_object_id=p.object_id
            JOIN sys.schemas ps ON p.schema_id=ps.schema_id
            JOIN sys.objects r ON fk.referenced_object_id=r.object_id
            JOIN sys.schemas rs ON r.schema_id=rs.schema_id
            WHERE ps.name IN (N'lh_demo_erp',N'lh_demo_mes',N'lh_demo_ctl')
               OR rs.name IN (N'lh_demo_erp',N'lh_demo_mes',N'lh_demo_ctl'))
        THEN 1 ELSE 0 END AS bit) AS safe;
END

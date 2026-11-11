-- Owned disposable fixture only; Runner supplies the database marker guard.
-- Use the supported CDC procedure, never edit cdc.change_tables directly.
-- A CI catalog may resolve this alias to the existing fixed role. A CS
-- catalog may create a distinct role. Read back both identities afterwards;
-- do not assume that CDC preserves the supplied spelling in its metadata.
CREATE TABLE dbo.fixture_role_alias_data (id int NOT NULL PRIMARY KEY);
EXEC sys.sp_cdc_enable_table
    @source_schema=N'dbo',@source_name=N'fixture_role_alias_data',
    @capture_instance=N'fixture_role_alias_v1',
    @role_name=N'LH_DEMO_CDC_READER',@supports_net_changes=0,
    @allow_partition_switch=0;

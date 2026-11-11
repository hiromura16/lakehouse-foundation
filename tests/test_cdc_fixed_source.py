"""Test fixed CDC contracts with synthetic metadata, not a SQL/Kafka server.

These are unit and static image/Compose contracts. They do not establish live
CDC capture, driver interoperability, SQL Server Agent health or deployment.
"""

import contextlib
import copy
import importlib.util
import json
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import yaml

# Skip only an absent optional package; keep broken installs as errors.
try:
    import pyarrow as pa
except ModuleNotFoundError as error:
    if (
        error.name != "pyarrow"
        or importlib.util.find_spec("pyarrow") is not None
    ):
        raise
    raise unittest.SkipTest(
        "Optional tests need pyarrow. In a uv virtual environment, run "
        "uv pip sync --require-hashes requirements-dev.lock."
    ) from error

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "infra/cdc"))
import config  # noqa: E402
import connector  # noqa: E402
import consumer  # noqa: E402
import fixed_source  # noqa: E402

VALUES = {
    "CDC_SOURCE_MODE": "fixed-demo",
    "SOURCE_SQLSERVER_HOST": "sql-source.example.invalid",
    "SOURCE_SQLSERVER_PORT": "1433",
    "SOURCE_SQLSERVER_DATABASE": "demo_database",
    "SOURCE_SQLSERVER_USER": "example_reader",
    "SOURCE_SQLSERVER_PASSWORD": "synthetic-reader-value",
    "SOURCE_SQLSERVER_ENCRYPT": "true",
    "SOURCE_SQLSERVER_TRUST_SERVER_CERTIFICATE": "false",
    "SOURCE_SQLSERVER_TIMEZONE": "UTC",
    "DEMO_SQLSERVER_INSTALLATION_ID": "a" * 32,
    "DEMO_SQLSERVER_OWNERSHIP_TOKEN": "b" * 64,
}


class MetadataCursor:
    """Respond only to the fixed metadata SELECT protocol."""

    def __init__(self, adapter, fault=None):
        self.adapter = adapter
        self.fault = fault
        self.statements = []
        self.pending = []

    def execute(self, statement, arguments=None):
        """Keep statements for the no-DML assertion and inject safe faults."""
        self.statements.append((statement, arguments))
        if self.fault == "driver":
            raise RuntimeError("synthetic-private-driver-value")
        if "is_cdc_enabled" in statement:
            self.pending = [(0 if self.fault == "database" else 1,)]
        elif "ct.capture_instance,ct.role_name" in statement:
            table = next(
                name
                for name, item in self.adapter.TABLES.items()
                if item["schema"] + "." + item["table"] == arguments[0]
            )
            capture = self.adapter.CAPTURE_INSTANCES[table]
            self.pending = [(capture, "lh_demo_cdc_reader")]
            if self.fault == "capture":
                self.pending.append(("unrelated_capture", "legacy_reader"))
        elif "cc.column_name" in statement:
            table = next(
                name
                for name, capture in self.adapter.CAPTURE_INSTANCES.items()
                if capture == arguments[0]
            )
            columns = [
                *self.adapter.TABLES[table]["columns"],
                *fixed_source.EXTRA_COLUMNS,
            ]
            if self.fault == "columns":
                columns.remove("original_values")
            self.pending = [(name,) for name in columns]
        elif "c.name,ty.name,c.scale" in statement:
            self.pending = [
                (name, "datetime2", 6 if self.fault == "precision" else 7)
                for name in {"updated_at", "created_at"}
                | self.adapter.TIME_COLUMNS
            ]
        elif "IS_MEMBER" in statement:
            self.pending = [(0 if self.fault == "role" else 1,)]
        else:
            raise AssertionError("Unexpected preflight SQL.")

    def fetchone(self):
        """Return one metadata row or SQL NULL absence."""
        return self.pending.pop(0) if self.pending else None

    def fetchall(self):
        """Return a bounded metadata result, never application data."""
        rows, self.pending = self.pending, []
        return rows


def metadata_adapter(fault=None):
    """Use the real fixed contract with an injected read-only connection."""
    real = fixed_source.source_adapter()
    adapter = SimpleNamespace(
        TABLES=real.TABLES,
        CAPTURE_INSTANCES=real.CAPTURE_INSTANCES,
        TIME_COLUMNS=real.TIME_COLUMNS,
        validate_settings=real.validate_settings,
        ownership=mock.Mock(),
        permissions_safe=mock.Mock(return_value=fault != "permissions"),
    )
    cursor = MetadataCursor(adapter, fault)
    handle = SimpleNamespace(cursor=lambda: cursor, commit=mock.Mock())
    roles = []

    @contextlib.contextmanager
    def connection(values, *, role):
        del values
        roles.append(role)
        yield handle

    adapter.connection = connection
    if fault == "ownership":
        adapter.ownership.side_effect = ValueError("private-marker-value")
    return adapter, cursor, handle, roles


class CdcImageLayoutTests(unittest.TestCase):
    """Import from Dockerfile COPY inputs without building or deploying."""

    def test_fixed_adapter_imports_from_the_actual_image_copy_layout(self):
        dockerfile = (ROOT / "infra/cdc/Dockerfile").read_text()
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory)
            for line in dockerfile.splitlines():
                if not line.startswith("COPY "):
                    continue
                parts = shlex.split(line)
                if parts[1].startswith("--") or not parts[-1].startswith("./"):
                    continue
                destination = image / parts[-1]
                for pattern in parts[1:-1]:
                    sources = list(ROOT.glob(pattern))
                    self.assertTrue(sources, pattern)
                    for source in sources:
                        if source.is_dir():
                            shutil.copytree(source, destination)
                        else:
                            target = (
                                destination / source.name
                                if parts[-1].endswith("/")
                                else destination
                            )
                            target.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(source, target)
            smoke = (
                "import sys; from pathlib import Path; "
                "root = Path.cwd(); "
                "sys.path[:0] = [str(root / 'cdc'), str(root)]; "
                "import fixed_source, connector, consumer; "
                "adapter = fixed_source.source_adapter(); "
                "assert Path(adapter.__file__).parent == root / 'scripts'; "
                "assert adapter.SAFETY_DEFINITION; "
                "assert len(fixed_source.table_mapping()) == 6"
            )
            result = subprocess.run(
                [sys.executable, "-I", "-c", smoke],
                cwd=image,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            # Prove isolation: the checkout cannot mask a missing image asset.
            (image / "scripts/sqlserver_safety.sql").unlink()
            missing = subprocess.run(
                [sys.executable, "-I", "-c", smoke],
                cwd=image,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertNotEqual(missing.returncode, 0)
            self.assertIn("sqlserver_safety.sql", missing.stderr)


class FixedCdcConfigurationTests(unittest.TestCase):
    """Keep fixed capture narrow while preserving the generic interface."""

    def test_fixed_mode_uses_source_reader_and_separate_derived_names(self):
        values = config.settings(
            {
                **VALUES,
                "CDC_SQLSERVER_PASSWORD": "not-the-reader-value",
                "CDC_TOPIC_PREFIX": "old-source",
                "CDC_GROUP_ID": "old-group",
            }
        )
        self.assertEqual(
            values["CDC_SQLSERVER_PASSWORD"],
            VALUES["SOURCE_SQLSERVER_PASSWORD"],
        )
        self.assertEqual(
            values["CDC_CONNECTOR_NAME"], fixed_source.FIXED_PREFIX
        )
        self.assertEqual(values["CDC_TOPIC_PREFIX"], fixed_source.FIXED_PREFIX)
        self.assertEqual(values["CDC_GROUP_ID"], fixed_source.FIXED_GROUP)
        self.assertEqual(values["CDC_TEST_TRUST_SERVER_CERTIFICATE"], "false")
        mapping = config.selected_mapping(values, ROOT / "no-map.json")
        self.assertEqual(len(mapping), 6)
        self.assertEqual(
            {item["source_schema"] for item in mapping.values()},
            {"lh_demo_erp", "lh_demo_mes"},
        )
        self.assertTrue(
            all(
                name.startswith(fixed_source.FIXED_PREFIX + ".")
                for name in config.topic_names(values, mapping)
            )
        )

    def test_connector_has_exact_table_columns_tls_and_credential_references(
        self,
    ):
        values = config.settings(VALUES)
        mapping = fixed_source.table_mapping()
        result = connector.configuration(values, mapping)
        self.assertEqual(
            result["database.user"], "${env:SOURCE_SQLSERVER_USER}"
        )
        self.assertEqual(
            result["database.password"], "${env:SOURCE_SQLSERVER_PASSWORD}"
        )
        self.assertNotIn(
            VALUES["SOURCE_SQLSERVER_PASSWORD"], json.dumps(result)
        )
        self.assertEqual(result["driver.encrypt"], "true")
        self.assertEqual(result["driver.trustServerCertificate"], "false")
        self.assertEqual(result["time.precision.mode"], "adaptive")
        self.assertEqual(result["snapshot.isolation.mode"], "exclusive")
        self.assertEqual(result["provide.transaction.metadata"], "true")
        self.assertEqual(
            result["event.processing.failure.handling.mode"], "fail"
        )
        self.assertEqual(
            result["event.converting.failure.handling.mode"], "fail"
        )
        for scope in ("tables", "databases"):
            name = "schema.history.internal.store.only.captured." + scope
            self.assertEqual(result[name + ".ddl"], "true")
        self.assertEqual(
            result["schema.history.internal.kafka.topic"],
            "_history_" + fixed_source.FIXED_PREFIX,
        )
        expected_tables = {
            re.escape(item["source_schema"] + "." + item["source_table"])
            for item in mapping.values()
        }
        self.assertEqual(
            set(result["table.include.list"].split(",")), expected_tables
        )
        selected = result["column.include.list"].split(",")
        self.assertEqual(
            len(selected),
            sum(len(item["columns"]) + 3 for item in mapping.values()),
        )
        for item in mapping.values():
            prefix = item["source_schema"] + "." + item["source_table"] + "."
            for column in fixed_source.EXTRA_COLUMNS:
                self.assertIn(re.escape(prefix + column), selected)

    def test_explicit_poc_trust_does_not_disable_encryption(self):
        values = config.settings(
            {
                **VALUES,
                "SOURCE_SQLSERVER_TRUST_SERVER_CERTIFICATE": "true",
            }
        )
        result = connector.configuration(values, fixed_source.table_mapping())
        self.assertEqual(result["driver.trustServerCertificate"], "true")
        self.assertEqual(result["driver.encrypt"], "true")

    def test_invalid_mode_flags_identity_schema_and_source_are_rejected(self):
        for change in (
            {"CDC_SOURCE_MODE": "other"},
            {"SOURCE_SQLSERVER_ENCRYPT": "false"},
            {"SOURCE_SQLSERVER_TRUST_SERVER_CERTIFICATE": "yes"},
            {"SOURCE_SQLSERVER_TIMEZONE": "Asia/Tokyo"},
            {"DEMO_SQLSERVER_ERP_SCHEMA": "dbo"},
            {"DEMO_SQLSERVER_INSTALLATION_ID": "bad"},
            {"SOURCE_SQLSERVER_USER": ""},
            {"CDC_STATE_VOLUME_NAME": "../state"},
        ):
            with self.subTest(setting=next(iter(change))):
                with self.assertRaises(ValueError):
                    config.settings({**VALUES, **change})

    def test_fixed_mapping_cannot_be_replaced_with_arbitrary_sql_objects(self):
        mapping = fixed_source.table_mapping()
        for change in ("schema", "columns", "extra"):
            bad = copy.deepcopy(mapping)
            if change == "schema":
                bad["erp_products"]["source_schema"] = "dbo"
            elif change == "columns":
                bad["erp_products"]["columns"]["free_sql"] = "product_id"
            else:
                bad["other_table"] = bad["erp_products"]
            with self.subTest(change=change):
                with self.assertRaises(ValueError):
                    connector.configuration(config.settings(VALUES), bad)

    def test_generic_mode_keeps_original_identity_and_millisecond_contract(
        self,
    ):
        values = config.settings(
            {
                "CDC_SQLSERVER_HOST": "generic-source.example.invalid",
                "CDC_SQLSERVER_DATABASE": "generic_database",
                "CDC_SQLSERVER_USER": "example_reader",
                "CDC_SQLSERVER_PASSWORD": "synthetic-generic-value",
            }
        )
        mapping = {
            item["table"]: {
                "source_schema": "dbo",
                "source_table": item["table"],
                "columns": {name: name for name in item["columns"]},
            }
            for item in config.CONTRACTS
        }
        result = connector.configuration(values, mapping)
        self.assertEqual(result["time.precision.mode"], "connect")
        self.assertEqual(result["snapshot.isolation.mode"], "read_committed")
        self.assertEqual(
            result["database.password"], "${env:CDC_SQLSERVER_PASSWORD}"
        )
        self.assertEqual(
            set(consumer.source_identity(values, mapping)),
            {
                "database",
                "host",
                "port",
                "prefix",
                "mapping",
            },
        )
        self.assertEqual(values["CDC_GROUP_ID"], "lakehouse-cdc")


class FixedPreflightTests(unittest.TestCase):
    """Preflight may read metadata but cannot repair source permissions/CDC."""

    def test_preflight_observes_exact_captures_using_only_reader_selects(self):
        adapter, cursor, handle, roles = metadata_adapter()
        result = fixed_source.preflight(
            config.settings(VALUES),
            fixed_source.table_mapping(),
            adapter=adapter,
        )
        self.assertEqual(
            result,
            {
                "checked": True,
                "tables": 6,
                "schema_version": 1,
            },
        )
        self.assertEqual(roles, ["source"])
        adapter.ownership.assert_called_once()
        adapter.permissions_safe.assert_called_once_with(cursor, role="source")
        self.assertTrue(
            all(
                statement.startswith("SELECT ")
                for statement, _ in cursor.statements
            )
        )
        handle.commit.assert_not_called()

    def test_hidden_or_different_metadata_fails_without_raw_driver_details(
        self,
    ):
        for fault in (
            "database",
            "capture",
            "columns",
            "precision",
            "role",
            "permissions",
            "ownership",
            "driver",
        ):
            adapter, _, handle, _ = metadata_adapter(fault)
            with self.subTest(fault=fault):
                with self.assertRaisesRegex(
                    ValueError, "preflight failed"
                ) as caught:
                    fixed_source.preflight(
                        config.settings(VALUES),
                        fixed_source.table_mapping(),
                        adapter=adapter,
                    )
                self.assertNotIn(
                    "private", str(caught.exception).replace("privately", "")
                )
                handle.commit.assert_not_called()

    def test_registration_preflight_failure_cannot_contact_connect(self):
        with (
            mock.patch.object(connector, "preflight", side_effect=ValueError),
            mock.patch.object(connector, "build_opener") as opener,
        ):
            with self.assertRaises(ValueError):
                connector.register(
                    config.settings(VALUES),
                    fixed_source.table_mapping(),
                    "http://cdc-connect:8083",
                )
            opener.assert_not_called()

    def test_custom_ca_placeholder_is_refused_before_a_driver_connects(self):
        adapter = fixed_source.source_adapter()
        target = "/run/source-certs/source-ca.pem"
        with mock.patch.object(
            adapter,
            "validate_settings",
            return_value={
                **VALUES,
                "SOURCE_SQLSERVER_CA_FILE": "/run/source-certs/source-ca.pem",
            },
        ):
            with self.assertRaisesRegex(
                ValueError, "private Compose override"
            ):
                config.settings(
                    {
                        **VALUES,
                        "SOURCE_SQLSERVER_CA_FILE": target,
                    }
                )

    def test_python_ca_requires_java_store_except_explicit_poc_trust(
        self,
    ):
        adapter = fixed_source.source_adapter()
        store = "/run/cdc/sqlserver-truststore.p12"
        values = {
            **VALUES,
            "SOURCE_SQLSERVER_CA_FILE": "/private/example-ca.pem",
        }
        with mock.patch.object(
            adapter,
            "validate_settings",
            side_effect=lambda selected, **_: dict(selected),
        ):
            with self.assertRaisesRegex(ValueError, "does not configure Java"):
                config.settings(values)
            trusted = config.settings(
                {
                    **values,
                    "CDC_SQLSERVER_TRUSTSTORE": store,
                }
            )
            result = connector.configuration(
                trusted, fixed_source.table_mapping()
            )
            self.assertEqual(
                result["driver.trustStore"],
                "/run/cdc/sqlserver-truststore.p12",
            )
            trusted = config.settings(
                {
                    **values,
                    "SOURCE_SQLSERVER_TRUST_SERVER_CERTIFICATE": "true",
                }
            )
            result = connector.configuration(
                trusted, fixed_source.table_mapping()
            )
            self.assertEqual(result["driver.trustServerCertificate"], "true")


class MemoryRawTable:
    """Model append/scan with real Arrow schemas, without an Iceberg server."""

    def __init__(self, schema, properties):
        self.arrow_schema = schema
        self.properties = dict(properties)
        self.rows = []

    def schema(self):
        """Expose the explicit Arrow schema expected by the production code."""
        return SimpleNamespace(as_arrow=lambda: self.arrow_schema)

    def scan(self, *, row_filter, selected_fields):
        """Return historical identities as an in-memory Arrow projection."""
        field, values = row_filter
        if field != "event_id":
            raise AssertionError("Unexpected raw scan filter.")
        schema = pa.schema([(name, pa.string()) for name in selected_fields])
        rows = [
            {name: row[name] for name in selected_fields}
            for row in self.rows
            if row["event_id"] in values
        ]
        return SimpleNamespace(
            to_arrow=lambda: pa.Table.from_pylist(rows, schema=schema)
        )

    def append(self, table):
        """Preserve existing rows exactly, as immutable raw append requires."""
        self.rows.extend(table.to_pylist())


class MemoryRawCatalog:
    """Model two isolated raw tables without creating real files/services."""

    def __init__(self):
        self.namespaces = set()
        self.tables = {}

    def namespace_exists(self, name):
        """Report only namespaces created in this test instance."""
        return name in self.namespaces

    def create_namespace(self, name):
        """Remember a namespace without side effects outside Python memory."""
        self.namespaces.add(name)

    def table_exists(self, name):
        """Check one in-memory table identity."""
        return name in self.tables

    def create_table(self, name, *, schema, properties):
        """Create an empty table with the proposed schema and marker."""
        self.tables[name] = MemoryRawTable(schema, properties)

    def load_table(self, name):
        """Return the tracked table; unexpected names raise immediately."""
        return self.tables[name]


def raw_record(offset=0, *, tombstone=False):
    """Build a synthetic Debezium value with exact sidecar and provenance."""
    value = {
        "op": "u",
        "before": {"product_id": "P1", "run_id": "a" * 12},
        "after": {
            "product_id": "P1",
            "run_id": "a" * 12,
            "original_values": '{"product_name":"日本語"}',
        },
        "source": {
            "db": "demo_database",
            "schema": "lh_demo_erp",
            "table": "products",
            "commit_lsn": "00000001:00000001:0001",
            "change_lsn": "00000001:00000001:0002",
            "event_serial_no": 2,
            "ts_ms": 1000,
        },
        "transaction": {"id": "synthetic-transaction"},
    }
    return {
        "topic": fixed_source.FIXED_PREFIX + ".lh_demo_erp.products",
        "partition": 0,
        "offset": offset,
        "key": '{"product_id":"P1"}',
        "value": None if tombstone else json.dumps(value, ensure_ascii=False),
        "ingested_at": "2026-04-01T12:00:00Z",
    }


class FixedRawLogTests(unittest.TestCase):
    """Keep exact event evidence separately from rebuildable current rows."""

    def setUp(self):
        self.catalog = MemoryRawCatalog()
        self.identity = "c" * 64

        # The source-quality environment deliberately excludes worker SDKs.
        # Mock only the expression constructor; append/dedup/Arrow are real.
        def in_expression(field, values):
            """Represent the single allowlisted raw scan predicate."""
            return field, tuple(values)

        expressions = SimpleNamespace(In=in_expression)
        modules = {
            "pyiceberg": SimpleNamespace(expressions=expressions),
            "pyiceberg.expressions": expressions,
        }
        previous = {name: sys.modules.get(name) for name in modules}
        sys.modules.update(modules)

        def restore_expression_modules():
            """Restore only our keys; preserve Arrow's lazy pandas imports."""
            for name, module in previous.items():
                if module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = module

        self.addCleanup(restore_expression_modules)

    def append(self, records):
        """Use the real append implementation against the memory catalog."""
        return consumer.append_raw(
            self.catalog,
            records,
            namespace="sqlcdc_events",
            identity=self.identity,
        )

    def test_raw_retains_full_envelope_and_explicit_operation_metadata(self):
        record = raw_record()
        self.assertEqual(self.append([record]), 1)
        row = self.catalog.tables["sqlcdc_events.events"].rows[0]
        self.assertEqual(row["envelope_json"], record["value"])
        self.assertEqual(row["operation"], "u")
        self.assertEqual(row["tombstone"], "false")
        self.assertEqual(row["transaction_id"], "synthetic-transaction")
        self.assertEqual(row["commit_lsn"], "00000001:00000001:0001")
        self.assertEqual(row["source_ts_ms"], "1000")
        self.assertEqual(json.loads(row["after_json"])["product_id"], "P1")

    def test_tombstone_is_separate_raw_record_and_is_never_a_delete_rewrite(
        self,
    ):
        self.assertEqual(
            self.append([raw_record(), raw_record(1, tombstone=True)]), 2
        )
        rows = self.catalog.tables["sqlcdc_events.events"].rows
        self.assertEqual(
            [row["operation"] for row in rows], ["u", "tombstone"]
        )
        self.assertEqual(rows[1]["tombstone"], "true")
        self.assertIsNone(rows[1]["envelope_json"])
        self.assertEqual(rows[1]["before_json"], "null")

    def test_replay_deduplicates_offsets_without_changing_first_ingestion(
        self,
    ):
        record = raw_record()
        self.assertEqual(self.append([record, record]), 1)
        original = copy.deepcopy(
            self.catalog.tables["sqlcdc_events.events"].rows
        )
        replay = {**record, "ingested_at": "2026-04-02T12:00:00Z"}
        self.assertEqual(self.append([replay]), 0)
        self.assertEqual(
            self.catalog.tables["sqlcdc_events.events"].rows, original
        )

    def test_changed_payload_at_same_offset_refuses_a_broker_identity_reset(
        self,
    ):
        record = raw_record()
        self.append([record])
        record["value"] = record["value"].replace('"op": "u"', '"op": "d"')
        with self.assertRaisesRegex(ValueError, "changed payload"):
            self.append([record])
        self.assertEqual(
            len(self.catalog.tables["sqlcdc_events.events"].rows), 1
        )

    def test_empty_value_and_null_tombstone_cannot_share_a_delivery_identity(
        self,
    ):
        empty = {**raw_record(), "key": "", "value": ""}
        self.append([empty])
        row = self.catalog.tables["sqlcdc_events.events"].rows[0]
        self.assertEqual(row["key_json"], "")
        self.assertEqual(row["envelope_json"], "")
        self.assertEqual(row["tombstone"], "false")
        with self.assertRaisesRegex(ValueError, "changed payload"):
            self.append([{**empty, "value": None}])

    def test_fixed_namespace_never_claims_generic_raw_or_different_source(
        self,
    ):
        record = raw_record()
        consumer.append_raw(self.catalog, [record])
        self.append([record])
        self.assertEqual(
            set(self.catalog.tables),
            {
                "cdc_events.events",
                "sqlcdc_events.events",
            },
        )
        self.identity = "d" * 64
        with self.assertRaisesRegex(ValueError, "ownership or schema"):
            self.append([raw_record(1)])
        self.assertEqual(len(self.catalog.tables["cdc_events.events"].rows), 1)

    def test_malformed_event_is_saved_before_current_state_is_refused(self):
        values = config.settings(VALUES)
        mapping = fixed_source.table_mapping()
        record = {**raw_record(), "value": "not-json"}
        admin = SimpleNamespace(catalog=lambda: self.catalog)
        state = SimpleNamespace(fixed_source=False, complete=mock.Mock())
        with self.assertRaisesRegex(ValueError, "current-state support"):
            consumer.process(
                admin,
                state,
                [record],
                mapping,
                config.topic_names(values, mapping),
                values=values,
            )
        row = self.catalog.tables["sqlcdc_events.events"].rows[0]
        self.assertEqual(row["envelope_json"], "not-json")
        state.complete.assert_not_called()


class KafkaMessageDecodingTests(unittest.TestCase):
    """Kafka null and empty bytes have different immutable meanings."""

    def message(self, value, key=b'{"product_id":"P1"}'):
        """Supply only the Kafka message methods used by the decoder."""
        return SimpleNamespace(
            value=lambda: value,
            key=lambda: key,
            topic=lambda: "example-topic",
            partition=lambda: 0,
            offset=lambda: 2,
        )

    def test_empty_bytes_stay_empty_instead_of_becoming_a_tombstone(self):
        record = consumer.message_record(self.message(b"", b""))
        self.assertEqual(record["value"], "")
        self.assertEqual(record["key"], "")

    def test_null_value_and_key_are_retained_as_null(self):
        record = consumer.message_record(self.message(None, None))
        self.assertIsNone(record["value"])
        self.assertIsNone(record["key"])

    def test_a_business_delete_is_an_envelope_not_a_tombstone(self):
        payload = b'{"op":"d","before":{"product_id":"P1"},"after":null}'
        record = consumer.message_record(self.message(payload))
        self.assertEqual(json.loads(record["value"])["op"], "d")
        self.assertEqual(record["value"], payload.decode())

    def test_oversized_value_is_rejected_before_decode(self):
        with self.assertRaisesRegex(ValueError, "16 MiB"):
            consumer.message_record(
                self.message(b"a" * (16 * 1024 * 1024 + 1))
            )

    def test_combined_key_value_limit_matches_replay_before_decoding(self):
        with self.assertRaisesRegex(ValueError, "16 MiB"):
            consumer.message_record(
                self.message(b"a" * (consumer.MAX_RECORD_BYTES - 1), b"{}")
            )
        with self.assertRaisesRegex(ValueError, "16 MiB"):
            consumer.message_record(
                self.message(None, b"a" * (consumer.MAX_RECORD_BYTES + 1))
            )


class FixedCdcStaticContracts(unittest.TestCase):
    """Check image inputs without Docker starts or package resolution."""

    def test_compose_does_not_share_writer_auth_or_marker_with_java_connect(
        self,
    ):
        document = yaml.safe_load(
            (ROOT / "infra/cdc/compose.yaml").read_text()
        )
        services = document["services"]
        for name in ("cdc-connect", "cdc-register", "cdc-consumer"):
            environment = services[name]["environment"]
            self.assertIn("SOURCE_SQLSERVER_PASSWORD", environment)
            self.assertNotIn("DEMO_SQLSERVER_PASSWORD", environment)
            self.assertNotIn("SOURCE_SQLSERVER_CONTROL_PASSWORD", environment)
        self.assertNotIn(
            "DEMO_SQLSERVER_OWNERSHIP_TOKEN",
            services["cdc-connect"]["environment"],
        )
        for name in ("cdc-register", "cdc-consumer"):
            self.assertIn(
                "DEMO_SQLSERVER_OWNERSHIP_TOKEN", services[name]["environment"]
            )
            mounts = [
                item
                for item in services[name]["volumes"]
                if isinstance(item, dict)
                and item.get("target") == "/run/source-certs/source-ca.pem"
            ]
            self.assertEqual(len(mounts), 1)
            self.assertTrue(mounts[0]["read_only"])
            self.assertEqual(mounts[0]["source"], "../proxy/empty-ca.pem")
        self.assertIn(
            "CDC_STATE_VOLUME_NAME:-", document["volumes"]["cdc-state"]["name"]
        )

    def test_cdc_adds_only_the_existing_pinned_driver_stanza(self):
        lock = (ROOT / "infra/cdc/requirements.lock").read_text()
        demo = (ROOT / "infra/demo/requirements.lock").read_text()

        def hashes(text):
            """Compare the reused artifact hashes without network lookups."""
            return set(re.findall(r"sha256:([0-9a-f]{64})", text))

        self.assertEqual(
            hashes(lock.split("pymssql==2.4.6", 1)[1]),
            hashes(
                demo.split("pymssql==2.4.6", 1)[1].split(
                    "\npython-dateutil==", 1
                )[0]
            ),
        )
        self.assertNotIn("streamlit==", lock)
        dockerfile = (ROOT / "infra/cdc/Dockerfile").read_text()
        self.assertIn("COPY scripts/sqlserver_demo.py", dockerfile)
        self.assertIn(
            "--require-hashes requirements.lock cdc-requirements.lock",
            dockerfile,
        )


if __name__ == "__main__":
    unittest.main()

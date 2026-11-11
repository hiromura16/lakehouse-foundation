"""Test sanitized SQL observations with synthetic HTTP/query fakes."""

import importlib
import json
import sys
import threading
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest import mock

import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "infra/demo"))
OBSERVER = importlib.import_module("sqlsource_observer")
TOKEN = "unit-worker"
OBSERVED = "2026-04-01T12:00:00.1234567Z"
REQUEST_ID = "a" * 32
NEXT_URI = "http://trino:8080/v1/statement/queued/test/1"


def consumer_fixture():
    """Supply public cache state and private extras for filtering tests."""
    return {
        "mode": "fixed-demo",
        "ready": True,
        "blocked": False,
        "busy": False,
        "observation_time": datetime.now(timezone.utc).isoformat(),
        "last_success": None,
        "error_code": None,
        "current_rows": {name: None for name in OBSERVER.IDS},
        "partitions": [
            {
                "table": "erp_products",
                "partition": 0,
                "completed_offset": 1,
                "broker_low": 0,
                "broker_high": 5,
                "next_offset": 2,
                "lag": 3,
                "state": "observed",
                "topic": "private-topic-fixture",
            }
        ],
        "raw_events_appended_since_start": 0,
        "rebuild": None,
        "source_host": "sql-private.example.invalid",
        "password": TOKEN,
    }


def connector_fixture():
    """Supply Connect lifecycle fields plus details that must never escape."""
    return {
        "name": "lakehouse-demo-sqlserver",
        "connector": {
            "state": "RUNNING",
            "worker_id": "private-worker-fixture",
        },
        "tasks": [
            {
                "id": 0,
                "state": "RUNNING",
                "trace": "private-trace-fixture",
                "worker_id": "private-worker-fixture",
            }
        ],
    }


def observation_query(statement, *, maximum):
    """Return only the fixed query shapes; reject accidental new SQL scope."""
    if "UNION ALL" in statement:
        if maximum != 6:
            raise AssertionError("Unexpected count bound")
        return [[name, 3, 2, 1] for name in OBSERVER.IDS]
    if "sql_control.batches" in statement:
        batch_id = "b" * 64
        apply_id = sha256((batch_id + "\n" + OBSERVED).encode()).hexdigest()
        counts = {name: 3 for name in OBSERVER.IDS}
        return [
            [
                apply_id,
                batch_id,
                "incremental",
                OBSERVED,
                json.dumps(
                    {"selected": counts, "changed": counts, "current": counts}
                ),
                "14",
            ]
        ]
    if "sql_control.progress" in statement:
        return [["applying"]]
    if statement == "SELECT count(*) FROM iceberg.sqlcdc_events.events":
        return [[25]]
    raise AssertionError("Unexpected observer query")


class ObservationContractTests(unittest.TestCase):
    """Separate observations from missing metadata and integration proof."""

    def test_batch_layers_completion_and_pending_are_independent(self):
        result = OBSERVER.pipeline_status(observation_query)
        self.assertEqual(result["state"], "observed")
        self.assertTrue(result["pending"])
        self.assertFalse(result["consistent_snapshot"])
        self.assertEqual(result["layers"][0]["bronze_rows"], 3)
        self.assertEqual(result["last_batch"]["counts"]["erp_products"], 3)
        self.assertEqual(
            result["last_batch"]["changed_counts"]["erp_products"], 3
        )
        self.assertNotIn("source_identity", json.dumps(result))

    def test_duplicate_progress_is_unknown_instead_of_falsely_idle(self):
        def query(statement, *, maximum):
            if "sql_control.progress" in statement:
                self.assertEqual(maximum, 2)
                self.assertIn("LIMIT 2", statement)
                return [["idle"], ["applying"]]
            return observation_query(statement, maximum=maximum)

        result = OBSERVER.pipeline_status(query)
        self.assertEqual(result["state"], "partial")
        self.assertIsNone(result["pending"])
        self.assertFalse(result["progress_observed"])

    def test_unavailable_counts_and_lag_are_null_never_guessed_zero(self):
        def unavailable(*_, **__):
            raise RuntimeError("private-db-error-fixture")

        result = OBSERVER.pipeline_status(unavailable)
        self.assertEqual(result["state"], "unavailable")
        self.assertIsNone(result["pending"])
        self.assertTrue(
            all(row["bronze_rows"] is None for row in result["layers"])
        )
        self.assertNotIn("private-db-error-fixture", json.dumps(result))
        value = consumer_fixture()
        value["partitions"][0].update(broker_high=None, lag=0)
        result = OBSERVER.consumer_observation(value)
        self.assertIsNone(result["partitions"][0]["lag"])
        self.assertEqual(result["partitions"][0]["state"], "unavailable")

    def test_future_or_inconsistent_cache_times_are_rejected(self):
        for field in ("observation_time", "last_success"):
            value = consumer_fixture()
            value[field] = (
                datetime.now(timezone.utc) + timedelta(minutes=5)
            ).isoformat()
            with self.assertRaises(OBSERVER.ObservationUnavailable):
                OBSERVER.consumer_observation(value)

    def test_invalid_partition_ranges_never_claim_observed(self):
        for changes in (
            {"broker_low": 6, "broker_high": 5},
            {"broker_low": 3},
            {"broker_high": 1, "lag": 0},
        ):
            value = consumer_fixture()
            value["partitions"][0].update(changes)
            with self.assertRaises(OBSERVER.ObservationUnavailable):
                OBSERVER.consumer_observation(value)
        for field in ("completed_offset", "broker_low"):
            value = consumer_fixture()
            value["partitions"][0][field] = None
            result = OBSERVER.consumer_observation(value)
            self.assertEqual(result["partitions"][0]["state"], "unavailable")
            self.assertIsNone(result["partitions"][0]["lag"])

    def test_partial_count_failure_keeps_a_real_completed_batch_observation(
        self,
    ):
        def query(statement, *, maximum):
            if "UNION ALL" in statement:
                raise RuntimeError("private-count-fixture")
            return observation_query(statement, maximum=maximum)

        result = OBSERVER.pipeline_status(query)
        self.assertEqual(result["state"], "partial")
        self.assertTrue(result["history_observed"])
        self.assertIsNotNone(result["last_batch"])
        self.assertIsNone(result["layers"][0]["silver_rows"])

    def test_fixed_count_queries_reject_unknown_names_and_duplicate_results(
        self,
    ):
        with self.assertRaises(OBSERVER.ObservationUnavailable):
            OBSERVER.count_statement("dbo; SELECT")
        query = mock.Mock(return_value=[["erp_products", 1, 1, 0]] * 6)
        with self.assertRaises(OBSERVER.ObservationUnavailable):
            OBSERVER.read_layers("batch", query)
        for origin in ("batch", "cdc"):
            statement = OBSERVER.count_statement(origin)
            self.assertEqual(statement.count("UNION ALL"), 5)
            self.assertNotIn("SOURCE_SQLSERVER", statement)

    def test_consumer_and_connect_extras_and_raw_topics_are_dropped(self):
        consumer = OBSERVER.consumer_observation(consumer_fixture())
        connector = OBSERVER.connector_observation(connector_fixture())
        result = json.dumps({"consumer": consumer, "connector": connector})
        for private in (
            TOKEN,
            "sql-private.example.invalid",
            "private-topic-fixture",
            "private-worker-fixture",
            "private-trace-fixture",
        ):
            self.assertNotIn(private, result)
        self.assertEqual(consumer["partitions"][0]["lag"], 3)
        self.assertIsNone(consumer["current_rows"]["erp_products"])
        self.assertEqual(connector["tasks"], [{"id": 0, "state": "RUNNING"}])

    def test_unobserved_start_and_stale_cache_are_not_healthy_claims(self):
        value = consumer_fixture()
        value.update(
            observation_time=None, ready=False, error_code="CDC_STARTING"
        )
        value["partitions"] = []
        result = OBSERVER.consumer_observation(value)
        self.assertEqual(result["state"], "unobserved")
        self.assertEqual(result["partitions"], [])
        value["observation_time"] = OBSERVED
        self.assertEqual(
            OBSERVER.consumer_observation(value)["state"], "stale"
        )

    def test_invalid_error_codes_lag_and_logical_tables_are_rejected(self):
        variants = []
        value = consumer_fixture()
        value["error_code"] = "private-backend-message"
        variants.append(value)
        value = consumer_fixture()
        value["partitions"][0]["lag"] = 0
        variants.append(value)
        value = consumer_fixture()
        value["partitions"][0]["table"] = "private_source_table"
        variants.append(value)
        for value in variants:
            with self.assertRaises(OBSERVER.ObservationUnavailable):
                OBSERVER.consumer_observation(value)

    def test_disabled_or_generic_cdc_performs_no_service_queries(self):
        requester, query = mock.Mock(), mock.Mock()
        for values in (
            {},
            {"COMPOSE_PROFILES": "cdc", "CDC_SOURCE_MODE": "generic"},
        ):
            result = OBSERVER.cdc_status(
                values, query=query, requester=requester
            )
            self.assertFalse(result["configured"])
            self.assertIsNone(result["raw_rows"])
        requester.assert_not_called()
        query.assert_not_called()

    def test_cdc_service_cache_and_persisted_raw_count_have_distinct_meanings(
        self,
    ):
        def requester(target, **kwargs):
            if target == "consumer-status":
                self.assertEqual(kwargs, {"token": TOKEN})
                return consumer_fixture()
            self.assertEqual(target, "connector-status")
            self.assertEqual(kwargs, {})
            return connector_fixture()

        result = OBSERVER.cdc_status(
            {
                "COMPOSE_PROFILES": "demo,cdc",
                "CDC_SOURCE_MODE": "fixed-demo",
                "WORKER_TOKEN": TOKEN,
                "SOURCE_SQLSERVER_PASSWORD": "private-source-fixture",
            },
            query=observation_query,
            requester=requester,
        )
        self.assertEqual(result["raw_rows"], 25)
        self.assertEqual(
            result["consumer"]["raw_events_appended_since_start"], 0
        )
        self.assertFalse(result["consistent_snapshot"])
        self.assertNotIn("private-source-fixture", json.dumps(result))


class BoundedHttpObservationTests(unittest.TestCase):
    """Exercise real HTTPX transport logic with an in-memory mock transport."""

    def client_factory(self, handler):
        real_client = httpx.Client
        transport = httpx.MockTransport(handler)
        return mock.patch.object(
            OBSERVER.httpx,
            "Client",
            side_effect=lambda **kwargs: real_client(
                transport=transport, **kwargs
            ),
        )

    def test_trino_pagination_uses_only_validated_fixed_origin(self):
        calls = []

        def handler(request):
            calls.append(request)
            if request.method == "POST":
                return httpx.Response(
                    200,
                    json={
                        "data": [["erp_products", 1, 1, 0]],
                        "nextUri": NEXT_URI,
                    },
                )
            return httpx.Response(
                200,
                json={
                    "data": [
                        [name, 1, 1, 0]
                        for name in OBSERVER.IDS
                        if name != "erp_products"
                    ]
                },
            )

        with self.client_factory(handler) as factory:
            rows = OBSERVER.query_rows(OBSERVER.count_statement("batch"))
        self.assertEqual(len(rows), 6)
        self.assertEqual(
            [request.method for request in calls], ["POST", "GET"]
        )
        self.assertTrue(all(request.url.host == "trino" for request in calls))
        self.assertFalse(factory.call_args.kwargs["trust_env"])
        self.assertFalse(factory.call_args.kwargs["follow_redirects"])

    def test_external_next_uri_and_oversized_pages_never_escape_as_raw_text(
        self,
    ):
        for payload in (
            {"nextUri": "http://private-service.example.invalid/query"},
            {"data": [["private-page-fixture" * 5000]]},
        ):
            calls = []

            def handler(request):
                calls.append(request)
                return httpx.Response(200, json=payload)

            with self.client_factory(handler):
                with self.assertRaises(
                    OBSERVER.ObservationUnavailable
                ) as error:
                    OBSERVER.query_rows("SELECT fixed")
            self.assertEqual(len(calls), 1)
            self.assertNotIn("private-service", str(error.exception))
            self.assertNotIn("private-page", str(error.exception))

    def test_poll_deadline_cancels_only_the_validated_trino_reference(self):
        calls = []

        def handler(request):
            calls.append(request)
            if request.method == "DELETE":
                return httpx.Response(204)
            return httpx.Response(
                200,
                json={"nextUri": NEXT_URI},
            )

        with (
            self.client_factory(handler),
            mock.patch.object(
                OBSERVER.time, "monotonic", side_effect=[0, 0, 0, 0, 11]
            ),
        ):
            with self.assertRaises(OBSERVER.ObservationUnavailable):
                OBSERVER.query_rows("SELECT fixed")
        self.assertEqual(
            [request.method for request in calls], ["POST", "DELETE"]
        )
        self.assertEqual(calls[-1].url.host, "trino")

    def test_auth_is_scoped_and_rebuild_transport_failure_is_unknown(
        self,
    ):
        calls = []

        def handler(request):
            calls.append(request)
            if request.method == "POST":
                raise httpx.ReadTimeout(
                    "private-timeout-detail", request=request
                )
            return httpx.Response(200, json={})

        with self.client_factory(handler):
            OBSERVER.request_json("consumer-status", token=TOKEN)
            OBSERVER.request_json("connector-status")
            with self.assertRaises(OBSERVER.RebuildUnknown) as error:
                OBSERVER.request_json(
                    "consumer-rebuild", token=TOKEN, payload={"confirm": True}
                )
            with self.assertRaises(OBSERVER.ObservationUnavailable):
                OBSERVER.request_json(
                    "http://external.example.invalid", token=TOKEN
                )
        self.assertEqual(calls[0].headers["Authorization"], "Bearer " + TOKEN)
        self.assertNotIn("Authorization", calls[1].headers)
        self.assertNotIn("private-timeout-detail", str(error.exception))

    def test_slow_fragments_and_encoded_bodies_are_rejected(self):
        clock = [0]
        fragments = []

        class SlowChunks(httpx.SyncByteStream):
            def __iter__(self):
                fragments.append(1)
                yield b'{"data":'
                clock[0] = 11
                fragments.append(2)
                yield b"[]}"  # Too late even though the body is tiny.
                fragments.append(3)
                yield b" "

        response = httpx.Response(200, stream=SlowChunks())
        with mock.patch.object(
            OBSERVER.time, "monotonic", side_effect=lambda: clock[0]
        ):
            with self.assertRaises(OBSERVER.ObservationUnavailable):
                OBSERVER._bounded_json(response, deadline=10)
        self.assertEqual(fragments, [1, 2])
        response = httpx.Response(
            200,
            headers={"Content-Encoding": "gzip"},
            stream=SlowChunks(),
        )
        with self.assertRaises(OBSERVER.ObservationUnavailable):
            OBSERVER._bounded_json(response, deadline=float("inf"))

    def test_post_read_and_protocol_failures_have_indeterminate_outcomes(self):
        for failure in (httpx.ReadError, httpx.RemoteProtocolError):

            def handler(request):
                raise failure("private-wire-error", request=request)

            with self.client_factory(handler):
                with self.assertRaises(OBSERVER.RebuildUnknown) as error:
                    OBSERVER.request_json(
                        "consumer-rebuild",
                        token=TOKEN,
                        payload={"confirm": True},
                    )
            self.assertNotIn("private-wire-error", str(error.exception))

    def test_accepted_post_with_unreadable_body_has_unknown_outcome(self):
        for content in (b"private-malformed-fixture", b"x" * 65537):

            def handler(_request):
                return httpx.Response(202, content=content)

            with self.client_factory(handler):
                with self.assertRaises(OBSERVER.RebuildUnknown) as error:
                    OBSERVER.request_json(
                        "consumer-rebuild",
                        token=TOKEN,
                        payload={"confirm": True},
                    )
            self.assertNotIn("private-malformed", str(error.exception))

    def test_default_gold_preview_uses_bounded_pinned_http_projection(self):
        calls = []
        columns = list(OBSERVER.GOLD["sales"]["columns"])
        source = {name: None for name in columns}
        source.update(sales_order_id="SO-test", unit_price="0.10000000000001")

        def handler(request):
            statement = request.content.decode()
            calls.append(statement)
            self.assertEqual(request.url.host, "trino")
            self.assertEqual(request.headers["Accept-Encoding"], "identity")
            if "$refs" in statement:
                self.assertIn("WHERE name = 'main' LIMIT 1", statement)
                return httpx.Response(200, json={"data": [[123]]})
            encoded = json.dumps({"data": [[source[x] for x in columns]]})
            # Real JSON decimal numbers must not round through binary float.
            encoded = encoded.replace('"0.10000000000001"', "0.10000000000001")
            return httpx.Response(200, content=encoded.encode())

        with self.client_factory(handler):
            result = OBSERVER.gold_preview("cdc", "sales", 3)
        self.assertEqual(len(calls), 2)
        self.assertIn('iceberg.sqlcdc_gold."sales_fulfillment$refs"', calls[0])
        self.assertIn("FOR VERSION AS OF 123", calls[1])
        self.assertTrue(calls[1].endswith("ORDER BY sales_order_id LIMIT 3"))
        self.assertNotIn("SELECT *", calls[1])
        self.assertEqual(result["snapshot_id"], "123")
        self.assertEqual(result["rows"][0]["unit_price"], "0.10000000000001")


class GoldPreviewTests(unittest.TestCase):
    """Verify decimals, snapshot provenance and private-column filtering."""

    def exporter(self, values, datasets, limit, *, origin):
        self.assertEqual(values, {"BIND_IP": "trino", "TRINO_PORT": "8080"})
        self.assertEqual(datasets, ["sales"])
        return {
            "datasets": [
                {
                    "dataset": "sales",
                    "table": "iceberg."
                    + OBSERVER.ORIGINS[origin]
                    + "_gold.sales_fulfillment",
                    "snapshot_id": "12345",
                    "rows": [
                        {
                            "sales_order_id": "SO-DEMO",
                            "unit_price": Decimal("1234567890123456.78"),
                            "order_date": date(2026, 1, 5),
                            "last_shipped_at": datetime(
                                2026, 1, 6, 12, tzinfo=timezone.utc
                            ),
                            "source_database": "private-db-fixture",
                            "password": TOKEN,
                        }
                    ],
                }
            ]
        }

    def test_snapshot_preview_keeps_decimals_dates_and_drops_private_columns(
        self,
    ):
        result = OBSERVER.gold_preview(
            "batch", "sales", 10, exporter=self.exporter
        )
        self.assertEqual(
            result["rows"][0]["unit_price"], "1234567890123456.78"
        )
        self.assertEqual(result["rows"][0]["order_date"], "2026-01-05")
        self.assertEqual(
            result["rows"][0]["last_shipped_at"], "2026-01-06T12:00:00+00:00"
        )
        self.assertFalse(result["complete_dataset"])
        self.assertEqual(result["snapshot_id"], "12345")
        self.assertNotIn("private-db-fixture", json.dumps(result))
        self.assertNotIn(TOKEN, json.dumps(result))

    def test_unknown_origin_dataset_and_limits_fail_before_export(self):
        exporter = mock.Mock()
        for origin, dataset, limit in (
            ("sql_gold", "sales", 10),
            ("batch", "arbitrary_table", 10),
            ("cdc", "sales", 101),
            ("batch", "sales", True),
        ):
            with self.assertRaises(OBSERVER.ObservationUnavailable):
                OBSERVER.gold_preview(
                    origin, dataset, limit, exporter=exporter
                )
        exporter.assert_not_called()

    def test_wrong_schema_and_nonfinite_business_values_are_rejected(self):
        for alteration in ("schema", "nonfinite"):

            def exporter(*args, **kwargs):
                result = self.exporter(*args, **kwargs)
                if alteration == "schema":
                    result["datasets"][0]["table"] = "private_db.dbo.source"
                else:
                    result["datasets"][0]["rows"][0]["unit_price"] = Decimal(
                        "NaN"
                    )
                return result

            with self.assertRaises(OBSERVER.ObservationUnavailable):
                OBSERVER.gold_preview("cdc", "sales", 10, exporter=exporter)


class ObserverRouteTests(unittest.TestCase):
    """Test typed authenticated routes without a DB or real network."""

    def setUp(self):
        self.jobs = SimpleNamespace(operation=threading.Lock())
        self.store = SimpleNamespace(
            applied={
                "COMPOSE_PROFILES": "demo,cdc",
                "CDC_SOURCE_MODE": "fixed-demo",
                "WORKER_TOKEN": TOKEN,
            }
        )
        self.requester = mock.Mock(
            return_value={
                "accepted": True,
                "request_id": REQUEST_ID,
                "status": "queued",
                "completed": False,
                "private_detail": TOKEN,
            }
        )

        def authorize(authorization: str | None = Header(default=None)):
            if authorization != "Bearer synthetic-control":
                raise HTTPException(401)

        self.app = FastAPI()

        @self.app.exception_handler(RequestValidationError)
        async def invalid_request(_, __):
            return JSONResponse({"code": "INVALID_REQUEST"}, status_code=422)

        self.app.include_router(
            OBSERVER.observation_router(
                self.store,
                self.jobs,
                authorize,
                query=observation_query,
                requester=self.requester,
            )
        )
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.headers = {"Authorization": "Bearer synthetic-control"}

    def test_rebuild_auth_strict_consent_and_queued_is_not_completed(self):
        self.assertEqual(
            self.client.post(
                "/sql-source/cdc/rebuild", json={"confirm": True}
            ).status_code,
            401,
        )
        for value in (
            {},
            {"confirm": False},
            {"confirm": 1},
            {"confirm": "true"},
            {"confirm": True, "sql": TOKEN},
        ):
            response = self.client.post(
                "/sql-source/cdc/rebuild", json=value, headers=self.headers
            )
            self.assertEqual(response.status_code, 422)
            self.assertNotIn(TOKEN, response.text)
        self.requester.assert_not_called()
        response = self.client.post(
            "/sql-source/cdc/rebuild",
            json={"confirm": True},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(
            response.json(),
            {
                "accepted": True,
                "request_id": REQUEST_ID,
                "status": "queued",
                "completed": False,
            },
        )
        self.assertFalse(self.jobs.operation.locked())
        self.assertFalse(hasattr(self.jobs, "jobs"))

    def test_rebuild_unknown_or_busy_is_a_fixed_error_without_backend_details(
        self,
    ):
        for error, status in (
            (OBSERVER.RebuildBusy(TOKEN), 409),
            (OBSERVER.RebuildUnknown(TOKEN), 503),
            (RuntimeError(TOKEN), 503),
        ):
            self.requester.side_effect = error
            response = self.client.post(
                "/sql-source/cdc/rebuild",
                json={"confirm": True},
                headers=self.headers,
            )
            self.assertEqual(response.status_code, status)
            self.assertNotIn(TOKEN, response.text)
            self.assertFalse(self.jobs.operation.locked())

    def test_observations_are_typed_and_preview_identifiers_are_finite(self):
        response = self.client.get(
            "/sql-source/pipeline-status", headers=self.headers
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["pending"])
        for suffix in (
            "origin=dbo",
            "origin=batch&dataset=arbitrary",
            "origin=cdc&dataset=sales&limit=101",
        ):
            response = self.client.get(
                "/sql-source/preview?" + suffix, headers=self.headers
            )
            self.assertEqual(response.status_code, 422)
        self.assertIn("/sql-source/cdc/rebuild", self.app.openapi()["paths"])


class SqlObservationUiTests(unittest.TestCase):
    """Click real Streamlit controls through an in-memory API substitute."""

    def setUp(self):
        try:
            from streamlit.testing.v1 import AppTest
        except ModuleNotFoundError as error:
            if (
                error.name != "streamlit"
                or importlib.util.find_spec("streamlit") is not None
            ):
                raise
            self.skipTest("Streamlit is not installed in this test runtime.")
        self.app_test = AppTest
        self.calls = []
        self.reply_status = None
        client = ModuleType("sql_observer_test_client")
        client.request = self.request
        previous = sys.modules.get(client.__name__)
        sys.modules[client.__name__] = client

        def restore():
            if previous is None:
                sys.modules.pop(client.__name__, None)
            else:
                sys.modules[client.__name__] = previous

        self.addCleanup(restore)

    def request(self, path, *, method="GET", payload=None, timeout=15):
        """Record requests and provide only finite public response fixtures."""
        self.calls.append((method, path, payload))
        if path == "/sql-source/checkpoints":
            return {"configured": False, "baseline_complete": False}
        if path == "/sql-source/cdc/rebuild":
            return {
                "accepted": True,
                "request_id": REQUEST_ID,
                "status": "queued",
                "completed": False,
            }
        if path == "/sql-source/cdc/status":
            return self.reply_status
        if path.startswith("/sql-source/preview?origin=batch&dataset="):
            return {
                "origin": "batch",
                "dataset": "production",
                "table": "iceberg.sql_gold.factory_daily_production",
                "snapshot_id": "123",
                "rows": [{"factory_id": "F001", "good_qty": 10}],
                "row_limit": 20,
                "complete_dataset": False,
            }
        raise AssertionError("Unexpected UI request")

    def app(self):
        source = (
            "import streamlit as st\n"
            "from sqlsource_ui import render_sql_observations\n"
            "from sql_observer_test_client import request\n"
            "st.session_state.setdefault('active_job', None)\n"
            "render_sql_observations(request)\n"
        )
        app = self.app_test.from_string(source).run(timeout=10)
        self.assertFalse(app.exception)
        return app

    def test_watermark_is_explicit_and_rebuild_queue_never_sets_job_success(
        self,
    ):
        app = self.app()
        self.assertEqual(self.calls, [])
        self.assertTrue(app.button(key="sql-rebuild-request").disabled)
        app.button(key="sql-observe-watermark").click().run(timeout=10)
        self.assertEqual(self.calls[-1][1], "/sql-source/checkpoints")
        app.checkbox(key="sql-rebuild-consent").check().run(timeout=10)
        app.button(key="sql-rebuild-request").click().run(timeout=10)
        self.assertFalse(app.exception)
        self.assertEqual(
            self.calls[-1],
            ("POST", "/sql-source/cdc/rebuild", {"confirm": True}),
        )
        self.assertEqual(
            app.session_state["sql-last-rebuild-request"], REQUEST_ID
        )
        self.assertIsNone(app.session_state["active_job"])
        self.assertEqual(len(app.success), 0)
        self.assertTrue(any("受付だけ" in text.value for text in app.info))

    def test_failed_and_blocked_consumers_show_safe_lifecycle(self):
        for blocked, code in (
            (False, "CDC_PROCESSING_FAILED"),
            (True, "CDC_QUERY_REVIEW_REQUIRED"),
        ):
            with self.subTest(blocked=blocked):
                value = consumer_fixture()
                value.update(ready=False, blocked=blocked, error_code=code)
                connect = connector_fixture()
                connect["tasks"][0]["state"] = "FAILED"
                self.reply_status = OBSERVER.cdc_status(
                    {"COMPOSE_PROFILES": "demo"}
                )
                self.reply_status.update(
                    configured=True,
                    consumer=OBSERVER.consumer_observation(value),
                    connector=OBSERVER.connector_observation(connect),
                )
                app = self.app()
                app.button(key="sql-observe-cdc").click().run(timeout=10)
                self.assertFalse(app.exception)
                text = "\n".join(item.value for item in app.markdown)
                self.assertIn("ready=false", text)
                self.assertIn("blocked=" + str(blocked).lower(), text)
                self.assertIn("connector稼働状態: RUNNING", text)
                self.assertTrue(any(code in item.value for item in app.error))
                self.assertTrue(
                    any("RUNNING以外" in item.value for item in app.warning)
                )
                self.assertTrue(
                    any("FAILED" in str(item.value) for item in app.dataframe)
                )
                if blocked:
                    self.assertTrue(
                        any("blocked" in item.value for item in app.error)
                    )
                self.assertNotIn("private-trace-fixture", str(app))
                self.assertNotIn("private-worker-fixture", str(app))
                self.assertEqual(len(app.success), 0)

    def test_unknown_lifecycle_does_not_display_raw_diagnostic(self):
        self.reply_status = OBSERVER.cdc_status({"COMPOSE_PROFILES": "demo"})
        self.reply_status["consumer"]["error_code"] = "private-error-secret"
        app = self.app()
        app.button(key="sql-observe-cdc").click().run(timeout=10)
        self.assertFalse(app.exception)
        text = "\n".join(item.value for item in app.markdown)
        self.assertIn("ready=unknown", text)
        self.assertIn("blocked=unknown", text)
        self.assertIn("connector稼働状態: unknown", text)
        self.assertTrue(any("unknown" in item.value for item in app.warning))
        self.assertNotIn("private-error-secret", str(app.markdown))
        self.assertNotIn("private-error-secret", str(app.warning))
        self.assertTrue(all(item.value == "未観測" for item in app.metric))
        self.assertEqual(len(app.success), 0)

    def test_unobserved_counts_and_changed_preview_selection_stay_explicit(
        self,
    ):
        self.reply_status = OBSERVER.cdc_status({"COMPOSE_PROFILES": "demo"})
        app = self.app()
        app.button(key="sql-observe-cdc").click().run(timeout=10)
        self.assertFalse(app.exception)
        self.assertTrue(all(item.value == "未観測" for item in app.metric))
        app.button(key="sql-preview-request").click().run(timeout=10)
        self.assertFalse(app.exception)
        self.assertTrue(
            any("snapshot 123" in text.value for text in app.caption)
        )
        app.number_input(key="sql-preview-limit").set_value(1).run(timeout=10)
        self.assertFalse(
            any("snapshot 123" in text.value for text in app.caption)
        )


if __name__ == "__main__":
    unittest.main()

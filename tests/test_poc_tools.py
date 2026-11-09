"""Check explicit PoC seeds, endpoint boundaries and Spark runtime parity."""

import importlib.util
import io
import json
import os
import re
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]


def module(name):
    """Load a PoC helper without starting its command-line interface."""
    spec = importlib.util.spec_from_file_location(
        "poc_" + name, ROOT / "infra/poc" / (name + ".py")
    )
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


class PocToolsTests(unittest.TestCase):
    """Keep seeded examples small, repeatable and separate from real jobs."""

    def test_existing_vector_collection_is_not_overwritten(self):
        """A repeated seed preserves data edited in the native GUI."""
        seed = module("seed_tools")
        with patch.object(
            seed, "request", return_value={"result": {}}
        ) as call:
            result = seed.seed_vector({"QDRANT_API_KEY": "synthetic" * 4})
        self.assertTrue(result["existing"])
        self.assertEqual(call.call_count, 1)
        self.assertEqual(call.call_args.args[0], "GET")

    def test_manual_lineage_keeps_stable_run_and_explicit_demo_identity(self):
        """A GUI demonstration cannot look like a real medallion job run."""
        seed = module("seed_tools")
        first = seed.lineage_event()
        second = seed.lineage_event()
        self.assertEqual(first["run"], second["run"])
        self.assertEqual(first["job"]["name"], "manual-demo-lineage")
        self.assertEqual(first["job"]["namespace"], "lakehouse-poc")
        self.assertEqual(len(first["inputs"]), 2)
        self.assertEqual(len(first["outputs"]), 1)

    def test_empty_success_body_is_valid_for_lineage(self):
        """Marquez can acknowledge a valid event without a JSON response."""
        seed = module("seed_tools")
        response = io.BytesIO(b"")
        response.status = 201
        opener = Mock()
        opener.open.return_value = response
        with patch.object(seed, "OPENER", opener):
            value = seed.request(
                "POST",
                "http://marquez:5000/api/v1/lineage",
                seed.lineage_event(),
                accepted=(200, 201),
            )
        self.assertIsNone(value)

    def test_seed_never_forwards_keys_to_external_endpoints(self):
        """The seed helper never acts as an arbitrary HTTP proxy."""
        seed = module("seed_tools")
        with patch.object(seed, "OPENER") as opener:
            with self.assertRaises(ValueError):
                seed.request(
                    "GET",
                    "https://company.example/collections",
                    headers={"api-key": "synthetic"},
                )
        opener.open.assert_not_called()

    def test_notebook_and_compute_use_the_same_spark_and_iceberg_jars(self):
        """Interactive and batch work must keep the same runtime."""
        compute = (ROOT / "infra/compute/Dockerfile").read_text()
        notebook = (ROOT / "infra/poc/Dockerfile.notebooks").read_text()
        self.assertEqual(
            re.search(r"(?m)^FROM (.+)$", compute).group(1),
            re.search(r"(?m)^FROM (.+)$", notebook).group(1),
        )
        # Compare both checked JAR hashes, not surrounding Dockerfile text.
        expression = r"(?m)^\s+([0-9a-f]{128})\s*\\?$"
        self.assertEqual(
            re.findall(expression, compute), re.findall(expression, notebook)
        )
        self.assertEqual(len(re.findall(expression, notebook)), 2)

    def test_initial_notebook_contains_no_saved_execution_output(self):
        """Published examples contain code and prose, never runtime secrets."""
        for path in (ROOT / "infra/poc/notebooks").glob("*.ipynb"):
            notebook = json.loads(path.read_text())
            for cell in notebook["cells"]:
                if cell["cell_type"] == "code":
                    self.assertIsNone(cell["execution_count"])
                    self.assertEqual(cell["outputs"], [])


class SparkNotebookTests(unittest.TestCase):
    """Keep remote execution, callback addresses and Python workers aligned."""

    def settings(self, profiles="compute,notebooks"):
        """Return synthetic values without opening network connections."""
        return {
            "COMPOSE_PROFILES": profiles,
            "WAREHOUSE_NAME": "synthetic-warehouse",
            "S3_ACCESS_KEY": "synthetic-access",
            "S3_SECRET_KEY": "synthetic-secret",
        }

    def builder(self, master):
        """Represent initialization while inspecting Spark configuration."""
        context = SimpleNamespace(
            master=master, applicationId="app-synthetic", setLogLevel=Mock()
        )
        spark = SimpleNamespace(sparkContext=context)
        builder = Mock()
        builder.master.return_value = builder
        builder.appName.return_value = builder
        builder.config.return_value = builder
        builder.getOrCreate.return_value = spark
        session = SimpleNamespace(builder=builder)
        modules = {
            "pyspark": SimpleNamespace(),
            "pyspark.sql": SimpleNamespace(SparkSession=session),
        }
        return builder, spark, modules

    def test_compute_disabled_selects_explicit_local_mode(self):
        """An unused remote setting never changes the local-only selection."""
        helper = module("lakehouse_session")
        values = self.settings("notebooks")
        values["SPARK_MASTER_URL"] = "invalid-unused-value"
        actual = helper.execution_settings(values)
        self.assertEqual(actual["master"], "local[2]")
        self.assertEqual(actual["executor_python"], helper.DRIVER_PYTHON)
        self.assertIsNone(actual["master_ui"])
        self.assertIsNone(actual["history_ui"])

    def test_cluster_uses_reachable_driver_and_executor_python(self):
        """Python tasks launch the worker venv, not the driver's local path."""
        helper = module("lakehouse_session")
        master = "spark://spark-master:7077"
        builder, spark, modules = self.builder(master)
        with (
            patch.dict(os.environ, self.settings(), clear=True),
            patch.dict(sys.modules, modules),
            patch.object(helper, "show_session") as display,
        ):
            self.assertIs(helper.spark_session(), spark)
            self.assertEqual(
                os.environ["PYSPARK_PYTHON"], helper.EXECUTOR_PYTHON
            )
        options = dict(call.args for call in builder.config.call_args_list)
        self.assertEqual(options["spark.driver.host"], "jupyter")
        self.assertEqual(options["spark.driver.bindAddress"], "0.0.0.0")
        self.assertEqual(options["spark.driver.port"], "39000")
        self.assertEqual(
            options["spark.pyspark.driver.python"], helper.DRIVER_PYTHON
        )
        self.assertEqual(
            options["spark.pyspark.python"], helper.EXECUTOR_PYTHON
        )
        self.assertEqual(options["spark.eventLog.dir"], helper.EVENT_LOG_DIR)
        display.assert_called_once_with(spark)

    def test_remote_failure_never_retries_on_local(self):
        """An unavailable Master produces an error instead of false success."""
        helper = module("lakehouse_session")
        builder, _, modules = self.builder("spark://spark-master:7077")
        builder.getOrCreate.side_effect = RuntimeError("unavailable")
        with (
            patch.dict(os.environ, self.settings(), clear=True),
            patch.dict(sys.modules, modules),
        ):
            with self.assertRaisesRegex(RuntimeError, "No automatic local"):
                helper.spark_session()
        builder.master.assert_called_once_with("spark://spark-master:7077")
        self.assertEqual(builder.getOrCreate.call_count, 1)

    def test_existing_local_session_cannot_mask_cluster_selection(self):
        """getOrCreate must not silently return an existing local context."""
        helper = module("lakehouse_session")
        _, spark, modules = self.builder("local[2]")
        with (
            patch.dict(os.environ, self.settings(), clear=True),
            patch.dict(sys.modules, modules),
        ):
            with self.assertRaisesRegex(RuntimeError, "another master"):
                helper.spark_session()
        spark.sparkContext.setLogLevel.assert_not_called()

    def test_session_links_include_master_proxy_and_history(self):
        """Users can reach driver UI through the configured Master proxy."""
        helper = module("lakehouse_session")
        _, spark, _ = self.builder("spark://spark-master:7077")
        with patch.dict(os.environ, self.settings(), clear=True):
            info = helper.session_info(spark)
        self.assertEqual(info["mode"], "standalone")
        self.assertTrue(
            info["application_ui"].endswith("/proxy/app-synthetic/")
        )
        self.assertTrue(info["history_ui"].startswith("http://localhost:"))
        self.assertIsNotNone(info["worker_ui"])

    def test_compute_python_dependencies_match_notebook_lock(self):
        """Remote Pandas/Arrow tasks use the same dependency versions."""
        compute = (ROOT / "infra/compute/requirements.lock").read_text()
        notebook = (ROOT / "infra/poc/requirements-notebooks.lock").read_text()
        expression = r"(?m)^([A-Za-z0-9_.-]+)==([^ \\]+)"
        expected = dict(re.findall(expression, notebook))
        actual = dict(re.findall(expression, compute))
        self.assertTrue(actual)
        for name, version in actual.items():
            self.assertEqual(version, expected[name], name)


if __name__ == "__main__":
    unittest.main()

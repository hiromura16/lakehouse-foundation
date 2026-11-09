"""Check explicit PoC seeds, endpoint boundaries and Spark runtime parity."""

import importlib.util
import io
import json
import re
import unittest
from pathlib import Path
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
        notebook = json.loads(
            (ROOT / "infra/poc/notebooks/01-lakehouse-tour.ipynb").read_text()
        )
        for cell in notebook["cells"]:
            if cell["cell_type"] == "code":
                self.assertIsNone(cell["execution_count"])
                self.assertEqual(cell["outputs"], [])


if __name__ == "__main__":
    unittest.main()

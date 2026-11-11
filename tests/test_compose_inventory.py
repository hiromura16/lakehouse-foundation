"""Derive source budgets from synthetic YAML, without Docker/private env."""

import copy
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import yaml

from scripts import compose_inventory as inventory


def write_sources(root, documents=None):
    """Create the registered disposable public-shaped Compose source files."""
    documents = documents or [
        {
            "services": {
                f"synthetic-{index}": {
                    "image": "upstream:synthetic",
                    "restart": "unless-stopped",
                    "mem_limit": "10m",
                }
            }
        }
        for index in range(len(inventory.COMPOSE_FILES))
    ]
    for relative, document in zip(inventory.COMPOSE_FILES, documents):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(document))
    return documents


class MemoryUnitTests(unittest.TestCase):
    """Memory limits are bounded binary units, never observed process usage."""

    def test_binary_suffixes_case_and_fractional_unit(self):
        for text, expected in (
            (1024, 1024),
            ("1024", 1024),
            ("1b", 1),
            ("2k", 2048),
            ("2KB", 2048),
            ("1MiB", 1024**2),
            ("1.5G", 3 * 1024**3 // 2),
            ("0.5 gB", 1024**3 // 2),
            ("1TiB", 1024**4),
        ):
            with self.subTest(text=text):
                self.assertEqual(inventory.memory_bytes(text), expected)

    def test_unlimited_unknown_interpolated_and_nonfinite_rejected(self):
        for value in (
            0,
            -1,
            True,
            None,
            1.5,
            "-1g",
            "unlimited",
            "${LIMIT:-1g}",
            "NaN",
            "Infinity",
            "1.1b",
            "2t",
            "1p",
            "1mBjunk",
            "1bb",
            "1bib",
            "1e5",
        ):
            with (
                self.subTest(value=value),
                self.assertRaises(inventory.InventoryError),
            ):
                inventory.memory_bytes(value)

    def test_memory_limit_forms_must_agree(self):
        service = {
            "mem_limit": "1g",
            "deploy": {"resources": {"limits": {"memory": "1024m"}}},
        }
        self.assertEqual(inventory.service_memory(service), 1024**3)
        service["mem_limit"] = "2g"
        with self.assertRaisesRegex(inventory.InventoryError, "CONFLICTING"):
            inventory.service_memory(service)
        with self.assertRaisesRegex(inventory.InventoryError, "MISSING"):
            inventory.service_memory({})

    def test_replicas_are_bounded_integers(self):
        for value in (True, -1, "1", 129):
            with self.assertRaises(inventory.InventoryError):
                inventory.replicas({"deploy": {"replicas": value}})
        self.assertEqual(inventory.replicas({}), 1)
        self.assertEqual(inventory.replicas({"deploy": {"replicas": 0}}), 0)


class SourceDerivationTests(unittest.TestCase):
    """Exercise overlap, builder ownership, mounts and lifecycle intent."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory(
            prefix="compose-inventory-unit-"
        )
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.documents = write_sources(self.root)

    def rewrite(self):
        write_sources(self.root, self.documents)

    def test_registered_sources_are_public_and_environment_independent(self):
        before = inventory.rendered(self.root)
        with patch.dict(
            os.environ,
            {
                "COMPOSE_PROFILES": "ignored-private-selection",
                "MEM_LIMIT": "999g",
                "COMPOSE_PROJECT_NAME": "ignored-private-project",
            },
        ):
            after = inventory.rendered(self.root)
        self.assertEqual(before, after)
        report = json.loads(before)
        self.assertEqual(report["counts"]["compose_files"], 9)
        self.assertEqual(report["counts"]["services"], 9)
        self.assertEqual(report["counts"]["profiles"], 0)
        self.assertFalse(report["runtime_measured"])
        self.assertEqual(
            report["all_profiles"]["budget"]["resident_bytes"], 90 * 1024**2
        )
        self.assertNotIn(str(self.root), before)

    def test_overlapping_profiles_and_core_are_deduplicated(self):
        shared = self.documents[1]["services"]["synthetic-1"]
        shared["profiles"] = ["demo", "mcp"]
        self.documents[2]["services"]["synthetic-2"]["profiles"] = ["mcp"]
        self.rewrite()
        report = inventory.derive(self.root)
        rows = report["services"]
        combined = inventory.select_services(rows, ["demo", "mcp"])
        self.assertEqual(len(combined), 9)
        self.assertEqual(
            inventory.budget(combined)["resident_bytes"], 90 * 1024**2
        )
        profiles = {row["profile"]: row for row in report["profiles"]}
        self.assertEqual(profiles["core"]["service_count"], 7)
        self.assertEqual(profiles["demo"]["additional_service_count"], 1)
        self.assertEqual(profiles["mcp"]["additional_service_count"], 2)
        # Adding independent profile totals would double-count core/shared.
        summed = sum(
            profiles[name]["budget_including_core"]["resident_bytes"]
            for name in ("demo", "mcp")
        )
        self.assertGreater(
            summed, report["all_profiles"]["budget"]["resident_bytes"]
        )
        with self.assertRaisesRegex(
            inventory.InventoryError, "PROFILE_UNKNOWN"
        ):
            inventory.select_services(rows, ["unknown"])

    def test_local_image_has_one_build_owner_and_distinct_consumers(self):
        image = "${COMPOSE_PROJECT_NAME:-synthetic}-application:local"
        for index in (0, 1, 2):
            self.documents[index]["services"][f"synthetic-{index}"][
                "image"
            ] = image
        self.documents[0]["services"]["synthetic-0"]["build"] = {
            "context": "."
        }
        self.rewrite()
        report = inventory.derive(self.root)
        self.assertEqual(report["counts"]["local_images"], 1)
        self.assertEqual(report["counts"]["build_owners"], 1)
        self.assertEqual(report["counts"]["local_image_consumers"], 2)
        self.assertEqual(report["local_images"][0]["builder"], "synthetic-0")
        self.assertEqual(
            report["local_images"][0]["consumers"],
            ["synthetic-1", "synthetic-2"],
        )
        self.documents[1]["services"]["synthetic-1"]["build"] = {
            "context": "."
        }
        self.rewrite()
        with self.assertRaisesRegex(
            inventory.InventoryError, "OWNER_NOT_UNIQUE"
        ):
            inventory.derive(self.root)

    def test_local_consumer_without_owner_is_not_silently_counted(self):
        self.documents[0]["services"]["synthetic-0"]["image"] = (
            "synthetic:local"
        )
        self.rewrite()
        with self.assertRaisesRegex(
            inventory.InventoryError, "OWNER_NOT_UNIQUE"
        ):
            inventory.derive(self.root)

    def test_volume_declarations_bind_and_anonymous_are_separate(self):
        first = self.documents[0]
        first["volumes"] = {
            "shared-data": {
                "name": "${DATA_VOLUME_PREFIX:-synthetic}_shared-data"
            }
        }
        first["services"]["synthetic-0"]["volumes"] = [
            "shared-data:/data",
            "/synthetic-readonly:/host:ro",
            "/anonymous",
        ]
        self.documents[1]["services"]["synthetic-1"]["volumes"] = [
            {"type": "volume", "source": "shared-data", "target": "/data"}
        ]
        self.rewrite()
        report = inventory.derive(self.root)
        self.assertEqual(report["counts"]["named_volumes"], 1)
        self.assertEqual(
            report["counts"]["named_volume_service_references"], 2
        )
        self.assertEqual(
            report["volumes"][0]["services"], ["synthetic-0", "synthetic-1"]
        )
        first_row = report["services"][0]
        self.assertEqual(first_row["bind_mount_count"], 1)
        self.assertEqual(first_row["anonymous_volume_count"], 1)
        self.assertNotIn("/synthetic-readonly", json.dumps(report))

    def test_lifecycle_budgets_distinguish_no_restart_and_zero_replicas(self):
        initialization = self.documents[0]["services"]["synthetic-0"]
        initialization["restart"] = "no"
        consumer = self.documents[1]["services"]["synthetic-1"]
        consumer["restart"] = "no"
        consumer["healthcheck"] = {"test": ["CMD", "synthetic-health"]}
        consumer["depends_on"] = {
            "synthetic-0": {"condition": "service_completed_successfully"}
        }
        explicit = self.documents[2]["services"]["synthetic-2"]
        explicit["deploy"] = {"replicas": 0}
        self.rewrite()
        report = inventory.derive(self.root)
        rows = {row["service"]: row for row in report["services"]}
        self.assertEqual(rows["synthetic-0"]["lifecycle"], "one_shot")
        self.assertEqual(rows["synthetic-1"]["lifecycle"], "resident")
        self.assertEqual(rows["synthetic-2"]["lifecycle"], "explicit_only")
        total = report["all_profiles"]["budget"]
        self.assertEqual(total["resident_bytes"], 70 * 1024**2)
        self.assertEqual(total["one_shot_bytes"], 10 * 1024**2)
        self.assertEqual(total["explicit_only_bytes"], 10 * 1024**2)
        self.assertEqual(total["explicit_only_instances"], 0)
        self.assertEqual(total["explicit_only_services"], 1)

    def test_restart_no_alone_does_not_misclassify_continuous_consumer(self):
        self.documents[0]["services"]["synthetic-0"]["restart"] = "no"
        self.rewrite()
        report = inventory.derive(self.root)
        self.assertEqual(report["counts"]["unknown_lifecycles"], 1)
        self.assertEqual(
            report["all_profiles"]["budget"]["unknown_bytes"], 10 * 1024**2
        )

    def test_replica_resource_sum_and_repeated_service_rejection(self):
        self.documents[0]["services"]["synthetic-0"]["deploy"] = {
            "replicas": 3
        }
        self.rewrite()
        rows = inventory.derive(self.root)["services"]
        self.assertEqual(
            inventory.budget(rows)["resident_bytes"], 110 * 1024**2
        )
        with self.assertRaisesRegex(inventory.InventoryError, "REPEATED"):
            inventory.budget(rows + [rows[0]])

    def test_duplicate_service_explicit_key_undeclared_volume_fail(self):
        self.documents[1]["services"]["synthetic-0"] = copy.deepcopy(
            self.documents[0]["services"]["synthetic-0"]
        )
        self.rewrite()
        with self.assertRaisesRegex(inventory.InventoryError, "DEFINED_TWICE"):
            inventory.derive(self.root)
        self.documents[1]["services"].pop("synthetic-0")
        self.documents[0]["services"]["synthetic-0"]["volumes"] = [
            "missing:/data"
        ]
        self.rewrite()
        with self.assertRaisesRegex(inventory.InventoryError, "UNDECLARED"):
            inventory.derive(self.root)
        path = self.root / inventory.COMPOSE_FILES[0]
        path.write_text("services: {}\nservices: {}\n")
        with self.assertRaisesRegex(inventory.InventoryError, "DUPLICATE_KEY"):
            inventory.derive(self.root)

    def test_yaml_anchor_overrides_are_valid_not_duplicate_explicit_keys(self):
        first = self.root / inventory.COMPOSE_FILES[0]
        first.write_text("""
x-runtime: &runtime
  image: upstream:synthetic
  restart: unless-stopped
  mem_limit: 10m
services:
  synthetic-0:
    <<: *runtime
    mem_limit: 20m
""")
        report = inventory.derive(self.root)
        self.assertEqual(
            report["services"][0]["memory_limit_bytes"], 20 * 1024**2
        )

    def test_check_detects_comment_only_input_drift_without_mutating_report(
        self,
    ):
        output = self.root / inventory.OUTPUT
        output.parent.mkdir(parents=True)
        original = inventory.rendered(self.root)
        output.write_text(original)
        with (
            patch.object(inventory, "ROOT", self.root),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(inventory.main(["--check"]), 0)
            source = self.root / inventory.COMPOSE_FILES[0]
            source.write_text(source.read_text() + "# changed public source\n")
            self.assertEqual(inventory.main(["--check"]), 1)
        self.assertEqual(output.read_text(), original)

    def test_public_source_symlink_rejected(self):
        source = self.root / inventory.COMPOSE_FILES[0]
        other = self.root / "other-public.yml"
        source.rename(other)
        source.symlink_to(other)
        with self.assertRaisesRegex(inventory.InventoryError, "SYMLINK"):
            inventory.derive(self.root)

    def test_ancestor_source_symlink_rejected(self):
        infra = self.root / "infra"
        moved = self.root / "moved-public-infra"
        infra.rename(moved)
        infra.symlink_to(moved, target_is_directory=True)
        with self.assertRaisesRegex(inventory.InventoryError, "SYMLINK"):
            inventory.derive(self.root)

    def test_output_symlink_rejected_and_unchanged_generation_is_noop(self):
        output = self.root / inventory.OUTPUT
        output.parent.mkdir(parents=True)
        sentinel = self.root / "keep.json"
        sentinel.write_text("unchanged")
        output.symlink_to(sentinel)
        with (
            patch.object(inventory, "ROOT", self.root),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(inventory.main([]), 1)
        self.assertEqual(sentinel.read_text(), "unchanged")
        output.unlink()
        with (
            patch.object(inventory, "ROOT", self.root),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(inventory.main([]), 0)
            before = output.stat().st_mtime_ns
            self.assertEqual(inventory.main([]), 0)
            self.assertEqual(output.stat().st_mtime_ns, before)


class ActualPublicSourcesTests(unittest.TestCase):
    """Keep derivation compatible with current source, not a magic total."""

    def test_current_inventory_has_no_unknown_lifecycle_or_duplicate_ids(self):
        report = inventory.derive()
        names = [row["service"] for row in report["services"]]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(report["counts"]["unknown_lifecycles"], 0)
        self.assertEqual(report["counts"]["core_services"], 6)
        self.assertEqual(set(report["all_profiles"]["services"]), set(names))

    def test_opt_in_host_and_lab_budgets_include_required_profiles(self):
        report = inventory.derive()
        profiles = {row["profile"]: row for row in report["profiles"]}
        self.assertEqual(report["counts"]["profiles"], 14)
        monitoring = set(profiles["monitoring"]["services"])
        host = set(profiles["monitoring-host"]["services"])
        self.assertTrue({"node-exporter", "cadvisor"}.isdisjoint(monitoring))
        self.assertEqual(host - monitoring, {"node-exporter", "cadvisor"})
        self.assertEqual(
            profiles["monitoring-host"]["required_profiles"], ["monitoring"]
        )
        self.assertEqual(
            profiles["sail-lab"]["required_profiles"], ["compute"]
        )
        self.assertEqual(
            profiles["openobserve-lab"]["required_profiles"], ["monitoring"]
        )
        self.assertTrue(
            set(profiles["compute"]["services"])
            <= set(profiles["sail-lab"]["services"])
        )
        self.assertEqual(
            profiles["monitoring"]["additional_budget"]["resident_bytes"],
            1792 * 1024**2,
        )
        self.assertEqual(
            profiles["monitoring-host"]["additional_budget"]["resident_bytes"],
            2176 * 1024**2,
        )


if __name__ == "__main__":
    unittest.main()

"""Exercise source draft boundaries with synthetic metadata and no database."""

import copy
import importlib.util
import json
import re
import unittest
from pathlib import Path
from unittest import mock

try:
    import pydantic
except ModuleNotFoundError as error:
    if (
        error.name != "pydantic"
        or importlib.util.find_spec("pydantic") is not None
    ):
        raise
    raise unittest.SkipTest(
        "Install the hash-locked source-quality tools."
    ) from error

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "onboarding_contract_tests", ROOT / "infra/demo/onboarding_contract.py"
)
CONTRACT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CONTRACT)


def proposal_request():
    """Prepare complete human choices while leaving all review notes unset."""
    discovery = CONTRACT.builtin_discovery()
    table = discovery["tables"][0]
    names = [column["name"] for column in table["columns"]]
    return {
        "discovery": discovery,
        "source_id": "factory-demo",
        "owner_team": "基盤チーム",
        "selections": [
            {
                "schema_name": table["schema_name"],
                "table_name": table["table_name"],
                "included_columns": names,
                "logical_name": "製品一覧",
                "business_purpose": "工場の日次分析に使用",
                "freshness_minutes": 15,
                "delete_handling": "retain",
                "primary_key_confirmed": True,
                "column_classifications": {name: "internal" for name in names},
            }
        ],
    }


def codes(result):
    """Compare stable diagnostic IDs without depending on display wording."""
    return {item["code"] for item in result["blockers"]}


def two_table_request():
    """Select two distinct tables with complete business draft choices."""
    request = proposal_request()
    table = request["discovery"]["tables"][1]
    columns = [column["name"] for column in table["columns"]]
    selection = copy.deepcopy(request["selections"][0])
    selection.update(
        {
            "schema_name": table["schema_name"],
            "table_name": table["table_name"],
            "included_columns": columns,
            "logical_name": "製造指図一覧",
            "column_classifications": {name: "internal" for name in columns},
        }
    )
    request["selections"].append(selection)
    return request


def reviewed_plan(request):
    """Fill hash-bound self-declared notes, never verified approvals."""
    request["review"] = {
        "contract_sha256": CONTRACT.propose(request)["contract_sha256"],
        "business": True,
        "technical": True,
        "dba": True,
        "security": True,
    }
    return CONTRACT.make_plan(request)


class OnboardingMetadataTests(unittest.TestCase):
    """Prove fixture provenance, strict types and bounded JSON transport."""

    def test_synthetic_fixture_exactly_matches_six_public_ddl_tables(self):
        self.assertTrue(issubclass(CONTRACT.Discovery, pydantic.BaseModel))
        ddl = (ROOT / "infra/sqlserver/002_demo_tables.sql").read_text()
        fixture = CONTRACT.builtin_discovery()
        self.assertEqual(len(fixture["tables"]), 6)
        self.assertEqual(fixture["origin"], "synthetic")
        for table in fixture["tables"]:
            identity = (
                "[" + table["schema_name"] + "].[" + table["table_name"] + "]"
            )
            block = ddl.split("CREATE TABLE " + identity + " (", 1)[1]
            block = block.split("\n);", 1)[0]
            expected = [
                (match[0], match[1], match[2] == "NULL")
                for match in re.findall(
                    r"^    \[([^]]+)\] ([a-z0-9(),]+) (NOT NULL|NULL)",
                    block,
                    re.MULTILINE,
                )
            ]
            actual = [
                (column["name"], column["sql_type"], column["nullable"])
                for column in table["columns"]
            ]
            self.assertEqual(actual, expected)
            self.assertIn(
                "PRIMARY KEY ([" + table["primary_key"][0] + "])", block
            )
            self.assertFalse(table["cdc_enabled"])
            self.assertIsNone(table["capture_instance"])

    def test_fixture_preserves_landing_business_column_meaning(self):
        sources = json.loads((ROOT / "medallion/sources.json").read_text())
        fixture = CONTRACT.builtin_discovery()
        for source, table in zip(sources["sources"], fixture["tables"]):
            self.assertTrue(source["table"].endswith(table["table_name"]))
            self.assertEqual(table["primary_key"], source["columns"][:1])
            names = [column["name"] for column in table["columns"]]
            self.assertEqual(
                names,
                source["columns"]
                + ["created_at", "run_id", "original_values"],
            )

    def test_builtin_fixture_and_engine_need_no_files_or_sockets(self):
        with (
            mock.patch.object(Path, "read_text", side_effect=AssertionError),
            mock.patch("socket.socket", side_effect=AssertionError),
        ):
            request = proposal_request()
            result = CONTRACT.make_plan(request)
        self.assertFalse(result["database_checked"])
        self.assertEqual(result["status"], "plan")

    def test_unknown_credentials_or_connection_fields_are_rejected(self):
        for key in ["password", "host", "connection_string", "token", "path"]:
            value = CONTRACT.builtin_discovery()
            value[key] = "synthetic-private-sentinel"
            with (
                self.subTest(field=key),
                self.assertRaises(ValueError) as error,
            ):
                CONTRACT.Discovery.model_validate(value)
            self.assertNotIn(
                "synthetic-private-sentinel", str(error.exception)
            )

    def test_nested_unknown_fields_are_rejected(self):
        for location in ["table", "column"]:
            value = CONTRACT.builtin_discovery()
            target = value["tables"][0]
            if location == "column":
                target = target["columns"][0]
            target["sample_rows"] = ["synthetic-private-sentinel"]
            with (
                self.subTest(location=location),
                self.assertRaises(ValueError),
            ):
                CONTRACT.Discovery.model_validate(value)

    def test_strict_booleans_and_version_reject_python_coercions(self):
        for version in [True, 1.0, "1", 2]:
            value = CONTRACT.builtin_discovery()
            value["schema_version"] = version
            with self.subTest(version=version), self.assertRaises(ValueError):
                CONTRACT.Discovery.model_validate(value)
        for value in [1, 0, "false"]:
            discovery = CONTRACT.builtin_discovery()
            discovery["tables"][0]["columns"][0]["nullable"] = value
            with self.subTest(nullable=value), self.assertRaises(ValueError):
                CONTRACT.Discovery.model_validate(discovery)

    def test_sql_fragments_and_identifier_traversal_are_rejected(self):
        for name in ["dbo.x", "../products", "x;DROP", "products\n", ""]:
            value = CONTRACT.builtin_discovery()
            value["tables"][0]["table_name"] = name
            with self.subTest(name=name), self.assertRaises(ValueError):
                CONTRACT.Discovery.model_validate(value)
        for kind in ["int;DROP TABLE x", "nvarchar(20) NULL", "INT", "int\n"]:
            value = CONTRACT.builtin_discovery()
            value["tables"][0]["columns"][0]["sql_type"] = kind
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                CONTRACT.Discovery.model_validate(value)

    def test_duplicate_tables_columns_keys_are_rejected(self):
        original = CONTRACT.builtin_discovery()
        samples = []
        value = copy.deepcopy(original)
        value["tables"].append(copy.deepcopy(value["tables"][0]))
        samples.append(value)
        value = copy.deepcopy(original)
        value["tables"][0]["columns"].append(value["tables"][0]["columns"][0])
        samples.append(value)
        value = copy.deepcopy(original)
        value["tables"][0]["primary_key"] *= 2
        samples.append(value)
        for value in samples:
            with self.subTest(value=value["tables"][0]["table_name"]):
                with self.assertRaises(ValueError):
                    CONTRACT.Discovery.model_validate(value)

    def test_unknown_primary_key_metadata_is_rejected(self):
        value = CONTRACT.builtin_discovery()
        value["tables"][0]["primary_key"] = ["unknown_key"]
        with self.assertRaises(ValueError):
            CONTRACT.Discovery.model_validate(value)

    def test_aggregate_metadata_and_per_table_bounds(self):
        table = CONTRACT.builtin_discovery()["tables"][0]
        table["columns"] = [
            {"name": "c" + str(index), "sql_type": "int", "nullable": False}
            for index in range(128)
        ]
        table["primary_key"] = ["c0"]
        tables = []
        for index in range(5):
            item = copy.deepcopy(table)
            item["table_name"] = "t" + str(index)
            tables.append(item)
        with self.assertRaises(ValueError):
            CONTRACT.Discovery.model_validate(
                {"origin": "imported", "tables": tables}
            )
        table["columns"].append(
            {"name": "overflow", "sql_type": "int", "nullable": False}
        )
        with self.assertRaises(ValueError):
            CONTRACT.DiscoveredTable.model_validate(table)

    def test_json_bytes_limit_duplicate_keys_utf8_and_nonfinite(self):
        valid = json.dumps(CONTRACT.builtin_discovery()).encode()
        self.assertEqual(CONTRACT.parse_discovery(valid).origin, "synthetic")
        malformed = [
            b"x" * (CONTRACT.MAX_METADATA_BYTES + 1),
            b'{"origin":"synthetic","origin":"imported"}',
            b'{"nested":{"x":1,"x":2}}',
            b"\xff",
            b"{",
            b"NaN",
            b"Infinity",
            b"-Infinity",
            b"[" * 2000,
        ]
        for value in malformed:
            with (
                self.subTest(prefix=value[:30]),
                self.assertRaises(ValueError),
            ):
                CONTRACT.parse_discovery(value)

    def test_proposal_transport_also_refuses_duplicate_keys(self):
        raw = json.dumps(proposal_request())
        self.assertIsInstance(
            CONTRACT.parse_proposal(raw), CONTRACT.ProposalRequest
        )
        with self.assertRaises(ValueError):
            CONTRACT.parse_proposal('{"source_id":"a","source_id":"b"}')


class OnboardingProposalTests(unittest.TestCase):
    """Exercise human choices, warnings and immutable review binding."""

    def test_initial_draft_has_no_verified_permission_or_live_evidence(self):
        result = CONTRACT.propose(proposal_request())
        self.assertEqual(result["status"], "proposed")
        self.assertEqual(
            result["contract"]["api_version"], "lakehouse.source/v1alpha1"
        )
        self.assertEqual(len(result["contract"]["tables"]), 1)
        for key in [
            "applied",
            "can_apply",
            "approvals_verified",
            "database_checked",
        ]:
            self.assertFalse(result[key])
        self.assertFalse(result["review"]["verified"])
        self.assertIn("REVIEW_DBA_PENDING", codes(result))

    def test_all_checkboxes_with_matching_hash_are_only_self_declared_notes(
        self,
    ):
        request = proposal_request()
        draft = CONTRACT.propose(request)
        request["review"] = {
            "contract_sha256": draft["contract_sha256"],
            "business": True,
            "technical": True,
            "dba": True,
            "security": True,
        }
        plan = CONTRACT.make_plan(request)
        self.assertTrue(plan["review"]["hash_matches"])
        self.assertTrue(plan["review"]["dba"])
        self.assertEqual(plan["blockers"], [])
        self.assertFalse(plan["approvals_verified"])
        self.assertFalse(plan["can_apply"])
        self.assertEqual(plan["steps"][2]["status"], "not_implemented")

    def test_changed_business_choice_invalidates_prior_review(self):
        request = proposal_request()
        digest = CONTRACT.propose(request)["contract_sha256"]
        request["review"] = {"contract_sha256": digest, "business": True}
        request["selections"][0]["delete_handling"] = "propagate"
        result = CONTRACT.propose(request)
        self.assertIn("STALE_REVIEW", codes(result))
        self.assertFalse(result["review"]["business"])
        self.assertNotEqual(digest, result["contract_sha256"])

    def test_changed_metadata_even_unselected_table_invalidates_review(self):
        request = proposal_request()
        digest = CONTRACT.propose(request)["contract_sha256"]
        request["review"] = {"contract_sha256": digest, "dba": True}
        request["discovery"]["tables"][1]["columns"][1]["nullable"] = False
        result = CONTRACT.propose(request)
        self.assertIn("STALE_REVIEW", codes(result))
        self.assertNotEqual(result["contract_sha256"], digest)

    def test_even_invalid_selection_changes_bind_hash(self):
        request = proposal_request()
        request["selections"][0]["table_name"] = "missing_a"
        first = CONTRACT.propose(request)
        request["selections"][0]["table_name"] = "missing_b"
        second = CONTRACT.propose(request)
        self.assertIn("UNKNOWN_TABLE", codes(first))
        self.assertNotEqual(
            first["contract_sha256"], second["contract_sha256"]
        )

    def test_hash_is_repeatable_and_dictionary_order_independent(self):
        request = proposal_request()
        first = CONTRACT.propose(request)
        ordered = {key: request[key] for key in reversed(list(request))}
        second = CONTRACT.propose(ordered)
        self.assertEqual(first["contract_sha256"], second["contract_sha256"])
        self.assertEqual(
            first["contract_sha256"],
            CONTRACT.canonical_hash(first["contract"]),
        )

    def test_missing_unknown_excluded_or_nullable_key_blocks(self):
        for mode in ["missing", "excluded", "nullable", "unconfirmed"]:
            request = proposal_request()
            table = request["discovery"]["tables"][0]
            selected = request["selections"][0]
            expected = {
                "missing": "MISSING_PRIMARY_KEY",
                "excluded": "PRIMARY_KEY_EXCLUDED",
                "nullable": "NULLABLE_PRIMARY_KEY",
                "unconfirmed": "PRIMARY_KEY_UNCONFIRMED",
            }[mode]
            if mode == "missing":
                table["primary_key"] = []
            elif mode == "excluded":
                selected["included_columns"].remove(table["primary_key"][0])
            elif mode == "nullable":
                table["columns"][0]["nullable"] = True
            else:
                selected["primary_key_confirmed"] = False
            with self.subTest(mode=mode):
                self.assertIn(expected, codes(CONTRACT.propose(request)))

    def test_unknown_columns_or_classification_targets_block(self):
        request = proposal_request()
        request["selections"][0]["included_columns"].append("absent_column")
        request["selections"][0]["column_classifications"]["absent_column"] = (
            "public"
        )
        result = CONTRACT.propose(request)
        self.assertIn("UNKNOWN_COLUMN", codes(result))
        self.assertIn("UNKNOWN_CLASSIFICATION_COLUMN", codes(result))

    def test_unresolved_business_policy_owner_and_classification_block(self):
        request = proposal_request()
        request["owner_team"] = ""
        request["selections"][0].update(
            {
                "logical_name": "",
                "business_purpose": "",
                "delete_handling": "unreviewed",
                "column_classifications": {},
            }
        )
        result = CONTRACT.propose(request)
        self.assertTrue(
            {
                "OWNER_TEAM_REQUIRED",
                "BUSINESS_CONTEXT_REQUIRED",
                "DELETE_POLICY_UNREVIEWED",
                "COLUMN_CLASSIFICATION_REQUIRED",
            }.issubset(codes(result))
        )

    def test_selected_excluded_classification_blocks(self):
        request = proposal_request()
        request["selections"][0]["column_classifications"]["product_name"] = (
            "excluded"
        )
        self.assertIn(
            "COLUMN_CLASSIFICATION_REQUIRED", codes(CONTRACT.propose(request))
        )

    def test_computed_and_unsupported_types_block_without_guessing_mapping(
        self,
    ):
        for value in [
            "rowversion",
            "decimal(39,2)",
            "decimal(18,20)",
            "datetime2(8)",
            "nvarchar(4001)",
        ]:
            request = proposal_request()
            request["discovery"]["tables"][0]["columns"][1]["sql_type"] = value
            with self.subTest(kind=value):
                self.assertIn(
                    "COLUMN_COMPATIBILITY_REVIEW",
                    codes(CONTRACT.propose(request)),
                )
        request = proposal_request()
        request["discovery"]["tables"][0]["columns"][1]["computed"] = True
        self.assertIn(
            "COLUMN_COMPATIBILITY_REVIEW", codes(CONTRACT.propose(request))
        )

    def test_sensitive_name_is_hint_and_never_final_classification(self):
        request = proposal_request()
        result = CONTRACT.propose(request)
        self.assertIn(
            "SENSITIVE_COLUMN_CANDIDATE",
            {x["code"] for x in result["warnings"]},
        )
        original = next(
            x
            for x in result["contract"]["tables"][0]["columns"]
            if x["name"] == "original_values"
        )
        self.assertEqual(original["classification"], "internal")
        self.assertFalse(result["approvals_verified"])

    def test_existing_capture_metadata_and_unknown_cdc_state_are_not_hidden(
        self,
    ):
        request = proposal_request()
        table = request["discovery"]["tables"][0]
        table["cdc_enabled"] = True
        self.assertIn(
            "CAPTURE_METADATA_INCOMPLETE", codes(CONTRACT.propose(request))
        )
        table["capture_instance"] = "custom_capture_v2"
        result = CONTRACT.propose(request)
        self.assertEqual(
            result["contract"]["tables"][0]["capture_instance"],
            "custom_capture_v2",
        )
        self.assertFalse(result["database_checked"])

    def test_duplicate_selection_is_rejected(self):
        request = proposal_request()
        request["selections"].append(copy.deepcopy(request["selections"][0]))
        with self.assertRaises(ValueError):
            CONTRACT.propose(request)

    def test_duplicate_explicit_capture_names_block_after_review_notes(self):
        request = two_table_request()
        for table in request["discovery"]["tables"][:2]:
            table["capture_instance"] = "shared_capture_v1"
            table["cdc_enabled"] = True
        result = reviewed_plan(request)
        self.assertEqual(codes(result), {"CAPTURE_NAME_COLLISION"})
        for key in [
            "can_apply",
            "applied",
            "database_checked",
            "approvals_verified",
        ]:
            self.assertFalse(result[key])
        self.assertIn(
            "METADATA_UNVERIFIED",
            {item["code"] for item in result["warnings"]},
        )

    def test_generated_capture_names_can_collide_across_different_pairs(self):
        request = two_table_request()
        for index, (schema, table_name) in enumerate(
            [
                ("a_b", "c"),
                ("a", "b_c"),
            ]
        ):
            for target in [
                request["discovery"]["tables"][index],
                request["selections"][index],
            ]:
                target.update(
                    {"schema_name": schema, "table_name": table_name}
                )
        result = reviewed_plan(request)
        self.assertEqual(
            [
                item["capture_instance"]
                for item in result["contract"]["tables"]
            ],
            ["a_b_c_v1", "a_b_c_v1"],
        )
        self.assertEqual(codes(result), {"CAPTURE_NAME_COLLISION"})
        self.assertFalse(result["can_apply"])

    def test_distinct_capture_names_stay_valid_drafts_without_live_evidence(
        self,
    ):
        request = two_table_request()
        request["discovery"]["tables"][0]["capture_instance"] = "products_v2"
        request["discovery"]["tables"][1]["capture_instance"] = "orders_v2"
        result = reviewed_plan(request)
        self.assertEqual(result["blockers"], [])
        self.assertFalse(result["database_checked"])
        self.assertFalse(result["can_apply"])

    def test_explicit_capture_limit_accepts_100_and_rejects_101(self):
        request = proposal_request()
        table = request["discovery"]["tables"][0]
        table["capture_instance"] = "c" * 100
        result = reviewed_plan(request)
        self.assertEqual(result["blockers"], [])
        table["capture_instance"] += "c"
        with self.assertRaises(ValueError):
            CONTRACT.propose(request)

    def test_generated_capture_limit_accepts_100_and_blocks_101(self):
        for schema_length in [48, 49]:
            request = proposal_request()
            for target in [
                request["discovery"]["tables"][0],
                request["selections"][0],
            ]:
                target["schema_name"] = "s" * schema_length
                target["table_name"] = "t" * 48
            result = reviewed_plan(request)
            capture = result["contract"]["tables"][0]["capture_instance"]
            with self.subTest(length=len(capture)):
                self.assertEqual(len(capture), schema_length + 52)
                expected = (
                    {"CAPTURE_NAME_TOO_LONG"} if len(capture) == 101 else set()
                )
                self.assertEqual(codes(result), expected)
                self.assertFalse(result["can_apply"])

    def test_composite_key_order_is_preserved_and_hash_bound(self):
        request = proposal_request()
        table = request["discovery"]["tables"][0]
        table["columns"][1]["nullable"] = False
        table["primary_key"] = ["product_id", "product_name"]
        first = CONTRACT.propose(request)
        self.assertEqual(
            first["contract"]["tables"][0]["primary_key"],
            ["product_id", "product_name"],
        )
        table["primary_key"].reverse()
        second = CONTRACT.propose(request)
        self.assertNotEqual(
            first["contract_sha256"], second["contract_sha256"]
        )

    def test_unknown_approval_flag_and_freshness_coercions_are_rejected(self):
        request = proposal_request()
        request["review"] = {"approved": True}
        with self.assertRaises(ValueError):
            CONTRACT.propose(request)
        request = proposal_request()
        request["selections"][0]["freshness_minutes"] = True
        with self.assertRaises(ValueError):
            CONTRACT.propose(request)

    def test_sql_credentials_and_control_text_do_not_enter_annotations(self):
        for text in [
            "password=synthetic-private-sentinel",
            "PWD=synthetic-private-sentinel",
            "SERVER=example.invalid;DATABASE=example;UID=example",
            "aws_secret_access_key=synthetic-private-sentinel",
            " ".join(["Bearer", "synthetic-private-sentinel"]),
            "https://example.invalid/path",
            "first\nsecond",
            "text\x00end",
        ]:
            request = proposal_request()
            request["selections"][0]["business_purpose"] = text
            with (
                self.subTest(kind=text[:8]),
                self.assertRaises(ValueError) as error,
            ):
                CONTRACT.propose(request)
            self.assertNotIn(
                "synthetic-private-sentinel", str(error.exception)
            )

    def test_runtime_permission_or_apply_fields_are_rejected(self):
        for key in [
            "can_apply",
            "approved",
            "connection",
            "sql",
            "output_path",
        ]:
            request = proposal_request()
            request[key] = True
            with self.subTest(field=key), self.assertRaises(ValueError):
                CONTRACT.make_plan(request)

    def test_forged_proposal_response_cannot_be_submitted_as_plan(self):
        proposal = CONTRACT.propose(proposal_request())
        proposal.update({"can_apply": True, "approvals_verified": True})
        with self.assertRaises(ValueError):
            CONTRACT.make_plan(proposal)


if __name__ == "__main__":
    unittest.main()

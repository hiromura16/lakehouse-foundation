"""Synthetic only; no API, daemon, env, host or database access."""

import copy
import unittest
from dataclasses import replace

from infra.management.planning import Approval, Member, PlanError, plan


def approvals():
    return {
        "foundation": Approval(
            "foundation",
            "foundation-example",
            (
                Member(
                    "catalog-db",
                    "a" * 64,
                    "sha256:" + "1" * 64,
                    "1" * 64,
                    "2" * 64,
                ),
                Member(
                    "catalog",
                    "b" * 64,
                    "sha256:" + "2" * 64,
                    "3" * 64,
                    "4" * 64,
                    ("catalog-db",),
                ),
                Member(
                    "worker",
                    "c" * 64,
                    "sha256:" + "3" * 64,
                    "5" * 64,
                    "6" * 64,
                    ("catalog",),
                ),
            ),
            ("catalog-migrate",),
        ),
        "vision": Approval(
            "vision",
            "vision-example",
            (
                Member(
                    "catalog-db",
                    "d" * 64,
                    "sha256:" + "4" * 64,
                    "7" * 64,
                    "8" * 64,
                ),
            ),
        ),
    }


def inventory(approval, state="running"):
    return [
        {
            "service": m.service,
            "container_id": m.container_id,
            "image_id": m.image_id,
            "mount_identity_digest": m.mount_identity_digest,
            "runtime_identity_digest": m.runtime_identity_digest,
            "project": approval.project,
            "owner": "lakehouse-foundation",
            "kind": "daemon",
            "state": state,
        }
        for m in approval.members
    ]


class WholePlan(unittest.TestCase):
    def setUp(self):
        self.approved = approvals()
        self.current = inventory(self.approved["foundation"])

    def call(self, action="restart", **kwargs):
        return plan(
            self.approved,
            {"poc_id": "foundation", "action": action},
            self.current,
            admission_ready=kwargs.get("admission_ready", True),
            workloads_idle=kwargs.get("workloads_idle", True),
            interruption_ack=kwargs.get("interruption_ack", False),
        )

    def test_full_restart_reverses_dependencies_then_starts(self):
        result = self.call()
        self.assertEqual(
            result["steps"],
            [
                {"service": s, "action": a}
                for a, names in (
                    ("stop", ("worker", "catalog", "catalog-db")),
                    ("start", ("catalog-db", "catalog", "worker")),
                )
                for s in names
            ],
        )
        self.assertFalse(result["can_execute"])
        self.assertFalse(result["automatic_retry"])
        self.assertFalse(result["volume_deletion"])

    def test_bootstrap_is_evidence_only(self):
        result = self.call()
        self.assertEqual(
            result["excluded_completed_bootstraps"], ["catalog-migrate"]
        )
        self.assertFalse(
            any(s["service"] == "catalog-migrate" for s in result["steps"])
        )

    def test_stopped_start_only(self):
        self.current = inventory(self.approved["foundation"], "stopped")
        self.assertEqual(len(self.call("start")["steps"]), 3)

    def test_repeated_start_is_noop(self):
        self.assertEqual(self.call("start")["steps"], [])

    def test_low_memory_does_not_block_safe_stop(self):
        self.assertEqual(
            len(self.call("stop", admission_ready=False)["steps"]), 3
        )
        self.current = inventory(self.approved["foundation"], "stopped")
        with self.assertRaisesRegex(PlanError, "MEMORY_BUDGET"):
            self.call("start", admission_ready=False)

    def test_busy_and_unknown_workload_block(self):
        for state in (False, None, "unknown"):
            with self.subTest(state=state), self.assertRaises(PlanError):
                self.call("stop", workloads_idle=state)

    def test_missing_container_requires_owner_setup(self):
        self.current.pop()
        with self.assertRaisesRegex(PlanError, "INITIAL_SETUP_REQUIRED"):
            self.call()

    def test_default_independent_manager_project_cannot_be_a_poc(self):
        self.approved["foundation"] = replace(
            self.approved["foundation"], project="lakehouse-manager"
        )
        with self.assertRaisesRegex(PlanError, "APPROVAL_BAD"):
            self.call()

    def test_identity_and_owner_drift_rejected(self):
        original = copy.deepcopy(self.current)
        for key, value in (
            ("container_id", "0" * 64),
            ("image_id", "sha256:" + "0" * 64),
            ("mount_identity_digest", "changed"),
            ("runtime_identity_digest", "changed"),
            ("project", "other-example"),
            ("owner", "foreign"),
            ("state", "unknown"),
            ("kind", "oneshot"),
        ):
            self.current = copy.deepcopy(original)
            self.current[0][key] = value
            with self.subTest(key=key), self.assertRaises(PlanError):
                self.call()

    def test_duplicate_and_foreign_member_rejected(self):
        for value in (
            dict(self.current[0]),
            dict(self.current[0], service="foreign"),
        ):
            original = list(self.current)
            self.current.append(value)
            with self.assertRaises(PlanError):
                self.call()
            self.current = original

    def test_env_projection_is_not_accepted(self):
        self.current[0]["Env"] = ["SYNTHETIC=value"]
        with self.assertRaisesRegex(PlanError, "PROJECTION_BAD"):
            self.call()

    def test_public_request_cannot_choose_commands_or_targets(self):
        for request in (
            {"poc_id": "other", "action": "stop"},
            {"poc_id": "foundation", "action": "up"},
            {
                "poc_id": "foundation",
                "action": "stop",
                "container_id": "a" * 64,
            },
            {"poc_id": [], "action": "stop"},
        ):
            with self.subTest(request=request), self.assertRaises(PlanError):
                plan(
                    self.approved,
                    request,
                    self.current,
                    admission_ready=True,
                    workloads_idle=True,
                )

    def test_shared_container_identity_is_not_adopted(self):
        self.approved["vision"] = replace(
            self.approved["vision"],
            members=(self.approved["foundation"].members[0],),
        )
        with self.assertRaisesRegex(PlanError, "PROJECTS_OVERLAP"):
            self.call()

    def test_manager_bootstrap_and_shared_source_are_excluded(self):
        for name in (
            "poc-manager",
            "shared-sqlserver",
            "spark-check",
            "cdc-register",
        ):
            first = replace(
                self.approved["foundation"].members[0], service=name
            )
            original = self.approved["foundation"]
            self.approved["foundation"] = replace(original, members=(first,))
            self.current = inventory(self.approved["foundation"])
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(PlanError, "EXCLUDED_SERVICE"),
            ):
                self.call()
            self.approved["foundation"] = original

    def test_dependency_cycles_block(self):
        members = self.approved["foundation"].members
        self.approved["foundation"] = replace(
            self.approved["foundation"],
            members=(
                replace(members[0], dependencies=("worker",)),
                *members[1:],
            ),
        )
        with self.assertRaisesRegex(PlanError, "DEPENDENCY_CYCLE"):
            self.call()

    def test_input_model_is_unchanged(self):
        before = copy.deepcopy((self.approved, self.current))
        self.call()
        self.assertEqual((self.approved, self.current), before)

    def test_invalid_image_and_digest_shapes_are_not_approvals(self):
        first = self.approved["foundation"].members[0]
        original = self.approved["foundation"]
        for fields in (
            {"image_id": "example:local"},
            {"container_id": "a" * 12},
            {"mount_identity_digest": "not-a-digest"},
            {"runtime_identity_digest": None},
        ):
            self.approved["foundation"] = replace(
                original, members=(replace(first, **fields),)
            )
            with self.subTest(fields=fields), self.assertRaises(PlanError):
                self.call()

    def test_management_project_cannot_be_enrolled(self):
        self.approved["foundation"] = replace(
            self.approved["foundation"], project="lakehouse-control"
        )
        with self.assertRaisesRegex(PlanError, "APPROVAL_BAD"):
            self.call()

    def test_two_logical_targets_cannot_alias_one_project(self):
        self.approved["vision"] = replace(
            self.approved["vision"],
            project=self.approved["foundation"].project,
        )
        with self.assertRaisesRegex(PlanError, "PROJECTS_OVERLAP"):
            self.call()

    def test_writable_store_shared_between_pocs_is_rejected(self):
        for poc_id in ("foundation", "vision"):
            approval = self.approved[poc_id]
            self.approved[poc_id] = replace(
                approval,
                members=(
                    replace(
                        approval.members[0], writable_storage_ids=("f" * 64,)
                    ),
                    *approval.members[1:],
                ),
            )
        with self.assertRaisesRegex(PlanError, "WRITABLE_STORES_OVERLAP"):
            self.call()

    def test_source_reader_restart_policy_does_not_define_lifecycle(self):
        approval = self.approved["vision"]
        self.approved["vision"] = replace(
            approval,
            members=(replace(approval.members[0], service="cdc-consumer"),),
        )
        rows = inventory(self.approved["vision"], "stopped")
        result = plan(
            self.approved,
            {"poc_id": "vision", "action": "start"},
            rows,
            admission_ready=True,
            workloads_idle=True,
        )
        self.assertEqual(
            result["steps"], [{"service": "cdc-consumer", "action": "start"}]
        )

    def test_unknown_bootstrap_claim_is_rejected(self):
        self.approved["foundation"] = replace(
            self.approved["foundation"],
            completed_bootstraps=("arbitrary-import",),
        )
        with self.assertRaisesRegex(PlanError, "APPROVAL_BAD"):
            self.call()

    def test_malformed_observed_state_is_a_fixed_rejection(self):
        self.current[0]["state"] = {}
        with self.assertRaisesRegex(PlanError, "IDENTITY_CHANGED"):
            self.call()

    def test_public_result_contains_no_container_or_image_ids(self):
        result = self.call()
        text = str(result)
        for row in self.current:
            self.assertNotIn(row["container_id"], text)
            self.assertNotIn(row["image_id"], text)
            self.assertNotIn(row["project"], text)

    def test_noop_start_does_not_require_idle_or_memory_admission(self):
        result = self.call("start", admission_ready=False, workloads_idle=None)
        self.assertEqual(result["steps"], [])

    def test_interruption_requires_a_separate_operator_approval(self):
        with self.assertRaisesRegex(PlanError, "WORKLOAD_BUSY"):
            self.call("stop", workloads_idle=False, interruption_ack=True)

    def test_operator_approved_busy_stop_returns_explicit_effect(self):
        self.approved["foundation"] = replace(
            self.approved["foundation"], allow_interruption=True
        )
        result = self.call("stop", workloads_idle=False)
        self.assertTrue(result["requires_interruption_ack"])
        self.assertFalse(result["interruption_acknowledged"])
        self.assertEqual(
            result["effect"], "graceful_stop_interruption_possible"
        )
        self.assertFalse(result["can_execute"])
        self.assertFalse(result["volume_deletion"])

    def test_unknown_jobs_remain_unverified_after_owner_ack(self):
        self.approved["foundation"] = replace(
            self.approved["foundation"], allow_interruption=True
        )
        result = self.call(
            "restart", workloads_idle=None, interruption_ack=True
        )
        self.assertTrue(result["requires_interruption_ack"])
        self.assertTrue(result["interruption_acknowledged"])
        self.assertFalse(result["can_execute"])

    def test_interruption_approval_never_bypasses_identity_or_memory(self):
        self.approved["foundation"] = replace(
            self.approved["foundation"], allow_interruption=True
        )
        with self.assertRaisesRegex(PlanError, "MEMORY_BUDGET"):
            self.call(
                "restart",
                workloads_idle=False,
                interruption_ack=True,
                admission_ready=False,
            )
        self.current[0]["owner"] = "foreign"
        with self.assertRaisesRegex(PlanError, "IDENTITY_CHANGED"):
            self.call("stop", workloads_idle=False, interruption_ack=True)


if __name__ == "__main__":
    unittest.main()

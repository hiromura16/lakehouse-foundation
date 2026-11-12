"""Validate notebook preparation using disposable synthetic directories only.

The filesystem cases run as the current non-root Linux identity and affect
only TemporaryDirectory fixtures. They do not apply ownership to host paths,
exercise sudo, or establish Docker/Windows/target-host acceptance. Credential
switching and denied/unsupported filesystem paths are tested with mocks.
"""

import contextlib
import errno
import importlib.util
import io
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "notebook_workspace_subject", ROOT / "scripts/notebook_workspace.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
LINUX_USER = os.name == "posix" and hasattr(os, "O_PATH") and os.geteuid() != 0


def values(path, **changes):
    """Select only an invented fixture, never read a site.env file."""
    return {
        "COMPOSE_PROFILES": "notebooks",
        "NOTEBOOKS_DIR": str(path),
        "WORKER_UID": str(os.geteuid() or 1000),
        "WORKER_GID": str(os.getegid()),
        **changes,
    }


class SelectionTests(unittest.TestCase):
    """Reject unsafe literals and identities before opening a workspace."""

    def test_profile_off_does_not_require_keys_or_touch_the_filesystem(self):
        with mock.patch.object(MODULE, "walk", side_effect=AssertionError):
            for operation in ({}, {"apply": True}, {"probe": True}):
                result = MODULE.prepare_workspace({}, **operation)
                self.assertFalse(result["enabled"])
                self.assertTrue(result["ready"])
                self.assertEqual(result["action"], "none")
                self.assertEqual(result["probe"], "not_run")

    def test_missing_root_uid_and_non_numeric_identifiers_are_refused(self):
        for key, samples in (
            (
                "WORKER_UID",
                ("", None, "0", "-1", "root", "1.2", "99999999999"),
            ),
            ("WORKER_GID", ("", None, "-1", "group", "1.2", "99999999999")),
        ):
            for value in samples:
                with self.subTest(key=key, kind=type(value).__name__):
                    result = MODULE.inspect_workspace(
                        values("/srv/unit-workspace", **{key: value})
                    )
                    self.assertEqual(result["action"], "blocked")
                    self.assertEqual(
                        result["reason"], "INVALID_WORKSPACE_IDENTITY"
                    )
        _, uid, gid = MODULE.selected(
            values("/srv/unit-workspace", WORKER_UID="1234", WORKER_GID="0"),
            ROOT,
        )
        self.assertEqual((uid, gid), (1234, 0))

    def test_roots_protected_checkout_relative_and_traversal_are_refused(self):
        for path in (
            "/",
            "/srv",
            "/tmp",
            "/etc/unit-notebooks",
            "/proc/unit",
            "/var/lib/docker/unit",
            "/var/lib/postgresql/unit",
            "/var/log/unit",
            "/home/nonroot",
            "/home/nonroot/.ssh/workspace",
            "relative/workspace",
            "/srv/../tmp/workspace",
            "/srv/./workspace",
            "/srv//workspace",
            "/srv/workspace/",
            str(ROOT / "notebooks"),
        ):
            with self.subTest(path_kind=Path(path).name):
                with mock.patch.object(
                    MODULE, "walk", side_effect=AssertionError
                ):
                    result = MODULE.inspect_workspace(values(path))
                self.assertEqual(result["action"], "blocked")

    def test_identity_mode_estimate_never_uses_root_access(self):
        owner = SimpleNamespace(
            st_uid=1234, st_gid=2345, st_mode=stat.S_IFDIR | 0o750
        )
        self.assertTrue(MODULE.mode_access(owner, 1234, 2345, 7))
        self.assertFalse(MODULE.mode_access(owner, 3456, 2345, 2))
        self.assertFalse(MODULE.mode_access(owner, 3456, 4567, 1))

    def test_private_settings_overlap_is_blocked_before_any_walk_or_apply(
        self,
    ):
        """Neither a private root nor either overlap direction is mountable."""
        boundary = Path("/srv/synthetic/private")
        for source in ("control", "env-file"):
            for candidate in (boundary, boundary.parent, boundary / "books"):
                selected = values(candidate)
                options = {}
                if source == "control":
                    selected["CONTROL_SETTINGS_DIR"] = str(boundary)
                else:
                    options["settings_path"] = boundary / "site.env"
                for operation in ({}, {"apply": True}, {"probe": True}):
                    with self.subTest(source=source, operation=operation):
                        with mock.patch.object(
                            MODULE, "walk", side_effect=AssertionError
                        ):
                            result = MODULE.prepare_workspace(
                                selected, **options, **operation
                            )
                        self.assertEqual(result["action"], "blocked")
                        self.assertEqual(
                            result["reason"],
                            "WORKSPACE_OVERLAPS_PRIVATE_SETTINGS",
                        )
                        self.assertFalse(result["mutation_attempted"])

    def test_private_alias_and_checkout_ancestor_are_not_adopted(self):
        """Reject alias ambiguity and a workspace containing the checkout."""
        with tempfile.TemporaryDirectory() as folder:
            parent = Path(folder)
            workspace = parent / "workspace"
            workspace.mkdir()
            alias = parent / "alias"
            alias.symlink_to(workspace, target_is_directory=True)
            with mock.patch.object(MODULE, "walk", side_effect=AssertionError):
                result = MODULE.inspect_workspace(
                    values(workspace, CONTROL_SETTINGS_DIR=str(alias))
                )
            self.assertEqual(result["reason"], "UNSAFE_PRIVATE_SETTINGS_PATH")
        # Use a synthetic checkout so a real home-directory parent does not
        # hit the independent protected-home rule before the overlap rule.
        checkout = Path("/srv/synthetic-checkout/repository")
        with mock.patch.object(MODULE, "walk", side_effect=AssertionError):
            result = MODULE.inspect_workspace(
                values(checkout.parent), root=checkout
            )
        self.assertEqual(result["reason"], "WORKSPACE_INSIDE_CHECKOUT")

    def test_unsafe_parent_ownership_and_write_bits_are_not_repaired(self):
        uid = os.geteuid() or 1000
        for owner, mode in ((uid + 2000, 0o755), (uid, 0o775), (uid, 0o777)):
            metadata = SimpleNamespace(
                st_uid=owner, st_mode=stat.S_IFDIR | mode
            )
            self.assertFalse(
                MODULE.parent_safe(metadata, Path("/srv/unit"), uid)
            )
        sticky = SimpleNamespace(st_uid=0, st_mode=stat.S_IFDIR | 0o1777)
        self.assertTrue(MODULE.parent_safe(sticky, Path("/tmp"), uid))
        self.assertFalse(MODULE.parent_safe(sticky, Path("/srv/shared"), uid))

    def test_operation_flags_require_real_booleans(self):
        for option in ({"apply": 1}, {"probe": "true"}):
            with self.assertRaises(MODULE.WorkspaceError):
                MODULE.prepare_workspace({}, **option)

    def test_internal_child_argument_errors_are_normalized(self):
        """Malformed internal arguments cannot echo private caller text."""
        output = io.StringIO()
        with (
            mock.patch.object(
                MODULE.sys,
                "argv",
                ["workspace", "--probe-child", "private-value", "1", "1", "/"],
            ),
            contextlib.redirect_stdout(output),
        ):
            MODULE.main()
        self.assertEqual(json.loads(output.getvalue())["probe"], "failed")
        self.assertNotIn("private-value", output.getvalue())


@unittest.skipUnless(
    LINUX_USER, "Disposable Linux tests require a non-root UID."
)
class DisposableLinuxWorkspaceTests(unittest.TestCase):
    """Use only a synthetic temporary subtree as its current owner."""

    def setUp(self):
        fixture = tempfile.TemporaryDirectory(prefix="workspace-unit-")
        self.addCleanup(fixture.cleanup)
        self.directory = Path(fixture.name)
        self.target = self.directory / "notebooks"
        self.values = values(self.target)
        original_safe = MODULE.parent_safe

        def synthetic_root(metadata, path, uid):
            # The managed sandbox maps host-root metadata to65534. Model a
            # normal Linux root/tmp owner only in these disposable unit cases;
            # production inspection keeps its strict owner checks unchanged.
            if str(path) in {"/", "/tmp"} and metadata.st_uid == 65534:
                metadata = SimpleNamespace(st_uid=0, st_mode=metadata.st_mode)
            return original_safe(metadata, path, uid)

        patched = mock.patch.object(MODULE, "parent_safe", synthetic_root)
        patched.start()
        self.addCleanup(patched.stop)

    def ready_workspace(self):
        """Create a controlled fixture without invoking any host installer."""
        self.target.mkdir()
        self.target.chmod(MODULE.WORKSPACE_MODE)

    def test_missing_workspace_and_bounded_parents_are_created_once(self):
        target = self.directory / "private" / "nested" / "notebooks"
        selected = values(target)
        planned = MODULE.inspect_workspace(selected)
        self.assertEqual(planned["action"], "create")
        self.assertFalse(target.exists())
        result = MODULE.prepare_workspace(selected, apply=True)
        self.assertTrue(result["ready"])
        self.assertTrue(result["applied"])
        self.assertEqual(result["created_parents"], 2)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o2770)
        self.assertEqual(stat.S_IMODE(target.parent.stat().st_mode), 0o750)
        self.assertEqual(target.stat().st_uid, os.geteuid())
        self.assertEqual(target.stat().st_gid, os.getegid())
        with (
            mock.patch.object(MODULE.os, "fchmod", side_effect=AssertionError),
            mock.patch.object(MODULE.os, "fchown", side_effect=AssertionError),
            mock.patch.object(MODULE.os, "mkdir", side_effect=AssertionError),
        ):
            repeated = MODULE.prepare_workspace(selected, apply=True)
        self.assertFalse(repeated["applied"])
        self.assertEqual(repeated["action"], "none")

    def test_existing_correct_directory_is_ready_but_probe_is_not_run(self):
        self.ready_workspace()
        result = MODULE.inspect_workspace(self.values)
        self.assertTrue(result["ready"])
        self.assertTrue(result["owner_match"])
        self.assertTrue(result["mode_match"])
        self.assertEqual(result["access_estimate"], "estimated_allowed")
        self.assertEqual(result["probe"], "not_run")

    def test_mode_fix_preserves_notebooks_and_other_metadata(self):
        self.target.mkdir(mode=0o755)
        notebook = self.target / "lesson.ipynb"
        original = b'{"cells": [{"source": "synthetic notebook"}]}'
        notebook.write_bytes(original)
        notebook.chmod(0o640)
        before = notebook.stat()
        plan = MODULE.inspect_workspace(self.values)
        self.assertEqual(plan["action"], "fix")
        self.assertFalse(plan["mode_match"])
        result = MODULE.prepare_workspace(self.values, apply=True)
        self.assertEqual(result["before"]["action"], "fix")
        self.assertTrue(result["ready"])
        self.assertEqual(notebook.read_bytes(), original)
        self.assertEqual(notebook.stat().st_ino, before.st_ino)
        self.assertEqual(stat.S_IMODE(notebook.stat().st_mode), 0o640)

    def test_ownership_mismatch_is_reported_without_guessing_a_write_pass(
        self,
    ):
        self.target.mkdir(mode=0o755)
        different = values(self.target, WORKER_GID=str(os.getegid() + 1))
        result = MODULE.inspect_workspace(different)
        self.assertEqual(result["action"], "fix")
        self.assertFalse(result["owner_match"])
        self.assertFalse(result["mode_match"])
        self.assertEqual(result["probe"], "not_run")

    def test_partial_owner_change_then_mode_failure_never_claims_no_change(
        self,
    ):
        self.target.mkdir(mode=0o755)
        different = values(self.target, WORKER_GID=str(os.getegid() + 1))
        with (
            mock.patch.object(MODULE.os, "fchown", return_value=None),
            mock.patch.object(
                MODULE.os, "fchmod", side_effect=PermissionError
            ),
        ):
            result = MODULE.prepare_workspace(different, apply=True)
        self.assertEqual(result["action"], "blocked")
        self.assertTrue(result["mutation_attempted"])
        self.assertTrue(result["applied"])
        self.assertEqual(result["apply_status"], "partial_or_unknown")
        self.assertEqual(result["metadata_changes"], 1)

    def test_first_metadata_syscall_failure_is_explicitly_unknown(self):
        self.target.mkdir(mode=0o755)
        with mock.patch.object(
            MODULE.os, "fchmod", side_effect=PermissionError
        ):
            result = MODULE.prepare_workspace(self.values, apply=True)
        self.assertIsNone(result["applied"])
        self.assertEqual(result["apply_status"], "partial_or_unknown")
        self.assertEqual(result["metadata_changes"], 0)

    def test_partial_parent_creation_is_preserved_and_reported(self):
        target = self.directory / "private" / "nested" / "notebooks"
        original_mkdir = MODULE.os.mkdir

        def fail_later(name, *arguments, **keywords):
            if name == "nested":
                raise PermissionError("synthetic private OS text")
            return original_mkdir(name, *arguments, **keywords)

        with mock.patch.object(MODULE.os, "mkdir", side_effect=fail_later):
            result = MODULE.prepare_workspace(values(target), apply=True)
        self.assertEqual(result["apply_status"], "partial_or_unknown")
        self.assertTrue(result["applied"])
        self.assertEqual(result["created_parents"], 1)
        self.assertTrue((self.directory / "private").is_dir())
        self.assertFalse((self.directory / "private" / "nested").exists())
        self.assertNotIn("private OS", json.dumps(result))

    def test_plan_is_completely_mutation_free(self):
        self.ready_workspace()
        names = (
            "mkdir",
            "fchmod",
            "fchown",
            "write",
            "unlink",
            "chmod",
            "chown",
        )
        with contextlib.ExitStack() as stack:
            for name in names:
                stack.enter_context(
                    mock.patch.object(
                        MODULE.os, name, side_effect=AssertionError
                    )
                )
            stack.enter_context(
                mock.patch.object(
                    MODULE.subprocess, "run", side_effect=AssertionError
                )
            )
            stack.enter_context(
                mock.patch.object(
                    MODULE.os, "access", side_effect=AssertionError
                )
            )
            result = MODULE.prepare_workspace(self.values)
        self.assertEqual(result["probe"], "not_run")
        self.assertFalse(result["applied"])
        self.assertTrue(result["ready"])

    def test_normal_file_symlink_leaf_ancestor_and_dangling_are_refused(self):
        normal = self.directory / "normal"
        normal.write_text("synthetic fixture")
        real = self.directory / "real"
        real.mkdir()
        linked = self.directory / "link"
        linked.symlink_to(real, target_is_directory=True)
        dangling = self.directory / "dangling"
        dangling.symlink_to(
            self.directory / "absent", target_is_directory=True
        )
        for target in (
            normal,
            linked,
            linked / "child",
            dangling,
            dangling / "child",
        ):
            with self.subTest(kind=target.name):
                result = MODULE.prepare_workspace(values(target), apply=True)
                self.assertEqual(result["action"], "blocked")
        self.assertEqual(normal.read_text(), "synthetic fixture")
        self.assertFalse((real / "child").exists())
        self.assertTrue(dangling.is_symlink())

    def test_unsafe_parent_and_too_many_missing_parents_are_not_changed(self):
        self.directory.chmod(0o777)
        result = MODULE.prepare_workspace(self.values, apply=True)
        self.assertEqual(result["reason"], "UNSAFE_WORKSPACE_PARENT")
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o777)
        self.assertFalse(self.target.exists())
        self.directory.chmod(0o700)
        target = self.directory / "a" / "b" / "c" / "d" / "notebooks"
        result = MODULE.prepare_workspace(values(target), apply=True)
        self.assertEqual(result["reason"], "TOO_MANY_MISSING_PARENTS")
        self.assertFalse((self.directory / "a").exists())

    def test_workspace_replacement_after_open_prevents_metadata_mutation(self):
        self.target.mkdir(mode=0o755)
        original_open = MODULE.os.open
        moved = self.directory / "previous"
        replaced = False

        def replace_after_open(path, flags, *args, **kwargs):
            nonlocal replaced
            fd = original_open(path, flags, *args, **kwargs)
            if (
                path == "notebooks"
                and not (flags & os.O_PATH)
                and not replaced
            ):
                self.target.rename(moved)
                self.target.mkdir(mode=0o700)
                replaced = True
            return fd

        with (
            mock.patch.object(
                MODULE.os, "open", side_effect=replace_after_open
            ),
            mock.patch.object(MODULE.os, "fchmod", side_effect=AssertionError),
            mock.patch.object(MODULE.os, "fchown", side_effect=AssertionError),
        ):
            result = MODULE.prepare_workspace(self.values, apply=True)
        self.assertEqual(result["reason"], "WORKSPACE_PATH_CHANGED")
        self.assertEqual(stat.S_IMODE(moved.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o700)

    def test_probe_requires_ready_metadata_and_never_creates_workspace(self):
        with mock.patch.object(
            MODULE, "run_probe", side_effect=AssertionError
        ):
            result = MODULE.prepare_workspace(self.values, probe=True)
        self.assertEqual(
            result["reason"], "WORKSPACE_PROBE_REQUIRES_READY_ROOT"
        )
        self.assertFalse(self.target.exists())

    def test_explicit_same_identity_probe_uses_unnamed_inode_and_closes_it(
        self,
    ):
        if not set(os.getgroups()) <= {os.getegid()}:
            self.skipTest("Current process has extra supplementary groups.")
        self.ready_workspace()
        before = sorted(self.target.iterdir())
        result = MODULE.prepare_workspace(self.values, probe=True)
        if result["probe"] == "unsupported":
            self.skipTest("Disposable filesystem lacks O_TMPFILE support.")
        self.assertEqual(result["probe"], "passed", result)
        self.assertEqual(result["cleanup"], "unlinked_inode_closed")
        self.assertEqual(sorted(self.target.iterdir()), before)
        self.assertFalse(result["applied"])

    def test_probe_unsupported_permission_and_cleanup_failures_are_reported(
        self,
    ):
        self.ready_workspace()
        original_open = MODULE.os.open

        def denied(path, flags, *args, **kwargs):
            if flags & os.O_TMPFILE == os.O_TMPFILE:
                raise OSError(errno.EOPNOTSUPP, "synthetic private OS text")
            return original_open(path, flags, *args, **kwargs)

        with MODULE.walk(self.target) as handle:
            with (
                mock.patch.object(MODULE.os, "open", side_effect=denied),
                mock.patch.object(MODULE.os, "getgroups", return_value=[]),
            ):
                result = MODULE._probe_child(
                    str(self.target),
                    handle.nodes[-1][1],
                    os.geteuid(),
                    os.getegid(),
                )
        self.assertEqual(result["probe"], "unsupported")
        self.assertNotIn("private", json.dumps(result))
        with mock.patch.object(
            MODULE,
            "run_probe",
            return_value={
                "probe": "cleanup_failed",
                "reason": "WORKSPACE_PROBE_CLEANUP_FAILED",
                "cleanup": "close_failed",
            },
        ):
            result = MODULE.prepare_workspace(self.values, probe=True)
        self.assertFalse(result["ready"])
        self.assertEqual(result["action"], "blocked")
        self.assertEqual(result["probe"], "cleanup_failed")

    def test_probe_permission_failure_and_real_close_error_do_not_pass(self):
        self.ready_workspace()
        original_open = MODULE.os.open
        original_close = MODULE.os.close
        probe_fds = set()

        def permission_failure(path, flags, *arguments, **keywords):
            if flags & os.O_TMPFILE == os.O_TMPFILE:
                raise PermissionError("synthetic denied")
            return original_open(path, flags, *arguments, **keywords)

        with MODULE.walk(self.target) as handle:
            with (
                mock.patch.object(
                    MODULE.os, "open", side_effect=permission_failure
                ),
                mock.patch.object(MODULE.os, "getgroups", return_value=[]),
            ):
                result = MODULE._probe_child(
                    str(self.target),
                    handle.nodes[-1][1],
                    os.geteuid(),
                    os.getegid(),
                )
        self.assertEqual(result["probe"], "failed")
        self.assertEqual(result["reason"], "WORKSPACE_PROBE_PERMISSION_FAILED")

        def record_probe(path, flags, *arguments, **keywords):
            fd = original_open(path, flags, *arguments, **keywords)
            if flags & os.O_TMPFILE == os.O_TMPFILE:
                probe_fds.add(fd)
            return fd

        def close_failure(fd):
            original_close(fd)
            if fd in probe_fds:
                probe_fds.remove(fd)
                raise OSError("synthetic close failure after kernel close")

        with MODULE.walk(self.target) as handle:
            with (
                mock.patch.object(MODULE.os, "open", side_effect=record_probe),
                mock.patch.object(
                    MODULE.os, "close", side_effect=close_failure
                ),
                mock.patch.object(MODULE.os, "getgroups", return_value=[]),
            ):
                result = MODULE._probe_child(
                    str(self.target),
                    handle.nodes[-1][1],
                    os.geteuid(),
                    os.getegid(),
                )
        if result["probe"] == "unsupported":
            self.skipTest("Disposable filesystem lacks O_TMPFILE support.")
        self.assertEqual(result["probe"], "cleanup_failed")
        self.assertEqual(result["cleanup"], "close_failed")
        self.assertEqual(list(self.target.iterdir()), [])

    def test_root_child_drops_groups_before_gid_and_uid_with_mock_privileges(
        self,
    ):
        self.ready_workspace()
        calls = []
        uid, gid = os.geteuid(), os.getegid()
        with MODULE.walk(self.target) as handle:
            with (
                mock.patch.object(MODULE.os, "geteuid", side_effect=[0, uid]),
                mock.patch.object(MODULE.os, "getgroups", return_value=[]),
                mock.patch.object(
                    MODULE.os,
                    "setgroups",
                    side_effect=lambda value: calls.append(("groups", value)),
                ),
                mock.patch.object(
                    MODULE.os,
                    "setgid",
                    side_effect=lambda value: calls.append(("gid", value)),
                ),
                mock.patch.object(
                    MODULE.os,
                    "setuid",
                    side_effect=lambda value: calls.append(("uid", value)),
                ),
            ):
                result = MODULE._probe_child(
                    str(self.target), handle.nodes[-1][1], uid, gid
                )
        self.assertEqual(calls, [("groups", []), ("gid", gid), ("uid", uid)])
        self.assertIn(result["probe"], {"passed", "unsupported"})

    def test_extra_groups_cannot_create_a_false_pass(self):
        self.ready_workspace()
        with MODULE.walk(self.target) as handle:
            with (
                mock.patch.object(
                    MODULE.os,
                    "getgroups",
                    return_value=[os.getegid(), os.getegid() + 1],
                ),
                mock.patch.object(
                    MODULE.os, "write", side_effect=AssertionError
                ),
            ):
                result = MODULE._probe_child(
                    str(self.target),
                    handle.nodes[-1][1],
                    os.geteuid(),
                    os.getegid(),
                )
        self.assertEqual(result["probe"], "failed")
        self.assertEqual(
            result["reason"], "WORKSPACE_PROBE_IDENTITY_UNAVAILABLE"
        )

    def test_probe_timeout_reports_failure_without_root_cleanup(self):
        self.ready_workspace()
        with MODULE.walk(self.target) as handle:
            with mock.patch.object(
                MODULE.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired("unit", 5),
            ):
                result = MODULE.run_probe(handle, os.geteuid(), os.getegid())
        self.assertEqual(result["probe"], "failed")
        self.assertEqual(result["cleanup"], "child_exit_closes_unlinked_inode")
        self.assertEqual(list(self.target.iterdir()), [])

    def test_cli_plan_emits_only_safe_metadata_without_private_values(self):
        private = {**self.values, "HTTP_PROXY": "synthetic-private-proxy"}
        output = io.StringIO()
        settings = SimpleNamespace(load=mock.Mock(return_value=private))
        with (
            mock.patch.dict("sys.modules", {"settings": settings}),
            mock.patch.object(
                MODULE.sys,
                "argv",
                ["workspace", "--env-file", "/private/unit.env"],
            ),
            contextlib.redirect_stdout(output),
        ):
            code = MODULE.main()
        self.assertEqual(code, 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["action"], "create")
        self.assertNotIn(str(self.target), output.getvalue())
        self.assertNotIn("synthetic-private-proxy", output.getvalue())
        self.assertFalse(self.target.exists())

    def test_mount_roots_and_unknown_mount_metadata_are_not_mutated(self):
        self.ready_workspace()
        with mock.patch.object(MODULE, "mount_identity", side_effect=[2, 1]):
            result = MODULE.prepare_workspace(self.values, apply=True)
        self.assertEqual(result["reason"], "WORKSPACE_MOUNT_ROOT_REFUSED")
        with mock.patch.object(
            MODULE,
            "mount_identity",
            side_effect=MODULE.WorkspaceError(
                "WORKSPACE_MOUNT_IDENTITY_UNAVAILABLE"
            ),
        ):
            result = MODULE.prepare_workspace(self.values, apply=True)
        self.assertEqual(
            result["reason"], "WORKSPACE_MOUNT_IDENTITY_UNAVAILABLE"
        )
        self.assertFalse(result["mutation_attempted"])

    def test_probe_does_not_inherit_environment_secrets(self):
        self.ready_workspace()
        reply = SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "probe": "passed",
                    "reason": None,
                    "cleanup": "unlinked_inode_closed",
                }
            ),
        )
        with MODULE.walk(self.target) as handle:
            expected_fd = handle.nodes[-1][1]
            with mock.patch.object(
                MODULE.subprocess, "run", return_value=reply
            ) as run:
                MODULE.run_probe(handle, os.geteuid(), os.getegid())
        arguments = run.call_args.kwargs
        self.assertEqual(arguments["env"], {"PATH": os.defpath})
        self.assertEqual(arguments["timeout"], MODULE.PROBE_TIMEOUT)
        self.assertEqual(arguments["pass_fds"], (expected_fd,))


if __name__ == "__main__":
    unittest.main()

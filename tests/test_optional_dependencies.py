"""Exercise real import guards without installing or importing packages."""

import ast
import builtins
import importlib.util
import unittest
from pathlib import Path
from types import ModuleType
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
GUARDS = (
    ("cdc_fixtures.py", "pyarrow"),
    ("test_cdc_fixed_source.py", "pyarrow"),
    ("test_fixed_sql_api.py", "fastapi"),
    ("test_sqlserver_contract.py", "sqlfluff"),
    ("test_cdc_collation.py", "sqlfluff"),
    ("test_collation_fixture.py", "sqlfluff"),
    ("test_permissions_recovery.py", "sqlfluff"),
    ("test_sqlsource_client.py", "httpx"),
    ("test_sqlsource_observer.py", "httpx"),
    ("test_sqlsource_observer.py", "fastapi"),
)


def guard_code(filename, package):
    """Compile the actual top-level guarded import, excluding app code."""
    path = ROOT / "tests" / filename
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in tree.body:
        if not isinstance(node, ast.Try):
            continue
        for statement in node.body:
            if isinstance(statement, ast.Import):
                names = [alias.name for alias in statement.names]
            elif isinstance(statement, ast.ImportFrom):
                names = [statement.module or ""]
            else:
                continue
            if any(name.split(".")[0] == package for name in names):
                fragment = ast.Module(body=[node], type_ignores=[])
                return compile(fragment, str(path), "exec")
    raise AssertionError(f"Missing optional import guard: {filename}")


def execute_guard(filename, package, failure=None):
    """Inject one optional import outcome with no network or app imports."""

    def optional_import(name, globals=None, locals=None, fromlist=(), level=0):
        """Supply a synthetic module or raise the original import failure."""
        if name.split(".")[0] != package:
            raise AssertionError("Guard attempted an unrelated import.")
        if failure is not None:
            raise failure
        module = ModuleType(name)
        for field in fromlist or ():
            setattr(module, field, object())
        return module

    namespace = {
        "__builtins__": {**vars(builtins), "__import__": optional_import},
        "importlib": importlib,
        "unittest": unittest,
    }
    exec(guard_code(filename, package), namespace)


class OptionalDependencyTests(unittest.TestCase):
    """Skip absent packages while retaining installed/transitive errors."""

    def test_genuinely_absent_packages_skip_with_uv_lock_guidance(self):
        for filename, package in GUARDS:
            failure = ModuleNotFoundError("synthetic missing", name=package)
            with self.subTest(file=filename, package=package):
                with mock.patch.object(
                    importlib.util, "find_spec", return_value=None
                ) as finder:
                    with self.assertRaises(unittest.SkipTest) as caught:
                        execute_guard(filename, package, failure)
                finder.assert_called_once_with(package)
                self.assertIs(caught.exception.__cause__, failure)
                self.assertIn(
                    "uv pip sync --require-hashes requirements-dev.lock",
                    str(caught.exception),
                )

    def test_installed_but_broken_package_remains_the_original_error(self):
        for filename, package in GUARDS:
            failure = ModuleNotFoundError("synthetic broken", name=package)
            with self.subTest(file=filename, package=package):
                with mock.patch.object(
                    importlib.util, "find_spec", return_value=object()
                ) as finder:
                    with self.assertRaises(ModuleNotFoundError) as caught:
                        execute_guard(filename, package, failure)
                finder.assert_called_once_with(package)
                self.assertIs(caught.exception, failure)

    def test_missing_transitive_dependency_is_never_skipped(self):
        for filename, package in GUARDS:
            for missing in ("synthetic_transitive", package + ".broken"):
                failure = ModuleNotFoundError("synthetic", name=missing)
                with self.subTest(file=filename, missing=missing):
                    with mock.patch.object(
                        importlib.util, "find_spec", return_value=None
                    ) as finder:
                        with self.assertRaises(ModuleNotFoundError) as caught:
                            execute_guard(filename, package, failure)
                    finder.assert_not_called()
                    self.assertIs(caught.exception, failure)

    def test_other_import_failures_remain_errors(self):
        for filename, package in GUARDS:
            failure = ImportError("synthetic incompatible binary")
            with self.subTest(file=filename, package=package):
                with mock.patch.object(importlib.util, "find_spec") as finder:
                    with self.assertRaises(ImportError) as caught:
                        execute_guard(filename, package, failure)
                finder.assert_not_called()
                self.assertIs(caught.exception, failure)

    def test_successful_import_never_enters_skip_detection(self):
        for filename, package in GUARDS:
            with self.subTest(file=filename, package=package):
                with mock.patch.object(importlib.util, "find_spec") as finder:
                    execute_guard(filename, package)
                finder.assert_not_called()


if __name__ == "__main__":
    unittest.main()

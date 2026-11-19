"""合成ファイルだけでCI静的検査の対象範囲・失敗伝播を確認する。"""

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import check_ci_sources as checker


class SourceChecks(unittest.TestCase):
    """未追跡・隠しpath・空白名を見落とさずshell自体は実行しない。"""

    def test_all_yaml_shell_and_only_git_exclusion(self):
        """.githubを含めた全階層を対象にし、.gitだけを除く。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            names = (
                ".yamllint.yml",
                ".github/workflows/example.yaml",
                "space directory/check me.sh",
                "nested/.hidden/config.yml",
                ".git/ignored.yaml",
                "README.md",
            )
            for name in names:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("synthetic")
            with patch.object(checker.subprocess, "run") as run:
                checker.check(root)
            yaml_call, shell_call = run.call_args_list
            self.assertEqual(
                set(yaml_call.args[0][5:]),
                {
                    ".yamllint.yml",
                    ".github/workflows/example.yaml",
                    "nested/.hidden/config.yml",
                },
            )
            self.assertEqual(
                shell_call.args[0],
                ["shellcheck", "--", "space directory/check me.sh"],
            )
            self.assertTrue(yaml_call.kwargs["check"])
            self.assertEqual(shell_call.kwargs["cwd"], root)

    def test_failure_and_missing_tool_are_not_success(self):
        """lint失敗・実行ファイル欠落を隠さず非ゼロ終了にする。"""
        for error in (
            subprocess.CalledProcessError(1, ["yamllint"]),
            FileNotFoundError("synthetic missing tool"),
            ValueError("SOURCE_COVERAGE_EMPTY"),
        ):
            with self.subTest(error=type(error).__name__):
                with patch.object(checker, "check", side_effect=error):
                    self.assertEqual(checker.main(), 1)

    def test_lint_failure_propagates_from_runner(self):
        """外部lintが失敗した場合に次工程や成功表示へ進まない。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "example.yml").write_text("valid: true")
            (root / "example.sh").write_text("#!/bin/sh\nexit 0\n")
            with patch.object(
                checker.subprocess,
                "run",
                side_effect=subprocess.CalledProcessError(1, ["yamllint"]),
            ) as run:
                with self.assertRaises(subprocess.CalledProcessError):
                    checker.check(root)
                self.assertEqual(run.call_count, 1)

    def test_walk_error_is_not_silently_ignored(self):
        """読取不能な階層を全走査成功と扱わない。"""

        def fail_walk(*args, **kwargs):
            kwargs["onerror"](PermissionError("synthetic unreadable"))

        with patch.object(checker.os, "walk", side_effect=fail_walk):
            with self.assertRaises(PermissionError):
                checker.source_paths(Path("."))

    def test_symlink_cannot_hide_unscanned_sources(self):
        """repo外のディレクトリを辿らず、見落とした成功も返さない。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "linked").symlink_to(root, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "DIRECTORY_SYMLINK"):
                checker.source_paths(root)

    def test_yaml_config_and_workflow_keep_narrow_policy(self):
        """3規則だけ無効化し、履歴範囲・redact・既存Compose検査を保持。"""
        import yaml

        root = Path(__file__).resolve().parents[1]
        config = yaml.safe_load((root / ".yamllint.yml").read_text())
        self.assertEqual(config["extends"], "default")
        self.assertEqual(
            config["rules"],
            {
                "document-start": "disable",
                "line-length": "disable",
                "indentation": "disable",
            },
        )
        workflow = (root / ".github/workflows/validate.yml").read_text()
        self.assertIn("on:  # yamllint disable-line rule:truthy", workflow)
        self.assertIn("fetch-depth: 0", workflow)
        self.assertIn("--redact=100 --no-banner --log-opts=HEAD", workflow)
        self.assertNotIn("--report-path", workflow)
        self.assertNotIn("pull_request_target", workflow)
        for name in (
            "demo",
            "sail",
            "cdc",
            "openobserve",
            "lakehouse",
            "compute",
        ):
            self.assertIn(f"-f infra/{name}/compose.yaml", workflow)
        self.assertIn("config --quiet", workflow)


if __name__ == "__main__":
    unittest.main()

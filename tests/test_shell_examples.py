"""公開shell例の抽出・非互換検出・非実行の契約を検証する。"""

import contextlib
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/shell_examples.py"
SPEC = importlib.util.spec_from_file_location("shell_examples", SCRIPT)
SHELL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SHELL)
HAS_SHELLS = bool(shutil.which("bash") and shutil.which("zsh"))


class ShellExtractionTests(unittest.TestCase):
    """言語名だけで判定せず、明示ラベルとfenceの境界を守る。"""

    def test_variable_fences_and_explicit_label_routing(self):
        text = (
            "**Bash / Zsh共通**\n\n~~~~shell\nprintf '%s' common\n~~~~~~\n"
            "**Zsh**\n\n```bash\nsetopt INTERACTIVE_COMMENTS\n```\n"
            "**Bash**\n\n   ```sh\nmapfile -t VALUES\n   ```\n"
        )
        examples = SHELL.extract_examples(text, "guide.md")
        self.assertEqual(
            [example.shells for example in examples],
            [("bash", "zsh"), ("zsh",), ("bash",)],
        )
        self.assertEqual([example.line for example in examples], [3, 8, 13])
        self.assertEqual(examples[1].language, "bash")

    def test_non_shell_outer_fences_hide_nested_examples(self):
        text = (
            "````markdown\n**Bash**\n```bash\nexit 1\n```\n````\n"
            "~~~shellfish\nread -p fake\n~~~\n"
            "```bash```\n"
            "**Bash / Zsh共通**\n```sh\nprintf '%s' visible\n```\n"
        )
        examples = SHELL.extract_examples(text, "guide.md")
        self.assertEqual(len(examples), 1)
        self.assertIn("visible", examples[0].code)

    def test_wrong_or_short_fence_cannot_close_an_example(self):
        text = "**Bash**\n~~~~bash\n```\n~~~\n~~~~\n"
        examples = SHELL.extract_examples(text, "guide.md")
        self.assertEqual(examples[0].code, "```\n~~~\n")

    def test_every_shell_language_requires_an_immediate_label(self):
        for language in ("bash", "sh", "shell", "zsh"):
            with self.subTest(language=language):
                with self.assertRaisesRegex(ValueError, "guide.md:1"):
                    SHELL.extract_examples(
                        f"```{language}\nprintf ok\n```\n", "guide.md"
                    )
        stale = "**Bash**\nOther prose.\n```bash\nprintf ok\n```\n"
        with self.assertRaisesRegex(ValueError, "直前"):
            SHELL.extract_examples(stale, "guide.md")

    def test_labels_cannot_leak_from_a_previous_block(self):
        text = "**Bash**\n```bash\nprintf ok\n```\n```bash\nprintf ok\n```\n"
        with self.assertRaisesRegex(ValueError, "guide.md:5"):
            SHELL.extract_examples(text, "guide.md")

    def test_an_unclosed_shell_fence_is_actionable(self):
        with self.assertRaisesRegex(ValueError, "fenceを閉じ"):
            SHELL.extract_examples("**Bash**\n```bash\nprintf ok\n", "x.md")


class CommonSemanticsTests(unittest.TestCase):
    """構文上は通るread等を、共通例の成功として扱わない。"""

    def test_obvious_nonportable_common_operations_are_rejected(self):
        cases = [
            "read -p 'Name: ' VALUE\n",
            "read -rp 'Name: ' VALUE\n",
            "read -r -p 'Name: ' VALUE\n",
            "read -r 'VALUE?Name: '\n",
            'printf "%s" "${BASH_SOURCE[0]}"\n',
            "printf '%s' $BASH_SOURCE\n",
            "mapfile -t VALUES\n",
            "readarray -t VALUES\n",
            "status=0\n",
            "export status=0\n",
            "path=/tmp/fixture\n",
            "setopt INTERACTIVE_COMMENTS\n",
        ]
        for code in cases:
            with self.subTest(code=code):
                self.assertTrue(SHELL.common_incompatibilities(code))

    def test_python_heredocs_quotes_and_comments_are_not_shell_commands(self):
        code = (
            "python3 - <<'PYTHON'\nstatus=0\nmapfile = 'data'\n"
            "print('$BASH_SOURCE')\nPYTHON\n"
            "python3 -c '\nstatus=1\nprint(\"read -rp\")\n'\n"
            "cat <<'FIRST' <<-SECOND\nread -p data\nFIRST\n"
            "\tstatus=1\n\tSECOND\n"
            "# read -p fake; status=2; $BASH_SOURCE\n"
            "printf '%s' '$BASH_SOURCE'\n"
            'printf "%s" "\\$BASH_SOURCE"\n'
            "printf '%s' 'status=2 read -p example'\n"
            "echo mapfile readarray status=2\n"
        )
        self.assertEqual(SHELL.common_incompatibilities(code), [])

    def test_real_shell_after_a_heredoc_is_still_checked(self):
        code = "cat <<'PY'\nstatus=0\nPY\nread -rp 'Name: ' VALUE\n"
        self.assertEqual(len(SHELL.common_incompatibilities(code)), 1)

    def test_compatible_read_and_quoted_function_arguments_are_allowed(self):
        code = 'read -r VALUE\nhelper() { printf "%s" "$@"; }\n'
        self.assertEqual(SHELL.common_incompatibilities(code), [])


class ShellCheckTests(unittest.TestCase):
    """一時Git fixtureだけを使い、cwd・分岐・非書込みを確かめる。"""

    def setUp(self):
        """実サービスやprivate設定を持たないGitルートを準備する。"""
        temporary = tempfile.TemporaryDirectory(prefix="shell-examples-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)

    def write(self, name, body):
        """fixture内の文書だけを明示して作る。"""
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")

    def test_git_discovery_handles_spaces_ignores_and_changed_cwd(self):
        self.write("README.md", "# Root\n")
        self.write("docs/space name.md", "# New\n")
        self.write(".gitignore", "ignored/\n")
        self.write("ignored/private.md", "# Ignored\n")
        subprocess.run(
            ["git", "-C", str(self.root), "add", "README.md"], check=True
        )
        previous = Path.cwd()
        try:
            os.chdir(self.root.parent)
            self.assertEqual(
                SHELL.markdown_paths(self.root),
                ["README.md", "docs/space name.md"],
            )
        finally:
            os.chdir(previous)

    def test_explicit_labels_choose_the_expected_checkers(self):
        self.write(
            "README.md",
            (
                "**Bash / Zsh共通**\n```bash\nprintf ok\n```\n"
                "**Bash**\n```sh\nread -rp 'Name: ' VALUE\n```\n"
                "**Zsh**\n```zsh\nsetopt INTERACTIVE_COMMENTS\n```\n"
            ),
        )
        with (
            mock.patch.object(
                SHELL,
                "shell_executables",
                return_value={
                    "bash": "/example/bash",
                    "zsh": "/example/zsh",
                },
            ),
            mock.patch.object(
                SHELL, "syntax_error", return_value=None
            ) as check,
        ):
            statistics, errors = SHELL.check_examples(self.root)
        self.assertEqual(errors, [])
        self.assertEqual(statistics, {"documents": 1, "examples": 3})
        self.assertEqual(
            [call.args[1] for call in check.call_args_list],
            ["bash", "zsh", "bash", "zsh"],
        )

    def test_missing_either_shell_is_an_error(self):
        for missing in ("bash", "zsh"):
            with self.subTest(missing=missing):
                with mock.patch.object(
                    SHELL.shutil,
                    "which",
                    side_effect=lambda name: None if name == missing else name,
                ):
                    with self.assertRaisesRegex(ValueError, missing):
                        SHELL.shell_executables()

    def test_syntax_argv_cannot_execute_a_snippet_or_startup_hook(self):
        example = SHELL.ShellExample(
            "guide.md", 3, "bash", ("zsh",), "printf value > marker\n"
        )
        result = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.dict(os.environ, {"BASH_ENV": "/never/source"}):
            with mock.patch.object(
                SHELL.subprocess, "run", return_value=result
            ) as run:
                self.assertIsNone(
                    SHELL.syntax_error(
                        example, "zsh", "/example/zsh", self.root
                    )
                )
        self.assertEqual(run.call_args.args[0], ["/example/zsh", "-f", "-n"])
        self.assertEqual(run.call_args.kwargs["input"], example.code)
        self.assertNotIn("BASH_ENV", run.call_args.kwargs["env"])
        self.assertNotIn("shell", run.call_args.kwargs)

    @unittest.skipUnless(HAS_SHELLS, "Bash/Zshの実行試験には両shellが必要")
    def test_cli_default_and_check_from_other_cwd_never_write(self):
        self.write(
            "README.md",
            (
                "**Bash / Zsh共通**\n```bash\n"
                "printf value > written-if-executed\n"
                "printf '%s' \"$(touch substitution-if-executed)\"\n```\n"
            ),
        )
        before = {
            path.relative_to(self.root): path.stat().st_mtime_ns
            for path in self.root.rglob("*")
            if path.is_file()
        }
        for flags in ([], ["--check"]):
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--root",
                    str(self.root),
                    *flags,
                ],
                cwd=self.root.parent,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(
                result.returncode, 0, result.stdout + result.stderr
            )
            self.assertIn("1 shell examples", result.stdout)
        after = {
            path.relative_to(self.root): path.stat().st_mtime_ns
            for path in self.root.rglob("*")
            if path.is_file()
        }
        self.assertEqual(before, after)

    @unittest.skipUnless(HAS_SHELLS, "Bash/Zshの実行試験には両shellが必要")
    def test_broken_common_read_fails_without_waiting_for_input(self):
        self.write(
            "README.md",
            ("**Bash / Zsh共通**\n```bash\nread -rp 'Name: ' VALUE\n```\n"),
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = SHELL.main(["--root", str(self.root), "--check"])
        self.assertEqual(result, 1)
        self.assertIn("readの-p", output.getvalue())
        self.assertNotIn("Traceback", output.getvalue())

    @unittest.skipUnless(HAS_SHELLS, "Bash/Zshの実行試験には両shellが必要")
    def test_real_shell_syntax_failure_names_document_and_shell(self):
        self.write("README.md", "**Bash**\n```bash\nif true; then\n```\n")
        statistics, errors = SHELL.check_examples(self.root)
        self.assertEqual(statistics["examples"], 1)
        self.assertIn("README.md:2: bash:", errors[0])


@unittest.skipUnless(HAS_SHELLS, "Bash/Zshの実行試験には両shellが必要")
class InteractiveShellTests(unittest.TestCase):
    """構文検査とは別に、固定された無害な対話例だけを実行する。"""

    def test_shell_specific_read_prompts_preserve_backslashes_and_spaces(self):
        value = "example\\literal with spaces"
        snippets = {
            "bash": "read -r -p 'Value: ' VALUE\nprintf '%s' \"$VALUE\"\n",
            "zsh": "read -r 'VALUE?Value: '\nprintf '%s' \"$VALUE\"\n",
        }
        for shell, code in snippets.items():
            with self.subTest(shell=shell):
                flags = ["-f"] if shell == "zsh" else ["--noprofile", "--norc"]
                result = subprocess.run(
                    [shell, *flags, "-c", code],
                    input=value + "\n",
                    text=True,
                    capture_output=True,
                    timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, value)

    def test_interactive_zsh_comments_work_after_the_explicit_preparation(
        self,
    ):
        code = (
            "setopt INTERACTIVE_COMMENTS\n"
            "# この行を対話端末へ貼ってもコマンドにしない\n"
            "[[ -o interactivecomments ]] || exit 9\n"
            "printf '%s' ready\n"
        )
        result = subprocess.run(
            ["zsh", "-f", "-i", "+m", "-c", code],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "ready")
        self.assertNotIn("command not found: #", result.stderr)


if __name__ == "__main__":
    unittest.main()

"""Dockerなしで hosted シェル本体の所有確認とモード分岐を検証。"""

import subprocess
import tempfile
import unittest
from pathlib import Path

SH = Path(__file__).with_name("hosted.sh")
SETUP = r"""
scratch=$sandbox
id=fixture-proof
net=owned-network
label=fixture-label
broker=$(printf '%064d' 1)
sql=$(printf '%064d' 2)
connect=$(printf '%064d' 3)
client=$(printf '%064d' 4)
mkdir "$scratch/restart-state"
state=original
mark() { printf '%s\n' "$1" >> "$scratch/marks"; }
d() {
    if [[ $1 == start && $2 == "$connect" ]]; then
        [[ $state == stopped ]] || return 1
        mark START
        state=restarted
        return 0
    fi
    [[ $1 == container && $2 == inspect && $3 == --format ]] || return 99
    case "$4" in
        fixture-label)
            printf '%s\n' "$fixture_label"
            ;;
        '{{len .HostConfig.PortBindings}} {{.HostConfig.Privileged}}')
            printf '0 false\n' ;;
        '{{.HostConfig.NetworkMode}}') printf '%s\n' "$net" ;;
        '{{range $key, $value := .NetworkSettings.Networks}}{{$key}}{{end}}')
            printf '%s\n' "$id" ;;
        '{{range .NetworkSettings.Networks}}{{.NetworkID}}{{end}}')
            printf '%s\n' "$net" ;;
        '{{.State.StartedAt}}') printf '%s\n' "$state" ;;
        '{{.State.Running}}')
            if [[ $state == stopped ]]; then printf 'false\n';
            else printf 'true\n'; fi ;;
        *) return 99 ;;
    esac
}
dt() {
    [[ $# == 5 && $1 == 45s && $2 == stop && $3 == -t &&
       $4 == 30 && $5 == "$connect" && $state == original ]] || return 98
    mark STOP
    state=stopped
}
run_client() {
    mark "$1"
    if [[ $1 == SQL_SOURCE_RESTART_BEFORE=pass ]]; then
        [[ $state == original ]]
        printf '{}\n' > "$scratch/restart-state/restart.json"
    elif [[ $1 == SQL_SOURCE_RESTART=pass* ]]; then
        [[ $state == restarted ]]
    elif [[ $1 == SQL_SOURCE_KAFKA=pass* ]]; then
        [[ $state == original ]]
    else
        return 97
    fi
}
"""


class HostedModeTest(unittest.TestCase):
    """実行する小区間だけを抽出し、外部コマンドを無害なstubで置換。"""

    def select(self, mode, *, owned=True):
        """既存SSH/Dockerに触れず Bash 本体の制御入力だけを動かす。"""
        shell = SH.read_text(encoding="utf-8")
        begin = shell.index('for cid in "$broker" "$sql" "$connect" "$client"')
        end = shell.index("stage=LOCAL_BROKER_AND_SQL", begin)
        owner_checks = shell[begin:end]
        begin = shell.index("run_selected_fixture() {")
        end = shell.index("stage=CLEANUP", begin)
        selected = shell[begin:end]
        begin = shell.index("readonly fixture_mode=")
        end = shell.index("scratch=$(mktemp", begin)
        selector = shell[begin:end]
        with tempfile.TemporaryDirectory(prefix="hosted-mode-model-") as temp:
            marks = Path(temp) / "marks"
            fixture_label = "fixture-proof" if owned else "different-fixture"
            script = (
                'set -euo pipefail\nsandbox=$2\nset -- "$1"\n'
                + f"fixture_label={fixture_label}\n"
                + selector
                + SETUP
                + owner_checks
                + selected
            )
            result = subprocess.run(
                ["bash", "-c", script, "bash-model", mode, temp],
                capture_output=True,
                check=False,
                text=True,
                timeout=10,
            )
            return (
                result.returncode,
                marks.read_text().splitlines() if marks.exists() else [],
            )

    def test_restart_routes_after_owned_network_and_two_markers(self):
        """途中でDockerのネットワーク値が選択モードを上書きしない。"""
        self.assertEqual(
            self.select("restart"),
            (
                0,
                [
                    "SQL_SOURCE_RESTART_BEFORE=pass",
                    "STOP",
                    "START",
                    "SQL_SOURCE_RESTART=pass snapshot=1 insert=1 update=1 "
                    "delete=1 tombstone=1",
                ],
            ),
        )

    def test_plain_source_has_no_restart(self):
        """通常試験では停止・再起動や先行レシートを要求しない。"""
        self.assertEqual(
            self.select("source"),
            (
                0,
                [
                    "SQL_SOURCE_KAFKA=pass snapshot=1 insert=1 update=1 "
                    "delete=1 tombstone=1"
                ],
            ),
        )

    def test_nonowned_container_cannot_reach_client_or_stop(self):
        """いずれのmodeでも所有者違いはコンテナ操作を開始しない。"""
        for mode in ("source", "restart"):
            status, marks = self.select(mode, owned=False)
            self.assertNotEqual(status, 0)
            self.assertEqual(marks, [])

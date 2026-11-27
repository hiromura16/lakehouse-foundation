"""所有された5表launcherのBash制御だけをDockerなしで実行する。"""

import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("hosted.sh")

MODEL = r"""
scratch=$1
scenario=$2
broker=$(printf '%064d' 1)
sql=$(printf '%064d' 2)
connect=$(printf '%064d' 3)
preparer=$(printf '%064d' 4)
source=$(printf '%064d' 5)
id=owned-fixture
net=owned-network-id
label=docker-label-format
resource_label=network-label-format
cluster=AAAAAAAAAAAAAAAAAAAAAA
expected_image=owned-image
running=false
mark() { printf '%s\n' "$1" >> "$scratch/order"; }
d() {
    if [[ $1 == start ]]; then
        if [[ $2 == "$broker" ]]; then
            [[ $# == 2 && $running == false ]]
            mark BROKER_START
            running=true
        else
            [[ $2 == "$sql" && $3 == "$connect" && $running == true ]]
            mark SQL_AND_CONNECT_START
        fi
        return
    fi
    [[ $2 == inspect && $3 == --format ]] || return 99
    if [[ $1 == network ]]; then
        [[ $5 == "$net" ]]
        case "$4" in
            network-label-format) printf '%s\n' "$id" ;;
            '{{.Internal}} {{.Driver}}') printf 'true bridge\n' ;;
            *) return 98 ;;
        esac
        return
    fi
    [[ $1 == container ]]
    case "$4" in
        docker-label-format)
            if [[ $scenario == foreign && $5 == "$broker" ]]; then
                printf 'another-owned-fixture\n'
            else
                printf '%s\n' "$id"
            fi ;;
        '{{len .HostConfig.PortBindings}} {{.HostConfig.Privileged}}')
            printf '0 false\n' ;;
        '{{.HostConfig.NetworkMode}}') printf '%s\n' "$net" ;;
        '{{range $key, $value := .NetworkSettings.Networks}}{{$key}}{{end}}')
            printf '%s\n' "$id" ;;
        '{{range .NetworkSettings.Networks}}{{.NetworkID}}{{end}}')
            printf '%s\n' "$net" ;;
        '{{.State.Status}}')
            if [[ $scenario == existing ]]; then printf 'running\n';
            else printf 'created\n'; fi ;;
        '{{.State.Running}}') printf '%s\n' "$running" ;;
        '{{index .Config.Labels "normal.cdc.scale.cluster"}}')
            if [[ $scenario == cluster ]]; then printf 'wrong-fixture\n';
            else printf '%s\n' "$cluster"; fi ;;
        '{{.Image}}') printf '%s\n' "$expected_image" ;;
        *) return 97 ;;
    esac
}
dt() {
    [[ $1 == 7s && $2 == exec && $3 == "$broker" && $running == true ]]
    mark OWNED_BROKER_READY
}
run_marker() {
    case "$1" in
        "$preparer")
            [[ $3 == SQL_SCALE_TOPICS=pass ]]
            mark PREPARE_TOPICS
            if [[ $scenario == occupied ]]; then return 94; fi ;;
        "$source")
            expected='SQL_SCALE_NATIVE=pass tables=5 snapshot=112'\
' insert=256 update=512 delete=256 tombstone=256'
            [[ $3 == "$expected" ]]
            mark NATIVE_SOURCE ;;
        *) return 96 ;;
    esac
}
"""


class LauncherTest(unittest.TestCase):
    """実Bashの関数を隔離し、所有とphaseの否定例を確認。"""

    def dispatch(self, scenario):
        """Docker/SQL/Kafka呼出しをstubへ置換した場合の順序を読む。"""
        text = SCRIPT.read_text(encoding="utf-8")
        begin = text.index("require_owned() {")
        end = text.index("run_marker() {", begin)
        guard = text[begin:end]
        begin = text.index("run_fixture() {")
        end = text.index("stage=CLEANUP", begin)
        phases = text[begin:end]
        with tempfile.TemporaryDirectory(prefix="scale-launcher-model-") as d:
            outcome = subprocess.run(
                [
                    "bash",
                    "-c",
                    "set -euo pipefail\n" + MODEL + guard + phases,
                    "model",
                    d,
                    scenario,
                ],
                capture_output=True,
                check=False,
                text=True,
                timeout=8,
            )
            marks = Path(d) / "order"
            return outcome.returncode, (
                marks.read_text(encoding="utf-8").splitlines()
                if marks.exists()
                else []
            )

    def test_owned_broker_first_then_topics_only_then_sql_and_connect(self):
        """既存のConnect処理を横取りせず、topic先行を強制する。"""
        self.assertEqual(
            self.dispatch("valid"),
            (
                0,
                [
                    "BROKER_START",
                    "OWNED_BROKER_READY",
                    "PREPARE_TOPICS",
                    "SQL_AND_CONNECT_START",
                    "NATIVE_SOURCE",
                ],
            ),
        )

    def test_wrong_owner_and_existing_broker_denied_before_start(self):
        """所有の誤り、既稼働broker、他cluster IDを肯定しない。"""
        for scenario in ("foreign", "existing"):
            result, marks = self.dispatch(scenario)
            self.assertNotEqual(result, 0)
            self.assertEqual(marks, [])
        result, marks = self.dispatch("cluster")
        self.assertNotEqual(result, 0)
        self.assertEqual(marks, ["BROKER_START"])

    def test_nonempty_topic_guard_blocks_sql_and_connect(self):
        """prepare-topicsがfreshと実証できなければ後半を起動しない。"""
        result, marks = self.dispatch("occupied")
        self.assertNotEqual(result, 0)
        self.assertEqual(
            marks, ["BROKER_START", "OWNED_BROKER_READY", "PREPARE_TOPICS"]
        )

    def test_explicit_mode_and_ordinary_host_paths_do_not_access_docker(self):
        """mode未指定・他mode・通常ホストを入口で拒否する。"""
        for args, expected in (
            ([], "SCALE_EXPLICIT_MODE_REQUIRED"),
            (["chain"], "SCALE_EXPLICIT_MODE_REQUIRED"),
            (["--source-scale"], "SCALE_HOSTED_RUNNER_REQUIRED"),
        ):
            run = subprocess.run(
                ["bash", str(SCRIPT), *args],
                env={"PATH": "/usr/bin:/bin"},
                capture_output=True,
                check=False,
                text=True,
                timeout=5,
            )
            self.assertNotEqual(run.returncode, 0)
            self.assertIn(expected, run.stderr)

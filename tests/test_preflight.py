"""Compose配備前のファイル・ネットワーク診断を読み取り専用で検査する。"""

import io
import json
import os
import unittest
from argparse import Namespace
from contextlib import redirect_stderr, redirect_stdout
from ipaddress import ip_network
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from scripts import preflight


def subnet(value):
    """テスト値をネットワークとして比較する。"""
    return ip_network(value)


def model(networks, used=None):
    """Composeから取得する有効サービスのJSON構造にそろえる。"""
    return {
        "name": "fixture",
        "services": {"app": {"networks": dict.fromkeys(used or networks)}},
        "networks": networks,
    }


def live(key, prefix, *, project="fixture", scope="local"):
    """Docker Engineで存在する割当を模擬する。"""
    return {
        "name": f"{project}_{key}",
        "scope": scope,
        "ipam": "default",
        "config": [{"Subnet": prefix}],
        "project": project,
        "key": key,
    }


class RouteTest(unittest.TestCase):
    """経路の所属にかかわらず、重複判定に使うネットワークを取得する。"""

    def test_policy_table_and_direct_route(self):
        """VPNの追加経路を拾い、経路の重複を生むローカル行を省く。"""
        with patch.object(
            preflight,
            "read_json",
            side_effect=[
                [
                    {"dst": "default"},
                    {"dst": "10.254.0.0/24", "scope": "link"},
                    {"dst": "10.253.0.0/24", "table": "52"},
                    {"dst": "127.0.0.4", "type": "local"},
                ],
                [{"dst": "2001:db8:5::/64", "table": "52"}],
            ],
        ) as read:
            direct, other = preflight.routes()
        self.assertEqual(direct, {subnet("10.254.0.0/24")})
        self.assertEqual(
            other, {subnet("10.253.0.0/24"), subnet("2001:db8:5::/64")}
        )
        for args in read.call_args_list:
            self.assertEqual(args.args[0][-2:], ["table", "all"])


class FileTest(unittest.TestCase):
    """使用中の参照元だけを診断し、パスやファイル内容を公開しない。"""

    def test_required_bind_and_build_secret_are_checked(self):
        """未使用の定義を除き、必須パスと通常ファイルを確認する。"""
        with TemporaryDirectory() as directory:
            data = Path(directory)
            missing = data / "required"
            optional = data / "create"
            doc = {
                "services": {
                    "app": {
                        "build": {"secrets": [{"source": "build-id"}]},
                        "secrets": [{"source": "from-env"}],
                        "configs": [{"source": "from-inline"}],
                        "volumes": [
                            {
                                "type": "bind",
                                "source": str(missing),
                                "bind": {"create_host_path": False},
                            },
                            {"type": "bind", "source": str(optional)},
                        ],
                    }
                },
                "secrets": {
                    "build-id": {"file": str(data)},
                    "from-env": {"environment": "OPERATOR_VALUE"},
                    "unused": {"file": str(data / "unused")},
                },
                "configs": {"from-inline": {"content": "configured"}},
            }
            result = preflight.file_findings(doc)
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(
            [result[k] for k in ("total", "missing", "invalid", "review")],
            [3, 1, 1, 1],
        )
        self.assertNotIn(str(data), json.dumps(result))


class NetworkTest(unittest.TestCase):
    """自社経路、既存Compose、未使用プロファイルを分けて扱う。"""

    def test_used_networks_only_and_reused_network(self):
        """同じ割当の再利用を競合扱いせず、新規bridgeだけ数える。"""
        spec = model(
            {
                "reuse": {
                    "name": "fixture_reuse",
                    "ipam": {"config": [{"subnet": "172.22.0.0/16"}]},
                },
                "new": {"name": "fixture_new"},
                "unused": {"name": "fixture_unused"},
                "v6": {"name": "fixture_v6", "enable_ipv4": False},
            },
            used=["reuse", "new", "v6"],
        )
        counts, _reserved, required = preflight.bridge_plan(
            spec, [live("reuse", "172.22.0.0/16")], {subnet("172.22.0.0/16")}
        )
        self.assertEqual(required, 1)
        self.assertFalse(any(counts.values()))

    def test_static_collision_warnings_and_external_network(self):
        """標準IPAMは競合を検知し、会社用VPNと外部構成を区別する。"""
        spec = model(
            {
                "first": {
                    "name": "fixture_first",
                    "ipam": {"config": [{"subnet": "10.20.0.0/24"}]},
                },
                "second": {
                    "name": "fixture_second",
                    "ipam": {"config": [{"subnet": "10.20.0.32/27"}]},
                },
                "external": {"name": "external", "external": True},
                "plugin": {"name": "plugin", "driver": "custom"},
            }
        )
        counts, _reserved, required = preflight.bridge_plan(
            spec,
            [live("other", "10.20.0.0/16", project="other")],
            {subnet("10.20.0.0/16"), subnet("fd00::/64")},
        )
        self.assertEqual(required, 0)
        self.assertEqual(counts["docker_overlap"], 2)
        self.assertEqual(counts["route_overlap"], 2)
        self.assertEqual(counts["external_missing"], 1)
        self.assertEqual(counts["custom_driver"], 1)

    def test_changed_owned_network_is_checked_against_other_projects(self):
        """同じ名前でも範囲が変わる場合は他の割当と重複しないか調べる。"""
        spec = model(
            {
                "reuse": {
                    "name": "fixture_reuse",
                    "ipam": {"config": [{"subnet": "10.11.0.0/16"}]},
                },
            }
        )
        counts, *_ = preflight.bridge_plan(
            spec,
            [
                live("reuse", "10.10.0.0/16"),
                live("n", "10.11.1.0/24", project="other"),
            ],
            set(),
        )
        self.assertEqual(counts["recreate_review"], 1)
        self.assertEqual(counts["docker_overlap"], 1)

    def test_stock_and_large_custom_pool_without_duplicate_blocking(self):
        """複数の重複する予約と大きなプールで空き数を正しく数える。"""
        self.assertEqual(
            preflight.available_slots(preflight.ipv4_pools(None), []), 31
        )
        pools = preflight.ipv4_pools([{"Base": "10.0.0.0/8", "Size": 32}])
        reservations = [
            subnet("10.0.0.0/9"),
            subnet("10.0.0.0/24"),
            subnet("fd00::/64"),
        ]
        self.assertEqual(preflight.available_slots(pools, reservations), 2**23)

    def test_pool_shortage_is_advisory(self):
        """空きが足りない場合も予測結果を警告として返す。"""
        with patch.object(preflight, "read_json", return_value=None):
            result = preflight.pool_finding(
                ["docker"],
                [subnet("172.16.0.0/12")],
                {subnet("192.168.0.0/16")},
                1,
            )
        self.assertEqual(result["status"], "WARN")
        self.assertEqual(result["available_estimate"], 0)


class CommandTest(unittest.TestCase):
    """Docker CLIの読み取りだけに限定し、入力と生出力を報告しない。"""

    def test_compose_flags_keep_repeat_order(self):
        args = Namespace(
            env_file=["one", "two"],
            file=["base", "overlay"],
            profile=["a", "b"],
            project_name="fixture",
        )
        command = preflight.compose_command(["sudo", "-n", "docker"], args)
        self.assertEqual(
            command,
            [
                "sudo",
                "-n",
                "docker",
                "compose",
                "--env-file",
                "one",
                "--env-file",
                "two",
                "-f",
                "base",
                "-f",
                "overlay",
                "--profile",
                "a",
                "--profile",
                "b",
                "-p",
                "fixture",
                "config",
                "--format",
                "json",
            ],
        )

    def test_remote_checks_skip_host_and_leave_private_model_internal(self):
        config = model({"default": {"name": "fixture_default"}})
        config["services"]["app"]["environment"] = {
            "AUTH": "secret-fixture-value"
        }
        out = io.StringIO()
        with (
            patch("sys.argv", ["preflight", "--json"]),
            patch.object(preflight, "read_json", return_value=config),
            patch.object(preflight, "read", return_value="linux"),
            patch.object(preflight, "host_mode", return_value=(False, False)),
            patch.object(preflight, "active_networks", return_value=[]),
            patch.object(preflight, "file_findings") as files,
            patch.object(preflight, "routes") as routes,
            redirect_stdout(out),
        ):
            self.assertEqual(preflight.main(), 0)
        files.assert_not_called()
        routes.assert_not_called()
        self.assertNotIn("secret-fixture-value", out.getvalue())
        data = {
            item["code"]: item["status"]
            for item in json.loads(out.getvalue())["findings"]
        }
        self.assertEqual(data["HOST_FILES"], "SKIP")
        self.assertEqual(data["HOST_ROUTES"], "SKIP")
        self.assertEqual(data["IPV4_POOL"], "SKIP")

    def test_bad_arg_diagnostic_has_no_private_argument(self):
        err = io.StringIO()
        with (
            patch("sys.argv", ["preflight", "--PRIVATE-PATH"]),
            redirect_stderr(err),
            self.assertRaises(SystemExit) as error,
        ):
            preflight.main()
        self.assertEqual(error.exception.code, 2)
        self.assertNotIn("PRIVATE-PATH", err.getvalue())


class HostModeTest(unittest.TestCase):
    """contextとEngineの実接続先が違う場合にホストを誤診しない。"""

    def test_docker_host_override_does_not_certify_local_context(self):
        """DOCKER_HOST指定時に、別VMのbindや経路を手元で検査しない。"""
        with (
            patch.dict(
                os.environ,
                {"DOCKER_HOST": "tcp://engine.example.invalid:2376"},
                clear=True,
            ),
            patch.object(preflight, "read") as reader,
        ):
            self.assertEqual(
                preflight.host_mode(["docker"], "linux"), (False, False)
            )
        reader.assert_not_called()

    def test_sudo_does_not_assume_environment_preservation(self):
        """sudo先が呼出元と同じcontextを選ぶと推測しない。"""
        with (
            patch.dict(
                os.environ,
                {
                    "DOCKER_HOST": "unix:///fixture.sock",
                    "DOCKER_CONTEXT": "fixture",
                },
                clear=True,
            ),
            patch.object(preflight, "read") as reader,
        ):
            self.assertEqual(
                preflight.host_mode(["sudo", "-n", "docker"], "linux"),
                (False, False),
            )
        reader.assert_not_called()

    def test_explicit_context_with_remote_endpoint_stays_nonlocal(self):
        """明示contextが優先されても、SSH Engineをホストと同一視しない。"""
        with (
            patch.dict(
                os.environ,
                {
                    "DOCKER_HOST": "unix:///fixture.sock",
                    "DOCKER_CONTEXT": "remote-fixture",
                },
                clear=True,
            ),
            patch.object(preflight, "read", return_value="ssh://fixture"),
            patch.object(
                preflight, "read_json", return_value=["Fixture Linux", []]
            ),
        ):
            self.assertEqual(
                preflight.host_mode(["docker"], "linux"), (False, False)
            )


def compose_port(address=None, port="23456", *, protocol="tcp", target=80):
    """ハードコードした合成ポートだけをComposeの出力形式にする。"""
    return {
        "host_ip": address,
        "published": port,
        "protocol": protocol,
        "target": target,
    }


def docker_port(address, project, service, *, configured=(), oneoff=False):
    """現在のDocker割当を、会社のIP・サービス名を使わずに作る。"""
    return preflight.DockerPort(
        preflight.PublishedPort(
            preflight.bind_address(address), 23456, "tcp", 80
        ),
        project,
        service,
        oneoff,
        frozenset(configured),
    )


class PublishedPortsTest(unittest.TestCase):
    """同じサービスの再配備と、占有・不明な公開ポートを区別する。"""

    def diagnose(self, services, *, records=(), listeners=None, **scope):
        """本物のDockerや待受ソケットなしで診断の集計を確認する。"""
        active = {("tcp", 23456): records}
        with (
            patch.object(preflight, "docker_ports", return_value=active),
            patch.object(
                preflight, "listener_ports", return_value=listeners or {}
            ),
        ):
            return preflight.ports_finding(
                ["docker"],
                {"name": "fixture", "services": services},
                **{"local": True, "rootless": False, **scope},
            )

    def test_fixed_ips_and_protocols_are_compared_separately(self):
        """異なるループバックやTCP/UDPは別の割当として扱う。"""
        output = self.diagnose(
            {
                "a": {
                    "ports": [
                        compose_port("127.0.0.1"),
                        compose_port("127.0.0.1", protocol="udp"),
                    ]
                },
                "b": {
                    "ports": [compose_port("127.0.0.2"), compose_port("::1")]
                },
            }
        )
        self.assertEqual(output["status"], "PASS")
        self.assertEqual(output["fixed"], 4)

    def test_ipv4_wildcard_and_v6_loopback_collisions(self):
        """同じIPファミリーでのワイルドカードの重複は終了1になる。"""
        output = self.diagnose(
            {
                "a": {
                    "ports": [compose_port("127.0.0.1"), compose_port("::1")]
                },
                "b": {"ports": [compose_port("0.0.0.0"), compose_port("::")]},
            }
        )
        self.assertEqual(output["status"], "FAIL")
        self.assertEqual(output["config_conflicts"], 2)
        self.assertGreaterEqual(output["possible"], 1)

    def test_other_project_docker_collision_and_host_data_privacy(self):
        """割当を確認できた別Appとの競合は、値やサービス名を隠して返す。"""
        output = self.diagnose(
            {"internal-user-service": {"ports": [compose_port("127.0.0.1")]}},
            records=[
                docker_port("0.0.0.0", "internal-owner", "internal-service")
            ],
            listeners={("tcp", 23456): {preflight.bind_address("0.0.0.0")}},
        )
        self.assertEqual(output["status"], "FAIL")
        self.assertEqual(output["docker_conflicts"], 1)
        self.assertEqual(output["host_review"], 0)
        text = json.dumps(output)
        for private in ("internal-", "23456", "127.0.0.1", "0.0.0.0"):
            self.assertNotIn(private, text)

    def test_existing_binding_keeps_same_service_id(self):
        """同じ役割の同一割当を再配備するときは二重占有とみなさない。"""
        declaration = preflight.PublishedPort(None, 23456, "tcp", 80)
        records = [
            docker_port(ip, "fixture", "a", configured=[declaration])
            for ip in ("0.0.0.0", "::")
        ]
        output = self.diagnose(
            {"a": {"ports": [compose_port()]}},
            records=records,
            listeners={
                ("tcp", 23456): {item.binding.address for item in records}
            },
        )
        self.assertEqual(output["status"], "PASS")
        self.assertEqual(output["docker_conflicts"], 0)
        direct = self.diagnose(
            {"a": {"ports": [compose_port("127.0.0.1")]}},
            records=[docker_port("127.0.0.1", "fixture", "a")],
        )
        self.assertEqual(direct["status"], "PASS")

    def test_same_project_changed_binding_and_one_off(self):
        """再作成が必要な割当と、通常の再配備で残る一時コンテナを区別する。"""
        running = docker_port("127.0.0.1", "fixture", "a")
        output = self.diagnose(
            {"a": {"ports": [compose_port("127.0.0.1", target=81)]}},
            records=[running],
        )
        self.assertEqual(output["status"], "WARN")
        self.assertEqual(output["recreate_review"], 1)
        output = self.diagnose(
            {"a": {"ports": [compose_port("127.0.0.1")]}},
            records=[docker_port("127.0.0.1", "fixture", "a", oneoff=True)],
        )
        self.assertEqual(output["status"], "FAIL")

    def test_ipv6_wildcard_scope_and_unknown_host_listener_are_warnings(self):
        """デュアルスタックと所有者が分からないOS待受は個別に確認する。"""
        output = self.diagnose(
            {"a": {"ports": [compose_port("127.0.0.1")]}},
            records=[docker_port("::", "other", "b")],
            listeners={("tcp", 23456): {preflight.bind_address("127.0.0.1")}},
        )
        self.assertEqual(output["status"], "WARN")
        self.assertEqual(output["possible"], 1)
        self.assertEqual(output["host_review"], 1)

    def test_rootless_and_implicit_address_do_not_claim_host_certainty(self):
        """ローカルrootlessではDockerだけを使い、省略アドレスは警告にする。"""
        services = {"a": {"ports": [compose_port()]}}
        active = {("tcp", 23456): [docker_port("127.0.0.1", "other", "b")]}
        with (
            patch.object(preflight, "docker_ports", return_value=active),
            patch.object(preflight, "listener_ports") as host,
        ):
            output = preflight.ports_finding(
                ["docker"],
                {"name": "fixture", "services": services},
                local=True,
                rootless=True,
            )
        self.assertEqual(output["status"], "WARN")
        self.assertEqual(output["possible"], 1)
        self.assertEqual(output["host_unreadable"], 1)
        host.assert_not_called()

    def test_dynamic_ranges_and_unsupported_items_do_not_probe(self):
        """割当先が固定されない設定だけでは、DockerとOSの照合をしない。"""
        services = {
            "a": {
                "ports": [
                    compose_port(port=None),
                    compose_port(port=0),
                    compose_port(port="23456-23459"),
                    compose_port("127.0.0.1", protocol="sctp"),
                ]
            },
            "b": {
                "network_mode": "host",
                "ports": [compose_port("127.0.0.1")],
            },
        }
        with (
            patch.object(preflight, "docker_ports") as docker,
            patch.object(preflight, "listener_ports") as host,
        ):
            output = preflight.ports_finding(
                ["docker"],
                {"name": "fixture", "services": services},
                local=True,
                rootless=False,
            )
        self.assertEqual(output["status"], "WARN")
        self.assertEqual(output["unchecked"], 5)
        docker.assert_not_called()
        host.assert_not_called()

    def test_unavailable_observers_do_not_hide_static_conflicts(self):
        """Dockerとssを読めない場合も設定内の重複は残る。"""
        services = {
            "a": {"ports": [compose_port("127.0.0.1")]},
            "b": {"ports": [compose_port("127.0.0.1")]},
        }
        with (
            patch.object(
                preflight, "docker_ports", side_effect=preflight.ReadError
            ),
            patch.object(
                preflight, "listener_ports", side_effect=preflight.ReadError
            ),
        ):
            output = preflight.ports_finding(
                ["docker"],
                {"name": "fixture", "services": services},
                local=True,
                rootless=False,
            )
        self.assertEqual(output["status"], "FAIL")
        self.assertEqual(output["docker_unreadable"], 1)
        self.assertEqual(output["host_unreadable"], 1)

    def test_remote_skip_does_not_inspect_ports(self):
        """別ホストにはEngineの公開割当やローカルssの判定を適用しない。"""
        with (
            patch.object(preflight, "docker_ports") as docker,
            patch.object(preflight, "listener_ports") as host,
        ):
            output = preflight.ports_finding(
                ["docker"], {}, local=False, rootless=False
            )
        self.assertEqual(output["status"], "SKIP")
        docker.assert_not_called()
        host.assert_not_called()

    def test_ss_numeric_output_parses_ipv4_mapped_ipv6(self):
        """IPv4射影とIPv6のワイルドカードを共通形式へ変える。"""
        with patch.object(
            preflight,
            "read",
            side_effect=[
                "tcp LISTEN 0 128 127.0.0.1%lo:23456 0.0.0.0:*",
                "\n".join(
                    (
                        "tcp LISTEN 0 128 [::ffff:127.0.0.1]:23456 *:*",
                        "udp UNCONN 0 0 *:23456 *:*",
                        "udp UNCONN 0 0 [2001:db8::5]%eth0:23457 *:*",
                    )
                ),
            ],
        ) as reader:
            result = preflight.listener_ports()
        self.assertEqual(
            result[("tcp", 23456)], {preflight.bind_address("127.0.0.1")}
        )
        self.assertEqual(
            result[("udp", 23456)], {preflight.bind_address("::")}
        )
        self.assertEqual(
            result[("udp", 23457)], {preflight.bind_address("2001:db8::5")}
        )
        self.assertTrue(
            all("-p" not in call.args[0] for call in reader.call_args_list)
        )

    def test_docker_wire_format_uses_read_only_calls(self):
        """NetworkSettingsの実ポートを優先し、設定だけの未割当を除く。"""
        payload = {
            "project": "fixture",
            "service": "a",
            "oneoff": "False",
            "configured": {
                "80/tcp": [{"HostIp": "", "HostPort": "23456"}],
                "90/tcp": [{"HostIp": "", "HostPort": ""}],
            },
            "published": {
                "80/tcp": [{"HostIp": "0.0.0.0", "HostPort": "23456"}],
                "90/tcp": None,
            },
        }
        with patch.object(
            preflight,
            "read",
            side_effect=["a" * 64, json.dumps(payload)],
        ) as reader:
            active = preflight.docker_ports(["docker"])
        self.assertEqual(list(active), [("tcp", 23456)])
        self.assertEqual(active[("tcp", 23456)][0].service, "a")
        self.assertEqual(
            [item.args[0][1] for item in reader.call_args_list],
            ["ps", "inspect"],
        )

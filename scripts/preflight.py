#!/usr/bin/env python3
"""Docker Composeの配備前診断を、秘密値を表示せずに実行する。

Foundation Rの汎用診断（1790f50、fc774fe、d3a9568）を移植した。
外部DB接続、コンテナ起動、ポート予約、ネットワーク変更は行わない。
結果は診断時点の観測と推定であり、Deploy成功の保証ではない。
"""

import argparse
import json
import os
import stat
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv6Address, ip_address, ip_network
from itertools import combinations
from pathlib import Path

DEFAULT_IPV4_POOLS = (
    ("172.17.0.0/16", 16),
    ("172.18.0.0/16", 16),
    ("172.19.0.0/16", 16),
    ("172.20.0.0/14", 16),
    ("172.24.0.0/14", 16),
    ("172.28.0.0/14", 16),
    ("192.168.0.0/16", 20),
)
NETWORK_FORMAT = (
    '{"name":{{json .Name}},"scope":{{json .Scope}},'
    '"ipam":{{json .IPAM.Driver}},"config":{{json .IPAM.Config}},'
    '"project":{{json (index .Labels "com.docker.compose.project")}},'
    '"key":{{json (index .Labels "com.docker.compose.network")}}}'
)


PORT_FORMAT = (
    '{"project":{{json (index .Config.Labels "com.docker.compose.project")}},'
    '"service":{{json (index .Config.Labels "com.docker.compose.service")}},'
    '"oneoff":{{json (index .Config.Labels "com.docker.compose.oneoff")}},'
    '"configured":{{json .HostConfig.PortBindings}},'
    '"published":{{json .NetworkSettings.Ports}}}'
)
HOST_FAMILIES = {"-4": "0.0.0.0", "-6": "::"}


@dataclass(frozen=True)
class PublishedPort:
    """固定するホスト側の割当と対応するコンテナ側のポート。"""

    address: IPv4Address | IPv6Address | None
    port: int
    protocol: str
    target: int


@dataclass(frozen=True)
class DockerPort:
    """Dockerが実際に割り当てたソケットと再配備判定用の情報。"""

    binding: PublishedPort
    project: str
    service: str
    oneoff: bool
    configured: frozenset[PublishedPort]


class ReadError(Exception):
    """CLIの生出力や引数を報告へ持ち出さない。"""


class Parser(argparse.ArgumentParser):
    """入力エラーにも利用者が渡した非公開の引数を表示しない。"""

    def error(self, _message):
        self.print_usage(sys.stderr)
        self.exit(2, "引数を確認してください。詳細は --help にあります。\n")


def read(command):
    """Dockerとipの読取結果はメモリ内で処理し、stderrは公開しない。"""
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=True,
            timeout=25,
            text=True,
        )
    except (OSError, UnicodeError, subprocess.SubprocessError):
        raise ReadError from None
    return result.stdout.strip()


def read_json(command):
    """読取コマンドの結果から秘密値を含まないエラーだけを伝える。"""
    try:
        return json.loads(read(command))
    except ValueError:
        raise ReadError from None


def compose_command(docker, args):
    """同種の複数オプションを入力順に渡し、Composeのマージを使う。"""
    command = [*docker, "compose"]
    for flag, values in (
        ("--env-file", args.env_file),
        ("-f", args.file),
        ("--profile", args.profile),
    ):
        for value in values:
            command.extend((flag, value))
    if args.project_name:
        command.extend(("-p", args.project_name))
    return [*command, "config", "--format", "json"]


def finding(code, status, message, **counts):
    """報告は固定文と数値だけから作る。"""
    return {"code": code, "status": status, "message": message, **counts}


def source_files(model):
    """有効サービスで使うbind・secret・configのファイルだけを列挙する。"""
    paths = {}
    references = defaultdict(set)
    for spec in model["services"].values():
        for mount in spec.get("volumes", []):
            if mount.get("type") != "bind":
                continue
            path = mount["source"]
            mandatory = not mount.get("bind", {}).get("create_host_path", True)
            paths[path] = (
                paths.get(path, (False, False))[0] or mandatory,
                False,
            )
        for kind, sources in (
            ("secrets", spec.get("secrets", [])),
            ("secrets", (spec.get("build") or {}).get("secrets", [])),
            ("configs", spec.get("configs", [])),
        ):
            references[kind].update(
                source if isinstance(source, str) else source["source"]
                for source in sources
            )
    for kind, names in references.items():
        for name in names:
            source = model.get(kind, {}).get(name, {}).get("file")
            if source:
                paths[source] = (True, True)
    return paths


def file_findings(model):
    """必須の欠落と、権限や作成設定のため判断できない項目を分ける。"""
    paths = source_files(model)
    missing = invalid = review = 0
    for source, (mandatory, file_required) in paths.items():
        path = Path(source)
        try:
            if not path.is_absolute():
                review += 1
                continue
            mode = path.stat().st_mode
        except (FileNotFoundError, NotADirectoryError):
            missing += mandatory
            review += not mandatory
        except OSError:
            # 実行ユーザー、Docker、BuildKitのファイル権限は異なることがある。
            review += 1
        else:
            if file_required and not stat.S_ISREG(mode):
                invalid += 1
            elif file_required and not os.access(path, os.R_OK):
                review += 1
    status = "FAIL" if missing or invalid else "WARN" if review else "PASS"
    return finding(
        "HOST_FILES",
        status,
        "有効なbind・secret・configの参照元を確認しました。",
        total=len(paths),
        missing=missing,
        invalid=invalid,
        review=review,
    )


def active_networks(docker):
    """コンテナがつながっていない既存ネットワークも割当済みとして読み取る。"""
    ids = read(
        [*docker, "network", "ls", "--quiet", "--no-trunc"]
    ).splitlines()
    networks = []
    for start in range(0, len(ids), 100):
        end = start + 100
        raw = read(
            [
                *docker,
                "network",
                "inspect",
                "--format",
                NETWORK_FORMAT,
                *ids[start:end],
            ]
        )
        try:
            networks.extend(json.loads(line) for line in raw.splitlines())
        except ValueError:
            raise ReadError from None
    return networks


def port_number(value):
    """任意割当と範囲は単一のポート番号に変換しない。"""
    digits = str(value)
    if digits.isascii() and digits.isdigit() and len(digits) <= 5:
        number = int(digits)
        if 1 <= number <= 65535:
            return number
    return None


def bind_address(value):
    """IPv4射影アドレスを、同じ割当として照合できる形式にそろえる。"""
    if not value:
        return None
    address = ip_address(value.removeprefix("[").removesuffix("]"))
    if isinstance(address, IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def planned_ports(model):
    """有効なComposeサービスだけから、確認できる固定割当を取得する。"""
    bindings = defaultdict(set)
    unchecked = 0
    for service, spec in model["services"].items():
        for item in spec.get("ports") or []:
            if (
                not isinstance(item, dict)
                or spec.get("network_mode") == "host"
            ):
                unchecked += 1
                continue
            protocol = item.get("protocol", "tcp")
            port = port_number(item.get("published"))
            target = port_number(item.get("target"))
            if (
                protocol not in ("tcp", "udp")
                or port is None
                or target is None
            ):
                unchecked += 1
                continue
            try:
                address = bind_address(item.get("host_ip"))
            except (ValueError, AttributeError):
                unchecked += 1
            else:
                bindings[(protocol, port)].add(
                    (service, PublishedPort(address, port, protocol, target))
                )
    return bindings, unchecked


def docker_bindings(mappings):
    """Engineの公開情報から、実際にホストポートを使うTCP/UDPを拾う。"""
    for container_spec, allocations in (mappings or {}).items():
        target_text, _, protocol = container_spec.partition("/")
        target = port_number(target_text)
        if target is None or protocol not in ("tcp", "udp"):
            continue
        for entry in allocations or []:
            port = port_number(entry.get("HostPort"))
            if port is not None:
                yield PublishedPort(
                    bind_address(entry.get("HostIp")), port, protocol, target
                )


def docker_ports(docker):
    """稼働中コンテナの情報を、秘密を含む環境や引数を取得せずに読む。"""
    ids = read([*docker, "ps", "--quiet", "--no-trunc"]).splitlines()
    active = defaultdict(list)
    for start in range(0, len(ids), 100):
        end = start + 100
        raw = read(
            [
                *docker,
                "inspect",
                "--type",
                "container",
                "--format",
                PORT_FORMAT,
                *ids[start:end],
            ]
        )
        try:
            for line in raw.splitlines():
                row = json.loads(line)
                configured = frozenset(docker_bindings(row["configured"]))
                for binding in docker_bindings(row["published"]):
                    active[(binding.protocol, binding.port)].append(
                        DockerPort(
                            binding,
                            row["project"],
                            row["service"],
                            str(row.get("oneoff") or "").lower() == "true",
                            configured,
                        )
                    )
        except (ValueError, TypeError, KeyError, AttributeError):
            raise ReadError from None
    return active


def listener_ports():
    """名前解決やプロセス表示を使わず、IPv4・IPv6の待受だけを照合する。"""
    listeners = defaultdict(set)
    for family, wildcard in HOST_FAMILIES.items():
        raw = read(["ss", "-H", "-l", "-n", "-t", "-u", family])
        for line in raw.splitlines():
            fields = line.split()
            if len(fields) < 6 or fields[0] not in ("tcp", "udp"):
                raise ReadError
            address, _, port_text = fields[4].rpartition(":")
            port = port_number(port_text)
            if port is None or not address:
                raise ReadError
            try:
                # ssはIPv4とIPv6のどちらにも %インターフェース を付ける。
                # ホスト待受は警告用なので、アドレスの範囲だけで比較する。
                scope_free = address.partition("%")[0]
                ip = bind_address(
                    wildcard if scope_free == "*" else scope_free
                )
            except ValueError:
                raise ReadError from None
            listeners[(fields[0], port)].add(ip)
    return listeners


def overlap_kind(left, right):
    """候補を確実な重複、実装依存の重複、別の割当に分ける。"""
    if left is None or right is None:
        # 省略時のバインド先はEngineやネットワークの設定で変わり得る。
        return "possible"
    if left.version != right.version:
        # IPv6ワイルドカードがIPv4を受けるかはsocket設定に依存する。
        if IPv6Address("::") in (left, right):
            return "possible"
        return None
    if left == right or left.is_unspecified or right.is_unspecified:
        return "certain"
    return None


def docker_collision(wanted, service, project, active):
    """同じサービスの同一割当を除き、他の所有者との重複を分類する。"""
    hits = set()
    for record in active:
        relation = overlap_kind(wanted.address, record.binding.address)
        if relation is None:
            continue
        if record.project == project and not record.oneoff:
            if record.service == service and (
                wanted in record.configured or wanted == record.binding
            ):
                continue
            # 同じApp Nameの別割当は、再作成・撤去予定に依存する。
            hits.add("recreate_review")
        elif relation == "certain":
            hits.add("docker_conflicts")
        else:
            hits.add("possible")
    return hits


def ports_finding(docker, model, *, local, rootless):
    """公開値を出さずに固定ポートの衝突または確認残りの件数を返す。"""
    if not local:
        return finding(
            "PORTS", "SKIP", "配備VMのローカルEngineで公開ポートを検査します。"
        )
    try:
        planned, unchecked = planned_ports(model)
    except (AttributeError, TypeError):
        return finding(
            "PORTS", "WARN", "公開ポートのCompose出力形式を確認できません。"
        )
    counts = dict.fromkeys(
        (
            "config_conflicts",
            "docker_conflicts",
            "possible",
            "recreate_review",
            "host_review",
            "docker_unreadable",
            "host_unreadable",
        ),
        0,
    )
    if planned:
        active, listeners = {}, {}
        try:
            active = docker_ports(docker)
        except ReadError:
            counts["docker_unreadable"] += 1
        if rootless:
            counts["host_unreadable"] += 1
        else:
            try:
                listeners = listener_ports()
            except ReadError:
                counts["host_unreadable"] += 1
        for socket, wanted in planned.items():
            records = active.get(socket, [])
            known_hosts = {record.binding.address for record in records}
            for (_, left), (_, right) in combinations(wanted, 2):
                relation = overlap_kind(left.address, right.address)
                if relation == "certain":
                    counts["config_conflicts"] += 1
                elif relation == "possible":
                    counts["possible"] += 1
            for service, binding in wanted:
                for hit in docker_collision(
                    binding, service, model.get("name"), records
                ):
                    counts[hit] += 1
                # Dockerがすでに報告した待受は2重に数えず、他は警告に留める。
                for address in listeners.get(socket, set()) - known_hosts:
                    if overlap_kind(address, binding.address):
                        counts["host_review"] += 1
                        break
    if counts["config_conflicts"] or counts["docker_conflicts"]:
        level = "FAIL"
    else:
        level = "WARN" if unchecked or any(counts.values()) else "PASS"
    return finding(
        "PORTS",
        level,
        "固定の公開ポートと既存の割当を確認しました。",
        fixed=sum(len(bindings) for bindings in planned.values()),
        unchecked=unchecked,
        **counts,
    )


def routes():
    """デフォルト経路を除き、直接接続とその他の経路を分ける。"""
    direct = set()
    other = set()
    for family in ("-4", "-6"):
        command = ["ip", "-j", family, "route", "show", "table", "all"]
        for route in read_json(command):
            if route.get("type", "unicast") in {
                "local",
                "broadcast",
                "multicast",
            }:
                continue
            destination = route.get("dst")
            if not destination or destination == "default":
                continue
            prefix = ip_network(destination, strict=False)
            if not prefix.prefixlen:
                continue
            if route.get("scope") == "link" or (
                "gateway" not in route and route.get("protocol") == "kernel"
            ):
                direct.add(prefix)
            else:
                other.add(prefix)
    return direct, other


def subnets(config, key):
    """モデルとEngineで大文字小文字が違うIPAMのアドレスを読み取る。"""
    return [
        ip_network(entry[key], strict=False)
        for entry in config or []
        if entry.get(key)
    ]


def overlaps(prefix, candidates):
    """IPアドレスの世代が同じ範囲の重なりを確認する。"""
    return any(
        prefix.version == candidate.version and prefix.overlaps(candidate)
        for candidate in candidates
    )


def bridge_plan(model, current, host_routes):
    """実際に使うComposeネットワークから、新規の標準bridgeだけを検査する。"""
    used = {
        key
        for spec in model["services"].values()
        for key in spec.get("networks", {})
    }
    by_name = defaultdict(list)
    occupied = []
    for network in current:
        by_name[network["name"]].append(network)
        if network["scope"] == "local" and network["ipam"] == "default":
            occupied.extend(subnets(network["config"], "Subnet"))
    conflicts = dict.fromkeys(
        (
            "docker_overlap",
            "route_overlap",
            "external_missing",
            "name_review",
            "custom_driver",
            "recreate_review",
        ),
        0,
    )
    explicit = []
    implicit_count = 0
    for key in sorted(used):
        spec = model["networks"][key]
        candidates = by_name[spec["name"]]
        if spec.get("external"):
            conflicts["external_missing"] += not candidates
            continue
        ipam = spec.get("ipam") or {}
        if (
            spec.get("driver", "bridge") != "bridge"
            or ipam.get("driver", "default") != "default"
        ):
            conflicts["custom_driver"] += 1
            continue
        declared = subnets(ipam.get("config"), "subnet")
        if candidates:
            own = [
                n
                for n in candidates
                if n["project"] == model["name"] and n["key"] == key
            ]
            if len(own) != 1 or len(candidates) != 1:
                conflicts["name_review"] += 1
                continue
            own_subnets = subnets(own[0]["config"], "Subnet")
            # 既存範囲が同じ場合は、自身の経路や割当を競合扱いしない。
            changed = [
                prefix for prefix in declared if prefix not in own_subnets
            ]
            if changed:
                conflicts["recreate_review"] += 1
            # 再作成が必要な定義でも、他ネットワークとの重なりは調べる。
            for prefix in changed:
                other_networks = [
                    bound for bound in occupied if bound not in own_subnets
                ]
                conflicts["docker_overlap"] += overlaps(
                    prefix, other_networks + explicit
                )
                conflicts["route_overlap"] += overlaps(
                    prefix, host_routes - set(own_subnets)
                )
                explicit.append(prefix)
            continue
        for prefix in declared:
            conflicts["docker_overlap"] += overlaps(
                prefix, occupied + explicit
            )
            conflicts["route_overlap"] += overlaps(prefix, host_routes)
            explicit.append(prefix)
        implicit_count += spec.get("enable_ipv4", True) and not any(
            prefix.version == 4 for prefix in declared
        )
    return conflicts, occupied + explicit, implicit_count


def ipv4_pools(raw):
    """Engineの設定が空なら公式既定値を使い、非標準の重複は推測しない。"""
    entries = (
        [(item["Base"], item["Size"]) for item in raw]
        if raw
        else DEFAULT_IPV4_POOLS
    )
    pools = []
    for base, size in entries:
        prefix = ip_network(base, strict=False)
        if prefix.version != 4:
            continue
        if not prefix.prefixlen <= size <= 32 or overlaps(
            prefix, (pool for pool, _size in pools)
        ):
            raise ValueError
        pools.append((prefix, size))
    if not pools:
        raise ValueError
    return pools


def available_slots(pools, reservations):
    """既定IPv4プールの空き数を、全サブネットを列挙せず区間で推定する。"""
    available = 0
    for base, prefix_size in pools:
        steps = 1 << (32 - prefix_size)
        first = int(base.network_address)
        blocked = sorted(
            (
                (max(first, int(net.network_address)) - first) // steps,
                (
                    min(
                        int(base.broadcast_address), int(net.broadcast_address)
                    )
                    - first
                )
                // steps,
            )
            for net in reservations
            if net.version == 4 and base.overlaps(net)
        )
        last = -1
        remaining = 1 << (prefix_size - base.prefixlen)
        for start, end in blocked:
            remaining -= max(0, end - max(start - 1, last))
            last = max(last, end)
        available += remaining
    return available


def pool_finding(docker, reserved, direct, required):
    """IPv4の推定値はアドバイスとし、ネットワークの作成は行わない。"""
    if not required:
        return finding(
            "IPV4_POOL",
            "PASS",
            "自動割当が必要な新規IPv4 bridgeはありません。",
        )
    try:
        raw = read_json(
            [*docker, "info", "--format", "{{json .DefaultAddressPools}}"]
        )
        available = available_slots(ipv4_pools(raw), [*reserved, *direct])
    except (ReadError, KeyError, TypeError, ValueError):
        return finding(
            "IPV4_POOL", "SKIP", "有効なIPv4プール一覧を取得できません。"
        )
    status = "WARN" if available < required else "PASS"
    return finding(
        "IPV4_POOL",
        status,
        "既存割当と直接接続経路から、新規IPv4 bridgeの空き数を概算しました。",
        required=required,
        available_estimate=available,
    )


def host_mode(docker, server_os):
    """配備VM自身だけで、bindのパスとホスト経路の検査を許可する。"""
    # DOCKER_HOSTはcontextの選択を変えずにEngineの接続先を上書きする。
    # sudoの環境引継ぎも推測せず、明示override時はホスト検査を省く。
    overridden = bool(os.environ.get("DOCKER_HOST"))
    if overridden and (
        docker[0] == "sudo" or not os.environ.get("DOCKER_CONTEXT")
    ):
        return False, False
    try:
        endpoint = read(
            [
                *docker,
                "context",
                "inspect",
                "--format",
                "{{.Endpoints.docker.Host}}",
            ]
        )
        info = read_json(
            [
                *docker,
                "info",
                "--format",
                "[{{json .OperatingSystem}},{{json .SecurityOptions}}]",
            ]
        )
    except ReadError:
        return False, False
    unix = endpoint.startswith("unix:///")
    on_linux = sys.platform.startswith("linux") and server_os == "linux"
    in_container = any(
        Path(path).exists() for path in ("/.dockerenv", "/run/.containerenv")
    )
    local = (
        unix
        and on_linux
        and not in_container
        and "desktop" not in info[0].lower()
    )
    rootless = any("name=rootless" in value for value in info[1] or [])
    return local, rootless


def checks(args):
    """固定の診断結果のみを集め、Dockerの状態を変更しない。"""
    docker = ["sudo", "-n", "docker"] if args.sudo else ["docker"]
    try:
        model = read_json(compose_command(docker, args))
        if not isinstance(model.get("services"), dict):
            raise ReadError
    except (ReadError, AttributeError):
        return [
            finding(
                "COMPOSE",
                "FAIL",
                "Composeを展開できません。入力ファイルや必須変数を確認してください。",
            )
        ]
    report = [
        finding(
            "COMPOSE",
            "PASS",
            "Composeのマージ、プロファイル、変数展開を確認しました。",
        )
    ]
    try:
        engine = read([*docker, "version", "--format", "{{.Server.Os}}"])
        if not engine or engine == "<no value>":
            raise ReadError
    except ReadError:
        report.append(
            finding(
                "ENGINE",
                "FAIL",
                "Dockerの接続先と権限を確認してください。",
            )
        )
        return report
    report.append(finding("ENGINE", "PASS", "Docker Engineに接続できました。"))
    local, rootless = host_mode(docker, engine)
    direct, other = set(), set()
    routes_known = False
    if local:
        report.append(file_findings(model))
        try:
            direct, other = routes()
            routes_known = True
            report.append(
                finding(
                    "HOST_ROUTES",
                    "PASS",
                    "配備VMのIPv4・IPv6経路表を読み取りました。",
                )
            )
        except (ReadError, ValueError):
            report.append(
                finding(
                    "HOST_ROUTES",
                    "SKIP",
                    "配備VMの ip コマンドを確認してください。",
                )
            )
    else:
        report.append(
            finding(
                "HOST_FILES",
                "SKIP",
                "配備VMのローカルEngineと確認できないため参照元の検査を省きます。",
            )
        )
        report.append(
            finding(
                "HOST_ROUTES",
                "SKIP",
                "配備VMのローカルEngineと確認できないため経路の検査を省きます。",
            )
        )
    try:
        current = active_networks(docker)
    except ReadError:
        report.append(
            finding(
                "IPAM",
                "SKIP",
                "既存ネットワークを読み取れません。Dockerの読取権限を確認してください。",
            )
        )
        report.append(
            ports_finding(docker, model, local=local, rootless=rootless)
        )
        return report
    counts, reserved, required = bridge_plan(model, current, direct | other)
    status = (
        "FAIL"
        if counts["docker_overlap"]
        else "WARN"
        if any(counts.values())
        else "PASS"
    )
    report.append(
        finding(
            "IPAM",
            status,
            "新規bridgeのIPAMと、既存ネットワークの関係を確認しました。",
            **counts,
        )
    )
    if rootless or not local:
        report.append(
            finding(
                "IPV4_POOL",
                "SKIP",
                "Engineとホストのネットワーク空間が異なる場合は空き数を概算しません。",
            )
        )
    elif not routes_known and required:
        report.append(
            finding(
                "IPV4_POOL", "SKIP", "ホスト経路なしでは空き数を概算しません。"
            )
        )
    else:
        report.append(pool_finding(docker, reserved, direct, required))
    report.append(ports_finding(docker, model, local=local, rootless=rootless))
    return report


def main():
    """終了1は確定した配備問題、終了2は使い方の問題を示す。"""
    parser = Parser(description=__doc__)
    parser.add_argument(
        "--env-file",
        action="append",
        default=[],
        help="非公開環境ファイル。指定順で読み込みます。",
    )
    parser.add_argument(
        "-f",
        "--file",
        action="append",
        default=[],
        help="Composeファイル。指定順でマージします。",
    )
    parser.add_argument(
        "--profile",
        action="append",
        default=[],
        help="対象プロファイル。複数指定できます。",
    )
    parser.add_argument("-p", "--project-name", help="DokployのApp Name。")
    parser.add_argument(
        "--sudo",
        action="store_true",
        help="Dockerは sudo -n docker で読み取ります。",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="固定診断コードと件数をJSONで出力します。",
    )
    args = parser.parse_args()
    try:
        report = checks(args)
    except (TypeError, ValueError, KeyError, AttributeError):
        report = [
            finding(
                "MODEL",
                "WARN",
                "このComposeまたはEngineの出力形式に対応できません。",
            )
        ]
    status = 1 if any(item["status"] == "FAIL" for item in report) else 0
    if args.json:
        print(
            json.dumps(
                {"findings": report, "exit_code": status}, ensure_ascii=False
            )
        )
    else:
        for item in report:
            count_text = " ".join(
                f"{key}={value}"
                for key, value in item.items()
                if isinstance(value, int)
            )
            print(
                f"{item['status']} {item['code']} {item['message']}",
                count_text,
            )
    return status


if __name__ == "__main__":
    raise SystemExit(main())

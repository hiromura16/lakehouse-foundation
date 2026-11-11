#!/usr/bin/env python3
"""Generate built-in Grafana templates; never contact or update Grafana."""

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DESTINATION = ROOT / "infra/monitoring/dashboards"
DATASOURCE = {"type": "prometheus", "uid": "foundation-prometheus"}
FS = 'fstype!~"tmpfs|squashfs|overlay|nsfs|proc|sysfs|devtmpfs"'
CONTAINER = (
    'container_label_com_docker_compose_project!="",'
    'container_label_com_docker_compose_service!="",name!="",'
    'container_label_com_docker_compose_project=~"$project",'
    'container_label_com_docker_compose_service=~"$service",'
    'name=~"$container",id!="/"'
)


def panel(index, title, expression, unit="short"):
    """Use only built-in time-series panels and stable datasource UIDs."""
    return {
        "id": index,
        "title": title,
        "type": "timeseries",
        "datasource": DATASOURCE,
        "gridPos": {
            "x": ((index - 1) % 2) * 12,
            "y": ((index - 1) // 2) * 8,
            "w": 12,
            "h": 8,
        },
        "targets": [{"refId": "A", "expr": expression}],
        "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
    }


def notice(index, title, body):
    """Make unavailable evidence visible instead of displaying fake zeros."""
    model = panel(index, title, "")
    model.pop("targets")
    model.pop("datasource")
    model["type"] = "text"
    model["options"] = {"mode": "markdown", "content": body}
    return model


def variable(name, query):
    """Derive selectors from emitted metrics rather than deployment IDs."""
    return {
        "name": name,
        "type": "query",
        "datasource": DATASOURCE,
        "query": query,
        "refresh": 1,
        "multi": True,
        "includeAll": True,
        "allValue": ".+",
        "current": {"text": "All", "value": "$__all"},
    }


def dashboard(uid, title, panels, variables):
    """Supply an editable create-only seed without external plugins."""
    return {
        "uid": uid,
        "title": title,
        "schemaVersion": 41,
        "version": 1,
        "editable": True,
        "tags": ["lakehouse", "source-template"],
        "timezone": "browser",
        "time": {"from": "now-1h", "to": "now"},
        "refresh": "30s",
        "panels": panels,
        "templating": {"list": variables},
        "links": [],
    }


def templates():
    """Describe host and socket-free container views with explicit gaps."""
    host = 'instance=~"$instance"'
    host_specs = [
        (
            "CPU used (%)",
            "100 * (1 - avg by(instance) "
            f'(rate(node_cpu_seconds_total{{{host},mode="idle"}}[5m])))',
            "percent",
        ),
        (
            "CPU modes",
            f"sum by(mode) (rate(node_cpu_seconds_total{{{host}}}[5m]))",
            "short",
        ),
        (
            "Logical CPUs",
            f'count(node_cpu_seconds_total{{{host},mode="idle"}})',
            "short",
        ),
        (
            "Load 1 / 5 / 15 minutes",
            f'{{__name__=~"node_load1|node_load5|node_load15",{host}}}',
            "short",
        ),
        (
            "Memory available",
            f"node_memory_MemAvailable_bytes{{{host}}}",
            "bytes",
        ),
        (
            "Memory used",
            f"node_memory_MemTotal_bytes{{{host}}} - "
            f"node_memory_MemAvailable_bytes{{{host}}}",
            "bytes",
        ),
        (
            "Memory cache / buffers",
            f'{{__name__=~"node_memory_Cached_bytes|'
            f'node_memory_Buffers_bytes",{host}}}',
            "bytes",
        ),
        (
            "Swap used",
            f"node_memory_SwapTotal_bytes{{{host}}} - "
            f"node_memory_SwapFree_bytes{{{host}}}",
            "bytes",
        ),
        (
            "Swap pages in/out per second",
            f"rate({{__name__=~"
            f'"node_vmstat_pswpin|node_vmstat_pswpout",{host}}}[5m])',
            "ops",
        ),
        (
            "Filesystem used (%)",
            f"100 * (1 - node_filesystem_avail_bytes"
            f"{{{host},{FS}}} / node_filesystem_size_bytes)",
            "percent",
        ),
        (
            "Filesystem available",
            f"node_filesystem_avail_bytes{{{host},{FS}}}",
            "bytes",
        ),
        (
            "Inodes used (%)",
            f"100 * (1 - node_filesystem_files_free"
            f"{{{host},{FS}}} / (node_filesystem_files > 0))",
            "percent",
        ),
        (
            "Disk read/write bytes",
            f'rate({{__name__=~"node_disk_read_bytes_'
            f'total|node_disk_written_bytes_total",{host}}}[5m])',
            "Bps",
        ),
        (
            "Disk read/write operations",
            f'rate({{__name__=~"node_disk_reads_'
            f'completed_total|node_disk_writes_completed_total",{host}}}[5m])',
            "iops",
        ),
        (
            "CPU iowait (%)",
            f"100 * avg(rate(node_cpu_seconds_total{{{host},"
            'mode="iowait"}[5m]))',
            "percent",
        ),
        ("Uptime", f"time() - node_boot_time_seconds{{{host}}}", "s"),
        ("Processes", f"node_processes_pids{{{host}}}", "short"),
    ]
    host_panels = [panel(i, *spec) for i, spec in enumerate(host_specs, 1)]
    host_panels.append(
        notice(
            len(host_panels) + 1,
            "Network / TCP / systemd: acceptance pending",
            "Host root/PID access does not prove host network namespace "
            "access. "
            "Network/TCP and systemd collectors are deliberately disabled. "
            "No host-network or D-Bus permission is granted by this template.",
        )
    )
    container_specs = [
        (
            "Observed project containers",
            f"count(container_last_seen{{{CONTAINER}}} > time() - 60)",
            "short",
        ),
        (
            "CPU cores",
            f"rate(container_cpu_usage_seconds_total{{{CONTAINER}}}[5m])",
            "short",
        ),
        (
            "Working set",
            f"container_memory_working_set_bytes{{{CONTAINER}}}",
            "bytes",
        ),
        (
            "Finite memory limit used (%)",
            "100 * "
            "foundation:container_memory_used:ratio{"
            'container_label_com_docker_compose_project=~"$project",'
            'container_label_com_docker_compose_service=~"$service",'
            'name=~"$container"}',
            "percent",
        ),
        (
            "Network receive/transmit",
            f'rate({{__name__=~"container_network_'
            f'receive_bytes_total|container_network_transmit_bytes_total",'
            f"{CONTAINER}}}[5m])",
            "Bps",
        ),
        (
            "Filesystem usage",
            f"container_fs_usage_bytes{{{CONTAINER}}}",
            "bytes",
        ),
        (
            "Emitted OOM events",
            f"increase(container_oom_events_total{{{CONTAINER}}}[10m])",
            "short",
        ),
        (
            "Start-time changes (not a restart counter)",
            f"changes(container_start_time_seconds{{{CONTAINER}}}[1h])",
            "short",
        ),
    ]
    container_panels = [
        panel(i, *spec) for i, spec in enumerate(container_specs, 1)
    ]
    container_panels.append(
        notice(
            len(container_panels) + 1,
            "Socket-free metadata may be unavailable",
            "Docker 29/cgroup v2 metrics and Compose labels require separate "
            "runtime acceptance. Empty selectors mean missing evidence; they "
            "are not zero containers. No Docker socket, privileged mode or "
            "synthetic restart/health/JVM metric is supplied.",
        )
    )
    return {
        "host.json": dashboard(
            "lakehouse-host-overview",
            "Lakehouse Host Overview",
            host_panels,
            [
                variable(
                    "instance",
                    "label_values(node_cpu_seconds_total, instance)",
                )
            ],
        ),
        "containers.json": dashboard(
            "lakehouse-container-overview",
            "Lakehouse Container Overview",
            container_panels,
            [
                variable(
                    "project",
                    "label_values(container_last_seen, "
                    "container_label_com_docker_compose_project)",
                ),
                variable(
                    "service",
                    "label_values(container_last_seen{"
                    "container_label_com_docker_compose_project=~"
                    '"$project"},container_label_com_docker_compose_service)',
                ),
                variable(
                    "container",
                    "label_values(container_last_seen{"
                    "container_label_com_docker_compose_project=~"
                    '"$project",container_label_com_docker_compose_service'
                    '=~"$service"},name)',
                ),
            ],
        ),
    }


def main():
    """Write deterministic public templates or check committed derivatives."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    for name, model in templates().items():
        target = DESTINATION / name
        content = json.dumps(model, ensure_ascii=False, indent=2) + "\n"
        if args.check:
            if not target.exists() or target.read_text() != content:
                raise SystemExit("Regenerate monitoring templates: " + name)
        else:
            target.write_text(content)


if __name__ == "__main__":
    main()

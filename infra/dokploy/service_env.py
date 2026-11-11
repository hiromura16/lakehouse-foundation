"""Reconcile only Dokploy's managed environment, without logging its values.

Docker calls capture stdout/stderr: CLI failures can contain credentials or
private network details. Diagnostics expose state names and SET/NOT_SET only.
This helper is invoked exclusively after the installer's ownership checks.
"""

import argparse
import json
import subprocess
import sys
import time

from config import read_env, service_environment

MANAGED_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "BUILDKIT_NO_CLIENT_TOKEN",
)


class ServiceError(ValueError):
    """Carry only application-owned text and filtered service state."""


def docker(arguments, timeout=15):
    """Execute one bounded CLI call; never echo private arguments or output."""
    try:
        result = subprocess.run(
            ["docker", *arguments],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ServiceError(
            "Dokploy Docker operation failed; details withheld."
        ) from None
    if result.returncode:
        raise ServiceError(
            "Dokploy Docker operation failed; details withheld."
        )
    return result.stdout


def environment_arguments(current, desired):
    """Return a single idempotent update, preserving every unmanaged key.

    Comparing lists detects old duplicate assignments as well as value drift.
    Remove managed assignments before adding the complete canonical set.
    """
    present = [
        item for item in current if item.partition("=")[0] in MANAGED_KEYS
    ]
    wanted = [key + "=" + value for key, value in desired.items()]
    if sorted(present) == sorted(wanted):
        return []
    arguments = []
    for key in MANAGED_KEYS:
        if any(item.partition("=")[0] == key for item in present):
            arguments.extend(["--env-rm", key])
    for item in wanted:
        arguments.extend(["--env-add", item])
    return arguments


def wait_ready(timeout=120, clock=time.monotonic, sleep=time.sleep):
    """Wait for the new service spec, rather than accepting an old 1/1 task."""
    deadline = clock() + timeout
    replicas = "NOT_SET"
    task_state = "NOT_SET"
    task_error = "NOT_SET"
    while clock() < deadline:
        remaining = max(0.01, min(10, deadline - clock()))
        service = json.loads(
            docker(["service", "inspect", "dokploy"], remaining)
        )[0]
        replicas = docker(
            [
                "service",
                "ls",
                "--filter",
                "name=^dokploy$",
                "--format",
                "{{.Replicas}}",
            ],
            max(0.01, min(10, deadline - clock())),
        ).strip()
        tasks = docker(
            [
                "service",
                "ps",
                "--no-trunc",
                "--filter",
                "desired-state=running",
                "--format",
                "{{json .}}",
                "dokploy",
            ],
            max(0.01, min(10, deadline - clock())),
        )
        rows = [json.loads(line) for line in tasks.splitlines() if line]
        states = [row.get("CurrentState", "").split(" ")[0] for row in rows]
        # Task errors are untrusted Docker text and may contain private URLs.
        task_error = (
            "SET" if any(row.get("Error") for row in rows) else ("NOT_SET")
        )
        safe = {
            "Running",
            "Pending",
            "Preparing",
            "Starting",
            "Rejected",
            "Failed",
            "Shutdown",
            "New",
            "Assigned",
            "Ready",
        }
        task_state = ",".join(s if s in safe else "OTHER" for s in states)
        update = service.get("UpdateStatus", {}).get("State", "completed")
        if (
            replicas == "1/1"
            and states == ["Running"]
            and (update == "completed")
        ):
            return
        if update in {"paused", "rollback_paused", "rollback_completed"}:
            break
        sleep(min(2, max(0, deadline - clock())))
    # Even replica counts are parsed before display; never print raw CLI text.
    if not replicas.replace("/", "").isdigit():
        replicas = "NOT_SET"
    raise ServiceError(
        f"Dokploy did not converge: replicas={replicas}; "
        f"task_state={task_state}; task_error={task_error} "
        "(private error details withheld). Preserve volumes; inspect locally."
    )


def synchronize(desired):
    """Update env once if needed, preserving the service and its state."""
    service = json.loads(docker(["service", "inspect", "dokploy"]))[0]
    specification = service["Spec"]
    if specification.get("Labels", {}).get("lab.owner") != (
        "lakehouse-foundation"
    ):
        raise ServiceError("Refusing an unmanaged Dokploy service.")
    current = specification["TaskTemplate"]["ContainerSpec"].get("Env", [])
    arguments = environment_arguments(current, desired)
    if arguments:
        docker(["service", "update", "--detach=true", *arguments, "dokploy"])
    wait_ready()


def main():
    """Apply the already-reviewed installer operation; no arbitrary shell."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", required=True)
    parser.add_argument("operation", choices=["create", "sync"])
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    desired = service_environment(read_env(args.env_file))
    if args.operation == "sync":
        synchronize(desired)
    else:
        command = args.arguments
        if command[:1] == ["--"]:
            command = command[1:]
        if command[:2] != ["service", "create"]:
            raise ValueError("Expected a Dokploy service creation command.")
        # Insert all env options before the pinned image argument.
        options = [
            part
            for key, value in desired.items()
            for part in ("--env", key + "=" + value)
        ]
        docker([*command[:-1], *options, command[-1]], timeout=120)
        wait_ready()


if __name__ == "__main__":
    try:
        main()
    except ServiceError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None
    except (ValueError, KeyError, TypeError, IndexError):
        # Validation and convergence messages contain only fixed text/status.
        # JSON/CLI payloads must never be interpolated into an exception.
        print(
            "Dokploy environment operation failed; inspect private state.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None

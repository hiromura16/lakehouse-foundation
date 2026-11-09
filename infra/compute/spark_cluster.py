"""Run the fixed single-host Spark services without background daemons."""

import argparse
import json
import os
import stat
import urllib.error
import urllib.request
from pathlib import Path

VOLUME_PATHS = (Path("/opt/spark/events"), Path("/opt/spark/work"))
CLASSES = {
    "master": "org.apache.spark.deploy.master.Master",
    "worker": "org.apache.spark.deploy.worker.Worker",
    "history": "org.apache.spark.deploy.history.HistoryServer",
}
HEALTH_URLS = {
    "master": "http://127.0.0.1:8080/json/",
    "worker": "http://127.0.0.1:8081/json/",
    "history": "http://127.0.0.1:18080/api/v1/applications",
}


def initialise_volumes(uid, gid, paths=VOLUME_PATHS):
    """Own only the two mount roots, without traversing stored user files."""
    if not uid.isdecimal() or int(uid) == 0 or not gid.isdecimal():
        raise ValueError("Use a non-root worker UID and a numeric GID.")
    for path in paths:
        metadata = path.lstat()
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("A fixed Spark volume root must be a directory.")
    for path in paths:
        # No recursion or symlink following: existing event/work data stays.
        os.chown(path, int(uid), int(gid), follow_symlinks=False)
        os.chmod(path, 0o2770, follow_symlinks=False)


def command(role):
    """Return the fixed foreground class and internal networking options."""
    arguments = ["/opt/spark/bin/spark-class", CLASSES[role]]
    if role == "master":
        arguments += [
            "--host",
            "spark-master",
            "--port",
            "7077",
            "--webui-port",
            "8080",
        ]
    elif role == "worker":
        arguments += [
            "--host",
            "spark-worker",
            "--port",
            "7078",
            "--webui-port",
            "8081",
            "--cores",
            "2",
            "--memory",
            "2g",
            "--work-dir",
            "/opt/spark/work/worker",
            "spark://spark-master:7077",
        ]
    return arguments


def healthy(role):
    """Probe one fixed local JSON endpoint, bypassing every outbound proxy."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(HEALTH_URLS[role], timeout=3) as response:
            payload = json.loads(response.read(1024 * 1024))
        if role == "master":
            return payload.get("status") == "ALIVE"
        if role == "worker":
            return payload.get("masterurl") == "spark://spark-master:7077"
        return isinstance(payload, list)
    except (OSError, ValueError, AttributeError, urllib.error.URLError):
        return False


def main():
    """Initialise dedicated roots or replace this process with one daemon."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["init", *CLASSES, "health"])
    parser.add_argument("role", nargs="?", choices=CLASSES)
    args = parser.parse_args()
    if args.action == "init":
        initialise_volumes(
            os.environ.get("WORKER_UID", ""),
            os.environ.get("WORKER_GID", ""),
        )
        print("Spark volume roots ready; existing files retained.")
        return
    if args.action == "health":
        if args.role is None:
            parser.error("health requires a fixed service role.")
        raise SystemExit(0 if healthy(args.role) else 1)
    directory = Path("/opt/spark/work") / args.action
    directory.mkdir(mode=0o2770, parents=True, exist_ok=True)
    os.execvpe(command(args.action)[0], command(args.action), os.environ)


if __name__ == "__main__":
    main()

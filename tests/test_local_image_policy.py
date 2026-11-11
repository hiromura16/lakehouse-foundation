"""Render local image policies with Compose; never pull/build/start images."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
FILES = [
    ROOT / "infra" / name / "compose.yaml"
    for name in (
        "lakehouse",
        "compute",
        "orchestration",
        "demo",
        "cdc",
        "poc",
        "mcp",
    )
]
BUILDERS = {
    "worker",
    "airflow",
    "demo-api",
    "cdc-register",
    "superset-init",
    "jupyter",
    "mcp",
    "spark-volumes-init",
}
CONSUMERS = {
    "demo",
    "cdc-consumer",
    "superset",
    "spark-master",
    "spark-worker",
    "spark-history",
    "spark-check",
}


class LocalImagePolicyTests(unittest.TestCase):
    """Single builders own local tags; official images keep their policy."""

    def test_every_local_tag_has_one_builder_and_explicit_consumer_policy(
        self,
    ):
        services = {}
        for path in FILES:
            services.update(yaml.safe_load(path.read_text())["services"])
        changed = set()
        groups = {}
        for name, service in services.items():
            image = service.get("image", "")
            if not image.endswith(":local"):
                self.assertNotIn("pull_policy", service)
                continue
            changed.add(name)
            groups.setdefault(image, []).append(name)
            self.assertEqual(
                service["pull_policy"],
                "build" if name in BUILDERS else "never",
            )
            self.assertEqual("build" in service, name in BUILDERS)
        self.assertEqual(changed, BUILDERS | CONSUMERS)
        for names in groups.values():
            self.assertEqual(len(set(names) & BUILDERS), 1)
        smoke = services["spark-check"]
        self.assertEqual(smoke["deploy"]["replicas"], 0)
        self.assertEqual(smoke["restart"], "no")
        self.assertIn("spark-work:/opt/spark/work", smoke["volumes"])
        self.assertEqual(smoke["command"][2], "local[2]")

    @unittest.skipUnless(shutil.which("docker"), "Docker CLI unavailable")
    def test_compose_policies_in_core_compute_notebooks_and_combined_profiles(
        self,
    ):
        """Rendering proves configuration, never cold-start compatibility."""
        with tempfile.TemporaryDirectory(prefix="image-policy-unit-") as name:
            directory = Path(name)
            env_file = directory / "private/site.env"
            subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/configure.py"),
                    "--output",
                    str(env_file),
                ],
                check=True,
                capture_output=True,
            )
            values = dict(
                line.split("=", 1)
                for line in env_file.read_text().splitlines()
                if line and not line.startswith("#")
            )
            for profiles in (
                "",
                "compute",
                "notebooks",
                "compute,notebooks",
                "demo,monitoring,bi",
            ):
                with self.subTest(profiles=profiles):
                    current = {
                        **values,
                        "COMPOSE_PROFILES": profiles,
                        "COMPOSE_PROJECT_NAME": "synthetic-images",
                        "NOTEBOOKS_DIR": str(directory / "notebooks"),
                    }
                    env_file.write_text(
                        "".join(
                            key + "=" + value + "\n"
                            for key, value in current.items()
                        )
                    )
                    command = [
                        "docker",
                        "compose",
                        "--env-file",
                        str(env_file),
                    ]
                    for path in FILES:
                        command += ["-f", str(path)]
                    command += ["config", "--format", "json"]
                    environment = {**os.environ, **current}
                    result = subprocess.run(
                        command,
                        env=environment,
                        capture_output=True,
                        text=True,
                        check=True,
                    )
                    services = json.loads(result.stdout)["services"]
                    for service, item in services.items():
                        if service in BUILDERS | CONSUMERS:
                            self.assertEqual(
                                item["pull_policy"],
                                "build" if service in BUILDERS else "never",
                            )
                            self.assertTrue(
                                item["image"].startswith("synthetic-images-")
                            )
                    self.assertEqual(
                        "spark-master" in services, "compute" in profiles
                    )
                    self.assertEqual(
                        "jupyter" in services, "notebooks" in profiles
                    )
                    self.assertEqual(
                        "spark-volumes-init" in services,
                        bool(
                            {"compute", "notebooks"} & set(profiles.split(","))
                        ),
                    )


if __name__ == "__main__":
    unittest.main()

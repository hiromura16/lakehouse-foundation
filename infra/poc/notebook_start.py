"""Seed new notebook files without replacing an operator's saved work."""

import os
import shutil
from pathlib import Path


def main():
    """Require a private token and hide the token-bearing startup banner."""
    token = os.environ.get("JUPYTER_TOKEN", "")
    if len(token) < 32:
        raise ValueError("Generate a private Jupyter token before starting.")
    workspace = Path("/workspace")
    if not workspace.is_dir() or not os.access(workspace, os.W_OK):
        raise ValueError("Mount a workspace writable by the configured UID.")
    for source in Path("/opt/lakehouse/notebooks").glob("*.ipynb"):
        destination = workspace / source.name
        if not destination.exists():
            with destination.open("xb") as output:
                output.write(source.read_bytes())
    py4j = list(Path("/opt/spark/python/lib").glob("py4j-*-src.zip"))
    if len(py4j) != 1:
        raise ValueError("Expected the Spark image's single bundled Py4J.")
    os.environ["PYTHONPATH"] = ":".join(
        ["/opt/lakehouse", "/opt/spark/python", str(py4j[0])]
    )
    executable = shutil.which("jupyter-lab")
    if executable is None:
        raise ValueError("The locked Jupyter environment is missing.")
    os.execv(
        executable, [executable, "--config=/opt/lakehouse/notebook_config.py"]
    )


if __name__ == "__main__":
    main()

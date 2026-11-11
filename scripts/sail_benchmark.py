#!/usr/bin/env python3
"""Plan or explicitly run the fixed synthetic Spark/Sail lab comparison.

This entry point intentionally does not start/stop Compose services, install
dependencies, read site.env, or accept arbitrary SQL/destinations/endpoints.
Only the operator-approved run subcommand writes dedicated lab tables.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# The official Spark image bundles Py4J as one zip. Unlike spark-submit,
# this Python entrypoint does not add it automatically. Select only that
# image-owned fixed directory; never fetch/reinstall a mismatched client.
bundled_py4j = list(Path("/opt/spark/python/lib").glob("py4j-*-src.zip"))
if len(bundled_py4j) == 1:
    sys.path.append(str(bundled_py4j[0]))

from infra.sail.runner import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())

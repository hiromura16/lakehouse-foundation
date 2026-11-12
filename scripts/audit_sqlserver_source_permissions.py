"""Run the conservative full DB permission audit separately from startup.

Use the approved SOURCE environment in a private session. No raw names,
credentials or driver messages are printed. No grants or SQL writes occur.
"""

import json
import os
import sys

import sqlserver_demo as adapter


def main():
    """Return a conservative audit verdict, not a runtime registration gate."""
    try:
        selected = adapter.validate_settings(dict(os.environ), role="source")
        with adapter.connection(selected, role="source") as handle:
            cursor = handle.cursor()
            adapter.ownership(cursor, selected, role="source")
            passed = adapter.permissions_safe(
                cursor, role="source", full_audit=True
            )
        print(json.dumps({"full_source_permission_audit": bool(passed)}))
        return 0 if passed else 1
    except Exception:
        print('{"full_source_permission_audit":"unavailable"}')
        return 2


if __name__ == "__main__":
    sys.exit(main())

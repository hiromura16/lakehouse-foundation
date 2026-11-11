#!/bin/sh
# Explicit composition works for CLI and raw Dokploy Compose alike.
# No chown/chmod, daemon access, secret rendering, or broad env interpolation.
set -eu
# Inspect only; a stale named volume is never repaired during startup. GNU
# stat does not follow the selected leaf; the immutable image owns ancestors.
expected="$(id -u):$(id -g):700"
actual="$(stat -c '%u:%g:%a' -- /var/lib/vector 2>/dev/null || true)"
if [ "$(id -u)" = 0 ] || [ -L /var/lib/vector ] || \
   [ ! -d /var/lib/vector ] || [ ! -w /var/lib/vector ] || \
   [ "$actual" != "$expected" ]; then
  printf '%s\n' 'Vector state is not prepared for the selected runtime user.' >&2
  exit 78
fi
case ",${COMPOSE_PROFILES:-}," in
  *,openobserve-lab,*)
    if [ ! -r /run/secrets/openobserve-vector.json ]; then
      printf '%s\n' 'Vector lab ingestion credentials are not prepared.' >&2
      exit 78
    fi
    exec /usr/bin/vector --log-format json \
      --config common.yaml --config loki.yaml --config openobserve.yaml
    ;;
  *)
    exec /usr/bin/vector --log-format json \
      --config common.yaml --config loki.yaml
    ;;
esac

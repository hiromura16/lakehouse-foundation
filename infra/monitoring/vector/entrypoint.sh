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
set -- --log-format json --config common.yaml --config loki.yaml
case ",${COMPOSE_PROFILES:-}," in
  *,openobserve-lab,*)
    token_file=/run/secrets/openobserve-vector.json
    token_owner="$(stat -c '%u:%a' -- "$token_file" 2>/dev/null || true)"
    if [ ! -f "$token_file" ] || [ -L "$token_file" ] || \
       [ ! -r "$token_file" ] || \
       [ "$token_owner" != "$(id -u):600" ] || \
       [ "$(wc -c < "$token_file")" -gt 4096 ] || \
       ! tr -d '\n\r' < "$token_file" | grep -Eq '^[[:space:]]*\{[[:space:]]*"token"[[:space:]]*:[[:space:]]*"o2oi_[A-Za-z0-9]{32}"[[:space:]]*\}[[:space:]]*$'; then
      printf '%s\n' 'Vector lab ingestion credentials are not prepared.' >&2
      exit 78
    fi
    set -- "$@" --config openobserve.yaml
    ;;
  *)
    ;;
esac
case ",${COMPOSE_PROFILES:-}," in
  *,monitoring-host,*)
    set -- "$@" --config host-expected.yaml
    ;;
esac
exec /usr/bin/vector "$@"

#!/usr/bin/env bash
# Generate/check/install proxy configuration. Never restarts Docker.
set -Eeuo pipefail
umask 077
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
mode=plan
input_file=
usage() {
  cat <<'USAGE'
Usage:
  configure-proxy.sh --init /absolute/private/proxy.env
  configure-proxy.sh --env-file /absolute/private/proxy.env [--plan]
  sudo configure-proxy.sh --env-file /absolute/private/proxy.env --apply
Default is read-only plan. --init creates a new 0600 template and parent (0700).
--apply installs one managed systemd drop-in and root-only environment file.
Docker is never restarted; existing unrelated drop-ins are never changed.
USAGE
}
while (($#)); do
  case "$1" in
    --init)
      mode=init
      shift
      input_file=${1:?Missing output path}
      ;;
    --env-file)
      shift
      input_file=${1:?Missing input path}
      ;;
    --plan)
      mode=plan
      ;;
    --apply)
      mode=apply
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      usage >&2
      exit 2
      ;;
  esac
  shift
done
if [[ -z "$input_file" || "$input_file" != /* ]]; then
  usage >&2
  exit 2
fi
command -v python3 >/dev/null
# Python validates settings and writes files; this wrapper only parses CLI.
exec python3 "$SCRIPT_DIR/proxy_settings.py" "$mode" "$input_file" "$REPO_DIR"

#!/usr/bin/env bash
# Portable, private Dokploy foundation. The default operation only prints a plan.
set +x  # Private env and Docker arguments must never enter shell trace output.
set -Eeuo pipefail
umask 077
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
OWNER=lakehouse-foundation
CONFIG=/etc/lakehouse-foundation/dokploy
GATEWAY=lakehouse-dokploy-gateway
DOKPLOY_IMAGE=dokploy/dokploy:v0.31.0@sha256:971c5879c059f60390e1c4efeaf63a39d75cbc2157e48e81bd738587afe339f9
POSTGRES_IMAGE=postgres:16@sha256:43f9275df91e012c3da3d86bea5d7bb5e67a473296cc906c5f9e80b8176b1a37
TRAEFIK_IMAGE=traefik:v3.6.25@sha256:996b026a32b1bb6c85c200e5cf25276c1caa84ddea6b1e3a33e78cdc5cbe8d0c
NGINX_IMAGE=nginx:stable-alpine@sha256:bb8f722b4be005fcd7827816a136ab779fd40185d85b5171be915cc7fc0575c7
ENV_FILE=
APPLY=false

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
usage() {
  printf 'Usage: bash infra/dokploy/install.sh --env-file /private/path/foundation.env [--apply]\n'
  printf 'Without --apply, validate configuration and print the plan without host changes.\n'
}
while (($#)); do
  case "$1" in
    --env-file) (($# >= 2)) || die '--env-file needs a path.'; ENV_FILE=$2; shift 2 ;;
    --apply) APPLY=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die 'Unknown argument.' ;;
  esac
done
[[ -n "$ENV_FILE" ]] || die 'An external private --env-file is required.'
command -v python3 >/dev/null || die 'Python 3 is required.'

# Do not source shell code or import unrelated source-system credentials.
VALUES=$(python3 "$SCRIPT_DIR/config.py" read-env "$ENV_FILE") ||
  die 'Configuration validation failed.'

mapfile -t SETTINGS <<< "$VALUES"
BIND_IP=${SETTINGS[0]}
ALLOWED_CIDRS=${SETTINGS[1]}
ADMIN_PORT=${SETTINGS[2]}
ADVERTISE_ADDR=${SETTINGS[3]}
ADDRESS_POOL=${SETTINGS[4]}
SUBNET_SIZE=${SETTINGS[5]}
BRIDGE_CIDR=${SETTINGS[6]:-}
BIND_IPS=(127.0.0.1)
[[ $BIND_IP == 127.0.0.1 ]] || BIND_IPS+=("$BIND_IP")

python3 "$SCRIPT_DIR/config.py" service-plan "$ENV_FILE"
[[ $APPLY == true ]] || exit 0
[[ $EUID == 0 ]] || die '--apply requires root; preserve --env-file when invoking sudo.'
[[ $(uname -m) == x86_64 ]] || die 'This image lock supports x86-64 Linux hosts.'
for cmd in docker ip ss systemctl curl openssl install; do
  command -v "$cmd" >/dev/null || die "Required command is missing: $cmd"
done
# Never start a stopped daemon implicitly: doing so can restart unrelated workloads.
docker info >/dev/null 2>&1 || die 'Docker must already be running and accessible. Review existing restart policies before starting it manually.'
for addr in "$BIND_IP" "$ADVERTISE_ADDR"; do
  ip -o -4 addr show | awk '{print $4}' | cut -d/ -f1 | grep -Fxq "$addr" || die "Address not assigned to this host: $addr"
done

check_owned() {
  local kind=$1 name=$2 format=$3
  if docker "$kind" inspect "$name" >/dev/null 2>&1; then
    [[ $(docker "$kind" inspect --format "$format" "$name") == "$OWNER" ]] || die "Existing unrelated $kind: $name"
  fi
}
for name in dokploy dokploy-postgres; do
  check_owned service "$name" '{{index .Spec.Labels "lab.owner"}}'
  check_owned volume "$name" '{{index .Labels "lab.owner"}}'
done
for name in dokploy-traefik "$GATEWAY"; do
  check_owned container "$name" '{{index .Config.Labels "lab.owner"}}'
done
check_owned network dokploy-network '{{index .Labels "lab.owner"}}'
for name in dokploy_postgres_password dokploy_auth_secret; do
  check_owned secret "$name" '{{index .Spec.Labels "lab.owner"}}'
done
if [[ -e /etc/dokploy && ! -f "$CONFIG/managed-swarm" ]]; then
  die '/etc/dokploy already exists without this installation identity.'
fi
swarm_state=$(docker info --format '{{.Swarm.LocalNodeState}}')
if [[ $swarm_state == active ]]; then
  [[ -f "$CONFIG/managed-swarm" ]] || die 'An unrelated Swarm is active; it will not be joined or reset.'
  [[ $(cat "$CONFIG/managed-swarm") == "$(docker info --format '{{.Swarm.NodeID}}')" ]] || die 'Swarm identity differs from the installation record.'
  [[ $(docker info --format '{{.Swarm.ControlAvailable}}') == true ]] || die 'Run on the recorded Swarm manager.'
  docker info --format '{{json .Swarm.Cluster}}' |
    python3 "$SCRIPT_DIR/config.py" swarm "$ADDRESS_POOL" "$SUBNET_SIZE"
elif [[ $swarm_state == inactive ]]; then
  [[ ! -e "$CONFIG/managed-swarm" ]] || die 'A previous Swarm identity exists; inspect it before reinitializing.'
else
  die "Unexpected Swarm state: $swarm_state"
fi

check_port() {
  local protocol=$1 published=$2 container=$3 target=$4 flag
  if [[ $protocol == tcp ]]; then flag=-ltn; else flag=-lun; fi
  if ss -H "$flag" "sport = :$published" | grep -q .; then
    [[ -n $container ]] || die "$protocol port $published is already occupied."
    [[ $(docker container inspect --format '{{.State.Running}}' "$container" 2>/dev/null) == true ]] || die "$protocol port $published is occupied by another service."
    docker container port "$container" "$target/$protocol" | awk -v port="$published" '
      $NF ~ ":" port "$" { found=1 } END { exit !found }
    ' || die "$protocol port $published is not published by $container."
  fi
}
check_port tcp "$ADMIN_PORT" "$GATEWAY" 80
check_port tcp 80 dokploy-traefik 80
check_port tcp 443 dokploy-traefik 443
check_port udp 443 dokploy-traefik 443
if [[ $swarm_state == inactive ]]; then
  check_port tcp 2377 '' 2377
  check_port tcp 7946 '' 7946
  check_port udp 7946 '' 7946
  check_port udp 4789 '' 4789
fi
# Refuse old or wildcard bindings rather than silently retaining a previous network.
for name in "$GATEWAY" dokploy-traefik; do
  if docker container inspect "$name" >/dev/null 2>&1; then
    kind=traefik
    if [[ $name == "$GATEWAY" ]]; then
      kind=gateway
    fi
    docker container inspect --format '{{json .HostConfig.PortBindings}}' "$name" |
      python3 "$SCRIPT_DIR/config.py" ports "$kind" "$ADMIN_PORT" \
        "$BIND_IP" "${BIND_IPS[@]}"
  fi
done

# Detect address-pool overlap with assigned host networks before initializing Swarm.
ip -j -4 addr show |
  python3 "$SCRIPT_DIR/config.py" host-networks "$ADDRESS_POOL"

inspect_bridge() {
  docker network inspect docker_gwbridge --format '{{json .IPAM.Config}}' |
    python3 "$SCRIPT_DIR/config.py" bridge "$ADDRESS_POOL" "$BRIDGE_CIDR"
}
if docker network inspect docker_gwbridge >/dev/null 2>&1; then
  ACTUAL_BRIDGE=$(inspect_bridge) || die 'Docker gateway bridge validation failed.'
fi
# All checks above are read-only. Host changes begin here.
for image in "$DOKPLOY_IMAGE" "$POSTGRES_IMAGE" "$TRAEFIK_IMAGE" "$NGINX_IMAGE"; do
  docker pull "$image"
done
install -d -m 755 "$CONFIG" /etc/dokploy
if [[ $swarm_state == inactive ]]; then
  docker swarm init --advertise-addr "$ADVERTISE_ADDR" --listen-addr "$ADVERTISE_ADDR:2377" \
    --data-path-addr "$ADVERTISE_ADDR" --default-addr-pool "$ADDRESS_POOL" --default-addr-pool-mask-length "$SUBNET_SIZE"
  docker info --format '{{.Swarm.NodeID}}' > "$CONFIG/managed-swarm"
  chmod 600 "$CONFIG/managed-swarm"
fi
# Docker owns this bridge. Its detected subnet is allowed narrowly, without changing it.
ACTUAL_BRIDGE=$(inspect_bridge) || die 'Bridge verification failed. Preserve the initialized Swarm and inspect network settings before retrying.'
if ! docker network inspect dokploy-network >/dev/null 2>&1; then
  docker network create --driver overlay --attachable --label "lab.owner=$OWNER" dokploy-network
fi
for name in dokploy dokploy-postgres; do
  docker volume inspect "$name" >/dev/null 2>&1 || docker volume create --label "lab.owner=$OWNER" "$name"
done
for name in dokploy_postgres_password dokploy_auth_secret; do
  if ! docker secret inspect "$name" >/dev/null 2>&1; then
    openssl rand -hex 32 | docker secret create --label "lab.owner=$OWNER" "$name" -
  fi
done
if ! docker service inspect dokploy-postgres >/dev/null 2>&1; then
  docker service create --detach --name dokploy-postgres --label "lab.owner=$OWNER" \
    --constraint 'node.role==manager' --network dokploy-network \
    --env POSTGRES_USER=dokploy --env POSTGRES_DB=dokploy --env TZ=UTC \
    --secret source=dokploy_postgres_password,target=/run/secrets/postgres_password \
    --env POSTGRES_PASSWORD_FILE=/run/secrets/postgres_password \
    --mount type=volume,source=dokploy-postgres,target=/var/lib/postgresql/data \
    --log-driver json-file --log-opt max-size=10m --log-opt max-file=3 "$POSTGRES_IMAGE"
fi
if ! docker service inspect dokploy >/dev/null 2>&1; then
  python3 "$SCRIPT_DIR/service_env.py" --env-file "$ENV_FILE" create -- \
    service create --detach --name dokploy --label "lab.owner=$OWNER" \
    --replicas 1 --constraint 'node.role==manager' --network dokploy-network \
    --mount type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock \
    --mount type=bind,source=/etc/dokploy,target=/etc/dokploy \
    --mount type=volume,source=dokploy,target=/root/.docker \
    --secret source=dokploy_postgres_password,target=/run/secrets/postgres_password \
    --secret source=dokploy_auth_secret,target=/run/secrets/dokploy_auth_secret \
    --env POSTGRES_PASSWORD_FILE=/run/secrets/postgres_password \
    --env BETTER_AUTH_SECRET_FILE=/run/secrets/dokploy_auth_secret \
    --env TZ=UTC --env RELEASE_TAG=v0.31.0 \
    --update-parallelism 1 --update-order stop-first \
    --log-driver json-file --log-opt max-size=10m --log-opt max-file=3 "$DOKPLOY_IMAGE"
else
  # Reconcile only the seven managed env names; never recreate DB or Swarm.
  python3 "$SCRIPT_DIR/service_env.py" --env-file "$ENV_FILE" sync
fi
python3 "$SCRIPT_DIR/config.py" render-nginx "$CONFIG/nginx.conf" \
  "$ALLOWED_CIDRS" "$ADDRESS_POOL" "$ACTUAL_BRIDGE"
if ! docker container inspect "$GATEWAY" >/dev/null 2>&1; then
  UI_PORTS=()
  for addr in "${BIND_IPS[@]}"; do UI_PORTS+=(-p "$addr:$ADMIN_PORT:80"); done
  docker run -d --name "$GATEWAY" --label "lab.owner=$OWNER" \
    --restart unless-stopped --network dokploy-network \
    --mount "type=bind,source=$CONFIG/nginx.conf,target=/etc/nginx/nginx.conf,readonly" \
    "${UI_PORTS[@]}" --log-opt max-size=10m --log-opt max-file=3 "$NGINX_IMAGE"
else
  docker start "$GATEWAY" >/dev/null
  docker exec "$GATEWAY" nginx -t
  docker exec "$GATEWAY" nginx -s reload
fi
ready=false
deadline=$((SECONDS + 240))
while ((SECONDS < deadline)); do
  if curl --noproxy '*' --connect-timeout 2 --max-time 5 -fsS -o /dev/null "http://127.0.0.1:$ADMIN_PORT/" 2>/dev/null && [[ -s /etc/dokploy/traefik/traefik.yml ]]; then
    ready=true; break
  fi
  sleep 2
done
[[ $ready == true ]] || die 'Dokploy readiness deadline reached. Keep its volumes and inspect the deployment.'
if ! docker container inspect dokploy-traefik >/dev/null 2>&1; then
  docker run -d --name dokploy-traefik --label "lab.owner=$OWNER" \
    --restart unless-stopped --network dokploy-network \
    --mount type=bind,source=/etc/dokploy/traefik/traefik.yml,target=/etc/traefik/traefik.yml \
    --mount type=bind,source=/etc/dokploy/traefik/dynamic,target=/etc/dokploy/traefik/dynamic \
    --mount type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock,readonly \
    -p "$BIND_IP:80:80" -p "$BIND_IP:443:443/tcp" -p "$BIND_IP:443:443/udp" \
    --log-opt max-size=10m --log-opt max-file=3 "$TRAEFIK_IMAGE"
else
  docker start dokploy-traefik >/dev/null
fi
systemctl enable docker
printf 'Dokploy is ready. Use the private configured UI address.\n'
printf 'Keep API keys private. Recheck private port bindings after Dokploy UI upgrades.\n'

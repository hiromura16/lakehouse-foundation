#!/usr/bin/env bash
# GitHub の所有 runner だけで短命 broker と試験 worker を作る。
set -Eeuo pipefail
umask 077
[[ ${GITHUB_ACTIONS:-} == true &&
   ${RUNNER_ENVIRONMENT:-} == github-hosted &&
   ${GITHUB_REPOSITORY:-} == hiromura16/lakehouse-foundation &&
   ${RUNNER_OS:-} == Linux && ${RUNNER_ARCH:-} == X64 &&
   -d ${RUNNER_TEMP:-} && -S /var/run/docker.sock ]] || {
    echo 'FAIL: HOSTED_RUNNER_REQUIRED' >&2
    exit 1
}
scratch=$(mktemp -d "$RUNNER_TEMP/cdc-contract-fixture.XXXXXXXX")
unset DOCKER_CONTEXT DOCKER_HOST
export DOCKER_CONFIG="$scratch/config"
mkdir "$DOCKER_CONFIG"
id="cdc-contract-$(python3 -c 'import secrets; print(secrets.token_hex(12))')"
root=$(git rev-parse --show-toplevel)
stage=INITIALIZATION
net=''
d() {
    docker --host unix:///var/run/docker.sock "$@"
}
clean_container() {
    local cid label
    cid=$(<"$1")
    [[ $cid =~ ^[0-9a-f]{64}$ ]] || return 1
    label=$(d container inspect --format '{{index .Config.Labels "contract.fixture"}}' "$cid") ||
        return 1
    [[ $label == "$id" ]] || return 1
    d container rm -fv "$cid" >/dev/null || return 1
}
cleanup() {
    local rc=$? f label
    trap - EXIT
    for f in "$scratch/client.id" "$scratch/broker.id"; do
        if [[ -f $f ]]; then
            clean_container "$f" || rc=1
        fi
    done
    if [[ -n $net ]]; then
        if [[ $net =~ ^[0-9a-f]{64}$ ]] &&
            label=$(d network inspect --format '{{index .Labels "contract.fixture"}}' "$net") &&
            [[ $label == "$id" ]]; then
            d network rm "$net" >/dev/null || rc=1
        else
            rc=1
        fi
    fi
    # Private temp files and the image are removed with the hosted VM.
    if (( rc != 0 )); then
        printf 'FAIL: HOSTED_%s\n' "$stage" >&2
    fi
    exit "$rc"
}
trap cleanup EXIT

stage=VERIFIED_IMAGES
broker_image=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["kafka"]["pinned"])' \
    "$root/infra/cdc/images.lock.json")
[[ $broker_image =~ ^apache/kafka:[0-9]+\.[0-9]+\.[0-9]+@sha256:[0-9a-f]{64}$ ]]
d pull "$broker_image" >/dev/null 2>&1
build="$scratch/build"
mkdir "$build"
cp "$root/infra/lakehouse/requirements.lock" "$build/lakehouse.lock"
cp "$root/infra/cdc/requirements.lock" "$build/cdc.lock"
cp "$root/tests/integration/cdc_contract_iceberg/requirements.lock" "$build/sqlite.lock"
cat > "$build/Dockerfile" <<'DOCKERFILE'
FROM docker.io/library/python:3.14-slim@sha256:ac25e5afbfdb4a94ff99cc24b0345719210133144481480bcba6085efa36a341
COPY --from=ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 /uv /usr/local/bin/uv
WORKDIR /requirements
COPY *.lock .
RUN uv --no-cache pip sync --system --require-hashes --only-binary sqlalchemy \
    --default-index https://pypi.org/simple lakehouse.lock cdc.lock sqlite.lock
DOCKERFILE
d build --iidfile "$scratch/image.id" "$build" >/dev/null 2>&1
fixture_image=$(<"$scratch/image.id")
[[ $fixture_image =~ ^sha256:[0-9a-f]{64}$ ]]

stage=FIXTURE_CODE
worktree="$scratch/snapshot"
mkdir "$worktree"
git -C "$root" archive --format=tar HEAD infra/cdc scripts/cdc_contract.py     medallion/sources.json tests/integration/cdc_contract_iceberg |
    tar -xf - -C "$worktree"

stage=INTERNAL_NETWORK
net=$(d network create --internal --label "contract.fixture=$id" "$id")
[[ $net =~ ^[0-9a-f]{64}$ ]]
[[ $(d network inspect --format '{{.Internal}} {{.Driver}}' "$net") == 'true bridge' ]]
cluster=$(python3 -c 'import secrets; print(secrets.token_urlsafe(16))')
d create --cidfile "$scratch/broker.id" --name "$id-broker" \
    --label "contract.fixture=$id" --network "$net" --network-alias contract-broker \
    -e "CLUSTER_ID=$cluster" -e KAFKA_NODE_ID=1 \
    -e KAFKA_PROCESS_ROLES=broker,controller \
    -e 'KAFKA_LISTENERS=PLAINTEXT://:9092,CONTROLLER://:9093' \
    -e 'KAFKA_ADVERTISED_LISTENERS=PLAINTEXT://contract-broker:9092' \
    -e 'KAFKA_LISTENER_SECURITY_PROTOCOL_MAP=CONTROLLER:PLAINTEXT,PLAINTEXT:PLAINTEXT' \
    -e KAFKA_CONTROLLER_LISTENER_NAMES=CONTROLLER \
    -e 'KAFKA_CONTROLLER_QUORUM_VOTERS=1@contract-broker:9093' \
    -e KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR=1 \
    -e KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR=1 \
    -e KAFKA_TRANSACTION_STATE_LOG_MIN_ISR=1 \
    -e KAFKA_AUTO_CREATE_TOPICS_ENABLE=false \
    -e KAFKA_LOG_DIRS=/var/lib/kafka/data "$broker_image" >/dev/null
d create --cidfile "$scratch/client.id" --name "$id-client" \
    --label "contract.fixture=$id" --network "$net" \
    --user "$(id -u):$(id -g)" --cap-drop ALL \
    --security-opt no-new-privileges --read-only --tmpfs /tmp:rw,nosuid,nodev \
    --mount "type=bind,src=$worktree,dst=/checkout,readonly" \
    -e PYTHONDONTWRITEBYTECODE=1 -e "CONTRACT_FIXTURE_ID=$id" \
    -e CONTRACT_FIXTURE_HOSTED=true "$fixture_image" \
    python /checkout/tests/integration/cdc_contract_iceberg/proof.py \
    --hosted-kafka >/dev/null
broker=$(<"$scratch/broker.id")
client=$(<"$scratch/client.id")
stage=RESOURCE_OWNERSHIP
for cid in "$broker" "$client"; do
    [[ $cid =~ ^[0-9a-f]{64}$ ]]
    [[ $(d container inspect --format '{{index .Config.Labels "contract.fixture"}}' "$cid") == "$id" ]]
    mode=$(d container inspect --format '{{.HostConfig.NetworkMode}}' "$cid")
    [[ $mode == "$net" || $mode == "$id" ]]
    # shellcheck disable=SC2016 # Template variables belong to Docker.
    [[ $(d container inspect --format '{{range $key, $value := .NetworkSettings.Networks}}{{$key}}{{end}}' "$cid") == "$id" ]]
    [[ $(d container inspect --format '{{len .HostConfig.PortBindings}}' "$cid") == 0 ]]
    [[ $(d container inspect --format '{{.HostConfig.Privileged}}' "$cid") == false ]]
done

stage=BROKER_START
d start "$broker" >/dev/null
[[ $(d container inspect --format '{{range .NetworkSettings.Networks}}{{.NetworkID}}{{end}}' "$broker") == "$net" ]]
deadline=$(( SECONDS + 75 ))
until timeout 6s docker --host unix:///var/run/docker.sock exec "$broker" \
    /opt/kafka/bin/kafka-topics.sh --bootstrap-server localhost:9092 \
    --list >/dev/null 2>&1; do
    (( SECONDS < deadline ))
    sleep 1
done
stage=TOPIC_CREATION
timeout 15s docker --host unix:///var/run/docker.sock exec "$broker" \
    /opt/kafka/bin/kafka-topics.sh --bootstrap-server localhost:9092 \
    --create --topic contract_fixture.synthetic.example.orders \
    --partitions 1 --replication-factor 1 >/dev/null 2>&1
stage=DELIVERY_AND_STORAGE
timeout 160s docker --host unix:///var/run/docker.sock start --attach "$client"
[[ $(d container inspect --format '{{.State.ExitCode}}' "$client") == 0 ]]
stage=CLEANUP

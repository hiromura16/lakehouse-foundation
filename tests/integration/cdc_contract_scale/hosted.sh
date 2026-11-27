#!/usr/bin/env bash
# 5表の全資源を新規作成するGitHub-hosted使用専用。既存資源は使わない。
set -Eeuo pipefail
umask 077
authorize_launcher() {
    [[ $# == 1 && ( $1 == --source-scale || $1 == --chain-scale ) ]] || {
        echo 'FAIL: SCALE_EXPLICIT_MODE_REQUIRED' >&2
        return 1
    }
    [[ ${GITHUB_ACTIONS:-} == true && ${RUNNER_ENVIRONMENT:-} == github-hosted &&
       ${GITHUB_REPOSITORY:-} == hiromura16/lakehouse-foundation &&
       ${GITHUB_EVENT_NAME:-} == pull_request &&
       ${GITHUB_REF:-} =~ ^refs/pull/[1-9][0-9]*/merge$ &&
       ${RUNNER_OS:-} == Linux && ${RUNNER_ARCH:-} == X64 &&
       -d ${RUNNER_TEMP:-} && -S /var/run/docker.sock ]] || {
        echo 'FAIL: SCALE_HOSTED_RUNNER_REQUIRED' >&2
        return 1
    }
}
authorize_launcher "$@"
readonly fixture_mode=$1
scratch=$(mktemp -d "$RUNNER_TEMP/normal-cdc-scale.XXXXXXXX")
unset DOCKER_CONTEXT DOCKER_HOST BUILDX_BUILDER BUILDX_CONFIG DOCKER_TLS_VERIFY DOCKER_CERT_PATH
export DOCKER_CONFIG="$scratch/docker"
mkdir "$DOCKER_CONFIG"
id="normal-cdc-$(openssl rand -hex 12)"
root=$(git rev-parse --show-toplevel)
stage=IMAGES
net=''
tls=''
d() { docker --host unix:///var/run/docker.sock "$@"; }
dt() { local seconds=$1; shift; timeout "$seconds" docker --host unix:///var/run/docker.sock "$@"; }
label='{{index .Config.Labels "normal.cdc.scale.fixture"}}'
resource_label='{{index .Labels "normal.cdc.scale.fixture"}}'
cleanup() {
    local rc=$? f cid role
    trap - EXIT
    for role in source preparer connect sql broker certs; do
        f="$scratch/$role.id"
        [[ -f $f ]] || continue
        cid=$(<"$f")
        if [[ $cid =~ ^[0-9a-f]{64}$ &&
              $(d container inspect --format "$label" "$cid") == "$id" ]]; then
            d container rm -fv "$cid" >/dev/null || rc=1
        else
            rc=1
        fi
    done
    if [[ -n $net ]]; then
        if [[ $net =~ ^[0-9a-f]{64}$ &&
              $(d network inspect --format "$resource_label" "$net") == "$id" ]]; then
            d network rm "$net" >/dev/null || rc=1
        else
            rc=1
        fi
    fi
    if [[ -n $tls ]]; then
        if [[ $tls == "$id-tls" &&
              $(d volume inspect --format "$resource_label" "$tls") == "$id" ]]; then
            d volume rm "$tls" >/dev/null || rc=1
        else
            rc=1
        fi
    fi
    # 0700 の scratch 内に限るファイルは hosted VM と共に破棄する。
    if (( rc != 0 )); then printf 'FAIL: SCALE_HOSTED_%s\n' "$stage" >&2; fi
    exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
lock="$root/infra/cdc/images.lock.json"
readarray -t images < <(python3 - "$lock" <<'PY'
import json
import sys
with open(sys.argv[1], encoding="utf-8") as document:
    lock = json.load(document)
print(lock["kafka"]["pinned"])
assert lock["debezium"]["policy"] == "release-tag"
print(lock["debezium"]["tag"])
PY
)
[[ ${#images[@]} == 2 ]]
broker_image=${images[0]}
connect_tag=${images[1]}
[[ $broker_image =~ ^apache/kafka:[0-9]+\.[0-9]+\.[0-9]+@sha256:[0-9a-f]{64}$ ]]
[[ $connect_tag == quay.io/debezium/connect:3.7.1.Final ]]
sql_image=mcr.microsoft.com/mssql/server@sha256:4525e3efac9f3e241cb003bb2ff2a82818b502f37e12049d788b1a9fa7e13b7f
for image in "$broker_image" "$connect_tag" "$sql_image"; do
    dt 120s pull --platform linux/amd64 "$image" >/dev/null 2>&1
done
mapfile -t receipts < <(d image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$connect_tag" |
    grep -Ex 'quay.io/debezium/connect@sha256:[0-9a-f]{64}')
[[ ${#receipts[@]} == 1 ]]
connect_image=${receipts[0]}
tag_id=$(d image inspect --format '{{.Id}}' "$connect_tag")
selected_id=$(d image inspect --format '{{.Id}}' "$connect_image")
[[ $tag_id =~ ^sha256:[0-9a-f]{64}$ && $tag_id == "$selected_id" ]]

stage=BUILD_CLIENT
build="$scratch/build"
mkdir "$build"
cp "$root/infra/cdc/requirements.lock" "$build/cdc.lock"
cat > "$build/Dockerfile" <<'DOCKERFILE'
FROM docker.io/library/python:3.14-slim@sha256:ac25e5afbfdb4a94ff99cc24b0345719210133144481480bcba6085efa36a341
COPY --from=ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 /uv /usr/local/bin/uv
COPY cdc.lock /tmp/cdc.lock
RUN uv --no-cache pip sync --system --require-hashes --only-binary :all: /tmp/cdc.lock
DOCKERFILE

build_timeout=180s
if [[ $fixture_mode == --chain-scale ]]; then
    cp "$root/infra/lakehouse/requirements.lock" "$build/lakehouse.lock"
    cp "$root/tests/integration/cdc_contract_iceberg/requirements.lock" "$build/sqlite.lock"
    sed -i '$d' "$build/Dockerfile"
    cat >> "$build/Dockerfile" <<'CHAIN_BUILD'
COPY lakehouse.lock sqlite.lock /tmp/
RUN uv --no-cache pip sync --system --require-hashes --only-binary sqlalchemy \
    --default-index https://pypi.org/simple /tmp/lakehouse.lock /tmp/cdc.lock /tmp/sqlite.lock
CHAIN_BUILD
    build_timeout=320s
fi
dt "$build_timeout" build --iidfile "$scratch/image.id" "$build" >/dev/null 2>&1
client_image=$(<"$scratch/image.id")
[[ $client_image =~ ^sha256:[0-9a-f]{64}$ ]]
snapshot="$scratch/checkout"
mkdir -m 0755 "$snapshot"
sealed=(
    infra/cdc scripts/cdc_contract.py medallion/sources.json
    tests/integration/cdc_contract_sql_source/proof.py
    tests/integration/cdc_contract_scale/fixture.py
    tests/integration/cdc_contract_scale/source.py
)
last_argument=run
chain_env=()
if [[ $fixture_mode == --chain-scale ]]; then
    sealed+=(
        tests/integration/cdc_contract_scale/chain.py
        tests/integration/cdc_contract_scale/storage.py
    )
    last_argument=run-chain
    chain_env=(-e CDC_SQL_SCALE_CHAIN=true)
fi
git -C "$root" archive --format=tar HEAD "${sealed[@]}" | tar -xf - -C "$snapshot"
stage=CERTIFICATE
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj '/CN=Isolated CDC fixture CA' \
    -addext basicConstraints=critical,CA:TRUE -addext keyUsage=critical,keyCertSign,cRLSign \
    -keyout "$scratch/ca.key" -out "$scratch/ca.pem" >/dev/null 2>&1
openssl req -newkey rsa:2048 -nodes -subj '/CN=localhost' \
    -keyout "$scratch/server.key" -out "$scratch/server.csr" >/dev/null 2>&1
printf '%s\n' 'subjectAltName=DNS:localhost,DNS:fixture-sql' \
    'extendedKeyUsage=serverAuth' > "$scratch/extensions"
openssl x509 -req -in "$scratch/server.csr" -CA "$scratch/ca.pem" \
    -CAkey "$scratch/ca.key" -CAcreateserial -days 1 -extfile "$scratch/extensions" \
    -out "$scratch/server.pem" >/dev/null 2>&1
keytool -importcert -noprompt -alias normal-fixture-ca -file "$scratch/ca.pem" \
    -keystore "$scratch/trust.p12" -storetype PKCS12 -storepass fixture-ca-only >/dev/null 2>&1
cat > "$scratch/mssql.conf" <<'CONF'
[network]
forceencryption = 1
tlscert = /fixture-tls/server.pem
tlskey = /fixture-tls/server.key
[sqlagent]
enabled = true
CONF
cat > "$scratch/freetds.conf" <<'CONF'
[global]
    encryption = require
    tds version = 7.4
    ca file = /fixture/ca.pem
    check certificate hostname = yes
[fixture-sql]
    host = fixture-sql
    port = 1433
CONF
chmod 0444 "$scratch"/{mssql.conf,freetds.conf,ca.pem,trust.p12}
admin_password="Aa9!$(openssl rand -hex 24)"
reader_password="Aa9!$(openssl rand -hex 24)"
[[ $admin_password != "$reader_password" ]]
printf '::add-mask::%s\n' "$admin_password" "$reader_password"
printf 'MSSQL_SA_PASSWORD=%s\n' "$admin_password" > "$scratch/sql.env"
printf 'FIXTURE_SQL_READER_PASSWORD=%s\n' "$reader_password" > "$scratch/connect.env"
printf 'FIXTURE_SQL_ADMIN_PASSWORD=%s\nFIXTURE_SQL_READER_PASSWORD=%s\n' \
    "$admin_password" "$reader_password" > "$scratch/client.env"
unset admin_password reader_password

stage=OWNED_RESOURCES
tls=$(d volume create --label "normal.cdc.scale.fixture=$id" "$id-tls")
[[ $tls == "$id-tls" && $(d volume inspect --format "$resource_label" "$tls") == "$id" ]]
d create --pull never --cidfile "$scratch/certs.id" --name "$id-certs" \
    --network none --label "normal.cdc.scale.fixture=$id" --user 0 \
    --security-opt no-new-privileges --entrypoint /bin/sh \
    --mount "type=bind,src=$scratch/server.key,dst=/incoming.key,readonly" \
    --mount "type=bind,src=$scratch/server.pem,dst=/incoming.pem,readonly" \
    --mount "type=volume,src=$tls,dst=/fixture-tls" "$sql_image" -ec \
    'install -m 0400 -o mssql /incoming.key /fixture-tls/server.key
     install -m 0400 -o mssql /incoming.pem /fixture-tls/server.pem' >/dev/null
certs=$(<"$scratch/certs.id")
[[ $certs =~ ^[0-9a-f]{64}$ &&
   $(d container inspect --format "$label" "$certs") == "$id" &&
   $(d container inspect --format '{{.HostConfig.NetworkMode}}' "$certs") == none ]]
dt 15s start --attach "$certs" >/dev/null 2>&1
[[ $(d container inspect --format '{{.State.ExitCode}}' "$certs") == 0 ]]
net=$(d network create --internal --label "normal.cdc.scale.fixture=$id" "$id")
[[ $net =~ ^[0-9a-f]{64}$ && $(d network inspect --format '{{.Internal}} {{.Driver}}' "$net") == 'true bridge' ]]
cluster=$(openssl rand -base64 16 | tr '/+' '_-' | tr -d '=\n')
create() {
    local role=$1; shift
    d create --pull never --cidfile "$scratch/$role.id" --name "$id-$role" \
        --label "normal.cdc.scale.fixture=$id" --network "$net" "$@" >/dev/null
}
create broker --label "normal.cdc.scale.cluster=$cluster" --network-alias fixture-broker --memory 1g --cpus 2 \
    -e "CLUSTER_ID=$cluster" -e KAFKA_NODE_ID=1 -e KAFKA_PROCESS_ROLES=broker,controller \
    -e 'KAFKA_LISTENERS=PLAINTEXT://:9092,CONTROLLER://:9093' \
    -e 'KAFKA_ADVERTISED_LISTENERS=PLAINTEXT://fixture-broker:9092' \
    -e 'KAFKA_LISTENER_SECURITY_PROTOCOL_MAP=CONTROLLER:PLAINTEXT,PLAINTEXT:PLAINTEXT' \
    -e KAFKA_CONTROLLER_LISTENER_NAMES=CONTROLLER -e 'KAFKA_CONTROLLER_QUORUM_VOTERS=1@fixture-broker:9093' \
    -e KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR=1 -e KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR=1 \
    -e KAFKA_TRANSACTION_STATE_LOG_MIN_ISR=1 -e KAFKA_GROUP_INITIAL_REBALANCE_DELAY_MS=0 \
    -e KAFKA_AUTO_CREATE_TOPICS_ENABLE=false -e 'KAFKA_HEAP_OPTS=-Xms256m -Xmx512m' \
    -e KAFKA_LOG_DIRS=/var/lib/kafka/data "$broker_image"
create sql --network-alias fixture-sql --memory 3g --cpus 2 --init \
    --env-file "$scratch/sql.env" -e ACCEPT_EULA=Y -e MSSQL_PID=Developer \
    -e MSSQL_AGENT_ENABLED=true -e MSSQL_MEMORY_LIMIT_MB=2048 \
    --mount "type=bind,src=$scratch/mssql.conf,dst=/var/opt/mssql/mssql.conf,readonly" \
    --mount "type=volume,src=$tls,dst=/fixture-tls,readonly" "$sql_image"
create connect --network-alias fixture-connect --memory 1440m --cpus 2 --init \
    --env-file "$scratch/connect.env" -e BOOTSTRAP_SERVERS=fixture-broker:9092 \
    -e GROUP_ID=fixture-scale-connect-group -e CONFIG_STORAGE_TOPIC=_fixture_scale_connect_configs \
    -e OFFSET_STORAGE_TOPIC=_fixture_scale_connect_offsets -e STATUS_STORAGE_TOPIC=_fixture_scale_connect_status \
    -e ADVERTISED_HOST_NAME=fixture-connect -e CONNECT_CONFIG_STORAGE_REPLICATION_FACTOR=1 \
    -e CONNECT_OFFSET_STORAGE_REPLICATION_FACTOR=1 -e CONNECT_STATUS_STORAGE_REPLICATION_FACTOR=1 \
    -e CONNECT_OFFSET_STORAGE_PARTITIONS=1 -e CONNECT_STATUS_STORAGE_PARTITIONS=1 \
    -e CONNECT_CONFIG_PROVIDERS=env \
    -e CONNECT_CONFIG_PROVIDERS_ENV_CLASS=org.apache.kafka.common.config.provider.EnvVarConfigProvider \
    -e 'HEAP_OPTS=-Xms256m -Xmx512m' \
    --mount "type=bind,src=$scratch/trust.p12,dst=/fixture/trust.p12,readonly" "$connect_image"

expected_image=$(d image inspect --format '{{.Id}}' "$broker_image")
[[ $expected_image =~ ^sha256:[0-9a-f]{64}$ ]]
entry=/checkout/tests/integration/cdc_contract_scale/source.py
shared_client_flags=(
    --memory 768m --cpus 1 --user "$(id -u):$(id -g)" --cap-drop ALL
    --security-opt no-new-privileges --read-only --tmpfs "/tmp:rw,nosuid,nodev"
    -e CDC_SQL_SOURCE_HOSTED=true -e "CDC_SQL_SOURCE_ID=$id"
    -e CDC_SQL_SCALE_HOSTED=true -e "CDC_SQL_SCALE_KAFKA_ID=$cluster"
    -e FREETDSCONF=/fixture/freetds.conf -e PYTHONDONTWRITEBYTECODE=1
    --mount "type=bind,src=$snapshot,dst=/checkout,readonly"
)
create preparer "${shared_client_flags[@]}" "$client_image" python -B "$entry" prepare-topics
create source "${shared_client_flags[@]}" "${chain_env[@]}" --env-file "$scratch/client.env" \
    --mount "type=bind,src=$scratch/freetds.conf,dst=/fixture/freetds.conf,readonly" \
    --mount "type=bind,src=$scratch/ca.pem,dst=/fixture/ca.pem,readonly" \
    "$client_image" python -B "$entry" "$last_argument"
broker=$(<"$scratch/broker.id")
sql=$(<"$scratch/sql.id")
connect=$(<"$scratch/connect.id")
preparer=$(<"$scratch/preparer.id")
source=$(<"$scratch/source.id")

require_owned() {
    local cid=$1 network_mode
    [[ $cid =~ ^[0-9a-f]{64}$ &&
       $(d container inspect --format "$label" "$cid") == "$id" &&
       $(d container inspect --format '{{len .HostConfig.PortBindings}} {{.HostConfig.Privileged}}' "$cid") == '0 false' ]] || return 1
    network_mode=$(d container inspect --format '{{.HostConfig.NetworkMode}}' "$cid")
    [[ $network_mode == "$net" || $network_mode == "$id" ]] || return 1
    # shellcheck disable=SC2016 # Template variables belong to Docker.
    [[ $(d container inspect --format '{{range $key, $value := .NetworkSettings.Networks}}{{$key}}{{end}}' "$cid") == "$id" ]]
}
require_started_network() {
    [[ $(d container inspect --format '{{range .NetworkSettings.Networks}}{{.NetworkID}}{{end}}' "$1") == "$net" ]]
}
require_broker() {
    # envだけでは証拠にしない。runner作成時のDocker ID/所有ラベル/
    # 固定イメージ/ネットワークと別に、後でPythonがKafka cluster IDを照合する。
    require_owned "$broker" && require_started_network "$broker" &&
        [[ $(d network inspect --format "$resource_label" "$net") == "$id" &&
           $(d network inspect --format '{{.Internal}} {{.Driver}}' "$net") == 'true bridge' &&
           $(d container inspect --format '{{.State.Running}}' "$broker") == true &&
           $(d container inspect --format '{{index .Config.Labels "normal.cdc.scale.cluster"}}' "$broker") == "$cluster" &&
           $(d container inspect --format '{{.Image}}' "$broker") == "$expected_image" ]]
}
run_marker() {
    local cid=$1 limit=$2 marker=$3
    require_owned "$cid"
    [[ $(d container inspect --format '{{.State.Status}}' "$cid") == created ]]
    if dt "$limit" start --attach "$cid" > "$scratch/driver.out" 2>&1; then
        [[ $(d container inspect --format '{{.State.ExitCode}}' "$cid") == 0 ]]
        grep -Fx "$marker" "$scratch/driver.out"
    else
        grep -Em1 '^FAIL: SCALE_[A-Z0-9_]+/[A-Z0-9_]+$' "$scratch/driver.out" >&2 || true
        return 1
    fi
}
run_fixture() {
    local cid deadline
    stage=OWNED
    for cid in "$broker" "$sql" "$connect" "$preparer" "$source"; do
        require_owned "$cid"
    done
    stage=FRESH_BROKER
    [[ $(d container inspect --format '{{.State.Status}}' "$broker") == created ]]
    d start "$broker" >/dev/null
    require_broker
    deadline=$(( SECONDS + 70 ))
    until dt 7s exec "$broker" /opt/kafka/bin/kafka-topics.sh \
        --bootstrap-server localhost:9092 --list >/dev/null 2>&1; do
        (( SECONDS < deadline ))
        sleep 1
    done
    stage=KAFKA_BOOTSTRAP
    require_broker
    run_marker "$preparer" 100s 'SQL_SCALE_TOPICS=pass'
    stage=SQL_AND_CONNECT
    require_broker
    d start "$sql" "$connect" >/dev/null
    require_started_network "$sql"
    require_started_network "$connect"
    stage=ACTUAL_SQL_AND_KAFKA
    if [[ $fixture_mode == --chain-scale ]]; then
        stage=ACTUAL_SQL_KAFKA_AND_ICEBERG
        run_marker "$source" 900s 'SQL_SCALE_CHAIN=pass source_records=1392 local_partitions=5 source_partitions=5 outstanding=0 raw_rows=1392 current_rows=112 silver_rows=112'
    else
    run_marker "$source" 650s 'SQL_SCALE_NATIVE=pass tables=5 snapshot=112 insert=256 update=512 delete=256 tombstone=256'
    fi
}
run_fixture
stage=CLEANUP

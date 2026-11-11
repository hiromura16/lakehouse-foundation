# バージョンと連携互換性

<!-- doc-purpose:start -->
> **役割:** 固定image・Python環境・Spark/Iceberg等の組合せと互換性の確認方法を調べる。
>
> **対象読者:** 基盤構築担当 / 開発・設計担当
>
> **読むタイミング:** 依存更新を計画するときや、build・起動・連携のどこで失敗したかを分けるとき。
>
> **前提となる資料:** [Lakehouse Foundation アーキテクチャ](<ARCHITECTURE.md>)
>
> **次に読む資料:** [採用版の公式リファレンスと文書監査](<REFERENCES.md>) / [ソース・指標・API・計算基盤を拡張する](<CUSTOMIZATION.md>) / [検証記録](<VALIDATION.md>)
>
> **文書一覧:** [目的・読者から探す文書マニフェスト](<DOCUMENTATION-MANIFEST.md>)
<!-- doc-purpose:end -->

<!-- shell-usage:start -->
**コマンドのシェル:** 「Bash / Zsh共通」は同じ内容を使い、専用例は自分のシェルを選びます。[詳しい使い分け](SHELLS.md)。

Zshの対話端末では、コメント付きの例を貼る前に一度だけ次を実行します。Bashでは不要です。

**Zsh**

```zsh
setopt INTERACTIVE_COMMENTS
```
<!-- shell-usage:end -->

照合日: 2026-11-10。対象は、このcheckoutのDockerfile・Compose・hash lockで固定した版です。公式の版付き文書・release tagと実装を照合しました。配布物の実在確認、ビルド成功、コンテナ起動、システム間の読み書きは別の検証として扱い、実機の成功結果は [検証記録](VALIDATION.md) を正とします。

## 採用版を確認する

表の版は自動追従する `latest` ではありません。上流に新しい版が出ても、lockの変更、依存解決、実ビルド、読み書き・保存の検証を経てから更新します。

| 部品 | 比較・確認した版 | 採用・判断 |
| --- | --- | --- |
| Apache Spark | 4.3.0（2026-10-30） | Icebergの公開runtimeがある4.1系の保守版 **4.1.4** を採用 |
| Apache Iceberg Java | 1.12.0（2026-09-29） | Spark runtimeとAWS bundleを **1.12.0** に統一 |
| Apache Airflow | 3.4.0（2026-11-06） | **3.4.0 / Python 3.12** の公式イメージと同じconstraintsを採用 |
| Airflow standard provider | 1.21.0 | 3.4.0のPython 3.12 constraintsと一致。2.0.0rc1は除外 |
| Airflow HTTP provider | 6.3.0（3.4.0 constraintsの指定） | 同版へ固定。HTTP ConnectionのGUIに利用 |
| Airflow metadata PostgreSQL | 18.6を採用 | Airflow 3.4.0の公式試験対象にPostgreSQL 18が含まれる |
| core worker | Python 3.14 / PyIceberg 0.12 / PyArrow 26.0.1 | 実機確認した依存をlockへ固定 |
| 自動化・監視 | n8n 2.46.7 / Grafana 13.3.0 / Prometheus 3.16.0 | 専用DB・永続volume、dashboardのGUI保存を実機確認 |
| core | Trino 484 / Lakekeeper 0.14.3 / SeaweedFS 4.53 / PostgreSQL 18.6 | 採用digestは `infra/lakehouse/images.lock.json` を参照 |
| BI | Superset 6.1.0 / SQLAlchemy 1.4.54 / Trino Python 0.340.0 | `bi`。接続→SQL Lab→Dataset→Chart→Dashboardは [POC-TOOLS.md](POC-TOOLS.md) |
| Notebook | JupyterLab 4.6.6 / ipykernel 7.3.0 | `notebooks`。Sparkと同じPython 3.10、Pandas 2.3.3 / Arrow 25.0.0 / NumPy 2.2.6 |
| ベクトル・系統 | Qdrant 1.20.0 / Marquez 0.50.0 | `vector` / `lineage`。MarquezはLinux x86-64で検証 |
| MCP | Python SDK 2.3.0 / httpx2 2.16.0 | `mcp`。v2の `Client` とStreamable HTTPを使用。v1のクライアント例は流用しない |
| AI例 | langchain-core 1.7.6 / langchain-ollama 1.1.0 / LangSmith 0.14.17 | `examples/ai/requirements.lock`。独立venvでDocument化、任意のローカル推論 |

当初比較したSpark 4.3.0の公開と、IcebergのSpark 4.3向けruntimeの公開は別です。4.1用JARを別のSpark minorへ流用せず、今回の連携プロファイルは4.1.4へ固定します。[Spark 4.1.4文書](https://spark.apache.org/docs/4.1.4/) / [Iceberg 1.12.0のSpark設定](https://github.com/apache/iceberg/blob/apache-iceberg-1.12.0/docs/docs/spark-configuration.md)

AirflowはGitHubの正式リリースとPyPIの安定版を照合しました。開発中の `/stable` 文書やrc版だけを見て採用版を決めません。[3.4.0正式リリース](https://github.com/apache/airflow/releases/tag/3.4.0) / [PyPI配布](https://pypi.org/project/apache-airflow/3.4.0/) / [standard provider](https://pypi.org/project/apache-airflow-providers-standard/1.21.0/)

core workerはPython 3.14を採用します。Python 3.15は依存互換性を確認していないため採用しません。CIのホストPython 3.12、AirflowのPython 3.12、Sparkの同梱Pythonは独立した環境で、workerのPythonへ合わせて一括更新するものではありません。

## Python環境を一つへ揃えない理由

初心者が追加packageを試すときも、先に「どのプロセスのPythonか」を確かめます。hostのvenvへ入れたpackageがSpark executorへ届くことはありません。

| 実行する処理 | 環境の正本 | 揃える相手 |
| --- | --- | --- |
| ファイル取込・メダリオン制御 | worker Python3.14とcore lock | PyIceberg/FileIO/Arrow、REST/S3接続 |
| Jupyterからremote Python task | Notebookのdriver venv | Spark WorkerのPython minor、共有Pandas/Arrow/NumPy等 |
| Airflowのtask | 公式Python3.12 venvとconstraints | Airflow本体・標準/HTTP Provider・DB driver |
| hostのSQL抽出 | uvで作った専用reader venv | pymssql/FreeTDS、会社DBのTLSと入力契約 |
| AI Document化・任意推論 | examples/aiの独立lock | LangChain連携とexport JSONの契約 |
| 図のSVG生成 / binary出力 | 標準Python / 専用export venv | 日本語font、CairoSVG/PDF/PPTX依存。アプリへ追加しない |

Notebookにだけ新しいArrowを入れてremote UDFが失敗した場合、Trinoの版を変える前にdriverとexecutorの実版を比較します。逆に、カタログへ接続できてもS3への書込みが失敗する場合は、Python minorよりFileIO・endpoint・資格情報を調べます。

## Spark・Iceberg・Java・S3の組み合わせ

| 項目 | 固定値 |
| --- | --- |
| Spark | 4.1.4 |
| Scala | 2.13、Sparkのソース指定は2.13.17 |
| Java | 21。Spark 4.1.4の対応範囲はJava 17/21 |
| Python | イメージに含まれるPython 3。Spark 4.1.4の対応範囲は3.10以上 |
| SparkのHadoop依存 | 4.1.4のソース指定は3.4.2 |
| Iceberg | 1.12.0 |
| Spark runtime | `org.apache.iceberg:iceberg-spark-runtime-4.1_2.13:1.12.0` |
| S3 bundle | `org.apache.iceberg:iceberg-aws-bundle:1.12.0` |
| S3 I/O | `org.apache.iceberg.aws.s3.S3FileIO` |
| AWS SDK | Iceberg 1.12.0のBOM指定は2.54.17。bundleを使い、別版を追加しない |
| テーブル形式 | Iceberg v2 / Parquet |

Iceberg 1.12.0のSpark 4.1用ビルドは4.1.3を基準にしています。4.1.4は同じSpark 4.1系の保守版ですが、組み合わせの成功は後述のスモークで確認します。Scala 2.12のruntime、別Sparkマイナー版のruntime、別版のIceberg AWS bundleを混在させません。[Spark 4.1.4の対応要件](https://spark.apache.org/docs/4.1.4/) / [Sparkの固定依存](https://github.com/apache/spark/blob/v4.1.4/pom.xml) / [Iceberg 1.12.0の依存定義](https://github.com/apache/iceberg/blob/apache-iceberg-1.12.0/gradle/libs.versions.toml)

S3FileIOへendpoint、region、path-styleとprivate設定のアクセス鍵を渡します。S3A経由のHadoop FileIOを使わないため、`hadoop-aws` やAWS SDK v1のbundleを追加しません。余分なSDK追加によるclass競合を避けます。

### 配布物の正確な参照

```text
apache/spark:4.1.4-scala2.13-java21-python3-ubuntu
@sha256:847f91f8bb2f28d632064dfe632d423020f8fd60b27c7704c0e5bf5b0c6432c7

org.apache.iceberg:iceberg-spark-runtime-4.1_2.13:1.12.0
org.apache.iceberg:iceberg-aws-bundle:1.12.0
```

イメージの参照は上のタグとdigestを連結した一つの値です。公式manifestにamd64とarm64があることを確認していますが、両アーキテクチャでの実行成功を意味しません。参照の正本は `infra/compute/Dockerfile` と `images.lock.json` です。[公式Dockerfile](https://github.com/apache/spark-docker/blob/master/4.1.4/scala2.13-java21-python3-ubuntu/Dockerfile)

JARは次のMaven Central URLからビルド時に取得します。公式のSHA-512をロックへ保存し、取得後に照合します。実行時に `--packages` で追加ダウンロードしないため、ビルド済みイメージは閉じたネットワークでも同じJARを使えます。

- [Spark runtime JAR](https://repo.maven.apache.org/maven2/org/apache/iceberg/iceberg-spark-runtime-4.1_2.13/1.12.0/iceberg-spark-runtime-4.1_2.13-1.12.0.jar) / [SHA-512](https://repo.maven.apache.org/maven2/org/apache/iceberg/iceberg-spark-runtime-4.1_2.13/1.12.0/iceberg-spark-runtime-4.1_2.13-1.12.0.jar.sha512)
- [AWS bundle JAR](https://repo.maven.apache.org/maven2/org/apache/iceberg/iceberg-aws-bundle/1.12.0/iceberg-aws-bundle-1.12.0.jar) / [SHA-512](https://repo.maven.apache.org/maven2/org/apache/iceberg/iceberg-aws-bundle/1.12.0/iceberg-aws-bundle-1.12.0.jar.sha512)

### 常駐SparkとJupyterからの利用

`compute` profileはMaster・Worker・History Serverの3サービスを常駐させます。
既存のSpark/Iceberg imageを共用し、workerの枠は2 core / 2 GiB、containerは
4 GiBに制限します。RPCの7077/7078は内部networkだけで使用します。

| 画面 | private設定と既定host port |
| --- | --- |
| Master | `SPARK_MASTER_UI_PORT=8082` |
| Worker | `SPARK_WORKER_UI_PORT=8085` |
| History | `SPARK_HISTORY_UI_PORT=18080` |

各画面の公開先はloopbackです。Masterのreverse proxyを有効にし、Workerと実行中
アプリのUIをMaster経由で参照します。リンクの基点はprivateのMaster UI portを
使う `http://localhost:<port>` です。遠隔操作では同じportのSSH転送を用意します。
HistoryへのリンクにもprivateのHistory portを使います。RPCを外部へ公開する必要は
ありません。[Spark standalone](https://spark.apache.org/docs/4.1.4/spark-standalone.html)

Jupyterはclient driverとして `spark://spark-master:7077` へ投入します。driverの
hostは `jupyter`、RPCは39000、block managerは39001、実行中UIは4040を内部で使います。
Python driverは `/opt/notebook-venv/bin/python`、executorは
`/opt/spark-venv/bin/python` です。同じ固定baseのPython 3.10を使い、numpy 2.2.6・
pandas 2.3.3・PyArrow 25.0.0を揃えます。executor環境もuv 0.13.7とhash lockで
構築し、実行時には依存を取得しません。Notebook側の `compute` 無効時は明示した
local実行を使い、cluster接続失敗を黙ってlocalへ切り替えません。

`spark-events` volumeをJupyterとHistoryが `/opt/spark/events` で共有します。
Jupyterはevent logを有効化し、終了後もHistoryから参照できます。`spark-work` は
workerの一時作業・ログとHistory cacheです。両volume名は `DATA_VOLUME_PREFIX`
の規約に従い、通常の停止で保持します。専用initは二つのmount rootだけを
`WORKER_UID/GID` とmode 2770へ揃え、保存済みファイルを再帰変更・削除しません。
そのため導入後にUID/GIDを変える場合は、別途データの所有権移行を計画します。
Historyの自動削除は無効です。event logの容量・保存期間を運用で管理します。
[Historyとevent log](https://spark.apache.org/docs/4.1.4/monitoring.html)

この構成は同一チームの信頼されたCompose networkを前提とする単一ホスト構成です。
Spark RPCの利用者認証・TLS、UIのSSO、HA、第三者Notebookの隔離は同梱していません。
Sparkを使える利用者はexecutor上でコードを動かせます。会社での利用者・networkの
境界は別途設定してください。新しい常駐clusterの実機確認は検証記録を正とします。

### Sparkの連携スモーク

coreが正常に起動し、バケットとWarehouseが初期化済みの状態で実行します。

**Bash / Zsh共通**

```bash
export LAKEHOUSE_ENV_FILE="$HOME/.config/lakehouse-foundation/site.env"
./scripts/lakehouse.sh --env-file "$LAKEHOUSE_ENV_FILE" spark-check
```

この操作は既存project/core/volumeとMaster/Workerのreadinessを確認した後、`compute` の一時 `spark-check` でlocal[2]の相互参照を確認します。分散executorでこのjobが実行された証拠ではありません。通常の `up` ではこの一時serviceだけをreplicas 0にし、上記Master・Worker・Historyを常駐させます。Docker Composeを直接使う場合は同じ追加Composeを全て指定し、`--profile compute build spark-volumes-init` を実行した後、`--profile compute run --rm --no-deps spark-check` を実行します。

`infra/compute/smoke.sql` は専用の `compatibility.spark_roundtrip` 表を作成・置換します。固定の3行に日本語・decimalを含め、Sparkで書いた表をSparkで読み、その後Trinoの `iceberg.compatibility.spark_roundtrip` から同じ行を読みます。再実行でも追記による二重化をせず、業務表は対象にしません。成功/失敗は版・12段階・時刻・件数・由来・cleanupを持つ安全なJSONへ出力し、鍵や接続文字列を表示しません。

この一時スモークは小さなlocal実行の読み書き確認です。常駐clusterの受入では、別途Jupyterから分散task・Python UDF・Arrow処理を実行し、Masterの完了アプリ、Workerのexecutor、Historyのevent logを照合します。大量データ、複数writerの競合、複数ホスト、認証付きOIDC、Iceberg v3の機能は別の確認です。

### Composeを直接操作する場合の省略しない例

以下はリポジトリのrootでBashまたはZshを使う場合です。以後の `dc` は7本を結合する関数で、コンテナを起動するものではありません。private envのprofileを使い、公開ソースへ秘密値を展開しません。共通loaderで検査・正規化して子プロセスへ渡すため、shellに残った別projectや古いproxyで対象を取り違えません。

**Bash / Zsh共通**

```bash
export LAKEHOUSE_ENV_FILE="$HOME/.config/lakehouse-foundation/site.env"
dc() {
  python3 -c '
import subprocess
import sys
from pathlib import Path

from scripts.settings import environment, load

root = Path.cwd()
values = load(sys.argv[1])
command = ["docker", "compose", "--env-file", sys.argv[1]]
for name in (
    "lakehouse", "compute", "orchestration", "demo", "cdc", "poc", "mcp"
):
    command.extend(["-f", str(root / "infra" / name / "compose.yaml")])
subprocess.run(command + sys.argv[2:], env=environment(values), check=True)
' "$LAKEHOUSE_ENV_FILE" "$@"
}
dc config --quiet
dc --profile compute build spark-volumes-init
dc --profile compute run --rm --no-deps spark-check
```

関数内のPythonコードは `-c` で渡し、標準入力を消費しません。`dc exec -T ...` へファイルやpipeを渡す保守でも同じ入力が子プロセスへ届きます。対話操作が必要な場合は、その製品のTerminalを使ってください。

`spark-check` はlocalでの相互読み書き試験です。常駐Workerを使う試験はJupyterの `02-spark-cluster.ipynb` で行います。Dokploy配下のサイトでは、同じprojectを手元のCLIで別管理せず、[Dokploy操作手順](DOKPLOY.md) の対象Container Terminalと [Notebook手順](POC-TOOLS.md) を使用します。

## Airflow・Python・Providerの組み合わせ

| 項目 | 固定値・実装 |
| --- | --- |
| Airflow | 3.4.0 |
| Python | 3.12 |
| standard provider | 1.21.0 |
| HTTP provider | 6.3.0 |
| common-compat provider | constraintsで1.23.0へ固定 |
| metadata DB | 専用PostgreSQL 18.6、psycopg 3接続 |
| Executor | LocalExecutor、同時タスクを小さく制限 |
| 起動方式 | `airflow standalone`、検証用途 |
| DAG | `lakehouse_medallion`、手動起動、定期scheduleなし |

Airflow 3.4.0の公式試験対象はPython 3.11～3.14、PostgreSQL 14～18です。公式3.4.0 Docker Composeと同じ `postgresql+psycopg` の接続方式を使用します。[対応要件の固定ソース](https://github.com/apache/airflow/blob/3.4.0/airflow-core/docs/installation/prerequisites.rst) / [公式3.4.0 Compose](https://github.com/apache/airflow/blob/3.4.0/airflow-core/docs/howto/docker-compose/docker-compose.yaml)

公式イメージは既に `/home/airflow/.local` をvenvとして使います。このPython・PATH・entrypointを維持し、固定したuv 0.13.7の `pip install --python /home/airflow/.local/bin/python` で同じ本体・Provider・constraintsを適用します。新しいvenvへ本体を移さず、追加後は `uv pip check` で依存を検査します。uvの配布digestは [共有lock](../infra/uv/images.lock.json) に記録しています。[公式3.4.0のvenv設定](https://github.com/apache/airflow/blob/3.4.0/Dockerfile#L2545)

```text
apache/airflow:3.4.0-python3.12
@sha256:818f94affc5bd7ce0be0cc04f2ace09aa8382e65190a73a08881229ff32700cb

postgres:18.6-alpine
@sha256:123b17998ccaf47e53b502e8c6a1e79afd8f579c6f3eec83c563da026f957395
```

Providerを追加する際は `apache-airflow==3.4.0` も同時に指定し、AirflowとPythonの組が一致する [公式constraints](https://raw.githubusercontent.com/apache/airflow/constraints-3.4.0/constraints-3.12.txt) を使用します。constraintsの内容はSHA-256で固定し、ビルド時の変更を検出します。`pip check` で依存整合を確認します。別のPython版のconstraintsや、古いAirflow 2系のOperator importを混在させません。

DAGは `airflow.providers.standard.operators.python.PythonOperator` と `airflow.sdk.BaseHook` を使用します。HTTPリクエストはPython標準の `urllib.request` です。Provider HTTPのOperatorに依存して処理を実行する構成ではなく、HTTP providerはConnectionのGUI種別も提供するために同じconstraintsで固定します。

### Airflowの起動と接続設定

private envの `COMPOSE_PROFILES` に既存の選択を残して `orchestration` を追加します。CLI管理の場合だけ通常の `up` で起動し、Dokploy管理では同じサービスEnvironmentを更新してDeployします。新しい設定項目は `AIRFLOW_PORT`（既定8081）、`AIRFLOW_DB_PASSWORD`、`AIRFLOW_FERNET_KEY`、`AIRFLOW_JWT_SECRET` です。Fernet鍵は32バイトをURL-safe Base64にした値で、通常のhex乱数とは異なります。

**Bash / Zsh共通**

```bash
./scripts/lakehouse.sh --env-file "$LAKEHOUSE_ENV_FILE" up
```

既定では `http://localhost:8081/` から開きます。初回のSimple Auth管理者名は `admin` です。ランダムな初期パスワードはAirflowの名前付きstateボリューム内にある `/opt/airflow/simple_auth_manager_passwords.json.generated` へ生成されます。導入先のローカル端末でのみ確認し、ファイル内容や認証情報を公開ログ・Git・チャットへコピーしません。`standalone` の起動ログにも初期認証情報が出る場合があるため、生ログを共有しません。

Connection画面で次を設定します。

| 項目 | 値 |
| --- | --- |
| Connection ID | `lakehouse_worker` |
| Connection Type | `HTTP` |
| Host | `worker` |
| Schema | `http` |
| Port | `8000` |
| Password | private envの `WORKER_TOKEN` の値 |

このConnectionはタスク実行時に読みます。トークンはDAGやGitへ保存しません。workerとTrinoを含むcoreを先に起動し、必要な `.ready` 付き入力を用意してから、DAG画面で `lakehouse_medallion` を手動実行します。DAGはBearer認証付きでworkerの `/run` へ `medallion` を依頼します。SQL変換はcoreの版管理されたSQLを使います。

Airflow自身がデータを生成したり外部DBへ書き込んだりはしません。疑似データ生成、coreの変換、Airflowによる実行依頼はそれぞれ別の処理です。タスクは認証失敗・応答エラーを検出し、トークンとworker応答の詳細をタスクログやXComへ出さない実装です。

### 検証用途と本番運用の境界

`standalone` はAPIサーバー、scheduler、DAG processor等を一つのコンテナで動かす検証構成です。本番では役割を分離し、企業IdP、Secrets backend、バックアップ、監視、ワーカーの資源枠と再試行方針を別途設計します。

PostgreSQL 18のデータボリュームはイメージの規約に合わせて `/var/lib/postgresql` へマウントします。既存の17系データを18のコンテナへそのまま付け替えることはメジャーアップグレードの手順になりません。既存データがある場合は、dump/restoreまたは正式なアップグレード手順と復元試験を先に確定します。

## Dokploy 0.31.0・Docker29のproxy変更の検証境界

今回の変更は既存の採用image/digest、Docker29の `docker info --format '{{json .Swarm.Cluster}}'`、optional CA placeholder、canonical NO_PROXYを維持し、Dokploy本体の管理env同期を追加します。daemon proxyと本体env、アプリbuild/runtimeの三層を混同しません。[適用面と診断](COMPANY-PROXY.md#dokploy配置で確認する3つのproxy適用面)

| 検証の種類 | この変更で確認する内容 | 別途必要な受入 |
| --- | --- | --- |
| unit / 合成private設定 | 0/1既定・upgrade保持、不正値拒否、6変数とNO_PROXY、重複修復・no-op、timeout・秘密非表示 | 実proxyの認証方式・CA・許可先 |
| CLI/Compose静的検査 | Bash構文、dry-run無変更、全profileの補間、既存Docker29/CA/bypass回帰 | 同じhostでのimage buildとservice起動 |
| 本体serviceの現地受入 | 今回のローカル変更では未実施 | ownershipの一致、更新specの収束、Git clone、registry/token疎通 |
| BuildKit効果 | `DOKPLOY_BUILDKIT_NO_CLIENT_TOKEN=1` を標準名へ渡す契約まで | キャッシュにない固定baseでのauth/metadata再試験。解消保証なし |

BuildKit switchはclient token取得経路の変更候補です。上流masterの [authprovider実装](https://github.com/moby/buildkit/blob/master/session/auth/authprovider/authprovider.go) から、採用Docker/Composeに同梱された実装と同じだと断定しません。company proxyで失敗を直した実績や認証回避機能としては扱いません。[Docker service updateのenv仕様](https://docs.docker.com/reference/cli/docker/service/update/)

### 将来提案: 完成imageの事前build

中期案として、CIでworker/demo/Superset/CDC/MCPの完成imageをbuildし、GHCR・GitLab Container Registry・Harbor等へ保管してComposeからdigest固定で使う方式を検討します。配置先でのBuildKit依存を減らせますが、registry到達・CA・資格情報・image配布範囲の設計は残ります。これは文書内の将来提案に留め、今回の変更では公開Issue作成、CIからのimage公開、既存ローカルbuild方式の切替を行いません。

## プロキシと確認状態

各任意イメージのbuildへ渡す変数は、`HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY` とその小文字の **6項目** です。一方、private `site.env` の生成・upgradeでは **7項目目の `LAKEHOUSE_NO_PROXY`** も保存します。この専用キーへ必須内部名と利用者の追加先をまとめ、大小文字のNO_PROXYと同期します。CLIは選択したprivate設定から正規化し、Dokployのraw配置では生成・upgrade済みの専用キーをサービスEnvironmentにも転記します。

JAR・constraints・Python依存の取得はbuild側、必要な外向きHTTP接続はruntime側でこの設定を使います。内部の `storage` / `catalog` / `trino` / `worker` 等を会社proxyへ送らないよう、必須内部名を標準化しています。Javaの外向き接続が環境変数だけで対応すると仮定せず、[PROXY.md](PROXY.md) のJVM/SDK設定と企業CAを必要に応じて適用します。

配布物の版・digest・checksum照合とPython構文検査、Compose結合検査は実施済みです。SparkのREST/S3書き込みとTrino相互参照、およびAirflowコンテナからのDAG実行は実機で成功しました。詳細は [検証記録](VALIDATION.md) を参照してください。会社proxyと外部SQL Serverは、その環境で別途確認します。

## 検証で必要になったS3アクセス設定

PyIcebergは `header.X-Iceberg-Access-Delegation=client-managed` を指定し、private設定のS3資格情報を使います。Lakekeeperが自動選択するremote signingへ切り替わらないようにするためです。Sparkが作った表をこのFileIOでも読み取れることを確認しました。これは共有資格情報を使う単一チーム向け基盤であり、利用者ごとのテーブル認可を保証する設定ではありません。OIDC・STSの短命資格情報を導入するときは、権限外アクセスの拒否を含めて別途検証します。

[PyIcebergのREST設定](https://py.iceberg.apache.org/configuration/) / [Lakekeeperのアクセス方式実装](https://github.com/lakekeeper/lakekeeper/blob/v0.14.3/crates/lakekeeper/src/api/iceberg/v1/tables.rs)


Sparkの共通imageは `spark-volumes-init` だけがbuildし、Master/Worker/Historyと明示smokeは同じimageを参照します。`spark-check` CLIも先に共通imageをbuildします。`up --build` を含む配置更新ではdaemonが再作成される場合があるため、実行中ジョブの完了を確認してから更新します。履歴volumeの保持は無停止の保証とは別です。

## 今回照合した公式資料と実装

公式サイトに版選択がない場合は、release tagと同梱コードも確認しています。`latest` のURLだけから採用版の互換性を推定しません。

| 確認対象 | 一次資料 | このリポジトリの正本 |
| --- | --- | --- |
| Spark RPC・UI・event log | [4.1.4 Standalone](https://spark.apache.org/docs/4.1.4/spark-standalone.html)、[4.1.4 Monitoring](https://spark.apache.org/docs/4.1.4/monitoring.html) | `infra/compute/`、`infra/poc/lakehouse_session.py` |
| Iceberg catalog設定 | [1.12.0 tagの設定文書](https://github.com/apache/iceberg/blob/apache-iceberg-1.12.0/docs/docs/spark-configuration.md) | 固定JAR・checksum、SparkSession設定 |
| Airflow GUI接続とrun | [3.4.0 Connections](https://airflow.apache.org/docs/apache-airflow/3.4.0/howto/connection.html)、[3.4.0 Dag Runs](https://airflow.apache.org/docs/apache-airflow/3.4.0/core-concepts/dag-run.html) | `infra/orchestration/Dockerfile` とDAG |
| Superset dataset・chart | [6.1.0 Dashboard tutorial](https://superset.apache.org/user-docs/6.1.0/using-superset/creating-your-first-dashboard/) | `infra/poc/superset_init.py` とSuperset lock |
| MCP v2 transport | [SDK v2.3.0のClient transports](https://github.com/modelcontextprotocol/python-sdk/blob/v2.3.0/docs/client/transports.md) | `infra/mcp/server.py` とlock |
| AI固定package | [langchain-core 1.7.6](https://pypi.org/project/langchain-core/1.7.6/)、[langchain-ollama 1.1.0](https://pypi.org/project/langchain-ollama/1.1.0/) | `examples/ai/requirements.lock` |

詳細なGUI・認証・保存の照合先は [GUI-WALKTHROUGHS.md](GUI-WALKTHROUGHS.md)、[GRAFANA.md](GRAFANA.md)、[POC-TOOLS.md](POC-TOOLS.md)、[MCP.md](MCP.md) の末尾にあります。

## 更新候補を採用版へ変えるための確認順

1. **配布物を固定する。** image tagとdigest、JAR coordinateとchecksum、Python requirementsとlock、公式constraintsを一つの変更へまとめます。
2. **依存を検査する。** 専用uv環境/imageで解決し、既存venvを意図せず別環境へ置き換えていないことを確認します。
3. **小さい往復で確認する。** Spark→Iceberg→Trino、worker→REST→S3、Airflow→workerの各境界を別々に確認します。
4. **更新後の状態を確認する。** 既存tableの読戻し、新規書込み、再実行、再起動後の履歴/GUI保存を照合します。新規の空環境での成功だけで既存データ互換を判定しません。
5. **採用記録を更新する。** 結果のcommit、platform、入力範囲、未確認事項を記録し、lock・文書・図の版表示を同時に更新します。

Sparkの受入ではlocal smokeとremote実行を両方記録します。local smokeが成功してもexecutorのPython環境やdriver callbackは検証できません。AirflowではDAGのimport成功とtaskの実HTTP成功を分けます。

### 症状から版・設定の調査先を絞る

| 症状 | 最初に比較するもの | 避ける変更 |
| --- | --- | --- |
| Sparkのclassが見つからない/同名methodがない | Spark minor、Scala suffix、Iceberg runtimeとAWS bundle | 不足しそうなJARを別版で追加する |
| Python taskだけremoteで失敗 | driver/executorのPython minorと実行path、共有依存 | 接続失敗をlocalへ自動fallbackさせる |
| Arrow処理だけ失敗 | 両側のPandas/Arrow/NumPy、schemaと型 | Notebookだけ最新版へ更新する |
| Airflow UIは開くがDAGが読めない | SDK/Operator import、Provider、constraintsのPython版 | Airflow2系の手順を混在させる |
| RESTは通るがmanifest/Parquetを読めない | FileIO、S3 endpoint/region/path-style、access方式 | TLS検証や認証を外す |
| PostgreSQL更新後に起動しない | 既存data directoryのmajor版と移行手順 | 旧volumeを新majorへそのまま接続する |

実装の対応先は [SparkSession helper](../infra/poc/lakehouse_session.py)、[Spark daemon](../infra/compute/spark_cluster.py)、[Airflow Dockerfile](../infra/orchestration/Dockerfile)、[worker FileIO](../scripts/admin.py) です。障害の切分けは [TROUBLESHOOTING.md](TROUBLESHOOTING.md) を使います。

## 固定SQL sourceとCDCの検証範囲

SQL Server 2022向けDDLは型・PK・所有権・固定captureを静的検査し、SQLFluffで全文parseします。Debezium 3.7.1.Finalの設定は3.7公式仕様に合わせ、fixed-demoだけexclusive初回snapshot、adaptive日時、decimal文字列を使います。Pythonの合成イベント・in-memory catalogueによるreplay試験と、実SQL Server→Kafka→Icebergの受入試験は別です。実接続・Agent・外部Proxy・Java/PythonのCA検証・イメージbuildは導入先で確認します。[固定source手順と制約](SQLSERVER-FIXED-DEMO.md)


新しいplan/apply/probe、projectの優先順、local-only、結果の保存と未受入事項は [COMPUTE-VALIDATION.md](COMPUTE-VALIDATION.md) を正本とします。raw Composeだけのsmokeではhost preflightとprivate結果保存を行わないため、通常は同じprojectを明示するCLIの専用操作を使います。Deployの管理入口は切り替えません。

監視拡張のhost権限・GUI保持・検証未了事項は [段階的な監視基盤](OBSERVABILITY.md) を参照してください。既存monitoringの更新でも、新規host mountは配置前に承認が必要です。

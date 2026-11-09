# バージョンと連携互換性

調査日: 2026-11-09。公開リリース、配布パッケージ、実在するMaven座標とイメージmanifestを照合しました。以下は依存関係を固定した採用構成です。配布物の実在確認、ビルド成功、コンテナ起動、システム間の読み書きは別の検証として扱い、実機の成功結果は検証記録を正とします。

## 最新安定版と採用版

| 部品 | 確認した最新安定版 | 採用・判断 |
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

Spark 4.3.0が最新であることと、IcebergのSpark 4.3向けruntimeが公開されていることは別です。調査時点で `iceberg-spark-runtime-4.2_2.13` と `4.3_2.13` のMaven metadataは404でした。4.1用JARを4.3へ流用せず、今回の連携プロファイルは4.1.4へ固定します。[Spark公式リリース](https://spark.apache.org/news/) / [Iceberg公式リリース](https://iceberg.apache.org/releases/) / [4.1用Maven metadata](https://repo.maven.apache.org/maven2/org/apache/iceberg/iceberg-spark-runtime-4.1_2.13/maven-metadata.xml)

AirflowはGitHubの正式リリースとPyPIの安定版を照合しました。開発中の `/stable` 文書やrc版だけを見て採用版を決めません。[3.4.0正式リリース](https://github.com/apache/airflow/releases/tag/3.4.0) / [PyPI配布](https://pypi.org/project/apache-airflow/3.4.0/) / [standard provider](https://pypi.org/project/apache-airflow-providers-standard/1.21.0/)

core workerはPython 3.14を採用します。Python 3.15は依存互換性を確認していないため採用しません。CIのホストPython 3.12、AirflowのPython 3.12、Sparkの同梱Pythonは独立した環境で、workerのPythonへ合わせて一括更新するものではありません。

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

```bash
./scripts/lakehouse.sh --env-file /absolute/private/site.env spark-check
```

この操作は `compute` プロファイルの `spark-check` を一時コンテナとして起動し、local[2]で既存の相互参照を確認します。通常の `up` ではこの一時serviceだけをreplicas 0にし、上記Master・Worker・Historyを常駐させます。Docker Composeを直接使う場合は同じ追加Composeを全て指定し、`--profile compute run --rm --build --no-deps spark-check` を実行します。

`infra/compute/smoke.sql` は専用の `compatibility.spark_roundtrip` 表を作成・置換します。固定の3行に日本語・decimalを含め、Sparkで書いた表をSparkで読み、その後Trinoの `iceberg.compatibility.spark_roundtrip` から同じ行を読みます。再実行でも追記による二重化をせず、業務表は対象にしません。成功時は版と行数だけを出力し、鍵や接続文字列を表示しません。

この一時スモークは小さなlocal実行の読み書き確認です。常駐clusterの受入では、別途Jupyterから分散task・Python UDF・Arrow処理を実行し、Masterの完了アプリ、Workerのexecutor、Historyのevent logを照合します。大量データ、複数writerの競合、複数ホスト、認証付きOIDC、Iceberg v3の機能は別の確認です。

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

private envの `COMPOSE_PROFILES` に `orchestration` を追加し、通常の `up` で起動します。新しい設定項目は `AIRFLOW_PORT`（既定8081）、`AIRFLOW_DB_PASSWORD`、`AIRFLOW_FERNET_KEY`、`AIRFLOW_JWT_SECRET` です。Fernet鍵は32バイトをURL-safe Base64にした値で、通常のhex乱数とは異なります。

```bash
./scripts/lakehouse.sh --env-file /absolute/private/site.env up
```

既定では `http://localhost:8081/` から開きます。初回のSimple Auth管理者名は `admin` です。ランダムな初期パスワードはAirflowの名前付きstateボリューム内にある `/opt/airflow/simple_auth_manager_passwords.json.generated` へ生成されます。導入先のローカル端末でのみ確認し、ファイル内容や認証情報を公開ログ・Git・チャットへコピーしません。`standalone` の起動ログにも初期認証情報が出る場合があるため、生ログを共有しません。

Connection画面で次を設定します。

| 項目 | 値 |
| --- | --- |
| Connection ID | `lakehouse_worker` |
| Connection Type | `HTTP` |
| Host | `http://worker` |
| Port | `8000` |
| Password | private envの `WORKER_TOKEN` の値 |

このConnectionはタスク実行時に読みます。トークンはDAGやGitへ保存しません。workerとTrinoを含むcoreを先に起動し、必要な `.ready` 付き入力を用意してから、DAG画面で `lakehouse_medallion` を手動実行します。DAGはBearer認証付きでworkerの `/run` へ `medallion` を依頼します。SQL変換はcoreの版管理されたSQLを使います。

Airflow自身がデータを生成したり外部DBへ書き込んだりはしません。疑似データ生成、coreの変換、Airflowによる実行依頼はそれぞれ別の処理です。タスクは認証失敗・応答エラーを検出し、トークンとworker応答の詳細をタスクログやXComへ出さない実装です。

### 検証用途と本番運用の境界

`standalone` はAPIサーバー、scheduler、DAG processor等を一つのコンテナで動かす検証構成です。本番では役割を分離し、企業IdP、Secrets backend、バックアップ、監視、ワーカーの資源枠と再試行方針を別途設計します。

PostgreSQL 18のデータボリュームはイメージの規約に合わせて `/var/lib/postgresql` へマウントします。既存の17系データを18のコンテナへそのまま付け替えることはメジャーアップグレードの手順になりません。既存データがある場合は、dump/restoreまたは正式なアップグレード手順と復元試験を先に確定します。

## プロキシと確認状態

各任意イメージのビルドは6つのHTTP(S)/NO_PROXY変数を受け取り、JAR・constraints・Python依存の取得に使用します。実行コンテナにも必要な変数を渡します。Javaの外向き接続は環境変数だけで対応すると仮定せず、[PROXY.md](PROXY.md) のJVM/SDK設定と企業CAを必要に応じて適用します。coreの `catalog`、`storage`、`trino`、`worker`、`airflow`、`airflow-db` 等は内部接続としてNO_PROXYに追加します。

配布物の版・digest・checksum照合とPython構文検査、Compose結合検査は実施済みです。SparkのREST/S3書き込みとTrino相互参照、およびAirflowコンテナからのDAG実行は実機で成功しました。詳細は [検証記録](VALIDATION.md) を参照してください。会社proxyと外部SQL Serverは、その環境で別途確認します。

## 検証で必要になったS3アクセス設定

PyIcebergは `header.X-Iceberg-Access-Delegation=client-managed` を指定し、private設定のS3資格情報を使います。Lakekeeperが自動選択するremote signingへ切り替わらないようにするためです。Sparkが作った表をこのFileIOでも読み取れることを確認しました。これは共有資格情報を使う単一チーム向け基盤であり、利用者ごとのテーブル認可を保証する設定ではありません。OIDC・STSの短命資格情報を導入するときは、権限外アクセスの拒否を含めて別途検証します。

[PyIcebergのREST設定](https://py.iceberg.apache.org/configuration/) / [Lakekeeperのアクセス方式実装](https://github.com/lakekeeper/lakekeeper/blob/v0.14.3/crates/lakekeeper/src/api/iceberg/v1/tables.rs)


Sparkの共通imageは `spark-volumes-init` だけがbuildし、Master/Worker/Historyと明示smokeは同じimageを参照します。`spark-check` CLIも先に共通imageをbuildします。`up --build` を含む配置更新ではdaemonが再作成される場合があるため、実行中ジョブの完了を確認してから更新します。履歴volumeの保持は無停止の保証とは別です。

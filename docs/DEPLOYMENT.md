# 導入・検証・停止

公開リポジトリ `lakehouse-foundation` を取得し、実際の環境設定だけをチェックアウトの外へ置いて導入します。clone URLはREADMEを参照してください。この文書の `/absolute/private/site.env`、共有利用者、許可範囲は説明用であり、導入先で選んだ値に置き換えます。

## 前提条件

- Linux、Bash、Python 3、Git、Docker Engine、Docker Composeプラグイン。
- Composeのinline configを使うため **Docker Compose 2.23.1以上**。Pythonはスクリプトが要求する版を使用します。
- 実行ユーザーがDockerを操作できること。Dockerを操作できる権限はホスト管理権限として扱います。
- 使用するイメージを取得できる通信、または社内レジストリへのミラー。
- coreと任意のプロファイルを動かすRAM・CPU・永続ディスク、既存サービスと競合しないポート。

SambaとDokployのホスト設定には別途管理権限が必要です。既存のDocker/Swarm、共有、Webポートがある場合は、その状態を確認してから該当手順を実行します。coreの起動だけでSambaやDokployのホスト設定は変更されません。

```bash
docker version
docker compose version
python3 --version
```

inline configの必要版は [Docker Compose公式文書](https://docs.docker.com/reference/compose-file/configs/) を参照します。

## ホストのパスとコンテナ内のパス

cloneする場所、ホスト上のランディング、コンテナ内の作業場所は別のものです。公開ソースに特定の利用者のホームディレクトリを設定する必要はありません。

| パスの種類 | 決め方 |
| --- | --- |
| リポジトリの取得先 | 任意。スクリプトは自身の配置場所（Pythonの `__file__` 等）からルートを解決する |
| private設定ファイル | オペレーターがリポジトリ外の場所を選び、`--env-file` または `LAKEHOUSE_ENV_FILE` で指定する |
| ホストのランディング | private envの `LANDING_DIR` に専用の絶対パスを指定する |
| DB・S3等の名前付きvolume | Dockerが管理する。clone先のディレクトリには保存しない |
| コンテナ内のコード | `/opt/lakehouse` 等、イメージ内で決めた場所を使用する |
| コンテナ内のランディング | ホストの `LANDING_DIR` を `/landing` として参照する |
| 公式イメージ内の利用者領域 | `/home/node` 等はコンテナ内のパス。ホストの利用者名やホームの指定ではない |

たとえば、ホスト側で選んだランディング先の名前を変えても、コンテナからは `/landing` として見えます。Sambaの共有先はホスト側の `LANDING_DIR` と一致させます。共有する既存ディレクトリの所有者を確認し、workerのUID/GIDを合わせて読み取りを許可します。

名前付きvolumeの実際の保存先はDockerの設定で決まります。clone先を変えることと、データの保存先を移すことは別です。同じ `COMPOSE_PROJECT_NAME`／Dokployの `appName` と既存volumeを維持して再配置すれば、コードの取得先に依存せずデータを保持できます。

DokployのGitソースでは、作業コピーの場所はDokployが管理します。`/etc/dokploy/...` のようなclone先を公開Composeへ書き込む必要はありません。ランディング用のホストパスだけを `LANDING_DIR` で指定します。移行元のRawに導入先固有のパスがある場合も、それを公開Gitの正本へ持ち込まず、必要なサイト設定へ移します。詳細は [DOKPLOY.md](DOKPLOY.md) を参照してください。

### private設定ファイルの指定を省略する

生成済みのprivate設定ファイルの場所を、一度だけ環境変数へ設定できます。次は利用者自身の設定ディレクトリに保存した場合の例です。`$HOME` は実行する利用者に合わせて展開され、公開ソースに個人のパスを固定しません。

```bash
export LAKEHOUSE_ENV_FILE="$HOME/.config/lakehouse-foundation/site.env"
```

そのシェルでは `--env-file` を省略し、必要な操作を選んで実行できます。

| 操作 | コマンド |
| --- | --- |
| 設定の確認 | `./scripts/lakehouse.sh plan` |
| 起動 | `./scripts/lakehouse.sh up` |
| 取り込みとメダリオン | `./scripts/lakehouse.sh medallion` |
| 状態の確認 | `./scripts/lakehouse.sh status` |
| volumeを保持して停止 | `./scripts/lakehouse.sh down` |

明示した `--env-file` が環境変数より優先されます。既存の指定方法もそのまま使用できます。

```bash
./scripts/lakehouse.sh --env-file /absolute/private/another-site.env plan
```

どちらの方法でも秘密ファイルはGitの外に保持します。環境変数へ設定するのはファイルの場所であり、パスワードの内容をシェルへ貼り付ける必要はありません。

## 1. 非公開のサイト設定を作る

cloneしたリポジトリのルートで実行します。生成先は自分で選べます。`LAKEHOUSE_ENV_FILE` を使う場合は、その変数で指定した場所と同じ場所へ生成します。

```bash
python3 scripts/configure.py --output /absolute/private/site.env
```

生成されたファイルをローカルのエディターで開きます。実値をREADME、Compose、Gitのコミット、公開する検証ログへ転記しません。ファイルは0600、格納先は0700の非公開ディレクトリとします。

| 設定 | 初期値・設定方法 |
| --- | --- |
| `COMPOSE_PROJECT_NAME` | `lakehouse`。同じDockerホストで複数環境を動かす場合は変更 |
| `DATA_VOLUME_PREFIX` | 通常は空欄。既存volume名を引き継ぐ移行時だけ、非公開設定へ既存の接頭辞を指定 |
| `BIND_IP` | `127.0.0.1`。他端末から接続する場合だけ導入先の限定アドレスへ変更 |
| `LANDING_DIR` | リポジトリ外の専用絶対パス。Sambaの共有先と同じ値にする |
| `TRINO_PORT` | `8080` |
| `CATALOG_PORT` | `8181` |
| `S3_PORT` | `8333` |
| `GRAFANA_PORT` | `3000`。monitoring有効時のみ使用 |
| `PROMETHEUS_PORT` | `9090`。monitoring有効時のみ使用 |
| `N8N_PORT` | `5678`。automation有効時のみ使用 |
| `AIRFLOW_PORT` | `8081`。orchestration有効時のみ使用 |
| `COMPOSE_PROFILES` | 初期は空。`automation`、`monitoring`、`compute`、`orchestration`、`demo`、`cdc` を必要に応じてカンマ区切り |
| `S3_BUCKET_RAW` | `lakehouse-raw` |
| `S3_BUCKET_WAREHOUSE` | `lakehouse-warehouse` |
| `WAREHOUSE_NAME` | `lakehouse` |
| S3・PostgreSQL・暗号鍵等 | 生成値を保持。導入後に再生成しない |
| プロキシ6変数 | 直接接続ではHTTP(S)を空欄。会社では許可されたproxyとNO_PROXYを設定 |
| `SOURCE_SQLSERVER_*` | 外部の会社SQL Serverを使うときだけ設定 |
| `DEMO_SQLSERVER_*` | 疑似データをSQLへ投入するときだけ設定。SOURCEとは別の専用DB |

同じ `COMPOSE_PROJECT_NAME` と同じ設定ファイルを継続して使います。既存ボリュームを使う状態でDBパスワードや暗号鍵だけを変更すると、接続や復号に失敗します。設定ファイルはデータと合わせて安全にバックアップしてください。

`DATA_VOLUME_PREFIX` が空欄または未設定なら、名前付きvolumeは `<Composeプロジェクト名>_<volumeキー>` となり、既定の動作は変わりません。core側のComposeに7個、Airflowに2個の計9個を定義し、CDCでは `cdc-kafka` と `cdc-state` の2個が加わり計11個です。使用するprofileに応じて実体を確認します。ランディングやprivate設定等のbind mountはこの数に含めません。指定するとデータの物理名だけがその接頭辞へ変わります。コンテナ・ネットワークの識別子である `COMPOSE_PROJECT_NAME` やDokployの `appName` と区別し、既存volume名を維持する移行専用として扱います。値は空欄、または先頭が小文字英字で、小文字英数字・ハイフン・アンダースコアからなる41文字以内です。

volumeの自動rename・コピーは行いません。移行前に使用する全volumeの解決名と既存の実名を照合し、CDCを使う場合は追加の2個も含めます。旧writerを停止してから一つの配置へ切り替えます。別の環境と同じ接頭辞を共有せず、移行後も指定を保持します。詳細は [Dokployで既存volumeを引き継ぐ手順](DOKPLOY.md#既存volume名を引き継ぐ場合) を参照します。

`BIND_IP=127.0.0.1` のままでは、画面をサーバー自身のブラウザーかSSHポート転送から利用します。別端末の `localhost` は別端末自身を指します。LAN等のアドレスへ変更する場合、ネットワーク制限と利用者認証を先に検討します。標準coreは単一ホストでの検証向けです。

## 2. 会社のプロキシを設定する

サイト設定の `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY` と小文字版を設定します。内部サービスは `NO_PROXY` に含めます。値は私有ファイルに保持します。

コンテナのenvだけではDocker daemonのimage pullに適用されません。会社でpull用プロキシが必要なら、[PROXY.md](PROXY.md) の独立キットでdaemon用設定を用意します。設定の配置とDocker再起動は別操作です。既存コンテナへの影響を確認してから反映します。

```bash
./scripts/configure-proxy.sh --init /absolute/private/proxy.env
```

ローカルで編集した後に確認します。

```bash
./scripts/configure-proxy.sh --env-file /absolute/private/proxy.env --plan
sudo ./scripts/configure-proxy.sh --env-file /absolute/private/proxy.env --apply
```

`--apply` はdaemonを再起動しません。企業CA、認証方式、Javaや各SDKの対応は [PROXY.md](PROXY.md) の確認項目に従います。会社のproxyでの接続は、その実環境で検証してから成功と記録します。

## 3. coreを起動する

`LANDING_DIR` は先に作成し、共有利用者が読み書きできる所有者・権限を設定します。後述のSamba手順で作成するか、管理者が専用ディレクトリを用意します。既定の `/srv/lakehouse/landing` を使う例は次のとおりです。利用者・グループを環境に合わせて置き換えます。

```bash
sudo install -d -m 0770 -o <existing-share-user> -g <existing-share-group> /srv/lakehouse/landing
```

起動前の設定確認はホストを変更しません。

```bash
./scripts/lakehouse.sh --env-file /absolute/private/site.env plan
```

```bash
./scripts/lakehouse.sh --env-file /absolute/private/site.env up
```

この操作はSeaweedFS、PostgreSQL、Lakekeeperのマイグレーションとサーバー、Trinoを起動し、初期化ジョブでS3バケットとWarehouseを準備します。初期化状態を確認し、毎回新しいWarehouseを重複作成しない構成です。

初回はimage取得と起動に時間がかかります。失敗した場合は原因を修正して同じ設定で再実行します。初期化途中のDBやボリュームを削除してやり直す必要があると即断しません。

Lakekeeperは初期化時に利用条件への同意を記録するAPIを使います。採用ソフトウェアの利用条件は [ARCHITECTURE.md](ARCHITECTURE.md) と上流資料を確認してください。

## 4. coreの往復試験を行う

```bash
./scripts/lakehouse.sh --env-file /absolute/private/site.env verify
```

合成のCSV・JSON・画像を使い、S3に原本を保存し、IcebergのメタデータとTrinoのSQL結果を照合します。この検証は専用の試験データを作成します。画像原本の保存とそのメタデータの検証を、画像理解・OCRの実装完了とは扱いません。

coreの検証成功が確認するのは、このスタック内のS3・RESTカタログ・Iceberg・Trinoの接続です。Sambaクライアントからの読み書き、会社SQL Server、実際の会社proxy、バックアップからの復旧はそれぞれ別の確認です。

既定のアドレス・ポートを使った場合の入口は次のとおりです。ポートを変更した場合はprivate envに合わせます。

| 入口 | サーバー自身からの接続 |
| --- | --- |
| Trino | `http://localhost:8080/` |
| Lakekeeper UI | `http://localhost:8181/ui/` |
| Iceberg REST | `http://localhost:8181/catalog` |
| S3 API | `http://localhost:8333/`。ブラウザー向け一覧画面ではなく署名付きAPI |
| Grafana（monitoring） | `http://localhost:3000/` |
| Prometheus（monitoring） | `http://localhost:9090/` |
| n8n（automation） | `http://localhost:5678/` |
| Airflow（orchestration） | `http://localhost:8081/` |

## 5. MES・ERP疑似データでメダリオンを動かす

製品・製造指図・受注・出荷のCSVと、製造・停止実績のJSON Linesを生成できます。`--output` は `LANDING_DIR` と同じ専用ディレクトリか、別のリポジトリ外の作業場所を指定します。既定のランディング先へ生成する例です。

```bash
python3 scripts/generate-demo.py \
  --destination landing --output /srv/lakehouse/landing \
  --seed 42 --orders 20 --days 7 --scenario edge
```

生成器の仕事は素材の生成と投入までです。Iceberg・Trino・メダリオン処理は呼び出しません。`landing` の出力は6ソース、ファイルごとの `.ready` 完了マーカー、件数・ハッシュ等のmanifestです。変換結果の期待値は生成器から独立した `medallion/expected-demo.json` に保持します。詳細な列・型・注入する異常・指標は [DEMO-DATA.md](DEMO-DATA.md) を参照します。

SQLにも素材を投入したい場合は、private envに `DEMO_SQLSERVER_HOST` / `PORT` / `DATABASE` / `USER` / `PASSWORD` を設定します。業務参照用の `SOURCE_SQLSERVER_*` と別の専用DBを指定します。SQL用ドライバー等の前提は [DEMO-DATA.md](DEMO-DATA.md) を参照してください。

```bash
python3 scripts/generate-demo.py \
  --destination sqlserver --output /absolute/private/demo-source \
  --env-file /absolute/private/site.env --allow-sql-write \
  --seed 42 --orders 20 --days 7 --scenario edge
```

`both` はランディングとSQLへ投入します。SQLモードは `--allow-sql-write` が必要で、システムDBとSOURCEのDBを拒否します。毎回新しい `lh_demo_<random>` schemaを作り、6つのnvarchar staging表へ挿入します。既存schemaを再利用せず、DB作成・DROP・既存業務表の変更は行いません。SQL投入成功は後段のCDC・メダリオン成功を意味しません。

以下の取り込み・メダリオンは **別の操作** として実行します。このファイル経路の確認には `landing` か `both` を使います。SQLからは [読み取り専用extractor](SOURCE-SQLSERVER.md) と [CDC経路](CDC.md) も提供しています。extractorは表・列のmapping、CDCは専用の接続・対象表・有効化が必要で、素材生成だけでは開始しません。会社の実DBでの接続と受入は未検証です。

Bronzeへの取り込みだけを実行する場合:

```bash
./scripts/lakehouse.sh --env-file /absolute/private/site.env ingest
```

Bronze取り込みに続けてSilver・隔離・Goldまで実行する場合:

```bash
./scripts/lakehouse.sh --env-file /absolute/private/site.env medallion
```

Silverは宣言した型へ変換し、主キーごとに最新の有効行を残します。無効行は同名のソース別quarantine表へ理由付きで隔離します。Goldの `factory_daily_production` と `sales_fulfillment` で、製造数量・停止時間・歩留まり・計画達成率と、受注・出荷・残数・履行率を参照できます。OEEは算出しません。

業務変換は `medallion/01_silver.sql`、`02_quarantine.sql`、`03_gold.sql` を編集します。同じデータで再実行しても追記による二重計上をしないよう派生表を置き換えます。ただし複数表をまとめたトランザクションではないため、失敗した場合は処理結果を確認して再実行します。

自分でファイルをコピーした場合は、コピー完了後に完了マーカーを作成します。取り込みは原本のSHA-256と一致するマーカーが必要です。

```bash
python3 scripts/mark-ready.py /srv/lakehouse/landing/csv/erp_products.csv
```

生成器はマーカーも出力するため、生成したままのファイルには追加作業は不要です。ファイルを書き換えた場合はマーカーを再生成します。

## 6. 任意のプロファイルを追加する

private envの `COMPOSE_PROFILES` を編集します。

```dotenv
COMPOSE_PROFILES=automation,monitoring
```

同じ `up` を実行します。

```bash
./scripts/lakehouse.sh --env-file /absolute/private/site.env up
```

`automation` はn8nと専用DB、`monitoring` はGrafanaとPrometheusを追加します。n8nの初回利用者登録や、通知先・業務ワークフローの設定は別途行います。Grafanaの初期資格情報は生成されたprivate envを参照し、公開文書に貼り付けません。

n8nの提供テンプレートをimportし、手動トリガーからワーカーの `/run` へ `medallion` を依頼できます。n8nの資格情報画面でHeader Authを作り、`Authorization` に `Bearer <WORKER_TOKEN>` を設定します。実トークンはprivate envの値を使い、テンプレートへ保存しません。列のマッピングや型変換の規則はSQLを編集し、GUIでは開始条件や接続を設定します。

定期実行のスケジュール、業務アラート、モデル推論は必要に応じて追加します。実装済みの [CDC](CDC.md) は `cdc` profileで、[操作GUI/API](DEMO-GUI.md) と [AI向けcontext取得](AI-READINESS.md) は `demo` profileで利用します。接続先・権限・mapping等の準備と導入先での受入を行い、profileを有効にしただけで業務フローが完成したとは扱いません。

Sparkは `spark-check` から一時ジョブとして実行します。Airflowは `orchestration` を有効化すると手動DAGを追加します。正確な版・JAR・Provider、追加のprivate鍵、GUIでのworker接続、実行手順は [COMPATIBILITY.md](COMPATIBILITY.md) を参照します。

## 7. 外部の会社SQL Serverを指定する

private envの次の項目へ、DB管理者から指定された接続先と参照用資格情報を設定します。

```dotenv
SOURCE_SQLSERVER_HOST=
SOURCE_SQLSERVER_PORT=1433
SOURCE_SQLSERVER_DATABASE=
SOURCE_SQLSERVER_USER=
SOURCE_SQLSERVER_PASSWORD=
```

設定後、名前解決とTCP到達性を確認します。

```bash
./scripts/lakehouse.sh --env-file /absolute/private/site.env source-check
```

`source-check` は外部DBへの読み取り専用の到達性確認です。TCP接続を確認するもので、SQL認証、SELECT、証明書検証、CDCの成功を確認するものではありません。SQLパスワードを渡してSQLを実行したり、外部DBの状態を変更したりしません。

読み取り専用extractorの表・列mappingは [sqlserver-tables.example.json](../infra/source/sqlserver-tables.example.json) を見本に、Git外のprivate設定として用意します。[SOURCE-SQLSERVER.md](SOURCE-SQLSERVER.md) の手順でSELECTとファイル出力を確認します。本物の資格情報やmappingを公開ファイルへ書き戻しません。連続取り込みには [CDC.md](CDC.md) の別手順を使い、暗号化と証明書の信頼は会社の構成に合わせて確認します。

coreの初期化ジョブは会社DBへテーブル作成・更新を行いません。CDCを開始する場合は、DB管理者と対象表、CDC有効化、権限、保持期間を別途決めます。TCP疎通、認証、SELECT、CDCの順に確認し、実機へ接続するまで外部DBは未検証と記録します。

## 8. Sambaのランディング共有を追加する

Sambaはホストの共有サービスへ別手順で設定します。`LANDING_DIR` と `--path` を同じ値にします。Sambaパッケージ、稼働する `smbd`、Unix利用者、Sambaアカウントが先に必要です。このキットは利用者パスワードを作成・変更しません。

最初は読み取り専用の計画を確認します。以下の山括弧の項目を、その会社で使う利用者と許可範囲へ置き換えます。

```bash
sudo python3 scripts/setup-samba.py \
  --user <existing-share-user> \
  --path /srv/lakehouse/landing \
  --allow <allowed-client-cidr> \
  --plan
```

内容を確認して適用します。

```bash
sudo python3 scripts/setup-samba.py \
  --user <existing-share-user> \
  --path /srv/lakehouse/landing \
  --allow <allowed-client-cidr> \
  --apply
```

共有名の既定値は `Lakehouse-Landing` です。既存の共有と競合する場合は処理を止めます。適用時にバックアップ、`testparm` による検証、`smbd` の設定再読込を行います。共有のアクセス許可は導入先のクライアントIP/CIDRを指定します。利用者パスワードと既存共有の所有権を一括変更しません。

許可したWindows/Macクライアントから、CSV・JSON・画像のコピー、読戻し、ハッシュ一致を確認します。この共有設定はファイルの受け口です。コピー完了後に `.ready` を作成し、CLIまたはn8nから取り込み・メダリオン処理を実行します。共有ディレクトリを自動監視して毎回起動する設定は別途追加します。

## 9. Dokployを別に導入する

DokployはcoreのComposeとは別に導入します。提供するインストーラーはx86-64 Linux向けにイメージを固定し、稼働中のDockerを前提にします。`--apply` なしの既定動作は設定の検証とplanだけです。公式の導入手順は [Dokployのインストール](https://docs.dokploy.com/docs/core/installation) を参照します。

private envに `DOKPLOY_BIND_IP`、`DOKPLOY_ALLOWED_CIDRS`、`DOKPLOY_ADMIN_PORT`、`SWARM_ADVERTISE_ADDR`、`SWARM_ADDRESS_POOL`、`SWARM_SUBNET_SIZE` を設定します。必要なら `DOCKER_BRIDGE_CIDR` も指定します。アドレスは導入先のものを使い、Swarmのアドレスプールは会社・VPN・Dockerの既存ネットワークと重複しない範囲にします。管理画面は既定3100、Traefikは80/443を使用するため、既存のWebサービスと競合しないか確認します。

```bash
bash infra/dokploy/install.sh --env-file /absolute/private/site.env
sudo bash infra/dokploy/install.sh --env-file /absolute/private/site.env --apply
```

管理画面を開いて最初の管理者アカウントを登録し、コンテナ・DBの正常性と画面へのアクセスを確認します。初回登録を完了するまで、管理画面への接続を意図した利用者のみに限定します。

既存のDokployがある場合は再利用を優先します。別のSwarmから強制離脱したり、既存のDB・ボリュームを初期化したりして導入しません。Dokployからcoreを管理する場合も、サイト固有envを非公開のEnvironment設定へ入れ、同じスタックをCLIとUIから同時に別名で起動しないようにします。

## 10. 停止・再開・復旧

```bash
./scripts/lakehouse.sh --env-file /absolute/private/site.env down
```

`down` はスタックを停止し、データボリュームを保持します。coreの `down` だけでSambaやDokployを停止することはありません。再開は同じenvを使って `up`、続いて `verify` を実行します。

ボリューム削除、プロジェクト名の変更、データディレクトリの移動、暗号鍵の再生成は通常の再起動手順に含みません。バックアップからの復旧は、カタログDB、暗号鍵、S3内のIcebergと原本、任意のn8n DBと暗号鍵の整合を確認して別環境で実施します。

## 問題が起きた場合

| 症状 | 確認すること |
| --- | --- |
| Composeが `content` を解釈しない | Composeプラグインが2.23.1以上か |
| imageを取得できない | Docker daemon側proxy、CA、認証、レジストリ到達性 |
| 内部接続がproxyへ流れる | サービス名・内部アドレスがNO_PROXYに含まれるか |
| PostgreSQLの認証失敗 | 初回作成時のパスワードと現在のenvが一致するか |
| Lakekeeperが起動しない | PostgreSQLの正常性、migrateの完了、暗号鍵の保持 |
| Warehouse初期化が失敗 | S3認証、バケット、内部endpoint、region、既存登録との一致 |
| TrinoがS3を使えない | `fs.s3.enabled`、S3鍵、endpoint、path-style設定 |
| 他端末から画面が開けない | BIND_IPがloopbackか、ポート・経路・アクセス制御が正しいか |
| SMB共有へ書けない | 既存Sambaアカウント、許可CIDR、Unix側のディレクトリ権限 |
| 外部SQL Serverへ接続できない | DNS、経路、ポート、TLS、認証、参照権限を個別に確認 |

詳細を共有する際は、envファイル、展開済みCompose、接続文字列、プロキシ認証情報、ホスト名・IPを含む生ログをそのまま公開しません。機能、エラーの種類、採用版、秘密を除いた再現手順を記録します。


Docker 29／Buildx Bake／社内proxyで判明した互換性修正の取り込みと、`LAKEHOUSE_NO_PROXY` の生成・Dokployへの登録は [会社VMの更新手順](COMPANY-UPDATE.md) を参照してください。

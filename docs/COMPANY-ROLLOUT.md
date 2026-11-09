# 会社への導入ガイド

このガイドは、初めて担当する人が公開ソースから閉域検証環境を構築し、会社SQL Serverの読み取りとメダリオン処理を確認するための順序を示します。更新日: 2026-11-09。ここにある成功条件は導入先で確認する項目であり、会社ネットワークで既に成功したという記録ではありません。

最初はcoreを単独で確認し、n8n、監視、Airflowを必要な順に追加します。接続元DB、Samba、企業proxy、Docker、データ内容は別々の担当範囲なので、失敗した段階が分かる形で進めます。

## 1. 担当と受入範囲を決める

| 担当 | 導入前に決める内容 |
| --- | --- |
| ホスト管理者 | Docker操作権限、永続ディスク、使えるport、既存Swarm/コンテナへの影響 |
| ネットワーク担当 | 外向きproxy、企業CA、許可宛先、SQL Serverと共有利用者からの到達経路 |
| DB担当 | 読み取り専用ログイン、対象表/ビュー、日時の保存方式、抽出の実行時間帯 |
| データ担当 | MES/ERPの6入力契約と実表の対応、件数・金額・数量の照合方法 |
| 運用担当 | private設定の保管、GUI管理者、バックアップ、監視、ジョブ実行の責任者 |

この版のcoreは単一ホスト・共有資格情報を使う検証構成です。会社DBでのCDC受入、企業IdP、冗長化、バックアップからの復元、AI推論を導入済みとは扱いません。`production` というDokploy環境名だけで本番要件が満たされるものでもありません。[ARCHITECTURE.md](ARCHITECTURE.md)

## 2. ホストと資源を確認する

Linux x86-64、Bash、Git、ホストPython 3.12以上、uv 0.13.7、Docker Engine、Docker Compose 2.23.1以上を用意します。通常利用者からDockerを操作できることを管理者が確認します。スクリプト自身はDocker権限を追加しません。SambaとDokployのホスト設定だけは別途管理権限が必要です。

```bash
uname -m
python3 --version
uv --version
docker version
docker compose version
docker info --format '{{.DockerRootDir}}'
free -h
docker system df
ss -lnt
```

永続ディスクはclone先ではなく、Dockerのdata rootとランディング先の両方を確認します。空き容量には原本、Icebergのデータ/metadata、旧snapshot、DB、ログ、バックアップの分を含めます。

| 構成 | Composeで設定するメモリ上限の概数 | 運用上の判断 |
| --- | --- | --- |
| core | 起動中のmigrationを含め約4.4GiB | まずこの構成だけで起動し、ホストOSと既存サービスの余力を別に確保する |
| n8n追加 | 約1.4GiB | ワークフローと実行履歴用DBの保存量も見る |
| 監視追加 | 約0.75GiB | Prometheusは15日/2GBの保存上限を設定。Grafana DBは別に保持する |
| Airflow追加 | 約4.4GiB | standaloneの複数役割を1コンテナで動かすため、core起動後に追加する |
| 操作画面・FastAPI追加 | 約1.25GiB | 専用private設定dirとlandingの書込権限を準備する |
| 常駐Spark | Master/Worker/Historyの上限はCompose参照 | computeで起動。Notebookのdriverメモリは別途確保する |
| Sparkスモーク | 一時的に2GiB | 明示の互換確認。常駐クラスタの代わりにはしない |

これらは設定上限の合計で、実測使用量や必要ホストRAMの保証ではありません。入力量・同時実行数・既存ワークロードを含めて導入先で実測します。port競合がある場合は [CONFIGURATION.md](CONFIGURATION.md) のhost portを変更し、既存サービスを停止して空ける判断を初手にしません。

## 3. 公開ソースを取得する

```bash
git clone https://github.com/hiromura16/lakehouse-foundation.git
cd lakehouse-foundation
git rev-parse HEAD
```

clone先は任意です。導入記録には確認したcommitと利用するprofileを残します。以後のコマンドはリポジトリのルートで実行します。Git取得にproxyが必要な会社では、会社標準のGit/端末proxy設定を先に使います。

イメージ、Spark JAR、Airflow constraintsは、リポジトリの固定値を一組で使います。最新版のタグへ一部だけ書き換えず、変更時は [COMPATIBILITY.md](COMPATIBILITY.md) に従って連携を確認します。

## 4. イメージ取得前にproxyとCAを準備する

Docker daemon、build、実行コンテナ、Java/SDKは別の接続主体です。`site.env` にproxyを書くだけでは、Docker daemonのimage pullへ反映されません。

```bash
./scripts/configure-proxy.sh \
  --init "$HOME/.config/lakehouse-foundation/proxy.env"
```

生成したファイルを手元で編集し、会社指定のHTTP(S) proxyと内部の除外先を記入します。直接接続ならHTTP(S)は空欄のままです。内容をシェルで `source` しません。

```bash
./scripts/configure-proxy.sh \
  --env-file "$HOME/.config/lakehouse-foundation/proxy.env" --plan
```

daemonへの配置が必要な会社だけ、管理者が `--apply` で配置し、Docker再起動の時間を決めます。このコマンドは設定ファイルを配置するだけで、daemonを再起動しません。

```bash
sudo ./scripts/configure-proxy.sh \
  --env-file "$HOME/.config/lakehouse-foundation/proxy.env" --apply
```

企業CAをホスト・必要なイメージ・JVMに登録してから通信を確認します。TLS検証の無効化で回避せず、詳しい反映方法と障害の切り分けは [PROXY.md](PROXY.md) を使います。

## 5. private設定を生成して固定する

```bash
python3 scripts/configure.py \
  --output "$HOME/.config/lakehouse-foundation/site.env"
export LAKEHOUSE_ENV_FILE="$HOME/.config/lakehouse-foundation/site.env"
```

生成先はリポジトリ外で、ファイル0600・親0700です。生成された秘密値を保持し、次の項目から編集します。

- `COMPOSE_PROJECT_NAME`：環境ごとの固定名。Dokployも使う場合は、その環境の `appName` と合わせる。
- `LANDING_DIR`、`WORKER_UID`、`WORKER_GID`：入力ディレクトリと読む利用者を合わせる。
- `BIND_IP` とport：初回はloopbackで確認し、共有する入口を必要な範囲で設計する。
- proxy6変数：daemon用ファイルとは別に `site.env` へも設定する。
- `COMPOSE_PROFILES`：初回は空。後から任意機能を追加する。

全項目の意味・生成既定・変更影響は [CONFIGURATION.md](CONFIGURATION.md) を参照します。`DATA_VOLUME_PREFIX` は新規導入では空のままにします。DBや暗号鍵を含む既存設定を、新しく生成したファイル全体で置き換えません。

## 6. ランディングを用意し、必要ならSambaを設定する

以下の例は新規の専用ディレクトリです。別の場所を使う場合は、private設定の `LANDING_DIR` と同じ完成した絶対パスへ置き換えます。

```bash
LANDING_PATH=/srv/lakehouse/landing
if [ -e "$LANDING_PATH" ]; then
  stat -c '%a %u:%g %n' "$LANDING_PATH"
else
  sudo install -d -m 0770 -o "$(id -un)" -g "$(id -gn)" "$LANDING_PATH"
fi
```

既存ディレクトリがある場合は、その所有者と用途を確認し、所有権を一括変更しません。上の `LANDING_PATH` は説明用のシェル変数で、`site.env` では変数展開されません。envには実際の絶対パスを書きます。workerはこの場所を `/landing` として読み取り、入力を書き換えません。

Sambaを使う場合は、会社標準の手順でSambaと `testparm` / `pdbedit` を用意します。共有に使うUnixユーザーとSambaアカウントは管理者が準備し、既存ユーザーのパスワードを再設定しないようにします。共有先はDockerやS3の内部volumeではなく、このランディングです。

```bash
SMB_SHARE_USER="$(id -un)"
read -r -p '許可する接続元CIDRを入力: ' SMB_ALLOWED_CIDR
python3 scripts/setup-samba.py \
  --user "$SMB_SHARE_USER" --allow "$SMB_ALLOWED_CIDR" \
  --path "$LANDING_PATH" --plan
```

planが対象共有と明示allowlistを示したら、管理者が同じ引数で適用します。

```bash
sudo python3 scripts/setup-samba.py \
  --user "$SMB_SHARE_USER" --allow "$SMB_ALLOWED_CIDR" \
  --path "$LANDING_PATH" --apply
```

共有名の既定は `Lakehouse-Landing`。専用includeだけを追加し、既存共有や認証情報を変更しません。同名共有や曖昧なincludeは拒否します。適用時はbackup→testparm→smbd reloadを行い、失敗時は設定を戻します。許可されたクライアントから認証して、新しいファイルの作成・読み取り・削除を確認してください。これは後述のcore verifyとは別の受入項目です。

手動投入するファイルはコピー完了後に `.ready` を作成します。

```bash
python3 scripts/mark-ready.py "$LANDING_PATH/csv/erp_products.csv"
```

取り込み可能な業務ファイルの名前・列は [sources.json](../medallion/sources.json) が正本です。任意のCSV名を置くだけで業務のSilver/Goldが自動生成される仕組みではありません。

## 7. coreを起動して内部の往復を確認する

```bash
./scripts/lakehouse.sh plan
./scripts/lakehouse.sh up
./scripts/lakehouse.sh status
./scripts/lakehouse.sh verify
```

`plan` は設定の検査、`up` はbuild・起動・初期化、`status` はコンテナ状態の確認です。`catalog-migrate` の正常終了と、catalog/Trino/workerのhealthyを確認します。初回のbuildが失敗した場合は、proxy/CA/依存取得のどこで失敗したかを分けて確認します。

`verify` は隔離した合成データとnamespaceを使い、取り込み、型整理、隔離、不正値、集計、再実行を照合します。成功時はJSONの `ok: true` とchecksを確認します。会社DBの正しさやSambaクライアントの認証を検証するコマンドではありません。検証用のnamespaceと原本は通常終了・通常の失敗時に整理されます。

既定portの場合、サーバー自身からTrinoは `http://localhost:8080/`、Lakekeeperは `http://localhost:8181/ui/` へ接続できます。別端末の `localhost` はその端末自身です。遠隔利用は会社標準のSSHポート転送や、認証・HTTPSを備えた限定入口を使います。

## 8. 会社SQL Serverを読み取り専用でつなぐ

DB担当が対象表/ビューへのSELECT権限を持つログインを用意します。`site.env` の `SOURCE_SQLSERVER_*` に接続先、DB、読み取り用利用者、TLSのCAを記入します。タイムゾーンのない日時型を現地時刻で保存している場合は `SOURCE_SQLSERVER_TIMEZONE` も合わせます。

```bash
./scripts/lakehouse.sh source-check
```

これは名前解決とTCP到達確認までです。成功してもSQL認証、TLS、SELECT、CDCの成功とは扱いません。実際の抽出は専用のホストvenvを用意して行います。

```bash
uv venv --python 3.12 \
  "$HOME/.local/share/lakehouse-foundation/source-reader-venv"
uv pip sync \
  --python "$HOME/.local/share/lakehouse-foundation/source-reader-venv/bin/python" \
  requirements-demo.txt
install -m 0600 infra/source/sqlserver-tables.example.json \
  "$HOME/.config/lakehouse-foundation/sqlserver-tables.json"
```

コピーしたJSONをローカルで編集し、6表の会社列名を契約列へ一対一に対応付けます。SQL式や任意WHERE句を入れる形式ではありません。必要な変換や特殊な識別子は、DB担当が準備した読み取り用ビューで解決します。既存の私有対応表がある場合は上のcopyで上書きせず、そのファイルを継続して使います。

```bash
"$HOME/.local/share/lakehouse-foundation/source-reader-venv/bin/python" \
  scripts/extract-sqlserver.py \
  --env-file "$LAKEHOUSE_ENV_FILE" \
  --table-map "$HOME/.config/lakehouse-foundation/sqlserver-tables.json" \
  --output "$LANDING_PATH" --max-rows 100000
```

抽出はSELECTだけで、DB変更・CDC有効化・後続処理の起動をしません。6表全体の取得・形式検査に成功した後、6ファイル、各ready、`source-extract.json` を公開します。各SELECTの時点は別で、6表を同一時点へ固定するtransactionではありません。厳密な時点整合が必要なら、上流で読み取り用snapshot等を用意します。

初回は既存ファイルを上書きしません。次の全件snapshotを意図的に置き換える場合だけ `--force` を使います。抽出中にn8n/Airflow等から取り込みを同時起動せず、抽出成功→変換の順序にします。件数上限、TLS、NULL、日時の詳細は [SOURCE-SQLSERVER.md](SOURCE-SQLSERVER.md) を参照してください。DEMO設定は別の専用DBへの合成書き込みであり、ここでは使いません。

## 9. 会社データのメダリオンを検証する

```bash
./scripts/lakehouse.sh medallion
```

この操作でBronze取り込み→Silver→Quarantine→Goldを実行します。同じファイルhashならスキップし、内容更新時はそのファイル由来のBronze行を置き換えます。SQLは表単位のatomic replaceで、全Goldを一括transactionで入れ替える構成ではありません。

会社の受入では、抽出manifestの件数とBronze、Silver/Quarantineへの振り分け、Goldの数量・金額を、DB担当が用意した同じ範囲の集計と照合します。`verify` の合成期待値を会社データの期待値として流用しません。

TrinoのCLIで読み取り確認する例です。CLIは公式Trinoイメージに同梱されています。[Trino公式](https://trino.io/docs/current/installation/containers.html)

```bash
docker compose --env-file "$LAKEHOUSE_ENV_FILE" \
  -f infra/lakehouse/compose.yaml \
  exec -T trino trino --execute \
  'SELECT count(*) FROM iceberg.gold.factory_daily_production'
```

`iceberg.gold.sales_fulfillment`、`iceberg.quarantine.erp_products` 等も同様に確認します。件数や歩留まりの期待値は導入先の業務条件で決め、合成データの固定件数で受入しません。[DEMO-DATA.md](DEMO-DATA.md) とSQLのコメントを使って、日付・集計粒度・改訂優先の意味を確認します。

## 10. 操作画面、n8n、監視、Airflowを順に追加する

### 設定と素材投入の画面を使う

private envに `demo` profileを追加し、設定ファイルの親dirが専用0700、ファイルが0600、`CONTROL_SETTINGS_DIR` / `CONTROL_SETTINGS_NAME` がその場所を示すことを確認して `plan` → `up` を実行します。既定では操作画面が `http://localhost:8501/`、Swaggerが `http://localhost:8502/docs` です。画面のサーバーはprivate tokenでAPIへ接続するため、通常利用者が毎回tokenを入力する必要はありません。

設定画面では秘密値を読み戻さず、空の秘密欄は現在値を保持します。保存してもDocker、Samba、SQL Server、企業proxyの設定が自動変更されることはありません。反映待ちの表示がある間はAPI起動時の旧設定が使われるため、管理者が同じprivate設定を確認して再配置します。DokployのEnvironmentへ転記した値とは自動同期しません。

素材画面からCSV/JSON/画像の1回投入または有限マイクロバッチを選べます。SQL書込みには別のDEMO設定と明示同意が必要です。素材投入だけでは取り込み/変換を起動せず、後続処理のボタンを別に操作します。取消は次の素材バッチから停止し、既に作った素材は保持します。画面の進捗とmanifestを確認して、処理中にボタンを繰り返さないようにします。[DEMO-GUI.md](DEMO-GUI.md)、[API.md](API.md)

画面自体に企業IdPや利用者別認可はまだないため、loopbackまたは認証を設けた会社標準の入口で使います。GoldのAI向け参照は固定表の上限付きread-only exportで、モデル呼出しや外部AI送信は行いません。[AI-READINESS.md](AI-READINESS.md)

### n8nで実行する

private envを `COMPOSE_PROFILES=automation` に変更して `up` を再実行します。既定では `http://localhost:5678/` です。初回にownerアカウントを作り、`infra/automation/workflow-medallion.json` をインポートします。

HTTP Header Authの資格情報をGUIで作成し、ヘッダー `Authorization` にprivate設定の `WORKER_TOKEN` をBearer形式で登録します。HTTP Requestノードでその資格情報を選び、Manual runから1回実行します。固定の `http://worker:8000/run` を使い、tokenをworkflow JSONへ保存しません。画面の入力ファイル生成や任意SQLの編集機能までを、このワークフローに実装しているわけではありません。[GUI-OPTIONS.md](GUI-OPTIONS.md)

### 監視を追加する

private envを `COMPOSE_PROFILES=automation,monitoring` にして `up` を実行します。既定ではGrafanaが `http://localhost:3000/`、Prometheusが `http://localhost:9090/` です。CLIのupは初回ダッシュボードを一度だけ作成し、既存GUI編集は上書きしません。

Grafanaへ `admin` とprivate設定の現在の管理者パスワードでログインし、`Lakehouse Operations` の表示、手動ジョブ後の指標、GUI編集→Saveを確認します。Dokploy配置でCLIのupを通らない場合は `./scripts/lakehouse.sh grafana-init` を実行します。GUIのJSON exportと全体backupの違いは [GRAFANA.md](GRAFANA.md) を参照します。現在の指標だけで会社SQL Serverやホスト全体の監視が完成するとは扱いません。

### Airflowを追加する

資源を確認して `orchestration` をprofileへ追加し、同じ設定でupを実行します。初回は `http://localhost:8081/`、DAG `lakehouse_medallion` を手動で使います。Connection `lakehouse_worker` をHTTP、Host `http://worker`、Port `8000`、Passwordを `WORKER_TOKEN` にして設定します。

Simple Authの初期画面パスワードはstate volume内で生成され、DBパスワードとは別です。取得方法とProvider/constraintsは [COMPATIBILITY.md](COMPATIBILITY.md#airflowの起動と接続設定) に従います。standaloneの起動ログには初期資格情報が含まれ得るため、ログ全体を公開しません。

n8nとAirflowのどちらが定期実行を担当するかを決めます。workerのlockは同時実行を防ぎますが、二重にscheduleを作る理由にはしません。現DAGは自動scheduleを設定していません。

### Sparkを常駐させ、Jupyterから使う

private envの既存profileに `compute` を追加して `plan` → `up` を実行すると、Master/Worker/Historyが常駐します。`notebooks` も追加するとJupyterが内部の `spark://spark-master:7077` を使い、遠隔worker上で処理を実行します。Master UIは既定8082、Worker UIは8085、History UIは18080です。RPC7077はhostへ公開しません。

Notebook helperはcompute有効時のremote接続を明示し、接続失敗を黙ってlocalへ切り替えません。computeを使わないNotebookはlocal[2]です。event logとworker work用の専用volume、非root UID/GIDを保持し、再起動後の実行履歴も確認します。[COMPATIBILITY.md](COMPATIBILITY.md) と [POC-TOOLS.md](POC-TOOLS.md) にGUI・接続・資源設定を説明します。

core初期化後、独立した互換性スモークを確認する場合は次を実行します。

```bash
./scripts/lakehouse.sh spark-check
```

専用 `compatibility.spark_roundtrip` 表の固定3行をSparkで書き、SparkとTrinoで読みます。業務表の処理や分散Sparkクラスタを構築するコマンドではありません。

### CDCを追加する場合

全件SELECTの抽出が受入できてから、継続変更を読む必要性とDBAの許可を確認します。SQL Server側のCDC/Agent、有効化対象、初回snapshotと変更履歴の保持時間、DB負荷、6表の対応を決めます。`CDC_SQLSERVER_*` はSOURCE/DEMOから独立した資格情報です。6表のmappingは専用 `CDC_CONFIG_DIR`、Java truststoreは単一の `CDC_SQLSERVER_TRUSTSTORE_FILE` を指定します。Connectへはそのファイルだけを `/run/cdc/sqlserver-truststore.p12` へmountし、通常のhost PEM pathとは区別します。

Kafka、Debezium、consumer、Kafbat UIの任意構成を用意していますが、会社DBに対する実通信の成否は導入先で確認します。connector、topic、offset、raw変更イベント、現在状態、Goldの照合を段階ごとに記録します。[CDC.md](CDC.md) と [CONFIGURATION.md](CONFIGURATION.md#sql-serverの変更を読むcdc設定) の手順・設定を確認し、設定欄の保存や画面の起動だけでCDCの受入完了とはしません。

### PoCツールとAI向け入口を追加する

必要なprofileを現在のリストへ追加します。`bi` はSuperset、`notebooks` はSpark付きJupyter、`vector` はQdrant、`lineage` はMarquez、`mcp` はAIクライアント用です。各ポート・個別秘密値・外部 `NOTEBOOKS_DIR` を準備し、`plan` → `up` →GUI/APIの確認の順に進めます。全機能を一度に有効にする前にメモリとディスクを確認してください。

既存設定へ追加する場合は、profile変更の前に `python3 scripts/configure.py --output "$LAKEHOUSE_ENV_FILE" --upgrade` を実行します。既存の暗号鍵やパスワードは保持し、変更前ファイルを0600で保存します。追加した内部サービス名を `NO_PROXY` と `no_proxy` にも追記し、DokployのEnvironmentへ反映します。

[POC-PORTAL.md](POC-PORTAL.md) が画面の入口とデータ契約・品質・ヘルプを、[POC-TOOLS.md](POC-TOOLS.md) が各OSSの初期化・認証・保存方法を説明します。[MCP.md](MCP.md) では専用Bearer認証、read tools、明示有効化した場合だけ使える書込みツールを説明します。[ANOMALY-SCENARIOS.md](ANOMALY-SCENARIOS.md) から異常の影響と選び方を確認できます。

社内proxy導入は [COMPANY-PROXY.md](COMPANY-PROXY.md) を順に確認し、daemon、Git取得、BuildKit、コンテナ実行、Java、uv、内部通信を分けて受入します。GUIの診断は固定の外部取得先だけを試し、daemonの設定変更やDocker再起動は行いません。

## 11. Dokployを管理基盤にする場合

coreのCompose起動とは別に、Dokployのprivate設定を埋めてplanを実行します。

```bash
bash infra/dokploy/install.sh --env-file "$LAKEHOUSE_ENV_FILE"
```

既存Swarm、port、アドレスpoolの確認後に、管理者が同じコマンドへ `--apply` を付けます。管理外の既存Swarmは強制再初期化しません。GUIのProject→Environment→Composeを用意し、公開Gitの確認したcommitから配置します。

CLIから起動済みの同じ環境をDokployへ渡す場合は、`appName`、`COMPOSE_PROJECT_NAME`、volume、秘密値を揃え、二重writerを作らない移行として扱います。新規導入と既存データ移行の詳細は [DOKPLOY.md](DOKPLOY.md) を参照してください。

## 12. 受入結果と引き継ぎを残す

| 確認項目 | 受入の根拠 |
| --- | --- |
| ホスト/設定 | commit、profile、設定検査成功、選んだportとvolumeの対応を私有記録へ保存 |
| proxy/CA | daemon pull、build、外部API、内部S3/カタログを別々に確認 |
| core | healthy、隔離verifyの成功、再実行結果 |
| Samba使用時 | 許可クライアントの認証、ファイル作成・読込・削除、readyの扱い |
| 会社SQL | read-only認証/TLS、6表の列対応、抽出manifest、実行時間帯 |
| 業務変換 | Silver/Quarantine/Goldの会社側照合と、異常行の理由の確認 |
| GUI/監視使用時 | 認証、手動実行、失敗表示、Grafana GUI保存の確認 |
| 継続運用 | private env/暗号鍵/volumeのbackup先、復元試験、担当と停止・再開手順 |

閉域検証の受入と、本番の運用承認を分けて記録します。復元試験が未実施ならそのまま未実施とし、RPO/RTOや無停止性を確定しません。公開の [VALIDATION.md](VALIDATION.md) は実装検証の参考で、会社環境の証跡を代替しません。

引き継ぐものは、Git commit、私有設定の保管先、秘密値を含まない接続図、volume一覧、担当者が確認した受入結果です。private env、会社の表名対応、業務データ、ログの生出力は公開Gitへ戻しません。

## 途中で失敗したとき

| 症状 | 次に確認するもの |
| --- | --- |
| `plan` が秘密ファイル/port/keyで拒否 | [CONFIGURATION.md](CONFIGURATION.md)。0600、リポジトリ外、追加キー、port重複 |
| image pullだけ失敗 | daemonのproxy/CA/レジストリ許可。[PROXY.md](PROXY.md) |
| build中のpip/JAR取得だけ失敗 | BuildKit args、実際の取得先、CAとchecksum。版を外して回避しない |
| workerがlandingを読めない | `LANDING_DIR` の実体、UID/GID、親の通過権限、ready/hash、契約列 |
| SQLのTCPだけ成功して抽出失敗 | read-onlyログイン、TLSのCA/ホスト名、全6表のmapping、件数上限 |
| HTTP 409でworkerがblocked | Trinoで残存queryを確認する。取消不能の状態なので盲目的にジョブを再試行せず、確認後にworkerを再起動する |
| データが空に見える | project/appName/volume prefix/bucket/Warehouseの対応。volume削除や再投入より先に確認 |
| 画面は起動したが業務数値が違う | 日時、主キー、更新優先、NULL、表間snapshot時点、集計範囲を照合 |

停止は `./scripts/lakehouse.sh down` で、名前付きvolumeは保持します。障害調査中にvolume削除やpruneを復旧手順として実行しません。通常のログ・停止・再開の具体操作は [DEPLOYMENT.md](DEPLOYMENT.md) を参照してください。


SSRFの設定値、内部workerの例外、NO_PROXYとの違い、GUI保存と再配置、許可・拒否の受入は [SSRF.md](SSRF.md) を参照してください。

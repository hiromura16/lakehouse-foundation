# 導入・検証・停止

<!-- doc-purpose:start -->
> **役割:** private設定、host構成、CLIによる起動・検証・停止の操作を確認する。
>
> **対象読者:** 基盤構築担当 / 運用担当
>
> **読むタイミング:** 管理方法を決めて配置するときや、CLI管理の構成を起動・停止するとき。
>
> **前提となる資料:** [初めて構築する人の一本道ガイド](<START-HERE.md>) / [Linuxサーバーを最初に準備する](<HOST-SETUP.md>)
>
> **次に読む資料:** [Dokploy 0.31.0でGitから配置する](<DOKPLOY.md>) / [サイト設定リファレンス](<CONFIGURATION.md>) / [平時・更新・復旧の運用手順](<OPERATIONS.md>)
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

公開リポジトリ `lakehouse-foundation` を取得し、実際の環境設定だけをチェックアウトの外へ置いて導入します。clone URLはREADMEを参照してください。この文書の `/absolute/private/site.env`、共有利用者、許可範囲は説明用であり、導入先で選んだ値に置き換えます。

監査日: 2026-11-10。先に配置の所有者を一つ決めます。CLI管理を選んだ場合は本書の `up/down` を使い、Dokploy管理を選んだ場合は既存の同じComposeアプリから起動・停止します。どちらでも `site.env` と配置commit・profile・実volume名を一致させます。Dokploy導入そのものと、そこへこのスタックを配置する作業は別です。

| 作業 | 実施するホスト／利用者 | 書き込み先と確認結果 |
| --- | --- | --- |
| clone、設定生成、plan | レイクハウスホスト／通常の配置利用者 | repo外のprivate file0600。plan終了コード0 |
| CLI起動、状態確認 | 同ホスト／Docker操作権を持つ配置利用者 | 同じCompose project。healthとbootstrap成功 |
| Samba・daemon・Dokploy新設 | 同ホスト／管理者、明示したsudo操作 | 変更対象とbackupを確認してからapply |
| SQL権限・CDC有効化 | SQL Server／DBAが承認したSQL管理ツール | 対象DB/6表/権限/Agent/保持期間の確認記録 |
| 画面・SMB受入 | 許可した利用者端末 | 認証、到達範囲、ファイル往復、処理結果 |

## 前提条件

- Linux、Bash、Git、ホスト用Python3.12とuv、Docker Engine、Docker Composeプラグイン。コンテナ内Pythonは各imageの固定版を使います。
- 今回の互換性監査対象は **Docker Engine29系／Compose5.6.1**。inline config自体は2.23.1以上の機能ですが、その最低版で全構成を試験したという意味ではありません。
- 実行ユーザーがDockerを操作できること。Dockerを操作できる権限はホスト管理権限として扱います。
- 使用するイメージを取得できる通信、または社内レジストリへのミラー。
- coreと任意のプロファイルを動かすRAM・CPU・永続ディスク、既存サービスと競合しないポート。

SambaとDokployのホスト設定には別途管理権限が必要です。既存のDocker/Swarm、共有、Webポートがある場合は、その状態を確認してから該当手順を実行します。coreの起動だけでSambaやDokployのホスト設定は変更されません。

**Bash / Zsh共通**

```bash
docker version
docker compose version
python3 --version
```

終了コードと表示された版を導入記録へ残します。Docker管理権限はroot相当なので、権限のない利用者がsocketのmodeを緩めて解決しません。[Dockerの権限説明](https://docs.docker.com/engine/install/linux-postinstall/)、[Compose5.6.1公式release](https://github.com/docker/compose/releases/tag/v5.6.1)、[inline configs](https://docs.docker.com/reference/compose-file/configs/)

全コマンドは特記なければレイクハウスホストのclone済みリポジトリのルートで実施します。`sudo python3 scripts/configure.py ...` で生成するとUID/GIDもrootになるため、通常は配置利用者で生成します。コンテナへ任意にsudoやDocker socketを追加しません。

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

**Bash / Zsh共通**

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

**Bash / Zsh共通**

```bash
./scripts/lakehouse.sh --env-file /absolute/private/another-site.env plan
```

どちらの方法でも秘密ファイルはGitの外に保持します。環境変数へ設定するのはファイルの場所であり、パスワードの内容をシェルへ貼り付ける必要はありません。

## 1. 非公開のサイト設定を作る

cloneしたリポジトリのルートで実行します。生成先は自分で選べます。`LAKEHOUSE_ENV_FILE` を使う場合は、その変数で指定した場所と同じ場所へ生成します。

**Bash / Zsh共通**

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
| `COMPOSE_PROFILES` | 初期は空。`automation,monitoring,compute,orchestration,demo,cdc,bi,notebooks,vector,lineage,mcp` から必要なものをカンマ区切り |
| `S3_BUCKET_RAW` | `lakehouse-raw` |
| `S3_BUCKET_WAREHOUSE` | `lakehouse-warehouse` |
| `WAREHOUSE_NAME` | `lakehouse` |
| S3・PostgreSQL・暗号鍵等 | 生成値を保持。導入後に再生成しない |
| proxy設定 | 直接接続ではHTTP(S)4項目を空欄。`LAKEHOUSE_NO_PROXY` と大小文字の互換値は保持。会社追加分はprivateへ追記しupgrade |
| `SOURCE_SQLSERVER_*` | 外部の会社SQL Serverを使うときだけ設定 |
| `DEMO_SQLSERVER_*` | 疑似データをSQLへ投入するときだけ設定。SOURCEとは別の専用DB |

同じ `COMPOSE_PROJECT_NAME` と同じ設定ファイルを継続して使います。既存ボリュームを使う状態でDBパスワードや暗号鍵だけを変更すると、接続や復号に失敗します。設定ファイルはデータと合わせて安全にバックアップしてください。

`DATA_VOLUME_PREFIX` が空欄または未設定なら、名前付きvolumeは `<Composeプロジェクト名>_<volumeキー>` となります。定義はcore7個、compute2個、Airflow2個、CDC2個、PoC3個の計16個で、実際に使うものは有効profileによります。[全キー一覧](CONFIGURATION.md#data_volume_prefixを使う場面) から実体を確認します。ランディングやprivate設定等のbind mountはこの数に含めません。指定するとデータの物理名だけがその接頭辞へ変わります。コンテナ・ネットワークの識別子である `COMPOSE_PROJECT_NAME` やDokployの `appName` と区別し、既存volume名を維持する移行専用として扱います。値は空欄、または先頭が小文字英字で、小文字英数字・ハイフン・アンダースコアからなる41文字以内です。

volumeの自動rename・コピーは行いません。移行前に使用する全volumeの解決名と既存の実名を照合し、CDC・compute・PoC等の追加分も含めます。旧writerを停止してから一つの配置へ切り替えます。別の環境と同じ接頭辞を共有せず、移行後も指定を保持します。詳細は [Dokployで既存volumeを引き継ぐ手順](DOKPLOY.md#既存volume名を引き継ぐ場合) を参照します。

`BIND_IP=127.0.0.1` のままでは、画面をサーバー自身のブラウザーかSSHポート転送から利用します。別端末の `localhost` は別端末自身を指します。LAN等のアドレスへ変更する場合、ネットワーク制限と利用者認証を先に検討します。標準coreは単一ホストでの検証向けです。

### 既存site.envを更新する場合

新規生成コマンドは既存ファイルを上書きしません。既存サイトには次を使います。

**Bash / Zsh共通**

```bash
python3 scripts/configure.py --output /absolute/private/site.env --upgrade
./scripts/lakehouse.sh --env-file /absolute/private/site.env plan
```

不足項目を追加し、内部必須名とprivateのcanonical/大小文字のbypassをマージします。既存秘密値・profileは維持します。変更前は隣の `.before-upgrade` に0600で保存し、同名backupを上書きしません。前回の控えがある場合は内容を確認してprivate領域の別名へ保管してから更新します。変更が不要なら何も変更しません。[既存VM更新](COMPANY-UPDATE.md)

## 2. 会社のプロキシを設定する

privateのHTTP(S)4変数と `LAKEHOUSE_NO_PROXY` / `NO_PROXY` / `no_proxy` を使います。bypassは生成・upgradeで3項目を同期し、内部必須名を保持します。Dokploy管理ではupgrade後のcanonicalをEnvironmentにも登録します。古いshell変数がraw Composeの `--env-file` より優先するため、正規化済み設定を使います。

コンテナのenvだけではDocker daemonのimage pullに適用されません。会社でpull用プロキシが必要なら、[PROXY.md](PROXY.md) の独立キットでdaemon用設定を用意します。設定の配置とDocker再起動は別操作です。既存コンテナへの影響を確認してから反映します。

**Bash / Zsh共通**

```bash
./scripts/configure-proxy.sh --init /absolute/private/proxy.env
```

ローカルで編集した後に確認します。

**Bash / Zsh共通**

```bash
./scripts/configure-proxy.sh --env-file /absolute/private/proxy.env --plan
sudo ./scripts/configure-proxy.sh --env-file /absolute/private/proxy.env --apply
```

`--apply` はdaemonを再起動しません。企業CA、認証方式、Javaや各SDKの対応は [PROXY.md](PROXY.md) の確認項目に従います。会社のproxyでの接続は、その実環境で検証してから成功と記録します。

## 3. coreを起動する

`LANDING_DIR` は先に作成し、共有利用者が読み書きできる所有者・権限を設定します。後述のSamba手順で作成するか、管理者が専用ディレクトリを用意します。既定の `/srv/lakehouse/landing` を使う例は次のとおりです。利用者・グループを環境に合わせて置き換えます。

**Bash / Zsh共通**

```bash
SHARE_USER="$(id -un)"
SHARE_GROUP="$(id -gn)"
# 新規の専用pathにだけ実施。既存pathは先に所有者とACLを確認する。
LANDING_PATH=/srv/lakehouse/landing
if [ -e "$LANDING_PATH" ] || [ -L "$LANDING_PATH" ]; then
  stat -c '%a %u:%g %n' "$LANDING_PATH"
else
  sudo install -d -m 0770 -o "$SHARE_USER" -g "$SHARE_GROUP" "$LANDING_PATH"
fi
```

起動前の設定確認はホストを変更しません。

**Bash / Zsh共通**

```bash
./scripts/lakehouse.sh --env-file /absolute/private/site.env plan
```

**Bash / Zsh共通**

```bash
./scripts/lakehouse.sh --env-file /absolute/private/site.env up
```

この操作はSeaweedFS、PostgreSQL、Lakekeeperのマイグレーションとサーバー、Trino、worker、および選択profileを `up --build --wait` で起動し、その後workerのbootstrapでS3バケットとWarehouseを準備します。初期化状態を確認し、毎回新しいWarehouseを重複作成しない構成です。

初回はimage取得と起動に時間がかかります。正常時はコマンドが終了コード0になり、`status` で常駐サービスのhealthを確認できます。migration/init/register等の一時サービスは正常終了ならExited(0)で構いません。失敗時は [TROUBLESHOOTING.md](TROUBLESHOOTING.md) で原因を修正して同じ設定で再実行します。初期化途中のDBやボリュームを削除してやり直す必要があると即断しません。

Lakekeeperは初期化時に利用条件への同意を記録するAPIを使います。採用ソフトウェアの利用条件は [ARCHITECTURE.md](ARCHITECTURE.md) と上流資料を確認してください。

## 4. coreの往復試験を行う

**Bash / Zsh共通**

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

**Bash / Zsh共通**

```bash
python3 scripts/generate-demo.py \
  --destination landing --output /srv/lakehouse/landing \
  --seed 42 --orders 20 --days 7 --scenario edge
```

生成器の仕事は素材の生成と投入までです。Iceberg・Trino・メダリオン処理は呼び出しません。`landing` の出力は6ソース、ファイルごとの `.ready` 完了マーカー、件数・ハッシュ等のmanifestです。変換結果の期待値は生成器から独立した `medallion/expected-demo.json` に保持します。詳細な列・型・注入する異常・指標は [DEMO-DATA.md](DEMO-DATA.md) を参照します。

SQLにも素材を投入したい場合は、private envに `DEMO_SQLSERVER_HOST` / `PORT` / `DATABASE` / `USER` / `PASSWORD` を設定します。業務参照用の `SOURCE_SQLSERVER_*` と別の専用DBを指定します。SQL用ドライバー等の前提は [DEMO-DATA.md](DEMO-DATA.md) を参照してください。

**Bash / Zsh共通**

```bash
UV_PYTHON_DOWNLOADS=never uv venv --python /usr/bin/python3.12 \
  "$HOME/.venvs/lakehouse-demo"
uv pip sync --python "$HOME/.venvs/lakehouse-demo/bin/python" \
  requirements-demo.txt
"$HOME/.venvs/lakehouse-demo/bin/python" scripts/generate-demo.py \
  --destination sqlserver --output /absolute/private/demo-source \
  --env-file /absolute/private/site.env --allow-sql-write \
  --seed 42 --orders 20 --days 7 --scenario edge
```

`both` はランディングとSQLへ投入します。SQLモードは `--allow-sql-write` が必要で、システムDBとSOURCEのDBを拒否します。毎回新しい `lh_demo_<random>` schemaを作り、6つのnvarchar staging表へ挿入します。既存schemaを再利用せず、DB作成・DROP・既存業務表の変更は行いません。SQL投入成功は後段のCDC・メダリオン成功を意味しません。

以下の取り込み・メダリオンは **別の操作** として実行します。このファイル経路の確認には `landing` か `both` を使います。SQLからは [読み取り専用extractor](SOURCE-SQLSERVER.md) と [CDC経路](CDC.md) も提供しています。extractorは表・列のmapping、CDCは専用の接続・対象表・有効化が必要で、素材生成だけでは開始しません。会社の実DBでの接続と受入は未検証です。

Bronzeへの取り込みだけを実行する場合:

**Bash / Zsh共通**

```bash
./scripts/lakehouse.sh --env-file /absolute/private/site.env ingest
```

Bronze取り込みに続けてSilver・隔離・Goldまで実行する場合:

**Bash / Zsh共通**

```bash
./scripts/lakehouse.sh --env-file /absolute/private/site.env medallion
```

Silverは主キーごとの最新候補を選び、その型・値・参照を検証して有効な候補だけを残します。不正な最新版から過去の正常版へは戻しません。無効候補は同名のソース別quarantine表へ理由付きで隔離します。Goldの `factory_daily_production` と `sales_fulfillment` で、製造数量・停止時間・歩留まり・計画達成率と、受注・出荷・残数・履行率を参照できます。OEEは算出しません。

業務変換は `medallion/01_silver.sql`、`02_quarantine.sql`、`03_gold.sql` を編集します。同じデータで再実行しても追記による二重計上をしないよう派生表を置き換えます。ただし複数表をまとめたトランザクションではないため、失敗した場合は処理結果を確認して再実行します。

自分でファイルをコピーした場合は、コピー完了後に完了マーカーを作成します。取り込みは原本のSHA-256と一致するマーカーが必要です。

**Bash / Zsh共通**

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

**Bash / Zsh共通**

```bash
./scripts/lakehouse.sh --env-file /absolute/private/site.env up
```

`automation` はn8nと専用DB、`monitoring` はGrafanaとPrometheusを追加します。n8nの初回利用者登録や、通知先・業務ワークフローの設定は別途行います。Grafanaの初期資格情報は生成されたprivate envを参照し、公開文書に貼り付けません。

n8nの提供テンプレートをimportし、手動トリガーからワーカーの `/run` へ `medallion` を依頼できます。n8nの資格情報画面でHeader Authを作り、`Authorization` に `Bearer <WORKER_TOKEN>` を設定します。実トークンはprivate envの値を使い、テンプレートへ保存しません。列のマッピングや型変換の規則はSQLを編集し、GUIでは開始条件や接続を設定します。

定期実行のスケジュール、業務アラート、モデル推論は必要に応じて追加します。実装済みの [CDC](CDC.md) は `cdc` profileで、[操作GUI/API](DEMO-GUI.md) と [AI向けcontext取得](AI-READINESS.md) は `demo` profileで利用します。接続先・権限・mapping等の準備と導入先での受入を行い、profileを有効にしただけで業務フローが完成したとは扱いません。

`compute` はSpark Master/Worker/Historyを常駐起動します。`spark-check` は別の一時的なlocalスモークで、通常upでは起動しません。cluster接続の受入はJupyter等から別途行います。Airflowは `orchestration` を有効化すると手動DAGを追加します。正確な版・JAR・Provider、追加のprivate鍵、GUIでのworker接続、実行手順は [COMPATIBILITY.md](COMPATIBILITY.md) を参照します。

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

**Bash / Zsh共通**

```bash
./scripts/lakehouse.sh --env-file /absolute/private/site.env source-check
```

`source-check` は外部DBへの読み取り専用の到達性確認です。TCP接続を確認するもので、SQL認証、SELECT、証明書検証、CDCの成功を確認するものではありません。SQLパスワードを渡してSQLを実行したり、外部DBの状態を変更したりしません。

読み取り専用extractorの表・列mappingは [sqlserver-tables.example.json](../infra/source/sqlserver-tables.example.json) を見本に、Git外のprivate設定として用意します。[SOURCE-SQLSERVER.md](SOURCE-SQLSERVER.md) の手順でSELECTとファイル出力を確認します。本物の資格情報やmappingを公開ファイルへ書き戻しません。連続取り込みには [CDC.md](CDC.md) の別手順を使い、暗号化と証明書の信頼は会社の構成に合わせて確認します。

coreの初期化ジョブは会社DBへテーブル作成・更新を行いません。CDCを開始する場合は、DB管理者と対象表、CDC有効化、権限、保持期間を別途決めます。TCP疎通、認証、SELECT、CDCの順に確認し、実機へ接続するまで外部DBは未検証と記録します。

## 8. Sambaのランディング共有を追加する

Sambaはホストの共有サービスへ別手順で設定します。`LANDING_DIR` と `--path` を同じ値にします。Sambaパッケージ、稼働する `smbd`、Unix利用者、Sambaアカウントが先に必要です。このキットは利用者パスワードを作成・変更しません。

ホスト管理者がUnix利用者、Sambaアカウント、既存共有、許可するクライアント範囲を確認します。`smbd --version` とOSパッケージ版を記録します。Samba本体の導入・アカウント作成は会社の標準手順で別途行います。既存利用者のパスワードを作り直す手順ではありません。

以下は同じUnix利用者の例です。`ALLOW_CLIENT_CIDR=127.0.0.1/32` はホスト内の試験専用なので、実共有時は承認された端末範囲へ置き換えます。`SHARE_USER` が既存Sambaアカウントであることを確認してから進めます。

**Bash / Zsh共通**

```bash
SHARE_USER="$(id -un)"
ALLOW_CLIENT_CIDR="127.0.0.1/32"
sudo systemctl is-active smbd.service
sudo testparm -s /etc/samba/smb.conf
```

最初は設定を書き換えないplanを実行します。Samba設定の読取に管理権限が必要な環境に合わせ、ここではsudoを使います。

**Bash / Zsh共通**

```bash
sudo python3 scripts/setup-samba.py \
  --user "$SHARE_USER" \
  --path /srv/lakehouse/landing \
  --allow "$ALLOW_CLIENT_CIDR" \
  --plan
```

内容を確認して適用します。

**Bash / Zsh共通**

```bash
sudo python3 scripts/setup-samba.py \
  --user "$SHARE_USER" \
  --path /srv/lakehouse/landing \
  --allow "$ALLOW_CLIENT_CIDR" \
  --apply
```

共有名の既定値は `Lakehouse-Landing` です。同名共有、管理外include、動的include等の競合があると停止します。対象は `/etc/samba/smb.conf.d/lakehouse-foundation.conf` と、`/etc/samba/smb.conf` の管理マーカー付きinclude blockです。適用前に `/var/backups/lakehouse-foundation/samba/<時刻-ID>/`（0700、内容0600）へ控えを保存し、`testparm` 検証と `smbd` reloadを行います。失敗時は元の設定を戻してreloadします。共有のアクセス許可は導入先のクライアントIP/CIDRを指定します。利用者パスワード、既存directory、他共有の所有権は変更しません。既存 `LANDING_DIR` の権限不一致はhelperが直さないため、管理者が対象pathだけを調整します。新規作成時はlandingと `csv/json/images/quarantine` を用意します。

apply成功後も `testparm` は構文確認までです。既存SMB接続をいったん切り、新規接続で共有へ認証して確認します。[Samba設定](https://www.samba.org/samba/docs/current/man-html/smb.conf.5.html)、[testparmの確認範囲](https://www.samba.org/samba/docs/current/man-html/testparm.1.html)

許可したWindows/Macクライアントから、CSV・JSON・画像のコピー、読戻し、ハッシュ一致を確認します。この共有設定はファイルの受け口です。コピー完了後に `.ready` を作成し、CLIまたはn8nから取り込み・メダリオン処理を実行します。共有ディレクトリを自動監視して毎回起動する設定は別途追加します。

## 9. Dokployを別に導入する

DokployはcoreのComposeとは別に導入します。提供するインストーラーはx86-64 Linux向けにイメージを固定し、稼働中のDockerを前提にします。`--apply` なしの既定動作は設定の検証とplanだけで、port空き・host所有権・既存Swarm等の本検査はrootのapplyに入ってから実施します。plan成功をhost全体の導入可否確認と扱いません。公式の導入手順は [Dokployのインストール](https://docs.dokploy.com/docs/core/installation) を参照します。

private envに `DOKPLOY_BIND_IP`、`DOKPLOY_ALLOWED_CIDRS`、`DOKPLOY_ADMIN_PORT`、`SWARM_ADVERTISE_ADDR`、`SWARM_ADDRESS_POOL`、`SWARM_SUBNET_SIZE` を設定します。必要なら `DOCKER_BRIDGE_CIDR` も指定します。アドレスは導入先のものを使い、Swarmのアドレスプールは会社・VPN・Dockerの既存ネットワークと重複しない範囲にします。このinstallerの管理画面は既定3100（上流の既定3000とは別）、TraefikはTCP80/443とUDP443を使用するため、既存のWebサービスと競合しないか確認します。

**Bash / Zsh共通**

```bash
bash infra/dokploy/install.sh --env-file /absolute/private/site.env
sudo bash infra/dokploy/install.sh --env-file /absolute/private/site.env --apply
```

管理画面を開いて最初の管理者アカウントを登録し、コンテナ・DBの正常性と画面へのアクセスを確認します。初回登録を完了するまで、管理画面への接続を意図した利用者のみに限定します。

既存のDokployがある場合は再利用を優先します。別のSwarmから強制離脱したり、既存のDB・ボリュームを初期化したりして導入しません。Dokployからcoreを管理する場合も、サイト固有envを非公開のEnvironment設定へ入れ、同じスタックをCLIとUIから同時に別名で起動しないようにします。

## 10. 停止・再開・復旧

**Bash / Zsh共通**

```bash
./scripts/lakehouse.sh --env-file /absolute/private/site.env down
```

`down` はスタックを停止し、データボリュームを保持します。coreの `down` だけでSambaやDokployを停止することはありません。再開は同じenvを使って `up`、続いて `verify` を実行します。

ボリューム削除、プロジェクト名の変更、データディレクトリの移動、暗号鍵の再生成は通常の再起動手順に含みません。バックアップからの復旧は、カタログDB、暗号鍵、S3内のIcebergと原本、任意のn8n DBと暗号鍵の整合を確認して別環境で実施します。

## 問題が起きた場合

| 症状 | 確認すること |
| --- | --- |
| Composeが `content` を解釈しない | plugin版を確認。採用手順のCompose5.6.1と相違がないか |
| imageを取得できない | Docker daemon側proxy、CA、認証、レジストリ到達性 |
| 内部接続がproxyへ流れる | private設定をupgradeしcanonical3項目を同期、Dokploy Environmentも更新して再配置 |
| PostgreSQLの認証失敗 | 初回作成時のパスワードと現在のenvが一致するか |
| Lakekeeperが起動しない | PostgreSQLの正常性、migrateの完了、暗号鍵の保持 |
| Warehouse初期化が失敗 | S3認証、バケット、内部endpoint、region、既存登録との一致 |
| TrinoがS3を使えない | `fs.s3.enabled`、S3鍵、endpoint、path-style設定 |
| 他端末から画面が開けない | BIND_IPがloopbackか、ポート・経路・アクセス制御が正しいか |
| SMB共有へ書けない | 既存Sambaアカウント、許可CIDR、Unix側のディレクトリ権限 |
| 外部SQL Serverへ接続できない | DNS、経路、ポート、TLS、認証、参照権限を個別に確認 |

詳細を共有する際は、envファイル、展開済みCompose、接続文字列、プロキシ認証情報、ホスト名・IPを含む生ログをそのまま公開しません。機能、エラーの種類、採用版、秘密を除いた再現手順を記録します。


Docker 29／Buildx Bake／社内proxyで判明した互換性修正の取り込みと、`LAKEHOUSE_NO_PROXY` の生成・Dokployへの登録は [会社VMの更新手順](COMPANY-UPDATE.md) を参照してください。


## 監査した版・公式資料

ホストEngine29系とCompose5.6.1を対象に、公開コードの挙動を照合しました。ホスト版の自動更新は行いません。実際に配置するimageは各Composeとlockのdigestが正本です。n8n2.46.7、Debezium3.7.1.Final、Kafka4.4.0の個別設定は [SSRF.md](SSRF.md) と [CDC.md](CDC.md) を参照します。

| 一次資料 | 用途 |
| --- | --- |
| [Docker29 release notes](https://docs.docker.com/engine/release-notes/29/)、[Compose5.6.1](https://github.com/docker/compose/releases/tag/v5.6.1) | 導入先の版確認 |
| [Swarmネットワーク](https://docs.docker.com/engine/swarm/networking/) | address poolは初期化時に決定。既存clusterを再初期化して直さない |
| [Dokploy0.31.0 source](https://github.com/Dokploy/dokploy/tree/v0.31.0)、[公式導入](https://docs.dokploy.com/docs/core/installation) | 提供installerとの違い・管理権限を確認 |
| [Samba公式](https://www.samba.org/samba/docs/current/man-html/smb.conf.5.html) | host既存版のman pageと共有設定を照合 |

## 初回の構築を段階で止めて確認する

### 用語を最初に結び付ける

| 用語 | 本構成での意味 | 配置時に記録するもの |
| --- | --- | --- |
| image | Dockerfileと固定依存から作る実行物 | tagだけでなくdigest/lockとGit commit |
| container | imageを設定・mount付きで起動した実体 | 同じprojectのservice名と状態 |
| named volume | DB/S3/GUI等の永続データ | 解決された実volume名と暗号鍵の組 |
| bind mount | hostの既存dir/fileをcontainerへ渡すもの | landing/private/workspaceの実体と所有者 |
| one-shot | migration/init/register等、一度の処理で終了するservice | 終了コード0。runningである必要はない |
| profile | coreに追加する機能の選択 | 既存の選択一覧と追加先の必要条件 |

「Gitから取得できた」「Composeが解釈できた」「imageがbuildできた」「healthyになった」「業務結果が合った」を一つの成功にまとめません。失敗した段階を特定すると、不要なDB初期化を避けられます。

| 段階 | 操作・観測 | 次へ進む条件 |
| --- | --- | --- |
| 取得 | cloneしたcommit、lockと必要fileを確認 | 取得漏れやローカル一時修正の混入がない |
| host/private | Docker、proxy、path、所有者、空port | 通常配置利用者が必要な操作を行える |
| 構成 | `lakehouse.sh plan` | 終了0。有効profileと永続先の対応を確認済み |
| build/pull | 同じ管理入口から配置 | 依存checksumとimage作成が成功 |
| 起動/初期化 | DB→migration→catalog→worker | one-shot0、常駐serviceのhealthが正常 |
| 内部往復 | `lakehouse.sh verify` | 合成の11検査、同じ原本の再投入skip |
| 業務/UI | 素材→明示変換→Gold、各native GUI | 期待した粒度・保存先・認証・保存が正しい |

Dokployルートでは、新規のDokploy導入を済ませてからGitサービスをDeployします。本書の章番号は個別作業を調べる参照順であり、先にCLIでcoreを起動してからDokployへ移すことを必須にしていません。[新規導入の一本道](START-HERE.md)

## 複数環境を安全に置く

| 対象 | 別環境で分けるもの | 共有しても環境分離にならないもの |
| --- | --- | --- |
| 実行名 | Dokployの生成後appName / CLI project名 | 画面上のdisplay nameだけ |
| 永続化 | 独立volume、空のDATA_VOLUME_PREFIXまたは別実名 | 同じprefixを使う二つのEnvironment |
| 入出力 | landing、Notebook workspace、private設定dir | 同じhost dirのbind |
| 認証 | DB鍵、token、暗号鍵、GUI管理者 | 一つの資格情報を別名で登録するだけ |
| 到達先 | 空portと必要なSSH転送 | 同じhostportを取り合うprofile設定 |

合成データをGUIで通常namespaceへ取り込んだ環境を、そのまま会社データの受入先にしません。会社側は新しい空の環境を用意し、isolated verify以外のダミー生成を避けます。landingからファイルを消しても既存Bronzeは消えません。

別のcloneへ移るだけなら、データの移行を伴うとは限りません。一方、別hostへの移動はvolume/bind/秘密値の転送と復元試験が必要です。同じappNameを入力してDeployするだけでは、元hostのデータが新hostへコピーされません。

## 資源を見積もる担当者へ

Composeの `mem_limit` は設定上限で、実測RSSや必要host RAMの保証ではありません。全profileの常駐service上限は、公開構成の集計で約30.56GiBです。one-shot、build、OS、Docker/Dokploy、file cacheやbackupの余力は別に考えます。profile別の値と共有serviceの重複は [実装台帳](reports/IMPLEMENTATION-INVENTORY.md#9-資源上限の見積もりに使う値) を確認してください。

1. coreだけを受け入れ、実際の入力件数・fileサイズ・処理時間を記録します。
2. demo/monitoring、n8n等を一つずつ追加し、起動時ピークと平常時を分けます。
3. SparkはJupyter driverとWorker executorの両方を見ます。Workerのcontainer上限と広告するexecutor用メモリは別です。
4. ディスクは原本、Iceberg data/metadataと旧snapshot、DB、Kafka、GUI履歴、backupの増加を測ります。`down` はこれらを削除しません。
5. 小さいVMでは不要profileを増やさず、処理の同時実行も一つずつ確認します。資料に書いた上限をそのまま性能結果へ転記しません。

## 再配置が失敗した時の管理境界

| 観測 | 保持するもの | 次の操作 |
| --- | --- | --- |
| pull/buildで停止 | 起動済みserviceと既存volume | 通信/CA/lock/空容量を確認。既存DBを作り直さない |
| 新serviceの初期化で停止 | 正常終了済みのmigrationとDB | 対象serviceの失敗段階を確認。同じappで再試行 |
| health待ちがtimeout | 実container状態・ログ・資源 | 起動途中か異常終了かを区別。二重Deployを連打しない |
| APIだけ新token、clientは旧値 | DB/暗号鍵 | 正しい一組へ同期し、同じappへ反映 |
| DB schema更新後に起動失敗 | 更新前の整合backup | 単純な旧image起動を復旧と決めず、正式手順か別環境復元 |

自作imageのlocal tagは `COMPOSE_PROJECT_NAME` にも依存します。保守用のraw Composeでbuildする場合は、shellに残る別環境のproject/profile/port値まで混入しないよう、[運用の7Compose補助関数](OPERATIONS.md#postgresqlの論理dump例) を同じrepoルートで使います。設定を別の値へ展開した `config` 全文は秘密を含み得るため公開しません。

## 構成を拡張する開発者の受入

新serviceを加える場合、Composeのservice定義に加えてprofile、依存順、health、one-shotか常駐か、資源上限、log保持、volume/bind、実行UID、port公開範囲、proxy/CA、設定検査を決めます。Docker socketをUIへ渡して便利さを作る前提にはしません。

新規/再配置、profile未選択/選択、既存volume/空volume、proxy/直接、CAあり/なしを分けて構文・起動・小さい業務往復を確認します。初期化が冪等でも、DB major更新や秘密鍵rotationの安全性まで保証するわけではありません。追加の永続データはbackup/restore一覧と構成図にも反映します。


Notebook workspaceのread-only計画と承認後のapply/probe、既存projectを明示したSpark検証、機械可読な結果は [compute配置前・検証手順](COMPUTE-VALIDATION.md) を参照します。通常Deployでspark-checkが常駐しないのは正常です。local tagのpull方針はFROM/uv/JAR通信や未解決のBuildKit auth session timeoutを修復する保証ではありません。

# サイト設定リファレンス

<!-- doc-purpose:start -->
> **役割:** 生成器・Compose・CLIが使う設定項目と、保存先・認証・反映先を照合する。
>
> **対象読者:** 基盤構築担当 / 運用担当 / 開発・設計担当
>
> **読むタイミング:** private設定を作成・変更し、何に使われる値かを調べるとき。
>
> **前提となる資料:** [初めて構築する人の一本道ガイド](<START-HERE.md>)
>
> **次に読む資料:** [プロキシ設定の場所と反映方法](<PROXY.md>) / [SSRF対策と内部接続の設定](<SSRF.md>) / [平時・更新・復旧の運用手順](<OPERATIONS.md>)
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

この文書は `scripts/configure.py` が生成する設定と、現在のCompose・各CLIが読む項目を説明します。監査日: 2026-11-10。会社への導入順序は [COMPANY-ROLLOUT.md](COMPANY-ROLLOUT.md)、通常の操作は [DEPLOYMENT.md](DEPLOYMENT.md) を参照してください。

## まず決めること

リポジトリの取得先、設定ファイル、ランディング、Dockerの永続データは別の場所です。clone先は任意で、スクリプトが自身の配置からリポジトリのルートを見つけます。会社側で特定の `apps` ディレクトリを作る必要はありません。

**Bash / Zsh共通**

```bash
python3 scripts/configure.py \
  --output "$HOME/.config/lakehouse-foundation/site.env"
export LAKEHOUSE_ENV_FILE="$HOME/.config/lakehouse-foundation/site.env"
./scripts/lakehouse.sh plan
```

生成は通常利用者で行います。`WORKER_UID` / `WORKER_GID` にその利用者の数値IDが入るため、同じ利用者がランディングを扱う場合に権限を揃えやすくなります。既存の設定ファイルは上書きせず、秘密値は表示しません。導入後にもう一度生成してパスワードや暗号鍵を入れ替える操作には使いません。

| 保存するもの | 場所と扱い |
| --- | --- |
| 公開コード・設定見本 | cloneしたリポジトリ |
| `site.env` | リポジトリ外。ファイル0600、親ディレクトリ0700 |
| 実際のSQL表・列の対応表 | リポジトリ外のJSON、0600 |
| 入力CSV/JSON/画像 | `LANDING_DIR` の専用ディレクトリ |
| S3・DB・GUIの保存内容 | Dockerの名前付きvolume |
| 企業CAとproxyの実値 | サイト内の非公開設定。Gitへ追加しない |

## 読み込み方と優先順位

`lakehouse.sh` は明示した `--env-file` を優先し、省略時だけ `LAKEHOUSE_ENV_FILE` を使います。環境変数に入れるのは**ファイルの場所**です。どちらも未指定なら引数エラーとなり、Dockerは起動しません。

**Bash / Zsh共通**

```bash
# シェルに設定した既定サイトを使う。
./scripts/lakehouse.sh status

# この1回だけ別のprivate設定を選ぶ。
./scripts/lakehouse.sh --env-file /absolute/private/another-site.env plan
```

`configure.py --output`、SQL抽出の `--env-file`、Dokploy導入の `--env-file` は別の引数です。`LAKEHOUSE_ENV_FILE` を設定しても、それらの明示引数が不要になるわけではありません。

設定は1行1項目の `KEY=value` です。値の中で `$HOME`、他の変数、`$(...)`、コマンドを展開しません。たとえばシェルの `export LAKEHOUSE_ENV_FILE="$HOME/..."` ではシェルがホームを展開しますが、`site.env` の `LANDING_DIR` には完成した絶対パスを記入します。引用符を付けない単一行ASCIIを共通形式にすると、core、SQL抽出、Dokployの各読み込み方法で扱いやすくなります。

`plan` は秘密値を展開表示しない `docker compose config --quiet` を使います。通常の `docker compose config` や `docker inspect` の出力には資格情報が現れ得るため、公開資料の設定一覧として使いません。

## 実行単位・パス・永続データ

「秘密」の「社内」は、パスワードではないものの、導入先の内部情報としてprivate設定に保持する項目です。

| 項目 | 生成時の既定 | 意味と変更時の影響 | 秘密 |
| --- | --- | --- | --- |
| `COMPOSE_PROJECT_NAME` | `lakehouse` | コンテナ、ネットワーク、ローカルimageタグの実行単位。小文字英字で始まる41文字以内の英数字・`_`・`-`。初回に固定し、変更は別配置として扱う | 社内 |
| `DATA_VOLUME_PREFIX` | 空 | 通常は空で、volume実名のprefixに `COMPOSE_PROJECT_NAME` を使う。既存volume名を維持して実行プロジェクト名だけ移す場合の専用設定。後述 | 社内 |
| `LANDING_DIR` | `/srv/lakehouse/landing` | ホスト上の専用絶対パス。workerへ `/landing` として読み取り専用mountする。変更時は新しいディレクトリ・Samba共有先・UID/GIDを揃え、対象を再配置する | 社内 |
| `WORKER_UID` | 生成利用者のUID | workerの実行ユーザーID。ランディングの読み取り権限に合わせる。ファイルの所有権を自動で直す設定ではない | 社内 |
| `WORKER_GID` | 生成利用者のGID | workerの実行グループID。親ディレクトリの通過権限とACLも確認する | 社内 |
| `COMPOSE_PROFILES` | 空 | coreに加える機能。`automation,monitoring,compute,orchestration,demo,cdc,bi,notebooks,vector,lineage,mcp` からカンマ区切りで選択。変更後は同じprivate設定で `up` を実行する | いいえ |

Dokployの `appName` はGUI側の項目であり、`site.env` に別の `APP_NAME` を作る仕組みではありません。Dokployは `-p <appName>` でComposeを実行するため、同じ環境をCLIでも操作する場合は `COMPOSE_PROJECT_NAME` をその `appName` に合わせます。画面のEnvironment名だけではデータは分離されません。[DOKPLOY.md](DOKPLOY.md)

### DATA_VOLUME_PREFIXを使う場面

通常の新規導入では空のままにします。定義は次の16キーで、有効profileにより実際に作る対象が変わります。実名は `<prefix>_<volumeキー>` です。

| Compose | volumeキー |
| --- | --- |
| lakehouse | `catalog-db,storage,trino,n8n-db,n8n,prometheus,grafana` |
| compute | `spark-events,spark-work` |
| orchestration | `airflow-db,airflow-state` |
| cdc | `cdc-kafka,cdc-state` |
| poc | `superset-db,qdrant,marquez-db` |

private設定、`LANDING_DIR`、`NOTEBOOKS_DIR`、truststore等のbind mountはこの数に含みません。空の場合のprefixは `COMPOSE_PROJECT_NAME` です。

移行時にだけ、元のvolume prefixを `DATA_VOLUME_PREFIX` に指定し、新しい実行名と既存の永続データを分けて扱えます。形式の制約は `COMPOSE_PROJECT_NAME` と同じです。この設定はデータのコピー、DBのアップグレード、バックアップの復元を行いません。

切り替える前に、既存volumeの実名とデータの用途、元のパスワード・暗号鍵、DBイメージの版を確認し、元のwriterを停止します。別の環境と同じprefixを設定すると同じDBやS3領域へ接続するため、developmentとproductionの分離には使いません。prefixを誤った場合は空のデータ領域が新しく作られることもあるので、再投入より先に名前の対応を確認します。

## 接続口

| 項目 | 生成時の既定 | 意味と変更時の影響 | 秘密 |
| --- | --- | --- | --- |
| `BIND_IP` | `127.0.0.1` | ホストへ公開する各portのbind先。初期はサーバー自身からのみ利用する。特定のlocal/private IPv4を指定でき、変更時は認証・HTTPS・ネットワーク制限と接続URLを見直す | 社内 |
| `TRINO_PORT` | `8080` | TrinoのWeb/SQL入口。Web UIは主にクエリー状況の確認用で、SQL実行にはCLI/JDBC等を使う | 社内 |
| `CATALOG_PORT` | `8181` | LakekeeperのUI/REST/管理API入口 | 社内 |
| `S3_PORT` | `8333` | SeaweedFSのS3 API入口。署名付きのAPIで、匿名のファイル一覧画面ではない | 社内 |
| `GRAFANA_PORT` | `3000` | `monitoring` のGrafana入口。既存Webサービスとの競合を確認する | 社内 |
| `PROMETHEUS_PORT` | `9090` | `monitoring` のPrometheus入口 | 社内 |
| `N8N_PORT` | `5678` | `automation` のn8n入口。n8nコンテナ内部は5678を使う | 社内 |
| `AIRFLOW_PORT` | `8081` | `orchestration` のAirflow入口。Airflowコンテナ内部は8080を使う | 社内 |
| `DEMO_GUI_PORT` | `8501` | `demo` の操作画面。画面自体に利用者別認証はないためloopback/限定入口で使う | 社内 |
| `DEMO_API_PORT` | `8502` | `demo` または `mcp` のFastAPI/Swagger。業務操作はCONTROL_API_TOKENによるBearer認証 | 社内 |
| `SPARK_MASTER_UI_PORT` | `8082` | `compute` のMaster UI。RPC portを変える設定ではない | 社内 |
| `SPARK_WORKER_UI_PORT` | `8085` | `compute` のWorker UI | 社内 |
| `SPARK_HISTORY_UI_PORT` | `18080` | `compute` のHistory UI。event log volumeと対で利用 | 社内 |

host portは1024〜65535から選びます。coreと**有効profileで公開するhost port同士**の重複を拒否し、無効profileのportは数値範囲だけを検査します。例としてcomputeが無効なら `TRINO_PORT=8082` を保持できますが、computeを有効化すると既定のSpark Master UIと競合します。未使用の設定行や秘密値を削除する必要はありません。

ここで変えるのはホスト側のportです。Docker内部の `http://worker:8000`、`http://trino:8080`、`http://catalog:8181`、`http://storage:8333` は別のアドレス体系です。n8n/Airflowからworkerへ接続する設定を、ホスト側の `localhost` に書き換えません。

## S3・カタログ・worker

| 項目 | 生成時の既定 | 意味と変更時の影響 | 秘密 |
| --- | --- | --- | --- |
| `WAREHOUSE_NAME` | `lakehouse` | LakekeeperのWarehouse識別名。既存Warehouseと保存先の対応を保持する。名前変更だけで既存表は移らない | 社内 |
| `S3_BUCKET_RAW` | `lakehouse-raw` | 原本をハッシュ別に保存するbucket。変更しても既存原本は自動移動しない | 社内 |
| `S3_BUCKET_WAREHOUSE` | `lakehouse-warehouse` | IcebergのParquet/metadataを保存するbucket。既存カタログと一緒に扱う | 社内 |
| `S3_ACCESS_KEY` | 32文字のhexを自動生成 | このスタックのS3利用者の鍵ID。保存先と全クライアントを揃えて更新する | はい |
| `S3_SECRET_KEY` | 64文字のhexを自動生成 | S3署名用の秘密鍵。S3側とworker/Trino/Sparkを整合させる | はい |
| `CATALOG_DB_PASSWORD` | 64文字のhexを自動生成 | Lakekeeper用PostgreSQLのパスワード。既存DBはenv変更だけでは改訂されない | はい |
| `CATALOG_ENCRYPTION_KEY` | 64文字のhexを自動生成 | カタログが保存する資格情報の暗号鍵。DBと一緒にバックアップし、既存データ利用中に再生成しない | はい |
| `WORKER_TOKEN` | 64文字のhexを自動生成 | 固定ジョブAPI `/run` のBearer認証。変更時はn8n/Airflowに保存した資格情報も更新する | はい |

Warehouse/bucket名は、現在の検査では小文字英字で始まる3〜51文字の小文字英数字・ハイフンを使います。接続先のS3 endpointは現在のcore実装ではDocker内部の `storage` であり、任意の外部S3 endpointをこの一覧のenvだけで切り替える実装ではありません。Cloudflare R2等へ移す場合は、接続・権限・費用・実データの移動を別途設計します。

## n8nとGrafana

| 項目 | 生成時の既定 | 意味と変更時の影響 | 秘密 |
| --- | --- | --- | --- |
| `N8N_DB_PASSWORD` | 64文字のhexを自動生成 | 専用PostgreSQLへの接続。既存DBパスワードはenv変更だけで更新されない | はい |
| `N8N_ENCRYPTION_KEY` | 64文字のhexを自動生成 | n8nの保存資格情報を復号する鍵。n8n DBとセットで保持する | はい |
| `N8N_HOST` | `localhost` | n8nが認識するホスト名。他端末へ提供する場合は実際の入口に合わせる | 社内 |
| `N8N_PROTOCOL` | `http` | n8nのURL方式。入口をHTTPS化した場合はproxy/URL設定も整合させる | いいえ |
| `N8N_SECURE_COOKIE` | `false` | loopback HTTPでの初期確認用。HTTPSで共有する構成へ移る際はcookie設定も見直す | いいえ |
| `GRAFANA_ADMIN_PASSWORD` | 48文字のhexを自動生成 | 初期管理者 `admin` の設定。GUIで変更した場合はprivate設定も現在の値に合わせる。既存Grafana DBのパスワードをenvだけでリセットする機能ではない | はい |

n8nのownerアカウントやAirflowの画面ログインを、この表のDBパスワードと取り違えないようにします。Grafanaのダッシュボードは初回作成後、GUIで保存した内容を正本として扱います。[GRAFANA.md](GRAFANA.md)

## Airflow

| 項目 | 生成時の既定 | 意味と変更時の影響 | 秘密 |
| --- | --- | --- | --- |
| `AIRFLOW_DB_PASSWORD` | 64文字のhexを自動生成 | Airflow metadata DBへの接続。DBとAirflowを整合させて更新する | はい |
| `AIRFLOW_FERNET_KEY` | 32バイト乱数のURL-safe Base64 | Connection等の保存内容を暗号化する鍵。通常44文字で、hexの秘密値とは別形式。鍵のローテーションはAirflow側の手順で行う | はい |
| `AIRFLOW_JWT_SECRET` | 64文字のhexを自動生成 | AirflowのAPI認証用secret。変更時はAirflow再配置と既存token等への影響を確認する | はい |

画面の初期ログイン情報はSimple Auth Managerがstate volume内に生成します。`AIRFLOW_DB_PASSWORD` は画面のパスワードではありません。現実装は `airflow standalone` の検証構成で、企業IdPや分散workerを備えた本番構成とは別です。[COMPATIBILITY.md](COMPATIBILITY.md#airflowの起動と接続設定)

## 会社SQL Serverを読むSOURCE設定

| 項目 | 生成時の既定 | 意味と変更時の影響 | 秘密 |
| --- | --- | --- | --- |
| `SOURCE_SQLSERVER_HOST` | 空 | 既存の会社DBのDNS名またはIPv4。ソース抽出時は必須 | 社内 |
| `SOURCE_SQLSERVER_PORT` | `1433` | 会社DBのTCP port。HTTP proxyのportではない | 社内 |
| `SOURCE_SQLSERVER_DATABASE` | 空 | 読み取る業務DB。system DBは選択できない | 社内 |
| `SOURCE_SQLSERVER_USER` | 空 | 必要な表/ビューのSELECTだけを許可した専用ログイン | 社内 |
| `SOURCE_SQLSERVER_PASSWORD` | 空 | そのログインのパスワード。workerコンテナへ一括配布せず、ホスト上の抽出ツールが読む | はい |
| `SOURCE_SQLSERVER_CA_FILE` | 空（抽出時は `system`） | ホストの信頼ストア、または企業CAのPEMファイル。TLS暗号化と証明書ホスト名検証を行う | 社内 |
| `SOURCE_SQLSERVER_TIMEZONE` | `UTC` | タイムゾーン情報のないSQL日時型の解釈。実際の保存方式に合わせる。文字列日時の形式を自動修正する設定ではない | いいえ |

`source-check` はTCP到達確認だけです。実際のSQL認証・SELECT・TLS・6表の列対応は `extract-sqlserver.py` で確認します。`SOURCE_SQLSERVER_*` を設定してcoreを起動しただけでは、業務データの抽出やCDCは開始しません。[SOURCE-SQLSERVER.md](SOURCE-SQLSERVER.md)

## 合成データを専用SQLへ書くDEMO設定

| 項目 | 生成時の既定 | 意味と変更時の影響 | 秘密 |
| --- | --- | --- | --- |
| `DEMO_SQLSERVER_HOST` | 空 | 合成データの投入先。会社SOURCEとは別の専用テストDBを指定する | 社内 |
| `DEMO_SQLSERVER_PORT` | `1433` | テストDBのTCP port | 社内 |
| `DEMO_SQLSERVER_DATABASE` | 空 | 作成済みの専用DB。system DBおよびSOURCEと同名のDBは拒否する | 社内 |
| `DEMO_SQLSERVER_USER` | 空 | 専用DBに新規スキーマと表を作成しINSERTできるテスト用ログイン | 社内 |
| `DEMO_SQLSERVER_PASSWORD` | 空 | そのテスト用ログインのパスワード | はい |
| `DEMO_SQLSERVER_CA_FILE` | 空（実行時は `system`） | テストDBのTLS検証に使うホスト側PEMまたはsystem trust | 社内 |
| `DEMO_SQLSERVER_HOSTNAME_IN_CERTIFICATE` | 空 | 接続先alias/IPと証明書名が異なる場合に、CAで検証した証明書へ照合する明示名。英数字・`_`・`.`・`-` の1〜253文字。空なら接続hostを照合し、TLS検証は維持する | 社内 |

SOURCEは**読み取り**、DEMOは明示的に許可した**書き込み**です。DEMOへの投入は `generate-demo.py --destination sqlserver` 等に加え、`--env-file` と `--allow-sql-write` を必要とし、毎回新しい `lh_demo_...` スキーマを作ります。通常の会社導入で業務データを読むだけなら、DEMO項目は空のままで構いません。[DEMO-DATA.md](DEMO-DATA.md)

## GUIと操作APIのprivate設定

| 項目 | 生成時の既定 | 意味と変更時の影響 | 秘密 |
| --- | --- | --- | --- |
| `CONTROL_API_TOKEN` | 64文字のhexを自動生成 | StreamlitからFastAPI、およびSwagger等の操作クライアントへのBearer鍵。変更後はAPIと画面を整合させて再配置する | はい |
| `CONTROL_SETTINGS_DIR` | 生成先ファイルの親ディレクトリ | APIへ `/private` としてRW mountする専用0700ディレクトリ。設定ファイルのatomic replaceに必要。無関係な秘密ファイルを同居させない | 社内 |
| `CONTROL_SETTINGS_NAME` | 生成先のファイル名 | 上記dir内の0600設定ファイル名。pathを含まない名前。GUIから保存先の移動は拒否する | 社内 |

`demo` profileで設定画面とFastAPIが起動します。APIだけにランディングとprivate dirをmountし、Streamlit画面側にはhostファイルをmountしません。両方を `WORKER_UID` / `WORKER_GID` で実行するため、専用dirはそのIDで読み書きできる必要があります。Docker socketを渡してGUIから任意の再配置を行う構成ではありません。

GUIの保存は **saved**、コンテナへの反映は **applied** と区別します。設定APIは保存後も起動時のDEMO接続設定を使い、変更がある間は `pending_restart` を返します。`LANDING_DIR` を保存しても `/landing` mountは移りません。profile/port/UID/volume/秘密鍵の変更は影響を確認し、同じ設定でCLIまたはDokployから再配置します。DokployのEnvironmentとprivateファイルは自動同期しません。[API.md](API.md)

GUIからSQLへ素材を投入する場合は、`DEMO_SQLSERVER_CA_FILE` のCAをこの専用dir配下へ置くと、host側絶対pathをAPIの `/private` 側へ安全に対応付けます。dir外、symlink、regular file以外は拒否します。CLIは元のhost pathを使えます。`SOURCE_SQLSERVER_CA_FILE` やJava用truststoreを同じ仕組みで自動配布する設定ではありません。

## SQL Serverの変更を読むCDC設定

CDCはSOURCEの全件SELECTと別の選択機能です。DB担当がSQL Server側のCDC、SQL Server Agent、対象表の権限を準備し、専用の接続情報と6表の対応JSONを確認してから有効にします。設定欄を埋めるだけで会社DBのCDCを自動有効化しません。生成器の値が正本で、旧private設定には後述の `--upgrade` で不足keyを追加します。

| 項目 | 既定・未設定時 | 意味と変更時の影響 | 秘密 |
| --- | --- | --- | --- |
| `CDC_CONFIG_DIR` | 生成先private dir配下の `cdc` | hostのprivate CDC設定dir。6表のtable-map.jsonを登録/consumer用へread-only mountする。Connectへdir全体を渡さない。変更はmountの再配置が必要 | 社内 |
| `CDC_SQLSERVER_HOST` | 空 | CDCを読むSQL Server。SOURCE/DEMOとは独立した接続先 | 社内 |
| `CDC_SQLSERVER_PORT` | `1433` | CDCソースのTCP port | 社内 |
| `CDC_SQLSERVER_DATABASE` | 空 | CDC有効化済みの対象業務DB。変更は別のcapture履歴として設計する | 社内 |
| `CDC_SQLSERVER_USER` | 空 | DBAが許可したcapture読取用ログイン。通常のSELECT専用ログインと権限を混同しない | 社内 |
| `CDC_SQLSERVER_PASSWORD` | 空 | capture読取用パスワード。Debeziumへ環境変数providerで渡す | はい |
| `CDC_DEMO_OWNERSHIP_TOKEN` | 空 | 準備済み専用CDC検証DBの所有権を確認する秘密鍵。DB準備helperが生成した値を保持し、通常の会社DBには使わない | はい |
| `CDC_SQLSERVER_TRUSTSTORE` | 空 | Javaの既定CAを使うか、`/run/cdc/sqlserver-truststore.p12` のコンテナ側truststore pathを指定。host pathをそのまま書かない | 社内 |
| `CDC_SQLSERVER_TRUSTSTORE_FILE` | 空 | host側のPKCS12/JKSファイル。公開CA証明書だけを格納し、Connectの `/run/cdc/sqlserver-truststore.p12` へ単一ファイルでread-only mount。変更は再配置が必要 | 社内 |
| `CDC_SQLSERVER_TRUSTSTORE_PASSWORD` | 空 | 上記truststoreのパスワード | はい |
| `CDC_SQLSERVER_HOSTNAME_IN_CERTIFICATE` | 空 | DBA/ネットワーク担当が確認した証明書のホスト名。証明書検証を省略する欄ではない | 社内 |
| `CDC_SQLSERVER_AGENT_STATUS_QUERY` | 空 | 必要な場合だけ、DBAが承認したAgent状態確認SELECTを指定。通常は製品既定を使う | 社内 |
| `CDC_TOPIC_PREFIX` | `factory` | Debeziumのtopic名前空間。既存履歴の途中で変更しない | 社内 |
| `CDC_CONNECTOR_NAME` | `factory-source` | 登録するconnector識別名。変更は新しいcaptureとして影響確認が必要 | 社内 |
| `CDC_CONNECT_PORT` | `8083` | Kafka Connectのhost側管理port。CDC Composeはloopbackへ固定して公開する | 社内 |
| `CDC_UI_PORT` | `8084` | Kafbat UIのhost側port。loopbackへ固定して公開する | 社内 |
| `CDC_KAFKA_CLUSTER_ID` | 22文字URL-safe値を生成して固定 | Kafkaの永続cluster識別子。データvolumeと一緒に維持し、再起動ごとに再生成しない | 社内 |
| `CDC_GROUP_ID` | `lakehouse-cdc` | consumer groupとConnect groupの基準名。変更時はoffsetと再読込範囲を確認 | 社内 |
| `CDC_BATCH_SIZE` | `100` | 1バッチに読む変更イベント数。1〜1000で、メモリ・反映頻度との兼ね合いを見る | いいえ |
| `CDC_POLL_SECONDS` | `5` | 1バッチの取得待ち秒数。1〜60 | いいえ |

これらは設定画面のallowlistにも含み、所有権tokenも秘密欄として読み戻しません。CDC用のソース変更APIは `CDC_DEMO_OWNERSHIP_TOKEN` と専用DB内markerの一致を要求し、DEMO管理資格情報で固定した追加/更新/削除だけを実行します。[API.md](API.md#cdc用の追加更新削除を試す)

CDC接続・mapping・connector登録の検査はCDCツールが行い、設定保存だけを会社DBへの接続成功とは扱いません。`CDC_CONFIG_DIR` / `CDC_SQLSERVER_TRUSTSTORE_FILE` の保存は `pending_mount_change` になります。topic、group、cluster ID、volumeの変更は単なる表示名の変更ではなく、再読込・重複・履歴分離に関わるため、既存offsetとraw eventを確認してから行います。

`CDC_TEST_TRUST_SERVER_CERTIFICATE` は既定falseの隔離試験専用overrideです。loopbackの `lh_cdc_demo_...` DBに限定し、通常のGUI編集項目には含めません。会社接続はJava truststoreと証明書名を正しく設定します。SQL Serverを模した単体試験や専用DBでの試験成功を、会社側でのCDC受入結果に転記しません。

## 任意のPoCサービス

各機能は `bi`、`notebooks`、`vector`、`lineage` profileで選びます。portはComposeの既定、秘密値は生成器の値を保持します。既存site.envは `--upgrade` で不足キーを補い、秘密値を再生成して置き換えません。入口状態の読み方は [POC-PORTAL.md](POC-PORTAL.md) を参照してください。

| 項目 | 既定・指定 | 意味と変更時の影響 | 秘密 |
| --- | --- | --- | --- |
| `SUPERSET_PORT` | 8088 | BIのhost入口。loopbackで公開 | 社内 |
| `SUPERSET_DB_PASSWORD` | 生成値を保持 | Superset metadata DB。DB側と整合が必要 | はい |
| `SUPERSET_SECRET_KEY` | 生成値を保持 | 保存情報の暗号化/signing。DBと一緒に保持 | はい |
| `SUPERSET_ADMIN_USERNAME` | admin | 初期管理者名。既存アカウントをenvだけで置換しない | 社内 |
| `SUPERSET_ADMIN_PASSWORD` | 生成値を保持 | 初期管理者の秘密値。URLへ含めない | はい |
| `SUPERSET_ADMIN_EMAIL` | admin@example.invalid | 初期管理者のメール欄。実運用の値はprivateへ | 社内 |
| `SUPERSET_REDIS_PASSWORD` | 生成値を保持 | BI専用Redisの認証 | はい |
| `JUPYTER_PORT` | 8888 | JupyterLabのhost入口。loopbackで公開 | 社内 |
| `JUPYTER_TOKEN` | 生成値を保持 | Notebookのログイン。portalのURLへ埋め込まない | はい |
| `NOTEBOOKS_DIR` | `/srv/lakehouse/notebooks` | Notebookの保存先。UID/GIDとmountを揃え、変更は再配置 | 社内 |
| `QDRANT_PORT` | 6333 | Qdrant API/画面のhost入口。loopbackで公開 | 社内 |
| `QDRANT_API_KEY` | 生成値を保持 | collection作成等の管理用。読取専用鍵と分離 | はい |
| `QDRANT_READ_ONLY_API_KEY` | 生成値を保持 | 参照用。管理キーの代わりに書込みへ使わない | はい |
| `MARQUEZ_API_PORT` | 5000 | lineage登録/参照APIのhost入口 | 社内 |
| `MARQUEZ_UI_PORT` | 3001 | lineage画面のhost入口 | 社内 |
| `MARQUEZ_DB_PASSWORD` | 生成値を保持 | Marquez metadata DB。DBと整合して管理 | はい |
| `CORPORATE_CA_FILE` | 空 | host上の公開企業CA PEM。空欄時は0バイトの `infra/proxy/empty-ca.pem` を使い追加なし。自作imageへBuildKit secretでOS/Java trust追加、n8nへ公開CA単体bind。診断時はCONTROL_SETTINGS_DIR配下。変更はcacheなし再build／再配置 | 社内 |

SSL_CERT_FILE、REQUESTS_CA_BUNDLE、AWS_CA_BUNDLE、CURL_CA_BUNDLEはimage内の固定bundleを指すruntime設定です。private入力としてGUIから任意のpathに変更する項目ではありません。CA保存後の反映と、保存値を使う診断を区別します。[PROXY.md](PROXY.md)

## Dokploy・Swarmの別導入設定

これらはcoreの `up` では使わず、`infra/dokploy/install.sh --env-file ...` が検査します。空の必須項目を実環境に合わせて埋めるまではDokploy導入を行えません。

| 項目 | 生成時の既定 | 意味と変更時の影響 | 秘密 |
| --- | --- | --- | --- |
| `DOKPLOY_BIND_IP` | 空 | Dokploy UI/Traefikを公開する限定IPv4。ホストに割り当て済みの値を使う | 社内 |
| `DOKPLOY_ALLOWED_CIDRS` | 空 | 許可するprivate/loopbackクライアントCIDRをカンマ区切り。全開放を指定する欄ではない | 社内 |
| `DOKPLOY_ADMIN_PORT` | `3100` | 管理画面のhost port。既存サービスとSwarm portを避ける | 社内 |
| `SWARM_ADVERTISE_ADDR` | 空 | このホストのSwarm管理用private IPv4。loopbackは不可 | 社内 |
| `SWARM_ADDRESS_POOL` | `172.30.0.0/16` | overlay用の生成既定CIDR。会社LAN/VPN/既存Dockerネットワークと重ならないことを確認して採用する | 社内 |
| `SWARM_SUBNET_SIZE` | `24` | poolから切るoverlay subnetのprefix長。導入後の既存Swarmに別のpool/sizeを強制適用しない | 社内 |
| `DOCKER_BRIDGE_CIDR` | 空（既存bridgeから検査） | `docker_gwbridge` の想定CIDRを指定する任意項目。値を書くだけでDocker daemonのbridgeを作り替える機能ではない | 社内 |

既存Swarmや同名の管理外リソースが見つかった場合、導入ツールは強制離脱・再初期化・削除で回避しません。会社の既存管理基盤を維持するのか、このinstallerで新設するのかを先に決めます。[DOKPLOY.md](DOKPLOY.md)

## アプリのproxy設定とcanonical bypass

| 項目 | 生成時の既定 | 意味と変更時の影響 | 秘密 |
| --- | --- | --- | --- |
| `HTTP_PROXY` / `http_proxy` | 両方空 | HTTP向けproxy URL。両caseを同じ値にする。変更後はbuild/runtimeの反映を確認 | 認証付きURLは秘密 |
| `HTTPS_PROXY` / `https_proxy` | 両方空 | HTTPS向けproxy URL。CONNECT proxyでは `http://` 方式の場合もある | 認証付きURLは秘密 |
| `LAKEHOUSE_NO_PROXY` | 必須内部40名のCSV | Compose補間用のcanonical。標準内部名とprivate追加先を正規化した正本 | 社内 |
| `NO_PROXY` / `no_proxy` | canonicalと同じ | clientが使う大小文字の互換値。生成/upgrade/GUIで3項目を同期 | 社内 |

必須名は [settings.pyのREQUIRED_NO_PROXY](../scripts/settings.py) を正本とします。loopback、実際の全Composeサービス、Dokployの内部サービス名を含みます。生成・CLI読込・upgradeは「必須 → 既存canonical → 大文字 → 小文字」の順でprivate値をマージし、空白・空要素・大小文字を無視した重複を除きます。host shellの値はこのマージに採用しません。CLIは子プロセスへ3項目を明示設定します。

GUIはbypassの1項目を編集すると会社追加分を含む希望一覧として扱い、必須名を戻して3項目を保存します。追加先を削除する際も1項目の明示編集で行えます。host上の手編集では3項目を揃えます。どれかに残った旧追加先はupgradeの和集合に残ります。

raw Compose/Dokploy Git配置では、upgrade後のcanonicalをEnvironmentへ引き継ぎます。未設定／空欄なら内部名を必ず含む旧形式fallbackになりますが、重複除去やshellで隠れた会社追加先の復元はしません。`LAKEHOUSE_NO_PROXY` 自体を別の値でexportしません。HTTP(S)4項目もCLIではprivateが優先、rawでは通常のshell補間優先です。

daemon用 `proxy.env` は従来の6項目のみで、canonicalを受け付けず、site設定とも自動同期しません。CIDR/domain suffixの意味はclientごとに違います。[PROXY.md](PROXY.md)、[Compose補間仕様](https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/)

## 更新時に使う判断

- clone先だけの変更では、ソースの場所とビルドcontextを確認する。データの移動とは別の作業である。
- 実行名やvolume prefixの変更では、再配置前にvolumeの実名と現在のwriterを確認する。
- DBパスワード・暗号鍵の変更では、対応するDBやGUIの正式な更新手順とバックアップを用意する。
- port・bind先の変更では、利用者のURL、接続元制限、n8n/Airflowの内部URLを混同しない。
- 入力形式・表の意味の変更では、envだけで吸収せず、`medallion/sources.json` とSQL、検証データを一緒に見直す。

生成器の現在の項目が正本です。配置担当がレイクハウスホストのリポジトリのルートで、既存のprivateファイルを更新します。

**Bash / Zsh共通**

```bash
python3 scripts/configure.py --output /absolute/private/site.env --upgrade
./scripts/lakehouse.sh --env-file /absolute/private/site.env plan
```

不足キーを追加し、bypass3項目を正規化します。他の既存値・秘密鍵・profileは保持します。元ファイルは隣の `site.env.before-upgrade` に0600で保存し、更新先も0600でatomic replaceします。変更が必要で控えが既に存在すると停止するので、控えを確認してprivate領域の一意な別名へ保管してから再実行します。変更不要ならファイルに触れず正常終了します。backup全体で現在の資格情報を上書きして戻さず、戻す項目を確認します。詳細は [COMPANY-UPDATE.md](COMPANY-UPDATE.md) です。


## MCPの追加設定

`mcp` profileはMCPと内部操作APIを起動します。`demo` も選ぶと操作画面を使えます。MCPへAPIの管理鍵を流用せず、専用tokenを使います。

| 項目 | 生成時の既定 | 意味と変更時の影響 | 秘密 |
| --- | --- | --- | --- |
| `MCP_PORT` | `8503` | hostの `/mcp` 入口。loopbackへ公開 | 社内 |
| `MCP_API_TOKEN` | 64文字hexを生成 | MCP client用Bearer鍵。APIのCONTROL_API_TOKENとは別 | はい |
| `MCP_ENABLE_MUTATIONS` | `false` | 既定は参照toolのみ。変更は書込み権限と操作範囲を確認して再配置 | いいえ |
| `MCP_ALLOWED_HOSTS` | `localhost:*,127.0.0.1:*,mcp:*` | 着信Hostの許可。外向きNO_PROXYやn8n SSRF例外とは別 | 社内 |

## n8nのSSRF設定

n8n2.46.7の上流既定は保護無効ですが、本構成は有効にして `worker` だけを例外にします。

| 項目 | 生成時の既定 | 意味と変更時の影響 | 秘密 |
| --- | --- | --- | --- |
| `N8N_SSRF_PROTECTION_ENABLED` | `true` | user入力で変わるHTTP宛先の保護を有効化 | いいえ |
| `N8N_SSRF_BLOCKED_IP_RANGES` | `default,100.64.0.0/10` | n8n標準の内部/特殊範囲と追加CIDRを拒否 | 社内 |
| `N8N_SSRF_ALLOWED_IP_RANGES` | 空 | 必要な狭いIP例外。許可が拒否より優先する | 社内 |
| `N8N_SSRF_ALLOWED_HOSTNAMES` | `worker` | DNS名だけ。空ならこの内部例外もなし | 社内 |
| `N8N_SSRF_BLOCKED_HOSTNAMES` | 空 | 業務上の拒否名。許可名が優先する | 社内 |
| `N8N_SSRF_DNS_CACHE_MAX_SIZE` | `1048576` | DNS cacheのbytes。基盤検査64KiB〜16MiB | いいえ |

保存だけではn8nに反映されません。同じ配置を再作成し、許可workerの成功と未許可内部先の拒否を両方試験します。[SSRF手順](SSRF.md)、[n8n固定版source](https://github.com/n8n-io/n8n/blob/n8n%402.46.7/packages/%40n8n/config/src/configs/ssrf-protection.config.ts)

## 常駐Sparkの画面

`compute` はMaster/Worker/Historyを常駐させます。`SPARK_MASTER_UI_PORT`（8082）、`SPARK_WORKER_UI_PORT`（8085）、`SPARK_HISTORY_UI_PORT`（18080）は他のhostportと重ならない値へ変更できます。cluster RPCとNotebook driver portはDocker内部だけです。canonicalには `spark-master,spark-worker,spark-history,spark-volumes-init,spark-check` が標準で含まれます。Notebookはcomputeの有無でremote/localを選びます。

`WORKER_UID/GID` はNotebookとSparkのshared volumeを使う非rootの同じ実行IDにします。再配置でデータを消す構成にはせず、運用開始後の実行ID変更は既存ファイル所有権の移行として扱います。SSRF設定6項目と値の意味は [SSRF.md](SSRF.md) を参照してください。


Docker 29／Buildx Bake／社内proxyで判明した互換性修正の取り込みと、`LAKEHOUSE_NO_PROXY` の生成・Dokployへの登録は [会社VMの更新手順](COMPANY-UPDATE.md) を参照してください。


## 対象版と出典の読み方

設定名・既定・検査の正本は [configure.py](../scripts/configure.py)、[settings.py](../scripts/settings.py)、各Compose、GUIの [control_settings.py](../infra/demo/control_settings.py) です。表にある生成既定と第三者製品の上流既定を区別します。

監査対象はDocker Engine29系／[Compose5.6.1](https://github.com/docker/compose/releases/tag/v5.6.1)、n8n2.46.7、Debezium3.7.1.Final、Kafka4.4.0です。SQL ServerとSambaは外部ホストの既存版を使い、リポジトリでserver版を固定していません。DBA／ホスト管理者が導入先の版・editionを記録します。[Debezium3.7](https://debezium.io/documentation/reference/3.7/connectors/sqlserver.html)、[Microsoft SQL権限](https://learn.microsoft.com/en-us/sql/relational-databases/security/permissions-database-engine?view=sql-server-ver17)

## 初学者向け: 設定とデータの位置を結び付ける

| 編集したいもの | 編集する場所 | 保存だけで変わるか |
| --- | --- | --- |
| 画面port・profile・接続先 | 外部private `site.env`、または設定GUI | 変わらない。管理者が同じアプリへ反映 |
| Dokployが次に配置する値 | Composeサービス自身のEnvironment | Save後のDeployが必要 |
| CSV/JSONの列・型・Goldの式 | 公開契約と `medallion/` のSQL | envでは変更できない。レビュー・試験・配置が必要 |
| Grafanaのdashboard | Grafana GUI | SaveするとGrafana DBへ保存。Composeの再編集は不要 |
| 会社の表/列名の対応 | Git外のmapping JSON | 次のextractor/CDCで利用。既存stateとの互換も確認 |
| ホストの共有・proxy | Samba/daemonの個別管理設定 | GUIのSaveでは変更されない |

`site.env` の `#` コメントは独立した行に置きます。`export KEY=...` はこのファイルの構文ではありません。`KEY=value # 説明` のようなinline commentを、各literal parserが同じ意味で解釈するとは限りません。生成された単一行ASCIIの形式を維持します。

生成された基盤用の鍵は英数字等の共通に扱える形式です。手入力した秘密値に `$`・引用符・空白等がある場合、Composeのdotenv補間と、SQLツールのliteral読込を同じものと仮定しません。SOURCE/DEMO専用のhost用ファイルは値を文字どおり読み、引用符も値の一部になる箇所があります。proxyのuserinfoはURLエンコードします。既存の会社資格情報を勝手に変更せず、**実際にその値を読むclientで照合**してから共有設定を使います。[Composeの補間・dotenv構文](https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/)

## 変更の種類から反映手順を選ぶ

| 変更する設定群 | 変更前に記録するもの | 反映・変更後の確認 |
| --- | --- | --- |
| host port | 現在のURL、空port、接続元 | 同じアプリ再配置。新URL/SSH転送を確認。内部URLは維持 |
| profile追加 | 既存一覧、必要secret、空port、容量 | 既存profileへ追加。新serviceとone-shot、各GUIの初期化を確認 |
| LANDING_DIR / NOTEBOOKS_DIR | 旧実体、UID/GID、保管するデータ | 新dirの準備と必要なデータ移行後にmount変更。envだけでコピーしない |
| WORKER_UID/GID | 既存file/volume所有者と共有利用者 | 非root条件・ACLを確認して移行。同じデータを別UIDで無計画に起動しない |
| CONTROL_SETTINGS_DIR/NAME | 旧private fileとAPI mount | host側で専用dir/fileを準備。GUIからの移動は使わない |
| project名 / data prefix | 全volume実名、writer、暗号鍵 | 旧writerを停止し、同じデータの引継ぎか新環境かを明示 |
| HTTP(S) proxy / CA | daemon/build/runtimeごとの旧設定 | 主体ごとに反映。CA内容の変更はcacheなしbuildも必要 |
| CONTROL_API_TOKEN | 現API/GUIの利用者 | APIとStreamlit、関連clientを一組で切替。旧tokenの失効と401拒否も確認 |
| WORKER_TOKEN | n8n credential、Airflow Connection | worker/clientの同期。旧資格情報をworkflow JSONへ書かない |
| DB password / 暗号鍵 | DB側の状態と復元点 | 製品の正式な資格情報/鍵更新を実施。env変更だけではDBを更新しない |
| SOURCE/DEMO接続 | 用途・対象DB・TLS名・権限 | 反映待ち中の投入を止め、新しい接続で目的別受入 |
| CDC識別子 / mapping | source identity、state、topic、offset | 同名登録の自動上書きなし。DBAと移行・再構築の計画を確認 |

### 例: Trinoのhost portだけを変更する

1. 配置先で新portが空いていることを管理者が確認します。ここでは説明用に `TRINO_PORT=8090` とします。
2. private fileまたは設定GUIでこの1項目だけ変更します。Trino内部の8080やSuperset内部接続を8090へ置換しません。
3. `plan` で有効serviceのport競合を検査します。Dokploy管理ではサービスEnvironmentにも同じ変更を保存しDeployします。
4. 既定8080のURLではなく新しいhost portで開き、SQL/workerの疎通を確認します。遠隔利用のSSH転送も右側のportを更新します。
5. volume名・bucket・暗号鍵の変更を伴わないことを確認します。画面が開かない時にデータ再生成を始めません。

### 例: notebooksを追加する

既存の `COMPOSE_PROFILES` へ `notebooks` を追記し、`NOTEBOOKS_DIR` とJupyter tokenを確認します。新しいworkspaceはGit外に作り、実行UID/GIDで読み書き可能にします。`compute` も有効ならremote Spark、無効なら明示local[2]です。profile追加だけで会社全利用者のkernelや資格情報を分離する構成にはなりません。[PoCツール](POC-TOOLS.md)

## 保存から適用までの状態を読む

| 観測 | 意味 | 次に行うこと |
| --- | --- | --- |
| `saved: true` | private fileへの書込みが成功 | 変更した項目を確認する |
| `applied: false` | 保存APIが再配置をしていない | 配置担当がCLI/Dokployで反映 |
| `pending_restart: true` | API起動時設定と保存値に差がある | 特にSQL接続・token変更時は新規投入を待つ |
| `pending_mount_change: true` | path/ID等に配置確認が必要な差がある | host実体・所有者・mountを確認 |
| 再配置後の表示が一致 | APIがその設定で読み直された | 他serviceのhealth/認証と業務往復は別に確認 |

既存の秘密欄を空のままGUI保存しても値は保持されます。秘密値を空にする作業と、入力し忘れた作業を同じ操作へまとめません。DB passwordや暗号鍵を「反映待ちを消すため」に再生成しないでください。

## 設定を変更する開発者の確認範囲

設定を一つ増やす時は生成器だけを更新して終わりにしません。[configure.py](../scripts/configure.py) の新規/upgrade、[settings.py](../scripts/settings.py) の形式・profile条件、各Composeの補間とsecret/mount、[GUI保存](../infra/demo/control_settings.py) のallowlistと秘密分類、native clientの反映方法を一組で確認します。

受入では、新規生成、古いprivate fileのupgrade、既存backupがある更新、変更不要のno-op、GUI保存/秘密非表示、選択profile/未選択profile、CLI/raw Compose/Dokploy、再配置後の実通信を区別します。社内の実値はtest fixtureへコピーせず、合成値で境界を試験します。

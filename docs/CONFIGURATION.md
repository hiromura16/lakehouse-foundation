# サイト設定リファレンス

この文書は `scripts/configure.py` が生成する設定と、現在のCompose・各CLIが読む項目を説明します。更新日: 2026-11-09。会社への導入順序は [COMPANY-ROLLOUT.md](COMPANY-ROLLOUT.md)、通常の操作は [DEPLOYMENT.md](DEPLOYMENT.md) を参照してください。

## まず決めること

リポジトリの取得先、設定ファイル、ランディング、Dockerの永続データは別の場所です。clone先は任意で、スクリプトが自身の配置からリポジトリのルートを見つけます。会社側で特定の `apps` ディレクトリを作る必要はありません。

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
| `COMPOSE_PROFILES` | 空 | coreに加える機能。`automation`、`monitoring`、`compute`、`orchestration`、`demo`、`cdc` をカンマ区切り。変更後は同じprivate設定で `up` を実行する | いいえ |

Dokployの `appName` はGUI側の項目であり、`site.env` に別の `APP_NAME` を作る仕組みではありません。Dokployは `-p <appName>` でComposeを実行するため、同じ環境をCLIでも操作する場合は `COMPOSE_PROJECT_NAME` をその `appName` に合わせます。画面のEnvironment名だけではデータは分離されません。[DOKPLOY.md](DOKPLOY.md)

### DATA_VOLUME_PREFIXを使う場面

通常の新規導入では空のままにします。対象は `catalog-db`、`storage`、`trino`、`n8n-db`、`n8n`、`prometheus`、`grafana`、`airflow-db`、`airflow-state` の9個と、CDCを使う場合の `cdc-kafka` / `cdc-state` で、実名は `<prefix>_<volumeキー>` です。空の場合のprefixは `COMPOSE_PROJECT_NAME` です。

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
| `DEMO_API_PORT` | `8502` | `demo` のFastAPI/Swagger。業務操作はCONTROL_API_TOKENによるBearer認証 | 社内 |

coreのhost portは1024〜65535から選び、同じサイト設定内で重複させません。任意プロファイルのportや秘密項目も生成され、設定検査の対象になるため、未使用だからといって行を削除しません。

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

CDCはSOURCEの全件SELECTと別の選択機能です。DB担当がSQL Server側のCDC、SQL Server Agent、対象表の権限を準備し、専用の接続情報と6表の対応JSONを確認してから有効にします。設定欄を埋めるだけで会社DBのCDCを自動有効化しません。生成器の値が正本で、旧private設定には新規keyを個別に追加します。

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

各機能は `bi`、`notebooks`、`vector`、`lineage` profileで選びます。portはComposeの既定、秘密値は生成器の値を保持します。既存site.envには追加キーを個別に補い、秘密値を再生成して置き換えません。入口状態の読み方は [POC-PORTAL.md](POC-PORTAL.md) を参照してください。

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
| `NOTEBOOKS_DIR` | 専用host dir | Notebookの保存先。UID/GIDとmountを揃え、変更は再配置 | 社内 |
| `QDRANT_PORT` | 6333 | Qdrant API/画面のhost入口。loopbackで公開 | 社内 |
| `QDRANT_API_KEY` | 生成値を保持 | collection作成等の管理用。読取専用鍵と分離 | はい |
| `QDRANT_READ_ONLY_API_KEY` | 生成値を保持 | 参照用。管理キーの代わりに書込みへ使わない | はい |
| `MARQUEZ_API_PORT` | 5000 | lineage登録/参照APIのhost入口 | 社内 |
| `MARQUEZ_UI_PORT` | 3001 | lineage画面のhost入口 | 社内 |
| `MARQUEZ_DB_PASSWORD` | 生成値を保持 | Marquez metadata DB。DBと整合して管理 | はい |
| `CORPORATE_CA_FILE` | 空 | host上の企業CA PEM。build secretでOS/Javaのtrustへ追加。診断で使う場合はCONTROL_SETTINGS_DIR配下に置く。変更はimage再build/再配置が必要 | 社内 |

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

## プロキシ6変数

| 項目 | 生成時の既定 | 意味と変更時の影響 | 秘密 |
| --- | --- | --- | --- |
| `HTTP_PROXY` | 空 | 外向きHTTP用proxy URL。runtimeとbuild argsへ渡す。認証付きURLなら資格情報として扱う | 条件付き |
| `http_proxy` | `HTTP_PROXY` と同じ値 | 小文字のみを参照するクライアント用。上段と同じ値へ揃える | 条件付き |
| `HTTPS_PROXY` | 空 | 外向きHTTPS用。HTTP CONNECT proxyならURLの方式が `http://` のこともある | 条件付き |
| `https_proxy` | `HTTPS_PROXY` と同じ値 | 小文字版。上段と同じ値へ揃える | 条件付き |
| `NO_PROXY` | 内部サービス名とloopback | Docker内のS3・カタログ・DB・worker等へ直接つなぐ除外先。社内SQLのTCPをHTTP proxyで中継する設定ではない | 社内 |
| `no_proxy` | `NO_PROXY` と同じ値 | 小文字版。除外先の差で内部通信がproxyへ出ないよう揃える | 社内 |

内部除外にはloopback、`storage,catalog,catalog-db,trino,worker` と、利用するn8n/Airflow/監視/操作画面/CDCのサービス名を含めます。CDCの実サービス名は `cdc-kafka,cdc-connect,cdc-consumer,cdc-ui` です。旧private設定のNO_PROXYも見直します。社内の追加接続先は実環境のprivate設定へ追記します。別ファイルのdaemon用 `proxy.env` は自動で `site.env` に同期されません。[PROXY.md](PROXY.md)

## 更新時に使う判断

- clone先だけの変更では、ソースの場所とビルドcontextを確認する。データの移動とは別の作業である。
- 実行名やvolume prefixの変更では、再配置前にvolumeの実名と現在のwriterを確認する。
- DBパスワード・暗号鍵の変更では、対応するDBやGUIの正式な更新手順とバックアップを用意する。
- port・bind先の変更では、利用者のURL、接続元制限、n8n/Airflowの内部URLを混同しない。
- 入力形式・表の意味の変更では、envだけで吸収せず、`medallion/sources.json` とSQL、検証データを一緒に見直す。

生成器の現在の項目が正本です。旧版のprivate設定を新しい版へ引き継ぐ場合は、追加キーを個別に補い、既存の秘密値を保持したまま `plan` を通します。新しく生成した別ファイルは比較用に使えますが、既存設定全体の置換には使いません。


## PoCツール・MCP・企業CAの追加設定

追加profileの一覧は `bi,notebooks,vector,lineage,mcp` です。coreだけの設定は引き続き利用できます。既存環境はprofile追加前に `python3 scripts/configure.py --output /absolute/private/site.env --upgrade` を実行し、新項目だけを追加します。既存の暗号鍵・パスワード・接続先・profileは保持し、直前ファイルを `.before-upgrade` として0600で保存します。控えが既にあるときは上書きしません。

| 設定 | 用途 |
| --- | --- |
| `SUPERSET_PORT` / `SUPERSET_DB_PASSWORD` / `SUPERSET_SECRET_KEY` | BIの入口・専用DB・署名鍵 |
| `SUPERSET_ADMIN_USERNAME` / `SUPERSET_ADMIN_PASSWORD` / `SUPERSET_ADMIN_EMAIL` | 初回BI管理者。既存ユーザーの秘密値を再初期化しない |
| `SUPERSET_REDIS_PASSWORD` | BI専用cache認証 |
| `JUPYTER_PORT` / `JUPYTER_TOKEN` / `NOTEBOOKS_DIR` | Spark付きNotebookの認証と外部workspace |
| `QDRANT_PORT` / `QDRANT_API_KEY` / `QDRANT_READ_ONLY_API_KEY` | vector DB。管理と参照の鍵を分離 |
| `MARQUEZ_API_PORT` / `MARQUEZ_UI_PORT` / `MARQUEZ_DB_PASSWORD` | 系統APIとUI・専用DB |
| `MCP_PORT` / `MCP_API_TOKEN` | AI接続先とMCP専用認証 |
| `MCP_ENABLE_MUTATIONS` / `MCP_ALLOWED_HOSTS` | 書込みtoolの明示有効化とHost許可 |
| `CORPORATE_CA_FILE` | buildへ渡す公開企業CA PEM chainのホスト側絶対path。未設定は追加CAなし |

`NOTEBOOKS_DIR` はリポジトリ外の絶対pathで事前作成し、非rootの `WORKER_UID` と数値 `WORKER_GID` に書込権を与えます。追加キーは生成された値を保持し、公開Composeへ秘密値を直書きしません。`SUPERSET_SECRET_KEY` と `JUPYTER_TOKEN` は32文字以上、Qdrantの2鍵は異なる値を使います。全hostportを一意にします。

`NO_PROXY` と `no_proxy` に、新しい内部名 `superset,superset-db,superset-redis,jupyter,qdrant,marquez,marquez-db,marquez-web,mcp` を追加します。`--upgrade` は既存のproxy指定を変更しません。GUI保存後はDokployのEnvironmentとprivate設定を一致させて再配置し、CA変更時はcacheを使わず再buildします。[PoCツール](POC-TOOLS.md)、[MCP](MCP.md)、[会社proxy手順](COMPANY-PROXY.md) も確認してください。


SSRFの設定値、内部workerの例外、NO_PROXYとの違い、GUI保存と再配置、許可・拒否の受入は [SSRF.md](SSRF.md) を参照してください。


## 常駐Sparkの画面

`compute` はMaster/Worker/Historyを常駐させます。`SPARK_MASTER_UI_PORT`（8082）、`SPARK_WORKER_UI_PORT`（8085）、`SPARK_HISTORY_UI_PORT`（18080）は他のhostportと重ならない値へ変更できます。cluster RPCとNotebook driver portはDocker内部だけです。`NO_PROXY` / `no_proxy` に `spark-master,spark-worker,spark-history,spark-volumes-init` を含めます。Notebookはcomputeの有無でremote/localを選びます。

`WORKER_UID/GID` はNotebookとSparkのshared volumeを使う非rootの同じ実行IDにします。再配置でデータを消す構成にはせず、運用開始後の実行ID変更は既存ファイル所有権の移行として扱います。SSRF設定6項目と値の意味は [SSRF.md](SSRF.md) を参照してください。

# GUI操作APIとSwagger

このAPIは、設定の保存、合成素材の投入、明示した後続処理、Goldの参照を担当します。FastAPIのSwagger UIから入力項目と応答形式を確認できます。初めて操作する場合は [DEMO-GUI.md](DEMO-GUI.md)、会社導入は [COMPANY-ROLLOUT.md](COMPANY-ROLLOUT.md) を先に読みます。

## 起動と認証

private設定に `COMPOSE_PROFILES=demo` を追加し、既存のprofileを使っている場合はカンマで併記します。`CONTROL_SETTINGS_DIR` はこの環境専用の0700ディレクトリ、`CONTROL_SETTINGS_NAME` はその中の0600設定ファイル名です。生成器は生成先から両方を設定します。GUIの実行UID/GIDが、privateディレクトリとランディングへアクセスできることを確認します。

```bash
export LAKEHOUSE_ENV_FILE="$HOME/.config/lakehouse-foundation/site.env"
./scripts/lakehouse.sh plan
./scripts/lakehouse.sh up
```

| 入口 | 既定のURL | 用途 |
| --- | --- | --- |
| 操作画面 | `http://localhost:8501/` | Streamlitの設定・素材投入・進捗確認 |
| Swagger UI | `http://localhost:8502/docs` | APIの入力形式を確認して実行 |
| OpenAPI | `http://localhost:8502/openapi.json` | APIクライアント用の仕様 |
| readiness | `http://localhost:8502/health` | 設定と認証tokenが読み込めたか |

既定のhost bindはloopbackです。別端末では会社標準のSSHポート転送等を使います。Streamlitの画面自体に利用者別ログインを実装していないため、共有する場合は入口の認証・HTTPS・接続元制限を先に準備します。SwaggerとOpenAPIは認証なしで仕様を表示できますが、業務操作APIはすべてBearer認証を要求します。`/health` の応答に秘密情報は含めません。

Swaggerの **Authorize** にprivate設定の `CONTROL_API_TOKEN` を入力します。HTTP Bearerの欄にはtoken本体を入力し、画面がヘッダーを作ります。tokenをURL、スクリーンショット、workflowのJSONへ含めません。Streamlitはサーバー側でtokenを読み、内部APIへ送ります。ブラウザーから既存tokenを読み戻す機能はありません。

SwaggerのJavaScript・CSS・faviconはimage内から配信します。ブラウザーから外部CDNや外部schema validatorへ接続せず、認証tokenの永続保存も無効にしています。依存ファイルの取得とchecksum確認はimage build時に行うため、閉域環境では許可したbuild経路を用意します。

このAPIはDocker socket、sudo権限、Dokployの全体管理鍵を使用しません。host設定の反映やコンテナ再配置は管理者の操作として残します。

## 操作の対応

| メソッドとパス | 動作 | 主な応答 |
| --- | --- | --- |
| `GET /health` | readiness。下流全体の疎通検査ではない | `200 {"status":"ok"}`、未設定時503 |
| `GET /settings` | 保存済みの非秘密値と秘密設定済みの印を取得 | values、configured_secrets、pendingフラグ |
| `PATCH /settings` | 許可された項目をprivateファイルへ保存 | `saved: true, applied: false` |
| `GET /status` | 固定した内部サービスの簡易healthを確認 | services、処理中、反映待ち |
| `POST /jobs` | 合成素材を1回、または有限回の連続投入 | 202、id、queued |
| `POST /cdc/demo/{action}` | 準備済み専用CDC検証DBの固定6行を変更 | 202、kind: cdc-source、ジョブID |
| `GET /jobs/{id}` | 素材投入または後続処理の進捗 | status、completed、results、error |
| `POST /jobs/{id}/cancel` | 次の素材バッチを取り消す | 現在の状態 |
| `POST /pipeline/ingest` | 原本とBronzeを取り込む | 202、ジョブID |
| `POST /pipeline/medallion` | 取り込みとSilver/Quarantine/Goldの更新 | 202、ジョブID |
| `POST /pipeline/verify` | 隔離した合成データで基盤を検証 | 202、ジョブID |
| `GET /ai/context` | 対象Goldをsnapshot付きで上限付き参照 | データ・snapshot・変換定義のmetadata |

`/status` はworker、Prometheus、Grafana、n8n、Airflow、Kafka Connect、Kafbat UIの固定URLだけに接続します。任意URLを入力する機能はありません。未起動の任意サービスが `healthy: false` でも、core全体の失敗とは限りません。workerの応答にbooleanの `busy` がある場合は、その実行中表示も返します。旧workerがこの項目を返さない場合は省略します。会社SQLへの認証や会社データの整合性は別に確認します。

## 設定を保存してから反映する

`GET /settings` の `values` には非秘密値だけが入ります。`secret_keys` は秘密欄の名前一覧、`configured_secrets` は値が設定済みの名前一覧です。秘密値そのものや部分マスクを返しません。

```json
{
  "values": {
    "TRINO_PORT": "8082",
    "SOURCE_SQLSERVER_TIMEZONE": "UTC"
  }
}
```

上の形式を `PATCH /settings` に送ります。項目名は設定参照のallowlist、値は単一行ASCII、最大4096文字です。JSONの数値ではなく文字列を送ります。1回に最大128項目を変更できます。未知のkey、改行、無効なportなどはファイルを置き換えず拒否します。秘密欄の空文字は「現在値を保持」です。秘密値を空に戻す作業は、privateファイルを管理者が直接編集します。

設定は同じprivateディレクトリに0600の一時ファイルを作り、CLIと共通の検査後にatomic replaceします。処理実行中は保存を409で拒否します。既存コメントは保持します。

| 応答項目 | 読み方 |
| --- | --- |
| `saved: true` | privateファイルへの保存が完了した |
| `applied: false` | APIから再配置やroot設定変更を実行していない |
| `pending_restart: true` | ファイルの値がAPI起動時の値と異なる |
| `pending_mount_change: true` | path、UID/GID、実行名、volume prefix等の変更があり、mount/配置確認が必要 |

保存後も、素材投入はAPI起動時のDEMO設定、後続処理は起動時のworker tokenを使います。素材ジョブの受付と進捗にも `pending_restart` を付けます。新しいSQL接続先にすぐ切り替わったと解釈せず、反映前は投入を保留して設定を確認します。

`LANDING_DIR` はhostのpathです。実行中のAPIは既存の `/landing` mountを使うため、保存だけで出力先は移りません。`CONTROL_SETTINGS_DIR` と `CONTROL_SETTINGS_NAME` の移動はGUIから拒否し、host側で設定・所有権・mountを揃えて再配置します。DBパスワードと暗号鍵はenv変更だけで既存DBを変更できないため、各製品の正式な更新手順が必要です。[CONFIGURATION.md](CONFIGURATION.md)

CLI管理の環境では、確認した同じprivate設定で `plan` と `up` を実行して反映します。Dokploy管理の環境では、GUIのEnvironmentへ保持した設定とprivateファイルは自動同期しません。管理者が両者を整合させてGit配置を再実行します。`pending_restart: false` はAPI自身の読込時点との一致であり、全コンテナやDokployの設定一致を証明するものではありません。

## 素材だけを投入する

```json
{
  "mode": "batch",
  "batches": 1,
  "seed": 42,
  "orders": 20,
  "days": 7,
  "scenario": "edge",
  "destination": "landing",
  "images": true,
  "allow_sql_write": false
}
```

この操作はCSV/JSON、必要なら画像をランディングへ生成します。素材のmanifest、ready、hashを作ります。取り込み、メダリオン、n8n、Airflowを自動起動しません。後続処理は別の明示操作です。

| 項目 | 指定範囲と判断 |
| --- | --- |
| `mode` | `batch` は1回、`stream` は有限のマイクロバッチ |
| `batches` | 1〜100。batchモードは1のみ |
| `interval_seconds` | 0〜60秒。待ち時間の合計は600秒以内 |
| `seed` | 0〜2147483647。同じ条件の再現に使う |
| `orders` | 1バッチ1〜1000件の基準受注数 |
| `days` | 1〜366日のデータ範囲 |
| `scenario` | `clean` または、改訂・不正値・未参照を含む `edge` |
| `destination` | `landing`、専用テストDBの `sqlserver`、両方の `both` |
| `images` | landing/bothのとき画像素材も生成する |
| `allow_sql_write` | SQLへ書く場合だけ、利用者が明示的にtrueにする |

SQL投入は `DEMO_SQLSERVER_*` の専用DBへ行い、毎回新しい `lh_demo_...` schemaを作ります。SOURCEと同じDB名やsystem DBは拒否します。APIは新規DBや会社側の権限を用意しません。SQLのみの投入で画像を選ぶと422になります。[DEMO-DATA.md](DEMO-DATA.md)

SQLの企業CAを使う場合、CAファイルを `CONTROL_SETTINGS_DIR` の配下に置き、`DEMO_SQLSERVER_CA_FILE` にそのhost側の絶対pathを設定します。APIは同じ相対位置を `/private` へ変換して読みます。たとえばprivate dirが `/srv/lakehouse/private` なら、CAは `/srv/lakehouse/private/enterprise-ca.pem` に置けます。専用dirの外、symlink、regular file以外は拒否します。hostのCLIも同じenvで元のpathを読めます。空または `system` は実行環境の信頼ストアを使うため、hostとコンテナそれぞれのCA登録を確認します。

## CDC用の追加・更新・削除を試す

DB管理者が `scripts/cdc-demo.py create` で専用検証DBを準備し、生成されたprivate情報を対象環境へ反映した後に使う操作です。 Streamlitでは「素材の投入」→「CDCテスト用DBに変更を投入」を開き、明示同意のチェック後に追加・更新・削除を選びます。画面はこの固定APIを呼び、共通ジョブ欄で進捗を表示します。画面からDB作成、CDC有効化、connector変更、任意SQLの実行はできません。

```http
POST /cdc/demo/insert
Content-Type: application/json
```

```json
{"confirm": true}
```

パス末尾は `insert`、`update`、`delete` のみです。本文はJSON booleanの `true` を要求し、数値1、文字列のtrue、未同意、TLS例外やSQL等の追加項目を拒否します。管理者が反映済みの `DEMO_SQLSERVER_*` 管理資格情報、`CDC_SQLSERVER_DATABASE`、`CDC_DEMO_OWNERSHIP_TOKEN` を使います。SOURCEのDB名も受け取り、同名DBを拒否します。所有権tokenは設定済みの印だけ表示し、APIから読み戻せません。

専用DB名は `lh_cdc_demo_` に16文字のhexが続く形式で、さらにDB内の所有権markerがprivate tokenと一致する必要があります。名前だけを似せたDBや会社の業務DBを投入対象にできません。CA/証明書名を検証する通常TLSで接続し、CLIの隔離試験用TLS例外はAPIへ公開しません。企業CAは前節のDEMO CAと同じprivate dir内の配置を使います。

通常は追加→更新→削除の順に1回ずつ確認します。対象は6表の固定した連携レコードで、各操作は6行を1 transactionで変更します。追加済み行への再追加や、存在しない行の更新・削除等の前提違反はrollbackします。エラーを見て同じボタンを連打せず、既にどこまでcommitしたかをジョブ結果から確認します。

受付は202、`kind: "cdc-source"` とジョブIDを返します。`GET /jobs/{id}` の `cdc_action` と結果を確認します。

```json
{
  "action": "update",
  "changed_rows": 6,
  "source_only": true
}
```

この成功はソースDBへのcommitを示します。KafkaやIcebergまでの到着を保証する応答ではありません。APIからworkerやpipelineを呼び出さず、稼働中のDebezium/consumerが変更を読む場合は、その設定済みCDC経路を別に確認します。実行中のDMLはtransactionを保つため途中取消できず、取消要求は409です。

Kafbat UIは既定で `http://localhost:8084/` にあり、broker、topic、consumer lag、Kafka Connectの状態・設定を確認する管理用画面です。CDC Composeではloopbackへbindします。アプリ利用者のデータ閲覧画面とは別なので、共有入口には会社標準の認証と接続制限を用意します。固定6行のソース変更→connector→topic→consumer→派生表を分けて照合してください。

## ジョブの進捗と取消

受付は202でジョブIDを返します。ブラウザーで処理完了まで長時間待たず、`GET /jobs/{id}` で確認します。

```json
{
  "id": "012345abcdef",
  "status": "queued",
  "kind": "source",
  "pending_restart": false
}
```

状態は `queued` → `running` → `succeeded`、`failed`、`cancelled` のいずれかです。`completed` は完了バッチ数、`batches` は予定数です。素材の結果にはmanifestの相対path、表ごとの行数/hash等を含みます。後続処理の結果は固定の件数・成功フラグだけに限定します。

素材生成と後続処理は共通mutexで1件ずつ実行します。重複した受付は409です。有限streamはバッチ間で取消と600秒の実行期限を確認します。実行中の1バッチを強制切断する方式ではなく、SQL transactionやファイル公開を途中で壊さない境界で止めます。既に生成・投入された素材は取消で削除しません。

pipelineは固定workerへ別の明示操作を送る非同期ジョブです。 受付前に固定healthを読み、`busy: true` または `blocked: true` なら409で新しいジョブを作らず拒否します。healthを確認できない場合は503です。事前確認と実行の間に状態が変わる場合も、worker自身の既存ロックが実行を排他します。既に開始したpipelineの取消は409で拒否します。worker側の実行が続いている可能性がある場合は、状態を確認し、同じボタンを繰り返しません。Trinoの取消が確認できずworkerがblockedになった場合は [DEPLOYMENT.md](DEPLOYMENT.md) の復旧手順を使います。

APIからworkerへの応答待ちがタイムアウトすると、ジョブは `failed` / `WORKER_RESULT_UNKNOWN` を返します。これはworkerの処理停止を意味せず、処理が継続している可能性があります。「状態」のworkerの `busy` と実際の結果を確認し、再実行を判断します。APIは自動再試行しません。

素材・CDC素材・pipelineのジョブ履歴は1プロセスのメモリ内に最大20件です。再起動すると履歴・進捗は失われます。複数Uvicorn workerで起動するとmutexと履歴を共有できないため、現構成は1workerで使います。長期監査や復旧の根拠は投入manifest・DB・メダリオン側の実体で確認します。

## AI向けの参照データ

`GET /ai/context?dataset=all&limit=100` は、固定したGoldの `production` / `sales` を参照します。`dataset` は `all`、`production`、`sales`、`limit` は表ごと1〜500行です。各表のmainが指すsnapshot IDを取得し、その版を固定して読みます。`exporter_reference_sql_sha256` はexport時checkoutの参照SQL、`exporter_code_sha256` はadapterコードのhashです。実snapshotを生成したSQLの版を保証する値ではありません。表間で同一transactionへ固定するものではありません。

任意SQL、外部URL、モデルの呼出先を入力する欄はありません。AIへの自動送信や業務判断の自動実行も行いません。件数上限があるJSONなので全件集計とは扱わず、データ鮮度・粒度・業務上の定義を利用者が確認します。Goldがない場合は409 `GOLD_UNAVAILABLE` となり、先にメダリオンを実行します。[AI-READINESS.md](AI-READINESS.md)

## エラーと確認先

エラーは `code` と日本語の `message` を返します。秘密値、接続URI、SQLやDBドライバの生エラーを応答へ含めません。入力エラーも送信したpasswordをそのまま返さない形式です。

| status / code | 次の操作 |
| --- | --- |
| 401 `AUTH_REQUIRED` | CONTROL_API_TOKENと、API起動時のtokenが一致するか確認 |
| 404 `JOB_NOT_FOUND` | IDとAPI再起動の有無を確認。失われた履歴を実行失敗とは即断しない |
| 409 `OPERATION_RUNNING` | 進捗欄を更新して、現在の処理を待つ |
| 409 `PIPELINE_RUNNING` | 開始済みpipelineは完了を待つ |
| 409 `WORKER_NOT_IDLE` | workerが実行中か確認待ち。状態と結果を確認してから再実行 |
| 409 `CDC_DEMO_RUNNING` | 開始済みの専用DB transactionは完了を待つ |
| 422 `CDC_DEMO_NOT_PREPARED` | 専用DBの準備結果、所有権token、DEMO管理資格情報、SOURCEとの分離を確認 |
| 422 `INVALID_REQUEST` | Swaggerの型・範囲・SQL書込同意を確認 |
| 422 `INVALID_SETTINGS` | key、形式、port、0600/0700、固定したprivate設定位置を確認 |
| 422 `DEMO_CA_UNAVAILABLE` | 専用private dir内のCAとsymlinkの有無を確認 |
| 503 `WORKER_UNAVAILABLE` | workerのhealth確認に失敗。接続・起動状態を確認し、新しいジョブが受け付けられていないことを確認 |
| 503 `SETTINGS_UNAVAILABLE` | private mount、設定ファイル名、権限、共通CLI検査を確認 |
| ジョブ内 `SOURCE_GENERATION_FAILED` | 専用DBの認証/TLS/権限、landing容量と所有者を確認 |
| ジョブ内 `CDC_DEMO_FAILED` | DB内所有権marker、固定行の現在状態、管理権限、通常TLSを確認 |
| ジョブ内 `WORKER_RESULT_UNKNOWN` | 応答は不明でもworker処理が継続している可能性。実行中表示と結果を確認してから再実行 |
| ジョブ内のその他の `WORKER_*` / `PIPELINE_FAILED` | workerのhealthと処理中状態を確認してから再試行を判断 |

APIのOpenAPI生成と単体試験は、認証、秘密値非表示、設定保存、source-only境界、取消、重複拒否、固定接続先、AI参照上限を対象にしています。会社DB・proxy・CA・共有クライアントでの成功は導入先の受入で別途記録します。

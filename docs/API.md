# GUI操作APIとSwagger

<!-- doc-purpose:start -->
> **役割:** Swaggerの認証と固定APIを使い、設定保存・外部入力の後続処理・非同期job・参照を行う。
>
> **対象読者:** データ利用担当 / 開発・設計担当
>
> **読むタイミング:** GUIと同じ操作をAPIクライアントや自動化から呼び出すとき。
>
> **前提となる資料:** [初めて構築する人の一本道ガイド](<START-HERE.md>) / [GUIで外部データの処理を確認する](<DEMO-GUI.md>)
>
> **次に読む資料:** [AIクライアントからMCPで操作する](<MCP.md>) / [n8n・Airflow・Grafanaを画面から使う](<GUI-WALKTHROUGHS.md>)
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

確認日: 2026-11-10（JST）。初回配置・作業端末・権限は [START-HERE.md](START-HERE.md) で準備します。以下の `up` はCLI管理の環境だけで使います。Dokploy管理では同じprivate設定とComposeサービスのEnvironmentを揃え、9ファイルを使ってDeployします。[0.31.0の配置手順](DOKPLOY.md)


このAPIは、設定の保存、外部データの後続処理、Goldの参照を担当します。FastAPIのSwagger UIから入力項目と応答形式を確認できます。初めて操作する場合は [DEMO-GUI.md](DEMO-GUI.md)、会社導入は [COMPANY-ROLLOUT.md](COMPANY-ROLLOUT.md) を先に読みます。

## 起動と認証

private設定に `COMPOSE_PROFILES=demo` を追加し、既存のprofileを使っている場合はカンマで併記します。`CONTROL_SETTINGS_DIR` はこの環境専用の0700ディレクトリ、`CONTROL_SETTINGS_NAME` はその中の0600設定ファイル名です。生成器は生成先から両方を設定します。GUIの実行UID/GIDが、privateディレクトリとランディングへアクセスできることを確認します。

**Bash / Zsh共通**

```bash
export LAKEHOUSE_ENV_FILE="$HOME/.config/lakehouse-foundation/site.env"
./scripts/lakehouse.sh plan
./scripts/lakehouse.sh up
```

| 入口 | 既定のURL | 用途 |
| --- | --- | --- |
| 操作画面 | `http://localhost:8501/` | Streamlitの設定・受信後の処理・進捗確認 |
| Swagger UI | `http://localhost:8502/docs` | APIの入力形式を確認して実行 |
| OpenAPI | `http://localhost:8502/openapi.json` | APIクライアント用の仕様 |
| readiness | `http://localhost:8502/health` | 設定と認証tokenが読み込めたか |

既定のhost bindはloopbackです。別端末では会社標準のSSHポート転送等を使います。Streamlitの画面自体に利用者別ログインを実装していないため、共有する場合は入口の認証・HTTPS・接続元制限を先に準備します。SwaggerとOpenAPIは認証なしで仕様を表示できますが、業務操作APIはすべてBearer認証を要求します。`/health` の応答に秘密情報は含めません。

Swaggerの **Authorize** にprivate設定の `CONTROL_API_TOKEN` を入力します。HTTP Bearerの欄にはtoken本体を入力し、画面がヘッダーを作ります。tokenをURL、スクリーンショット、workflowのJSONへ含めません。Streamlitはサーバー側でtokenを読み、内部APIへ送ります。ブラウザーから既存tokenを読み戻す機能はありません。

SwaggerのJavaScript・CSS・faviconはimage内から配信します。ブラウザーから外部CDNや外部schema validatorへ接続せず、認証tokenの永続保存も無効にしています。依存ファイルの取得とchecksum確認はimage build時に行うため、閉域環境では許可したbuild経路を用意します。

このAPIはDocker socket、sudo権限、Dokployの全体管理鍵を使用しません。host設定の反映やコンテナ再配置は管理者の操作として残します。

## Swaggerで受信後の処理を確認する

1. 認証を設定して `GET /capabilities` と `GET /status` を確認します。
2. 外部入力の配送を確認してから `POST /pipeline/medallion` を一度実行します。
3. 202のジョブIDを `GET /jobs/{id}` で照会します。202は完了ではありません。

## 操作の対応

| メソッドとパス | 動作 | 主な応答 |
| --- | --- | --- |
| `GET /health` | readiness。下流全体の疎通検査ではない | `200 {"status":"ok"}`、未設定時503 |
| `GET /settings` | 保存済みの非秘密値と秘密設定済みの印を取得 | values、configured_secrets、pendingフラグ |
| `PATCH /settings` | 許可された項目をprivateファイルへ保存 | `saved: true, applied: false` |
| `GET /status` | 固定した内部サービスの簡易healthを確認 | services、処理中、反映待ち |
| `GET /jobs/{id}` | 外部入力の後続処理の進捗 | status、completed、results、error |
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
    "TRINO_PORT": "8090",
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

## PoCポータルの読み取りAPI

すべてBearer認証を使います。任意SQL、任意URL、任意ファイルpathは入力に取りません。

| API | 返すもの・上限 |
| --- | --- |
| `GET /capabilities` | version、固定APIのread/write分類、必要profile、目的 |
| `GET /services` | 適用済みprofile/必要設定/固定healthから入口stateを区別。URLに秘密を含めない |
| `GET /catalog` | Silver6契約とGold2の列・型・説明・粒度・業務キー・公開の由来 |
| `GET /catalog/preview?dataset=all&limit=20` | 既存AI exporterと同じ固定Gold snapshot参照。limitは1〜500 |
| `GET /quality` | 固定6表のBronze/Silver/Quarantine件数。任意SQLなし、最大6行、期限付き取消 |
| `GET /jobs?limit=20` | 最新最大20件。persistent=false、再起動で消失、自動再実行なし |
| `GET /help` | 固定文書slugと同梱図ID、利用可能か |
| `GET /help/{slug}` | allowlist内のMarkdown。最大256KiB、symlink拒否 |
| `GET /help/assets/{id}` | 同梱SVG2種。最大512KiB、path指定不可 |
| `GET /network/config?use_saved=false` | オフラインのproxy/NO_PROXY/CA有無。秘密値を返さない |
| `POST /network/checks` | 固定HTTPS宛先のみを並列診断。設定反映・DB変更なし |

`/services` のstateは `not_enabled` / `unconfigured` / `available` / `unreachable` です。health未到達を未導入と断定しません。previewとqualityは表ごとの読取りで、全表の同一時点を保証しません。data catalogは会社SQLの接続先やprivateの表対応を含みません。

接続診断の本文は `{"targets":["pypi","maven","npm","github"],"use_saved":false}` です。重複・未知target・追加の任意URLを拒否します。既定は起動時のapplied設定で、use_saved=trueはprivateファイルの保存値を試します。応答のconfiguration_scopeとpending_restartで区別し、診断成功を反映完了とは扱いません。

企業CAのprivate入力はCORPORATE_CA_FILEだけです。診断では設定dir配下host pathを/privateへ安全に対応付けます。範囲外・不在・symlinkは422 NETWORK_CA_UNAVAILABLEです。runtimeのSSL_CERT_FILE等はimage内の固定bundleを使うため、設定APIから変更しません。[POC-PORTAL.md](POC-PORTAL.md)

## 後続処理の進捗と履歴

外部から届いたデータの取り込み、変換、SQLバッチ、復旧をジョブIDで照会できます。`GET /jobs` の最大20件はAPIプロセス内の履歴です。二重受付は409で拒否し、途中取消APIはありません。応答がタイムアウトしてもworkerは動いている場合があるため、`busy`、`blocked`、実データの状態を確認してから再実行を判断します。

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
| 409 `WORKER_NOT_IDLE` | workerが実行中か確認待ち。状態と結果を確認してから再実行 |
| 422 `INVALID_REQUEST` | Swaggerの型・範囲・処理への同意を確認 |
| 422 `INVALID_SETTINGS` | key、形式、port、0600/0700、固定したprivate設定位置を確認 |
| 503 `WORKER_UNAVAILABLE` | workerのhealth確認に失敗。接続・起動状態を確認し、新しいジョブが受け付けられていないことを確認 |
| 503 `SETTINGS_UNAVAILABLE` | private mount、設定ファイル名、権限、共通CLI検査を確認 |
| ジョブ内 `WORKER_RESULT_UNKNOWN` | 応答は不明でもworker処理が継続している可能性。実行中表示と結果を確認してから再実行 |
| ジョブ内のその他の `WORKER_*` / `PIPELINE_FAILED` | workerのhealthと処理中状態を確認してから再試行を判断 |

APIのOpenAPI生成と単体試験は、認証、秘密値非表示、設定保存、外部入力専用の境界、重複拒否、固定接続先、AI参照上限を対象にしています。会社DB・proxy・CA・共有クライアントでの成功は導入先の受入で別途記録します。

## 採用版の根拠

API実装は [api.py](../infra/demo/api.py)、状態・上限は [jobs.py](../infra/demo/job_manager.py) と [requirements](../infra/demo/requirements.txt) が正本です。FastAPI **0.162.2** の [HTTP Bearer実装](https://github.com/fastapi/fastapi/blob/0.162.2/fastapi/security/http.py) と [Swaggerのself-host手順](https://github.com/fastapi/fastapi/blob/0.162.2/docs/en/docs/how-to/custom-docs-ui-assets.md) を照合しています。Swagger UI **5.34.1** はbuild時のarchive hashを検証し、ローカルassetを配信します。frameworkの標準認証機構と、本基盤の共有token・mutex・入力allowlistは別の実装です。[版一覧と文書監査](REFERENCES.md)

## API利用者の実務上の判断: 受付と完了を分ける

APIを初めて使う人は、Swaggerからread操作を試してから後続処理の依頼へ進みます。連携プログラムの担当者は、HTTPの受付・jobの終了・データの照合を3つの段階として記録してください。操作用PCでAPIへ到達する経路と、API内部からworkerへ接続する経路も別です。

| 確認した事実 | 判断できること | まだ判断できないこと |
| --- | --- | --- |
| `/health` が200 | APIが起動時設定を読み込めた | worker、Trino、会社DBが正常か |
| POSTが202とidを返した | jobを受け付けた | 変換の成功 |
| `status=succeeded` | その種類のjobが完了した | 実データや会社数値の妥当性 |
| `pending_restart=false` | API起動時と保存設定が一致する | Dokployや全container設定の一致 |
| Gold previewが返った | 指定表の固定snapshotを参照できた | 全行・全表を同時点で集計したか |

### 自動連携が保持する最小情報

受付時刻、操作名、返されたjob ID、入力の非秘密条件、完了status、manifestまたは結果の参照を保持します。token、SQL password、private env全文をjobログへ書きません。GETのpoll間隔を決め、同じPOSTを結果確認の代わりに繰り返さないでください。

HTTP timeoutでjob IDを受け取れなかった場合、既に受付/commitされている可能性があります。現在のAPIに、任意の業務キーを使ったidempotency-keyや永続的な受付照会を実装しているわけではありません。直近履歴、対象manifest、worker状態から実体を確認し、再POSTの要否を判断します。

| 種類 | 取消・再試行の考え方 |
| --- | --- |
| pipeline | workerへ依頼済みなら停止を推測しない。busy/blockedと実体を確認 |
| read API | 表が未作成、認証、接続、上限のどれかを調べる。書込み操作で修復しない |

## 新しいAPIを足すときの変更箇所

| 責務 | 実装の場所 | 変更時の確認 |
| --- | --- | --- |
| 入力型・認証・受付 | [api.py](../infra/demo/api.py) | 余分なkey、上限、秘密を返さないvalidation |
| job排他・履歴 | [job_manager.py](../infra/demo/job_manager.py) | 受付競合、途中成功、再起動、結果不明 |
| catalog/状態/ヘルプ | [poc_api.py](../infra/demo/poc_api.py) | 固定allowlist、サイズ上限、symlink拒否、反映前後 |
| workerの実操作 | [admin.py](../scripts/admin.py) | 対応action、Trino失敗/取消、blocked、再実行 |

API仕様は `openapi.json` をクライアント生成の入力にできますが、認証や意味上の境界を自動解決するものではありません。変更後は認証なし、未知入力、409競合、成功、timeout/不明結果、秘密値非表示を試し、Swaggerと [capabilities](../infra/demo/poc_catalog.py) のread/write分類も揃えます。現在の1worker/メモリ内履歴を複数workerや永続queueへ広げる場合は、排他・永続化・再開契約を先に設計します。[変更手順](CUSTOMIZATION.md)

## 固定SQL sourceの操作

`GET /sql-source/status` はSOURCEの読取権限と6表の状態を観測します。書き込みは行いません。[安全条件・準備](SQLSERVER-FIXED-DEMO.md)


### SQL snapshotの明示処理

`POST /sql-source/batches/snapshot` は `{"confirm": true}` だけを受け付ける非同期jobです。SOURCE readerで固定6表を読み、共有Landingの検査済みJSONをworkerへ渡します。workerへ送るのは固定action・batch ID・観測時刻だけで、SQL認証は含みません。全6表・派生SQL・件数照合後に完了し、timeoutは結果不明とします。[再実行と切り戻し](SQLSERVER-FIXED-DEMO.md#5-フルスナップショットを取り込む)

### 増分とcheckpoint

`POST /sql-source/batches/incremental` はstrictな同意だけを受け付け、6表のbaselineと別control writerを必要とします。snapshotはcontrol未設定でも利用できますが、その場合は `checkpointed=false` です。増分の成功は `checkpointed=true` を含む応答で確認します。抽出窓件数と変更件数・反映後件数を区別し、物理DELETEや窓を超える遅延を完全検出したとは扱いません。[順序と限界](SQLSERVER-FIXED-DEMO.md#6-watermark増分バッチ)

### SQLバッチ・固定CDCの観測と再構築

すべて既存のBearer認証を必要とし、Swagger UIにも表示します。接続先、認証、任意SQL/table/URLを要求パラメーターにしません。未準備・未観測を成功やlag=0で埋めません。

| endpoint | 入力 | 応答の意味 |
| --- | --- | --- |
| `GET /sql-source/checkpoints` | 追加入力なし | 別control資格情報で実watermarkをSELECTだけで照会。configured/baseline_completeと固定6表の位置。読取readerへ書込み権限を追加しない |
| `GET /sql-source/pipeline-status` | 追加入力なし | SQL分析表の件数、batch完了台帳、未完の適用状態。SQL checkpointとは別に確認 |
| `GET /sql-source/cdc/status` | 追加入力なし | connector/task、内部consumerの観測時刻・offset/lag・rebuild状態、Raw/Current件数。driver/SQL例外の原文は出さない |
| `POST /sql-source/cdc/rebuild` | strictな `{"confirm": true}` のみ | 202と32桁hex request_id、status=queued、completed=false。同じIDの状態をGETで追跡し、受付を処理完了にしない |
| `GET /sql-source/preview` | `origin=batch|cdc`、`dataset=production|sales`、`limit=1..100` | allowlistのGoldだけを個別snapshot固定で参照。decimalは文字列・日付はISO。未知の列は公開しない |

consumerの管理APIはCompose内部8001で、hostへ公開しません。GUI APIが固定URLへWORKER_TOKENを使って仲介し、tokenやtopicの実値を返しません。ブラウザから直接consumerへ接続する必要はありません。再構築の条件・非原子的な保存境界・rollbackは [Current Stateの手順](SQLSERVER-FIXED-DEMO.md#8-current-statereplaygui監視) を参照します。

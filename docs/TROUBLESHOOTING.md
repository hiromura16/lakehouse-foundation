# 症状から確認する

最初に対象の環境、配置commit、profile、発生時刻、ジョブIDを確認します。`site.env`、展開済みCompose、DB接続文字列、token、生のドライバー例外を公開ログへ貼り付けません。データ削除や再生成を初期対応にせず、観測した状態と保存先を確認します。

## まず見分ける

| 観測 | 判断と次の操作 |
| --- | --- |
| 画面自体が開かない | bind・ポート・経路・コンテナhealthを確認。別端末のlocalhostは別端末自身 |
| 画面は開くがAPIが使えない | demo-api health、token、private設定のmountと権限を確認 |
| 素材投入が失敗 | ジョブIDとstatusを確認。完了したbatch／manifestを調べ、新規投入を重ねない |
| 素材はあるがGoldがない | 素材生成と処理は別。「処理・検証」または別の正規実行入口を確認 |
| コンテナはhealthyだが件数が違う | source manifest、Bronze、Silver、quarantine、Goldの順に照合 |
| 更新後に初期画面へ戻った | 空volumeや別prefixへ向いていないか確認。再初期化を進めない |

CLIの確認は、配置と同じprivate設定で行います。

```bash
export LAKEHOUSE_ENV_FILE="/absolute/private/site.env"
./scripts/lakehouse.sh plan
./scripts/lakehouse.sh status
```

## GUI・APIのエラー

| 症状／HTTP | 観測するもの | 判断・操作 |
| --- | --- | --- |
| `401` | CONTROL_API_TOKENの設定有無、UI/APIの配置時env | 同じ環境の同じtokenへ揃えて再配置。既存tokenをブラウザーへ表示させない |
| `422` | 入力の範囲・型、設定項目、DB書き込み許可 | 指図数1～1000、日数1～366、seed範囲、modeと回数、待機合計600秒以内を確認 |
| `409`・workerが実行中／確認待ち | worker healthのbusy／blocked、前回の処理結果 | 新しいジョブは受付前に拒否されている。完了または確認を待つ |
| `409`・別の操作が実行中 | 現在のsource／pipelineジョブとstatus | 終了を待つ。API内の排他で待たされている状態へ新規要求を重ねない |
| `404`・ジョブなし | API再起動の有無、入力したID | ジョブ履歴は永続の監査DBではない。残ったmanifestを確認して判断 |
| `500` | 固定エラーcode、health、所有者・空き容量 | 生例外を公開せず、管理者が非公開ログで原因を確認。即座に再投入しない |
| `503`・設定未準備 | 専用dir0700、env0600、ファイル名、APIユーザー | bind元の実ファイルとCONTROL_SETTINGS_DIR/NAMEを確認 |
| `failed` / `WORKER_RESULT_UNKNOWN` | workerの実行中表示と実際の結果 | APIの応答待ち時間を超えた状態。workerが処理継続中の可能性があるため、自動／即時の再実行をしない |
| `failed` | 完了回数、最後のmanifest、入力先状態 | 成功済みの素材は残り得る。部分成功を確認してから新しいrunを作る |
| `cancelled` | 完了回数、停止要求を出した時点 | 完成済みbatchは正常に残る。取消で既存データが削除されることはない |
| `queued`／`running`が長い | SQL接続のタイムアウト、ジョブ進捗、サービス資源 | 現在batchの完了を待つ。API再起動で強制停止する前にDB側の状態を確認 |

「状態」のworkerにbooleanの `busy` があれば実行中表示として使います。APIのジョブがタイムアウトでfailedになってもworkerの処理が止まったとは限りません。worker自身のhealthで `blocked: true` の場合はTrinoの取消確認待ちであり、残存queryを管理者が確認してから復旧します。新しいpipelineはbusy／blockedの間409で拒否します。pipelineの途中取消は提供していません。[API.md](API.md#ジョブの進捗と取消)

「保存しました」なのに反映しない場合は、`pending_restart`／mount変更の表示を確認します。設定保存はファイル更新までです。現在のAPI token、worker、UID、mount等は再配置後に確認します。Dokployのenvとprivateファイルを別の値のままにしません。

## ランディングが取り込まれない

| 観測 | 判断・操作 |
| --- | --- |
| `.ready`がない | コピー完了後にマーカーを生成。転送途中で作らない |
| SHA-256が一致しない | コピー中の変更・不完全転送・古いmarkerを確認。完成した原本に対して再生成 |
| hashは正しいがスキップされる | 同じ相対pathとhashの処理済み判定かを確認。再実行の重複防止として正常な場合がある |
| 元ファイルが16MiBを超える | 現行workerの1ファイル上限。入力を分割し、sources契約と命名を維持 |
| 100000行を超える・列不一致 | ソース列と型の原本契約を確認。上限回避のため検証を無効化しない |
| CSV／JSONは原本だけ保存され、業務表に入らない | sources.jsonの固定名、またはGUIの正式な増分命名か確認 |
| Permission denied | LANDING_DIR、所有者・グループ、WORKER_UID/GID、共有ユーザーを確認 |
| Sambaでは書けるがGUIでは書けない | APIのrw mountと同じUID/GIDを確認。worker側をrw化して解決しない |

手動ファイルの完成後に、例えば次を実行します。

```bash
python3 scripts/mark-ready.py /srv/lakehouse/landing/csv/erp_products.csv
```

GUIの正式な増分名は `csv/<table>__<hex12>_<4桁>.csv` またはJSON側の対応名です。任意にsuffixを付けるだけで全ファイルが業務表へ認識されるわけではありません。詳細は [DEMO-GUI.md](DEMO-GUI.md) と [MEDALLION.md](MEDALLION.md) を参照します。

## SQL Serverへダミー投入できない

専用の既存DEMO DB、必要なCREATE SCHEMA／CREATE TABLE／INSERT権限、TLS証明書・hostname、CAのコンテナ内pathを順に確認します。会社参照元の`SOURCE_SQLSERVER_DATABASE`と同名のDBやシステムDBは拒否されます。GUIの書き込み許可を明示しても、この拒否は解除されません。

SQLだけの投入ではCSV／JSONファイルは生成しません。6つのnvarchar staging表とmanifestを確認します。既存の業務schemaへの投入や、DB自体の作成を期待しないようにします。証明書検証を無効にして接続成功と扱いません。

会社参照元への`source-check`成功はTCPの到達性までです。SQL認証・SELECT・証明書・CDCの成功と区別します。[SOURCE-SQLSERVER.md](SOURCE-SQLSERVER.md)

## メダリオン・Gold・AI

| 症状 | 観測・判断・操作 |
| --- | --- |
| Silver件数が原本より少ない | 主キー最新版への重複排除とquarantine件数を確認。単純に欠損とは判断しない |
| Goldが空／作成されていない | 素材投入だけで終了していないか確認し、メダリオンを明示実行 |
| Goldの一部だけ更新 | 表ごとの原子的置換であり全表transactionではない。原因修正後に同じ入力で再実行 |
| GUIを複数回実行すると数量が増える | GUIは別IDの独立素材を追加する仕様。単一fixtureの再実行期待値と区別 |
| AI contextが`409` | Goldの存在とsnapshotを確認。先にメダリオン、次にcontext取得 |
| AIの数字が全件集計と違う | contextのrow_limitとcomplete_dataset=falseを確認。最大500行の抜粋を全件合計にしない |
| snapshotが表ごとに異なる | 各表を独立に固定する契約。全表共通snapshotと扱わない |

AI context取得はモデル実行や外部送信ではありません。根拠JSONの表名・snapshot ID・export側の参照SQL／adapterのhashを保持し、モデルへ渡すデータ範囲は別に決めます。このhashをsnapshot生成版の証明とは扱いません。[AI-READINESS.md](AI-READINESS.md)

## Spark・Airflow・n8n

| 症状 | 観測・判断・操作 |
| --- | --- |
| Sparkが常駐していない | spark-checkは一時ジョブ。通常upでreplicas=0は意図した動作 |
| SparkでClassNotFound／NoSuchMethod | Spark line・Scala binary・Iceberg runtime JARの一致、AWS bundleの重複を確認 |
| JARのhash照合失敗 | lockと正規配布物を確認。照合を飛ばして進めない |
| Sparkは書けるがTrinoで読めない | 同じREST catalog／warehouse／S3 endpointを参照しているか、表formatを確認 |
| AirflowのPythonOperator import失敗 | Airflow 3のstandard providerと公式constraintsを確認。古いimport例を混ぜない |
| Airflowへログインできない | state volume内の生成済み管理者passwordを導入先で確認。再生成せず既存stateを保持 |
| Airflow DAGがworkerへ接続できない | lakehouse_worker Connection、token、内部URL、HTTP providerを確認 |
| n8n workflowが`401` | Header AuthのAuthorization/Bearerとprivate worker tokenを確認 |
| n8n credentialsを復号できない | 元のDBとN8N_ENCRYPTION_KEYの組み合わせを確認 |

正しい版・座標・固定JARは [COMPATIBILITY.md](COMPATIBILITY.md) を参照します。Spark本体だけ、Airflow Providerだけの独立更新をせず、対応するスモークを実行します。Airflowの初期passwordが起動ログに含まれる場合があるため、生ログを公開しません。

## プロキシ・image取得・証明書

| 観測 | 判断・操作 |
| --- | --- |
| image pullだけ失敗 | Docker daemon側proxy／CA／認証を確認。コンテナenvだけでは反映しない |
| build内pip／curlだけ失敗 | build args、企業CA、配布元への許可を確認 |
| 実行中Python／SQLだけ失敗 | そのクライアントのproxy／CAと内部NO_PROXYを確認 |
| Java系Spark／Kafka／Debeziumだけ失敗 | JVM truststoreとSDKのproxy設定を確認。Python成功から類推しない |
| 内部service名への通信が外へ流れる | NO_PROXYと小文字版、サービス名・内部hostを確認 |
| GUIだけAPIへ接続しない | DEMO_API_URLとtokenを確認。UI内部API通信は外向きproxyを使わない |

会社CA・proxy認証は導入先の方針に合わせます。CAの信頼設定を入れることと、証明書検証を止めることを混同しません。[PROXY.md](PROXY.md)

## CDCが進まない・再開できない

CDCではソースDB、Debezium、Kafka、schema history、offset、取り込み先の状態を分けて確認します。まずconnector statusとcapture対象・権限・TLS・最終進捗を観測し、再登録や新規snapshotを繰り返しません。

再開位置とschema historyを失うと、同じデータの再取得や欠損の判断が必要になります。Connectのoffset/config/status topic、DB schema history、bridgeのcheckpointを一組として扱い、初期対応でtopicやoffsetを消しません。schema変更時は対象表と記録したschemaの整合を確認します。[Debezium SQL Server公式資料](https://debezium.io/documentation/reference/stable/connectors/sqlserver.html)

SQL ServerではCDCのchange table／LSNの保持とcapture/cleanupの状態をDB管理者と確認します。PostgreSQL系CDCを将来追加する場合はWALとreplication slotの保持が別の確認事項です。SQL Serverの問題をWAL設定で解決しようとしません。停止可能時間はソース側の保持期間と実際のlagから決めます。[Debezium PostgreSQL公式資料](https://debezium.io/documentation/reference/stable/connectors/postgresql.html)

CDCの対象・変換・削除イベント・再実行の保証は、導入したconnectorとbridgeの実装・専用手順に従います。この一般的な切り分けだけで会社DBのCDC実機検証が済んだとは扱いません。

## データが消えたように見える・復旧が必要

1. 変更後のプロジェクト名・DATA_VOLUME_PREFIX・実volume名を照合します。
2. 誤った空volumeへ向いている場合は新規初期化を止めます。元volumeと暗号鍵を保持します。
3. 正しい実体へ戻す場合も、全writerを停止して一つの配置へ切り替えます。
4. バックアップ復元が必要なら、元環境と異なる名前・ポート・volumeへ復元し検証します。

GrafanaのGUI変更はGrafana DBが正本です。初回テンプレートを再importして復旧する前に、そのDBへ正しくmountできているか確認します。[GRAFANA.md](GRAFANA.md)、[OPERATIONS.md](OPERATIONS.md)


SSRFの設定値、内部workerの例外、NO_PROXYとの違い、GUI保存と再配置、許可・拒否の受入は [SSRF.md](SSRF.md) を参照してください。

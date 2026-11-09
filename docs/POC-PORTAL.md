# PoC操作ポータル

操作画面を、素材投入だけでなく、サービスの入口、データの意味、読み取り確認、接続診断、履歴、同梱ヘルプから使えるようにしています。既定URLは `http://localhost:8501/`、操作APIとSwaggerは `http://localhost:8502/docs` です。実行する操作と、その結果を確認する操作を分けます。

## 最初に開く

private設定で `demo` profileを有効にし、既存の秘密値を保持して配置します。会社のproxyとCAは [PROXY.md](PROXY.md)、全体の順序は [COMPANY-ROLLOUT.md](COMPANY-ROLLOUT.md) を参照してください。

```bash
export LAKEHOUSE_ENV_FILE=/absolute/private/site.env
./scripts/lakehouse.sh plan
./scripts/lakehouse.sh up
```

追加サービスは必要なprofileだけを選びます。監視は `monitoring`、n8nは `automation`、Airflowは `orchestration`、Kafka管理画面は `cdc`、BIは `bi`、Notebookは `notebooks`、ベクトルDBは `vector`、lineageは `lineage` です。既存private設定では、追加された設定項目と容量を確認してから再配置します。ポータルからDockerの権限を使って自動インストールする仕組みではありません。

## 入口カードの読み方

| 表示 | 根拠 | 次の操作 |
| --- | --- | --- |
| profile未選択 | API起動時のprofileに含まれない | 必要性と資源を確認し、設定へ追加して再配置 |
| 必要設定が未完了 | profileは選択済みだが必須設定が空 | カードに表示されたkeyをprivate設定で確認 |
| 稼働を確認 | 固定した内部healthへの到達を確認 | 「開く」から各サービスのnative画面へ |
| 停止・起動待ち・未到達 | 設定はあるがhealthを確認できない | コンテナ状態、起動待ち、認証、内部経路を確認 |

未到達だけで未導入と断定しません。ポータルはDocker socketを持たず、HTTPによる到達確認と設定上の有効化を区別します。healthが成功しても、業務数値やバックアップの受入は別の確認です。

表示するURLにはパスワード、Jupyter token、Qdrant API keyを入れません。各製品のログインはその製品の認証で行います。localhostはブラウザーを開いた端末自身なので、遠隔利用では必要なportを会社標準の方法で転送します。PoC追加サービスとCDCのhost portはloopbackへ限定しています。

| サービス | profile | host portの既定 | 開くもの |
| --- | --- | --- | --- |
| Swagger | demo | 8502 | 固定APIの仕様・試行 |
| Trino | core | 8080 | SQL実行状況 |
| Lakekeeper | core | 8181 | Icebergカタログ |
| n8n | automation | 5678 | workflow編集・実行 |
| Grafana / Prometheus | monitoring | 3000 / 9090 | 監視画面・指標 |
| Airflow | orchestration | 8081 | DAGと実行結果 |
| Kafbat | cdc | 8084 | Kafka topic・lag・Connect |
| Superset | bi | 8088 | Goldの探索・BI |
| JupyterLab | notebooks | 8888 | Notebook分析 |
| Qdrant | vector | 6333 | vector collection |
| Marquez | lineage | 3001 | 登録済みlineage |

設定の保存後に `pending_restart` がある場合、カードは起動時の適用済み設定を使い続けます。新しいportやprofileを保存しただけでは入口が移りません。再配置と製品の初期化、データ登録はそれぞれ確認します。

## データを理解してから参照する

「データ」タブは、公開の6つのSilver契約と2つのGoldについて、列の型・説明、粒度、業務キー、由来を表示します。会社の接続先、実表名の対応JSON、パスワードをデータカタログへ返しません。

Silverは業務キーの最新有効行です。無効な最新版から古い有効行を復活させる方式ではありません。Goldの生産は工場とUTC日付、出荷は受注ごとの粒度です。集計値を解釈するときは、画面の粒度と [MEDALLION.md](MEDALLION.md) のSQL定義を一緒に確認します。

「Goldをプレビュー」は固定した2データセットだけを読みます。表ごとのmain snapshotを固定し、最大500行を返します。任意SQLや表名を入力する欄はありません。表間の同一時点や、抜粋からの全件合計を保証しません。JSONには表・snapshot・export時のコード参照を含みます。[AI-READINESS.md](AI-READINESS.md)

「品質件数を確認」は、固定6表のBronze、Silver、Quarantine件数を最大6行で表示します。固定SQL、短いHTTP timeout、全体期限、未完了queryの取消を使います。表が未作成なら先にメダリオンを実行します。件数は現在の各表の値で、表間の共通snapshotではありません。改訂・重複排除があるため `Bronze = Silver + Quarantine` を常に期待しません。

素材生成は「素材の投入」、後続処理は「処理・検証」です。プレビューや品質確認はデータを変更せず、メダリオンやworkflowを自動起動しません。26種類の異常素材は [ANOMALY-SCENARIOS.md](ANOMALY-SCENARIOS.md) を使って期待結果と照合します。

## 接続診断は適用済みか保存済みかを選ぶ

「接続診断」の既定はAPI起動時の適用済み設定です。「保存済み・反映前の設定で診断する」を選ぶと、privateファイルへ保存した値で試します。画面と応答の `configuration_scope` で、どちらを調べたかを明示します。

最初の表示はオフライン確認です。HTTP(S)/NO_PROXYの上下caseの有無・一致、内部サービスのbypass、CA設定の有無を返します。実URL、proxyのusername/password、CAのpathは返しません。Python urllibのNO_PROXY解釈なので、Javaや各SDKの動作は別に確認します。

接続を試す場合だけ、PyPI、Maven Central、npm、GitHubから選び「選んだ宛先へ接続診断」を押します。接続先は固定HTTPS4種で、任意URLを指定できません。最大4件を並列、各timeoutを短く制限し、redirectを追いません。結果はtarget名、proxy経由か、成功/失敗code、HTTP status、時間です。

企業CAは `CORPORATE_CA_FILE` にhost側のPEMを指定します。GUI診断で使う場合は専用 `CONTROL_SETTINGS_DIR` 配下のregular fileへ置きます。APIは同じ相対位置を `/private` へ対応付け、symlink、dir外、不在は `NETWORK_CA_UNAVAILABLE` で拒否します。本文に任意のpathを渡す機能はありません。

保存済み設定の診断では、image再build前にCAとproxyの組合せを確認できます。その成功は設定反映ではありません。buildではhostのCAをBuildKit secretとして渡してOS/Java trustへ追加し、runtimeのSSL_CERT_FILE等はimage内の固定bundleを使います。daemon、build、runtime、Javaは [PROXY.md](PROXY.md) の主体ごとに確認します。ポータルはDocker再起動、CAのhost配置、コンテナ再配置を実行しません。

## 履歴から結果を見直す

「履歴」はAPIプロセス内の最新最大20件です。「共通の進捗欄に表示」は既知のジョブIDを読み直す操作で、処理の再実行ではありません。

現在の履歴は永続DBではなく、応答も `persistent: false` を返します。API再起動後は履歴が失われます。manifest、出力ファイル、SQLの専用schema、Icebergの実体を確認して継続を判断し、自動再開を前提にしません。タイムアウトの `WORKER_RESULT_UNKNOWN` はworkerの停止を意味しないため、workerのbusyと実際の結果を先に確認します。[TROUBLESHOOTING.md](TROUBLESHOOTING.md)

## 同梱ヘルプと構成図

「ヘルプ」の文書一覧から選び「文書を開く」を押します。APIは固定した公開Markdownだけを読み、任意のhostファイルやprivate envを読めません。ファイルサイズとsymlinkも検査します。

構成図も許可した2つのSVGをAPIから取得し、Streamlitのサーバー側で表示します。ブラウザーから外部画像URLを自動取得する代わりに同梱図を使います。SwaggerのJS/CSSと合わせ、文書・図・仕様を確認するためのCDN依存を避けます。本文中の参照先サイトを開く場合の通信は別です。

imageにdocsを同梱していない版ではヘルプが未準備になります。Gitからdocsを含めて再buildしてください。frontendへhostの文書dirをmountする必要はありません。

## APIやAIクライアントから使う

`GET /capabilities` は固定APIのmethod、path、read/write、必要profile、目的を返します。これは操作の分類であり、実行権限の付与ではありません。実際のBearer認証・入力検査・書込み同意はAPIが行います。AIへ任意SQL、shell、任意URL、任意ファイル読取を提供しません。

MCP等のクライアントは、まずcapabilities、catalog、status、scenarios、既知ジョブ、helpから根拠を得て、明示的に許可された操作だけを呼びます。素材投入・設定保存・後続処理を読み取りと取り違えません。ルートとschemaの詳細は [API.md](API.md) とSwaggerを参照してください。

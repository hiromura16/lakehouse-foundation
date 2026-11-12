# PoC操作ポータル

<!-- doc-purpose:start -->
> **役割:** 操作ポータルの各ページから、サービス入口・データ・診断・履歴へ移動する。
>
> **対象読者:** 初めて使う人 / データ利用担当 / 運用担当
>
> **読むタイミング:** GUIを開いた後、目的の操作と結果確認の画面を探すとき。
>
> **前提となる資料:** [初めて構築する人の一本道ガイド](<START-HERE.md>)
>
> **次に読む資料:** [GUIからダミーデータを投入する](<DEMO-GUI.md>) / [GUI操作APIとSwagger](<API.md>) / [AIへ渡せるデータとLangChain連携](<AI-READINESS.md>)
>
> **文書一覧:** [目的・読者から探す文書マニフェスト](<DOCUMENTATION-MANIFEST.md>)
<!-- doc-purpose:end -->

**ソース登録の下書き**タブでは、YAMLを書かずにテーブル・列・業務方針を選び、
未承認contract案と確認事項を作れます。DBへ接続しない
[単独プレビューと操作手順](SOURCE-ONBOARDING.md)も用意しています。

既存mainのPoCを稼働させたまま試す場合は、[並行PoCの手順](PARALLEL-POC.md)で
別の実行フォルダ・uv環境・portを用意します。既存の管理アプリを更新せず比較できます。


<!-- shell-usage:start -->
**コマンドのシェル:** 「Bash / Zsh共通」は同じ内容を使い、専用例は自分のシェルを選びます。[詳しい使い分け](SHELLS.md)。

Zshの対話端末では、コメント付きの例を貼る前に一度だけ次を実行します。Bashでは不要です。

**Zsh**

```zsh
setopt INTERACTIVE_COMMENTS
```
<!-- shell-usage:end -->

確認日: 2026-11-10（JST）。初回配置・作業端末・権限は [START-HERE.md](START-HERE.md) で準備します。以下の `up` はCLI管理の環境だけで使います。Dokploy管理では同じprivate設定とComposeサービスのEnvironmentを揃え、9ファイルを使ってDeployします。[0.31.0の配置手順](DOKPLOY.md)


操作画面を、素材投入だけでなく、サービスの入口、データの意味、読み取り確認、接続診断、履歴、同梱ヘルプから使えるようにしています。既定URLは `http://localhost:8501/`、操作APIとSwaggerは `http://localhost:8502/docs` です。実行する操作と、その結果を確認する操作を分けます。

## 最初に開く

private設定で `demo` profileを有効にし、既存の秘密値を保持して配置します。会社のproxyとCAは [PROXY.md](PROXY.md)、全体の順序は [COMPANY-ROLLOUT.md](COMPANY-ROLLOUT.md) を参照してください。

**Bash / Zsh共通**

```bash
export LAKEHOUSE_ENV_FILE=/absolute/private/site.env
./scripts/lakehouse.sh plan
./scripts/lakehouse.sh up
```

追加サービスは必要なprofileだけを選びます。Sparkの常駐Master/Worker/Historyは `compute` です。通常監視は `monitoring`、host root/PID読取りのexporterは明示opt-inの `monitoring-host`、n8nは `automation`、Airflowは `orchestration`、Kafka管理画面は `cdc`、BIは `bi`、Notebookは `notebooks`、ベクトルDBは `vector`、lineageは `lineage` です。既存private設定では、追加された設定項目と容量を確認してから再配置します。ポータルからDockerの権限を使って自動インストールする仕組みではありません。

## 入口カードの読み方

| 表示 | 根拠 | 次の操作 |
| --- | --- | --- |
| profile未選択 | API起動時のprofileに含まれない | 必要性と資源を確認し、設定へ追加して再配置 |
| 必要設定が未完了 | profileは選択済みだが必須設定が空 | カードに表示されたkeyをprivate設定で確認 |
| 稼働を確認 | 固定した内部healthへの到達を確認 | 「開く」から各サービスのnative画面へ |
| 停止・起動待ち・未到達 | 設定はあるがhealthを確認できない | コンテナ状態、起動待ち、認証、内部経路を確認 |

未到達だけで未導入と断定しません。ポータルはDocker socketを持たず、HTTPによる到達確認と設定上の有効化を区別します。healthが成功しても、業務数値やバックアップの受入は別の確認です。

表示するURLにはパスワード、Jupyter token、Qdrant API keyを入れません。各製品のログインはその製品の認証で行います。localhostはブラウザーを開いた端末自身なので、遠隔利用では必要なportを会社標準の方法で転送します。PoC追加サービス・CDC・Sparkのhost portはloopbackへ限定しています。

| サービス | profile | host portの既定 | 開くもの |
| --- | --- | --- | --- |
| Swagger | demo | 8502 | 固定APIの仕様・試行 |
| Trino | core | 8080 | SQL実行状況 |
| Lakekeeper | core | 8181 | Icebergカタログ |
| n8n | automation | 5678 | workflow編集・実行 |
| Grafana / Prometheus | monitoring | 3000 / 9090 | 監視画面・指標 |
| Airflow | orchestration | 8081 | DAGと実行結果 |
| Spark Master | compute | 8082 | クラスタ受付、登録worker、実行アプリ |
| Spark Worker | compute | 8085 | worker資源とexecutor |
| Spark History | compute | 18080 | 保存したevent logによるアプリ履歴 |
| Kafbat | cdc | 8084 | Kafka topic・lag・Connect |
| Superset | bi | 8088 | Goldの探索・BI |
| JupyterLab | notebooks | 8888 | Notebook分析 |
| Qdrant | vector | 6333 | vector collection |
| Marquez | lineage | 3001 | 登録済みlineage |

Sparkの3入口は `SPARK_MASTER_UI_PORT` / `SPARK_WORKER_UI_PORT` / `SPARK_HISTORY_UI_PORT` でhost側のportを変更できます。内部healthは固定したMaster 8080、Worker 8081、History 18080へ接続し、ホストの公開portと区別します。compute未選択ならカードはprofile未選択、設定済みで到達できなければ停止・起動待ち・未到達と表示します。Master UIの稼働確認と、個々のSparkジョブの成功は別に確認します。Historyはevent logを参照するため、対象ログがまだなければ履歴一覧は空です。

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

## 初回の確認順と実装の正本

初回は「状態」でworker、「素材の投入」で1batch、「処理・検証」でmedallion、「データ」でGold/品質、「履歴」で同じID、「ヘルプ」で文書・図の順に確認します。サービス入口カードのhealthyだけでは、この往復は検証できません。設定画面の `保存済み・再配置待ち` が出たら対象変更を整理し、配置担当が反映してから次のSQL投入を行います。

入口・catalog・helpの公開allowlistは [poc_catalog.py](../infra/demo/poc_catalog.py)、APIは [poc_api.py](../infra/demo/poc_api.py) が正本です。Streamlit **1.67.0** のfragmentとFastAPI **0.162.2** のローカルSwagger配信を使います。[公式版への参照](REFERENCES.md) と [API仕様・試し方](API.md) を併せて確認してください。新しい文書をGitへ追加しても、help allowlistへ加えてimageを再buildするまではポータル内の一覧へ自動追加されません。

## 目的別に画面を選ぶ練習

初めて利用する人は、同じ操作用PCでStreamlitとSwaggerを開き、画面操作とAPIの対応を確認すると仕組みを理解できます。nativeサービスのログインはそれぞれ別で、ポータルを開いたことが全サービスへのログインを意味するわけではありません。

| やりたいこと | 最初に選ぶ場所 | 成功を確認する場所 |
| --- | --- | --- |
| 素材を1回作る | 素材の投入 | 共通job結果とmanifest。Goldの表示ではない |
| 最新の業務集計を作る | 処理・検証のmedallion | 別jobの成功→データのGold/品質 |
| 表の意味を知る | データのcatalog | 粒度、列、主キー、由来。実会社のmapは非公開 |
| 未起動の機能を追加する | 管理者のprivate設定/Dokploy | profile保存→Deploy→入口カード。カード自体はinstallしない |
| SQLを自由に試す | 許可されたSuperset/Jupyter/Trinoクライアント | 各nativeツール。ポータルの固定read APIとは別権限 |
| 手順や図を読む | ヘルプ、またはリポジトリ文書 | allowlist内の本文/図と、別提供の報告書素材 |

たとえばSupersetのカードが「profile未選択」なら、APIの再起動を繰り返す前に `bi` を有効にしたか確認します。「必要設定が未完了」なら表示keyをprivate設定で確認し、「停止・起動待ち・未到達」なら該当コンテナとnative healthを調べます。どの状態でも、そのまま業務データの再投入へ進みません。

## 運用時に混同しやすい3つの時点

1. **保存した時点:** 設定フォームがprivateファイルを書き換えた時点です。
2. **適用した時点:** 管理者がDokployのサービスenvと同期し、再配置した時点です。
3. **参照した時点:** 品質SQLやGold previewを実行した時点です。Goldは表ごとにsnapshotを選びます。

portを保存した直後でも、現在の入口カードとAPIは起動時の設定を使います。画面の値を変更してから古いタブのリンクを開くと、新設定へ到達したと誤解しやすくなります。再配置後に状態を更新し、対象環境・port・job IDと確認結果を対応付けてください。

## ポータルに新しい入口やヘルプを加える

| 追加対象 | 変更する場所 | 守る契約 |
| --- | --- | --- |
| serviceカード | [poc_catalog.py](../infra/demo/poc_catalog.py) のServiceDefinition/SERVICES | 固定ID・profile・port key・health・必要設定。URLへ秘密を入れない |
| healthの取得 | [poc_services.py](../infra/demo/poc_services.py) | 既知の内部宛先だけ、timeout、redirect拒否、必要最小限の資格情報 |
| データ契約の説明 | [poc_catalog.py](../infra/demo/poc_catalog.py) とsources.json | SQLの実列・型・粒度と一致。会社private mapを返さない |
| 公開ヘルプ/図 | 同catalogのallowlistとimage内のdocs | 任意pathを受け取らず、サイズ・symlinkを検査 |
| 操作APIの一覧 | capabilitiesとOpenAPI | read/write、必要profile、入力型と同意を一致させる |

図版集の14図を追加したことと、稼働中ヘルプの2SVG allowlistを変更したことは別です。新しいページを表示したければallowlistとimage再buildも更新します。画面の状態判定、秘密なしURL、無効profile、未設定、未到達、保存/未反映をテストし、nativeサービスの操作試験とは分けて記録します。[変更手順](CUSTOMIZATION.md)

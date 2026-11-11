# GUIからダミーデータを投入する

<!-- doc-purpose:start -->
> **役割:** Streamlitで素材生成条件を選び、投入jobと生成結果を確認する。
>
> **対象読者:** 初めて使う人 / データ利用担当
>
> **読むタイミング:** 配置済みの合成検証環境へ、GUIからダミー素材を投入するとき。
>
> **前提となる資料:** [初めて構築する人の一本道ガイド](<START-HERE.md>)
>
> **次に読む資料:** [異常シナリオを選んで素材を作る](<ANOMALY-SCENARIOS.md>) / [MES・ERPデータをBronzeからGoldへ整える](<MEDALLION.md>) / [PoC操作ポータル](<POC-PORTAL.md>)
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

確認日: 2026-11-10（JST）。初回配置・作業端末・権限は [START-HERE.md](START-HERE.md) で準備します。以下の `up` はCLI管理の環境だけで使います。Dokploy管理では同じprivate設定とComposeサービスのEnvironmentを揃え、7ファイルを使ってDeployします。[0.31.0の配置手順](DOKPLOY.md)


この画面では、MES・ERPの合成データをランディングディレクトリまたは専用の検証SQL Serverへ投入します。バッチと有限回のマイクロバッチを選べます。素材を作る操作から、Icebergへの取り込み・SQL変換・n8n・Airflowを自動起動することはありません。

## 誰が何をする画面か

この画面を使うのは、検証用の素材と結果を確認する担当者です。DockerやSambaの管理画面ではありません。基盤管理者が [ホスト準備](HOST-SETUP.md) と配置を済ませた後、利用者は操作用PCのブラウザーから入ります。遠隔利用の `localhost` は、そのPC自身なのでSSH転送等を先に用意します。

| 画面の操作 | 起きること | 起きないこと |
| --- | --- | --- |
| 素材の投入 | CSV/JSON/PNG、専用DBの新規schema等を生成 | Iceberg取込・SQL変換の自動開始 |
| 処理・検証 | 選んだworker操作を別jobとして依頼 | 素材の条件を自動修正して再投入 |
| 設定を保存 | private envの許可項目を保存 | Dokployやコンテナへの即時反映 |
| Gold/品質の参照 | 固定したデータの読み取り | 任意SQLの実行や自動修復 |

「一度投入したら何が増えるか」を確かめる最初の練習は、会社データと別の検証環境で行います。画面操作の途中でprofileやSQL接続先を変えず、1jobの完了とmanifestを確認してから次へ進みます。

## 最初に画面を開く

[導入手順](DEPLOYMENT.md) に沿ってprivate設定を生成します。既存の設定がある場合は同じファイルを継続して使い、秘密値を再生成しません。

**Bash / Zsh共通**

```bash
python3 scripts/configure.py --output /absolute/private/site.env
```

private設定で次を確認します。

| 項目 | 指定方法 |
| --- | --- |
| `COMPOSE_PROFILES` | `demo`。監視も使う場合は `demo,monitoring` 等 |
| `DEMO_GUI_PORT` | 既定 `8501` |
| `DEMO_API_PORT` | 既定 `8502` |
| `BIND_IP` | 初期は `127.0.0.1` |
| `LANDING_DIR` | この環境専用の、リポジトリ外のランディング先 |
| `CONTROL_SETTINGS_DIR` | private設定ファイルを置く専用ディレクトリ。生成時に設定される |
| `CONTROL_SETTINGS_NAME` | private設定ファイル名。生成時に設定される |
| `CONTROL_API_TOKEN` | 生成された秘密値を保持。ブラウザーに入力しない |
| `WORKER_UID` / `WORKER_GID` | ランディングとprivate設定を読み書きできる所有者 |

private設定のディレクトリは0700、ファイルは0600とします。APIは設定を安全に置き換えるため、ファイルだけでなく専用ディレクトリを読み書きします。他のアプリの資格情報を含む親ディレクトリを共有しません。ブラウザーUIにはホストディレクトリをmountしません。

**Bash / Zsh共通**

```bash
export LAKEHOUSE_ENV_FILE="/absolute/private/site.env"
./scripts/lakehouse.sh plan
./scripts/lakehouse.sh up
```

サーバー自身で `http://localhost:8501/` を開きます。別端末からはSSHポート転送等を使います。別端末の `localhost` はその端末自身です。画面を広く公開する場合は、利用者認証・TLS・接続制限を用意してからbind先を変更します。

## 一度だけ投入する

1. 「素材の投入」を開き、投入方式を「バッチ（一度だけ）」にします。
2. 投入先は最初に「ランディング」を選びます。
3. 乱数シード、指図・受注数、業務日数、データ品質を選びます。
4. 必要なら「合成の検査画像PNGも作る」を選びます。
5. 「素材の投入を開始」を押し、完了件数とソースごとの行数を確認します。

| 入力 | 意味・上限 |
| --- | --- |
| 乱数シード | 数量等の再現に使う整数。0～2147483647 |
| 指図・受注数 | 1回あたり1～1000。製造実績・出荷には複数の子レコードを生成 |
| 業務日数 | 合成する業務日の範囲。1～366 |
| `clean` | 型・参照が有効な合成データ |
| `edge` | 型不正、負数、参照不整合、重複、改訂、遅延到着を含む |
| 合成画像 | 1回につき人工的な検査パターンPNGを1枚。実写真やOCR結果ではない |

製品・製造指図・受注・出荷のCSVと、製造実績・停止実績のJSON Linesの計6ソースが対象です。列と異常ケースの説明は [DEMO-DATA.md](DEMO-DATA.md) を参照します。

成功時は実行メタデータをJSONで保存できます。メタデータには相対ファイル名、行数、SHA-256、run ID、batch番号、必要時の新規SQL schema名を含みます。パスワードや接続文字列は含みません。

## 異常シナリオを目的から選ぶ

「異常シナリオのプリセット」で正常、品質、改訂、工場ロス、出荷、転送の例を選び、カテゴリと複数選択欄で追加・除外します。行・業務値19件、ファイル・転送7件の計26件です。選択すると、対象、実際の変更、下流の隔離・重複排除・Goldへの影響・取り込み拒否/無視を同じ表で確認できます。

既存のclean/edgeは「ベースのデータ品質」で選べます。追加異常なしなら従来の生成値を維持します。「異常の影響率」は1〜100%で、対象の抽出率です。最終の異常行率やGold差分を直接指定する値ではありません。

高度なファイル異常は専用の検証landingで使い、明示同意が必要です。SQL-only、画像破損で画像未選択、競合する書換えの組み合わせは送信前に理由を表示して止めます。ファイル異常の選択集合を変えると同意を解除します。

生成後は「意図した異常の生成結果」に、選択したケース、操作した件数、実ファイル名、想定する下流の結果を表示します。意図して不正な素材を生成できた場合は生成ジョブは成功です。その後の取り込み拒否・無視等は別に照合します。全ケース、preset、率、seed、API/CLIの指定方法は [ANOMALY-SCENARIOS.md](ANOMALY-SCENARIOS.md) を参照してください。

## 有限回のストリーミングを試す

投入方式を「ストリーミング（有限マイクロバッチ）」へ変更し、回数と間隔を指定します。例えば3回・5秒間隔では、独立した3組のソースを順に投入して終了します。GUIは2～60回・1～60秒を選べ、待機時間の合計 `(回数 - 1) × 間隔` は600秒以内です。

これはファイルまたは検証DBへの到着を模擬する仕組みです。Kafkaへ直接送信する処理、SQL ServerのCDC、永続的な無限ストリームは含みません。業務日付は既存生成器と同じ合成期間を使い、実行時刻の業務イベントとして装いません。

進捗画面は約2秒ごとに更新します。「次のマイクロバッチから停止する」で、現在の投入が終わった後の続行を止めます。ブラウザーを閉じるだけでは停止操作になりません。APIの再起動をまたぐジョブの自動再開は前提にせず、既にできたファイルとmanifestを確認してから新しいジョブを開始します。

## ファイル名・ID・完了マーカーの契約

各実行に小文字16進12桁の `run_id`、各回に1から始まる4桁の `batch` を割り当てます。同じ実行・同じ回の上書きは拒否します。

```text
csv/erp_products__<run_id>_0001.csv
csv/erp_work_orders__<run_id>_0001.csv
csv/erp_sales_orders__<run_id>_0001.csv
csv/erp_shipments__<run_id>_0001.csv
json/mes_events__<run_id>_0001.jsonl
json/mes_downtime__<run_id>_0001.jsonl
images/inspection__<run_id>_0001.png        任意
manifests/demo__<run_id>_0001.json          実行メタデータ
```

各ソースと画像には `<ファイル名>.ready` を作り、内容のSHA-256を記録します。完成したファイルを原子的に公開し、その後でマーカーを原子的に公開します。複数ファイルをまとめたトランザクションではないため、途中失敗時には完成済みファイルが残ることがあります。manifestは最後に書きます。

製品・指図・受注・出荷・製造イベント・停止イベントのIDと参照IDへ `G<run_id>B<batch>-` を付け、別の実行・別の回の主キーが衝突しないようにします。同じバッチ内の意図した重複・改訂は保持します。`updated_at` は元の順序を保ち、回数に応じて秒をずらします。工場IDと業務日付は集計用に維持します。

複数の画面から同じ出力先へ同時に投入しようとすると、実行中の1バッチ以外を拒否します。既存CLI生成器の固定ファイルを上書きする操作はありません。

## 専用の検証SQL Serverへ投入する

private設定に `DEMO_SQLSERVER_HOST`、`PORT`、`DATABASE`、`USER`、`PASSWORD` を設定します。業務参照元の `SOURCE_SQLSERVER_*` と別の専用DBを用意します。GUIには既存のパスワードを返しません。

投入先を「専用の検証SQL Server」または「両方」にし、「検証DBへの書き込みを明示的に許可する」を選びます。許可がない投入、システムDB、設定済みの `SOURCE_SQLSERVER_DATABASE` と同名のDBへの投入は拒否します。

接続はTLS暗号化と証明書・ホスト名検証を必須にします。GUIに検証を無効化するスイッチはありません。企業CAが必要なら `CONTROL_SETTINGS_DIR` 配下へCAを置き、`DEMO_SQLSERVER_CA_FILE` にhost側の絶対pathを指定します。APIは同じ相対位置を `/private` 側へ対応付けます。専用dir外のpathやsymlinkは拒否します。空または `system` を使う場合はhostとAPIコンテナそれぞれの信頼ストアを確認します。

接続alias/IPと証明書の名前が異なる場合だけ、設定の `DEMO_SQLSERVER_HOSTNAME_IN_CERTIFICATE` にDB担当が確認した照合名を指定します。CAと証明書名の検証は維持し、空欄では従来どおり接続hostを照合します。変更後は再配置して反映します。

1回ごとに新しいランダムな `lh_demo_<random>` schemaを作り、6つのnvarchar staging表へINSERTします。DB作成、既存schemaの再利用、業務表へのUPDATE・DELETE、CDCの有効化は行いません。異常な文字列もソースとして保持するため、型付き業務DBの完全な再現ではありません。

SQLだけを選ぶ場合はCSV・JSON・PNGをランディングへ出力せず、実行manifestを残します。「両方」はDBとファイルをまたぐ一つのトランザクションではありません。途中失敗後に自動で全体を巻き戻さず、投入結果を確認します。

## CDCテスト用DBの追加・更新・削除を試す

「素材の投入」の **CDCテスト用DBに変更を投入** を開きます。通常の素材生成とは別の操作で、管理者が準備した `lh_cdc_demo_...` 専用DBの固定6レコードを変更します。事前準備は [CDC.md](CDC.md) に従い、反映済みのDEMO管理資格情報、CDC対象DB、秘密の `CDC_DEMO_OWNERSHIP_TOKEN` が必要です。画面からDBを新設したり、会社の業務DBへ変更したりすることはできません。

1. 専用DBの準備結果と、設定保存後の再配置が完了していることを確認します。
2. 「準備済みの専用CDCテストDBへの変更を許可する」を選びます。選ぶまでは3つのボタンを無効にしています。
3. 「追加（INSERT）」を押し、共通の進捗欄で完了を確認します。
4. 「更新（UPDATE）」、続いて「削除（DELETE）」を、それぞれ完了を確認して実行します。
5. 実行メタデータの `source_only: true` と `changed_rows: 6`、その後のCDC経路を別々に確認します。

各操作はジョブIDで受け付けます。進捗は既存の素材/処理と同じ欄に表示し、反映待ちの設定がある場合は起動時の旧設定を使う注意を表示します。確認チェックは専用DBへの変更の明示同意であり、DB内の所有権markerと秘密tokenの照合を省略するものではありません。TLS検証を無効にするスイッチもありません。

開始済みのDB変更は1 transactionを保つため途中取消できません。既に存在する固定行への再追加、存在しない固定行の更新・削除等は前提違反としてrollbackします。同じボタンを繰り返さず、結果を確認してください。

この3操作から取り込み・変換を自動実行することはありません。成功はソースDBへのcommitまでで、KafkaやIcebergへの到着を示しません。稼働中のDebezium/consumerが変更を読む場合は、設定済みのCDC経路による伝播をKafbat UI（既定 `http://localhost:8084/`）、topic、consumer状態、派生表で照合します。固定APIと応答の詳細は [API.md](API.md#cdc用の追加更新削除を試す) を参照します。

## 設定と状態を確認する

「設定」では基本・SQL接続・プロキシ・サービスの項目を分けて編集します。既存の秘密値は返さず、「資格情報を更新する」のpassword欄は空欄なら保持します。「設定を保存」はprivate設定を保存する操作です。必要な再配置まで自動実行するものではありません。画面の「保存済み・再配置待ち」を確認し、配置時のenvとmountも整合させます。

「状態」ではAPIやworkerの到達性等を確認します。素材の投入成功と、Bronze・Silver・Goldの処理成功は別に確認します。投入後の処理方法は [GUI-WALKTHROUGHS.md](GUI-WALKTHROUGHS.md) と [MEDALLION.md](MEDALLION.md) を参照します。

「処理・検証」では、素材の投入と独立して次のボタンを選べます。

| ボタン | 実行内容 |
| --- | --- |
| 原本を取り込む | 完成済みファイルをBronzeへ取り込む |
| Silver・Goldまで変換 | 原本の取り込みから派生表の更新まで実行する |
| 基盤の往復検証 | 専用の合成試験データでS3・Iceberg・Trinoを照合する |

操作はジョブIDで受け付け、共通の進捗欄に結果を表示します。処理に時間がかかっても同じボタンを繰り返さず、進捗を更新して確認します。pipelineの途中取消は提供せず、停止ボタンは素材のマイクロバッチだけに表示します。

「AIの準備」ではGoldの対象を選び、表ごとの最大500行をsnapshot ID付きで取得できます。表ごとにmainのsnapshotを固定し、export時checkoutの参照SQLとadapterのハッシュを添えてJSON保存します。このハッシュは実snapshotを生成したSQLの保証ではありません。行数制限があるため、全件集計として扱いません。この操作からモデルを呼び出したり、外部AIへ送信したりしません。

## APIと実装の境界

APIのSwagger UIは既定で `http://localhost:8502/docs` です。直接操作にはprivate設定のBearer tokenを使います。トークンを公開ログやスクリーンショットへ含めません。Streamlitは内部APIへサーバー側から認証し、ブラウザーへ既存トークンを配信しません。

投入ロジックは `infra/demo/service.py` の `generate_batch()` に分離しています。Streamlitは `POST /jobs` を呼ぶ薄いクライアントで、有限ループ・状態・取消はAPIが管理します。今後Kafka等の入力方式を増やす場合も、ソースへの投入と後段処理を分ける境界を維持します。

Streamlit 1.67.0、FastAPI 0.162.2、Uvicorn 0.54.0等を公式PyPI metadataで確認し、依存全体をhash付きlockへ固定しています。Pythonの対応版は採用するStreamlitのmetadataとlockを参照し、このイメージはPython 3.14を使用します。[Streamlitの公式配布](https://pypi.org/project/streamlit/)、[Python対応と導入手順](https://docs.streamlit.io/get-started/installation/command-line)

## UIの再表示とジョブ継続

Streamlit **1.67.0** の [fragment](https://github.com/streamlit/streamlit/blob/1.67.0/lib/streamlit/runtime/fragment.py) はブラウザーsessionが有効な間の再表示に使います。素材の有限ループ自体はFastAPI側が管理します。タブを閉じたことを取消と解釈せず、再接続時は「履歴」で既知IDを確認します。API再起動後の履歴は残りません。初期設定、SQL権限、書込み対象の違いは [会社SQL](SOURCE-SQLSERVER.md)・[CDC](CDC.md)、版と照合範囲は [REFERENCES.md](REFERENCES.md) を参照します。

## 実務での小さな実験: 一度に1条件だけ変える

1. 検証用環境でbatch、landing、seed42、20件、7日、clean、追加異常なしを選びます。素材の成功を確認し、表示されたrun ID/manifestを保存します。
2. 「Silver・Goldまで変換」を一度だけ押し、jobの成功と「データ」の件数/Goldを保存します。
3. 厳密なGold比較では、正常runと異常runを別の空の検証環境で実行します。異常側は同じ基礎条件に `reject_spike` だけを追加します。
4. 生成manifestの変更件数と、変換後Goldの不良数・歩留まりを比較します。不良の急増は型不正ではないため、必ずしもQuarantineを増やしません。

同じ通常namespaceへ2runを追加してから全体合計を引くだけでは、正常分の追加と異常の影響が混ざります。同じ環境で比較する中級者は、由来と業務キーでSilverをrunごとに絞り、独立集計します。標準Goldにrun別filterが自動追加されるわけではありません。また、`edge` の基礎異常と追加異常を最初から重ねると、どのルールが結果を変えたかを判断しにくくなります。

## 途中で画面を閉じた・結果が分からないとき

| 観測した状態 | 最初にすること | 次の投入を始める条件 |
| --- | --- | --- |
| ブラウザーを閉じた | 再接続して履歴/既知job IDを読む | 実行中か完了かを確認できた |
| 有限streamを止めたい | 次batchから停止するボタンを使う | 現在batchが終わり、完了済み素材を確認した |
| SQL変更が処理中 | 取消を繰り返さず結果を待つ | commitまたは失敗を確認した |
| APIを再起動して履歴がない | manifest、専用schema、workerの状態を照合 | 前の操作の実体が分かった |
| `WORKER_RESULT_UNKNOWN` | workerのbusy/blockedと出力を調べる | 継続実行や残存queryがないことを確認した |
| 保存済み・再配置待ち | 管理者がprivate設定とDokploy envを同期して再配置 | 新しい接続/mountが反映された |

履歴が空になったことと、ファイルやDBが元へ戻ったことは別です。確認のために同じ開始ボタンを再押下しません。

## UIを変えるときの責務

入力欄・表示は [app.py](../infra/demo/app.py)、APIへの通信は [client.py](../infra/demo/client.py)、素材処理は [service.py](../infra/demo/service.py) です。新しい選択欄を足すときは、APIの型・上限、共通の異常検査、manifest、AppTest/認証API試験まで確認します。画面だけで検査しても、APIを直接呼ぶ利用者の入力は制限できません。

SQLの本番接続を試すボタンや、Docker socket、任意shell、既存秘密値の読み戻しをこの画面へ追加する構成にはしていません。必要な新機能は、目的・対象・権限・失敗時の扱いを決めて [カスタマイズ](CUSTOMIZATION.md) へ進みます。

## 固定疑似ERP/MESをSQL Serverへ投入する

投入先に固定SQLとLanding併用の2方式を追加しました。従来のランダムschema方式と分け、DBAが固定DDLを適用した後に所有権・限定権限を診断します。書込み許可と共有sourceへの追加同意を両方必要とします。入力・型・run ID・有限microbatch・I/U/Dの手順は [固定SQL source](SQLSERVER-FIXED-DEMO.md) を参照してください。

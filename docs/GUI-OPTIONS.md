# GUIで設定・操作する選択肢

<!-- doc-purpose:start -->
> **役割:** 操作・オーケストレーション・列変換のGUIを分け、同梱機能と追加候補を比較する。
>
> **対象読者:** データ利用担当 / 開発・設計担当
>
> **読むタイミング:** 画面で何を設定したいか整理し、追加OSSや実装の採否を決めるとき。
>
> **前提となる資料:** [Lakehouse Foundation アーキテクチャ](<ARCHITECTURE.md>)
>
> **次に読む資料:** [n8n・Airflow・Grafanaを画面から使う](<GUI-WALKTHROUGHS.md>) / [ソース・指標・API・計算基盤を拡張する](<CUSTOMIZATION.md>)
>
> **文書一覧:** [目的・読者から探す文書マニフェスト](<DOCUMENTATION-MANIFEST.md>)
<!-- doc-purpose:end -->

照合日: 2026-11-10。現在の操作入口は **Streamlit + FastAPI** で、素材投入、設定保存、状態確認、登録済み処理、AI context取得を提供する。[操作GUI](DEMO-GUI.md) と [API/Swagger](API.md) に手順をまとめている。n8nは実行手順の可視化・連携に使い、CDCの管理にはKafbat UIも提供する。Bronzeの取り込みはPyIceberg、Silver/Goldの変換はTrino SQLとしてGit管理し、業務ルールはSQLとJSONのデータ契約で定義する。

GUIで「実行順序やタイミングを組む」ことと、「列の型・結合・集計を画面で定義する」ことは分けて選ぶ。後者まで必要ならApache Hopが有力な追加候補となる。以下は公式資料で確認した製品機能の比較であり、この基盤との接続試験が済んだという意味ではない。

## まず同梱の画面を使う

導入・ログイン・保存までの手順は右の文書にある。`COMPOSE_PROFILES` が空ならcoreだけで、下の任意ツールは起動しない。既存の必要profileを残して選ぶ。

| やりたいこと | 今ある画面・採用版 | profile | 手順 |
| --- | --- | --- | --- |
| 疑似素材の投入、設定、異常シナリオ | Streamlit / FastAPI Swagger | `demo` | [DEMO-GUI.md](DEMO-GUI.md)、[API.md](API.md) |
| 入口・データ契約・品質・履歴・ヘルプ | 同じStreamlit内のタブ | `demo` | [POC-PORTAL.md](POC-PORTAL.md) |
| 処理の手動実行と外部連携 | n8n 2.46.7 | `automation` | [GUI-WALKTHROUGHS.md](GUI-WALKTHROUGHS.md) |
| DAG run、task logs、接続資格情報 | Airflow 3.4.0 | `orchestration` | [GUI-WALKTHROUGHS.md](GUI-WALKTHROUGHS.md) |
| 運用グラフを編集・保存 | Grafana 13.3.0 / Prometheus 3.16.0 | `monitoring` | [GRAFANA.md](GRAFANA.md) |
| GoldへSQL、Chart、Dashboard | Superset 6.1.0 | `bi` | [POC-TOOLS.md](POC-TOOLS.md) |
| Python / Spark / Icebergの対話実行 | JupyterLab 4.6.6 | `notebooks` | [POC-TOOLS.md](POC-TOOLS.md) |
| 分散実行中の状態、終了後の履歴 | Spark 4.1.4 Master / Worker / History | `compute` | [COMPATIBILITY.md](COMPATIBILITY.md) |
| vector / payload / query | Qdrant 1.20.0のDashboard | `vector` | [POC-TOOLS.md](POC-TOOLS.md) |
| job / datasetの来歴を見る | Marquez 0.50.0 | `lineage` | [POC-TOOLS.md](POC-TOOLS.md) |
| SQL Serverの変更とKafka状態を見る | Kafbat UI / Kafka Connect | `cdc` | [CDC.md](CDC.md) |
| AIクライアントから型付きtoolを呼ぶ | MCP SDK 2.3.0。専用Web画面ではない | `mcp` | [MCP.md](MCP.md) |

操作の最初はStreamlitの「入口」で必要な画面を開き、「データ」で粒度・列・品質を確認する。上記のURLはbrowserからの入口で、コンテナ間は `worker` / `trino` 等の内部名を使う。private設定の保存だけで稼働コンテナが更新されたとは判断せず、保存待ち表示と再配置後の状態を確認する。

### 選択を絞る例

- 取り込みを試してBIまで見る: `demo,bi`。workerがメダリオンを処理し、SupersetでGoldを読む。
- Notebookから分散加工する: `notebooks,compute`。Jupyter driverとSpark Workerの同一Python依存、event logを使用する。
- 運用の自動化と可視化: `automation,monitoring`。同梱n8nフローは手動実行から始める。
- CDCを確認する: `cdc,demo`。専用疑似DBの固定DMLと別のcdcメダリオンを使い、通常のsnapshot取込と区別する。

GUIを追加しても列契約は自動変更されない。Datasetの編集、Notebookの任意コード、設定保存、workerの登録済みジョブは、それぞれ別の操作権限を必要とする。

## GUIを増やす前に、変更したいものを決める

「画面で操作したい」を次の三つへ言い換えると、追加製品が必要か判断できます。

| 変更したいもの | まず使うもの | コードの変更が必要になる境界 |
| --- | --- | --- |
| 件数・seed・投入先・異常率 | 同梱Streamlit/Swagger | 新しいソース契約・新しい異常の定義 |
| 実行する時刻・APIの順番・通知 | n8nまたはAirflow | 業務SQL、workerへ公開する新action |
| 列の対応・型・参照・集計式 | source mapとmedallion SQL | GUI変換製品を採用する場合も、契約・テスト・配布の管理が必要 |
| 見せる指標・filter・Chart配置 | Superset | Goldに存在しない指標・粒度の追加 |
| 収集指標とパネル | Grafana | 未収集のhost/DB/CDC指標にはexporter等が必要 |

例えば「受注の残数を出す」だけなら既存GoldとSupersetで始めます。「MESの新しい列を追加する」なら、source mapを編集するだけで全層が増えるとは考えず、入力契約、取込schema、Silver/隔離、Gold、期待値、catalog説明の変更を見積もります。

## 候補比較

**ここから下のHop / NiFi / Airbyte / dbt / Dagsterは未同梱・未接続。** このリポジトリにprofile、認証、volume、版lock、起動コマンドはない。候補の機能を、そのまま本キットのGUI機能として説明しない。

| 候補 | GUIで設定できる範囲 | Iceberg RESTへの接続 | 追加する運用対象・適用先 |
| --- | --- | --- | --- |
| **n8n：同梱の連携GUI** | 手動/定期起動、API呼び出し、分岐、通知。列の結合・型変換・集計は登録済みSQL/JSON契約で定義 | n8nはジョブAPIまたはTrinoのHTTP APIを呼ぶ。RESTカタログとS3への接続はPyIceberg/Trino側に集約 | 実装済みのautomationプロファイルを利用。大量の明細をn8n内に展開せず、結果件数・状態を渡す |
| **Apache Hop** | パイプライン画面で入力・列選択・計算・条件・結合等を組む。SQL実行ステップもある | 2.20.0のreleaseでLake table input/outputを確認。RESTカタログ等の詳細は比較時のmanualを参照し、導入時に配布版と再照合する | GUIとJavaの実行環境。ブラウザ運用ならHop Web/Serverも対象。変換定義を画面中心に作りたい場合 |
| **Apache NiFi** | ブラウザでファイル/DB取得、Record変換、経路分岐、失敗時の処理を組む。細かいルールにはRecordPath/式/SQLを入力 | PutIcebergRecord + RESTIcebergCatalog + S3 FileIO/Parquet writer/Record readerを設定 | JVM、キュー、Content/FlowFile/Provenanceリポジトリの容量を管理。継続取り込みや多数の経路・再送を可視化したい場合 |
| **Airbyte** | 接続元/先、表・列、同期方式、実行間隔を画面で設定。任意の業務集計を描くGUIではない | MSSQL sourceはCDC対応。S3 Data Lake destinationはS3互換保存先とRESTカタログに対応 | Kubernetes/ジョブ実行基盤。SQL Server等からの複製を製品化したコネクターに任せたい場合。今回のComposeにそのまま1コンテナ追加する構成ではない |

n8nのHTTP設定は [HTTP Request node](https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.httprequest/) を参照。Hopの画面変換は [Pipelines](https://hop.apache.org/manual/latest/pipeline/pipelines.html)、RESTカタログへの出力は [Lake table output](https://hop.apache.org/manual/latest/pipeline/transforms/spark-lake-table-output.html) を参照。NiFiの構成要素は [PutIcebergRecord](https://nifi.apache.org/components/org.apache.nifi.processors.iceberg.PutIcebergRecord/) と [RESTIcebergCatalog](https://nifi.apache.org/components/org.apache.nifi.services.iceberg.catalog.RESTIcebergCatalog/) で確認できる。Airbyteの対応は [MSSQL source](https://docs.airbyte.com/integrations/sources/mssql) と [S3 Data Lake destination](https://docs.airbyte.com/integrations/destinations/s3-data-lake) を参照。

## 今回のn8nとメダリオン処理

実装方針は次の役割分担とする。実際の起動手順と検証済み範囲はREADME/DEPLOYMENTと実行結果を正とする。

1. **Bronze**：受領CSV/JSON/画像の原本をS3へ保管し、ハッシュ・受領情報と取り込みレコードをPyIcebergで保存する。
2. **Silver**：Trino SQLで型、必須項目、重複等を整理する。採用するキーと不正行の扱いをSQL/JSON契約に残す。
3. **Gold**：Silverから業務で使う集計をTrino SQLで生成する。集計粒度と対象期間を定義する。
4. **n8n画面**：同梱は手動起動 → 処理API → 結果確認。処理APIがBronze→Silver→Goldの順序と失敗判定を管理する。Schedule、通知、再試行は別途追加が必要。

運用者は画面から実行・結果確認を行い、変換内容の変更はSQL/JSONの差分としてレビューできるようにする。任意のSQLを一般利用者がWebhookへ渡す方式は避け、登録済みの処理と許可したパラメーターを呼び出す。

Trinoをn8nから直接HTTPで呼ぶ方法もあるが、`POST /v1/statement` のHTTP 200で完了とは判断しない。応答の `error` を検査し、`nextUri` がある間はGETを継続して終了を確認する。この処理をジョブAPIにまとめれば、n8n側のフローを簡潔に保てる。[Trino client REST API](https://trino.io/docs/current/develop/client-protocol.html)

## Hop / NiFi / Airbyteを追加する条件

**Hop** は、SQLを書かずに列処理を組む要求が増えた段階で小さなCSVを使って試す。Hop engineのIceberg入出力ではLakehouseプラグインの有無と版を固定する。カタログ管理の表へPATHモードで直接書かず、RESTカタログのTABLEモードを使う。最新資料の対応だけで既存の配布イメージにも機能があるとは判断しない。[Hop 2.20リリース](https://hop.apache.org/blog/2026/11/hop-2.20.0/) / [Lake table input](https://hop.apache.org/manual/latest/pipeline/transforms/spark-lake-table-input.html)

Hop 2.20.0のreleaseでは、Spark名の旧Iceberg transformからLake table input/outputへの置き換えが説明されている。古い手順のtransform名を新規手順へ流用しない。URLに `latest` を含むmanualは将来変わるので、採用する際は実際の配布tag・plugin・Java版を新たにlockする。

HopからTrino JDBCを登録してSQLを実行する案も可能性がある。HopのGeneric接続はdriver classとURLを設定でき、TrinoはJDBC driverを提供する。ただしHopとTrinoを組み合わせた実行は本リポジトリでは未検証で、SQL方言やメタデータ取得の確認が必要。これはSQL入力が必要な経路である。[Hop接続設定](https://hop.apache.org/manual/latest/metadata-types/rdbms-connection.html) / [Trino JDBC](https://trino.io/docs/current/client/jdbc.html)

**NiFi** は、再送・滞留・取り込み経路を画面で管理したい場合に試す。CSVReader/JsonTreeReaderとRecord変換を構成し、Icebergへの書き込みを行う。NiFiのQueryRecordによる集計は個々のFlowFile内が範囲となるため、全履歴に対するGold集計はTrino等のテーブル処理へ渡す。[UpdateRecord](https://nifi.apache.org/components/org.apache.nifi.processors.standard.UpdateRecord/) / [QueryRecord](https://nifi.apache.org/components/org.apache.nifi.processors.standard.QueryRecord/)

**Airbyte** は、会社のSQL Serverへの複製接続をまとめたい場合に試す。CDCにはソースDB/表の有効化と権限設定が別途必要で、GUIの接続だけでは完了しない。標準の推奨リソースは4 CPU以上・8GB以上のメモリで、abctlも内部にKubernetesを作成する。core基盤や変換ジョブと別に予算を取る。[公式Quickstart](https://docs.airbyte.com/platform/using-airbyte/getting-started/oss-quickstart) / [abctl](https://docs.airbyte.com/platform/deploying-airbyte/abctl)

## RESTカタログで共通して確認すること

Hop/NiFi/Airbyteをこの基盤へつなぐ場合、カタログURI、Warehouse識別子、namespace、必要な認証を設定する。さらに、クライアントからS3 endpointへ到達できること、region/path-styleとS3資格情報が正しいことを確認する。カタログへの疎通だけではParquetの読み書き成功を意味しない。資格情報の払い出しを使う場合はカタログと保存先双方の対応を確認し、静的資格情報方式と混在させない。

これらの追加製品からLakekeeper/SeaweedFSへの実接続は未検証。導入判断の前に、合成データの書き込み→Trino参照→再試行→重複/更新の扱い→再起動後の継続を小規模に確認する。会社プロキシとCA設定は [PROXY.md](PROXY.md) に従う。

## dbt + Dagsterを選ぶ場合

SQLモデルが増え、依存関係、部分再実行、実行履歴を管理したくなったら追加候補となる。DagsterのUIはdbtモデルをassetとして表示し、選んだモデルの実行や失敗・ログの確認ができる。**変換の定義にはdbtのSQL/YAML等が必要**で、ここでは列変換を全てドラッグ操作で定義する代替とは扱わない。Trino用adapterと各製品の互換版も別途検証する。[Dagsterとdbt](https://docs.dagster.io/integrations/libraries/dbt)

## ライセンスとリソースの考え方

Hop/NiFiはApache License 2.0のOSS。n8n CommunityはSustainable Use Licenseで、OSI型のOSSと同じ条件とは扱わない。Airbyteのリポジトリ本体はELv2で、connectorや有償機能の条件も確認する。外部向けサービス提供ではそれぞれの採用tagの条件を確認する。[Hop LICENSE](https://github.com/apache/hop/blob/master/LICENSE) / [NiFi LICENSE](https://github.com/apache/nifi/blob/main/LICENSE) / [n8n 2.46.7 LICENSE](https://github.com/n8n-io/n8n/blob/n8n@2.46.7/LICENSE.md) / [Airbyte LICENSE](https://github.com/airbytehq/airbyte/blob/master/LICENSE)

Dagster OSSとdbt CoreはApache 2.0を基準に選び、商用クラウド版等と区別する。[Dagster LICENSE](https://github.com/dagster-io/dagster/blob/master/LICENSE) / [dbt LICENSE](https://github.com/dbt-labs/dbt/blob/main/LICENSE)

Airbyte以外の必要メモリは、この環境で測定していない。n8nにはフローと実行履歴、HopにはJVMと変換中のデータ、NiFiにはJVMに加えて永続キューと来歴、DagsterにはUI/daemonと実行ワーカーの予算が必要、という運用上の見積もりで比較する。入力件数・同時実行・保持期間を決めてから実測し、GUI追加だけでデータ処理の負荷が軽くなるとは判断しない。

## 今回照合した一次資料

| 資料 | 確認したこと・適用範囲 |
| --- | --- |
| [n8n 2.46.7 tag](https://github.com/n8n-io/n8n/releases/tag/n8n@2.46.7)、[手動・自動実行](https://docs.n8n.io/workflows/executions/manual-partial-and-production-executions/) | 同梱Manual Triggerと、自動起動する公開workflowを区別 |
| [Superset 6.1.0 tutorial](https://superset.apache.org/user-docs/6.1.0/using-superset/creating-your-first-dashboard/) | SQL Lab、Dataset、Explore、保存するDashboardの役割 |
| [Hop 2.20.0 release](https://hop.apache.org/blog/2026/11/hop-2.20.0/)、[Lake table output manual](https://hop.apache.org/manual/latest/pipeline/transforms/spark-lake-table-output.html) | 候補版の機能比較。未同梱のため接続互換性を保証しない |
| [NiFi PutIcebergRecord](https://nifi.apache.org/components/org.apache.nifi.processors.iceberg.PutIcebergRecord/) | 候補Processorの設定項目。未採用でversion lockなし |
| [Airbyte MSSQL](https://docs.airbyte.com/integrations/sources/mssql) | 比較時のconnector 5.1.3表示とCDC対応。platform、destination、導入版は未選定 |
| [Dagster dbt integration](https://docs.dagster.io/integrations/libraries/dbt) | UIでモデル・履歴を扱う候補。SQL/YAMLの定義を置き換えるものではない |

同梱の版・profileの正本は7本のComposeとlockで、上の候補製品の文書ではない。新規候補を実装する際は、[COMPATIBILITY.md](COMPATIBILITY.md) と同じく版固定、秘密設定、保存・復元、小規模読み書き試験を追加する。

## 追加GUI製品の採用試験を一つの成果物にする

未同梱のHop/NiFi/Airbyte等を評価する場合、画面が開いたことに加えて次を一組で残します。

1. **入力の境界:** 専用の合成CSVまたは専用DB、主キー、更新/削除の意味を決める。
2. **保存の境界:** 書込先namespaceを分け、REST catalogとS3の両方へ必要な権限だけで接続する。
3. **再実行の境界:** 同じ素材、更新版、途中失敗、再起動で件数がどう変わるか確認する。
4. **配布の境界:** GUIで作った定義をexportし、資格情報を除いてレビューできる形へする。
5. **運用の境界:** キュー/実行履歴/定義/鍵/DBの保存・復元と、proxy/CAの主体を決める。

列処理のGUI化を選んでも、レビューする単位は消えません。誰が変更でき、どの定義をどの環境へ配置し、どの期待値で受け入れたかを残します。比較表の候補は、この試験を完了するまでは「今回実装したGUI」として報告しません。

### 操作しやすさとAIの権限を合わせて設計する

同梱APIは固定action、MCPは既定read-onlyです。n8nのHTTPノード、Notebook、追加GUI製品のSQL実行器は、設定次第でより広い操作を持ちます。AIクライアントには、まずcatalog/status/品質/既知jobの参照から渡し、任意SQL・任意URL・shell実行を同じ権限へまとめません。

選定メモには「その画面が何を保存するか」「再実行で何が変わるか」「設定を公開せず再配置できるか」を記載します。[意思決定の雛形](reports/DECISION-RECORD-TEMPLATE.md) に採用理由と未確認を残すと、機能数だけの比較になりにくくなります。

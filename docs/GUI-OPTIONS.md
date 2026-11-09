# GUIで設定・操作する選択肢

調査日: 2026-11-09。現在の操作入口は **Streamlit + FastAPI** で、素材投入、設定保存、状態確認、登録済み処理、AI context取得を提供する。[操作GUI](DEMO-GUI.md) と [API/Swagger](API.md) に手順をまとめている。n8nは実行手順の可視化・連携に使い、CDCの管理にはKafbat UIも提供する。Bronzeの取り込みはPyIceberg、Silver/Goldの変換はTrino SQLとしてGit管理し、業務ルールはSQLとJSONのデータ契約で定義する。

GUIで「実行順序やタイミングを組む」ことと、「列の型・結合・集計を画面で定義する」ことは分けて選ぶ。後者まで必要ならApache Hopが有力な追加候補となる。以下は公式資料で確認した製品機能の比較であり、この基盤との接続試験が済んだという意味ではない。

## 候補比較

| 候補 | GUIで設定できる範囲 | Iceberg RESTへの接続 | 追加する運用対象・適用先 |
| --- | --- | --- | --- |
| **n8n：同梱の連携GUI** | 手動/定期起動、API呼び出し、分岐、通知。列の結合・型変換・集計は登録済みSQL/JSON契約で定義 | n8nはジョブAPIまたはTrinoのHTTP APIを呼ぶ。RESTカタログとS3への接続はPyIceberg/Trino側に集約 | 実装済みのautomationプロファイルを利用。大量の明細をn8n内に展開せず、結果件数・状態を渡す |
| **Apache Hop** | パイプライン画面で入力・列選択・計算・条件・結合等を組む。SQL実行ステップもある | 最新manualのLake table input/outputはHop engineでIcebergのRESTカタログに対応。TABLEモードでカタログとS3を設定する | GUIとJavaの実行環境。ブラウザ運用ならHop Web/Serverも対象。Sparkは必要な機能だけで追加。変換定義を画面中心に作りたい場合 |
| **Apache NiFi** | ブラウザでファイル/DB取得、Record変換、経路分岐、失敗時の処理を組む。細かいルールにはRecordPath/式/SQLを入力 | PutIcebergRecord + RESTIcebergCatalog + S3 FileIO/Parquet writer/Record readerを設定 | JVM、キュー、Content/FlowFile/Provenanceリポジトリの容量を管理。継続取り込みや多数の経路・再送を可視化したい場合 |
| **Airbyte** | 接続元/先、表・列、同期方式、実行間隔を画面で設定。任意の業務集計を描くGUIではない | MSSQL sourceはCDC対応。S3 Data Lake destinationはS3互換保存先とRESTカタログに対応 | Kubernetes/ジョブ実行基盤。SQL Server等からの複製を製品化したコネクターに任せたい場合。今回のComposeにそのまま1コンテナ追加する構成ではない |

n8nのHTTP設定は [HTTP Request node](https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.httprequest/) を参照。Hopの画面変換は [Pipelines](https://hop.apache.org/manual/latest/pipeline/pipelines.html)、RESTカタログへの出力は [Lake table output](https://hop.apache.org/manual/latest/pipeline/transforms/spark-lake-table-output.html) を参照。NiFiの構成要素は [PutIcebergRecord](https://nifi.apache.org/components/org.apache.nifi.processors.iceberg.PutIcebergRecord/) と [RESTIcebergCatalog](https://nifi.apache.org/components/org.apache.nifi.services.iceberg.catalog.RESTIcebergCatalog/) で確認できる。Airbyteの対応は [MSSQL source](https://docs.airbyte.com/integrations/sources/mssql) と [S3 Data Lake destination](https://docs.airbyte.com/integrations/destinations/s3-data-lake) を参照。

## 今回のn8nとメダリオン処理

実装方針は次の役割分担とする。実際の起動手順と検証済み範囲はREADME/DEPLOYMENTと実行結果を正とする。

1. **Bronze**：受領CSV/JSON/画像の原本をS3へ保管し、ハッシュ・受領情報と取り込みレコードをPyIcebergで保存する。
2. **Silver**：Trino SQLで型、必須項目、重複等を整理する。採用するキーと不正行の扱いをSQL/JSON契約に残す。
3. **Gold**：Silverから業務で使う集計をTrino SQLで生成する。集計粒度と対象期間を定義する。
4. **n8n画面**：手動起動またはスケジュール → 処理API → 結果確認/分岐という流れを表示する。処理APIがBronze→Silver→Goldの順序と失敗判定を管理する。

運用者は画面から実行・結果確認を行い、変換内容の変更はSQL/JSONの差分としてレビューできるようにする。任意のSQLを一般利用者がWebhookへ渡す方式は避け、登録済みの処理と許可したパラメーターを呼び出す。

Trinoをn8nから直接HTTPで呼ぶ方法もあるが、`POST /v1/statement` のHTTP 200で完了とは判断しない。応答の `error` を検査し、`nextUri` がある間はGETを継続して終了を確認する。この処理をジョブAPIにまとめれば、n8n側のフローを簡潔に保てる。[Trino client REST API](https://trino.io/docs/current/develop/client-protocol.html)

## Hop / NiFi / Airbyteを追加する条件

**Hop** は、SQLを書かずに列処理を組む要求が増えた段階で小さなCSVを使って試す。Hop engineのIceberg入出力ではLakehouseプラグインの有無と版を固定する。カタログ管理の表へPATHモードで直接書かず、RESTカタログのTABLEモードを使う。最新資料の対応だけで既存の配布イメージにも機能があるとは判断しない。[Hop 2.20リリース](https://hop.apache.org/blog/2026/11/hop-2.20.0/) / [Lake table input](https://hop.apache.org/manual/latest/pipeline/transforms/spark-lake-table-input.html)

HopからTrino JDBCを登録してSQLを実行する案も可能性がある。HopのGeneric接続はdriver classとURLを設定でき、TrinoはJDBC driverを提供する。ただしHopとTrinoを組み合わせた実行は本リポジトリでは未検証で、SQL方言やメタデータ取得の確認が必要。これはSQL入力が必要な経路である。[Hop接続設定](https://hop.apache.org/manual/latest/metadata-types/rdbms-connection.html) / [Trino JDBC](https://trino.io/docs/current/client/jdbc.html)

**NiFi** は、再送・滞留・取り込み経路を画面で管理したい場合に試す。CSVReader/JsonTreeReaderとRecord変換を構成し、Icebergへの書き込みを行う。NiFiのQueryRecordによる集計は個々のFlowFile内が範囲となるため、全履歴に対するGold集計はTrino等のテーブル処理へ渡す。[UpdateRecord](https://nifi.apache.org/components/org.apache.nifi.processors.standard.UpdateRecord/) / [QueryRecord](https://nifi.apache.org/components/org.apache.nifi.processors.standard.QueryRecord/)

**Airbyte** は、会社のSQL Serverへの複製接続をまとめたい場合に試す。CDCにはソースDB/表の有効化と権限設定が別途必要で、GUIの接続だけでは完了しない。標準の推奨リソースは4 CPU以上・8GB以上のメモリで、abctlも内部にKubernetesを作成する。core基盤や変換ジョブと別に予算を取る。[公式Quickstart](https://docs.airbyte.com/platform/using-airbyte/getting-started/oss-quickstart) / [abctl](https://docs.airbyte.com/platform/deploying-airbyte/abctl)

## RESTカタログで共通して確認すること

Hop/NiFi/Airbyteをこの基盤へつなぐ場合、カタログURI、Warehouse識別子、namespace、必要な認証を設定する。さらに、クライアントからS3 endpointへ到達できること、region/path-styleとS3資格情報が正しいことを確認する。カタログへの疎通だけではParquetの読み書き成功を意味しない。資格情報の払い出しを使う場合はカタログと保存先双方の対応を確認し、静的資格情報方式と混在させない。

これらの追加製品からLakekeeper/SeaweedFSへの実接続は未検証。導入判断の前に、合成データの書き込み→Trino参照→再試行→重複/更新の扱い→再起動後の継続を小規模に確認する。会社プロキシとCA設定は [PROXY.md](PROXY.md) に従う。

## dbt + Dagsterを選ぶ場合

SQLモデルが増え、依存関係、部分再実行、実行履歴を管理したくなったら追加候補となる。DagsterのUIはdbtモデルをassetとして表示し、選んだモデルの実行や失敗・ログの確認ができる。**変換の定義にはdbtのSQL/YAML等が必要**で、ここでは列変換を全てドラッグ操作で定義する代替とは扱わない。Trino用adapterと各製品の互換版も別途検証する。[Dagsterとdbt](https://docs.dagster.io/integrations/libraries/dbt)

## ライセンスとリソースの考え方

Hop/NiFiはApache License 2.0のOSS。n8n CommunityはSustainable Use Licenseで、社内業務利用を想定した条件があり、OSI型のOSSと同じ条件とは扱わない。Airbyteのリポジトリ本体はELv2で、connectorや有償機能の条件も確認する。外部向けサービス提供ではそれぞれの条件を確認する。[Hop LICENSE](https://github.com/apache/hop/blob/master/LICENSE) / [NiFi LICENSE](https://github.com/apache/nifi/blob/main/LICENSE) / [n8n LICENSE](https://github.com/n8n-io/n8n/blob/master/LICENSE.md) / [Airbyte LICENSE](https://github.com/airbytehq/airbyte/blob/master/LICENSE)

Dagster OSSとdbt CoreはApache 2.0を基準に選び、商用クラウド版等と区別する。[Dagster LICENSE](https://github.com/dagster-io/dagster/blob/master/LICENSE) / [dbt Core公式](https://github.com/dbt-labs/dbt-core/blob/main/docs/roadmap/2025-05-new-engine-same-language.md)

Airbyte以外の必要メモリは、この環境で測定していない。n8nにはフローと実行履歴、HopにはJVMと変換中のデータ、NiFiにはJVMに加えて永続キューと来歴、DagsterにはUI/daemonと実行ワーカーの予算が必要、という運用上の見積もりで比較する。入力件数・同時実行・保持期間を決めてから実測し、GUI追加だけでデータ処理の負荷が軽くなるとは判断しない。

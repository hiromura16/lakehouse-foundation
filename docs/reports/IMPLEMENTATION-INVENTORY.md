# 報告書用の実装構成一覧

<!-- doc-purpose:start -->
> **役割:** 34サービス・11profile・port・volume・接続主体を公開定義から技術付録として照合する。
>
> **対象読者:** 基盤構築担当 / 開発・設計担当 / 評価・報告担当
>
> **読むタイミング:** 詳細図の箱と線を確認するときや、構成定義の数量と制約を引用するとき。
>
> **前提となる資料:** [Lakehouse Foundation アーキテクチャ](<../ARCHITECTURE.md>)
>
> **次に読む資料:** [バージョンと連携互換性](<../COMPATIBILITY.md>) / [報告する主張と根拠の対応表](<EVIDENCE-MATRIX.md>)
>
> **文書一覧:** [目的・読者から探す文書マニフェスト](<../DOCUMENTATION-MANIFEST.md>)
<!-- doc-purpose:end -->

照合日: 2026-11-10（JST）。照合基点は文書監査を反映した `b63b9d0` です。公開Compose・固定lock・同梱コードを読み取り、詳細構成図の各箱と線を確認するための技術付録として整理しました。導入先の実ホスト名・接続先・保存パス・秘密値は含めません。稼働環境への接続や再配置は行っていません。

[素材一覧](README.md) から目的の図を選びます。過去の実機結果は [verification.json](../verification.json)・[VALIDATION.md](../VALIDATION.md)、採用版の公式資料は [REFERENCES.md](../REFERENCES.md) を参照してください。以下の個数は構成定義の数であり、性能値や常時起動数ではありません。

## 1. 数え方と配置境界

| 項目 | 公開定義から確認できる値 | 図・報告書での表し方 |
| --- | --- | --- |
| アプリのCompose | 7ファイル | lakehouse → compute → orchestration → demo → cdc → poc → mcpの順で同じprojectへ結合 |
| service定義 | 34 | core 6＋追加定義28。34個の常駐アプリとは表現しない |
| 任意profile | 11 | automation、monitoring、compute、orchestration、demo、cdc、bi、notebooks、vector、lineage、mcp |
| 一時初期化 | 4service | catalog-migrate、spark-volumes-init、cdc-register、superset-init。正常時は終了コード0 |
| 明示smoke | 1service | spark-check。通常upはreplicas=0、CLIから明示実行 |
| buildを持つservice | 8 | worker、spark-volumes-init、airflow、demo-api、cdc-register、superset-init、jupyter、mcp |
| 名前付きvolume | 16種類の定義 | 必須coreが参照するのはcatalog-db/storage/trinoの3種。残りは選択profileの状態保存 |
| ホスト上の別構成 | Dokploy、Samba、daemon proxy/CA | アプリ34serviceとは別の管理領域として描く |
| 業務DB | 外部SQL Server | 会社の既存DB。公開ComposeからSQL Server本体は起動しない |
| 環境ごとの識別 | COMPOSE_PROJECT_NAME、生成後のDokploy appName | 所属Environment名だけでは保存先・port・鍵が分離されない |

DokployはSwarm上の管理サービスと専用PostgreSQL、管理画面を制限するNginx、Traefikを別途準備します。レイクハウス本体は **Docker Compose** で配置し、Swarm Stackとして配備する手順ではありません。全アプリserviceは `traefik.enable=false` で、標準Composeだけで利用者向けHTTPS入口が自動構成されるわけではありません。[Dokploy手順](../DOKPLOY.md)・[installer](../../infra/dokploy/install.sh)

## 2. 34serviceと起動依存

`healthy` は依存先のhealthcheck成功、`完了0` はone-shotの正常終了、`開始` はcontainerの開始です。ここはComposeの `depends_on` だけを示します。実行中にHTTP/DBへ接続する関係とは分けて図示してください。依存条件の成功だけで認証・業務データ・復元を検証したことにはなりません。

| service | profile | 役割 | Composeに宣言された起動依存 |
| --- | --- | --- | --- |
| catalog-db | core | Lakekeeper用PostgreSQL | なし |
| storage | core | SeaweedFSのS3・filer・原本/表データ | なし |
| catalog-migrate | core | カタログDBのschema移行 | catalog-db healthy |
| catalog | core | Iceberg REST catalog | catalog-migrate 完了0 |
| trino | core | SQL実行・Iceberg読書き | catalog healthy |
| worker | core | bootstrap、ファイル取込、メダリオン、verify | catalog healthy、storage 開始 |
| n8n-db | automation | workflow/credentials用PostgreSQL | なし |
| n8n | automation | GUIのworkflowとworker依頼 | n8n-db healthy |
| prometheus | monitoring | worker/自身の指標をscrape | なし |
| grafana | monitoring | 指標可視化、GUI編集を保持 | なし |
| spark-volumes-init | computeまたはnotebooks | 共通image build、専用volumeルートの権限準備 | なし |
| spark-master | compute | standaloneクラスタ受付 | spark-volumes-init 完了0 |
| spark-worker | compute | executorの起動・処理 | spark-volumes-init 完了0、spark-master healthy |
| spark-history | compute | event logから完了アプリを表示 | spark-volumes-init 完了0 |
| spark-check | compute | 明示実行だけのlocal[2]互換smoke | なし。通常はreplicas=0 |
| airflow-db | orchestration | Airflow metadata DB | なし |
| airflow | orchestration | standalone / LocalExecutorの手動DAG | airflow-db healthy |
| demo-api | demoまたはmcp | 操作API、設定保存、素材ジョブ | なし。worker接続は要求時に検査 |
| demo | demo | Streamlit操作ハブ | demo-api healthy |
| cdc-kafka | cdc | 単一broker/controllerのKafka KRaft | なし |
| cdc-connect | cdc | Debezium SQL Server connector | cdc-kafka healthy |
| cdc-ui | cdc | Kafbatのtopic/lag/Connect管理 | cdc-connect healthy |
| cdc-register | cdc | 固定設定のconnector登録・既存一致確認 | cdc-connect healthy |
| cdc-consumer | cdc | raw保持、現在値、cdc系メダリオン、checkpoint | worker/catalog/trino healthy、cdc-register 完了0 |
| superset-db | bi | BI設定・Dataset・Chart・Dashboard用DB | なし |
| superset-redis | bi | Superset cache | なし |
| superset-init | bi | migration、初期admin、接続・Gold2Dataset準備 | superset-db/superset-redis/worker/trino healthy |
| superset | bi | SQL Lab / Explore / Dashboard | superset-init 完了0 |
| jupyter | notebooks | Notebook、Trino、Spark driver | worker/trino healthy、spark-volumes-init 完了0。spark-master/worker healthyは任意依存 |
| qdrant | vector | vector保存、近傍検索、native Console | なし |
| marquez-db | lineage | 系統情報用PostgreSQL | なし |
| marquez | lineage | lineage API | marquez-db healthy |
| marquez-web | lineage | lineage GUI | marquez healthy |
| mcp | mcp | 固定tool/resourceをStreamable HTTPで公開 | demo-api healthy |

`notebooks` だけならJupyterのSparkはlocal[2]です。`compute` も選ぶとstandalone Masterへ接続し、接続失敗を黙ってlocalへ切り替えません。`mcp` だけでもAPIは起動しますが、Streamlit画面には別途 `demo` が必要です。`cdc-consumer` は連続処理のプロセスですが、異常時の自動再起動は `restart: 'no'` です。[Spark実装](../../infra/poc/lakehouse_session.py)・[CDC Compose](../../infra/cdc/compose.yaml)

正本: [core](../../infra/lakehouse/compose.yaml)・[compute](../../infra/compute/compose.yaml)・[Airflow](../../infra/orchestration/compose.yaml)・[demo](../../infra/demo/compose.yaml)・[CDC](../../infra/cdc/compose.yaml)・[PoC tools](../../infra/poc/compose.yaml)・[MCP](../../infra/mcp/compose.yaml)。

## 3. 公開portと内部portを分ける

次は公開定義の既定値です。実際の会社アドレスを示すものではありません。`BIND_IP` の既定はloopbackです。Spark・CDC・追加PoCツールは `BIND_IP` を変更しても公開bindがloopback固定です。他端末ではSSH転送等、会社で認められた入口を別途用意します。

| service / 入口 | host portの設定key・既定 | container内port | host bind |
| --- | --- | --- | --- |
| storage S3 | S3_PORT / 8333 | 8333 | BIND_IP |
| catalog REST/UI | CATALOG_PORT / 8181 | 8181 | BIND_IP |
| trino SQL/UI | TRINO_PORT / 8080 | 8080 | BIND_IP |
| n8n | N8N_PORT / 5678 | 5678 | BIND_IP |
| prometheus | PROMETHEUS_PORT / 9090 | 9090 | BIND_IP |
| grafana | GRAFANA_PORT / 3000 | 3000 | BIND_IP |
| airflow | AIRFLOW_PORT / 8081 | 8080 | BIND_IP |
| demo Streamlit | DEMO_GUI_PORT / 8501 | 8501 | BIND_IP |
| demo-api / Swagger | DEMO_API_PORT / 8502 | 8000 | BIND_IP |
| mcp HTTP | MCP_PORT / 8503 | 8001 | BIND_IP |
| spark-master UI | SPARK_MASTER_UI_PORT / 8082 | 8080 | loopback固定 |
| spark-worker UI | SPARK_WORKER_UI_PORT / 8085 | 8081 | loopback固定 |
| spark-history UI | SPARK_HISTORY_UI_PORT / 18080 | 18080 | loopback固定 |
| cdc-connect REST | CDC_CONNECT_PORT / 8083 | 8083 | loopback固定 |
| cdc-ui Kafbat | CDC_UI_PORT / 8084 | 8080 | loopback固定 |
| superset | SUPERSET_PORT / 8088 | 8088 | loopback固定 |
| jupyter | JUPYTER_PORT / 8888 | 8888 | loopback固定 |
| qdrant REST / dashboard | QDRANT_PORT / 6333 | 6333 | loopback固定 |
| marquez API | MARQUEZ_API_PORT / 5000 | 5000 | loopback固定 |
| marquez-web | MARQUEZ_UI_PORT / 3001 | 3000 | loopback固定 |

以下はアプリのComposeネットワーク内だけで使い、標準ではhostへpublishしません。

| 内部接続 | portと用途 |
| --- | --- |
| worker HTTP | 8000 /run、/health、/metrics。n8n・Airflow・API・Prometheusから接続 |
| PostgreSQL 5系統 | catalog-db/n8n-db/airflow-db/superset-db/marquez-dbの各5432。DBは共用しない |
| superset-redis | 6379。永続正本ではなくcache |
| cdc-kafka | 9092 broker、9093 controller。Connect/consumer/UIは内部名を使う |
| Spark RPC | Master 7077、Worker 7078 |
| Spark driver / block manager | Jupyter 39000 / 39001。executorからのcallback用 |
| Spark application UI | driver 4040。MasterのreverseProxyを経由 |
| Marquez管理health | 5001。GUI公開APIの5000とは別 |

Dokploy管理画面はアプリの上表とは別で、`DOKPLOY_ADMIN_PORT` の既定3100と管理用bind/allowlistを使います。Sambaはホスト上の共有設定であり、34serviceの公開portには含めません。SQL Serverも `SOURCE_SQLSERVER_PORT` / `CDC_SQLSERVER_PORT` / `DEMO_SQLSERVER_PORT` の外部TCP接続です。HTTP proxyへSQLのTCP接続を送る構成ではありません。

## 4. 名前付きvolume全16種類

物理名は各volumeの `name` 定義で決まり、通常は `COMPOSE_PROJECT_NAME`、移行時は `DATA_VOLUME_PREFIX` の接頭辞を使います。Dokployの生成appNameを変更しただけで既存データが移動するわけではありません。[既存データの引継ぎ](../DOKPLOY.md#既存volume名を引き継ぐ場合)

| volume key | 主な利用service / mount先 | 保持する内容 |
| --- | --- | --- |
| catalog-db | catalog-db / PostgreSQL領域 | Iceberg REST catalog、ストレージ資格情報の暗号化されたDB |
| storage | storage /data | 受領原本bucket、Icebergのdata/metadata bucket、filer状態 |
| trino | trino /data/trino | Trinoのローカル作業領域。Icebergの正本ではない |
| n8n-db | n8n-db / PostgreSQL領域 | workflow、実行情報、暗号化されたcredentials |
| n8n | n8nのユーザー領域 | n8n自身の設定・関連ファイル |
| prometheus | prometheus /prometheus | TSDB。設定は15日/2GBの保持制限 |
| grafana | grafana /var/lib/grafana | GUIで保存したDashboard、ユーザー等のGrafana DB |
| spark-events | init/master/worker/history/jupyterの /opt/spark/events | Spark event log。History再表示の根拠 |
| spark-work | init/master/worker/historyの /opt/spark/work | daemon log、worker作業、History store |
| airflow-db | airflow-db / PostgreSQL領域 | DAG実行・Connection等のmetadata |
| airflow-state | airflow /opt/airflow | standalone状態、初期Simple Authのpassword file等 |
| cdc-kafka | cdc-kafka /var/lib/kafka/data | source topic、Connect内部topic、schema history等 |
| cdc-state | cdc-consumer /state | SQLiteのソース現在値、identity、完了checkpoint、blocked状態 |
| superset-db | superset-db / PostgreSQL領域 | GUIのDataset/Chart/Dashboard、認証/接続情報 |
| qdrant | qdrant /qdrant/storage | collection、vector、payload |
| marquez-db | marquez-db / PostgreSQL領域 | namespace、dataset、job、runと系統情報 |

Redisには名前付きvolumeを割り当てていません。JupyterのNotebook本体は下表のhost bindです。Grafanaの初回JSONはseedであり、file dashboard providerの継続上書きではありません。

バックアップはvolumeだけでなく、カタログ暗号鍵、n8n暗号鍵、Airflow Fernet key、private設定、S3全体と整合した停止点が必要です。Dokploy自身の管理volumeと設定は別に保管します。現在の単一ホスト構成でHAやRPO/RTOを実証済みとは扱いません。[運用・復元](../OPERATIONS.md)

## 5. host bindと公開設定ファイル

| host側の設定変数/公開ファイル | container内での役割 | アクセス境界 |
| --- | --- | --- |
| LANDING_DIR | workerの /landing | read-only、存在を事前確認。workerは素材を書き換えない |
| LANDING_DIR | demo-apiの /landing | 合成素材・manifestを生成するためread/write |
| CONTROL_SETTINGS_DIR / CONTROL_SETTINGS_NAME | demo-apiの /private 配下 | 専用0700 dir/0600 fileをread/write。atomic保存用にdir単位mount |
| NOTEBOOKS_DIR | jupyter /workspace | 実行UID/GIDがread/write。Notebook出力は業務情報を含み得る |
| CDC_CONFIG_DIR | register/consumer /run/cdc | 実表の対応JSON等をread-only。Git外で保持 |
| CDC_SQLSERVER_TRUSTSTORE_FILE | Connect専用のtruststore | 指定したCA truststoreだけread-only。設定dir全体は渡さない |
| CORPORATE_CA_FILE | n8nの追加CA | 指定したPEMだけread-only。未指定はexact0B placeholder |
| corporate_ca build secret | 自作imageのOS/JVM信頼設定 | build時だけ入力。追加CAへの信頼は完成imageに残る |
| infra/lakehouseのTrino設定 | Trino catalog/JVM/node/config | Compose configsとして指定。実S3鍵は環境変数から参照 |
| Prometheus/Grafana datasource設定 | scrape / datasource | configsで配布。DashboardのGUI保存とは別 |
| infra/poc/marquez.yaml | Marquez API設定 | configsで配布 |

MCPはhost bindを持たず、read-only root filesystemと制限付きtmpfsを使います。Streamlitの `demo` にもhost bindはありません。GUI→APIがprivate設定と素材へ接続する境界を図に入れます。GUI/API自身にはDocker socket・sudo・Dokploy管理鍵を渡していません。

## 6. 実行時の線を描くための接続表

| 起点 → 終点 | 種類 | 実装する処理・境界 |
| --- | --- | --- |
| Sambaクライアント → host landing | データ | 完成したCSV/JSON/画像を置き、readyを最後に公開 |
| host extractor → 外部SQL → landing | データ | 許可6表/列のSELECTと全件素材化。ソースへのDMLなし |
| demo GUI → demo-api → landing/DEMO SQL | 制御＋素材 | batch/有限stream/26異常。生成だけでpipelineを開始しない |
| demo-api → worker:8000/run | 制御 | 明示ingest/medallion/verify。worker tokenで認証、非同期IDでpoll |
| n8n/Airflow → worker:8000/run | 制御 | 同梱は手動開始。credential/Connectionにworker tokenを保持 |
| worker → storage | データ | ready/hash照合、原本保存、Bronze Iceberg更新 |
| worker → catalog | metadata | 起動bootstrap、warehouse/namespace/table、PyIceberg REST |
| worker → trino → catalog/storage | 制御＋SQL/データ | Silver6＋Quarantine6＋Gold2、14SQLを表単位で置換 |
| 外部SQL → cdc-connect → cdc-kafka | データ | CDCのsnapshotとI/U/D、Debezium envelope、topic/schema history |
| cdc-kafka → cdc-consumer → catalog/storage/trino | データ | raw→SQLite現在値→cdc系メダリオン→local checkpoint→Kafka offset |
| cdc-ui → Kafka/Connect | 管理参照 | topic、lag、connector/task状態・設定 |
| jupyter driver → Spark Master/Worker | 計算制御＋データ | remote executor、callback。compute未選択ならlocal[2] |
| Spark/Jupyter → catalog/storage | データ | 同じREST warehouseとS3を使うIceberg読書き |
| Superset → Trino | データ参照 | Gold2Dataset、SQL Lab、Chart/Dashboard |
| demo-api/AI CLI → Trino | データ参照 | 固定Gold、main snapshotを各表固定、各表最大500行 |
| AI client → MCP → demo-api | 制御・参照 | 9read tools/2resources。3writeは明示opt-inと個別confirm |
| Prometheus → worker/自身 | 指標参照 | 15秒scrape。host/DB/全CDC用exporterは未同梱 |
| Grafana → Prometheus | 指標参照 | provisioned datasource。DashboardはGUIで編集・保存 |
| Qdrant/Marquezの手動seed | 任意デモ | 合成vector/手動lineageのみ。embedding/実ジョブ自動収集とは別 |

ファイル系は `bronze/silver/quarantine/gold`、CDCは `cdc_events/cdc_bronze/cdc_silver/cdc_quarantine/cdc_gold` の別namespaceを使います。CDC rawの識別はtopic/partition/offsetで、全処理を一つの分散transactionにする仕組みではありません。workerのlock、APIのmutex、CDC stateのfile lockは各プロセスの境界であり、任意の外部writerまで全体排他するものではありません。[consumer](../../infra/cdc/consumer.py)・[worker](../../scripts/admin.py)・[API](../../infra/demo/api.py)

## 7. proxy・CA・SSRFを別の線で描く

| 接続主体 | proxy/CA設定の場所 | 図に添える説明 |
| --- | --- | --- |
| Git/OS package/uv取得を行うhost | 会社標準の端末・OS設定 | source取得までの前提。アプリenvで一括設定できない |
| Docker daemon | daemon用proxy6項目とhostの信頼CA | registryからのpull/push。appのNO_PROXYと管理主体を分ける |
| BuildKit / 自作Dockerfile | HTTP(S)両case、canonical由来NO_PROXY、corporate_ca | Python/npm/Maven等のbuild取得。image/lock/checksumを保持 |
| Python/Nodeの実行HTTP | service環境と各runtimeのtrust | 外向きのみ会社proxy、内部名は直接。各clientの解釈を受入 |
| Spark/Trino/Airflow等のJava | JVMの信頼ストア、client固有設定 | shell proxy変数だけで全JVM通信が設定されるとは扱わない |
| SQL Server TCP / JDBC | DB向けTCP経路、CA/truststore、証明書名 | HTTP proxy経路と分離。TLS検証を維持 |
| n8n HTTP Request | n8nのSSRF設定＋runtime通信設定 | NO_PROXYは経路、SSRFは宛先許可。既定の内部例外はworkerのみ |

`LAKEHOUSE_NO_PROXY` が生成・upgradeで正規化するcanonical値です。必須内部名と会社追加先をまとめ、NO_PROXY/no_proxyへ同期します。保存と反映は別で、DokployのサービスEnvironmentとhost private設定は自動同期しません。[会社proxy](../COMPANY-PROXY.md)・[SSRF](../SSRF.md)

## 8. 採用版・実装済み・追加評価の区分

| 組 | 採用版の要点 | 正本 |
| --- | --- | --- |
| core | SeaweedFS 4.53、Lakekeeper 0.14.3、Trino484、PostgreSQL18.6、worker Python3.14/PyIceberg0.12.0 | [core lock](../../infra/lakehouse/images.lock.json)・[worker依存](../../infra/lakehouse/requirements.txt) |
| 計算 | Spark4.1.4/Scala2.13/Java21、Iceberg runtime4.1版1.12.0＋AWS bundle同版 | [compute lock](../../infra/compute/images.lock.json) |
| orchestration | Airflow3.4.0 Python3.12、standard1.21.0、HTTP6.3.0、common-compat1.23.0 | [Airflow lock](../../infra/orchestration/images.lock.json) |
| 操作 | Streamlit1.67.0、FastAPI0.162.2、Uvicorn0.54.0、pymssql2.4.6 | [demo依存](../../infra/demo/requirements.txt) |
| 自動化/監視 | n8n2.46.7、Grafana13.3.0、Prometheus3.16.0 | [core lock](../../infra/lakehouse/images.lock.json) |
| CDC | Kafka4.4.0、Debezium3.7.1.Final、Kafbat1.5.0 | [CDC lock](../../infra/cdc/images.lock.json) |
| 分析拡張 | Superset6.1.0、Redis8.10.3、Qdrant1.20.0、Marquez0.50.0、JupyterLab4.6.6 | [PoC lock](../../infra/poc/images.lock.json)・[Notebook依存](../../infra/poc/requirements-notebooks.txt) |
| AI/環境構築 | MCP SDK2.3.0、uv0.13.7、LangChain独立lock | [MCP依存](../../infra/mcp/requirements.txt)・[uv lock](../../infra/uv/images.lock.json)・[AI lock](../../examples/ai/requirements.lock) |

| 区分 | 報告書へ載せる内容 |
| --- | --- |
| 実装済み | ファイル/SQL素材、ready、メダリオン、任意CDC、常駐Spark、GUI/API、固定AI context/MCP、各OSSの小規模デモ |
| 記録済みの検証 | 合成データの11検査、再投入skip、CDC I/U/Dと再開、Spark/Trino相互参照、GUI保存等。日付と範囲はverification.jsonを引用 |
| 模擬試験 | 認証付きCONNECT・CA欠落・SSRF拒否等。実会社proxy/IdP全体の成功へ読み替えない |
| 未評価 | 会社の実DB/実proxy/IdP、実負荷・長期耐久、HA、バックアップ復元・RPO/RTO、モデル推論 |
| 未実装/選択案 | OCR→embedding→RAGの一貫処理、権限/削除同期、実ジョブlineage自動収集、host/DB exporter、Cloudflare/ClickHouseの実配備 |

Qdrantの3点検索は合成数値の機能疎通で、意味検索精度の評価ではありません。Marquezは手動seedの系統表示で、自動収集の実証ではありません。Gold contextは表ごとのsnapshot固定で、複数表の同一transactionや、そのsnapshotを生成した変換SQL版の証明は別課題です。[AIの境界](../AI-READINESS.md)・[実装追跡](../IMPLEMENTATION-STATUS.md)

## 9. 資源上限の見積もりに使う値

以下はComposeの `mem_limit` をMiBへ換算して足した**設定上限**です。測定した常時使用量、必要RAMの保証、性能試験の結果ではありません。OS・Docker・Dokploy・build・ファイルcache等の使用量は含めません。ホスト容量と同時実行の可否は導入先で測定します。

| 選択対象 | 常駐serviceの上限合計 MiB | one-shotの上限合計 MiB | 数える際の注意 |
| --- | ---: | ---: | --- |
| core | 4224 | 256 | 必須6定義、migrationを含む |
| automation | 1408 | 0 | n8nと専用DB |
| monitoring | 768 | 0 | PrometheusとGrafana |
| compute | 5888 | 128 | smokeは通常起動しないため別枠 |
| orchestration | 4480 | 0 | Airflow standaloneと専用DB |
| demo | 1280 | 0 | demo-apiはmcpと共有 |
| cdc | 3328 | 256 | consumerは異常時に終了する設定 |
| bi | 2752 | 1536 | initのピークと本体/DB/cacheを分ける |
| notebooks | 4096 | 128 | initはcomputeと共有。remote executorはcompute側 |
| vector | 1024 | 0 | Qdrant |
| lineage | 1792 | 0 | Marquez API/Web/DB |
| mcp | 768 | 0 | mcp本体256＋共有demo-api512 |
| 全serviceを一意に集計 | **31296** | **2176** | 29常駐＋4one-shot。共有serviceを二重計上しない |
| 明示spark-check | 2048 | — | 前行に含めない、一時的なlocal smoke |

全常駐の設定上限は約30.56GiBです。profile別の行には共有serviceがあるため、そのまま全行を足して全体値にしません。Spark Workerのcontainer上限4GiBと、クラスタへ広告するexecutor用RAM2GiBも別の値です。Jupyter driverとexecutorのメモリは、それぞれのcontainer側で見ます。

図や報告書では「coreから開始し、目的別profileを段階追加」「構成上限と実測値は別欄」と表記し、未測定のスループット・応答時間・費用削減率を埋めません。性能と受入の記録様式は [評価計画](EVALUATION-PLAN.md) を使います。

## この一覧から稼働環境を照合する

初心者はserviceの名前より先に、core、追加profile、初期化one-shot、明示smokeの4種類を区別します。34は定義数で、すべてが常駐するという意味ではありません。必要なprofileだけを選び、one-shotの正常終了を異常停止と混同しません。

| 照合するもの | 公開側 | 導入先で確認するもの |
| --- | --- | --- |
| 構成 | 7つのCompose、選択profile | 実際のCommand、サービスenv、配置commit |
| 保存 | volume keyとbindの役割 | 確定appName由来の実volume、privateの保存先、UID/GID |
| 通信 | container名/内部port、host bind | 会社の接続制限、proxy bypass、SSH転送 |
| 資源 | 宣言されたlimit | 起動/build/通常/負荷時の実使用量と空き |
| 機能 | 起動依存と実装入口 | health、代表job、業務照合、停止・再開後の状態 |

公開の既定portを別環境でもそのまま使うと衝突します。Environmentを分けるだけでなく、project/appName、volume、private設定、landing/workspace、host port、鍵の組を確認します。実値の対応表は社内で管理します。

## 構成変更時の追従

serviceを追加/削除したら、起動依存、NO_PROXY、health、資源、永続化、GUI入口、役割別の図を更新します。Composeの宣言と稼働結果は別の根拠なので、定義数を更新しただけで配備済みとは記録しません。図の基点が過去のcommitなら、差分と再照合の範囲を明記します。

上級者は [CUSTOMIZATION](../CUSTOMIZATION.md) の追加profileの手順と [COMPATIBILITY](../COMPATIBILITY.md) の版の組合せを使います。復旧対象はこの表のvolumeだけでなく、外部bind、DB、暗号鍵、GUIの保存物も含め、[運用手順](../OPERATIONS.md) で一組として確認します。

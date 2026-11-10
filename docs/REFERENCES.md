# 採用版の公式リファレンスと文書監査

監査日: **2026-11-10（JST）**。初回の操作順は [START-HERE.md](START-HERE.md) です。このページは、各手順がどの版・公式資料・リポジトリ内実装を根拠にしているかを示します。文書監査のためにimageや依存版を更新していません。

## 参照するときのルール

1. 実際のimageは `infra/*/images.lock.json` のtag＋digest、Pythonはrequirements/constraintsとhash lock、Spark JARはcoordinate＋SHA-512を正とします。
2. 公式サイトに版付き文書がある場合は採用版を使います。Webがlatest/stableのみの場合、同じ版のGit tagの文書・設定・UI実装も照合します。変わり続けるWebページを、将来も同じ仕様の保証にはしません。
3. 公式機能と本基盤固有のSQL・API・初期化処理を分けます。メダリオンの業務式、GUIの保存/未反映、素材の異常26件は本基盤の実装が正本です。
4. 文書・構文の検証と、コンテナ起動・会社での受入を分けます。[VALIDATION.md](VALIDATION.md) の過去の実機結果が、会社の全ネットワーク・データ・権限を保証するものではありません。

## 固定版と読んだ一次資料

| 対象・採用版 | 公式リファレンス・固定ソース | 手順へ反映した内容 |
| --- | --- | --- |
| Dokploy **0.31.0** | [tag](https://github.com/Dokploy/dokploy/tree/v0.31.0)・[Compose builder](https://github.com/Dokploy/dokploy/blob/v0.31.0/packages/server/src/utils/builders/compose.ts)・[変数解決](https://github.com/Dokploy/dokploy/blob/v0.31.0/packages/server/src/utils/docker/utils.ts) | Provider→Git、Compose Type、生成後appName、サービスEnvironment、Create Environment File、Run Command、Deploy/Rebuild。画面ごとの詳細引用は [DOKPLOY.md](DOKPLOY.md) |
| Docker **29系** / Compose **5.6.1** | [Engine API 1.52](https://docs.docker.com/reference/api/engine/version/v1.52/)・[Compose v5.6.1](https://github.com/docker/compose/releases/tag/v5.6.1)・[daemon proxy](https://docs.docker.com/engine/daemon/proxy/)・[Compose補間](https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/) | Swarm取得、daemonとアプリproxyの分離、env優先順位、CA secret。`plan`とホスト検査の差 |
| Dokploy内のPostgreSQL **16系** / Traefik **3.6.25** / Nginx **stable-alpine digest** | [PostgreSQL 16](https://www.postgresql.org/docs/16/)・[Traefik v3.6](https://doc.traefik.io/traefik/v3.6/)・[Nginx公式image](https://hub.docker.com/_/nginx) | 管理基盤専用。採用digestは [Dokploy lock](../infra/dokploy/images.lock.json)。アプリDB18.6とは別 |
| SeaweedFS **4.53** | [release](https://github.com/seaweedfs/seaweedfs/releases/tag/4.53)・[同tag S3実装](https://github.com/seaweedfs/seaweedfs/blob/4.53/weed/s3api/s3api_server.go)・[S3資格情報](https://github.com/seaweedfs/seaweedfs/wiki/S3-Credentials) | 認証設定、原本/warehouse分離、内部endpoint、Samba共有との分離 |
| Lakekeeper **0.14.3** | [同tag Compose](https://github.com/lakekeeper/lakekeeper/blob/v0.14.3/docker-compose/docker-compose.yaml)・[bootstrap](https://github.com/lakekeeper/lakekeeper/blob/v0.14.3/docs/docs/bootstrap.md)・[S3 storage](https://github.com/lakekeeper/lakekeeper/blob/v0.14.3/docs/docs/storage-s3.md) | migrate→serve→一度だけbootstrap、warehouse、暗号鍵、client-managedとsigning/STSの違い |
| Trino **484** | [Iceberg](https://github.com/trinodb/trino/blob/484/docs/src/main/sphinx/connector/iceberg.md)・[REST catalog](https://github.com/trinodb/trino/blob/484/docs/src/main/sphinx/object-storage/metastores.md)・[S3](https://github.com/trinodb/trino/blob/484/docs/src/main/sphinx/object-storage/file-system-s3.md) | 表単位のatomic replace、snapshot参照、v3 experimental、REST/S3設定。484のWeb URLが参照できない場合もtag文書を利用可能 |
| PyIceberg **0.12.0** / Python **3.14** | [PyIceberg設定](https://github.com/apache/iceberg-python/blob/pyiceberg-0.12.0/mkdocs/docs/configuration.md)・[REST client](https://github.com/apache/iceberg-python/blob/pyiceberg-0.12.0/pyiceberg/catalog/rest/__init__.py)・[Python 3.14](https://docs.python.org/3.14/) | FileIO/S3/signingの役割、固定context、workerの依存lock。全コンテナのPythonを3.14へ統一する構成ではない |
| アプリPostgreSQL **18.6** / Redis **8.10.3** | [PostgreSQL 18](https://www.postgresql.org/docs/18/)・[公式PostgreSQL image](https://hub.docker.com/_/postgres)・[Redis設定](https://redis.io/docs/latest/operate/oss_and_stack/management/config/) | DB永続化、18系のmount、初回passwordと既存DBの差、Superset専用cache。独立DBと暗号鍵の復元 |
| Spark **4.1.4** / Iceberg runtime **1.12.0** | [Spark standalone](https://spark.apache.org/docs/4.1.4/spark-standalone.html)・[monitoring](https://spark.apache.org/docs/4.1.4/monitoring.html)・[Iceberg同tag](https://github.com/apache/iceberg/blob/apache-iceberg-1.12.0/docs/docs/spark-configuration.md) | Scala2.13/Java21、同minor用JAR、driver/executor Python一致、常駐とlocal smoke、UI/History、event volume |
| Airflow **3.4.0 Python3.12** / standard **1.21.0** / HTTP **6.3.0** | [3.4.0 prerequisites](https://github.com/apache/airflow/blob/3.4.0/airflow-core/docs/installation/prerequisites.rst)・[同版Compose](https://github.com/apache/airflow/blob/3.4.0/airflow-core/docs/howto/docker-compose/docker-compose.yaml)・[constraints3.12](https://raw.githubusercontent.com/apache/airflow/constraints-3.4.0/constraints-3.12.txt) | 公式venv維持、Provider組合せ、standalone、Simple Auth state、Connectionと手動DAG |
| n8n **2.46.7** | [固定tagのSSRF設定](https://github.com/n8n-io/n8n/blob/n8n%402.46.7/packages/%40n8n/config/src/configs/ssrf-protection.config.ts)・[公式Docker手順](https://docs.n8n.io/hosting/installation/docker/)・[credentials](https://docs.n8n.io/credentials/) | owner初期登録、Header Auth、workflow import/保存、DBと暗号鍵。上流既定falseのSSRFを本構成でtrueへ設定 |
| Kafka **4.4.0** / Debezium **3.7.1.Final** / Kafbat **1.5.0** | [Kafka 4.4](https://kafka.apache.org/44/configuration/)・[Debezium 3.7 SQL Server](https://debezium.io/documentation/reference/3.7/connectors/sqlserver.html)・[Kafbat tag](https://github.com/kafbat/kafka-ui/tree/v1.5.0) | CDC前提、connector、schema history、raw/current/checkpoint/offset、Kafbatでの状態確認。DBA作業をGUI投入と分離 |
| 外部SQL Server / Samba | [Microsoft CDC](https://learn.microsoft.com/en-us/sql/relational-databases/track-changes/enable-and-disable-change-data-capture-sql-server)・[Samba smb.conf](https://www.samba.org/samba/docs/current/man-html/smb.conf.5.html) | これらはrepoで本体版を固定しない。会社の実版を導入記録へ残し、Agent・edition/権限・共有認証・到達性を別受入 |
| pymssql **2.4.6** / FreeTDS | [同tag API](https://github.com/pymssql/pymssql/blob/v2.4.6/docs/ref/pymssql.rst)・[FreeTDS設定](https://www.freetds.org/userguide/freetdsconf.html) | TLS暗号化とCA/ホスト名検証、SELECT用と素材writerの分離。stable Webが古い場合はtagを優先 |
| Streamlit **1.67.0** / FastAPI **0.162.2** / Uvicorn **0.54.0** / Swagger UI **5.34.1** | [Streamlit fragment](https://github.com/streamlit/streamlit/blob/1.67.0/lib/streamlit/runtime/fragment.py)・[FastAPI Bearer](https://github.com/fastapi/fastapi/blob/0.162.2/fastapi/security/http.py)・[同版self-host docs](https://github.com/fastapi/fastapi/blob/0.162.2/docs/en/docs/how-to/custom-docs-ui-assets.md) | UI再表示とサーバーjob継続、共有Bearer、Authorize、ローカルSwagger、1workerとメモリ内履歴。上限は本API実装 |
| Grafana **13.3.0** / Prometheus **3.16.0** | [Grafana 13.3](https://grafana.com/docs/grafana/v13.3/)・[Prometheus 3.16](https://prometheus.io/docs/prometheus/3.16/configuration/configuration/) | datasourceとGUI dashboardを分離、初回seedは一度、編集をDB保持。worker/自身targetだけで全ホスト監視とはしない |
| Superset **6.1.0** / JupyterLab **4.6.6** | [Superset版付きtutorial](https://superset.apache.org/user-docs/6.1.0/using-superset/creating-your-first-dashboard/)・[Jupyter interface](https://jupyterlab.readthedocs.io/en/stable/user/interface.html)・[Running](https://jupyterlab.readthedocs.io/en/stable/user/running.html) | 初期admin/token、SQLLab→Dataset→Chart→Dashboard、Notebook保存とkernel終了。Jupyter Webはstableのためlock版も併記 |
| Qdrant **1.20.0** / Marquez **0.50.0** | [Qdrant同tag config](https://github.com/qdrant/qdrant/blob/v1.20.0/config/config.yaml)・[Web UI](https://qdrant.tech/documentation/interfaces/web-ui/)・[Marquez同tag Compose](https://github.com/MarquezProject/marquez/blob/0.50.0/docker-compose.yml) | read/writeキー、native Console、合成vector検索、手動lineage seedと永続化。embedding/実ジョブ収集は別 |
| MCP Python SDK **2.3.0** / LangChain core **1.7.6**・Ollama **1.1.0** | [MCP SDK tag](https://github.com/modelcontextprotocol/python-sdk/tree/v2.3.0)・[LangChain core配布](https://pypi.org/project/langchain-core/1.7.6/)・[Ollama配布](https://pypi.org/project/langchain-ollama/1.1.0/) | HTTP/Host/token、readとopt-in write、snapshot根拠、外部tracing無効化。モデル推論は任意 |
| uv **0.13.7** | [同版release](https://github.com/astral-sh/uv/releases/tag/0.13.7)・[同tag pip docs](https://github.com/astral-sh/uv/tree/0.13.7/docs/pip) | 仮想環境とhash lock、固定tool配布、既存Airflow venv維持 |
| Cloudflare / ClickHouse等の外部候補 | [Cloudflareの用途と公式資料](CLOUDFLARE.md)・[ClickHouse Iceberg](https://clickhouse.com/docs/engines/table-engines/integrations/iceberg) | 未配備の選択肢。アカウント機能は版固定のコンテナと分け、採用時に再確認 |

Docker/Nginx等のホスト・基盤imageはminor/patch表記だけでなく、lockのdigestで取得内容を固定します。上表のWebには更新される資料も含まれます。具体的なUIの根拠は固定tagを優先し、ページ到達確認だけを精読・互換性検証の代替にはしません。

## 全Markdownの監査対象と結果

公開MarkdownはREADME、samples、docs、図版出典を対象に確認しました。各行は本文・コマンド・導線・参照の照合対象です。実機試験の結果を新たに捏造せず、過去の結果と今回の文書検証を分けています。

| 文書 | 今回そろえた内容 |
| --- | --- |
| [README](../README.md) | 最初の入口、install.sh、0.31.0の実画面・変数・appName |
| [START-HERE](START-HERE.md) | 端末/権限/設定生成→core→GUI→全追加機能→停止の一本道 |
| [COMPANY-ROLLOUT](COMPANY-ROLLOUT.md) | 会社担当、Samba、実SQL抽出、業務照合、私有受入記録 |
| [DOKPLOY](DOKPLOY.md)・[COMPANY-UPDATE](COMPANY-UPDATE.md) | tagのUI/サービス変数/生成env/Command、既存writerとvolumeの引継ぎ |
| [DEPLOYMENT](DEPLOYMENT.md)・[CONFIGURATION](CONFIGURATION.md) | 全設定、profile、host前提、16種volume、保存と反映 |
| [PROXY](PROXY.md)・[COMPANY-PROXY](COMPANY-PROXY.md) | daemon/build/runtime/Java/uv、canonical、大小case、CA、受入 |
| [SOURCE-SQLSERVER](SOURCE-SQLSERVER.md)・[CDC](CDC.md) | DBA/reader/writer、TLS、map、完了marker、CDC伝播と再開 |
| [SSRF](SSRF.md) | 2.46.7の既定と本構成の違い、NO_PROXYとの違い、例外優先 |
| [ARCHITECTURE](ARCHITECTURE.md)・[DIRECTORY-GUIDE](DIRECTORY-GUIDE.md) | 常駐Spark、全実装層、存在しない自動catalogを除去、全主要dir |
| [DEMO-DATA](DEMO-DATA.md)・[DEMO-GUI](DEMO-GUI.md) | 素材専用、uv、専用DB、run ID、stream/取消、CDC DMLとの差 |
| [ANOMALY-SCENARIOS](ANOMALY-SCENARIOS.md)・[MEDALLION](MEDALLION.md) | 26ケース/7preset、比較範囲、最新候補、11検査、表単位commit |
| [API](API.md)・[POC-PORTAL](POC-PORTAL.md) | Swaggerの初回ジョブ、認証・poll、履歴・固定API・ヘルプの範囲 |
| [GUI-OPTIONS](GUI-OPTIONS.md)・[GUI-WALKTHROUGHS](GUI-WALKTHROUGHS.md) | native画面の初期化/保存/確認、SQL編集の役割、管理方法の区別 |
| [GRAFANA](GRAFANA.md) | 新API seed、GUI編集の保持、datasourceとdashboardの違い |
| [COMPATIBILITY](COMPATIBILITY.md)・[POC-TOOLS](POC-TOOLS.md) | 採用版、Spark/Airflow依存、各OSSの実操作・初期資格情報・保存 |
| [AI-READINESS](AI-READINESS.md)・[MCP](MCP.md)・[CLOUDFLARE](CLOUDFLARE.md) | 根拠snapshot、参照/変更境界、実装済みと選択案の区別 |
| [OPERATIONS](OPERATIONS.md)・[TROUBLESHOOTING](TROUBLESHOOTING.md) | 7Composeでの補助操作、更新backup、常駐Spark、拒否/未反映の切分け |
| [IMPLEMENTATION-STATUS](IMPLEMENTATION-STATUS.md)・[VALIDATION](VALIDATION.md) | 現在の文書監査と過去の実機試験、未受入の会社条件を分離 |
| [図版出典](assets/README.md)・[samples](../samples/README.md) | 公式出典と同梱/offline、合成素材の作成と実データ非同梱 |
| このREFERENCES | 版・一次資料・全体の照合範囲。上流変更時は再監査 |

## 機密情報と検証の境界

公開前に現ファイル・Git index・全ローカル参照の履歴を検査します。一般的な秘密値/鍵/証明書/実データの規則に加え、提供資料内の実会社ホスト・IP・proxy・識別名と既存秘密値を**Git外の照合入力**として使います。検査結果に照合文字列そのものは出しません。社内添付資料や非公開のdeny listは公開しません。

既知情報と検査規則に対する検出結果を記録し、未知の機密が一切存在しないことを無条件に保証する表現は使いません。会社で新しい設定・Notebook・GUI exportを追加した場合も、実値を除去して公開前の検査を繰り返します。詳細は [検証記録](VALIDATION.md) です。

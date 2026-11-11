"""Publish fixed portal services, data contracts and offline help metadata."""

import json
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class ServiceDefinition:
    """Describe an entry point without embedding credentials in its URL."""

    id: str
    title: str
    profile: str
    port_key: str
    port: int
    path: str
    health_url: str
    description: str
    required: tuple[str, ...] = ()
    alternative_profiles: tuple[str, ...] = ()

    @property
    def profiles(self) -> tuple[str, ...]:
        """List alternative profiles that can enable this single service."""
        return (
            (self.profile, *self.alternative_profiles) if self.profile else ()
        )


SERVICES = (
    ServiceDefinition(
        "openobserve",
        "OpenObserve Logs Lab",
        "openobserve-lab",
        "OPENOBSERVE_PORT",
        5080,
        "/",
        "http://openobserve:5080/healthz",
        "Lokiと同じ秘匿済みログを比較します。loopback接続を使用します。",
        (
            "OPENOBSERVE_ROOT_USER_EMAIL",
            "OPENOBSERVE_ROOT_USER_PASSWORD",
            "OPENOBSERVE_INGEST_TOKEN_FILE",
        ),
    ),
    ServiceDefinition(
        "swagger",
        "操作API / Swagger",
        "demo",
        "DEMO_API_PORT",
        8502,
        "/docs",
        "http://demo-api:8000/health",
        "入力仕様と固定APIを確認します。",
        ("CONTROL_API_TOKEN",),
        alternative_profiles=("mcp",),
    ),
    ServiceDefinition(
        "mcp",
        "MCP / AIクライアント",
        "mcp",
        "MCP_PORT",
        8503,
        "/mcp",
        "http://mcp:8001/health",
        "AIクライアント用の接続先です。MCP_API_TOKENで別途認証します。",
        ("MCP_API_TOKEN", "CONTROL_API_TOKEN"),
    ),
    ServiceDefinition(
        "trino",
        "Trino",
        "",
        "TRINO_PORT",
        8080,
        "/ui/",
        "http://trino:8080/v1/info",
        "クエリーの実行状況を確認します。",
    ),
    ServiceDefinition(
        "catalog",
        "Lakekeeper",
        "",
        "CATALOG_PORT",
        8181,
        "/ui/",
        "http://catalog:8181/health",
        "Icebergカタログを確認します。",
    ),
    ServiceDefinition(
        "spark-master",
        "Spark Master",
        "compute",
        "SPARK_MASTER_UI_PORT",
        8082,
        "/",
        "http://spark-master:8080/",
        "常時起動するSparkクラスタの受付とworker登録を確認します。",
    ),
    ServiceDefinition(
        "spark-worker",
        "Spark Worker",
        "compute",
        "SPARK_WORKER_UI_PORT",
        8085,
        "/",
        "http://spark-worker:8081/",
        "Spark workerの資源とexecutorの稼働状況を確認します。",
    ),
    ServiceDefinition(
        "spark-history",
        "Spark History",
        "compute",
        "SPARK_HISTORY_UI_PORT",
        18080,
        "/",
        "http://spark-history:18080/",
        "保存したSpark event logからアプリの実行履歴を確認します。",
    ),
    ServiceDefinition(
        "n8n",
        "n8n",
        "automation",
        "N8N_PORT",
        5678,
        "/",
        "http://n8n:5678/healthz",
        "ワークフローを編集・明示実行します。",
        ("N8N_DB_PASSWORD", "N8N_ENCRYPTION_KEY"),
    ),
    ServiceDefinition(
        "grafana",
        "Grafana",
        "monitoring",
        "GRAFANA_PORT",
        3000,
        "/",
        "http://grafana:3000/api/health",
        "監視画面とダッシュボードを開きます。",
        ("GRAFANA_ADMIN_PASSWORD",),
    ),
    ServiceDefinition(
        "prometheus",
        "Prometheus",
        "monitoring",
        "PROMETHEUS_PORT",
        9090,
        "/",
        "http://prometheus:9090/-/ready",
        "収集した指標を確認します。",
    ),
    ServiceDefinition(
        "airflow",
        "Airflow",
        "orchestration",
        "AIRFLOW_PORT",
        8081,
        "/",
        "http://airflow:8080/api/v2/monitor/health",
        "DAGと実行結果を確認します。",
        ("AIRFLOW_DB_PASSWORD", "AIRFLOW_FERNET_KEY", "AIRFLOW_JWT_SECRET"),
    ),
    ServiceDefinition(
        "kafbat",
        "Kafka / Kafbat",
        "cdc",
        "CDC_UI_PORT",
        8084,
        "/",
        "http://cdc-ui:8080/actuator/health",
        "topic・lag・Connectを確認します。",
        (
            "CDC_SQLSERVER_HOST",
            "CDC_SQLSERVER_DATABASE",
            "CDC_SQLSERVER_USER",
            "CDC_SQLSERVER_PASSWORD",
        ),
    ),
    ServiceDefinition(
        "superset",
        "Superset",
        "bi",
        "SUPERSET_PORT",
        8088,
        "/",
        "http://superset:8088/health",
        "Goldを探索し、BI画面を作ります。",
        (
            "SUPERSET_DB_PASSWORD",
            "SUPERSET_SECRET_KEY",
            "SUPERSET_ADMIN_PASSWORD",
            "SUPERSET_REDIS_PASSWORD",
        ),
    ),
    ServiceDefinition(
        "jupyter",
        "JupyterLab",
        "notebooks",
        "JUPYTER_PORT",
        8888,
        "/lab",
        "http://jupyter:8888/api/status",
        "Notebookで分析を試します。",
        ("JUPYTER_TOKEN", "NOTEBOOKS_DIR"),
    ),
    ServiceDefinition(
        "qdrant",
        "Qdrant",
        "vector",
        "QDRANT_PORT",
        6333,
        "/dashboard",
        "http://qdrant:6333/readyz",
        "ベクトル索引とcollectionを確認します。",
        ("QDRANT_API_KEY", "QDRANT_READ_ONLY_API_KEY"),
    ),
    ServiceDefinition(
        "marquez",
        "Marquez",
        "lineage",
        "MARQUEZ_UI_PORT",
        3001,
        "/",
        "http://marquez-web:3000/",
        "登録済みのlineageを確認します。",
        ("MARQUEZ_DB_PASSWORD",),
    ),
)

HELP_DOCUMENTS = {
    "start": ("はじめに", "START-HERE.md", "目的別の最初の操作"),
    "portal": ("PoCポータル", "POC-PORTAL.md", "入口・データ・ヘルプの使い方"),
    "mcp": ("MCPの接続", "MCP.md", "AIクライアントの認証とtool範囲"),
    "ssrf": ("SSRFの接続制限", "SSRF.md", "接続許可とNO_PROXYの違い"),
    "poc-tools": (
        "PoCツール",
        "POC-TOOLS.md",
        "BI・Notebook・vector・lineage",
    ),
    "company": ("会社への導入", "COMPANY-ROLLOUT.md", "導入と受入の順序"),
    "configuration": ("設定一覧", "CONFIGURATION.md", "設定と反映の区別"),
    "demo": ("素材投入GUI", "DEMO-GUI.md", "素材生成と専用SQLへの投入"),
    "fixed-sql": (
        "固定SQL source",
        "SQLSERVER-FIXED-DEMO.md",
        "所有権・限定権限・同意と固定6表の検証",
    ),
    "anomalies": (
        "異常シナリオ",
        "ANOMALY-SCENARIOS.md",
        "26ケースの期待挙動",
    ),
    "medallion": ("メダリオン", "MEDALLION.md", "粒度・改訂・隔離・集計"),
    "proxy": ("会社proxy / CA", "PROXY.md", "接続主体別の設定と診断"),
    "company-proxy": (
        "会社proxy実践手順",
        "COMPANY-PROXY.md",
        "会社での準備・適用・切り分け",
    ),
    "api": ("API / Swagger", "API.md", "固定APIと認証・進捗"),
    "cdc": ("SQL Server CDC", "CDC.md", "captureと変更の照合"),
    "ai": ("AI向けデータ", "AI-READINESS.md", "snapshot付きの根拠JSON"),
    "troubleshooting": (
        "障害の切り分け",
        "TROUBLESHOOTING.md",
        "症状から確認先へ",
    ),
}
HELP_ASSETS = {
    "architecture": ("全体構成", "architecture.svg"),
    "ai-cloudflare": ("AI・クラウド連携", "ai-cloudflare.svg"),
}

SOURCE_DETAILS = {
    "erp_products": ("製品マスター", "製品IDごとの最新有効行"),
    "erp_work_orders": ("製造指図", "製造指図IDごとの最新有効行"),
    "erp_sales_orders": ("受注", "受注IDごとの最新有効行"),
    "erp_shipments": ("出荷", "出荷IDごとの最新有効行"),
    "mes_events": ("製造実績", "実績イベントIDごとの最新有効行"),
    "mes_downtime": ("設備停止", "停止イベントIDごとの最新有効行"),
}
COLUMN_TYPES = {
    "standard_cycle_seconds": "DECIMAL(18,3)",
    "planned_date": "DATE",
    "planned_qty": "BIGINT",
    "order_date": "DATE",
    "promised_date": "DATE",
    "ordered_qty": "BIGINT",
    "unit_price": "DECIMAL(18,2)",
    "shipped_at": "TIMESTAMP WITH TIME ZONE",
    "shipped_qty": "BIGINT",
    "event_time": "TIMESTAMP WITH TIME ZONE",
    "good_qty": "BIGINT",
    "reject_qty": "BIGINT",
    "start_time": "TIMESTAMP WITH TIME ZONE",
    "end_time": "TIMESTAMP WITH TIME ZONE",
    "updated_at": "TIMESTAMP WITH TIME ZONE",
}
COLUMN_DESCRIPTIONS = {
    "product_id": "製品の業務キー",
    "product_name": "製品名",
    "product_family": "製品群",
    "unit": "数量の単位",
    "standard_cycle_seconds": "標準サイクル秒。現GoldではOEEを計算しない",
    "updated_at": "ソースの改訂時刻。最新版の選択に使用",
    "factory_id": "工場の識別子",
    "work_order_id": "製造指図の業務キー・実績の参照先",
    "planned_date": "指図の計画日",
    "planned_qty": "計画数量",
    "sales_order_id": "受注の業務キー・出荷の参照先",
    "order_date": "受注日",
    "promised_date": "納期。UTC出荷日がこの日を過ぎると遅延",
    "ordered_qty": "受注数量",
    "unit_price": "単価。通貨換算は実装しない",
    "shipment_id": "出荷の業務キー",
    "shipped_at": "出荷日時",
    "shipped_qty": "出荷数量。Goldでは受注ごとに合計",
    "event_id": "製造実績の業務キー",
    "event_time": "製造実績の発生日時",
    "good_qty": "良品数量",
    "reject_qty": "不良数量",
    "downtime_id": "停止実績の業務キー",
    "start_time": "停止開始日時",
    "end_time": "停止終了日時",
    "reason_code": "停止理由のコード",
    "_source_file": "取り込んだソースファイルの識別子",
    "_source_hash": "ソース内容のSHA-256",
    "_ingested_at": "取り込み時刻",
    "production_date": "UTCの生産日。停止は開始日に帰属",
    "downtime_minutes": "停止の合計分数",
    "total_qty": "良品数と不良数の合計",
    "yield_pct": "良品数÷総数の百分率。分母ゼロはNULL",
    "plan_adherence_pct": "良品数÷計画数の百分率。100%超過も許す",
    "open_qty": "未出荷数量。最小0",
    "fulfillment_pct": "出荷数÷受注数の百分率。100%超過も許す",
    "late_delivered_qty": "納期後に出荷した数量",
    "late_shipment_count": "納期後の出荷件数",
    "is_fully_shipped": "出荷数量が受注数量以上か",
    "last_shipped_at": "最後の出荷日時",
}

GOLD = {
    "production": {
        "title": "工場・日別の生産",
        "table": "factory_daily_production",
        "grain": "UTCの日付と工場IDの組合せごとに1行",
        "primary_key": ["factory_id", "production_date"],
        "columns": {
            "factory_id": "VARCHAR",
            "production_date": "DATE",
            "planned_qty": "BIGINT",
            "good_qty": "BIGINT",
            "reject_qty": "BIGINT",
            "downtime_minutes": "DOUBLE",
            "total_qty": "BIGINT",
            "yield_pct": "DOUBLE",
            "plan_adherence_pct": "DOUBLE",
        },
        "provenance": ["erp_work_orders", "mes_events", "mes_downtime"],
    },
    "sales": {
        "title": "受注別の出荷・未出荷",
        "table": "sales_fulfillment",
        "grain": "受注IDごとに1行。出荷を受注単位へ集約して結合",
        "primary_key": ["sales_order_id"],
        "columns": {
            "sales_order_id": "VARCHAR",
            "factory_id": "VARCHAR",
            "product_id": "VARCHAR",
            "order_date": "DATE",
            "promised_date": "DATE",
            "ordered_qty": "BIGINT",
            "unit_price": "DECIMAL(18,2)",
            "shipped_qty": "BIGINT",
            "open_qty": "BIGINT",
            "fulfillment_pct": "DOUBLE",
            "late_delivered_qty": "BIGINT",
            "late_shipment_count": "BIGINT",
            "is_fully_shipped": "BOOLEAN",
            "last_shipped_at": "TIMESTAMP WITH TIME ZONE",
        },
        "provenance": ["erp_sales_orders", "erp_shipments"],
    },
}


def dataset_catalog() -> dict:
    """Describe public contracts without querying or naming private DBs."""
    contracts = json.loads((ROOT / "medallion/sources.json").read_text())
    result = []
    for contract in contracts["sources"]:
        name = contract["table"]
        title, grain = SOURCE_DETAILS[name]
        columns = {
            key: COLUMN_TYPES.get(key, "VARCHAR")
            for key in contract["columns"]
        }
        columns.update(
            {
                "_source_file": "VARCHAR",
                "_source_hash": "VARCHAR",
                "_ingested_at": "TIMESTAMP WITH TIME ZONE",
            }
        )
        result.append(
            {
                "id": name,
                "title": title,
                "layer": "silver",
                "table": "iceberg.silver." + name,
                "grain": grain,
                "primary_key": contract["columns"][:1],
                "columns": [
                    {
                        "name": key,
                        "type": kind,
                        "description": COLUMN_DESCRIPTIONS.get(key, key),
                    }
                    for key, kind in columns.items()
                ],
                "provenance": [
                    contract["path"],
                    "medallion/sources.json",
                    "medallion/01_silver.sql",
                ],
            }
        )
    for key, item in GOLD.items():
        result.append(
            {
                "id": key,
                "title": item["title"],
                "layer": "gold",
                "table": "iceberg.gold." + item["table"],
                "grain": item["grain"],
                "primary_key": item["primary_key"],
                "columns": [
                    {
                        "name": name,
                        "type": kind,
                        "description": COLUMN_DESCRIPTIONS.get(name, name),
                    }
                    for name, kind in item["columns"].items()
                ],
                "provenance": [
                    "iceberg.silver." + name for name in item["provenance"]
                ]
                + ["medallion/03_gold.sql"],
            }
        )
    return {"version": 1, "datasets": result}


def capabilities(values=None) -> dict:
    """Tell AI clients what fixed operations exist and which can write."""
    definitions = [
        (
            "capabilities",
            "GET",
            "/capabilities",
            "read",
            "固定APIと権限範囲の説明",
        ),
        ("help_asset", "GET", "/help/assets/{id}", "read", "同梱の構成図"),
        ("services", "GET", "/services", "read", "Sparkを含む入口と稼働状態"),
        ("catalog", "GET", "/catalog", "read", "公開データ契約と粒度・由来"),
        (
            "preview",
            "GET",
            "/catalog/preview",
            "read",
            "固定Goldの上限付き参照",
        ),
        ("quality", "GET", "/quality", "read", "固定6表の層ごとの件数"),
        ("status", "GET", "/status", "read", "固定内部サービスの状態"),
        ("scenarios", "GET", "/scenarios", "read", "合成異常の定義と期待挙動"),
        ("jobs", "GET", "/jobs", "read", "メモリ内の最近の実行履歴"),
        ("job", "GET", "/jobs/{id}", "read", "既知ジョブの進捗と結果"),
        ("help", "GET", "/help", "read", "同梱した公開文書の一覧"),
        ("help_document", "GET", "/help/{slug}", "read", "許可した公開文書"),
        ("ai_context", "GET", "/ai/context", "read", "snapshot付きGold根拠"),
        (
            "network_config",
            "GET",
            "/network/config",
            "read",
            "proxy設定の有無",
        ),
        (
            "network_checks",
            "POST",
            "/network/checks",
            "read",
            "固定4宛先の診断",
        ),
        ("settings", "GET", "/settings", "read", "非秘密設定と設定済みの印"),
        ("save_settings", "PATCH", "/settings", "write", "private設定の保存"),
        ("generate", "POST", "/jobs", "write", "合成素材だけを生成"),
        (
            "sql_batch",
            "POST",
            "/sql-source/batches/{mode}",
            "write",
            "固定SQLのsnapshot/増分とSQL用メダリオン更新",
        ),
        (
            "fixed_sql_status",
            "GET",
            "/sql-source/status",
            "read",
            "所有権付き固定SQLの状態",
        ),
        (
            "sql_checkpoints",
            "GET",
            "/sql-source/checkpoints",
            "read",
            "限定control資格情報で実watermarkをSELECT照会",
        ),
        (
            "sql_pipeline_status",
            "GET",
            "/sql-source/pipeline-status",
            "read",
            "SQLバッチ件数・完了台帳・適用途中の状態",
        ),
        (
            "fixed_cdc_status",
            "GET",
            "/sql-source/cdc/status",
            "read",
            "固定CDCのconnector・offset・Raw/Current状態",
        ),
        (
            "fixed_cdc_rebuild",
            "POST",
            "/sql-source/cdc/rebuild",
            "write",
            "明示同意でRaw snapshotからCurrentを非同期再構築",
        ),
        (
            "sql_gold_preview",
            "GET",
            "/sql-source/preview",
            "read",
            "batch/CDC別の固定Goldをsnapshot固定・上限付きで参照",
        ),
        (
            "fixed_sql_dml",
            "POST",
            "/sql-source/demo/{action}",
            "write",
            "固定試験レコードのINSERT・UPDATE・DELETE",
        ),
        (
            "pipeline",
            "POST",
            "/pipeline/{action}",
            "write",
            "明示した後続処理",
        ),
        (
            "cdc_demo",
            "POST",
            "/cdc/demo/{action}",
            "write",
            "所有する専用DBの固定DML",
        ),
        (
            "cancel",
            "POST",
            "/jobs/{id}/cancel",
            "write",
            "次の素材バッチを取消",
        ),
    ]
    return {
        "version": 1,
        "capabilities": [
            {
                "id": key,
                "method": method,
                "path": path,
                "access": access,
                "service": "control_api",
                "required_profiles": ["demo", "mcp"],
                "profile_match": "any",
                "description": description,
                "input_notes": "入力の型・上限・同意はOpenAPIで確認します。",
            }
            for key, method, path, access, description in definitions
        ]
        + [
            {
                "id": "mcp_transport",
                "method": "POST",
                "path": "/mcp",
                "service": "mcp",
                "required_profiles": ["mcp"],
                "profile_match": "any",
                "access": (
                    "write"
                    if (values or {}).get("MCP_ENABLE_MUTATIONS") == "true"
                    else "read"
                ),
                "description": (
                    "別portのMCP transport。既定は読み取りtoolだけです。"
                ),
                "input_notes": (
                    "MCP_API_TOKENで認証します。変更toolは"
                    "MCP_ENABLE_MUTATIONS=trueの場合だけ公開します。"
                ),
            }
        ],
    }

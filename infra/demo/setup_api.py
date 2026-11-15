"""Guide optional-component setup without giving the web process host powers.

The plan contains fixed names, prerequisites and profile changes only. It is
not an executable approval, a health result or an initial-install job. Existing
runner approval and the owning deployment manager remain separate boundaries.
"""

from fastapi import APIRouter, Depends, HTTPException
from lifecycle_catalog import profile_set
from poc_catalog import SERVICES
from pydantic import BaseModel, ConfigDict, Field

COMPONENTS = (
    (
        "compute",
        "Spark",
        "常駐するMaster・Worker・Historyを準備します。",
        "compute",
        ("compute",),
        (),
        True,
        (
            (
                "build",
                "共通イメージを先にビルド",
                "spark-volumes-initだけを共通imageのbuild担当にします。",
            ),
            (
                "bootstrap",
                "専用volumeを初期化して起動",
                "初期化成功後、Master・Worker・Historyを同じprojectで起動します。",
            ),
            (
                "validation",
                "クラスタ登録を確認",
                "Worker登録と明示spark-checkを確認します。通常起動では表を書きません。",
            ),
        ),
    ),
    (
        "notebooks",
        "JupyterLab",
        "保存領域を確認してNotebookを使います。",
        "notebooks",
        ("notebooks",),
        (),
        True,
        (
            (
                "host",
                "保存領域を検査",
                "NOTEBOOKS_DIR・UID/GID・mode2770を専用planで確認し、変更はoperatorが承認します。",
            ),
            (
                "build",
                "Notebookイメージを準備",
                "Spark共通imageを先にbuildし、Jupyterをbuildします。",
            ),
            (
                "validation",
                "保存と実行先を確認",
                "Notebookの保存と再読込を確認します。compute併用時は実クラスタ接続を検査します。",
            ),
        ),
    ),
    (
        "openobserve-lab",
        "OpenObserve Lab",
        "通常Lokiを維持して秘匿済みログを比較します。",
        "openobserve-lab",
        ("monitoring", "openobserve-lab"),
        ("monitoring",),
        True,
        (
            (
                "bootstrap",
                "本体だけ先に起動",
                "Labを通常Vectorで有効にする前に本体healthと管理者ログインを確認します。",
            ),
            (
                "configuration",
                "配送専用tokenを作成",
                "root passwordと別の取り込みtokenをprivate fileへ保存します。",
            ),
            (
                "validation",
                "Vectorの追加配送を確認",
                "Lokiと同じ秘匿済みeventを照合します。実配送・保持・容量は別途検証します。",
            ),
        ),
    ),
    (
        "sail-lab",
        "Sail Lab",
        "Sparkを残し、同じ合成データで比較します。",
        "sail-lab",
        ("compute", "sail-lab"),
        ("compute",),
        True,
        (
            (
                "build",
                "Sparkから順にビルド",
                "Spark共通image、Sail server、比較clientの順に準備します。",
            ),
            (
                "validation",
                "RPCと型の互換性を確認",
                "Connect・REST catalog・Trinoを検査します。"
                "TCP healthだけで成功としません。",
            ),
            (
                "validation",
                "比較処理は明示実行",
                "spark_lab/sail_labだけに合成データを書きます。起動時の自動benchmarkはありません。",
            ),
        ),
    ),
    (
        "orchestration",
        "Airflow",
        "DAGを手動実行する基盤を追加します。",
        "orchestration",
        ("orchestration",),
        (),
        False,
        (
            (
                "configuration",
                "DB・暗号鍵を確認",
                "専用DB password、Fernet、JWTの既存秘密値を維持します。",
            ),
            (
                "bootstrap",
                "DBと本体を初期化",
                "同じmanagerで起動してログインを確認します。DAGはpause状態を維持します。",
            ),
        ),
    ),
    (
        "vector",
        "Qdrant",
        "検索用のベクトル索引を追加します。Vectorログcollectorとは別です。",
        "vector",
        ("vector",),
        (),
        False,
        (
            (
                "configuration",
                "用途別APIキーを確認",
                "管理用と読取専用の異なるkeyを設定します。",
            ),
            (
                "bootstrap",
                "保存先と起動を確認",
                "専用volumeとhealthを確認します。collectionへの自動投入はありません。",
            ),
        ),
    ),
    (
        "lineage",
        "Marquez",
        "データ処理の来歴を確認する画面を追加します。",
        "lineage",
        ("lineage",),
        (),
        False,
        (
            (
                "bootstrap",
                "DB・API・Webを起動",
                "専用DBを含む3サービスを同じmanagerで準備します。",
            ),
            (
                "access",
                "公開前にAccessを確認",
                "製品内ログインがないため、保護済みの経路だけから利用します。",
            ),
        ),
    ),
    (
        "mcp",
        "MCP",
        "AIクライアント用の限定API入口を追加します。",
        "mcp",
        ("mcp",),
        (),
        False,
        (
            (
                "configuration",
                "専用認証と操作範囲を確認",
                "MCP tokenとCONTROL tokenを分離します。"
                "変更系toolは既定無効です。",
            ),
            (
                "validation",
                "AIクライアントで接続確認",
                "通常のWeb画面ではありません。認証値をURLへ含めません。",
            ),
        ),
    ),
    (
        "cdc",
        "Kafka / CDC",
        "承認した検証用ソースから変更履歴を取り込みます。",
        "cdc",
        ("cdc",),
        (),
        False,
        (
            (
                "configuration",
                "ソース権限と接続先を確認",
                "限定readerと固定table/ownershipを確認します。会社DBへ自動接続しません。",
            ),
            (
                "bootstrap",
                "CDC初期化を別途承認",
                "DB管理者の手動DDLとConnector登録を分け、DBA資格情報をruntimeへ渡しません。",
            ),
            (
                "validation",
                "履歴・offset・再実行を確認",
                "Raw履歴とCurrent Stateを分けます。"
                "DELETEとtombstoneも照合します。",
            ),
        ),
    ),
    (
        "monitoring-host",
        "ホスト指標",
        "Node ExporterとcAdvisorを追加します。",
        "monitoring-host",
        ("monitoring", "monitoring-host"),
        ("monitoring",),
        False,
        (
            (
                "host",
                "読取りmountを確認・承認",
                "host root/PID/Docker data rootの読取り範囲を確認します。"
                "socketや権限を自動追加しません。",
            ),
            (
                "validation",
                "実host値と指標を照合",
                "取得できない項目を正常なゼロと扱いません。ネットワーク/TCP/systemdは対象外です。",
            ),
        ),
    ),
)
SERVICE_IDS = {
    "compute": "spark-master",
    "notebooks": "jupyter",
    "openobserve-lab": "openobserve",
    "orchestration": "airflow",
    "vector": "qdrant",
    "lineage": "marquez",
    "mcp": "mcp",
    "cdc": "kafbat",
}


def setup_components(public, applied):
    """Derive metadata without network, filesystem probing or secret output."""
    values = public["values"]
    present = set(public["configured_secrets"])
    selected = profile_set(values)
    catalog = {item.id: item for item in SERVICES}
    rows = []
    for (
        identifier,
        title,
        description,
        profile,
        required,
        depends,
        managed,
        instructions,
    ) in COMPONENTS:
        card = catalog.get(SERVICE_IDS.get(identifier))
        keys = card.required if card else ()
        if (
            identifier == "cdc"
            and values.get("CDC_SOURCE_MODE") == "fixed-demo"
        ):
            keys = (
                "SOURCE_SQLSERVER_HOST",
                "SOURCE_SQLSERVER_DATABASE",
                "SOURCE_SQLSERVER_USER",
                "SOURCE_SQLSERVER_PASSWORD",
                "DEMO_SQLSERVER_INSTALLATION_ID",
                "DEMO_SQLSERVER_OWNERSHIP_TOKEN",
            )
        if identifier == "monitoring-host":
            keys = ("CADVISOR_DOCKER_DATA_ROOT", "MONITORING_LOG_DIR")
        missing = [
            key for key in keys if not values.get(key) and key not in present
        ]
        active = profile in selected
        link_key = {
            "compute": "SPARK_MASTER_UI_PUBLIC_URL",
            "notebooks": "JUPYTER_PUBLIC_URL",
            "openobserve-lab": "OPENOBSERVE_PUBLIC_URL",
            "orchestration": "AIRFLOW_PUBLIC_URL",
            "vector": "QDRANT_PUBLIC_URL",
            "lineage": "MARQUEZ_UI_PUBLIC_URL",
            "mcp": "MCP_PUBLIC_URL",
            "cdc": "CDC_UI_PUBLIC_URL",
        }.get(identifier)
        steps = [
            {
                "id": f"{identifier}-{index}",
                "kind": kind,
                "title": heading,
                "description": detail,
            }
            for index, (kind, heading, detail) in enumerate(instructions, 1)
        ]
        if link_key:
            steps.append(
                {
                    "id": identifier + "-access",
                    "kind": "access",
                    "title": "外部入口とログインを確認",
                    "description": (
                        "Tunnel・限定Access・公開URLを揃えます。"
                        "起動だけで外部公開はしません。"
                    ),
                }
            )
        rows.append(
            {
                "id": identifier,
                "title": title,
                "description": description,
                "profile": profile,
                "selected": active,
                "configured": not missing,
                "missing_settings": missing,
                "management_supported": managed,
                "required_profiles": list(required),
                "depends_on": list(depends),
                "steps": steps,
                "state": "configuration_required"
                if missing
                else ("selected" if active else "ready_for_operator_review"),
                "external_link_configured": bool(
                    link_key and values.get(link_key)
                ),
            }
        )
    return rows


class SetupPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    component_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,48}$")


def setup_router(store, authorize):
    """Prepare fixed plans without saving profiles or running jobs."""
    router = APIRouter(
        tags=["初回セットアップ"], dependencies=[Depends(authorize)]
    )

    @router.get("/setup", summary="追加コンポーネントの準備状況と手順")
    def status():
        public = store.public()
        return {
            "version": 1,
            "manager": "operator-compose",
            "apply_supported": False,
            "applied_profiles": sorted(profile_set(store.applied)),
            "desired_profiles": sorted(profile_set(public["values"])),
            "pending_restart": public["pending_restart"],
            "components": setup_components(public, store.applied),
        }

    @router.post("/setup/plan", summary="固定対象の初回セットアップ計画を作る")
    def plan(request: SetupPlan):
        public = store.public()
        rows = setup_components(public, store.applied)
        item = next(
            (row for row in rows if row["id"] == request.component_id), None
        )
        if item is None:
            raise HTTPException(
                404, detail={"code": "SETUP_COMPONENT_UNKNOWN"}
            )
        before = profile_set(public["values"])
        return {
            "version": 1,
            "component_id": item["id"],
            "title": item["title"],
            "profiles_before": sorted(before),
            "profiles_after": sorted(before | set(item["required_profiles"])),
            "settings_to_review": item["missing_settings"],
            "steps": item["steps"],
            "can_apply": False,
            "requires_operator": True,
            "notice": (
                "準備計画です。保存・build・起動・権限変更は行いません。"
                "既存managerで初回導入を承認し、完了後に管理runnerを再承認します。"
            ),
        }

    return router

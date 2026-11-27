"""操作APIを介して外部入力の処理・状態を確認するポータル。"""

import json

import streamlit as st
from client import request
from poc_ui import (
    render_data,
    render_help,
    render_history,
    render_network,
    render_portal,
)
from sqlsource_ui import (
    render_sql_batches,
    render_sql_observations,
    render_sql_source,
)

st.set_page_config(page_title="Lakehouse PoC ポータル", page_icon="🏭")
st.title("Lakehouse PoC ポータル")
st.caption("外部から受け取ったデータを処理し、状態と分析結果を確認します。")


def call_api(path, *, method="GET", payload=None, timeout=15):
    """Display safe request errors without credentials or raw server logs."""
    try:
        return request(path, method=method, payload=payload, timeout=timeout)
    except ValueError as error:
        st.error(str(error))
        return None


@st.fragment(run_every="2s")
def render_job():
    """受信後の処理と復旧の状態を照会し、再送や変更は行わない。"""
    job_id = st.session_state.get("active_job")
    result = st.session_state.get("job_result")
    if job_id:
        result = call_api("/jobs/" + job_id)
        if result is None:
            return
        st.session_state["job_result"] = result
    if not result:
        return
    st.subheader("実行結果を確認する")
    st.button("進捗を更新", key="job-refresh")
    status = result.get("status", "queued")
    st.write("状態: " + str(status))
    st.metric("完了した処理", result.get("completed", 0))
    if status in {"succeeded", "failed", "cancelled"}:
        st.session_state["active_job"] = None
    if result.get("error"):
        st.error(result["error"].get("message", "処理が完了しませんでした。"))
    if result.get("results"):
        rejected = sum(
            item.get("rejected_batches", 0) for item in result["results"]
        )
        if rejected:
            st.warning(
                f"{rejected} 件の外部ファイルバッチを隔離しました。"
                "配送元の完成印と原本を照合してください。"
            )
        st.download_button(
            "実行メタデータを保存",
            data=json.dumps(result["results"], ensure_ascii=False, indent=2),
            file_name="job-result.json",
            mime="application/json",
        )


def render_settings():
    """Edit API-approved private settings while preserving blank secrets."""
    st.subheader("接続・配置の設定")
    st.caption(
        "変更は非公開の設定へ保存します。反映に再配置が必要な項目があります。"
    )
    st.caption(
        "monitoringは通常監視です。hostの読取りはmonitoring-hostを追加した"
        "場合だけ有効になり、monitoringも必要です。sail-labにはcomputeも"
        "必要です。保存後、配置側でも同じprofileを明示して再配置します。"
    )
    result = call_api("/settings")
    if result is None:
        return
    values = result.get("values", {})
    secret_keys = set(result.get("secret_keys", []))
    if result.get("pending_restart"):
        st.warning(
            "保存済みの変更があります。再配置までは現在の設定で動きます。"
        )
    if result.get("pending_mount_change"):
        st.info(
            "保存先や実行ユーザーの変更は、配置側のmount設定も確認してください。"
        )
    configured = set(result.get("configured_secrets", []))
    changes = {}
    labels = {
        "COMPOSE_PROJECT_NAME": "環境の名前",
        "DATA_VOLUME_PREFIX": "既存データ名の接頭辞（通常は空欄）",
        "BIND_IP": "画面を公開するアドレス",
        "LANDING_DIR": "ファイルの受け口（ホストの絶対パス）",
        "COMPOSE_PROFILES": "追加機能（カンマ区切り）",
        "MONITORING_LOG_DIR": "ログcapture専用directory（Git外）",
        "MONITORING_HOST_ALIAS": "ログ用の架空host識別名",
        "CADVISOR_DOCKER_DATA_ROOT": "確認済みDocker保存先（読取り専用）",
        "OPENOBSERVE_PORT": "OpenObserve比較UIのloopback port",
        "OPENOBSERVE_ROOT_USER_EMAIL": "OpenObserve管理者ID（Lab専用）",
        "OPENOBSERVE_ROOT_USER_PASSWORD": "OpenObserve管理者password",
        "OPENOBSERVE_INGEST_TOKEN_FILE": "取込専用tokenのprivate JSON file",
        "SOURCE_SQLSERVER_HOST": "業務参照元SQL Server",
        "SOURCE_SQLSERVER_DATABASE": "業務参照元のDB名",
        "SOURCE_SQLSERVER_USER": "参照元reader（SELECT専用）",
        "SOURCE_SQLSERVER_HOSTNAME_IN_CERTIFICATE": "SOURCE:証明書の照合名",
        "SOURCE_SQLSERVER_CONTROL_USER": "checkpoint専用ユーザー",
        "SOURCE_SQLSERVER_CONTROL_PASSWORD": "checkpoint専用パスワード",
        "SOURCE_SQLSERVER_BATCH_OVERLAP_SECONDS": "増分で重ねて読む秒数",
        "SOURCE_SQLSERVER_BATCH_SAFETY_LAG_SECONDS": "増分の安全遅延（秒）",
        "DEMO_SQLSERVER_INSTALLATION_ID": "導入ID（既存環境では保持）",
        "DEMO_SQLSERVER_OWNERSHIP_TOKEN": "所有権鍵（既存環境では保持）",
        "DEMO_SQLSERVER_ERP_SCHEMA": "固定ERP schema（変更不可）",
        "DEMO_SQLSERVER_MES_SCHEMA": "固定MES schema（変更不可）",
        "DEMO_SQLSERVER_CONTROL_SCHEMA": "固定制御schema（変更不可）",
        "SOURCE_SQLSERVER_TRUST_SERVER_CERTIFICATE": "SOURCE:証明書検証省略",
        "CDC_SOURCE_MODE": "CDC sourceの種類（generic / fixed-demo）",
        "CDC_STATE_VOLUME_NAME": "CDC状態volume名（既存は保持）",
        "N8N_SSRF_PROTECTION_ENABLED": "n8nのSSRF保護（true/false）",
        "N8N_SSRF_BLOCKED_IP_RANGES": "拒否するIP範囲（defaultは製品既定）",
        "N8N_SSRF_ALLOWED_IP_RANGES": "例外として許可するIP範囲",
        "N8N_SSRF_ALLOWED_HOSTNAMES": "例外として許可するホスト名",
        "N8N_SSRF_BLOCKED_HOSTNAMES": "拒否するホスト名",
        "N8N_SSRF_DNS_CACHE_MAX_SIZE": "SSRF検査のDNSキャッシュ上限",
    }
    groups = {
        "基本": [],
        "SQL接続": [],
        "プロキシ": [],
        "接続制限（SSRF）": [],
        "サービスと配置": [],
    }
    basic = {
        "COMPOSE_PROJECT_NAME",
        "DATA_VOLUME_PREFIX",
        "BIND_IP",
        "LANDING_DIR",
        "COMPOSE_PROFILES",
    }
    for key in values:
        if key in basic:
            group = "基本"
        elif key.startswith(("SOURCE_SQLSERVER_", "DEMO_SQLSERVER_")):
            group = "SQL接続"
        elif key.upper().endswith("PROXY"):
            group = "プロキシ"
        elif key.startswith("N8N_SSRF_"):
            group = "接続制限（SSRF）"
        else:
            group = "サービスと配置"
        groups[group].append(key)
    with st.form("private-settings"):
        for group, keys in groups.items():
            with st.expander(group, expanded=group == "基本"):
                if group == "接続制限（SSRF）":
                    st.caption(
                        "NO_PROXYはproxyを迂回する宛先、SSRFは接続の許可制御です。"
                        "生成既定は保護を有効にし、内部workerを明示例外にします。"
                    )
                for key in keys:
                    value = str(values[key])
                    entered = st.text_input(
                        labels.get(key, key), value=value, help=key
                    )
                    if entered != value:
                        changes[key] = entered
        with st.expander("資格情報を更新する（空欄は保持）"):
            for key in sorted(secret_keys):
                entered = st.text_input(
                    labels.get(key, key),
                    value="",
                    type="password",
                    help=("設定済み。" if key in configured else "未設定。")
                    + "空欄は現在の秘密値を保持します。",
                )
                if entered:
                    changes[key] = entered
        submitted = st.form_submit_button("設定を保存")
    if submitted and changes:
        saved = call_api(
            "/settings", method="PATCH", payload={"values": changes}
        )
        if saved is not None:
            st.success("設定を保存しました。再配置は別の操作です。")
    with st.expander("接続制限の同梱説明"):
        if st.button("SSRFの設定説明を開く", key="settings-ssrf-help"):
            document = call_api("/help/ssrf")
            if document is not None:
                st.session_state["settings-ssrf-document"] = document
        document = st.session_state.get("settings-ssrf-document")
        if document:
            st.markdown(document["markdown"])


def render_pipeline():
    """Submit one explicit downstream action independently of source jobs."""
    st.subheader("取り込み・変換・検証")
    st.write("素材が用意できてから、実行する処理を選びます。")
    st.caption("素材投入とは別のジョブです。同時に実行できる処理は一つです。")
    actions = [
        (
            "ingest",
            "原本を取り込む",
            "完成済みファイルをBronzeへ取り込みます。",
        ),
        (
            "medallion",
            "Silver・Goldまで変換",
            "原本取り込みと派生表の更新を行います。",
        ),
        (
            "verify",
            "基盤の往復検証",
            "合成の試験データでS3・Iceberg・SQLを照合します。",
        ),
    ]
    for column, (action, label, description) in zip(st.columns(3), actions):
        with column:
            st.write(description)
            if st.button(label, key="pipeline-" + action):
                result = call_api("/pipeline/" + action, method="POST")
                if result is not None:
                    job_id = result.get("id", result.get("job_id"))
                    if job_id:
                        st.session_state["active_job"] = job_id
                        st.session_state["job_kind"] = "pipeline"
                        st.session_state["job_result"] = result
                        st.success(
                            "処理を受け付けました。下の進捗を確認してください。"
                        )
    st.caption(
        "処理開始後は同じボタンを繰り返さず、ジョブの結果を確認します。"
    )


def render_ai_context():
    """Download bounded Gold facts with their committed snapshot references."""
    st.subheader("AIへ渡す根拠データ")
    st.write("Goldを読み取り、表名とIceberg snapshot ID付きで保存します。")
    st.caption(
        "モデルは呼び出しません。先に取り込み・変換を完了してください。"
    )
    dataset = st.selectbox("対象", ["all", "production", "sales"])
    limit = st.number_input("表ごとの最大行数", 1, 500, 100)
    if st.button("根拠データを準備する"):
        with st.spinner("Goldのsnapshotを固定して取得しています…"):
            result = call_api(
                f"/ai/context?dataset={dataset}&limit={int(limit)}",
                timeout=120,
            )
        if result is not None:
            st.session_state["ai_context"] = result
    context = st.session_state.get("ai_context")
    if context:
        datasets = context.get("datasets", [])
        st.dataframe(
            [
                {
                    "表": item.get("table"),
                    "snapshot ID": item.get("snapshot_id"),
                    "取得行数": len(item.get("rows", [])),
                }
                for item in datasets
            ],
            hide_index=True,
            width="stretch",
        )
        st.download_button(
            "根拠JSONを保存する",
            data=json.dumps(
                context, ensure_ascii=False, indent=2, default=str
            ),
            file_name="gold-ai-context.json",
            mime="application/json",
        )
        st.caption(
            "行数制限があるため、全件の集計結果として扱わないでください。"
        )


(
    portal_tab,
    pipeline_tab,
    data_tab,
    settings_tab,
    status_tab,
    network_tab,
    history_tab,
    help_tab,
    ai_tab,
) = st.tabs(
    [
        "入口",
        "処理・検証",
        "データ",
        "設定",
        "状態",
        "接続診断",
        "履歴",
        "ヘルプ",
        "AIの準備",
    ]
)
with portal_tab:
    render_portal(call_api)
with data_tab:
    render_data(call_api)
with network_tab:
    render_network(call_api)
    render_sql_source(call_api)
with history_tab:
    render_history(call_api)
with help_tab:
    render_help(call_api)
with pipeline_tab:
    render_pipeline()
    render_sql_batches(call_api)
with settings_tab:
    render_settings()
with status_tab:
    st.subheader("接続状態")
    status = call_api("/status")
    if status is not None:
        st.json(status)
    st.caption("初期設定ではサーバー自身またはSSHポート転送から利用します。")
    render_sql_observations(call_api)
with ai_tab:
    render_ai_context()

# One shared progress area prevents duplicate polling widgets across tabs.
render_job()

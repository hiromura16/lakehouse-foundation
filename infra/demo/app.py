"""Human-facing source generator backed by the internal FastAPI service."""

import json

import streamlit as st
from client import request

st.set_page_config(page_title="Lakehouse データ投入", page_icon="🏭")
st.title("データ投入スタジオ")
st.caption("MES・ERPの合成データを、ファイルまたは専用の検証DBへ投入します。")


def call_api(path, *, method="GET", payload=None, timeout=15):
    """Display safe request errors without credentials or raw server logs."""
    try:
        return request(path, method=method, payload=payload, timeout=timeout)
    except ValueError as error:
        st.error(str(error))
        return None


def render_source_form():
    """Collect source-only inputs and create one finite generation job."""
    st.subheader("1. 素材の作り方を選ぶ")
    st.write(
        "製品・製造指図・受注・出荷のCSVと、製造・停止実績のJSONを作ります。"
    )
    mode_label = st.radio(
        "投入方式",
        ["バッチ（一度だけ）", "ストリーミング（有限マイクロバッチ）"],
    )
    streaming = mode_label.startswith("ストリーミング")
    destination_label = st.selectbox(
        "投入先", ["ランディング", "専用の検証SQL Server", "両方"]
    )
    destination = {
        "ランディング": "landing",
        "専用の検証SQL Server": "sqlserver",
        "両方": "both",
    }[destination_label]
    with st.form("source-generation"):
        left, right = st.columns(2)
        seed = left.number_input("乱数シード", 0, 2147483647, 42)
        orders = right.number_input("1回あたりの指図・受注数", 1, 1000, 20)
        days = left.number_input("業務日数", 1, 366, 7)
        scenario = right.selectbox("データ品質", ["edge", "clean"])
        st.caption("edgeは型不正・重複・参照不整合・遅延到着を含みます。")
        batches = 1
        interval = 0
        if streaming:
            batches = left.number_input("マイクロバッチ回数", 2, 60, 3)
            interval = right.number_input("投入間隔（秒）", 1, 60, 5)
            st.caption(
                "指定回数で終了します。Kafkaへの直接送信ではありません。"
            )
        images = st.checkbox(
            "合成の検査画像PNGも作る", disabled=destination == "sqlserver"
        )
        consent = False
        if destination != "landing":
            st.info("専用の既存DEMO DBへ、毎回新しいschemaを作成します。")
            consent = st.checkbox("検証DBへの書き込みを明示的に許可する")
            st.caption("会社の参照元DB・システムDBへの投入は拒否されます。")
        submitted = st.form_submit_button("素材の投入を開始", type="primary")
    if submitted:
        if (int(batches) - 1) * int(interval) > 600:
            st.error("待機時間の合計は600秒以内に設定してください。")
            return
        if destination != "landing" and not consent:
            st.error("検証DBへ投入する場合は、書き込みの許可が必要です。")
            return
        result = call_api(
            "/jobs",
            method="POST",
            payload={
                "mode": "stream" if streaming else "batch",
                "batches": int(batches),
                "interval_seconds": int(interval),
                "seed": int(seed),
                "orders": int(orders),
                "days": int(days),
                "scenario": scenario,
                "destination": destination,
                "images": images and destination != "sqlserver",
                "allow_sql_write": consent,
            },
        )
        if result is not None:
            job_id = result.get("job_id", result.get("id"))
            if job_id:
                st.session_state["active_job"] = job_id
                st.session_state["job_kind"] = "source"
                st.session_state["job_result"] = result
                st.success(
                    "投入を受け付けました。下の進捗で結果を確認できます。"
                )
    st.caption(
        "素材の投入から、取り込み・変換を自動で起動することはありません。"
    )


def render_cdc_form():
    """Submit fixed source DML only after explicit dedicated-DB consent."""
    with st.expander("CDCテスト用DBに変更を投入"):
        st.write(
            "管理者が準備した専用CDCテストDBへ、固定した6表のレコードを"
            "追加・更新・削除します。"
        )
        st.info(
            "準備済みの専用DBと所有権キーが必要です。"
            "会社の業務DBは対象にできません。"
            "接続情報とキーは設定の保存後に再配置して反映します。"
        )
        confirmed = st.checkbox(
            "準備済みの専用CDCテストDBへの変更を許可する",
            key="cdc-dml-confirm",
        )
        actions = [
            ("insert", "追加（INSERT）"),
            ("update", "更新（UPDATE）"),
            ("delete", "削除（DELETE）"),
        ]
        for column, (action, label) in zip(st.columns(3), actions):
            if column.button(
                label, key="cdc-" + action, disabled=not confirmed
            ):
                result = call_api(
                    "/cdc/demo/" + action,
                    method="POST",
                    payload={"confirm": True},
                )
                if result is not None:
                    st.session_state["active_job"] = result["id"]
                    st.session_state["job_kind"] = "cdc-source"
                    st.session_state["job_result"] = result
                    st.success("変更を受け付けました。下の進捗を確認します。")
        st.caption(
            "通常は追加→更新→削除の順に確認します。"
            "開始済みのDB変更は途中取消できません。"
        )
        st.caption(
            "この操作から後続処理を自動実行しません。"
            "稼働中のCDC経路による伝播は、状態・Kafka UI・派生表で"
            "別に確認してください。"
        )


@st.fragment(run_every="2s")
def render_job():
    """Poll the selected job and allow cancellation between microbatches."""
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
    status = result.get("status", result.get("state", "accepted"))
    results = result.get("results", [])
    completed = result.get("completed", len(results))
    st.write("状態: " + str(status))
    kind = result.get("kind", st.session_state.get("job_kind", "source"))
    source_job = kind == "source"
    cdc_job = kind == "cdc-source"
    if (source_job or cdc_job) and result.get("pending_restart"):
        st.warning(
            "保存した設定は未反映です。現在の起動時設定で投入しています。"
            "反映する場合は再配置してください。"
        )
    label = "完了したマイクロバッチ" if source_job else "完了した処理"
    if cdc_job:
        label = "完了したDB変更"
        st.info(
            "専用DBの変更は途中取消できません。"
            "成功はソースDBへのcommitで、下流への到着は別に確認します。"
        )
    st.metric(label, completed)
    terminal = {
        "completed",
        "succeeded",
        "success",
        "done",
        "failed",
        "cancelled",
    }
    if status in terminal:
        st.session_state["active_job"] = None
    elif source_job and job_id and st.button("次のマイクロバッチから停止する"):
        call_api("/jobs/" + job_id + "/cancel", method="POST")
    if result.get("error"):
        st.error(
            "投入が完了しませんでした。設定・権限・TLSを確認してください。"
        )
    rows = []
    for batch in results:
        for source in batch.get("sources", []):
            rows.append(
                {
                    "回": batch.get("batch"),
                    "ソース": source.get("table"),
                    "行数": source.get("rows"),
                    "ファイル": source.get("path"),
                }
            )
    if rows:
        st.dataframe(rows, hide_index=True, width="stretch")
    if results:
        st.download_button(
            "実行メタデータを保存",
            data=json.dumps(results, ensure_ascii=False, indent=2),
            file_name="job-result.json",
            mime="application/json",
        )


def render_settings():
    """Edit API-approved private settings while preserving blank secrets."""
    st.subheader("接続・配置の設定")
    st.caption(
        "変更は非公開の設定へ保存します。反映に再配置が必要な項目があります。"
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
        "SOURCE_SQLSERVER_HOST": "業務参照元SQL Server",
        "SOURCE_SQLSERVER_DATABASE": "業務参照元のDB名",
        "DEMO_SQLSERVER_HOST": "ダミー投入用SQL Server",
        "DEMO_SQLSERVER_DATABASE": "ダミー投入専用DB名",
        "DEMO_SQLSERVER_USER": "ダミー投入用ユーザー",
    }
    groups = {"基本": [], "SQL接続": [], "プロキシ": [], "サービスと配置": []}
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
        else:
            group = "サービスと配置"
        groups[group].append(key)
    with st.form("private-settings"):
        for group, keys in groups.items():
            with st.expander(group, expanded=group == "基本"):
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
                    key,
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


source_tab, pipeline_tab, settings_tab, status_tab, ai_tab = st.tabs(
    ["素材の投入", "処理・検証", "設定", "状態", "AIの準備"]
)
with source_tab:
    render_source_form()
    render_cdc_form()
with pipeline_tab:
    render_pipeline()
with settings_tab:
    render_settings()
with status_tab:
    st.subheader("接続状態")
    status = call_api("/status")
    if status is not None:
        st.json(status)
    st.caption("初期設定ではサーバー自身またはSSHポート転送から利用します。")
with ai_tab:
    render_ai_context()

# One shared progress area prevents duplicate polling widgets across tabs.
render_job()

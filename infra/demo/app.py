"""Human-facing source generator backed by the internal FastAPI service."""

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
from sqlsource_ui import render_sql_batches, render_sql_source

st.set_page_config(page_title="Lakehouse PoC ポータル", page_icon="🏭")
st.title("Lakehouse PoC ポータル")
st.caption("MES・ERPの合成データを、ファイルまたは専用の検証DBへ投入します。")


def call_api(path, *, method="GET", payload=None, timeout=15):
    """Display safe request errors without credentials or raw server logs."""
    try:
        return request(path, method=method, payload=payload, timeout=timeout)
    except ValueError as error:
        st.error(str(error))
        return None


def render_scenario_selection(catalog):
    """Keep preset edits visible before any source-generation submission."""
    scenarios = {item["id"]: item for item in catalog.get("scenarios", [])}
    presets = {item["id"]: item for item in catalog.get("presets", [])}
    chosen_preset = st.selectbox(
        "異常シナリオのプリセット",
        ["custom", *presets],
        format_func=lambda key: (
            "標準・手動で選ぶ" if key == "custom" else presets[key]["label"]
        ),
        key="source-preset",
    )
    if st.session_state.get("applied-source-preset") != chosen_preset:
        preset = presets.get(chosen_preset, {})
        # Reset only when a different preset is deliberately selected.
        st.session_state["source-anomalies"] = list(
            preset.get("anomalies", [])
        )
        st.session_state["source-base-scenario"] = preset.get(
            "base_scenario", "edge"
        )
        st.session_state["applied-source-preset"] = chosen_preset
    scenario = st.selectbox(
        "ベースのデータ品質", ["edge", "clean"], key="source-base-scenario"
    )
    st.caption(
        "edgeは従来の異常・改訂入り、cleanは正常な基礎データです。"
        "追加異常が空なら従来と同じ素材を生成します。"
    )
    categories = sorted({item["category"] for item in scenarios.values()})
    category_labels = {"row": "行・業務データ", "file": "ファイル・転送"}
    category = st.selectbox(
        "異常のカテゴリ",
        ["すべて", *categories],
        format_func=lambda key: category_labels.get(key, key),
    )
    existing = st.session_state.get("source-anomalies", [])
    visible = [
        key
        for key, item in scenarios.items()
        if category == "すべて"
        or item["category"] == category
        or key in existing
    ]
    selected = st.multiselect(
        "追加する異常（複数選択・除外できます）",
        visible,
        format_func=lambda key: scenarios[key]["label"],
        key="source-anomalies",
    )
    rate = st.slider("異常の影響率（%）", 1, 100, 10, key="anomaly-rate")
    st.caption(
        "行異常の対象選択率です。最終の隔離率とは異なります。"
        "ファイル異常は率にかかわらず対象ファイル全体へ作用します。"
    )
    items = [scenarios[key] for key in selected]
    if items:
        st.dataframe(
            [
                {
                    "異常": item["label"],
                    "カテゴリ": category_labels.get(
                        item["category"], item["category"]
                    ),
                    "対象": ", ".join(item["targets"]),
                    "素材で変わること": item["description"],
                    "期待する下流の挙動": item["expected_effect"],
                }
                for item in items
            ],
            hide_index=True,
            width="stretch",
        )
    advanced = [
        item["id"] for item in items if item.get("requires_file_consent")
    ]
    if st.session_state.get("confirmed-file-fault-selection") != advanced:
        st.session_state["allow-file-faults"] = False
        st.session_state["confirmed-file-fault-selection"] = advanced
    allowed = False
    if advanced:
        st.warning(
            "高度なファイル異常は、取り込み拒否や無視される素材を"
            "意図して作ります。専用の検証用ランディングで使ってください。"
        )
        allowed = st.checkbox(
            "高度なファイル異常を意図して生成することに同意する",
            key="allow-file-faults",
        )
    return {
        "scenario": scenario,
        "anomalies": selected,
        "anomaly_rate": rate,
        "allow_file_faults": allowed,
    }, items


def render_source_form():
    """Collect source-only inputs and explain deliberate faults before POST."""
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
        "投入先",
        [
            "ランディング",
            "専用の検証SQL Server",
            "両方",
            "固定疑似ERP/MES SQL Server",
            "ランディング + 固定SQL source",
        ],
    )
    destination = {
        "ランディング": "landing",
        "専用の検証SQL Server": "sqlserver",
        "両方": "both",
        "固定疑似ERP/MES SQL Server": "fixed-sqlserver",
        "ランディング + 固定SQL source": "landing-fixed-sqlserver",
    }[destination_label]
    catalog = call_api("/scenarios")
    if catalog is None:
        st.info("異常カタログを確認できません。状態を確認して再試行します。")
        return
    st.session_state["source-scenario-catalog"] = catalog
    selection, items = render_scenario_selection(catalog)
    images = st.checkbox(
        "合成の検査画像PNGも作る",
        disabled=destination in {"sqlserver", "fixed-sqlserver"},
    )
    incompatibilities = []
    for item in items:
        policy_destination = {
            "fixed-sqlserver": "sqlserver",
            "landing-fixed-sqlserver": "both",
        }.get(destination, destination)
        if policy_destination not in item["supported_destinations"]:
            incompatibilities.append(
                item["label"] + "は現在の投入先では使えません。"
            )
        if item.get("requires_images") and not images:
            incompatibilities.append(
                item["label"] + "には画像素材の生成が必要です。"
            )
    for group in catalog.get("conflicts", []):
        conflicts = [item["label"] for item in items if item["id"] in group]
        if len(conflicts) > 1:
            incompatibilities.append(
                "同じ値やファイルを競合して変更します。どちらかを外します: "
                + "、".join(conflicts)
            )
    needs_consent = any(item.get("requires_file_consent") for item in items)
    if needs_consent and not selection["allow_file_faults"]:
        incompatibilities.append("高度なファイル異常への明示同意が必要です。")
    for message in incompatibilities:
        st.error(message)
    with st.form("source-generation"):
        left, right = st.columns(2)
        seed = left.number_input("乱数シード", 0, 2147483647, 42)
        orders = right.number_input("1回あたりの指図・受注数", 1, 1000, 20)
        days = left.number_input("業務日数", 1, 366, 7)
        batches = 1
        interval = 0
        if streaming:
            batches = left.number_input("マイクロバッチ回数", 2, 60, 3)
            interval = right.number_input("投入間隔（秒）", 1, 60, 5)
            st.caption(
                "指定回数で終了します。Kafkaへの直接送信ではありません。"
            )
        consent = False
        fixed_consent = False
        run_id = st.text_input("run ID（空欄なら自動生成、12桁hex）", value="")
        if destination != "landing":
            fixed = destination in {
                "fixed-sqlserver",
                "landing-fixed-sqlserver",
            }
            st.info(
                "固定6表へ投入します。型に入らない原値はJSON列に保持します。"
                if fixed
                else "専用の既存DEMO DBへ新しいschemaを作成します。"
            )
            consent = st.checkbox("検証DBへの書き込みを明示的に許可する")
            if fixed:
                fixed_consent = st.checkbox(
                    "固定3schema・所有権を確認し、"
                    "共有source DBへの書込みに同意する"
                )
            else:
                st.caption(
                    "会社の参照元DB・システムDBへの投入は拒否されます。"
                )
        submitted = st.form_submit_button(
            "素材の投入を開始",
            type="primary",
            disabled=bool(incompatibilities),
        )
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
                **selection,
                "destination": destination,
                "images": images
                and destination
                not in {
                    "sqlserver",
                    "fixed-sqlserver",
                },
                "allow_sql_write": consent,
                "confirm_shared_source_database": fixed_consent,
                "run_id": run_id or None,
            },
        )
        if result is not None:
            job_id = result.get("job_id", result.get("id"))
            if job_id:
                st.session_state["active_job"] = job_id
                st.session_state["job_kind"] = "source"
                st.session_state["job_result"] = result
                st.success(
                    "素材生成を受け付けました。意図した不正素材も生成結果で"
                    "確認し、後続処理の期待挙動と区別してください。"
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


def render_source_diagnostics(results):
    """Distinguish expected bad-source output from generation failures."""
    catalog = st.session_state.get("source-scenario-catalog", {})
    definitions = {item["id"]: item for item in catalog.get("scenarios", [])}
    summaries = []
    row_changes = []
    file_faults = []
    for batch in results:
        diagnostics = batch.get("diagnostics")
        if not isinstance(diagnostics, dict):
            continue
        selected = diagnostics.get("anomalies", [])
        summaries.append(
            {
                "回": batch.get("batch"),
                "選択した異常": "、".join(
                    definitions.get(key, {}).get("label", key)
                    for key in selected
                ),
                "指定率（%）": diagnostics.get("anomaly_rate"),
                "manifest": batch.get("manifest"),
            }
        )
        for change in diagnostics.get("row_changes", []):
            item = definitions.get(change["id"], {})
            row_changes.append(
                {
                    "回": batch.get("batch"),
                    "異常": item.get("label", change["id"]),
                    "対象表": change.get("table"),
                    "変更対象件数": change.get("affected"),
                    "期待する下流の挙動": item.get("expected_effect", ""),
                }
            )
        for fault in diagnostics.get("file_faults", []):
            item = definitions.get(fault["id"], {})
            file_faults.append(
                {
                    "回": batch.get("batch"),
                    "異常": item.get("label", fault["id"]),
                    "対象ファイル": fault.get("path"),
                    "期待する下流の挙動": fault.get("expected_effect", ""),
                }
            )
    if not summaries:
        return
    st.subheader("意図した異常の生成結果")
    st.caption(
        "件数は生成時に変更した対象数です。Quarantine件数やGoldへの"
        "最終影響は、後続処理を明示実行して別に確認します。"
    )
    st.dataframe(summaries, hide_index=True, width="stretch")
    if row_changes:
        st.dataframe(row_changes, hide_index=True, width="stretch")
    if file_faults:
        st.warning(
            "意図したファイル異常の生成が完了しました。生成ジョブの成功と、"
            "下流で想定する取り込み拒否・無視等の結果は別です。"
            "対象ファイルと期待挙動を確認してください。"
        )
        st.dataframe(file_faults, hide_index=True, width="stretch")


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
    if source_job:
        render_source_diagnostics(results)
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
        "SOURCE_SQLSERVER_USER": "参照元reader（SELECT専用）",
        "SOURCE_SQLSERVER_CONTROL_USER": "checkpoint専用ユーザー",
        "SOURCE_SQLSERVER_CONTROL_PASSWORD": "checkpoint専用パスワード",
        "SOURCE_SQLSERVER_BATCH_OVERLAP_SECONDS": "増分で重ねて読む秒数",
        "SOURCE_SQLSERVER_BATCH_SAFETY_LAG_SECONDS": "増分の安全遅延（秒）",
        "DEMO_SQLSERVER_HOST": "ダミー投入用SQL Server",
        "DEMO_SQLSERVER_DATABASE": "ダミー投入専用DB名",
        "DEMO_SQLSERVER_USER": "ダミー投入用ユーザー",
        "DEMO_SQLSERVER_ALLOW_SHARED_SOURCE_DATABASE": "共有source DB許可",
        "DEMO_SQLSERVER_INSTALLATION_ID": "導入ID（既存環境では保持）",
        "DEMO_SQLSERVER_OWNERSHIP_TOKEN": "所有権鍵（既存環境では保持）",
        "DEMO_SQLSERVER_ERP_SCHEMA": "固定ERP schema（変更不可）",
        "DEMO_SQLSERVER_MES_SCHEMA": "固定MES schema（変更不可）",
        "DEMO_SQLSERVER_CONTROL_SCHEMA": "固定制御schema（変更不可）",
        "DEMO_SQLSERVER_TRUST_SERVER_CERTIFICATE": "DEMO:証明書検証省略",
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
    source_tab,
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
        "素材の投入",
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
with history_tab:
    render_history(call_api)
with help_tab:
    render_help(call_api)
with source_tab:
    render_source_form()
    render_sql_source(call_api)
    render_cdc_form()
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
with ai_tab:
    render_ai_context()

# One shared progress area prevents duplicate polling widgets across tabs.
render_job()

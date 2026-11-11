"""Render fixed SQL controls using the same authenticated API as Swagger."""

import streamlit as st


def render_sql_source(call_api):
    """Show initialization diagnostics before source-only test mutations."""
    with st.expander("固定SQL sourceの初期化・状態・CDC用DML"):
        st.caption(
            "DDLはDBAが別途適用します。この画面はschemaを作成・削除しません。"
            "旧PoC表への操作はありません。状態取得だけでは書込みません。"
        )
        if st.button("固定schema・所有権・限定権限を診断", key="sql-diagnose"):
            result = call_api("/sql-source/status", timeout=120)
            if result is not None:
                st.json(result)
        run_id = st.text_input(
            "CDC試験用run ID（12桁hex）",
            value="012345abcdef",
            key="sql-dml-run",
        )
        consent = st.checkbox(
            "所有権を確認した固定SQL sourceの試験レコードへ書き込む",
            key="sql-dml-consent",
        )
        for column, action in zip(
            st.columns(3), ("insert", "update", "delete")
        ):
            with column:
                if st.button(
                    action.upper(),
                    key="sql-fixed-" + action,
                    disabled=not consent,
                ):
                    result = call_api(
                        "/sql-source/demo/" + action,
                        method="POST",
                        payload={"confirm": True, "run_id": run_id},
                    )
                    if result is not None:
                        st.session_state["active_job"] = result["id"]
                        st.session_state["job_kind"] = "cdc-source"
                        st.session_state["job_result"] = result
                        st.success(
                            "SQL操作を受け付けました。完了を照会します。"
                        )
        st.caption("DMLの完了はKafka/Icebergへの到着成功を意味しません。")


def render_sql_batches(call_api):
    """Keep extraction separate from source generation and require consent."""
    st.subheader("固定SQL sourceから取り込む")
    st.caption(
        "SOURCE readerで固定6表を読み、SQL用Bronze・Silver・Goldを更新します。"
        "Landing用・CDC用の分析表は別です。読取り中は短時間のlockを使います。"
    )
    confirmed = st.checkbox(
        "固定SQL sourceの全snapshotを取得しSQL用分析表を置換する",
        key="sql-snapshot-consent",
    )
    if st.button(
        "SQL Serverフルスナップショット",
        key="sql-snapshot",
        disabled=not confirmed,
    ):
        result = call_api(
            "/sql-source/batches/snapshot",
            method="POST",
            payload={"confirm": True},
        )
        if result is not None:
            st.session_state["active_job"] = result["id"]
            st.session_state["job_kind"] = "pipeline"
            st.session_state["job_result"] = result
            st.success("snapshot処理を受け付けました。最終状態を確認します。")
    st.caption(
        "途中失敗では表の世代が混ざることがあります。再実行で収束を確認し、"
        "SQL用GoldをSupersetへ登録します。workerのtimeoutは結果不明です。"
    )
    st.info(
        "増分の前に専用control writerを設定し、snapshotの結果で"
        "checkpointed=trueを確認します。readerへ書込み権限は追加しません。"
    )
    incremental = st.checkbox(
        "重ね読みした増分を反映し、成功後だけ専用watermarkを更新する",
        key="sql-incremental-consent",
    )
    if st.button(
        "Watermark増分バッチ", key="sql-incremental", disabled=not incremental
    ):
        result = call_api(
            "/sql-source/batches/incremental",
            method="POST",
            payload={"confirm": True},
        )
        if result is not None:
            st.session_state["active_job"] = result["id"]
            st.session_state["job_kind"] = "pipeline"
            st.session_state["job_result"] = result
            st.success(
                "増分処理を受け付けました。完了とcheckpointを確認します。"
            )
    st.caption(
        "countsは重ね読みを含む抽出件数です。新規行数とは異なります。"
        "窓を超える遅延commit・物理DELETEは全件照合かCDCで補います。"
    )


def render_sql_observations(call_api):
    """Read measured state explicitly; queueing never claims completion."""
    st.subheader("SQL batch・CDCの観測とGold")
    st.caption(
        "件数と状態はそれぞれの観測時点です。未観測は0件と区別します。"
        "Raw CDCの業務payloadや接続の秘密値は表示しません。"
    )
    if st.button("SQL batchの状態を更新", key="sql-observe-batch"):
        value = call_api("/sql-source/pipeline-status", timeout=60)
        if value is not None:
            st.session_state["sql-observation-batch"] = value
    batch = st.session_state.get("sql-observation-batch")
    if batch:
        st.caption(
            "batchの前回観測: "
            + batch["state"]
            + " / "
            + str(batch.get("observed_at"))
        )
        st.dataframe(batch["layers"], hide_index=True, width="stretch")
        if batch.get("pending") is True:
            st.warning(
                "SQL用分析表は処理途中です。再実行・照合で収束を確認します。"
            )
        if batch.get("last_batch"):
            with st.expander("最後に記録されたbatch完了"):
                st.json(batch["last_batch"])
        else:
            st.info(
                "batch完了の観測はありません。停止や未実行の断定ではありません。"
            )

    with st.expander("SQL watermarkを明示的に照会する"):
        st.caption(
            "この照会だけはprivate設定のSQL controlへ接続します。"
            "参照資格情報へ書込み権限は追加しません。"
        )
        if st.button("SQL watermarkを照会", key="sql-observe-watermark"):
            value = call_api("/sql-source/checkpoints", timeout=120)
            if value is not None:
                st.session_state["sql-observation-watermarks"] = value
        positions = st.session_state.get("sql-observation-watermarks")
        if positions:
            st.json(positions)

    if st.button("CDCの状態を更新", key="sql-observe-cdc"):
        value = call_api("/sql-source/cdc/status", timeout=60)
        if value is not None:
            st.session_state["sql-observation-cdc"] = value
    cdc = st.session_state.get("sql-observation-cdc")
    consumer = cdc.get("consumer", {}) if cdc else {}
    if cdc:
        st.caption(
            "consumer観測: "
            + consumer.get("state", "unobserved")
            + " / Connect観測: "
            + cdc["connector"]["state"]
        )
        if not cdc["configured"]:
            st.info("固定CDCのprofile・mode・準備状況を確認します。")
        st.write(
            "consumerの観測時刻: " + str(consumer.get("observation_time"))
        )
        st.write("consumerの最終成功: " + str(consumer.get("last_success")))
        st.metric(
            "保存済みRaw CDC件数",
            cdc["raw_rows"] if cdc["raw_rows"] is not None else "未観測",
        )
        since_start = consumer.get("raw_events_appended_since_start")
        st.metric(
            "このconsumer起動中のRaw追記件数",
            since_start if since_start is not None else "未観測",
        )
        st.dataframe(cdc["layers"], hide_index=True, width="stretch")
        if consumer.get("partitions"):
            st.dataframe(
                consumer["partitions"], hide_index=True, width="stretch"
            )
        else:
            st.caption(
                "offset・lagは未観測です。0への追いつきと取り違えません。"
            )
        if consumer.get("rebuild"):
            st.json(consumer["rebuild"])

    with st.expander("保存済みRaw CDCから現在値を再構築する"):
        st.caption(
            "Raw snapshotを固定し、現在値とCDC用分析表を再構築します。"
            "ソースDBのDDLや、Kafkaのoffset初期化は行いません。"
        )
        confirm = st.checkbox(
            "Raw履歴からの再構築を明示的に許可する", key="sql-rebuild-consent"
        )
        rebuilding = consumer.get("rebuild") or {}
        busy = consumer.get("busy") is True or rebuilding.get("status") in {
            "queued",
            "running",
        }
        if st.button(
            "CDCの現在値を再構築",
            key="sql-rebuild-request",
            disabled=not confirm or busy,
        ):
            result = call_api(
                "/sql-source/cdc/rebuild",
                method="POST",
                payload={"confirm": True},
                timeout=15,
            )
            if result is not None:
                st.session_state["sql-last-rebuild-request"] = result[
                    "request_id"
                ]
                st.info(
                    "consumerへ要求を受け付けました。完了はCDCの状態を更新して"
                    "同じ要求IDで確認します。受付だけでは再構築完了ではありません。"
                )
        requested = st.session_state.get("sql-last-rebuild-request")
        if requested:
            st.code("request_id: " + requested, language="text")
            if rebuilding.get("request_id") != requested:
                st.caption(
                    "この要求IDの完了をまだ観測していません。状態を確認し、"
                    "受付結果不明のまま要求を重ねないでください。"
                )

    with st.expander("SQL系Goldをsnapshot固定でプレビューする"):
        origin = st.selectbox(
            "Goldの由来",
            ("batch", "cdc"),
            format_func=lambda value: {
                "batch": "SQL batch",
                "cdc": "SQL CDC",
            }[value],
            key="sql-preview-origin",
        )
        dataset = st.selectbox(
            "Goldのデータ",
            ("production", "sales"),
            format_func=lambda value: {
                "production": "工場日別生産",
                "sales": "受注別出荷",
            }[value],
            key="sql-preview-dataset",
        )
        limit = st.number_input(
            "プレビューの最大行数", 1, 100, 20, key="sql-preview-limit"
        )
        if st.button("SQL系Goldを取得", key="sql-preview-request"):
            value = call_api(
                "/sql-source/preview?origin="
                + origin
                + "&dataset="
                + dataset
                + "&limit="
                + str(int(limit)),
                timeout=120,
            )
            if value is not None:
                st.session_state["sql-gold-preview"] = value
        preview = st.session_state.get("sql-gold-preview")
        if (
            preview
            and preview["origin"] == origin
            and preview["dataset"] == dataset
            and preview["row_limit"] == int(limit)
        ):
            st.caption(
                preview["table"] + " / snapshot " + preview["snapshot_id"]
            )
            st.dataframe(preview["rows"], hide_index=True, width="stretch")
            st.caption(
                "前回取得した上限付き抜粋です。全件集計や同時snapshotではありません。"
            )

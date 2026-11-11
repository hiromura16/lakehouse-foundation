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

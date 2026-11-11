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

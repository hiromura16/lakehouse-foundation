"""Render a PoC portal using only authenticated fixed API operations."""

import json
import re

import streamlit as st

STATE_LABELS = {
    "not_enabled": "profile未選択",
    "unconfigured": "必要設定が未完了",
    "available": "稼働を確認",
    "unreachable": "停止・起動待ち・未到達",
}


def render_portal(call_api):
    """Separate service configuration from observed reachability."""
    st.subheader("PoCで使うサービス")
    st.caption(
        "入口は適用済み設定から表示します。別端末のlocalhostはその端末自身です。"
        "会社標準のSSHポート転送や限定入口を使ってください。"
    )
    result = call_api("/services")
    if result is None:
        return
    if result.get("pending_restart"):
        st.warning(
            "保存した変更は反映待ちです。現在の稼働設定の入口を表示します。"
        )
    columns = st.columns(3)
    for index, service in enumerate(result.get("services", [])):
        with columns[index % 3]:
            with st.container(border=True):
                st.markdown("**" + service["title"] + "**")
                st.write(STATE_LABELS[service["state"]])
                st.caption(service["description"])
                profiles = service.get("profiles") or [service["profile"]]
                st.caption("profile: " + (" または ".join(profiles) or "core"))
                missing = service.get("missing_settings", [])
                if missing:
                    st.caption("確認する設定: " + ", ".join(missing))
                if service["id"] == "mcp":
                    st.code(service["url"], language="text")
                    st.caption("このURLをAIクライアントの接続先に設定します。")
                else:
                    st.link_button(
                        service["title"] + "を開く",
                        service["url"],
                        disabled=not service["enabled"]
                        or not service["configured"],
                    )

    st.caption(
        "未到達は未導入の断定ではありません。停止、起動待ち、内部経路を"
        "確認します。ログインの秘密値はURLへ含めません。"
    )
    with st.expander("AIやAPIクライアントが使える操作"):
        metadata = call_api("/capabilities")
        if metadata:
            st.dataframe(
                [
                    {
                        "操作": item["description"],
                        "メソッド": item["method"],
                        "API": item["path"],
                        "接続先": item.get("service", "control_api"),
                        "種類": item["access"],
                    }
                    for item in metadata.get("capabilities", [])
                ],
                hide_index=True,
                width="stretch",
            )


def render_data(call_api):
    """Explain grain and provenance before displaying bounded Gold data."""
    st.subheader("データの意味と確認")
    result = call_api("/catalog")
    if result is None or not result.get("datasets"):
        return
    datasets = {item["id"]: item for item in result["datasets"]}
    selected = st.selectbox(
        "データ契約",
        list(datasets),
        format_func=lambda key: datasets[key]["title"],
        key="portal-dataset",
    )
    item = datasets[selected]
    st.write("粒度: " + item["grain"])
    st.write("業務キー: " + ", ".join(item["primary_key"]))
    st.code(item["table"], language="text")
    st.dataframe(item["columns"], hide_index=True, width="stretch")
    st.caption("由来: " + " → ".join(item["provenance"]))
    st.caption(
        "これは公開契約です。会社の接続先・実表名の対応表は返しません。"
    )
    st.markdown("**Goldを上限付きで確認する**")
    target = st.selectbox(
        "プレビューの対象",
        ["production", "sales", "all"],
        key="portal-preview-target",
    )
    limit = st.number_input(
        "プレビューの表ごとの最大行数", 1, 500, 20, key="portal-preview-limit"
    )
    if st.button("Goldをプレビュー", key="portal-preview"):
        with st.spinner("mainのsnapshotを固定して読み取っています…"):
            preview = call_api(
                f"/catalog/preview?dataset={target}&limit={int(limit)}",
                timeout=120,
            )
        if preview is not None:
            st.session_state["portal-preview-result"] = preview
    preview = st.session_state.get("portal-preview-result")
    if preview:
        for data in preview.get("datasets", []):
            st.write(data["table"] + " / snapshot " + data["snapshot_id"])
            st.dataframe(
                data.get("rows", []), hide_index=True, width="stretch"
            )
        st.caption(
            "上限付きの抜粋です。全件の合計・表間の同時点とは扱いません。"
        )
        st.download_button(
            "プレビューJSONを保存",
            json.dumps(preview, ensure_ascii=False),
            "gold-preview.json",
            "application/json",
        )
    if st.button("品質件数を確認", key="portal-quality"):
        result = call_api("/quality", timeout=15)
        if result is not None:
            st.session_state["portal-quality-result"] = result
    quality = st.session_state.get("portal-quality-result")
    if quality:
        st.dataframe(
            quality.get("datasets", []), hide_index=True, width="stretch"
        )
        st.caption(quality.get("description", ""))


def render_help(call_api):
    """Read allowlisted bundled help and diagrams without a browser CDN."""
    st.subheader("同梱ヘルプと構成図")
    listing = call_api("/help")
    if listing is None:
        return
    documents = {
        item["slug"]: item
        for item in listing.get("documents", [])
        if item.get("available")
    }
    if documents:
        selected = st.selectbox(
            "読む文書",
            list(documents),
            format_func=lambda key: documents[key]["title"],
            key="portal-help",
        )
        st.caption(documents[selected]["description"])
        if st.button("文書を開く", key="portal-open-help"):
            result = call_api("/help/" + selected)
            if result is not None:
                st.session_state["portal-help-result"] = result
        document = st.session_state.get("portal-help-result")
        if document:
            st.markdown("### " + document["title"])
            # Diagrams use the bundled asset API instead of Markdown URLs.
            content = re.sub(
                r"!\[[^\]]*\]\([^)]*\)",
                "（構成図は下の同梱図から開けます）",
                document["markdown"],
            )
            st.markdown(content)
            st.caption("関連文書は上の文書一覧から選択できます。")
    else:
        st.info("文書を同梱したimageへ更新してください。")
    assets = {
        item["id"]: item
        for item in listing.get("assets", [])
        if item.get("available")
    }
    if assets:
        selected = st.selectbox(
            "構成図",
            list(assets),
            format_func=lambda key: assets[key]["title"],
            key="portal-help-asset",
        )
        if st.button("同梱図を表示", key="portal-open-asset"):
            result = call_api("/help/assets/" + selected)
            if result is not None:
                st.session_state["portal-help-asset-result"] = result
        asset = st.session_state.get("portal-help-asset-result")
        if asset:
            st.image(asset["content"], caption=asset["title"])


def render_network(call_api):
    """Make the chosen settings scope explicit before fixed HTTPS checks."""
    st.subheader("会社proxyとCAの診断")
    saved = st.checkbox(
        "保存済み・反映前の設定で診断する", key="network-use-saved"
    )
    st.caption(
        "既定はAPI起動時の適用済み設定です。診断から設定反映は行いません。"
    )
    report = call_api("/network/config?use_saved=" + str(saved).lower())
    if report:
        if report.get("pending_restart"):
            st.warning(
                "設定に反映待ちの変更があります。診断対象の選択を確認します。"
            )
        st.write("診断対象: " + report["configuration_scope"])
        configuration = report.get("configuration", {})
        st.dataframe(
            [
                {"変数": key, **value}
                for key, value in configuration.get("variables", {}).items()
            ],
            hide_index=True,
            width="stretch",
        )
        bypass = configuration.get("internal_bypass", {})
        missing = [key for key, value in bypass.items() if not value]
        if missing:
            st.info("NO_PROXYで確認する内部名: " + ", ".join(missing))
        st.caption(
            "CA設定の有無: "
            + str(configuration.get("custom_ca_configured", False))
        )
    targets = st.multiselect(
        "接続する固定HTTPS宛先",
        ["pypi", "maven", "npm", "github"],
        default=["pypi", "maven", "npm", "github"],
        key="network-targets",
    )
    if st.button(
        "選んだ宛先へ接続診断", disabled=not targets, key="network-checks"
    ):
        result = call_api(
            "/network/checks",
            method="POST",
            payload={"targets": targets, "use_saved": saved},
            timeout=15,
        )
        if result is not None:
            st.session_state["network-check-result"] = result
    result = st.session_state.get("network-check-result")
    if result:
        st.write("実行した設定: " + result["configuration_scope"])
        st.dataframe(
            result.get("checks", []), hide_index=True, width="stretch"
        )
        st.caption("URL・認証情報・CAのpath・生の例外は診断結果に含めません。")


def render_history(call_api):
    """Review known jobs without treating history as permission to retry."""
    st.subheader("最近の実行を見直す")
    result = call_api("/jobs?limit=20")
    if result is None:
        return
    st.caption(
        "現在のAPIプロセス内の最大20件です。再起動すると履歴は失われます。"
        "再表示から処理を再実行・自動再開しません。"
    )
    jobs = {item["id"]: item for item in result.get("jobs", [])}
    if not jobs:
        st.info("このAPIプロセスに履歴はまだありません。")
        return
    st.dataframe(
        [
            {
                key: item.get(key)
                for key in ("id", "kind", "status", "completed", "batches")
            }
            for item in jobs.values()
        ],
        hide_index=True,
        width="stretch",
    )
    identifier = st.selectbox("結果を見るジョブ", list(jobs), key="portal-job")
    selected = jobs[identifier]
    if selected.get("error"):
        st.warning(
            selected["error"].get("message", "結果を確認してください。")
        )
    if st.button("共通の進捗欄に表示", key="portal-show-job"):
        st.session_state["active_job"] = identifier
        st.session_state["job_kind"] = selected["kind"]
        st.session_state["job_result"] = selected

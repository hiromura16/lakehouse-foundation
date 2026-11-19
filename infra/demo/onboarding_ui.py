"""Build private source-contract drafts with widgets, without DB changes.

The renderer calls only the onboarding proposal API after an explicit user
action. Draft review checkboxes are self-declared notes, never authorization
receipts. Every rerun binds downloadable evidence to all current inputs.
"""

import hashlib
import json

import streamlit as st

MAX_METADATA_BYTES = 256 * 1024
CLASSIFICATIONS = {
    "unknown": "未確認",
    "public": "公開可能",
    "internal": "社内限定",
    "sensitive": "機密・個人情報を含む",
    "excluded": "対象外",
}
DELETE_POLICIES = {
    "unreviewed": "まだ決めていない",
    "retain": "削除前の情報を保持したい",
    "propagate": "削除を連携先にも反映したい",
}
REVIEW_ROLES = {
    "business": "現場・業務担当",
    "technical": "システム担当",
    "dba": "DBA",
    "security": "情報管理・セキュリティ担当",
}


def fingerprint(value):
    """Bind all current private inputs without logging their contents."""
    encoded = json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def parse_discovery_upload(content):
    """Reject oversized, ambiguous or malformed JSON before a request is sent.

    File extensions are only a UI aid. The server separately performs strict
    metadata schema validation and must reject credentials and unknown keys.
    """
    if not isinstance(content, bytes) or len(content) > MAX_METADATA_BYTES:
        raise ValueError("JSONファイルは256KiB以下にしてください。")

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("同じ項目名が重複するJSONは使えません。")
            result[key] = value
        return result

    def reject_constant(_value):
        raise ValueError("JSONにNaNやInfinityは使えません。")

    try:
        value = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise ValueError(
            "有効なUTF-8のメタデータJSONを選んでください。"
        ) from None
    if not isinstance(value, dict):
        raise ValueError("メタデータJSONの最上位はオブジェクトにします。")
    return value


def forget_results():
    """Invalidate evidence and review notes after input changes."""
    for key in list(st.session_state):
        if key.startswith(("onboarding-result-", "onboarding-review-")):
            del st.session_state[key]


def load_discovery(call_api):
    """Load metadata only after an explicit action, never on tab rendering."""
    if st.button("合成6表で試す", key="onboarding-example"):
        response = call_api("/onboarding/example")
        if response:
            # Both the API and standalone preview return this fixed envelope.
            discovery = response.get("discovery")
            if discovery:
                forget_results()
                st.session_state["onboarding-discovery"] = discovery
    uploaded = st.file_uploader(
        "メタデータJSONを読み込む（任意）",
        type=["json"],
        key="onboarding-upload",
        help="行データ、パスワード、接続文字列を含めないでください。",
    )
    st.caption(
        "この試作はDBへ接続しません。社内の表名・列名も機密情報に"
        "なるため、実metadataの取込みは許可された環境だけで行います。"
    )
    if st.button(
        "選んだJSONを検査して読み込む",
        key="onboarding-import",
        disabled=uploaded is None,
    ):
        try:
            if uploaded.size > MAX_METADATA_BYTES:
                raise ValueError("JSONファイルは256KiB以下にしてください。")
            discovery = parse_discovery_upload(uploaded.getvalue())
        except ValueError as error:
            st.error(str(error))
        else:
            response = call_api(
                "/onboarding/validate-discovery",
                method="POST",
                payload=discovery,
            )
            if response:
                forget_results()
                st.session_state["onboarding-discovery"] = response[
                    "discovery"
                ]


def render_table(table, metadata_digest):
    """Collect business decisions while retaining detected technical facts."""
    table_id = table["schema_name"] + "." + table["table_name"]
    prefix = "onboarding-table-" + metadata_digest + "-" + table_id
    columns = {column["name"]: column for column in table["columns"]}
    primary_key = table["primary_key"]
    with st.expander(table_id, expanded=True):
        st.caption("検出した主キー候補: " + (", ".join(primary_key) or "なし"))
        st.dataframe(
            [
                {
                    "列": item["name"],
                    "SQL型": item["sql_type"],
                    "NULL可": item["nullable"],
                    "主キー候補": item["name"] in primary_key,
                    "計算列": item.get("computed", False),
                }
                for item in columns.values()
            ],
            hide_index=True,
            width="stretch",
        )
        selected = st.multiselect(
            "取り込む列",
            list(columns),
            default=list(columns),
            key=prefix + "-columns",
        )
        logical_name = st.text_input(
            "業務上の名前",
            value=table["table_name"],
            max_chars=128,
            key=prefix + "-logical-name",
        )
        purpose = st.text_input(
            "このデータを使う目的",
            max_chars=512,
            key=prefix + "-purpose",
        )
        freshness = st.number_input(
            "必要な鮮度（分）",
            min_value=1,
            max_value=10080,
            value=60,
            key=prefix + "-freshness",
            help="業務要望の記録です。実際の遅延やSLAを保証しません。",
        )
        deletion = st.selectbox(
            "元のデータが削除されたとき",
            list(DELETE_POLICIES),
            format_func=DELETE_POLICIES.get,
            key=prefix + "-delete",
        )
        confirmed = st.checkbox(
            "主キー候補を確認した（この下書きの確認メモ）",
            key=prefix + "-primary-key",
        )
        st.caption(
            "情報区分は人が判断します。未確認や機密の列を自動承認しません。"
        )
        classifications = {
            name: st.selectbox(
                name + " の情報区分",
                list(CLASSIFICATIONS),
                format_func=CLASSIFICATIONS.get,
                key=prefix + "-classification-" + name,
            )
            for name in selected
        }
    return {
        "schema_name": table["schema_name"],
        "table_name": table["table_name"],
        "included_columns": selected,
        "logical_name": logical_name,
        "business_purpose": purpose,
        "freshness_minutes": int(freshness),
        "delete_handling": deletion,
        "column_classifications": classifications,
        "primary_key_confirmed": confirmed,
    }


def render_findings(result):
    """Show server-owned messages without displaying raw validation errors."""
    for item in result.get("blockers", []):
        st.error(item["code"] + ": " + item["message"])
    for item in result.get("warnings", []):
        st.warning(item["code"] + ": " + item["message"])


def render_results(call_api, payload, input_digest):
    """Expose only evidence tied to current input and review fingerprints."""
    if st.button(
        "契約案を作る",
        key="onboarding-propose",
        disabled=not payload["selections"],
    ):
        result = call_api(
            "/onboarding/propose", method="POST", payload=payload
        )
        if result:
            forget_results()
            st.session_state["onboarding-result-proposal"] = result
            st.session_state["onboarding-result-input"] = input_digest
    proposal = st.session_state.get("onboarding-result-proposal")
    if not proposal:
        return
    st.subheader("3. 未決事項と確認メモ")
    render_findings(proposal)
    st.info(
        "以下はこの画面の自己申告メモです。本人確認、権限別承認、"
        "DBA適用許可としては扱いません。"
    )
    review = {
        role: st.checkbox(
            label + "の確認メモを付ける",
            key="onboarding-review-" + role,
        )
        for role, label in REVIEW_ROLES.items()
    }
    review["contract_sha256"] = proposal["contract_sha256"]
    plan_payload = {**payload, "review": review}
    plan_digest = fingerprint(plan_payload)
    if st.session_state.get("onboarding-result-plan-input") != plan_digest:
        st.session_state.pop("onboarding-result-plan", None)
    if st.button("実行しない計画を確認", key="onboarding-plan"):
        result = call_api(
            "/onboarding/plan", method="POST", payload=plan_payload
        )
        if result:
            st.session_state["onboarding-result-plan"] = result
            st.session_state["onboarding-result-plan-input"] = plan_digest
    plan = st.session_state.get("onboarding-result-plan")
    if plan:
        render_findings(plan)
        st.write("適用: 不可 ／ 正式な承認: 未検証 ／ DB確認: 未実施")
        st.dataframe(plan.get("steps", []), hide_index=True, width="stretch")
    st.caption(
        "保存先はブラウザーのダウンロード設定に従います。metadataを含む"
        "ため非公開の保管場所を選んでください。サーバーへの永続保存や"
        "保存先の0600設定を、このボタンで保証するものではありません。"
    )
    st.download_button(
        "非公開の契約案JSONを保存",
        data=json.dumps(proposal, ensure_ascii=False, indent=2),
        file_name="source-contract-draft.json",
        mime="application/json",
        key="onboarding-download-draft",
    )
    if plan:
        st.download_button(
            "非公開の計画JSONを保存",
            data=json.dumps(plan, ensure_ascii=False, indent=2),
            file_name="source-onboarding-plan.json",
            mime="application/json",
            key="onboarding-download-plan",
        )
    with st.expander("技術担当向け: 自動生成された内容"):
        st.json(plan or proposal)


def render_onboarding(call_api):
    """Guide a person from metadata to a non-executable contract draft."""
    st.subheader("データ連携の組み立て（試作）")
    st.write(
        "YAMLを書かずに、表・列と業務上の使い方を選びます。"
        "この画面は契約案と計画を作るだけで、DBやCDCを変更しません。"
    )
    st.subheader("1. 元になる表の情報を選ぶ")
    load_discovery(call_api)
    discovery = st.session_state.get("onboarding-discovery")
    if not discovery:
        st.info("まず合成6表で操作を試せます。実DB接続は不要です。")
        return
    metadata_digest = fingerprint(discovery)
    tables = {
        table["schema_name"] + "." + table["table_name"]: table
        for table in discovery["tables"]
    }
    st.caption(
        "情報の由来: "
        + (
            "合成metadata"
            if discovery["origin"] == "synthetic"
            else "取込JSON"
        )
        + " ／ DBの実在・権限・CDCの稼働は未確認"
    )
    st.subheader("2. 連携する表と使い方を選ぶ")
    source_id = st.text_input(
        "連携の識別名（英数字）",
        value="configured-demo",
        max_chars=64,
        key="onboarding-source-id",
    )
    owner = st.text_input(
        "責任を持つ部門・チーム",
        max_chars=128,
        key="onboarding-owner-team",
    )
    selected = st.multiselect(
        "連携するテーブル（試作は最大6表）",
        list(tables),
        default=list(tables)[:1],
        max_selections=6,
        key="onboarding-tables-" + metadata_digest,
    )
    payload = {
        "discovery": discovery,
        "source_id": source_id,
        "owner_team": owner,
        "selections": [
            render_table(tables[key], metadata_digest) for key in selected
        ],
    }
    input_digest = fingerprint(payload)
    previous = st.session_state.get("onboarding-result-input")
    if previous and previous != input_digest:
        forget_results()
        st.warning(
            "入力が変わったため、以前の契約案・計画・確認メモを無効にしました。"
            "現在の内容で契約案を作り直してください。"
        )
    render_results(call_api, payload, input_digest)

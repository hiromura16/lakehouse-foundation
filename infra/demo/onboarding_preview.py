"""Run the onboarding GUI against the pure draft engine, without services.

Use the repository's hash-locked uv development environment and Streamlit's
loopback launch option. This entry reads only the bundled synthetic contract;
it does not load site.env, connect to databases or register a connector.
"""

import streamlit as st
from onboarding_contract import (
    Discovery,
    ProposalRequest,
    builtin_discovery,
    make_plan,
    metadata_hash,
    propose,
)
from onboarding_ui import render_onboarding
from pydantic import ValidationError


def local_request(path, *, method="GET", payload=None, timeout=15):
    """Mirror the API's fixed draft routes without making HTTP requests."""
    del timeout
    try:
        if path == "/onboarding/example" and method == "GET":
            discovery = builtin_discovery()
            return {
                "schema_version": 1,
                "discovery": discovery,
                "metadata_sha256": metadata_hash(discovery),
                "database_checked": False,
            }
        if path == "/onboarding/schema" and method == "GET":
            return {
                "schema_version": 1,
                "discovery_schema": Discovery.model_json_schema(),
                "proposal_schema": ProposalRequest.model_json_schema(),
            }
        if path == "/onboarding/validate-discovery" and method == "POST":
            discovery = Discovery.model_validate(payload).model_copy(
                update={"origin": "imported"}
            )
            return {
                "schema_version": 1,
                "discovery": discovery.model_dump(mode="json"),
                "metadata_sha256": metadata_hash(discovery),
                "database_checked": False,
            }
        if path == "/onboarding/propose" and method == "POST":
            return propose(payload)
        if path == "/onboarding/plan" and method == "POST":
            return make_plan(payload)
    except (ValidationError, ValueError, TypeError):
        # Pydantic exceptions may contain rejected private values. Only an
        # application-owned generic message reaches the preview UI.
        st.error("入力項目・型・重複・選択した表と列を確認してください。")
        return None
    st.error("この試作では許可した下書き操作だけを利用できます。")
    return None


def main():
    """Open a standalone metadata editor with no deployment capability."""
    st.set_page_config(page_title="データ連携の組み立て", page_icon="🧩")
    st.title("メタデータからデータ連携を組み立てる")
    st.caption("ローカル試作 / DB接続なし / 正式承認・適用なし")
    render_onboarding(local_request)


if __name__ == "__main__":
    main()

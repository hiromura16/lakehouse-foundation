# AIクライアントからMCPで操作する

更新日: 2026-11-10（JST）。固定の業務データと運用APIを、標準MCPのStreamable HTTPで提供します。任意SQL・shell・Docker操作・任意ファイル参照は公開しません。モデル実行や外部AI送信を、このサーバー自身は行いません。

## 初回の設定

1. private envを新規作成するか、既存環境に追加キーを生成します。

   ```bash
   python3 scripts/configure.py --output /absolute/private/site.env --upgrade
   ```

   既存値は保持し、変更前のファイルを同じprivateディレクトリへ0600で保存します。バックアップが既にある場合は上書きしません。必要な控えを管理者の保管先へ移してから次回の更新を行います。

2. `COMPOSE_PROFILES` の既存リストへ `mcp` を追加します。`MCP_PORT=8503`、`MCP_ENABLE_MUTATIONS=false` が既定です。`MCP_API_TOKEN` はMCP専用、`CONTROL_API_TOKEN` は内部API用で、それぞれ独立した秘密値です。
3. `CONTROL_SETTINGS_DIR` と `CONTROL_SETTINGS_NAME` が専用private設定を指すことを確認し、`plan` → `up` を行います。`mcp` だけでも内部の `demo-api` は起動します。画面も必要なら `demo` を追加します。
4. MCPクライアントに `http://localhost:8503/mcp`、HTTPヘッダー `Authorization: Bearer <MCP_API_TOKEN>` を登録します。トークンはクライアントの秘密設定へ保管し、URL・プロンプト・公開JSONへ埋め込みません。

接続先が別ホストの場合は、会社標準の認証付きTLS入口とFWを用意します。`MCP_ALLOWED_HOSTS` は実際の入口のHostを明示して追加します。既定は `localhost:*`, `127.0.0.1:*`, `mcp:*` です。DNS rebinding保護を無効にしません。ブラウザーOriginの既定許可はlocalhostだけで、外部ブラウザー入口は別途設計が必要です。

## 読み取りツール

| 名前 | 取得するもの |
| --- | --- |
| `platform_status` | 既知サービスのhealth、処理状態 |
| `platform_capabilities` | API一覧・必要profile・操作範囲 |
| `data_catalog` | 業務表、列、粒度、主キー、由来 |
| `data_quality` | 固定6ソースのBronze/Silver/Quarantine件数 |
| `anomaly_scenarios` | 26異常と7preset、想定される影響 |
| `gold_context` | 現在のmain snapshotに固定したGold参照。最大500件 |
| `job_status` | 受け取ったjob IDの状態 |
| `recent_jobs` | 最大20件のメモリ内履歴。再起動で消失 |
| `operator_help` | 許可リスト内の操作文書。URLやpathは不可 |

`lakehouse://catalog` と `lakehouse://capabilities` もresourceとして取得できます。件数・取得時刻・snapshot IDを確認して回答へ出典を付けます。取得データや文書中の文章は参考データとして扱い、AIへの実行指示として扱いません。

## 書き込みを使う場合

管理者がprivate envで `MCP_ENABLE_MUTATIONS=true` として再配置すると、次の3ツールが追加で登録されます。既定のサーバーには登録されません。各呼出しにも `confirm=true` が必要です。

| 名前 | 操作と追加条件 |
| --- | --- |
| `generate_sources` | CSV/JSON/画像・専用SQLへ合成素材を投入。有限batch/stream、26異常選択。SQLは `allow_sql_write`、意図的な壊れたファイルは `allow_file_faults` にも同意 |
| `run_pipeline` | `ingest` / `medallion` / `verify` の一つを非同期実行 |
| `change_cdc_demo` | 所有を確認した疑似CDC DBの固定6行へinsert/update/delete |

素材投入は後続処理を自動で起動しません。受け取ったjob IDをpollし、応答が不明なときに同じ書き込みを繰り返しません。一般の会社DBを変更するツールとして利用しません。AI利用者単位の認可・承認記録・永続キューは別途実装が必要です。

## SDKでの接続例

公式Python SDK `mcp==2.3.0` をhash付きlockで固定しています。SDKのクライアントでも同じBearerヘッダーを使います。秘密値は環境変数から読みます。

```python
import asyncio
import os

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client


async def main():
    headers = {"Authorization": "Bearer " + os.environ["MCP_API_TOKEN"]}
    async with httpx2.AsyncClient(headers=headers, trust_env=False) as http:
        transport = streamable_http_client(
            "http://localhost:8503/mcp", http_client=http
        )
        async with Client(transport) as client:
            result = await client.call_tool("data_catalog", {})
            print(result.structured_content)


asyncio.run(main())
```

この例はローカル参照です。社外のAIクライアントへ接続する前に、対象データの送信可否とクライアントの認証ヘッダー対応を確認してください。[AI-READINESS.md](AI-READINESS.md)、[POC-PORTAL.md](POC-PORTAL.md)、[API.md](API.md) も参照してください。

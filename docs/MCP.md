# AIクライアントからMCPで操作する

照合日: 2026-11-10。公式Python SDK **2.3.0**、`httpx2==2.16.0`、`infra/mcp/server.py` が対象です。固定の業務データと運用APIを、標準MCPのStreamable HTTPで提供します。任意SQL・shell・Docker操作・任意ファイル参照は公開しません。モデル実行や外部AI送信を、このサーバー自身は行いません。

## 初回の設定

1. private envがまだなければ `--upgrade` を付けずに生成します。既存環境に追加キーを生成する場合は次を実行します。

   ```bash
   python3 scripts/configure.py \
     --output "$HOME/.config/lakehouse-foundation/site.env" --upgrade
   ```

   既存値は保持し、変更前のファイルを同じprivateディレクトリへ0600で保存します。バックアップが既にある場合は上書きしません。必要な控えを管理者の保管先へ移してから次回の更新を行います。

2. `COMPOSE_PROFILES` の既存リストへ `mcp` を追加します。`MCP_PORT=8503`、`MCP_ENABLE_MUTATIONS=false` が既定です。`MCP_API_TOKEN` はMCP専用、`CONTROL_API_TOKEN` は内部API用で、それぞれ独立した秘密値です。
3. `CONTROL_SETTINGS_DIR` と `CONTROL_SETTINGS_NAME` が専用private設定を指すことを確認し、`plan` → `up` を行います。`mcp` だけでも内部の `demo-api` は起動します。画面も必要なら `demo` を追加します。
4. MCPクライアントに `http://localhost:8503/mcp`、HTTPヘッダー `Authorization: Bearer <MCP_API_TOKEN>` を登録します。トークンはクライアントの秘密設定へ保管し、URL・プロンプト・公開JSONへ埋め込みません。

CLIの起動例はリポジトリrootで次のとおりです。wrapperが7本のComposeを全て選択します。Docker Composeを直接操作する場合は [COMPATIBILITY.md](COMPATIBILITY.md#composeを直接操作する場合の省略しない例) の7ファイルの定義を使います。

```bash
export LAKEHOUSE_ENV_FILE="$HOME/.config/lakehouse-foundation/site.env"
./scripts/lakehouse.sh plan
./scripts/lakehouse.sh up
```

Dokployではサービス自身のEnvironmentへ生成・upgrade済みの設定を保存してからDeployします。`mcp` だけを有効にしても依存先 `demo-api` は必要で、別のMCP専用projectへ切り離す構成ではありません。[DOKPLOY.md](DOKPLOY.md) のContainersから両サービスのhealthを確認できます。

### 接続欄に入れるもの

| クライアントの設定 | 入力 |
| --- | --- |
| Transport | Streamable HTTP / HTTP |
| Server URL | `http://localhost:<MCP_PORT>/mcp`。privateの実portへ置換 |
| Header name | `Authorization` |
| Header value | `Bearer ` + private `MCP_API_TOKEN` の実値 |
| OAuth | このキットはOAuth issuerではない。静的Bearerヘッダー対応を確認 |

tokenは管理者のローカルエディターでprivate envの該当1項目だけを確認します。`CONTROL_API_TOKEN` はMCPから内部APIへの別の鍵なので、MCPクライアントへ渡しません。n8nのworker Credentialとも別です。

`/mcp` はWebの操作画面ではありません。ブラウザーで直接開いた際の401は、Bearerを送っていない場合の既定動作です。`GET /health` は認証不要の疎通確認ですが、200だけでGold取得や内部API認証が成功したことにはなりません。

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

### 読み取りだけで始める操作順

1. `platform_status` で到達状態とbusy状態を確認します。
2. `platform_capabilities` と `data_catalog` で表名・粒度・使える操作を確認します。
3. `data_quality` で取込・隔離件数を確認します。表が未作成なら、先に人が素材投入とmedallionを完了します。
4. `gold_context` を `{"dataset":"sales","limit":20}` 等で呼びます。応答の表名・snapshot ID・件数上限を読み、回答の根拠へ残します。
5. 不明な操作は `operator_help` へ `{"topic":"start"}` 等を渡して調べます。URLやファイルpathを渡すツールではありません。

MCPサーバーを再起動してもIceberg表は消えません。一方、`recent_jobs` の履歴は内部APIプロセスのメモリで、永続監査台帳ではありません。GUIや外部クライアント側でも受付時のjob IDと結果を管理します。

## 書き込みを使う場合

管理者がprivate envで `MCP_ENABLE_MUTATIONS=true` として再配置すると、次の3ツールが追加で登録されます。既定のサーバーには登録されません。各呼出しにも `confirm=true` が必要です。

| 名前 | 操作と追加条件 |
| --- | --- |
| `generate_sources` | CSV/JSON/画像・専用SQLへ合成素材を投入。有限batch/stream、26異常選択。SQLは `allow_sql_write`、意図的な壊れたファイルは `allow_file_faults` にも同意 |
| `run_pipeline` | `ingest` / `medallion` / `verify` の一つを非同期実行 |
| `change_cdc_demo` | 所有を確認した疑似CDC DBの固定6行へinsert/update/delete |

素材投入は後続処理を自動で起動しません。受け取ったjob IDをpollし、応答が不明なときに同じ書き込みを繰り返しません。一般の会社DBを変更するツールとして利用しません。AI利用者単位の認可・承認記録・永続キューは別途実装が必要です。

書き込みを有効にした環境でも、素材生成の終了、取り込み/medallionの終了は別ジョブです。`confirm=true` は呼出し側から渡すBooleanであり、サーバーが人の承認画面を表示する機能ではありません。人が結果を確認してから次の操作へ進める運用をクライアント側にも用意します。

## SDKでの接続例

公式Python SDK `mcp==2.3.0` をhash付きlockで固定しています。SDKのクライアントでも同じBearerヘッダーを使います。秘密値は環境変数から読みます。

独立venvを作り、client例はそのPythonで実行します。既存worker・Airflowの環境へSDKを追加しません。

```bash
uv venv --python 3.12 "$HOME/.local/share/lakehouse/mcp-client"
uv pip sync --python "$HOME/.local/share/lakehouse/mcp-client/bin/python" \
  --require-hashes infra/mcp/requirements.lock
```

次をprivateな `mcp-check.py` に保存します。`MCP_API_TOKEN` を環境へ設定していなければ、端末の非表示入力で値を受け取ります。コードへtokenを書き込みません。

```python
import asyncio
import getpass
import os

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client


async def main():
    token = os.environ.get("MCP_API_TOKEN") or getpass.getpass("MCP token: ")
    headers = {"Authorization": "Bearer " + token}
    timeout = httpx2.Timeout(30.0, read=300.0)
    async with httpx2.AsyncClient(
        headers=headers, timeout=timeout, trust_env=False
    ) as http:
        transport = streamable_http_client(
            os.environ.get("MCP_URL", "http://localhost:8503/mcp"),
            http_client=http,
        )
        async with Client(transport) as client:
            tools = await client.list_tools()
            print("Tools:", sorted(tool.name for tool in tools.tools))
            result = await client.call_tool("data_catalog", {})
            if result.is_error:
                raise RuntimeError("MCP tool returned an error.")
            print("Catalog read:", result.structured_content is not None)


asyncio.run(main())
```

既定では9個の読み取りtoolが見えます。`MCP_ENABLE_MUTATIONS=true` なら12個です。SDK v1の `ClientSession` / `streamablehttp_client(headers=...)` の例と混ぜません。v2はheaders・timeoutを `httpx2.AsyncClient` へ渡し、transportを `Client` で開きます。自作clientの既定5秒timeoutではGold取得が途中で切れるため、上の待ち時間を明示します。

| 症状 | 確認先 |
| --- | --- |
| HTTP401 / MCP認証エラー | URL、Bearerの半角空白、MCP用token、再配置後の設定 |
| Host/Origin拒否 | `MCP_ALLOWED_HOSTS` と実入口。保護を無効化しない |
| toolは見えるがBackendエラー | `demo-api` のhealthと `CONTROL_API_TOKEN` の一致 |
| `gold_context` だけ失敗 | Goldの有無、Trino、件数1～500、120秒の内部context取得 |
| mutation toolが見えない | 既定動作。明示opt-in後の再配置が必要 |
| 接続だけ成功しtool結果が失敗 | `is_error` を確認。HTTP接続成功と業務成功は別 |

この例はローカル参照です。社外のAIクライアントへ接続する前に、対象データの送信可否とクライアントの認証ヘッダー対応を確認してください。[AI-READINESS.md](AI-READINESS.md)、[POC-PORTAL.md](POC-PORTAL.md)、[API.md](API.md) も参照してください。

## 今回照合した一次資料と実装

- [公式SDK v2.3.0 Client transports](https://github.com/modelcontextprotocol/python-sdk/blob/v2.3.0/docs/client/transports.md)。`Client` / `httpx2` / timeout / Streamable HTTPの契約を確認しました。
- [公式SDK v2の移行説明](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/migration.md) は変更点の補助資料で、実際の採用版は `infra/mcp/requirements.lock` が正本です。
- `infra/mcp/server.py` の9読み取りtool、2 resource、opt-in時の3 mutation、BearerGateとHost保護、`tests/test_mcp_server.py` の公式SDK protocol試験へ照合しました。OAuthや任意SQLを同梱機能として説明しません。

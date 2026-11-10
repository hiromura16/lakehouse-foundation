# AIへ渡せるデータとLangChain連携

<!-- doc-purpose:start -->
> **役割:** Goldとsnapshotの根拠をJSON・LangChain Documentへ渡し、推論やRAGとの境界を確認する。
>
> **対象読者:** データ利用担当 / 開発・設計担当
>
> **読むタイミング:** AIへ渡す業務contextを取得するときや、モデル連携の追加要件を決めるとき。
>
> **前提となる資料:** [MES・ERPデータをBronzeからGoldへ整える](<MEDALLION.md>)
>
> **次に読む資料:** [AIクライアントからMCPで操作する](<MCP.md>) / [Cloudflareを使う場所と社内に残す場所](<CLOUDFLARE.md>) / [ソース・指標・API・計算基盤を拡張する](<CUSTOMIZATION.md>)
>
> **文書一覧:** [目的・読者から探す文書マニフェスト](<DOCUMENTATION-MANIFEST.md>)
<!-- doc-purpose:end -->

<!-- shell-usage:start -->
**コマンドのシェル:** 「Bash / Zsh共通」は同じ内容を使い、専用例は自分のシェルを選びます。[詳しい使い分け](SHELLS.md)。

Zshの対話端末では、コメント付きの例を貼る前に一度だけ次を実行します。Bashでは不要です。

**Zsh**

```zsh
setopt INTERACTIVE_COMMENTS
```
<!-- shell-usage:end -->

照合日: 2026-11-10。Trino **484**、langchain-core **1.7.6**、langchain-ollama **1.1.0**、LangSmith **0.14.17**、Trino Python client **0.340.0** の固定lockと同梱コードが対象です。

この構成では、AIが利用する事実と、その根拠を取り出す経路を用意します。モデルの推論、画像OCR、埋め込みの生成、ベクトル索引は別の段階です。モデルやクラウドのアカウントを用意しなくても、データの参照とLangChain Documentへの変換を検証できます。

| 操作 | 今の実装 | 必要な入口 |
| --- | --- | --- |
| 根拠をJSONで取得 | 実装済み。既定2つのGoldをsnapshot付きで参照 | Streamlit / FastAPI / CLI / MCP |
| LangChain Document化 | 実装済み。推論・embedding呼出しなし | 独立したAI venv |
| ローカルモデルの回答 | 明示指定したOllamaを呼ぶ例だけを同梱 | 別途導入・管理したOllamaとモデル |
| Qdrant近傍検索 | `vector` profileの合成数値デモを同梱 | [POC-TOOLS.md](POC-TOOLS.md) |
| 文書・画像のRAG | OCR、chunk、embedding、権限・削除同期は未実装 | 追加実装 |
| Cloudflare / 外部AI | 設計候補。アカウント・鍵・通信は未接続 | [CLOUDFLARE.md](CLOUDFLARE.md) |

## AIが使うもの

| 対象 | 目的 | この実装での扱い |
| --- | --- | --- |
| Goldの型付き業務値 | 生産、歩留まり、出荷、遅延の説明 | 固定の2データセットを読み取る |
| Iceberg snapshot ID | 回答がどの時点の表に基づくか追跡 | main参照のsnapshotを取得して `FOR VERSION AS OF` で同じ版を読む |
| 参照SQLのSHA-256 | export時checkoutの参照SQLを識別 | `exporter_reference_sql_sha256`。実snapshotの生成SQLの証明ではない |
| adapterのSHA-256 | context取得コードを識別 | `exporter_code_sha256`。exportに使用したコードを記録 |
| 元ファイルのハッシュとS3参照 | 原本への追跡、再処理 | Bronzeと原本保存で保持 |
| モデル名・処理版 | 推論の再現と比較 | 推論例の応答に選択モデルと参照を含める |

テーブルごとにsnapshotを固定します。複数テーブルを一度に固定するグローバルトランザクションを提供するものではありません。上限付きのデータを、全工場の完全な母集団と解釈しないよう `complete_dataset: false` を付けます。

snapshotは各表の `$refs` から `name='main'` を選びます。作成時刻が最も新しいsnapshotを選ぶ方法ではないため、rollback後も現在のmainが指す版を読みます。[Trino 484のIceberg文書](https://github.com/trinodb/trino/blob/484/docs/src/main/sphinx/connector/iceberg.md)

`exporter_reference_sql_sha256` は、export時のcheckoutにある `03_gold.sql` のハッシュです。過去のsnapshotを生成した時のSQLと一致する保証はありません。生成版は現時点では不明として扱い、将来はsnapshotと実行SQLのhash・commitを対応付けるrun監査台帳を追加します。`exporter_code_sha256` もデータの生成版を証明する値ではありません。

## 参照の流れ

```mermaid
flowchart LR
  GOLD[Goldの業務表] --> SNAP[mainのsnapshotを選択]
  SNAP --> READ[固定SQL / 上限付きSELECT]
  READ --> CONTEXT[根拠付きcontext JSON]
  CONTEXT --> DOC[LangChain Document]
  DOC --> LOCAL[任意のローカルOllama]
  LOCAL --> ANSWER[表名とsnapshotを引用した回答]
```

モデルへSQL実行器、workerの操作トークン、S3鍵、MES/ERPの接続資格情報は渡しません。初期の閉域Trinoは利用者認証を完成した環境ではないため、`lakehouse-ai-reader` というuser名を指定しただけで強い権限分離ができるとは扱いません。共有運用ではTrino認証・認可とGoldの参照権限を設定します。

## GUIから確認する

`demo` profileのStreamlitで「AIの準備」を開き、production、sales、allと件数上限を選びます。FastAPIの `/ai/context` が既定のGoldを読み、表とsnapshotを返します。画面に取得中と表示し、この要求だけ最大120秒待ちます。通常ジョブの受付・進捗確認は15秒です。context JSONは業務情報を含むので、取得したファイルの保存先を社内の管理対象にします。

1. 先に「素材の投入」で合成入力を用意し、「処理・検証」でmedallionを1回成功させます。既存の入力がある場合は生成を繰り返す必要はありません。
2. 「データ」でGoldの2表と件数を確認します。
3. 「AIの準備」で対象と **表ごとの最大行数** を選び、**根拠データを準備する** を押します。
4. 表名、snapshot ID、返った行数を確認し、**根拠JSONを保存する** からprivateな保存先へDownloadします。
5. 出力は表ごとに上限があるため、このサンプル行数を全社売上や全工場の合計として扱いません。

Dokploy配置でも、この操作はStreamlitから内部FastAPIへ依頼するだけです。任意SQLやモデルAPI keyを画面へ入力する欄はありません。APIを直接使う場合は [API.md](API.md) のBearer認証、AIクライアントは [MCP.md](MCP.md) の `gold_context` を使います。

Goldが未作成なら、素材投入とメダリオン処理を先に実行します。AIの取得ボタンはデータを生成したり変換したりしません。

## CLIでcontextを保存する

独立したuv **0.13.7** の仮想環境を使います。worker、Spark、Airflowの依存をこの環境へ混在させません。`requirements.lock` は既存のLangChain/Trino固定版と推移依存をhash付きで保持し、Python 3.12と3.14向けに共通解決しています。以下はリポジトリroot、hostにPython 3.12とuvを導入済みの場合です。企業proxy・CAの設定は [COMPANY-PROXY.md](COMPANY-PROXY.md) を先に確認します。

**Bash / Zsh共通**

```bash
export LAKEHOUSE_ENV_FILE="$HOME/.config/lakehouse-foundation/site.env"
uv venv --python 3.12 "$HOME/.local/share/lakehouse/ai-venv"
uv pip sync --python "$HOME/.local/share/lakehouse/ai-venv/bin/python" \
  --require-hashes examples/ai/requirements.lock

"$HOME/.local/share/lakehouse/ai-venv/bin/python" scripts/ai_context.py \
  --env-file "$LAKEHOUSE_ENV_FILE" \
  --dataset all --limit 100 \
  --output "$HOME/.config/lakehouse-foundation/ai-facts/context.json"
```

出力先はGit外の絶対パス、親は0700、新規ファイルは0600です。既存ファイルは上書きしません。`--limit` は各データセットの行数で、1〜500です。行はproductionなら工場・日、salesなら受注IDで固定順に選びます。ここでモデルや外部AI APIは呼びません。

```json
{
  "context_version": 1,
  "exporter_reference_sql_sha256": "...",
  "exporter_code_sha256": "...",
  "datasets": [
    {
      "table": "iceberg.gold.sales_fulfillment",
      "snapshot_id": "...",
      "complete_dataset": false,
      "rows": []
    }
  ]
}
```

上は構造を示す短縮例です。snapshot IDを固定値として設定する見本ではありません。

## LangChainで使う

同梱例は `langchain-core` のDocumentとprompt、`langchain-ollama` のChatOllamaを使います。LangChainを変換エンジンの代わりに使うものではなく、確定した事実をモデルへ渡す役割です。[LangChainの統合パッケージ](https://reference.langchain.com/python/integrations/overview)、[Ollama統合](https://reference.langchain.com/python/langchain-ollama)

**Bash / Zsh共通**

```bash
# Document化だけを確認。推論は起動しません。
"$HOME/.local/share/lakehouse/ai-venv/bin/python" examples/ai/context_chain.py \
  --context "$HOME/.config/lakehouse-foundation/ai-facts/context.json"
```

Documentには行のJSON、表名、snapshot ID、export時の参照SQLとadapterのハッシュ、上限付きサンプルの印を持たせます。生成時SQLの確定情報としては扱いません。Document IDは参照と内容のハッシュです。実機では34件のGold行をDocumentへ変換できることを確認しました。

`{"documents": N, "inference": false}` の件数が、context内の全datasetの `rows` 合計と一致することを確認します。Nは入力・上限で変わり、34という固定値を期待する試験ではありません。この段階ではOllamaを起動しなくても成功します。

ローカルOllamaへ導入済みのモデルがある場合だけ、明示的に推論します。モデルの取得・ライセンス・必要メモリは選択モデルごとに確認し、モデルの正確な版も運用記録へ残します。

**Bash / Zsh共通**

```bash
"$HOME/.local/share/lakehouse/ai-venv/bin/python" examples/ai/context_chain.py \
  --context "$HOME/.config/lakehouse-foundation/ai-facts/context.json" \
  --ollama-url http://localhost:11434 \
  --model '<導入済みのモデル名とタグ>' \
  --question '取得した範囲で、出荷が遅れた受注を根拠と一緒に説明して'
```

この例に外部AI APIキーは不要です。context本文は最大1MiB、promptへ組み込む事実は最大64,000文字です。文章内に指示に見える内容があってもデータとして扱うpromptを使い、実行ツールをモデルへ付与しません。回答の正確さは、引用元と業務ルールを照合して評価します。

Ollama自体のインストール・モデル取得はこのComposeには含まれません。`--model` の説明用文字列を実際の導入済みtagへ置き換えます。例のURLはlocalhostか具体的なprivate IPを検証し、公開モデルAPIや任意の外部DNSへ接続する一般的なprovider設定にはしていません。推論エラーでcontextを勝手に別providerへ転送しません。

### 結果を読み直す

回答の引用に `iceberg.gold.<table>@<snapshot_id>` が含まれるか、数値がcontextの該当行と一致するかを人が確認します。contextにない期間・工場・受注について断定する回答は採用しません。値が変わった場合は新しいファイル名へ再exportし、古いcontextの内容を上書きせず取得時刻とsnapshotを比較します。

| 症状 | 確認 |
| --- | --- |
| Gold表がない | 素材とmedallionの成功を確認 |
| GUIでcontext取得がtimeout | Trinoの状態・対象・上限。通常ジョブの受付とは待機が別 |
| CLIの接続拒否 | private envの `BIND_IP` / `TRINO_PORT` と実行端末からの到達性 |
| outputが既存・親権限エラー | 新しい外部絶対path、親0700、symlinkなしを使用 |
| Document化だけ失敗 | context_version=1、1MiB上限、対応lockを確認 |
| モデル接続だけ失敗 | Ollamaが別途動いているか、導入済みtag、許可されたlocal/private URL |

推論の呼び出しは `langsmith.tracing_context(enabled=False)` で囲み、呼び出し元の環境でLangSmith tracingを有効にしていても、この例から根拠データを外部tracingへ送らないようにします。[公式のtracing制御](https://reference.langchain.com/python/langsmith/run_helpers/tracing_context)

## 次に追加するもの

| 段階 | 追加する実装 | 確認すること |
| --- | --- | --- |
| 画像・文書 | OCR、画像モデル、chunk分割 | 原本ハッシュとページ・座標への参照、誤読率 |
| 検索 | Qdrant等の索引とembedding | モデル版、次元、原本削除時の索引削除、権限filter |
| 分析対話 | 許可された指標の検索、回答評価 | 正解例、引用、欠損時の回答、誤った因果推論 |
| 共有運用 | IdP、利用者別認可、監査 | 検索結果と原本の両方で権限を確認 |
| 外部AI | 送信対象、保持、ログ方針を決める | 会社の通信・情報管理要件、費用、処理地域 |

CloudflareのAI機能を使う選択肢は [CLOUDFLARE.md](CLOUDFLARE.md) に整理しています。現時点でモデル推論や外部サービス接続が検証済みであるとは記載しません。

## 今回照合した一次資料とコード

- [Trino 484のIceberg文書](https://github.com/trinodb/trino/blob/484/docs/src/main/sphinx/connector/iceberg.md) と `scripts/ai_context.py`。`$refs`、time travel、表ごとのsnapshot固定を確認しました。
- [langchain-core 1.7.6](https://pypi.org/project/langchain-core/1.7.6/)、[langchain-ollama 1.1.0](https://pypi.org/project/langchain-ollama/1.1.0/) の配布情報と `examples/ai/requirements.lock`。採用版と公開されている別版を区別しました。
- [LangSmith tracing_context](https://reference.langchain.com/python/langsmith/run_helpers/tracing_context) と `context_chain.py`。`enabled=False`、Document metadata、モデルへ実行toolを渡さない構成へ照合しました。参考文書が指す新しいAPIへ自動更新する手順ではありません。
- GUIのボタン・待機・Downloadは `infra/demo/app.py`、APIは `infra/demo/poc_api.py` と `api.py` を正本にしています。

## 初めてAIの準備をする人へ: 参照と推論を分ける

AIレディーは、モデルが自動で正しい判断をする意味ではありません。この基盤では、型のある表、業務指標の定義、原本の由来、表ごとのsnapshot、固定の取得APIを先に用意します。操作担当はブラウザーでcontextを保存し、分析/AI担当はそのJSONを受け取って意味と範囲を確認します。秘密のenvやworker tokenを渡す必要はありません。

| やりたいこと | 今使える経路 | 成功を確認するもの |
| --- | --- | --- |
| 業務値の意味を知る | データcatalog | 粒度、列、主キー、Goldの式 |
| 同じ版の事実を保存する | GUI/API/CLIのcontext export | 表名、snapshot_id、返却件数、上限 |
| LangChainへ渡す | 同梱Document adapter | metadataの表・snapshotと内容の対応 |
| AIクライアントから読む | MCPのgold_context等 | 認証、tool結果、構造化された応答 |
| 文書の意味検索や回答を評価する | 追加のmodel/index/評価計画 | 正解集合、権限、引用、品質、費用。現時点の自動完成ではない |

## 質問の種類から「答えられる範囲」を判断する

例えばsalesを20行だけ取得したcontextに対して「全受注の未出荷残はいくつか」と聞くと、抜粋の合計を全件合計として答える危険があります。この場合は「この20行の範囲では」と限定するか、全件の正式な集計を別の許可されたSQL/データセットとして用意します。モデルへ任意SQLを実行させて上限を回避する設計にはしません。

| 質問 | 必要な事実 | 現contextの限界 |
| --- | --- | --- |
| この工場日の歩留まりは | 対象のproduction行と指標定義 | 対象行が抜粋へ含まれることを確認 |
| 全工場の期間合計は | 同じ範囲の全件または専用集計 | 行数上限の抜粋だけでは母集団を保証しない |
| その値はいつの表か | tableとsnapshot_id | exporterのcreated_atだけではデータの業務鮮度は分からない |
| 不良が増えた原因は | 比較対象、設備/工程、独立した原因根拠 | 合計の変化だけで原因や因果関係を証明しない |
| 元の写真に何が写るか | 原本、OCR/画像model、評価 | 現context exporterは画像理解を行わない |

snapshotを参照できなくなった場合、別の版へ読み替えて同じ根拠だったように扱いません。参照版が失効/利用不能かを確認し、新しい取得として記録します。複数表はそれぞれmainを選ぶため、同時点の整合が必要な評価には別の実行監査・公開世代の仕組みが必要です。

## AI連携を拡張する実装ポイントと回帰

固定表・並び順・行数境界は [ai_context.py](../scripts/ai_context.py) のDATASETS/collect、Document化と任意推論は [context_chain.py](../examples/ai/context_chain.py)、公開範囲は [MCP server](../infra/mcp/server.py) にあります。

新しいGoldを公開するなら、①粒度/式/鮮度/母集団、②snapshotを固定する方法、③最大行数と列、④秘密/利用者権限、⑤モデルへ渡すmetadata、⑥根拠を引用できない時の応答、を先に決めます。任意表名やSQLを引数へ増やすより、必要な業務データセットを固定で追加します。

回帰ではmainのrollback、Gold未作成、空の結果、上限、異なる表のsnapshot、参照SQL hashの意味、外部tracing、秘密値非表示を確認します。回答精度を測る段階では、評価問題と正解・不明の基準、モデル/設定、参照snapshot、権限を持たない質問、回答遅延/費用を記録します。Document34件生成の過去記録を、そのモデルの正答率へ読み替えません。[評価計画](reports/EVALUATION-PLAN.md)・[変更手順](CUSTOMIZATION.md)

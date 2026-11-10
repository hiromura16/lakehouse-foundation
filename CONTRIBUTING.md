# 変更する人のための読みやすさと検証規約

<!-- doc-purpose:start -->
> **役割:** 読みやすいコード・コメント・CLI説明と、専用uv環境での品質検査規約を揃える。
>
> **対象読者:** 開発・設計担当
>
> **読むタイミング:** コードや文書を変更する前と、差分をレビューに出す前。
>
> **前提となる資料:** [ファイルの場所と変更のしかた](<docs/DIRECTORY-GUIDE.md>)
>
> **次に読む資料:** [レビュー指摘を改善へ取り込む](<docs/REVIEW-WORKFLOW.md>) / [図と出力スクリプトを変更する人へ](<docs/reports/EDITING-GUIDE.md>)
>
> **文書一覧:** [目的・読者から探す文書マニフェスト](<docs/DOCUMENTATION-MANIFEST.md>)
<!-- doc-purpose:end -->

初回構築は [START-HERE](docs/START-HERE.md)、レビューの取り込みは [REVIEW-WORKFLOW](docs/REVIEW-WORKFLOW.md)、図とexportの編集は [EDITING-GUIDE](docs/reports/EDITING-GUIDE.md) を参照します。実接続先・秘密値・企業CA・実データはGit外に保持します。

## コードを読む人へ必要な情報を残す

- moduleのdocstringで、目的・入力・出力・副作用・処理の入口を説明します。
- publicな関数・class・初期化関数に役割を示すdocstringを付けます。重要な引数・戻り値・例外は意味も記載します。
- CLIの全引数に `help` を付け、必要な前提、保存先、明示同意、上限、既定と対象外を説明します。`--help` の確認でサービスやDBを変更しません。
- コメントには、処理を直訳するだけでなく、順序・拒否・再実行・互換性・信頼境界をその形にした理由を書きます。
- 一つの関数が複数の工程を担い始めたら、意味のある工程名で分けます。小さく分けるだけの抽象化や、動作と食い違う説明は追加しません。
- 変数・引数には用途を示す名前を使います。座標などの慣用的な短い名前は単位と座標系を説明します。寸法や上限の固定値には意味のある定数名を付けます。
- 型注釈は呼び出し方の理解に役立つ箇所へ付けます。型・コメント・実装の意味を揃えます。

PythonはPEP8と79文字の規約、Ruff・pycodestyleを使います。SQLはCTE名とコメントで粒度・集計順・NULL/不正値・更新優先・表単位commitを明示します。Composeはprofile・依存・永続化・host公開範囲が人間に分かる区切りとコメントを維持します。shellは入口を薄く保ち、host変更のplan/applyと実行権限を見える形にします。

説明だけの変更では、フラグ名、既定値、validation、実行順序を混ぜて変更しません。必要な動作修正は理由・影響・検証を別に示します。パスや設定は変数で指定し、特定の配置先の値を例として埋め込みません。

## 専用uv環境で検査する

リポジトリルートから、通常ユーザーで実行します。

```bash
QUALITY_VENV="$HOME/.local/share/lakehouse-foundation/source-quality-venv"
uv venv --python 3.12 "$QUALITY_VENV"
uv pip sync --python "$QUALITY_VENV/bin/python" \
  --require-hashes requirements-dev.lock

"$QUALITY_VENV/bin/ruff" check .
"$QUALITY_VENV/bin/ruff" format --check .
"$QUALITY_VENV/bin/pycodestyle" --max-line-length=79 scripts tests infra
"$QUALITY_VENV/bin/python" -m unittest discover -s tests
"$QUALITY_VENV/bin/sqlfluff" lint --dialect trino medallion
"$QUALITY_VENV/bin/python" scripts/render_report_assets.py --check
"$QUALITY_VENV/bin/python" scripts/documentation.py --check
"$QUALITY_VENV/bin/python" scripts/check-source.py --history
git diff --check
```

CIの公開検査には会社の実秘密値はありません。公開前にはGit外の既知情報リストと秘密値照合も別途行います。文字列検査だけで画像・PDF・Notebookが公開可能と判断せず、実出力とmetadataも確認します。

図・説明を変更した場合は、[生成・出力・確認手順](docs/reports/EDITING-GUIDE.md) を使います。データ処理や版を変えた場合は、関連する互換性・機能・再実行試験を追加します。合成機能試験、会社での受入、性能・復元・モデル推論の確認は、対象と成功条件を分けて記録します。

## 初めて変更する場合

1. [文書マニフェスト](docs/DOCUMENTATION-MANIFEST.md) で対象機能の手順・設計・検証を揃えます。
2. 稼働環境と分けたbranch・uv環境を用意し、変更前に同じ操作を確認します。DBやvolumeの削除で再現条件を作りません。
3. 一つの利用者の操作を例に、変更前後の結果を書きます。読みやすさの改善は、入力・出力・副作用が変わらないことを確認します。
4. 影響するコード・SQL・Compose・文書・図・期待値を一緒に直し、差分と検査結果をPRへ示します。

MDを追加・改名する場合は [documentation-manifest.json](docs/documentation-manifest.json) へ目的・読者・使う場面・関連文書を登録し、`python3 scripts/documentation.py` で冒頭の役割欄と一覧を生成します。生成区間以外の説明は手で書きます。CAPTIONSは図の生成物なので、役割欄も含めて描画スクリプトから生成します。

## 上級者が変更前に確認する契約

| 変更領域 | 維持・更新するもの |
| --- | --- |
| データ変換 | 行の粒度、最新版優先、隔離理由、指標の母数、表単位commit、再実行 |
| API/GUI/MCP | 固定jobの範囲、token、確認入力、上限、timeout後の状態、互換payload |
| profile/Compose | 依存、host公開、volume名、one-shot、proxy/CA、初期鍵の継続 |
| バージョン | image digest、Python lock、Spark/Scala/Java/Iceberg、Airflow constraints |
| 図・報告 | 根拠commit、既存試験と現地受入の区別、未評価、再生成とリンク |

失敗を広い例外処理で隠したり、検査をskipへ変えて通したりせず、再現する境界をテストします。必要な依存が本当に未導入の場合と、導入済み環境の破損は区別します。詳細な拡張例は [CUSTOMIZATION](docs/CUSTOMIZATION.md)、レビューの記録方法は [REVIEW-WORKFLOW](docs/REVIEW-WORKFLOW.md) を参照してください。

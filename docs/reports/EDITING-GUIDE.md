# 図と出力スクリプトを変更する人へ

<!-- doc-purpose:start -->
> **役割:** figures.json、描画・exportコード、生成物の責務と変更後の検証を理解する。
>
> **対象読者:** 開発・設計担当
>
> **読むタイミング:** 図の意味・レイアウト・出力形式を変更し、生成差分を確認するとき。
>
> **前提となる資料:** [変更する人のための読みやすさと検証規約](<../../CONTRIBUTING.md>) / [PoC報告書・発表に使う図版と雛形](<README.md>)
>
> **次に読む資料:** [報告書用の実装構成一覧](<IMPLEMENTATION-INVENTORY.md>) / [検証記録](<../VALIDATION.md>)
>
> **文書一覧:** [目的・読者から探す文書マニフェスト](<../DOCUMENTATION-MANIFEST.md>)
<!-- doc-purpose:end -->

<!-- shell-usage:start -->
**コマンドのシェル:** 「Bash / Zsh共通」は同じ内容を使い、専用例は自分のシェルを選びます。[詳しい使い分け](../SHELLS.md)。

Zshの対話端末では、コメント付きの例を貼る前に一度だけ次を実行します。Bashでは不要です。

**Zsh**

```zsh
setopt INTERACTIVE_COMMENTS
```
<!-- shell-usage:end -->

図の編集元、処理の入口、変更後に確かめることを説明します。[素材集](README.md) の通常利用では、この文書の編集作業は不要です。

## ファイルの責任を分ける

| ファイル | 入力と出力 | 変更する場面 |
| --- | --- | --- |
| [figures.json](figures.json) | 図の意味・座標・線・caption・根拠・Mermaid | 図の内容・配置・説明を変更 |
| [render_report_assets.py](../../scripts/render_report_assets.py) | 図仕様→SVG/MMD/HTML/CSV/台帳 | 全図共通の描画・文字組・リンクを変更 |
| [report_export.py](../../scripts/report_export.py) | 生成済み公開SVG→Git外のPNG/PDF/PPTX/ZIP | 文書形式・紙面・metadata・梱包を変更 |
| [requirements-export.lock](requirements-export.lock) | 追加描画依存を固定 | export依存の組合せを検証して更新 |
| [出力境界テスト](../../tests/test_report_exports.py) | 外部参照・不正出力先の拒否を検証 | exportの入力/出力境界を変更 |

`render_report_assets.py` が設定ファイル・DB・実GUIへ接続することはありません。`report_export.py` はimportされる補助モジュールで、独立したCLIを持ちません。呼び出す入口は `render_report_assets.py --export-dir` です。

## figures.jsonの項目を読む

| 項目 | 意味・注意 |
| --- | --- |
| `reviewed_on` / `reviewed_source_commit` | 内容を照合した日と公開commit。図の再生成日・全機能の再試験日とは別 |
| `id` | 例 `03-medallion`。同名のSVG/MMDとHTML anchorへ使う一意ID |
| `title` / `subtitle` | 1枚を単独で貼ったときの主題と対象範囲 |
| `audience` / `caption` | 読み手・用途と、報告書へ添える説明 |
| `sources` | リポジトリルートからの公開参照。実在するコード/文書を指定 |
| `width` / `height` | SVG viewBoxの座標幅・高さ。標準1920×1080、詳細図01は2400×1500 |
| `panels` | 箱の位置・大きさ・見出し・本文・意味付け |
| `edges` | source/targetのpanel key、データ/制御/計画、短いラベルと経由点 |
| `notes` | 合成条件、表単位commit、未実施など、図と一緒に残す制約 |
| `mermaid` | 同じ意味の編集用略図。SVGとは別の座標系なので内容を同時に直す |

panelの `x` / `y` は左上、`width` / `height` はviewBox単位です。本文は `lines` の配列で、意味のまとまりごとに区切ります。長すぎる行は日本語幅を考慮して折り返し、箱からはみ出す場合はエラーにします。文字を小さくし続けて押し込まず、本文を短くする・箱を広げる・別図に分ける順に検討します。

`tone` はblue/teal/purple/bronze/silver/gold/red/gray、`tag` は短い補足です。`logo` は取得元を記録した7種だけを受け付けます。未知の画像をdata URLで埋め込む形式にはしません。会社の実画面・企業CA・私有図をロゴとして紛れ込ませないためです。

## 線は実際の経路を描く

- `data`：原本・行・イベント・ログなどが移動する実線。
- `control`：処理依頼・設定・認証などの破線。
- `optional`：明示的に選ぶ連携、今後の計画・受入などの点線。

`source` / `target` は同図のpanelの `key` を指定します。隣接した箱は自動接続します。遠い箱の接続には `points: [[x, y], ...]` を指定し、箱や別のラベルを横断しない経路を作ります。矢印を見た目だけで接続せず、コードの呼び出し元・保存元を確認します。例として、Spark event logを書くのはJupyter内driver、同梱n8n/Airflowの依頼先はFastAPIではなくworkerです。

## 編集して確認する順序

1. 伝える相手、図で答える問い、根拠、確認していないことを決めます。
2. 公開実装を読み、figures.jsonの内容・caption・notes・sources・Mermaidを同じ変更として直します。
3. リポジトリルートで生成し、SVG/HTML/台帳の差分を確認します。

**Bash / Zsh共通**

```bash
python3 scripts/render_report_assets.py
python3 scripts/render_report_assets.py --check
python3 scripts/render_report_assets.py --help
```

4. SVGを拡大して読み、文字、矢印、余白、色に依存しない意味付けを確認します。gallery.htmlはPC幅と小さい画面でも開きます。
5. 文書形式を変えた場合は専用uv環境から新しいGit外ディレクトリへexportします。[依存導入と出力手順](README.md#pngpdfpowerpointを一括で作る)
6. PDFの日本語検索・font埋込・ページ数、PPTXの図とcaption/notes、PNGの文字、ZIPの相対名と内容を確認します。PPTXの構造確認とOfficeでの表示確認は区別します。
7. 自動品質検査と機密検査を行い、公開可能な図仕様・コード・SVG・説明文だけをコミットします。

`--check` は書込みません。通常生成と `--export-dir` は、まずrepo内の標準テキスト生成物を更新します。外部exportが途中失敗した場合は部分出力を確認し、新しい出力ディレクトリで再試行します。失敗先に実データを置かず、既存報告書の上書き先として再利用しません。

## スクリプトを読む順序

`load_spec()` → `generated_assets()` → `main()` の順で、入力検査・組立・保存/差分検査を確認します。描画の細部は `render_panel()` / `route_points()` / `render_edge()` / `render_svg()` へ進みます。

exportは `external_destination()` と `validate_report_svg()` で先に入力を確認し、個別形式→PDF集→PPTX/一覧画像→文書→hash台帳→ZIP→権限の順で進みます。新しい寸法や形式を追加する場合は、単位を名前・コメントへ書きます。PDFはpoint、SVG/PNGはpixel、PPTXの配置はinchです。

コメントは処理の翻訳より「なぜその順序・境界・既定値か」を残します。公開入口のdocstringと `--help` には入力・出力・書込み・依存・失敗時の扱いを書きます。会社実値を使った例や、未評価を完了に見せる説明は追加しません。

## 編集ミスを切り分ける

| 症状 | 先に確認すること |
| --- | --- |
| JSONを読めない | UTF-8、引用符、末尾カンマ、配列/辞書の対応 |
| 必須項目・日付のエラー | 指摘されたfigure/panelのkeyと値、YYYY-MM-DDの実在日 |
| 文字領域のエラー | 本文を意味ごとに短くし、箱と余白を広げる |
| `--check` が差分を報告 | 正本を保存し通常生成、生成物のdiffをレビュー |
| export依存が見つからない | report専用venvのPythonで実行し、lock同期と `uv pip check` |
| 既存出力先を拒否 | 以前の出力を保持し、新しいGit外絶対パスを選ぶ |
| 日本語fontがない | Noto CJKとfontconfigを準備し、PDF内の日本語も確認 |

CAPTIONSは生成物です。説明はfigures.json/rendererへ反映し、文書冒頭の役割は [文書registry](../documentation-manifest.json) から生成します。新しいMDの追加時は文書生成と図生成の両 `--check` を通します。

文字コードの修正やcache化など、図の意味を変えない変更では、既存14SVGと画像出力の同一性も確認します。説明を変える変更ではbyte一致を成功条件にせず、根拠・caption・Mermaid・SVGが同じ内容を伝えることを確認します。

# ファイルの場所と変更のしかた

<!-- doc-purpose:start -->
> **役割:** 目的に対応するファイル、実行経路、公開コードとprivate状態の置き場を探す。
>
> **対象読者:** 開発・設計担当 / 基盤構築担当
>
> **読むタイミング:** 実装を読み始めるときや、変更対象と関連する確認箇所を絞るとき。
>
> **前提となる資料:** [Lakehouse Foundation アーキテクチャ](<ARCHITECTURE.md>)
>
> **次に読む資料:** [変更する人のための読みやすさと検証規約](<../CONTRIBUTING.md>) / [ソース・指標・API・計算基盤を拡張する](<CUSTOMIZATION.md>)
>
> **文書一覧:** [目的・読者から探す文書マニフェスト](<DOCUMENTATION-MANIFEST.md>)
<!-- doc-purpose:end -->

確認日: 2026-11-10（JST）。新規導入は [START-HERE.md](START-HERE.md)、採用版の根拠は [REFERENCES.md](REFERENCES.md) を参照します。公開するコード、配置先の設定、保存データを分離しています。リポジトリへ追加する前に、導入先のアドレス・個人のパス・認証情報がコードへ紛れていないことを確認します。

## ディレクトリの役割

| 場所 | 内容 | 変更する場面 |
| --- | --- | --- |
| `infra/lakehouse/` | S3、カタログDB、カタログ、Trino、worker、n8n、監視のComposeと固定依存 | coreの資源、イメージ、内部設定を変更するとき |
| `infra/compute/` | 常駐Spark Master/Worker/History、JAR/checksum、独立スモーク | Sparkを更新する、処理エンジンを拡張するとき |
| `infra/orchestration/` | Airflow、専用DB、constraints取得、DAG | 依存タスクやスケジュールを増やすとき |
| `infra/automation/` | n8nへインポートするワークフロー | トリガー、通知、外部連携の流れを変更するとき |
| `infra/monitoring/` | Prometheusのscrape、Grafana datasource、初回dashboard | 監視対象や初期の可視化例を変更するとき |
| `infra/demo/` | 素材投入と設定のStreamlit/FastAPI | 入力UIや疑似ソースを増やすとき |
| `infra/cdc/` | CDCの接続・イベント処理・再開の構成 | DB変更の連続取り込みを設定するとき |
| `infra/poc/` | Superset・Jupyter・Qdrant・Marquezと初期化例 | 個別分析ツールを追加・変更するとき |
| `infra/mcp/` | 専用Bearer認証と固定MCP tool/resource | AIクライアントの公開範囲を変更するとき |
| `infra/proxy/` | daemon用テンプレート、build/runtime CA、0バイトCA placeholder | 会社の通信設定を準備するとき。実CAは外部へ置く |
| `infra/uv/` | 固定版uv imageとdigest | Python環境の構築ツールを更新するとき |
| `infra/source/` | SQL Server 6表の公開mapping例 | コピーをprivate化して実表・列を設定するとき |
| `examples/ai/` | LangChain Document化、任意ローカルLLMの例とlock | AIの根拠データを受け渡すとき |
| `samples/` | 合成入力の作り方 | 実データを持ち込まず最初の素材を作るとき |
| `infra/dokploy/` | ホスト側のDokploy導入と設定検査 | 管理基盤を新規に設けるとき |
| `medallion/` | ソース契約、Silver、隔離、Gold、期待値チェック | 業務ルールや品質条件を変更するとき |
| `scripts/` | 設定生成、起動制御、投入、抽出、公開検査 | 操作やホスト設定の入口を変更するとき |
| `tests/` | 入出力境界、誤接続の拒否、再実行、認証等の試験 | 実装を変更し、期待する境界を検証するとき |
| `.github/workflows/` | 秘密値を使わない公開CI | ソース品質の検査を変更するとき |
| `docs/` | 設計、操作、検証、図版 | 仕様・操作・検証結果が変わったとき |
| `docs/reports/` | 14図のSVG/Mermaid、台帳、報告/評価の雛形、独立export lock | 説明図・caption・受入記録を更新するとき |

## 主なコマンド

| コマンド | 役割 | 実データやサービスへの影響 |
| --- | --- | --- |
| `scripts/configure.py` | Git外にprivate envを新規生成 | 既存設定を上書きしない |
| `scripts/lakehouse.sh ... plan` | Composeを結合して構文・補間を確認 | コンテナを起動しない |
| `scripts/lakehouse.sh ... up` | build、起動、初期化 | 選択した環境を更新する |
| `scripts/lakehouse.sh ... ingest` | ready付き素材を取り込む | 原本とBronzeを更新する |
| `scripts/lakehouse.sh ... medallion` | 取り込み後にSilver・隔離・Goldを再構築 | 派生表をテーブルごとに置き換える |
| `scripts/lakehouse.sh ... verify` | 一時namespaceで期待値を照合 | 専用の検証領域だけを作成・後片付けする |
| `scripts/lakehouse.sh ... spark-check` | SparkとTrinoの相互参照 | 専用のcompatibility表を置き換える |
| `scripts/lakehouse.sh ... down` | 通常の停止 | 名前付きvolumeを保持する |
| `scripts/generate-demo.py` | 合成素材だけを投入 | 取り込み・変換を起動しない |
| `scripts/extract-sqlserver.py` | 許可した列をSELECTしlandingへ出す | ソースDBへの書き込みを行わない |
| `scripts/mark-ready.py` | コピー済み素材の完了ハッシュを作成 | 対象ファイルの隣へmarkerを作る |
| `scripts/grafana_setup.py` | 初期dashboard作成・private export | 既存dashboardを上書きしない |
| `scripts/check-source.py` | 公開ファイルとGit履歴の検査 | ローカル読み取りのみ |
| `scripts/documentation.py` | 全MDの目的欄とマニフェストを生成 | 公開MDを更新。`--check` は読取りだけ |
| `scripts/shell_examples.py` | 全MDのコマンド例をBash/Zshで検査 | 構文だけを読み、sudo・接続・Deployを実行しない |

引数の正本は各スクリプトの `--help` です。ホスト設定のSamba、Docker proxy、Dokployは、アプリ起動とは別に管理します。ルート権限が必要な作業をアプリのGUIへ隠して実行しません。

文書の入口は [全MDマニフェスト](DOCUMENTATION-MANIFEST.md) です。[LEARNING-PATH](LEARNING-PATH.md) は習熟度別の読む順、[HOST-SETUP](HOST-SETUP.md) は空のVMの準備、[GLOSSARY](GLOSSARY.md) は用語、[CUSTOMIZATION](CUSTOMIZATION.md) は入力契約・指標・API・計算基盤の拡張を扱います。manifestのJSONは文書の登録正本で、site.envのような環境設定ではありません。

## 「何を変えるか」から、最初に開くファイルを選ぶ

同じ機能でも、画面、API、業務ルール、保存、配置は別のファイルが受け持ちます。症状を見て一番上の画面だけを直すのではなく、その値を決める正本まで辿ります。

| 相談・変更 | 最初に読む実装 | 一緒に読むもの |
| --- | --- | --- |
| 設定項目や初期値 | [configure.py](../scripts/configure.py)、[settings.py](../scripts/settings.py) | GUI SettingsStore、CONFIGURATION、company compatibility tests |
| CLI起動/検証の入口 | [control.py](../scripts/control.py) | 9Compose、private設定の読み方 |
| 取込・原本・SQL実行 | [admin.py](../scripts/admin.py) | sources.json、Silver/隔離/Gold SQL、worker protocol tests |
| GUIボタンや非同期進捗 | [app.py](../infra/demo/app.py)、[api.py](../infra/demo/api.py) | client、job_manager、API/UI tests |
| 異常素材を増やす | [source_scenarios.py](../scripts/source_scenarios.py) | 生成器、GUI catalog、独立した期待値 |
| CDC再開や追加mapping | [consumer.py](../infra/cdc/consumer.py)、[state.py](../infra/cdc/state.py) | connector/config、CDC state/registration tests |
| Notebookのremote接続 | [lakehouse_session.py](../infra/poc/lakehouse_session.py) | Spark cluster script、compute/notebookの同じ依存版 |
| AIクライアントへ公開する操作 | [server.py](../infra/mcp/server.py) | 固定APIのcapabilities、確認同意、MCP tests |
| 報告書の箱・矢印・caption | [render_report_assets.py](../scripts/render_report_assets.py) | reports/figures.json、生成物の一致検査 |
| PDF/PNG/PPTXの出力方法 | [report_export.py](../scripts/report_export.py) | 独立export lock、日本語font、export tests |

### CLIから処理本体までを追う例

`lakehouse.sh medallion` はshell内で業務SQLを実装していません。shell wrapper→control.py→同じCompose projectのworkerへexec→workerの固定 `/run` →取込と版管理SQL、の順に辿れます。n8n/Airflow/APIの入口も同じworkerの固定actionへ依頼します。HTTP 200だけを見る箇所、SQLの完了まで追跡する箇所、表置換の境界を分けて読んでください。

### 最初の変更を小さくレビューする

1. 文書や `--help` の修正なら、処理・flag・既定値を変えず、説明と入力例が実装に一致するか確認します。
2. 入力制約の変更なら、許すケースだけでなく拒否するケース・既存の秘密値非表示もテストします。
3. 保存や再開の変更なら、成功時だけでなく途中失敗・再実行・既存byte保持を確かめます。
4. GUI追加なら、表示だけでなくAPI側の型/範囲/認証/同意も揃えます。
5. 生成物を変えたら、生成元とSVG/台帳/HTMLの差分を同じ変更へ含めます。binaryの実exportはGit外で確認します。

担当外の画像tag、SQL書式、認証設定を同じ修正でまとめて更新すると、何が効いたか切り分けにくくなります。関係するコード・テスト・説明・検証結果を一つの単位としてレビューし、別の改善は別の差分へ分けます。

## 変更例: Goldの指標を追加する

1. 指標の粒度と式を決めます。受注別か工場・日別かを最初に明示します。
2. `medallion/03_gold.sql` に計算を追加し、分納・複数実績による行増幅を確認します。
3. 独立したチェックSQLと期待値を追加します。実装式をそのままテストへ複写しないようにします。
4. `docs/MEDALLION.md` に業務上の意味、NULLや遅延の扱いを追記します。
5. Python/SQL/Composeの検査と合成データの検証を実行します。
6. 検証環境へ配置し、実際の結果を確認してから業務環境へ進めます。

## 変更例: コンテナを更新する

Composeのタグだけでなく、digest、対応する依存lock、`COMPATIBILITY.md`、実機結果を同じ変更として扱います。Sparkのマイナー版、Scala、Iceberg runtime、AWS bundleを別々に更新しません。Airflowも本体・Python・Provider・constraintsを組で変更します。

## 公開しないもの

CLIの保守・追加機能では9ファイルを同じ順で結合します。Dokployの初回core-only既定Commandはcore1ファイルだけでも動きますが、標準ガイドは初回から9ファイルを指定します。標準CLIは自動で結合します。[9ファイルのコマンド](DOKPLOY.md) を部分的に削らないでください。

実際の `site.env`、企業CA、管理APIキー、実データ、volume、バックアップ、GUI exportの未レビュー原本を公開しません。これらはGit外の専用ディレクトリに保存します。過去の非公開環境のGit履歴を、この公開リポジトリへ取り込みません。

## 実装を読みやすく保つ

公開module/関数/class/初期化のdocstringとCLI引数helpを揃えています。[CONTRIBUTING](../CONTRIBUTING.md) にコメントの目的・命名・単位・uv検査、[レビュー手順](REVIEW-WORKFLOW.md) に並行指摘の取り込み方を記録しています。

# ファイルの場所と変更のしかた

公開するコード、配置先の設定、保存データを分離しています。リポジトリへ追加する前に、導入先のアドレス・個人のパス・認証情報がコードへ紛れていないことを確認します。

## ディレクトリの役割

| 場所 | 内容 | 変更する場面 |
| --- | --- | --- |
| `infra/lakehouse/` | S3、カタログDB、カタログ、Trino、worker、n8n、監視のComposeと固定依存 | coreの資源、イメージ、内部設定を変更するとき |
| `infra/compute/` | SparkとIceberg JARの組、checksum、相互参照スモーク | Sparkを更新する、処理エンジンを拡張するとき |
| `infra/orchestration/` | Airflow、専用DB、constraints取得、DAG | 依存タスクやスケジュールを増やすとき |
| `infra/automation/` | n8nへインポートするワークフロー | トリガー、通知、外部連携の流れを変更するとき |
| `infra/monitoring/` | Prometheusのscrape、Grafana datasource、初回dashboard | 監視対象や初期の可視化例を変更するとき |
| `infra/demo/` | 素材投入と設定のStreamlit/FastAPI | 入力UIや疑似ソースを増やすとき |
| `infra/cdc/` | CDCの接続・イベント処理・再開の構成 | DB変更の連続取り込みを設定するとき |
| `infra/dokploy/` | ホスト側のDokploy導入と設定検査 | 管理基盤を新規に設けるとき |
| `medallion/` | ソース契約、Silver、隔離、Gold、期待値チェック | 業務ルールや品質条件を変更するとき |
| `scripts/` | 設定生成、起動制御、投入、抽出、公開検査 | 操作やホスト設定の入口を変更するとき |
| `tests/` | 入出力境界、誤接続の拒否、再実行、認証等の試験 | 実装を変更し、期待する境界を検証するとき |
| `.github/workflows/` | 秘密値を使わない公開CI | ソース品質の検査を変更するとき |
| `docs/` | 設計、操作、検証、図版 | 仕様・操作・検証結果が変わったとき |

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

引数の正本は各スクリプトの `--help` です。ホスト設定のSamba、Docker proxy、Dokployは、アプリ起動とは別に管理します。ルート権限が必要な作業をアプリのGUIへ隠して実行しません。

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

実際の `site.env`、企業CA、管理APIキー、実データ、volume、バックアップ、GUI exportの未レビュー原本を公開しません。これらはGit外の専用ディレクトリに保存します。過去の非公開環境のGit履歴を、この公開リポジトリへ取り込みません。

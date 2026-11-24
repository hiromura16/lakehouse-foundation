# 現行アーキテクチャの棚卸し

<!-- doc-purpose:start -->
> **役割:** オンプレミス版と Cloudflare 活用版の構成、データ経路、責務と検証範囲を区別して把握する。
>
> **対象読者:** 開発・設計担当
>
> **読むタイミング:** 段階的な改善の着手前に、現行構成と根拠をそろえるとき。
>
> **前提となる資料:** なし。必要な用語は本文で確認します。
>
> **次に読む資料:** [CDC Consumer の責務と置換条件](<CDC-CONSUMER-ASSESSMENT.md>) / [業務境界とデータ契約のたたき台](<DOMAIN-BOUNDARIES-DRAFT.md>) / [段階的リファクタリングの実行計画](<REFACTORING-ROADMAP.md>)
>
> **文書一覧:** [目的・読者から探す文書マニフェスト](<../DOCUMENTATION-MANIFEST.md>)
<!-- doc-purpose:end -->

## 調査境界と読み方

この文書は対象コミットの Git 管理下にある構成・実装・試験を読んだ結果であり、配備先の稼働状況ではない。オンプレミスの比較基準は `main` (`85150062f7ec97242fc2ab39849753a5dcb9d9ce`)、Cloudflare 活用の実験は `codex/vm-vision-poc` (`287aa0d35e4ca536bb2886fef26944b11069600f`)。Vision は Cloudflare 管理面と任意の配送経路を持つが、SQL Server CDC や全ストレージが移設済みという意味ではない。

A: 中核維持、B: 標準製品への置換を評価、C: 宣言的定義へ寄せる、D: PoC 補助として分離維持、E: 統合・簡素化、F: 無依存・代替を証明できた場合だけ廃止。分類は責務ごとであり、サービス全体を削除する指示ではない。

## データと依存の実態

| 責務・分類 | 現行の入出力と依存 | 証拠と残る運用責務 |
|---|---|---|
| SQL Server 取り込み、A/C | SQL Server の CDC capture → Kafka Connect 上の Debezium → テーブル別 Kafka topic。固定デモと汎用設定がある。登録は別の一回実行サービス | `infra/cdc/{compose.yaml,connector.py,fixed_source.py}`、`infra/sqlserver/`。登録、既存 capture と権限の整合、接続拒否時の診断、ソース・Kafka 側の保持範囲を維持 |
| CDC 適用、A/B/C に分解 | Kafka → RawCDC Iceberg append → SQLite 現在値 → CDC Bronze と派生表 → ローカル完了位置 → Kafka 同期 commit | `infra/cdc/{consumer.py,consumer_runtime.py,state.py,cdc_replay.py,runtime_control.py}`。複数表・SQLite・Kafka の境界は単一トランザクションではない。詳細は [Consumer 評価](CDC-CONSUMER-ASSESSMENT.md) |
| ファイル・SQL バッチ、A/C | ファイル取り込み、SQL 抽出・バッチは CDC とは別の入り口。公開サンプル契約と運用入出力から判定し、実データが接続済みとは見なさない | `scripts/{admin.py,mark-ready.py,extract-sqlserver.py,sql_batch_worker.py}`、`infra/source/sqlserver-tables.example.json`、`medallion/sources.json`。到着完了・再実行単位の明示を維持 |
| 保存・カタログ・クエリ、A/C | PostgreSQL カタログ DB、オブジェクト保存、Lakekeeper の REST カタログ、Trino、処理 worker。オプションの Spark lab は検証用 | `infra/lakehouse/compose.yaml`、`infra/compute/compose.yaml`、`medallion/0*_*.sql`。Catalog と warehouse の対応した復元、認証、テーブル更新互換性が必要 |
| 変換・提供、A/C | 標準 SQL を基準に Bronze → Silver、隔離 → Gold。PoC の Superset は読取器、格納や CDC の実行主体ではない | `medallion/{01_silver,02_quarantine,03_gold}.sql`、`medallion/checks/`、`infra/poc/compose.yaml`。鮮度、粒度、品質、アクセスの検査を別々に行う |
| 運用・監視、C/E | CLI 起動・設定・検査、Prometheus/Grafana/Loki/Vector。n8n と Airflow は構成ファイルがあり、同じ中核ジョブが両方で運用中とは言えない | `scripts/{control.py,settings.py,grafana_setup.py}`、`infra/{lakehouse,orchestration}/compose.yaml`。優先する標準ツールは実ジョブ・利用者・障害時責任を照合してから選ぶ |
| 管理とデモ、D/E | main の demo API/GUI、CDC UI、確認用ラボは維持。Vision は React/BFF と管理 runner を追加。管理 UI の便利さと中核の依存は別 | `infra/demo/compose.yaml`、Vision の `frontend/`、`infra/demo/portal_gateway.py`、`infra/management/{api,planning,journal,runner}.py`。権限、排他、監査、管理要求の永続状態を外さない |
| 追加の実験、D/F候補のみ | MCP、OpenObserve、Sail、ノートブック等は別 Compose または PoC 領域。見た目の重複は未使用の証拠ではない | `infra/{mcp,openobserve,sail,poc}/compose.yaml`。公開 CLI の `scripts/lakehouse.sh` も参照一覧と外部利用確認までは現状維持 |
| Vision 管理面、A/C/D | Workers/Access と D1 に管理契約・revision・監査を置く。業務データや物理接続秘密の正本ではない。任意の relay は選択列を Basin 側 HTTP に配送し、受理後に Kafka を同期 commit | Vision の `cloudflare/control-plane/{cloudflare.config.ts,migrations/0001_control.sql,src/}` と `infra/cloudflare/basin_relay.py`。HTTP 受理と後段 Iceberg 完了は別。R2/Basin/D1 の費用、制限、互換性は未検証で、公式仕様と隔離実測が必要 |

## 既知の状態と証明の限界

- 分けて守る状態: SQL Server capture/LSN、Connect の offset と schema history、Kafka の topic/partition/consumer group、Consumer の SQLite と完了・保留状態、Iceberg のカタログと warehouse、Vision 管理面の revision/監査、認証設定と秘密の参照方式。実際の保持期間・復旧点・復旧時間目標は未確認。
- main の依存検査（PR #34）は明示サービス選択時の Compose グラフを証明する。`--profile cdc up` 全体では `cdc-ui` が対象になり得る。UI 停止中の実データ経路、障害復旧、稼働保証ではない。main #33 と Vision #36 の Grafana 出力整理はすでに反映され、再度計画に入れない。
- Vision の `.github/workflows/cdc-compat.yml` は対象 PR の使い捨て SQL Server/CDC 互換試験であり、main の実経路や新しい Sink を保証しない。main の `.github/workflows/sqlserver-cdc.yml` は別スコープ。既存の別途イメージ供給対策・GUI 作業にこの文書は変更を加えない。
- 現行の登録・変換・管理を一元的に支配する新フレームワークは追加しない。追跡した範囲は上表。実配置、全利用者・手順、外部依存は棚卸し継続とする。

次の文書: [ドメイン境界](DOMAIN-BOUNDARIES-DRAFT.md)／[実行順と判断ゲート](REFACTORING-ROADMAP.md)。

# ソース・指標・API・計算基盤を拡張する

<!-- doc-purpose:start -->
> **役割:** Gold指標・入力契約・API・計算環境の変更を、実装箇所と回帰条件へ落とし込む。
>
> **対象読者:** 開発・設計担当
>
> **読むタイミング:** 導入済みPoCを専用検証環境で拡張し、移行・再実行・受入を計画するとき。
>
> **前提となる資料:** [Lakehouse Foundation アーキテクチャ](<ARCHITECTURE.md>) / [MES・ERPデータをBronzeからGoldへ整える](<MEDALLION.md>) / [変更する人のための読みやすさと検証規約](<../CONTRIBUTING.md>)
>
> **次に読む資料:** [ファイルの場所と変更のしかた](<DIRECTORY-GUIDE.md>) / [PoCの評価計画と受入条件](<reports/EVALUATION-PLAN.md>)
>
> **文書一覧:** [目的・読者から探す文書マニフェスト](<DOCUMENTATION-MANIFEST.md>)
<!-- doc-purpose:end -->

このガイドは、導入済みPoCを変更する開発・設計担当向けです。初回構築は [START-HERE](START-HERE.md)、読む順番は [LEARNING-PATH](LEARNING-PATH.md) を使います。ここで示す拡張手順は変更計画であり、未実装機能を有効化するスイッチではありません。

## 変更は専用の検証環境で始める

1. 変更目的、入力の粒度、期待する出力、既存利用者への影響を一文で定義します。
2. 確認したcommitから作業branchを作り、会社データと別のprivate設定・project・volume・landingを用意します。
3. 変更前の固定fixtureとGUI/APIの往復を確認し、基準結果を残します。
4. 変更を最小の契約/SQL/API/依存へ適用し、関連する図と文書も更新します。
5. 失敗・再実行・旧データの扱いを試し、会社側の受入は別に実施します。

既存DB・鍵・volumeを再生成して移行の問題を隠しません。停止点、backup、戻すcommit、戻せるデータ版を先に決めます。[運用](OPERATIONS.md)

## 1. Goldの業務指標を追加する

例として「不良率」を考えます。まず既存Goldへ読取SQLで計算を試します。以下は現在のGold列を使うSELECTで、表を更新しません。

```sql
SELECT
    factory_id,
    production_date,
    good_qty,
    reject_qty,
    total_qty,
    round(100e0 * reject_qty / nullif(total_qty, 0), 2) AS reject_pct
FROM iceberg.gold.factory_daily_production
ORDER BY factory_id, production_date;
```

`100e0`で整数除算を避け、分母0はNULLにします。期間全体の不良率は日別割合の平均ではなく、同じ対象範囲の不良数合計÷生産数合計で計算します。

```sql
SELECT
    factory_id,
    sum(reject_qty) AS reject_qty,
    sum(total_qty) AS total_qty,
    round(
        100e0 * sum(reject_qty) / nullif(sum(total_qty), 0),
        2
    ) AS reject_pct
FROM iceberg.gold.factory_daily_production
GROUP BY factory_id
ORDER BY factory_id;
```

業務要件を満たすと確認したら、次を一組で変更します。

| 変更対象 | 決めること・確認 |
| --- | --- |
| `medallion/03_gold.sql` | 指標の式、工場/UTC日の粒度、NULL/0/負数/親子関係 |
| `medallion/checks/` / expected-demo.json | 実装式を複写せず独立した既知入力で期待値を用意 |
| `infra/demo/poc_catalog.py` | 列名・型・説明・粒度と固定catalogの更新 |
| `scripts/ai_context.py` / API schema | 新列を参照させる場合の固定SELECTと契約 |
| Superset / Notebook / 報告書 | 利用側のdataset列・表示・指標定義・比較条件 |
| docs / 図 | 何を算出し何を算出しないか、由来と未評価の範囲 |

Goldを新しい表にする場合、現在のpreview/AI APIは固定2表なので自動的には公開されません。新表と新APIを明示設計し、任意SQLの入口で代替しません。Trinoの表置換は1表単位で、全利用者への一斉切替機構は別に必要です。[採用484の表置換](https://github.com/trinodb/trino/blob/484/docs/src/main/sphinx/connector/iceberg.md#replace-tables)

## 2. 新しいMES/ERP契約・列を受け入れる

標準は6契約固定です。任意CSVをlandingへ置くだけで、新しい業務表がSilver/Goldへ自動追加される構成ではありません。

| 順序 | 変更箇所 | 必要な判断 |
| --- | --- | --- |
| 契約定義 | `medallion/sources.json` | path、列、主キー、更新時刻、型、文字コード、NULL |
| 入力生成/抽出 | generate-demo / service / SQL table-map | 合成値、実列対応、読取り権限、完了marker |
| 取込 | `scripts/admin.py` のresolve_source/ensure_tables/ingest | 原本判定・Bronze型・hash/再投入・上限・画像分離 |
| 採用/隔離 | 01_silver.sql / 02_quarantine.sql | 最新候補、妥当性、親参照、不正理由 |
| 業務集計 | 03_gold.sql | 粒度とJOIN増幅、遅延/訂正/削除 |
| 利用境界 | catalog/API/MCP/Notebook | 固定allowlist、型、認可、行数上限、由来 |
| 移行/試験 | 別namespaceと独立fixture | 旧schema/既存snapshot/旧入力の互換性、戻し方 |

`ensure_tables()`は不足する表の準備で、すべての既存Iceberg schemaを自動移行する機能ではありません。列追加・型変更・partition変更では、PyIceberg/Trino/Sparkの採用版ごとの対応と旧データの読書きを別に試験します。会社の特殊な列名や変換は、まずDBAが用意するread-onlyビューで標準契約へ対応できるか検討します。

ファイル削除と行削除を混同しません。同じ相対pathの全件ファイルを置換する経路と、DBのDELETEイベントを追うCDC経路は別です。変更を取り込む方式ごとに重複・訂正・再開条件を決めます。[メダリオン](MEDALLION.md)・[CDC](CDC.md)

## 3. GUI/API/MCPの操作を追加する

| 層 | 主な場所 | 設計・試験 |
| --- | --- | --- |
| 操作と応答型 | `infra/demo/api.py` / poc_api.py | 固定method/path、入力範囲、Bearer、秘密非表示 |
| 有限job | job_manager.py / service.py | 排他、完了/取消境界、timeout後の結果不明、20件非永続 |
| GUI | app.py / poc_ui.py / client.py | 操作目的、同意、確認、API成功と下流到着の区別 |
| 公開catalog | poc_catalog.py | capability/read-write分類、service、help allowlist |
| MCP | `infra/mcp/server.py` のcreate_server/Backend | 既定read-only、opt-in write、confirm、固定backend paths |
| 設定 | control_settings.py / scripts/settings.py | allowed keys、保存/適用、private path、変更反映 |

機能を増やすために任意shell・任意URL・任意SQLの汎用実行器を公開しません。新しい操作は最小の許可対象と入力型へ限定し、受付・実行・確定・再試行を設計します。設定保存とhost/DB/containerの変更を一つのボタンへ隠しません。

受入では正常系だけでなく、401、422、409、部分失敗、timeout中の継続、秘密を含む下流エラー、再起動後の履歴消失を確認します。レスポンスとログの両方を点検します。[API](API.md)・[MCP](MCP.md)

## 4. Spark・Airflow・imageを更新する

タグだけを変更せず、digest、Python lock、Spark minor/Scala/Java/Iceberg runtime/AWS bundle、Airflow/Python/Provider/constraintsを組で更新します。ビルド成功、import成功、実際の分散実行は別の確認です。

| 段階 | 最小の確認 |
| --- | --- |
| source | 正式版・image manifest・checksum・license・廃止変更 |
| build | 元Dockerfileとlockから再構築、uv pip check、CAあり/なし |
| compute | driver/executorのPython minor・Arrow/Pandas、remote実行、History |
| データ | Iceberg read/writeとTrino相互参照、既存表/snapshotの読取り |
| orchestration | 手動DAG、Connection解決、worker依頼、保存/再起動 |
| 運用 | restart影響、旧imageへ戻す条件、DB migrationの戻せる範囲 |

旧imageへ戻すだけで新DB schemaを戻せるとは限りません。更新前の整合backupと復元試験を計画します。[互換性](COMPATIBILITY.md)

## 5. profile・サービスを追加する

7Composeへ新しいservice/profileを加える場合は、service名・専用volume・secret・port・loopback・health・resource・logging・起動/終了の責任を定義します。共有のNO_PROXY canonical、設定生成/検査、GUI入口、固定診断、CI Compose検査、報告図、manifestを同時に更新します。

新しい公開portを無条件に全interfaceへbindしません。Notebook等がコード実行権を持つ場合は利用者分離と認可を設計します。選択しないprofileの秘密項目がcoreの起動を止めないことも確認します。

## 6. 性能・復旧・AIの評価を追加する

入力サイズ、同時実行、既存負荷、snapshot数、cold/warm cache、計測区間、タイムゾーンを揃え、元の比較方法を保存します。設定mem_limitを実使用量として報告しません。実行回数と失敗/再試行も記録します。

AIではcontext生成、OCR/embedding、検索、回答生成を分けて測ります。鮮度、根拠、権限、削除同期、誤回答、遅延、費用を評価し、合成vectorやDocument化を意味検索精度の代わりにしません。

[評価計画](reports/EVALUATION-PLAN.md) と [報告書雛形](reports/POC-REPORT-TEMPLATE.md) へ、事前の成功条件・実測・限界を記入します。未実施項目は空欄のまま理由を残し、構成図や通過テスト数から性能・HA・業務効果を推測しません。

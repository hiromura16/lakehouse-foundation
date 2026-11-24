# CDC Consumer の責務と置換条件

<!-- doc-purpose:start -->
> **役割:** 現行 CDC Consumer の整合性条件と、標準 Sink と宣言的変換への移行前に比較する証拠を整理する。
>
> **対象読者:** 開発・設計担当
>
> **読むタイミング:** CDC 処理の分割、再送、標準 Sink の評価条件を検討するとき。
>
> **前提となる資料:** [現行アーキテクチャの棚卸し](<CURRENT-ARCHITECTURE-INVENTORY.md>)
>
> **次に読む資料:** [段階的リファクタリングの実行計画](<REFACTORING-ROADMAP.md>)
>
> **文書一覧:** [目的・読者から探す文書マニフェスト](<../DOCUMENTATION-MANIFEST.md>)
<!-- doc-purpose:end -->

## 現行の整合性境界

`infra/cdc/consumer.py` の `process` / `run` と `consumer_runtime.py` の `Runtime` が適用順を決め、`state.py` と `cdc_replay.py` が位置、状態、復旧を支える。SQL Server の capture 世代と source LSN、Kafka の topic/partition/offset、Iceberg snapshot、SQLite の状態は交換可能な同一の値ではない。

| 現行責務と観測点 | 候補と残る責務 | 移行前に比較する証拠 |
|---|---|---|
| SQL Server capture/reader と Connector 登録 | Debezium と既存の scoped 診断を維持。Raw の方式変更を capture・接続設定・Connect offset の変更に結び付けない | 既存 capture と権限、source identity、登録済み設定が変わらない。イベント欠落なし |
| Kafka polling、有限 batch/バイト上限、エラー、同期 ack | PR 3 では既存 Python に残す。PR 4 で Sink の入出力を別 group と別 namespace で比較 | 不正 topic/schema、通常・上限・タイムアウト時の運用、ack 境界を比較。分離後の観測点も必要 |
| Raw append とイベント同一性 | 検証する候補は Apache Iceberg Sink Connector。topic/partition/offset 由来の識別、固定デモの同一位置・異なる payload 拒否、原文・キー保持を失わない | raw event ID 集合、payload/key の整合、欠落・不正重複ゼロ、append 前後の中断。監査用原文の保持・公開範囲を合意 |
| 現在値、LSN・削除・再構築 | Python が SQLite に stage し、完了位置を記録。代替の Spark SQL 等で、正しい順序、tombstone、誤った復活禁止、再構築、identity mismatch/lost state の停止を証明するまで残す | INSERT/UPDATE/DELETE、再送、同一キーへの版逆転、異なる capture lineage、同じ時刻の更新、途中停止、初期空状態・消失状態 |
| Bronze/派生、部分成功 | 現在値から 6 表を置換し SQL を実行。複数表は独立 commit、部分反映があり得る。Silver/Gold/Quarantine は後段の宣言的 SQL と比較し、必要な最小アダプターのみ検討 | 各表の世代差検出、原値・理由・再処理、型・欠損、トランザクション内の訂正、日付・粒度、正しい Gold 集計 |
| 停止/再開・再処理、ヘルス | strict source identity、状態消失拒否、リビルド、観測と異常終了、認証済み runtime control を維持。単純な書込先変更では代替できない | 同一 group の開始位置と最初の offset、古い writer 停止、再開 RPO/RTO、権限不足の拒否と非公開の診断 |

現行順序は **Raw 保存 → 現在値 stage → Bronze/派生 → ローカル完了位置 → Kafka 同期 ack**。この順序を観測できることと、全工程を原子的に戻せることを混同しない。オープン形式であることだけで、sink の routing、schema evolution、catalog 対応、offset と Iceberg commit の整合が満たされるとは判断しない。

## 目標と置換評価

検証目標は Kafka → 標準 Iceberg Sink → Raw/Bronze → Spark SQL 等の宣言的な変換 → Silver/Gold/Quarantine。構成採用済みではない。Raw は原文と送達証跡、Bronze は解釈前の事実を失わない境界、Current State は順序つき派生、Silver はコンテキスト別の適合、Quarantine は理由付き隔離、Gold は目的別の読み取り。Raw と Bronze の物理分割は ADR の対象とし、論理分割を先に固定する。

- PR 3: 既存の raw writer を **隔離された試験用の Raw-only 経路**として評価する。現行経路の ack 意味を先に変更しない。後段にだけ頼るデータの保持範囲・遅延・再読取の根拠が必須。
- PR 4: 公式のリリース・設定仕様を調べ、適合する場合のみ別 Kafka group、別カタログ namespace、個別 ACL の使い捨て比較環境に Sink を入れる。原表、新旧同時書込、既存 consumer/connector offsets、DLQ が存在するとの仮定は禁止。offset/commit 前後の障害、schema 追加・型変更、未知の payload、namespace routing を再現する。要求に不適合なら、採用せず不足を記録する。
- PR 5: 現行 SQL/fixtures を基準に、同じ入力・件数・値・品質理由・順序で比較。承認済みの公開契約を超える互換性を主張しない。PR 6 は見届け期間と復帰演習を通過してからだけ縮退・削除する。

## 試験と切替の判定

`tests/test_cdc_{fixed_source,fixed_state,state,replay,runtime,registration,health_contract}.py` は現行特性の根拠で、実際の end-to-end の代わりではない。新経路は同一の合成ソース fixture、固定イメージ・設定、別 writer/別 namespace で照合する。Raw・現在値・派生それぞれの完了と Kafka 位置の差を採取し、同一 event ID の実質重複ゼロ、欠落ゼロ、削除復活ゼロ、pause/resume 後の不正飛び越しゼロを判定する。監視は遅延、未適用件数、誤ルート、隔離理由、未承認の権限使用の有無を個別に示す。

分岐間で共用するのは意味・fixture・成否基準。main の catalog と Vision の任意 relay/managed 経路は同じ挙動と仮定しない。成功後も、運用切替とバックアップ復元は別の承認・検証とする。[段階と復旧手順](REFACTORING-ROADMAP.md)。

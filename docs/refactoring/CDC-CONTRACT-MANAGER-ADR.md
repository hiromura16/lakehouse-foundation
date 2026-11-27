# CDC Contract Managerの境界と段階導入

<!-- doc-purpose:start -->
> **役割:** CDC契約の静的境界、既存互換、段階移行とOSS比較の判定条件を定める。
>
> **対象読者:** 開発・設計担当
>
> **読むタイミング:** CDCの契約を追加・変更する前や、外部sinkの隔離比較を設計するとき。
>
> **前提となる資料:** [CDC Consumer の責務と置換条件](<CDC-CONSUMER-ASSESSMENT.md>)
>
> **次に読む資料:** [段階的リファクタリングの実行計画](<REFACTORING-ROADMAP.md>)
>
> **文書一覧:** [目的・読者から探す文書マニフェスト](<../DOCUMENTATION-MANIFEST.md>)
<!-- doc-purpose:end -->

**状態:** フェーズ1の契約と読取りCLIに続き、フェーズ2Aで合成再生と同値性を検証します。接続・設定反映・sink置換は未実行です。既定の固定6表および汎用CDCが稼働時の正本です。

## 決定

ソース、送達、保存、派生、運用を次の境界に分けます。

| 境界 | 責務と越えてはいけない線 |
|---|---|
| Source / Debezium | SQL表・capture・型・snapshot方針。Site側の既存CDC、接続先・認証は契約ファイルやCLIへ渡さない。資格情報と実対象は別の承認済みprivate設定から解決する。 |
| 送達 / Kafka | topic、partition、offset、event envelope、Kafka tombstone。OffsetとDebeziumのsource LSNは別物。group・offsetやschema historyを別engineと共有しない。 |
| Raw / Consumer | 業務解釈より先に原本を追記し、topic/partition/offsetを発送位置として一意化。論理変更の判定はsource identity、順序、業務PKと切り離す。 |
| Current / Silver | 順序付き業務PKに基づく最新状態または追記。削除、遅着、重複、隔離と型保証を契約で明示。Silverの業務ルール／参照整合はsink製品の代替ではない。 |
| 管理 / 観測 | 定義のレビュー、差分、stageの状態、監査。フェーズ1は静的CLIだけ。未来のGUIは直接DB・Kafka・Icebergへ書かず、認証済みAPIが限定workerへ仕事を渡す。 |

契約は配置資源の作成指示ではなく、**期待する型と境界の宣言**です。宣言が妥当でもSQL側の実表・権限・CDC・snapshot構成、Kafka topicの存在、Iceberg schema/field ID、消費者実行中の互換を確認したことにはなりません。現時点では実際のSQLやネットワークへ接続しません。

## フェーズ1: 実装の基準

- [JSON Schema v1](../../contracts/cdc/contract.schema.json)に従う、UTF-8 JSON一形式です。DB、schema、table、topic prefix、enabled、順序付きPK、current/append、Raw/Current/Silverの保存先、物理列の名前・型・NULL可否・幅・精度・スケール、削除とsnapshotの方針を宣言します。識別子は先頭が英小文字のASCII表記で最大63文字（topic prefixのみハイフン可）。SQL Serverのsystem DB・物理sourceの重複・短縮/完全topic名の衝突を拒否し、組み立てた名前も[Kafkaの249文字上限](https://kafka.apache.org/44/operations/basic-kafka-operations/)で検査します。URL、認証、SQL断片、任意の処理コマンドは含めません。
- [固定6表の公開fixture](../../contracts/cdc/fixed-demo-v1.json)は既存実装の照合用です。DB名は合成ラベルで、実行時の設定を置換しません。変更してもconsumer設定・テーブル・offsetは変化しません。意図した共有Rawだけを同一stream IDで明示し、Current/Silverや別streamの衝突は拒否します。
- diffは列落ち・型の縮小・必要な値域の縮小・鍵順変更・保存先や方針の変更を安全な自動移行にしません。たとえばdecimalはprecisionの増加だけで十分ではなく、整数部と小数部の両方を失わない必要があります。diffは旧→新の値域保存と追加列の必須性を静的に調べます。新→旧の読取り、旧producerとの両方向互換、実runtimeや配置の互換までは示しません。名前の変更は同じ列の型変更とは扱いません。PKは[SQL Serverの上限](https://learn.microsoft.com/en-us/sql/sql-server/maximum-capacity-specifications-for-sql-server)に合わせて32列までとします。PK列は定義（同型の拡大も含む）を固定し、[索引keyにできないMAX型](https://learn.microsoft.com/en-us/sql/relational-databases/sql-server-index-design-guide)を拒否します。索引種別・複合キーの実バイト上限・照合順序は後段の実テーブル確認と区別します。
- 表形式が受理されたこと、オフライン差分に破壊的変更がないこと、将来別の明示承認で運用上適用できることは三つの別判定。validate/diffは入力を変更せず、機械可読な結果を返します。SQLの存在確認・CDC照会は後段の専用事前確認です。

リポジトリルートから実行します。

**Bash / Zsh共通**

```bash
python3 scripts/cdc_contract.py validate contracts/cdc/fixed-demo-v1.json
python3 scripts/cdc_contract.py diff contracts/cdc/fixed-demo-v1.json contracts/cdc/fixed-demo-v1.json
```

exit 0はvalid/compatible、exit 1はinvalidまたはincompatible、exit 2は引数の使い方が不正です。これはlive apply、connector start、DDL、バックフィルの入口ではありません。

### 固定6表の基準と意図的に残す制約

- [表・型DDL](../../infra/sqlserver/002_demo_tables.sql)、[captureの有効化](../../infra/sqlserver/004_demo_cdc_enable.sql)、[固定mappingとread-only preflight](../../infra/cdc/fixed_source.py)、[topicの生成規則](../../infra/cdc/config.py)、[connector](../../infra/cdc/connector.py)を比較します。KafkaではDB名あり・なしのtopic名を受け付けます。初回snapshotは現状維持で固定モードの排他snapshotは別途DBA判断です。
- 固定モードはRawの `sqlcdc_events.events`、Currentの `sqlcdc_bronze`、Silverの `sqlcdc_silver` を使用します。[Raw writer](../../infra/cdc/raw_writer.py)はnullの配送記録と空文字、DELETE本体を区別します。[Current State](../../infra/cdc/state.py)は物理型付き像だけでなく原値JSONを使い、PK一致を確認し、DELETEでは版の証跡を残して遅着で復活しないようにします。CDCの名前空間は[consumer](../../infra/cdc/consumer.py)が選び、[既存のSilver SQL](../../medallion/01_silver.sql)の現在値集約を再利用します。
- 現行固定runtimeは単一の文字列PK（ASCII、最大160）、12桁の小文字hexのrun ID、UTC、LSN/event順序、業務像とRawの分離を守ります。固定の値域・長さと物理型は別です。物理datetime2(7)に相当する文字列表現を使う箇所があっても、全経路の7桁保持は主張しません（Silverの日時関数に注意）。複合PK、append、別形式、任意のDDL/型移行はv1宣言の領域で、既存runtimeが動かせるという意味ではありません。
- 既存防御は[レコード上限](../../infra/cdc/event_limits.py)の16 MiB、[現在値側](../../infra/cdc/state.py)の10万行/表と64 MiB、および[rebuild Raw](../../infra/cdc/raw_history.py)の10万イベントです。上限引上げや無制限の後追いは行いません。複数コンポーネントをまたぐ単一transactionや、自動的なexactly-once保証もありません。

## フェーズ2A: 既定OFFの合成consumer

`consumer.process` のbatch入口は明示的な契約処理を受けられます。合成実験のCLIだけがこの入口を選び、`consumer.run` の既定のSQL接続、Kafka group、offset、稼働状態volume、Iceberg保存先、既存rebuildには接続しません。フラグ単独でもファイル引数単独でも通常consumerへは進みません。

**Bash / Zsh共通**

```bash
CDC_CONTRACT_EXPERIMENT=true python3 infra/cdc/consumer.py \
  --contract-preview "$CONTRACT_FILE" --records-file "$RECORDS_FILE"
```

両ファイルは合成用のUTF-8 JSONを別途生成します。recordsは `topic, partition, offset, key, value, ingested_at` の配列で、key/valueはDebeziumのJSON文字列またはnullです。値やSQL資格情報は受入結果へ出しません。最大1,000イベント、入力16 MiB。CLIはメモリ内のSQLiteだけを使い、結果件数と固定reasonを返します。運用Composeには選択設定を追加していません。

- 固定6表: 同じ契約から現行mappingを照合後、既存のRaw・Current・Silver処理へ渡すフックを使い、フックなしと同じ入力で結果・削除・Tombstone・完了位置を比較します。運用既定はフックなしのままです。
- 動的経路: enabled表の両topic名からsourceを選択し、SQL Server型、nullable、精度、複合PKの順を厳密に解釈します。JSONの上で数値化できても、小数は文字列で桁を保存します。日時はDebeziumのadaptive単位と宣言したscaleを照合し、7桁目まで落とさない場合を検査します。SQL Serverのcollation等価性は未検証なので照合時に空白除去や大文字小文字変換を推測しません。
- Rawを派生より先に保存し、未知topic、不正型、複製位置の異内容、派生失敗では完了位置を進めません。処理失敗は隔離用の固定理由を保存して停止し、自動skip/ACKは行いません。Currentとbasic Silverは検証用SQLiteの同一transactionで公開され、Iceberg上の複数表transactionを意味しません。再生は、派生行・完了位置と同じSQLite transactionで確定した受理順だけを使います。Rawへの保存順とは別なので、失敗後に並び順を変えて再試行しても、分割間・同一表の別名間で勝手に並べ替えません。再生したCurrent/Silver、版、完了位置と受理順を照合し、差があれば成功扱いしません。
- この段階では稼働環境のoffset移送やrebuildは追加しません。Kafka consumerの正式経路を繋ぐ前に、専用group・catalog・state／単一writerの実証、状態喪失時の拒否、Icebergの部分commit・schema/型の互換、実engineからの不完全なbefore像/未取得LOB、管理者承認の復元経路を同じ隔離fixtureで確認します。結合済みの現行SQLで行うSilver/Goldもこの単純な投影とは別です。

## フェーズ2B: 実カタログへの一表合成試験

[試験](../../tests/integration/cdc_contract_iceberg/proof.py)は新しい単一 partition の Kafka、専用 group、専用ディレクトリの SQLite/PyIceberg SqlCatalog/Parquet だけで、一つの複合キーを持つ snapshot・UPDATE・DELETE・tombstone 各1件を処理します。SQL Server、Debezium、稼働中の broker、REST catalog、Trino は利用しません。専用 Actions runner の一回限りの内部ネットワークに broker と試験プロセスがあり、ホストポートやアプリの認証情報はありません。通常の Consumer/Compose から起動されず、運用設定は変わりません。

Raw → Current → Silver の順に実 Iceberg を commit・readback した後、別 SQLite 接続から完了位置を確認して同期 Kafka ACK を行います。Silver の catalog pointer の実更新だけを一度拒否し、障害中の Raw/Current は残る一方、Silver と完了位置/ACK は旧位置のままか比較します。接続を閉じて同じディレクトリと group を開き直し、再配送後の各世代・型と終端位置を照合します。価格は decimal、datetime2(7) は桁を失わない文字列で保存します。これらは試験専用の追加型と保存世代であり、運用の型やビューの変更ではありません。

Current/Silver は配送位置ごとの**世代レシート**で、障害時には一部の Iceberg 世代が先に観測可能です。複数表間の atomic commit や一般のクエリへの一括公開、実プロセスの強制終了、機器・DBを含む復旧を保証しません。SQLite を開き直すときは必須ファイルの欠落、状態の schema/契約の不一致を拒否します。受理行だけの消失は起動時の検査ではなく、後段の明示 replay で検出します。topic/partition/offset は同じ partition 内の順を表すだけで、複数 partition・別名間の受理順を再構築できません。独立した耐久受理台帳と復旧契約は後続課題です。 この試験の再開は Silver の失敗が local commit より前に起きた場合だけです。local commit 後・broker ACK 前にプロセスが落ちて broker 位置が遅れていれば、再開時に位置の不一致で拒否します。この障害区間の自動再配送・ACK修復は未実装です。

ローカルの明示 `--local-storage-only` は実 Iceberg への読み書きと再開を行いますが、Kafka 配送・ACK だけ代替です。実 Kafka の証拠は専用 CI 実行結果に限ります。5表、実 SQL Server/Debezium、運用 PostgreSQL/REST catalog/Trino、登録・変更の反映、再構築やデプロイの合格を意味しません。

## フェーズ3A: 合成登録の plan/status（読み取り専用）

[登録計画CLI](../../scripts/cdc_registration.py)は、合成scopeを明示し、呼出側が指定した2つの手元の JSON だけを比較します。SQL Server、Kafka、HTTP、既存 Consumer 設定、資格情報へ接続しません。ファイルを生成・更新する機能や apply コマンドはありません。

**Bash / Zsh共通**

```bash
python3 scripts/cdc_registration.py plan --offline-fixture \
  --fixture-id "$FIXTURE_ID" --desired "$DESIRED_CONTRACT" --observed "$OBSERVED_FIXTURE"
python3 scripts/cdc_registration.py status --offline-fixture \
  --fixture-id "$FIXTURE_ID" --desired "$DESIRED_CONTRACT" --observed "$OBSERVED_FIXTURE"
```

観測ファイルは `version=1`、`environment="synthetic"`、呼出時と一致する `fixture_id`（`fixture_` と小文字16進32桁）、`state`、`registered_contract`、`observed_spec_sha256` のみです。`present` は既登録の契約と観測した正規仕様の照合値の両方、`absent` と `unknown` は両方 `null` を要求します。入力が手元で記述された合成報告である限り、`absent` は実際の登録不存在、`present` は観測の真偽や鮮度の証明にはなりません。観測値を希望契約から作るとドリフトを検出できないので、別に採取・正規化した仕様を使います。証跡の公開は想定しません。

内部 `compile_spec()` は enabled 表だけを DB/トピックprefixで束ね、既存コネクタ形式の正規表現リテラルによる `table.include.list`、`column.include.list`、DB付きtopicと旧alias、順序付き複合キー・型付き列・出力の対応を作ります。表名と列名の並びは正規化し、複合キー順は変えません。履歴が衝突し得る別DBのprefix共用や同一コネクタのsnapshot方針混在を拒否します。snapshot方針は未変換の宣言であり、実コネクタの設定値ではありません。合成構成の照合で、現行の固定6表の登録方式は変更しません。

- `plan` はscope再検証、読取り専用preflight、追加・許容改訂、書出し先・既存offsetの照合を順に出します。稼働相当のコネクタへ表を追加する計画は、snapshotが再度実行されると仮定せず、backfillとoffsetの別検討を先に含めます。削除、無効化、縮小、キーや出力先の変更は実行候補にしません。
- `status` は件数、固定診断、照合値のみです。終了コード0は `aligned` または `planned`、1は `blocked`、2は `invalid`。観測未確定、仕様のドリフト、非互換変更で計画を止めます。CLIは元の識別子、パスや入力本文を出力しません。照合値も機密な入力の匿名化に使わず、成果物を公開前に検査します。
- `approval_candidate_sha256` はscope・希望契約・希望仕様・観測・操作順を結び付ける未署名の照合値で、権限、確認や実行チケットではありません。後続 apply は認証された観測から再計画し、鮮度・有効期限・権限・対象・fingerprintへの明示承認を検証します。専用workerの排他の下で各段階の事前/事後状態を照合し、同じ承認と操作IDの完了記録を再使用します。未知や部分失敗時は再試行で上書きせず停止して再観測します。既存group・offset・領域の削除やresetは通常 apply に含めません。

## OSS評価: 現行のKafkaを残す比較

2026-11-27に公開一次資料で机上確認。依存をインストールせず、接続・負荷・障害テストは未実行です。

| 観点 | memiiso Debezium Server Iceberg | Apache Flink + Iceberg |
|---|---|---|
| 入口・ライセンス | [Apache-2.0のプロジェクト](https://github.com/memiiso/debezium-server-iceberg)。検討版は[1.2.1.Final](https://github.com/memiiso/debezium-server-iceberg/releases/tag/1.2.1.Final)（Java 21、Debezium 3.6.3、Iceberg 1.11.0）。DB→Debezium Server→Icebergが公開構成。Kafkaにoffsetや履歴を置くことと、任意の既存Kafka topicをsourceにすることは別であり、同版の正式なKafka sourceは確認できなかった。[プロジェクトのKafka source質問](https://github.com/memiiso/debezium-server-iceberg/issues/154)もサポート根拠ではない。専用adapterなしに現行Kafkaの置換候補としない。 | [Flink](https://github.com/apache/flink/blob/master/LICENSE)/[Iceberg](https://github.com/apache/iceberg/blob/main/LICENSE)はApache-2.0。[Kafka source](https://nightlies.apache.org/flink/flink-docs-release-2.2/docs/connectors/datastream/kafka/)と[Debezium JSON](https://nightlies.apache.org/flink/flink-docs-release-2.2/docs/connectors/table/formats/debezium/)が公開されており、既存Kafkaを残す比較の第一候補。Flink/connector/IcebergランタイムJAR・Java・カタログの組合せと配布物のLICENSE/NOTICEは実験前に固定して確認する。 |
| I/U/D・複合PK | [sink設定](https://github.com/memiiso/debezium-server-iceberg/blob/1.2.1.Final/docs/iceberg.md)にupsertと削除を保持する設定がある。既定の追記・ソフト削除は現行Currentの物理除去と同値ではない。Keyの順、複合key、DELETE本体/Tombstone、キーがない場合の追記への降格を実測するまで同値としない。 | [Iceberg Flink upsert](https://iceberg.apache.org/docs/latest/flink-writes/)はV2でPK/equality fieldsを指定できる。パーティション元の列がequality fieldsに必要な場合がある。複合キー・DELETE・null valueの原本保持・遅着の順序は、Flinkの設定名だけで保証しない。 |
| decimal / datetime / schema | [同版sink](https://github.com/memiiso/debezium-server-iceberg/blob/1.2.1.Final/docs/iceberg.md)はtemporal精度・nullable変換・列追加の設定がある。桁境界、decimal文字列表現、元時刻とUTC/7桁の往復、名前変更・型縮小の拒否を試験する。自動列追加は許可者の契約審査に代わらない。 | Debezium JSONのschema付き／なしと現在の文字列decimalを照合し、timestamp/timezone・精度を変換境界で明示する。[Iceberg schemaのfield ID](https://iceberg.apache.org/spec/#schema-evolution)を維持し、安全な追加とrename/縮小を区別する。FlinkとTrinoの読取り差も測る。 |
| 中断・再起動 | [1.2.1リリース](https://github.com/memiiso/debezium-server-iceberg/releases/tag/1.2.1.Final)の曖昧commitへの保護変更と[Iceberg offset実装](https://github.com/memiiso/debezium-server-iceberg/blob/1.2.1.Final/debezium-server-iceberg-sink/src/main/java/io/debezium/server/iceberg/offset/IcebergOffsetBackingStore.java)は障害試験の根拠。DB側offsetであって既存group引継ぎの証拠ではない。commit直前・応答喪失・再開で確認する。 | [Flinkのcheckpointと復旧](https://nightlies.apache.org/flink/flink-docs-release-2.2/docs/concepts/stateful-stream-processing/)は入力位置と状態を保存する。Icebergの確定snapshot、実際のoffset、重複再送を照合する。checkpointだけで複数sinkや旧consumerとのexactly-onceを宣言しない。Job UID、savepoint、保持切れも別検査。FlinkのSQLデコーダが原本やtombstoneを変換・除外する場合、既存Rawを比較基準のまま維持し、代替sinkだけで原本保持できるとは扱わない。 |

**判断:** 現行Python consumerを維持。Kafkaを読む要件ではFlink + Icebergを先に隔離比較し、memiisoは別入力構成の参考候補に留めます。楽観的な「Kafka対応」の推測で置き換えません。Raw原本・固定CDCの順序と保持・Current/Silver・隔離・復元の全てが既存経路以上と確認できなければ本採用はしません。

## 段階導入、停止と復元

| 順序 | 実装と受入 | 停止・復元 |
|---|---|---|
| 1. 形式・差分・fixture | CLI、静的互換、6表のsource/topic/PK/Raw/Current/Silver照合。この段階は公開契約のみ。 | 撤回はコード差分だけで完了。live side effectなし。 |
| 2. Consumer読取 | 既定OFFのfeature flag、明示fixtureのみ、従来と同じRaw/Current/Silverと独立した状態の比較。複合PKは順序、部分NULL、同名別表、snapshotより新しいLSN、削除後の遅着を検査。外部SQLの実表/列・CDC catalog・権限は読み取り専用preflightで、観測不能ならfail closed。 | flagを戻し、既存group/offsetのmoveや既存DDLをしない。 |
| 3. plan/apply/status | planは読み取り、期限付きfingerprintに対象・変更・互換を結び付ける。専用workerのみapply、明示承認、最小権限、排他、状態照合・改変検知、idempotencyと固定reason。未知の状態で再実行しない。 | 途中失敗は停止・記録・再観測。承認された元設定・checkpoint・snapshotを使う。データ消去とresetは通常applyに含めず、所有が証明できる隔離fixtureに限定。 |
| 4. E2EとOSS比較 | 既存と同じengineで合成5表の112 snapshot行と後続1024観測を実数照合。I/U/DとTombstoneを別勘定、訂正・重複・毒レコード、Quarantine、独立した停止再起動・書込失敗、Raw replay、明示fixture限定reset後の再開を含む。次にOSSで同じ入力・評価を別namespace、別consumer group・state、分離した資格情報/データで再現。 | 生産group・topic・カタログ・volumeを共有せず、停止しても現行パイプラインへ影響させない。既存資産・本番役割にcleanupを波及させない。 |
| 5. 管理面と切替 | エンジンの回復・負荷・同値性が揃ってから、別PRでGUI/API/workerを分離。RBAC、CSRF、監査、状態表示、破壊操作と読み取りを別権限に。業務データの編集や資格情報表示は扱わない。独立レビュー、同値性・復元手順の実測、監督された切替の後にだけ古い分岐を別変更で除去。 | 既存reader及び管理面を保ち、状態を消さない。切替失敗時は根拠が取れる旧checkpointを維持、再開条件を再確認。 |

合成受入ではRawの位置一意、削除後Currentからの除去、Silver/GoldとTrinoでの対象キーと件数、隔離理由と再投入時の重複ゼロを個別に比べます。テスト結果に各範囲のRun ID/バージョンと観測件数を残し、実機例・認証情報・実DSN・原文データを公開に含めません。112/1024は将来の再現可能な合成入力の基準数であり、このフェーズで到達済みではありません。

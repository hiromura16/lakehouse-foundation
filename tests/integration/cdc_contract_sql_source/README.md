# 隔離SQL ServerからKafkaまでの合成受入

<!-- doc-purpose:start -->
> **役割:** SQL Serverの合成1表からDebezium経由でKafkaへ届く配送を検証し、CIで保証する範囲を明確にする。
>
> **対象読者:** 開発・設計担当 / 評価・報告担当
>
> **読むタイミング:** CDCの入口を変更するときや、隔離CI結果と実環境で未確認の項目を切り分けるとき。
>
> **前提となる資料:** [SQL Server の変更を Iceberg へ反映する](<../../../docs/CDC.md>)
>
> **次に読む資料:** [CDC Contract Managerの境界と段階導入](<../../../docs/refactoring/CDC-CONTRACT-MANAGER-ADR.md>)
>
> **文書一覧:** [目的・読者から探す文書マニフェスト](<../../../docs/DOCUMENTATION-MANIFEST.md>)
<!-- doc-purpose:end -->

このテストの合格は、専用の1表が SQL Server 2022 → Debezium
3.7.1.Final → Kafka の順に配信された証拠です。実配信の判定は
GitHub Actionsの当該コミットのログで確認してください。実運用への
接続、配置、変更、offsetの操作を行う手順ではありません。

* 同一リポジトリの該当PR、またはmainの明示的なworkflow dispatchだけ。
  GitHub-hosted Linux/amd64 runner、ローカルDockerソケット、OpenSSL、
  keytool、公式レジストリとハッシュ固定済みwheelの取得が必要です。
* 起動前だけイメージとwheelを取得します。SQL 2022 Developerは
  immutable digest、Kafkaは既存lock、Debeziumは既存の3.7.1.Final
  release-tag policyを維持します。その実行で得たRepoDigestで起動し、
  実Connectが返すSQL Server plugin版でも照合します。
* 生成した資格情報は管理者とreaderで分離。管理者は使い捨てDBだけを
  準備し、readerにはその表・CDCの読取を与えます。SQL Server 2022の
  Agent観測に追加する権限は試験限定で、実環境の推奨GRANTではありません。
  Java/Pythonの双方で一時CAによりSQL証明書と接続名を検証します。
* 専用Docker internal networkに置き、SQL、Connect、Kafkaのポートは
  公開しません。Connect内部topicだけをcompactし、schema historyを
  単一partition・実行中無期限で保持します。同じ版の
  [上流試験の完全topic名](https://github.com/debezium/debezium/blob/v3.7.1.Final/debezium-connector-sqlserver/src/test/java/io/debezium/connector/sqlserver/SqlServerConnectorIT.java#L1044-L1056)
  に合わせ、1個の完全名だけを読みます。別名や余分な配送は失敗です。
* 初期snapshotを受信後、別の行へINSERT／UPDATE／DELETEを順に実行。
  5配送（r/c/u/d と Kafka null）を同じpartitionの連続offsetとして照合。
  合成複合キーの順序、SQLの識別、before/after、NULLと空文字、
  decimal文字列、datetime2(7)のナノ秒整数を
  [3.7の型仕様](https://debezium.io/documentation/reference/3.7/connectors/sqlserver.html)
  に沿って検査します。adaptiveは試験の明示設定で、現行generic既定の
  connectモードが7桁を保存したという証拠ではありません。
* 出力は固定stage・固定reasonまたは成否だけです。認証、実体名・LSN・
  イベント本文、driver/Connectエラー原文は送りません。移行、リセット、
  既存環境、Kafkaの永続ACK、Raw以降の格納、5表統合・再起動・BIは
  このテストに含めません。

Dockerなしでcheckerだけを確認する場合:

    python3 -B -m unittest discover \
      -s tests/integration/cdc_contract_sql_source -p test_proof.py

## 再起動を含む別の検証

別の `disposable-sql-source-restart` ジョブは、snapshot 1件をKafkaで
確認してから別行のINSERTを実行します。Connect の読取専用 REST で
そのINSERTと一致する保存済み streaming 位置を確認し、Kafka の
schema history と snapshot・INSERT 両レコードのバイト列を記録します。
同じ所有済み Connect コンテナだけを停止し、停止状態と同じ起動IDを
確認した間に、INSERT済みの行をSQLでUPDATEします。SQLの更新後の値と
CDC変更表の実コミットLSNを確認し、Kafkaは停止期間の前後で
2件のままであることを検証します。SQL ServerとKafkaは再起動せず、
再登録・offset上書き・履歴削除を行いません。

再起動後は同じ履歴と最初の2レコードを再読し、停止中にSQLで捕捉した
コミットLSNと一致するUPDATEがKafkaの3件目として現れることを確認します。
Connect RESTの起動時の保存位置は元のINSERTまたは検証済みUPDATEだけを
許し、UPDATEの保存を確かめた後にDELETEを実行します。snapshotを再発行せず、
Kafkaの0–4番の5配送が1回ずつであること、DELETEのcommit LSNまで
source位置が保存されたこと、元の履歴が残ることを確認します。
snapshotだけの直後の再起動や、途中で強制終了した復旧は別の試験です。
公開ログには中身・位置・ハッシュを載せません。同じ実配送を読む
下流検証用の受け渡しはありますが、このジョブは契約consumer、
Iceberg、Broker group ACK、強制終了後の復旧や本番での厳密な一度きり配送を
保証しません。

## 同じ配送を契約 Consumer から Iceberg まで通す検証

`disposable-sql-wire-iceberg` は、上の source 再起動と同じ Kafka に残る
配送を再生成せずに読み、Consumer が受け取ったバイト列とも照合します。
手渡し成功の印と最終の保存成功の印は別々です。初回 snapshot の保存と ACK の後、Consumer、
SQLite、Icebergカタログを閉じて再度開き、SQLiteの完了位置とKafkaの
実group位置が一致する場合だけ続きます。読み取り・ローカル完了・
Kafka ACK は別々に検査します。

使い捨ての SQLiteカタログ＋ローカルParquet に対し、Raw 5件（Kafka
tombstoneのnullを含む）を元の配送と比較します。各配信後の Current と
Silver の世代も別々に再読出しし、snapshot行が残り、更新で NULL と
空文字が区別され、削除行は戻らず、decimal と7桁時刻が保存されることを
確認します。7桁時刻の検証用Iceberg列は厳密な文字列です。

このジョブは既存の契約実験用hookを明示使用します。現行の本番Sink、
オブジェクトストア、Trino/BI、強制終了や5表の受入ではありません。

補足証跡では、Raw／Current／Silver各表の実Iceberg snapshotを照会し、
append後の親子関係と、カタログを再openした際の同じsnapshot ID・内容を
検査します。最終SQLは保持行の複合主キー、NULL、decimalと7桁時刻を
読み、削除キーが存在しないことと、両派生層の保持行に差分がないことを
確認します。Kafkaの最新位置、実group位置、別のread-only SQLite接続の
完了位置もそれぞれ読み、lag=0を照合します。ログは件数・差分数だけで、
実snapshot IDやデータ値を出しません。

## 5表への拡張: オフライン照合の基準

`tests/integration/cdc_contract_scale/fixture.py` は、表別の合成型・
入力順と差分、固定範囲のSQLパラメータ、独立に求めた最終像を検査します。
source変更件数にはKafkaのtombstone（値がnullの別配送）は含めません。

| 合成表 | snapshot | INSERT | UPDATE | DELETE | Kafka tombstone |
| --- | ---: | ---: | ---: | ---: | ---: |
| alpha | 16 | 32 | 64 | 32 | 32 |
| bravo | 20 | 40 | 80 | 40 | 40 |
| charlie | 24 | 48 | 96 | 48 | 48 |
| delta | 24 | 64 | 128 | 64 | 64 |
| echo | 28 | 72 | 144 | 72 | 72 |
| 合計 | 112 | 256 | 512 | 256 | 256 |

SQL変更は計1024件、Kafkaで照合する件数はsnapshotとtombstoneを
含め1392件。各topicのpartition 0で欠落・余分な配送・別表への混入を
拒否し、2つの派生層の最終像をイベント再生とは別の算出方法で比較します。
異なるtopic間で共通のoffsetや順序があるとはみなしません。

    python3 -B tests/integration/cdc_contract_scale/fixture.py
    python3 -B -m unittest discover \
      -s tests/integration/cdc_contract_scale -p 'test_*.py'

これは合成ワイヤ・契約Consumer・SQLite上での照合です。実SQL、
Connect/Kafkaや実Icebergの5表実行結果ではありません。次の隔離試験で、
固定5表のSQL snapshot順をtableごとに `ORDER BY site_id, record_id` で
明示し、1表版と同じ起動境界で実SQLの件数／Kafka実バイト列／Raw／
Current・SilverのIceberg readbackを比較します。故障後の再開は、
最後に確定した表・partitionの位置から同じ最終像へ戻る場合のみ合格。

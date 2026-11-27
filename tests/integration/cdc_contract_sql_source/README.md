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
同じ所有済み Connect コンテナだけを停止し、新しい起動を確認します。
SQL Server と Kafka は再起動しません。再登録・offset上書き・履歴の
削除は行いません。

再起動後も同じ保存位置、履歴、最初の2レコードを再読してからのみ、
INSERT済みの行にUPDATEとDELETEを実行します。snapshotを再発行せず、
Kafkaの0–4番の5配送が1回ずつであること、DELETEのcommit LSNまで
source位置が保存されたこと、元の履歴が残ることを確認します。
snapshotだけの直後の再起動や、途中で強制終了した復旧は別の試験です。
公開ログには中身・位置・ハッシュを載せません。同じ実配送を読む
下流検証用の受け渡しはありますが、このジョブは契約consumer、
Iceberg、Broker group ACK、強制終了後の復旧や本番での厳密な一度きり配送を
保証しません。

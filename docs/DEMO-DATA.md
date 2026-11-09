# MES・ERPのダミーデータ投入

`generate-demo.py` は工場MES・ERPに似た原データを作り、ランディングディレクトリか、専用のSQL Server検証DBへ投入するだけのツールです。取り込み、メダリオン変換、Trino、Dokployは呼び出しません。会社の実データを参照する必要はありません。

## ランディングへ生成する

Python 3.10以降の標準ライブラリだけで動きます。出力先はGitリポジトリの外に置きます。既存の空ディレクトリも使えます。

```sh
python3 scripts/generate-demo.py \
  --destination landing \
  --output /srv/demo-input \
  --seed 42 --orders 20 --days 7 --scenario edge
```

同じseed・orders・days・scenarioなら、6ソースの内容とSHA-256は毎回同じです。時刻の基準は固定の架空操業日であり、実行日時を使いません。`--orders` は製造指図・受注それぞれの件数、`--days` は操業日数です。2工場・4製品を使い、1指図に2製造イベント、1受注に2出荷を作ります。

既存の生成ファイルと `manifest.json` は通常上書きしません。別の出力先を使うか、意図的に置き換える場合だけ `--force` を指定します。他の名前のファイルは削除しません。ソースごとに一時ファイルから完成名へ切り替え、その後 `<filename>.ready` にSHA-256を記録します。生成器は取り込みサービスを起動しませんが、既にそのディレクトリを監視する取り込みサービスがある場合は、readyの完成後にそのサービスが独立して取り込むことがあります。

`manifest.json` はseed・シナリオ・行数・ハッシュ・SQLへ投入した場合の新規スキーマ名を記録します。検証SQLやlakehouseへの操作指示は含みません。ランディングとSQLの両方へ投入する場合は `--destination both` を使います。ファイル公開とSQLコミットの間には分散トランザクションがなく、片側の成功後にもう片側が失敗する可能性があります。

## 6つの入力契約

正本は [sources.json](../medallion/sources.json) です。CSVにはヘッダーを付け、JSON Linesは1行1オブジェクトです。すべてUTF-8で、値は数値・日付も含め文字列です。時刻はUTCのISO 8601、日付は `YYYY-MM-DD` です。

| ソース | ファイル | 主キー・参照 |
| --- | --- | --- |
| ERP製品マスター | `csv/erp_products.csv` | `product_id` |
| ERP製造指図 | `csv/erp_work_orders.csv` | `work_order_id`、`product_id`を参照 |
| ERP受注 | `csv/erp_sales_orders.csv` | `sales_order_id`、`product_id`を参照 |
| ERP出荷 | `csv/erp_shipments.csv` | `shipment_id`、`sales_order_id`を参照 |
| MES製造実績 | `json/mes_events.jsonl` | `event_id`、`work_order_id`を参照 |
| MES停止履歴 | `json/mes_downtime.jsonl` | `downtime_id`、`work_order_id`を参照 |

全ソースの `updated_at` は、その業務キーの版の新しさを示します。MESの `event_time` や停止開始・終了時刻は実際の事象時刻です。この区別により遅れて到着した実績を元の操業日に集計できます。原本をBronzeへ取り込む際に、取り込み側が `_source_file`・`_source_hash`・`_ingested_at` を付けます。

`clean` は型不正や参照切れのないデータです。通常の分納・未出荷残・納期後の出荷は含みます。`edge` は次を追加します。

- 同じ製造イベントの完全重複と、良品数を3増やした新しい改訂版。
- 事象時刻は古いままで、更新時刻が後日の遅延到着イベント。
- 数値として解釈できない良品数・単価、負の良品数・出荷数。
- 未登録製品、存在しない製造指図・受注への参照。
- 終了時刻が開始時刻より前の停止履歴。

## SQL Serverの専用検証DBへ投入する

この機能だけ追加ドライバが必要です。

```sh
python3 -m venv /srv/demo-venv
/srv/demo-venv/bin/python -m pip install -r requirements-demo.txt
```

管理者が先に専用の検証DBと書き込み用ユーザーを用意します。生成器はDBを作りません。会社の読み取り接続を示す `SOURCE_SQLSERVER_*` を書き込みには使わず、別の非公開envファイルに次を設定します。ファイルはリポジトリ外・0600にします。

```dotenv
DEMO_SQLSERVER_HOST=demo-sql.example.invalid
DEMO_SQLSERVER_PORT=1433
DEMO_SQLSERVER_DATABASE=ManufacturingDemo
DEMO_SQLSERVER_USER=demo_writer
DEMO_SQLSERVER_PASSWORD=REPLACE_LOCALLY
DEMO_SQLSERVER_CA_FILE=system
SOURCE_SQLSERVER_DATABASE=CompanyProduction
```

`SOURCE_SQLSERVER_DATABASE` は書き込み禁止対象のDB名です。会社用設定と同じファイルを使う場合も、書き込みに使う資格情報は `DEMO_SQLSERVER_*` だけです。`master`・`model`・`msdb`・`tempdb` と、設定済みの会社ソースDB名を拒否します。書き込み先が会社の業務DBと別であることを管理者が確認してください。

```sh
/srv/demo-venv/bin/python scripts/generate-demo.py \
  --destination sqlserver \
  --output /srv/demo-run-metadata \
  --env-file /srv/private/demo.env \
  --allow-sql-write --seed 42 --orders 20 --days 7 --scenario edge
```

実行ごとに `lh_demo_<ランダム値>` という新規スキーマを使います。既存スキーマを再利用するオプションはなく、同名が存在した場合は停止します。新規6表の作成とINSERTだけを行い、会社の既存表をUPDATE・DELETE・DROPしません。CDC・ログイン権限も変更しません。SQL側のDDLとINSERTは1トランザクションでコミットし、途中エラーならロールバックします。`--force` でもSQLの既存スキーマは置き換えません。検証後のスキーマ削除は管理者の別作業です。

各SQL表も全列 `nvarchar(max)` の生データ用ステージングです。異常な型の文字列と重複版を検証するための設計であり、本番MES・ERPの型付きスキーマ、制約、インデックス、更新負荷、CDC性能の再現ではありません。型・CDCの試験は実ソース契約に対応した別の試験で行います。

接続は暗号化を必須にし、通常はCAと証明書のホスト名も検証します。`DEMO_SQLSERVER_CA_FILE` はOSの信頼ストアを使う `system`、または社内CAのPEMファイルを指定します。FreeTDSの `require` は暗号化を要求し、証明書検証には `ca file` の指定も必要なので、ツールはプロセス専用設定で両方を指定します。[FreeTDS公式設定](https://www.freetds.org/userguide/freetdsconf.htm)、[pymssqlの暗号化引数](https://github.com/pymssql/pymssql/blob/master/docs/ref/pymssql.rst)

自己署名証明書のローカル検証DBに限り、`--test-trust-server-certificate` で証明書検証を例外扱いできます。このフラグはloopback接続だけを許可し、暗号化自体は必須のままです。会社接続でこの例外は使いません。

## メダリオン変換と期待値は別の作業

生成器の実行後、取り込み・変換を明示的に実行します。[01_silver.sql](../medallion/01_silver.sql)、[02_quarantine.sql](../medallion/02_quarantine.sql)、[03_gold.sql](../medallion/03_gold.sql) の順です。取り込み前の6表は `iceberg.bronze`、整備済みは `iceberg.silver`、不正レコードは `iceberg.quarantine`、業務集計は `iceberg.gold` に置きます。

Silverは `TRY_CAST` と安全な時刻パースを使い、主キーごとに `updated_at`・原本ハッシュ・取り込み時刻の降順で最新の1版を選びます。同順位では原本の文字列表現で順序を固定します。完全重複と過去版は隔離件数に加えません。最新候補の型・数量・親参照が不正なら、そのレコードを隔離し、過去の正常版へ自動的に戻しません。不正な更新時刻は新旧比較できず、パースできる時刻より後順位になります。隔離表には `raw_record_json` と理由コードを残すので元の不正文字列も確認できます。

Goldの `factory_daily_production` は工場・UTC日ごとの計画数、良品数、不良数、生産数、停止分、歩留まり、計画達成率です。計画・実績・停止をそれぞれ先に集約してから結合し、子明細同士の結合で数量を増幅させません。歩留まりは良品数÷生産数、計画達成率は良品数÷計画数で、分母0ならNULLです。停止時間は開始日の集計で、日跨ぎの按分は行いません。稼働カレンダーと速度基準がないためOEEは算出しません。

`sales_fulfillment` は受注ごとの受注数、出荷数、未出荷残、履行率、納期後出荷数・件数を示します。出荷は受注ごとに先に集約し、納期後の判定はUTC出荷日が約束日を過ぎた場合です。未出荷残は0未満にならず、過剰出荷なら履行率は100%を超えます。

Icebergの置換コミットは表ごとです。全6表・隔離表・Goldをまとめるトランザクションはなく、変換途中に複数表を読むと実行世代が混在し得ます。定期運用では実行単位を固定し、成功後に利用側へ切り替える仕組みを追加してください。

[expected-demo.json](../medallion/expected-demo.json) はseed42・orders20・days7・edge専用の検証fixtureです。生成器のmanifestとは別に管理し、SQLとは独立した整数・Decimal・日付の計算で期待値を用意しました。代表値は次のとおりです。

| 表 | Bronze | Silver | Quarantine |
| --- | ---: | ---: | ---: |
| 製品 | 4 | 4 | 0 |
| 製造指図 | 21 | 20 | 1 |
| 受注 | 21 | 20 | 1 |
| 出荷 | 42 | 40 | 2 |
| 製造実績 | 46 | 41 | 3 |
| 停止履歴 | 21 | 20 | 1 |

原本155行は、Silverの145行、Quarantineの8行、重複・旧版の2行に分かれます。

工場集計の合計は計画2347、良品2154、不良62、生産2216、停止185分です。受注集計は受注1435、出荷1363、残72、納期後出荷132・4件です。fixtureはこれらの合計に加えて日別・受注別の詳細、最新改訂版、遅延到着日、不正文字列の保持も照合します。別パラメーターの生成結果にこの固定fixtureを当てないでください。

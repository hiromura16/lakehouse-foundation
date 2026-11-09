# 既存SQL Serverからランディングへ抽出する

`extract-sqlserver.py` は会社の既存DBを読み取り、MES・ERPの6つの入力契約に対応するCSV・JSON Linesを作ります。SQLは列を明示した `SELECT TOP (上限+1)` だけです。DB・表の作成、UPDATE・DELETE・DROP、CDCや権限の設定は行いません。ダミー投入用 `generate-demo.py` とは別のツールです。

このbridgeは全件スナップショット用です。差分watermark・CDC・ストリーミングは実装していません。SQL認証や読み取りの実行を伴わない `source-check` のTCP到達確認とは用途が異なります。

## 読み取り専用の接続設定

SQL接続には追加ドライバが必要です。ダミーSQL投入と同じ固定依存ファイルを使います。

```sh
python3 -m venv /srv/source-reader-venv
/srv/source-reader-venv/bin/python -m pip install -r requirements-demo.txt
```

導入先で、必要な表・ビューにSELECTだけを許可したログインを準備します。ツール自身は権限を作ったり変更したりしません。接続設定はリポジトリ外の非公開ファイルへ保存し、0600にします。

```dotenv
SOURCE_SQLSERVER_HOST=source-sql.example.invalid
SOURCE_SQLSERVER_PORT=1433
SOURCE_SQLSERVER_DATABASE=YOUR_SOURCE_DATABASE
SOURCE_SQLSERVER_USER=YOUR_READ_ONLY_LOGIN
SOURCE_SQLSERVER_PASSWORD=SET_LOCALLY
SOURCE_SQLSERVER_CA_FILE=system
SOURCE_SQLSERVER_TIMEZONE=UTC
```

`SOURCE_SQLSERVER_CA_FILE` はOSの信頼ストアを使う `system`、または社内CAのPEMファイルです。接続は暗号化を必須とし、CAと証明書ホスト名を検証します。ホストのFreeTDS設定を書き換えず、実行プロセス用の一時設定だけを使います。プロキシ用envと同じファイルを使っても、HTTPプロキシでSQLのTCP接続が中継されるわけではありません。

`SOURCE_SQLSERVER_TIMEZONE` はSQLの `datetime`・`datetime2` 等がタイムゾーン情報を持たない場合の解釈です。既定はUTCです。会社のDBが現地時刻を保存している場合は、そのIANAタイムゾーンへ必ず変更します。オフセット付き日時はその値を使ってUTCへ変換します。元から文字列の日時は文字列のままなので、入力契約のISO 8601形式を持つ列・既存ビューへマッピングしてください。夏時間の重複するローカル日時など、元の値だけでは時刻が一意に決まらないデータは、上流でオフセットを明確にする必要があります。

## テーブル・列を対応付ける

[sqlserver-tables.example.json](../infra/source/sqlserver-tables.example.json) をリポジトリ外へコピーし、実際のスキーマ・表・列に合わせます。内部のオブジェクト名も含むため、コピー先も0600にします。雛形の `YOUR_DEMO_SCHEMA` は、ダミーツールが作ったスキーマ名へ置き換えて接続確認するときにも使えます。

各エントリーの形は次のとおりです。以下は方向を説明する抜粋で、実際には6表すべての全契約列が必要です。

```json
{
  "tables": [
    {
      "target_table": "erp_products",
      "source_schema": "YOUR_SOURCE_SCHEMA",
      "source_table": "YOUR_PRODUCT_TABLE",
      "columns": {
        "ITEM_CODE": "product_id",
        "ITEM_NAME": "product_name"
      }
    }
  ]
}
```

`columns` は **会社の列名 → Bronzeの列名** です。各会社列は1つの契約列へ対応付け、契約列を省略・重複させません。[sources.json](../medallion/sources.json) の6表と全列が正本です。出力ファイル名と列順はこの契約で固定します。JSONへSQL文、WHERE句、式、Pythonコードを埋め込むことはできません。識別子は英数字・アンダースコアに限定し、SQL Serverの角括弧で引用します。特殊文字を含む既存名や複雑な型変換が必要なら、管理者が事前に用意した読み取り用ビューを対象にしてください。

## 抽出する

```sh
/srv/source-reader-venv/bin/python scripts/extract-sqlserver.py \
  --env-file /srv/private/site.env \
  --table-map /srv/private/sqlserver-tables.json \
  --output /srv/lakehouse/landing \
  --max-rows 100000
```

通常は既存の出力ファイル・ready・抽出manifestがあれば停止します。意図的な次の全件スナップショットには `--force` を追加します。削除対象はなく、指定先の6ファイル・そのready・`source-extract.json` だけを置き換えます。出力先はリポジトリ外に限定し、シンボリックリンクは拒否します。

各SELECTは上限より1行多く読める形にして件数を確認します。1表でも `--max-rows` を超えると、その実行からは1ファイルも公開しません。上限は1〜100000行です。6表すべての取得・型変換・契約検査が終わってから、一時ファイルから完成名へ切り替え、各 `<filename>.ready` にSHA-256を書きます。SQLの返却順に依存しないよう行順も固定するため、内容が変わらなければファイルのハッシュも同じです。

出力は `csv/erp_products.csv`、`csv/erp_work_orders.csv`、`csv/erp_sales_orders.csv`、`csv/erp_shipments.csv`、`json/mes_events.jsonl`、`json/mes_downtime.jsonl` です。数値は文字列、日付は `YYYY-MM-DD`、Pythonの日時型はUTC ISO 8601、Decimalは指数表記なしに変換します。SQL NULLは空文字になります。NULLと元の空文字を区別する必要がある場合は、この原本契約を拡張する別の対応が必要です。バイナリー等、契約外の値は公開前に拒否します。

`source-extract.json` は公開用の論理表名、件数、ハッシュ、抽出時刻だけを記録し、内部DB名・スキーマ・資格情報は含めません。コンソールの成功・失敗メッセージにも接続先や会社列名を出しません。

## 取り込み・変換を別に実行する

このツールはランディングまでで終了します。成功を確認してから、別の明示的な操作で取り込み・メダリオン更新を実行します。

```sh
./scripts/lakehouse.sh --env-file /srv/private/site.env medallion
```

各SELECTは通常の読み取り分離レベルで別々に実行し、6表を同じ時点へ固定するトランザクションはありません。表間の厳密な時点整合が必要なら、会社側の読み取り用スナップショットやレプリカを用意してください。ファイルの公開も1ファイル単位です。抽出中に別の定期取り込みを同時実行せず、抽出成功後に次工程を起動する順序で運用します。Silver・Quarantine・Goldの置換も表ごとであり、全体の一括トランザクションではありません。

同じ原本ハッシュなら取り込みはスキップされ、更新したファイルはそのファイル由来のBronze行を置き換えます。原本バケットにはハッシュ別に内容を保存します。削除行を含む全件スナップショットの扱いと、更新イベントを蓄積するCDCでは意味が異なるため、CDCへ移行するときは別の契約を定義します。

自己署名証明書のローカル検証DBだけは `--test-trust-server-certificate` を付けられます。loopback接続限定で、暗号化は必須のままです。会社の接続では証明書検証を省略しません。

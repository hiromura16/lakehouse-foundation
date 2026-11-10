# 既存SQL Serverからランディングへ抽出する

`extract-sqlserver.py` は会社の既存DBを読み取り、MES・ERPの6つの入力契約に対応するCSV・JSON Linesを作ります。SQLは列を明示した `SELECT TOP (上限+1)` だけです。DB・表の作成、UPDATE・DELETE・DROP、CDCや権限の設定は行いません。ダミー投入用 `generate-demo.py` とは別のツールです。

このbridgeは全件スナップショット用です。差分watermark・CDC・ストリーミングは実装していません。SQL認証や読み取りの実行を伴わない `source-check` のTCP到達確認とは用途が異なります。

## 実行する場所と事前確認

2026-11-10監査。会社SQL Serverの版・editionはこのリポジトリで固定しません。DBAが実際の版、読取対象、TLS名、抽出可能な時間と件数を確認します。抽出はレイクハウスホスト上の**通常の配置利用者**が、cloneしたリポジトリのルートで行います。DBの権限準備はDBAがSQL Serverへ接続して別途行います。

| 段階 | 担当と操作 | 確認する結果 |
| --- | --- | --- |
| DB準備 | DBAが既存login/userと承認した6表／viewを確認 | 対象だけのSELECTが可能。書込み・管理権限は不要 |
| host準備 | 配置利用者がuv環境、private設定、mappingを用意 | envとJSONがrepo外0600、landingがそのUID/GIDで書込み可能 |
| 到達性 | `lakehouse.sh source-check` | TCP接続成功。SQL認証やTLS成功とは区別 |
| 抽出 | 下記extractorを1回実行 | 終了コード0、6素材とready、抽出manifest |
| 下流確認 | 同時writerがいない状態で明示 `medallion` | Bronze/Silver/隔離/Goldの件数を業務期待値と照合 |

`read_only=True` というclient指定だけをDB側の権限制御の代わりにしません。DBAが既存userへ対象object/列のSELECTを付与し、会社規定に合わせて監査します。[Microsoftの権限モデル](https://learn.microsoft.com/en-us/sql/relational-databases/security/permissions-database-engine?view=sql-server-ver17)

## 読み取り専用の接続設定

SQL接続には追加ドライバが必要です。ダミーSQL投入と同じ固定依存ファイルを使います。

```sh
UV_PYTHON_DOWNLOADS=never uv venv --python /usr/bin/python3.12 \
  "$HOME/.venvs/lakehouse-source-reader"
uv pip sync --python "$HOME/.venvs/lakehouse-source-reader/bin/python" \
  requirements-demo.txt
```

ホストのPython3.12は事前に導入済みのものを指定します。uv0.13.7と `pymssql==2.4.6` が対象です。`requirements-demo.txt` は版固定入力で、host用のfull hash lockではありません。proxy/CAが必要なら [会社proxy手順](COMPANY-PROXY.md) を先に実施します。依存同期後に次で版を確認します。

```bash
"$HOME/.venvs/lakehouse-source-reader/bin/python" -c \
  'import pymssql; print(pymssql.__version__)'
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

この設定を既存の0600 `site.env` へ追記するか、SOURCE専用の外部0600ファイルへ保存します。extractorへ渡すenvにはSOURCE項目だけでも構いませんが、`lakehouse.sh` には生成したサイト全体の設定が必要です。

`SOURCE_SQLSERVER_CA_FILE` はOSの信頼ストアを使う `system`、または社内CAのPEMファイルです。接続は暗号化を必須とし、CAと証明書ホスト名を検証します。ホストのFreeTDS設定を書き換えず、実行プロセス用の一時設定だけを使います。プロキシ用envと同じファイルを使っても、HTTPプロキシでSQLのTCP接続が中継されるわけではありません。

SOURCE extractorは接続hostと証明書名が一致するDNSを使います。DEMO用 `DEMO_SQLSERVER_HOSTNAME_IN_CERTIFICATE` はSOURCEには適用されません。名前不一致ならDBA/DNS担当と接続名・証明書を修正します。[FreeTDSのCAとhostname検証](https://www.freetds.org/userguide/freetdsconf.html)

`SOURCE_SQLSERVER_TIMEZONE` はSQLの `datetime`・`datetime2` 等がタイムゾーン情報を持たない場合の解釈です。既定はUTCです。会社のDBが現地時刻を保存している場合は、そのIANAタイムゾーンへ必ず変更します。オフセット付き日時はその値を使ってUTCへ変換します。元から文字列の日時は文字列のままなので、入力契約のISO 8601形式を持つ列・既存ビューへマッピングしてください。夏時間の重複するローカル日時など、元の値だけでは時刻が一意に決まらないデータは、上流でオフセットを明確にする必要があります。

## テーブル・列を対応付ける

配置利用者が次のようにコピーし、ローカルのエディターで編集します。保存先directoryは0700にします。既存の対応表は上書きせず保持します。

```bash
TABLE_MAP=/absolute/private/sqlserver-tables.json
if [ -e "$TABLE_MAP" ] || [ -L "$TABLE_MAP" ]; then
  stat -c '%a %u:%g %n' "$TABLE_MAP"
else
  install -m 0600 infra/source/sqlserver-tables.example.json "$TABLE_MAP"
fi
```

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

出力先をprivate `LANDING_DIR` と揃えます。以下は既定pathの例です。抽出中はn8n/Airflow/CLI/GUIから同じ素材を取り込まないよう、定期開始を止めます。

```sh
"$HOME/.venvs/lakehouse-source-reader/bin/python" scripts/extract-sqlserver.py \
  --env-file /absolute/private/site.env \
  --table-map /absolute/private/sqlserver-tables.json \
  --output /srv/lakehouse/landing \
  --max-rows 100000
```

通常は既存の出力ファイル・ready・抽出manifestがあれば停止します。意図的な次の全件スナップショットには `--force` を追加します。削除対象はなく、指定先の6ファイル・そのready・`source-extract.json` だけを置き換えます。出力先はリポジトリ外に限定し、シンボリックリンクは拒否します。

各SELECTは上限より1行多く読める形にして件数を確認します。1表でも `--max-rows` を超えると、その実行からは1ファイルも公開しません。上限は1〜100000行です。6表すべての取得・型変換・契約検査が終わってから、一時ファイルから完成名へ切り替え、各 `<filename>.ready` にSHA-256を書きます。SQLの返却順に依存しないよう行順も固定するため、内容が変わらなければファイルのハッシュも同じです。

出力は `csv/erp_products.csv`、`csv/erp_work_orders.csv`、`csv/erp_sales_orders.csv`、`csv/erp_shipments.csv`、`json/mes_events.jsonl`、`json/mes_downtime.jsonl` です。数値は文字列、日付は `YYYY-MM-DD`、Pythonの日時型はUTC ISO 8601、Decimalは指数表記なしに変換します。SQL NULLは空文字になります。NULLと元の空文字を区別する必要がある場合は、この原本契約を拡張する別の対応が必要です。バイナリー等、契約外の値は公開前に拒否します。

成功後は `source-extract.json` の6表の件数・hashを確認し、各 `.ready` が対応するファイルhashと一致することを確認します。異常終了時は新しい成功manifestがあると推測せず、前回のファイルと時刻を区別してから再試行します。

`source-extract.json` は公開用の論理表名、件数、ハッシュ、抽出時刻だけを記録し、内部DB名・スキーマ・資格情報は含めません。コンソールの成功・失敗メッセージにも接続先や会社列名を出しません。

## 取り込み・変換を別に実行する

このツールはランディングまでで終了します。成功を確認してから、別の明示的な操作で取り込み・メダリオン更新を実行します。

```sh
./scripts/lakehouse.sh --env-file /absolute/private/site.env medallion
```

各SELECTは通常の読み取り分離レベルで別々に実行し、6表を同じ時点へ固定するトランザクションはありません。表間の厳密な時点整合が必要なら、会社側の読み取り用スナップショットやレプリカを用意してください。ファイルの公開も1ファイル単位です。抽出中に別の定期取り込みを同時実行せず、抽出成功後に次工程を起動する順序で運用します。Silver・Quarantine・Goldの置換も表ごとであり、全体の一括トランザクションではありません。

同じ原本ハッシュなら取り込みはスキップされ、更新したファイルはそのファイル由来のBronze行を置き換えます。原本バケットにはハッシュ別に内容を保存します。削除行を含む全件スナップショットの扱いと、更新イベントを蓄積するCDCでは意味が異なるため、CDCへ移行するときは別の契約を定義します。

自己署名証明書のローカル検証DBだけは `--test-trust-server-certificate` を付けられます。loopback接続限定で、暗号化は必須のままです。会社の接続では証明書検証を省略しません。


## 切り分けと一次資料

| 失敗する段階 | 次に確認するもの |
| --- | --- |
| TCP到達確認 | DB用DNS、port、route、FW。HTTP proxyでは代用しない |
| login／TLS | SOURCE資格情報、CA chain、期限、接続hostの証明書名 |
| SELECT | DBAが承認したschema/table/column、object/列のSELECT権限 |
| 契約／件数 | mapping6表、型、1表上限100000、NULLと空文字の扱い |
| ファイル公開 | landingの所有者・親の通過権限・空容量、既存素材の有無 |

仕様の正本は [extract-sqlserver.py](../scripts/extract-sqlserver.py)、[sources.json](../medallion/sources.json)、`pymssql==2.4.6` の固定依存です。公式の [pymssql2.4.6 API source](https://github.com/pymssql/pymssql/blob/v2.4.6/docs/ref/pymssql.rst) と [FreeTDS TLS設定](https://www.freetds.org/userguide/freetdsconf.html) を採用版に照合します。SQL ServerのCDC有効化やAgent権限はこの読取bridgeに不要で、継続取り込みを選ぶ場合だけ [CDC.md](CDC.md) へ進みます。

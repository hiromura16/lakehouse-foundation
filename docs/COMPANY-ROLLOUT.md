# 会社への導入・受入ガイド

<!-- doc-purpose:start -->
> **役割:** 会社側の担当分担、Samba、外部SQL、業務照合と導入受入条件を決める。
>
> **対象読者:** 基盤構築担当 / データ利用担当 / 評価・報告担当
>
> **読むタイミング:** 新規構築の手順に会社固有の準備と業務側の受入を組み合わせるとき。
>
> **前提となる資料:** [初めて構築する人の一本道ガイド](<START-HERE.md>)
>
> **次に読む資料:** [会社のプロキシ・CAを設定する](<COMPANY-PROXY.md>) / [既存SQL Serverからランディングへ抽出する](<SOURCE-SQLSERVER.md>) / [PoCの評価計画と受入条件](<reports/EVALUATION-PLAN.md>)
>
> **文書一覧:** [目的・読者から探す文書マニフェスト](<DOCUMENTATION-MANIFEST.md>)
<!-- doc-purpose:end -->

<!-- shell-usage:start -->
**コマンドのシェル:** 「Bash / Zsh共通」は同じ内容を使い、専用例は自分のシェルを選びます。[詳しい使い分け](SHELLS.md)。

Zshの対話端末では、コメント付きの例を貼る前に一度だけ次を実行します。Bashでは不要です。

**Zsh**

```zsh
setopt INTERACTIVE_COMMENTS
```
<!-- shell-usage:end -->

確認日: 2026-11-10（JST）。新規の構築順は **[START-HERE.md](START-HERE.md)** が正本です。このページは会社で必要な担当分担、Samba、実SQL Serverへの接続、業務データの照合を補足します。導入先のIP・ホスト名・proxy・実表名・資格情報はprivate記録へ保存し、公開文書へ転記しません。

## 1. 共通準備と配置の入口

| 準備 | 実施・確認する担当 | 手順 |
| --- | --- | --- |
| サーバー・Docker権限・永続ディスク・空port | ホスト管理者 | [初回準備](START-HERE.md) |
| daemon/build/runtime proxy、企業CA、許可宛先 | ネットワーク担当 | [企業proxy手順](COMPANY-PROXY.md) |
| private設定0600/0700、実行UID、landing所有者 | 配置担当 | [設定一覧](CONFIGURATION.md) |
| Dokployの導入→公開Git→サービスEnvironment→Deploy | 配置担当 | [固定版Dokploy手順](DOKPLOY.md) |
| データ契約・実表/列map、read-only権限、時刻定義 | DBA・データ担当 | [SQL抽出](SOURCE-SQLSERVER.md) |
| バックアップ・復元・GUI管理者・実行時刻 | 運用担当 | [運用](OPERATIONS.md) |

初回はcoreの往復検証を済ませ、次にdemo/monitoringを加え、追加profileを一つずつ有効にします。採用版・資源上限は [互換性](COMPATIBILITY.md)・[配置](DEPLOYMENT.md) を確認します。会社固有のVM容量から全機能同時起動を保証しません。

Dokploy管理かCLI管理かを先に決めます。**Dokploy導入はレイクハウスの初回Deployより先**です。先にCLIで同じ環境を立ち上げる必要はありません。既存のCLI環境を引き継ぐ場合は、別DBを作らないため [COMPANY-UPDATE.md](COMPANY-UPDATE.md) のwriter停止・確定appName・volume prefixの手順を使います。

閉域PoCは共有資格情報・単一ホストの構成です。利用者別認可、会社SQLのCDC、IdP、冗長化、復元、モデル推論は各担当が別途受入します。Dokployの初期環境名 `production` は本番要件の充足を示すものではありません。

## 2. ランディングを用意し、必要ならSambaを設定する

以下の例は新規の専用ディレクトリです。別の場所を使う場合は、private設定の `LANDING_DIR` と同じ完成した絶対パスへ置き換えます。

**Bash / Zsh共通**

```bash
LANDING_PATH=/srv/lakehouse/landing
if [ -e "$LANDING_PATH" ]; then
  stat -c '%a %u:%g %n' "$LANDING_PATH"
else
  sudo install -d -m 0770 -o "$(id -un)" -g "$(id -gn)" "$LANDING_PATH"
fi
```

既存ディレクトリがある場合は、その所有者と用途を確認し、所有権を一括変更しません。上の `LANDING_PATH` は説明用のシェル変数で、`site.env` では変数展開されません。envには実際の絶対パスを書きます。workerはこの場所を `/landing` として読み取り、入力を書き換えません。

Sambaを使う場合は、会社標準の手順でSambaと `testparm` / `pdbedit` を用意します。新規Ubuntuでのpackage・アカウント準備は [HOST-SETUP](HOST-SETUP.md#5b-新規ubuntuでsambaを使う場合だけ準備する) にあります。共有に使うUnixユーザーとSambaアカウントは管理者が準備し、既存ユーザーのパスワードを再設定しないようにします。共有先はDockerやS3の内部volumeではなく、このランディングです。

**Bash**

```bash
SMB_SHARE_USER="$(id -un)"
read -r -p '許可する接続元CIDRを入力: ' SMB_ALLOWED_CIDR
python3 scripts/setup-samba.py \
  --user "$SMB_SHARE_USER" --allow "$SMB_ALLOWED_CIDR" \
  --path "$LANDING_PATH" --plan
```

**Zsh**

```zsh
SMB_SHARE_USER="$(id -un)"
read -r 'SMB_ALLOWED_CIDR?許可する接続元CIDRを入力: '
python3 scripts/setup-samba.py \
  --user "$SMB_SHARE_USER" --allow "$SMB_ALLOWED_CIDR" \
  --path "$LANDING_PATH" --plan
```

planが対象共有と明示allowlistを示したら、管理者が同じ引数で適用します。

**Bash / Zsh共通**

```bash
sudo python3 scripts/setup-samba.py \
  --user "$SMB_SHARE_USER" --allow "$SMB_ALLOWED_CIDR" \
  --path "$LANDING_PATH" --apply
```

共有名の既定は `Lakehouse-Landing`。専用includeだけを追加し、既存共有や認証情報を変更しません。同名共有や曖昧なincludeは拒否します。適用時はbackup→testparm→smbd reloadを行い、失敗時は設定を戻します。許可されたクライアントから認証して、新しいファイルの作成・読み取り・削除を確認してください。これは後述のcore verifyとは別の受入項目です。

手動投入するファイルはコピー完了後に `.ready` を作成します。

**Bash / Zsh共通**

```bash
python3 scripts/mark-ready.py "$LANDING_PATH/csv/erp_products.csv"
```

取り込み可能な業務ファイルの名前・列は [sources.json](../medallion/sources.json) が正本です。任意のCSV名を置くだけで業務のSilver/Goldが自動生成される仕組みではありません。

## 受入を「基盤」と「業務」の二つに分ける

初めての担当者はcoreのhealthyやverify成功を先に確認します。業務担当・DBAはその後、同じ抽出範囲で件数・版・数量・時刻を照合します。どちらか一方だけでは会社データを利用者へ公開する判断になりません。

| 判定点 | 基盤担当の確認 | 業務/DB担当の確認 |
| --- | --- | --- |
| source接続 | DNS/TCP、TLS、read-only認証 | 対象DB/表/期間、読み取り負荷・時間帯 |
| 抽出完了 | 6ファイル/ready/manifest、上限・形式 | 6SELECTが別時点である影響、元の件数との一致 |
| Bronze | 原本hash、取り込んだ相対path、再投入skip | 文字列の欠損や列対応、想定する改訂の有無 |
| Silver/隔離 | 型変換と理由code、最新候補の規則 | 主キー、更新順、親参照、隔離を直す担当 |
| Gold | 14SQLの完了、表の読戻し | 粒度・UTC日・数量・金額・分母、同じ範囲の照合 |
| 利用開始 | GUI認証、入口、保存/backup、担当 | どの未確認を残して利用してよいか |

### 会社の列を6契約へ対応付ける前に決める

| 契約上の項目 | 会社側で決める内容 |
| --- | --- |
| 業務主キー | 製品/指図/受注/出荷/イベント/停止が一意になる列。表示名や行番号で代用しない |
| updated_at | 同じキーの版を比較できる時刻。ロード時刻と混同しない |
| 日付・時刻 | UTC、offset付き、現地時刻のどれか。SOURCE_SQLSERVER_TIMEZONEは元の保存方式に合わせる |
| 数量・単価 | 単位、桁、NULL、負数、取消、単価の通貨。標準SQLで表現できない場合はルール追加 |
| 親参照 | MES実績→指図、出荷→受注等の関係。部署別の同じ番号が衝突しないか |
| 削除・全件の意味 | 抽出ファイルの置換とDB DELETEイベントを区別。後者はCDCの別経路 |

mapping JSONは列名の対応です。任意の計算式やWHERE句で抽出範囲を補う仕組みではありません。型・識別子・業務変換が合わない場合は、DBA管理の読み取り用ビューか、契約/SQL変更として設計します。6表すべてを一つの時点へ固定したい場合も、上流snapshot等の方法を先に決めます。

### 小さい照合から全範囲へ進む

最初はDBAが特定できる少数の指図・受注を選び、対応する子実績/出荷も含む読み取り範囲を用意します。抽出の `--max-rows` は1表の上限で、集計対象を意味するfilterではありません。上限超過を避けるため値を上げる前に、受入範囲と読取用ビューを見直します。

1. 同じ対象のsource件数と抽出manifestを比較する。
2. 主キーの改訂・重複がある場合、Bronzeの行数がSilverへどう集約されるかを説明する。
3. 隔離表の理由と元文字列を確認し、意図した業務例外か不正かを担当者が判断する。
4. 工場日別・受注別のGoldを、同じ日付/単位/範囲の独立集計と比較する。
5. 再抽出・再変換と、実際の修正/削除の経路を確認してから範囲を広げる。

合成runは通常のBronzeに残るため、会社用の新環境へ混在させません。固定fixtureの155/145/8行やGold合計を、会社データの期待値として使わないでください。

## 3. 会社SQL Serverを読み取り専用でつなぐ

**GUIで合成データを試した環境とは別の、空の会社データ用環境を準備します。** 新しいCompose/appName、volume、private設定、landingと空きportを使い、同じ手順でcoreを配置します。会社用環境では通常のダミー生成を行わず、隔離namespaceの `verify` だけを実行します。GUI runのBronzeはファイルの相対path単位で残るため、landingの削除や会社CSVの追加では過去の合成行を消せません。既存DBを初期化して切り替える操作は本手順に含めません。

ここからはその会社用の `LAKEHOUSE_ENV_FILE` と `LANDING_PATH` を指定して、通常ユーザーで実行します。

DB担当が対象表/ビューへのSELECT権限を持つログインを用意します。`site.env` の `SOURCE_SQLSERVER_*` に接続先、DB、読み取り用利用者、TLSのCAを記入します。タイムゾーンのない日時型を現地時刻で保存している場合は `SOURCE_SQLSERVER_TIMEZONE` も合わせます。

**Bash / Zsh共通**

```bash
./scripts/lakehouse.sh source-check
```

これは名前解決とTCP到達確認までです。成功してもSQL認証、TLS、SELECT、CDCの成功とは扱いません。実際の抽出は専用のホストvenvを用意して行います。

**Bash / Zsh共通**

```bash
uv venv --python 3.12 \
  "$HOME/.local/share/lakehouse-foundation/source-reader-venv"
uv pip sync \
  --python "$HOME/.local/share/lakehouse-foundation/source-reader-venv/bin/python" \
  requirements-demo.txt
TABLE_MAP="$(dirname "$LAKEHOUSE_ENV_FILE")/sqlserver-tables.json"
if [ -e "$TABLE_MAP" ] || [ -L "$TABLE_MAP" ]; then
  stat -c '%a %u:%g %n' "$TABLE_MAP"
else
  install -m 0600 infra/source/sqlserver-tables.example.json "$TABLE_MAP"
fi
```

コピーしたJSONをローカルで編集し、6表の会社列名を契約列へ一対一に対応付けます。SQL式や任意WHERE句を入れる形式ではありません。必要な変換や特殊な識別子は、DB担当が準備した読み取り用ビューで解決します。既存の私有対応表がある場合は上のcopyで上書きせず、そのファイルを継続して使います。

**Bash / Zsh共通**

```bash
"$HOME/.local/share/lakehouse-foundation/source-reader-venv/bin/python" \
  scripts/extract-sqlserver.py \
  --env-file "$LAKEHOUSE_ENV_FILE" \
  --table-map "$TABLE_MAP" \
  --output "$LANDING_PATH" --max-rows 100000
```

抽出はSELECTだけで、DB変更・CDC有効化・後続処理の起動をしません。6表全体の取得・形式検査に成功した後、6ファイル、各ready、`source-extract.json` を公開します。各SELECTの時点は別で、6表を同一時点へ固定するtransactionではありません。厳密な時点整合が必要なら、上流で読み取り用snapshot等を用意します。

初回は既存ファイルを上書きしません。次の全件snapshotを意図的に置き換える場合だけ `--force` を使います。抽出中にn8n/Airflow等から取り込みを同時起動せず、抽出成功→変換の順序にします。件数上限、TLS、NULL、日時の詳細は [SOURCE-SQLSERVER.md](SOURCE-SQLSERVER.md) を参照してください。DEMO設定は別の専用DBへの合成書き込みであり、ここでは使いません。

## 4. 会社データのメダリオンを検証する

**Bash / Zsh共通**

```bash
./scripts/lakehouse.sh medallion
```

この操作でBronze取り込み→Silver→Quarantine→Goldを実行します。同じファイルhashならスキップし、内容更新時はそのファイル由来のBronze行を置き換えます。SQLは表単位のatomic replaceで、全Goldを一括transactionで入れ替える構成ではありません。

会社の受入では、抽出manifestの件数とBronze、Silver/Quarantineへの振り分け、Goldの数量・金額を、DB担当が用意した同じ範囲の集計と照合します。`verify` の合成期待値を会社データの期待値として流用しません。

TrinoのCLIで読み取り確認する例です。先に [運用の補助関数](OPERATIONS.md#postgresqlの論理dump例) の `lakehouse_compose` を同じrepoルートで定義してください。7Composeと正規化したprivate設定を使い、shellに残る別project名の影響を避けます。CLIは公式Trino imageに同梱されています。

**Bash / Zsh共通**

```bash
lakehouse_compose exec -T trino trino --execute \
  'SELECT count(*) FROM iceberg.gold.factory_daily_production'
```

`iceberg.gold.sales_fulfillment`、`iceberg.quarantine.erp_products` 等も同様に確認します。件数や歩留まりの期待値は導入先の業務条件で決め、合成データの固定件数で受入しません。[DEMO-DATA.md](DEMO-DATA.md) とSQLのコメントを使って、日付・集計粒度・改訂優先の意味を確認します。

## 5. 追加機能と公開しない設定

[初回ガイドの追加機能表](START-HERE.md#6-必要な機能を一つずつ追加する) からSpark・Jupyter・n8n・Airflow・Superset・Qdrant・Marquez・MCPを順に確認します。profileを増やす時は既存の一覧を保持し、Dokploy管理ではprivate設定と**Composeサービス自身のEnvironment**へ同じ変更を反映してDeployします。CLI管理だけが `plan`→`up` を使います。

n8nとAirflowの定期実行を二重にしません。設定GUIの保存と適用は別です。企業CA・外向きproxy・n8nのSSRF例外も保存後に再配置が必要です。[SSRF](SSRF.md) はNO_PROXYとは別の接続許可設定です。

会社SQLのCDCは [CDC.md](CDC.md) のDBA前提と専用権限を先に準備します。GUIの疑似データ投入はDEMO用の別DBだけを対象にし、SOURCE業務DBに書込みません。通常のダミー生成は新規schemaへ素材をINSERTする方式で、CDC用の固定INSERT/UPDATE/DELETEとは別です。

## 6. 受入結果と引き継ぎを残す

| 確認項目 | 受入の根拠 |
| --- | --- |
| ホスト/設定 | commit、profile、設定検査成功、選んだportとvolumeの対応を私有記録へ保存 |
| proxy/CA | daemon pull、build、外部API、内部S3/カタログを別々に確認 |
| core | healthy、隔離verifyの成功、再実行結果 |
| Samba使用時 | 許可クライアントの認証、ファイル作成・読込・削除、readyの扱い |
| 会社SQL | read-only認証/TLS、6表の列対応、抽出manifest、実行時間帯 |
| 業務変換 | Silver/Quarantine/Goldの会社側照合と、異常行の理由の確認 |
| GUI/監視使用時 | 認証、手動実行、失敗表示、Grafana GUI保存の確認 |
| 継続運用 | private env/暗号鍵/volumeのbackup先、復元試験、担当と停止・再開手順 |

閉域検証の受入と、本番の運用承認を分けて記録します。復元試験が未実施ならそのまま未実施とし、RPO/RTOや無停止性を確定しません。公開の [VALIDATION.md](VALIDATION.md) は実装検証の参考で、会社環境の証跡を代替しません。

引き継ぐものは、Git commit、私有設定の保管先、秘密値を含まない接続図、volume一覧、担当者が確認した受入結果です。private env、会社の表名対応、業務データ、ログの生出力は公開Gitへ戻しません。

## 途中で失敗したとき

| 症状 | 次に確認するもの |
| --- | --- |
| `plan` が秘密ファイル/port/keyで拒否 | [CONFIGURATION.md](CONFIGURATION.md)。0600、リポジトリ外、追加キー、port重複 |
| image pullだけ失敗 | daemonのproxy/CA/レジストリ許可。[PROXY.md](PROXY.md) |
| build中のpip/JAR取得だけ失敗 | BuildKit args、実際の取得先、CAとchecksum。版を外して回避しない |
| workerがlandingを読めない | `LANDING_DIR` の実体、UID/GID、親の通過権限、ready/hash、契約列 |
| SQLのTCPだけ成功して抽出失敗 | read-onlyログイン、TLSのCA/ホスト名、全6表のmapping、件数上限 |
| HTTP 409でworkerがblocked | Trinoで残存queryを確認する。取消不能の状態なので盲目的にジョブを再試行せず、確認後にworkerを再起動する |
| データが空に見える | project/appName/volume prefix/bucket/Warehouseの対応。volume削除や再投入より先に確認 |
| 画面は起動したが業務数値が違う | 日時、主キー、更新優先、NULL、表間snapshot時点、集計範囲を照合 |

CLI管理の停止は `./scripts/lakehouse.sh down`、Dokploy管理の停止はGUIのStopで、名前付きvolumeは保持します。障害調査中にvolume削除やpruneを復旧手順として実行しません。通常のログ・停止・再開の具体操作は [DEPLOYMENT.md](DEPLOYMENT.md) を参照してください。


SSRFの設定値、内部workerの例外、NO_PROXYとの違い、GUI保存と再配置、許可・拒否の受入は [SSRF.md](SSRF.md) を参照してください。

## 運用へ渡す前に担当者が説明できること

設定を保存する人とDeployする人、sourceの不正行を直す人、n8n/Airflowの実行担当、blocked queryを確認する人、バックアップ/復元を行う人を決めます。各担当は「どの設定と保存物を使うか」「失敗時に何を再実行してよいか」「未受入は何か」を説明できる状態にします。

DB資格情報や暗号鍵、会社の対応表、GUI exportの原本、Notebook outputを公開repoへ戻しません。報告書には確認結果と一般化した図を載せ、機密を含む証跡の管理先は会社の私有記録で引き継ぎます。[受入の評価票](reports/EVALUATION-PLAN.md)・[証跡対応表](reports/EVIDENCE-MATRIX.md)・[運用](OPERATIONS.md)

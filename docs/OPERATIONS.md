# 平時・更新・復旧の運用手順

2026-11-10監査。この文書は単一ホスト構成の手作業runbookです。自動バックアップ、無停止更新、DB migrationの自動巻き戻しは実装していません。導入時は [START-HERE.md](START-HERE.md)、設定値は [CONFIGURATION.md](CONFIGURATION.md)、画面操作は [DEMO-GUI.md](DEMO-GUI.md) を参照します。

## 操作する場所と管理の正本

レイクハウスを動かすホストのrepoルートで、通常の配置利用者がprivate設定を選びます。Docker操作権が必要です。daemon/Samba/Dokployホスト設定の変更だけ管理者が別途行います。CLIかDokployのどちらで配置を管理するか決め、同じstackを別名で同時起動しません。`COMPOSE_PROJECT_NAME`、Dokployの `appName`、利用中volumeの対応を運用台帳に残します。

本書のCLI起動停止例はCLI管理用です。Dokploy管理では同じ既存アプリの操作に読み替えます。backupのexec等をCLIで行う場合も、同じcommit・appName・private env・7Composeを使う保守作業として管理者が統一します。

## 毎日の確認

1. Dokployで対象Project・Environment・アプリを選び、配置したcommitとサービスのhealthを確認します。
2. Streamlitの「状態」でAPI・workerへの到達性を確認します。任意サービスを使わない環境では、そのサービスが停止中でも異常とは限りません。
3. 実行中ジョブを確認してから素材を投入します。結果の件数・run ID・manifestを記録します。
4. 必要なときだけ「処理・検証」から取り込みやメダリオンを実行し、結果を確認します。
5. Grafanaで資源・サービスの状態を確認し、業務データの件数・quarantineもSQLで確認します。コンテナがhealthyであることだけで、データ品質が正常とは判断しません。

同じ処理をn8n・Airflow・GUI・CLIから同時に開始しません。GUI API内では同時処理を制限しますが、外部のCLIやスケジューラーまで一つの分散ロックで排他する構成ではありません。定期実行の所有者を一つに決めます。

CLIを使うときは同じcommit、private env、Composeプロジェクトへ向けます。

```bash
export LAKEHOUSE_ENV_FILE="/absolute/private/site.env"
./scripts/lakehouse.sh status
./scripts/lakehouse.sh plan
```

## 設定を保存して反映する

GUIの「設定を保存」はprivateファイルの保存です。稼働中コンテナの環境変数、mount、API token、workerの設定はその場で切り替わりません。「再配置待ち」を確認し、次の順に反映します。

1. 変更項目・理由・担当・反映する環境を記録します。秘密値自体は記録へ貼り付けません。
2. private設定を安全にバックアップします。ファイル0600、専用ディレクトリ0700を維持します。
3. Dokployを正本に配置する場合は、そのEnvironmentの非公開envと、GUIが保存したprivateファイルを整合させます。正規化された `LAKEHOUSE_NO_PROXY` を含むbypass3項目も引き継ぎます。UIの設定を別の古いenvで上書きしないようにします。
4. `plan`で構成を確認し、実行中ジョブを終了させてから同じアプリを再配置します。
5. health、GUI/API認証、データ件数、再配置待ち表示を確認します。

`LANDING_DIR`、UID/GID、設定ディレクトリ、プロジェクト名、データprefixの変更ではmountやデータの選択先も確認します。ディレクトリを変更して設定を保存するだけで、実データが移動することはありません。

DBパスワード・S3資格情報・カタログ暗号鍵・n8n暗号鍵・Airflow Fernet keyを単独で再生成しません。既存DBの資格情報変更や暗号鍵のローテーションは、各製品の手順と復元試験を伴う別作業です。

## GUIで編集したものの正本

| 対象 | 正本・保持するもの |
| --- | --- |
| Compose・Python・SQL・依存 | 確認済みGit commitとlock |
| 接続先・秘密値 | 環境専用のprivate設定／Dokployの非公開env |
| Grafana dashboard | GUIで保存したGrafana DB。初回JSONはテンプレート |
| n8n workflow・credentials | n8n DBとその暗号鍵 |
| Airflow connection | Airflow DBとFernet key。初期管理者情報はstateにも保持 |
| ランディング | ホストの専用ディレクトリと`.ready`／manifest |
| Iceberg | カタログDB・カタログ暗号鍵・S3内のmetadata／dataを一組として保持 |

Grafanaの再起動・再初期化では、既存UIDを上書きしません。GUI変更をレビュー用JSONへ出す方法は [GRAFANA.md](GRAFANA.md) を参照します。JSON exportだけでGrafanaのユーザー・権限・全設定を復元できるとは扱いません。

## 停止と再開

通常停止ではvolumeを保持します。

```bash
./scripts/lakehouse.sh down
```

Dokployで管理している場合は対象アプリの停止操作を使い、CLIと別の名前のアプリを新規起動しません。停止前には次を行います。

1. GUIの新規受付、n8nの定期実行／Webhook、Airflow DAG等の新規開始を止めます。
2. ソースの有限streamを取消し、実行中batchの終了を待ちます。処理・検証ジョブは途中取消を前提にしません。
3. 外部CLI、Spark、SQL利用者、CDC経路からも書き込みがないことを確認します。
4. DB・S3等を止める前に、必要な処理が完了したことを確認します。

再開は同じprivate設定、プロジェクト名、データprefix、暗号鍵で行います。

```bash
./scripts/lakehouse.sh up
./scripts/lakehouse.sh verify
```

`verify`は専用の合成試験を行います。業務メダリオンの正しさ、会社SQL Server、会社proxy、Sambaクライアント、バックアップ復元を全て保証するものではありません。

## 既存データを別の配置元へ引き継ぐ

`COMPOSE_PROJECT_NAME`／Dokployの`appName`は実行環境の識別子です。`DATA_VOLUME_PREFIX`は既存の物理volume名を選ぶための移行用設定で、通常は空欄にします。[DOKPLOY.md](DOKPLOY.md#既存volume名を引き継ぐ場合)

移行前に全volumeの解決名を既存の実名と照合し、旧コンテナを全て停止してから一つの新配置を起動します。prefixはvolumeをrename・copyしません。誤ったprefixで空のDBが見えた場合は初期化を進めず、設定と実体を確認します。別環境やPreviewへ同じprefixを共有しません。

volume定義は全7Composeで16キーです。core7、compute2、Airflow2、CDC2、PoC3のうち利用中のものと、landing/private設定/notebook等のbind mountを含め、実際の保存先を確認します。[全キー一覧](CONFIGURATION.md#data_volume_prefixを使う場面)

## 更新とロールバック

既存サイトのprivate設定は、通常の配置利用者が `python3 scripts/configure.py --output /absolute/private/site.env --upgrade` で更新します。不足キー追加とbypass3項目正規化を行い、既存の他の値は保持します。変更前は隣の `.before-upgrade`（0600）に控えます。既存控えは上書きしないのでprivateな別名へ保管してから次の変更を行います。変更不要ならno-opです。GUIの保存だけでEngine・image・mountは更新されません。[会社VM更新](COMPANY-UPDATE.md)

更新前に、現在のcommit、イメージdigest、private設定の版、利用中profile、実volume名、復元点を記録します。変更先は別環境で `plan`、health、往復検証、メダリオン、必要なSpark／Airflow／CDCの試験を通してから本環境へ反映します。

アプリの更新でDB schemaが変わる場合があります。古いcommitへ戻すだけではDBが元に戻らないため、各製品がそのDB schemaのdowngradeを保証しているか確認します。保証できない場合は、更新前の一組のバックアップを別環境へ復元して切り戻します。稼働中volumeへ古いDBイメージを試しに接続しません。

新しい構成に切り替えた後も旧アプリを同じデータへ再起動しません。復旧判断が済むまで旧volumeと設定を保持します。

## バックアップを取る

### 整合する停止点を決める

DBだけ、S3だけ、暗号鍵だけの保管では復旧できません。Icebergへの書き込み、ソース生成、定期実行、CDC bridge等を止め、同じ停止点のデータ一式を保管します。メダリオンは表ごとの置換であり、複数表をまとめたトランザクションではありません。途中実行を避けます。

| 保管対象 | 一緒に保持するもの |
| --- | --- |
| Lakekeeper PostgreSQL | S3内Iceberg metadata/data、カタログ暗号鍵、warehouse情報 |
| SeaweedFS | storage volume全体。オブジェクトだけでなくfiler等のメタデータも対象 |
| n8n | 専用PostgreSQL、n8n volume、暗号鍵 |
| Airflow | 専用PostgreSQL、state volume、Fernet/JWT鍵、DAGのcommit |
| Grafana | 停止したGrafana volume、設定、必要なプラグイン |
| Prometheus | 停止したTSDB volumeと設定 |
| ランディング | CSV／JSON／画像、完了マーカー、manifest |
| CDC有効時 | Kafkaデータ・Connect offset/config/status・schema history、CDC状態、private connector設定 |
| compute有効時 | Spark event log/work volume、UID/GIDとjob commit。未完了jobの自動再実行はしない |
| PoC有効時 | Superset metadata DBとSECRET_KEY、Qdrant volumeと鍵、Marquez DB、Notebook host dirと認証 |
| 共通 | private設定、利用したGit commit、digest/lock、実volume名の対応表 |

GUI/API、ソース投入、カタログサーバー、Trino、Spark、n8n、Airflow、CDC等のwriterを止めます。論理dumpを取る間は対象DBだけを動かし、cold copyを取る前にはDB・S3・Grafana等も正常停止します。Grafanaの既定SQLiteをファイルコピーする場合も停止が必要です。[Grafana公式バックアップ手順](https://grafana.com/docs/grafana/latest/administration/back-up-grafana/)

### PostgreSQLの論理dump例

リポジトリのルートで、配置と同じComposeファイル・同じenvを使う補助関数を定義できます。これは既存のDocker Composeへの引数を揃える例です。

```bash
lakehouse_compose() {
  python3 - "$LAKEHOUSE_ENV_FILE" "$@" <<'PYTHON'
import subprocess
import sys
from pathlib import Path

from scripts.settings import environment, load

root = Path.cwd()
values = load(sys.argv[1])
command = ["docker", "compose", "--env-file", sys.argv[1]]
for name in (
    "lakehouse", "compute", "orchestration", "demo", "cdc", "poc", "mcp"
):
    command.extend(["-f", str(root / "infra" / name / "compose.yaml")])
subprocess.run(command + sys.argv[2:], env=environment(values), check=True)
PYTHON
}
```

この関数はrepoルートでのみ使い、Pythonは検査済みsite値を子プロセスへ渡します。古いshellのproxyやproject値を直接Composeへ混入させません。新しいstackを起動せず、既存DBへのexecと確認に使います。


writer停止後に、必要なDBについてだけ実行します。未使用profileのDBを起動する必要はありません。新しい保存先を選び、既存のarchiveへ上書きしません。

```bash
umask 077
set -o noclobber
BACKUP_DIR=/absolute/private/lakehouse-backup
mkdir -m 0700 "$BACKUP_DIR"
lakehouse_compose exec -T catalog-db \
  pg_dump -U lakekeeper -d lakekeeper -Fc > "$BACKUP_DIR/catalog.dump"
```

n8nとAirflowも使う場合の例です。

```bash
lakehouse_compose exec -T n8n-db \
  pg_dump -U n8n -d n8n -Fc > "$BACKUP_DIR/n8n.dump"
lakehouse_compose exec -T airflow-db \
  pg_dump -U airflow -d airflow -Fc > "$BACKUP_DIR/airflow.dump"
```

各コマンドの終了コードとstderrを確認します。リダイレクト先が存在するだけで成功とは判断しません。`pg_restore --list`等でarchiveを検査し、ファイルのハッシュを記録します。`pg_dump`は単一DBの整合した論理exportですが、S3や別DBと同時点になる機能やcluster共通roleの保管は含みません。[PostgreSQL公式pg_dump](https://www.postgresql.org/docs/18/app-pgdump.html)

### 停止したvolumeを保管する

全writerと対象サービスを停止した後、管理者のストレージsnapshotまたは一時コンテナによるread-only volumeのarchiveで保管します。Docker公式にはtarを使う例があります。コピー元は解決済みの実volume名を事前にinspectし、保存先はリポジトリ外にします。[Docker公式volumeバックアップ](https://docs.docker.com/engine/storage/volumes/#back-up-restore-or-migrate-data-volumes)

稼働中PostgreSQLのdataディレクトリを通常のtarでコピーする手順にはしません。cold copyは停止を確認してから行い、所有者・権限とDBのmajor versionも記録します。外部SQL Server自体のバックアップはDB管理者の運用で扱います。

保管先を別の媒体・障害領域へ複製し、暗号化・アクセス権・保持期間を決めます。バックアップ中に新しいジョブを開始しないよう、保守中であることを利用者へ伝えます。

## 別環境へ復元して確かめる

1. 元環境と異なるプロジェクト名・prefix・ポート・ランディングを用意します。元volumeへ上書きしません。
2. 記録したcommitと互換イメージを使い、同じ停止点のS3・カタログ・鍵・任意サービスを一組として戻します。
3. DBはcold dataを戻す方法と、空の専用DBへ`pg_restore`する方法を混ぜて二重適用しません。論理restoreは製品migrationを走らせる前の手順を確認します。
4. サービスを順に起動し、既存warehouse、Iceberg snapshot、原本hash、Gold件数、GUI編集、n8n credentials、Airflow Connectionを確認します。
5. 復元所要時間・復元できた時点・不足した対象を記録し、定期処理は確認後に一つずつ有効化します。

バックアップファイルを作成した段階と復元試験成功を分けて記録します。本手順の記載だけで、各導入先の復旧試験が済んだことにはなりません。


## 監査した版と公式資料

coreのPostgreSQL18.6、n8n2.46.7、Kafka4.4.0／Debezium3.7.1.Final、Dokploy0.31.0を対象に公開Composeとlockを照合しました。hostのDocker Engine29系／Compose5.6.1は導入先の実版を記録します。AirflowやPoC側のDB majorは各Composeを確認し、そのコンテナに含まれる対応pg_dumpを使います。

| 一次資料 | 本手順の対応 |
| --- | --- |
| [Compose project名](https://docs.docker.com/compose/how-tos/project-name/)、[Docker volume](https://docs.docker.com/engine/storage/volumes/#back-up-restore-or-migrate-data-volumes) | 同じデータ領域への二重writerを避け、実volume名を記録 |
| [PostgreSQL18 pg_dump](https://www.postgresql.org/docs/18/app-pgdump.html) | 論理dumpの成功と復元試験を分ける |
| [Kafka4.4 Connect設定](https://kafka.apache.org/44/configuration/kafka-connect-configs/)、[Debezium3.7](https://debezium.io/documentation/reference/3.7/connectors/sqlserver.html) | offset・schema履歴・source側保持を一組で扱う |

公式の一般手順と、このリポジトリで実装した範囲は区別します。実会社のbackup／restore試験は導入先で記録してください。

# 会社VMの修正を取り込み、Dokployへ再配置する

対象: 導入済みの会社VMと **Dokploy v0.31.0**。初めて導入する場合は [はじめに](START-HERE.md) から進みます。この文書では、既存のSamba・Dokploy・Swarm・保存データを保持し、公開Gitの互換性修正を取り込んでアプリを配置し直します。ホスト名・IP・proxy認証・CA実pathはprivate設定へ残します。

画面名は[採用tagの公式UI](https://github.com/Dokploy/dokploy/tree/v0.31.0/apps/dokploy/components/dashboard/compose)で確認しています。所属Environmentの設定画面と、**Composeサービス自身のEnvironmentタブ**を区別してください。[初回の詳しい画面手順](DOKPLOY.md)

## 今回の修正と変わらないもの

| 問題 | 公開Gitの修正 |
| --- | --- |
| 既存Swarmの設定取得で未対応サブコマンドを呼ぶ | `docker info --format '{{json .Swarm.Cluster}}'` を既存pool検証へ渡す |
| CA未指定時にBakeが `/dev/null` の追加読取り権限を求める | Git管理する0バイトの `infra/proxy/empty-ca.pem` を使用 |
| shell側の古い `NO_PROXY` がsite設定に優先する | `LAKEHOUSE_NO_PROXY` を生成し、CLIとComposeで確定値を共有 |
| 内部S3通信が会社proxyへ流れる | 必須内部名と会社の追加値をマージし、worker・Trinoにも両caseを渡す |

Docker Composeの補間はshell変数をenv fileより優先します。専用変数へ正規化し、Dokployのサービス変数にも登録します。[Docker公式の補間仕様](https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/)

これらはソース修正です。**既存Dokploy／Swarmの再初期化、daemon再起動、volume削除は更新の必須操作ではありません。** アプリのDeployでは必要なcontainerが再作成されることがあるため、更新時間を決めます。データ・暗号鍵の控えと復元の準備は [運用手順](OPERATIONS.md) を使います。

## 1. 配置先VMで現在の名前と設定を控える

作業するのは、**実際にレイクハウスを動かすVMの端末**です。Dockerを利用できる通常ユーザーで、独立して管理しているリポジトリへ移動します。Dokploy管理下のcode作業コピーを手編集しません。

ブラウザーでは対象の Project → Environment → Composeサービスを開き、サービス名の下の **実appName** を私有記録へ控えます。作成時のApp Name入力にはランダム6文字が付くため、入力元の名前から推測しません。Advanced → Run CommandのCommandが空欄なら、**Default Command** の `-p` でも確認できます。既存カスタムCommandの `-p` は手入力値なので、ヘッダーの実名と照合します。[生成規則](https://github.com/Dokploy/dokploy/blob/v0.31.0/packages/server/src/db/schema/utils.ts)、[実appNameを表示する画面](https://github.com/Dokploy/dokploy/blob/v0.31.0/apps/dokploy/pages/dashboard/project/%5BprojectId%5D/environment/%5BenvironmentId%5D/services/compose/%5BcomposeId%5D.tsx)

端末でprivate設定と公開可能な変更を控えます。

```bash
REPO_DIR="$(git rev-parse --show-toplevel)"
cd "$REPO_DIR"
export LAKEHOUSE_ENV_FILE="$HOME/.config/lakehouse-foundation/site.env"

umask 077
UPDATE_BACKUP_DIR="$(mktemp -d "$HOME/.config/lakehouse-foundation/update-backup.XXXXXX")"
cp -p "$LAKEHOUSE_ENV_FILE" "$UPDATE_BACKUP_DIR/site.env"
git status --short --branch
git diff --check
git diff > "$UPDATE_BACKUP_DIR/local-changes.patch"
```

patchには未追跡ファイルとステージ済み変更が含まれません。必要なら次も控え、未追跡ファイルは一覧を見て個別にprivate領域へ保管します。

```bash
git diff --cached > "$UPDATE_BACKUP_DIR/staged-changes.patch"
git ls-files --others --exclude-standard
```

**期待結果:** envと変更の控えがprivate領域にでき、現在のbranch・変更内容を把握できます。秘密値を含むpatchはGitへ入れません。公開可能な一時修正だけをstashへ退避する場合は `git stash push -u -m "company temporary compatibility fixes"` を使えます。会社固有値の入った変更をstash／commitする手順にはしません。判断できない変更がある場合は、ここで内容を分離してから先へ進みます。`git clean`や強制resetで消しません。

## 2. 公開mainを取得し、実行する版を確認する

```bash
git fetch --prune origin
git switch main
git pull --ff-only origin main
git log -1 --oneline
```

**期待結果:** ローカルのmainがfast-forwardされ、採用commitが確認できます。ローカル変更や分岐で止まったら手順1へ戻ります。古いpatch／stashを機械的に再適用せず、公開側に取り込まれた一時修正は重ねません。

0バイトplaceholderも確認できます。CAの中身を表示する操作ではありません。

```bash
python3 - <<'PY'
from pathlib import Path
path = Path('infra/proxy/empty-ca.pem')
assert path.is_file() and path.stat().st_size == 0
print('Empty CA placeholder: OK')
PY
```

今回の取得先はアプリの公開Gitです。Dokploy本体のバージョンを上げる操作ではありません。

## 3. private設定をupgradeし、proxy除外を確認する

```bash
python3 scripts/configure.py --output "$LAKEHOUSE_ENV_FILE" --upgrade
chmod 600 "$LAKEHOUSE_ENV_FILE"
./scripts/lakehouse.sh plan
python3 scripts/proxy-doctor.py --env-file "$LAKEHOUSE_ENV_FILE"
```

`--upgrade`は既存の秘密値を保持します。`.before-upgrade` が既に存在して拒否された場合は、その控えを手順1のprivate backupへ移して保管し、再実行します。既存backupや暗号鍵を上書きしません。[設定一覧](CONFIGURATION.md)

`LAKEHOUSE_NO_PROXY` は、必須内部名 → privateの既存 `LAKEHOUSE_NO_PROXY` → `NO_PROXY` → `no_proxy` の順に空白と重複を除いて作ります。会社の追加名・IP・CIDRを保持し、3項目を同じ値へ揃えます。親shellのlegacy値はこの正規化へ取り込みません。確定済みの専用変数をshellへ別値でexportしません。

**期待結果:** planが成功し、診断で内部serviceのbypassが有効です。診断だけの成功はdaemon pull、build、Java、実会社proxyまでの成功ではありません。CAを使う場合は正式な公開PEMと適用先も [会社proxy](COMPANY-PROXY.md) で確認します。n8nの宛先許可は別の [SSRF設定](SSRF.md)です。

## 4. CLIで動いている構成を引き継ぐ場合だけ停止する

**既に同じDokployサービスで管理している場合は、この停止を飛ばして手順5へ進みます。** CLIで動かしたDB／S3をDokployへ引き継ぐ場合だけ、旧 `COMPOSE_PROJECT_NAME`、profile、volumeの実名、外部ディレクトリを控えます。

旧設定のまま次を実行し、旧writerを止めます。

```bash
./scripts/lakehouse.sh down
```

named volumeは保持されます。**停止前にプロジェクト名をDokploy用へ書き換えません。** 書き換えると旧containerを対象にできなくなります。停止後に、private envの `COMPOSE_PROJECT_NAME` を手順1の実appNameへ変更します。旧volume接頭辞が違う場合は `DATA_VOLUME_PREFIX` へ旧接頭辞を指定し、使用する全volumeの解決名を照合します。[既存volume引継ぎ](DOKPLOY.md#既存volume名を引き継ぐ場合)

**期待結果:** 旧DB・S3等のwriterが停止し、元のデータvolumeが残ります。接頭辞や保存先を確認できない場合は新配置を起動せず、私有記録と実volumeを照合します。同じvolumeへ二つのDB／S3を起動しません。

## 5. ブラウザーで対象ComposeのGitと変数を保存する

手順1と同じ Project → Environment → Composeサービスを開きます。新しいComposeをもう一つ作る操作ではありません。

1. **General → Provider → Git** を選びます。Repository URLは `https://github.com/hiromura16/lakehouse-foundation.git`、Branchは `main`、Compose Pathは `infra/lakehouse/compose.yaml` として **Save** します。公開HTTPSのためSSH Keyは不要です。
2. **Composeサービス自身のEnvironmentタブ**を開きます。カード名は **Environment Settings** です。upgrade後のprivate envと一致させ、**Create Environment FileをON → Save** します。
3. `COMPOSE_PROJECT_NAME` が画面の実appNameと一致し、移行用 `DATA_VOLUME_PREFIX`、既存の秘密値、`LAKEHOUSE_NO_PROXY`、proxy両case、CA、landing／privateディレクトリが保持されたことを確認します。
4. **Advanced → Run Command → Command** を確認し、配置対象のComposeファイルとprofileを合わせます。coreのみなら空欄の既定Commandを利用できます。追加機能を使っている場合は既存profileを不用意に空へ戻さず、[7ファイルの指定](DOKPLOY.md#任意composeを重ねる)を維持します。

GitHubタブは接続済みGitHub Provider用の別方式です。公開URLの直接入力にはGitタブを使います。[0.31.0のGitフォーム](https://github.com/Dokploy/dokploy/blob/v0.31.0/apps/dokploy/components/dashboard/compose/general/generic/save-git-provider-compose.tsx)

所属EnvironmentやProjectへ変数を貼るだけではサービスへ自動継承されません。必要ならサービスの値から `${{environment.KEY}}` 等を明示参照する方式ですが、この手順では全設定をサービス自身へ登録します。Create Environment FileがOFFの場合、UIの変数は今回のComposeへ反映されません。Save後に同じ画面を開き直して確認します。[Environment画面](https://github.com/Dokploy/dokploy/blob/v0.31.0/apps/dokploy/components/dashboard/application/environment/show-environment.tsx)、[変数解決実装](https://github.com/Dokploy/dokploy/blob/v0.31.0/packages/server/src/utils/docker/utils.ts#L471)

カスタムCommandは入力の先頭を `compose` にし、`-p`へ実appNameを直接記入します。Dokployが `docker` を付けます。`$VAR`や改行・shell置換を使わず、入力欄へ一行で保存します。**Pull latest images on deploy** のswitchはカスタムCommandには効きません。[Run Command実装](https://github.com/Dokploy/dokploy/blob/v0.31.0/apps/dokploy/components/dashboard/compose/advanced/add-command.tsx)

private envは配置先VMの専用ディレクトリにも保持します。`demo-api`はそのホスト側pathをbindします。Dokployが生成する `infra/lakehouse/.env` は別のfileであり、GUI設定の保存先を自動同期しません。VMのprivate envとサービスEnvironmentを管理者が一致させてから反映します。

## 6. RebuildではなくDeployして取り込む

Generalの **Deploy → Deploy Composeの確認** を実行します。**RebuildはGitを再取得しない**ため、今回のソース更新の代わりにはなりません。[採用版の操作実装](https://github.com/Dokploy/dokploy/blob/v0.31.0/apps/dokploy/components/dashboard/compose/general/actions.tsx)

Deploymentsタブで今回の記録を開き、Git取得 → 使用Commandと実appName → build → Compose起動を確認します。失敗した段階のログを私有記録へ残し、秘密を含む全文を公開しません。既定Commandは起動を待ち切る設定ではないため、成功表示の後もContainersで確認します。

| 確認対象 | 期待結果 |
| --- | --- |
| CA未指定のworker build | 0バイトplaceholderを読み、CA helperがskip。`/dev/null`の追加権限要求なし |
| `catalog-db` / `catalog` / `trino` / `worker` | healthy |
| `storage` | running。認証なしS3ルートの403とproxy由来の502を区別 |
| `catalog-migrate`等のone-shot | Exited (0)。追加profileの正常initも同様 |
| worker初期化 | 起動時にbucket・Warehouse等のbootstrapを自動実行してからready |
| 内部bypass | storage / catalog / trino等への接続が会社proxyへ流れない |
| 既存データ | 旧volumeと暗号鍵を使い、既存内容が残る |

Containersタブの更新ボタンで状態を取り直します。対象行の三点メニューに **View Logs** と **Terminal** があります。Exited (0)のone-shotを常駐させるためにStartし直しません。非ゼロ終了の場合はそのserviceのログへ戻ります。[採用版Containers画面](https://github.com/Dokploy/dokploy/blob/v0.31.0/apps/dokploy/components/dashboard/compose/containers/show-compose-containers.tsx)

## 7. 同じVM・設定で往復試験し、更新を記録する

配置先VMの独立cloneで、採用commitとprivate envがDokploy配置に一致することを確認してから実行します。

```bash
./scripts/lakehouse.sh status
./scripts/lakehouse.sh verify
```

verifyは既存workerへexecします。Dokploy管理中に、別プロジェクト名でCLIの `up` を並行実行しません。成功後は使用commit、実appName、profile、正常one-shot、データ保持の結果を私有運用記録へ残します。追加profileの受入は [Dokploy](DOKPLOY.md) と各機能の文書へ進みます。

| 失敗した段階 | 戻る場所 |
| --- | --- |
| Git取得／branch切替 | 手順1・2。ローカル変更と公開commitを確認 |
| plan／proxy診断 | 手順3、[会社proxy](COMPANY-PROXY.md) |
| appName・volumeの対象が不明 | 手順1・4。起動前に照合 |
| 必須変数がない／古い値が出る | 手順5。サービスEnvironmentとCreate Environment File |
| 新しい修正がbuildに現れない | 手順6。RebuildではなくDeployかを確認 |
| one-shotだけ停止している | 終了codeを確認。0なら正常、非ゼロは対象ログ |
| healthyにならない／データが違う | [障害対応](TROUBLESHOOTING.md)。volume削除でやり直さない |

更新前の状態へ戻す場合も、データのschema互換性・元のGit版・秘密値・volumeの組を確認します。旧コードを起動し直すだけで復元が保証されるわけではありません。[更新・復元runbook](OPERATIONS.md)

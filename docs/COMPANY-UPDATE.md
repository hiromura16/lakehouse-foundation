# 会社VMの修正を取り込み、Dokployへ再配置する

<!-- doc-purpose:start -->
> **役割:** 既存の設定・鍵・volumeを保持し、会社VMの修正を取り込んでDokployへ再配置する。
>
> **対象読者:** 基盤構築担当 / 運用担当
>
> **読むタイミング:** 既に動いている環境へ公開Gitの互換性修正を反映するとき。
>
> **前提となる資料:** [Dokploy 0.31.0でGitから配置する](<DOKPLOY.md>) / [サイト設定リファレンス](<CONFIGURATION.md>)
>
> **次に読む資料:** [平時・更新・復旧の運用手順](<OPERATIONS.md>) / [症状から確認する](<TROUBLESHOOTING.md>)
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

## 更新前後で一組として照合する記録

更新担当は、次の表を公開Git外の運用記録へ用意します。秘密値を表へ貼らず、保管場所や同一性を確認した結果だけを残します。

| 記録欄 | 更新前 | 更新後の合格条件 |
| --- | --- | --- |
| Git commit / 配置管理者 | 現在の版とCLI/Dokployの担当 | 意図した版を同じ管理入口から配置 |
| 実appName / project名 | ヘッダーとCLI設定の対応 | 変更しない、または承認した移行先と一致 |
| profile / host port | 稼働中に選んでいる機能 | 追加・削除の意図が説明でき、他環境と競合しない |
| volume / 外部dir | 物理volume名とlanding/workspaceの対応 | 既存データを使う先が一致 |
| 暗号鍵 / DB資格情報 | private管理先で確認 | 通常更新では保持。ローテーションは別の手順 |
| 直前ジョブ / データ件数 | 実行中か、最終成功と照合範囲 | 二重writerがなく、同じ範囲を読戻せる |

更新後にデータが空に見える場合は、新規素材の投入より先にこの対応を確認します。再投入で表を埋めると、元のvolumeを選べていない問題を隠すことがあります。

## 1. 配置先VMで現在の名前と設定を控える

作業するのは、**実際にレイクハウスを動かすVMの端末**です。Dockerを利用できる通常ユーザーで、独立して管理しているリポジトリへ移動します。Dokploy管理下のcode作業コピーを手編集しません。

ブラウザーでは対象の Project → Environment → Composeサービスを開き、サービス名の下の **実appName** を私有記録へ控えます。作成時のApp Name入力にはランダム6文字が付くため、入力元の名前から推測しません。Advanced → Run CommandのCommandが空欄なら、**Default Command** の `-p` でも確認できます。既存カスタムCommandの `-p` は手入力値なので、ヘッダーの実名と照合します。[生成規則](https://github.com/Dokploy/dokploy/blob/v0.31.0/packages/server/src/db/schema/utils.ts)、[実appNameを表示する画面](https://github.com/Dokploy/dokploy/blob/v0.31.0/apps/dokploy/pages/dashboard/project/%5BprojectId%5D/environment/%5BenvironmentId%5D/services/compose/%5BcomposeId%5D.tsx)

端末でprivate設定と公開可能な変更を控えます。

**Bash / Zsh共通**

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

**Bash / Zsh共通**

```bash
git diff --cached > "$UPDATE_BACKUP_DIR/staged-changes.patch"
git ls-files --others --exclude-standard
```

**期待結果:** envと変更の控えがprivate領域にでき、現在のbranch・変更内容を把握できます。秘密値を含むpatchはGitへ入れません。公開可能な一時修正だけをstashへ退避する場合は `git stash push -u -m "company temporary compatibility fixes"` を使えます。会社固有値の入った変更をstash／commitする手順にはしません。判断できない変更がある場合は、ここで内容を分離してから先へ進みます。`git clean`や強制resetで消しません。

## 2. 公開mainを取得し、実行する版を確認する

**Bash / Zsh共通**

```bash
git fetch --prune origin
git switch main
git pull --ff-only origin main
git log -1 --oneline
```

**期待結果:** ローカルのmainがfast-forwardされ、採用commitが確認できます。ローカル変更や分岐で止まったら手順1へ戻ります。古いpatch／stashを機械的に再適用せず、公開側に取り込まれた一時修正は重ねません。

0バイトplaceholderも確認できます。CAの中身を表示する操作ではありません。

**Bash / Zsh共通**

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

**Bash / Zsh共通**

```bash
python3 scripts/configure.py --output "$LAKEHOUSE_ENV_FILE" --upgrade
chmod 600 "$LAKEHOUSE_ENV_FILE"
./scripts/lakehouse.sh plan
python3 scripts/proxy-doctor.py --env-file "$LAKEHOUSE_ENV_FILE"
```

`--upgrade`は既存の秘密値を保持します。`.before-upgrade` が既に存在して拒否された場合は、その控えを手順1のprivate backupへ移して保管し、再実行します。既存backupや暗号鍵を上書きしません。[設定一覧](CONFIGURATION.md)

`LAKEHOUSE_NO_PROXY` は、必須内部名 → privateの既存 `LAKEHOUSE_NO_PROXY` → `NO_PROXY` → `no_proxy` の順に空白と重複を除いて作ります。会社の追加名・IP・CIDRを保持し、3項目を同じ値へ揃えます。親shellのlegacy値はこの正規化へ取り込みません。確定済みの専用変数をshellへ別値でexportしません。

**期待結果:** planが成功し、診断で内部serviceのbypassが有効です。診断だけの成功はdaemon pull、build、Java、実会社proxyまでの成功ではありません。CAを使う場合は正式な公開PEMと適用先も [会社proxy](COMPANY-PROXY.md) で確認します。n8nの宛先許可は別の [SSRF設定](SSRF.md)です。

### `.before-upgrade` が既にある場合

変更が必要なupgradeで既存backupがあると、現在の実装は `An upgrade backup already exists.` と退避・再実行の案内を返します。元envとbackupは書き換えません。ファイルが既に最新で変更がない場合は、backupが存在していても成功します。

エラーを見たときだけ、既存backupを私有記録と照合して保管します。手順1で作った `UPDATE_BACKUP_DIR` を同じ端末で保持している場合の例です。

**Bash / Zsh共通**

```bash
: "${LAKEHOUSE_ENV_FILE:?Select the private env first}"
: "${UPDATE_BACKUP_DIR:?Create the private backup directory first}"
UPGRADE_BACKUP="${LAKEHOUSE_ENV_FILE}.before-upgrade"
SAVED_UPGRADE_BACKUP="$UPDATE_BACKUP_DIR/site.before-upgrade.env"
if [ -e "$SAVED_UPGRADE_BACKUP" ]; then
  printf '%s\n' 'Backup destination already exists; choose a new private location.'
else
  mv -n -- "$UPGRADE_BACKUP" "$SAVED_UPGRADE_BACKUP"
fi
```

`mv -n` は保存先を上書きしません。移動先が既にある場合や元backupが見つからない場合は、削除せず保管状況を確認します。退避先が0600であり、元backupが移動したことを確認してから `configure.py --upgrade` を再実行します。バックアップ内の秘密値を端末へ全文表示する必要はありません。

### 不在・拒否・反映待ちを読み分ける

| 表示・症状 | 意味 | 次に確認する場所 |
| --- | --- | --- |
| `Private env file not found.` | 指定した外部通常ファイルを読めない | 同じ端末のLAKEHOUSE_ENV_FILE、生成済みファイルの有無 |
| `Use a private env file outside the repository.` | Git内のpathを選んでいる | Git外の環境専用設定へ戻す。Gitへenvを置いて回避しない |
| `An upgrade backup already exists.` | 変更前backupの上書きを拒否 | 上の退避手順。元env/backupを保持 |
| `no changes made` | 追加・正規化する差分がなかった | 異常ではない。必要ならprofile等の明示変更へ進む |
| GUIに反映待ち | privateファイルの保存と起動時の値が違う | サービスEnvironment、mount、再Deploy後の状態 |

これらの復旧経路は [configure.py](../scripts/configure.py)、[settings.py](../scripts/settings.py)、[回帰テスト](../tests/test_company_compatibility.py) で確認できます。コード更新の受入と、企業proxyや本物の業務DBの現地受入は引き続き分けます。

## Dokployサービス自身へproxyとBuildKit設定を同期する

これはアプリのEnvironment更新とは別の、本リポジトリinstallerが所有するDokploy本体の更新です。以下は導入先で保守時間に行う手順であり、ソース修正のローカルテストで実サービスへapplyした記録ではありません。管理外のDokploy／Swarmは取り込まず停止するので、所有権labelやinstallation identityを後付けして拒否を回避しません。

1. 手順1のprivate backupと、手順3のupgradeを先に完了します。proxyの両caseと既存NO_PROXY追加先を保持します。
2. ローカルのエディターで `DOKPLOY_BUILDKIT_NO_CLIENT_TOKEN=1` を確認します。既定1、無効化は0です。空欄・`true` 等はplanで拒否します。proxy値をコマンド引数へ直書きしません。
3. 次のdry-runで、proxyの `SET/NOT_SET` とBuildKitの `enabled/disabled` の2行を確認します。host変更・通信はありません。

**Bash / Zsh共通**

```bash
: "${LAKEHOUSE_ENV_FILE:?Select the existing private site.env first}"
bash infra/dokploy/install.sh --env-file "$LAKEHOUSE_ENV_FILE"
```

4. 反映対象・private設定・保守時間を確認した導入先の管理者が実行します。既存Swarmのpool/identity等が一致しない場合は停止し、再初期化しません。

**Bash / Zsh共通**

```bash
sudo bash infra/dokploy/install.sh --env-file "$LAKEHOUSE_ENV_FILE" --apply
```

新規の場合はcreate、既存の場合は所有権確認後のenv同期です。HTTP(S)の空欄は対応する管理変数を削除し、0は `BUILDKIT_NO_CLIENT_TOKEN` を削除します。管理対象7キーの現在値を内部比較して、差分か重複がある場合だけ1回の `--env-rm` / `--env-add` updateを行います。同じ設定を再適用しても不要なenv updateを発生させません。既存のその他env・mount・volume・管理者・アプリ・Git Provider・Swarm IDを保持します。管理するproxy値を手動変更して残す方式ではありません。

更新後は旧taskの `1/1` だけでなくupdate完了とRunningを最大120秒で確認します。timeout／paused等は失敗として停止し、replicas・許可したtask state・task errorの有無だけを出します。rawエラー・service inspect全文には秘密値が含まれ得るため、公開ログに貼りません。installer全体のimage取得やUI待機はこの120秒とは別です。

5. [会社proxyの読取診断](COMPANY-PROXY.md#dokploy配置で確認する3つのproxy適用面) で、管理キーのpresence、同じDokployコンテナのGit取得、registry401、token endpoint200を確認します。
6. その結果を記録してから、手順5以降で同じComposeアプリへ通常のDeployを行います。キャッシュにない固定base imageのmetadata取得で再発しないことも別途確認します。1の設定だけで障害解消済みとは判断しません。

### BuildKit設定だけ元へ戻す

他の設定・秘密値を保持したままprivate switchを0へ編集し、同じdry-run→保守時間のapplyを行います。管理する標準名が削除されたことをpresence診断で確認します。DB／volumeやSwarmを戻す操作は不要です。proxy自体も除去する場合はHTTP(S)大文字・小文字を一組で編集し、別のdaemon／アプリ設定の反映も [会社proxy手順](COMPANY-PROXY.md) で確認します。古いbackup全体で現在の資格情報を上書きしません。

## 4. CLIで動いている構成を引き継ぐ場合だけ停止する

**既に同じDokployサービスで管理している場合は、この停止を飛ばして手順5へ進みます。** CLIで動かしたDB／S3をDokployへ引き継ぐ場合だけ、旧 `COMPOSE_PROJECT_NAME`、profile、volumeの実名、外部ディレクトリを控えます。

旧設定のまま次を実行し、旧writerを止めます。

**Bash / Zsh共通**

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

**Bash / Zsh共通**

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

## 固定SQL sourceの拡張を反映する場合

コードのレビュー・公開反映後、既存private設定へ通常の `configure.py --upgrade` を実行し、新しいownership ID/tokenは初回設定後に保持します。Source/Demoの接続・TLS、shared-source明示許可、別control principal、CDC mode/state volumeを [固定SQL source手順](SQLSERVER-FIXED-DEMO.md) に従って確認します。旧設定や秘密値を新しい生成ファイルで置き換えません。

DDLはアプリDeployと別工程です。DBAが001→002→003→006で固定sourceを受け入れ、GUIで投入・snapshot・増分を確認した後だけ004を適用します。実SQL Serverで未検証のDDLをアプリ起動時に自動実行することはありません。005は明示的な無効化専用で通常更新に含めません。[段階別受入とrollback](COMPANY-ROLLOUT.md#固定sql-sourceバッチcdcを追加するときの受入順)

既存Dokploy、Swarm、管理者、保存volumeは保持します。固定SQLの新コード・unit/staticテスト成功は、BuildKit proxy対策やCDCの会社側での成功を保証しません。初回は実機受入の証拠を別に取得します。

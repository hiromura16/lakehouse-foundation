# 会社VMの修正取り込みとDokploy再配置

会社で一時修正したチェックアウトを、公開Gitの互換性修正へ揃える手順です。会社の実ホスト名・IP・proxy認証・CAはprivate設定へ保持します。既存のSamba、Dokploy、Swarm、named volumeを継続利用します。

## 今回の修正

| 問題 | 修正 |
| --- | --- |
| 既存Swarmの設定取得で未対応のサブコマンドを呼ぶ | `docker info --format '{{json .Swarm.Cluster}}'` を既存pool検証へ渡す |
| CA未指定時にBakeが `/dev/null` の読取り権限を要求する | Git管理する0バイトの `infra/proxy/empty-ca.pem` を使用 |
| 古いshellの `NO_PROXY` がsite設定に優先される | 確定値 `LAKEHOUSE_NO_PROXY` を生成し、CLIとComposeで共有 |
| 内部S3通信が会社proxyへ流れる | 必須内部名と利用者の追加値をマージし、workerとTrinoにも両caseを渡す |

Docker Composeの補間ではshell変数が `--env-file` より優先されます。専用変数へ正規化してからDokployへ登録することで、同名の古いlegacy変数との衝突を避けます。[Docker公式の補間仕様](https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/)

## 1. ローカル修正とprivate設定を控える

配置先のリポジトリへ移動し、通常ユーザーで実行します。`REPO_DIR` は現在のclone先から求めるため、特定の配置パスには依存しません。

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

未追跡ファイルはpatchに含まれません。現在の変更を確認し、一時修正をGitのstashへ退避する場合は次を実行します。

```bash
git stash push -u -m "company temporary compatibility fixes"
```

stash・patch・backupには会社固有値が含まれる可能性があります。私有領域で保持し、公開Gitへ追加しません。ignoredのprivate設定はそのまま保持し、`git clean`、volume削除、Swarm再初期化は更新手順へ含めません。

## 2. mainを取得する

```bash
git fetch --prune origin
git switch main
git pull --ff-only origin main
git log -1 --oneline
```

一時修正が公開側に反映されたことを確認してから進めます。古いpatchやstashを機械的に再適用すると、同じ修正を二重に入れる場合があります。private env、landing、Samba設定、Dokployの管理状態、named volumeはGitの更新対象外です。

## 3. proxy除外を正規化する

既存private設定を更新し、既存の秘密値を保持したまま専用の確定値を追加します。

```bash
python3 scripts/configure.py --output "$LAKEHOUSE_ENV_FILE" --upgrade
chmod 600 "$LAKEHOUSE_ENV_FILE"
./scripts/lakehouse.sh --env-file "$LAKEHOUSE_ENV_FILE" plan
python3 scripts/proxy-doctor.py --env-file "$LAKEHOUSE_ENV_FILE"
```

変更前の `.before-upgrade` が既に存在して更新を拒否された場合は、その控えを上記private backupディレクトリへ移して保管し、同じupgradeを再実行します。既存の控えを上書きしません。

確定値は、必須内部名 → privateの既存 `LAKEHOUSE_NO_PROXY` → `NO_PROXY` → `no_proxy` の順に空白・重複を除いて作ります。会社固有の追加名・IP・CIDRを保持し、3項目を同じ値に揃えます。親shellのlegacy値はCLIの確定処理へ取り込みません。

専用値を生成した後、会社の追加値はGUIまたはprivate設定で管理します。GUIの編集では3項目を同期します。ファイルを手編集して除外先を削除する場合は、3項目を同じ内容に揃えてから確認します。別項目に古い値が残ると、保持目的のマージで再び追加されます。

Dokployへの直接配置には、**upgrade後の `LAKEHOUSE_NO_PROXY` もEnvironmentへ登録**します。旧設定だけを使うraw Composeでは内部名を補うfallbackがありますが、同名shell変数に隠れた会社の追加値は取得できません。追加値の保持と重複除去には正規化が必要です。専用変数自体もshellへ別値でexportしない運用にします。

`NO_PROXY` は通信経路の除外指定です。n8nの宛先許可は別の [SSRF設定](SSRF.md) で管理します。daemonのpull用proxy、BuildKit、Java/SDKの対応範囲は [会社proxy・CA手順](COMPANY-PROXY.md) で確認します。

## 4. CLIからDokployへ切り替える

CLI側の起動実績がある場合は、現在使っている `COMPOSE_PROJECT_NAME` とvolume名を私有記録へ控えます。旧CLIコンテナが起動中なら、**旧プロジェクト名の設定のまま**停止します。

```bash
./scripts/lakehouse.sh --env-file "$LAKEHOUSE_ENV_FILE" down
```

この停止はnamed volumeを保持します。`COMPOSE_PROJECT_NAME` をDokploy用へ変更した後に停止すると、旧コンテナを見失うため、順番を守ります。DokployのGUIで既に管理している環境の更新は、そのComposeサービスから行います。

CLIのデータを引き継ぐ場合は、既存volumeの接頭辞を確認して `DATA_VOLUME_PREFIX` へ設定します。新規データとして始める場合は空欄です。機械的に空欄へ戻すと別のvolumeが選ばれる場合があるため、実名を照合してください。[既存volumeの引継ぎ](DOKPLOY.md#既存volume名を引き継ぐ場合)

## 5. Dokploy Environmentを更新する

導入済みDokployの対象Composeサービスを開きます。インストーラーの再実行は今回の更新には必要ありません。

| 設定 | 値 |
| --- | --- |
| Source | Git |
| Repository | `https://github.com/hiromura16/lakehouse-foundation.git` |
| Branch | `main` |
| Compose path | `infra/lakehouse/compose.yaml` |
| Compose方式 | Docker Compose |
| `.env`生成 | 有効 |
| AutoDeploy | 初回受入まで手動 |

private設定の `COMPOSE_PROJECT_NAME` を実際の `appName` と一致させ、エディターからprivate設定をEnvironmentへ転記します。`LAKEHOUSE_NO_PROXY`、HTTP(S) proxyの両case、必要な秘密値、外部landing pathを含めます。Project共通値の継承で別の値が混ざらないよう、対象サービスのEnvironmentを確認します。

最初にcoreだけを受け入れる場合は `COMPOSE_PROFILES=` とします。追加profile用の変数が登録されていても、そのprofileを有効にするまで追加サービスは起動しません。coreの受入後、VMのメモリ予算に合わせて機能を追加します。

coreだけの既定Compose pathでも今回の修正は利用できます。追加の7ファイルを重ねるCustom Commandは [Dokploy手順](DOKPLOY.md#任意composeを重ねる) を使い、`-p` の名前を実際の `appName` へ合わせます。

## 6. Deployして受け入れる

設定保存 → 公開mainのcommit確認 → Deploy → Build log → Containers、の順で確認します。

| 確認 | 期待結果 |
| --- | --- |
| 企業CA未指定のworker build | 0バイトplaceholderでCA helperがskip。`/dev/null`の追加権限要求なし |
| catalog-db / catalog / Trino / worker | healthy |
| storage | running。認証なしS3ルートの403は正常な拒否 |
| catalog-migrate等のone-shot | exit 0。正常終了も受入に含める |
| workerのS3接続 | proxy由来の502や待機ループが続かない |
| 内部bypass | storage / catalog / trino等の必須名が有効 |
| 往復試験 | `lakehouse.sh verify` が成功 |

同じappNameとprivate設定を使い、配置後に実行します。

```bash
./scripts/lakehouse.sh --env-file "$LAKEHOUSE_ENV_FILE" status
./scripts/lakehouse.sh --env-file "$LAKEHOUSE_ENV_FILE" verify
```

CLIの `verify` は稼働中workerへexecする検証です。Dokploy管理中に別プロジェクト名でCLIの `up` を並行実行しません。接続先が別サーバーの場合は、実際の配置先で上記を実行してください。

秘密値を含むCompose全文や `docker inspect` 全文を公開ログへ貼りません。proxy由来の応答と、認証なしS3の正常な拒否を分け、会社固有の設定・実測結果はprivateに残します。失敗時はvolumeを消してやり直さず、[障害対応](TROUBLESHOOTING.md) と [会社proxy手順](COMPANY-PROXY.md) を確認します。

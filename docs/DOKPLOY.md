# Dokploy 0.31.0でGitから配置する

<!-- doc-purpose:start -->
> **役割:** Dokploy 0.31.0の画面で、生成appName・個別Environment・7つのComposeを揃えて配置する。
>
> **対象読者:** 基盤構築担当 / 運用担当
>
> **読むタイミング:** Dokployへログインできた後、同じComposeアプリを初回配置・再配置するとき。
>
> **前提となる資料:** [初めて構築する人の一本道ガイド](<START-HERE.md>) / [導入・検証・停止](<DEPLOYMENT.md>)
>
> **次に読む資料:** [会社VMの修正を取り込み、Dokployへ再配置する](<COMPANY-UPDATE.md>) / [n8n・Airflow・Grafanaを画面から使う](<GUI-WALKTHROUGHS.md>)
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

確認日: 2026-11-10。この手順の画面名と動作は、採用imageの **Dokploy v0.31.0** と[同じ公式tagのソース](https://github.com/Dokploy/dokploy/tree/v0.31.0)（commit `3ac1c8f2e025d7db7696542eb2770e08f598214c`）で確認しました。更新されるWeb版の[公式Compose説明](https://docs.dokploy.com/docs/core/docker-compose)は補助資料です。別バージョンの画面を0.31.0の仕様と読み替えません。

ここでは、会社VMにDokployが導入済みで、管理画面へログインできるところから説明します。未導入の場合は [はじめに](START-HERE.md) と [基盤配置](DEPLOYMENT.md) でホスト・Docker・Dokploy・Sambaを先に準備します。既存DokployやSwarmを作り直す手順ではありません。

## 初めての人が混同しやすい名前を分ける

| 名前 | 何を識別するか | 変更するときの判断 |
| --- | --- | --- |
| Project | Dokployの管理上のまとまり | 同じ基盤の検証/受入をまとめる入口。データの保存名ではない |
| Environment | Project内の所属 | development/production等の用途。別port・別保存先・別鍵の準備は別に必要 |
| ComposeのName | 画面上の表示名 | 見やすい名称へ変更できる。実appNameとは別 |
| 作成後のappName | Dokployが `docker compose -p` へ渡す実行名 | ヘッダーを正とし、CLIのCOMPOSE_PROJECT_NAMEへ合わせる |
| service | Compose内のworker、trino等の定義 | 公開コードの責務。表示名の変更に合わせて書き換えない |
| container | その配置で実行する実体 | 更新で作り直され得る。DBの永続データ自体ではない |

用語が初めてなら [用語集](GLOSSARY.md)、host準備が未完了なら [HOST-SETUP.md](HOST-SETUP.md) で分担を確認します。まずブラウザーで対象EnvironmentとappNameを読み、次に同じ環境のprivate設定を端末で選びます。名前を一つ変えてエラーが消えるか試す操作より、実行名・保存先・鍵が一組で合っているか確認する方が、空のDBを作る原因を見つけやすくなります。

## 作業場所と最初に控えるもの

| 場所・担当 | 行うこと |
| --- | --- |
| 作業PCのブラウザー／Dokploy管理者 | Project・Compose作成、Git・Environment・Commandの保存、Deploy、ログ確認 |
| **実際の配置先VM**の端末／Dockerを利用できる通常ユーザー | Git取得、private `site.env`の生成・確認、landing等の権限、plan、配置後verify |
| 同VMの管理者 | 必要時だけDocker daemonのproxy・CA・ホスト権限を設定 |

Dokployとアプリの配置先が別サーバーの場合、privateディレクトリ・landing・Dockerの確認は**アプリ配置先**で行います。画面の `Select a Server` の選択先と一致させます。初心者向けの以下の例は、Dokployとアプリが同じVMの構成です。

private記録へ、Project名、Environment名、サービスの表示名、**作成後のappName**、配置先サーバー、Git branchとcommit、private envの保管先を控えます。URL・認証・CA実path・生ログを公開Gitへ書きません。

## Dokploy自身のGit取得とBuildKitをproxy環境で準備する

Docker daemon、Dokployサービス、Lakehouseのbuild/runtimeは別の通信主体です。daemon用proxyだけでは、Dokployコンテナ内の `git clone` の成功を保証しません。また、通常の `docker pull` が成功しても、BuildKitのmetadata解決・レジストリtoken取得が同じ経路を使うとは限りません。

private `site.env` の `HTTP_PROXY` / `HTTPS_PROXY` に値があると、installerはDokployサービスへ同じ値の大文字・小文字を渡します。両方のproxyを設定した場合、`HTTP_PROXY,HTTPS_PROXY,NO_PROXY,http_proxy,https_proxy,no_proxy` の6変数が揃います。`NO_PROXY` は必須内部名とprivateの3項目をマージした値です。`LAKEHOUSE_NO_PROXY` という独自名はサービスへ渡さず、標準名の両caseへ変換します。未設定のHTTP(S)変数を空文字で追加しません。

| private設定 | Dokployサービスの動作 |
| --- | --- |
| `DOKPLOY_BUILDKIT_NO_CLIENT_TOKEN=1`（生成・upgrade既定） | `BUILDKIT_NO_CLIENT_TOKEN=1` を管理対象として追加・更新 |
| `DOKPLOY_BUILDKIT_NO_CLIENT_TOKEN=0` | 管理対象の `BUILDKIT_NO_CLIENT_TOKEN` を除去 |
| 空欄・0/1以外 | planで拒否。空欄を無効化の代用にしない |

このswitchはBuildKit client側のtoken取得経路を変えるための**検証未了の対策候補**です。`failed to authorize: DeadlineExceeded` / `context deadline exceeded` の解消保証、認証やTLS検証の無効化、会社proxyでの成功実績を意味しません。[BuildKitの実装](https://github.com/moby/buildkit/blob/master/session/auth/authprovider/authprovider.go) は上流masterへの参照であり、採用Docker CLIへ同梱されたBuildKitの挙動と現地で照合します。

配置先VMの独立cloneでprivate設定をupgradeし、ローカルのエディターでproxy・switch・既存ネットワーク値を確認します。ファイル全文やproxy値をログへ出しません。

**Bash / Zsh共通**

```bash
export LAKEHOUSE_ENV_FILE="$HOME/.config/lakehouse-foundation/site.env"
python3 scripts/configure.py --output "$LAKEHOUSE_ENV_FILE" --upgrade
bash infra/dokploy/install.sh --env-file "$LAKEHOUSE_ENV_FILE"
```

有効な設定でのdry-runの標準出力は次の2行だけです。Docker・network・ファイル変更は行いません。`SET` は設定がある意味で、疎通や稼働観測ではありません。

```text
Dokploy service proxy: SET
Dokploy BuildKit client token mode: enabled
```

反映を行う保守時間の手順は [既存Dokployサービスへの反映](COMPANY-UPDATE.md#dokployサービス自身へproxyとbuildkit設定を同期する) にあります。新規導入時はservice createへ必要なenvを配列で渡します。既存導入時は所有権・Swarm identity等を確認し、管理する7キーだけを1回のservice updateで同期します。重複キーを除去してから追加し、同じ設定の2回目はenv updateを省略します。DB・管理者・アプリ・Git Provider・volume・Traefikを作り直しません。

更新後は旧taskの `1/1` だけで成功とせず、update完了とRunningを最大120秒の収束待機で照合します。失敗時はreplicas・許可したtask state・task errorの `SET/NOT_SET` を表示します。raw task errorは秘密値を含み得るため表示しません。ネットワークやownershipの不一致は強制変更せず停止します。通常のinstaller全体ではimage取得やUI readinessなど別の待機もあります。

DokployのComposeサービス自身のEnvironmentタブへproxyを保存するだけでは、**Dokploy本体サービス**の環境変数は変わりません。逆に本体への同期だけではアプリのbuild/runtimeのproxyを代替しません。確認コマンドとHTTP 401/200の読み方は [会社proxyの3つの適用面](COMPANY-PROXY.md#dokploy配置で確認する3つのproxy適用面) を使います。

## 1. ProjectとEnvironmentを作る

ブラウザーでProject一覧の **Create Project** を押し、Nameに `lakehouse-foundation`、Descriptionに用途を入力して作成します。0.31.0では **production Environmentが自動作成**されます。最初の検証用にはEnvironment選択のメニューから **Create Environment** を押し、Nameを `development` として **Create** します。作業前に選択中のEnvironmentを毎回確認します。[Project作成実装](https://github.com/Dokploy/dokploy/blob/v0.31.0/packages/server/src/services/project.ts)、[Environment選択画面](https://github.com/Dokploy/dokploy/blob/v0.31.0/apps/dokploy/components/dashboard/project/advanced-environment-selector.tsx)

```text
lakehouse-foundation          Project
├── development               初回の検証先
│   └── platform              Composeサービスの表示名
└── production                自動作成される。受入後に別設定で導入
```

これは管理画面の所属です。Environmentを変えるだけで、ホストのport・保存先・DB・volumeが自動分離されるわけではありません。各環境のappName、port、landing、privateディレクトリ、暗号鍵を別々にします。

**期待結果:** `development` が選択された、まだアプリのない画面になります。`production` しか見えない場合は選択メニューへ戻ります。作成ボタンがない場合は、管理者にProject／Environment／サービス作成権限を確認してもらいます。

## 2. Composeを作り、生成後のappNameを控える

選択中のEnvironmentで **Create Service → Compose** を選びます。ダイアログ名は **Create Compose** です。

| 作成画面の欄 | 入力例・選択 |
| --- | --- |
| Name | `platform`。画面の表示名 |
| App Name | `lakehouse-development-platform`。生成名の元になる文字列 |
| Compose Type | **Docker Compose** |
| Select a Server (Optional) | 同VMなら **Dokploy**。登録サーバーがない場合、欄自体が表示されない |
| Description | 環境の用途。秘密値は入力しない |

**Create** を押してサービスを開きます。サービス名の下の小さな文字列が、作成後の実際の **appName** です。例えば入力が `lakehouse-development-platform` でも、結果は `lakehouse-development-platform-a1b2c3` のようにランダム6文字が付きます。これは説明用の名前で、実際の末尾は画面から控えます。[作成フォーム](https://github.com/Dokploy/dokploy/blob/v0.31.0/apps/dokploy/components/dashboard/project/add-compose.tsx)、[生成規則](https://github.com/Dokploy/dokploy/blob/v0.31.0/packages/server/src/db/schema/utils.ts)

作成後の鉛筆アイコン **Modify Compose** で変更できるのはNameとDescriptionです。appNameを変更する欄ではありません。更新APIもappNameをDB更新から除外します。入力した元の名前へ戻そうとせず、**確定したappNameをこの先の設定へ合わせます**。[変更画面](https://github.com/Dokploy/dokploy/blob/v0.31.0/apps/dokploy/components/dashboard/compose/update-compose.tsx)、[更新実装](https://github.com/Dokploy/dokploy/blob/v0.31.0/packages/server/src/services/compose.ts#L212)

**期待結果:** Generalタブの **Deploy Settings** に `Compose` と表示されます。`Stack` は選びません。本構成はローカルbuild、Compose依存条件、inline configsを使用します。Dokploy自身がSwarmで動くことと、アプリをStackで配置することは別です。[0.31.0のCompose Type定義](https://github.com/Dokploy/dokploy/blob/v0.31.0/packages/server/src/db/schema/compose.ts)

## 3. General → Provider → Gitへ公開URLを登録する

Generalタブの **Provider** カードで **Git** タブを選びます。GitHubのURLを直接入力する場合も、選ぶのはこの **Git** です。

| Gitタブの欄 | 入力 |
| --- | --- |
| Repository URL | `https://github.com/hiromura16/lakehouse-foundation.git` |
| Branch | `main`。配布を固定する運用では、管理者が確認したbranch／tag |
| Compose Path | `infra/lakehouse/compose.yaml` |
| SSH Key | 公開HTTPS URLには不要。欄があれば **None**。**Add SSH Key** は押さなくてよい |
| Watch Paths | 最初は空欄 |
| Enable Submodules | OFF |

**Save** を押します。`Git Provider Saved` が成功通知です。保存だけではcloneや起動は行われません。[Git入力フォームと保存処理](https://github.com/Dokploy/dokploy/blob/v0.31.0/apps/dokploy/components/dashboard/compose/general/generic/save-git-provider-compose.tsx)

**GitHubタブ**は、Settingsで接続済みのGitHub Providerからアカウントやリポジトリを選ぶ別方式です。未接続の場合はSettingsへの案内が出ます。今回の公開HTTPS cloneにGitHub AppやPATを用意する必要はありません。**Raw**はCompose本文用で、Dockerfile・Python・SQLが揃う方式ではないため本手順では使用しません。[Providerタブ実装](https://github.com/Dokploy/dokploy/blob/v0.31.0/apps/dokploy/components/dashboard/compose/general/generic/show.tsx)

**失敗時:** URL・Branch・Compose Pathを見直します。GitHubタブの接続案内で止まった場合はGitタブへ戻ります。cloneは後のDeploy時なので、Save成功だけを取得成功とは扱いません。

## 4. 配置先VMでprivate設定とディレクトリを準備する

VMの通常ユーザーで、独立して保持するcloneのルートへ移動します。Dokployが再取得する作業コピーへprivate設定を作りません。

**Bash / Zsh共通**

```bash
export LAKEHOUSE_ENV_FILE="$HOME/.config/lakehouse-foundation/site.env"
python3 scripts/configure.py --output "$LAKEHOUSE_ENV_FILE"
```

既存ファイルがある場合は新規生成せず、[更新手順](COMPANY-UPDATE.md)のbackupと `--upgrade` を使います。エディターで次を確認します。秘密値を確認するためにファイル全文を端末へ表示しません。

| private設定 | 合わせるもの |
| --- | --- |
| `COMPOSE_PROJECT_NAME` | **手順2の生成後appName**。入力元のslugとは違う |
| `DATA_VOLUME_PREFIX` | 新規導入は空欄。既存データ移行は後述 |
| `LANDING_DIR` | 配置先VMの外部絶対パス。Sambaと同じ実体 |
| `WORKER_UID` / `WORKER_GID` | landing等を使用する非rootユーザーの数値ID |
| `CONTROL_SETTINGS_DIR` / `CONTROL_SETTINGS_NAME` | GUIが保存するprivate envの専用ディレクトリ／file名 |
| `COMPOSE_PROFILES` | 最初のcore受入は空欄 |
| portとbind | VM内の他環境と重複しない値。初回はloopback |
| proxyとCA | `LAKEHOUSE_NO_PROXY`、HTTP(S)両case、必要時だけ `CORPORATE_CA_FILE` |
| パスワード・暗号鍵・token | 生成値を保持。既存環境は作り直さない |

`LANDING_DIR`は事前作成し、指定UID/GIDにアクセス権を与えます。`demo` profileで使うprivateディレクトリは0700、envは0600にし、API実行UIDと所有者を合わせます。`notebooks`には別の `NOTEBOOKS_DIR` が必要です。proxy／CA／daemon設定と準備コマンドは [会社導入](COMPANY-ROLLOUT.md)・[会社proxy](COMPANY-PROXY.md)・[設定一覧](CONFIGURATION.md)を参照します。

**Bash / Zsh共通**

```bash
./scripts/lakehouse.sh plan
python3 scripts/proxy-doctor.py --env-file "$LAKEHOUSE_ENV_FILE"
```

**期待結果:** 設定検証が成功します。planは起動しません。ここでCLIの `up` を実行して、Dokployと二つのwriterを作らないようにします。既存CLIからの切替は [会社VM更新](COMPANY-UPDATE.md) を先に読みます。

## 5. Compose自身のEnvironmentタブへ登録する

ブラウザーで **対象Composeサービス → Environment** を開きます。カード名は **Environment Settings** です。ここへ手順4のprivate envをローカルのエディターから転記し、**Create Environment FileをON → Save** とします。秘密値を扱う画面なのでスクリーンショットを共有しません。[0.31.0のEnvironment画面](https://github.com/Dokploy/dokploy/blob/v0.31.0/apps/dokploy/components/dashboard/application/environment/show-environment.tsx)

同名の「Environment」は、Project配下の所属とサービスの変数タブで別の意味です。**所属EnvironmentやProjectの変数欄へsite.envを貼るだけでは、Composeの変数になりません。** 0.31.0はサービス変数の値から、次の明示参照を解決します。

```dotenv
EXAMPLE_REGION=${{project.EXAMPLE_REGION}}
EXAMPLE_TEAM=${{environment.EXAMPLE_TEAM}}
EXAMPLE_COPY=${{EXAMPLE_LOCAL}}
```

これは参照構文の例で、本基盤へ追加する設定ではありません。初回は全てComposeサービス自身の変数欄に置き、参照先の登録漏れを避けます。共通値を分離するときだけ明示参照へ変更します。[変数解決の実装](https://github.com/Dokploy/dokploy/blob/v0.31.0/packages/server/src/utils/docker/utils.ts#L471)

ONの場合、次のDeployで指定Composeと同じディレクトリに `.env` が作られます。本構成では作業コピー内の `infra/lakehouse/.env` です。Composeコマンドはこのfileを補間に使い、必要な値だけを各serviceへ渡します。UIの全変数が全containerへ自動注入されるわけではありません。OFFではUIの変数はこのCompose配置へ反映されません。[env作成・実行処理](https://github.com/Dokploy/dokploy/blob/v0.31.0/packages/server/src/utils/builders/compose.ts)

GUI操作ハブが保存する外部 `site.env` と、Dokploy Environmentは別の保存先です。一方のSaveで他方まで同期しません。変更後は管理者が両方を一致させてDeployします。生成された作業コピー内 `.env` はDokployが管理するため、手編集しません。

**期待結果:** Save後に同じタブを開き直して値とON状態が残ります。Create Environment Fileが見えない場合は、GeneralのProviderがGitで保存済みか、アカウントの変数編集権限があるかを確認します。設定保存だけでは稼働中containerは更新されません。

## 6. 初回はcoreだけをDeployする

[初回ガイド](START-HERE.md) と同じ標準ルートでは、profileは空欄のまま、下の「任意Composeを重ねる」の**7ファイルのCommandを初回から登録**します。未選択profileのサービスは起動しません。

coreだけを最小設定で確認する別の方法もあります。coreのみ・追加File Mountsなしで既定コマンドを使う場合は、**Advanced → Run Command → Commandを空欄**にしてSaveします。表示される **Default Command** の `-p` が実appName、`--env-file` が `infra/lakehouse/.env`、`-f` が指定Composeであることを読み取ります。なお、カスタムCommandが設定済みのときはDefault Command欄もその内容を表示します。確定名はヘッダーを正とし、手入力の `-p` を無条件に信用しません。初回は **Pull latest images on deploy** をOFFにします。tagを任意更新する指示ではなく、公開lockを使います。[Run Command画面](https://github.com/Dokploy/dokploy/blob/v0.31.0/apps/dokploy/components/dashboard/compose/advanced/add-command.tsx)

Generalへ戻り、初回は **Auto DeployをOFF**のまま **Deploy → Deploy Composeの確認**を実行します。Deploymentsタブへ移り、今回の実行ログを開きます。**Deployはsourceを取得し直します。Rebuildはsourceを取得し直しません。** Gitの修正を取り込むときにRebuildだけを押しても更新されません。[Deploy／Rebuild画面](https://github.com/Dokploy/dokploy/blob/v0.31.0/apps/dokploy/components/dashboard/compose/general/actions.tsx)、[Git clone実装](https://github.com/Dokploy/dokploy/blob/v0.31.0/packages/server/src/utils/providers/git.ts)

ログの `App Name`、`Source Type: docker git`、`Compose Type: docker-compose`、実行Commandを確認します。Git取得 → worker build → Compose起動が進みます。7ファイルの標準Commandは `--wait` を指定します。空欄時の既定コマンドは `--wait` を含まないため、**Deploy成功と全serviceのhealthyは別々に確認**します。[0.31.0のコマンド生成](https://github.com/Dokploy/dokploy/blob/v0.31.0/packages/server/src/utils/builders/compose.ts#L125)

## 7. Containers・Logs・往復試験で受け入れる

ブラウザーの **Containers** タブでStateとStatusを確認し、更新アイコンで再取得します。対象container行の三点メニューには **View Logs** と **Terminal** があります。失敗したserviceは **Logs** タブまたはView Logsから確認します。[Containers画面](https://github.com/Dokploy/dokploy/blob/v0.31.0/apps/dokploy/components/dashboard/compose/containers/show-compose-containers.tsx)

| 対象 | 期待する状態 |
| --- | --- |
| `catalog-db` / `catalog` / `trino` / `worker` | 起動後にhealthy |
| `storage` | running。認証なしS3ルートの403は認証拒否であり、停止の証拠ではない |
| `catalog-migrate` | **Exited (0)**。catalogのschema準備が終わった正常one-shot |
| 追加profileの `spark-volumes-init` / `superset-init` / `cdc-register` | 成功時はExited (0)。常駐させる対象ではない |
| `spark-check` | 明示実行しない限りcontainerなし。通常upで起動しない |

workerはimageのCMDで `admin.py serve` を実行し、起動時にbucket・Warehouse等のbootstrapを再試行してからreadyになります。初回Dokploy Deployに別途手動bootstrapは不要です。正本は [worker Dockerfile](../infra/lakehouse/Dockerfile) と [serve実装](../scripts/admin.py)です。

one-shotを緑色のrunningにするため手動Startを繰り返しません。非ゼロ終了ならそのserviceのログを確認します。配置先VMでは、同じソース版・外部private env・実appNameで次を実行します。

**Bash / Zsh共通**

```bash
./scripts/lakehouse.sh status
./scripts/lakehouse.sh verify
```

verifyは既存workerへexecする往復試験です。別名でCLIの `up` を追加しません。成功後、使用commit・profile・正常終了one-shotを私有運用記録へ残します。会社の実SQL／proxy／CA／IdPやバックアップ復元の受入は、別の確認です。

## 任意Composeを重ねる

coreのファイルだけで扱える追加profileは `automation`／`monitoring` です。GUI/API、Spark、Airflow、CDC、PoC、MCPを使うときは7ファイルを同じCompose projectへ重ねます。**Advanced → Run Commandカード → Command欄**へ、次を**一行**で入力してSaveします。見出しに「Custom Command」というタブを探す必要はありません。

以下の `lakehouse-development-platform-a1b2c3` は、**手順2で控えた実appNameに置き換えます**。元の入力slugや別環境の名前を使いません。

```text
compose -p lakehouse-development-platform-a1b2c3 --env-file infra/lakehouse/.env -f infra/lakehouse/compose.yaml -f infra/compute/compose.yaml -f infra/orchestration/compose.yaml -f infra/demo/compose.yaml -f infra/cdc/compose.yaml -f infra/poc/compose.yaml -f infra/mcp/compose.yaml up -d --build --remove-orphans --wait --wait-timeout 600
```

0.31.0は先頭に `docker` を付けるので、入力の先頭は `compose` です。カスタム指定は既定コマンド全体を置換します。`$VAR`、改行、`;`、pipe、shell置換等は拒否されるため、`-p`へ変数を埋め込まず実名を入力します。`Pull latest images on deploy` はカスタム指定時には効きません。[入力検査と実行実装](https://github.com/Dokploy/dokploy/blob/v0.31.0/packages/server/src/utils/builders/compose.ts#L78)

次にprivate envとComposeのEnvironmentタブの両方で必要な `COMPOSE_PROFILES` を保存します。例えばGUIと監視は `demo,monitoring`、Jupyterから常駐Sparkを使う場合は `notebooks,compute` を含めます。**設定を保存 → Deploy → 全serviceと正常one-shotを再確認**します。追加キー・必要ディレクトリ・メモリの条件は [設定一覧](CONFIGURATION.md) と [PoCツール](POC-TOOLS.md)を読みます。

Sparkのbuild正本は `spark-volumes-init` 一つです。`spark-check` はCLIの専用操作で共通imageをbuildしてから明示実行します。Airflow Connection、Grafana dashboardのGUI保存、n8n credential等はそれぞれの文書に従います。[互換性](COMPATIBILITY.md)、[Grafana](GRAFANA.md)、[GUI操作](GUI-WALKTHROUGHS.md)

## 既存volume名を引き継ぐ場合

**新規は `DATA_VOLUME_PREFIX` を空欄**にします。既定の物理名は `<実appName>_<volumeキー>` です。移行の場合だけ、旧volumeの接頭辞をprivate設定へ指定します。`COMPOSE_PROJECT_NAME` は現在の実appName、`DATA_VOLUME_PREFIX` は引き継ぐ物理データの名前と区別します。

| 定義ファイル | 名前付きvolumeキー |
| --- | --- |
| core | `catalog-db`, `storage`, `trino`, `n8n-db`, `n8n`, `prometheus`, `grafana` |
| compute | `spark-events`, `spark-work` |
| orchestration | `airflow-db`, `airflow-state` |
| CDC | `cdc-kafka`, `cdc-state` |
| PoC | `superset-db`, `qdrant`, `marquez-db` |

使用するprofileの解決名を、VM上の既存volume実名と一つずつ照合します。bind mountのlanding・private設定・notebook workspaceは別に確認します。prefixはrename・コピー・復元を実行しません。違うprefixを入力すると空の別volumeが作られるため、**旧writerを停止してから**一つの配置へ切り替えます。移行手順は [会社VM更新](COMPANY-UPDATE.md)、控えと復元は [運用](OPERATIONS.md) を使います。

Project／Environment／Preview間で同じprefixや保存先を共有しません。DB・S3のvolume、各暗号鍵・資格情報を保持します。通常更新では **Fresh Volumes**、volume削除、prune、Swarm再初期化を使用しません。[0.31.0のFresh Volumes実装](https://github.com/Dokploy/dokploy/blob/v0.31.0/apps/dokploy/components/dashboard/compose/advanced/fresh-volumes.tsx)

## パス・保存元・更新元の関係

Gitの作業コピーはDokployが管理し、Deployで再取得します。`LANDING_DIR`と `CONTROL_SETTINGS_DIR` はそのcodeディレクトリの外へ作ります。名前付きvolumeはDockerが管理します。コードは公開Git、秘密値は外部private envとCompose自身のEnvironment、Grafana等のGUI状態は各volumeに保持する構成です。

Git Sourceのclone先を固定して入力する必要はありません。`/landing`、`/opt/lakehouse`、公式imageの `/home/node` は**container内のpath**で、VMのユーザー名やclone先ではありません。追加のFile Mounts／Domains／Isolated Deployment／Patchesは、この初回手順では設定しません。後で追加する場合はコマンド・network・path解決の変更を別途確認します。[pathの説明](DEPLOYMENT.md#ホストのパスとコンテナ内のパス)

## 迷ったときの戻り先

| 症状 | 戻って確認する場所 |
| --- | --- |
| GitHub接続画面でURL入力できない | 手順3のProvider → **Git** |
| `.env`に変数がない／必須変数エラー | 手順5の**サービス自身のEnvironment**、Create Environment File、Save |
| `$`や改行を含むCommandで失敗 | 任意Composeの一行コマンド。`docker`は書かず、`-p`は確定名 |
| 更新したはずのGit修正がない | Generalの**Deploy**を使ったか。Rebuildだけでは再取得しない |
| 画面で操作欄が見つからない | 0.31.0か、対象serviceか、管理者の権限かを確認 |
| 別の空データ／同じportの競合 | appName・prefix・配置先・旧writer・private envを照合 |
| 一部containerがExited (0) | 手順7のone-shot一覧。失敗なら非ゼロとログで判断 |
| clone／pull／buildのproxy失敗 | [会社proxy](COMPANY-PROXY.md)。サービスenvはGit clone・daemon pullの設定を代替しない |

通常の更新は [会社VM更新](COMPANY-UPDATE.md)、日常の操作・backup・復元は [運用](OPERATIONS.md)、原因調査は [障害対応](TROUBLESHOOTING.md)へ進みます。

## 保存したのに反映されないときの追い方

例としてGUIのportを変える場合、次の順で確認します。画面の変更を複数箇所へ一度に広げず、一つの差分で確認してください。

1. 配置先VMのprivate envで対象のportだけを変更し、`plan` を実行します。port競合や未設定を先に解消します。
2. Composeサービス自身のEnvironmentへ同じ変更を保存します。Create Environment FileがONであることを確認します。
3. GeneralからDeployし、Deploymentが使用したappName・Command・取得したGit版を確認します。RebuildはGit更新の代わりにしません。
4. Containersで対象serviceを確認します。元portへ開いたブラウザーを更新するだけでは、新portの受入になりません。
5. SSH転送等を使っている場合は転送先portも揃え、新しい入口と認証を確認します。操作ハブの反映待ち表示だけで他serviceの設定一致を証明しません。

外部private env、Dokployのサービス変数、生成される作業コピー内 `.env` は三つの別の保存場所です。GUIのSave先を追う図は [配置ライフサイクル](reports/figures/11-deployment-lifecycle.svg)、実装の入口は [control_settings.py](../infra/demo/control_settings.py) です。

## 上級者が配置変更をレビューする単位

| 変更 | Deploy前に比較するもの | 配置後の受入 |
| --- | --- | --- |
| profile追加 | 既存profileの保持、必要なprivateキー、host bind、メモリ枠 | one-shotの完了、追加GUI認証、既存データの保持 |
| image/依存更新 | Dockerfile、tag/digest、Python/JAR/constraints、migration有無 | buildだけでなく実読書き・再起動・GUI保存 |
| path/UID/GID | 元と新しいmount、所有者、既存ファイルの権限 | workerの読取、API/Notebookの書込、既存ファイルの非破壊 |
| appName/volume prefix | 全物理volumeの対応、旧writer停止、暗号鍵 | 空DBになっていないこと、既存raw/表/GUI状態の読戻し |
| credentials/暗号鍵 | 対象DBや製品側の変更手順、復元可能な控え | 新認証と旧保存内容の復号。env変更だけで完了しない |

Deployの失敗時は、Git取得、build、container起動、アプリ初期化のどこまで進んだかを分けます。前段が成功したから後段を一括で巻き戻せるとは限りません。旧コードへの切替も、既に進んだDB migrationとデータ形式に互換性があることを確かめてから行います。


Notebook workspaceのread-only計画と承認後のapply/probe、既存projectを明示したSpark検証、機械可読な結果は [compute配置前・検証手順](COMPUTE-VALIDATION.md) を参照します。通常Deployでspark-checkが常駐しないのは正常です。local tagのpull方針はFROM/uv/JAR通信や未解決のBuildKit auth session timeoutを修復する保証ではありません。

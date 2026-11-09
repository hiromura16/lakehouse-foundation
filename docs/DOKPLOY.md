# Dokployでの配置と運用

更新日: 2026-11-09。会社への新規導入では、一つのレイクハウスを一つのProjectにまとめ、`development` と `production` のEnvironmentを分ける運用を推奨します。この文書は汎用の手順で、特定の導入先の設定や稼働状態を示すものではありません。

## ProjectとEnvironmentの分け方

```text
lakehouse-foundation          Project
├── development               Environment
│   └── platform              Docker Compose service
└── production                Environment
    └── platform              Docker Compose service
```

coreのPostgreSQL、S3、RESTカタログ、Trino、workerを一つのComposeサービスとして扱います。コンテナごとのログ・正常性・資源設定はCompose内で管理できます。現段階では、DB、ストレージ、カタログを別々のDokployサービスへ分割するより、マイグレーションと依存関係を同じComposeで管理する方が簡潔です。

n8nと監視はcore内の任意プロファイルで追加します。SparkとAirflowは別のComposeファイルを重ねますが、同じEnvironmentの同じComposeプロジェクトとして配置します。更新周期や運用責任、障害時の扱いが分かれる段階で、役割ごとの配置分割を検討します。

DokployのProject／Environmentは、画面上の所属・設定・権限の整理単位です。**Environment名を変えるだけで、同じホストのネットワーク・ボリューム・ディレクトリ・ポートが自動的に別物になるわけではありません。** 実際の分離は次の項目で確認します。[Dokployの構成階層と変数継承](https://docs.dokploy.com/docs/core/multi-tenancy)

| 項目 | 環境ごとの扱い |
| --- | --- |
| Dokploy `appName` | developmentとproductionで独立した固定名にする |
| `COMPOSE_PROJECT_NAME` | そのEnvironmentの `appName` と一致させる |
| Dockerネットワーク | Composeプロジェクトごとに生成。共有external networkは意図した場合だけ利用 |
| 名前付きvolume | プロジェクトごとのvolumeを使用。固定 `name` や `external` での共有を避ける |
| `LANDING_DIR` | 環境ごとに別の専用ディレクトリを指定 |
| ホストポート | 同じホスト・bind先で重ならない値にする |
| パスワード・暗号鍵・workerトークン | 環境ごとに生成し、その環境のprivate設定で保持 |
| 外部SQL接続 | 環境と用途に合う接続先・権限を指定 |
| 外部S3を共有する場合 | bucket・prefix・Warehouse・資格情報の境界を別途設定 |

Dokployの既定Composeコマンドは `-p <appName>` を指定します。この指定はCompose内の `name` やenvのプロジェクト名より優先されるため、CLIの設定も同じ名前へ合わせます。`appName` は初回配置前に決め、永続データを使い始めた後の変更は移行作業として扱います。[v0.31.0のコマンド生成](https://github.com/Dokploy/dokploy/blob/v0.31.0/packages/server/src/utils/builders/compose.ts)

`production` は環境の名前です。閉域検証用coreをその名前で配置しただけで、本番用の利用者認証、冗長化、バックアップ、復旧試験が完成するわけではありません。運用要件は [ARCHITECTURE.md](ARCHITECTURE.md) を参照してください。

## GitとRawの正本

会社用の推奨は **公開Gitソース** です。Compose、Dockerfile、Python、SQL、依存lockを同じcommitから取得し、Git上のファイルを配置の正本にします。接続先と秘密値はDokployの非公開Environment設定で管理します。

| Source | 用途と扱い |
| --- | --- |
| Git / GitHub | 通常の配置。確認したbranchとcommitをデプロイ記録へ残す |
| Raw | 単独Composeの試験や移行中の構成に使用。内容の版と所有者を記録する |

このリポジトリはDockerfileやSQL等も使うため、Compose本文をRawエディターへ貼るだけでは関連ファイルが揃いません。Rawを使う場合は、必要なイメージを事前にビルド・配布する等の条件を別に用意します。GitとRawの両方を同じ構成の独立した正本として更新しません。[公式Providerの説明](https://docs.dokploy.com/docs/core/providers)

Dokployから一時的に変更した設定は、公開可能なコード変更とprivate設定を分けて回収します。コードはGitへ戻して検証し、次の配置でUI上の変更が消えたり、別の構成へ戻ったりしないようにします。Dockerイメージと依存の固定値はリポジトリ内のlockを正とします。

## 公開Gitから会社へ配置する

### 1. サイト設定を準備する

会社の配置先へリポジトリをcloneし、リポジトリ外にprivate設定を生成します。clone URLはREADMEを参照します。

```bash
python3 scripts/configure.py --output /absolute/private/site.env
```

配置先に合わせて、`appName` と同じ `COMPOSE_PROJECT_NAME`、bind先、ポート、ランディング先、workerのUID/GID、必要なプロファイルを設定します。Sambaの実体と `LANDING_DIR` を一致させ、実行ユーザーのアクセス権も確認します。値を公開Composeへ書き戻しません。

Docker daemonのプロキシ、企業CA、Sambaの設定はDokployのアプリ配置とは別に準備します。詳細は [DEPLOYMENT.md](DEPLOYMENT.md) と [PROXY.md](PROXY.md) を参照します。

```bash
./scripts/lakehouse.sh --env-file /absolute/private/site.env plan
```

この段階は設定の確認だけです。同じアプリをCLIとDokployから別々の名前で二重起動しません。

### 2. ProjectとComposeサービスを作る

DokployでProjectを作り、まず `development` を用意します。そのEnvironmentの中にDocker Composeサービスを一つ作り、配置先サーバーと環境ごとに独立した `appName` を設定します。CLIからも使う名前は小文字・数字・ハイフン／アンダースコアで揃えます。

| 設定欄 | 指定内容 |
| --- | --- |
| Compose type | `Docker Compose` |
| Source | `Git` の公開HTTPS URL、または接続済みGitHub Provider |
| Repository | READMEに記載された公開リポジトリ |
| Branch | 配置する内容を確認したbranch |
| Compose path | `infra/lakehouse/compose.yaml` |
| Environment | 用意したサイト設定のうち、この環境で必要な値 |
| `.env`生成 | Environment設定からの生成を有効にする |
| AutoDeploy | 初回の起動と再配置の確認後に必要なら有効化 |

`Stack` は選びません。この構成はローカルbuild、Composeの依存条件、inline configsを使うDocker Compose方式です。Dokploy自身がSwarmを使うことと、各アプリをStack方式で動かすことは別です。[公式のComposeとStackの説明](https://docs.dokploy.com/docs/core/docker-compose)

### 3. private設定と初回配置を確認する

Environment設定を保存し、`.env`生成が有効なことを確認します。v0.31.0では、上記Compose pathに対する生成先はDokployの作業コピー内の `infra/lakehouse/.env` です。これはDokployが保持する非公開の実行用ファイルで、公開リポジトリへコミットするファイルではありません。

UIの環境変数は、Composeの `${VAR_NAME}` 補間と `environment` から必要なコンテナへ渡します。UIへ登録した全変数が自動で全コンテナへ入る構成ではありません。Project共通設定へ各環境固有の秘密値やプロジェクト名を置くと、継承先を取り違えやすいため、環境またはサービスの設定へ分けます。[Composeの環境変数の扱い](https://docs.dokploy.com/docs/core/docker-compose)

Deployを実行し、build、PostgreSQL、カタログmigration、S3、カタログ、Trino、workerの順で結果を確認します。workerは起動時に初期化を行います。失敗時はログの対象コンテナを選んで原因を確認し、DBやvolumeの削除を初期対応にしません。

GitHubのclone成功は、会社proxy経由のimage取得や依存ダウンロードの成功を保証しません。daemon、build、実行コンテナの通信を分けて確認します。

## 任意Composeを重ねる

`infra/lakehouse/compose.yaml` だけを指定する既定配置では、coreとその中にある `automation`／`monitoring` を扱います。`compute` と `orchestration` は追加ファイルにも定義があるため、複数の `-f` を指定します。

CLIでは `scripts/lakehouse.sh` が3つのファイルをまとめて扱います。DokployではAdvancedのCustom Commandへ同じ組み合わせを指定できます。Custom Commandは既定コマンド全体を置き換え、Dokploy側が先頭へ `docker` を付けます。

次は `appName` が説明用の `lakehouse-development` と一致する場合の入力例です。実際のEnvironmentの `appName` に合わせ、UIの欄へ一つのコマンドとして設定します。

```text
compose -p lakehouse-development --env-file infra/lakehouse/.env -f infra/lakehouse/compose.yaml -f infra/compute/compose.yaml -f infra/orchestration/compose.yaml up -d --build --remove-orphans --wait --wait-timeout 240
```

`COMPOSE_PROFILES` には必要なものだけを指定します。`compute` のSparkスモークは通常の `up` では常駐せず、CLIの `spark-check` で一時実行します。Airflowの接続トークンはAirflowのConnection画面へ登録します。[COMPATIBILITY.md](COMPATIBILITY.md) に追加要件と実行方法があります。

Custom Commandを使うと、pull、build、待機、削除対象等のflagも自分で保持する必要があります。UIが表示する既定値と比較し、別アプリの `appName`、共有volumeを消す操作、意図しないプロファイルを含めません。Preview等で別の `appName` を使う場合も、その名前へ合わせた構成を別に用意します。

## 永続データと配置元のパス

原本・DB・カタログ等は名前付きvolume、ランディングはリポジトリ外の絶対パスを使います。コードはDockerfileでイメージへコピーし、小さな設定はinline configsで渡します。clone先の相対ディレクトリをDBや原本の保存先にしません。

Gitソースの作業コピーはDokployが管理するため、clone先をホスト固有の設定として入力する必要はありません。ホスト側のランディング先は `LANDING_DIR` で指定し、コンテナ内では `/landing` として参照します。イメージ内の `/opt/lakehouse` や公式イメージの `/home/node` はホストのパスと別です。詳しくは [DEPLOYMENT.md のパスの説明](DEPLOYMENT.md#ホストのパスとコンテナ内のパス) を参照してください。

CLIで単発操作する際は、リポジトリ外のprivate設定を `LAKEHOUSE_ENV_FILE` へ設定すれば、毎回の `--env-file` を省略できます。明示したflagが優先されます。旧Rawの導入先固有パスは公開Gitへコピーせず、必要な値をその環境のprivate設定として管理します。

Dokployの再cloneやAutoDeployで作業コピーが更新されても、永続データを別に保持できる構成です。UIのFile Mounts等を追加すると `--project-directory` やパス解決が変わる場合があるため、必要時はbuild contextと全mount先を再確認します。通常の構成では、コードや設定を渡すための追加File Mountsは不要です。[公式のmountと再配置の説明](https://docs.dokploy.com/docs/core/docker-compose)

UIで生成した `.env` に対し、サイト設定の外部保存を前提とするCLIへ直接パスを渡しません。CLIから単発処理を行う場合は、同じ設定を保持するリポジトリ外のprivate env、同じ `appName`／プロジェクト名、同じソース版を使います。

## 旧構成から移行する間の扱い

既存データソースの検証環境や旧自動化は、移行中だけ用途別のEnvironmentに残せます。画面上の所属変更と、実際のコンテナ・ネットワーク・volumeの移動は別作業として記録します。

旧自動化のDB、暗号鍵、volumeを保持し、新構成で復元・接続・実行を確認してから切り替えます。同じ定期処理やWebhookを二重に有効化しません。停止中の旧試験も、保存データと復元の要否を確認してから廃止します。Projectを整理するためだけに既存データを削除しません。

移行が終わった後は、通常運用を `development` と `production` に戻します。残す検証用構成には用途と停止条件を付け、所有者と正本を明確にします。

## 配置後と更新時の確認

1. Environment、`appName`、CLIのプロジェクト名、volumeの所属が一致していることを確認します。
2. サービスの正常性と、原本・メダリオンの検証結果を確認します。
3. 再配置してもデータが保持されることを確かめます。
4. コード版とイメージのlock、private設定の管理先、バックアップ対象を記録します。

通常の配置管理はDokployへ揃え、単発の取り込み・検証等をCLIから実行する場合も同じ環境へ向けます。複数の正本から同じアプリを同時に再配置しません。会社のproxy、外部SQL、利用者認証、バックアップからの復元は、それぞれ実環境で確認した結果を記録します。

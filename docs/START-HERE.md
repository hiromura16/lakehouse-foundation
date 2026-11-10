# 初めて構築する人の一本道ガイド

確認日: 2026-11-10（JST）。**このページを上から進め、各段階の成功条件を確認してから次へ進みます。** 新規のLinux x86-64サーバーと、操作用PCを想定します。採用版は [公式リファレンスと文書監査表](REFERENCES.md)、仕組みは [構成図](ARCHITECTURE.md) にあります。会社で一時修正した既存環境の更新は [COMPANY-UPDATE.md](COMPANY-UPDATE.md) を使います。

## 0. 作業場所と管理方法を決める

| 言葉 | この手順での意味 |
| --- | --- |
| サーバー端末 | 配置するLinuxへSSH接続した端末。特記がなければここで実行 |
| 操作用PC | ブラウザーを開く手元のPC。ここでのlocalhostはサーバーではない |
| 通常ユーザー | Docker操作を許可された配置担当。Python・Git・日常操作を実行 |
| 管理者 | `sudo` が必要なホスト準備だけを実行 |
| private設定 | サーバー上のGit外にある `site.env`。秘密値を含む |
| profile | 起動する追加機能の選択。coreは常に含む |

日常の配置担当を **DokployかCLIのどちらか一つ** に決めます。Dokploy管理なら配置・停止・再配置はDokployで行い、CLIは同じ環境の `plan` / `status` / `verify` / `grafana-init` 等の補助操作に使います。同じ環境へ別のCompose名で `up` すると、別DB・別volumeを作る原因になります。

初回はcoreだけを確認し、その後 `demo,monitoring` を追加します。全機能は11profile・34service定義で、全コンテナの上限メモリを同時に確保すると20GiBを超えます。Docker/Dokploy/OS/build用メモリも必要です。小さいVMでは段階ごとに使用量を確認し、全機能の同時起動を前提にしません。[容量と永続化](DEPLOYMENT.md)

## 1. ホストを準備する

通常ユーザーで実行します。

```bash
uname -m
python3 --version
git --version
uv --version
docker version
docker compose version
docker buildx version
free -h
ss -lnt
```

成功条件は `x86_64`、Python 3.12以上、uv 0.13.7、DockerのClientとServerの両方が表示されることです。Composeは2.23.1以上、今回の互換検証はDocker 29系とCompose 5.6.1です。`permission denied` ならホスト管理者がDocker操作権限を準備します。Pythonが古ければOS標準を置換せず、会社標準の方法で対応版を追加して `python3` の実行先を確認します。

Dockerが未導入のUbuntuでは、管理者が [Docker公式のUbuntu導入手順](https://docs.docker.com/engine/install/ubuntu/) のapt repository方式で **Engine・CLI・containerd・Buildx・Compose plugin** を導入します。既存コンテナのあるホストで競合パッケージを無条件に削除しません。Dockerへの通常ユーザーアクセスは [公式のLinux導入後設定](https://docs.docker.com/engine/install/linux-postinstall/) に従います。Dockerグループは強いホスト権限を持つため、許可された配置担当だけに付けます。

uvは [0.13.7の公式配布](https://github.com/astral-sh/uv/releases/tag/0.13.7) を使います。Pythonの追加依存用venvもuvで作成します。ホストへ一括 `pip install` する必要はありません。

会社proxyが必要なら、Git・apt・uv取得用の会社標準設定を先に用意します。Docker導入後、**image pullの前に** [COMPANY-PROXY.md](COMPANY-PROXY.md) のdaemon proxy・企業CA準備を済ませます。アプリのenvだけではdaemonのpullを設定できません。

## 2. 公開Gitを取得してprivate設定を作る

通常ユーザーで、書き込み可能な任意の作業ディレクトリへcloneします。

```bash
git clone https://github.com/hiromura16/lakehouse-foundation.git
cd lakehouse-foundation
git rev-parse HEAD
python3 scripts/configure.py \
  --output "$HOME/.config/lakehouse-foundation/site.env"
export LAKEHOUSE_ENV_FILE="$HOME/.config/lakehouse-foundation/site.env"
```

以降のサーバー端末のコマンドはこのリポジトリのルートで実行します。別のSSHセッションでは再び `cd` と `export LAKEHOUSE_ENV_FILE=...` が必要です。生成器は既存ファイルを上書きしません。既存環境なら再生成せず、[更新手順](COMPANY-UPDATE.md) の `--upgrade` を使います。

エディターでprivate設定を開きます。`site.env` はshellではなく設定データです。`source` せず、`$HOME` や `$(id -u)` を値へ書きません。パスにはそのサーバー上の実際の絶対パスを書きます。公開Composeを直接書き換える必要はありません。

| key | 最初の設定 |
| --- | --- |
| `COMPOSE_PROFILES` | 初回は空欄。core成功後に `demo,monitoring` |
| `COMPOSE_PROJECT_NAME` | CLIなら環境ごとの固定名。Dokployなら作成後の確定appNameへ変更する |
| `DATA_VOLUME_PREFIX` | 新規は空。既存データの引継ぎでは元の値を保持 |
| `LANDING_DIR` | 専用の外部絶対パス。新規例は `/srv/lakehouse/landing` |
| `WORKER_UID` / `WORKER_GID` | 下記 `id` の数値。生成時は通常ユーザーの値が設定される |
| `CONTROL_SETTINGS_DIR` / `CONTROL_SETTINGS_NAME` | 生成器が設定するprivate設定の親dir / ファイル名 |
| `BIND_IP` | 初回は `127.0.0.1` |
| `LAKEHOUSE_NO_PROXY` | 生成された内部名一覧を保持し、会社の内部宛先を追加 |
| `HTTP_PROXY` / `HTTPS_PROXY` と小文字版 | proxyを使う場合だけ同じ値を両caseへ設定 |
| `CORPORATE_CA_FILE` | 必要な場合だけGit外のPEM絶対パス。未使用なら空 |

パスワード・暗号鍵・tokenは生成値を保持します。全項目の意味は [CONFIGURATION.md](CONFIGURATION.md) です。GUIの接続診断で企業CAを使う場合はprivate設定専用dirの配下へ置きます。CA未指定時の空ファイルは同梱済みで、`/dev/null` への差し替えは不要です。

```bash
id -u
id -g
stat -c '%a %u:%g %n' "$LAKEHOUSE_ENV_FILE"
stat -c '%a %u:%g %n' "$(dirname "$LAKEHOUSE_ENV_FILE")"
```

成功条件はファイル0600・専用親dir0700で、所有者が `WORKER_UID/GID` と一致することです。**秘密ファイルの内容を導入記録やGitへ貼り付けません。**

## 3. データを置く場所を準備する

新しい専用landingを用意する例です。既存のディレクトリへ一括chownを行わず、用途と所有者を先に確認します。

```bash
LANDING_PATH=/srv/lakehouse/landing
if [ -e "$LANDING_PATH" ] || [ -L "$LANDING_PATH" ]; then
  stat -c '%a %u:%g %n' "$LANDING_PATH"
else
  sudo install -d -m 0770 -o "$(id -un)" -g "$(id -gn)" "$LANDING_PATH"
fi
stat -c '%a %u:%g %n' /srv/lakehouse/landing
./scripts/lakehouse.sh plan
```

private設定の `LANDING_DIR` と上のパスを揃えます。成功時は `Compose configuration valid` と `Profiles: core only` が表示され、コンテナはまだ起動しません。Sambaが必要なら [会社導入の共有準備](COMPANY-ROLLOUT.md) へ進みます。SambaなしでもGUIからファイルを生成できます。

## 4A. Dokployで配置する場合

**Dokploy本体の導入と、その上へのレイクハウスDeployは別の作業です。** 既存のDokploy 0.31.0を使う場合はインストーラーを省略し、次のGUI登録へ進みます。他の版では画面名や挙動を読み替える前に互換性を確認します。

新規導入ではprivate設定へ `DOKPLOY_BIND_IP`、`DOKPLOY_ALLOWED_CIDRS`、`SWARM_ADVERTISE_ADDR`、LAN/VPNと重ならない `SWARM_ADDRESS_POOL` / `SWARM_SUBNET_SIZE` を入力します。値はホスト管理者・ネットワーク担当と決めます。管理画面portの既定は3100です。

```bash
# 設定値の検査と計画表示。Dockerやportを含む全ホスト検査ではない。
bash infra/dokploy/install.sh --env-file "$LAKEHOUSE_ENV_FILE"

# 管理者がホスト前提を検査し、導入する。
sudo bash infra/dokploy/install.sh --env-file "$LAKEHOUSE_ENV_FILE" --apply
```

成功後、許可した管理用アドレスとportでDokploy画面を開き、初期管理者を登録します。インストーラーが既存Swarmを拒否したら、`swarm leave` / resetで回避せず [Dokployの既存環境手順](DOKPLOY.md) を確認します。公式の汎用 `curl | sh` と、この固定版インストーラーを続けて二重実行しません。

### GUIへ公開Gitを登録する

次は **0.31.0** の表示です。各入力位置・公式tagへの根拠は [DOKPLOY.md](DOKPLOY.md) にあります。

1. Projectを一つ作成します。初期の `production` に加え、Environmentの選択メニューから `Create Environment` で `development` を作成して開きます。名前だけで本番認可が有効になるものではありません。
2. `Create Service` から `Compose` を作成し、`Compose Type` は `Docker Compose` を選びます。
3. `General` → `Provider` カード → **Git** タブを選びます。公開URLの登録にGitHub Provider接続は不要です。
4. `Repository URL` = `https://github.com/hiromura16/lakehouse-foundation.git`、`Branch` = `main`、`Compose Path` = `infra/lakehouse/compose.yaml` を保存します。
5. サービス画面ヘッダーの表示名直下から、生成後の確定appNameを控えます。作成時の入力名にランダムsuffixが付くため、入力した名前をそのまま使いません。
6. サーバー上のprivate設定の `COMPOSE_PROJECT_NAME` を、その確定名と一致させます。ここまでコンテナはDeployしません。
7. **Composeサービス自身のEnvironmentタブ** にprivate設定の内容を登録します。`Create Environment File` をONにして保存します。このCompose Pathでは `infra/lakehouse/.env` が生成されます。Project/Environment変数を置くだけでは自動継承されません。
8. `Advanced` → **Run Command** → `Command` に下記を設定して保存します。`APP_NAME_FROM_HEADER` は必ず先ほどの確定名へ置き換えます。
9. `Auto Deploy` は初回OFFのまま `Deploy` を押し、Deploymentの完了とサービスの状態を確認します。

```text
compose -p APP_NAME_FROM_HEADER --env-file infra/lakehouse/.env -f infra/lakehouse/compose.yaml -f infra/compute/compose.yaml -f infra/orchestration/compose.yaml -f infra/demo/compose.yaml -f infra/cdc/compose.yaml -f infra/poc/compose.yaml -f infra/mcp/compose.yaml up -d --build --remove-orphans --wait --wait-timeout 600
```

欄には `compose` から入力します。Dokployが `docker` を付けるため、`docker compose` は入力しません。7つの `-f` は追加機能が未選択でも残します。ここはshellスクリプト欄ではなく、pipe・`&&`・複数コマンドは使えません。profileはEnvironmentの `COMPOSE_PROFILES` で選びます。

`catalog-migrate` は終了コード0、その他coreは起動/healthyを確認します。one-shotの `Exited (0)` は正常です。build失敗なら [TROUBLESHOOTING.md](TROUBLESHOOTING.md) でproxy・CA・不足資源を切り分けます。設定を変更しただけでは配置済みコンテナは変わりません。

配置後、通常ユーザーのサーバー端末で同じprivate設定・確定appNameを使います。

```bash
./scripts/lakehouse.sh status
./scripts/lakehouse.sh verify
```

`verify` 成功時は `ok: true`、11検査（業務SQL10＋画像1）、再投入7件skipを確認します。Dokploy管理では次の4Bを実行せず、5へ進みます。

## 4B. CLIで配置する場合

Dokployを使用しない環境だけ、サーバー端末で実行します。

```bash
./scripts/lakehouse.sh up
./scripts/lakehouse.sh status
./scripts/lakehouse.sh verify
```

`up` がbuild・起動・bootstrapを行い、monitoring選択時はGrafana初期dashboardも一度だけ作成します。成功条件は4Aと同じです。

## 5. 操作GUI・監視を追加してブラウザーで開く

private設定を `COMPOSE_PROFILES=demo,monitoring` にします。Dokploy管理ならComposeサービスのEnvironmentにも同じ値を保存し、手順4Aの7ファイルのCommandを使ってDeployします。CLI管理だけが `./scripts/lakehouse.sh up` を使います。両管理方法で `./scripts/lakehouse.sh plan` は実行できます。

配置成功後、Dokploy管理では通常ユーザーで標準dashboardを一度だけ作成します。CLIのupを使った場合は自動実行済みです。

```bash
./scripts/lakehouse.sh grafana-init
```

既存UIDのGUI編集は上書きしません。`demo-api` / `demo` / `prometheus` / `grafana` の稼働を確認します。private設定dirとlandingのUID/GIDが一致しなければ、まず手順2・3の権限を修正します。

サーバー自身のブラウザーなら操作GUI `http://localhost:8501/`、Swagger `http://localhost:8502/docs`、Grafana `http://localhost:3000/` を開きます。**別のPCで作業する場合は操作用PCの端末** で次を実行し、接続中のままブラウザーを開きます。最初の2行は自分のSSH接続先を入力します。

```bash
read -r -p 'サーバーのSSHユーザー: ' SSH_LOGIN
read -r -p 'サーバーのSSHホスト: ' SSH_TARGET
ssh -N -L 8501:127.0.0.1:8501 -L 8502:127.0.0.1:8502 \
  -L 3000:127.0.0.1:3000 "$SSH_LOGIN@$SSH_TARGET"
```

変更したhost portを使う場合は右側のportも合わせます。左側のportがPC上で使用中なら別の空portを使い、その番号でブラウザーを開きます。接続を共有する段階では、会社標準の認証・HTTPS・接続制限を設けます。StreamlitはAPIへサーバー側認証しますが、Streamlit画面自体の利用者別ログインは同梱していません。

### 初回のデータ往復

1. 操作GUIの「素材の投入」で、投入先ランディング、batch、seed42、20件、7日、最初はcleanを選択し、開始します。
2. 画面下の「実行結果を確認する」欄で「進捗を更新」を押し、成功と6ソースの行数・manifestを確認します。画像を選んだ場合は画像1枚も確認します。
3. 「処理・検証」→「Silver・Goldまで変換」を一度押し、完了を待ちます。
4. 「データ」でcatalogを読み、「Goldをプレビュー」と「品質件数を確認」を実行します。
5. 「AIの準備」でsnapshot ID付きのJSONを取得します。これだけではLLMを起動したり、社外へ送信したりしません。
6. 次に [異常シナリオ](ANOMALY-SCENARIOS.md) の品質・改訂・工場ロスを一つずつ試し、結果の変化を比較します。

通常のGUI runには独立したIDが付き、追加するたびに素材が増えます。GUIで累積した件数を固定のverify fixture155行と比較しません。固定期待値の検証は隔離namespaceを使う「基盤の往復検証」です。

APIも試す場合はSwaggerの `Authorize` へprivate設定の `CONTROL_API_TOKEN` 本体を入力し、まず `GET /capabilities`、`GET /catalog`、`GET /jobs` を使います。書込みAPIは操作内容を確認して実行します。[APIの例と結果確認](API.md)

## 6. 必要な機能を一つずつ追加する

毎回、private設定の **既存profileを保持して** カンマで追加します。GUIの「設定を保存」はprivateファイルの保存だけです。Dokploy管理ではComposeサービスのEnvironmentにも同じ変更を反映して保存→Deploy、CLI管理では `plan`→`up` を実行します。両者は自動同期しません。

| 順番・profile | 最初に行うこと | 開く画面・成功条件 | 手順 |
| --- | --- | --- | --- |
| `automation` | n8n初回owner登録、資格情報へworker token、同梱workflowをimport | 5678で手動実行が成功 | [GUI手順](GUI-WALKTHROUGHS.md)・[SSRF](SSRF.md) |
| `compute` | Master/Worker/Historyを常駐。UIDとvolumeを保持 | 8082/8085/18080、Worker登録・ジョブ履歴 | [互換性とSpark](COMPATIBILITY.md) |
| `notebooks` | `NOTEBOOKS_DIR` を外部に作り同じUID/GIDで書込み可能にする | 8888、private tokenでログイン、compute有効ならremote Spark | [Jupyterと初期token](POC-TOOLS.md) |
| `bi` | 初期Superset秘密設定を確認。one-shot initの正常終了を待つ | 8088、初期adminでログイン、Gold2datasetを探索 | [Superset](POC-TOOLS.md) |
| `vector` | Qdrantのwrite/readキーを保持 | 6333/dashboard、合成vectorの検索。埋め込み生成は別途 | [Qdrant](POC-TOOLS.md) |
| `lineage` | amd64・専用DBを確認 | 3001、Marquezへ手動seedした系統を表示 | [Marquez](POC-TOOLS.md) |
| `orchestration` | Airflow初期pw取得、HTTP Connectionを作る | 8081、手動DAG `lakehouse_medallion` 成功 | [Airflowの画面操作](GUI-WALKTHROUGHS.md) |
| `mcp` | privateのMCP token、Host制限、クライアント接続を設定 | 既定8503/mcp、参照toolを確認 | [MCP](MCP.md) |
| `cdc` | **有効化前に** DBAがAgent・CDC・専用参照権限・6表map・TLSを準備 | 8084、Connect RUNNING・topic・lag・各層のI/U/Dを照合 | [CDCの準備から受入まで](CDC.md) |

追加画面を遠隔で開く場合は、そのhost portもSSH転送に追加します。初期資格情報は各専用ガイドに従いサーバー内で確認し、公開ログへ貼りません。n8nとAirflowの定期実行担当は一つに決めます。同梱workflow/DAGは手動開始です。

Sparkは常駐クラスタを使います。明示の互換スモークも実行できます。

```bash
./scripts/lakehouse.sh spark-check
```

スモークはlocal[2]で専用compatibility表へ書き、Trinoから参照します。Jupyterからremote workerへ投入する確認とは別です。**会社データは別の空の環境で受け入れます。** このGUI例の合成素材は通常のBronzeへ残り、landingからファイルを消すだけでは削除されません。別のDokploy Environment/Compose（またはCLI project）、新しいvolume・landing・private設定・空いているportを用意し、会社データ側では合成素材を生成しません。隔離namespaceだけを使う `verify` は実行できます。会社データへの接続は [SQL読取り](SOURCE-SQLSERVER.md)・[会社の受入手順](COMPANY-ROLLOUT.md) に進みます。SQL Server本体はこのリポジトリから起動しません。

## 7. 停止・更新・復旧を決める

Dokploy管理はGUIのStop、CLI管理は次を使います。

```bash
./scripts/lakehouse.sh down
```

通常停止では名前付きvolumeを保持します。`down -v` やvolume pruneはこの手順に含めません。再開・更新時も同じappName・volume prefix・暗号鍵・保存先を継続します。更新は [COMPANY-UPDATE.md](COMPANY-UPDATE.md)、整合したバックアップと別環境復元は [OPERATIONS.md](OPERATIONS.md) を使います。

配置commit、profile、各段階の成功条件だけを導入記録へ残します。実接続先・秘密値・実データ・生ログ・画面に映る資格情報は公開リポジトリへ入れません。一般化した記録と [VALIDATION.md](VALIDATION.md) の既存試験を区別します。

## PoCの結果を説明する

[報告書用素材集](reports/README.md)から、構成・処理・検証結果の図と報告書雛形を選べます。会社で記入する結果と既存の合成試験は分け、性能・費用・復元・推論などの未評価を成果値で埋めません。

# プロキシ設定の場所と反映方法

2026-11-10監査。会社で初めて設定する場合は、順番と診断をまとめた [COMPANY-PROXY.md](COMPANY-PROXY.md) を先に読みます。本書は設定ファイルの役割を調べるための参照です。操作はレイクハウスを動かすLinuxホストのclone済みリポジトリで行い、`sudo` の付いた箇所だけホスト管理者が実施します。

## どの通信を設定するか

| 通信する主体 | 読む設定 | 反映する操作 |
| --- | --- | --- |
| ホストのGit／uv | 会社標準の端末proxyとCA | そのclientでclone／依存取得を確認 |
| Docker daemon | systemdのdaemon専用 `proxy.env`、ホストの信頼CA | 管理者が保守時間にDockerを再起動 |
| 自作imageのbuild | `site.env` のproxy項目とBuildKit CA secret | 同じ配置元でimageをbuild |
| 起動済みコンテナ | Composeのenvironment、各clientの設定 | 同じアプリを再配置 |
| Java／SQL Server | JVMのproxy・truststore、SQL接続TLS | 製品別に設定して実通信を確認 |

daemon用ファイルはコンテナへ自動配布されません。Docker CLIの `~/.docker/config.json` にあるproxy指定もdaemon設定とは別です。[Docker daemon](https://docs.docker.com/engine/daemon/proxy/)、[Docker CLI](https://docs.docker.com/engine/cli/proxy/)

## 1. daemon専用ファイルを作る

通常利用者が次を実行します。生成先はリポジトリ外、専用directory0700、file0600です。

```bash
./scripts/configure-proxy.sh --init "$HOME/.config/lakehouse-foundation/proxy.env"
```

生成済みファイルをローカルのエディターで開きます。受け付けるのは `HTTP_PROXY,HTTPS_PROXY,NO_PROXY,http_proxy,https_proxy,no_proxy` の6項目だけです。認証なしの形は次のとおりです。

```dotenv
HTTP_PROXY=http://proxy.example.invalid:3128
HTTPS_PROXY=http://proxy.example.invalid:3128
http_proxy=http://proxy.example.invalid:3128
https_proxy=http://proxy.example.invalid:3128
```

`NO_PROXY` / `no_proxy` は生成済みの内部名を残して同じ値にします。上の4行だけでファイル全体を置き換えません。proxyを使わないときはHTTP(S)4項目を空にします。URLは引用符・空白・shell展開を使わず、userinfoの予約文字をURLエンコードします。PAC/WPADやNTLM/KerberosはこのURL設定だけで対応済みとは扱いません。

確認後、必要な場合だけ管理者が配置します。

```bash
./scripts/configure-proxy.sh --env-file "$HOME/.config/lakehouse-foundation/proxy.env" --plan
sudo ./scripts/configure-proxy.sh --env-file "$HOME/.config/lakehouse-foundation/proxy.env" --apply
```

planは値を表示せず検査します。applyは `/etc/lakehouse-foundation/proxy.env`（root所有0600）と `/etc/systemd/system/docker.service.d/90-lakehouse-foundation-proxy.conf`（0644）を配置し、Dockerを再起動しません。管理外の配置先や `daemon.json` のproxy定義があれば拒否します。他のdrop-inの競合は管理者が別途確認します。rootless Docker／Docker Desktopへはこのsystem用手順を適用しません。

保守時間に管理者が実施し、承認済みregistryから固定imageを取得できることを確認します。

```bash
sudo systemctl daemon-reload
sudo systemctl restart docker
```

`EnvironmentFile` はshellとして実行されません。ファイル内のURLエンコードは `%` のままで、systemdの `Environment=` 行へ直接書く場合の `%%` と混同しません。[systemd環境ファイル](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html)

## 2. アプリ用site.envを正規化する

daemonファイルとは別に、private `site.env` のHTTP(S)4項目を設定します。アプリのbypassは次の3項目を同じ値にします。

| 項目 | 役割 |
| --- | --- |
| `LAKEHOUSE_NO_PROXY` | Compose補間用canonical。全7Composeと追加テンプレートが参照 |
| `NO_PROXY` / `no_proxy` | clientへ渡す大小文字の互換値。canonicalへ同期 |

生成・`--upgrade`・CLI読込は、必須内部名とprivateファイル内の既存3項目を結合して重複を除きます。shellの古い除外先は採用しません。通常利用者がリポジトリのルートで実行します。

```bash
python3 scripts/configure.py --output /absolute/private/site.env --upgrade
./scripts/lakehouse.sh --env-file /absolute/private/site.env plan
```

upgradeは既存秘密値を保持し、変更前を隣の `.before-upgrade` に0600で保存します。変更が必要でその控えが既にあれば上書きせず停止します。先に控えをprivate領域の別名へ保管します。詳しい更新順は [COMPANY-UPDATE.md](COMPANY-UPDATE.md) です。

`storage,catalog,trino,worker` などは実際のDocker内部名です。SQL・会社API・社内registry等の追加除外先はprivateに追記します。CIDR等の意味はclientごとに異なるため、必要なhost名を明示します。SQL ServerのTCP経路をHTTP proxyで作る設定ではありません。

## 3. 配置の所有者を一つにする

CLI管理なら、proxyをsite設定へ揃えた後に同じ利用者が次を実行します。

```bash
./scripts/lakehouse.sh --env-file /absolute/private/site.env up
./scripts/lakehouse.sh --env-file /absolute/private/site.env verify
```

Dokploy管理ならupgrade後の `LAKEHOUSE_NO_PROXY` を含む同じsite設定を既存アプリのEnvironmentへ登録し、そのアプリを再配置します。CLIから別名の同じスタックを新設しません。GUI保存はprivateファイルの更新までで、DokployのEnvironmentとは自動同期しません。

raw Composeはshell変数が `--env-file` より優先されます。古いshellの `NO_PROXY/no_proxy` を避けるため専用canonicalを使いますが、`LAKEHOUSE_NO_PROXY` 自体を別値でexportするとraw操作ではその値が優先されます。通常は検査済み値を明示するCLIを使います。[Compose公式の補間優先順位](https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/)

canonicalのない旧raw設定では必須内部名をfallbackで補い、両caseを一致させます。この経路はCSV重複除去や、shellに隠れたprivate追加値の復元をしません。会社追加分を保持する配置では先にupgradeが必要です。

`infra/proxy/compose.proxy.yaml.example` は追加サービス向けの見本です。拡張blockだけでは何も適用されず、environment／build.argsから参照します。同梱サービスは既に設定済みなので、例を全サービスへ重ねる必要はありません。

### proxyファイルを補助入力にする場合

直接Composeでは `--env-file site.env --env-file proxy.env` の順で後者が同名変数を上書きします。ただしsiteのcanonicalがあると後者の旧 `NO_PROXY` だけではbypassは変わりません。通常はsite設定に統合し、upgradeしてCLIを使います。daemon用6項目ファイルへcanonicalを加えません。補助入力が必要ならprivateなサイト設定側で3項目を同期し、全7Compose、同じproject/profile/volume名を使って `config --quiet` で確認します。raw `up` はCLIのwarehouse bootstrapやGrafana初期登録を代行しません。[配置手順](DEPLOYMENT.md)

## 4. 企業CA・uv・Javaを設定する

`CORPORATE_CA_FILE` はホスト上の公開CA PEM chainです。自作imageはBuildKit secretから既存OS/JRE trustへ追加します。空欄／未設定時の `infra/proxy/empty-ca.pem` は**0バイトのregular file**です。実証明書を書き込まず、削除せず、`/dev/null` へ置き換えません。Buildx Bakeでも使える任意CAの入力です。CA更新・削除時はcacheなしで再buildします。[Build secretとcache](https://docs.docker.com/build/cache/invalidation/#build-secrets)

公式n8n2.46.7は再buildせず、公開CAだけをread-only bindし `NODE_EXTRA_CA_CERTS` で追加します。CA空欄なら追加CA変数も空欄です。TLS検証を無効化する設定へ変更しません。完全な手順は [会社CA設定](COMPANY-PROXY.md) を参照します。

hostのPython環境はuvで作ります。uv0.13.7ではOS trust利用に `UV_SYSTEM_CERTS=true`／`--system-certs` を使えます。`SSL_CERT_FILE` を指定するときは必要な公開rootも保持します。`PIP_CERT` はpip向けで、uvの設定と同一ではありません。[uv TLS](https://docs.astral.sh/uv/concepts/authentication/certificates/)

Javaは環境変数だけで全clientへproxyが適用されるとは限りません。JVMの `http.nonProxyHosts` は `|` 区切りです。SQL Server用Java truststoreとHTTP用CAも区別します。[JDKネットワーク設定](https://docs.oracle.com/en/java/javase/25/docs/api/java.base/java/net/doc-files/net-properties.html)、[CDC TLS](CDC.md)

## 確認・戻し方・対象版

hostの `proxy-doctor.py`、操作画面のネットワーク診断、daemonのpull、image build、内部S3/Trino、実n8n HTTP Requestを個別に確認します。一つの成功で全clientの接続成功とは判断しません。実会社の宛先・資格情報・展開済みComposeは公開ログへ出しません。

直接接続へ戻すときはHTTP(S)4項目を空にし、daemonはapply後の明示restart、コンテナは既存アプリの再配置を行います。canonical3項目は残します。CA削除には空欄からのcacheなしbuildが必要です。

監査対象はDocker Engine29系／[Compose5.6.1](https://github.com/docker/compose/releases/tag/v5.6.1)、uv0.13.7、n8n2.46.7です。版は各Compose/Dockerfile/lockを正本とし、hostのEngineとComposeは管理者が記録します。会社proxyの受入は導入先で別途実施します。

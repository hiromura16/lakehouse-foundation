# プロキシ設定の場所と反映方法

<!-- doc-purpose:start -->
> **役割:** 通信主体別のproxy設定ファイル、CA受渡しと反映方法を参照する。
>
> **対象読者:** 基盤構築担当 / 開発・設計担当
>
> **読むタイミング:** 会社向け手順を読んだ後に、個別の設定場所や再起動対象を調べるとき。
>
> **前提となる資料:** [会社のプロキシ・CAを設定する](<COMPANY-PROXY.md>)
>
> **次に読む資料:** [サイト設定リファレンス](<CONFIGURATION.md>) / [SSRF対策と内部接続の設定](<SSRF.md>)
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

**Bash / Zsh共通**

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

**Bash / Zsh共通**

```bash
./scripts/configure-proxy.sh --env-file "$HOME/.config/lakehouse-foundation/proxy.env" --plan
sudo ./scripts/configure-proxy.sh --env-file "$HOME/.config/lakehouse-foundation/proxy.env" --apply
```

planは値を表示せず検査します。applyは `/etc/lakehouse-foundation/proxy.env`（root所有0600）と `/etc/systemd/system/docker.service.d/90-lakehouse-foundation-proxy.conf`（0644）を配置し、Dockerを再起動しません。管理外の配置先や `daemon.json` のproxy定義があれば拒否します。他のdrop-inの競合は管理者が別途確認します。rootless Docker／Docker Desktopへはこのsystem用手順を適用しません。

保守時間に管理者が実施し、承認済みregistryから固定imageを取得できることを確認します。

**Bash / Zsh共通**

```bash
sudo systemctl daemon-reload
sudo systemctl restart docker
```

`EnvironmentFile` はshellとして実行されません。ファイル内のURLエンコードは `%` のままで、systemdの `Environment=` 行へ直接書く場合の `%%` と混同しません。[systemd環境ファイル](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html)

## 2. アプリ用site.envを正規化する

daemonファイルとは別に、private `site.env` のHTTP(S)4項目を設定します。アプリのbypassは次の3項目を同じ値にします。

| 項目 | 役割 |
| --- | --- |
| `LAKEHOUSE_NO_PROXY` | Compose補間用canonical。全9Composeと追加テンプレートが参照 |
| `NO_PROXY` / `no_proxy` | clientへ渡す大小文字の互換値。canonicalへ同期 |

生成・`--upgrade`・CLI読込は、必須内部名とprivateファイル内の既存3項目を結合して重複を除きます。shellの古い除外先は採用しません。通常利用者がリポジトリのルートで実行します。

**Bash / Zsh共通**

```bash
python3 scripts/configure.py --output /absolute/private/site.env --upgrade
./scripts/lakehouse.sh --env-file /absolute/private/site.env plan
```

upgradeは既存秘密値を保持し、変更前を隣の `.before-upgrade` に0600で保存します。変更が必要でその控えが既にあれば上書きせず停止します。先に控えをprivate領域の別名へ保管します。詳しい更新順は [COMPANY-UPDATE.md](COMPANY-UPDATE.md) です。

`storage,catalog,trino,worker` などは実際のDocker内部名です。SQL・会社API・社内registry等の追加除外先はprivateに追記します。CIDR等の意味はclientごとに異なるため、必要なhost名を明示します。SQL ServerのTCP経路をHTTP proxyで作る設定ではありません。

## 3. 配置の所有者を一つにする

CLI管理なら、proxyをsite設定へ揃えた後に同じ利用者が次を実行します。

**Bash / Zsh共通**

```bash
./scripts/lakehouse.sh --env-file /absolute/private/site.env up
./scripts/lakehouse.sh --env-file /absolute/private/site.env verify
```

Dokploy管理ならupgrade後の `LAKEHOUSE_NO_PROXY` を含む同じsite設定を既存アプリのEnvironmentへ登録し、そのアプリを再配置します。CLIから別名の同じスタックを新設しません。GUI保存はprivateファイルの更新までで、DokployのEnvironmentとは自動同期しません。

raw Composeはshell変数が `--env-file` より優先されます。古いshellの `NO_PROXY/no_proxy` を避けるため専用canonicalを使いますが、`LAKEHOUSE_NO_PROXY` 自体を別値でexportするとraw操作ではその値が優先されます。通常は検査済み値を明示するCLIを使います。[Compose公式の補間優先順位](https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/)

canonicalのない旧raw設定では必須内部名をfallbackで補い、両caseを一致させます。この経路はCSV重複除去や、shellに隠れたprivate追加値の復元をしません。会社追加分を保持する配置では先にupgradeが必要です。

`infra/proxy/compose.proxy.yaml.example` は追加サービス向けの見本です。拡張blockだけでは何も適用されず、environment／build.argsから参照します。同梱サービスは既に設定済みなので、例を全サービスへ重ねる必要はありません。

### proxyファイルを補助入力にする場合

直接Composeでは `--env-file site.env --env-file proxy.env` の順で後者が同名変数を上書きします。ただしsiteのcanonicalがあると後者の旧 `NO_PROXY` だけではbypassは変わりません。通常はsite設定に統合し、upgradeしてCLIを使います。daemon用6項目ファイルへcanonicalを加えません。補助入力が必要ならprivateなサイト設定側で3項目を同期し、全9Compose、同じproject/profile/volume名を使って `config --quiet` で確認します。raw `up` はCLIのwarehouse bootstrapやGrafana初期登録を代行しません。[配置手順](DEPLOYMENT.md)

## 4. 企業CA・uv・Javaを設定する

`CORPORATE_CA_FILE` はホスト上の公開CA PEM chainです。自作imageはBuildKit secretから既存OS/JRE trustへ追加します。空欄／未設定時の `infra/proxy/empty-ca.pem` は**0バイトのregular file**です。実証明書を書き込まず、削除せず、`/dev/null` へ置き換えません。Buildx Bakeでも使える任意CAの入力です。CA更新・削除時はcacheなしで再buildします。[Build secretとcache](https://docs.docker.com/build/cache/invalidation/#build-secrets)

公式n8n2.46.7は再buildせず、公開CAだけをread-only bindし `NODE_EXTRA_CA_CERTS` で追加します。CA空欄なら追加CA変数も空欄です。TLS検証を無効化する設定へ変更しません。完全な手順は [会社CA設定](COMPANY-PROXY.md) を参照します。

hostのPython環境はuvで作ります。uv0.13.7ではOS trust利用に `UV_SYSTEM_CERTS=true`／`--system-certs` を使えます。`SSL_CERT_FILE` を指定するときは必要な公開rootも保持します。`PIP_CERT` はpip向けで、uvの設定と同一ではありません。[uv TLS](https://docs.astral.sh/uv/concepts/authentication/certificates/)

Javaは環境変数だけで全clientへproxyが適用されるとは限りません。JVMの `http.nonProxyHosts` は `|` 区切りです。SQL Server用Java truststoreとHTTP用CAも区別します。[JDKネットワーク設定](https://docs.oracle.com/en/java/javase/25/docs/api/java.base/java/net/doc-files/net-properties.html)、[CDC TLS](CDC.md)

## 確認・戻し方・対象版

hostの `proxy-doctor.py`、操作画面のネットワーク診断、daemonのpull、image build、内部S3/Trino、実n8n HTTP Requestを個別に確認します。一つの成功で全clientの接続成功とは判断しません。実会社の宛先・資格情報・展開済みComposeは公開ログへ出しません。

直接接続へ戻すときはHTTP(S)4項目を空にし、daemonはapply後の明示restart、コンテナは既存アプリの再配置を行います。canonical3項目は残します。CA削除には空欄からのcacheなしbuildが必要です。

監査対象はDocker Engine29系／[Compose5.6.1](https://github.com/docker/compose/releases/tag/v5.6.1)、uv0.13.7、n8n2.46.7です。版は各Compose/Dockerfile/lockを正本とし、hostのEngineとComposeは管理者が記録します。会社proxyの受入は導入先で別途実施します。

## 設定を調べる時の3つの層

| 層 | 問い | 正本 |
| --- | --- | --- |
| 保存値 | 次の配置に何を使いたいか | 専用private `site.env` と管理対象のdaemonファイル |
| 配置値 | 現在のコンテナやdaemonが何を読んで起動したか | 同じアプリの配置記録・起動時環境・image |
| 実通信 | どのclientがどの経路で成功/失敗したか | 当該clientでの許可された接続試験 |

保存値の変更、構文検査、実通信の試験は別の操作です。GUIに保存済みの値で診断して成功しても、既存コンテナがその値を使っているとは限りません。逆に、稼働中コンテナが古いproxyで通信できても、新しい保存値が正しいとは判断できません。

一般の `env` / `printenv` / `docker inspect` / `docker compose config --environment` をそのまま共有するとproxy認証を含み得ます。まず `proxy-doctor.py` の値を返さない診断を使い、必要な詳細だけ管理者が私有端末で確認します。

## canonical bypassを変更する具体例

会社の承認済み内部APIが増えた場面を想定します。以下のDNS名は説明用であり、そのまま疎通するサーバーではありません。

1. privateの現在のbypass一覧へ `mes-api.example.invalid` を加えます。host編集ではcanonical/大小caseの3項目を同じ一覧にします。
2. `configure.py --upgrade` で必須名と重複除去を確認し、同じprivate設定でplanします。
3. Dokploy管理ではサービス自身のEnvironmentへ同じ設定を登録し、同じアプリでDeployします。CLI管理だけが `up` を使います。
4. 当該APIを利用するclientから、会社の承認した方法で実接続を確認します。固定4先の診断へ任意URLを追加する機能はありません。
5. 不要になった追加先を削除する際、GUIでは希望の一覧を1項目へ明示編集します。host編集では3項目すべてから削除します。片方へ残すとupgradeで戻ります。

内部名 `worker` を削除して外部proxyへ送る試験と、SSRFの許可を外して拒否を確かめる試験は違います。通常構成で必要な内部名は必須リストへ戻ります。保護の受入は [SSRF.md](SSRF.md) の試験を使います。

## 接続が成立するまでの分解

| 段階 | 観測例 | 修正する場所 |
| --- | --- | --- |
| 実行主体の選択 | hostだけ成功、buildだけ失敗 | 成功した主体の設定を他へ適用済みと仮定しない |
| DNS/route | proxyまたは直接宛先を解決できない | 会社DNS・route・VPN・FW |
| proxy認証 | CONNECTで407 | 正式な認証方式とURL userinfo |
| 宛先の許可 | CONNECT拒否、HTTP拒否 | proxy側のdomain/port許可 |
| TLS chain | certificate verify失敗 | そのclientが実際に使うtrust bundle |
| hostname | 証明書の照合名と接続名が不一致 | 正式な接続DNS名とサーバー証明書 |
| 業務認証 | HTTPS自体は成功、アプリが401/403 | API/SQLの用途別資格情報・権限 |

proxyのBasic認証と、接続先APIのBasic/Bearer認証は別です。片方へ正しいpasswordを入れても、他方の401/407は解決しません。HTTP statusだけでなく、どの段階で返された応答かを区別します。

## 新しいサービスを追加する設計者へ

追加サービスは [compose.proxy.yaml.example](../infra/proxy/compose.proxy.yaml.example) を読むだけでは反映されません。build argsとruntime environmentの参照を実サービスへ記述し、内部bypassの必須名、CAの反映方法、private設定の検査を一緒に更新します。公開imageへ企業CAや認証を埋め込まない構成を選びます。

| 確認 | 設計判断 |
| --- | --- |
| base imageの取得者 | Docker daemonかremote builderか。image内CA処理より先に通信する |
| 依存を取得するclient | uv、curl、Maven/JVM等の独自proxy/CA解釈を確認 |
| runtimeの外部通信 | 起動時ダウンロードを必要とするか。固定lockとoffline起動を優先して評価 |
| 内部通信 | service名を固定し、外部入力のURLを無制限にfetchしない |
| CAの撤去 | cacheなしbuildで元baseのtrustへ戻せるか |
| 証跡 | 接続先・認証を隠した成否と、未検証clientを記録できるか |

OS bundleを更新しても、Javaのcacertsや独自のHTTP Sessionがそのbundleを使うとは限りません。実装を変更した時は、同じ固定先の「許可」「拒否」「CA不足」「内部直接通信」をそのclientで試します。上流の仕様変更は、同じtagのsourceとlockを見直して取り込みます。

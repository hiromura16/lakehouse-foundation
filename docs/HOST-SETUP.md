# Linuxサーバーを最初に準備する

<!-- doc-purpose:start -->
> **役割:** 新規Ubuntuで操作端末・権限・Docker・Git・uvの前提を準備する。
>
> **対象読者:** 初めて使う人 / 基盤構築担当
>
> **読むタイミング:** アプリ配置前にhostの状態と不足している前提を確認するとき。
>
> **前提となる資料:** [初めて構築する人の一本道ガイド](<START-HERE.md>)
>
> **次に読む資料:** [会社のプロキシ・CAを設定する](<COMPANY-PROXY.md>) / [導入・検証・停止](<DEPLOYMENT.md>)
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

対象は新規のUbuntu LTS・Linux x86-64のPoCサーバーです。採用互換性の確認はDocker Engine29系/Compose5.6.1、アプリimageは各lockを使います。既存のDocker・Samba・Dokployが動いている場合は再導入せず、確認の節だけを使います。構築全体は [START-HERE](START-HERE.md)、用語は [GLOSSARY](GLOSSARY.md) を参照します。

## 1. 操作用PCと配置先を分ける

Git、Python、Dockerの確認は配置先サーバーの端末で行います。ブラウザーは操作用PCで開けます。操作用PCのlocalhostは配置先ではないため、SSH転送等が必要です。実接続先は私有記録へ保存し、公開手順へ書き戻しません。

**Bash / Zsh共通**

```bash
id
uname -m
cat /etc/os-release
python3 --version
command -v git
docker version
docker compose version
free -h
df -h
```

| 確認 | 成功条件 | 失敗したとき |
| --- | --- | --- |
| OS/CPU | 対象Linuxでx86_64 | ARMや別OSは互換性・image対応を別評価 |
| Python | `python3` が3.12以上 | 会社標準で対応版を追加。OS用Pythonを強制置換しない |
| Docker | ClientとServerが表示 | 未導入、daemon停止、socket権限を分けて確認 |
| Compose | pluginの版を表示 | 単独の旧docker-composeと混同しない |
| disk | Docker data rootとlandingに必要な余力 | clone先だけの空きでは判断しない |

原本、Iceberg metadata/data、旧snapshot、DB、ログ、build cache、backup用の容量を考慮します。全機能のmemory上限合計は実測使用量・推奨RAMではありません。[構成・資源一覧](reports/IMPLEMENTATION-INVENTORY.md)

## 2. Git・apt・uv取得用の通信を先に用意する

会社proxy/CAが必要なら、端末・apt・Git・curlへ会社標準の設定を先に適用します。app用site.envを作る前から必要になる通信です。認証URLを公開コマンド履歴や文書へ貼らず、管理された設定から使います。

ホストのCA追加はネットワーク管理者が承認した公開証明書だけを信頼ストアへ登録します。秘密鍵は使いません。TLS検証無効化や証明書確認の省略を初期導入の回避策にしません。[主体別のproxy/CA設定](COMPANY-PROXY.md)

## 3. Docker未導入の新規Ubuntuだけに導入する

この節は管理者作業です。会社で標準イメージや社内repositoryを用意している場合はそちらを使います。既存containerのあるhostで競合packageを無条件に削除しません。[Docker公式Ubuntu手順](https://docs.docker.com/engine/install/ubuntu/)

新規hostで基本ツールとDocker公式署名鍵を準備する例です。

**Bash / Zsh共通**

```bash
sudo apt-get update
sudo apt-get install ca-certificates curl git python3 python3-venv
sudo install -d -m 0755 /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
```

Dockerのapt sourceが未登録であることを管理者が確認し、対象OSのcodenameとarchitectureを使って登録します。既存のsourceは重複追加しません。

**Bash / Zsh共通**

```bash
DOCKER_ARCH="$(dpkg --print-architecture)"
UBUNTU_CODENAME="$(. /etc/os-release; printf '%s' "$VERSION_CODENAME")"
sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null <<EOF_DOCKER
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $UBUNTU_CODENAME
Components: stable
Architectures: $DOCKER_ARCH
Signed-By: /etc/apt/keyrings/docker.asc
EOF_DOCKER
sudo apt-get update
apt-cache madison docker-ce
apt-cache madison docker-compose-plugin
```

`apt-cache`の実出力から、承認したEngine29系とCompose5.6.1のpackage版文字列を選びます。OSごとのsuffixを推測して固定しません。存在しない場合は社内ミラー・承認版・互換試験の方針を管理者と決めます。

**Bash**

```bash
read -r -p '一覧にあるDocker29系のpackage版全文: ' DOCKER_PACKAGE_VERSION
read -r -p '一覧にあるCompose5.6.1のpackage版全文: ' COMPOSE_PACKAGE_VERSION
sudo apt-get install \
  "docker-ce=$DOCKER_PACKAGE_VERSION" \
  "docker-ce-cli=$DOCKER_PACKAGE_VERSION" \
  containerd.io docker-buildx-plugin \
  "docker-compose-plugin=$COMPOSE_PACKAGE_VERSION"
```

**Zsh**

```zsh
read -r 'DOCKER_PACKAGE_VERSION?一覧にあるDocker29系のpackage版全文: '
read -r 'COMPOSE_PACKAGE_VERSION?一覧にあるCompose5.6.1のpackage版全文: '
sudo apt-get install \
  "docker-ce=$DOCKER_PACKAGE_VERSION" \
  "docker-ce-cli=$DOCKER_PACKAGE_VERSION" \
  containerd.io docker-buildx-plugin \
  "docker-compose-plugin=$COMPOSE_PACKAGE_VERSION"
```

Buildx/containerdも導入記録へ実版を残します。今後apt更新する場合は、Dockerのrestart影響とCompose/BuildKitの検証を伴う変更として扱います。Dokploy installerがDocker package管理まで代行する構成ではありません。

## 4. 通常ユーザーへ承認したDocker操作権を与える

管理者が承認した配置利用者だけに付与します。Dockerグループはroot相当のhost権限なので、全員へ配布したりsocketをworld-writableへ変えたりしません。[公式の導入後設定](https://docs.docker.com/engine/install/linux-postinstall/)

**Bash / Zsh共通**

```bash
getent group docker
sudo usermod -aG docker "$(id -un)"
```

管理者が別利用者として作業している場合は、対象Unix利用者名を確認して指定します。上の例は自分自身を配置担当にする場合です。一度ログアウトしてSSH接続し直し、通常ユーザーで確認します。

**Bash / Zsh共通**

```bash
id -nG
docker version
docker compose version
docker buildx version
docker info --format '{{.DockerRootDir}}'
```

daemon proxy/CAが必要なら [会社proxy手順](COMPANY-PROXY.md) を適用し、Docker再起動が必要な変更は影響を確認して別に実施します。既存hostの再起動をこの確認作業のためだけに行いません。

## 5. uvとPython環境を準備する

uvは0.13.7を使用します。[固定版の公式配布](https://github.com/astral-sh/uv/releases/tag/0.13.7) からOS/architectureに一致するbinaryまたはinstallerを取得し、会社標準の保管/検査方法で導入します。[導入方法の公式説明](https://docs.astral.sh/uv/getting-started/installation/)

新規に導入する通常ユーザー向けの例です。会社proxy/CAの準備後、固定版のinstallerを取得・確認して実行します。既に会社の配布方法で導入済みなら再実行しません。

**Bash / Zsh共通**

```bash
UV_INSTALL_DIR="$(mktemp -d)"
curl -LsSf https://astral.sh/uv/0.13.7/install.sh \
  -o "$UV_INSTALL_DIR/install.sh"
less "$UV_INSTALL_DIR/install.sh"
sh "$UV_INSTALL_DIR/install.sh"
```

installerが表示するPATH設定を確認してログインし直します。会社の配布binaryを使う場合は、その導入先へPATHを通します。確認後、上の一時ディレクトリに秘密がないことを確認して整理します。

**Bash / Zsh共通**

```bash
uv --version
python3 --version
```

uvがPATHに見つからなければ導入先とログインshellを確認し、`sudo pip install`で代替しません。生成器/制御は標準Pythonで動き、SQL抽出・AI・品質検査・図exportは目的別のuv venvへ追加依存を入れます。各手順のlockとPython版を使い分けます。

## 5B. 新規UbuntuでSambaを使う場合だけ準備する

この節はホスト管理者向けで、Windows等からlandingへファイルを置く場合に使います。既存の会社Samba・ドメイン参加・共有アカウントがある場合は、管理担当の方式を継続します。[Ubuntuの導入説明](https://ubuntu.com/tutorials/install-and-configure-samba) と [Sambaアカウントの公式説明](https://www.samba.org/samba/docs/current/man-html/smbpasswd.8.html) を参照します。

**Bash / Zsh共通**

```bash
sudo apt-get update
sudo apt-get install samba smbclient
smbd --version
command -v testparm
command -v pdbedit
systemctl is-active smbd
```

新規ホストでsmbdが停止している場合だけ、管理者が会社の通信制御を確認して `sudo systemctl enable --now smbd` を実行します。共有を外部へ全許可するfirewall設定は使わず、会社の許可クライアントから必要なSMB通信だけを通します。

共有に使う既存Unix利用者を確認します。次は自分を共有担当にする場合の例です。別の利用者なら管理者が対象名を指定します。

**Bash / Zsh共通**

```bash
SMB_SHARE_USER="$(id -un)"
id "$SMB_SHARE_USER"
sudo pdbedit -L -u "$SMB_SHARE_USER"
```

既存Sambaアカウントが表示されたらその資格情報を継続します。検索に失敗しただけで未登録とは判断せず、管理者がDBの読取結果と登録状態を確認します。**未登録だと確認できた場合だけ**次を実行し、端末の対話入力でSMB専用passwordを設定します。既存アカウントに `-a` を使うとpassword変更になるため繰り返しません。

**Bash / Zsh共通**

```bash
sudo smbpasswd -a "$SMB_SHARE_USER"
```

ここまでではレイクハウス共有を作りません。landingを作ってから [会社導入のplan/apply](COMPANY-ROLLOUT.md#2-ランディングを用意し必要ならsambaを設定する) へ進み、専用includeと許可CIDRを設定します。クライアントはファイルエクスプローラーのサーバー接続欄へ、私有のサーバー名と共有名 `Lakehouse-Landing` を入力し、SMB利用者で認証します。接続先名とpasswordは公開文書へ書き戻しません。

許可クライアントからテスト用ファイルの作成・読取・削除を確認します。ファイルコピーの成功とworkerの取込成功は別です。実素材は契約名・列を揃え、コピー完了後にready/hashを作成します。共有を介してS3内部volumeや秘密設定を公開しません。

## 6. この時点で完了したことを確認する

| 完了するもの | ここではまだ完了しないもの |
| --- | --- |
| hostのツール・Docker操作権 | レイクハウスやDokployのDeploy |
| image取得前のproxy/CA経路 | 全runtime/Java/SQLの実疎通 |
| 設置先と資源の確認 | landing/workspaceの準備、DB/鍵/volume |
| uvとPythonの確認 | 各GUIの初期管理者・ワークフロー登録 |

次は [START-HEREのclone/private設定](START-HERE.md#2-公開gitを取得してprivate設定を作る) へ進みます。Sambaは [会社導入](COMPANY-ROLLOUT.md)、Dokploy本体は [初回ガイドのinstall.sh手順](START-HERE.md#4a-dokployで配置する場合)、その上のアプリ配置は [DOKPLOY](DOKPLOY.md) で行います。

## 上級者向け: hostの標準化・更新・復旧

OSの初期構成とアプリのGit履歴を分け、承認したDocker/Compose/Buildx版、daemon設定、data root、時刻同期、firewall、disk監視を会社のhost管理へ記録します。Docker公開portはhost firewallの一般ルールを迂回する場合があるため、loopback bindとDocker-awareな通信制御を実通信で確認します。

Docker data rootの変更、Swarmの再初期化、既存volumeの移動は、この新規導入手順を再実行して解決する作業ではありません。対象、停止点、backup、復元、rollbackを別の変更計画として扱います。利用者認可・HA・資源上限・RPO/RTOも、PoCの単一host成功から自動的に確定しません。

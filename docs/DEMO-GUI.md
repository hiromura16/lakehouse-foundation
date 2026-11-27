# GUIで外部データの処理を確認する

<!-- doc-purpose:start -->
> **役割:** 外部入力の後続処理、接続診断、状態と履歴を確認する。
>
> **対象読者:** 初めて使う人 / データ利用担当
>
> **読むタイミング:** 外部入力を受信後、ポータルから処理を確認するとき。
>
> **前提となる資料:** [初めて構築する人の一本道ガイド](<START-HERE.md>)
>
> **次に読む資料:** [外部入力の受入・隔離を検証する](<ANOMALY-SCENARIOS.md>) / [MES・ERPデータをBronzeからGoldへ整える](<MEDALLION.md>) / [PoC操作ポータル](<POC-PORTAL.md>)
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

確認日: 2026-11-10（JST）。初回配置・作業端末・権限は [START-HERE.md](START-HERE.md) で準備します。以下の `up` はCLI管理の環境だけで使います。Dokploy管理では同じprivate設定とComposeサービスのEnvironmentを揃え、9ファイルを使ってDeployします。[0.31.0の配置手順](DOKPLOY.md)


外部データの取り込み、変換・検証、履歴、状態、参照、接続診断を提供します。外部アプリから入力を届ける方法は [外部データの入口](../README.md#外部データの入口) を参照してください。Docker/Sambaの管理画面やデータ生成画面ではありません。

## 最初に画面を開く

[導入手順](DEPLOYMENT.md) に沿ってprivate設定を生成します。既存の設定がある場合は同じファイルを継続して使い、秘密値を再生成しません。

**Bash / Zsh共通**

```bash
python3 scripts/configure.py --output /absolute/private/site.env
```

private設定で次を確認します。

| 項目 | 指定方法 |
| --- | --- |
| `COMPOSE_PROFILES` | `demo`。監視も使う場合は `demo,monitoring` 等 |
| `DEMO_GUI_PORT` | 既定 `8501` |
| `DEMO_API_PORT` | 既定 `8502` |
| `BIND_IP` | 初期は `127.0.0.1` |
| `LANDING_DIR` | この環境専用の、リポジトリ外のランディング先 |
| `CONTROL_SETTINGS_DIR` | private設定ファイルを置く専用ディレクトリ。生成時に設定される |
| `CONTROL_SETTINGS_NAME` | private設定ファイル名。生成時に設定される |
| `CONTROL_API_TOKEN` | 生成された秘密値を保持。ブラウザーに入力しない |
| `WORKER_UID` / `WORKER_GID` | ランディングとprivate設定を読み書きできる所有者 |

private設定のディレクトリは0700、ファイルは0600とします。APIは設定を安全に置き換えるため、ファイルだけでなく専用ディレクトリを読み書きします。他のアプリの資格情報を含む親ディレクトリを共有しません。ブラウザーUIにはホストディレクトリをmountしません。

**Bash / Zsh共通**

```bash
export LAKEHOUSE_ENV_FILE="/absolute/private/site.env"
./scripts/lakehouse.sh plan
./scripts/lakehouse.sh up
```

サーバー自身で `http://localhost:8501/` を開きます。別端末からはSSHポート転送等を使います。別端末の `localhost` はその端末自身です。画面を広く公開する場合は、利用者認証・TLS・接続制限を用意してからbind先を変更します。

## 外部入力を処理・確認する

1. 外部アプリからLandingまたはSQL Serverの合意済み受け口へデータを届けます。
2. ファイルの場合は [完成印と契約](DEMO-DATA.md) を確認し、「処理・検証」で投入済みファイルを取り込みます。
3. 「履歴」で処理を確認し、「データ」で品質やGoldを参照します。CDCは「状態」でConnector/Consumerを確認します。

## 設定と状態を確認する

「設定」では基本・SQL接続・プロキシ・サービスの項目を分けて編集します。既存の秘密値は返さず、「資格情報を更新する」のpassword欄は空欄なら保持します。「設定を保存」はprivate設定を保存する操作です。必要な再配置まで自動実行するものではありません。画面の「保存済み・再配置待ち」を確認し、配置時のenvとmountも整合させます。

「状態」ではAPIやworkerの到達性等を確認します。配送完了とBronze・Silver・Goldの処理成功は別に確認します。投入後の処理方法は [GUI-WALKTHROUGHS.md](GUI-WALKTHROUGHS.md) と [MEDALLION.md](MEDALLION.md) を参照します。

「処理・検証」では、外部データの配送完了後に次のボタンを選べます。

| ボタン | 実行内容 |
| --- | --- |
| 原本を取り込む | 完成済みファイルをBronzeへ取り込む |
| Silver・Goldまで変換 | 原本の取り込みから派生表の更新まで実行する |
| 基盤の往復検証 | 専用の合成試験データでS3・Iceberg・Trinoを照合する |

操作はジョブIDで受け付け、共通の進捗欄に結果を表示します。処理に時間がかかっても同じボタンを繰り返さず、進捗を更新して確認します。pipelineの途中取消は提供しません。

「AIの準備」ではGoldの対象を選び、表ごとの最大500行をsnapshot ID付きで取得できます。表ごとにmainのsnapshotを固定し、export時checkoutの参照SQLとadapterのハッシュを添えてJSON保存します。このハッシュは実snapshotを生成したSQLの保証ではありません。行数制限があるため、全件集計として扱いません。この操作からモデルを呼び出したり、外部AIへ送信したりしません。

## APIと実装の境界

画面は内部の認証付き操作APIを使い、後続処理をジョブIDで照会します。同じ操作を重ねて送信しません。APIを再起動するとプロセス内の履歴は消えますが、外部原本や永続データは変更されません。

「接続診断」には固定SQL sourceの読取診断があります。「処理・検証」でSQLバッチ、「状態」でCDCやバッチの状態を確認できます。[固定SQL source](SQLSERVER-FIXED-DEMO.md) · [操作API](API.md) · [PoC操作ポータル](POC-PORTAL.md)

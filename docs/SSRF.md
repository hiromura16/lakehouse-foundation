# SSRF対策と内部接続の設定

<!-- doc-purpose:start -->
> **役割:** n8nの宛先制御、内部worker通信、proxyと固定APIの境界を設定する。
>
> **対象読者:** 基盤構築担当 / 開発・設計担当
>
> **読むタイミング:** 内部APIへの接続許可を決めるときや、SSRF制御による拒否を確認するとき。
>
> **前提となる資料:** [サイト設定リファレンス](<CONFIGURATION.md>) / [会社のプロキシ・CAを設定する](<COMPANY-PROXY.md>)
>
> **次に読む資料:** [n8n・Airflow・Grafanaを画面から使う](<GUI-WALKTHROUGHS.md>) / [症状から確認する](<TROUBLESHOOTING.md>)
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

監査日: 2026-11-10（JST）。対象はn8n2.46.7です。SSRFは、画面やワークフローに入れたURLを使って、サーバーから意図しない宛先へ通信させる攻撃です。この基盤では、n8nの宛先制御、固定API、プロキシ、ネットワーク制限をそれぞれ設定します。

## 実行場所と対象

配置担当がレイクハウスホストのrepoルートでprivate `site.env` を編集し、既存の同じCLI/Dokploy配置へ反映します。n8n2.46.7の [固定source](https://github.com/n8n-io/n8n/blob/n8n%402.46.7/packages/%40n8n/config/src/configs/ssrf-protection.config.ts) と、[公式release](https://github.com/n8n-io/n8n/releases/tag/n8n%402.46.7) を照合しました。hostのFW全体や全プログラムの通信を、この設定だけで制御する構成ではありません。

## 最初に設定する値

このリポジトリのn8n構成はSSRF保護を有効にし、同梱workflowが使うDocker内部名 `worker` だけを例外にします。上流n8nの既定は無効なので、上流の既定と本構成の既定は区別してください。[n8n公式の設定一覧](https://docs.n8n.io/deploy/host-n8n/configure-n8n/basic-configuration/use-environment-variables/ssrf-protection)

private `site.env` の設定例です。会社の実接続先を公開Gitへ書きません。

```dotenv
N8N_SSRF_PROTECTION_ENABLED=true
N8N_SSRF_BLOCKED_IP_RANGES=default,100.64.0.0/10
N8N_SSRF_ALLOWED_IP_RANGES=
N8N_SSRF_ALLOWED_HOSTNAMES=worker
N8N_SSRF_BLOCKED_HOSTNAMES=
N8N_SSRF_DNS_CACHE_MAX_SIZE=1048576
```

| 項目 | 本基盤での扱い |
| --- | --- |
| `N8N_SSRF_PROTECTION_ENABLED` | 通常 `true`。内部接続のために全体を無効化せず、必要な例外を追加 |
| `N8N_SSRF_BLOCKED_IP_RANGES` | `default` を保持し、必要な会社の禁止範囲をCIDRで追加 |
| `N8N_SSRF_ALLOWED_IP_RANGES` | 必要な宛先だけ。単一IPv4は `/32`、IPv6は `/128` を検討 |
| `N8N_SSRF_ALLOWED_HOSTNAMES` | URL・portを含まないDNS名。例は `worker`。空欄なら内部名の例外なし |
| `N8N_SSRF_BLOCKED_HOSTNAMES` | 業務上禁止する名前の追加。名前の拒否だけに防御を任せない |
| `N8N_SSRF_DNS_CACHE_MAX_SIZE` | 通常1048576。基盤の入力検査では64KiB〜16MiB |

許可する名前は自分たちが管理するDNSだけにします。hostname許可はIPの拒否より優先されるため、広いwildcardを入れる前に対象を確認します。名前の許可はportやpathの許可まで絞る設定ではありません。同梱workflowの `http://worker:8000/run` を固定し、外部入力から接続先を組み立てない構成を保ちます。[n8n公式の有効化手順](https://docs.n8n.io/deploy/host-n8n/configure-n8n/security/enable-ssrf-protection)

公開IPへのHTTP通信は既定で許可されます。業務上必要な公開ドメインだけへ接続を絞る場合は、会社proxyやFWのegress制御も設定します。

n8n本体は無効なCIDRを警告して飛ばす場合があります。本基盤の `plan` とGUI保存は先にCIDR・hostname・boolean・cacheサイズを検査し、誤入力を拒否します。hostname欄に `http://worker:8000` を入力せず、`worker` とします。全宛先を許可する `*` は基盤の入力検査では受け付けません。

## GUIとDokployで反映する

1. 既存設定は、profileや許可先を増やす前に、通常の配置利用者が `python3 scripts/configure.py --output /absolute/private/site.env --upgrade` を実行します。既存秘密値を保持し、canonical bypassも正規化します。必要な変更がある場合は隣の `.before-upgrade` を作り、既存backupは上書きしません。[更新手順](COMPANY-UPDATE.md)
2. 操作ポータルの設定で「SSRF・外向き通信」を開き、必要な内部名だけを設定して保存します。
3. 保存結果の「反映待ち」を確認します。保存はn8nの再起動ではありません。
4. 管理者がprivate envとDokploy Environmentを一致させて再配置します。CLIで管理する場合は次を実行します。

**Bash / Zsh共通**

   ```bash
   ./scripts/lakehouse.sh --env-file /absolute/private/site.env plan
   ./scripts/lakehouse.sh --env-file /absolute/private/site.env up
   ```

5. 同梱n8n workflowを手動実行し、workerを呼び出せることと、許可しない内部先が拒否されることを両方確認します。

追加の会社APIが必要なら、許可名をカンマ区切りで追記します。例は `worker,mes-api.example.invalid` です。実会社名はprivate envだけに書きます。保護を戻す場合は変更前ファイルの**SSRF項目だけ**を確認して戻し、他の新しい秘密値を古いbackupで上書きしません。

## プロキシ・TLSとは別の設定

| 仕組み | 決めること |
| --- | --- |
| `HTTP_PROXY` / `HTTPS_PROXY` | どのproxyを経由して通信するか |
| `LAKEHOUSE_NO_PROXY` → `NO_PROXY` / `no_proxy` | どの宛先をproxy経由にしないか。必須内部名とprivate追加分を正規化 |
| SSRFのallow/block | アプリがその宛先へ通信してよいか |
| 企業CA・TLS hostname検証 | 通信相手の証明書を信頼できるか |
| FW・egress policy | ネットワークとしてどの通信を通すか |

`worker` がcanonical bypassに含まれていてもSSRFの許可にはなりません。逆にhostnameのSSRF許可だけではproxy経路や企業CAは設定されません。n8nは小文字のproxy変数を優先するため、大文字・小文字を揃えます。会社proxyを経由する場合も、許可済み接続と拒否対象の両方を実際のHTTP Requestノードで確認します。[企業proxy・CA手順](COMPANY-PROXY.md)

## この基盤の他の通信制限

| 部分 | 実装している境界 |
| --- | --- |
| FastAPIの状態・service確認 | サービスごとの固定内部URLだけをprobe。利用者がURLを渡す欄はない |
| 通信診断 | PyPI / Maven / npm / GitHubの固定HTTPS先だけ。任意URL不可、redirect拒否、認証URLを応答へ出さない |
| workerのTrino HTTP処理 | 継続・取消URLのscheme・host・port等で内部originを検査。別originへtoken付き通信を転送しない。pathのallowlistは持たない |
| MCP | operatorが選んだ内部APIへ固定pathだけ。redirect拒否、Bearer認証、Host保護。任意SQL・shell・URL fetchのtoolはない |
| 同梱help | 文書・SVGの許可リスト、サイズ上限、symlink拒否。任意pathを開かず、外部画像の自動読込みを抑止 |
| SQL Server接続 | 管理者がprivate設定する業務用TCP接続。TLS/証明書名検証と用途別DB権限を使う |
| Spark | Master RPCとdriver portはDocker内部だけ。UIはloopbackで、信頼したNotebook/ジョブから利用 |

APIのBearer認証と、MCPのHost検査は別の保護です。`MCP_ALLOWED_HOSTS` はMCPへの**着信**Hostの許可であり、n8nの**発信**hostname許可と混同しません。MCPの既定設定と接続例は [MCP.md](MCP.md) を参照してください。

## 受入と切り分け

| 確認 | 期待する結果 |
| --- | --- |
| 同梱n8nの手動workflow | `worker` への許可された処理が成功 |
| 別の試験workflowから未許可の内部health URLを参照 | HTTP RequestノードがSSRFで拒否。秘密値を付けず試験 |
| `worker` をhostname許可から外して再配置 | 同梱workflowが拒否される。試験後は狭い許可へ戻す |
| 許可先のTLS名不一致・CA欠落 | TLS失敗として扱い、証明書検証を無効化して回避しない |
| proxyだけを経由する会社環境 | 実proxyを通した許可・拒否試験を記録 |

拒否ログの宛先・接続情報はprivateに保管します。実会社の宛先を公開issueへ貼りません。n8nの保護範囲はnodeや実装によって異なるため、独自node・raw socket・DB接続・Sparkの任意コードに、同じHTTP保護が自動適用されるとは扱いません。FWとネットワークの分離、workflow編集権限、資格情報の権限も合わせて運用します。

本書の値は固定したn8n2.46.7の設定sourceと公開Composeを照合しました。公式latest文書との差が出た場合は採用tagを確認します。会社proxy・DNS・node追加後の許可／拒否結果は導入先で記録してください。


検証環境では `automation,monitoring` を有効にし、Manual Trigger → HTTP Request（認証なし）の試験workflowで `http://prometheus:9090/-/ready` を指定して、restrictedエラーを確認しました。これは拒否されることが期待結果です。同じ環境の同梱medallion workflowは `worker` の狭い例外で成功しました。試験workflowはGUIのArchive後にDeleteし、実業務workflowへ混ぜません。


確認の順は、n8nログイン → 手動workflowのworker成功 → 認証情報なしの未許可内部health拒否 → 会社proxy経路で同じ許可／拒否、です。画面上のエラー種類と実行IDを記録し、宛先・token・業務payloadは公開しません。設定を戻した場合も同じアプリを再配置してから再試験します。

## 接続元を区別してから設定する

ブラウザーで開いたURLと、その画面からサーバーが取りに行くURLは別です。例えば自分のPCでn8nを開き、HTTP Requestノードへ `http://worker:8000/health` を入れると、workerへ接続するのはn8nコンテナです。PCのDNSやPCのlocalhostで疎通できた結果を、そのまま使いません。

| 用語・場所 | この基盤での意味 | 最初に確認する人 |
| --- | --- | --- |
| 発信元 | HTTP Requestを実行するn8nコンテナ | workflow作成者 |
| hostname | URLの名前部分。Docker内部名 `worker` など | 配置担当 |
| 解決先IP | DNSが返した実アドレス。名前が同じでも変わり得る | ネットワーク管理者 |
| redirect | 接続先が別URLへ移る応答。最初のURLだけで判断しない | workflow作成者 |
| CIDR | IP範囲の表記。`/32` はIPv4の1宛先 | ネットワーク管理者 |
| 認証 | 接続できた後、呼出先がその操作を許すか | API管理者 |

HTTPの `401` は呼出先の認証拒否です。SSRFのrestrictedエラー、DNS失敗、proxyの `407`、TLS失敗と分けます。`worker` を許可した結果、workerの任意のAPIまで認証なしで使えることにはなりません。実処理には別途worker tokenが必要です。

## 内部APIを一つ追加する変更票の例

新たなAPIを使うときは、接続先名だけでなく「誰が、どの操作を、どの資格情報で呼ぶか」を決めます。次は書式例です。`mes-api.example.invalid` は説明用で、実在する接続先として実行しません。

| 記録欄 | 記入例・判断 |
| --- | --- |
| 操作 | 承認済みのMES試験APIから読取り。最初は副作用のないhealth確認 |
| URLの管理 | 管理者が固定し、フォームや外部Webhookの入力でhost/pathを置換しない |
| SSRF | `worker,mes-api.example.invalid`。既存の必要な `worker` を残す |
| proxy | 同じ宛先を直接接続する方針ならcanonical bypassへ追加。SSRF許可とは別に設定 |
| TLS | 正式な証明書名と企業CA。IPへ置き換えてhostname検証を回避しない |
| 資格情報 | n8nのCredentialsへ保存。試験healthは可能なら認証情報なし |
| 完了条件 | 追加先への許可、別内部先への拒否、既存worker workflow成功 |
| 撤回条件 | 用途廃止・DNS管理終了・資格情報失効時に許可を削除し再配置 |

変更は次の順に進めます。

1. 管理者が接続先とAPIの利用権限を確認します。IP範囲を広く許可する前に、固定名だけで足りるか判断します。
2. 操作ポータルでSSRFの許可名を保存します。proxy経路の変更があれば同じ設定画面で別項目も更新します。CIDRのタイプミスやURL付きの名前で保存が失敗したら、入力を修正します。
3. 配置担当が同じアプリへ反映します。Dokploy管理では既存Environmentとprivateファイルを揃えてDeployし、別名のCLI `up` は行いません。
4. 再配置後に新しい手動workflowで固定URLを確認します。最初の試験に本番の更新APIや本番Webhookを使いません。
5. 許可先の成功と、許可外の拒否を記録します。成功だけで変更を完了にしません。
6. 試験workflowをArchive後にDeleteします。実業務へ残す場合はCredentials、編集者、実行権限、定期起動の有無を別途レビューします。

ホスト名を許可すると、その名前が指すIPの拒否より優先されます。したがって「会社のprivate範囲だから安全」とは判断できません。DNSの管理者が変わる場合も許可の再確認が必要です。n8n公式はredirectとDNS解決も保護対象と説明していますが、この基盤の検証済み事実は上記の固定workflowで確認した範囲です。任意の独自nodeに対する網羅試験とは扱いません。[n8n公式の保護範囲](https://docs.n8n.io/deploy/host-n8n/configure-n8n/security/enable-ssrf-protection)

## 許可・拒否試験の判定を揃える

| 試験 | 期待する観測 | その観測だけでは分からないこと |
| --- | --- | --- |
| 同梱medallion workflowを手動実行 | workerへ認証し、処理結果を確認できる | 他のnodeや任意URLも安全か |
| 認証なしで未許可の `prometheus` healthを指定 | n8nのrestrictedエラー | Prometheus自体のhealth。拒否されるので到達しない |
| 許可した試験APIの証明書を検査 | 正しいCA・hostnameでTLS成功 | API利用権限や返却データの品質 |
| n8n経由だけ失敗、同じホストのcurlは成功 | クライアント差を特定する入口 | 直ちにSSRF誤検知とは断定できない |
| 許可名を除いて再配置し再試験 | 意図した経路が拒否される | DNS・proxy変更後も同じ結果になるか |

未許可先の試験は、`monitoring` が有効な隔離試験環境で `http://prometheus:9090/-/ready` を使えます。HTTP RequestノードのAuthenticationはNone、実行はManual Triggerとし、tokenを添付しません。許可名へ `prometheus` を追加して成功させる必要はありません。単なるタイムアウトや名前解決失敗は、SSRFによる拒否の合格結果と区別します。

## 拡張する開発者の確認点

設定の構文は [scripts/settings.py](../scripts/settings.py)、GUIの公開・保存境界は [control_settings.py](../infra/demo/control_settings.py)、n8nへの受け渡しは [core Compose](../infra/lakehouse/compose.yaml) が入口です。構文検査の試験は [test_control.py](../tests/test_control.py)、秘密値を返さず保存する試験は [test_demo_api.py](../tests/test_demo_api.py) を参照します。

新しいHTTP機能を足すときは、固定接続先を選べる設計を先に検討します。任意URLが必要な設計へ変えるなら、認証だけを追加して終わりにせず、名前解決後の宛先、redirect、proxy経路、tokenの転送範囲、timeout、応答サイズを一緒に設計・試験します。既存のn8n設定がFastAPIやPythonライブラリーへ自動適用されることはありません。保存の入力検査と、配置後の実通信試験をそれぞれ残します。

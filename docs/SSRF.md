# SSRF対策と内部接続の設定

監査日: 2026-11-10（UTC）。対象はn8n2.46.7です。SSRFは、画面やワークフローに入れたURLを使って、サーバーから意図しない宛先へ通信させる攻撃です。この基盤では、n8nの宛先制御、固定API、プロキシ、ネットワーク制限をそれぞれ設定します。

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
| workerのTrino HTTP処理 | 継続・取消URLのoriginとpathを検査。別originへtoken付き通信を転送しない |
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

# 会社プロキシへの切り替え

2026-11-09 作成。自宅では `HTTP_PROXY` / `HTTPS_PROXY` を空欄にして直接接続する。会社では下記の設定箇所を切り分け、必要なサービスにだけ適用する。ホストのDocker・コンテナ設定は変更していない。

## 設定する場所

| 場所 | 対象となる通信 | 設定方法 |
| --- | --- | --- |
| Docker daemon | イメージ取得、レジストリ接続 | systemd の `EnvironmentFile` |
| 各コンテナ | パッケージ取得、Webhook、外部API | Compose / Dokploy の環境変数 |
| ビルド処理 | `RUN pip install` 等 | BuildKit の予約済み proxy build args |
| Javaアプリ | JDKや利用SDKの外部HTTP接続 | JVMのproxyプロパティ、SDK固有設定 |
| TLS証明書 | HTTPS検査プロキシのCA信頼 | ホスト・各イメージ・JVMで個別に登録 |

daemon設定だけではコンテナの外向き通信を設定できない。Docker CLIの `~/.docker/config.json` にあるproxy設定もdaemonの設定とは別物である。[Docker daemon設定](https://docs.docker.com/engine/daemon/proxy/)、[Docker CLI設定](https://docs.docker.com/engine/cli/proxy/)

## 1. 私有ファイルを用意する

リポジトリには `.example` だけを保存する。認証情報を含む実ファイルはリポジトリの外に置き、共有しない。

```bash
./scripts/configure-proxy.sh --init "$HOME/.config/lakehouse-foundation/proxy.env"
```

ファイルをエディターで開き、上段・下段を同じ内容にする。値は引用符なしのリテラル。空白やシェル展開は使わず、ユーザー名・パスワードの予約文字はURLエンコードする。

```dotenv
HTTP_PROXY=http://proxy.company.example:3128
HTTPS_PROXY=http://proxy.company.example:3128
NO_PROXY=localhost,127.0.0.1,::1,sqlserver,lakekeeper,trino,seaweedfs,postgres,kafka,debezium,redis,dokploy,dokploy-postgres,prometheus,grafana,n8n
http_proxy=http://proxy.company.example:3128
https_proxy=http://proxy.company.example:3128
no_proxy=localhost,127.0.0.1,::1,sqlserver,lakekeeper,trino,seaweedfs,postgres,kafka,debezium,redis,dokploy,dokploy-postgres,prometheus,grafana,n8n
```

HTTPSの宛先でも、一般的なHTTP CONNECTプロキシのURLは `http://proxy:port` になる。会社の指定方式に合わせる。PAC/WPAD、NTLM/Kerberos認証の場合は、単純なURLで対応できるかを会社の仕様で確認する。

`NO_PROXY` には実際に使うComposeサービス名、内部ドメイン、内部S3エンドポイント、会社内の接続先アドレス、内部レジストリを追加する。例の `seaweedfs` とは別の名前でS3 APIに接続する場合は、その名前も必要。CIDRやドメイン接尾辞の解釈はクライアントごとに異なるので、内部サービス名と接続先IPを明示し、各アプリで確認する。会社へ移す際には接続先アドレスを実環境の値へ設定する。

## 2. Docker daemonへ適用する

```bash
./scripts/configure-proxy.sh --env-file "$HOME/.config/lakehouse-foundation/proxy.env"
sudo ./scripts/configure-proxy.sh --env-file "$HOME/.config/lakehouse-foundation/proxy.env" --apply
```

`--apply` を付けない動作はplanで、値の検証と配置予定だけを表示する。`--apply` は次の2ファイルのみを配置し、Dockerを再起動しない。

- `/etc/lakehouse-foundation/proxy.env`: root所有、0600
- `/etc/systemd/system/docker.service.d/90-lakehouse-foundation-proxy.conf`: 実値を含まないdrop-in

既存の `daemon.json` にproxy設定がある場合は拒否する。daemon設定が環境変数より優先されるためである。他のdrop-inに競合するproxy設定がないこともローカルで確認する。既存の管理外ファイルは上書きしない。[Dockerの優先順位](https://docs.docker.com/engine/daemon/proxy/)

メンテナンス時間に以下を実行して初めてdaemonへ反映する。Docker再起動に伴う既存コンテナへの影響を事前に確認する。

```bash
sudo systemctl daemon-reload
sudo systemctl restart docker
```

`EnvironmentFile` の値はシェルとして実行されない。本キットではURLエンコードの `%` は1つのまま記載する。systemdの `Environment=` 行へ直接埋め込む方式とは異なる。[systemd環境ファイル](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html)

認証付きURLは環境変数になるため、rootやDocker管理者は参照できる。`systemctl show ... Environment`、`docker inspect`、`docker compose config` の結果をチャットやGitへ貼り付けない。

## 3. コンテナ・Dokploy・ビルドへ適用する

`infra/proxy/compose.proxy.yaml.example` のenvironmentを、外部接続する各サービスへ組み込む。Composeの `--env-file` は補間用であり、それだけでは全コンテナに環境変数を渡さない。例の `x-outbound-proxy` も定義だけなので、各サービスから参照する必要がある。リポジトリのルートから、stack用の私有envとproxy専用envを両方読ませる。後に指定したproxyファイルが同名変数を上書きする。proxyファイルだけを指定するとDB等のstack設定が不足する。

```bash
docker compose --env-file /private/site.env \
  --env-file "$HOME/.config/lakehouse-foundation/proxy.env" \
  -f infra/lakehouse/compose.yaml up -d
```

`scripts/lakehouse.sh --env-file /private/site.env up` を使う場合は、生成された `/private/site.env` 内の同じ6変数を設定する。daemon用のproxy.envだけを書き換えてもsite.envへは転記されない。別ファイル併用時は上記の直接Composeコマンドを使う。現在のstackは外部通信を行うadmin/n8nへproxy変数を渡す。追加する外向きワーカーにも明示的に渡す。

Dokployから動かす場合は、対象ComposeのEnvironment画面へ同じ6変数を登録し、その変数をComposeのenvironmentで参照して再デプロイする。n8nのWebhook・外部API、Airflow/Pythonワーカー、LLMゲートウェイ等を対象にする。プロキシを必要としないDBや内部サービスに一括適用しない。

BuildKitにはproxy用の予約済みbuild argsがある。Compose例の `build.args` から渡し、Dockerfileでは `ARG HTTP_PROXY` や `ENV HTTP_PROXY` として再宣言・保存しない。予約済みproxy引数は通常の履歴・キャッシュから除外されるが、Dockerfileで参照すると取り扱いが変わる。ログに値を表示する処理も避ける。別プロセスのremote BuildKitでベースイメージ取得が必要な場合は、そのdaemon側のproxy/CAも設定する。[BuildKit proxy引数](https://docs.docker.com/build/building/variables/)

## 4. 企業CAとJava

HTTPS検査がある場合、配布されたCAのフィンガープリントを確認し、ホストと各コンテナの信頼ストアへ登録する。Ubuntu系のイメージなら `/usr/local/share/ca-certificates/*.crt` と `update-ca-certificates` を使う。CA登録済みの派生イメージを作り、再作成後も維持する。ホストへの登録だけではコンテナに伝わらない。[DockerのCA設定](https://docs.docker.com/engine/network/ca-certs/)

pipは `PIP_CERT` でCAバンドルを指定できる。必要に応じて `REQUESTS_CA_BUNDLE` / `CURL_CA_BUNDLE` も設定する。TLS検証を無効化する `--trusted-host` や `verify=False` で回避しない。[pip HTTPS証明書](https://pip.pypa.io/en/stable/topics/https-certificates/)

JavaはHTTP(S)_PROXYに一律対応すると仮定しない。標準JDKでは `http.proxyHost` / `http.proxyPort` / `https.proxyHost` / `https.proxyPort` を利用でき、`http.nonProxyHosts` はカンマでなく `|` 区切りになる。Trino、Spark、Debezium、S3 SDKはそれぞれ実装の接続設定を確認する。[JDKのネットワーク設定](https://docs.oracle.com/en/java/javase/25/docs/api/java.base/java/net/doc-files/net-properties.html)

JavaのCAはOS側と別に、既存の公開CAを保持したtruststoreへ企業CAを追加する。`-Djavax.net.ssl.trustStore=/run/certs/cacerts` で指定可能。認証情報を `JAVA_TOOL_OPTIONS` の文字列に埋め込まず、各製品の秘密管理方式を使う。[JSSE truststore](https://docs.oracle.com/en/java/javase/25/security/java-secure-socket-extension-jsse-reference-guide.html)

## 動作確認と戻し方

daemon側は小さな既知イメージのpull、コンテナ側は会社で許可されたHTTPS宛先と内部S3/RESTカタログへの接続を分けて確認する。内部通信がproxyへ出ないこと、CA付きHTTPSが成功することを確認する。現時点では会社proxy未接続のため、会社内での実通信は未検証。

自宅用へ戻すときは私有ファイルの `HTTP_PROXY` / `HTTPS_PROXY` と小文字版を空欄にし、plan、apply、メンテナンス時のDocker再起動を行う。コンテナ側も空欄へ戻して対象だけを再デプロイする。`NO_PROXY` は残す。

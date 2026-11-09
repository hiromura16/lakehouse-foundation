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
HTTP_PROXY=http://proxy.example.invalid:3128
HTTPS_PROXY=http://proxy.example.invalid:3128
NO_PROXY=localhost,127.0.0.1,::1,storage,catalog,catalog-db,trino,worker,n8n,n8n-db,prometheus,grafana,airflow,airflow-db,dokploy,dokploy-postgres
http_proxy=http://proxy.example.invalid:3128
https_proxy=http://proxy.example.invalid:3128
no_proxy=localhost,127.0.0.1,::1,storage,catalog,catalog-db,trino,worker,n8n,n8n-db,prometheus,grafana,airflow,airflow-db,dokploy,dokploy-postgres
```

HTTPSの宛先でも、一般的なHTTP CONNECTプロキシのURLは `http://proxy:port` になる。会社の指定方式に合わせる。PAC/WPAD、NTLM/Kerberos認証の場合は、単純なURLで対応できるかを会社の仕様で確認する。

`NO_PROXY` には実際に使うComposeサービス名、内部ドメイン、内部S3エンドポイント、会社内の接続先アドレス、内部レジストリを追加する。このスタックのS3 APIは `storage`、RESTカタログは `catalog` なので、製品名と接続ホスト名を混同しない。別のエンドポイント名を使う場合は、その名前も必要。CIDRやドメイン接尾辞の解釈はクライアントごとに異なるので、内部サービス名と接続先IPを明示し、各アプリで確認する。会社へ移す際には接続先アドレスを実環境の値へ設定する。

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
docker compose --env-file /absolute/private/site.env \
  --env-file "$HOME/.config/lakehouse-foundation/proxy.env" \
  -f infra/lakehouse/compose.yaml up -d
```

`scripts/lakehouse.sh --env-file /absolute/private/site.env up` を使う場合は、生成された `/absolute/private/site.env` 内の同じ6変数を設定する。daemon用のproxy.envだけを書き換えてもsite.envへは転記されない。別ファイル併用時は上記の直接Composeコマンドを使う。現在のstackはworker/n8nと、任意追加するAirflow/Sparkにproxy変数を渡す。追加する外向きサービスにも明示的に渡す。

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


## 導入時の通信確認を4段階に分ける

会社への導入順序は [COMPANY-ROLLOUT.md](COMPANY-ROLLOUT.md)、設定項目は [CONFIGURATION.md](CONFIGURATION.md) を参照する。端末でブラウザーが開けることと、Dockerのpullやコンテナ内の通信が成功することは別の確認になる。

| 段階 | 現実装で通信する主体 | 最初に確認する箇所 |
| --- | --- | --- |
| source取得 | ホストのGit | 会社標準のGit/端末proxyとCA。Dockerの設定では代用しない |
| image取得 | Docker daemon / remote builder | daemonの環境、registryへの到達、ホストCA。build argsだけでは直らない |
| build | workerのpip、Sparkのapt/curl、Airflowのconstraints/pip | `site.env` の6変数、BuildKit、buildイメージ内のCA |
| runtime | worker/n8n/Airflow/Sparkの各HTTP client | envの反映、内部NO_PROXY、JVM/SDK固有設定、clientのCA |

現DockerfileではworkerのPython依存をPyPIから、SparkのJARをMaven Centralから、Airflowのconstraintsを公式GitHubから取得する。具体的なURLとchecksumはDockerfile/lockに固定されている。会社の許可先一覧には、Docker registry本体だけでなく、その配布/CDNや依存取得先も必要となる場合がある。失敗時は接続先のドメインをローカルで確認し、checksumやTLS検証を外して回避しない。

## 別ファイルを併用する場合の適用範囲

通常のCLI操作は、`site.env` にも同じproxy6変数を設定して `lakehouse.sh` を使う。daemon専用の `proxy.env` が自動的にsite設定へ取り込まれることはない。

Composeを直接実行して2ファイルを補間に使う場合、coreだけでなく任意の追加ファイルも必要な組み合わせで指定する。

```bash
docker compose --env-file /absolute/private/site.env \
  --env-file "$HOME/.config/lakehouse-foundation/proxy.env" \
  -f infra/lakehouse/compose.yaml \
  -f infra/compute/compose.yaml \
  -f infra/orchestration/compose.yaml config --quiet
```

設定検査後の起動には末尾を `up -d --build --wait --wait-timeout 240` にする。これはComposeの起動であり、CLIが行う明示bootstrapやGrafana初回dashboard登録までを代替するものではない。通常はsite.envへproxyを反映してCLIを使い、直接Composeを選ぶ場合は [DEPLOYMENT.md](DEPLOYMENT.md) の初期化手順も確認する。

## CAを追加する作業と秘密値の設定を混同しない

proxy URLをenvへ保存しても、企業CAがコンテナへ自動配布されることはない。現在のComposeには任意の企業CAを一括mountする設定項目を設けていないため、必要なイメージの派生版や会社の信頼済みbase imageを用意し、buildとruntime双方で確認する。

ホストで動くSQL抽出の `SOURCE_SQLSERVER_CA_FILE` はSQL ServerのTLS用であり、HTTP proxyのCAを全コンテナへ配る設定ではない。会社DBへはSQLのTCP経路が必要で、HTTP(S)_PROXYにSQLの宛先を書いてもその経路は作られない。

## 症状から確認箇所を選ぶ

| 症状 | 調べる箇所 |
| --- | --- |
| `docker pull` のみtimeout | daemonのproxy、registry/CDNへの許可、会社側の名前解決 |
| pullは成功し `pip` / `curl` がTLS失敗 | buildイメージのCAとbuild args。ホストCAだけでは不足する |
| coreの内部S3/RESTがproxyへ流れる | `storage,catalog,catalog-db,trino,worker` 等を両caseのNO_PROXYへ追加し、対象を再作成する |
| Pythonは成功しJavaだけ失敗 | JVMのproxy/非proxy指定、S3 SDK設定、Java truststoreを個別に確認 |
| proxy設定を変更したが挙動が変わらない | daemonは明示restart、コンテナは再配置、buildは再buildという反映単位を確認 |
| SQL ServerのTCP接続ができない | DB用の経路・firewall・DNSを確認。HTTP proxyの問題と切り分ける |

会社proxy、CA、外部DBでの成否はその導入先で記録する。公開リポジトリの検証結果や、設定ファイルの構文検査だけで会社経路の成功とはしない。

## 操作画面とCDCを追加した場合

`demo` profileのAPIからworker・health確認への通信は固定した内部URLで行い、HTTP proxyを使わないclientを明示しています。Gold参照はTrino clientを使うため、両caseのNO_PROXYに `trino` を保持します。source素材のSQL投入はSQLのTCP接続です。操作画面のbuildではPyPI依存を取得するため、同じbuild argsと企業CAの準備が必要です。NO_PROXYには追加サービス名 `demo,demo-api,cdc-kafka,cdc-connect,cdc-ui,cdc-consumer` も追記して上下caseを揃えます。

CDCのSQL Server接続はJavaのTLSを使います。`CDC_SQLSERVER_TRUSTSTORE` は `/run/cdc` 配下にmountしたtruststoreの**コンテナ側path**です。hostのPEMや `DEMO_SQLSERVER_CA_FILE` をそのまま流用せず、登録したCA・証明書名・truststore形式をDB担当と確認します。KafkaやConnectの内部宛先は外向きHTTP proxyへ流しません。

GUIでproxy値を保存した場合も、daemonの配置・restart、コンテナ再配置、必要なimage再buildは別の反映操作です。GUIが返す `saved` はその完了を表しません。[API.md](API.md#設定を保存してから反映する)

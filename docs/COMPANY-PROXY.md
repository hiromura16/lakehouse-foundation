# 会社のプロキシ・CAを設定する

この手順は、会社のサーバーへ初めて配置する担当者向けです。ブラウザーで閲覧できること、Gitでcloneできること、Dockerがimageを取得できること、コンテナが外部へ通信できることを分けて確認します。実際の会社proxy・CA・認証方式では未検証です。ローカルの模擬認証proxyとTLS証明書で試験した範囲は末尾に示します。

本書は2026-11-10に公開コードと公式資料を監査しました。通信経路ごとの作業を、レイクハウスを動かすLinuxホストで行います。通常利用者がprivate設定を編集し、`sudo` の付いたホスト設定だけを管理者が実施します。全コマンドの作業ディレクトリはcloneしたリポジトリのルートです。Dokploy管理の場合は、最後の反映先を既存の同じアプリに統一します。

| 実行主体 | 編集・反映するファイル | 完了の確認 |
| --- | --- | --- |
| 通常の配置担当 | リポジトリ外の `site.env` | `lakehouse.sh plan` が終了コード0 |
| Dockerホスト管理者 | daemon用 `proxy.env` とsystemd drop-in、ホストCA | 承認済み保守時間の反映後、固定imageを取得できる |
| 同じ配置担当／Dokploy管理者 | imageと既存アプリ | build成功、health、内部S3/Trino、許可HTTPSの実通信 |

## 1. 接続条件を担当部署へ確認する

| 確認項目 | 決める内容 |
| --- | --- |
| proxy | host、port、HTTP CONNECTかTLS接続のproxyか |
| 認証 | 認証なし、Basic、NTLM/Kerberos、クライアント証明書等 |
| HTTPS検査 | 企業CAの有無、正式な配布元、SHA-256 fingerprint |
| 直接通信 | 内部service、社内DB、registry、社内ドメインの除外先 |
| 許可する外部先 | GitHub/GHCR、各image registryとCDN、PyPI、Maven、npm等 |
| 運用 | Docker再起動の保守時間、CA更新・失効、資格情報の更新担当 |

PAC/WPADのURLを `HTTPS_PROXY` へ書くだけでは使えません。NTLM/KerberosやmTLSが必要な場合は、会社標準の中継・認証方法を各clientと組み合わせて確認します。以下の模擬試験はHTTP CONNECTとBasic認証までです。

## 2. 非公開の設定を準備する

リポジトリのルートで、daemon用のprivateテンプレートを作ります。

```bash
./scripts/configure-proxy.sh --init /absolute/private/proxy/proxy.env
```

生成先のディレクトリは0700、ファイルは0600です。ローカルのエディターで6変数を設定します。実URLや資格情報をREADME・Git・公開ログへ転記しません。

```dotenv
HTTP_PROXY=http://proxy.example.invalid:3128
HTTPS_PROXY=http://proxy.example.invalid:3128
NO_PROXY=localhost,127.0.0.1,::1,storage,catalog,catalog-db,trino,worker
http_proxy=http://proxy.example.invalid:3128
https_proxy=http://proxy.example.invalid:3128
no_proxy=localhost,127.0.0.1,::1,storage,catalog,catalog-db,trino,worker
```

これは形の例です。生成テンプレートには他の内部serviceも入るので、それらを消さず会社の除外先を追加します。HTTPSの宛先でも、通常のCONNECT proxyへの接続URLは `http://proxy:port` です。会社がproxy自体へのTLSを指定する場合だけ `https://proxy:port` とし、利用clientの対応を確認します。

Basic認証では `http://<encoded-user>:<encoded-password>@proxy.example.invalid:3128` という形を使います。`@`、`:`、`%`等の予約文字はuserinfo内でURLエンコードします。引用符、空白、シェル展開を使いません。大文字・小文字の各ペアは同じ値にします。tokenやパスワードをコマンド引数へ直接入力せず、privateファイルを編集します。

daemon用 `proxy.env` は上の**6変数だけ**を受け付けます。`LAKEHOUSE_NO_PROXY` をこのファイルへ加えると検査エラーになります。

アプリ用のprivate `site.env` には、同じHTTP(S)4変数に加え、生成済みの `LAKEHOUSE_NO_PROXY` / `NO_PROXY` / `no_proxy` を保持します。会社の追加除外先はprivate側へ追記し、後述の `--upgrade` で3項目を正規化します。daemon用ファイルとは自動同期しません。GUI保存もdaemon設定やコンテナ再配置を自動実行しません。

## 3. Docker daemonとimage取得を確認する

まずplanを実行します。値の検査だけで、Dockerは再起動しません。

```bash
./scripts/configure-proxy.sh --env-file /absolute/private/proxy/proxy.env --plan
```

Linuxのsystem Dockerへ適用するときだけ、管理者が次を実行します。

```bash
sudo ./scripts/configure-proxy.sh --env-file /absolute/private/proxy/proxy.env --apply
```

キットは `/etc/lakehouse-foundation/proxy.env`（root所有0600）と `/etc/systemd/system/docker.service.d/90-lakehouse-foundation-proxy.conf`（0644）を配置します。配置先の管理外ファイルと `daemon.json` のproxy設定は拒否します。他のsystemd drop-inの内容まで自動照合する機能ではないため、管理者が既存設定との競合も確認します。`daemon.json` のproxy指定は環境変数より優先されるので、両方を別の値で管理しません。rootless DockerやDocker Desktopはこのsystem用キットを流用せず、それぞれの設定場所を使います。[Docker daemon公式](https://docs.docker.com/engine/daemon/proxy/)

HTTPS検査を行う場合、daemonが使うホストの信頼ストアにも企業CAが必要です。Linuxの配置例は次のとおりです。実施前に正式な配布元とfingerprintを照合します。

```bash
openssl x509 -in /absolute/private/certs/company-root.crt -noout -sha256 -fingerprint
sudo install -m 0644 /absolute/private/certs/company-root.crt /usr/local/share/ca-certificates/lakehouse-corporate.crt
sudo update-ca-certificates
```

Docker再起動の影響を確認した保守時間に、管理者が反映します。

```bash
sudo systemctl daemon-reload
sudo systemctl restart docker
```

このリポジトリのツールは上記restartを自動実行しません。対象Composeで使うlock済みimageのpullを確認します。端末の `HTTP_PROXY` だけを設定してpull成功を期待しないようにします。[Dockerのホスト・コンテナCA](https://docs.docker.com/engine/network/ca-certs/)

## 4. buildに企業CAを渡す

private `site.env` の `CORPORATE_CA_FILE` に、公開CAだけを含むPEM chainのホスト側絶対パスを設定します。空欄・未設定では、リポジトリ内の0バイト `infra/proxy/empty-ca.pem` を使い、追加CAなしでbuildします。placeholderへ実際の証明書を書き込みません。秘密鍵・client証明書の秘密鍵・proxy認証URLはこのファイルに入れません。GUIで再build前のCAを診断したい場合は、`CONTROL_SETTINGS_DIR` 配下に公開CAを置きます。APIはその専用ディレクトリ内のfileだけを `/private` 側へ安全に対応付けます。

対象はworker、demo API/GUI、CDC Python処理、Airflow、Spark、Superset、Jupyter、MCPの自作imageです。ComposeのBuildKit secret `corporate_ca` からCAを渡し、[install_ca.py](../infra/proxy/install_ca.py) が依存取得より前に処理します。

- PEM CERTIFICATEだけを検証し、秘密鍵・非PEM・大きすぎる入力を拒否します。
- OSの既存root bundleを保持し、CA chainをhash名で追加します。
- Javaのkeytoolと既存cacertsがあるimageでは、既存entryを削除せずhash aliasを追加します。
- AirflowはrootでCAを追加した後、元のairflowユーザーと公式venvへ戻ります。

CAは公開証明書ですが、**追加した信頼は完成imageに残ります**。企業CA入りimageの配布範囲を決めます。BuildKit secretだから完成imageにCAが残らない、という意味ではありません。認証URLや秘密鍵はCOPYしません。[BuildKit secret公式](https://docs.docker.com/build/building/secrets/)

CAを初めて追加・更新・削除するときは、secretの内容変更だけではbuild cacheが無効にならないため、`--no-cache` で作り直します。[Dockerのsecretとcache](https://docs.docker.com/build/cache/invalidation/#build-secrets)

```bash
./scripts/lakehouse.sh --env-file /absolute/private/site.env plan

env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy \
  -u NO_PROXY -u no_proxy -u LAKEHOUSE_NO_PROXY \
  docker compose --env-file /absolute/private/site.env \
  -f infra/lakehouse/compose.yaml \
  -f infra/compute/compose.yaml \
  -f infra/orchestration/compose.yaml \
  -f infra/demo/compose.yaml \
  -f infra/cdc/compose.yaml \
  -f infra/poc/compose.yaml \
  -f infra/mcp/compose.yaml build --no-cache

./scripts/lakehouse.sh --env-file /absolute/private/site.env up
```

上は全7Composeを指定した例で、有効profileの対象をbuildします。`env -u` はこのコマンドに限って古いshell値の補間を除き、privateファイルを採用します。CLI管理なら同じ配置利用者が `up` まで行います。Dokploy管理ならCLIの `up` を追加実行せず、同じ7ファイル・secret参照・private設定を使う既存アプリでcacheなしの再build／再配置を行います。両方を別名で起動しません。

base imageやuv image自体の取得は、このCA処理より先です。その通信にはdaemon／remote builder側のproxyとCAが必要です。image内のCA追加で、まだpullできないbase imageを取得できるようにはなりません。

## 5. uv・Python・Java・AWSを分ける

uvは0.13.7をtagとdigestで固定しています。標準はMozillaのrootを使い、企業のOS trustを使う場合は `uv --system-certs` または `UV_SYSTEM_CERTS=true` が必要です。`SSL_CERT_FILE` で指定したbundleは既定の信頼元を置き換えるため、企業CAだけでなく必要な公開rootも保持します。[uv公式のTLS設定](https://docs.astral.sh/uv/concepts/authentication/certificates/)

自作imageでは追加後のOS bundleを次へ指定します。

| client | 信頼先／proxyで確認するもの |
| --- | --- |
| uv | `UV_SYSTEM_CERTS=true`、`SSL_CERT_FILE`。本体はrustls系でPython requestsとは別 |
| Python標準ssl/urllib | `SSL_CERT_FILE`とOS bundle |
| Python requests | `REQUESTS_CA_BUNDLE`。独自Sessionやverify指定で変わる場合を確認 |
| curl | `CURL_CA_BUNDLE`とproxy変数 |
| Python AWS/Botocore系 | `AWS_CA_BUNDLE`、SDKのproxy設定。S3 clientごとの適用を確認 |
| n8n / Node.js | `NODE_EXTRA_CA_CERTS`で公開CAを追加。下記のn8n手順を確認 |
| Java/JVM | JREのcacerts、`http(s).proxyHost`等、`http.nonProxyHosts` |
| AWS SDK for Java | 使用HTTP clientとJVM設定・envの優先順位を確認 |

uvの `UV_OFFLINE=1` は完成環境でのパッケージ・Python取得を防ぐ設定です。アプリのHTTP通信全体を禁止するものではありません。build中の依存取得は許可されたproxy経路で行います。

JavaへHTTP(S)_PROXYを設定するだけで、全HTTP clientが動くとは扱いません。JVMの除外は `|` 区切りの `http.nonProxyHosts` であり、カンマ区切りNO_PROXYとは異なります。AWS SDK for JavaはHTTP clientと設定の優先順位にも差があります。認証情報を `JAVA_TOOL_OPTIONS` の文字列へ埋め込まず、製品の秘密設定へ分けます。[JDK公式](https://docs.oracle.com/en/java/javase/25/docs/api/java.base/java/net/doc-files/net-properties.html)、[AWS SDK公式](https://docs.aws.amazon.com/sdk-for-java/latest/developer-guide/http-config-proxy-support.html)

Trino、Lakekeeper、Grafana等の第三者imageへ、このBuildKit secretが一律に適用される構成ではありません。そこへ外向きHTTPS通信を追加する場合は、各製品のproxyとtrust設定を別途確認します。DebeziumのSQL Server TLSは既存のJava truststore設定を使い、HTTP proxy用の設定と区別します。[CDC手順](CDC.md)

### n8nのHTTP Request nodeで社内HTTPSへ接続する

公式n8n imageは再buildせず、同じ `CORPORATE_CA_FILE` を
`/etc/n8n/corporate-ca.pem` へ読み取り専用でbindします。Nodeの
`NODE_EXTRA_CA_CERTS` は既存の公開rootを残し、そのPEM chainを追加します。
自作imageのOS bundle設定とは別の仕組みです。CA未設定・空欄ではこの変数も
空欄になり、追加CAの読み込みと警告を発生させません。
[Node公式の追加CA](https://nodejs.org/api/cli.html#node_extra_ca_certsfile)

指定するのは公開CA証明書だけです。秘密鍵、資格情報、`site.env` を同じmountへ
渡しません。事前に正式なfingerprintを照合し、Nodeユーザーが読めるPEM fileに
します。公開CA自体のmodeは0644で構いません。ホストの保存ディレクトリ全体を
n8nへ渡す必要はありません。

```bash
openssl x509 -in /absolute/private/certs/company-root.crt \
  -noout -sha256 -fingerprint
chmod 0644 /absolute/private/certs/company-root.crt
```

private設定の `CORPORATE_CA_FILE`、HTTP(S)4変数と正規化済みbypass3項目を確認してから、planと再配置を
行います。n8nはCAを起動時に読むため、画面で設定を保存しただけでは反映されません。
CAの内容だけを更新した場合も、保守時間にn8nを再起動／再作成します。既存の
`n8n` / `n8n-db` volumeと暗号鍵は保持します。この方式ではCA用image buildは不要です。

HTTP Request nodeの **Proxy** 欄は通常空欄にし、共通のHTTP(S)_PROXYを使います。
node個別のProxyは共通設定より優先されます。小文字のproxy変数は大文字より優先
されるので、両caseを同じ値に保ちます。内部の `worker` 等はNO_PROXYに残します。
[n8n proxy変数](https://docs.n8n.io/hosting/configuration/environment-variables/deployment/)、
[HTTP RequestのProxy](https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.httprequest/#proxy)

認証なし、またはURL userinfoを使うBasic認証のHTTP CONNECTを想定しています。
API宛てのBasic auth credentialはproxy認証の代用にはなりません。NTLM/Kerberosの
自動認証、PAC/WPAD、proxy自体へのmTLSは同梱設定で対応済みとは扱わず、会社標準の
認証中継や各clientを別途確認します。実会社proxyでn8nのHTTP Requestを実行し、
外向きHTTPSと内部bypassの両方を受け入れ確認してください。

**Ignore SSL Issues** は無効のままにします。独自nodeやcredentialがTLSの `ca` を
明示するとNode標準の追加CAが使われないことがあるため、製品側の設定も照合します。
Code node、外部task runner、独自community nodeの通信まで一律に保証する設定では
ありません。[n8nの証明書検証](https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.httprequest/#ignore-ssl-issues)

## 6. 内部通信をproxyへ送らない

このスタックでは `LAKEHOUSE_NO_PROXY` がCompose補間用の正本です。通常利用者がprivate `site.env` を編集した後、リポジトリのルートで実行します。

```bash
python3 scripts/configure.py --output /absolute/private/site.env --upgrade
./scripts/lakehouse.sh --env-file /absolute/private/site.env plan
```

生成・upgrade・CLI読込は、必須内部名 → 既存canonical → `NO_PROXY` → `no_proxy` の順にprivate値だけを結合し、空白・空要素・大文字小文字を無視した重複を除去します。3項目は同じCSVとなります。標準名にはloopback、実際の全Composeサービス、Dokployの内部名が含まれます。`storage,catalog,trino,worker` を製品名へ置き換えません。

upgradeは他の既存設定・秘密値を保持し、必要な変更があるときだけ元ファイルを隣の `site.env.before-upgrade` に0600で保存します。既存backupを上書きしないため、前回の控えがある場合は内容を確認し、private領域の別の一意な名前へ保管してから再実行します。変更不要ならbackupの有無にかかわらず正常終了します。更新順は [COMPANY-UPDATE.md](COMPANY-UPDATE.md) を参照してください。

CLIは検査済みprivate値で子プロセスの3項目を明示上書きするため、古いshellの除外値を採用しません。Dokploy Git配置ではupgrade後の**3項目を含む同じsite設定**を既存アプリのEnvironmentへ転記します。`LAKEHOUSE_NO_PROXY` 自体を別値でexportしません。

旧ファイルをraw Composeへ直接渡す場合、canonical未設定／空欄でも必須内部名はfallbackで補われ、両caseは一致します。ただし、そのfallbackは重複除去やshellに隠れた旧private追加値の復元を行いません。会社の追加除外先を確実に引き継ぐには先にupgradeします。[Compose公式の補間優先順位](https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/)

GUIでbypassの1項目を編集すると「会社追加分を含む希望の一覧」として扱い、必須名を戻して3項目を同期します。追加先を削除する際はGUIで1項目だけ編集できます。host上の手編集は3項目を揃えてからupgradeしてください。片方に残った追加先は和集合に戻ります。保存結果は `saved=true, applied=false` で、既存アプリの再配置後に初めて反映されます。

直接接続先はURLでなくhost名等を記入します。CIDR・domain suffix・portの解釈はclientごとに異なるため、Java／各SDKの実通信も照合します。SQL ServerはHTTP proxyではなく専用TCP経路を使います。

## 7. 固定先への接続診断を行う

hostのツール用環境もuvで作ります。既存Pythonの絶対パスは導入先に合わせます。

```bash
UV_PYTHON_DOWNLOADS=never uv venv --python /usr/bin/python3.12 /absolute/private/proxy-tools
/absolute/private/proxy-tools/bin/python scripts/proxy-doctor.py \
  --env-file /absolute/private/site.env
```

`--check`なしは通信せず、6変数のpresence・大小文字一致・固定内部serviceのbypass設定だけを返します。値、認証情報、CAの実pathは表示しません。

外向きの試験は許可された固定IDを選びます。任意URLは指定できません。

```bash
/absolute/private/proxy-tools/bin/python scripts/proxy-doctor.py \
  --env-file /absolute/private/site.env \
  --check pypi --check maven --check npm --check github --timeout 5
```

| ID | 固定のHTTPS先 |
| --- | --- |
| `pypi` | `https://pypi.org/simple/` |
| `maven` | `https://repo.maven.apache.org/maven2/` |
| `npm` | `https://registry.npmjs.org/-/ping` |
| `github` | `https://api.github.com/meta` |

診断はHTTP CONNECTとBasic認証に対応し、最大4件を並列に調べます。redirectは追跡せず、status・安全なerror code・所要時間・proxy経由かだけを返します。TLS-to-proxyの `https://proxy` はこの標準ライブラリ診断では未対応と返します。proxy本体へのTLS、NTLM/Kerberos、mTLSが必要な環境では、会社標準clientでも別途確認します。

host診断の成功は、そのhost上のPython通信だけの結果です。コンテナ内の診断、uv build、daemon pull、Java/S3はそれぞれ実行主体が異なります。内部bypassもPythonの設定判定であり、JVMの実通信まで確認した結果ではありません。

操作ハブでは、起動時設定を使う診断と保存済み設定を使う診断を区別します。起動時の確認は完成imageに入ったOS trustを使います。保存済みCAの確認は専用privateディレクトリのPEMをsystem contextへ追加し、公開rootを残して試します。その成功は「その設定でPython診断が通る」ことまでで、daemon・各image・JVMへの反映済みを意味しません。反映には必要な管理者操作と再build／再配置を行います。

## 8. エラーから次の確認を選ぶ

| code／観測 | 確認・操作 |
| --- | --- |
| `PROXY_AUTH_REQUIRED`／407 | userinfoのURL encode、資格情報、会社の認証方式。秘密をログへ出さず確認 |
| `PROXY_CONNECT_REJECTED` | proxyの接続先許可、port、対象ドメイン。blockをTLS無効化で回避しない |
| `TLS_VERIFY_FAILED` | 企業CA・中間CA・証明書名・期限、実行主体のbundle。host成功だけでcontainerも成功としない |
| `TLS_TO_PROXY_NOT_SUPPORTED` | 診断clientの制限。会社が指定したproxy方式を勝手にhttpへ変更しない |
| `DNS_FAILED` | proxy hostまたは直接宛先の名前解決、会社DNS/VPN |
| `TIMEOUT`／`CONNECTION_FAILED` | 経路、firewall、proxy稼働、許可先。繰り返す前に対象の層を確認 |
| `REDIRECT_BLOCKED` | redirect先を管理者が確認。自動で任意hostへ認証情報を転送しない |
| `INVALID_CONFIGURATION` | private設定のURL、CA file、引数を確認。値は公開しない |
| pullのみ失敗 | daemon／remote builder側。コンテナenvでは修正されない |
| uvだけTLS失敗 | system-certs／SSL_CERT_FILE、既定rootの置換を確認 |
| Javaだけ失敗 | cacerts・JVM property・SDK独自client、SQL用truststoreを確認 |
| n8nだけTLS失敗 | CA fileの公開PEM・読取権限・起動時の追加CA、独自nodeのca指定を確認 |
| 内部通信がproxyへ出る | private設定をupgradeしcanonical3項目を確認。Dokploy Environmentにも転記して既存アプリを再配置。Java/SDK独自除外は別に確認 |

## 検証済み範囲と元へ戻す方法

ローカルのループバック上で模擬Basic認証CONNECT proxyとTLS originを起動し、認証＋CAの成功、誤認証407、CA不足、NO_PROXY迂回、redirect拒否、秘密値非表示を試験しています。生成した秘密鍵・証明書は一時領域だけに置き、公開sourceへ含めません。

CA helperは既存root保持、OS bundle再生成後のroot復元、秘密鍵拒否、空入力no-op、Javaの非破壊hash alias追加を一時領域で検証しています。これを会社の実proxy、会社CA、NTLM等の認証試験が済んだ結果として扱いません。

n8nの固定image内のNode子プロセスでは、追加CA未設定・空欄で警告なし、公開CA bundleの読込、存在しないfileでの警告を確認しています。CA未設定・空欄・指定時の全profile Composeも解決し、追加差分がn8n専用envと読み取り専用bindだけであることを照合しています。既存n8nの設定・workflow・dataは変更せず、n8n HTTP Request自体を模擬proxyへ実行した結果ではありません。

戻す場合はprivateのHTTP(S)_PROXYを両caseとも空にし、daemonはplan→apply→管理者の保守時間でrestart、コンテナは再配置します。CAをimageから除くときは `CORPORATE_CA_FILE` を空にし、元のbaseから `--no-cache` で再buildします。稼働中のtruststoreを手作業で削除して対処しません。データvolume・暗号鍵は保持します。


診断の `uv_system_certs` が `null` の場合は、サイト設定だけからimageのbuild設定を判定できない意味です。無効という意味ではありません。自作Dockerfileでは `UV_SYSTEM_CERTS=1` とOS bundleを明示しています。GUIのPython通信診断でuvの依存取得やJava、daemonの成功までを判定しません。


SSRFの設定値、内部workerの例外、NO_PROXYとの違い、GUI保存と再配置、許可・拒否の受入は [SSRF.md](SSRF.md) を参照してください。


## 会社VMの修正取り込みと確定bypass

[COMPANY-UPDATE.md](COMPANY-UPDATE.md) に、ローカル一時修正の退避からGit更新、private設定の正規化、Dokploy再配置、verifyまでの順番を記載しています。`configure.py --upgrade` は内部必須名とprivateの既存3項目をマージして、`LAKEHOUSE_NO_PROXY` / `NO_PROXY` / `no_proxy` を同じ値へ揃えます。Docker Composeは専用値をbuild argsとruntimeへ渡し、古いshellのlegacy値との衝突を避けます。

会社固有値の保持と重複除去には、upgrade後の専用値をDokploy Environmentにも登録します。正規化前のraw Compose fallbackは内部名を補いますが、shellに隠れたenv-fileの追加値を復元する処理ではありません。`LAKEHOUSE_NO_PROXY` 自体を別値でexportせず、同じprivate設定を正本にします。


## 監査した版と一次資料

採用値の正本は各Composeと [uv image lock](../infra/uv/images.lock.json) です。Docker Engine29系／Compose5.6.1はホスト側の監査対象で、リポジトリがホストパッケージを自動更新するものではありません。自作imageのuvは0.13.7、n8nは2.46.7です。

| 一次資料 | 本書で照合した点 |
| --- | --- |
| [Docker29 release notes](https://docs.docker.com/engine/release-notes/29/)、[Compose5.6.1](https://github.com/docker/compose/releases/tag/v5.6.1) | 対象のEngine/Compose系統 |
| [daemon proxy](https://docs.docker.com/engine/daemon/proxy/)、[Build secrets](https://docs.docker.com/build/building/secrets/) | daemonとbuildの反映単位 |
| [uvのTLS設定](https://docs.astral.sh/uv/concepts/authentication/certificates/)、[Nodeの追加CA](https://nodejs.org/api/cli.html#node_extra_ca_certsfile) | clientごとに異なるtrust |
| [n8n2.46.7](https://github.com/n8n-io/n8n/releases/tag/n8n%402.46.7) | 採用するn8n版 |

公式のlatest文書は更新されるので、更新時は固定した版のsourceと新しい挙動を再照合します。

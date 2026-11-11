# 症状から確認する

<!-- doc-purpose:start -->
> **役割:** 観測した症状から設定・起動・通信・job・データの失敗箇所を絞り込む。
>
> **対象読者:** 基盤構築担当 / データ利用担当 / 運用担当
>
> **読むタイミング:** 成功条件を満たさないときや、削除や再生成を行う前に原因を調べるとき。
>
> **前提となる資料:** [初めて構築する人の一本道ガイド](<START-HERE.md>)
>
> **次に読む資料:** [サイト設定リファレンス](<CONFIGURATION.md>) / [平時・更新・復旧の運用手順](<OPERATIONS.md>)
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

2026-11-10監査。最初に対象の環境、配置commit、profile、発生時刻、ジョブIDを確認します。`site.env`、展開済みCompose、DB接続文字列、token、生のドライバー例外を公開ログへ貼り付けません。データ削除や再生成を初期対応にせず、観測した状態と保存先を確認します。

## まず見分ける

| 観測 | 判断と次の操作 |
| --- | --- |
| 画面自体が開かない | bind・ポート・経路・コンテナhealthを確認。別端末のlocalhostは別端末自身 |
| 画面は開くがAPIが使えない | demo-api health、token、private設定のmountと権限を確認 |
| 素材投入が失敗 | ジョブIDとstatusを確認。完了したbatch／manifestを調べ、新規投入を重ねない |
| 素材はあるがGoldがない | 素材生成と処理は別。「処理・検証」または別の正規実行入口を確認 |
| コンテナはhealthyだが件数が違う | source manifest、Bronze、Silver、quarantine、Goldの順に照合 |
| 更新後に初期画面へ戻った | 空volumeや別prefixへ向いていないか確認。再初期化を進めない |

確認はレイクハウスホストのリポジトリのルートで、通常の配置利用者が行います。CLIでは配置と同じprivate設定を選びます。Dokploy管理なら同じアプリのcommit・Environment・Deploymentログ・healthと照合します。調査中に別名の `up` を追加しません。

**Bash / Zsh共通**

```bash
export LAKEHOUSE_ENV_FILE="/absolute/private/site.env"
./scripts/lakehouse.sh plan
./scripts/lakehouse.sh status
```

`plan` は構成検査、`status` は実コンテナの状態です。構成検査だけが成功しても起動成功ではありません。一時的なmigration/init/registerがExited(0)なのは正常な場合があります。

## 更新・Docker29・初回build

| 症状 | 次の確認と期待する結果 |
| --- | --- |
| `site.env.before-upgrade` が既にある | 元控えを確認してprivateな一意の別名へ保管。必要な変更のあるupgradeだけ再実行。控えを削除・上書きして解決しない |
| `Settings already include all current keys` | 正常なno-op。canonical3項目を含め変更不要だった |
| Buildx BakeでCA inputを扱えない | 同じcommitの `infra/proxy/empty-ca.pem` がregular fileで0バイトか確認。CAなしを `/dev/null` へ変更しない |
| CAなしでn8n bind失敗 | checkout欠落、empty-caの存在、`CORPORATE_CA_FILE` の未設定／空欄を確認。存在しないpathをdirectoryとして新規作成しない |
| Docker29でSwarm検査がobject/type error | 配置commitとinstaller helperを更新し、同じ既存Swarmを検査。`swarm leave --force`／再初期化は初期対応にしない |
| raw Composeで内部通信がSquidへ流れる | 下記canonical移行を実施。`--env-file` より古いshell値が優先され得る |
| `up` でCDC registerだけ失敗 | 同名connectorの読戻し差異、HTTP／権限／TLSをprivateに確認。同一設定ならno-op成功する。消して再作成しない |

checkout上のplaceholder確認はホストを変更しません。

**Bash / Zsh共通**

```bash
test -f infra/proxy/empty-ca.pem && test ! -s infra/proxy/empty-ca.pem
./scripts/lakehouse.sh --env-file /absolute/private/site.env plan
```

最初の終了コード0が「存在する空regular file」、planの0が「選択設定の構文検査成功」です。会社更新の順は [COMPANY-UPDATE.md](COMPANY-UPDATE.md) へ進みます。

## DokployのGit clone timeoutとBuildKit auth timeout

| 症状 | 意味・調べる経路 | 次の確認 |
| --- | --- | --- |
| `Failed to connect to github.com port 443` | Dokploy本体からのGit取得がtimeout | daemonとは別に本体のproxy6変数、CA、Git経路を確認 |
| 通常pullは通るが `load metadata` で失敗 | daemon pullとBuildKit client/builderのmetadata/auth経路が異なる可能性 | 同じ本体内のGit・registry・token疎通、builderと同梱CLI版 |
| `failed to authorize: DeadlineExceeded` / `context deadline exceeded` | token取得等が期限内に完了しなかった。原因は確定しない | private switch、proxy/CA適用面、許可先と認証応答を個別比較 |
| `/v2/` がHTTP401 | 未認証レジストリ入口へ到達した | 認証成功ではない。token endpoint、対象imageの権限を別確認 |
| 公開token endpointがHTTP200 | その固定token要求が到達・成功した | token本文は表示せず、実buildを別確認 |
| installerがenv同期後にtimeout／paused | 本体が期待specへ収束していない | 安全なreplicas/task state/error presence、既存volume保持。rawログはprivateに調査 |

[会社proxyの読取診断](COMPANY-PROXY.md#dokploy配置で確認する3つのproxy適用面) と [本体の同期手順](COMPANY-UPDATE.md#dokployサービス自身へproxyとbuildkit設定を同期する) を使います。`DOKPLOY_BUILDKIT_NO_CLIENT_TOKEN=1` は標準名へ変換される**検証未了の対策候補**で、特定障害の解消保証ではありません。0で管理する標準名を除去できます。空欄・true等の値は使いません。通常のpullやcurlの成功だけで「BuildKit対策済み」とは記録しません。

### 正確な固定imageを手動pullする一時回避

導入先で必要性を判断した場合だけ、失敗したbase imageと同じtag・digestをソースのlock/Dockerfileから選びます。次は本リポジトリのuv固定baseを用いる例です。実行後の再Deployが進んでも、キャッシュ依存の一時回避であり、毎回の手動pullを恒久運用にしません。

**Bash / Zsh共通**

```bash
docker pull ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490
```

`latest` や別digestへの置換、認証・TLS無効化、Swarmやvolumeの削除で症状を消さないでください。対策後はキャッシュにない固定baseのmetadata取得まで確認し、ローカルmock検証と会社の実経路の結果を分けます。

## GUI・APIのエラー

| 症状／HTTP | 観測するもの | 判断・操作 |
| --- | --- | --- |
| `401` | CONTROL_API_TOKENの設定有無、UI/APIの配置時env | 同じ環境の同じtokenへ揃えて再配置。既存tokenをブラウザーへ表示させない |
| `422` | 入力の範囲・型、設定項目、DB書き込み許可 | 指図数1～1000、日数1～366、seed範囲、modeと回数、待機合計600秒以内を確認 |
| `409`・workerが実行中／確認待ち | worker healthのbusy／blocked、前回の処理結果 | 新しいジョブは受付前に拒否されている。完了または確認を待つ |
| `409`・別の操作が実行中 | 現在のsource／pipelineジョブとstatus | 終了を待つ。API内の排他で待たされている状態へ新規要求を重ねない |
| `404`・ジョブなし | API再起動の有無、入力したID | ジョブ履歴は永続の監査DBではない。残ったmanifestを確認して判断 |
| `500` | 固定エラーcode、health、所有者・空き容量 | 生例外を公開せず、管理者が非公開ログで原因を確認。即座に再投入しない |
| `503`・設定未準備 | 専用dir0700、env0600、ファイル名、APIユーザー | bind元の実ファイルとCONTROL_SETTINGS_DIR/NAMEを確認 |
| `failed` / `WORKER_RESULT_UNKNOWN` | workerの実行中表示と実際の結果 | APIの応答待ち時間を超えた状態。workerが処理継続中の可能性があるため、自動／即時の再実行をしない |
| `failed` | 完了回数、最後のmanifest、入力先状態 | 成功済みの素材は残り得る。部分成功を確認してから新しいrunを作る |
| `cancelled` | 完了回数、停止要求を出した時点 | 完成済みbatchは正常に残る。取消で既存データが削除されることはない |
| `queued`／`running`が長い | SQL接続のタイムアウト、ジョブ進捗、サービス資源 | 現在batchの完了を待つ。API再起動で強制停止する前にDB側の状態を確認 |

「状態」のworkerにbooleanの `busy` があれば実行中表示として使います。APIのジョブがタイムアウトでfailedになってもworkerの処理が止まったとは限りません。worker自身のhealthで `blocked: true` の場合はTrinoの取消確認待ちであり、残存queryを管理者が確認してから復旧します。新しいpipelineはbusy／blockedの間409で拒否します。pipelineの途中取消は提供していません。[API.md](API.md#ジョブの進捗と取消)

「保存しました」なのに反映しない場合は、`pending_restart`／mount変更の表示を確認します。設定保存はファイル更新までです。現在のAPI token、worker、UID、mount等は再配置後に確認します。Dokployのenvとprivateファイルを別の値のままにしません。

## ランディングが取り込まれない

| 観測 | 判断・操作 |
| --- | --- |
| `.ready`がない | コピー完了後にマーカーを生成。転送途中で作らない |
| SHA-256が一致しない | コピー中の変更・不完全転送・古いmarkerを確認。完成した原本に対して再生成 |
| hashは正しいがスキップされる | 同じ相対pathとhashの処理済み判定かを確認。再実行の重複防止として正常な場合がある |
| 元ファイルが16MiBを超える | 現行workerの1ファイル上限。入力を分割し、sources契約と命名を維持 |
| 100000行を超える・列不一致 | ソース列と型の原本契約を確認。上限回避のため検証を無効化しない |
| CSV／JSONは原本だけ保存され、業務表に入らない | sources.jsonの固定名、またはGUIの正式な増分命名か確認 |
| Permission denied | LANDING_DIR、所有者・グループ、WORKER_UID/GID、共有ユーザーを確認 |
| Sambaでは書けるがGUIでは書けない | APIのrw mountと同じUID/GIDを確認。worker側をrw化して解決しない |

手動ファイルの完成後に、例えば次を実行します。

**Bash / Zsh共通**

```bash
python3 scripts/mark-ready.py /srv/lakehouse/landing/csv/erp_products.csv
```

GUIの正式な増分名は `csv/<table>__<hex12>_<4桁>.csv` またはJSON側の対応名です。任意にsuffixを付けるだけで全ファイルが業務表へ認識されるわけではありません。詳細は [DEMO-GUI.md](DEMO-GUI.md) と [MEDALLION.md](MEDALLION.md) を参照します。

## SQL Serverへダミー投入できない

専用の既存DEMO DB、必要なCREATE SCHEMA／CREATE TABLE／INSERT権限、TLS証明書・hostname、CAのコンテナ内pathを順に確認します。会社参照元の`SOURCE_SQLSERVER_DATABASE`と同名のDBやシステムDBは拒否されます。GUIの書き込み許可を明示しても、この拒否は解除されません。

SQLだけの投入ではCSV／JSONファイルは生成しません。6つのnvarchar staging表とmanifestを確認します。既存の業務schemaへの投入や、DB自体の作成を期待しないようにします。証明書検証を無効にして接続成功と扱いません。

会社参照元への`source-check`成功はTCPの到達性までです。SQL認証・SELECT・証明書・CDCの成功と区別します。[SOURCE-SQLSERVER.md](SOURCE-SQLSERVER.md)

## メダリオン・Gold・AI

| 症状 | 観測・判断・操作 |
| --- | --- |
| Silver件数が原本より少ない | 主キー最新版への重複排除とquarantine件数を確認。単純に欠損とは判断しない |
| Goldが空／作成されていない | 素材投入だけで終了していないか確認し、メダリオンを明示実行 |
| Goldの一部だけ更新 | 表ごとの原子的置換であり全表transactionではない。原因修正後に同じ入力で再実行 |
| GUIを複数回実行すると数量が増える | GUIは別IDの独立素材を追加する仕様。単一fixtureの再実行期待値と区別 |
| AI contextが`409` | Goldの存在とsnapshotを確認。先にメダリオン、次にcontext取得 |
| AIの数字が全件集計と違う | contextのrow_limitとcomplete_dataset=falseを確認。最大500行の抜粋を全件合計にしない |
| snapshotが表ごとに異なる | 各表を独立に固定する契約。全表共通snapshotと扱わない |

AI context取得はモデル実行や外部送信ではありません。根拠JSONの表名・snapshot ID・export側の参照SQL／adapterのhashを保持し、モデルへ渡すデータ範囲は別に決めます。このhashをsnapshot生成版の証明とは扱いません。[AI-READINESS.md](AI-READINESS.md)

## Spark・Airflow・n8n

| 症状 | 観測・判断・操作 |
| --- | --- |
| Sparkが常駐していない | compute profile、Master/Worker/Historyのhealthとshared volume権限を確認。replicas=0で通常起動しないのは一時spark-checkだけ |
| SparkでClassNotFound／NoSuchMethod | Spark line・Scala binary・Iceberg runtime JARの一致、AWS bundleの重複を確認 |
| JARのhash照合失敗 | lockと正規配布物を確認。照合を飛ばして進めない |
| Sparkは書けるがTrinoで読めない | 同じREST catalog／warehouse／S3 endpointを参照しているか、表formatを確認 |
| AirflowのPythonOperator import失敗 | Airflow 3のstandard providerと公式constraintsを確認。古いimport例を混ぜない |
| Airflowへログインできない | state volume内の生成済み管理者passwordを導入先で確認。再生成せず既存stateを保持 |
| Airflow DAGがworkerへ接続できない | lakehouse_worker Connection、token、内部URL、HTTP providerを確認 |
| n8n workflowが`401` | Header AuthのAuthorization/Bearerとprivate worker tokenを確認 |
| n8n credentialsを復号できない | 元のDBとN8N_ENCRYPTION_KEYの組み合わせを確認 |

正しい版・座標・固定JARは [COMPATIBILITY.md](COMPATIBILITY.md) を参照します。Spark本体だけ、Airflow Providerだけの独立更新をせず、対応するスモークを実行します。Airflowの初期passwordが起動ログに含まれる場合があるため、生ログを公開しません。

## プロキシ・image取得・証明書

| 観測 | 判断・操作 |
| --- | --- |
| image pullだけ失敗 | Docker daemon側proxy／CA／認証を確認。コンテナenvだけでは反映しない |
| build内uv／curlだけ失敗 | build args、企業CA、uvのsystem trust、配布元への許可を確認 |
| 実行中Python／SQLだけ失敗 | そのクライアントのproxy／CAと内部NO_PROXYを確認 |
| Java系Spark／Kafka／Debeziumだけ失敗 | JVM truststoreとSDKのproxy設定を確認。Python成功から類推しない |
| 内部service名への通信が外へ流れる | site.envをupgradeしcanonical3項目を同期。Dokploy Environmentへも登録し既存アプリを再配置 |
| GUIだけAPIへ接続しない | DEMO_API_URLとtokenを確認。UI内部API通信は外向きproxyを使わない |

通常利用者がprivateファイルの会社追加先を保持したまま `configure.py --output ... --upgrade` し、`LAKEHOUSE_NO_PROXY` / `NO_PROXY` / `no_proxy` を同期します。CLIはprivate値を子プロセスへ明示し、raw Composeは専用canonicalを使います。古いshell値やdaemon専用proxy.envだけを直しても既存コンテナは変わりません。rawのlegacy fallbackは必須名を補いますが、会社追加先の完全保持にはupgradeが必要です。

会社CA・proxy認証は導入先の方針に合わせます。CAの信頼設定を入れることと、証明書検証を止めることを混同しません。[PROXY.md](PROXY.md)

## CDCが進まない・再開できない

CDCではソースDB、Debezium、Kafka、schema history、offset、取り込み先の状態を分けて確認します。まずconnector statusとcapture対象・権限・TLS・最終進捗を観測し、再登録や新規snapshotを繰り返しません。

再開位置とschema historyを失うと、同じデータの再取得や欠損の判断が必要になります。Connectのoffset/config/status topic、DB schema history、bridgeのcheckpointを一組として扱い、初期対応でtopicやoffsetを消しません。schema変更時は対象表と記録したschemaの整合を確認します。[Debezium SQL Server公式資料](https://debezium.io/documentation/reference/3.7/connectors/sqlserver.html)

SQL ServerではCDCのchange table／LSNの保持とcapture/cleanup・SQL Agentの状態をDBAと確認します。WAL/replication slotはPostgreSQLの仕組みなので、このSQL Server経路の修復手順に混ぜません。停止可能時間はソース側の保持期間と実際のlagから決めます。[Microsoft CDC準備](https://learn.microsoft.com/en-us/sql/relational-databases/track-changes/enable-and-disable-change-data-capture-sql-server?view=sql-server-ver17)

CDCの対象・変換・削除イベント・再実行の保証は、導入したconnectorとbridgeの実装・専用手順に従います。この一般的な切り分けだけで会社DBのCDC実機検証が済んだとは扱いません。

## データが消えたように見える・復旧が必要

1. 変更後のプロジェクト名・DATA_VOLUME_PREFIX・実volume名を照合します。
2. 誤った空volumeへ向いている場合は新規初期化を止めます。元volumeと暗号鍵を保持します。
3. 正しい実体へ戻す場合も、全writerを停止して一つの配置へ切り替えます。
4. バックアップ復元が必要なら、元環境と異なる名前・ポート・volumeへ復元し検証します。

GrafanaのGUI変更はGrafana DBが正本です。初回テンプレートを再importして復旧する前に、そのDBへ正しくmountできているか確認します。[GRAFANA.md](GRAFANA.md)、[OPERATIONS.md](OPERATIONS.md)


SSRFの設定値、内部workerの例外、NO_PROXYとの違い、GUI保存と再配置、許可・拒否の受入は [SSRF.md](SSRF.md) を参照してください。


## 監査対象版と一次資料

対象はDocker Engine29系／Compose5.6.1、n8n2.46.7、Debezium3.7.1.Final／Kafka4.4.0です。SQL Server・Sambaは導入先の版を記録します。版の差を原因候補に含め、固定imageを単独でlatestへ変えて試す手順にはしません。

| 一次資料 | 観測との対応 |
| --- | --- |
| [Docker29](https://docs.docker.com/engine/release-notes/29/)、[Compose補間優先順位](https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/) | Engine互換・古いshellの影響 |
| [Kafka4.4 Connect REST](https://kafka.apache.org/44/kafka-connect/user-guide/#rest-interface) | status/configは参照、offset変更は別の管理操作 |
| [n8n2.46.7固定設定](https://github.com/n8n-io/n8n/blob/n8n%402.46.7/packages/%40n8n/config/src/configs/ssrf-protection.config.ts) | SSRF既定と適用範囲 |
| [Samba testparm](https://www.samba.org/samba/docs/current/man-html/testparm.1.html) | 構文合格とSMB認証・実アクセスの成功は別 |

## 初めて対応するときの、変更しない調査順

「エラーを消す」前に、どの層まで成功したかを切り分けます。同じ症状でも、配置先が違う場合と、処理が途中で止まった場合では復旧手順が変わります。調査中は新しい投入、定期実行、設定の連続変更を止め、既存データを保持します。

| 順序 | 配置担当が見るもの | 成功の意味 | 失敗したら |
| --- | --- | --- | --- |
| 1 | 開いているDokploy Environment／アプリ、配置commit、private設定の選択 | 目的の環境を見ている | 別環境の再起動やデータ操作を中止 |
| 2 | `plan` | 選択値と7Composeの構成検査が通る | missing file、権限、値、profile、portを修正。まだ再配置しない |
| 3 | `status`／Dokployサービス状態 | コンテナの起動状態が分かる | failed initと常駐サービスを区別し、対象ログを非公開で確認 |
| 4 | GUIの「状態」、API `/health` | APIの準備状態や到達性が分かる | mount・token・内部API設定を確認 |
| 5 | 既存ジョブのGETとworkerのbusy／blocked | 再投入できるかを判断する材料が揃う | 不明結果なら既存処理を調べ、POSTを繰り返さない |
| 6 | manifest／原本hash／各層の件数 | 業務処理の成否を判定できる | 入力契約と処理の確定点から調査 |

`plan` の前にDocker自体が使えないなら、[ホスト準備](HOST-SETUP.md) のEngine・Compose・権限を確認します。Dockerグループへの所属後は新しいログインが必要な場合があります。`sudo` で別のホーム・Docker接続先・設定を使い、たまたま起動できた状態を通常運用にしません。

ホストのランディングやprivate設定ディレクトリが存在するか、空き容量があるかだけを確認する例です。repoルートで実行し、private値やファイル内容は表示しません。これはホスト利用者の視点の確認であり、コンテナUIDの読書きやACL、別mount先の同一性まで保証しません。

**Bash / Zsh共通**

```bash
python3 - "$LAKEHOUSE_ENV_FILE" <<'PYTHON'
import json
import shutil
import sys
from pathlib import Path

from scripts.settings import load

values = load(sys.argv[1])
for key in ("LANDING_DIR", "CONTROL_SETTINGS_DIR"):
    directory = Path(values[key])
    exists = directory.is_dir()
    result = {"setting": key, "directory_exists": exists}
    if exists:
        result["mode"] = oct(directory.stat().st_mode & 0o777)
        result["free_bytes"] = shutil.disk_usage(directory).free
    print(json.dumps(result))
PYTHON
```

`directory_exists=false` なら、正しい設定と実体を確認してから [DEPLOYMENT.md](DEPLOYMENT.md) の作成手順へ戻ります。既存ディレクトリへの再帰的な `chmod`／`chown`、存在しないbind元を空ディレクトリで埋める操作は、原因が確定してから行います。空き容量不足では新規writerを止め、保持対象とバックアップを確認します。`docker system prune --volumes` や `down -v` は初期対処にしません。

## 「失敗」の前後に残るものを確認する

| 最後に成功したところ | 残り得る状態 | 再開の判断 |
| --- | --- | --- |
| source jobを受け付けた | ジョブIDのみ、まだ素材なし | GETで終了まで確認。受付応答だけで失敗／成功にしない |
| 有限streamの途中batchが完了 | 完成済み素材とmanifest。後続batchは未作成の場合がある | 完了回数と素材を照合。新規jobは別IDで追加するため二重投入を判断 |
| ファイルだけ到着、ready未到着 | 未取込の原本 | 転送元が閉じたこととhashを確認してからmarker作成 |
| Bronzeまで完了 | 原本オブジェクトと処理済み判定、Silver／Goldは古い場合がある | 同じ入力の処理を確認し、必要なら明示してメダリオンを再実行 |
| メダリオンの一部SQLが完了 | 一部表だけ新しいsnapshot | 残存queryがなくwriterが一つであることを確認し、原因修正後に同じ入力で再実行 |
| CDC state完了、Kafka commit応答失敗 | bridgeのdurable checkpointがbroker表示より進んでいる場合がある | 再開時のstateとbroker範囲を確認。offsetを手動で巻き戻さない |
| GUI/APIが再起動 | ジョブ履歴消失、既存ファイルや外部処理は残る | `404` を未実行と判断しない。manifest・DB・Trino側を照合 |

readyを付け直す操作は、業務表への強制再取込の指定ではありません。workerは相対pathと内容hashも使います。逆に別名へコピーして取込ませると別素材になる場合があります。「もう一度押す」前に [MEDALLION.md](MEDALLION.md) の入力契約と再実行範囲を確認します。

## workerの結果不明・確認待ちを復旧する

APIの待機timeout、workerのエラー、Trinoのquery取消は別の状態です。呼出元の通信が終わってもサーバー側のSQLが終了しているとは限りません。`WORKER_RESULT_UNKNOWN` や `blocked: true` を見たら、次の順で管理者へ引き継ぎます。

1. 対象job ID、操作名、開始・終了時刻、配置commit、返された固定エラーcodeを保存します。会社payloadとtokenは除きます。
2. 新規のGUI実行、外部CLI、n8n、Airflow、Spark、CDCから同じ表へのwriterがないことを確認します。
3. 管理者がTrino側の実行中queryと状態を確認し、残存処理の終了または取消完了を確定します。workerだけを再起動してこの確認を省略しません。
4. 原本hash、処理済み情報、Silver／Goldのsnapshotと件数を照合し、部分完了を記録します。元ファイルを削除して件数を合わせません。
5. 原因を修正してから一つの正規入口で再開します。workerの確認待ちはAPIの「強制解除」ボタンで解消する機能ではありません。CDCのSQLiteに残るblocked状態は [CDC.md](CDC.md) の復旧判断も必要です。

状態管理の実装は [scripts/admin.py](../scripts/admin.py)、APIがbusy／blockedを見て受付を拒否する処理は [infra/demo/api.py](../infra/demo/api.py) にあります。改修時は、正常終了だけでなく応答timeout、取消応答不明、再開時の二重writerを試験します。[worker protocol試験](../tests/test_worker_protocol.py)、[API試験](../tests/test_demo_api.py)

## 再現試験と問い合わせに残す記録

問い合わせの記録は、秘密設定の控えと分離します。次の項目を社内の非公開記録へ残し、公開issueでは合成素材で再現した部分だけを使います。ジョブIDやcommitは秘密値ではなくても、実DB名や実URLと組み合わせた台帳は社内扱いにします。

| 項目 | 記録する内容 |
| --- | --- |
| 期待 | 「同一素材のメダリオン再実行でGold件数不変」など判定できる条件 |
| 実際 | どの層・表・操作で何が違うか。生の業務行は添付しない |
| 対象 | commit、固定image版、選択profile、CLI／Dokployの管理方式 |
| 時間 | timezone付き時刻、source／pipelineのjob ID、開始と最後の進捗 |
| 最後の正常点 | manifestの存在、ready/hash一致、完了したbatch、完了したSQL |
| エラー | HTTP status、固定code、終了コード。例外全文は別のprivate保管 |
| 変更 | 直前の設定・CA・image・schema変更。パスワードそのものは書かない |
| 再現 | 隔離環境、同じseedとscenario、単一writer、最小の操作手順 |
| 実施済み | 読取確認と変更操作を区別し、変更前後の判定を残す |

原因候補は一度に一つずつ変えます。例えばproxy、CA、image版を同時に変更すると、何が原因だったか判定できません。隔離環境で成功したあとも、会社のDNS・proxy・SQL認証・Sambaクライアントは導入先の条件で受け入れます。報告に使う合成データの期待値と、会社DBの実件数を混在させません。

## 固定SQLの診断が拒否する

[固定SQL source](SQLSERVER-FIXED-DEMO.md) のDB・固定schema・installation/version・所有権・限定権限を順に確認します。旧PoCロールや制御schema全体のINSERT/UPDATEを持つwriterは、安全条件を満たさない場合があります。アプリから権限を緩めずDBAに影響を確認します。型不正は原値JSONに残りますが、不正PKは投入を拒否します。driverの生例外はAPIへ返しません。

### SQL増分のbaseline・世代・結果不明

まずcontrol付きsnapshotの `checkpointed=true` と6表のbaselineを確認します。限定control principalがない、3 identityが重複する、checkpointの世代がworkerと違う、全件置換が未完なら増分を強行しません。worker timeoutは終了の証拠ではありません。状態を再確認し、必要ならcontrol付きフルsnapshotで照合します。更新窓の外の遅延commitと物理DELETEは通常増分だけで検出できません。[保証範囲](SQLSERVER-FIXED-DEMO.md#6-watermark増分バッチ)

## 固定SQLのCDCやrebuildが止まる

まず `CDC_SOURCE_MODE` と保存先を確認します。genericの `cdc_*` とfixed-demoの `sqlcdc_*` は別です。Source reader、固定capture、private CA/Java truststore、既存connector設定を順に照合します。空payloadはtombstoneではなく、Raw保存後の解釈エラーとして止まる場合があります。

rebuildの202は完了ではありません。同じrequest IDを再照会し、欠落Raw、offset gap、source identity不一致、query取消未確定を確認します。Rawやstateの削除、イベントのskip、offsetの手動巻戻しを成功への近道にしません。Icebergの複数表は独立commitなので、途中失敗後は利用を止め、保全した履歴から再照合します。[再生条件と保存境界](SQLSERVER-FIXED-DEMO.md#8-current-statereplaygui監視)


## SQL Server error 468 / checker error 51019

003のrole/permission、004/005のcapture/index/column比較でsystem/catalogと対象DBのcollation差が現れる場合、承認済み修正版の両辺BIN2比較を使います。server/database collation変更、`DATABASE_DEFAULT`での回避、安全gate解除はしません。006は読取り監査で同型の異種列競合を確認していません。

失敗後はchecker・role・grant・CDC captureが残っていないと推測せず、新しい専用DBA sessionで読取り専用preflightを行います。51019は未知の同名checkerを保護する停止です。自動DROPや定義の強制更新を行いません。003成功とrole割当が既に確認できた場合は006へ進み、修正版004の承認後に004→006とします。003未確認ならpreflight→必要な003→006で止めて照合します。005は明示的なrollback専用です。[残存診断・Windows sqlcmd・部分完了の再開](SQLSERVER-DDL-RECOVERY.md)

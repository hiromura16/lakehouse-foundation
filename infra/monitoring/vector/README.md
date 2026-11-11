# Vectorのログ正規化・転送設定

<!-- doc-purpose:start -->
> **役割:** 選択版の明示YAML合成、parser期待fixture、権限と配送上限を開発時に照合する。
>
> **対象読者:** 基盤構築担当 / 運用担当 / 開発・設計担当
>
> **読むタイミング:** ログ収集の設定・合成試験・機密境界を確認するとき。
>
> **前提となる資料:** [段階的な監視基盤の配置と受入](<../../../docs/OBSERVABILITY.md>)
>
> **次に読む資料:** [症状から確認する](<../../../docs/TROUBLESHOOTING.md>)
>
> **文書一覧:** [目的・読者から探す文書マニフェスト](<../../../docs/DOCUMENTATION-MANIFEST.md>)
<!-- doc-purpose:end -->

> **目的:** 選択したComposeプロジェクトのログだけを、秘密を含み得るprivate spoolから読み、共通の正規化・伏字処理後にLokiと任意のOpenObserve Labへ送る設計と検証境界を説明します。
> **役割:** 管理者向けの収集手順、設定ファイルの責務、再実行・容量・障害時の制約を定義します。実環境の受入記録ではありません。

この構成はVector **0.61.0-debian**の完成済み公式binaryを使います。
Alloy、Promtail、Docker logging driver plugin、独自Rust実装は追加しません。
既存の`docker logs`と`json-file`の10MiB×3ローテーションを維持します。

## 収集の権限境界

VectorへDocker socketやホストの`/var/log`を渡しません。
管理者が明示的に承認した有限の収集操作だけを[scripts/log_capture.py](../../../scripts/log_capture.py)で実行します。
このツールはサービスを起動せず、権限を付与せず、daemon設定を変更せず、バックグラウンド常駐もしません。
既存のDocker CLI閲覧権限は強い権限であり、このリポジトリの実装だけで新たに付与されることはありません。

`MONITORING_LOG_DIR`はcheckout外の絶対パスです。実際のパスを公開文書へ書かず、private設定で選びます。
事前に所有者が準備・承認した専用ディレクトリだけを使い、ツールはディレクトリ作成・chown・chmodを行いません。
ディレクトリは実行UID所有の0700または0750、出力は同じUID所有の0600とします。
Vectorの実行UIDも`WORKER_UID`に揃えます。ACL、SELinux、mount設定などによる追加の制約は別途確認が必要です。

**Bash / Zsh共通**

```bash
python3 scripts/log_capture.py \
  --env-file "$ENV_FILE" \
  --confirm-capture \
  --minutes 15 \
  --tail 500
```

これは**承認後に実際のログを読み、privateファイルへ書く操作**です。通常deployでは実行しません。
`$ENV_FILE`は管理者が既存private設定を選択した変数です。
会社ログをテストfixtureやGit、公開ログ、PR本文へコピーしません。

選択対象はCompose project、`app.owner=lakehouse-foundation`、固定service allowlistのすべてを満たすコンテナです。
`ps`後にlabelsだけを再検証し、`.Env`や認証設定は読みません。
元のコンテナIDはSHA-256へ変換し、実際のコンテナ名・IDを転送しません。
serviceとreplica番号から安定した`container_alias`を作ります。
別管理のDokploy/Traefik/そのDBや、他プロジェクトは既定で収集しません。
これらのparser参考はmatrixに残しますが、収集許可を意味しません。

実行時間上限は全体60秒、1コマンド2MiB、1回16MiB/10,000行/64コンテナ、1行の元メッセージ16KiBです。
stdout/stderrは別に読み、Docker timestampを先に検証します。
リモートDocker endpointの誤選択を避けるため、空の一時CLI configとlocal default contextを明示します。
ホストのDocker credential store、shellの`DOCKER_HOST`、Proxyやsource DB資格情報を子プロセスへ渡しません。

## ファイルと処理の順序

| ファイル | 役割 |
| --- | --- |
| `entrypoint.sh` | profileを完全一致で選び、必要な設定だけを明示合成 |
| `common.yaml` | private file source、checkpoint、共通transform、内部metrics |
| `transforms/envelope.vrl` | version、project/service、timestamp/streamと許可されたmetadataを検証 |
| `transforms/severity.vrl` | 明示levelとアプリ固有位置だけからseverityを正規化 |
| `common.yaml`の`multiline` | container/stream/process単位で最大32行・1秒の継続行を集約 |
| `transforms/redact.vrl` | 両sinkの前に伏字・field allowlist・event IDを確定 |
| `loki.yaml` | 固定の内部Loki endpoint、JSON encoding、限定label |
| `openobserve.yaml` | lab時だけ読む第二sinkとprivate file secret backend |
| `transforms/openobserve.vrl` | 同じeventにOpenObserve用`_timestamp`だけを追加 |
| `log_parsers.yaml` | バージョン・公式参考・synthetic sample・fallback・未検証事項 |
| `tests/cases.json` | synthetic入力と期待値。実際の測定結果ではない |
| `tests/native-tests.yaml` | `build_fixtures.py`から生成する公式Vector用のassertion |

設定はYAML includeを仮定しません。`--config`を複数回指定して明示合成し、VRLのfile pathは`/etc/vector`を基準に解決します。
一般的な環境変数interpolationやunconfined templateの危険なopt-outは有効にしません。
Loki labelは固定keyと検証済みvalueです。`source_event_id`はbodyにだけ保持します。

Vector stateの`/var/lib/vector`は選択UID:GID・0700である必要があります。
派生imageのbuild時に**新規volumeのcopy-up用**directory metadataを作ります。
既存volumeのowner/mode違いは起動時に拒否し、勝手に修復しません。
管理者による既存volumeの調査・権限是正・applyは別途承認が必要です。

## severityとmultilineの扱い

最終levelは`trace/debug/info/warn/error/fatal/unknown`の7値だけです。
`level_raw`、`level_normalized`、`level_source`、`level_parser`、`level_parse_status`を保持します。
body内の最終`level`も`level_normalized`と一致させます。

優先順は明示JSON level/lvl/severity/severity_text/log.level/levelname、次に各アプリの決まったtext位置、次に文書化された固定prefixです。
`uvicorn.error`のINFO、stderrのINFO、HTTP 404/500、本文のerror/failed、topic名、module/class名からerror/fatalを推測しません。
未認識形式・parse失敗はunknownです。認識できないraw levelは伏字にします。

PostgreSQLのLOG/NOTICEはinfo、WARNINGはwarn、ERRORはerror、FATAL/PANICはfatalです。
Redisのverboseはdebugへ正規化します。対応根拠と暫定形式はmatrixを参照してください。
Trino console JSONは`log.console-format=JSON`が公式設定です。
PostgreSQLへ`logging_collector`変更や再起動を要求してDocker stdoutを失う変更は行いません。

Python/Javaの継続行・最後の例外class行は親levelを継承し、継続行の単語で再計算しません。
`discard` merge strategyで**最初の親**のlevel・timestamp・identityを保持します。
PostgreSQL textはPIDでも分離します。同一process/container/stream内で複数traceが交錯した場合は完全には復元できません。
例外を一つのJSON eventで出す形式を優先してください。
Rust panicや未確認形式はunknownのままです。
32行、1秒、出力4096文字で制限し、切詰めを`truncated`で示します。
4096文字は最悪のJSON escapeでも約24KiBで、固定長metadataを含めてLoki64KiB line上限に余裕を持たせます。

Lokiの自動level推測は無効化し、Grafanaには明示levelを渡します。
これは選択版のsourceに基づく設定です。Grafana13.3での実際の色・filter一致は別の受入試験です。

## 伏字とevent identity

元ログにはsecretがあり得ます。原ファイルはprivateのまま保持し、転送前に許可fieldだけへ再構成します。
Authorization Bearer/Basic、Cookie/Set-Cookie、password/token/API key、SQL接続PWD、AWS/S3 key、camelCase SDK field、URL userinfoを対象にします。
機密assignment/headerを検知したメッセージは部分保持せず全体を伏字にし、severityとscopeを残します。
loggerにも既知のcredential patternを残しません。任意のstructured application fieldは転送しません。
アクセスログのclient/query/任意routeは保持せず、statusはseverityと別fieldです。

Regexは未知のsecret形式を完全には判別できません。
アプリ側でsecretを出さないことが第一の対策です。alias/projectにもsecretや個人識別情報を入れないでください。
synthetic fixtureは伏字の回帰検査であり、会社ログの安全性を証明しません。

`source_event_id`は元container identityのhash、元timestamp、stream、元行position hash、集約本文のhashから決定します。
伏字後の両sinkで同じIDを保持します。retryのたびに新しいUUIDを作りません。
同一container/time/streamで完全に同じ行が複数出る場合は衝突し得ます。
収集窓の重複でも同じeventを再送し得るため、比較・重複検出の補助でありexactly-once保証ではありません。
multilineの切れ目が収集窓や終了timeoutで変わると集約IDも変わります。

## lab無効時・障害時・容量

`openobserve-lab`が無い通常monitoringは`common.yaml`と`loki.yaml`だけを読みます。
OpenObserveのDNS、health、secret backendを参照せず、空のplaceholder tokenは未使用です。
有効時だけComposeのprivate config JSONから`token`を読み、固定username `collector`とのBasic認証に使います。
root/bootstrap passwordを転送用に再利用しません。raw Compose renderもsecret materialなので公開しません。
内部HTTPは公開endpointではなく、ネットワークの全体的な機密性を保証するACLでもありません。

両sinkにそれぞれ512MiB disk buffer、`drop_newest`、最大10retryを設定します。
`retry_max_duration_secs`はretry間の最大待ち時間であり、ジョブ全体のtimeoutではありません。
全体ackは無効です。失敗sinkが満杯の際はdropを許し、無限の保持とlosslessを約束しません。
retry終了による破棄もあり得ます。Loki側も有限保持であり、常時at-least-onceとは説明しません。
OpenObserveのHTTP 2xxは一部record失敗を含み得るため、HTTP送信成功だけでは全recordのquery可能性を証明しません。
source_event_idで送受信件数とquery可視性を照合してください。

spoolは64ファイル×16MiBまでで最大約1GiB、二つのsink bufferは合計約1GiBです。
これにLoki data/WAL、OpenObserve data、元Dockerログ、Prometheus/Grafanaの容量と余裕が必要です。
これは上限・予算であり実測の消費量ではありません。

captureは内容hash名で排他的に発行し、同じ内容の再実行は書込みを省きます。
ファイルを自動削除・上書きしません。満杯は停止し、管理者へreviewを要求します。
publication途中失敗で既にfinal fileがある場合もあり、エラー後はprivate spoolを再検査します。
file sourceはinode+device fingerprintと永続checkpointを使用します。
既存fileの削除・inode再利用・volume喪失・クラッシュ境界での欠落/重複はruntime検証が必要です。
checkpoint確認なしでspoolを消す運用や、元ログがローテーションした後の無限replayはできません。

## 検証と未完了の受入

以下は公式Vector binaryを用いる検証です。privateログや実envを入力せず、public設定とsynthetic fixtureだけを使います。
file pathのためVector設定directoryをworking directoryにします。

**Bash / Zsh共通**

```bash
vector validate --no-environment common.yaml loki.yaml
vector test common.yaml loki.yaml tests/native-tests.yaml
```

fan-out設定の検証には`openobserve.yaml`を追加し、隔離された一時container内だけへsynthetic token JSONを用意します。
本番secretをvalidatorログへ表示しません。
`--no-environment`はruntimeの到達性・permission・永続化・query結果を証明しません。

Pythonのsource/static/capture testsとnative fixtureの整合性チェックは公式VRL実行とは別です。
小さなfixtureの期待値から「parse成功99%以上」「false error/fatal 0件の実測」とは報告しません。
公式Vector実行後に、known-formatの対象数・成功数、unknown対象数、false error/fatal数を記録します。
会社の20〜50行/serviceによる原文→Vector→Loki/OO→UI比較は、private原文を保持し、集計値だけ公開する別途承認の試験です。

現在の環境では公式Vector validator/runtimeを実行できていないため、VRL compile、config validity、multiline flush、実metrics名、checkpoint、独立sinkの進行を完了扱いにしません。
Loki/OpenObserveそれぞれの長時間停止、buffer飽和、disk-full、再起動、復旧重複/欠落、old timestamp拒否、24時間容量/latency測定も未実行です。
承認後の隔離試験でこれらを測定してから会社へ適用します。

## 公式参考

- [Vector0.61.0 source](https://github.com/vectordotdev/vector/tree/v0.61.0)
- [file source](https://vector.dev/docs/reference/configuration/sources/file/)
- [reduceとfirst/last merge strategy](https://vector.dev/docs/reference/configuration/transforms/reduce/)
- [Loki sink](https://vector.dev/docs/reference/configuration/sinks/loki/)
- [HTTP sink](https://vector.dev/docs/reference/configuration/sinks/http/)
- [file secret backend](https://github.com/vectordotdev/vector/blob/v0.61.0/src/secrets/file.rs)
- [Vector unit tests](https://vector.dev/docs/reference/configuration/unit-tests/)
- [OpenObserve Vector ingestion](https://openobserve.ai/docs/ingestion/logs/vector/)
